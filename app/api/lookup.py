"""按货号查询：一个货号在各国官网的售价、尺码库存，以及得物报价。

和 /arbitrage/raw 的区别：
    raw 是「把全量比价结果倒出来给前端算」，按站点切片；
    这里是「我手上有个货号，想知道它各国什么价、哪儿有货」，按货号横切。

得物报价默认读库（dewu_quotes），带 live=true 才实时调接口 ——
实时查会占用得物的日调用额度，不该是默认行为。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.dao.mongo_client import MongoConnection

log = logging.getLogger(__name__)

router = APIRouter(prefix="/lookup", tags=["货号查询"])

SITE_LABEL = {"us": "美国", "kr": "韩国", "jp": "日本", "gb": "英国", "ca": "加拿大"}
SITE_ORDER = ["us", "kr", "jp", "gb", "ca"]


def _iso(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt if dt.endswith("Z") else dt + "Z"
    if getattr(dt, "tzinfo", None) is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _one(db, sku: str, live: bool) -> dict[str, Any]:
    from app.core.sizing import in_stock

    rows = list(db["products"].find({"sku": sku}, {"_id": 0}))
    sites: list[dict] = []
    for d in sorted(rows, key=lambda x: SITE_ORDER.index(x.get("site", "us"))
                    if x.get("site") in SITE_ORDER else 99):
        site = d.get("site", "us")
        sites.append({
            "site": site, "site_cn": SITE_LABEL.get(site, site),
            "name": d.get("name"), "category": d.get("category"),
            "url": d.get("url"), "currency": d.get("currency"),
            "orig_price": d.get("orig_price"), "sale_price": d.get("sale_price"),
            "discount_pct": d.get("discount_pct"),
            "promo_code": d.get("promo_code"), "promo_rate": d.get("promo_rate"),
            "is_sold_out": d.get("is_sold_out", False),
            "available_sizes": d.get("available_sizes") or [],
            "size_count": len(d.get("available_sizes") or []),
            # 时间：判断这条数据还能不能信
            "scraped_at": _iso(d.get("updated_at") or d.get("scraped_at")),
            "sizes_at": _iso(d.get("sizes_updated_at")),
            "first_seen": _iso(d.get("created_at")),
        })

    # ── 得物 ──────────────────────────────────────────────────────────
    dewu: dict[str, Any] = {"found": False, "source": None, "sizes": []}
    if live:
        try:
            import dewu_client as dc
            dc.load_env()
            r = dc.query_article_full(sku) or {}
            dewu = {"found": bool(r.get("sizes")), "source": "live",
                    "title": r.get("title"), "sizes": r.get("sizes") or []}
        except Exception as exc:                       # 实时查失败就退回读库
            log.warning("实时查得物失败 %s: %s", sku, exc)
            dewu["error"] = f"{type(exc).__name__}: {exc}"
    if not dewu["found"]:
        q = db["dewu_quotes"].find_one({"sku": sku}, {"_id": 0})
        if q:
            dewu = {"found": True, "source": "cache", "title": q.get("title"),
                    "fetched_at": _iso(q.get("fetched_at")),
                    "sizes": q.get("sizes") or []}
        elif not live:
            m = db["match_status"].find_one({"sku": sku}, {"_id": 0})
            if m and m.get("found") is False:
                dewu["note"] = f"得物查过但没有该货号（{m.get('tries', 0)} 次尝试）"

    # ── 逐尺码横向对照：这个尺码得物什么价、哪些国家有货 ──────────────
    matrix: list[dict] = []
    for s in dewu.get("sizes", []):
        size = s.get("size")
        avail = {}
        for x in sites:
            st = in_stock(size, x["available_sizes"]) if x["available_sizes"] else None
            avail[x["site"]] = st
        matrix.append({
            "size": size,
            "dewu_price": s.get("globalMinPrice"),      # 香港报价，人民币结算
            "monthly_sales": s.get("globalSoldNum30"),
            "sales_mom": s.get("globalMonthToMonthRatio"),
            "global_sku_id": s.get("globalSkuId"),
            "in_stock": avail,                          # {site: True/False/None}
        })
    matrix.sort(key=lambda r: (-(r["monthly_sales"] or 0), str(r["size"])))

    return {"sku": sku, "sites": sites, "site_count": len(sites),
            "dewu": {k: v for k, v in dewu.items() if k != "sizes"},
            "sizes": matrix}


@router.get("/sizes/unparsed", summary="解析不了的尺码（按出现次数排序）")
def unparsed_sizes(site: str | None = Query(None, description="限定站点，留空看全部"),
                   limit: int = Query(40, ge=1, le=300)):
    """尺码格式的「体检报告」。

    和 /arbitrage/promos 里的 unparsed_badges 是同一个思路：
    Adidas 换尺码写法时先在这里冒头，据此扩 sizing 的规则，
    而不是等某天偶然发现库存列一直在骗人。

    安全兜底已在 in_stock 里：解析不了的尺码返回 None（未知），
    不会被误报成「断码」—— 所以这里的条目是待办，不是事故。
    """
    from collections import Counter

    from app.core.sizing import normalize

    db = MongoConnection.from_environment(required=True).db
    q = {"site": site} if site else {}
    bad: Counter = Counter()
    total = ok = 0
    for d in db["products"].find(q, {"site": 1, "available_sizes": 1}):
        st = d.get("site", "?")
        for raw in (d.get("available_sizes") or []):
            if not raw or str(raw).strip().upper() == "HIDDEN":
                continue
            total += 1
            if normalize(raw) is None:
                bad[(st, str(raw))] += 1
            else:
                ok += 1

    by_site: dict[str, dict] = {}
    for (st, _), n in bad.items():
        by_site.setdefault(st, {"failed": 0})["failed"] += n
    for st in by_site:
        t = db["products"].count_documents({"site": st})
        by_site[st]["products"] = t

    return {
        "scanned_sizes": total, "parsed": ok, "failed": total - ok,
        "failure_rate": round((total - ok) / total, 4) if total else 0,
        "by_site": by_site,
        "top": [{"site": st, "size": sz, "count": n}
                for (st, sz), n in bad.most_common(limit)],
    }


@router.get("/sizes/registry", summary="尺码写法登记表（含首次出现时间）")
def size_registry(status: str | None = Query(None, description="new | known | resolved"),
                  source: str | None = Query(None, description="site | dewu"),
                  limit: int = Query(60, ge=1, le=500)):
    """与 /sizes/unparsed 的分工：

        unparsed  现算快照，回答「当前有哪些解析不了」
        registry  持久登记，回答「哪些是【新冒出来】的、什么时候第一次见到」

    抓取时自动登记，新写法汇总成一条告警；补上规则后下一轮自动销账为
    resolved（记录保留，用来回答「这个写法我们什么时候开始支持的」）。
    """
    from app.dao.unparsed_size_dao import UnparsedSizeDAO

    db = MongoConnection.from_environment(required=True).db
    dao = UnparsedSizeDAO(db)
    q: dict = {}
    if status:
        q["status"] = status
    if source:
        q["source"] = source
    rows = list(dao.coll.find(q, {"_id": 0})
                .sort([("status", 1), ("count", -1)]).limit(limit))
    for r in rows:
        for k in ("first_seen", "last_seen", "alerted_at", "resolved_at"):
            if r.get(k) is not None:
                r[k] = _iso(r[k])
    return {"summary": dao.summary(), "count": len(rows), "items": rows}


@router.get("/{article}", summary="按货号查各国售价与尺码库存")
def lookup(article: str,
           live: bool = Query(False, description="实时调得物接口（消耗调用额度），默认读库")):
    """货号大小写不敏感，内部统一转大写 —— 得物接口对大小写敏感。"""
    sku = article.strip().upper()
    if not sku:
        raise HTTPException(400, "货号不能为空")

    db = MongoConnection.from_environment(required=True).db
    out = _one(db, sku, live)
    if not out["sites"] and not out["dewu"]["found"]:
        raise HTTPException(404, f"库里没有货号 {sku}，各国官网与得物都无记录")
    return out


@router.get("", summary="批量按货号查（逗号分隔）")
def lookup_many(articles: str = Query(..., description="逗号分隔，最多 20 个"),
                live: bool = Query(False)):
    skus = [a.strip().upper() for a in articles.split(",") if a.strip()][:20]
    if not skus:
        raise HTTPException(400, "至少给一个货号")
    db = MongoConnection.from_environment(required=True).db
    return {"count": len(skus), "items": [_one(db, s, live) for s in skus]}
