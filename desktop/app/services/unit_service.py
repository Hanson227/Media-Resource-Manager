# -*- coding: utf-8 -*-
"""资源单元操作服务 —— GUI 与 HTTP API 的**唯一实现**。

为什么要有这一层：桌面端左栏右键菜单（合并/拆分/标记/排除/封面）与 Web 端
（单元卡片菜单、批量合并）必须做同一件事。历史上这类逻辑直接写在
`main_window` 的槽函数里，Web 想用就只能照抄一遍 —— 两份实现必然漂移
（"产生与清理必须同源"的同类问题）。
因此规则是：**单元级写操作只在这里实现**，GUI 槽函数与 API 路由都调用它。

返回：
    统一的 `UnitActionResult`（frozen dataclass）：ok / message / 受影响单元 id 列表。
    失败不抛异常（GUI 槽函数抛出等于"点了没反应"，API 侧还要转 500），
    调用方只看 ok 与 message。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

from app.db.engine import DatabaseManager
from app.db import queries as q
from app.utils.constants import ExcludeReason
from app.utils.media_types import is_media_file

logger = logging.getLogger(__name__)

__all__ = [
    "UnitActionResult", "merge_units", "split_unit", "mark_manual_unit",
    "unmark_manual_unit", "exclude_unit", "restore_excluded_unit",
    "set_cover", "clear_cover",
]


@dataclass(frozen=True)
class UnitActionResult:
    """单元操作结果（不可变，GUI 与 API 共用）。"""

    ok: bool
    message: str = ""
    unit_ids: tuple = ()
    changed: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message,
                "unit_ids": list(self.unit_ids), "changed": dict(self.changed)}


def _fail(message: str) -> UnitActionResult:
    logger.warning(f"单元操作失败: {message}")
    return UnitActionResult(ok=False, message=message)


def merge_units(parent_id: int, child_ids: Sequence[int],
                *, config=None) -> UnitActionResult:
    """把若干子单元并入父单元（只改库：子单元标 merged，父单元重算统计）。

    内容日期取父/子中的最新值 —— 否则合并后排序会退回到父单元自己的旧日期。
    """
    children = [int(c) for c in child_ids if int(c) != int(parent_id)]
    if not parent_id or not children:
        return _fail("合并需要 1 个父单元与至少 1 个子单元")
    try:
        with DatabaseManager.session() as session:
            parent = q.get_unit_by_id(session, parent_id)
            if parent is None:
                return _fail(f"找不到父单元 (ID={parent_id})")
            latest = parent.content_modified_at
            child_units = q.get_units_by_ids(session, children) or []
            for cu in child_units:
                if cu.content_modified_at is not None and (
                        latest is None or cu.content_modified_at > latest):
                    latest = cu.content_modified_at
            merged = []
            for cid in children:
                q.mark_unit_merged(session, cid, parent_id)
                merged.append(cid)
            total_files = 0
            total_size = 0
            for uid in [parent_id] + children:
                files = q.get_files_by_unit(session, uid)
                total_files += len(files)
                total_size += sum(f.size_bytes for f in files)
            q.update_unit_stats(session, parent_id, total_files, total_size,
                                content_modified_at=latest)
        return UnitActionResult(ok=True, unit_ids=(parent_id, *children),
                                changed={"merged": len(children),
                                         "file_count": total_files,
                                         "total_size": total_size},
                                message=f"已合并 {len(children)} 个单元")
    except Exception as e:  # noqa: BLE001 — 见模块 docstring
        logger.error(f"合并单元异常: {e}", exc_info=True)
        return _fail(f"合并失败: {e}")


def split_unit(unit_id: int, *, config=None) -> UnitActionResult:
    """拆分子元（把该单元及其子单元的 merged 标记清掉）。"""
    if not unit_id:
        return _fail("无效的单元 ID")
    try:
        with DatabaseManager.session() as session:
            children = q.get_child_units(session, unit_id) or []
            for child in children:
                q.unmerge_unit(session, child.id)
            q.unmerge_unit(session, unit_id)
        return UnitActionResult(
            ok=True, unit_ids=(unit_id, *[c.id for c in children]),
            changed={"children": len(children)},
            message=f"已拆分（释放 {len(children)} 个子单元）" if children else "已拆分")
    except Exception as e:  # noqa: BLE001
        logger.error(f"拆分单元异常: {e}", exc_info=True)
        return _fail(f"拆分失败: {e}")


def mark_manual_unit(folder_path: str, *, config=None) -> UnitActionResult:
    """把某个文件夹标记为资源单元（不存在则新建手动单元）。

    手动标记通常意味着"我想留着它"，因此同时加星标（与桌面端行为一致）。
    """
    path_text = str(folder_path or "").strip()
    if not path_text:
        return _fail("路径为空")
    folder = Path(path_text)
    if not folder.is_dir():
        return _fail(f"目录不存在: {folder}")
    try:
        with DatabaseManager.session() as session:
            unit = q.get_unit_by_path(session, path_text)
            if unit is not None:
                q.mark_unit_manual(session, unit.id, True)
                q.set_unit_starred(session, unit.id, True)
                return UnitActionResult(ok=True, unit_ids=(unit.id,),
                                        message=f"已标记: {unit.name}")
            roots = q.get_all_roots(session) or []
            root_id = roots[0].id if roots else None
            if not root_id:
                return _fail("还没有媒体库根目录，请先添加媒体库")
            # 采集目录内媒体文件的真实最新修改时间（排序要用）
            extensions = getattr(config, "media_extensions", None)
            latest: Optional[float] = None
            for entry in folder.rglob("*"):
                try:
                    if not entry.is_file():
                        continue
                    if extensions and not is_media_file(entry, extensions):
                        continue
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                latest = mtime if latest is None else max(latest, mtime)
            content_dt = datetime.fromtimestamp(latest) if latest is not None else None
            created = q.create_unit(session, path_text, folder.name, root_id,
                                    is_manual=True, file_count=0, total_size=0,
                                    content_modified_at=content_dt)
            unit_id = getattr(created, "id", None)
            if unit_id:
                q.set_unit_starred(session, unit_id, True)
        return UnitActionResult(ok=True,
                                unit_ids=(unit_id,) if unit_id else (),
                                message=f"已标记: {folder.name}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"标记单元异常: {e}", exc_info=True)
        return _fail(f"标记失败: {e}")


def unmark_manual_unit(unit_id: int, *, config=None) -> UnitActionResult:
    """取消手动标记（同时取消星标）。"""
    if not unit_id:
        return _fail("无效的单元 ID")
    try:
        with DatabaseManager.session() as session:
            q.mark_unit_manual(session, unit_id, False)
            q.set_unit_starred(session, unit_id, False)
        return UnitActionResult(ok=True, unit_ids=(unit_id,), message="已取消标记")
    except Exception as e:  # noqa: BLE001
        logger.error(f"取消标记异常: {e}", exc_info=True)
        return _fail(f"取消标记失败: {e}")


def exclude_unit(unit_id: int, *, config=None) -> UnitActionResult:
    """把单元标为已排除（**起因记成 user**）。

    起因必须是 user：扫描自动排除（missing/empty）才会被下一次扫描自动恢复，
    用户的选择不能被扫描抹掉（见 constants.ExcludeReason）。
    """
    if not unit_id:
        return _fail("无效的单元 ID")
    try:
        with DatabaseManager.session() as session:
            unit = q.get_unit_by_id(session, unit_id)
            if unit is None:
                return _fail(f"找不到资源单元 (ID={unit_id})")
            name = unit.name
            q.mark_unit_excluded(session, unit_id, reason=ExcludeReason.USER.value)
        return UnitActionResult(ok=True, unit_ids=(unit_id,),
                                message=f"已排除: {name}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"排除单元异常: {e}", exc_info=True)
        return _fail(f"排除失败: {e}")


def restore_excluded_unit(unit_id: int, *, config=None) -> UnitActionResult:
    """取消排除（恢复为 active）。"""
    if not unit_id:
        return _fail("无效的单元 ID")
    try:
        with DatabaseManager.session() as session:
            unit = q.get_unit_by_id(session, unit_id)
            if unit is None:
                return _fail(f"找不到资源单元 (ID={unit_id})")
            q.unexclude_unit(session, unit_id)
        return UnitActionResult(ok=True, unit_ids=(unit_id,), message="已恢复")
    except Exception as e:  # noqa: BLE001
        logger.error(f"恢复单元异常: {e}", exc_info=True)
        return _fail(f"恢复失败: {e}")


def set_cover(unit_id: int, cover_path: str, *, config=None) -> UnitActionResult:
    """设置单元封面（并让旧封面的缩略图缓存失效）。"""
    path_text = str(cover_path or "").strip()
    if not unit_id or not path_text:
        return _fail("封面路径为空")
    try:
        with DatabaseManager.session() as session:
            old = q.get_unit_by_id(session, unit_id)
            old_cover = old.cover_path if old else None
            q.set_unit_cover(session, unit_id, path_text)
        if old_cover and old_cover != path_text:
            from app.db.models import MediaFile
            from app.services.cleanup_service import CleanupService
            with DatabaseManager.session() as session:
                old_file = session.query(MediaFile).filter(
                    MediaFile.path == old_cover,
                    MediaFile.resource_unit_id == unit_id,
                ).first()
                if old_file:
                    CleanupService.invalidate_thumbnail(old_file.id)
        return UnitActionResult(ok=True, unit_ids=(unit_id,), message="封面已设置")
    except Exception as e:  # noqa: BLE001
        logger.error(f"设置封面异常: {e}", exc_info=True)
        return _fail(f"设置封面失败: {e}")


def clear_cover(unit_id: int, *, config=None) -> UnitActionResult:
    """清除手动封面（回到自动取第一张）。"""
    if not unit_id:
        return _fail("无效的单元 ID")
    try:
        with DatabaseManager.session() as session:
            q.clear_unit_cover(session, unit_id)
        return UnitActionResult(ok=True, unit_ids=(unit_id,), message="封面已清除")
    except Exception as e:  # noqa: BLE001
        logger.error(f"清除封面异常: {e}", exc_info=True)
        return _fail(f"清除封面失败: {e}")
