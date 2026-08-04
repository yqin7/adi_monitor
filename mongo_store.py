"""MongoDB Atlas persistence for Adidas product snapshots."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable


class MongoStore:
    def __init__(self, uri: str, database: str = "adidas_monitor", collection: str = "products"):
        try:
            from pymongo import MongoClient
        except ImportError as exc:
            raise RuntimeError("MongoDB support requires pymongo: pip install pymongo") from exc

        self._client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        self._db = self._client[database]
        self._collection = self._db[collection]
        self._history = self._db["price_history"]
        self._collection.create_index("sku", unique=True)
        self._collection.create_index("category")
        self._collection.create_index("updated_at")
        try:
            self._history.drop_index("sku_1_snapshot_id_1")
        except Exception:
            pass
        self._history.create_index([("sku", 1), ("observed_at", -1)])
        try:
            self._history.drop_index("sku_1_observed_at_1")
        except Exception:
            pass
        self._history.create_index([("sku", 1), ("batch_id", 1)], unique=True)
        self._client.admin.command("ping")

    @classmethod
    def from_environment(cls, required: bool = False) -> "MongoStore | None":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            if required:
                raise RuntimeError("MONGODB_URI is not configured")
            return None
        return cls(
            uri,
            database=os.getenv("MONGODB_DATABASE", "adidas_monitor"),
            collection=os.getenv("MONGODB_COLLECTION", "products"),
        )

    def upsert_products(
        self,
        products: Iterable[dict[str, Any]],
        source_file: str | None = None,
        include_sizes: bool = False,
        record_price_history: bool = False,
    ) -> int:
        from pymongo import UpdateOne

        now = datetime.now(timezone.utc).replace(microsecond=0)
        batch_id = source_file or now.strftime("price_%Y%m%d_%H%M%S")
        total = len(products) if hasattr(products, "__len__") else None
        total_label = str(total) if total is not None else "?"
        product_ops = []
        history_ops = []
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

            product_ops.append(UpdateOne({"sku": sku}, {"$set": document}, upsert=True))

            if record_price_history:
                history_doc = {
                    "sku": sku,
                    "batch_id": batch_id,
                    "observed_at": now,
                    "sale_price": item.get("sale_price"),
                    "orig_price": item.get("orig_price"),
                    "discount_pct": item.get("discount_pct"),
                    "is_sold_out": item.get("is_sold_out", False),
                    "name": item.get("name"),
                }
                history_ops.append(
                    UpdateOne(
                        {"sku": sku, "batch_id": batch_id},
                        {"$setOnInsert": history_doc},
                        upsert=True,
                    )
                )
            count += 1

            if len(product_ops) >= 500:
                batch_size = len(product_ops)
                self._collection.bulk_write(product_ops, ordered=False)
                products_written += batch_size
                product_ops.clear()
                print(f"  products: {products_written}/{total_label}", flush=True)
            if len(history_ops) >= 500:
                batch_size = len(history_ops)
                self._history.bulk_write(history_ops, ordered=False)
                history_written += batch_size
                history_ops.clear()
                print(f"  price_history: {history_written}/{total_label}", flush=True)

        if product_ops:
            batch_size = len(product_ops)
            self._collection.bulk_write(product_ops, ordered=False)
            products_written += batch_size
            print(f"  products: {products_written}/{total_label}", flush=True)
        if history_ops:
            batch_size = len(history_ops)
            self._history.bulk_write(history_ops, ordered=False)
            history_written += batch_size
            print(f"  price_history: {history_written}/{total_label}", flush=True)
        print(f"MongoDB 写入完成: products={products_written}, price_history={history_written}", flush=True)
        return count

    def get_products(self, skus: list[str] | None = None) -> list[dict[str, Any]]:
        query = {}
        if skus:
            normalized = [str(s).strip().upper() for s in skus if str(s).strip()]
            query = {"sku": {"$in": normalized}}
        return list(self._collection.find(query, {"_id": 0}))
    def close(self) -> None:
        self._client.close()


def sync_products_to_mongo(
    products: list[dict[str, Any]],
    source_file: str | None = None,
    include_sizes: bool = False,
    record_price_history: bool = False,
    required: bool = False,
) -> bool:
    """Sync to MongoDB when MONGODB_URI exists."""
    store = MongoStore.from_environment(required=required)
    if store is None:
        print("MongoDB 未配置，跳过云端同步（设置 MONGODB_URI 后自动启用）", flush=True)
        return False
    try:
        count = store.upsert_products(
            products,
            source_file=source_file,
            include_sizes=include_sizes,
            record_price_history=record_price_history,
        )
        action = "价格历史+当前数据" if record_price_history else "当前数据"
        print(f"MongoDB 同步完成: {count} 个 SKU（{action}）", flush=True)
        return True
    finally:
        store.close()