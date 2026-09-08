"""美国站尺码库存补全。

背景：
    美国站的商品主数据来自 Next.js PLP 路由（/plp-app/_next/data/...），
    该接口不返回尺码；官方的 /api/products/{sku}/availability 又被 Akamai 403。

发现：
    美国站同样提供韩国站那套搜索接口，且返回 availableSizes：
        GET /api/search/taxonomy?sitePath=us&query=<slug>&start=<n>

    所以尺码可以用这个接口按分类批量补，无需逐 SKU 请求。

写入：
    products.available_sizes（与韩国站同字段），按 (sku, site='us') 更新。
    尺码格式为美码，如 "M 10 / W 11"、"XL"，由 app.core.sizing 归一后与得物比对。
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
    """按分类拉取美国站尺码，批量写入 products.available_sizes。"""
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
                sizes = _clean_sizes(it.get("availableSizes"))
                if sku and sizes:
                    collected[sku] = sizes

        absorb(first)
        starts = [p * PAGE_SIZE for p in range(1, pages)]
        with ThreadPoolExecutor(WORKERS) as ex:
            for d in ex.map(lambda st: fetch_page(session, slug, st), starts):
                absorb(d)
        report(f"  [{slug}] 累计 {len(collected)} 个 SKU 有尺码")

    if not collected:
        return {"skus": 0, "updated": 0}

    # 尺码与价格是两次独立抓取（价格走 PLP，尺码走 taxonomy），
    # 所以单独记时间，前端才能分辨「价格新但尺码旧」这种情况。
    now = datetime.utcnow()
    ops = [UpdateOne({"sku": sku, "site": "us"},
                     {"$set": {"available_sizes": sizes,
                               "sizes_updated_at": now}})
           for sku, sizes in collected.items()]
    written = 0
    for i in range(0, len(ops), 2000):
        res = prod.bulk_write(ops[i:i + 2000], ordered=False)
        written += res.modified_count

    # 记录尺码快照，供补货/断码检测
    from app.dao.size_history_dao import SizeHistoryDAO
    sh = SizeHistoryDAO(conn.db)
    sh.ensure_indexes()
    diff = sh.record_changes("us", collected,
                             batch_id=datetime.now().strftime("us_sizes_%Y%m%d_%H%M%S"))
    report(f"美国站尺码补全完成：{len(collected)} 个 SKU，更新 {written} 条；"
           f"补货 {len(diff['new_in_stock'])} 个，断码 {len(diff['went_out_of_stock'])} 个")
    return {"skus": len(collected), "updated": written,
            "restock": diff["new_in_stock"], "out_of_stock": diff["went_out_of_stock"]}
