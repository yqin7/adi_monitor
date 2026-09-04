"""观察列表接口"""
import logging
from typing import List
from fastapi import APIRouter, HTTPException

from app import state
from app.models.schemas import WatchItemRequest, WatchItemResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/watch", tags=["Watch List"])


@router.post("", summary="添加监控项", response_model=WatchItemResponse)
async def add_watch_item(item: WatchItemRequest):
    """
    添加观察项（要通知的货）

    参数:
        sku: 商品 SKU
        size: 尺码
        name: 商品名称（用于通知）
        color: 配色（用于通知）

    返回:
        创建的观察项
    """
    if not state.watch_service:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        watch_item = state.watch_service.add_watch_item(
            sku=item.sku,
            size=item.size,
            name=item.name,
            color=item.color,
        )

        logger.info(f"添加观察项: {item.sku} {item.size}")

        return WatchItemResponse(
            id=watch_item.id,
            sku=watch_item.sku,
            size=watch_item.size,
            name=watch_item.name,
            color=watch_item.color,
            created_at=watch_item.created_at.isoformat(),
            enabled=watch_item.enabled,
        )

    except Exception as e:
        logger.error(f"添加观察项失败: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", summary="列出监控项", response_model=List[WatchItemResponse])
async def list_watch_items(enabled_only: bool = True):
    """
    列出所有观察项

    参数:
        enabled_only: 仅返回启用的项
    """
    if not state.watch_service:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        items = state.watch_service.get_all_watch_items(enabled_only=enabled_only)

        return [
            WatchItemResponse(
                id=item.id,
                sku=item.sku,
                size=item.size,
                name=item.name,
                color=item.color,
                created_at=item.created_at.isoformat(),
                enabled=item.enabled,
            )
            for item in items
        ]

    except Exception as e:
        logger.error(f"列表观察项失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{item_id}", summary="删除监控项")
async def delete_watch_item(item_id: str):
    """
    删除观察项

    参数:
        item_id: 观察项 ID
    """
    if not state.watch_service:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        success = state.watch_service.delete_watch_item(item_id)
        if not success:
            raise HTTPException(status_code=404, detail="观察项不存在")

        logger.info(f"删除观察项: {item_id}")
        return {"message": "观察项已删除"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除观察项失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{item_id}/disable", summary="禁用监控项")
async def disable_watch_item(item_id: str):
    """禁用观察项（不删除，只标记禁用）"""
    if not state.watch_service:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        success = state.watch_service.disable_watch_item(item_id)
        if not success:
            raise HTTPException(status_code=404, detail="观察项不存在")

        logger.info(f"禁用观察项: {item_id}")
        return {"message": "观察项已禁用"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"禁用观察项失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{item_id}/enable", summary="启用监控项")
async def enable_watch_item(item_id: str):
    """启用观察项"""
    if not state.watch_service:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        success = state.watch_service.enable_watch_item(item_id)
        if not success:
            raise HTTPException(status_code=404, detail="观察项不存在")

        logger.info(f"启用观察项: {item_id}")
        return {"message": "观察项已启用"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启用观察项失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
