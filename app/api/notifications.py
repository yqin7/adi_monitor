"""通知历史接口"""
import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException

from app import state
from app.models.schemas import NotificationResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", summary="查询通知历史", response_model=List[NotificationResponse])
async def get_notifications(sku: Optional[str] = None, limit: int = 100):
    """
    获取通知历史

    参数:
        sku: 筛选特定 SKU（可选）
        limit: 最多返回多少条
    """
    if not state.notification_service:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        records = state.notification_service.get_notifications(sku=sku, limit=limit)

        return [
            NotificationResponse(
                id=record.id,
                sku=record.sku,
                size=record.size,
                status=record.status,
                message=record.message,
                sent_at=record.sent_at.isoformat(),
            )
            for record in records
        ]

    except Exception as e:
        logger.error(f"获取通知历史失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
