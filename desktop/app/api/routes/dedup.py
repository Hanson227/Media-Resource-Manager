# -*- coding: utf-8 -*-
"""
查重路由 —— /api/dedup 真实数据库查询端点。
"""

from fastapi import APIRouter, Query, HTTPException

from app.api.schemas import DedupResultItem, DedupListResponse, DedupResolveRequest, StatusResponse
from app.db.engine import DatabaseManager
from app.db import queries as q

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
