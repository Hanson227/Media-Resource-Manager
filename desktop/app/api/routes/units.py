# -*- coding: utf-8 -*-
"""
单元路由 —— /api/units 真实数据库查询端点。
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy import func

from app.api.schemas import UnitItem, UnitListResponse
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.db.models import MediaFile

router = APIRouter(prefix="/api/units", tags=["资源单元"])


@router.get("", response_model=UnitListResponse)
async def list_units():
    """列出所有活跃状态的资源单元。"""
    try:
        with DatabaseManager.session() as session:
            units = q.get_all_active_units(session)

            # 封面文件 ID：手动设置的 cover_path 优先，没有则退到首个文件
            unit_ids = [u.id for u in units]
            cover_rows = (
                session.query(
                    MediaFile.resource_unit_id,
                    func.min(MediaFile.id).label("first_id"),
                )
                .filter(MediaFile.resource_unit_id.in_(unit_ids))
                .group_by(MediaFile.resource_unit_id)
                .all()
            )
            first_map = {row.resource_unit_id: row.first_id for row in cover_rows}
            # 查找手动设置过封面的单元 → 对应 cover_path 的 file_id
            cover_path_map = {}
            for u in units:
                if u.cover_path:
                    row = session.query(MediaFile.id).filter(
                        MediaFile.resource_unit_id == u.id,
                        MediaFile.path == u.cover_path,
                    ).first()
                    if row:
                        cover_path_map[u.id] = row[0]

            items = [
                UnitItem(
                    id=u.id, name=u.name, path=u.path,
                    file_count=u.file_count, total_size=u.total_size,
                    is_manual=u.is_manual, is_starred=u.is_starred,
                    status=u.status,
                    library_root_id=u.library_root_id,
                    library_root_name=Path(u.library_root.path).name if u.library_root else None,
                    cover_file_id=cover_path_map.get(u.id) or first_map.get(u.id),
                    created_at=u.created_at.isoformat() if u.created_at else None,
                )
                for u in units
            ]
            return UnitListResponse(units=items)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{unit_id}", response_model=UnitItem)
async def get_unit(unit_id: int):
    """获取单个资源单元详情。"""
    try:
        with DatabaseManager.session() as session:
            u = q.get_unit_by_id(session, unit_id)
            if not u:
                raise HTTPException(status_code=404, detail=f"单元不存在: {unit_id}")
            # 封面 ID：手动 cover_path 优先
            cover_file_id = None
            if u.cover_path:
                row = session.query(MediaFile.id).filter(
                    MediaFile.resource_unit_id == unit_id,
                    MediaFile.path == u.cover_path,
                ).first()
                cover_file_id = row[0] if row else None
            if cover_file_id is None:
                first_file = (
                    session.query(MediaFile.id)
                    .filter(MediaFile.resource_unit_id == unit_id)
                    .order_by(MediaFile.id)
                    .first()
                )
                cover_file_id = first_file[0] if first_file else None
            return UnitItem(
                id=u.id, name=u.name, path=u.path,
                file_count=u.file_count, total_size=u.total_size,
                is_manual=u.is_manual, is_starred=u.is_starred,
                status=u.status,
                library_root_id=u.library_root_id,
                library_root_name=Path(u.library_root.path).name if u.library_root else None,
                cover_file_id=cover_file_id,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{unit_id}/files")
async def get_unit_files(unit_id: int):
    """获取指定单元的所有文件列表（简要）。"""
    try:
        with DatabaseManager.session() as session:
            u = q.get_unit_by_id(session, unit_id)
            if not u:
                raise HTTPException(status_code=404, detail=f"单元不存在: {unit_id}")
            files = q.get_files_by_unit(session, unit_id)
            return {
                "unit_id": unit_id,
                "unit_name": u.name,
                "file_count": len(files),
                "files": [
                    {
                        "id": f.id,
                        "filename": f.filename,
                        "media_type": f.media_type,
                        "size_bytes": f.size_bytes,
                        "duration_ms": f.duration_ms,
                        "created_at": f.indexed_at.isoformat() if f.indexed_at else None,
                    }
                    for f in files[:200]
                ],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
