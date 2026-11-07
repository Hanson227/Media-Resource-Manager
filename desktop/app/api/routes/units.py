# -*- coding: utf-8 -*-
"""
单元路由 —— /api/units 真实数据库查询端点。
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import func

from app.api.schemas import (
    StatusResponse, UnitCoverRequest, UnitItem, UnitListResponse,
    UnitMarkRequest, UnitMergeRequest, UnitRenameRequest,
)
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.db.models import MediaFile
from app.services import unit_service
from app.services.file_ops import MODE_PATTERN, MODE_TRASH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/units", tags=["资源单元"])


@router.get("", response_model=UnitListResponse)
def list_units():
    """列出所有活跃状态的资源单元（封面与根名批量查询，避免 N+1）。"""
    try:
        with DatabaseManager.session() as session:
            units = q.get_all_active_units(session)
            unit_ids = [u.id for u in units]

            # 每个单元的首文件 ID（单次聚合查询）
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

            # 手动封面（cover_path）→ file_id：按「单元 + 路径」精确匹配，
            # 不能只看 path（不同单元可能有同名字段），否则会串封面。
            cover_file_map: dict[int, int] = {}
            covers = [u for u in units if u.cover_path]
            if covers:
                cover_unit_ids = [u.id for u in covers]
                cover_paths = [u.cover_path for u in covers]
                cover_by_unit = {u.id: u.cover_path for u in covers}
                rows = session.query(
                    MediaFile.resource_unit_id, MediaFile.id, MediaFile.path,
                ).filter(
                    MediaFile.resource_unit_id.in_(cover_unit_ids),
                    MediaFile.path.in_(cover_paths),
                ).all()
                for uid, fid, path in rows:
                    if cover_by_unit.get(uid) == path and uid not in cover_file_map:
                        cover_file_map[uid] = fid

            # 根目录名（单次查询）
            root_map = {r.id: r for r in q.get_all_roots(session)}

            items = [
                UnitItem(
                    id=u.id, name=u.name, path=u.path,
                    file_count=u.file_count, total_size=u.total_size,
                    is_manual=u.is_manual, is_starred=u.is_starred,
                    status=u.status,
                    library_root_id=u.library_root_id,
                    library_root_name=(
                        Path(root_map[u.library_root_id].path).name
                        if u.library_root_id in root_map else None
                    ),
                    cover_file_id=cover_file_map.get(u.id) or first_map.get(u.id),
                    created_at=u.created_at.isoformat() if u.created_at else None,
                    content_modified_at=(
                        u.content_modified_at.isoformat()
                        if u.content_modified_at else None
                    ),
                )
                for u in units
            ]
            return UnitListResponse(units=items)
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/merge")
def merge_units(payload: UnitMergeRequest, request: Request):
    """把一个或多个子单元并入父单元（Web 端多选合并）。"""
    cfg = getattr(request.app.state, "config", None)
    result = unit_service.merge_units(payload.parent_id, payload.child_ids, config=cfg)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {
        "success": True,
        "message": result.message,
        "unit_ids": list(result.unit_ids),
    }


@router.post("/mark")
def mark_manual_unit(payload: UnitMarkRequest):
    """把文件夹标记为资源单元（手动单元）。"""
    result = unit_service.mark_manual_unit(payload.folder_path)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {
        "success": True,
        "message": result.message,
        "unit_ids": list(result.unit_ids),
    }


@router.get("/excluded")
def list_excluded_units():
    """已排除/失效单元列表（Web「已排除文件夹」管理用）。"""
    with DatabaseManager.session() as session:
        units = q.get_excluded_units(session)
        return {
            "units": [
                {
                    "id": u.id,
                    "name": u.name,
                    "path": u.path,
                    "file_count": u.file_count,
                    "total_size": u.total_size,
                    "status": u.status,
                    "exclude_reason": u.exclude_reason,
                    "library_root_id": u.library_root_id,
                }
                for u in units
            ]
        }


@router.get("/{unit_id}", response_model=UnitItem)
def get_unit(unit_id: int):
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
                created_at=u.created_at.isoformat() if u.created_at else None,
                content_modified_at=(
                    u.content_modified_at.isoformat()
                    if u.content_modified_at else None
                ),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.get("/{unit_id}/files")
def get_unit_files(unit_id: int):
    """获取指定单元的所有文件列表（简要，供移动端整单元浏览）。"""
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
                    for f in files
                ],
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/{unit_id}/split")
def split_unit(unit_id: int):
    """把一个多文件夹单元拆回按目录自动判定的子单元。"""
    result = unit_service.split_unit(unit_id)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {
        "success": True,
        "message": result.message,
        "unit_ids": list(result.unit_ids),
    }


@router.post("/{unit_id}/rename")
def rename_unit(unit_id: int, payload: UnitRenameRequest):
    """修改单元显示名（只改库，不动磁盘目录）。"""
    result = unit_service.rename_unit(unit_id, payload.name)
    if not result.ok:
        status = 404 if "找不到资源单元" in result.message else 409
        raise HTTPException(status_code=status, detail=result.message)
    return {"success": True, "message": result.message}


@router.post("/{unit_id}/cover")
def set_unit_cover(unit_id: int, payload: UnitCoverRequest):
    """设置手动封面：优先用单元内 file_id，也可传绝对路径。"""
    path = (payload.path or "").strip()
    if payload.file_id is not None:
        with DatabaseManager.session() as session:
            row = session.query(MediaFile).filter(
                MediaFile.id == payload.file_id,
                MediaFile.resource_unit_id == unit_id,
            ).first()
            if row is None:
                raise HTTPException(status_code=404, detail="该文件不存在或不属于此单元")
            path = row.path
    if not path:
        raise HTTPException(status_code=422, detail="必须提供 file_id 或 path")
    result = unit_service.set_cover(unit_id, path)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {"success": True, "message": result.message}


@router.delete("/{unit_id}/cover")
def clear_unit_cover(unit_id: int):
    """清除手动封面，恢复自动取第一张。"""
    result = unit_service.clear_cover(unit_id)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {"success": True, "message": result.message}


@router.post("/{unit_id}/exclude")
def exclude_unit(unit_id: int):
    """把单元移出媒体库（保留磁盘文件与记录，状态置 excluded）。"""
    result = unit_service.exclude_unit(unit_id)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {"success": True, "message": result.message}


@router.post("/{unit_id}/restore")
def restore_excluded_unit(unit_id: int):
    """恢复一个被排除的单元。"""
    result = unit_service.restore_excluded_unit(unit_id)
    if not result.ok:
        raise HTTPException(status_code=409, detail=result.message)
    return {"success": True, "message": result.message}


@router.post("/{unit_id}/star")
def star_unit(unit_id: int):
    """收藏资源单元。"""
    try:
        with DatabaseManager.session() as session:
            u = q.get_unit_by_id(session, unit_id)
            if not u:
                raise HTTPException(status_code=404, detail=f"单元不存在: {unit_id}")
            q.set_unit_starred(session, unit_id, True)
        return {"success": True, "starred": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/{unit_id}/unstar")
def unstar_unit(unit_id: int):
    """取消收藏资源单元。"""
    try:
        with DatabaseManager.session() as session:
            u = q.get_unit_by_id(session, unit_id)
            if not u:
                raise HTTPException(status_code=404, detail=f"单元不存在: {unit_id}")
            q.set_unit_starred(session, unit_id, False)
        return {"success": True, "starred": False}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.delete("/{unit_id}", response_model=StatusResponse)
def delete_unit(
    unit_id: int,
    request: Request,
    mode: str = Query(MODE_TRASH, pattern=MODE_PATTERN,
                      description="trash=整个文件夹移至回收站（默认）；record=仅移除媒体库记录"),
):
    """删除资源单元（文件夹）。

    统一走 `unit_service.delete_unit`：先判卷（磁盘没连不删库）、再按 mode
    处理磁盘、最后删库并清缩略图。默认与桌面端「删除文件夹」一致：把
    **整个文件夹**移至回收站。
    """
    try:
        cfg = getattr(request.app.state, "config", None)
        result = unit_service.delete_unit(unit_id, mode=mode, config=cfg)
        if not result.ok:
            status = 404 if "找不到资源单元" in result.message else 409
            raise HTTPException(status_code=status, detail=result.message)
        return StatusResponse(success=True, message=result.message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")
