# -*- coding: utf-8 -*-
"""媒体库根目录操作 —— GUI 与 HTTP API 共用的唯一实现。

目前只包含「移除媒体库根目录」：桌面右键菜单与 Web 端 `DELETE /api/library/roots/{id}`
都必须走这里，避免两边各写一套"先删库还是先清缓存"的顺序，出现一边留孤儿缓存、
一边漏删记录的漂移。
"""

from __future__ import annotations

import logging

from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services.cleanup_service import CleanupService

logger = logging.getLogger(__name__)


class LibraryError(Exception):
    """媒体库操作失败（status_code 供 API 转成 HTTP 状态码）。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def remove_root_records(root_id: int, *, config=None) -> str:
    """删除媒体库根的库内记录（级联删单元/文件）并清理其缩略图缓存。

    **不动磁盘文件。** 调用方负责并发互斥（API 用 dedup gate，GUI 用
    `is_dedup_busy()` 预检），本函数不再重复加锁。

    返回被移除根的磁盘路径（供调用方展示/日志）。
    """
    with DatabaseManager.session() as session:
        root = q.get_root_by_id(session, root_id)
        if root is None:
            raise LibraryError(404, f"媒体库不存在: {root_id}")
        path = root.path

    # 必须先清缓存再删根：remove_root_thumbnails 靠 root_id 反查单元，
    # 根被级联删除后就查不到单元，缓存会变成永远没人认领的孤儿文件。
    try:
        CleanupService.remove_root_thumbnails(root_id)
    except Exception as exc:  # noqa: BLE001 — 缓存清理失败不该阻止删库
        logger.warning(f"移除根前清理缩略图失败 root_id={root_id}: {exc}")

    with DatabaseManager.session() as session:
        q.remove_library_root(session, root_id)
    return path
