"""FastAPI 微服务入口"""
import os
import uuid
import logging
import threading
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime

from config import load_config, get_mongodb_config
from mongo_store import MongoStore
from notifier import init_notifier
from scanner import ParallelScanner
from storage import WatchListStore, ChangeDetector
from models import WatchItem
from adidas_monitor import check_sku
from fetch_all_skus_and_sizes import run_full_scan, CATEGORIES

# ===== 日志配置 =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 得物 OAuth 待回调的 state 集合（防 CSRF）
_DEWU_OAUTH_STATES: set = set()

# ===== FastAPI 应用 =====
app = FastAPI(
    title="Adidas Monitor Scanner",
    description="定时扫描 Adidas 商品库存，检测变化并通过 Slack 通知。"
    "查看下方各分组接口，或访问 /redoc 获取更详细的文档视图。",
    version="1.1.0",
)

# ===== 后台任务状态追踪（进程内内存存储，重启后清空） =====
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()

JOB_TYPE_WATCH_SCAN = "watch_scan"                  # 监控列表扫描
JOB_TYPE_FULL_SCAN = "full_scan"                    # 全量扫描（仅价格）
JOB_TYPE_FULL_SCAN_WITH_SIZES = "full_scan_with_sizes"  # 全量扫描 + 尺码库存

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


def _create_job(job_type: str) -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "type": job_type,
            "status": STATUS_QUEUED,
            "stage": None,
            "message": None,
            "created_at": datetime.utcnow().isoformat(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
    return job_id


def _update_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None

# ===== 全局状态 =====
config = None
mongo_store = None
watch_store = None
scanner = None
notifier = None


# ===== 请求/响应模型 =====


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


# ===== 初始化 =====


@app.on_event("startup")
async def startup():
    """应用启动初始化"""
    global config, mongo_store, watch_store, scanner, notifier

    logger.info("应用启动中...")

    try:
        # 加载配置
        config = load_config()
        logger.info("配置加载成功")

        # 初始化 MongoDB
        uri, database, collection, history_collection = get_mongodb_config(config)
        mongo_store = MongoStore(uri, database, collection)
        logger.info("MongoDB 连接成功")

        # 初始化数据存储
        watch_store = WatchListStore(mongo_store._collection)
        logger.info("观察列表存储初始化成功")

        # 初始化扫描器
        concurrent_workers = config.get("notification", {}).get("concurrent_workers", 24)
        scanner = ParallelScanner(mongo_store._collection, max_workers=concurrent_workers)
        logger.info(f"扫描器初始化成功（{concurrent_workers} 个线程）")

        # 初始化 Slack 通知
        slack_config = config.get("slack", {})
        if slack_config.get("enabled"):
            webhook_url = slack_config.get("webhook_url")
            user_id = slack_config.get("mention_user_id")
            notifier = init_notifier(webhook_url, user_id)
            logger.info(f"Slack 通知已启用")
        else:
            logger.info("Slack 通知已禁用")

    except Exception as e:
        logger.error(f"启动失败: {e}")
        raise


@app.on_event("shutdown")
async def shutdown():
    """应用关闭清理"""
    if mongo_store:
        try:
            mongo_store._client.close()
            logger.info("MongoDB 连接已关闭")
        except Exception as e:
            logger.error(f"关闭 MongoDB 失败: {e}")


# ===== 健康检查 =====


@app.get("/health", tags=["System"], summary="健康检查")
async def health_check():
    """健康检查端点"""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "mongodb": mongo_store is not None,
        "slack": notifier is not None and notifier.enabled,
    }


# ===== 单品实时查询接口 =====


@app.get(
    "/products/{sku}",
    tags=["Products"],
    summary="实时查询单个商品的尺码库存",
    response_model=ProductQueryResponse,
)
def get_product(sku: str):
    """
    实时查询单个 SKU 的价格和各尺码库存状态（直接请求 Adidas 官网，不经过数据库缓存）。

    路径参数:
        sku: 商品 SKU，如 JR5408

    返回:
        商品名称、配色、价格、每个尺码的库存状态和数量
    """
    try:
        result = check_sku(sku)
        return result
    except Exception as e:
        logger.error(f"查询商品 {sku} 失败: {e}")
        raise HTTPException(status_code=404, detail=f"查询 {sku} 失败: {e}")


# ===== 监控列表扫描接口（原有功能：按需扫描 + 通知） =====


@app.post(
    "/scan",
    tags=["Scan"],
    summary="扫描监控列表（触发变化检测 + Slack 通知）",
    response_model=JobQueuedResponse,
)
async def trigger_scan(background_tasks: BackgroundTasks, req: ScanRequest = ScanRequest()):
    """
    扫描指定 SKU（或当前监控列表中的全部 SKU），检测库存变化并对匹配监控项发送 Slack 通知。

    这是轻量扫描，只扫描传入/监控列表中的 SKU，不会去发现全站新商品。
    如需发现全站所有 SKU，请使用 `/scan/full` 或 `/scan/full-with-sizes`。
    """
    if not scanner:
        raise HTTPException(status_code=500, detail="扫描器未初始化")

    skus = req.skus
    if not skus:
        watch_items = watch_store.get_all_watch_items(enabled_only=True)
        skus = sorted({item.sku for item in watch_items})
        if not skus:
            raise HTTPException(
                status_code=400,
                detail="监控列表为空，请先通过 POST /watch 添加要监控的商品，或在请求体中提供 skus 列表",
            )

    job_id = _create_job(JOB_TYPE_WATCH_SCAN)
    background_tasks.add_task(_run_watch_scan_job, job_id, skus)

    return JobQueuedResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        message=f"扫描任务已排队（{len(skus)} 个 SKU），可通过 GET /scan/jobs/{{job_id}} 查询进度",
    )


def _run_watch_scan_job(job_id: str, skus: List[str]):
    """后台执行监控列表扫描"""
    _update_job(job_id, status=STATUS_RUNNING, started_at=datetime.utcnow().isoformat())
    try:
        logger.info(f"[{job_id}] 开始扫描 {len(skus)} 个 SKU")
        result = scanner.scan_skus(skus)
        scanner.save_snapshots(result.snapshots)

        summary = {
            "total_skus": result.total_skus,
            "successful_scans": result.successful_scans,
            "failed_count": len(result.failed_skus),
            "success_rate": result.success_rate(),
            "notifications_sent": result.notifications_sent,
            "duration_seconds": result.duration_seconds(),
            "new_in_stock_items": result.new_in_stock_items,
        }
        _update_job(
            job_id,
            status=STATUS_SUCCESS,
            result=summary,
            finished_at=datetime.utcnow().isoformat(),
        )
        logger.info(f"[{job_id}] 扫描完成: {summary}")

    except Exception as e:
        logger.error(f"[{job_id}] 扫描失败: {e}")
        _update_job(
            job_id, status=STATUS_FAILED, error=str(e), finished_at=datetime.utcnow().isoformat()
        )


# ===== 全量扫描接口（发现全站 SKU） =====


@app.post(
    "/scan/full",
    tags=["Scan"],
    summary="立即触发全量扫描（仅价格，不抓尺码库存，速度快）",
    response_model=JobQueuedResponse,
)
async def trigger_full_scan(background_tasks: BackgroundTasks, req: FullScanRequest = FullScanRequest()):
    """
    爬取全站分类页（PLP）发现所有 SKU 及价格，写入 MongoDB（含价格历史）。

    不会抓取每个 SKU 的尺码库存，速度较快（几分钟级别）。
    如需同时获取尺码库存，请使用 `/scan/full-with-sizes`（耗时更长，10-20 分钟级别）。
    """
    job_id = _create_job(JOB_TYPE_FULL_SCAN)
    background_tasks.add_task(
        _run_full_scan_job, job_id, False, req.category, req.plp_workers
    )
    return JobQueuedResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        message=f"全量扫描（仅价格）任务已排队，可通过 GET /scan/jobs/{job_id} 查询进度",
    )


@app.post(
    "/scan/full-with-sizes",
    tags=["Scan"],
    summary="立即触发全量扫描（含尺码库存，耗时较长）",
    response_model=JobQueuedResponse,
)
async def trigger_full_scan_with_sizes(
    background_tasks: BackgroundTasks, req: FullScanRequest = FullScanRequest()
):
    """
    爬取全站分类页（PLP）发现所有 SKU 及价格，并为每个 SKU 补充尺码库存，全部写入 MongoDB。

    这是最完整的扫描（对应定时任务每 30 分钟执行的内容），
    10,000-20,000 个 SKU 预计耗时 10-20 分钟。
    """
    job_id = _create_job(JOB_TYPE_FULL_SCAN_WITH_SIZES)
    background_tasks.add_task(
        _run_full_scan_job, job_id, True, req.category, req.plp_workers
    )
    return JobQueuedResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        message=f"全量扫描（含尺码）任务已排队，可通过 GET /scan/jobs/{job_id} 查询进度",
    )


def _run_full_scan_job(job_id: str, include_sizes: bool, category: Optional[str], plp_workers: int):
    """后台执行全量扫描（复用 fetch_all_skus_and_sizes.run_full_scan）"""
    _update_job(job_id, status=STATUS_RUNNING, started_at=datetime.utcnow().isoformat())

    def progress(stage: str, message: str):
        _update_job(job_id, stage=stage, message=message)

    try:
        logger.info(f"[{job_id}] 开始全量扫描 (include_sizes={include_sizes}, category={category})")
        summary = run_full_scan(
            category=category,
            plp_workers=plp_workers,
            include_sizes=include_sizes,
            progress_cb=progress,
        )
        _update_job(
            job_id,
            status=STATUS_SUCCESS,
            result=summary,
            finished_at=datetime.utcnow().isoformat(),
        )
        logger.info(f"[{job_id}] 全量扫描完成: {summary}")

    except Exception as e:
        logger.error(f"[{job_id}] 全量扫描失败: {e}")
        _update_job(
            job_id, status=STATUS_FAILED, error=str(e), finished_at=datetime.utcnow().isoformat()
        )


@app.get(
    "/scan/jobs/{job_id}",
    tags=["Scan"],
    summary="查询扫描任务状态",
    response_model=JobResponse,
)
async def get_scan_job(job_id: str):
    """查询某次扫描任务（/scan、/scan/full、/scan/full-with-sizes 触发的任务）的执行状态和结果"""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.get(
    "/scan/jobs",
    tags=["Scan"],
    summary="列出最近的扫描任务",
    response_model=List[JobResponse],
)
async def list_scan_jobs(limit: int = 20):
    """列出最近触发的扫描任务，按创建时间倒序排列"""
    with _jobs_lock:
        jobs = list(_jobs.values())
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return jobs[:limit]


# ===== 观察列表接口 =====


@app.post("/watch", tags=["Watch List"], summary="添加监控项", response_model=WatchItemResponse)
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
    if not watch_store:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        watch_item = watch_store.add_watch_item(
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


@app.get("/watch", tags=["Watch List"], summary="列出监控项", response_model=List[WatchItemResponse])
async def list_watch_items(enabled_only: bool = True):
    """
    列出所有观察项

    参数:
        enabled_only: 仅返回启用的项
    """
    if not watch_store:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        items = watch_store.get_all_watch_items(enabled_only=enabled_only)

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


@app.delete("/watch/{item_id}", tags=["Watch List"], summary="删除监控项")
async def delete_watch_item(item_id: str):
    """
    删除观察项

    参数:
        item_id: 观察项 ID
    """
    if not watch_store:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        success = watch_store.delete_watch_item(item_id)
        if not success:
            raise HTTPException(status_code=404, detail="观察项不存在")

        logger.info(f"删除观察项: {item_id}")
        return {"message": "观察项已删除"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除观察项失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/watch/{item_id}/disable", tags=["Watch List"], summary="禁用监控项")
async def disable_watch_item(item_id: str):
    """禁用观察项（不删除，只标记禁用）"""
    if not watch_store:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        success = watch_store.disable_watch_item(item_id)
        if not success:
            raise HTTPException(status_code=404, detail="观察项不存在")

        logger.info(f"禁用观察项: {item_id}")
        return {"message": "观察项已禁用"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"禁用观察项失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/watch/{item_id}/enable", tags=["Watch List"], summary="启用监控项")
async def enable_watch_item(item_id: str):
    """启用观察项"""
    if not watch_store:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        success = watch_store.enable_watch_item(item_id)
        if not success:
            raise HTTPException(status_code=404, detail="观察项不存在")

        logger.info(f"启用观察项: {item_id}")
        return {"message": "观察项已启用"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启用观察项失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== 通知历史接口 =====


@app.get(
    "/notifications",
    tags=["Notifications"],
    summary="查询通知历史",
    response_model=List[NotificationResponse],
)
async def get_notifications(sku: Optional[str] = None, limit: int = 100):
    """
    获取通知历史

    参数:
        sku: 筛选特定 SKU（可选）
        limit: 最多返回多少条
    """
    if not watch_store:
        raise HTTPException(status_code=500, detail="观察列表存储未初始化")

    try:
        records = watch_store.get_notifications(sku=sku, limit=limit)

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


# ===== Slack 配置接口 =====


@app.get("/config/slack", tags=["Config"], summary="查看 Slack 配置状态")
async def get_slack_config():
    """获取 Slack 配置状态"""
    if not notifier:
        return {"enabled": False, "reason": "未初始化"}

    return {
        "enabled": notifier.enabled,
        "webhook_configured": bool(notifier.webhook_url),
        "user_mention_enabled": bool(notifier.user_id),
    }


@app.post("/config/slack", tags=["Config"], summary="更新 Slack Webhook 配置")
async def update_slack_config(webhook_url: str, user_id: Optional[str] = None):
    """
    更新 Slack 配置

    参数:
        webhook_url: Slack Webhook URL
        user_id: Slack 用户 ID（用于 @mention）
    """
    global notifier

    try:
        notifier = init_notifier(webhook_url, user_id)
        logger.info("Slack 配置已更新")

        return {
            "status": "success",
            "enabled": notifier.enabled,
            "webhook_configured": bool(notifier.webhook_url),
        }

    except Exception as e:
        logger.error(f"更新 Slack 配置失败: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ===== 根路由 =====


@app.get("/", tags=["System"], summary="服务信息 / 接口总览")
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


# ===== 得物 OAuth 授权 =====

def _dewu_oauth(sandbox: bool = False):
    """按环境构造 OAuth 助手"""
    from dewu_client import DewuClient
    from dewu_oauth import DewuOAuth

    prefix = "DEWU_SANDBOX_" if sandbox else "DEWU_"
    client = DewuClient(
        os.getenv(f"{prefix}APP_KEY", ""),
        os.getenv(f"{prefix}APP_SECRET", ""),
        sandbox=sandbox,
    )
    return DewuOAuth(client)


@app.get("/dewu/oauth/authorize", tags=["Dewu OAuth"], summary="跳转到得物授权页")
def dewu_oauth_authorize(sandbox: bool = Query(False, description="使用沙箱环境")):
    """生成随机 state 并 302 到得物授权页"""
    state = uuid.uuid4().hex
    _DEWU_OAUTH_STATES.add(state)
    try:
        url = _dewu_oauth(sandbox).authorize_url(state=state)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"得物凭证未配置: {e}")
    return RedirectResponse(url)


@app.get("/dewu/oauth/callback", tags=["Dewu OAuth"], summary="得物授权回调")
def dewu_oauth_callback(
    code: str = Query(..., description="授权码"),
    state: str = Query("", description="发起授权时下发的 state"),
    sandbox: bool = Query(False, description="使用沙箱环境"),
):
    """接收授权码并换取 access_token"""
    if state and state not in _DEWU_OAUTH_STATES:
        raise HTTPException(status_code=400, detail="state 校验失败，疑似 CSRF")
    _DEWU_OAUTH_STATES.discard(state)

    try:
        token = _dewu_oauth(sandbox).exchange_code(code)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    logger.info("得物授权成功，access_token 已缓存")
    return {
        "success": True,
        "expires_in": token.get("expires_in"),
        "has_refresh_token": bool(token.get("refresh_token")),
    }


@app.get("/dewu/oauth/status", tags=["Dewu OAuth"], summary="查看授权状态")
def dewu_oauth_status(sandbox: bool = Query(False, description="使用沙箱环境")):
    """不回显 token 本身，只报有效性"""
    token = _dewu_oauth(sandbox).store.load()
    if not token.get("access_token"):
        return {"authorized": False}
    expires_at = token.get("expires_at", 0)
    return {
        "authorized": True,
        "expires_at": datetime.fromtimestamp(expires_at).isoformat() if expires_at else None,
        "expired": bool(expires_at and expires_at <= datetime.now().timestamp()),
        "has_refresh_token": bool(token.get("refresh_token")),
    }


@app.get("/dewu/product/{article_number}", tags=["Dewu"],
         summary="按货号查得物商品")
def dewu_product_by_article(
    article_number: str,
    sandbox: bool = Query(False, description="使用沙箱环境"),
):
    """用 adidas 货号查得物 spu、尺码 sku 列表与官方指导价"""
    from dewu_price import build_query

    try:
        products = build_query(sandbox).lookup_article_numbers([article_number])
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"得物凭证未配置: {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if not products:
        raise HTTPException(status_code=404, detail=f"得物没有货号 {article_number}")
    return products[0]


@app.get("/dewu/income/{sku_id}", tags=["Dewu"], summary="按出价算到手价")
def dewu_expect_income(
    sku_id: int,
    bidding_price_fen: int = Query(..., description="出价，单位分"),
    bidding_type: int = Query(0, description="出价类型，0 为现货"),
    sandbox: bool = Query(False, description="使用沙箱环境"),
):
    """给定出价，返回各项手续费与到手价"""
    from dewu_price import build_query

    try:
        return build_query(sandbox).expect_income(sku_id, bidding_price_fen, bidding_type)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"得物凭证未配置: {e}")
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
