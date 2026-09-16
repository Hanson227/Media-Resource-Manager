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

from app.api.schemas import (
    BatchDeleteFailure, BatchDeleteRequest, BatchDeleteResponse,
    FileItem, FileDetailResponse, FileListResponse, StatusResponse,
)
from app.services.file_ops import MODE_PATTERN, MODE_TRASH, apply_mode

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

# 全局缩略图生成器（按 max_size 缓存，尺寸来自 config.thumbnail_max_size）
_thumb_gens: dict[int, ThumbnailGenerator] = {}
_DEFAULT_THUMB_SIZE = 256


def _get_thumb_gen(max_size: int) -> ThumbnailGenerator:
    """按配置尺寸取缩略图生成器（避免硬编码 256 忽略用户设置）。"""
    gen = _thumb_gens.get(max_size)
    if gen is None:
        gen = ThumbnailGenerator(max_size=max_size)
        _thumb_gens[max_size] = gen
    return gen

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


def _purge_thumbnail_cache(file_id: int, unit_path: Optional[Path], cfg) -> None:
    """删除某文件的缩略图缓存（集中缓存 + 旧版单元内缓存）。

    缩略图与它的 `.meta` sidecar 必须一起删：只删图会在目录里留下孤儿 meta，
    而 meta 正是判断"缓存属于哪个文件"的依据（见 ThumbnailGenerator.is_cache_valid）。
    """
    dirs: list[Path] = []
    if cfg and getattr(cfg, "thumbnail_cache_dir", None):
        dirs.append(Path(cfg.thumbnail_cache_dir))
    if unit_path:
        dirs.append(unit_path / ".thumbnails")
    for d in dirs:
        for suffix in ("", ".meta"):
            target = d / f"{file_id}_thumb.jpg{suffix}"
            if target.exists():
                target.unlink()


def _refresh_unit_stats(unit_id: Optional[int]) -> None:
    """删除文件后重算单元统计。

    unit.file_count / total_size 是落库的快照列，删文件不会自动变，
    而 Web 的单元卡片直接显示它们 —— 不重算就会显示"6 个文件"却只剩 5 个，
    直到下次扫描才纠正。
    """
    if not unit_id:
        return
    from sqlalchemy import func
    from app.db.models import MediaFile
    with DatabaseManager.session() as session:
        row = (
            session.query(
                func.count(MediaFile.id),
                func.coalesce(func.sum(MediaFile.size_bytes), 0),
            )
            .filter(MediaFile.resource_unit_id == unit_id)
            .one()
        )
        q.update_unit_stats(session, unit_id, int(row[0]), int(row[1]))


def _delete_one(file_id: int, mode: str, cfg) -> tuple[bool, str]:
    """删除一个媒体文件（按 mode 决定是否真的动磁盘）。

    返回:
        (是否成功, 说明)。
    """
    with DatabaseManager.session() as session:
        f = q.get_file_by_id(session, file_id)
        if not f:
            return False, f"文件不存在: {file_id}"
        path_str = f.path
        filename = f.filename
        unit_id = f.resource_unit_id
        unit = q.get_unit_by_id(session, unit_id) if unit_id else None
        unit_path = Path(unit.path) if unit else None

    # 先把磁盘文件安置好，再删数据库记录：反过来做的话，移回收站失败时
    # 记录已经没了，文件变成"库外的孤儿"，用户再也看不到它。
    ok, reason = apply_mode(path_str, mode)
    if not ok:
        return False, reason

    with DatabaseManager.session() as session:
        q.delete_media_file(session, file_id)

    _purge_thumbnail_cache(file_id, unit_path, cfg)
    _refresh_unit_stats(unit_id)
    return True, f"已删除: {filename}（{reason}）"


@router.post("/batch-delete", response_model=BatchDeleteResponse)
def batch_delete_files(body: BatchDeleteRequest, request: Request):
    """批量删除媒体文件。

    部分失败不影响其余条目，逐条返回失败原因（HTTP 200）。
    """
    cfg = getattr(request.app.state, "config", None)
    deleted: list[int] = []
    failed: list[BatchDeleteFailure] = []
    # 去重但保持顺序：重复 ID 会让第二次删除报"文件不存在"，
    # 把一个成功操作记成失败。
    for file_id in dict.fromkeys(body.file_ids):
        try:
            ok, reason = _delete_one(file_id, body.mode, cfg)
        except Exception as e:
            logger.error(f"删除文件失败 file_id={file_id}: {e}", exc_info=True)
            ok, reason = False, "内部错误，详见服务端日志"
        if ok:
            deleted.append(file_id)
        else:
            failed.append(BatchDeleteFailure(file_id=file_id, reason=reason))

    return BatchDeleteResponse(
        success=not failed,
        deleted=deleted,
        failed=failed,
        message=f"已删除 {len(deleted)} 个文件"
                + (f"，{len(failed)} 个失败" if failed else ""),
    )


@router.delete("/{file_id}", response_model=StatusResponse)
def delete_file(
    file_id: int,
    request: Request,
    mode: str = Query(MODE_TRASH, pattern=MODE_PATTERN,
                      description="trash=移至回收站（默认）；record=仅移除媒体库记录"),
):
    """删除指定文件。

    默认与桌面端一致：**移至回收站**并删除数据库记录（同时清理缩略图缓存）。
    mode=record 时只删数据库记录，磁盘文件保留。
    """
    cfg = getattr(request.app.state, "config", None)
    try:
        ok, message = _delete_one(file_id, mode, cfg)
        if not ok:
            # "不存在"用 404 表达，磁盘删除失败用 409 —— 客户端可区分重试语义
            status = 404 if message.startswith("文件不存在") else 409
            raise HTTPException(status_code=status, detail=message)
        return StatusResponse(success=True, message=message)
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

            cfg = getattr(request.app.state, "config", None)
            src = Path(f.path) if f.path else None
            max_size = (cfg.thumbnail_max_size if cfg and cfg.thumbnail_max_size
                        else _DEFAULT_THUMB_SIZE)
            gen = _get_thumb_gen(max_size)

            # 缓存文件名只带 file_id，而 file_id 会被复用（重置数据库后从 1 重新
            # 分配；删除最大 id 后下一条也拿到同一 id）。"文件存在"不等于"内容
            # 属于这个文件" —— 必须用 .meta（源路径+mtime+size）校验归属，
            # 否则新文件会显示上一个文件的图（实测：重置后重新扫描显示旧库照片）。
            if cfg and cfg.thumbnail_cache_dir:
                hit = gen.valid_cache_path(file_id, Path(cfg.thumbnail_cache_dir), src)
                if hit:
                    return FileResponse(str(hit), media_type="image/jpeg", headers=_THUMB_HEADERS)

            unit = q.get_unit_by_id(session, f.resource_unit_id)
            if unit:
                hit = gen.valid_cache_path(
                    file_id, Path(unit.path) / ".thumbnails", src)
                if hit:
                    return FileResponse(str(hit), media_type="image/jpeg", headers=_THUMB_HEADERS)

            # 缓存缺失或不属于本文件：按需生成（会覆盖掉失效的残留缓存）
            if unit and src and src.is_file():
                cache_dir = (cfg.thumbnail_cache_dir if cfg and cfg.thumbnail_cache_dir
                             else Path(unit.path) / ".thumbnails")
                try:
                    info = gen.generate(src, cache_dir, file_id=file_id)
                    if info.thumbnail_path.exists():
                        return FileResponse(str(info.thumbnail_path), media_type="image/jpeg",
                                            headers=_THUMB_HEADERS)
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
