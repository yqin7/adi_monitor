"""定时调度：让抓取无人值守地跑起来。

在此之前所有任务都得手点按钮 —— 凌晨上新没人知道，
严格说那还不是监控系统，只是个手动批量抓取工具。

设计要点：
  * 复用 job_runner，不另起一套执行路径。同一时刻仍只允许一个任务，
    调度器撞上手动任务时自己让路（跳过本轮，等下一轮）。
  * 分层频率：抓 Adidas 是自家目录接口、成本低，可以高频；
    查得物有日调用额度，低频且只补陈旧报价。
  * 随机抖动：固定整点发起请求本身就是机器特征，
    这和 full_scan 内部翻页抖动是同一个道理。

配置全部走环境变量，默认不启用 —— 免得有人本地跑起服务就开始自动打官网。
    SCHEDULER_ENABLED=1
    SCHEDULER_ADIDAS_CRON="17 * * * *"     # 每小时（默认整点后 17 分，避开整点高峰）
    SCHEDULER_DEWU_CRON="40 3 * * *"       # 每天凌晨 3:40
    SCHEDULER_SITES=us,kr,jp,gb,ca
    SCHEDULER_QUOTE_AGE_DAYS=7
    SCHEDULER_JITTER=300                   # 秒
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_scheduler = None

DEFAULT_ADIDAS_CRON = "17 * * * *"
DEFAULT_DEWU_CRON = "40 3 * * *"


def _env(key: str, default: str) -> str:
    return (os.getenv(key) or "").strip() or default


def enabled() -> bool:
    return _env("SCHEDULER_ENABLED", "0").lower() in ("1", "true", "yes", "on")


def _sites() -> list[str]:
    from app.api.jobs import ALL_SITES
    raw = [s.strip().lower() for s in _env("SCHEDULER_SITES", ",".join(ALL_SITES)).split(",")]
    return [s for s in raw if s in ALL_SITES] or ALL_SITES


def _run(name: str, **kwargs) -> None:
    """把一次定时触发交给 job_runner 执行。"""
    from app.api.jobs import JOBS, _make
    from app.services import job_runner

    if job_runner.is_busy():
        cur = job_runner.current()
        logger.info("调度[%s]：已有任务在跑（%s），本轮跳过",
                    name, cur.label if cur else "?")
        return

    sites = kwargs.get("sites") or _sites()
    ok, msg = job_runner.start(
        name, f"[定时] {JOBS[name]}",
        _make(name, sites, kwargs.get("limit"), kwargs.get("market", "CN"),
              kwargs.get("max_quote_age_days")))
    logger.info("调度[%s]：%s", name, "已启动" if ok else msg)


def start() -> None:
    """在 FastAPI 启动时调用。未启用或依赖缺失时安静跳过。"""
    global _scheduler
    if _scheduler is not None:
        return
    if not enabled():
        logger.info("定时调度未启用（设 SCHEDULER_ENABLED=1 开启）")
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("未安装 apscheduler，定时调度不可用：pip install apscheduler")
        return

    jitter = int(_env("SCHEDULER_JITTER", "300"))
    age = int(_env("SCHEDULER_QUOTE_AGE_DAYS", "7"))
    sched = BackgroundScheduler(timezone="Asia/Shanghai")

    adidas_cron = _env("SCHEDULER_ADIDAS_CRON", DEFAULT_ADIDAS_CRON)
    sched.add_job(_run, CronTrigger.from_crontab(adidas_cron, timezone="Asia/Shanghai"),
                  id="adidas", args=["adidas"], jitter=jitter,
                  max_instances=1, coalesce=True, misfire_grace_time=600)

    dewu_cron = _env("SCHEDULER_DEWU_CRON", DEFAULT_DEWU_CRON)
    sched.add_job(_run, CronTrigger.from_crontab(dewu_cron, timezone="Asia/Shanghai"),
                  id="dewu", args=["dewu"], kwargs={"max_quote_age_days": age},
                  jitter=jitter, max_instances=1, coalesce=True, misfire_grace_time=1800)

    sched.start()
    _scheduler = sched
    logger.info("定时调度已启动：Adidas「%s」站点 %s；得物「%s」只查 >%s 天；抖动 ±%ss",
                adidas_cron, ",".join(_sites()), dewu_cron, age, jitter)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("定时调度已停止")


def status() -> dict:
    """给接口看的当前调度状态。"""
    if _scheduler is None:
        return {"enabled": enabled(), "running": False, "jobs": []}
    return {
        "enabled": True, "running": True,
        "sites": _sites(),
        "jobs": [{"id": j.id,
                  "next_run": j.next_run_time.isoformat(timespec="seconds")
                  if j.next_run_time else None,
                  "trigger": str(j.trigger)}
                 for j in _scheduler.get_jobs()],
    }
