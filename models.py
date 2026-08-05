"""数据模型定义"""
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass, asdict
from enum import Enum
import uuid


class SizeStatus(Enum):
    """尺码库存状态"""
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


@dataclass
class SizeInfo:
    """尺码信息"""
    sku: str
    size: str
    status: str
    qty: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class ProductSnapshot:
    """产品快照（一次扫描结果）"""
    sku: str
    name: str
    color: str
    currency: str
    original_price: Optional[float]
    sale_price: Optional[float]
    overall_status: str
    sizes: List[SizeInfo]
    in_stock_sizes: List[SizeInfo]
    checked_at: datetime

    def to_dict(self):
        return {
            "sku": self.sku,
            "name": self.name,
            "color": self.color,
            "currency": self.currency,
            "original_price": self.original_price,
            "sale_price": self.sale_price,
            "overall_status": self.overall_status,
            "sizes": [s.to_dict() for s in self.sizes],
            "in_stock_sizes": [s.to_dict() for s in self.in_stock_sizes],
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass
class WatchItem:
    """观察项 - 用户想要通知的货"""
    id: str = None
    sku: str = None
    size: str = None
    name: str = ""  # 商品名称，用于通知显示
    color: str = ""  # 配色
    created_at: datetime = None
    enabled: bool = True

    def __post_init__(self):
        if self.id is None:
            self.id = str(uuid.uuid4())
        if self.created_at is None:
            self.created_at = datetime.utcnow()

    def to_dict(self):
        return {
            "id": self.id,
            "sku": self.sku,
            "size": self.size,
            "name": self.name,
            "color": self.color,
            "created_at": self.created_at,
            "enabled": self.enabled,
        }


@dataclass
class NotificationRecord:
    """通知记录"""
    id: str = None
    sku: str = None
    size: str = None
    watch_item_id: str = None
    status: str = "IN_STOCK"  # IN_STOCK, BACK_IN_STOCK, OUT_OF_STOCK
    message: str = ""
    sent_at: datetime = None
    slack_ts: str = None  # Slack message timestamp

    def __post_init__(self):
        if self.id is None:
            self.id = str(uuid.uuid4())
        if self.sent_at is None:
            self.sent_at = datetime.utcnow()

    def to_dict(self):
        return {
            "id": self.id,
            "sku": self.sku,
            "size": self.size,
            "watch_item_id": self.watch_item_id,
            "status": self.status,
            "message": self.message,
            "sent_at": self.sent_at,
            "slack_ts": self.slack_ts,
        }
