"""后台任务状态追踪（进程内内存存储，重启后清空）"""
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

JOB_TYPE_WATCH_SCAN = "watch_scan"                  # 监控列表扫描
JOB_TYPE_FULL_SCAN = "full_scan"                    # 全量扫描（仅价格）
JOB_TYPE_FULL_SCAN_WITH_SIZES = "full_scan_with_sizes"  # 全量扫描 + 尺码库存

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


class JobService:
    """跟踪后台扫描任务的状态"""

    def __init__(self):
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_job(self, job_type: str) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "type": job_type,
                "status": STATUS_QUEUED,
                "stage": None,
                "message": None,
                "created_at": datetime.utcnow().isoformat(),
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
            }
        return job_id

    def update_job(self, job_id: str, **kwargs) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kwargs)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def list_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j["created_at"], reverse=True)
        return jobs[:limit]
