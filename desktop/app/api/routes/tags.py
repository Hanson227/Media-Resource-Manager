# -*- coding: utf-8 -*-
"""
标签路由 —— /api/tags CRUD。
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.api.schemas import StatusResponse
from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tags", tags=["标签"])


@router.get("")
async def list_tags():
    """获取所有标签。"""
    try:
        with DatabaseManager.session() as session:
            tags = q.get_all_tags(session)
            return {"tags": [
                {"id": t.id, "name": t.name, "color": t.color}
                for t in tags
            ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_tag(name: str, color: Optional[str] = None):
    """创建新标签。"""
    try:
        with DatabaseManager.session() as session:
            existing = q.get_tag_by_name(session, name)
            if existing:
                raise HTTPException(status_code=409, detail=f"标签已存在: {name}")
            tag = q.create_tag(session, name, color)
            return {"id": tag.id, "name": tag.name, "color": tag.color}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{tag_id}")
async def delete_tag(tag_id: int):
    """删除标签。"""
    try:
        with DatabaseManager.session() as session:
            ok = q.delete_tag(session, tag_id)
            if not ok:
                raise HTTPException(status_code=404, detail=f"标签不存在: {tag_id}")
            return StatusResponse(success=True, message="标签已删除")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/by-file/{file_id}")
async def get_file_tags(file_id: int):
    """获取指定文件的所有标签。"""
    try:
        with DatabaseManager.session() as session:
            tags = q.get_file_tags(session, file_id)
            return {"file_id": file_id, "tags": [
                {"id": t.id, "name": t.name, "color": t.color}
                for t in tags
            ]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/by-file/{file_id}")
async def set_file_tags(file_id: int, tag_ids: list[int]):
    """设置文件的标签（全量替换）。"""
    try:
        with DatabaseManager.session() as session:
            q.set_file_tags(session, file_id, tag_ids)
            return StatusResponse(success=True, message="标签已更新")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mapped-files")
async def get_mapped_files(tag_ids: Optional[str] = Query(None, description="逗号分隔的标签ID列表")):
    """批量查询文件标签映射。可选按标签筛选。"""
    try:
        tids = None
        if tag_ids:
            try:
                tids = [int(x) for x in tag_ids.split(",")]
            except ValueError:
                raise HTTPException(status_code=422, detail="标签ID参数格式无效，请使用逗号分隔的数字列表")

        with DatabaseManager.session() as session:
            result = q.get_all_mapped_files(session, tids)
            return {"mappings": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
