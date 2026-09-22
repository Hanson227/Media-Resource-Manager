# -*- coding: utf-8 -*-
"""
HEIC 检查与转换路由 —— /api/heic。

与桌面端共用 `app.services.heic_service`：检查（scan）是只读的，转换（convert）
跑到后台线程里，客户端轮询 `/run/{task_id}` 或 `/active-task` 接上进度。

注意：处理函数一律用同步 def（FastAPI 自动放线程池），避免磁盘遍历/转换阻塞事件循环。
"""

import logging
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api import task_registry as registry
from app.core.heic_converter import (
    CONFLICT_MODES, DEFAULT_JPEG_QUALITY, is_heic_file,
)
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services import file_ops
from app.services import heic_service as svc
from app.services.dedup_gate import is_dedup_busy

logger = logging.getLogger(__name__)

HEIC_TASK_KINDS = ("heic",)
"""本路由负责的任务类型（active-task 只认它，不会误报查重任务）。"""

router = APIRouter(prefix="/api/heic", tags=["HEIC"])


# ============================================================
# 请求模型
# ============================================================

class HeicConvertOptionsBody(BaseModel):
    quality: int = Field(default=DEFAULT_JPEG_QUALITY, ge=1, le=100,
                         description="输出 JPEG 质量（1-100）")
    source_action: str = Field(
        default=svc.SOURCE_TRASH, pattern=svc.SOURCE_ACTION_PATTERN,
        description="源文件处置：trash=移至回收站（默认）/ delete=永久删除 / keep=保留。"
                    "回收站不可用（网络盘、无回收站分区）时服务端**保留源文件**并上报，"
                    "绝不静默降级为永久删除。",
    )
    on_conflict: str = Field(
        default="skip", pattern="^(" + "|".join(CONFLICT_MODES) + ")$",
        description="同名 JPG 已存在时：skip=跳过（默认）/ rename=自动加序号 / overwrite=覆盖。",
    )
    reindex: bool = Field(default=True, description="转换后立即重算哈希与人脸")
    fix_broken: bool = Field(
        default=True,
        description="同时修复「哈希为空串」的旧 HEIC 坏行"
                    "（旧版解不开 HEIC 留下的永久占位，不重置就永远不会被重算）",
    )


class HeicConvertRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, description="待转换的 HEIC 文件绝对路径")
    options: HeicConvertOptionsBody = Field(default_factory=HeicConvertOptionsBody)


# ============================================================
# 检查
# ============================================================

def _candidate_dict(c: svc.HeicCandidate) -> dict:
    return {
        "path": c.path,
        "filename": Path(c.path).name,
        "file_id": c.file_id,
        "size_bytes": c.size_bytes,
        "unit_id": c.unit_id,
        "unit_name": c.unit_name,
        "in_library": c.in_library,
        "exists": c.exists,
        "volume_ok": c.volume_ok,
        "has_perceptual_hash": c.has_perceptual_hash,
    }


@router.get("/scan")
def scan_heic(request: Request,
              include_disk: bool = Query(
                  True, description="同时遍历磁盘，补上还没入库的 HEIC（默认开）"),
              root_ids: Optional[str] = Query(
                  None, description="逗号分隔的媒体库根 ID；省略则扫描全部启用根")):
    """检查媒体库里的 HEIC 文件（只读，不修改任何东西）。

    返回的 `paths` 由客户端在转换时回传，因此检查本身没有副作用。
    """
    cfg = request.app.state.config
    try:
        roots = None
        if root_ids:
            ids = [int(x) for x in root_ids.split(",") if x.strip()]
            with DatabaseManager.session() as session:
                roots = [u.path for u in q.get_all_roots(session) if u.id in ids]
                if not roots:
                    raise HTTPException(status_code=404, detail="指定的媒体库根不存在")
        result = svc.scan_candidates(cfg, roots=roots, include_disk=include_disk)
        return {
            "total": result.total,
            "total_bytes": result.total_bytes,
            "from_db": result.from_db,
            "from_disk_only": result.from_disk_only,
            "broken_hash_count": result.broken_hash_count,
            "missing_count": result.missing_count,
            "invalid_header": result.invalid_header,
            "skipped_volume_roots": list(result.skipped_volume_roots),
            "roots_scanned": list(result.roots_scanned),
            "items": [_candidate_dict(c) for c in result.candidates],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HEIC 检查失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


# ============================================================
# 转换（异步任务）
# ============================================================

def _validate_paths(paths: list[str], config) -> list[str]:
    """校验待转换路径：必须在启用中的媒体库根下、且确实是 HEIC 文件。

    禁止任意路径有两个原因：① 转换会删源文件，越界路径等于把任意文件交给
    该端点处置；② 检查结果里的 `paths` 全部来自库内或库根的磁盘遍历。
    """
    with DatabaseManager.session() as session:
        root_paths = [str(Path(r.path)) for r in q.get_enabled_roots(session)]

    def under_root(p: Path) -> bool:
        s = str(p).lower()
        return any(s.startswith(str(Path(r)).lower().rstrip("\\/") + "\\")
                   or s.startswith(str(Path(r)).lower().rstrip("\\/") + "/")
                   for r in root_paths)

    accepted: list[str] = []
    for raw in paths:
        p = Path(raw)
        if not under_root(p):
            raise HTTPException(status_code=400,
                                detail=f"路径不在媒体库范围内: {raw}")
        if not p.is_file():
            raise HTTPException(status_code=404, detail=f"文件不存在: {raw}")
        if not file_ops.volume_available(p):
            raise HTTPException(status_code=409,
                                detail=f"磁盘未连接: {file_ops.volume_label(p)}")
        if not is_heic_file(p):
            raise HTTPException(status_code=415, detail=f"不是 HEIC 格式: {raw}")
        accepted.append(str(p))
    return accepted


def _run_heic_task(task_id: str, paths: list[str], options, config) -> None:
    """后台执行 HEIC 转换，把状态/进度/结果写回任务登记表。"""
    def update(**kw) -> None:
        registry.update(task_id, **kw)

    update(status="running", started_at=time.time(), phase="converting")
    try:
        try:
            outcome = svc.run_heic_pipeline(
                config, paths, options,
                progress_callback=lambda phase, cur, total: update(
                    phase=phase, progress=[cur, total]),
                cancelled=lambda: registry.is_cancel_requested(task_id),
            )
        except svc.HeicBusyError as e:
            update(status="failed", error=str(e), finished_at=time.time())
            return
        update(
            status="completed",
            phase=None,
            progress=None,
            finished_at=time.time(),
            result={
                "total": outcome.total,
                "converted": outcome.converted,
                "skipped": outcome.skipped,
                "failed": outcome.failed,
                "sources_removed": outcome.sources_removed,
                "sources_kept": outcome.sources_kept,
                "db_updated": outcome.db_updated,
                "healed": outcome.healed,
                "reindexed": outcome.reindexed,
                "cancelled": outcome.cancelled,
                "elapsed_seconds": outcome.elapsed_seconds,
                "items": [
                    {"path": i.path, "target": i.target, "status": i.status,
                     "error": i.error, "source_kept_reason": i.source_kept_reason}
                    for i in outcome.items
                ],
            },
        )
    except Exception as e:
        logger.error(f"HEIC 任务 {task_id} 执行失败: {e}", exc_info=True)
        update(status="failed", error="内部错误，详见服务端日志", finished_at=time.time())


@router.post("/convert", status_code=202)
def convert_heic(body: HeicConvertRequest, request: Request):
    """触发 HEIC → JPG 转换（异步）：立即返回 task_id，轮询 /run/{task_id}。"""
    cfg = request.app.state.config
    if is_dedup_busy():
        raise HTTPException(
            status_code=409,
            detail="已有查重/人脸重扫/HEIC 转换在运行，请稍后再试")

    accepted = _validate_paths(list(body.paths), cfg)

    task_id = registry.next_task_id("heic")
    registry.prune()
    registry.register(task_id, "heic", phase="converting", progress=[0, len(accepted)])

    options = svc.HeicConvertOptions(
        quality=int(body.options.quality),
        source_action=body.options.source_action,
        on_conflict=body.options.on_conflict,
        reindex=bool(body.options.reindex),
        fix_broken=bool(body.options.fix_broken),
    )
    threading.Thread(
        target=_run_heic_task,
        args=(task_id, accepted, options, cfg),
        daemon=True,
        name=f"heic-task-{task_id}",
    ).start()
    return {"status": "accepted", "task_id": task_id, "count": len(accepted)}


@router.get("/run/{task_id}")
def get_heic_task(task_id: str):
    """查询 HEIC 任务状态（轮询用）。"""
    registry.prune()
    task = registry.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"HEIC 任务不存在: {task_id}")
    return task


@router.get("/active-task")
def get_heic_active_task():
    """当前仍在跑/排队的 HEIC 转换任务（没有则 task_id 为 None）。

    页面切走/整页刷新后靠它重新接上进度条（与查重页同一套做法）。
    """
    task = registry.active(HEIC_TASK_KINDS)
    if not task:
        return {"task_id": None, "status": None, "kind": None}
    return task


@router.post("/cancel")
def cancel_heic():
    """请求取消正在跑的 HEIC 转换（协作式：当前文件处理完就停）。"""
    task_id = registry.cancel_active(HEIC_TASK_KINDS)
    if not task_id:
        return {"status": "idle", "task_id": None, "detail": "没有正在运行的 HEIC 任务"}
    return {"status": "cancelling", "task_id": task_id}
