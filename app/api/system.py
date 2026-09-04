"""系统相关接口：健康检查 / 根路由"""
from datetime import datetime
from fastapi import APIRouter

from app import state

router = APIRouter(tags=["System"])


@router.get("/health", summary="健康检查")
async def health_check():
    """健康检查端点"""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "mongodb": state.mongo_conn is not None,
        "slack": state.notifier is not None and state.notifier.enabled,
    }


@router.get("/", summary="服务信息 / 接口总览")
async def root():
    """根路由 —— 完整交互式文档见 /docs（Swagger）或 /redoc（ReDoc）"""
    return {
        "service": "Adidas Monitor Scanner",
        "version": "1.1.0",
        "docs": {"swagger": "/docs", "redoc": "/redoc"},
        "endpoints": {
            "health": "GET /health",
            "products": {
                "query_single": "GET /products/{sku}  — 实时查询单个商品尺码库存",
            },
            "scan": {
                "watch_list_scan": "POST /scan  — 扫描监控列表并通知",
                "full_scan": "POST /scan/full  — 立即全量扫描（仅价格）",
                "full_scan_with_sizes": "POST /scan/full-with-sizes  — 立即全量扫描（含尺码库存）",
                "job_status": "GET /scan/jobs/{job_id}  — 查询任务状态",
                "job_list": "GET /scan/jobs  — 列出最近任务",
            },
            "watch": {
                "list": "GET /watch",
                "add": "POST /watch",
                "delete": "DELETE /watch/{item_id}",
                "disable": "PATCH /watch/{item_id}/disable",
                "enable": "PATCH /watch/{item_id}/enable",
            },
            "notifications": "GET /notifications",
            "config": {
                "slack_status": "GET /config/slack",
                "slack_update": "POST /config/slack",
            },
        },
    }
