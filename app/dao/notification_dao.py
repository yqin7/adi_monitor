"""notifications 集合的持久化"""
from datetime import datetime, timedelta
from typing import List, Optional

from app.models.entities import NotificationRecord


class NotificationDAO:
    """通知记录数据库操作"""

    def __init__(self, db):
        self.db = db
        self.collection = db["notifications"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.collection.create_index("sku")
        self.collection.create_index("watch_item_id")
        self.collection.create_index("sent_at")
        self.collection.create_index([("sku", 1), ("size", 1), ("sent_at", -1)])

    def record(
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
        self.collection.insert_one(record.to_dict())
        return record

    def get_recent(self, sku: str, size: str, hours: int = 2) -> Optional[NotificationRecord]:
        """
        获取最近的通知记录（用于去重）

        检查过去 N 小时内是否已经通知过这个 SKU 的这个尺码
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        doc = self.collection.find_one(
            {"sku": sku, "size": size, "sent_at": {"$gte": cutoff_time}},
            sort=[("sent_at", -1)],
        )
        if doc:
            doc.pop("_id", None)
            return NotificationRecord(**doc)
        return None

    def count_recent(self, sku: str, size: str, hours: int = 24) -> int:
        """获取过去 N 小时内的通知次数"""
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        return self.collection.count_documents(
            {"sku": sku, "size": size, "sent_at": {"$gte": cutoff_time}}
        )

    def list(self, sku: Optional[str] = None, limit: int = 100) -> List[NotificationRecord]:
        """获取通知历史"""
        query = {}
        if sku:
            query["sku"] = sku

        docs = list(self.collection.find(query).sort("sent_at", -1).limit(limit))
        records = []
        for doc in docs:
            doc.pop("_id", None)
            records.append(NotificationRecord(**doc))
        return records
