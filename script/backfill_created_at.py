"""为存量 products 回填 created_at（一次性脚本）。

背景：
    upsert_products 之前没写 $setOnInsert，47,356 件存量商品一条 created_at 都没有。
    新加的 $setOnInsert 只对【将来新插入】的文档生效，存量补不上。

注意，这一步不是为了防误报：
    已存在的文档在 bulk_write 里走的是 matched 而非 upserted，
    不会被当成新品推送（已实测验证）。做回填纯粹是为了数据完整 ——
    否则「30 天内上架的商品」这类查询会把 4.7 万条 null 静默漏掉。

估计口径：
    取 price_history 里该 (sku, site) 最早的 observed_at。
    这是我们真正观测到它的最早时刻，是首次上架时间的【上界估计】——
    商品可能在我们开始抓之前就上架了，所以标记 first_seen_batch=backfill_estimate，
    免得日后有人把它当成精确的上架时间用。

用法：
    python script/backfill_created_at.py --dry-run    # 只看会改多少
    python script/backfill_created_at.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import UpdateOne                       # noqa: E402

from app.dao.mongo_client import MongoConnection    # noqa: E402

BATCH = 2000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    conn = MongoConnection.from_environment(required=True)
    db = conn.db
    prod, hist = db["products"], db["price_history"]

    todo = prod.count_documents({"created_at": {"$exists": False}})
    print(f"待回填: {todo} / {prod.count_documents({})}")
    if not todo:
        return 0

    print("从 price_history 聚合每个 (sku, site) 的最早观测时间 ...")
    earliest = {
        (r["_id"]["sku"], r["_id"]["site"]): r["t"]
        for r in hist.aggregate([
            {"$group": {"_id": {"sku": "$sku", "site": "$site"},
                        "t": {"$min": "$observed_at"}}},
        ], allowDiskUse=True)
    }
    print(f"  拿到 {len(earliest)} 条历史首见时间")

    ops, from_hist, from_updated, missing = [], 0, 0, 0
    for d in prod.find({"created_at": {"$exists": False}},
                       {"sku": 1, "site": 1, "updated_at": 1, "scraped_at": 1}):
        key = (d["sku"], d.get("site", "us"))
        ts = earliest.get(key)
        if ts is not None:
            from_hist += 1
            src = "backfill_price_history"
        else:
            # 没有价格历史（比如只在尺码接口出现过）-> 退而用最后一次抓取时间
            ts = d.get("updated_at") or d.get("scraped_at")
            if ts is None:
                missing += 1
                continue
            from_updated += 1
            src = "backfill_updated_at"
        ops.append(UpdateOne({"_id": d["_id"]},
                             {"$set": {"created_at": ts, "first_seen_batch": src}}))

    print(f"  来自 price_history: {from_hist}")
    print(f"  退化用 updated_at : {from_updated}")
    print(f"  两者都没有，跳过  : {missing}")

    if args.dry_run:
        print("dry-run，未写库")
        return 0

    written = 0
    for i in range(0, len(ops), BATCH):
        res = prod.bulk_write(ops[i:i + BATCH], ordered=False)
        written += res.modified_count
        print(f"  已写入 {written}/{len(ops)}")
    print(f"回填完成: {written} 条")

    left = prod.count_documents({"created_at": {"$exists": False}})
    print(f"仍无 created_at: {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
