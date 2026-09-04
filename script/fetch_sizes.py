#!/usr/bin/env python3
"""Fetch selected Adidas SKU sizes and inventory into MongoDB（CLI）

复用 app/services/full_scan_service.py 的核心逻辑，不重复实现。
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.full_scan_service import (
    fetch_availability, _needs_refresh,
    AVAIL_WORKERS, AVAIL_MIN_SLEEP, AVAIL_MAX_SLEEP, AVAIL_RETRIES,
)
from app.dao.mongo_client import MongoConnection
from app.dao.product_dao import ProductDAO


def enrich_sizes(
    items: list[dict],
    force: bool,
    workers: int,
    retries: int,
    min_sleep: float,
    max_sleep: float,
) -> None:
    to_fetch: list[dict] = []
    reused   = 0
    skipped  = 0

    for item in items:
        if not force and item.get("is_sold_out"):
            skipped += 1
            continue
        if not force and item.get("sizes"):
            if not _needs_refresh(item["sizes"]):
                reused += 1
                continue
        to_fetch.append(item)

    total = len(to_fetch)
    print(f"需重拉: {total}  复用: {reused}  售罄跳过: {skipped}  "
          f"线程: {workers}  retries: {retries}  sleep={min_sleep}-{max_sleep}s", flush=True)

    if not to_fetch:
        print("全部已有有效尺码，无需请求。")
        return

    sku_to_item = {item["sku"]: item for item in to_fetch}
    done    = 0
    failed  = 0
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_availability, sku, retries, min_sleep, max_sleep): sku
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
                rate_r  = done / elapsed if elapsed > 0 else 0
                eta     = int((total - done) / rate_r) if rate_r > 0 else 0
                print(f"  {done}/{total}  {rate_r:.1f} req/s  ETA {eta}s  fail={failed}",
                      end="\r", flush=True)

    elapsed = time.monotonic() - t_start
    print(f"\n  完毕: {total} 个  耗时 {elapsed:.0f}s  fail={failed}  复用={reused}")


def main():
    parser = argparse.ArgumentParser(description="从 MongoDB 按 SKU 补充/刷新尺码库存")
    parser.add_argument("--sku", nargs="+", required=True,
                        help="从 MongoDB 读取并更新指定 SKU 的尺码")
    parser.add_argument("--force", action="store_true",
                        help="强制重拉所有 SKU，忽略已有尺码和售罄状态")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前 N 个 SKU，0=全部")
    parser.add_argument("--workers", type=int, default=AVAIL_WORKERS,
                        help=f"并发线程数（默认 {AVAIL_WORKERS}）")
    parser.add_argument("--retries", type=int, default=AVAIL_RETRIES,
                        help=f"单 SKU 最大重试次数（默认 {AVAIL_RETRIES}）")
    parser.add_argument("--min-sleep", type=float, default=AVAIL_MIN_SLEEP,
                        help=f"每线程最小随机间隔秒数（默认 {AVAIL_MIN_SLEEP}）")
    parser.add_argument("--max-sleep", type=float, default=AVAIL_MAX_SLEEP,
                        help=f"每线程最大随机间隔秒数（默认 {AVAIL_MAX_SLEEP}）")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit 不能小于 0")
    if args.workers <= 0:
        parser.error("--workers 必须大于 0")
    if args.retries <= 0:
        parser.error("--retries 必须大于 0")
    if args.min_sleep < 0 or args.max_sleep < 0:
        parser.error("--min-sleep / --max-sleep 不能小于 0")
    if args.min_sleep > args.max_sleep:
        parser.error("--min-sleep 不能大于 --max-sleep")

    conn = MongoConnection.from_environment(required=True)
    try:
        product_dao = ProductDAO(conn.db)
        data = product_dao.get_products(args.sku)
        if not data:
            parser.error("MongoDB 中找不到指定 SKU")
        print(f"从 MongoDB 读取 {len(data)} 个 SKU")

        items = data[:args.limit] if args.limit > 0 else data
        if args.limit > 0:
            print(f"--limit {args.limit}：只处理前 {len(items)} 个")

        enrich_sizes(
            items,
            force=args.force,
            workers=args.workers,
            retries=args.retries,
            min_sleep=args.min_sleep,
            max_sleep=args.max_sleep,
        )

        batch_id = "size_" + time.strftime("%Y%m%d_%H%M%S")
        product_dao.upsert_products(
            items,
            source_file=batch_id,
            include_sizes=True,
            record_price_history=False,
        )
        print(f"尺码库存已同步到 MongoDB，批次号: {batch_id}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
