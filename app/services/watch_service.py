"""观察列表业务逻辑"""
from typing import List

from app.dao.watch_dao import WatchDAO
from app.models.entities import WatchItem


class WatchService:
    """封装观察列表的增删改查"""

    def __init__(self, watch_dao: WatchDAO):
        self.watch_dao = watch_dao

    def add_watch_item(self, sku: str, size: str, name: str = "", color: str = "") -> WatchItem:
        return self.watch_dao.add(sku=sku, size=size, name=name, color=color)

    def get_all_watch_items(self, enabled_only: bool = True) -> List[WatchItem]:
        return self.watch_dao.get_all(enabled_only=enabled_only)

    def delete_watch_item(self, item_id: str) -> bool:
        return self.watch_dao.delete(item_id)

    def disable_watch_item(self, item_id: str) -> bool:
        return self.watch_dao.disable(item_id)

    def enable_watch_item(self, item_id: str) -> bool:
        return self.watch_dao.enable(item_id)
