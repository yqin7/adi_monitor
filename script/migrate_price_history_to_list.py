"""一次性脚本：把 price_history 集合迁移进 products.price_list 数组。

背景：
    价格历史原来存在独立的 price_history 集合，一次查询/建趋势图都要单独
    连表。改成内嵌在 products.price_list 里后，读当前价格只需取数组最后
    一个元素，读趋势直接读整个数组，不用再跨集合查询。

字段兼容：
    存量 price_history 文档是旧版抓取脚本写的，用的是 snapshot_id /
    source_file，不是当前 product_dao.py 里的 batch_id 字段名；这里做了
    兼容读取。

用法：
    python script/migrate_price_history_to_list.py --dry-run   # 只统计，不写库
    python script/migrate_price_history_to_list.py              # 迁移，不删源集合
    python script/migrate_price_history_to_list.py --drop        # 迁移后删除 price_history（不可逆）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import UpdateOne                       # noqa: E402

from app.dao.mongo_client import MongoConnection    # noqa: E402

BATCH = 500


def build_entry(doc: dict) -> dict:
    return {
        "batch_id": doc.get("batch_id") or doc.get("snapshot_id") or doc.get("source_file"),
        "observed_at": doc.get("observed_at"),
        "sale_price": doc.get("sale_price"),
        "orig_price": doc.get("orig_price"),
        "discount_pct": doc.get("discount_pct"),
        "is_sold_out": doc.get("is_sold_out", False),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    ap.add_argument("--drop", action="store_true", help="迁移成功后删除 price_history 集合（不可逆）")
    args = ap.parse_args()

    conn = MongoConnection.from_environment(required=True)
    db = conn.db
    prod, hist = db["products"], db["price_history"]

    hist_total = hist.count_documents({})
    prod_total = prod.count_documents({})
    print(f"price_history: {hist_total} 条 / products: {prod_total} 条")
    if hist_total == 0:
        print("price_history 为空，无需迁移")
        return 0

    # MongoDB 端 $sort 在 38 万条数据上直接撞 32MB 内存限制；数据量不大，
    # 改成把整份 history 拉到本地按 (sku, site) 分组、Python 里排序更稳。
    print("拉取 price_history 全量数据并在本地按 (sku, site) 分组排序 ...")
    grouped: dict[tuple[str, str], list[dict]] = {}
    fetched = 0
    for d in hist.find({}, {
        "sku": 1, "site": 1, "observed_at": 1, "batch_id": 1, "snapshot_id": 1,
        "source_file": 1, "sale_price": 1, "orig_price": 1, "discount_pct": 1,
        "is_sold_out": 1,
    }):
        key = (d["sku"], d.get("site", "us"))
        grouped.setdefault(key, []).append(d)
        fetched += 1
        if fetched % 50000 == 0:
            print(f"  已读取 {fetched}/{hist_total}", flush=True)
    print(f"  共读取 {fetched} 条，{len(grouped)} 个 (sku, site) 分组")

    ops = []
    total_entries = 0
    for (sku, site), docs in grouped.items():
        docs.sort(key=lambda d: d.get("observed_at") or 0)
        entries = [build_entry(d) for d in docs]
        total_entries += len(entries)
        ops.append(UpdateOne({"sku": sku, "site": site}, {"$set": {"price_list": entries}}))

    print(f"  待写回 products 的更新: {len(ops)} 条，共 {total_entries} 条价格记录")

    if args.dry_run:
        print("dry-run，未写库")
        return 0

    written = 0
    matched = 0
    for i in range(0, len(ops), BATCH):
        res = prod.bulk_write(ops[i:i + BATCH], ordered=False)
        written += len(ops[i:i + BATCH])
        # matched_count 才是「products 里有没有这个 (sku, site)」；modified_count
        # 在重跑且内容完全相同时会是 0（MongoDB 判定无变化），不能拿来判断是否命中。
        matched += res.matched_count
        print(f"  已处理 {written}/{len(ops)}（实际命中 {matched}）", flush=True)

    missing_products = len(ops) - matched
    print(f"迁移完成：命中 products {matched} 条，未命中（products 里没有对应 sku+site）{missing_products} 条")

    if missing_products:
        print("警告：部分历史记录在 products 里找不到对应文档，这些历史没有迁移过去。")

    if args.drop:
        print("删除 price_history 集合 ...")
        hist.drop()
        print("已删除 price_history")
    else:
        print("未加 --drop，price_history 集合保留，确认数据无误后可单独加 --drop 重跑来删除")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
