"""扫描相关接口：监控列表扫描 + 全量扫描 + 任务状态查询"""
import logging
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException

from app import state
from app.models.schemas import JobQueuedResponse, JobResponse, ScanRequest, FullScanRequest
from app.services.job_service import (
    JOB_TYPE_WATCH_SCAN,
    JOB_TYPE_FULL_SCAN,
    JOB_TYPE_FULL_SCAN_WITH_SIZES,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    STATUS_FAILED,
)
from app.services.full_scan_service import run_full_scan

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Scan"])


@router.post(
    "/scan",
    summary="扫描监控列表（触发变化检测 + Slack 通知）",
    response_model=JobQueuedResponse,
)
async def trigger_scan(background_tasks: BackgroundTasks, req: ScanRequest = ScanRequest()):
    """
    扫描指定 SKU（或当前监控列表中的全部 SKU），检测库存变化并对匹配监控项发送 Slack 通知。

    这是轻量扫描，只扫描传入/监控列表中的 SKU，不会去发现全站新商品。
    如需发现全站所有 SKU，请使用 `/scan/full` 或 `/scan/full-with-sizes`。
    """
    if not state.scan_service:
        raise HTTPException(status_code=500, detail="扫描器未初始化")

    skus = req.skus
    if not skus:
        watch_items = state.watch_service.get_all_watch_items(enabled_only=True)
        skus = sorted({item.sku for item in watch_items})
        if not skus:
            raise HTTPException(
                status_code=400,
                detail="监控列表为空，请先通过 POST /watch 添加要监控的商品，或在请求体中提供 skus 列表",
            )

    job_id = state.job_service.create_job(JOB_TYPE_WATCH_SCAN)
    background_tasks.add_task(_run_watch_scan_job, job_id, skus)

    return JobQueuedResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        message=f"扫描任务已排队（{len(skus)} 个 SKU），可通过 GET /scan/jobs/{{job_id}} 查询进度",
    )


def _run_watch_scan_job(job_id: str, skus: List[str]):
    """后台执行监控列表扫描"""
    state.job_service.update_job(job_id, status=STATUS_RUNNING, started_at=datetime.utcnow().isoformat())
    try:
        logger.info(f"[{job_id}] 开始扫描 {len(skus)} 个 SKU")
        result = state.scan_service.scan_skus(skus)
        state.scan_service.save_snapshots(result.snapshots)

        summary = {
            "total_skus": result.total_skus,
            "successful_scans": result.successful_scans,
            "failed_count": len(result.failed_skus),
            "success_rate": result.success_rate(),
            "notifications_sent": result.notifications_sent,
            "duration_seconds": result.duration_seconds(),
            "new_in_stock_items": result.new_in_stock_items,
        }
        state.job_service.update_job(
            job_id,
            status=STATUS_SUCCESS,
            result=summary,
            finished_at=datetime.utcnow().isoformat(),
        )
        logger.info(f"[{job_id}] 扫描完成: {summary}")

    except Exception as e:
        logger.error(f"[{job_id}] 扫描失败: {e}")
        state.job_service.update_job(
            job_id, status=STATUS_FAILED, error=str(e), finished_at=datetime.utcnow().isoformat()
        )


@router.post(
    "/scan/full",
    summary="立即触发全量扫描（仅价格，不抓尺码库存，速度快）",
    response_model=JobQueuedResponse,
)
async def trigger_full_scan(background_tasks: BackgroundTasks, req: FullScanRequest = FullScanRequest()):
    """
    爬取全站分类页（PLP）发现所有 SKU 及价格，写入 MongoDB（含价格历史）。

    不会抓取每个 SKU 的尺码库存，速度较快（几分钟级别）。
    如需同时获取尺码库存，请使用 `/scan/full-with-sizes`（耗时更长，10-20 分钟级别）。
    """
    job_id = state.job_service.create_job(JOB_TYPE_FULL_SCAN)
    background_tasks.add_task(
        _run_full_scan_job, job_id, False, req.category, req.plp_workers
    )
    return JobQueuedResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        message=f"全量扫描（仅价格）任务已排队，可通过 GET /scan/jobs/{job_id} 查询进度",
    )


@router.post(
    "/scan/full-with-sizes",
    summary="立即触发全量扫描（含尺码库存，耗时较长）",
    response_model=JobQueuedResponse,
)
async def trigger_full_scan_with_sizes(
    background_tasks: BackgroundTasks, req: FullScanRequest = FullScanRequest()
):
    """
    爬取全站分类页（PLP）发现所有 SKU 及价格，并为每个 SKU 补充尺码库存，全部写入 MongoDB。

    这是最完整的扫描（对应定时任务每 30 分钟执行的内容），
    10,000-20,000 个 SKU 预计耗时 10-20 分钟。
    """
    job_id = state.job_service.create_job(JOB_TYPE_FULL_SCAN_WITH_SIZES)
    background_tasks.add_task(
        _run_full_scan_job, job_id, True, req.category, req.plp_workers
    )
    return JobQueuedResponse(
        job_id=job_id,
        status=STATUS_QUEUED,
        message=f"全量扫描（含尺码）任务已排队，可通过 GET /scan/jobs/{job_id} 查询进度",
    )


def _run_full_scan_job(job_id: str, include_sizes: bool, category: Optional[str], plp_workers: int):
    """后台执行全量扫描（复用 full_scan_service.run_full_scan）"""
    state.job_service.update_job(job_id, status=STATUS_RUNNING, started_at=datetime.utcnow().isoformat())

    def progress(stage: str, message: str):
        state.job_service.update_job(job_id, stage=stage, message=message)

    try:
        logger.info(f"[{job_id}] 开始全量扫描 (include_sizes={include_sizes}, category={category})")
        summary = run_full_scan(
            category=category,
            plp_workers=plp_workers,
            include_sizes=include_sizes,
            progress_cb=progress,
        )
        state.job_service.update_job(
            job_id,
            status=STATUS_SUCCESS,
            result=summary,
            finished_at=datetime.utcnow().isoformat(),
        )
        logger.info(f"[{job_id}] 全量扫描完成: {summary}")

    except Exception as e:
        logger.error(f"[{job_id}] 全量扫描失败: {e}")
        state.job_service.update_job(
            job_id, status=STATUS_FAILED, error=str(e), finished_at=datetime.utcnow().isoformat()
        )


@router.get(
    "/scan/jobs/{job_id}",
    summary="查询扫描任务状态",
    response_model=JobResponse,
)
async def get_scan_job(job_id: str):
    """查询某次扫描任务（/scan、/scan/full、/scan/full-with-sizes 触发的任务）的执行状态和结果"""
    job = state.job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@router.get(
    "/scan/jobs",
    summary="列出最近的扫描任务",
    response_model=List[JobResponse],
)
async def list_scan_jobs(limit: int = 20):
    """列出最近触发的扫描任务，按创建时间倒序排列"""
    return state.job_service.list_jobs(limit=limit)
