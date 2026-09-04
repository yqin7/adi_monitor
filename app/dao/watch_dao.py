"""watch_list 集合的持久化"""
from typing import List, Optional

from app.models.entities import WatchItem


class WatchDAO:
    """观察列表数据库操作"""

    def __init__(self, db):
        self.db = db
        self.collection = db["watch_list"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.collection.create_index("sku")
        self.collection.create_index("size")
        self.collection.create_index([("sku", 1), ("size", 1)], unique=True)
        self.collection.create_index("enabled")
        self.collection.create_index("created_at")

    def add(self, sku: str, size: str, name: str = "", color: str = "") -> WatchItem:
        """添加观察项"""
        watch_item = WatchItem(sku=sku, size=size, name=name, color=color)
        self.collection.insert_one(watch_item.to_dict())
        return watch_item

    def get(self, item_id: str) -> Optional[WatchItem]:
        """获取单个观察项"""
        doc = self.collection.find_one({"id": item_id})
        if doc:
            doc.pop("_id", None)
            return WatchItem(**doc)
        return None

    def get_all(self, enabled_only: bool = True) -> List[WatchItem]:
        """获取所有观察项"""
        query = {"enabled": True} if enabled_only else {}
        docs = list(self.collection.find(query))
        items = []
        for doc in docs:
            doc.pop("_id", None)
            items.append(WatchItem(**doc))
        return items

    def get_by_sku(self, sku: str, enabled_only: bool = True) -> List[WatchItem]:
        """按 SKU 获取观察项"""
        query = {"sku": sku}
        if enabled_only:
            query["enabled"] = True
        docs = list(self.collection.find(query))
        items = []
        for doc in docs:
            doc.pop("_id", None)
            items.append(WatchItem(**doc))
        return items

    def delete(self, item_id: str) -> bool:
        """删除观察项"""
        result = self.collection.delete_one({"id": item_id})
        return result.deleted_count > 0

    def disable(self, item_id: str) -> bool:
        """禁用观察项（不删除，只标记禁用）"""
        result = self.collection.update_one({"id": item_id}, {"$set": {"enabled": False}})
        return result.modified_count > 0

    def enable(self, item_id: str) -> bool:
        """启用观察项"""
        result = self.collection.update_one({"id": item_id}, {"$set": {"enabled": True}})
        return result.modified_count > 0
