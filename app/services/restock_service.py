"""补货 / 断码检测与通知。

旧的 scan_service 依赖 /api/products/{sku}/availability 逐 SKU 查询，
该接口已被 Akamai 403。这里改用尺码快照做差异检测：

    抓取写入 products.available_sizes 时，SizeHistoryDAO 同步记录快照
        ↓
    check_and_notify() 读出本轮 restock/out_of_stock 差异
        ↓
    与 watch_list 求交集（只推用户关注的 sku + 尺码）
        ↓
    去重（同一 sku+尺码 N 小时内不重复推）→ Slack

好处：不额外发任何请求，补货检测搭在既有抓取上，零风控成本。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.core.sizing import sizes_match

logger = logging.getLogger(__name__)

DEDUP_HOURS = 6          # 同一 sku+尺码在此时间内不重复通知


class RestockService:
    def __init__(self, db, notifier=None) -> None:
        self.db = db
        self.watch = db["watch_list"]
        self.notif = db["notifications"]
        self.prod = db["products"]
        self.notifier = notifier

    # ── 去重 ──────────────────────────────────────────────────────────────
    def _recently_notified(self, sku: str, size: str, site: str) -> bool:
        cutoff = datetime.utcnow() - timedelta(hours=DEDUP_HOURS)
        return self.notif.count_documents({
            "sku": sku, "size": size, "site": site,
            "change_type": "new_in_stock",
            "created_at": {"$gte": cutoff},
        }, limit=1) > 0

    # ── 主流程 ────────────────────────────────────────────────────────────
    def check_and_notify(self, site: str, restock: dict[str, list[str]]) -> dict[str, Any]:
        """restock: {sku: [新有货的尺码]}，来自 SizeHistoryDAO.record_changes。"""
        if not restock:
            return {"matched": 0, "notified": 0, "items": []}

        watches = list(self.watch.find({"sku": {"$in": list(restock)}, "enabled": True}))
        if not watches:
            return {"matched": 0, "notified": 0, "items": []}

        hits: list[dict] = []
        for w in watches:
            sku = w["sku"]
            wanted = w.get("size")
            for size in restock.get(sku, []):
                # 用户填的尺码口径可能与站点不同（US码 vs EU码），用归一化比对
                if wanted and not sizes_match(wanted, size):
                    continue
                if self._recently_notified(sku, size, site):
                    continue
                p = self.prod.find_one({"sku": sku, "site": site},
                                       {"name": 1, "url": 1, "sale_price": 1,
                                        "orig_price": 1, "currency": 1})
                hits.append({
                    "sku": sku, "size": size, "site": site,
                    "watch_id": w.get("id"),
                    "name": (p or {}).get("name", ""),
                    "url": (p or {}).get("url", ""),
                    "price": (p or {}).get("sale_price") or (p or {}).get("orig_price"),
                    "currency": (p or {}).get("currency", ""),
                    "change_type": "new_in_stock",
                    "created_at": datetime.utcnow(),
                })

        sent = 0
        for h in hits:
            if self.notifier:
                try:
                    ok = self.notifier.send_notification(
                        sku=h["sku"], size=str(h["size"]),
                        name=h.get("name") or "", color="",
                        price=h.get("price"), currency=h.get("currency") or "USD",
                        status=f"补货（{h['site'].upper()}）")
                    sent += 1 if ok else 0
                except Exception as exc:
                    logger.warning("通知发送失败 %s: %s", h["sku"], exc)
            self.notif.insert_one(dict(h))

        logger.info("补货检测：命中关注 %s 条，发送 %s 条", len(hits), sent)
        return {"matched": len(hits), "notified": sent, "items": hits}

    @staticmethod
    def _format(h: dict) -> str:
        price = f"{h.get('currency','')} {h['price']}" if h.get("price") else "价格未知"
        return (f"🔔 补货提醒 [{h['site'].upper()}]\n"
                f"{h['sku']}  {h['name']}\n"
                f"尺码 {h['size']} 现已有货 · {price}\n{h.get('url','')}")
