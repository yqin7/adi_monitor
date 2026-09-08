"""比价（套利机会）接口。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Query

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
    from datetime import datetime, timedelta
    dao = _dao()
    prod = dao.db["products"]

    def span(field: str) -> dict:
        q = {"site": site, field: {"$ne": None}}
        n = prod.count_documents(q)
        if not n:
            return {"count": 0, "newest": None, "oldest": None}
        newest = prod.find_one(q, {field: 1}, sort=[(field, -1)])
        oldest = prod.find_one(q, {field: 1}, sort=[(field, 1)])
        return {"count": n,
                "newest": _iso((newest or {}).get(field)),
                "oldest": _iso((oldest or {}).get(field))}

    # 美国站价格写在 updated_at，其余站写在 scraped_at
    price = span("updated_at") if site == "us" else span("scraped_at")
    sizes = span("sizes_updated_at")

    qf = dao.quote_freshness()
    quotes = {"count": qf.get("total", 0),
              "newest": _iso(qf.get("newest")), "oldest": _iso(qf.get("oldest")),
              "buckets": qf.get("buckets", {})}

    # 本站有多少货号还完全没有得物报价 —— 补全进度
    site_skus = prod.distinct("sku", {"site": site})
    quoted = dao.quotes.count_documents({"sku": {"$in": site_skus}}) if site_skus else 0

    return {"site": site, "server_time": _iso(datetime.utcnow()),
            "adidas_price": price, "adidas_sizes": sizes, "dewu_quotes": quotes,
            "coverage": {"site_skus": len(site_skus), "with_quote": quoted}}


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
    """取实时汇率。open.er-api 为主，frankfurter(ECB) 兜底，都失败则回落配置值。"""
    import requests
    from app.core.config import load_config
    from app.core.pricing import get_pricing_config

    for name, url, pick in (
        ("open.er-api", "https://open.er-api.com/v6/latest/USD",
         lambda d: (d["rates"], d.get("time_last_update_utc", ""))),
        ("frankfurter", "https://api.frankfurter.app/latest?from=USD&to=CNY,HKD,KRW,JPY,GBP,CAD,EUR",
         lambda d: (d["rates"], d.get("date", ""))),
    ):
        try:
            d = requests.get(url, timeout=12).json()
            rates, ts = pick(d)
            cny = rates["CNY"]
            from datetime import datetime as _dt
            out = {"source": name, "updated": ts, "live": True,
                   "fetched_at": _iso(_dt.utcnow()),
                   "usd_cny": round(cny, 4), "usd_hkd": round(rates["HKD"], 4)}
            # 各站本币 -> 人民币，供前端按 site 取用
            for cur in ("KRW", "JPY", "GBP", "CAD", "EUR"):
                if cur in rates and rates[cur]:
                    out[f"{cur.lower()}_cny"] = round(cny / rates[cur], 6)
                    out[f"usd_{cur.lower()}"] = round(rates[cur], 4)
            out["usd_cny_rate"] = round(cny, 4)
            return out
        except Exception:
            continue

    cfg = get_pricing_config(load_config())["fx"]
    return {"source": "config", "usd_cny": cfg["usd_cny"], "usd_hkd": cfg["usd_hkd"],
            "krw_cny": cfg.get("krw_cny", 0.0052), "updated": "", "live": False}


@router.get("/raw", summary="尺码级原始数据（供前端自行试算）")
def raw(site: str = Query("us", description="us 美国 | kr 韩国 | jp 日本 | gb 英国 | ca 加拿大"),
        min_sales: int = Query(0, ge=0), limit: int = Query(8000, ge=1, le=30000)):
    """返回计算利润所需的原始字段，不做任何费用假设。

    前端拿到后可按用户自填的返现/运费/汇率即时重算，无需回服务端。
    """
    from app.core.config import load_config
    from app.core.pricing import get_pricing_config
    from app.core.sizing import in_stock

    db = MongoConnection.from_environment(required=True).db
    dao = ArbitrageDAO(db)
    products = {d["sku"]: d for d in db["products"].find(
        {"site": site},
        {"sku": 1, "name": 1, "category": 1, "orig_price": 1, "sale_price": 1,
         "url": 1, "is_sold_out": 1, "promo_code": 1, "promo_rate": 1,
         "currency": 1, "available_sizes": 1,
         # 新鲜度：美国站价格在 updated_at，其余站在 scraped_at
         "updated_at": 1, "scraped_at": 1, "sizes_updated_at": 1})}

    rows = []
    for q in dao.quotes.find({}):
        quote_at = _iso(q.get("fetched_at"))
        p = products.get(q["sku"])
        if not p:
            continue
        usd = p.get("sale_price") or p.get("orig_price")
        if not usd:
            continue
        for s in q.get("sizes", []):
            price = s.get("globalMinPrice")
            if not price:
                continue
            sales = s.get("globalSoldNum30") or 0
            if sales < min_sales:
                continue
            rows.append({
                "sku": q["sku"], "size": s.get("size"),
                "name": p.get("name"), "category": p.get("category"),
                "url": p.get("url"), "is_sold_out": p.get("is_sold_out", False),
                "site": site, "currency": p.get("currency", "USD"),
                # 该尺码 Adidas 侧是否有货：True/False，拿不到尺码时为 None（未知）
                "in_stock": in_stock(s.get("size"), p.get("available_sizes")),
                "list_usd": p.get("orig_price"), "price_usd": usd,
                "promo_code": p.get("promo_code"), "promo_rate": p.get("promo_rate"),
                # 得物接口返回的就是【香港报价】，结算单位人民币(RMB)。
                # 国内报价 = 香港报价 × 1.09（电商税），由前端换算。
                "dewu_price": price,
                "monthly_sales": s.get("globalSoldNum30"),
                "sales_mom": s.get("globalMonthToMonthRatio"),
                "global_sku_id": s.get("globalSkuId"),
                # ── 数据新鲜度（前端圆点 + 悬停明细用）──
                "adidas_at": _iso(p.get("updated_at") or p.get("scraped_at")),
                "sizes_at": _iso(p.get("sizes_updated_at")),
                "dewu_at": quote_at,
            })
            if len(rows) >= limit:
                break

    cfg = get_pricing_config(load_config())
    return {"site": site, "count": len(rows),
            "sell_cn_fees": cfg["sell_cn"], "sell_hk_fees": cfg["sell_hk"],
            "items": rows}
