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
from app.utils.constants import MatchType

logger = logging.getLogger(__name__)


class DedupRunRequest(BaseModel):
    unit_ids: list[int] = Field(..., min_length=2)
    threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    index_first: bool = Field(
        default=False,
        description="先为未索引文件计算哈希（MD5/视频帧/人脸）再比对。"
                    "与桌面端「一键查重」一致；为 False 时未索引文件对查重不可见。",
    )


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
                    cfg_fields: dict, config, index_first: bool = False) -> None:
    """后台执行查重管线，并把状态/进度/结果写回任务表。

    参数:
        index_first: 为 True 时先跑索引管线（与桌面端「一键查重」一致）。
            不索引就比对，未索引文件对查重完全不可见，却会返回"查重完成"。
    """
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
            indexed_count = 0
            unindexed_before = 0
            if index_first:
                from app.db.engine import DatabaseManager
                from app.db import queries as q
                from app.services.index_service import run_index_pipeline
                with DatabaseManager.session() as session:
                    unindexed_before = q.get_unindexed_file_count(session)
                if unindexed_before > 0:
                    update(phase="indexing", progress=[0, unindexed_before])
                    index_result = run_index_pipeline(
                        config,
                        progress_callback=lambda cur, total: update(
                            phase="indexing", progress=[cur, total]),
                    )
                    indexed_count = index_result.hashed_count
                    update(phase="comparing", progress=None)

            update(phase="comparing")
            outcome = run_dedup_pipeline(
                unit_ids,
                threshold=threshold,
                phash_hamming_threshold=cfg_fields["phash_hamming_threshold"],
                dhash_hamming_threshold=cfg_fields["dhash_hamming_threshold"],
                face_similarity_threshold=cfg_fields["face_similarity_threshold"],
                face_enabled=cfg_fields["face_enabled"],
                related_min_matches=cfg_fields["related_min_matches"],
                load_face=cfg_fields["face_enabled"],  # 与 GUI 行为一致
                progress_callback=lambda cur, total: update(progress=[cur, total]),
            )
        finally:
            release_dedup_gate()
        update(
            status="completed",
            phase=None,
            progress=None,
            finished_at=time.time(),
            result={
                "total_compared": outcome.session.total_units_compared,
                "duplicates_found": len(outcome.session.duplicates_found),
                "related_found": len(outcome.session.related_found),
                "saved_count": outcome.saved_count,
                "related_saved": outcome.related_saved,
                "skipped_pairs": outcome.skipped_pairs,
                "elapsed_seconds": outcome.elapsed_seconds,
                "unindexed_before": unindexed_before,
                "indexed_count": indexed_count,
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
    level: Optional[str] = Query(
        None, pattern="^(duplicate|related)$",
        description="按命中等级过滤：duplicate=建议处置的重复；related=仅提醒的疑似相关",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """列出查重比对结果（SQL 分页 + 精确总数；单元名批量查询，避免 N+1）。"""
    try:
        with DatabaseManager.session() as session:
            page_results, total = q.get_dedup_results_page(
                session, unresolved_only=unresolved_only,
                page=page, per_page=per_page, level=level,
            )

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
                    match_level=dr.match_level,
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


@router.get("/counts")
def get_dedup_counts():
    """按命中等级统计未处置结果数量（列表页分组标题用）。"""
    try:
        with DatabaseManager.session() as session:
            return q.count_dedup_results_by_level(session)
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

            # 文件名：只显示 "#101 ↔ #103" 无法让用户判断该不该处理。
            # 一次 IN 查询取出本结果涉及的全部文件名（避免 N+1）。
            file_ids = {fm.file_a_id for fm in matches} | {fm.file_b_id for fm in matches}
            name_map: dict[int, str] = {}
            if file_ids:
                from app.db.models import MediaFile
                rows = (
                    session.query(MediaFile.id, MediaFile.filename)
                    .filter(MediaFile.id.in_(list(file_ids)))
                    .all()
                )
                name_map = {fid: fname for fid, fname in rows}

            return {
                "id": dr.id,
                "unit_a": {"id": dr.unit_a_id, "name": unit_a.name if unit_a else ""},
                "unit_b": {"id": dr.unit_b_id, "name": unit_b.name if unit_b else ""},
                "similarity_score": dr.similarity_score,
                "match_count": dr.match_count,
                "match_types": dr.match_types,
                "match_level": dr.match_level,
                "is_resolved": dr.is_resolved,
                "resolution": dr.resolution,
                "created_at": dr.created_at.isoformat() if dr.created_at else "",
                "file_matches": [
                    {"id": fm.id, "file_a_id": fm.file_a_id, "file_b_id": fm.file_b_id,
                     "file_a_name": name_map.get(fm.file_a_id, f"#{fm.file_a_id}"),
                     "file_b_name": name_map.get(fm.file_b_id, f"#{fm.file_b_id}"),
                     "similarity_score": fm.similarity_score, "match_type": fm.match_type,
                     # 人脸匹配是"线索"而非重复证据（不计入杰卡德），客户端要能区分
                     "is_hint": fm.match_type == MatchType.FACE.value}
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
        "phase": None,
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
        "face_similarity_threshold": cfg.face_similarity_threshold,
        "face_enabled": cfg.face_detection_enabled,
        "related_min_matches": cfg.related_min_matches,
    }
    threading.Thread(
        target=_run_dedup_task,
        args=(task_id, list(body.unit_ids), float(body.threshold), cfg_fields,
              cfg, bool(body.index_first)),
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
