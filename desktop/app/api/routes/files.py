# -*- coding: utf-8 -*-
"""
文件路由 —— /api/files 真实数据库查询端点。
"""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import FileResponse

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
async def get_file_thumbnail(file_id: int, request: Request):
    """返回缩略图图片二进制（供手机端直接显示）。"""
    try:
        with DatabaseManager.session() as session:
            f = q.get_file_by_id(session, file_id)
            if not f:
                raise HTTPException(status_code=404, detail=f"文件不存在: {file_id}")

            # 优先集中缓存，其次旧版 per-unit 缓存
            cfg = getattr(request.app.state, "config", None)
            if cfg and cfg.thumbnail_cache_dir:
                central = Path(cfg.thumbnail_cache_dir) / f"{file_id}_thumb.jpg"
                if central.exists():
                    return FileResponse(str(central), media_type="image/jpeg")

            unit = q.get_unit_by_id(session, f.resource_unit_id)
            if unit:
                thumb_file = Path(unit.path) / ".thumbnails" / f"{file_id}_thumb.jpg"
                if thumb_file.exists():
                    return FileResponse(str(thumb_file), media_type="image/jpeg")

        raise HTTPException(status_code=404, detail="缩略图未生成")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".mov": "video/quicktime",
    ".webm": "video/webm", ".flv": "video/x-flv",
    ".ts": "video/mp2t", ".m4v": "video/mp4",
    ".heic": "image/heic", ".heif": "image/heif",
}


@router.get("/{file_id}/stream")
async def stream_file(file_id: int):
    """流式传输原始媒体文件（支持 Range 请求头）。"""
    with DatabaseManager.session() as session:
        f = q.get_file_by_id(session, file_id)
        if not f:
            raise HTTPException(status_code=404, detail="文件不存在")

    path = Path(f.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已在磁盘上移除")

    media_type = MIME_MAP.get(f.extension.lower(), "application/octet-stream")
    return FileResponse(str(path), media_type=media_type, filename=f.filename)
