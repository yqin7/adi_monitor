"""一次性脚本：把 products.price_list 折叠成「价格变化点」。

规则与 ProductDAO._build_ops 一致：按时间顺序，sale_price / orig_price / is_sold_out
三者与上一条都相同的记录丢掉，每段价格只留首次出现的那条。
last_price_observed_at 缺失的商品，用原数组最后一条的 observed_at 补上，
这样「最后一段价格持续到什么时候」不会因为折叠而丢失。

不删表、不改字段，只把数组变短；可重复执行（已折叠的商品不会再变）。

用法：
    python script/compact_price_list.py --dry-run
    python script/compact_price_list.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import UpdateOne                       # noqa: E402

from app.dao.mongo_client import MongoConnection    # noqa: E402
from app.dao.product_dao import ProductDAO          # noqa: E402

BATCH = 1000


def compact(entries: list[dict]) -> list[dict]:
    out: list[dict] = []
    prev = None
    for e in entries:
        key = ProductDAO._price_key(e)
        if key != prev:
            out.append(e)
            prev = key
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = ap.parse_args()

    conn = MongoConnection.from_environment(required=True)
    prod = conn.db["products"]

    total_docs = prod.count_documents({"price_list.0": {"$exists": True}})
    print(f"有 price_list 的商品: {total_docs}")

    scanned = changed = before = after = 0
    ops: list[UpdateOne] = []
    written = 0

    def flush() -> None:
        nonlocal written
        if not ops or args.dry_run:
            ops.clear()
            return
        res = prod.bulk_write(ops, ordered=False)
        written += res.modified_count
        ops.clear()

    cur = prod.find({"price_list.0": {"$exists": True}},
                    {"sku": 1, "site": 1, "price_list": 1, "last_price_observed_at": 1})
    for d in cur:
        scanned += 1
        pl = d.get("price_list") or []
        new = compact(pl)
        before += len(pl)
        after += len(new)
        if len(new) != len(pl) or d.get("last_price_observed_at") is None:
            changed += 1
            upd: dict = {"price_list": new}
            if d.get("last_price_observed_at") is None:
                upd["last_price_observed_at"] = pl[-1].get("observed_at")
            ops.append(UpdateOne({"_id": d["_id"]}, {"$set": upd}))
        if len(ops) >= BATCH:
            flush()
            print(f"  已处理 {scanned}/{total_docs}，写入 {written}", flush=True)
    flush()

    saved = before - after
    print(f"扫描 {scanned} 件商品，需折叠 {changed} 件")
    print(f"价格记录 {before} -> {after}（去掉 {saved} 条，{saved / before * 100:.0f}%）")
    if args.dry_run:
        print("dry-run，未写库")
    else:
        print(f"已写入 {written} 件商品")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
