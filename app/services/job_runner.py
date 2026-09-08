"""后台任务运行器：供前端一键触发、查看进度与日志。

同一时刻只允许一个任务在跑（都是重 IO 且会撞外部限流，并行没有意义）。
日志保留在内存环形缓冲里，前端按 offset 增量拉取。
"""
from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Any, Callable

MAX_LOG = 4000          # 环形缓冲保留的行数


class Job:
    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label
        self.status = "running"          # running | done | failed | cancelled
        self.started_at = datetime.now()
        self.ended_at: datetime | None = None
        self.result: Any = None
        self.error: str | None = None
        self.lines: deque[str] = deque(maxlen=MAX_LOG)
        self.dropped = 0                 # 被环形缓冲挤掉的行数
        self.cancel = threading.Event()

    def log(self, msg: str) -> None:
        if len(self.lines) == MAX_LOG:
            self.dropped += 1
        self.lines.append(f"{datetime.now():%H:%M:%S}  {msg}")

    def snapshot(self, offset: int = 0) -> dict:
        """offset 是前端已收到的行号（含被挤掉的），返回其后的新行。"""
        base = self.dropped
        start = max(0, offset - base)
        return {
            "name": self.name, "label": self.label, "status": self.status,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "ended_at": self.ended_at.isoformat(timespec="seconds") if self.ended_at else None,
            "elapsed": int((self.ended_at or datetime.now()) .timestamp()
                           - self.started_at.timestamp()),
            "result": self.result, "error": self.error,
            "lines": list(self.lines)[start:],
            "next_offset": base + len(self.lines),
        }


_current: Job | None = None
_lock = threading.Lock()


def current() -> Job | None:
    return _current


def is_busy() -> bool:
    return _current is not None and _current.status == "running"


def start(name: str, label: str, fn: Callable[[Job], Any]) -> tuple[bool, str]:
    """启动任务。已有任务在跑时拒绝，返回 (是否启动, 说明)。"""
    global _current
    with _lock:
        if is_busy():
            return False, f"已有任务在运行：{_current.label}"
        job = Job(name, label)
        _current = job

    def runner() -> None:
        job.log(f"任务开始：{label}")
        try:
            job.result = fn(job)
            job.status = "cancelled" if job.cancel.is_set() else "done"
            job.log(f"任务{'已取消' if job.cancel.is_set() else '完成'}")
        except Exception as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.log(f"任务失败：{job.error}")
            job.log(traceback.format_exc()[-1500:])
        finally:
            job.ended_at = datetime.now()

    threading.Thread(target=runner, daemon=True, name=f"job-{name}").start()
    return True, "已启动"


def request_cancel() -> bool:
    """请求停止当前任务。任务在下一个检查点自行退出。"""
    if is_busy() and _current:
        _current.cancel.set()
        _current.log("收到停止请求，将在当前批次结束后退出…")
        return True
    return False
