"""Slack 配置接口"""
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException

from app import state
from app.services.slack_service import init_notifier

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/config", tags=["Config"])


@router.get("/slack", summary="查看 Slack 配置状态")
async def get_slack_config():
    """获取 Slack 配置状态"""
    if not state.notifier:
        return {"enabled": False, "reason": "未初始化"}

    return {
        "enabled": state.notifier.enabled,
        "webhook_configured": bool(state.notifier.webhook_url),
        "user_mention_enabled": bool(state.notifier.user_id),
    }


@router.post("/slack", summary="更新 Slack Webhook 配置")
async def update_slack_config(webhook_url: str, user_id: Optional[str] = None):
    """
    更新 Slack 配置

    参数:
        webhook_url: Slack Webhook URL
        user_id: Slack 用户 ID（用于 @mention）
    """
    try:
        state.notifier = init_notifier(webhook_url, user_id)
        logger.info("Slack 配置已更新")

        return {
            "status": "success",
            "enabled": state.notifier.enabled,
            "webhook_configured": bool(state.notifier.webhook_url),
        }

    except Exception as e:
        logger.error(f"更新 Slack 配置失败: {e}")
        raise HTTPException(status_code=400, detail=str(e))
