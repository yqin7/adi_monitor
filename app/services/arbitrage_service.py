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

import dewu_client as dc

from app.core.config import load_config
from app.core.pricing import evaluate, get_pricing_config, passes_filter
from app.dao.arbitrage_dao import ArbitrageDAO
from app.dao.mongo_client import MongoConnection

log = logging.getLogger(__name__)

QUOTE_WORKERS = 2          # 得物查询并发（实测 6 会触发 400010007 调用频次超限）
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
            r = dc.query_article_full(sku, region=region, currency=currency)
            return sku, (r if r.get("found") else None), False
        except Exception as exc:
            msg = str(exc)
            if RATE_LIMIT_CODE in msg and attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            log.warning("得物查询失败 %s: %s", sku, msg[:90])
            return sku, None, True
    return sku, None, True


def refresh_quotes(*, limit: int | None = None, only_discounted: bool = False,
                   include_missed: bool = False, site: str | None = None,
                   region: str = "CN", currency: str = "CNY",
                   progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """拉取得物报价并落库。

    only_discounted : 只查 Adidas 在打折的商品（套利机会主要来源）
    include_missed  : 是否重试历史未命中的货号（默认跳过，按冷却期轮换）
    """
    def report(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    conn = MongoConnection.from_environment(required=True)
    dao = ArbitrageDAO(conn.db)
    dao.ensure_indexes()
    dc.load_env()

    q: dict[str, Any] = {}
    if only_discounted:
        q["sale_price"] = {"$ne": None}
    if site:
        q["site"] = site
    all_skus = [d["sku"] for d in conn.db["products"].find(q, {"sku": 1})]
    targets = dao.skus_to_refresh(all_skus, include_missed=include_missed)
    if limit:
        targets = targets[:limit]

    report(f"候选 {len(all_skus)} 个，本轮查询 {len(targets)} 个（并发 {QUOTE_WORKERS}）")

    hit = miss = errors = 0
    with ThreadPoolExecutor(QUOTE_WORKERS) as ex:
        futures = {ex.submit(_fetch_one, s, region, currency): s for s in targets}
        for i, fut in enumerate(as_completed(futures), 1):
            sku, data, failed = fut.result()
            if data:
                dao.save_quote(sku, data)
                dao.mark_match(sku, True)
                hit += 1
            elif failed:
                errors += 1          # 查询本身失败，不标记未命中，下轮重试
            else:
                dao.mark_match(sku, False)
                miss += 1
            if i % 50 == 0:
                report(f"  进度 {i}/{len(targets)}  命中 {hit}  未命中 {miss}  失败 {errors}")

    report(f"报价补全完成：命中 {hit}，未命中 {miss}，失败 {errors}")
    return {"queried": len(targets), "hit": hit, "miss": miss, "errors": errors}


def compute(*, market: str = "CN", apply_filter: bool = True,
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

    products = {d["sku"]: d for d in conn.db["products"].find(
        {}, {"sku": 1, "name": 1, "category": 1, "orig_price": 1,
             "sale_price": 1, "url": 1, "is_sold_out": 1,
             "promo_code": 1, "promo_rate": 1})}

    rows: list[dict] = []
    considered = 0
    for quote in dao.quotes.find({}):
        sku = quote["sku"]
        p = products.get(sku)
        if not p:
            continue
        usd = p.get("sale_price") or p.get("orig_price")
        if not usd:
            continue

        for size in quote.get("sizes", []):
            price = size.get("globalMinPrice")
            if not price:
                continue
            considered += 1
            sales = size.get("globalSoldNum30")
            res = evaluate(usd, price, cfg, market=market,
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
                "adidas_list_usd": p.get("orig_price"),
                "adidas_price_usd": usd,
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

    written = dao.replace_arbitrage(rows)
    report(f"比价完成：评估 {considered} 个尺码，入选 {len(rows)}，写入 {written}")
    return {"considered": considered, "selected": len(rows), "written": written}
