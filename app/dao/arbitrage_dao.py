"""比价相关集合的数据访问。

dewu_quotes  : 货号 -> 各尺码得物报价 + 30天销量
match_status : 货号 -> 是否在得物命中（避免重复查询未命中的货号）
arbitrage    : 计算后的套利机会（尺码级）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, UpdateOne


class ArbitrageDAO:
    def __init__(self, db) -> None:
        self.db = db
        self.quotes = db["dewu_quotes"]
        self.match = db["match_status"]
        self.arb = db["arbitrage"]

    def ensure_indexes(self) -> None:
        self.quotes.create_index([("sku", ASCENDING)], unique=True)
        self.quotes.create_index([("fetched_at", ASCENDING)])
        self.match.create_index([("sku", ASCENDING)], unique=True)
        self.match.create_index([("found", ASCENDING), ("last_tried_at", ASCENDING)])
        self.arb.create_index([("sku", ASCENDING), ("size", ASCENDING)], unique=True)
        self.arb.create_index([("profit", DESCENDING)])
        self.arb.create_index([("roi", DESCENDING)])
        self.arb.create_index([("monthly_sales", DESCENDING)])

    # ── 报价 ──────────────────────────────────────────────────────────────
    def save_quote(self, sku: str, payload: dict) -> None:
        payload = {**payload, "sku": sku, "fetched_at": datetime.utcnow()}
        self.quotes.update_one({"sku": sku}, {"$set": payload}, upsert=True)

    def get_quote(self, sku: str) -> dict | None:
        return self.quotes.find_one({"sku": sku})

    # ── 命中状态 ──────────────────────────────────────────────────────────
    def mark_match(self, sku: str, found: bool) -> None:
        self.match.update_one(
            {"sku": sku},
            {"$set": {"found": found, "last_tried_at": datetime.utcnow()},
             "$inc": {"tries": 1}},
            upsert=True,
        )

    def skus_to_refresh(self, all_skus: Iterable[str], *, include_missed: bool,
                        missed_cooldown_days: int = 30) -> list[str]:
        """挑出本轮需要查询的货号：未查过的 + 已命中的；未命中的按冷却期跳过。"""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=missed_cooldown_days)
        known = {d["sku"]: d for d in self.match.find({}, {"sku": 1, "found": 1, "last_tried_at": 1})}
        out = []
        for sku in all_skus:
            rec = known.get(sku)
            if rec is None:
                out.append(sku)
            elif rec.get("found"):
                out.append(sku)
            elif include_missed and (rec.get("last_tried_at") or datetime.min) < cutoff:
                out.append(sku)
        return out

    # ── 套利结果 ──────────────────────────────────────────────────────────
    def replace_arbitrage(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        ops = [UpdateOne({"sku": r["sku"], "size": r["size"]},
                         {"$set": {**r, "computed_at": datetime.utcnow()}}, upsert=True)
               for r in rows]
        res = self.arb.bulk_write(ops, ordered=False)
        return (res.upserted_count or 0) + (res.modified_count or 0)

    def query_arbitrage(self, *, sort_by: str = "profit", order: str = "desc",
                        limit: int = 50, offset: int = 0,
                        min_profit: float | None = None,
                        min_roi: float | None = None,
                        min_sales: int | None = None,
                        category: str | None = None) -> dict[str, Any]:
        allowed = {"profit", "roi", "monthly_sales", "dewu_price", "cost_local"}
        if sort_by not in allowed:
            sort_by = "profit"
        direction = DESCENDING if order != "asc" else ASCENDING

        q: dict[str, Any] = {}
        if min_profit is not None:
            q["profit"] = {"$gte": min_profit}
        if min_roi is not None:
            q["roi"] = {"$gte": min_roi}
        if min_sales is not None:
            q["monthly_sales"] = {"$gte": min_sales}
        if category:
            q["category"] = category

        total = self.arb.count_documents(q)
        rows = list(self.arb.find(q, {"_id": 0})
                    .sort([(sort_by, direction)]).skip(offset).limit(limit))
        return {"total": total, "offset": offset, "limit": limit,
                "sort_by": sort_by, "order": order, "items": rows}
