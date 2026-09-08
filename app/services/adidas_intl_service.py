"""Adidas 各国站抓取（韩国/日本/英国/加拿大）。

这些站点都不走美国站那套 Next.js plp-app（一律 404），而是共用搜索接口：
    GET {host}/api/search/taxonomy?sitePath={sitePath}&query=<slug>&start=<n>

返回 itemList.items[]：
    productId       货号（各站同构，可直接与得物 articleNumber 匹配）
    price/salePrice 本币价格
    salePercentage  折扣百分比字符串
    availableSizes  在售尺码（格式各国不同，由 app.core.sizing 归一）

商品写入同一个 products 集合，用 site 字段区分。
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable

from curl_cffi import requests as cf_requests

from app.services.full_scan_service import make_session, IMPERSONATE, parse_badges

log = logging.getLogger(__name__)

PAGE_SIZE = 48
WORKERS = 6
MAX_RETRIES = 3
SLEEP_SEC = 0.8

# site -> (域名, sitePath, 币种, 站点中文名)
# 注意：加拿大站 sitePath 必须留空，传 ca/en_CA 等均返回 400 Config not found
SITES: dict[str, tuple[str, str, str, str]] = {
    "kr": ("www.adidas.co.kr", "kr", "KRW", "韩国"),
    "jp": ("www.adidas.jp",    "jp", "JPY", "日本"),
    "gb": ("www.adidas.co.uk", "gb", "GBP", "英国"),
    "ca": ("www.adidas.ca",    "",   "CAD", "加拿大"),
}

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


def fetch_page(session: cf_requests.Session, site: str, slug: str, start: int) -> dict | None:
    host, site_path, _, _ = SITES[site]
    url = f"https://{host}/api/search/taxonomy?sitePath={site_path}&query={slug}&start={start}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, impersonate=IMPERSONATE, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            log.warning("%s %s start=%s HTTP %s", site, slug, start, r.status_code)
        except Exception as exc:
            log.warning("%s %s start=%s %s", site, slug, start, type(exc).__name__)
        time.sleep(SLEEP_SEC * attempt)
    return None


def parse_item(it: dict, category: str, site: str) -> dict | None:
    sku = (it.get("productId") or "").strip()
    if not sku:
        return None
    host, _, currency, _ = SITES[site]

    price, sale = it.get("price"), it.get("salePrice")
    orig = price if price else sale
    sale_price = sale if (sale is not None and price is not None and sale < price) else None

    pct = None
    m = re.search(r"(\d+)", str(it.get("salePercentage") or ""))
    if m and int(m.group(1)) > 0:
        pct = int(m.group(1))

    link = it.get("link") or ""
    url = (f"https://{host}" + link) if link.startswith("/") else link
    sizes = [s for s in (it.get("availableSizes") or []) if s and str(s).lower() != "hidden"]

    return {
        "sku": sku, "site": site,
        "name": it.get("displayName", ""), "subtitle": it.get("subTitle", ""),
        "category": category, "url": url,
        "orig_price": orig, "sale_price": sale_price, "discount_pct": pct,
        "is_sold_out": not it.get("orderable"),
        "currency": currency,
        "available_sizes": sizes,
        "colour_variations": it.get("colorVariations", []),
        "rating": it.get("rating"), "rating_count": it.get("ratingCount"),
        **parse_badges(it.get("badges")),
        "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def scan_category(session, site: str, slug: str, label: str,
                  report: Callable[[str], None]) -> list[dict]:
    first = fetch_page(session, site, slug, 0)
    if not first:
        report(f"  [{label}] 无数据")
        return []
    il = first.get("itemList", {})
    total = il.get("count", 0)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    report(f"  [{label}] 总数 {total}，{pages} 页")

    out = [x for x in (parse_item(i, label, site) for i in il.get("items", [])) if x]
    starts = [p * PAGE_SIZE for p in range(1, pages)]
    with ThreadPoolExecutor(WORKERS) as ex:
        for d in ex.map(lambda st: fetch_page(session, site, slug, st), starts):
            if d:
                out.extend(x for x in
                           (parse_item(i, label, site)
                            for i in d.get("itemList", {}).get("items", [])) if x)
    report(f"  [{label}] 完成 {len(out)} 条")
    return out


def run_site_scan(site: str, category: str | None = None,
                  progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """抓取指定国家站并写入 products。"""
    if site not in SITES:
        raise ValueError(f"未知站点 {site}，可选 {list(SITES)}")

    def report(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    from app.dao.mongo_client import MongoConnection
    from app.dao.product_dao import ProductDAO

    label_cn = SITES[site][3]
    session = make_session()
    cats = [c for c in CATEGORIES if not category or c[0] == category]

    all_rows: list[dict] = []
    for slug, label, _ in cats:
        report(f"[{label_cn}] 开始抓取分类: {label}")
        all_rows.extend(scan_category(session, site, slug, label, report))

    priority = {c[1]: c[2] for c in CATEGORIES}
    seen: dict[str, dict] = {}
    for it in all_rows:
        cur = seen.get(it["sku"])
        if cur is None or priority.get(it["category"], 0) < priority.get(cur["category"], 0):
            seen[it["sku"]] = it
    deduped = list(seen.values())
    report(f"[{label_cn}] 去重后: {len(deduped)} 个唯一 SKU")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_id = f"{site}_price_{ts}"
    conn = MongoConnection.from_environment(required=True)
    try:
        ProductDAO(conn.db).upsert_products(deduped, source_file=batch_id,
                                            include_sizes=False, record_price_history=True)
        # 记录尺码快照，供补货/断码检测
        from app.dao.size_history_dao import SizeHistoryDAO
        sizes_map = {x["sku"]: x.get("available_sizes") or []
                     for x in deduped if x.get("available_sizes")}
        diff = {"new_in_stock": {}, "went_out_of_stock": {}}
        if sizes_map:
            sh = SizeHistoryDAO(conn.db)
            sh.ensure_indexes()
            diff = sh.record_changes(site, sizes_map, batch_id=batch_id)
    finally:
        conn.close()

    discounted = sum(1 for x in deduped if x.get("sale_price"))
    report(f"[{label_cn}] 完成：{len(deduped)} 个 SKU，打折 {discounted} 个；"
           f"补货 {len(diff['new_in_stock'])} 个，断码 {len(diff['went_out_of_stock'])} 个")
    return {"site": site, "total": len(deduped), "discounted": discounted,
            "batch_id": batch_id,
            "restock": diff["new_in_stock"], "out_of_stock": diff["went_out_of_stock"]}


def run_kr_scan(category: str | None = None, progress=None) -> dict[str, Any]:
    """向后兼容的韩国站入口。"""
    return run_site_scan("kr", category=category, progress=progress)
