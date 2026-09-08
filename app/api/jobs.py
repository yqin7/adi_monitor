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

        def notify_restock(site: str, res: dict | None):
            """抓取结果里带 restock 差异，与 watch_list 求交集后推送。"""
            restock = (res or {}).get("restock") or {}
            if not restock:
                return
            from app.dao.mongo_client import MongoConnection
            from app.services.restock_service import RestockService
            from app.services.slack_service import SlackNotifier
            conn = MongoConnection.from_environment(required=True)
            try:
                notifier = None
                try:
                    notifier = SlackNotifier()      # 从环境变量读 webhook
                except Exception:
                    pass
                r = RestockService(conn.db, notifier).check_and_notify(site, restock)
                log(f"  补货 {len(restock)} 个 SKU，命中关注 {r['matched']}，"
                    f"已通知 {r['notified']}")
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
                    log("=== 🇺🇸 美国站：补全尺码 ===")
                    out["us_sizes"] = us_sizes_service.run(progress=log)
                    notify_restock("us", out["us_sizes"])
                else:
                    log(f"=== {site.upper()} 站抓取 ===")
                    out[site] = adidas_intl_service.run_site_scan(site, progress=log)
                    notify_restock(site, out[site])

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


@router.post("/cancel", summary="停止当前任务")
def cancel():
    return {"cancelled": job_runner.request_cancel()}
