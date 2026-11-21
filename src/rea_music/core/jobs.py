"""持久化 Job 系统。

每个长任务一条 JSON 记录：状态、阶段、进度、工件、错误。
MCP 客户端断线不丢任务；幂等由内容哈希缓存保证（见 cache.py）。

并发约定（C1/R8 加固）：
- 所有对 job 记录的读-改-写都在 ``_LOCK`` 内完成，避免两个线程各自读到旧值
  再互相覆盖（实测会丢字段）。
- ``_write`` 先写同目录临时文件再 ``os.replace`` 原子改名：轮询方（MCP
  get_job_status）永远不会读到写了一半的 JSON。
- 读盘损坏的文件不再抛裸 JSONDecodeError，统一转成 ``job_corrupt``；
  ``list_jobs`` 跳过坏项而不是让整张表失败。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rea_music.core.config import load_config
from rea_music.core.errors import JobNotFound, REAError
from rea_music.core.paths import jobs_dir

_LOGGER = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ACTIVE: dict[str, dict] = {}
_POOL: ThreadPoolExecutor | None = None

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")

_HINT_CORRUPT = ("data/jobs/ 下的记录已损坏；重新发起任务即可，"
                 "旧记录可手动删除")


def _job_path(job_id: str) -> Path:
    return jobs_dir() / f"{job_id}.json"


def _corrupt_error(job_id: str, exc: Exception) -> REAError:
    return REAError(
        f"job 记录损坏，无法读取: {job_id}",
        code="job_corrupt",
        hint=_HINT_CORRUPT,
        details={"job_id": job_id, "error": f"{type(exc).__name__}: {exc}"},
    )


def _read_job_file(job_id: str) -> dict:
    """从磁盘读一条 job 记录；损坏时抛结构化 job_corrupt。

    Windows 上若正好撞上 os.replace 替换同一文件，open() 会短暂失败为
    PermissionError（默认共享模式不含 FILE_SHARE_DELETE）；这不是数据损坏，
    短暂退避重试即可，不能把它报成 job_corrupt。
    """
    path = _job_path(job_id)
    if not path.exists():
        raise JobNotFound(f"job {job_id} 不存在",
                          hint="job 记录位于 data/jobs/；确认 job_id 来自 analyze_track 的返回。")
    last_exc: Exception | None = None
    for attempt in range(6):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as exc:        # 正在被 replace：退避重试
            last_exc = exc
            time.sleep(0.005 * (attempt + 1))
        except (json.JSONDecodeError, OSError) as exc:
            raise _corrupt_error(job_id, exc) from None
    raise _corrupt_error(job_id, last_exc or OSError("unknown read error"))


def create_job(kind: str, params: dict) -> dict:
    job = {
        "job_id": uuid.uuid4().hex[:16],
        "kind": kind,
        "params": params,
        "status": "queued",
        "stage": None,
        "progress": 0.0,
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "retry_count": 0,
        "error": None,
        "result_ref": None,
    }
    _write(job)
    return job


def get_job(job_id: str) -> dict:
    with _LOCK:
        active = _ACTIVE.get(job_id)
        if active is not None:
            return dict(active)
    return _read_job_file(job_id)


def update_job(job_id: str, **fields) -> dict:
    """读-改-写整段加锁 + 原子落盘（并发安全）。"""
    with _LOCK:
        job = _ACTIVE.get(job_id)
        if job is None:
            job = _read_job_file(job_id)
        else:
            job = dict(job)
        job.update(fields)
        if job_id in _ACTIVE:
            _ACTIVE[job_id] = dict(job)
        _write(job)
    return dict(job)


def set_stage(job_id: str, stage: str, progress: float) -> None:
    update_job(job_id, stage=stage, progress=float(progress))


def submit(func, job: dict, *args, **kwargs) -> dict:
    """把 func 丢进进程内线程池执行。func 首参为 job_id。"""
    global _POOL
    cfg = load_config()
    if _POOL is None:
        _POOL = ThreadPoolExecutor(max_workers=cfg["jobs"]["max_workers"],
                                   thread_name_prefix="rea-job")
    job_id = job["job_id"]
    with _LOCK:
        _ACTIVE[job_id] = dict(job)

    def _run():
        update_job(job_id, status="running", started_at=time.time())
        try:
            result = func(job_id, *args, **kwargs)
            finished = {"status": "succeeded", "progress": 1.0,
                        "finished_at": time.time(), "result_ref": result}
        except Exception as exc:  # noqa: BLE001 —— 错误详情必须完整落盘
            finished = {"status": "failed", "finished_at": time.time(),
                        "error": _error_record(exc)}
        update_job(job_id, **finished)
        with _LOCK:
            _ACTIVE.pop(job_id, None)

    _POOL.submit(_run)
    return dict(job)


def run_inline(func, job: dict, *args, **kwargs) -> dict:
    """CLI 同步执行同一 func，与 MCP 后台路径共用实现。"""
    job_id = job["job_id"]
    update_job(job_id, status="running", started_at=time.time())
    try:
        result = func(job_id, *args, **kwargs)
        return update_job(job_id, status="succeeded", progress=1.0,
                          finished_at=time.time(), result_ref=result)
    except Exception as exc:  # noqa: BLE001
        return update_job(job_id, status="failed", finished_at=time.time(),
                           error=_error_record(exc))


def _error_record(exc: Exception) -> dict:
    """把异常落成可回放的结构化错误（code/hint/details 一起保留）。

    Agent 需要靠 details 区分"长度未知"与"m4a 需要 FFmpeg"这类不同原因；
    只留 message 会让下游把语义丢成一句话。traceback 只取最后 20 行，
    便于排障又不会把 job 记录撑大。
    """
    record = {"type": type(exc).__name__, "message": str(exc),
              "code": getattr(exc, "code", "internal_error")}
    hint = getattr(exc, "hint", None)
    details = getattr(exc, "details", None)
    if hint:
        record["hint"] = hint
    if details:
        record["details"] = details
    trace = getattr(exc, "__traceback__", None)
    if trace is not None:
        import traceback
        lines = traceback.format_exception(type(exc), exc, trace)
        record["traceback_tail"] = "".join(lines)[-2000:]
    return record


def _write(job: dict) -> None:
    """原子写：同目录临时文件 + os.replace（Windows 同卷亦为原子改名）。"""
    path = _job_path(job["job_id"])
    payload = json.dumps(job, ensure_ascii=False, indent=1)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        _replace_with_retry(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                _LOGGER.debug("临时 job 文件清理失败: %s", tmp, exc_info=True)


def _replace_with_retry(src: Path, dest: Path, attempts: int = 12) -> None:
    """os.replace 的短退避重试。

    Windows 上 MoveFileEx(REPLACE_EXISTING) 在目标文件正被其他线程读取时
    （默认共享模式不含 FILE_SHARE_DELETE）会短暂失败为 PermissionError；
    POSIX 一次即成。重试窗口累计约 1.4s（12 次，步长 0.02s 递增到上限 0.2s），
    覆盖外部读者读取整段时间；仍失败则抛出，由调用方如实记录。
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dest)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(0.02 * (attempt + 1), 0.2))


def list_jobs(limit: int = 50) -> list[dict]:
    """按 mtime 倒序列出 job；单条损坏只跳过并记 debug，不影响整张表。"""
    items = sorted(jobs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    out: list[dict] = []
    for path in items:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            _LOGGER.debug("跳过损坏的 job 记录 %s: %s", path.name, exc)
        if len(out) >= limit:
            break
    return out
