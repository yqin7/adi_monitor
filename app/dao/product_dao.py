"""products / price_history 集合的持久化"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

WRITE_BATCH_SIZE = 5000


class ProductDAO:
    """产品快照 + 价格历史的读写"""

    def __init__(self, db):
        self.db = db
        self.collection = db["products"]
        self.history = db["price_history"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        # 同一货号在不同站点（us/kr）价格不同，必须按 (sku, site) 区分
        self.collection.create_index([("sku", 1), ("site", 1)], unique=True)
        self.collection.create_index("site")
        self.collection.create_index("category")
        self.collection.create_index("updated_at")
        try:
            self.history.drop_index("sku_1_snapshot_id_1")
        except Exception:
            pass
        self.history.create_index([("sku", 1), ("observed_at", -1)])
        try:
            self.history.drop_index("sku_1_observed_at_1")
        except Exception:
            pass
        self.history.create_index([("sku", 1), ("site", 1), ("batch_id", 1)], unique=True)

    def get_products(self, skus: list[str] | None = None) -> list[dict[str, Any]]:
        query = {}
        if skus:
            normalized = [str(s).strip().upper() for s in skus if str(s).strip()]
            query = {"sku": {"$in": normalized}}
        return list(self.collection.find(query, {"_id": 0}))

    def find_by_sku(self, sku: str) -> dict[str, Any] | None:
        return self.collection.find_one({"sku": sku})

    def save_snapshot(self, sku: str, document: dict[str, Any]) -> None:
        """更新/插入单条产品快照（供扫描流程逐条写入）"""
        self.collection.update_one(
            {"sku": sku},
            {
                "$set": document,
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
        product_ops = []
        history_docs = []
        count = 0
        products_written = 0
        history_written = 0
        print(f"MongoDB 开始同步: {total_label} 个 SKU", flush=True)

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
            if record_price_history:
                document["last_price_observed_at"] = now
                document["last_price_batch_id"] = batch_id
            if include_sizes:
                document["has_sizes"] = bool(item.get("sizes"))
            else:
                # Price-only refresh must preserve previously stored size data.
                document.pop("sizes", None)
                document.pop("overall_status", None)
                document.pop("checked_at", None)

            product_ops.append(UpdateOne({"sku": sku, "site": site},
                                        {"$set": document}, upsert=True))

            if record_price_history:
                history_doc = {
                    "sku": sku,
                    "site": site,
                    "batch_id": batch_id,
                    "observed_at": now,
                    "sale_price": item.get("sale_price"),
                    "orig_price": item.get("orig_price"),
                    "discount_pct": item.get("discount_pct"),
                    "is_sold_out": item.get("is_sold_out", False),
                    "name": item.get("name"),
                }
                # Price history is append-only. A plain insert is faster than
                # an UpdateOne(upsert=True) because each new batch has a new batch_id.
                history_docs.append(history_doc)
            count += 1

            if len(product_ops) >= WRITE_BATCH_SIZE:
                batch_size = len(product_ops)
                self.collection.bulk_write(product_ops, ordered=False)
                products_written += batch_size
                product_ops.clear()
                print(f"  products: {products_written}/{total_label}", flush=True)
            if len(history_docs) >= WRITE_BATCH_SIZE:
                history_written += self._insert_history(history_docs, BulkWriteError)
                history_docs.clear()
                print(f"  price_history: {history_written}/{total_label}", flush=True)

        if product_ops:
            batch_size = len(product_ops)
            self.collection.bulk_write(product_ops, ordered=False)
            products_written += batch_size
            print(f"  products: {products_written}/{total_label}", flush=True)
        if history_docs:
            history_written += self._insert_history(history_docs, BulkWriteError)
            print(f"  price_history: {history_written}/{total_label}", flush=True)
        print(f"MongoDB 写入完成: products={products_written}, price_history={history_written}", flush=True)
        return count

    def _insert_history(self, documents: list[dict[str, Any]], bulk_write_error: type[Exception]) -> int:
        """Insert one append-only history batch and tolerate same-batch reruns."""
        try:
            result = self.history.insert_many(documents, ordered=False)
            return len(result.inserted_ids)
        except bulk_write_error as exc:
            details = getattr(exc, "details", {}) or {}
            write_errors = details.get("writeErrors", [])
            non_duplicate = [error for error in write_errors if error.get("code") != 11000]
            if non_duplicate:
                raise
            return int(details.get("nInserted", 0))


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
        count = dao.upsert_products(
            products,
            source_file=source_file,
            include_sizes=include_sizes,
            record_price_history=record_price_history,
        )
        action = "价格历史+当前数据" if record_price_history else "当前数据"
        print(f"MongoDB 同步完成: {count} 个 SKU（{action}）", flush=True)
        return True
    finally:
        conn.close()
