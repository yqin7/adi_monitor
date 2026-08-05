"""数据库操作扩展 - 观察列表和通知"""
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from models import WatchItem, NotificationRecord, ProductSnapshot


class WatchListStore:
    """观察列表数据库操作"""

    def __init__(self, mongo_collection):
        """
        初始化

        Args:
            mongo_collection: MongoDB 集合对象 (从 MongoStore 获取)
        """
        self.collection = mongo_collection
        self.db = mongo_collection.database
        self._init_collections()

    def _init_collections(self):
        """初始化集合和索引"""
        watch_list = self.db["watch_list"]
        notifications = self.db["notifications"]

        # 观察列表索引
        watch_list.create_index("sku")
        watch_list.create_index("size")
        watch_list.create_index([("sku", 1), ("size", 1)], unique=True)
        watch_list.create_index("enabled")
        watch_list.create_index("created_at")

        # 通知记录索引
        notifications.create_index("sku")
        notifications.create_index("watch_item_id")
        notifications.create_index("sent_at")
        notifications.create_index([("sku", 1), ("size", 1), ("sent_at", -1)])

    # ===== 观察列表 CRUD =====

    def add_watch_item(self, sku: str, size: str, name: str = "", color: str = "") -> WatchItem:
        """
        添加观察项

        Args:
            sku: 商品 SKU
            size: 尺码
            name: 商品名称
            color: 配色

        Returns:
            创建的观察项
        """
        watch_item = WatchItem(sku=sku, size=size, name=name, color=color)
        self.db["watch_list"].insert_one(watch_item.to_dict())
        return watch_item

    def get_watch_item(self, item_id: str) -> Optional[WatchItem]:
        """获取单个观察项"""
        doc = self.db["watch_list"].find_one({"id": item_id})
        if doc:
            doc.pop("_id", None)
            return WatchItem(**doc)
        return None

    def get_all_watch_items(self, enabled_only: bool = True) -> List[WatchItem]:
        """获取所有观察项"""
        query = {"enabled": True} if enabled_only else {}
        docs = list(self.db["watch_list"].find(query))
        items = []
        for doc in docs:
            doc.pop("_id", None)
            items.append(WatchItem(**doc))
        return items

    def get_watch_items_by_sku(self, sku: str, enabled_only: bool = True) -> List[WatchItem]:
        """按 SKU 获取观察项"""
        query = {"sku": sku}
        if enabled_only:
            query["enabled"] = True
        docs = list(self.db["watch_list"].find(query))
        items = []
        for doc in docs:
            doc.pop("_id", None)
            items.append(WatchItem(**doc))
        return items

    def delete_watch_item(self, item_id: str) -> bool:
        """删除观察项"""
        result = self.db["watch_list"].delete_one({"id": item_id})
        return result.deleted_count > 0

    def disable_watch_item(self, item_id: str) -> bool:
        """禁用观察项（不删除，只标记禁用）"""
        result = self.db["watch_list"].update_one({"id": item_id}, {"$set": {"enabled": False}})
        return result.modified_count > 0

    def enable_watch_item(self, item_id: str) -> bool:
        """启用观察项"""
        result = self.db["watch_list"].update_one({"id": item_id}, {"$set": {"enabled": True}})
        return result.modified_count > 0

    # ===== 通知记录 =====

    def record_notification(
        self,
        sku: str,
        size: str,
        watch_item_id: str,
        status: str = "IN_STOCK",
        message: str = "",
    ) -> NotificationRecord:
        """记录已发送的通知"""
        record = NotificationRecord(
            sku=sku, size=size, watch_item_id=watch_item_id, status=status, message=message
        )
        self.db["notifications"].insert_one(record.to_dict())
        return record

    def get_recent_notification(
        self, sku: str, size: str, hours: int = 2
    ) -> Optional[NotificationRecord]:
        """
        获取最近的通知记录（用于去重）

        检查过去 N 小时内是否已经通知过这个 SKU 的这个尺码

        Args:
            sku: 商品 SKU
            size: 尺码
            hours: 检查过去多少小时

        Returns:
            通知记录或 None
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        doc = self.db["notifications"].find_one(
            {"sku": sku, "size": size, "sent_at": {"$gte": cutoff_time}},
            sort=[("sent_at", -1)],
        )
        if doc:
            doc.pop("_id", None)
            return NotificationRecord(**doc)
        return None

    def get_notification_count(self, sku: str, size: str, hours: int = 24) -> int:
        """获取过去 N 小时内的通知次数"""
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        return self.db["notifications"].count_documents(
            {"sku": sku, "size": size, "sent_at": {"$gte": cutoff_time}}
        )

    def get_notifications(
        self, sku: Optional[str] = None, limit: int = 100
    ) -> List[NotificationRecord]:
        """获取通知历史"""
        query = {}
        if sku:
            query["sku"] = sku

        docs = list(self.db["notifications"].find(query).sort("sent_at", -1).limit(limit))
        records = []
        for doc in docs:
            doc.pop("_id", None)
            records.append(NotificationRecord(**doc))
        return records


# ===== 变化检测 =====


class ChangeDetector:
    """检测产品库存变化"""

    def __init__(self, mongo_collection):
        self.collection = mongo_collection
        self.db = mongo_collection.database
        self.watch_store = WatchListStore(mongo_collection)

    def detect_changes(self, current_snapshot: ProductSnapshot) -> Dict[str, List[str]]:
        """
        检测当前快照与上次的变化

        Args:
            current_snapshot: 当前的产品快照

        Returns:
            {
                'new_in_stock': [size1, size2],     # 新有货的尺码
                'went_out_of_stock': [size3],       # 刚下架的尺码
            }
        """
        sku = current_snapshot.sku
        previous = self.collection.find_one({"sku": sku})

        if not previous:
            # 第一次扫描，当前有货的都算"新有货"
            return {
                "new_in_stock": [s.size for s in current_snapshot.in_stock_sizes],
                "went_out_of_stock": [],
            }

        # 对比库存状态
        current_in_stock = {s.size for s in current_snapshot.in_stock_sizes}
        previous_in_stock = {s["size"] for s in previous.get("in_stock_sizes", [])}

        new_in_stock = current_in_stock - previous_in_stock
        went_out_of_stock = previous_in_stock - current_in_stock

        return {
            "new_in_stock": list(new_in_stock),
            "went_out_of_stock": list(went_out_of_stock),
        }

    def find_watched_changes(
        self, sku: str, changes: Dict[str, List[str]]
    ) -> List[Dict[str, Any]]:
        """
        找出用户关注的变化

        Args:
            sku: 商品 SKU
            changes: 检测到的变化

        Returns:
            [{
                'watch_item_id': str,
                'sku': str,
                'size': str,
                'name': str,
                'color': str,
                'change_type': 'new_in_stock' or 'went_out_of_stock'
            }]
        """
        watch_items = self.watch_store.get_watch_items_by_sku(sku)

        matched = []
        for watch_item in watch_items:
            if watch_item.size in changes["new_in_stock"]:
                matched.append(
                    {
                        "watch_item_id": watch_item.id,
                        "sku": sku,
                        "size": watch_item.size,
                        "name": watch_item.name,
                        "color": watch_item.color,
                        "change_type": "new_in_stock",
                    }
                )
            elif watch_item.size in changes["went_out_of_stock"]:
                matched.append(
                    {
                        "watch_item_id": watch_item.id,
                        "sku": sku,
                        "size": watch_item.size,
                        "name": watch_item.name,
                        "color": watch_item.color,
                        "change_type": "went_out_of_stock",
                    }
                )

        return matched
