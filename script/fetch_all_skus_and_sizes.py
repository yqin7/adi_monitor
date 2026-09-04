#!/usr/bin/env python3
"""Fetch Adidas product SKU and price data into MongoDB（CLI）

The default workflow is MongoDB-only; no local product JSON is written.
核心抓取逻辑见 app/services/full_scan_service.py。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.full_scan_service import run_full_scan, CATEGORIES, PLP_WORKERS


def main():
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

    try:
        summary = run_full_scan(
            category=args.category,
            plp_workers=args.plp_workers,
            include_sizes=not args.no_sizes,
            build_id=args.build_id,
        )
    except (RuntimeError, ValueError) as e:
        print(str(e), flush=True)
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"完成！共 {summary['total_skus']} 个唯一 SKU")
    print(f"批次号: {summary['batch_id']}")
    print("分类明细:")
    for cat, n in sorted(summary["categories"].items(), key=lambda x: -x[1]):
        print(f"  {cat}: {n}")
    print(f"打折商品: {summary['with_sale']}")
    print(f"售罄商品: {summary['sold_out']}")
    if summary["price_range"]:
        print(f"价格区间: ${summary['price_range'][0]} - ${summary['price_range'][1]}")


if __name__ == "__main__":
    main()
