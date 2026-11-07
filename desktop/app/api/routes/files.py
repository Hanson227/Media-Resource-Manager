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
from app.services.file_ops import MODE_PATTERN, MODE_TRASH

logger = logging.getLogger(__name__)

# HEIC/HEIF 浏览器不原生支持：注册 Pillow 插件后，在内存中转为 JPEG 传输。
# 注册实现收敛在 image_helpers（幂等，导入即生效），避免这里漏注册时
# pHash/dHash/缩略图静默解不开 HEIC。
from app.utils.image_helpers import ensure_heif_support, open_image_rgb_oriented
from app.utils.media_types import get_mime_type

_HEIF_OK = ensure_heif_support()

from app.core.thumbnail_generator import ThumbnailGenerator
from app.db.engine import DatabaseManager
from app.db import queries as q

# 全局缩略图生成器（按 max_size + 格式缓存，尺寸/格式来自 config）
_thumb_gens: dict[tuple[int, str], ThumbnailGenerator] = {}
_DEFAULT_THUMB_SIZE = 256


def _get_thumb_gen(max_size: int, fmt: str = "jpg") -> ThumbnailGenerator:
    """按配置尺寸与格式取缩略图生成器（避免硬编码 256/.jpg 忽略用户设置）。"""
    key = (max_size, (fmt or "jpg").lower())
    gen = _thumb_gens.get(key)
    if gen is None:
        gen = ThumbnailGenerator(max_size=max_size, format=key[1])
        _thumb_gens[key] = gen
    return gen

# 缩略图 HTTP 缓存头：封面变更后手机端能较快更新
_THUMB_HEADERS = {"Cache-Control": "private, max-age=86400"}

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


def _delete_one(file_id: int, mode: str, cfg) -> tuple[bool, str]:
    """删除一个媒体文件（按 mode 决定是否真的动磁盘）。

    统一走 `unit_service.delete_file`：GUI 与 API 共用同一份删除语义
    （判卷、三模式、清缩略图、重算统计），避免两处实现漂移。
    """
    from app.services import unit_service
    result = unit_service.delete_file(file_id, mode=mode, config=cfg)
    return result.ok, result.message


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
    """返回缩略图图片二进制（供手机端直接显示）。

    关键：**缩略图生成在 DB 会话之外**。旧实现把 OpenCV 抽帧放在
    `with DatabaseManager.session()` 里，一个大视频抽帧几秒就占着连接；
    网格一次滚出十几个封面时连接池（默认 5）被打满，其它请求排队超时。
    """
    cfg = getattr(request.app.state, "config", None)
    max_size = (cfg.thumbnail_max_size if cfg and cfg.thumbnail_max_size
                else _DEFAULT_THUMB_SIZE)
    fmt = ((cfg.thumbnail_format if cfg and cfg.thumbnail_format else "jpg")
           or "jpg").lower()
    gen = _get_thumb_gen(max_size, fmt)
    media_type = "image/png" if fmt == "png" else "image/jpeg"

    unit_path: Path | None = None
    src: Path | None = None
    try:
        with DatabaseManager.session() as session:
            f = q.get_file_by_id(session, file_id)
            if not f:
                raise HTTPException(status_code=404, detail=f"文件不存在: {file_id}")
            src = Path(f.path) if f.path else None

            # 缓存文件名只带 file_id，而 file_id 会被复用（重置数据库后从 1 重新
            # 分配；删除最大 id 后下一条也拿到同一 id）。"文件存在"不等于"内容
            # 属于这个文件" —— 必须用 .meta（源路径+mtime+size）校验归属。
            if cfg and cfg.thumbnail_cache_dir:
                hit = gen.valid_cache_path(file_id, Path(cfg.thumbnail_cache_dir), src)
                if hit:
                    return FileResponse(str(hit), media_type=media_type,
                                        headers=_THUMB_HEADERS)

            unit = q.get_unit_by_id(session, f.resource_unit_id)
            if unit:
                unit_path = Path(unit.path)
                hit = gen.valid_cache_path(file_id, unit_path / ".thumbnails", src)
                if hit:
                    return FileResponse(str(hit), media_type=media_type,
                                        headers=_THUMB_HEADERS)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")

    # ---- 缓存缺失或不属于本文件：在会话外按需生成（不占 DB 连接）----
    if unit_path and src and src.is_file():
        cache_dir = (cfg.thumbnail_cache_dir if cfg and cfg.thumbnail_cache_dir
                     else unit_path / ".thumbnails")
        try:
            info = gen.generate(src, Path(cache_dir), file_id=file_id)
            if info.thumbnail_path.exists():
                return FileResponse(str(info.thumbnail_path), media_type=media_type,
                                    headers=_THUMB_HEADERS)
        except Exception as gen_e:
            logger.warning(f"缩略图按需生成失败: {src} - {gen_e}")

    raise HTTPException(status_code=404, detail="缩略图未生成")


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

    media_type = get_mime_type(f.extension)
    # HEIC/HEIF 浏览器不原生支持，转为 JPEG 传输。
    # 注意 .heif 必须走同一分支：此前只判 "image/heic"，.heif 被原样返回，
    # 浏览器直接打不开（列表里显示成裂图）。
    if media_type in ("image/heic", "image/heif"):
        if not _HEIF_OK:
            raise HTTPException(status_code=415, detail="HEIC 支持未安装（缺少 pillow_heif）")
        try:
            rgb_img = open_image_rgb_oriented(path)
            if rgb_img is None:
                raise ValueError("无法解码 HEIC/HEIF")
            try:
                buf = io.BytesIO()
                rgb_img.save(buf, "JPEG", quality=90)
                buf.seek(0)
            finally:
                rgb_img.close()
            return Response(content=buf.read(), media_type="image/jpeg",
                            headers={
                                "Content-Disposition": f"inline; filename*=UTF-8''{quote(f.filename + '.jpg')}",
                                "Cache-Control": "private, max-age=3600",
                            })
        except Exception as e:
            logger.error(f"HEIC 转换失败 {f.path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="内部服务器错误")
    # 媒体流必须 inline：Starlette 的 FileResponse 传 filename 默认发
    # `Content-Disposition: attachment`，iOS Safari 等会把它当下载而不是播放。
    return FileResponse(str(path), media_type=media_type,
                        filename=f.filename,
                        content_disposition_type="inline",
                        headers={"Cache-Control": "private, max-age=3600"})
