# -*- coding: utf-8 -*-
"""按需转码 HTTP 端点 —— Web 端视频播放的转码任务编排。

职责边界：
    转码本身（ffmpeg 调用、缓存读写、meta）全在
    ``app.services.transcode_service``；本模块只负责
    「校验 → 拿互斥门 → 登记任务 → 起后台线程 → 映射成稳定 JSON」。

为什么状态要留在模块级（而不是 request/app.state）：
    Web 切页/刷新会丢掉 task_id，前端只能靠轮询 ``status`` 判断进度；
    运行态必须进程内全局可见、且不随请求对象回收 —— 与 heic 路由同一套思路。
    任务记录复用 ``app.api.task_registry``（kind="transcode"），
    模块级 ``_running`` 只维护「file_id → 正在跑的那个任务」的映射与实时进度。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api import task_registry as registry
from app.services import transcode_service as ts
from app.services.dedup_gate import acquire_dedup_gate, release_dedup_gate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transcode", tags=["transcode"])

TASK_KIND = "transcode"
"""任务登记表里的 kind（前端/日志据此区分转码与查重、HEIC 转换）。"""


# ============================================================
# 请求体
# ============================================================

class StartRequest(BaseModel):
    """`POST /api/transcode/start` 请求体。"""

    file_id: int = Field(..., ge=1, description="媒体库内的文件 ID")
    force: bool = Field(False, description="为 True 时丢弃已有缓存重新转码")


class CancelRequest(BaseModel):
    """`POST /api/transcode/cancel` 请求体。"""

    file_id: int = Field(..., ge=1, description="媒体库内的文件 ID")


# ============================================================
# 运行态（file_id → 正在执行的任务）
# ============================================================

@dataclass(frozen=True)
class _RunningEntry:
    """一个正在执行的转码任务快照（不可变，更新时整体替换）。"""

    file_id: int
    task_id: str
    filename: str
    progress: int = 0
    message: str = "准备转码"
    started_at: float = 0.0

    def snapshot(self) -> dict:
        """`GET /api/transcode/active` 里的单条记录。"""
        return {
            "file_id": self.file_id,
            "task_id": self.task_id,
            "filename": self.filename,
            "status": ts.STATUS_RUNNING,
            "progress": self.progress,
            "message": self.message,
            "started_at": self.started_at,
        }


_running: dict[int, _RunningEntry] = {}
"""file_id → 运行中任务；进程内唯一真相（登记表另存一份给 /active 之外的用途）。"""

_running_lock = threading.Lock()
"""保护 `_running`，同时让「检查是否在跑 → 登记 → 起线程」成为原子操作。"""


def _set_running_progress(file_id: int, progress: Optional[int] = None,
                          message: Optional[str] = None) -> None:
    """更新运行中任务的进度/文案（线程安全，条目不存在时忽略）。"""
    with _running_lock:
        entry = _running.get(file_id)
        if entry is None:
            return
        fields = {}
        if progress is not None:
            fields["progress"] = max(0, min(100, int(progress)))
        if message is not None:
            fields["message"] = message
        if fields:
            _running[file_id] = replace(entry, **fields)


# ============================================================
# 内部工具
# ============================================================

def _stream_url(file_id: int) -> str:
    """转码缓存播放地址（与 files 的 stream 同语义，只是读的是 MP4 缓存）。"""
    return f"{router.prefix}/stream?file_id={file_id}"


def _message_first_line(text: str, limit: int = 200) -> str:
    """ffmpeg 的 stderr 尾部可能多行，错误提示只展示第一行有意义的。"""
    for line in (text or "").splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:limit]
    return ""


def _validate_source(file_id: int) -> ts.TranscodeSource:
    """把服务层的 TranscodeError 转成 HTTPException（400/404/503）。"""
    try:
        return ts.load_source(file_id)
    except ts.TranscodeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _registry_status(result: ts.TranscodeResult) -> str:
    """领域状态 → 登记表状态（登记表用 completed/cancelled 的旧拼写）。"""
    if result.status == ts.STATUS_DONE:
        return "completed"
    if result.status == ts.STATUS_CANCELED:
        return "cancelled"
    return "failed"


def _finish_task(task_id: str, result: ts.TranscodeResult) -> None:
    """把最终结果写进登记表（含失败原因与耗时）。"""
    status = _registry_status(result)
    fields = {
        "status": status,
        "progress": int(result.progress),
        "phase": "done" if status == "completed" else status,
        "finished_at": time.time(),
        "elapsed_ms": int(result.elapsed_ms),
        "result": {
            "file_id": result.file_id,
            "status": result.status,
            "cached": bool(result.cached),
            "output": str(result.output_path) if result.output_path else None,
        },
    }
    if status == "failed":
        fields["error"] = result.message
    registry.update(task_id, **fields)


def _run_transcode_task(file_id: int, config, task_id: str) -> None:
    """后台线程体：跑转码并在**所有路径**上释放互斥门、清掉运行态。

    这里不向外抛异常：线程里的异常没人接，只能落成登记表的 failed。
    """
    registry.update(task_id, status="running", started_at=time.time(),
                    phase="transcoding")
    try:
        def on_progress(percent: int, message: str) -> None:
            _set_running_progress(file_id, percent, message)
            registry.update(task_id, progress=int(percent), message=message)

        result = ts.transcode(
            file_id,
            config,
            on_progress=on_progress,
            cancel_check=lambda: registry.is_cancel_requested(task_id),
        )
        _finish_task(task_id, result)
    except Exception as exc:  # noqa: BLE001 — 线程体不能把异常放出去
        logger.exception(f"转码任务异常 file_id={file_id} task={task_id}")
        registry.update(task_id, status="failed", error=str(exc),
                        phase="failed", finished_at=time.time())
    finally:
        # 先清运行态再放门：清晚了会让 /status 在门已释放后仍报 running。
        with _running_lock:
            _running.pop(file_id, None)
        release_dedup_gate()


# ============================================================
# 端点
# ============================================================

@router.get("/status")
def transcode_status(file_id: int, request: Request) -> dict:
    """查询某个文件的转码状态（前端轮询入口）。

    返回字段稳定：``available / status / progress / message / stream_url``。
    磁盘事实（meta）与运行态（``_running``）合并：运行中优先看运行态，
    否则看缓存 meta；meta 里的 running 若是进程崩溃残影，则如实报「已中断」。
    """
    source = _validate_source(file_id)
    config = request.app.state.config
    available = ts.is_available()

    with _running_lock:
        running = _running.get(file_id)
    if running is not None:
        return {
            "available": available,
            "status": ts.STATUS_RUNNING,
            "progress": int(running.progress),
            "message": running.message or "转码中",
            "stream_url": None,
        }

    disk = ts.read_meta_status(source, config)
    if disk.status == ts.STATUS_DONE:
        return {
            "available": available,
            "status": ts.STATUS_DONE,
            "progress": 100,
            "message": disk.message or "转码完成",
            "stream_url": _stream_url(file_id),
        }
    if disk.status == ts.STATUS_RUNNING:
        # meta 说在跑但进程内没有对应任务：上次进程被强杀，别让前端永远转圈。
        return {
            "available": available,
            "status": ts.STATUS_FAILED,
            "progress": int(disk.progress),
            "message": "上次转码被中断，可重新发起",
            "stream_url": None,
        }
    if disk.status == ts.STATUS_FAILED:
        return {
            "available": available,
            "status": ts.STATUS_FAILED,
            "progress": 0,
            "message": _message_first_line(disk.message) or "转码失败",
            "stream_url": None,
        }
    if disk.status == ts.STATUS_CANCELED:
        return {
            "available": available,
            "status": ts.STATUS_CANCELED,
            "progress": int(disk.progress),
            "message": "已取消",
            "stream_url": None,
        }
    return {
        "available": available,
        "status": ts.STATUS_NONE,
        "progress": 0,
        "message": "",
        "stream_url": None,
    }


@router.post("/start")
def start_transcode(payload: StartRequest, request: Request) -> dict:
    """发起转码（幂等：在跑/已完成的直接回现有状态，不重复起任务）。

    并发控制顺序：先看是否在跑（回现有任务）→ 已有缓存直接 done →
    强制刷新才清缓存 → 拿 dedup_gate（拿不到 409，避免与扫描/查重同时写盘）。
    """
    config = request.app.state.config
    source = _validate_source(payload.file_id)

    if not ts.is_available():
        raise HTTPException(status_code=503, detail="服务端缺少 ffmpeg，无法转码")

    # 整个「检查运行态 → 登记 → 起线程」在同一把锁内完成：
    # 否则两个并发 /start 会各自拿到「没人跑」的结论，起出两个 ffmpeg。
    with _running_lock:
        existing = _running.get(payload.file_id)
        if existing is not None:
            return {
                "success": True,
                "task_id": existing.task_id,
                "status": ts.STATUS_RUNNING,
                "message": "该文件正在转码",
            }

        if not payload.force and ts.is_cache_hit(source, config):
            return {
                "success": True,
                "task_id": None,
                "status": ts.STATUS_DONE,
                "message": "已有转码缓存",
            }

        if payload.force:
            removed = ts.clear_cache(payload.file_id, config)
            if removed:
                logger.info(f"强制重转：清理旧缓存 {removed} 个 (file_id={payload.file_id})")

        if not acquire_dedup_gate():
            raise HTTPException(
                status_code=409,
                detail="当前有扫描/查重/合并等任务在执行，请稍后再试",
            )

        task_id = registry.next_task_id(TASK_KIND)
        registry.register(task_id, TASK_KIND, phase="queued", progress=0)
        entry = _RunningEntry(
            file_id=payload.file_id,
            task_id=task_id,
            filename=source.filename,
            progress=0,
            message="准备转码",
            started_at=time.time(),
        )
        _running[payload.file_id] = entry

        try:
            thread = threading.Thread(
                target=_run_transcode_task,
                args=(payload.file_id, config, task_id),
                daemon=True,
                name=f"transcode-{payload.file_id}",
            )
            thread.start()
        except Exception as exc:  # noqa: BLE001 — 起线程失败要回滚门与运行态
            _running.pop(payload.file_id, None)
            release_dedup_gate()
            registry.update(task_id, status="failed", error=f"启动转码线程失败: {exc}",
                            finished_at=time.time())
            raise HTTPException(status_code=500,
                                detail=f"启动转码线程失败: {exc}") from exc

    return {
        "success": True,
        "task_id": task_id,
        "status": ts.STATUS_RUNNING,
        "message": "转码已开始",
    }


@router.post("/cancel")
def cancel_transcode(payload: CancelRequest, request: Request) -> dict:
    """请求取消（协作式：任务体在下一个进度检查点 terminate ffmpeg）。"""
    _validate_source(payload.file_id)
    with _running_lock:
        entry = _running.get(payload.file_id)
    if entry is None:
        return {"success": False, "message": "该文件没有正在进行的转码任务"}
    if not registry.request_cancel(entry.task_id):
        return {"success": False, "message": "转码任务已结束"}
    logger.info(f"请求取消转码 file_id={payload.file_id} task={entry.task_id}")
    return {"success": True, "message": "已发送取消请求"}


@router.get("/stream")
def stream_transcoded(file_id: int, request: Request):
    """播放转码缓存（仅 done 时可读；支持 Range，手机可拖动进度条）。"""
    source = _validate_source(file_id)
    config = request.app.state.config

    output = ts.get_cached_output(source, config)
    if output is None:
        with _running_lock:
            running = _running.get(file_id)
        if running is not None:
            raise HTTPException(status_code=409, detail="转码进行中，请稍后再试")
        raise HTTPException(status_code=404, detail="转码缓存不存在，请先发起转码")

    return FileResponse(
        str(output),
        media_type="video/mp4",
        filename=Path(source.filename).stem + ".mp4",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/active")
def active_transcodes() -> dict:
    """当前运行中的转码任务列表（Web 切页回来靠它恢复进度条）。"""
    with _running_lock:
        tasks = [entry.snapshot() for entry in _running.values()]
    tasks.sort(key=lambda item: item.get("started_at") or 0)
    return {"tasks": tasks, "count": len(tasks)}
