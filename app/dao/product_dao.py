"""products 集合的持久化（价格历史内嵌为 products.price_list 数组）"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

WRITE_BATCH_SIZE = 5000


class ProductDAO:
    """产品快照 + 内嵌价格历史（price_list）的读写"""

    def __init__(self, db):
        self.db = db
        self.collection = db["products"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        # 同一货号在不同站点（us/kr）价格不同，必须按 (sku, site) 区分。
        # 旧库上还留着 sku 单列唯一索引的话，国际站第一次写入就会 E11000
        # 整轮中断。
        try:
            self.collection.drop_index("sku_1")
        except Exception:
            pass
        self.collection.create_index([("sku", 1), ("site", 1)], unique=True)
        self.collection.create_index("site")
        self.collection.create_index("category")
        self.collection.create_index("updated_at")
        self.collection.create_index("created_at")
        # /arbitrage/freshness 的数据指纹要按站点取最新尺码时间，
        # 没索引就是内存阻塞排序，量大时会撞 32MB 限制让 /raw 直接 500
        self.collection.create_index([("site", 1), ("sizes_updated_at", -1)])

    def get_products(self, skus: list[str] | None = None) -> list[dict[str, Any]]:
        query = {}
        if skus:
            normalized = [str(s).strip().upper() for s in skus if str(s).strip()]
            query = {"sku": {"$in": normalized}}
        return list(self.collection.find(query, {"_id": 0}))

    # 监控列表扫描走的是 adidas.com/us 接口，快照只属于美国站。
    # 主键是 (sku, site)：不带 site 的话要么覆盖别国站的文档，
    # 要么 upsert 出一条 site=null 的孤儿，/lookup 就会出重复行。
    def find_by_sku(self, sku: str, site: str = "us") -> dict[str, Any] | None:
        return self.collection.find_one({"sku": sku, "site": site})

    def save_snapshot(self, sku: str, document: dict[str, Any], site: str = "us") -> None:
        """更新/插入单条产品快照（供扫描流程逐条写入）"""
        self.collection.update_one(
            {"sku": sku, "site": site},
            {
                "$set": {**document, "site": site},
                "$setOnInsert": {"created_at": datetime.utcnow()},
            },
            upsert=True,
        )

    def upsert_products(
        self,
        products: Iterable[dict[str, Any]],
        source_file: str | None = None,
        include_sizes: bool = False,
        record_price_history: bool = False,
    ) -> int:
        from pymongo import UpdateOne
        from pymongo.errors import BulkWriteError

        now = datetime.now(timezone.utc).replace(microsecond=0)
        batch_id = source_file or now.strftime("price_%Y%m%d_%H%M%S")
        total = len(products) if hasattr(products, "__len__") else None
        total_label = str(total) if total is not None else "?"
        # 每项 (sku, site, document, price_entry|None)，攒够一批再决定哪些要 $push
        pending: list[tuple[str, str, dict, dict | None]] = []
        new_skus: list[dict[str, str]] = []   # 本批首次入库的 (sku, site)
        count = 0
        products_written = 0
        price_points = 0
        print(f"MongoDB 开始同步: {total_label} 个 SKU", flush=True)

        def flush() -> None:
            nonlocal products_written, price_points
            if not pending:
                return
            ops, keys, pushed = self._build_ops(pending, now, batch_id, UpdateOne)
            price_points += pushed
            new_skus.extend(self._flush_products(ops, keys, BulkWriteError))
            products_written += len(ops)
            pending.clear()
            print(f"  products: {products_written}/{total_label}", flush=True)

        for item in products:
            sku = str(item.get("sku", "")).strip().upper()
            if not sku:
                continue

            document = dict(item)
            document["sku"] = sku
            site = str(item.get("site") or "us").lower()
            document["site"] = site
            document["updated_at"] = now
            document["source_file"] = source_file
            entry = None
            if record_price_history:
                document["last_price_observed_at"] = now
                document["last_price_batch_id"] = batch_id
                entry = {
                    "batch_id": batch_id,
                    "observed_at": now,
                    "sale_price": item.get("sale_price"),
                    "orig_price": item.get("orig_price"),
                    "discount_pct": item.get("discount_pct"),
                    "is_sold_out": item.get("is_sold_out", False),
                }
            if include_sizes:
                document["has_sizes"] = bool(item.get("sizes"))
            else:
                # Price-only refresh must preserve previously stored size data.
                document.pop("sizes", None)
                document.pop("overall_status", None)
                document.pop("checked_at", None)

            pending.append((sku, site, document, entry))
            count += 1
            if len(pending) >= WRITE_BATCH_SIZE:
                flush()

        flush()
        print(f"MongoDB 写入完成: products={products_written}, 新品={len(new_skus)}, "
              f"价格变化点={price_points}", flush=True)
        return {"count": count, "new_count": len(new_skus), "new_skus": new_skus,
                "price_points": price_points}

    @staticmethod
    def _price_key(e: dict | None) -> tuple:
        e = e or {}
        return (e.get("sale_price"), e.get("orig_price"), bool(e.get("is_sold_out", False)))

    def _build_ops(self, pending, now, batch_id, update_one):
        """把一批待写项变成 UpdateOne。

        price_list 只存「价格变化点」：先取这批商品 price_list 的最后一条，
        sale_price / orig_price / is_sold_out 三者都没变就不 $push ——
        实测 87% 的扫描价格与上次相同，逐次追加只会把文档撑大。
        「这个价最后一次确认是什么时候」由商品级 last_price_observed_at 承担。
        """
        need_last = [(s, t) for s, t, _, e in pending if e is not None]
        last: dict[tuple[str, str], dict | None] = {}
        if need_last:
            cur = self.collection.find(
                {"$or": [{"sku": s, "site": t} for s, t in need_last]},
                {"sku": 1, "site": 1, "price_list": {"$slice": -1}})
            for d in cur:
                pl = d.get("price_list") or []
                last[(d["sku"], d.get("site", "us"))] = pl[-1] if pl else None

        ops, keys, pushed = [], [], 0
        for sku, site, document, entry in pending:
            # 调用方可能把整条库里的文档原样传回来（fetch_sizes.py 就是）：
            # created_at 与 $setOnInsert 冲突会让整批报错（code 40），
            # price_list 被 $set 会把并发 $push 的变化点整段覆盖掉。
            for k in ("_id", "created_at", "first_seen_batch", "price_list"):
                document.pop(k, None)
            # $setOnInsert 只在首次插入时写，之后每轮扫描都不会覆盖 ——
            # 这是「这件商品第一次出现在官网」的唯一凭据，新品检测全靠它。
            update: dict[str, Any] = {
                "$set": document,
                "$setOnInsert": {"created_at": now, "first_seen_batch": batch_id},
            }
            if entry is not None and self._price_key(entry) != self._price_key(last.get((sku, site))):
                update["$push"] = {"price_list": entry}
                pushed += 1
                last[(sku, site)] = entry   # 同批再出现同一商品时，与这条比而不是与库里旧尾比
            ops.append(update_one({"sku": sku, "site": site}, update, upsert=True))
            keys.append((sku, site))
        return ops, keys, pushed

    def _flush_products(self, ops, keys, bulk_write_error) -> list[dict[str, str]]:
        """写一批 products，返回其中【首次插入】的 (sku, site)。

        bulk_write 的 upserted_ids 是 {ops 下标: _id}，下标对应传入顺序，
        据此回查 keys 就知道这批里哪些是新商品 —— 不用额外查一次库。

        重复键（旧唯一索引残留、并发写入）不该让整轮抓取崩掉：吞掉 11000，
        其余错误照旧抛出。
        """
        try:
            res = self.collection.bulk_write(ops, ordered=False)
            upserted = res.upserted_ids or {}
        except bulk_write_error as exc:
            details = getattr(exc, "details", {}) or {}
            others = [e for e in details.get("writeErrors", []) if e.get("code") != 11000]
            if others:
                raise
            upserted = {u["index"]: u["_id"] for u in details.get("upserted", [])}
            print(f"  警告：{len(details.get('writeErrors', []))} 条重复键已跳过", flush=True)
        return [{"sku": keys[i][0], "site": keys[i][1]}
                for i in upserted if i < len(keys)]


def sync_products_to_mongo(
    products: list[dict[str, Any]],
    source_file: str | None = None,
    include_sizes: bool = False,
    record_price_history: bool = False,
    required: bool = False,
) -> bool:
    """Sync to MongoDB when MONGODB_URI exists（供 CLI 脚本使用的便捷封装）"""
    from app.dao.mongo_client import MongoConnection

    conn = MongoConnection.from_environment(required=required)
    if conn is None:
        print("MongoDB 未配置，跳过云端同步（设置 MONGODB_URI 后自动启用）", flush=True)
        return False
    try:
        dao = ProductDAO(conn.db)
        stat = dao.upsert_products(
            products,
            source_file=source_file,
            include_sizes=include_sizes,
            record_price_history=record_price_history,
        )
        action = "价格历史+当前数据" if record_price_history else "当前数据"
        print(f"MongoDB 同步完成: {stat['count']} 个 SKU，"
              f"其中新品 {stat['new_count']} 个（{action}）", flush=True)
        return True
    finally:
        conn.close()
