# -*- coding: utf-8 -*-
"""
查重路由 —— /api/dedup 真实数据库查询端点。
"""

import threading

from fastapi import APIRouter, Query, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.schemas import DedupResultItem, DedupListResponse, DedupResolveRequest, StatusResponse
from app.core.dedup_engine import DedupEngine
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services.message_center import MessageCenter

_dedup_lock = threading.Lock()
_dedup_running = False


class DedupRunRequest(BaseModel):
    unit_ids: list[int] = Field(..., min_length=2)
    threshold: float = Field(default=0.80, ge=0.0, le=1.0)


router = APIRouter(prefix="/api/dedup", tags=["查重"])


@router.get("/results", response_model=DedupListResponse)
async def list_dedup_results(
    unresolved_only: bool = Query(True, description="仅显示未处理"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """列出查重比对结果。"""
    try:
        with DatabaseManager.session() as session:
            results = q.get_unresolved_duplicates(session, limit=100) if unresolved_only else []
            total = len(results)
            start = (page - 1) * per_page
            page_results = results[start:start + per_page]

            items = []
            for dr in page_results:
                unit_a = q.get_unit_by_id(session, dr.unit_a_id)
                unit_b = q.get_unit_by_id(session, dr.unit_b_id)
                items.append(DedupResultItem(
                    id=dr.id,
                    unit_a_id=dr.unit_a_id, unit_b_id=dr.unit_b_id,
                    unit_a_name=unit_a.name if unit_a else f"单元{dr.unit_a_id}",
                    unit_b_name=unit_b.name if unit_b else f"单元{dr.unit_b_id}",
                    similarity_score=dr.similarity_score,
                    match_count=dr.match_count,
                    match_types=dr.match_types or "",
                    is_resolved=dr.is_resolved,
                    resolution=dr.resolution,
                    created_at=dr.created_at,
                ))
            return DedupListResponse(results=items, total=total)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/results/{result_id}")
async def get_dedup_detail(result_id: int):
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
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/results/{result_id}/resolve", response_model=StatusResponse)
async def resolve_dedup(result_id: int, body: DedupResolveRequest):
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
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run")
async def run_dedup(body: DedupRunRequest, request: Request):
    """触发查重任务。"""
    global _dedup_running

    if not _dedup_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="查重任务正在运行中")
    if _dedup_running:
        _dedup_lock.release()
        raise HTTPException(status_code=409, detail="查重任务正在运行中")

    _dedup_running = True
    _dedup_lock.release()

    try:
        cfg = request.app.state.config

        unit_files_map = {}
        unit_names = {}
        with DatabaseManager.session() as session:
            for uid in body.unit_ids:
                unit = q.get_unit_by_id(session, uid)
                if not unit:
                    continue
                unit_names[uid] = unit.name
                files = q.get_files_by_unit(session, uid)
                unit_files_map[uid] = [
                    {"id": f.id, "path": f.path,
                     "md5_hash": f.md5_hash, "phash": f.phash, "dhash": f.dhash}
                    for f in files
                ]

        if len(unit_files_map) < 2:
            return {"status": "error", "message": "至少需要 2 个有效资源单元"}

        engine = DedupEngine(
            jaccard_threshold=body.threshold,
            phash_hamming_threshold=cfg.phash_hamming_threshold,
            dhash_hamming_threshold=cfg.dhash_hamming_threshold,
            face_enabled=cfg.face_detection_enabled,
            face_distance_threshold=cfg.face_distance_threshold,
        )

        result = engine.run_dedup(unit_files_map, unit_names)

        saved_results = []
        with DatabaseManager.session() as session:
            for dup in result.duplicates_found:
                dr = q.upsert_dedup_result(
                    session, dup.unit_a_id, dup.unit_b_id,
                    dup.jaccard_similarity, len(dup.file_matches),
                    dup.total_files_a, dup.total_files_b,
                    dup.match_types_str,
                )
                # 清除旧匹配对，避免 UNIQUE 约束冲突
                q.delete_file_matches_for_result(session, dr.id)
                for fm in dup.file_matches:
                    q.insert_file_match(
                        session, dr.id, fm.file_a_id, fm.file_b_id,
                        fm.score, fm.match_type,
                    )
                saved_results.append((dup, dr.id))
            # session 在此提交，释放锁

        # 在独立 session 中创建消息，避免 SQLite 锁冲突
        for dup, dr_id in saved_results:
            MessageCenter.create_dedup_alert(
                unit_a_name=dup.unit_a_name,
                unit_b_name=dup.unit_b_name,
                similarity=dup.jaccard_similarity,
                match_count=len(dup.file_matches),
                dedup_result_id=dr_id,
            )

        return {
            "status": "completed",
            "total_compared": result.total_units_compared,
            "duplicates_found": len(result.duplicates_found),
            "elapsed_seconds": result.elapsed_seconds,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _dedup_running = False
