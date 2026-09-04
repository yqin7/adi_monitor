"""Slack 通知服务"""
import os
import logging
from typing import Optional, Dict, List
from datetime import datetime
from urllib.request import urlopen, Request
import json

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Slack 通知管理"""

    def __init__(self, webhook_url: Optional[str] = None, user_id: Optional[str] = None):
        """
        初始化 Slack 通知

        Args:
            webhook_url: Slack Webhook URL (从环境变量 SLACK_WEBHOOK_URL 读取)
            user_id: Slack 用户 ID，用于 @mention (可选)
        """
        self.webhook_url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "").strip()
        self.user_id = user_id or os.getenv("SLACK_USER_ID", "").strip()
        self.enabled = bool(self.webhook_url)

    def send_notification(
        self,
        sku: str,
        size: str,
        name: str,
        color: str,
        price: Optional[float],
        currency: str = "USD",
        status: str = "有货",
    ) -> bool:
        """
        发送单个商品通知到 Slack

        Args:
            sku: 商品 SKU
            size: 尺码
            name: 商品名称
            color: 配色
            price: 价格
            currency: 货币
            status: 状态文本（有货、补货等）

        Returns:
            是否发送成功
        """
        if not self.enabled:
            logger.warning("Slack notifier not enabled")
            return False

        try:
            message = self._build_message(
                sku=sku,
                size=size,
                name=name,
                color=color,
                price=price,
                currency=currency,
                status=status,
            )
            return self._send_message(message)
        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")
            return False

    def send_batch_notification(self, items: List[Dict]) -> bool:
        """
        批量发送通知（多个商品一条消息）

        Args:
            items: 列表，每个元素是 {
                'sku': str,
                'size': str,
                'name': str,
                'color': str,
                'price': float,
                'currency': str,
                'status': str
            }

        Returns:
            是否发送成功
        """
        if not self.enabled or not items:
            return False

        try:
            message = self._build_batch_message(items)
            return self._send_message(message)
        except Exception as e:
            logger.error(f"Failed to send batch Slack notification: {e}")
            return False

    def _build_message(
        self,
        sku: str,
        size: str,
        name: str,
        color: str,
        price: Optional[float],
        currency: str,
        status: str,
    ) -> Dict:
        """构建单个通知消息"""
        price_str = f"{currency} {price}" if price else "N/A"
        mention = f"<@{self.user_id}> " if self.user_id else ""

        return {
            "text": f"{mention}🎉 {name} [{sku}] 有货了！",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🎉 {status}通知",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {
                            "type": "mrkdwn",
                            "text": f"*商品:*\n{name}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*配色:*\n{color}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*尺码:*\n{size}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*价格:*\n{price_str}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*SKU:*\n{sku}",
                        },
                        {
                            "type": "mrkdwn",
                            "text": f"*时间:*\n{datetime.now().strftime('%H:%M:%S')}",
                        },
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"🔗 <https://www.adidas.com/us/search?q={sku}|在 Adidas 上查看>",
                    },
                },
            ],
        }

    def _build_batch_message(self, items: List[Dict]) -> Dict:
        """构建批量通知消息"""
        mention = f"<@{self.user_id}> " if self.user_id else ""
        text_lines = [f"{mention}🎉 发现 {len(items)} 件有货商品！\n"]

        fields = []
        for item in items[:10]:  # 最多显示10个
            text = (
                f"*{item.get('name', 'N/A')}* [{item['sku']}]\n"
                f"尺码: {item['size']} | "
                f"配色: {item.get('color', 'N/A')} | "
                f"{item.get('currency', 'USD')} {item.get('price', 'N/A')}"
            )
            fields.append({"type": "mrkdwn", "text": text})

        return {
            "text": f"{mention}🎉 发现 {len(items)} 件有货商品",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"🎉 发现 {len(items)} 件有货",
                        "emoji": True,
                    },
                },
                {"type": "section", "fields": fields},
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        }
                    ],
                },
            ],
        }

    def _send_message(self, message: Dict) -> bool:
        """通过 Webhook 发送消息到 Slack"""
        try:
            data = json.dumps(message).encode("utf-8")
            request = Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request, timeout=10) as response:
                result = response.read().decode("utf-8")
                if result == "ok":
                    logger.info("Slack message sent successfully")
                    return True
                else:
                    logger.warning(f"Slack response: {result}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Slack message: {e}")
            return False


# 初始化全局通知器
_notifier: Optional[SlackNotifier] = None


def init_notifier(webhook_url: Optional[str] = None, user_id: Optional[str] = None):
    """初始化全局通知器"""
    global _notifier
    _notifier = SlackNotifier(webhook_url, user_id)
    return _notifier


def get_notifier() -> SlackNotifier:
    """获取全局通知器"""
    global _notifier
    if _notifier is None:
        _notifier = SlackNotifier()
    return _notifier
