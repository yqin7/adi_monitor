"""FastAPI 请求/响应模型（Pydantic）"""
from typing import List, Optional
from pydantic import BaseModel, Field

from app.services.full_scan_service import CATEGORIES


class WatchItemRequest(BaseModel):
    """添加观察项请求"""

    sku: str
    size: str
    name: str = ""
    color: str = ""


class WatchItemResponse(BaseModel):
    """观察项响应"""

    id: str
    sku: str
    size: str
    name: str
    color: str
    created_at: str
    enabled: bool


class NotificationResponse(BaseModel):
    """通知响应"""

    id: str
    sku: str
    size: str
    status: str
    message: str
    sent_at: str


class ScanResultResponse(BaseModel):
    """扫描结果响应"""

    status: str
    total_skus: int
    successful_scans: int
    failed_count: int
    success_rate: float
    notifications_sent: int
    duration_seconds: float
    new_in_stock_items: List[dict]


class ScanRequest(BaseModel):
    """监控列表扫描请求"""

    skus: Optional[List[str]] = Field(
        default=None,
        description="要扫描的 SKU 列表。留空则自动扫描当前监控列表（/watch）中的所有 SKU",
        examples=[["JR5408", "JR5410"]],
    )


class FullScanRequest(BaseModel):
    """全量扫描请求"""

    category: Optional[str] = Field(
        default=None,
        description=f"只扫描单个分类 slug，留空 = 扫描全部分类。可选值: {[c[0] for c in CATEGORIES]}",
    )
    plp_workers: int = Field(
        default=12, ge=1, le=32, description="分类翻页(PLP)并发线程数"
    )


class JobQueuedResponse(BaseModel):
    """任务已排队响应"""

    job_id: str
    status: str
    message: str


class JobResponse(BaseModel):
    """后台任务状态"""

    job_id: str
    type: str
    status: str = Field(description="queued | running | success | failed")
    stage: Optional[str] = None
    message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None


class SizeInfoResponse(BaseModel):
    """尺码库存信息"""

    sku: str
    size: str
    status: str
    qty: int = 0


class ProductQueryResponse(BaseModel):
    """单品实时查询结果"""

    sku: str
    name: str
    color: str
    currency: str
    original_price: Optional[float]
    sale_price: Optional[float]
    overall_status: str
    sizes: List[SizeInfoResponse]
    in_stock_sizes: List[SizeInfoResponse]
    checked_at: str
