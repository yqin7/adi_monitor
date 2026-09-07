"""比价（套利机会）接口。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Query

from app.dao.arbitrage_dao import ArbitrageDAO
from app.dao.mongo_client import MongoConnection
from app.services import arbitrage_service

router = APIRouter(prefix="/arbitrage", tags=["比价"])


def _dao() -> ArbitrageDAO:
    return ArbitrageDAO(MongoConnection.from_environment(required=True).db)


@router.get("", summary="套利机会列表（可按利润/利润率/销量排序）")
def list_opportunities(
    sort_by: str = Query("profit", description="profit | roi | monthly_sales | dewu_price | cost_local"),
    order: str = Query("desc", description="desc | asc"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_profit: float | None = Query(None, description="最低净利"),
    min_roi: float | None = Query(None, description="最低 ROI，0.15 = 15%"),
    min_sales: int | None = Query(None, description="最低 30 天销量"),
    category: str | None = Query(None),
):
    return _dao().query_arbitrage(
        sort_by=sort_by, order=order, limit=limit, offset=offset,
        min_profit=min_profit, min_roi=min_roi, min_sales=min_sales, category=category,
    )


@router.get("/stats", summary="比价数据概览")
def stats():
    dao = _dao()
    total = dao.arb.count_documents({})
    matched = dao.match.count_documents({"found": True})
    missed = dao.match.count_documents({"found": False})
    quotes = dao.quotes.count_documents({})
    top = list(dao.arb.find({}, {"_id": 0, "sku": 1, "size": 1, "name": 1,
                                 "profit": 1, "roi": 1, "monthly_sales": 1})
               .sort([("profit", -1)]).limit(5))
    return {"opportunities": total, "quotes": quotes,
            "matched": matched, "missed": missed, "top5_by_profit": top}


@router.post("/refresh", summary="拉取得物报价（后台执行）")
def refresh(background: BackgroundTasks,
            limit: int | None = Query(None, description="本轮最多查询多少个货号"),
            only_discounted: bool = Query(True, description="只查 Adidas 打折商品"),
            include_missed: bool = Query(False, description="是否重试历史未命中货号")):
    background.add_task(arbitrage_service.refresh_quotes,
                        limit=limit, only_discounted=only_discounted,
                        include_missed=include_missed)
    return {"message": "已在后台开始拉取得物报价", "limit": limit,
            "only_discounted": only_discounted}


@router.post("/compute", summary="重新计算套利机会")
def compute(market: str = Query("CN", description="CN 国内卖 | HK 香港卖"),
            apply_filter: bool = Query(True, description="是否应用利润/ROI/流动性门槛")):
    return arbitrage_service.compute(market=market, apply_filter=apply_filter)


@router.get("/promos", summary="当前促销活动概览（自动发现，措辞变化也能追踪）")
def promos(limit: int = Query(30, ge=1, le=200)):
    """按商品 badge 聚合当前在跑的活动。

    unparsed_badges 列出『看着像促销但没解析出折扣』的原始文案 ——
    Adidas 换措辞时先在这里出现，据此调整解析规则。
    """
    db = MongoConnection.from_environment(required=True).db
    pipe_promo = [
        {"$match": {"promo_text": {"$ne": None}}},
        {"$group": {"_id": {"text": "$promo_text", "code": "$promo_code",
                            "rate": "$promo_rate", "uncertain": "$promo_uncertain"},
                    "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": limit},
    ]
    active = [{"text": r["_id"]["text"], "code": r["_id"]["code"],
               "rate": r["_id"]["rate"], "uncertain": r["_id"].get("uncertain"),
               "products": r["count"]}
              for r in db["products"].aggregate(pipe_promo)]

    pipe_raw = [
        {"$unwind": "$badges"},
        {"$group": {"_id": "$badges", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}, {"$limit": limit},
    ]
    all_badges = [{"text": r["_id"], "count": r["count"]}
                  for r in db["products"].aggregate(pipe_raw)]

    unparsed = [{"text": r["_id"], "count": r["count"]}
                for r in db["products"].aggregate([
                    {"$match": {"promo_unparsed": {"$ne": None}}},
                    {"$unwind": "$promo_unparsed"},
                    {"$group": {"_id": "$promo_unparsed", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}}, {"$limit": limit}])]

    total = db["products"].count_documents({})
    with_promo = db["products"].count_documents({"promo_rate": {"$ne": None}})
    return {"total_products": total, "with_promo": with_promo,
            "coverage": round(with_promo / total, 4) if total else 0,
            "active_promos": active,
            "unparsed_badges": unparsed,
            "all_badges": all_badges}
