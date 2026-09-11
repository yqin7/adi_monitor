"""解析不了的尺码写法：登记 + 告警 + 自动销账。

为什么要单独存一张表，而不是每次现算：
    /lookup/sizes/unparsed 是全量扫一遍算出来的快照，回答「现在有哪些」。
    但我们真正想知道的是「今天【新冒出来】哪些」—— Adidas 改写法时，
    新格式混在 1,366 种存量里根本看不出来。有了 first_seen 才分得清。

状态机：
    new       首次出现，尚未告警
    known     已知待办（存量，或已告警过）
    resolved  规则补上后不再出现 —— 由 record() 自动销账，不用手工维护

安全前提：
    in_stock 对解析不了的尺码返回 None（未知）而非 False（断码），
    所以这里的条目是待办清单，不是线上事故。见 app/core/sizing.py。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from pymongo import ASCENDING, DESCENDING, UpdateOne

MAX_SAMPLES = 3


class UnparsedSizeDAO:
    def __init__(self, db) -> None:
        self.db = db
        self.coll = db["unparsed_sizes"]

    def ensure_indexes(self) -> None:
        self.coll.create_index([("source", ASCENDING), ("site", ASCENDING),
                                ("raw", ASCENDING)], unique=True)
        self.coll.create_index([("status", ASCENDING), ("count", DESCENDING)])
        self.coll.create_index([("first_seen", DESCENDING)])

    # ── 登记 ──────────────────────────────────────────────────────────
    def record(self, source: str, site: str | None,
               pairs: Iterable[tuple[str, str]],
               seed_as_known: bool = False) -> dict[str, Any]:
        """pairs: (原始尺码字符串, 出处货号)。

        本轮扫描的尺码全量传进来，由这里判断哪些解析不了。
        seed_as_known=True 用于回填存量，直接标成已知、不产生告警。
        返回 {new: [...], total_unparsed: n, resolved: n}。
        """
        from app.core.sizing import normalize

        now = datetime.utcnow()
        seen: dict[str, dict] = {}
        for raw, sku in pairs:
            if not raw or str(raw).strip().upper() in ("HIDDEN", "-", ""):
                continue
            if normalize(raw) is not None:
                continue
            key = str(raw)
            e = seen.setdefault(key, {"count": 0, "skus": []})
            e["count"] += 1
            if len(e["skus"]) < MAX_SAMPLES and sku not in e["skus"]:
                e["skus"].append(sku)

        if not seen:
            return {"new": [], "total_unparsed": 0,
                    "resolved": self._resolve(source, site, set(), now)}

        # 哪些是第一次见 —— 只有它们值得打扰用户
        known = {d["raw"] for d in self.coll.find(
            {"source": source, "site": site, "raw": {"$in": list(seen)}}, {"raw": 1})}
        fresh = [r for r in seen if r not in known]

        ops = []
        for raw, e in seen.items():
            status = "known" if (seed_as_known or raw in known) else "new"
            ops.append(UpdateOne(
                {"source": source, "site": site, "raw": raw},
                {"$set": {"count": e["count"], "last_seen": now,
                          "sample_skus": e["skus"]},
                 "$setOnInsert": {"first_seen": now, "status": status,
                                  "alerted_at": None}},
                upsert=True))
        for i in range(0, len(ops), 500):
            self.coll.bulk_write(ops[i:i + 500], ordered=False)

        # 本轮出现过的写法之外，之前登记的若已能解析 -> 销账
        resolved = self._resolve(source, site, set(seen), now)
        return {"new": fresh, "total_unparsed": sum(e["count"] for e in seen.values()),
                "resolved": resolved}

    def _resolve(self, source: str, site: str | None,
                 still_bad: set[str], now: datetime) -> int:
        """规则补上后，旧条目会不再出现在 still_bad 里 —— 标记为已解决。

        不删记录：留着能回答「这个写法我们什么时候支持的」。
        """
        from app.core.sizing import normalize

        cur = self.coll.find({"source": source, "site": site,
                              "status": {"$ne": "resolved"}}, {"raw": 1})
        done = [d["_id"] for d in cur
                if d["raw"] not in still_bad and normalize(d["raw"]) is not None]
        if done:
            self.coll.update_many({"_id": {"$in": done}},
                                  {"$set": {"status": "resolved", "resolved_at": now}})
        return len(done)

    # ── 告警 ──────────────────────────────────────────────────────────
    def pending_alerts(self, limit: int = 50) -> list[dict]:
        return list(self.coll.find({"status": "new", "alerted_at": None},
                                   {"_id": 0})
                    .sort([("count", DESCENDING)]).limit(limit))

    def mark_alerted(self, source: str, site: str | None, raws: list[str]) -> int:
        if not raws:
            return 0
        r = self.coll.update_many(
            {"source": source, "site": site, "raw": {"$in": raws}},
            {"$set": {"status": "known", "alerted_at": datetime.utcnow()}})
        return r.modified_count

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"by_status": {}, "by_source": {}}
        for r in self.coll.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
            out["by_status"][r["_id"] or "?"] = r["n"]
        for r in self.coll.aggregate([{"$group": {"_id": "$source", "n": {"$sum": 1},
                                                  "hits": {"$sum": "$count"}}}]):
            out["by_source"][r["_id"] or "?"] = {"formats": r["n"], "occurrences": r["hits"]}
        return out
