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
        # 货号 -> globalSkuId 映射缓存。得物商品目录很少变，缓存后可跳过接口140，
        # 是整个补全流程里最大的一笔调用节省。
        self.skumap = db["dewu_sku_map"]
        self.match = db["match_status"]
        self.arb = db["arbitrage"]

    def ensure_indexes(self) -> None:
        self.quotes.create_index([("sku", ASCENDING)], unique=True)
        self.quotes.create_index([("fetched_at", ASCENDING)])
        self.skumap.create_index([("sku", ASCENDING)], unique=True)
        self.skumap.create_index([("fetched_at", ASCENDING)])
        self.match.create_index([("sku", ASCENDING)], unique=True)
        self.match.create_index([("found", ASCENDING), ("last_tried_at", ASCENDING)])
        self.arb.create_index([("sku", ASCENDING), ("size", ASCENDING)], unique=True)
        self.arb.create_index([("profit", DESCENDING)])
        self.arb.create_index([("roi", DESCENDING)])
        self.arb.create_index([("monthly_sales", DESCENDING)])

    # ── 报价 ──────────────────────────────────────────────────────────────
    def save_quote(self, sku: str, payload: dict) -> None:
        """单条写入。批量场景请用 save_quotes —— 见那里的说明。"""
        payload = {**payload, "sku": sku, "fetched_at": datetime.utcnow()}
        self.quotes.update_one({"sku": sku}, {"$set": payload}, upsert=True)

    def save_quotes(self, items: list[tuple[str, dict]]) -> int:
        """批量写入报价。

        逐条 update_one 对着 Atlas 是每条一个网络往返，实测 259ms/条，
        200 个货号要 52 秒 —— 抓取日志里「已落库 200/6067」那两三分钟，
        有三分之一其实耗在这里，而不是在查得物接口。
        改成一次 bulk_write 后 13ms/条，整轮 6,067 个货号从 26 分钟降到 1.3 分钟。
        """
        if not items:
            return 0
        now = datetime.utcnow()
        ops = [UpdateOne({"sku": sku},
                         {"$set": {**payload, "sku": sku, "fetched_at": now}},
                         upsert=True)
               for sku, payload in items]
        written = 0
        for i in range(0, len(ops), 500):
            r = self.quotes.bulk_write(ops[i:i + 500], ordered=False)
            written += (r.upserted_count or 0) + (r.modified_count or 0)
        return written

    def get_quote(self, sku: str) -> dict | None:
        return self.quotes.find_one({"sku": sku})

    # ── 货号 -> globalSkuId 映射缓存 ──────────────────────────────────────
    def load_sku_maps(self, skus: Iterable[str], max_age_days: int = 30) -> dict[str, dict]:
        """批量取缓存的目录映射，过期的不返回（触发重新拉取）。"""
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        cur = self.skumap.find({"sku": {"$in": list(skus)}, "fetched_at": {"$gte": cutoff}})
        return {d["sku"]: d for d in cur}

    def save_sku_map(self, sku: str, info: dict) -> None:
        self.skumap.update_one(
            {"sku": sku},
            {"$set": {**info, "sku": sku, "fetched_at": datetime.utcnow()}},
            upsert=True,
        )

    # ── 命中状态 ──────────────────────────────────────────────────────────
    def mark_match(self, sku: str, found: bool) -> None:
        self.match.update_one(
            {"sku": sku},
            {"$set": {"found": found, "last_tried_at": datetime.utcnow()},
             "$inc": {"tries": 1}},
            upsert=True,
        )

    def skus_to_refresh(self, all_skus: Iterable[str], *, include_missed: bool,
                        missed_cooldown_days: int = 30,
                        max_quote_age_days: int | None = None) -> list[str]:
        """挑出本轮需要查询的货号，并按「最陈旧的排前面」返回。

        规则：
          - 从没查过的           -> 查（排最前，从无到有价值最高）
          - 已命中的             -> 查；若给了 max_quote_age_days，则报价还新鲜的跳过
          - 未命中的             -> 默认跳过；include_missed 时按冷却期重试

        max_quote_age_days 是省额度的关键：得物有日调用上限，不加这个参数时
        每轮都会把几千个已有新鲜报价的货号重查一遍。给个 7，就只补该补的。

        排序按报价时间升序 —— 中途被限流打断时，先跑完的是最该更新的那批。
        """
        from datetime import timedelta
        now = datetime.utcnow()
        cutoff = now - timedelta(days=missed_cooldown_days)
        quote_cutoff = (now - timedelta(days=max_quote_age_days)
                        if max_quote_age_days is not None else None)

        known = {d["sku"]: d for d in self.match.find({}, {"sku": 1, "found": 1, "last_tried_at": 1})}
        quoted = {d["sku"]: d.get("fetched_at")
                  for d in self.quotes.find({}, {"sku": 1, "fetched_at": 1})}

        out: list[tuple[datetime, str]] = []
        for sku in all_skus:
            rec = known.get(sku)
            fetched = quoted.get(sku)
            if rec is None:
                out.append((datetime.min, sku))
            elif rec.get("found"):
                if quote_cutoff and fetched and fetched >= quote_cutoff:
                    continue                       # 报价还新鲜，本轮不浪费额度
                out.append((fetched or datetime.min, sku))
            elif include_missed and (rec.get("last_tried_at") or datetime.min) < cutoff:
                out.append((rec.get("last_tried_at") or datetime.min, sku))

        out.sort(key=lambda t: t[0])
        return [sku for _, sku in out]

    # ── 数据新鲜度 ────────────────────────────────────────────────────────
    def quote_freshness(self) -> dict[str, Any]:
        """得物报价的时间分布：最新/最旧/中位数，以及分桶计数。"""
        from datetime import timedelta
        now = datetime.utcnow()
        total = self.quotes.count_documents({})
        if not total:
            return {"total": 0}

        newest = self.quotes.find_one({}, {"fetched_at": 1}, sort=[("fetched_at", DESCENDING)])
        oldest = self.quotes.find_one({}, {"fetched_at": 1}, sort=[("fetched_at", ASCENDING)])
        buckets = {}
        for label, days in (("d1", 1), ("d3", 3), ("d7", 7), ("d30", 30)):
            buckets[label] = self.quotes.count_documents(
                {"fetched_at": {"$gte": now - timedelta(days=days)}})
        buckets["over30"] = total - buckets["d30"]
        return {"total": total,
                "newest": (newest or {}).get("fetched_at"),
                "oldest": (oldest or {}).get("fetched_at"),
                "buckets": buckets}

    # ── 套利结果 ──────────────────────────────────────────────────────────
    def replace_arbitrage(self, rows: list[dict]) -> dict[str, int]:
        """整表替换：写入本轮结果，并清掉本轮不再成立的旧机会。

        原先只 upsert 不删 —— 名字叫 replace 却不 replace，昨天入选、
        今天价格变了已经不划算的行会永远留在 /arbitrage 列表里。
        用同一个 computed_at 打标，写完删掉所有更早的记录。
        """
        now = datetime.utcnow()
        if not rows:
            removed = self.arb.delete_many({}).deleted_count
            return {"written": 0, "removed": removed}
        ops = [UpdateOne({"sku": r["sku"], "size": r["size"]},
                         {"$set": {**r, "computed_at": now}}, upsert=True)
               for r in rows]
        written = 0
        for i in range(0, len(ops), 1000):
            res = self.arb.bulk_write(ops[i:i + 1000], ordered=False)
            written += (res.upserted_count or 0) + (res.modified_count or 0)
        removed = self.arb.delete_many({"computed_at": {"$lt": now}}).deleted_count
        return {"written": written, "removed": removed}

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
