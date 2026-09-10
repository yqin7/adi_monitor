"""Adidas × 得物 比价服务。

两段流程：
    1. refresh_quotes()  —— 调 dewu_client 拉得物报价+销量，落 dewu_quotes / match_status
    2. compute()         —— 用定价模型算净利/ROI，落 arbitrage（尺码级）

得物命中率实测约 23%（打折鞋类约 52%），所以 match_status 记录未命中货号，
默认跳过，避免每轮浪费大量调用。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

# dewu_client 是隔壁仓库 yqin7/dewu-monitor 提供的包（见 requirements.txt）。
# 不在模块顶层 import：app.main 启动就会连带导入本模块，缺这个依赖时
# 整个服务起不来 —— 而只有得物相关的任务真正需要它，比价页读库就能跑。
def _dc():
    try:
        import dewu_client as dc
        return dc
    except ImportError as exc:
        raise RuntimeError(
            "缺少 dewu_client。安装：pip install "
            "'dewu-client @ git+https://github.com/yqin7/dewu-monitor.git'"
            "（私有仓库，需先配好 GitHub 凭证）") from exc

from app.core.config import load_config
from app.core.pricing import evaluate, get_pricing_config, passes_filter
from app.dao.arbitrage_dao import ArbitrageDAO
from app.dao.mongo_client import MongoConnection

log = logging.getLogger(__name__)

# 得物并发压测（24 批 × 20 id）：
#   并发 2 -> 57 批/分钟，0 限流      并发 4 -> 116 批/分钟，0 限流
#   并发 6 -> 180 批/分钟，17% 限流   并发 8 -> 258 批/分钟，37% 限流
# 取 4：吞吐翻倍且不触发 400010007。
QUOTE_WORKERS = 4
RATE_LIMIT_CODE = "400010007"
MAX_RETRIES = 4
BACKOFF_BASE = 2.0         # 秒，指数退避


def _fetch_one(sku: str, region: str, currency: str) -> tuple[str, dict | None, bool]:
    """返回 (sku, 数据, 是否因限流/异常而失败)。

    第三个值区分『查了但得物没有这个货号』和『没查成功』——后者不能标记为未命中，
    否则会被 match_status 的冷却期挡住 30 天。
    """
    for attempt in range(MAX_RETRIES):
        try:
            r = _dc().query_article_full(sku, region=region, currency=currency)
            return sku, (r if r.get("found") else None), False
        except Exception as exc:
            msg = str(exc)
            if RATE_LIMIT_CODE in msg and attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            log.warning("得物查询失败 %s: %s", sku, msg[:90])
            return sku, None, True
    return sku, None, True


def _catalog_one(sku: str) -> tuple[str, dict | None, bool]:
    """接口140：货号 -> 各尺码 globalSkuId。返回 (sku, info, 是否失败)。"""
    for attempt in range(MAX_RETRIES):
        try:
            dc = _dc()
            info = dc.get_skus_by_article(sku, region=dc.CATALOG_REGION)
            return sku, (info or None), False
        except Exception as exc:
            msg = str(exc)
            if RATE_LIMIT_CODE in msg and attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            log.warning("目录查询失败 %s: %s", sku, msg[:90])
            return sku, None, True
    return sku, None, True


def _chunk(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def refresh_quotes(*, limit: int | None = None, only_discounted: bool = False,
                   include_missed: bool = False, site: str | None = None,
                   max_quote_age_days: int | None = None,
                   fetch_hk: bool = False,
                   region: str = "CN", currency: str = "CNY",
                   progress: Callable[[str], None] | None = None,
                   should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """拉取得物报价并落库。

    三阶段，相比逐货号 3 次调用可省下大部分请求：
      1. 目录 —— 只查 dewu_sku_map 里没有/已过期的货号（接口140，一次一个货号）
      2. 行情 —— 把所有货号的 globalSkuId 汇总后按 20 个一批（接口141/159 的上限）
                 跨货号攒批，避免每个货号只塞 7 个 id 就发一次
      3. 落库 —— 按货号拼回并写 dewu_quotes

    only_discounted    : 只查 Adidas 在打折的商品
    include_missed     : 是否重试历史未命中的货号
    max_quote_age_days : 只重查报价超过 N 天的货号（省得物调用额度）
    fetch_hk           : 是否额外按 HKD 口径再查一次价格。

    关于 fetch_hk：实测港币值 = 人民币值 × 汇率（202 个尺码，均值 1.1665
    对真实汇率 1.1661，差 0.03%），并不带来新信息，前端可用汇率现算。
    但它是接口真值、不依赖我们标定的系数，适合用来复核 1.155 那个换算。
    代价是价格类调用翻倍：全量一轮 6,520 -> 9,780 次，耗时约 72 -> 109 分钟。
    所以默认关闭，需要校验时再打开。
    """
    def report(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    conn = MongoConnection.from_environment(required=True)
    dao = ArbitrageDAO(conn.db)
    dao.ensure_indexes()
    _dc().load_env()

    q: dict[str, Any] = {}
    if only_discounted:
        q["sale_price"] = {"$ne": None}
    if site:
        q["site"] = site
    all_skus = sorted({d["sku"] for d in conn.db["products"].find(q, {"sku": 1})})
    targets = dao.skus_to_refresh(all_skus, include_missed=include_missed,
                                  max_quote_age_days=max_quote_age_days)
    if limit:
        targets = targets[:limit]

    # ── 阶段1：目录（能用缓存就不调接口）────────────────────────────────
    cached = dao.load_sku_maps(targets)
    todo = [s for s in targets if s not in cached]
    stale_note = (f"（只查报价 >{max_quote_age_days} 天的）"
                  if max_quote_age_days is not None else "")
    report(f"候选 {len(all_skus)}，本轮 {len(targets)}{stale_note}；"
           f"目录缓存命中 {len(cached)}，需拉取 {len(todo)}")

    catalogs: dict[str, dict] = {s: cached[s] for s in cached}
    miss = errors = 0
    if todo:
        with ThreadPoolExecutor(QUOTE_WORKERS) as ex:
            futures = [ex.submit(_catalog_one, s) for s in todo]
            for i, fut in enumerate(as_completed(futures), 1):
                if should_stop and should_stop():
                    report("已请求停止，跳过剩余目录查询")
                    for f in futures:
                        f.cancel()
                    break
                sku, info, failed = fut.result()
                if info:
                    dao.save_sku_map(sku, info)
                    dao.mark_match(sku, True)
                    catalogs[sku] = info
                elif failed:
                    errors += 1          # 查询失败，不标未命中，下轮重试
                else:
                    dao.mark_match(sku, False)
                    miss += 1
                if i % 200 == 0:
                    report(f"  目录 {i}/{len(todo)}  命中 {len(catalogs)}  "
                           f"未命中 {miss}  失败 {errors}")

    # ── 阶段2：分组攒批查行情，边查边落库 ──────────────────────────────
    # 每 SAVE_EVERY 个货号为一组：组内 skuId 攒成 20 个一批查价格/销量，
    # 查完整组一次性写 dewu_quotes。分组的意义是「进程中断不会全废」——
    # 用户掉线那次就靠它保住了 4,770 个货号的结果。
    SAVE_EVERY = 200
    skus_sorted = sorted(catalogs)
    total_ids = sum(len(catalogs[s].get("skus", [])) for s in skus_sorted)
    report(f"行情：{len(catalogs)} 个货号 / {total_ids} 个 skuId，"
           f"每 {SAVE_EVERY} 个货号一组查询并写库"
           + ("；同时查 HKD 口径（价格调用翻倍）" if fetch_hk else ""))

    # 重试统计：此前只有「彻底放弃」才写日志，中途退避成功是完全静默的 ——
    # 一组耗时异常时无从判断是不是撞了限流。这里按组累计，落库时一并报出来。
    retry_stat = {"rate_limited": 0, "slept": 0.0, "gave_up": 0}

    def _retrying(fn, c, tag):
        for attempt in range(MAX_RETRIES):
            try:
                return fn(c)
            except Exception as e:
                msg = str(e)
                if RATE_LIMIT_CODE in msg and attempt < MAX_RETRIES - 1:
                    delay = BACKOFF_BASE * (2 ** attempt)
                    retry_stat["rate_limited"] += 1
                    retry_stat["slept"] += delay
                    time.sleep(delay)
                    continue
                # 参数/签名不匹配这类错误必须炸出来。曾经一律吞掉返回 {}，
                # 结果会是「请求量涨了 50%、hkMinPrice 全空、界面只显示 —」
                # 而日志里只有一行 warning，很难联想到是调用方式变了。
                if isinstance(e, (TypeError, TabError, AttributeError)):
                    raise
                retry_stat["gave_up"] += 1
                log.warning("%s失败: %s", tag, msg[:80])
                return {}
        return {}

    def _price(c):
        return _retrying(lambda x: _dc().batch_price(x, region=region, currency=currency),
                         c, "批量价格")

    def _sales(c):
        return _retrying(lambda x: _dc().batch_sales(x), c, "批量销量")

    def _price_hk(c):
        """同一批 skuId 再按 HKD 口径查一次。

        实测 countryCode 传 CN 还是 HK 返回完全相同，变的只有 currency —— 
        所以这里只换 currency。得到的是同一个 globalMinPrice 的港币计价，
        与得物 App 里中国买家看到的实付价数值上几乎相等（49 个尺码实测
        买家端/HK 值均值 0.9886）。存下来供前端直接对照，不再靠系数换算。
        代价是价格类调用翻倍（销量不受影响），整轮约多 50% 请求。
        """
        return _retrying(lambda x: _dc().batch_price(x, region="HK", currency="HKD",
                                                     country_code="HK"),
                         c, "批量价格HK")

    saved = 0
    dewu_size_pairs: list[tuple[str, str]] = []   # 得物侧尺码写法，整轮末尾统一登记
    for gi in range(0, len(skus_sorted), SAVE_EVERY):
        if should_stop and should_stop():
            report(f"已请求停止，已落库 {saved} 个货号")
            break
        t_group = time.time()
        group = skus_sorted[gi:gi + SAVE_EVERY]
        id_to_sku: dict[int, str] = {}
        for sku in group:
            for s in catalogs[sku].get("skus", []):
                gid = s.get("globalSkuId")
                if gid:
                    id_to_sku[int(gid)] = sku
        chunks = list(_chunk(list(id_to_sku), 20))
        if not chunks:
            continue

        prices: dict[int, dict] = {}
        prices_hk: dict[int, dict] = {}
        sales: dict[int, dict] = {}
        with ThreadPoolExecutor(QUOTE_WORKERS) as ex:
            for r in ex.map(_price, chunks):
                prices.update(r)
        if fetch_hk:
            with ThreadPoolExecutor(QUOTE_WORKERS) as ex:
                for r in ex.map(_price_hk, chunks):
                    prices_hk.update(r)
        with ThreadPoolExecutor(QUOTE_WORKERS) as ex:
            for r in ex.map(_sales, chunks):
                sales.update(r)

        batch: list[tuple[str, dict]] = []
        for sku in group:
            info = catalogs[sku]
            sizes = []
            for s in info.get("skus", []):
                gid = s.get("globalSkuId")
                row = {"size": s.get("size"), "globalSkuId": gid,
                       **prices.get(gid, {}), **sales.get(gid, {})}
                if fetch_hk:
                    # 只在真查了的时候才写，否则会用 None 覆盖掉上一轮存下的值
                    hk = prices_hk.get(gid) or {}
                    row["hkMinPrice"] = hk.get("globalMinPrice")
                    row["hkLeakPrice"] = hk.get("leakPrice")
                sizes.append(row)
            payload = {k: v for k, v in info.items()
                       if k not in ("skus", "_id", "fetched_at")}
            payload.update({
                "found": True, "region": region, "currency": currency,
                "country": region, "sizes": sizes,
                "totalSoldNum30": sum(x.get("globalSoldNum30") or 0 for x in sizes),
            })
            batch.append((sku, payload))
            dewu_size_pairs.extend((z.get("size"), sku) for z in sizes if z.get("size"))
        # 整组一次写完。逐条 update_one 对 Atlas 是每条一个往返，200 条要 52 秒。
        dao.save_quotes(batch)
        saved += len(batch)
        extra = ""
        if retry_stat["rate_limited"] or retry_stat["gave_up"]:
            extra = (f"（限流重试 {retry_stat['rate_limited']} 次，"
                     f"累计退避 {retry_stat['slept']:.0f}s，放弃 {retry_stat['gave_up']} 次）")
            retry_stat.update(rate_limited=0, slept=0.0, gave_up=0)
        # 说明「已完成」而不是「已落库」：这一组的耗时绝大部分花在查得物接口
        # （每 20 个 skuId 一批、价格与销量各一次），写库只占很小一部分。
        report(f"  已完成 {saved}/{len(catalogs)} 个货号 "
               f"[本组 {time.time() - t_group:.0f}s]{extra}")

    report(f"报价补全完成：命中 {saved}，未命中 {miss}，失败 {errors}")

    # 得物侧的尺码写法也要登记 —— 官网和得物两边都可能冒出新格式，
    # 而库存匹配是两边归一化后比对，任一边解析不了都会让那一格变成未知。
    from app.dao.unparsed_size_dao import UnparsedSizeDAO
    usd = UnparsedSizeDAO(conn.db)
    usd.ensure_indexes()
    unparsed = usd.record("dewu", None, dewu_size_pairs)
    if unparsed["new"]:
        report(f"得物侧发现 {len(unparsed['new'])} 种新的尺码写法："
               f"{', '.join(repr(x) for x in unparsed['new'][:5])}"
               f"{' …' if len(unparsed['new']) > 5 else ''}")

    return {"queried": len(targets), "hit": saved, "miss": miss, "errors": errors,
            "catalog_cached": len(cached), "unparsed_sizes": unparsed["new"]}


def compute(*, market: str = "CN", apply_filter: bool = True,
            sites: list[str] | None = None,
            progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """用定价模型计算套利机会，写入 arbitrage 集合（尺码级）。"""
    def report(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    conn = MongoConnection.from_environment(required=True)
    dao = ArbitrageDAO(conn.db)
    dao.ensure_indexes()
    cfg = get_pricing_config(load_config())

    # 必须按站点取，且把本币折成美元。
    # 曾经这里是 find({}) 后按 sku 收敛成字典 —— 五个站的同一货号只剩最后
    # 遍历到的那条，KRW/JPY/GBP 价格被当成美元送进 evaluate()。实测 6,067 个
    # 有报价的货号里只有 970 个拿到美国站记录，其余 5,215 个算出天价成本后
    # 被 passes_filter 静默丢弃 —— arbitrage 表长期只有一百多条就是这么来的。
    from app.core.fx import SITE_CURRENCY, get_rates, to_usd

    picked = [x for x in (sites or list(SITE_CURRENCY)) if x in SITE_CURRENCY]
    rates = get_rates()
    if not rates.get("live"):
        report("警告：实时汇率获取失败，本轮用兜底汇率，利润仅供参考")
    # 定价模型里的 fx 原先只读静态配置（usd_cny 写死 7.12，实际约 6.7），
    # 6% 的偏差直接乘在每一单收入上。这里用实时值覆盖。
    cfg["fx"] = {**cfg.get("fx", {}),
                 "usd_cny": rates["usd_cny"], "usd_hkd": rates["usd_hkd"]}
    report(f"汇率：USD→CNY {rates['usd_cny']}，HKD→CNY "
           f"{rates['usd_cny'] / rates['usd_hkd']:.4f}（{rates['source']}）")

    products: dict[str, dict] = {}
    for d in conn.db["products"].find(
            {"site": {"$in": picked}},
            {"sku": 1, "site": 1, "currency": 1, "name": 1, "category": 1,
             "orig_price": 1, "sale_price": 1, "url": 1, "is_sold_out": 1,
             "promo_code": 1, "promo_rate": 1}):
        local = d.get("sale_price") or d.get("orig_price")
        if not local:
            continue
        cur = d.get("currency") or SITE_CURRENCY.get(d.get("site", "us"), "USD")
        d["_usd"] = to_usd(local, cur, rates)
        d["_site"] = d.get("site", "us")
        # 同一货号多个站都有时，留采购成本最低的那个 —— 比价本来就该挑最便宜的进货地
        cur_best = products.get(d["sku"])
        if cur_best is None or d["_usd"] < cur_best["_usd"]:
            products[d["sku"]] = d
    report(f"候选商品 {len(products)} 个货号（站点 {','.join(picked)}，"
           f"同货号取最低价站点）")

    rows: list[dict] = []
    considered = 0
    for quote in dao.quotes.find({}):
        sku = quote["sku"]
        p = products.get(sku)
        if not p:
            continue
        usd = p["_usd"]

        # 返现门户与销售税是美国站特有的（Rakuten/RetailMeNot、州销售税），
        # 套在日韩英加的采购上会让那些行的利润系统性偏高。
        # 按站点取一份配置：非美国站清零这两项，其余照旧。
        site_cfg = cfg
        if p["_site"] != "us":
            site_cfg = {**cfg, "purchase": {**cfg["purchase"],
                                            "cashback_portal": 0.0,
                                            "sales_tax": 0.0}}

        for size in quote.get("sizes", []):
            price = size.get("globalMinPrice")
            if not price:
                continue
            considered += 1
            sales = size.get("globalSoldNum30")
            res = evaluate(usd, price, site_cfg, market=market,
                           promo_rate=p.get("promo_rate"), promo_code=p.get("promo_code"))
            if apply_filter and not passes_filter(res, sales, cfg):
                continue
            rows.append({
                "sku": sku,
                "size": size.get("size"),
                "name": p.get("name"),
                "category": p.get("category"),
                "url": p.get("url"),
                "is_sold_out": p.get("is_sold_out", False),
                "site": p["_site"],
                "local_price": p.get("sale_price") or p.get("orig_price"),
                "local_currency": p.get("currency"),
                "adidas_list_usd": p.get("orig_price"),
                "adidas_price_usd": round(usd, 2),
                "promo_code": p.get("promo_code"),
                "promo_rate": p.get("promo_rate"),
                "after_promo_usd": res["cost"]["after_promo_usd"],
                "dewu_price": price,
                "monthly_sales": sales,
                "sales_mom": size.get("globalMonthToMonthRatio"),
                "market": res["market"],
                "currency": res["currency"],
                "cost_local": res["cost"]["cost_local"],
                "payout": res["payout"]["payout"],
                "profit": res["profit"],
                "roi": res["roi"],
                "global_sku_id": size.get("globalSkuId"),
            })

    st = dao.replace_arbitrage(rows)
    report(f"比价完成：评估 {considered} 个尺码，入选 {len(rows)}，"
           f"写入 {st['written']}，清掉已失效 {st['removed']}")
    return {"considered": considered, "selected": len(rows),
            "written": st["written"], "removed": st["removed"],
            "sites": picked, "fx_live": rates.get("live")}
