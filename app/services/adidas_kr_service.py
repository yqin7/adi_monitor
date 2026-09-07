"""Adidas 韩国站抓取。

韩国站不走全球的 Next.js plp-app（/plp-app/_next/data/... 一律 404），
而是有自己的搜索接口：
    GET /api/search/taxonomy?sitePath=kr&query=<slug>&start=<n>

返回 itemList.items[]，字段与美国站不同：
    productId      货号（与美国站同构，可直接与得物 articleNumber 匹配）
    price/salePrice 韩元
    salePercentage  折扣百分比字符串，如 "30%"
    availableSizes  尺码列表

商品写入同一个 products 集合，用 site 字段区分（us / kr）。
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable

from curl_cffi import requests as cf_requests

from app.services.full_scan_service import make_session, IMPERSONATE, parse_badges

log = logging.getLogger(__name__)

BASE = "https://www.adidas.co.kr/api/search/taxonomy"
PAGE_SIZE = 48
WORKERS = 6
MAX_RETRIES = 3
SLEEP_SEC = 0.8

# (slug, label, priority) —— priority 小的在去重时胜出
CATEGORIES = [
    ("men-shoes",      "men-shoes",      0),
    ("women-shoes",    "women-shoes",    0),
    ("kids-shoes",     "kids-shoes",     0),
    ("men-clothing",   "men-clothing",   0),
    ("women-clothing", "women-clothing", 0),
    ("kids-clothing",  "kids-clothing",  0),
    ("accessories",    "accessories",    0),
    ("sale",           "sale",           1),
]


def fetch_page(session: cf_requests.Session, slug: str, start: int) -> dict | None:
    url = f"{BASE}?sitePath=kr&query={slug}&start={start}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, impersonate=IMPERSONATE, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            log.warning("KR %s start=%s HTTP %s (retry %s)", slug, start, r.status_code, attempt)
        except Exception as exc:
            log.warning("KR %s start=%s %s (retry %s)", slug, start, type(exc).__name__, attempt)
        time.sleep(SLEEP_SEC * attempt)
    return None


def parse_item(it: dict, category: str) -> dict | None:
    sku = (it.get("productId") or "").strip()
    if not sku:
        return None

    price = it.get("price")
    sale = it.get("salePrice")
    # 韩国站两个字段都给；无折扣时相等，此时 sale_price 记为 None 与美国站语义一致
    orig = price if price else sale
    sale_price = sale if (sale is not None and price is not None and sale < price) else None

    pct = None
    m = re.search(r"(\d+)", str(it.get("salePercentage") or ""))
    if m and int(m.group(1)) > 0:
        pct = int(m.group(1))

    link = it.get("link") or ""
    url = ("https://www.adidas.co.kr" + link) if link.startswith("/") else link

    sizes = [s for s in (it.get("availableSizes") or []) if s and s != "hidden"]

    return {
        "sku": sku,
        "site": "kr",
        "name": it.get("displayName", ""),
        "subtitle": it.get("subTitle", ""),
        "category": category,
        "url": url,
        "orig_price": orig,
        "sale_price": sale_price,
        "discount_pct": pct,
        "is_sold_out": not it.get("orderable"),
        "currency": "KRW",
        "available_sizes": sizes,
        "colour_variations": it.get("colorVariations", []),
        "rating": it.get("rating"),
        "rating_count": it.get("ratingCount"),
        # 韩国站接口不返回 badges；促销体现在 salePercentage 上
        **parse_badges(None),
        "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def scan_category(session, slug: str, label: str,
                  report: Callable[[str], None]) -> list[dict]:
    first = fetch_page(session, slug, 0)
    if not first:
        report(f"  [{label}] 无数据")
        return []

    il = first.get("itemList", {})
    total = il.get("count", 0)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    report(f"  [{label}] 总数 {total}，需翻 {pages} 页")

    out = [x for x in (parse_item(i, label) for i in il.get("items", [])) if x]
    starts = [p * PAGE_SIZE for p in range(1, pages)]
    with ThreadPoolExecutor(WORKERS) as ex:
        futures = {ex.submit(fetch_page, session, slug, s): s for s in starts}
        for fut in as_completed(futures):
            d = fut.result()
            if not d:
                continue
            out.extend(x for x in
                       (parse_item(i, label) for i in d.get("itemList", {}).get("items", []))
                       if x)
    report(f"  [{label}] 完成 {len(out)} 条")
    return out


def run_kr_scan(category: str | None = None,
                progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """全量抓取韩国站并写入 products（site='kr'）。"""
    def report(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    from app.dao.mongo_client import MongoConnection
    from app.dao.product_dao import ProductDAO

    session = make_session()
    cats = [c for c in CATEGORIES if not category or c[0] == category]

    all_rows: list[dict] = []
    for slug, label, _ in cats:
        report(f"开始抓取分类: {label}")
        all_rows.extend(scan_category(session, slug, label, report))

    priority = {c[1]: c[2] for c in CATEGORIES}
    seen: dict[str, dict] = {}
    for it in all_rows:
        cur = seen.get(it["sku"])
        if cur is None or priority.get(it["category"], 0) < priority.get(cur["category"], 0):
            seen[it["sku"]] = it
    deduped = list(seen.values())
    report(f"去重后: {len(deduped)} 个唯一 SKU")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_id = f"kr_price_{ts}"
    conn = MongoConnection.from_environment(required=True)
    try:
        dao = ProductDAO(conn.db)
        dao.upsert_products(deduped, source_file=batch_id,
                            include_sizes=False, record_price_history=True)
    finally:
        conn.close()

    discounted = sum(1 for x in deduped if x.get("sale_price"))
    report(f"韩国站完成：{len(deduped)} 个 SKU，打折 {discounted} 个，批次 {batch_id}")
    return {"total": len(deduped), "discounted": discounted, "batch_id": batch_id}
