"""FastAPI 微服务入口"""
import logging
from fastapi import FastAPI

from app import state
from app.core.config import load_config, get_mongodb_config
from app.dao.mongo_client import MongoConnection
from app.dao.product_dao import ProductDAO
from app.dao.watch_dao import WatchDAO
from app.dao.notification_dao import NotificationDAO
from app.services.slack_service import init_notifier
from app.services.scan_service import ScanService
from app.services.watch_service import WatchService
from app.services.notification_service import NotificationService
from app.services.job_service import JobService
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.core.paths import PROJECT_ROOT
from app.api import system, products, scan, watch, notifications, config_router, arbitrage, jobs

# ===== 日志配置 =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ===== FastAPI 应用 =====
app = FastAPI(
    title="Adidas Monitor Scanner",
    description="定时扫描 Adidas 商品库存，检测变化并通过 Slack 通知。"
    "查看下方各分组接口，或访问 /redoc 获取更详细的文档视图。",
    version="1.1.0",
)

app.include_router(system.router)
app.include_router(products.router)
app.include_router(scan.router)
app.include_router(watch.router)
app.include_router(notifications.router)
app.include_router(config_router.router)
app.include_router(arbitrage.router)
app.include_router(jobs.router)

_STATIC = PROJECT_ROOT / "app" / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# 页面改动频繁，一律禁用浏览器缓存，避免看到旧版本还以为改动没生效。
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate",
             "Pragma": "no-cache", "Expires": "0"}


@app.get("/ui", include_in_schema=False)
def ui():
    """比价结果网页。"""
    return FileResponse(str(_STATIC / "index.html"), headers=_NO_CACHE)


@app.get("/ui/tasks", include_in_schema=False)
def ui_tasks():
    """数据任务页：按国家触发抓取、查看进度与日志。

    与比价页分开 —— 任务是低频的运维操作，混在天天要看的比价表上方
    既占地方，也让「哪些国家参与本轮抓取」没地方放。
    """
    return FileResponse(str(_STATIC / "tasks.html"), headers=_NO_CACHE)


@app.on_event("startup")
async def startup():
    """应用启动初始化"""
    logger.info("应用启动中...")

    try:
        # 加载配置
        state.config = load_config()
        logger.info("配置加载成功")

        # 初始化 MongoDB
        uri, database, _collection, _history_collection = get_mongodb_config(state.config)
        state.mongo_conn = MongoConnection(uri, database)
        logger.info("MongoDB 连接成功")

        # 初始化 DAO
        state.product_dao = ProductDAO(state.mongo_conn.db)
        state.watch_dao = WatchDAO(state.mongo_conn.db)
        state.notification_dao = NotificationDAO(state.mongo_conn.db)
        logger.info("DAO 初始化成功")

        # 初始化 Slack 通知
        slack_config = state.config.get("slack", {})
        if slack_config.get("enabled"):
            webhook_url = slack_config.get("webhook_url")
            user_id = slack_config.get("mention_user_id")
            state.notifier = init_notifier(webhook_url, user_id)
            logger.info("Slack 通知已启用")
        else:
            state.notifier = init_notifier(None, None)
            logger.info("Slack 通知已禁用")

        # 初始化 Service
        concurrent_workers = state.config.get("notification", {}).get("concurrent_workers", 24)
        state.scan_service = ScanService(
            product_dao=state.product_dao,
            watch_dao=state.watch_dao,
            notification_dao=state.notification_dao,
            notifier=state.notifier,
            max_workers=concurrent_workers,
        )
        state.watch_service = WatchService(state.watch_dao)
        state.notification_service = NotificationService(state.notification_dao)
        state.job_service = JobService()
        logger.info(f"扫描服务初始化成功（{concurrent_workers} 个线程）")

    except Exception as e:
        logger.error(f"启动失败: {e}")
        raise


@app.on_event("shutdown")
async def shutdown():
    """应用关闭清理"""
    if state.mongo_conn:
        try:
            state.mongo_conn.close()
            logger.info("MongoDB 连接已关闭")
        except Exception as e:
            logger.error(f"关闭 MongoDB 失败: {e}")
