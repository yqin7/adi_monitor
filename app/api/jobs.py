"""任务触发与进度查询接口。

前端点按钮 -> POST /jobs/run/{name}，随后轮询 GET /jobs/status?offset=N 增量取日志。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services import job_runner

router = APIRouter(prefix="/jobs", tags=["任务"])

ALL_SITES = ["us", "kr", "jp", "gb", "ca"]

JOBS = {
    "all": "全流程：Adidas 抓取 → 得物报价 → 算利润",
    "adidas": "只抓 Adidas：各国官网 SKU + 尺码",
    "dewu": "只查得物报价（跳过已知没有的货号）",
    "dewu-retry": "重查历史未命中货号",
    "compute": "只重算利润（不发请求）",
}


def _make(name: str, sites: list[str], limit: int | None, market: str,
          max_quote_age_days: int | None = None):
    """把任务名映射成可在后台线程执行的函数。"""
    def work(job: job_runner.Job):
        from app.services import adidas_intl_service, us_sizes_service
        from app.services.arbitrage_service import compute, refresh_quotes
        from app.services.full_scan_service import run_full_scan

        log = job.log
        stop = job.cancel.is_set
        out: dict = {}

        def notify(site: str, res: dict | None):
            """一次抓取的两类信号：补货（对关注列表）和新品（全量）。

            都不额外发请求 —— 差异是抓取过程中顺手算出来的。
            """
            res = res or {}
            if res.get("healthy") is False:
                # 抓取本身失败了，别把它当成「这一站今天没新品」
                log(f"  【数据不可信】{site.upper()} 本轮，已跳过写库与通知")
                return
            restock = res.get("restock") or {}
            new_skus = res.get("new_skus") or []
            if not restock and not new_skus:
                return
            from app.dao.mongo_client import MongoConnection
            from app.services.new_product_service import NewProductService
            from app.services.restock_service import RestockService
            from app.services.slack_service import SlackNotifier
            conn = MongoConnection.from_environment(required=True)
            try:
                notifier = None
                try:
                    notifier = SlackNotifier()      # 从环境变量读 webhook
                except Exception:
                    pass
                if restock:
                    r = RestockService(conn.db, notifier).check_and_notify(site, restock)
                    log(f"  补货 {len(restock)} 个 SKU，命中关注 {r['matched']}，"
                        f"已通知 {r['notified']}")
                if new_skus:
                    n = NewProductService(conn.db, notifier).record_and_notify(
                        site, new_skus, batch_id=res.get("batch_id"))
                    log(f"  新品 {n['found']} 个，跨站/时间窗去重后上报 {n['reported']}，"
                        f"已通知 {n['notified']}")
            finally:
                conn.close()

        def scan_adidas():
            for site in sites:
                if stop():
                    log("已请求停止，跳过剩余站点")
                    break
                if site == "us":
                    log("=== 🇺🇸 美国站：抓取 SKU ===")
                    out["us"] = run_full_scan(include_sizes=False, progress_cb=log)
                    notify("us", out["us"])              # 新品来自价格扫描
                    log("=== 🇺🇸 美国站：补全尺码 ===")
                    out["us_sizes"] = us_sizes_service.run(progress=log)
                    notify("us", out["us_sizes"])        # 补货来自尺码扫描
                else:
                    log(f"=== {site.upper()} 站抓取 ===")
                    out[site] = adidas_intl_service.run_site_scan(site, progress=log)
                    notify(site, out[site])              # 国际站一次抓取兼得两类信号

        if name == "adidas":
            scan_adidas()
        elif name == "dewu":
            out["dewu"] = refresh_quotes(limit=limit, progress=log, should_stop=stop,
                                         max_quote_age_days=max_quote_age_days)
            out["compute"] = compute(market=market, progress=log)
        elif name == "dewu-retry":
            log("重查历史未命中货号（按 30 天冷却期）")
            out["dewu"] = refresh_quotes(limit=limit, include_missed=True,
                                         progress=log, should_stop=stop)
            out["compute"] = compute(market=market, progress=log)
        elif name == "compute":
            out["compute"] = compute(market=market, progress=log)
        elif name == "all":
            scan_adidas()
            if not stop():
                log("=== 得物报价（跨站去重、跳过已知未命中）===")
                out["dewu"] = refresh_quotes(limit=limit, progress=log, should_stop=stop,
                                             max_quote_age_days=max_quote_age_days)
                out["compute"] = compute(market=market, progress=log)
        return out

    return work


@router.get("", summary="可运行的任务清单")
def list_jobs():
    return {"jobs": [{"name": k, "label": v} for k, v in JOBS.items()],
            "busy": job_runner.is_busy()}


@router.post("/run/{name}", summary="启动任务")
def run(name: str,
        sites: str = Query(",".join(ALL_SITES), description="逗号分隔，仅 adidas/all 生效"),
        limit: int | None = Query(None, description="本轮最多处理多少货号"),
        market: str = Query("CN"),
        max_quote_age_days: int | None = Query(
            None, description="只重查报价超过 N 天的货号，省得物调用额度；仅 dewu/all 生效")):
    if name not in JOBS:
        raise HTTPException(404, f"未知任务 {name}")
    picked = [s.strip() for s in sites.split(",") if s.strip() in ALL_SITES] or ALL_SITES
    ok, msg = job_runner.start(name, JOBS[name],
                               _make(name, picked, limit, market, max_quote_age_days))
    if not ok:
        raise HTTPException(409, msg)
    return {"started": True, "name": name, "label": JOBS[name], "sites": picked}


@router.get("/status", summary="当前任务状态与增量日志")
def status(offset: int = Query(0, ge=0, description="已收到的日志行号")):
    job = job_runner.current()
    if not job:
        return {"idle": True, "lines": [], "next_offset": 0}
    return {"idle": False, **job.snapshot(offset)}


@router.get("/new-products", summary="最近检测到的新品")
def new_products(days: int = Query(7, ge=1, le=90),
                 site: str | None = Query(None),
                 limit: int = Query(200, ge=1, le=2000)):
    """读 notifications 里 change_type=new_product 的记录。

    通知渠道没配也不影响 —— 检测结果始终落库，这里直接查出来看。
    """
    from datetime import datetime, timedelta

    from app.dao.mongo_client import MongoConnection

    db = MongoConnection.from_environment(required=True).db
    q: dict = {"change_type": "new_product",
               "created_at": {"$gte": datetime.utcnow() - timedelta(days=days)}}
    if site:
        q["site"] = site
    rows = list(db["notifications"].find(q, {"_id": 0})
                .sort([("created_at", -1)]).limit(limit))
    for r in rows:
        if hasattr(r.get("created_at"), "strftime"):
            r["created_at"] = r["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ")
    by_site: dict[str, int] = {}
    for r in rows:
        by_site[r.get("site", "?")] = by_site.get(r.get("site", "?"), 0) + 1
    return {"days": days, "count": len(rows), "by_site": by_site, "items": rows}


@router.get("/schedule", summary="定时调度状态")
def schedule():
    from app.services import scheduler
    return scheduler.status()


@router.post("/cancel", summary="停止当前任务")
def cancel():
    return {"cancelled": job_runner.request_cancel()}
