# -*- coding: utf-8 -*-
"""
查重路由 —— /api/dedup 真实数据库查询端点。

注意：处理函数一律使用同步 def（FastAPI 自动放入线程池执行），
避免长任务（run_dedup）阻塞事件循环；查重入口经 dedup_gate 与 GUI 互斥。
"""

import itertools
import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.schemas import DedupResultItem, DedupListResponse, DedupResolveRequest, StatusResponse
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services.dedup_gate import acquire_dedup_gate, release_dedup_gate
from app.services.dedup_service import run_dedup_pipeline

logger = logging.getLogger(__name__)


class DedupRunRequest(BaseModel):
    unit_ids: list[int] = Field(..., min_length=2)
    threshold: float = Field(default=0.80, ge=0.0, le=1.0)


# ============================================================
# 查重任务后台执行 + 轮询（POST /run 立即返回 task_id）
# ============================================================
_TASK_CAP = 20          # 保留的任务记录上限
_TASK_TTL_SECONDS = 3600  # 已完成任务的保留时间
_task_counter = itertools.count(1)
_tasks_lock = threading.Lock()
_dedup_tasks: dict[str, dict] = {}


def _prune_tasks(now: float) -> None:
    """清理超期已完成任务；超额时先淘汰最旧已完成任务。"""
    with _tasks_lock:
        expired = [
            tid for tid, t in _dedup_tasks.items()
            if t.get("finished_at") and now - t["finished_at"] > _TASK_TTL_SECONDS
        ]
        for tid in expired:
            _dedup_tasks.pop(tid, None)
        finished = sorted(
            (tid for tid, t in _dedup_tasks.items()
             if t.get("status") in ("completed", "failed")),
            key=lambda tid: _dedup_tasks[tid]["finished_at"],
        )
        while len(_dedup_tasks) > _TASK_CAP and finished:
            _dedup_tasks.pop(finished.pop(0), None)


def _run_dedup_task(task_id: str, unit_ids: list[int], threshold: float,
                    cfg_fields: dict) -> None:
    """后台执行查重管线，并把状态/进度/结果写回任务表。"""
    def update(**kw) -> None:
        with _tasks_lock:
            if task_id in _dedup_tasks:
                _dedup_tasks[task_id].update(kw)

    update(status="running", started_at=time.time())
    try:
        if not acquire_dedup_gate():
            update(status="failed",
                   error="已有查重任务正在运行（桌面端或其它请求），请稍后再试",
                   finished_at=time.time())
            return
        try:
            outcome = run_dedup_pipeline(
                unit_ids,
                threshold=threshold,
                phash_hamming_threshold=cfg_fields["phash_hamming_threshold"],
                dhash_hamming_threshold=cfg_fields["dhash_hamming_threshold"],
                face_distance_threshold=cfg_fields["face_distance_threshold"],
                face_enabled=cfg_fields["face_enabled"],
                load_face=cfg_fields["face_enabled"],  # 与 GUI 行为一致
                progress_callback=lambda cur, total: update(progress=[cur, total]),
            )
        finally:
            release_dedup_gate()
        update(
            status="completed",
            progress=None,
            finished_at=time.time(),
            result={
                "total_compared": outcome.session.total_units_compared,
                "duplicates_found": len(outcome.session.duplicates_found),
                "saved_count": outcome.saved_count,
                "skipped_pairs": outcome.skipped_pairs,
                "elapsed_seconds": outcome.elapsed_seconds,
            },
        )
    except ValueError as e:
        update(status="failed", error=str(e), finished_at=time.time())
    except Exception as e:
        logger.error(f"查重任务 {task_id} 执行失败: {e}", exc_info=True)
        update(status="failed", error="内部错误，详见服务端日志", finished_at=time.time())


router = APIRouter(prefix="/api/dedup", tags=["查重"])


@router.get("/results", response_model=DedupListResponse)
def list_dedup_results(
    unresolved_only: bool = Query(True, description="仅显示未处理"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """列出查重比对结果（单元名一次性批量查询，避免 N+1）。"""
    try:
        with DatabaseManager.session() as session:
            all_results = q.get_unresolved_duplicates(session, limit=100) if unresolved_only else q.get_all_dedup_results(session, limit=200)
            total = len(all_results)
            start = (page - 1) * per_page
            page_results = all_results[start:start + per_page]

            # 批量取涉及单元名
            unit_ids = {dr.unit_a_id for dr in page_results} | {dr.unit_b_id for dr in page_results}
            unit_map = {u.id: u.name for u in q.get_units_by_ids(session, list(unit_ids))}

            items = [
                DedupResultItem(
                    id=dr.id,
                    unit_a_id=dr.unit_a_id, unit_b_id=dr.unit_b_id,
                    unit_a_name=unit_map.get(dr.unit_a_id, f"单元{dr.unit_a_id}"),
                    unit_b_name=unit_map.get(dr.unit_b_id, f"单元{dr.unit_b_id}"),
                    similarity_score=dr.similarity_score,
                    match_count=dr.match_count,
                    match_types=dr.match_types or "",
                    is_resolved=dr.is_resolved,
                    resolution=dr.resolution,
                    created_at=dr.created_at,
                )
                for dr in page_results
            ]
            return DedupListResponse(results=items, total=total)
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.get("/results/{result_id}")
def get_dedup_detail(result_id: int):
    """获取查重结果详情（含文件匹配列表）。"""
    try:
        with DatabaseManager.session() as session:
            dr = q.get_dedup_by_id(session, result_id)
            if not dr:
                raise HTTPException(status_code=404, detail=f"查重结果不存在: {result_id}")

            matches = q.get_file_matches_for_result(session, result_id)
            unit_a = q.get_unit_by_id(session, dr.unit_a_id)
            unit_b = q.get_unit_by_id(session, dr.unit_b_id)

            return {
                "id": dr.id,
                "unit_a": {"id": dr.unit_a_id, "name": unit_a.name if unit_a else ""},
                "unit_b": {"id": dr.unit_b_id, "name": unit_b.name if unit_b else ""},
                "similarity_score": dr.similarity_score,
                "match_count": dr.match_count,
                "match_types": dr.match_types,
                "is_resolved": dr.is_resolved,
                "resolution": dr.resolution,
                "created_at": dr.created_at.isoformat() if dr.created_at else "",
                "file_matches": [
                    {"id": fm.id, "file_a_id": fm.file_a_id, "file_b_id": fm.file_b_id,
                     "similarity_score": fm.similarity_score, "match_type": fm.match_type}
                    for fm in matches
                ],
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/results/{result_id}/resolve", response_model=StatusResponse)
def resolve_dedup(result_id: int, body: DedupResolveRequest):
    """处理查重结果。"""
    try:
        with DatabaseManager.session() as session:
            dr = q.get_dedup_by_id(session, result_id)
            if not dr:
                raise HTTPException(status_code=404, detail=f"查重结果不存在: {result_id}")
            q.resolve_dedup(session, result_id, body.resolution)
            return StatusResponse(success=True, message=f"已标记: {body.resolution}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/run", status_code=202)
def run_dedup(body: DedupRunRequest, request: Request):
    """触发查重任务（异步）：立即返回 task_id，客户端轮询
    GET /api/dedup/run/{task_id} 获取进度/结果。与 GUI 入口共用互斥门。"""
    cfg = request.app.state.config
    task_id = f"dedup-{next(_task_counter)}"

    now = time.time()
    _prune_tasks(now)
    entry = {
        "task_id": task_id,
        "status": "queued",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "progress": None,
        "result": None,
        "error": None,
    }
    with _tasks_lock:
        _dedup_tasks[task_id] = entry

    cfg_fields = {
        "phash_hamming_threshold": cfg.phash_hamming_threshold,
        "dhash_hamming_threshold": cfg.dhash_hamming_threshold,
        "face_distance_threshold": cfg.face_distance_threshold,
        "face_enabled": cfg.face_detection_enabled,
    }
    threading.Thread(
        target=_run_dedup_task,
        args=(task_id, list(body.unit_ids), float(body.threshold), cfg_fields),
        daemon=True,
        name=f"dedup-task-{task_id}",
    ).start()

    return {"status": "accepted", "task_id": task_id}


@router.get("/run/{task_id}")
def get_dedup_task(task_id: str):
    """查询查重任务状态（轮询用）。"""
    _prune_tasks(time.time())
    with _tasks_lock:
        task = dict(_dedup_tasks.get(task_id) or {})
    if not task:
        raise HTTPException(status_code=404, detail=f"查重任务不存在: {task_id}")
    return task
