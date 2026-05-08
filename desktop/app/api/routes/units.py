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

            # 批量查询每个单元第一个文件的 ID（封面缩略图用）
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
            cover_map = {row.resource_unit_id: row.first_id for row in cover_rows}

            items = [
                UnitItem(
                    id=u.id, name=u.name, path=u.path,
                    file_count=u.file_count, total_size=u.total_size,
                    is_manual=u.is_manual, is_starred=u.is_starred,
                    status=u.status,
                    library_root_id=u.library_root_id,
                    library_root_name=Path(u.library_root.path).name if u.library_root else None,
                    cover_file_id=cover_map.get(u.id),
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
            # 单个单元也查封面
            first_file = (
                session.query(MediaFile.id)
                .filter(MediaFile.resource_unit_id == unit_id)
                .order_by(MediaFile.id)
                .first()
            )
            return UnitItem(
                id=u.id, name=u.name, path=u.path,
                file_count=u.file_count, total_size=u.total_size,
                is_manual=u.is_manual, is_starred=u.is_starred,
                status=u.status,
                library_root_id=u.library_root_id,
                library_root_name=Path(u.library_root.path).name if u.library_root else None,
                cover_file_id=first_file[0] if first_file else None,
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
                    }
                    for f in files[:200]
                ],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
