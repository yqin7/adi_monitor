"""Fetch Adidas product SKU and price data into MongoDB.

The default workflow is MongoDB-only; no local product JSON is written.
"""
import argparse, time, sys, random
from collections import Counter
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cf_requests
from adidas_monitor import check_sku
from mongo_store import MongoStore
from mongo_store import sync_products_to_mongo

# ── 配置 ───────────────────────────────────────────────────────────────────────
IMPERSONATE   = "chrome131"
PAGE_SIZE     = 48
SLEEP_SEC     = 1.5        # PLP 翻页间隔（秒）
MAX_RETRIES   = 3
PLP_WORKERS   = 8          # PLP 翻页并发线程（每个分类）

# availability 并发配置
AVAIL_WORKERS   = 16       # 默认并发线程数
AVAIL_RETRIES   = 4
AVAIL_MIN_SLEEP = 0.6      # 每线程请求后的随机等待
AVAIL_MAX_SLEEP = 1.0
# 智能增量：qty <= 此值 或 有 LOW_STOCK 尺码时，强制重新拉库存
LOW_STOCK_QTY  = 5

BASE_URL   = "https://www.adidas.com/plp-app/_next/data/{build_id}/us/{slug}.json"
AVAIL_URL  = "https://www.adidas.com/api/products/{sku}/availability?sitePath=us"

CATEGORIES = [
    # (slug,            extra_params,              label,   priority)
    ("men-clothing",    "path=us",                 "men-clothing",   0),
    ("women-clothing",  "path=us",                 "women-clothing",   0),
    ("kids-clothing",   "path=us",                 "kids-clothing",   0),
    ("men-shoes",       "path=us",                 "men-shoes",   0),
    ("women-shoes",     "path=us",                 "women-shoes",   0),
    ("kids-shoes",      "path=us",                 "kids-shoes",   0),
    ("accessories",     "path=us",                 "accessories",   0),
    ("sale",            "path=us&taxonomy=sale",   "sale",  1),
]

# ── buildId 管理 ───────────────────────────────────────────────────────────────
BUILD_ID_CACHE = Path(__file__).parent / ".build_id_cache"

def _validate_build_id(session: cf_requests.Session, bid: str, source: str) -> str | None:
    print(f"尝试 buildId ({source}): {bid} ...", end=" ", flush=True)
    try:
        test_url = BASE_URL.format(build_id=bid, slug="accessories") + "?start=0&path=us"
        r = session.get(test_url, impersonate=IMPERSONATE, timeout=20)
        if r.status_code == 200:
            print("有效!")
            BUILD_ID_CACHE.write_text(bid)
            return bid
        print(f"无效 ({r.status_code})")
    except Exception as e:
        print(f"错误: {e}")
    return None

def auto_discover_build_id(session: cf_requests.Session) -> str | None:
    """Open Adidas pages, discover the current Next.js buildId, and validate it."""
    try:
        from fetch_build_id import DEFAULT_URLS, discover_from_page
    except Exception as exc:
        print(f"自动获取 buildId 失败：缺少 Playwright 或依赖错误: {exc}", flush=True)
        return None

    for url in DEFAULT_URLS:
        print(f"自动获取 buildId：打开 {url} ...", flush=True)
        try:
            bid = discover_from_page(url=url, timeout_ms=30000, headed=False)
        except Exception as exc:
            print(f"  页面探测失败: {exc}", flush=True)
            continue
        if bid:
            print(f"  探测到 buildId: {bid}", flush=True)
            validated = _validate_build_id(session, bid, "自动获取")
            if validated:
                return validated

    return None


def load_build_id(session: cf_requests.Session, manual_build_id: str | None) -> str | None:
    if manual_build_id:
        validated = _validate_build_id(session, manual_build_id, "命令行")
        if validated:
            return validated
        print("命令行 buildId 无效，开始自动获取新的 buildId ...", flush=True)
        return auto_discover_build_id(session)

    cached = BUILD_ID_CACHE.read_text().strip() if BUILD_ID_CACHE.exists() else None
    if cached:
        validated = _validate_build_id(session, cached, "缓存")
        if validated:
            return validated
        print("缓存 buildId 无效，开始自动获取新的 buildId ...", flush=True)
    else:
        print("未提供 buildId，开始自动获取 ...", flush=True)

    return auto_discover_build_id(session)

# ── PLP 单页请求 ───────────────────────────────────────────────────────────────
def fetch_page(session: cf_requests.Session, build_id: str,
               slug: str, extra: str, start: int) -> dict | None:
    url = BASE_URL.format(build_id=build_id, slug=slug) + f"?start={start}&{extra}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, impersonate=IMPERSONATE, timeout=25)
            if r.status_code == 200:
                return r.json()
            elif r.status_code == 404:
                print(f"\n  404 (buildId 可能已过期): {url[:80]}")
                return None
            print(f"\n  {r.status_code} retry {attempt}")
            time.sleep(SLEEP_SEC * 2)
        except Exception as e:
            print(f"\n  ERROR retry {attempt}: {e}")
            time.sleep(SLEEP_SEC * 2)
    return None

# ── 解析 PLP 商品字段 ──────────────────────────────────────────────────────────
def parse_product(p: dict, category: str) -> dict:
    prices = p.get("priceData", {}).get("prices", [])
    sale_price = orig_price = discount_pct = None
    for pr in prices:
        if pr.get("type") == "sale":
            sale_price = pr.get("value")
        elif pr.get("type") == "original":
            orig_price = pr.get("value")
            discount_pct = pr.get("discountPercentage")

    return {
        "sku":               p["id"],
        "name":              p.get("title", ""),
        "subtitle":          p.get("subTitle", ""),
        "category":          category,
        "url":               "https://www.adidas.com" + p.get("url", ""),
        "sale_price":        sale_price,
        "orig_price":        orig_price,
        "discount_pct":      discount_pct,
        "is_sold_out":       p.get("priceData", {}).get("isSoldOut", False),
        "colour_variations": p.get("colourVariations", []),
        "rating":            p.get("rating"),
        "rating_count":      p.get("ratingCount"),
        "sizes":             [],
        "scraped_at":        datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

# ── 抓取单个分类（PLP 翻页）────────────────────────────────────────────────────
def fetch_category(
    session: cf_requests.Session,
    build_id: str,
    slug: str,
    extra: str,
    label: str,
    plp_workers: int,
) -> list[dict]:
    print(f"\n[{label}] 开始抓取 ...", flush=True)
    first = fetch_page(session, build_id, slug, extra, 0)
    if not first:
        print(f"[{label}] 第一页失败，跳过")
        return []

    info     = first.get("pageProps", {}).get("info", {})
    total    = info.get("count", 0)
    products = first.get("pageProps", {}).get("products", [])
    pages    = -(-total // PAGE_SIZE)
    print(f"  总数: {total}  (需翻 {pages} 页)")

    results = [parse_product(p, label) for p in products]
    if pages > 1:
        starts = [page_no * PAGE_SIZE for page_no in range(1, pages)]
        done_pages = 1
        with ThreadPoolExecutor(max_workers=max(1, plp_workers)) as executor:
            futures = {
                executor.submit(fetch_page, session, build_id, slug, extra, start): start
                for start in starts
            }
            for future in as_completed(futures):
                start = futures[future]
                done_pages += 1
                try:
                    data = future.result()
                except Exception as e:
                    print(f"\n  start={start} 异常: {e}")
                    data = None
                if not data:
                    print(f"  page {start // PAGE_SIZE + 1} 失败，跳过")
                    continue
                ps = data.get("pageProps", {}).get("products", [])
                results.extend(parse_product(p, label) for p in ps)
                print(
                    f"  page {done_pages}/{pages}  已获取 {len(results):5d}/{total}",
                    end="\r",
                    flush=True,
                )

    print(f"  完成: {len(results)} 条                    ")
    return results

# ── availability 请求（复用 adidas_monitor 的单 SKU 逻辑）──────────────────────
def fetch_availability(
    sku: str,
    retries: int = AVAIL_RETRIES,
    min_sleep: float = AVAIL_MIN_SLEEP,
    max_sleep: float = AVAIL_MAX_SLEEP,
) -> dict | None:
    sku = sku.upper().strip()
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            result = check_sku(sku)
            if max_sleep > 0:
                time.sleep(random.uniform(min_sleep, max_sleep))
            return {
                "sku": sku,
                "sizes": result.get("sizes", []),
                "overall_status": result.get("overall_status", "UNKNOWN"),
                "checked_at": result.get("checked_at"),
            }
        except Exception as e:
            last_err = str(e)
            if "404" in last_err:
                print(f"\n  [404] {sku} 已下架/不存在，跳过重试", flush=True)
                return {
                    "sku": sku,
                    "sizes": [],
                    "overall_status": "NOT_FOUND",
                    "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            print(f"\n  [ERR] {sku} attempt={attempt}/{retries} {last_err}", flush=True)
            if attempt < retries:
                time.sleep(min(3 * attempt, 10))
    print(f"\n  [FAIL] {sku} 全部重试耗尽: {last_err}", flush=True)
    return None

# ── 智能增量判断：判断一个 SKU 是否需要重新拉库存 ──────────────────────────────
def _needs_refresh(prev_sizes: list[dict]) -> bool:
    """True = 需要重新拉；False = 可以复用"""
    if not prev_sizes:
        return True
    for s in prev_sizes:
        if s.get("status") == "LOW_STOCK":
            return True
        if s.get("qty", 99) <= LOW_STOCK_QTY:
            return True
    return False

# ── 主库存抓取（含增量复用逻辑）──────────────────────────────────────────────
def enrich_sizes(items: list[dict], prev_map: dict[str, list] | None = None) -> None:
    # 分类：哪些需要重拉，哪些可以复用
    to_fetch   = []
    reused     = 0
    skipped    = 0   # 售罄跳过

    for item in items:
        sku = item["sku"]

        # 已确认全线售罄：跳过（availability 只会返回全 OUT_OF_STOCK）
        if item.get("is_sold_out"):
            skipped += 1
            continue

        if prev_map and sku in prev_map:
            prev_sizes = prev_map[sku]
            if not _needs_refresh(prev_sizes):
                item["sizes"] = prev_sizes
                reused += 1
                continue

        to_fetch.append(item)

    total = len(to_fetch)
    print(f"\n[尺码库存] 需重拉: {total}  复用: {reused}  售罄跳过: {skipped}  "
          f"线程: {AVAIL_WORKERS}  sleep={AVAIL_MIN_SLEEP}-{AVAIL_MAX_SLEEP}s", flush=True)

    if not to_fetch:
        print("  全部复用，跳过请求")
        return

    sku_to_item = {item["sku"]: item for item in to_fetch}
    done    = 0
    failed  = 0
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=AVAIL_WORKERS) as executor:
        futures = {
            executor.submit(fetch_availability, sku): sku
            for sku in sku_to_item
        }
        for future in as_completed(futures):
            sku  = futures[future]
            done += 1
            try:
                result = future.result()
                if result:
                    sku_to_item[sku]["sizes"] = result["sizes"]
                    sku_to_item[sku]["overall_status"] = result.get("overall_status", "UNKNOWN")
                    if result.get("checked_at"):
                        sku_to_item[sku]["checked_at"] = result["checked_at"]
                else:
                    failed += 1
            except Exception as e:
                print(f"\n  [EXCEPT] {sku}: {e}", flush=True)
                failed += 1
            if done % 50 == 0 or done == total:
                elapsed = time.monotonic() - t_start
                rate    = done / elapsed if elapsed > 0 else 0
                eta     = int((total - done) / rate) if rate > 0 else 0
                print(f"  {done}/{total}  {rate:.1f} req/s  ETA {eta}s  fail={failed}",
                      end="\r", flush=True)

    elapsed = time.monotonic() - t_start
    print(f"\n  完毕: {total} 个  耗时 {elapsed:.0f}s  fail={failed}  复用={reused}")

# ── 主函数 ─────────────────────────────────────────────────────────────────────
def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    parser = argparse.ArgumentParser(description="Adidas PLP 全量价格抓取（MongoDB-only）")
    parser.add_argument("--build-id", help="Next.js buildId（默认自动获取）")
    parser.add_argument("--category", help="只抓单个分类 slug（如 sale / men-shoes）")
    parser.add_argument("--plp-workers", type=int, default=PLP_WORKERS,
                        help=f"PLP 翻页并发线程数（默认 {PLP_WORKERS}）")
    parser.add_argument("--no-sizes", action="store_true",
                        help="跳过尺码/库存抓取")
    args = parser.parse_args()
    if args.plp_workers <= 0:
        parser.error("--plp-workers 必须大于 0")

    session = cf_requests.Session()
    build_id = load_build_id(session, args.build_id)
    if not build_id:
        print("无法获得有效 buildId，抓取终止。", flush=True)
        sys.exit(1)
    print(f"buildId: {build_id}")

    cats = CATEGORIES
    if args.category:
        cats = [c for c in CATEGORIES if c[0] == args.category]
        if not cats:
            parser.error(f"未知分类: {args.category}，可用: {[c[0] for c in CATEGORIES]}")

    all_results: list[dict] = []
    for slug, extra, label, _ in cats:
        all_results.extend(fetch_category(
            session, build_id, slug, extra, label, plp_workers=args.plp_workers
        ))
        time.sleep(SLEEP_SEC * 2)

    priority_map = {c[2]: c[3] for c in CATEGORIES}
    seen: dict[str, dict] = {}
    for item in all_results:
        sku = item["sku"]
        if sku not in seen:
            seen[sku] = item
        elif priority_map.get(item["category"], 0) < priority_map.get(seen[sku]["category"], 0):
            seen[sku] = item
    deduped = list(seen.values())
    print(f"\n去重后: {len(deduped)} 个唯一 SKU")

    batch_id = f"price_{ts}"
    sync_products_to_mongo(
        deduped,
        source_file=batch_id,
        include_sizes=False,
        record_price_history=True,
        required=True,
    )

    if not args.no_sizes:
        store = MongoStore.from_environment(required=True)
        try:
            previous = store.get_products([item["sku"] for item in deduped])
        finally:
            store.close()
        prev_map = {item["sku"]: item.get("sizes", []) for item in previous}
        enrich_sizes(deduped, prev_map)
        sync_products_to_mongo(
            deduped,
            source_file=batch_id,
            include_sizes=True,
            record_price_history=False,
            required=True,
        )

    cats_count = Counter(x["category"] for x in deduped)
    with_sale = [x for x in deduped if x["sale_price"] and x["orig_price"]
                 and x["sale_price"] < x["orig_price"]]
    sold_out = [x for x in deduped if x["is_sold_out"]]
    prices = [x["orig_price"] for x in deduped if x["orig_price"]]

    print(f"\n{'=' * 60}")
    print(f"完成！共 {len(deduped)} 个唯一 SKU")
    print(f"批次号: {batch_id}")
    print("分类明细:")
    for cat, n in cats_count.most_common():
        print(f"  {cat}: {n}")
    print(f"打折商品: {len(with_sale)}")
    print(f"售罄商品: {len(sold_out)}")
    if prices:
        print(f"价格区间: ${min(prices)} - ${max(prices)}")


if __name__ == "__main__":
    main()
