"""一次性脚本：把 price_history 集合迁移进 products.price_list 数组。

背景：
    价格历史原来存在独立的 price_history 集合，一次查询/建趋势图都要单独
    连表。改成内嵌在 products.price_list 里后，读当前价格只需取数组最后
    一个元素，读趋势直接读整个数组，不用再跨集合查询。

字段兼容：
    存量 price_history 文档是旧版抓取脚本写的，用的是 snapshot_id /
    source_file，不是当前 product_dao.py 里的 batch_id 字段名；这里做了
    兼容读取。

可重跑：
    写回时与 products 里已有的 price_list 按 batch_id 合并，不是整段覆盖 ——
    首轮迁移之后扫描 $push 进去的新价格不会被抹掉。

用法：
    python script/migrate_price_history_to_list.py --dry-run   # 只统计，不写库
    python script/migrate_price_history_to_list.py              # 迁移，不删源集合
    python script/migrate_price_history_to_list.py --drop        # 迁移后删除 price_history（不可逆）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
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


def sort_key(entry: dict) -> datetime:
    """存量记录是 naive datetime，新写入的是 aware(UTC)，缺字段的当最早；
    三种混在一起直接比较会 TypeError，这里统一成 aware UTC。"""
    t = entry.get("observed_at")
    if not isinstance(t, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def merge(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """按 batch_id 去重合并，已有的优先；无 batch_id 的记录按 observed_at 去重。"""
    seen: set = set()
    out: list[dict] = []
    for e in list(existing or []) + incoming:
        key = e.get("batch_id") or ("_t", sort_key(e))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=sort_key)
    return out


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

    total_entries = sum(len(v) for v in grouped.values())
    print(f"  待合并进 products 的分组: {len(grouped)} 个，共 {total_entries} 条价格记录")

    if args.dry_run:
        print("dry-run，未写库")
        return 0

    keys = list(grouped.keys())
    processed = matched = 0
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i + BATCH]
        # 读出这批商品现有的 price_list 做合并，避免整段覆盖抹掉迁移后新 $push 的记录
        existing: dict[tuple[str, str], list[dict]] = {}
        for p in prod.find({"$or": [{"sku": s, "site": t} for s, t in chunk]},
                           {"sku": 1, "site": 1, "price_list": 1}):
            existing[(p["sku"], p.get("site", "us"))] = p.get("price_list") or []
        ops = []
        for key in chunk:
            if key not in existing:
                continue
            merged = merge(existing[key], [build_entry(d) for d in grouped[key]])
            ops.append(UpdateOne({"sku": key[0], "site": key[1]},
                                 {"$set": {"price_list": merged}}))
        if ops:
            res = prod.bulk_write(ops, ordered=False)
            matched += res.matched_count
        processed += len(chunk)
        print(f"  已处理 {processed}/{len(keys)}（命中 {matched}）", flush=True)

    missing_products = len(keys) - matched
    print(f"迁移完成：命中 products {matched} 条，未命中（products 里没有对应 sku+site）{missing_products} 条")

    if not args.drop:
        print("未加 --drop，price_history 集合保留；确认无误后加 --drop 重跑即可（合并写入，可重复执行）")
        return 0
    if missing_products:
        print("有历史记录没迁移过去，拒绝删除 price_history。先处理未命中的再加 --drop。")
        return 1
    print("删除 price_history 集合 ...")
    hist.drop()
    print("已删除 price_history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
