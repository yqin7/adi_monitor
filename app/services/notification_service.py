"""通知历史查询业务逻辑"""
from typing import List, Optional

from app.dao.notification_dao import NotificationDAO
from app.models.entities import NotificationRecord


class NotificationService:
    """封装通知历史查询"""

    def __init__(self, notification_dao: NotificationDAO):
        self.notification_dao = notification_dao

    def get_notifications(self, sku: Optional[str] = None, limit: int = 100) -> List[NotificationRecord]:
        return self.notification_dao.list(sku=sku, limit=limit)
