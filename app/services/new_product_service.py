"""新品上架检测与通知。

数据来源不额外发请求 —— 全量扫描本来就把整个目录拉了一遍，
ProductDAO.upsert_products 顺手把「本批首次插入的 (sku, site)」带出来：

    UpdateOne(..., $setOnInsert={created_at}, upsert=True)
        ↓  bulk_write().upserted_ids 只含真正新插入的文档
    scan 返回 new_skus
        ↓
    record_and_notify() 跨站去重 + 时间窗去重
        ↓
    写 notifications，汇总推送

为什么新品检测比断码检测安全：
    某页抓失败 -> 那批 SKU 根本不进结果 -> 不会被当成新品；
    下一轮抓到了 -> 它们已在库里 -> 也不会补报。
    也就是可能漏报，但几乎不会误报。而通知这种东西，误报才是真的伤。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

logger = logging.getLogger(__name__)

DEDUP_HOURS = 72        # 同一货号在此时间内不重复推（下架又上架不算新品）
MAX_PUSH = 30           # 单次最多推多少条，超出只在汇总里给个数字


class NewProductService:
    def __init__(self, db, notifier=None) -> None:
        self.db = db
        self.prod = db["products"]
        self.notif = db["notifications"]
        self.notifier = notifier

    # ── 去重 ──────────────────────────────────────────────────────────────
    def _recently_notified(self, skus: Iterable[str]) -> set[str]:
        cutoff = datetime.utcnow() - timedelta(hours=DEDUP_HOURS)
        cur = self.notif.find(
            {"sku": {"$in": list(skus)}, "change_type": "new_product",
             "created_at": {"$gte": cutoff}},
            {"sku": 1})
        return {d["sku"] for d in cur}

    def _known_on_other_sites(self, skus: list[str], site: str) -> set[str]:
        """同一货号已经在别的站点出现过 -> 不算「全球新品」。

        一件商品常同时上五个站，不去重就会为同一个货号推五条。
        谁先扫到谁上报，后面几站因为库里已有别站记录而被压掉。
        """
        if not skus:
            return set()
        cur = self.prod.find({"sku": {"$in": skus}, "site": {"$ne": site}}, {"sku": 1})
        return {d["sku"] for d in cur}

    # ── 主流程 ────────────────────────────────────────────────────────────
    def record_and_notify(self, site: str, new_skus: list[dict],
                          batch_id: str | None = None) -> dict[str, Any]:
        """new_skus: [{'sku': ..., 'site': ...}]，来自 upsert_products 的返回。"""
        skus = [d["sku"] for d in (new_skus or []) if d.get("sku")]
        if not skus:
            return {"found": 0, "reported": 0, "notified": 0, "items": []}

        cross = self._known_on_other_sites(skus, site)
        recent = self._recently_notified(skus)
        fresh = [s for s in skus if s not in cross and s not in recent]
        if not fresh:
            logger.info("新品检测[%s]：%s 个新插入，跨站/时间窗去重后无需上报", site, len(skus))
            return {"found": len(skus), "reported": 0, "notified": 0, "items": []}

        now = datetime.utcnow()
        docs = []
        for p in self.prod.find(
                {"sku": {"$in": fresh}, "site": site},
                {"sku": 1, "name": 1, "url": 1, "category": 1,
                 "sale_price": 1, "orig_price": 1, "currency": 1}):
            docs.append({
                "sku": p["sku"], "site": site,
                "name": p.get("name", ""), "url": p.get("url", ""),
                "category": p.get("category"),
                "price": p.get("sale_price") or p.get("orig_price"),
                "currency": p.get("currency", "USD"),
                "change_type": "new_product",
                "batch_id": batch_id,
                "created_at": now,
            })

        if docs:
            self.notif.insert_many(docs)

        sent = 0
        if self.notifier and docs:
            try:
                items = [{"sku": d["sku"], "size": "-", "name": d["name"],
                          "color": "", "price": d["price"],
                          "currency": d["currency"],
                          "status": f"新品上架（{site.upper()}）"}
                         for d in docs[:MAX_PUSH]]
                sent = len(items) if self.notifier.send_batch_notification(items) else 0
            except Exception as exc:
                logger.warning("新品通知发送失败 %s: %s", site, exc)

        logger.info("新品检测[%s]：新插入 %s，跨站去重 -%s，时间窗去重 -%s，上报 %s，推送 %s",
                    site, len(skus), len(cross), len(recent), len(docs), sent)
        return {"found": len(skus), "reported": len(docs), "notified": sent,
                "items": docs}
