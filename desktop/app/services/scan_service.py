# -*- coding: utf-8 -*-
"""
扫描编排服务 —— 递归扫描 + 结果落库（不依赖 Qt，GUI 与 HTTP API 共用）。

由来：这段逻辑原先只存在于 `app/ui/workers/scan_worker.py`（QThread）。便携版
没有桌面 GUI，HTTP API 又需要同样的行为，若各写一份必然分叉（与
`dedup_service` / `index_service` 收敛前的问题同一类）。因此把"扫描 + 落库"
抽到这里，`ScanWorker` 只做信号转发。

落库顺序（不可乱，详见 docs/pitfalls.md）：
    恢复自动排除记录 → 批量新增/更新文件 → 删除失效文件记录 → 排除空壳单元
    → 写扫描会话。判据变化必须与清理同源，否则会留下 "0 个 · 0 B" 空壳。
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from app.core.scanner import MediaScanner, ScanResult
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.db.models import MediaFile
from app.services.cleanup_service import CleanupService

logger = logging.getLogger(__name__)

PATH_LOOKUP_CHUNK_SIZE = 500
"""批量预载路径映射时的分块大小（避开 SQLite 绑定变量上限）。"""


class RootRemapRequired(RuntimeError):
    """待扫描路径与某个"不可达的旧根"同尾：必须先决定改库还是当新库扫描。

    不拦的话，换盘符后重新扫描会在库里多出一套"同名不同盘符"的根与单元，
    旧记录永远清不掉（列表里两套同名单元，标签/星标全在旧 id 上）。
    """

    def __init__(self, path: str, old_path: str, root_id: int, candidates: list):
        self.path = path
        self.old_path = old_path
        self.root_id = root_id
        self.candidates = candidates
        super().__init__(
            f"媒体库路径可能已变化: {old_path} → {path}，"
            "请先执行路径修复（改库），或明确按新库扫描")


def load_existing_files(session, paths: list, chunk_size: int = PATH_LOOKUP_CHUNK_SIZE) -> dict:
    """分块查询既有 MediaFile 记录，返回 {path: MediaFile}。

    分块是为了避开 SQLite 的绑定变量上限；语义等价于单条 IN 查询。
    """
    existing: dict = {}
    if not paths:
        return existing
    for start in range(0, len(paths), max(1, chunk_size)):
        chunk = paths[start:start + chunk_size]
        for row in session.query(MediaFile).filter(MediaFile.path.in_(chunk)):
            existing[row.path] = row
    return existing


@dataclass(frozen=True)
class ScanSaveResult:
    """一次落库的统计结果。"""

    root_id: int
    new_files: int = 0
    updated_files: int = 0
    moved_files: int = 0
    healed_files: int = 0
    stale_files: int = 0
    restored_units: tuple = ()
    excluded_units: tuple = ()
    errors: tuple = ()


@dataclass(frozen=True)
class ScanOutcome:
    """一次完整扫描的结果。"""

    result: Optional[ScanResult] = None
    saved: Optional[ScanSaveResult] = None
    cancelled: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return (not self.cancelled) and self.result is not None


def _remove_stale_thumbnail(file_id: int, unit_path: Path, cache_dir) -> None:
    """删除失效文件对应的缩略图缓存（集中缓存 + 旧版单元内缓存）。

    历史遗留：这里原先直接删 `<单元>/.thumbnails/{id}_thumb.jpg`（旧模式路径），
    集中缓存上线后它打在错误的位置，删不到任何东西。改为走 CleanupService，
    缓存目录来自配置（便携版可能在另一块盘上）。
    """
    CleanupService.remove_thumbnail(file_id, cache_dir)
    if unit_path:
        CleanupService.remove_thumbnail(file_id, Path(unit_path) / ".thumbnails")


def save_scan_result(result: ScanResult, *,
                     thumbnail_cache_dir=None,
                     file_callback: Optional[Callable[[str], None]] = None,
                     unit_callback: Optional[Callable[[str, int], None]] = None,
                     enforce_root_guard: bool = True) -> ScanSaveResult:
    """把一次扫描结果写入数据库（唯一入口）。

    参数:
        result: MediaScanner 产出的结果（frozen dataclass）。
        thumbnail_cache_dir: 缩略图缓存目录（删除失效文件缓存时用）。
        file_callback: 每新增一个文件回调（GUI 用来滚动提示）。
        unit_callback: 每个单元回调 (单元名, 文件数)。
        enforce_root_guard: 是否启用"同尾不可达根"守卫（默认启用；
            测试与"明确按新库扫描"的调用方可传 False）。

    异常:
        RootRemapRequired: 存在同尾的不可达旧根（需用户先决定改库）。
    """
    root_path_str = str(result.root_path)
    if enforce_root_guard:
        guard = _check_root_guard(root_path_str, result.root_path)
        if guard is not None:
            raise RootRemapRequired(root_path_str, guard["old_path"],
                                    guard["root_id"], guard["candidates"])

    with DatabaseManager.session() as session:
        root = q.get_root_by_path(session, root_path_str)
        if not root:
            root = q.add_library_root(session, root_path_str)
            logger.info(f"新增媒体库根目录: {result.root_path}")

        scan_session = q.create_scan_session(session)
        scan_id = scan_session.id

        new_files = 0
        updated_files = 0
        errors: list = list(result.errors)
        produced_paths = [str(u.path) for u in result.units]

        # 恢复：本轮重新产出内容的自动排除记录（必须赶在单元统计写入之前判断；
        # 判据是 exclude_reason，不是 file_count —— 见 queries.restore_units_produced_by_scan）
        restored = q.restore_units_produced_by_scan(session, produced_paths)
        if restored:
            logger.info(f"恢复为活跃单元（重新产出内容）: {', '.join(restored)}")

        # 批量预载既有记录（取代逐文件查询的 N+1 往返）
        scanned_paths = [str(df.path) for unit in result.units for df in unit.files]
        existing_map = load_existing_files(session, scanned_paths)

        rows_to_add: list = []
        move_updates: list = []
        heal_ids: list = []

        for unit in result.units:
            existing_unit = q.get_unit_by_path(session, str(unit.path))
            content_dt = (datetime.fromtimestamp(unit.latest_mtime)
                          if unit.latest_mtime is not None else None)

            if existing_unit:
                unit_id = existing_unit.id
                q.update_unit_stats(session, unit_id, unit.file_count,
                                    unit.total_size, content_modified_at=content_dt)
            else:
                new_unit = q.create_unit(
                    session, path=str(unit.path), name=unit.name,
                    library_root_id=root.id, is_manual=False,
                    file_count=unit.file_count, total_size=unit.total_size,
                    content_modified_at=content_dt)
                unit_id = new_unit.id

            for df in unit.files:
                existing_file = existing_map.get(str(df.path))
                if existing_file is not None:
                    if existing_file.resource_unit_id != unit_id:
                        move_updates.append((existing_file.id, unit_id))
                    # 自愈：md5 占位为空串说明历史索引失败/文件曾缺失，
                    # 文件已恢复 → 重置哈希让它重新进入待索引队列。
                    if existing_file.md5_hash == "":
                        heal_ids.append(existing_file.id)
                    updated_files += 1
                else:
                    rows_to_add.append(MediaFile(
                        path=str(df.path), filename=df.filename,
                        extension=df.extension, media_type=df.media_type,
                        size_bytes=df.size_bytes, resource_unit_id=unit_id))
                    new_files += 1
                    if file_callback:
                        file_callback(df.filename)

            if unit_callback:
                unit_callback(unit.name, unit.file_count)

        if rows_to_add:
            session.add_all(rows_to_add)
            session.flush()
        for file_id, target_unit in move_updates:
            q.update_file_unit(session, file_id, target_unit)
        for file_id in heal_ids:
            q.clear_stale_hash_placeholders(session, file_id)

        # 清理：删除活跃单元中已不存在的文件记录及其缩略图缓存
        stale_file_count = 0
        stale_ids: list = []
        affected_units: set = set()
        for unit in q.get_units_by_root(session, root.id):
            if unit.status != "active":
                continue
            unit_path = Path(unit.path)
            if not unit_path.is_dir():
                continue
            for mf in q.get_files_by_unit(session, unit.id):
                if not Path(mf.path).is_file():
                    _remove_stale_thumbnail(mf.id, unit_path, thumbnail_cache_dir)
                    stale_ids.append(mf.id)
                    stale_file_count += 1
                    affected_units.add(unit.id)
        if stale_ids:
            session.query(MediaFile).filter(
                MediaFile.id.in_(stale_ids)
            ).delete(synchronize_session=False)
        if stale_file_count:
            logger.info(f"清理了 {stale_file_count} 个已不存在的文件记录及缩略图")
            for uid in affected_units:
                remaining = q.get_files_by_unit(session, uid)
                new_count = len(remaining)
                new_size = sum(f.size_bytes for f in remaining)
                latest = None
                for f in remaining:
                    try:
                        m = Path(f.path).stat().st_mtime
                    except OSError:
                        continue
                    latest = m if latest is None else max(latest, m)
                q.update_unit_stats(
                    session, uid, new_count, new_size,
                    content_modified_at=(datetime.fromtimestamp(latest)
                                         if latest is not None else None))

        # 排除本根下本轮已无内容的单元（必须在删除失效文件记录之后；
        # 判据不能退回"只看目录是否存在" —— 见 queries.exclude_empty_units）
        excluded_units = q.exclude_empty_units(session, root.id, produced_paths)
        if excluded_units:
            logger.info(f"排除已无内容的单元: {', '.join(excluded_units)}")

        q.update_scan_session(session, scan_id,
                              files_scanned=result.total_files,
                              new_files=new_files,
                              updated_files=updated_files,
                              errors_count=len(errors))
        q.finish_scan_session(session, scan_id, status="completed", errors=errors)

        root_id = root.id

    logger.info(f"数据库写入完成: 新增 {new_files} 个文件, 更新 {updated_files} 个文件")
    return ScanSaveResult(
        root_id=root_id, new_files=new_files, updated_files=updated_files,
        moved_files=len(move_updates), healed_files=len(heal_ids),
        stale_files=stale_file_count, restored_units=tuple(restored),
        excluded_units=tuple(excluded_units), errors=tuple(errors))


def _check_root_guard(path_str: str, root_path: Path) -> Optional[dict]:
    """同尾不可达根的守卫（延迟导入，避免 service 之间循环依赖）。"""
    from app.services.path_remap_service import find_equivalent_unreachable_root

    if _root_record_exists(path_str):
        return None
    return find_equivalent_unreachable_root(root_path)


def _root_record_exists(path_str: str) -> bool:
    """该路径是否已在库里有对应根目录记录。"""
    with DatabaseManager.session() as session:
        return q.get_root_by_path(session, path_str) is not None


def run_scan(config, root_path, *,
             progress_callback: Optional[Callable[[int, int], None]] = None,
             unit_callback: Optional[Callable[[str, int], None]] = None,
             file_callback: Optional[Callable[[str], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None,
             enforce_root_guard: bool = True) -> ScanOutcome:
    """执行一次完整扫描（扫描 + 落库），供 GUI 与 HTTP API 共用。

    参数:
        config: AppConfig（媒体扩展名 / 排除规则 / 缩略图缓存目录）。
        root_path: 媒体库根目录。
        progress_callback: (已完成, 总数) 进度回调。
        unit_callback: 每个单元回调 (单元名, 文件数)。
        file_callback: 每个新增文件回调（文件名）。
        cancelled: 取消探测；取消时不落库（避免写入半截结果）。
        enforce_root_guard: 见 save_scan_result。

    返回:
        ScanOutcome（cancelled=True 时 result 为 None）。
    """
    root_path = Path(root_path)
    scanner = MediaScanner(
        extensions=config.media_extensions,
        exclude_patterns=config.exclude_patterns,
        progress_callback=progress_callback,
    )

    result = scanner.scan_root(root_path)
    is_cancelled = cancelled or (lambda: False)
    if is_cancelled():
        logger.info(f"扫描已取消: {root_path}")
        return ScanOutcome(cancelled=True, reason="用户取消")

    saved = save_scan_result(
        result,
        thumbnail_cache_dir=getattr(config, "thumbnail_cache_dir", None),
        file_callback=file_callback,
        unit_callback=unit_callback,
        enforce_root_guard=enforce_root_guard,
    )
    return ScanOutcome(result=result, saved=saved)
