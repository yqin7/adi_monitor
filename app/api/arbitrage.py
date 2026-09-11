"""比价（套利机会）接口。"""
from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response

from app.dao.arbitrage_dao import ArbitrageDAO
from app.dao.mongo_client import MongoConnection
from app.services import arbitrage_service

router = APIRouter(prefix="/arbitrage", tags=["比价"])


def _dao() -> ArbitrageDAO:
    return ArbitrageDAO(MongoConnection.from_environment(required=True).db)


def _iso(dt) -> str | None:
    """统一输出带 Z 的 UTC 时间串。

    Mongo 里存的是 naive UTC，直接交给 FastAPI 序列化会丢掉时区标记，
    浏览器 new Date() 会当成本地时间解析 —— 在国内就是 8 小时的偏差。
    """
    if dt is None:
        return None
    if isinstance(dt, str):                     # 历史遗留的字符串格式
        return dt if dt.endswith("Z") else dt + "Z"
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── /raw 结果缓存 ─────────────────────────────────────────────────────
# 组装一次要从 Atlas 搬 14k products + 3.7k quotes，实测 10 秒。
# 数据只在抓取任务写库时才变，所以按「数据指纹」缓存：
# 指纹用两条走索引的轻查询算出来（约 50ms），比重建便宜 200 倍，
# 且抓取一落库指纹就变，不存在读到脏数据的风险。
# 缓存的是【已序列化的字节】而非 dict：1.47MB 的响应每次重新编码要 1.4 秒，
# 而这份内容在下一次抓取落库前是完全不变的。
_RAW_CACHE: dict[str, tuple[tuple, bytes]] = {}
_RAW_CACHE_MAX = 8          # 每份约 1.6MB；key 含用户可控的 limit/min_sales，
                            # 不设上限的话反复变参数能把上百 MB 钉在进程里
_FRESH_CACHE: dict[str, tuple[float, dict]] = {}   # site -> (写入时刻, 结果)
# 两个缓存的 key 都含 URL 参数，站点先过白名单，避免任意字符串把内存撑爆
SITES = ("us", "kr", "jp", "gb", "ca")


def _check_site(site: str) -> str:
    if site not in SITES:
        raise HTTPException(400, f"未知站点 {site}，可选 {'/'.join(SITES)}")
    return site


def _data_stamp(db, site: str) -> tuple:
    """数据指纹。必须覆盖所有会影响 /raw 输出的写入路径。

    曾经漏了 sizes_updated_at：us_sizes_service 只 $set available_sizes 与
    sizes_updated_at，不碰 updated_at，于是尺码刷新后指纹不变、缓存继续
    命中旧的 in_stock —— 库存列要等下一次价格抓取才跟上。
    """
    prod, q = db["products"], db["dewu_quotes"]
    newest_p = prod.find_one({"site": site}, {"updated_at": 1},
                             sort=[("updated_at", -1)]) or {}
    newest_s = prod.find_one({"site": site, "sizes_updated_at": {"$ne": None}},
                             {"sizes_updated_at": 1},
                             sort=[("sizes_updated_at", -1)]) or {}
    newest_q = q.find_one({}, {"fetched_at": 1}, sort=[("fetched_at", -1)]) or {}
    return (str(newest_p.get("updated_at")), str(newest_s.get("sizes_updated_at")),
            prod.count_documents({"site": site}),
            str(newest_q.get("fetched_at")), q.estimated_document_count())


@router.get("", summary="套利机会列表（可按利润/利润率/销量排序）")
def list_opportunities(
    sort_by: str = Query("profit", description="profit | roi | monthly_sales | dewu_price | cost_local"),
    order: str = Query("desc", description="desc | asc"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_profit: float | None = Query(None, description="最低净利"),
    min_roi: float | None = Query(None, description="最低 ROI，0.15 = 15%"),
    min_sales: int | None = Query(None, description="最低 30 天销量"),
    category: str | None = Query(None),
):
    return _dao().query_arbitrage(
        sort_by=sort_by, order=order, limit=limit, offset=offset,
        min_profit=min_profit, min_roi=min_roi, min_sales=min_sales, category=category,
    )


@router.get("/stats", summary="比价数据概览")
def stats():
    dao = _dao()
    total = dao.arb.count_documents({})
    matched = dao.match.count_documents({"found": True})
    missed = dao.match.count_documents({"found": False})
    quotes = dao.quotes.count_documents({})
    top = list(dao.arb.find({}, {"_id": 0, "sku": 1, "size": 1, "name": 1,
                                 "profit": 1, "roi": 1, "monthly_sales": 1})
               .sort([("profit", -1)]).limit(5))
    return {"opportunities": total, "quotes": quotes,
            "matched": matched, "missed": missed, "top5_by_profit": top}


@router.get("/freshness", summary="各数据源的新鲜度（顶栏用）")
def freshness(site: str = Query("us", description="us | kr | jp | gb | ca")):
    """回答「表里这批数据是什么时候扫的」。

    Adidas 价格与尺码是两条独立链路（价格走 PLP / taxonomy，美国站尺码另走一次），
    所以分开统计；得物报价是分批刷的，除了最新最旧还给出分桶，
    好判断「整体可信度」而不是被单个极值误导。
    """
    import time as _time
    from datetime import datetime, timedelta

    # 这个接口只是回答「数据什么时候扫的」，不需要秒级精确；
    # 而它要打十来个 Atlas 往返，每个往返都是几百毫秒。60 秒 TTL 足够。
    _check_site(site)
    cached = _FRESH_CACHE.get(site)
    if cached and _time.time() - cached[0] < 60:
        return {**cached[1], "cached": True}

    dao = _dao()
    prod = dao.db["products"]

    # 三个时间字段一条聚合搞定，替掉原来 6 次往返（每字段 1 count + 2 find_one）
    pfield = "updated_at" if site == "us" else "scraped_at"
    agg = list(prod.aggregate([
        {"$match": {"site": site}},
        {"$group": {
            "_id": None,
            "p_new": {"$max": f"${pfield}"}, "p_old": {"$min": f"${pfield}"},
            "p_cnt": {"$sum": {"$cond": [{"$ifNull": [f"${pfield}", False]}, 1, 0]}},
            "s_new": {"$max": "$sizes_updated_at"}, "s_old": {"$min": "$sizes_updated_at"},
            "s_cnt": {"$sum": {"$cond": [{"$ifNull": ["$sizes_updated_at", False]}, 1, 0]}},
            "total": {"$sum": 1},
        }},
    ]))
    a = agg[0] if agg else {}
    price = {"count": a.get("p_cnt", 0),
             "newest": _iso(a.get("p_new")), "oldest": _iso(a.get("p_old"))}
    sizes = {"count": a.get("s_cnt", 0),
             "newest": _iso(a.get("s_new")), "oldest": _iso(a.get("s_old"))}

    qf = dao.quote_freshness()
    quotes = {"count": qf.get("total", 0),
              "newest": _iso(qf.get("newest")), "oldest": _iso(qf.get("oldest")),
              "buckets": qf.get("buckets", {})}

    # 本站有多少货号已有得物报价 —— 补全进度。
    # 别反过来写：distinct 出 1.4 万个货号再拿去 $in 查 quotes，实测 8.9 秒；
    # 用小集合(3.7k quotes)的货号去 $in 查带索引的 products，是 0.3 秒的事。
    quote_skus = dao.quotes.distinct("sku")
    site_total = a.get("total", 0)
    quoted = (prod.count_documents({"site": site, "sku": {"$in": quote_skus}})
              if quote_skus else 0)

    out = {"site": site, "server_time": _iso(datetime.utcnow()),
           "adidas_price": price, "adidas_sizes": sizes, "dewu_quotes": quotes,
           "coverage": {"site_skus": site_total, "with_quote": quoted}}
    if len(_FRESH_CACHE) >= len(SITES) * 2:
        _FRESH_CACHE.pop(next(iter(_FRESH_CACHE)))
    _FRESH_CACHE[site] = (_time.time(), out)
    return {**out, "cached": False}


@router.post("/refresh", summary="拉取得物报价（后台执行）")
def refresh(background: BackgroundTasks,
            limit: int | None = Query(None, description="本轮最多查询多少个货号"),
            only_discounted: bool = Query(True, description="只查 Adidas 打折商品"),
            include_missed: bool = Query(False, description="是否重试历史未命中货号"),
            max_quote_age_days: int | None = Query(
                None, description="只重查报价超过 N 天的货号，省调用额度")):
    background.add_task(arbitrage_service.refresh_quotes,
                        limit=limit, only_discounted=only_discounted,
                        include_missed=include_missed,
                        max_quote_age_days=max_quote_age_days)
    return {"message": "已在后台开始拉取得物报价", "limit": limit,
            "only_discounted": only_discounted,
            "max_quote_age_days": max_quote_age_days}


@router.post("/compute", summary="重新计算套利机会")
def compute(market: str = Query("CN", description="CN 国内卖 | HK 香港卖"),
            apply_filter: bool = Query(True, description="是否应用利润/ROI/流动性门槛")):
    return arbitrage_service.compute(market=market, apply_filter=apply_filter)


@router.get("/promos", summary="当前促销活动概览（自动发现，措辞变化也能追踪）")
def promos(limit: int = Query(30, ge=1, le=200)):
    """按商品 badge 聚合当前在跑的活动。

    unparsed_badges 列出『看着像促销但没解析出折扣』的原始文案 ——
    Adidas 换措辞时先在这里出现，据此调整解析规则。
    """
    db = MongoConnection.from_environment(required=True).db
    pipe_promo = [
        {"$match": {"promo_text": {"$ne": None}}},
        {"$group": {"_id": {"text": "$promo_text", "code": "$promo_code",
                            "rate": "$promo_rate", "uncertain": "$promo_uncertain"},
                    "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": limit},
    ]
    active = [{"text": r["_id"]["text"], "code": r["_id"]["code"],
               "rate": r["_id"]["rate"], "uncertain": r["_id"].get("uncertain"),
               "products": r["count"]}
              for r in db["products"].aggregate(pipe_promo)]

    pipe_raw = [
        {"$unwind": "$badges"},
        {"$group": {"_id": "$badges", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": limit},
    ]
    all_badges = [{"text": r["_id"], "count": r["count"]}
                  for r in db["products"].aggregate(pipe_raw)]

    unparsed = [{"text": r["_id"], "count": r["count"]}
                for r in db["products"].aggregate([
                    {"$match": {"promo_unparsed": {"$ne": None}}},
                    {"$unwind": "$promo_unparsed"},
                    {"$group": {"_id": "$promo_unparsed", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}}, {"$limit": limit}])]

    total = db["products"].count_documents({})
    with_promo = db["products"].count_documents({"promo_rate": {"$ne": None}})
    return {"total_products": total, "with_promo": with_promo,
            "coverage": round(with_promo / total, 4) if total else 0,
            "active_promos": active,
            "unparsed_badges": unparsed,
            "all_badges": all_badges}


@router.get("/fx", summary="实时汇率（USD 基准）")
def fx():
    """统一走 app.core.fx —— 那里的兜底覆盖全部站点币种。

    此前这里的配置兜底只有 usd_cny / usd_hkd / krw_cny，日英加三站
    拿不到 key，前端就回落成 7.12 当本币汇率，成本虚高上百倍。
    """
    from app.core.fx import get_rates

    r = get_rates()
    return {**r, "fetched_at": _iso(__import__("datetime").datetime.utcnow())}


@router.get("/raw", summary="尺码级原始数据（供前端自行试算）")
def raw(site: str = Query("us", description="us 美国 | kr 韩国 | jp 日本 | gb 英国 | ca 加拿大"),
        min_sales: int = Query(0, ge=0), limit: int = Query(8000, ge=1, le=30000),
        no_cache: bool = Query(False, description="强制重建，跳过缓存")):
    """返回计算利润所需的原始字段，不做任何费用假设。

    前端拿到后可按用户自填的返现/运费/汇率即时重算，无需回服务端。

    响应做了两处瘦身（原来 4.1MB / 10 秒）：
      1. 商品信息放 products 字典，按货号存一份；items 只留尺码级字段。
         此前 name/url/category 会随每个尺码重复，一个 17 码的鞋重复 17 遍。
      2. dewu_quotes 只投影用到的 5 个字段（原文档每个尺码有 14 个）。
    前端 loadData 里再摊平回原来的扁平结构，calc/render 不用改。
    """
    from app.core.config import load_config
    from app.core.pricing import get_pricing_config
    from app.core.sizing import in_stock

    db = MongoConnection.from_environment(required=True).db
    dao = ArbitrageDAO(db)

    _check_site(site)
    stamp = _data_stamp(db, site)
    key = f"{site}:{min_sales}:{limit}"
    if not no_cache:
        hit = _RAW_CACHE.get(key)
        if hit and hit[0] == stamp:
            return Response(content=hit[1], media_type="application/json",
                            headers={"X-Cache": "HIT"})

    # 只取用到的字段：整份 quote 有 14 个尺码字段，前端只用 5 个
    proj = {"sku": 1, "fetched_at": 1, "sizes.size": 1, "sizes.globalMinPrice": 1,
            "sizes.hkMinPrice": 1,
            "sizes.globalSoldNum30": 1, "sizes.globalMonthToMonthRatio": 1,
            "sizes.globalSkuId": 1}
    quote_docs = list(dao.quotes.find({}, proj))

    # 先有报价才有比价行，所以只拉这批商品。
    # 此前是把本站 14,685 个商品全拉下来，其中一万多个没有得物报价、直接丢掉。
    products = {d["sku"]: d for d in db["products"].find(
        {"site": site, "sku": {"$in": [q["sku"] for q in quote_docs]}},
        {"sku": 1, "name": 1, "category": 1, "orig_price": 1, "sale_price": 1,
         "url": 1, "is_sold_out": 1, "promo_code": 1, "promo_rate": 1,
         "currency": 1, "available_sizes": 1,
         "updated_at": 1, "scraped_at": 1, "sizes_updated_at": 1})}

    out_prods: dict[str, dict] = {}
    rows: list[dict] = []
    for q in quote_docs:
        p = products.get(q["sku"])
        if not p:
            continue
        usd = p.get("sale_price") or p.get("orig_price")
        if not usd:
            continue
        quote_at = _iso(q.get("fetched_at"))
        sku = q["sku"]
        if sku not in out_prods:
            out_prods[sku] = {
                "name": p.get("name"), "category": p.get("category"),
                "url": p.get("url"), "is_sold_out": p.get("is_sold_out", False),
                "currency": p.get("currency", "USD"),
                "list_usd": p.get("orig_price"), "price_usd": usd,
                "promo_code": p.get("promo_code"), "promo_rate": p.get("promo_rate"),
                "adidas_at": _iso(p.get("updated_at") or p.get("scraped_at")),
                "sizes_at": _iso(p.get("sizes_updated_at")),
                "dewu_at": quote_at,
            }
        for sz in q.get("sizes", []):
            price = sz.get("globalMinPrice")
            if not price:
                continue
            sales = sz.get("globalSoldNum30") or 0
            if sales < min_sales:
                continue
            rows.append({
                "sku": sku, "size": sz.get("size"),
                # 该尺码 Adidas 侧是否有货：True/False，拿不到尺码时为 None（未知）
                "in_stock": in_stock(sz.get("size"), p.get("available_sizes")),
                # 得物接口返回的就是【香港报价】，结算单位人民币(RMB)。
                # 国内报价 = 香港报价 × 1.09（电商税），由前端换算。
                "dewu_price": price,
                # HKD 口径的同一字段。2026-09-10 之前抓的数据没有这一项，
                # 前端显示为空，等下一轮抓取补上。
                "dewu_price_hk": sz.get("hkMinPrice"),
                "monthly_sales": sz.get("globalSoldNum30"),
                "sales_mom": sz.get("globalMonthToMonthRatio"),
                "global_sku_id": sz.get("globalSkuId"),
            })
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break

    cfg = get_pricing_config(load_config())
    payload = {"site": site, "count": len(rows),
               "sell_cn_fees": cfg["sell_cn"], "sell_hk_fees": cfg["sell_hk"],
               "products": out_prods, "items": rows}
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(_RAW_CACHE) >= _RAW_CACHE_MAX:
        _RAW_CACHE.pop(next(iter(_RAW_CACHE)))       # 简单 FIFO，够用
    _RAW_CACHE[key] = (stamp, body)
    return Response(content=body, media_type="application/json",
                    headers={"X-Cache": "MISS"})
