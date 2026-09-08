"""全站 PLP 抓取 + 尺码库存补充（发现全站 SKU 的核心业务逻辑）"""
from __future__ import annotations

import re
import time
import random
from collections import Counter
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from curl_cffi import requests as cf_requests

from app.core.config import (
    load_config,
    get_requests_proxies,
    proxy_enabled,
    use_env_proxy,
)
from app.core.paths import PROJECT_ROOT
from app.services.product_service import check_sku
from app.dao.mongo_client import MongoConnection
from app.dao.product_dao import ProductDAO

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

# ── HTTP 会话 ─────────────────────────────────────────────────────────────────
def make_session(config: dict | None = None) -> cf_requests.Session:
    """按 config.yaml 的 proxy 开关创建会话。

    proxy.enabled = true  -> 只走配置的代理
    proxy.enabled = false -> 真正直连（默认忽略 HTTP_PROXY 等环境变量；
                             想沿用系统代理设 proxy.use_env_proxy: true）
    """
    cfg = config if config is not None else load_config()
    if proxy_enabled(cfg):
        session = cf_requests.Session(trust_env=False)
        session.proxies = get_requests_proxies(cfg)
        print(f"代理：已启用 -> {session.proxies['https']}", flush=True)
    elif use_env_proxy(cfg):
        session = cf_requests.Session(trust_env=True)
        print("代理：未配置，沿用系统环境变量代理", flush=True)
    else:
        session = cf_requests.Session(trust_env=False)
        print("代理：已关闭（直连）", flush=True)
    return session


# ── buildId 管理 ───────────────────────────────────────────────────────────────
BUILD_ID_CACHE = PROJECT_ROOT / ".build_id_cache"
FAILED_PAGES: list[dict] = []

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

def discover_build_id_from_data_404(session: cf_requests.Session) -> str | None:
    """用一个失效 buildId 请求 data 接口，从返回的 404 页面里提取当前 buildId。

    这条路不经过 HTML 页面（首页/分类页会被 Akamai 403 拦截），也不需要浏览器。
    """
    probe = BASE_URL.format(build_id="0" * 21, slug="men-shoes") + "?start=0&path=us"
    try:
        r = session.get(probe, impersonate=IMPERSONATE, timeout=25)
    except Exception as exc:
        print(f"  404 探测请求失败: {exc}", flush=True)
        return None

    for pattern in (r'"buildId"\s*:\s*"([A-Za-z0-9_-]{8,})"',
                    r'/_next/static/([A-Za-z0-9_-]{8,})/_ssgManifest\.js',
                    r'/_next/static/([A-Za-z0-9_-]{8,})/_buildManifest\.js'):
        for candidate in re.findall(pattern, r.text):
            if candidate.lower() not in ("css", "chunks", "media", "static") and "0" * 21 != candidate:
                return candidate
    return None


def auto_discover_build_id(session: cf_requests.Session) -> str | None:
    """发现当前 Next.js buildId 并校验。优先走 404 页面，失败再回退 Playwright。"""
    print("自动获取 buildId：探测 data 接口 404 页面 ...", flush=True)
    bid = discover_build_id_from_data_404(session)
    if bid:
        print(f"  探测到 buildId: {bid}", flush=True)
        validated = _validate_build_id(session, bid, "404探测")
        if validated:
            return validated

    try:
        from app.services import build_id_service
    except Exception as exc:
        print(f"自动获取 buildId 失败：缺少 Playwright 或依赖错误: {exc}", flush=True)
        return None

    for url in build_id_service.DEFAULT_URLS:
        print(f"自动获取 buildId：打开 {url} ...", flush=True)
        try:
            bid = build_id_service.discover_from_page(url=url, timeout_ms=30000, headed=False)
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
               slug: str, extra: str, start: int,
               category: str | None = None,
               record_failure: bool = True) -> dict | None:
    url = BASE_URL.format(build_id=build_id, slug=slug) + f"?start={start}&{extra}"
    had_error = False
    last_error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, impersonate=IMPERSONATE, timeout=25)
            if r.status_code == 200:
                if had_error and record_failure:
                    FAILED_PAGES.append({
                        "category": category or slug,
                        "slug": slug,
                        "extra": extra,
                        "start": start,
                        "error": last_error,
                    })
                return r.json()
            elif r.status_code == 404:
                print(f"\n  404 (buildId 可能已过期): {url[:80]}")
                return None
            had_error = True
            last_error = f"HTTP {r.status_code}"
            print(f"\n  {r.status_code} retry {attempt}")
            time.sleep(SLEEP_SEC * 2)
        except Exception as e:
            had_error = True
            last_error = str(e)
            print(f"\n  ERROR retry {attempt}: {e}")
            time.sleep(SLEEP_SEC * 2)

    if record_failure:
        FAILED_PAGES.append({
            "category": category or slug,
            "slug": slug,
            "extra": extra,
            "start": start,
            "error": last_error,
        })
    return None

# ── 解析 PLP 商品字段 ──────────────────────────────────────────────────────────
# ── 促销 badge 解析 ────────────────────────────────────────────────────────────
# Adidas 把活动逐商品标在 badges 上，措辞会变，例如：
#   "Extra 30% Off with CODE: EXTRA"   "30% Off Full Price"
#   "Save 25% with code SUMMER"        "Up to 40% Off"      "$20 Off"
# 所以不匹配整句，而是分别抽「折扣幅度」「促销码」「是否为不确定的上限表述」。

# 百分比：兼容 "30% off" / "save 25%" / "25% discount" / 纯 "30%"
PCT_RE = re.compile(r"(\d{1,2})\s*%(?:\s*(?:off|discount))?", re.I)
AMOUNT_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*off", re.I)
CODE_RE = re.compile(r"(?:with\s+)?code\s*[:\s]\s*([A-Z0-9][A-Z0-9_-]{2,19})\b", re.I)
UPTO_RE = re.compile(r"\bup\s+to\b", re.I)
# 非促销类 badge，出现这些词就不当作折扣
NON_PROMO = re.compile(r"best\s*seller|new\b|award|recycled|rarely\s+on\s+sale|"
                       r"members?\b|coming\s+soon|sold\s+out|limited", re.I)
# 看起来像促销但没解析出幅度的，标记出来供人工复核
PROMO_HINT = re.compile(r"%|off\b|sale|save|deal|discount|code", re.I)


def parse_badges(badges: list | None) -> dict:
    """通用促销解析。返回：

        badges           原始文案列表（永远保留，措辞变了也能事后追溯）
        promo_rate       折扣系数，30% off -> 0.70；无折扣为 None
        promo_code       促销码；自动折扣（无需码）为 None
        promo_text       命中的原文
        promo_amount_off 固定金额减免（美元），如 "$20 Off"
        promo_needs_code 是否需要输码
        promo_uncertain  是 "Up to X% off" 这类上限表述（不保证，成本不按它算）
        promo_unparsed   疑似促销但没解析出幅度的文案，用于发现新措辞
    """
    texts = [b.get("text", "").strip() for b in (badges or []) if b.get("text", "").strip()]

    best_rate = None
    best = {"promo_code": None, "promo_text": None, "promo_amount_off": None,
            "promo_needs_code": False, "promo_uncertain": False}
    unparsed: list[str] = []

    for t in texts:
        if NON_PROMO.search(t) and not PCT_RE.search(t) and not AMOUNT_RE.search(t):
            continue

        pct = PCT_RE.search(t)
        amt = AMOUNT_RE.search(t)
        if not pct and not amt:
            # 看着像促销却抽不出幅度 —— 大概率是新措辞，记下来供复核
            if PROMO_HINT.search(t):
                unparsed.append(t)
            continue

        code_m = CODE_RE.search(t)
        uncertain = bool(UPTO_RE.search(t))
        rate = round(1 - int(pct.group(1)) / 100, 4) if pct else None

        # "Up to X%" 不保证，不参与成本计算，仅记录
        if uncertain:
            if best["promo_text"] is None:
                best.update({"promo_text": t, "promo_uncertain": True,
                             "promo_code": code_m.group(1).upper() if code_m else None,
                             "promo_needs_code": bool(code_m)})
            continue

        # 取折扣最狠的一条
        if rate is not None and (best_rate is None or rate < best_rate):
            best_rate = rate
            best.update({"promo_code": code_m.group(1).upper() if code_m else None,
                         "promo_text": t, "promo_needs_code": bool(code_m),
                         "promo_uncertain": False,
                         "promo_amount_off": float(amt.group(1)) if amt else None})
        elif amt and best_rate is None:
            best.update({"promo_amount_off": float(amt.group(1)),
                         "promo_code": code_m.group(1).upper() if code_m else None,
                         "promo_text": t, "promo_needs_code": bool(code_m)})

    return {"badges": texts, "promo_rate": best_rate,
            "promo_unparsed": unparsed or None, **best}


def _abs_url(link: str) -> str:
    """Adidas 改版后 url 字段已是绝对地址，直接拼前缀会变成 https://www.adidas.comhttps://..."""
    link = (link or "").strip()
    if not link:
        return ""
    if link.startswith("http"):
        return link
    return "https://www.adidas.com" + (link if link.startswith("/") else "/" + link)


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
        "url":               _abs_url(p.get("url", "")),
        "sale_price":        sale_price,
        "orig_price":        orig_price,
        "discount_pct":      discount_pct,
        "is_sold_out":       p.get("priceData", {}).get("isSoldOut", False),
        **parse_badges(p.get("badges")),
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
    first = fetch_page(session, build_id, slug, extra, 0, category=label)
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
                executor.submit(fetch_page, session, build_id, slug, extra, start, label): start
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

# ── availability 请求（复用 product_service 的单 SKU 逻辑）─────────────────────
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

# ── 全量扫描核心逻辑（供 CLI 和 API 服务共同调用）──────────────────────────────
def run_full_scan(
    category: str | None = None,
    plp_workers: int = PLP_WORKERS,
    include_sizes: bool = True,
    build_id: str | None = None,
    progress_cb=None,
) -> dict:
    """执行一次全量 PLP 扫描（发现所有 SKU + 价格），可选补充尺码库存。

    Args:
        category: 只抓单个分类 slug（如 sale / men-shoes），None = 全部分类
        plp_workers: PLP 翻页并发线程数
        include_sizes: 是否在价格抓取后继续补充尺码库存（AVAIL 请求）
        build_id: 手动指定 Next.js buildId，None = 自动获取/使用缓存
        progress_cb: 可选回调 progress_cb(stage: str, message: str)，用于上报进度

    Returns:
        {
            "batch_id": str,
            "total_skus": int,
            "categories": {分类: 数量},
            "with_sale": int,
            "sold_out": int,
            "price_range": [min, max] | None,
            "include_sizes": bool,
        }
    """
    def _report(stage: str, message: str):
        if progress_cb:
            try:
                progress_cb(stage, message)
            except Exception:
                pass
        print(message, flush=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    session = make_session()
    _report("build_id", "获取 buildId ...")
    resolved_build_id = load_build_id(session, build_id)
    if not resolved_build_id:
        raise RuntimeError("无法获得有效 buildId，全量扫描终止")
    _report("build_id", f"buildId: {resolved_build_id}")

    cats = CATEGORIES
    if category:
        cats = [c for c in CATEGORIES if c[0] == category]
        if not cats:
            raise ValueError(f"未知分类: {category}，可用: {[c[0] for c in CATEGORIES]}")

    all_results: list[dict] = []
    for slug, extra, label, _ in cats:
        _report("plp", f"开始抓取分类: {label}")
        all_results.extend(fetch_category(
            session, resolved_build_id, slug, extra, label, plp_workers=plp_workers
        ))
        time.sleep(SLEEP_SEC * 2)

    if FAILED_PAGES:
        pending = FAILED_PAGES.copy()
        FAILED_PAGES.clear()
        _report("plp_retry", f"发现 {len(pending)} 个失败页面，开始集中重试...")
        recovered = 0
        unresolved = []
        for failure in pending:
            data = fetch_page(
                session,
                resolved_build_id,
                failure["slug"],
                failure["extra"],
                failure["start"],
                category=failure["category"],
                record_failure=False,
            )
            page_no = failure["start"] // PAGE_SIZE + 1
            if data:
                products = data.get("pageProps", {}).get("products", [])
                all_results.extend(parse_product(p, failure["category"]) for p in products)
                recovered += 1
                _report("plp_retry", f"  重试成功: {failure['category']} page {page_no}")
            else:
                unresolved.append(failure)
        _report("plp_retry", f"集中重试完成：恢复 {recovered} 个，仍失败 {len(unresolved)} 个")

    priority_map = {c[2]: c[3] for c in CATEGORIES}
    seen: dict[str, dict] = {}
    for item in all_results:
        sku = item["sku"]
        if sku not in seen:
            seen[sku] = item
        elif priority_map.get(item["category"], 0) < priority_map.get(seen[sku]["category"], 0):
            seen[sku] = item
    deduped = list(seen.values())
    _report("dedupe", f"去重后: {len(deduped)} 个唯一 SKU")

    batch_id = f"price_{ts}"
    _report("mongo_price", "写入价格数据到 MongoDB ...")
    conn = MongoConnection.from_environment(required=True)
    try:
        product_dao = ProductDAO(conn.db)
        stat = product_dao.upsert_products(
            deduped,
            source_file=batch_id,
            include_sizes=False,
            record_price_history=True,
        )
        new_skus = stat["new_skus"]
        _report("new", f"新上架 {stat['new_count']} 个 SKU")

        if include_sizes:
            _report("sizes", "补充尺码库存 ...")
            previous = product_dao.get_products([item["sku"] for item in deduped])
            prev_map = {item["sku"]: item.get("sizes", []) for item in previous}
            enrich_sizes(deduped, prev_map)
            _report("mongo_sizes", "写入尺码库存数据到 MongoDB ...")
            product_dao.upsert_products(
                deduped,
                source_file=batch_id,
                include_sizes=True,
                record_price_history=False,
            )
    finally:
        conn.close()

    cats_count = Counter(x["category"] for x in deduped)
    with_sale = [x for x in deduped if x["sale_price"] and x["orig_price"]
                 and x["sale_price"] < x["orig_price"]]
    sold_out = [x for x in deduped if x["is_sold_out"]]
    prices = [x["orig_price"] for x in deduped if x["orig_price"]]

    summary = {
        "batch_id": batch_id,
        "total_skus": len(deduped),
        "categories": dict(cats_count),
        "with_sale": len(with_sale),
        "sold_out": len(sold_out),
        "price_range": [min(prices), max(prices)] if prices else None,
        "include_sizes": include_sizes,
        "new_count": len(new_skus),
        "new_skus": new_skus,
    }
    _report("done", f"完成！共 {len(deduped)} 个唯一 SKU，批次号: {batch_id}")
    return summary
