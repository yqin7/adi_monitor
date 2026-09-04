"""应用级共享状态（在 FastAPI startup 时初始化的单例）"""
from typing import Optional

from app.dao.mongo_client import MongoConnection
from app.dao.product_dao import ProductDAO
from app.dao.watch_dao import WatchDAO
from app.dao.notification_dao import NotificationDAO
from app.services.slack_service import SlackNotifier
from app.services.scan_service import ScanService
from app.services.watch_service import WatchService
from app.services.notification_service import NotificationService
from app.services.job_service import JobService

config: Optional[dict] = None
mongo_conn: Optional[MongoConnection] = None
product_dao: Optional[ProductDAO] = None
watch_dao: Optional[WatchDAO] = None
notification_dao: Optional[NotificationDAO] = None
notifier: Optional[SlackNotifier] = None
scan_service: Optional[ScanService] = None
watch_service: Optional[WatchService] = None
notification_service: Optional[NotificationService] = None
job_service: Optional[JobService] = None
