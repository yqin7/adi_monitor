"""并行扫描引擎 - 支持大规模 SKU 扫描"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any
from datetime import datetime

from adidas_monitor import check_sku
from models import ProductSnapshot, SizeInfo
from notifier import get_notifier
from storage import ChangeDetector

logger = logging.getLogger(__name__)


class ScanResult:
    """扫描结果汇总"""

    def __init__(self):
        self.total_skus = 0
        self.successful_scans = 0
        self.failed_skus: List[str] = []
        self.snapshots: List[ProductSnapshot] = []
        self.new_in_stock_items: List[Dict[str, Any]] = []  # 有货的变化
        self.notifications_sent = 0
        self.start_time = datetime.utcnow()
        self.end_time: Optional[datetime] = None

    def duration_seconds(self) -> float:
        """扫描耗时（秒）"""
        end = self.end_time or datetime.utcnow()
        return (end - self.start_time).total_seconds()

    def success_rate(self) -> float:
        """成功率百分比"""
        if self.total_skus == 0:
            return 0
        return (self.successful_scans / self.total_skus) * 100


class ParallelScanner:
    """并行扫描器"""

    def __init__(self, mongo_collection, max_workers: int = 24, max_retries: int = 3):
        """
        初始化扫描器

        Args:
            mongo_collection: MongoDB 集合
            max_workers: 最大并发线程数
            max_retries: 单个 SKU 最大重试次数
        """
        self.mongo_collection = mongo_collection
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.change_detector = ChangeDetector(mongo_collection)
        self.notifier = get_notifier()

    def scan_skus(self, skus: List[str]) -> ScanResult:
        """
        并行扫描多个 SKU

        Args:
            skus: SKU 列表（10000-20000 个）

        Returns:
            扫描结果汇总
        """
        result = ScanResult()
        result.total_skus = len(skus)

        logger.info(f"开始扫描 {len(skus)} 个 SKU，使用 {self.max_workers} 个线程")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 提交所有任务
            futures = {
                executor.submit(self._scan_single_sku, sku): sku for sku in skus
            }

            # 收集结果
            for i, future in enumerate(futures):
                sku = futures[future]
                try:
                    snapshot = future.result()
                    if snapshot:
                        result.successful_scans += 1
                        result.snapshots.append(snapshot)

                        # 检测变化并触发通知
                        self._handle_changes(snapshot, result)
                    else:
                        # _scan_single_sku 内部已耗尽重试并记录了错误日志
                        result.failed_skus.append(sku)

                except Exception as e:
                    logger.error(f"扫描 {sku} 失败: {e}")
                    result.failed_skus.append(sku)

                # 定期输出进度
                if (i + 1) % 1000 == 0:
                    logger.info(f"进度: {i + 1}/{len(skus)}")

        result.end_time = datetime.utcnow()
        logger.info(
            f"扫描完成: "
            f"成功 {result.successful_scans}/{result.total_skus} "
            f"({result.success_rate():.1f}%) "
            f"耗时 {result.duration_seconds():.1f}s"
        )

        return result

    def _scan_single_sku(self, sku: str) -> Optional[ProductSnapshot]:
        """扫描单个 SKU（带重试）"""
        for attempt in range(self.max_retries):
            try:
                raw_result = check_sku(sku)

                # 转换为 ProductSnapshot
                snapshot = self._convert_to_snapshot(raw_result)
                return snapshot

            except Exception as e:
                if attempt < self.max_retries - 1:
                    logger.debug(f"[{sku}] 尝试 {attempt + 1}/{self.max_retries} 失败，重试中...")
                    import time

                    time.sleep(0.5)  # 重试前等待
                else:
                    logger.error(f"[{sku}] 所有重试都失败: {e}")
                    return None

        return None

    def _convert_to_snapshot(self, raw_result: dict) -> ProductSnapshot:
        """将原始扫描结果转换为 ProductSnapshot"""
        in_stock_sizes = [
            SizeInfo(
                sku=s["sku"],
                size=s["size"],
                status=s["status"],
                qty=s.get("qty", 0),
            )
            for s in raw_result.get("in_stock_sizes", [])
        ]

        all_sizes = [
            SizeInfo(
                sku=s["sku"],
                size=s["size"],
                status=s["status"],
                qty=s.get("qty", 0),
            )
            for s in raw_result.get("sizes", [])
        ]

        return ProductSnapshot(
            sku=raw_result["sku"],
            name=raw_result.get("name", ""),
            color=raw_result.get("color", ""),
            currency=raw_result.get("currency", "USD"),
            original_price=raw_result.get("original_price"),
            sale_price=raw_result.get("sale_price"),
            overall_status=raw_result.get("overall_status", "UNKNOWN"),
            sizes=all_sizes,
            in_stock_sizes=in_stock_sizes,
            checked_at=datetime.utcnow(),
        )

    def _handle_changes(self, snapshot: ProductSnapshot, result: ScanResult):
        """处理库存变化并发送通知"""
        # 检测变化
        changes = self.change_detector.detect_changes(snapshot)

        # 只处理有货的变化
        if not changes["new_in_stock"]:
            return

        # 查找用户关注的变化
        watched_changes = self.change_detector.find_watched_changes(
            snapshot.sku, changes
        )

        if not watched_changes:
            return

        logger.info(f"[{snapshot.sku}] 发现 {len(watched_changes)} 个用户关注的变化")

        # 去重检查 - 避免短时间内重复通知
        for change in watched_changes:
            if self._should_notify(snapshot.sku, change["size"]):
                # 发送通知
                self._send_notification(snapshot, change)
                result.notifications_sent += 1
                result.new_in_stock_items.append(change)

    def _should_notify(self, sku: str, size: str) -> bool:
        """检查是否应该发送通知（去重）"""
        recent_notif = self.change_detector.watch_store.get_recent_notification(
            sku, size, hours=2  # 2小时内不重复通知
        )
        return recent_notif is None

    def _send_notification(self, snapshot: ProductSnapshot, change: Dict[str, Any]):
        """发送单个商品通知"""
        try:
            # 记录通知
            self.change_detector.watch_store.record_notification(
                sku=snapshot.sku,
                size=change["size"],
                watch_item_id=change["watch_item_id"],
                status="IN_STOCK",
                message=f"{snapshot.name} [{snapshot.sku}] {change['size']} 有货",
            )

            # 发送 Slack
            if self.notifier.enabled:
                self.notifier.send_notification(
                    sku=snapshot.sku,
                    size=change["size"],
                    name=snapshot.name,
                    color=snapshot.color,
                    price=snapshot.sale_price,
                    currency=snapshot.currency,
                    status="有货",
                )

            logger.info(f"[{snapshot.sku}] {change['size']} 通知已发送")

        except Exception as e:
            logger.error(f"发送通知失败 [{snapshot.sku}] {change['size']}: {e}")

    def save_snapshots(self, snapshots: List[ProductSnapshot]):
        """保存扫描快照到数据库"""
        if not snapshots:
            return

        try:
            # 批量更新或插入
            for snapshot in snapshots:
                self.mongo_collection.update_one(
                    {"sku": snapshot.sku},
                    {
                        "$set": snapshot.to_dict(),
                        "$setOnInsert": {"created_at": datetime.utcnow()},
                    },
                    upsert=True,
                )

            logger.info(f"已保存 {len(snapshots)} 个产品快照到数据库")

        except Exception as e:
            logger.error(f"保存快照失败: {e}")
