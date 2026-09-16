"""美国站尺码库存补全。

背景：
    美国站的商品主数据来自 Next.js PLP 路由（/plp-app/_next/data/...），
    该接口不返回尺码；官方的 /api/products/{sku}/availability 又被 Akamai 403。

发现：
    美国站同样提供韩国站那套搜索接口，且返回 availableSizes：
        GET /api/search/taxonomy?sitePath=us&query=<slug>&start=<n>

    所以尺码可以用这个接口按分类批量补，无需逐 SKU 请求。

写入：
    products.size_range（这款有哪些码），按 (sku, site='us') 更新。
    尺码格式为美码，如 "M 10 / W 11"、"XL"。

【2026-09-16 起不再写 available_sizes】
    从美国出口（云服务器，走不走美国住宅代理都一样）请求这个接口，availableSizes 返回的是
    尺码范围而不是库存：IH7830 从 M 3.5 到 M 19 全部 27 个码都"可售"，9/15 从国内出口
    拿到的是 4 个码。全站对比：超过 15 个码的快照占比从 4% 跳到 27%，英/日站没变。
    逐商品的 /api/products/{sku}/availability 从服务器出去被 Akamai 403。
    所以美国站逐尺码库存目前没有可信来源：只存尺码范围，库存置为未知（前端显示 —），
    不做补货/断码判断。商品级 is_sold_out 来自 PLP 价格接口，仍可信。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from pymongo import UpdateOne

from app.services.full_scan_service import make_session, IMPERSONATE

log = logging.getLogger(__name__)

BASE = "https://www.adidas.com/api/search/taxonomy"
PAGE_SIZE = 48
WORKERS = 6
MAX_RETRIES = 3

# 与 full_scan_service.CATEGORIES 对齐的 slug
SLUGS = ["men-clothing", "women-clothing", "kids-clothing",
         "men-shoes", "women-shoes", "kids-shoes", "accessories"]


def fetch_page(session, slug: str, start: int) -> dict | None:
    url = f"{BASE}?sitePath=us&query={slug}&start={start}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, impersonate=IMPERSONATE, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except Exception as exc:
            log.warning("US sizes %s start=%s %s", slug, start, type(exc).__name__)
        time.sleep(0.8 * attempt)
    return None


def _clean_sizes(raw: list | None) -> list[str]:
    return [s for s in (raw or []) if s and str(s).strip().lower() != "hidden"]


def run(progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """按分类拉取美国站尺码范围，写入 products.size_range；库存未知，不写 available_sizes。"""
    def report(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    from app.dao.mongo_client import MongoConnection

    session = make_session()
    conn = MongoConnection.from_environment(required=True)
    prod = conn.db["products"]

    collected: dict[str, list[str]] = {}
    for slug in SLUGS:
        first = fetch_page(session, slug, 0)
        if not first:
            report(f"  [{slug}] 无数据")
            continue
        il = first.get("itemList", {})
        total = il.get("count", 0)
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        report(f"  [{slug}] 总数 {total}，{pages} 页")

        def absorb(d):
            for it in (d or {}).get("itemList", {}).get("items", []):
                sku = (it.get("productId") or "").strip().upper()
                # 空列表也要收：整只断码的商品若被滤掉，旧 available_sizes
                # 永远不清、record_changes 也看不到它，断码永远报不出来。
                if sku:
                    # availableSizes 是「这款有哪些码」，不是「哪些码有货」：
                    # orderable=0 的售罄商品照样列全部尺码（IJ7058 售罄仍列 9 个码）。
                    collected[sku] = _clean_sizes(it.get("availableSizes")) if it.get("orderable") else []

        absorb(first)
        starts = [p * PAGE_SIZE for p in range(1, pages)]
        with ThreadPoolExecutor(WORKERS) as ex:
            for d in ex.map(lambda st: fetch_page(session, slug, st), starts):
                absorb(d)
        report(f"  [{slug}] 累计 {len(collected)} 个 SKU")

    # 与国际站同一套健康门槛：抓崩了要明确报错，不能安静返回 0
    prev = prod.count_documents({"site": "us", "size_range": {"$exists": True}}) \
        or prod.count_documents({"site": "us", "available_sizes": {"$exists": True}})
    ratio = (len(collected) / prev) if prev else 1.0
    with_sizes = sum(1 for v in collected.values() if v)
    if not collected or (prev and ratio < 0.6):
        msg = (f"美国站尺码抓取异常：本轮 {len(collected)} 个，库里已有 {prev} 个"
               f"（{ratio:.0%}，门槛 60%）。已放弃写库，保留原有数据。")
        report(msg)
        log.error(msg)
        return {"skus": len(collected), "updated": 0, "healthy": False,
                "error": msg, "restock": {}, "out_of_stock": {}}
    if with_sizes == 0:
        # 尺码按 orderable 清空：若接口改了字段名（orderable 全缺失），这里会把全站
        # 尺码清成空、断码告警刷屏。抓到几千个 SKU 却一个尺码都没有，只能是接口变了。
        msg = (f"美国站尺码抓取异常：{len(collected)} 个 SKU 全部无尺码，疑似接口字段变化"
               f"（orderable / availableSizes）。已放弃写库，保留原有数据。")
        report(msg)
        log.error(msg)
        return {"skus": len(collected), "updated": 0, "healthy": False,
                "error": msg, "restock": {}, "out_of_stock": {}}

    # 只写尺码范围。available_sizes / sizes_updated_at 不动（见模块说明），
    # 库存对前端就是未知；也不写 size_history，否则补货/断码全是假信号。
    now = datetime.utcnow()
    ops = [UpdateOne({"sku": sku, "site": "us"},
                     {"$set": {"size_range": sizes, "size_range_updated_at": now}})
           for sku, sizes in collected.items()]
    written = 0
    for i in range(0, len(ops), 2000):
        res = prod.bulk_write(ops[i:i + 2000], ordered=False)
        written += res.modified_count
    diff = {"new_in_stock": {}, "went_out_of_stock": {}}

    from app.dao.unparsed_size_dao import UnparsedSizeDAO
    usd = UnparsedSizeDAO(conn.db)
    usd.ensure_indexes()
    unparsed = usd.record("site", "us",
                          ((z, sku) for sku, zs in collected.items() for z in zs))
    if unparsed["new"]:
        report(f"发现 {len(unparsed['new'])} 种新的尺码写法："
               f"{', '.join(repr(x) for x in unparsed['new'][:5])}"
               f"{' …' if len(unparsed['new']) > 5 else ''}")
    report(f"美国站尺码范围更新：{len(collected)} 个 SKU，更新 {written} 条"
           f"（美国出口拿不到逐尺码库存，库存按未知处理，不做补货/断码判断）")
    return {"skus": len(collected), "updated": written, "healthy": True,
            "unparsed_sizes": unparsed["new"],
            "restock": diff["new_in_stock"], "out_of_stock": diff["went_out_of_stock"]}
