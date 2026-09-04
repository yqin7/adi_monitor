"""使用 Playwright 自动打开 Adidas 页面并提取最新 Next.js buildId"""
from __future__ import annotations

import re

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.core.config import load_config, get_playwright_proxy
from app.core.paths import PROJECT_ROOT

BUILD_ID_CACHE = PROJECT_ROOT / ".build_id_cache"
DEFAULT_URLS = [
    "https://www.adidas.com/us",
    "https://www.adidas.com/us/accessories",
]
PATTERNS = [
    r'"buildId"\s*:\s*"([^"]+)"',
    r"/plp-app/_next/data/([^/]+)/",
    r"/_next/data/([^/]+)/",
    r"/plp-app/_next/static/([^/]+)/_buildManifest\.js",
    r"/_next/static/([^/]+)/_buildManifest\.js",
    r"/plp-app/_next/static/([^/]+)/_ssgManifest\.js",
    r"/_next/static/([^/]+)/_ssgManifest\.js",
]


def is_probable_build_id(value: str) -> bool:
    if not value:
        return False
    if value.lower() in {"css", "chunks", "media", "static", "_buildmanifest.js", "_ssgmanifest.js"}:
        return False
    if "/" in value or "." in value or len(value) < 8:
        return False
    return re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


def extract_build_id(text: str) -> str | None:
    for pattern in PATTERNS:
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1)
            if is_probable_build_id(candidate):
                return candidate
    return None


def _try_common_interactions(page, timeout_ms: int) -> None:
    """尽量模拟真实用户浏览，触发 Next 相关请求。"""
    cookie_selectors = [
        "button:has-text('Accept')",
        "button:has-text('I agree')",
        "button:has-text('Allow All')",
        "button:has-text('OK')",
        "#glass-gdpr-default-consent-accept-button",
        "[data-testid='glass-gdpr-default-consent-accept-button']",
    ]
    for selector in cookie_selectors:
        try:
            page.locator(selector).first.click(timeout=1500)
            break
        except Exception:
            pass

    for y in (400, 900, 1400):
        try:
            page.mouse.wheel(0, y)
            page.wait_for_timeout(600)
        except Exception:
            pass

    nav_selectors = [
        "a[href*='/us/accessories']",
        "a[href*='/us/men-shoes']",
        "a[href*='/us/women-shoes']",
        "a[href*='/us/sale']",
        "a:has-text('Accessories')",
        "a:has-text('Shoes')",
        "a:has-text('Sale')",
        "a:has-text('Men')",
        "a:has-text('Women')",
    ]
    for selector in nav_selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.click(timeout=2500)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 8000))
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)
            return
        except Exception:
            continue


def discover_from_page(url: str, timeout_ms: int, headed: bool) -> str | None:
    seen_candidates: list[str] = []

    with sync_playwright() as p:
        launch_kwargs = {
            "headless": not headed,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        try:
            proxy = get_playwright_proxy(load_config())
        except Exception:
            proxy = None
        if proxy:
            launch_kwargs["proxy"] = proxy
        try:
            browser = p.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception:
            browser = p.chromium.launch(**launch_kwargs)

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4] });
            window.chrome = window.chrome || { runtime: {} };
            """
        )

        def capture_candidate(text: str) -> None:
            bid = extract_build_id(text)
            if bid and bid not in seen_candidates:
                seen_candidates.append(bid)

        page.on("request", lambda req: capture_candidate(req.url))
        page.on("response", lambda resp: capture_candidate(resp.url))
        page.on("console", lambda msg: capture_candidate(msg.text))

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
            except PlaywrightTimeoutError:
                pass

            # 0. 模拟用户交互，尽量触发页面内导航和 next 请求
            _try_common_interactions(page, timeout_ms)

            # 1. 直接读运行时变量
            runtime_bid = page.evaluate(
                """() => {
                    try {
                        if (window.__NEXT_DATA__ && window.__NEXT_DATA__.buildId) {
                            return window.__NEXT_DATA__.buildId;
                        }
                    } catch (e) {}
                    return null;
                }"""
            )
            if runtime_bid:
                browser.close()
                return runtime_bid

            # 2. 从 HTML 中提取
            html = page.content()
            html_bid = extract_build_id(html)
            if html_bid:
                browser.close()
                return html_bid

            # 3. 从页面脚本 src / 网络请求中提取
            scripts = page.eval_on_selector_all(
                "script[src]",
                "(nodes) => nodes.map((n) => n.getAttribute('src') || '')",
            )
            for script_src in scripts:
                capture_candidate(script_src)

            browser.close()
        except PlaywrightTimeoutError:
            browser.close()
            return None
        except Exception:
            browser.close()
            raise

    return seen_candidates[0] if seen_candidates else None
