"""一次性脚本：撤销 2026-09-16 云上首轮写入的美国站尺码「库存」。

背景见 app/services/us_sizes_service.py 模块说明：从美国出口拿到的 availableSizes 是尺码范围，
不是库存，9/16 06:29 起的美国站尺码全是错的（IH7830 一下子 27 个码"有货"）。

做的事（只动 site=us）：
    1. products：sizes_updated_at >= 2026-09-16 的商品，把 available_sizes 搬到 size_range，
       available_sizes / sizes_updated_at 置 None（库存未知）
    2. size_history：删除 2026-09-16 起的美国站快照（batch us_sizes_20260916_*），
       让最后一条快照回到 9/15 国内出口抓的真实数据

用法：
    python script/reset_us_sizes_20260916.py --dry-run
    python script/reset_us_sizes_20260916.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import UpdateOne                       # noqa: E402

from app.dao.mongo_client import MongoConnection    # noqa: E402

CUTOFF = datetime(2026, 9, 16)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = MongoConnection.from_environment(required=True).db
    prod, sh = db["products"], db["size_history"]

    q = {"site": "us", "sizes_updated_at": {"$gte": CUTOFF}}
    n_prod = prod.count_documents(q)
    n_hist = sh.count_documents({"site": "us", "observed_at": {"$gte": CUTOFF}})
    print(f"美国站 9/16 起写过尺码的商品: {n_prod}；9/16 起的美国站尺码快照: {n_hist}")
    if args.dry_run:
        print("dry-run，未写库")
        return 0

    ops = []
    for d in prod.find(q, {"available_sizes": 1, "sizes_updated_at": 1}):
        ops.append(UpdateOne({"_id": d["_id"]}, {"$set": {
            "size_range": d.get("available_sizes") or [],
            "size_range_updated_at": d.get("sizes_updated_at"),
            "available_sizes": None,
            "sizes_updated_at": None,
        }}))
    done = 0
    for i in range(0, len(ops), 2000):
        done += prod.bulk_write(ops[i:i + 2000], ordered=False).modified_count
    r = sh.delete_many({"site": "us", "observed_at": {"$gte": CUTOFF}})
    print(f"products 已重置 {done} 条；size_history 已删 {r.deleted_count} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
