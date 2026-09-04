#!/usr/bin/env python3
"""
Adidas 商品价格 & 尺码库存监控脚本（CLI）
用法:
    python script/adidas_monitor.py                  # 查一次 JR5408
    python script/adidas_monitor.py JR5408 JR5410    # 查多个 SKU
    python script/adidas_monitor.py --watch JR5408   # 每5分钟持续监控

核心查询逻辑见 app/services/product_service.py。
"""

import json
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.product_service import check_sku


def print_result(r: dict):
    """格式化打印结果"""
    discount = ""
    if r["original_price"] and r["sale_price"] and r["original_price"] != r["sale_price"]:
        pct = int((1 - r["sale_price"] / r["original_price"]) * 100)
        discount = f"  (原价 {r['currency']} {r['original_price']}，折扣 -{pct}%)"

    print(f"\n{'='*55}")
    print(f"  商品: {r['name']} [{r['sku']}]")
    print(f"  配色: {r['color']}")
    print(f"  价格: {r['currency']} {r['sale_price']}{discount}")
    print(f"  状态: {r['overall_status']}")
    print(f"  时间: {r['checked_at']}")
    print(f"{'─'*55}")

    in_stock = r["in_stock_sizes"]
    if in_stock:
        print(f"  有货尺码 ({len(in_stock)} 个):")
        for s in in_stock:
            qty_str = f"  余 {s['qty']} 件" if s["qty"] > 0 else ""
            print(f"    ✓  {s['size']:<20}{qty_str}")
    else:
        print("  ⚠  全部尺码无货")

    no_stock = [s for s in r["sizes"] if s["status"] != "IN_STOCK"]
    if no_stock:
        sizes_str = "、".join(s["size"] for s in no_stock)
        print(f"\n  无货: {sizes_str}")

    print(f"{'='*55}")


def watch(skus: list, interval: int):
    """持续监控模式"""
    print(f"开始监控 {skus}，每 {interval} 秒刷新一次。按 Ctrl+C 退出。\n")
    while True:
        for sku in skus:
            try:
                r = check_sku(sku)
                print_result(r)
            except Exception as e:
                print(f"[{sku}] 错误: {e}")
        print(f"\n等待 {interval} 秒...\n")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Adidas 商品价格 & 库存监控")
    parser.add_argument("skus", nargs="*", default=["JR5408"],
                        help="一个或多个 SKU，如 JR5408 JR5410")
    parser.add_argument("--watch", action="store_true",
                        help="持续监控模式")
    parser.add_argument("--interval", type=int, default=300,
                        help="监控间隔（秒），默认 300 = 5分钟")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 格式输出（方便写入数据库）")
    args = parser.parse_args()

    if args.watch:
        watch(args.skus, args.interval)
        return

    results = []
    for sku in args.skus:
        try:
            r = check_sku(sku)
            if args.json:
                results.append(r)
            else:
                print_result(r)
        except Exception as e:
            print(f"[{sku}] 错误: {e}")

    if args.json and results:
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
