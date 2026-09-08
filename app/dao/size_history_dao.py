"""尺码快照历史。

products.available_sizes 只存当前状态，每次扫描会被覆盖，无法做前后对比。
本集合按 (sku, site) 追加快照，用于检测补货 / 断码：

    {sku, site, sizes: [...], observed_at, batch_id}

只在尺码集合发生变化时才追加新记录（相同则更新 last_seen_at），
避免每轮扫描都写一份重复数据 —— 47k SKU × 每天数次会迅速膨胀。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, UpdateOne


class SizeHistoryDAO:
    def __init__(self, db) -> None:
        self.db = db
        self.coll = db["size_history"]

    def ensure_indexes(self) -> None:
        self.coll.create_index([("sku", ASCENDING), ("site", ASCENDING),
                                ("observed_at", DESCENDING)])
        self.coll.create_index([("observed_at", DESCENDING)])

    def latest(self, sku: str, site: str) -> dict | None:
        return self.coll.find_one({"sku": sku, "site": site},
                                  sort=[("observed_at", DESCENDING)])

    def latest_many(self, skus: Iterable[str], site: str) -> dict[str, dict]:
        """批量取每个 sku 的最新一条快照。"""
        pipeline = [
            {"$match": {"sku": {"$in": list(skus)}, "site": site}},
            {"$sort": {"observed_at": -1}},
            {"$group": {"_id": "$sku", "doc": {"$first": "$$ROOT"}}},
        ]
        return {r["_id"]: r["doc"] for r in self.coll.aggregate(pipeline)}

    def record_changes(self, site: str, current: dict[str, list[str]],
                       batch_id: str | None = None) -> dict[str, Any]:
        """比对上一份快照，只为发生变化的 sku 追加记录。

        current: {sku: [尺码, ...]}
        返回 {changed, new_in_stock, went_out_of_stock}，其中后两项为
            {sku: [尺码, ...]}，供上层做通知。
        """
        now = datetime.utcnow()
        prev = self.latest_many(current.keys(), site)

        ops, restock, oos = [], {}, {}
        for sku, sizes in current.items():
            cur_set = set(sizes)
            old = prev.get(sku)
            if old is not None and set(old.get("sizes") or []) == cur_set:
                # 无变化：只更新最后一次观察时间，不追加新记录
                ops.append(UpdateOne({"_id": old["_id"]},
                                     {"$set": {"last_seen_at": now}}))
                continue

            if old is not None:
                old_set = set(old.get("sizes") or [])
                gained = sorted(cur_set - old_set)
                lost = sorted(old_set - cur_set)
                if gained:
                    restock[sku] = gained
                if lost:
                    oos[sku] = lost

            self.coll.insert_one({
                "sku": sku, "site": site, "sizes": sorted(cur_set),
                "observed_at": now, "last_seen_at": now, "batch_id": batch_id,
            })

        if ops:
            for i in range(0, len(ops), 1000):
                self.coll.bulk_write(ops[i:i + 1000], ordered=False)

        return {"changed": len(restock) + len(oos),
                "new_in_stock": restock, "went_out_of_stock": oos}
