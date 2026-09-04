# -*- coding: utf-8 -*-
"""
文件路由 —— /api/files 真实数据库查询端点。
"""

import io
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.api.schemas import FileItem, FileDetailResponse, FileListResponse, StatusResponse

logger = logging.getLogger(__name__)

# HEIC/HEIF 浏览器不原生支持，注册 Pillow 插件以便在内存中转为 JPEG
try:
    import pillow_heif  # noqa: F401 — registers HEIF opener with Pillow
    pillow_heif.register_heif_opener()
except ImportError:
    logger.warning("pillow_heif 未安装，HEIC 文件无法在浏览器中查看")
    pillow_heif = None  # type: ignore[assignment]

from app.core.thumbnail_generator import ThumbnailGenerator
from app.db.engine import DatabaseManager
from app.db import queries as q

# 全局缩略图生成器（按需生成）
_thumb_gen = ThumbnailGenerator(max_size=256)

# 缩略图 HTTP 缓存头：封面变更后手机端能较快更新
_THUMB_HEADERS = {"Cache-Control": "private, max-age=300"}

router = APIRouter(prefix="/api/files", tags=["文件"])


@router.get("", response_model=FileListResponse)
def list_files(
    unit_id: Optional[int] = Query(None, description="资源单元ID"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
):
    """列出媒体文件（支持按单元筛选和 SQL 分页，避免全量载入内存）。"""
    try:
        with DatabaseManager.session() as session:
            page_files, total = q.get_files_page(
                session, unit_id=unit_id, page=page, per_page=per_page,
            )
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
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.get("/{file_id}", response_model=FileDetailResponse)
def get_file(file_id: int):
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
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.delete("/{file_id}", response_model=StatusResponse)
def delete_file(file_id: int, request: Request):
    """删除指定文件记录（同时清理缩略图缓存）。"""
    unit_path = None
    try:
        with DatabaseManager.session() as session:
            f = q.get_file_by_id(session, file_id)
            if not f:
                raise HTTPException(status_code=404, detail=f"文件不存在: {file_id}")
            # 删除前记录单元路径，删完后清理缩略图用
            unit = q.get_unit_by_id(session, f.resource_unit_id)
            unit_path = Path(unit.path) if unit else None
            q.delete_media_file(session, file_id)

        # 清理缩略图缓存（文件已删，用之前记录的路径）
        cfg = getattr(request.app.state, "config", None)
        if cfg and cfg.thumbnail_cache_dir:
            central = Path(cfg.thumbnail_cache_dir) / f"{file_id}_thumb.jpg"
            if central.exists():
                central.unlink()
        if unit_path:
            local = unit_path / ".thumbnails" / f"{file_id}_thumb.jpg"
            if local.exists():
                local.unlink()

        return StatusResponse(success=True, message=f"已删除: {f.filename}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.get("/{file_id}/thumbnail")
def get_file_thumbnail(file_id: int, request: Request):
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
                    return FileResponse(str(central), media_type="image/jpeg", headers=_THUMB_HEADERS)

            unit = q.get_unit_by_id(session, f.resource_unit_id)
            if unit:
                thumb_file = Path(unit.path) / ".thumbnails" / f"{file_id}_thumb.jpg"
                if thumb_file.exists():
                    return FileResponse(str(thumb_file), media_type="image/jpeg", headers=_THUMB_HEADERS)

            # 缓存不存在，按需生成
            if unit and f.path:
                src = Path(f.path)
                if src.is_file():
                    cache_dir = cfg.thumbnail_cache_dir if cfg and cfg.thumbnail_cache_dir else Path(unit.path) / ".thumbnails"
                    try:
                        info = _thumb_gen.generate(src, cache_dir, file_id=file_id)
                        if info.thumbnail_path.exists():
                            return FileResponse(str(info.thumbnail_path), media_type="image/jpeg", headers=_THUMB_HEADERS)
                    except Exception as gen_e:
                        logger.warning(f"缩略图按需生成失败: {f.path} - {gen_e}")

        raise HTTPException(status_code=404, detail="缩略图未生成")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


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
def stream_file(file_id: int):
    """流式传输原始媒体文件（支持 Range 请求头）。"""
    with DatabaseManager.session() as session:
        f = q.get_file_by_id(session, file_id)
        if not f:
            raise HTTPException(status_code=404, detail="文件不存在")

    path = Path(f.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已在磁盘上移除")

    media_type = MIME_MAP.get(f.extension.lower(), "application/octet-stream")
    # HEIC 浏览器不原生支持，转为 JPEG 传输
    if media_type == "image/heic":
        if pillow_heif is None:
            raise HTTPException(status_code=415, detail="HEIC 支持未安装（缺少 pillow_heif）")
        try:
            from PIL import Image
            with Image.open(path) as img:
                rgb_img = img.convert("RGB")
                buf = io.BytesIO()
                rgb_img.save(buf, "JPEG", quality=90)
                buf.seek(0)
            return Response(content=buf.read(), media_type="image/jpeg",
                            headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(f.filename + '.jpg')}"})
        except Exception as e:
            logger.error(f"HEIC 转换失败 {f.path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="内部服务器错误")
    return FileResponse(str(path), media_type=media_type, filename=f.filename)
