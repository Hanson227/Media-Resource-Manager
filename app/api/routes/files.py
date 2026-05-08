# -*- coding: utf-8 -*-
"""
文件路由 —— /api/files 真实数据库查询端点。
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, HTTPException

from app.api.schemas import FileItem, FileDetailResponse, FileListResponse, StatusResponse
from app.db.engine import DatabaseManager
from app.db import queries as q

router = APIRouter(prefix="/api/files", tags=["文件"])


@router.get("", response_model=FileListResponse)
async def list_files(
    unit_id: Optional[int] = Query(None, description="资源单元ID"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    """列出媒体文件（支持按单元筛选和分页）。"""
    try:
        with DatabaseManager.session() as session:
            if unit_id:
                all_files = q.get_files_by_unit(session, unit_id)
            else:
                all_files = []
                units = q.get_all_active_units(session)
                for u in units:
                    all_files.extend(q.get_files_by_unit(session, u.id))

            total = len(all_files)
            start = (page - 1) * per_page
            page_files = all_files[start:start + per_page]

            items = [
                FileItem(
                    id=f.id, filename=f.filename, media_type=f.media_type,
                    size_bytes=f.size_bytes,
                    width=f.width, height=f.height,
                    md5_hash=f.md5_hash,
                )
                for f in page_files
            ]
            return FileListResponse(files=items, total=total, page=page, per_page=per_page)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{file_id}", response_model=FileDetailResponse)
async def get_file(file_id: int):
    """获取单个文件完整详情。"""
    try:
        with DatabaseManager.session() as session:
            f = q.get_file_by_id(session, file_id)
            if not f:
                raise HTTPException(status_code=404, detail=f"文件不存在: {file_id}")
            return FileDetailResponse(
                id=f.id, filename=f.filename, path=f.path,
                media_type=f.media_type, extension=f.extension,
                size_bytes=f.size_bytes,
                width=f.width, height=f.height,
                duration_ms=f.duration_ms,
                md5_hash=f.md5_hash, phash=f.phash, dhash=f.dhash,
                resource_unit_id=f.resource_unit_id,
                indexed_at=f.indexed_at,
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{file_id}", response_model=StatusResponse)
async def delete_file(file_id: int):
    """删除指定文件记录。"""
    try:
        with DatabaseManager.session() as session:
            f = q.get_file_by_id(session, file_id)
            if not f:
                raise HTTPException(status_code=404, detail=f"文件不存在: {file_id}")
            q.delete_media_file(session, file_id)
            return StatusResponse(success=True, message=f"已删除: {f.filename}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{file_id}/thumbnail")
async def get_file_thumbnail(file_id: int):
    """获取文件缩略图路径信息。"""
    try:
        with DatabaseManager.session() as session:
            f = q.get_file_by_id(session, file_id)
            if not f:
                raise HTTPException(status_code=404, detail=f"文件不存在: {file_id}")
            unit = q.get_unit_by_id(session, f.resource_unit_id)
            if unit:
                thumb_file = Path(unit.path) / ".thumbnails" / f"{file_id}_thumb.jpg"
                if thumb_file.exists():
                    return {
                        "file_id": file_id,
                        "thumbnail_path": str(thumb_file),
                        "size": thumb_file.stat().st_size,
                    }
            return {"file_id": file_id, "thumbnail_path": None, "message": "缩略图未生成"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
