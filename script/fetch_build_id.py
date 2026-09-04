#!/usr/bin/env python3
"""
使用 Playwright 自动打开 Adidas 页面并提取最新 buildId（CLI）

用法:
  python script/fetch_build_id.py
  python script/fetch_build_id.py --url https://www.adidas.com/us/accessories
  python script/fetch_build_id.py --headed
  python script/fetch_build_id.py --timeout 30000

核心探测逻辑见 app/services/build_id_service.py。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.build_id_service import DEFAULT_URLS, BUILD_ID_CACHE, discover_from_page


def main() -> None:
    parser = argparse.ArgumentParser(description="用 Playwright 自动获取 Adidas buildId")
    parser.add_argument("--url", action="append", dest="urls", help="自定义要尝试的页面 URL，可多次传入")
    parser.add_argument("--timeout", type=int, default=20000, help="页面加载超时（毫秒），默认 20000")
    parser.add_argument("--headed", action="store_true", help="以可见浏览器模式运行")
    parser.add_argument("--no-cache", action="store_true", help="只打印结果，不写入 .build_id_cache")
    args = parser.parse_args()

    urls = args.urls or DEFAULT_URLS

    for url in urls:
        print(f"尝试页面: {url}", flush=True)
        try:
            bid = discover_from_page(url=url, timeout_ms=args.timeout, headed=args.headed)
        except Exception as e:
            print(f"  失败: {e}", flush=True)
            continue

        if bid:
            print(f"发现 buildId: {bid}")
            if not args.no_cache:
                BUILD_ID_CACHE.write_text(bid, encoding="utf-8")
                print(f"已写入缓存: {BUILD_ID_CACHE}")
            return
        print("  未提取到 buildId", flush=True)

    print("未能自动获取 buildId")
    sys.exit(1)


if __name__ == "__main__":
    main()
