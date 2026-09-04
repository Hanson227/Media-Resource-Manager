# -*- coding: utf-8 -*-
"""
扫描工作线程 —— 后台执行文件夹扫描并将结果写入数据库。

使用 QThread 避免阻塞 GUI 线程。
发出进度信号供状态栏和进度条更新。
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot

from config import AppConfig
from app.core.scanner import MediaScanner, ScanResult
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.db.models import MediaFile

logger = logging.getLogger(__name__)


class ScanWorker(QThread):
    """后台扫描线程。

    信号:
        progress: 扫描进度 (当前文件夹, 总数)。
        file_found: 发现文件时发出 (file_path)。
        unit_found: 发现资源单元时发出 (unit_name, file_count)。
        finished: 扫描完成 (ScanResult)。
        error_occurred: 发生错误 (错误消息)。
    """

    progress = Signal(int, int)
    file_found = Signal(str)
    unit_found = Signal(str, int)
    finished = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, config: AppConfig, root_path: Path,
                 parent: Optional[QThread] = None) -> None:
        """初始化扫描线程。

        参数:
            config: 应用配置。
            root_path: 要扫描的媒体库根目录。
        """
        super().__init__(parent)
        self._config = config
        self._root_path = root_path
        self._cancelled = False

    def run(self) -> None:
        """执行扫描任务（在后台线程中运行）。"""
        try:
            # 创建扫描器
            scanner = MediaScanner(
                extensions=self._config.media_extensions,
                exclude_patterns=self._config.exclude_patterns,
                progress_callback=lambda cur, total: self.progress.emit(cur, total),
            )

            # 执行扫描
            result = scanner.scan_root(self._root_path)

            if self._cancelled:
                return

            # 写入数据库
            self._save_to_database(result)

            self.finished.emit(result)
            logger.info(f"扫描完成: {self._root_path}")

        except Exception as e:
            logger.error(f"扫描失败: {e}")
            self.error_occurred.emit(str(e))

    @Slot()
    def cancel(self) -> None:
        """取消扫描。"""
        self._cancelled = True
        logger.info("扫描已取消")

    def _save_to_database(self, result: ScanResult) -> None:
        """将扫描结果写入数据库。

        参数:
            result: 扫描结果。
        """
        try:
            with DatabaseManager.session() as session:
                # 创建/更新媒体库根目录
                root = q.get_root_by_path(session, str(result.root_path))
                if not root:
                    root = q.add_library_root(session, str(result.root_path))
                    logger.info(f"新增媒体库根目录: {result.root_path}")

                # 创建扫描会话
                scan_session = q.create_scan_session(session)
                scan_id = scan_session.id

                new_files = 0
                updated_files = 0
                errors: list[str] = list(result.errors)

                # ---- 批量预载：一次 IN 查询命中全部扫描路径的既有记录 ----
                # 取代原先“每个文件一次 get_file_by_path”的 N+1 往返。
                scanned_paths = [str(df.path) for unit in result.units for df in unit.files]
                existing_map: dict[str, MediaFile] = {}
                if scanned_paths:
                    for row in session.query(MediaFile).filter(MediaFile.path.in_(scanned_paths)):
                        existing_map[row.path] = row

                # 变更队列（海量文件时批处理，显著减少 SQLite 往返）
                rows_to_add: list[MediaFile] = []
                move_updates: list[tuple[int, int]] = []   # (file_id, target_unit_id)
                heal_ids: list[int] = []                   # 清空失败占位，待重索引

                # 处理每个资源单元
                for unit in result.units:
                    # 检查已存在的单元
                    existing_unit = q.get_unit_by_path(session, str(unit.path))

                    # 单元内容的真实最新修改时间（文件夹被移动后自身
                    # mtime/ctime 失真，必须用内部文件的 mtime 聚合值）
                    content_dt = (
                        datetime.fromtimestamp(unit.latest_mtime)
                        if unit.latest_mtime is not None else None
                    )

                    if existing_unit:
                        unit_id = existing_unit.id
                        q.update_unit_stats(
                            session, unit_id,
                            unit.file_count, unit.total_size,
                            content_modified_at=content_dt,
                        )
                    else:
                        new_unit = q.create_unit(
                            session,
                            path=str(unit.path),
                            name=unit.name,
                            library_root_id=root.id,
                            is_manual=False,
                            file_count=unit.file_count,
                            total_size=unit.total_size,
                            content_modified_at=content_dt,
                        )
                        unit_id = new_unit.id

                    # 处理每个文件（仅在内存中比对，最后统一落库）
                    for df in unit.files:
                        existing_file = existing_map.get(str(df.path))
                        if existing_file is not None:
                            # 更新已有文件的归属
                            if existing_file.resource_unit_id != unit_id:
                                move_updates.append((existing_file.id, unit_id))
                            # 自愈：md5 占位为空串说明历史索引失败/文件曾缺失；
                            # 文件现已恢复，重置哈希为 NULL 让其重新进入待索引队列。
                            if existing_file.md5_hash == "":
                                heal_ids.append(existing_file.id)
                            updated_files += 1
                        else:
                            rows_to_add.append(MediaFile(
                                path=str(df.path),
                                filename=df.filename,
                                extension=df.extension,
                                media_type=df.media_type,
                                size_bytes=df.size_bytes,
                                resource_unit_id=unit_id,
                            ))
                            new_files += 1
                            self.file_found.emit(df.filename)

                    self.unit_found.emit(unit.name, unit.file_count)

                # ---- 批量落库 ----
                if rows_to_add:
                    session.add_all(rows_to_add)
                    session.flush()
                for file_id, target_unit in move_updates:
                    q.update_file_unit(session, file_id, target_unit)
                for file_id in heal_ids:
                    q.clear_stale_hash_placeholders(session, file_id)

                # 清理：标记当前根下路径已不存在的单元为 excluded
                for stale_unit in q.get_units_by_root(session, root.id):
                    if stale_unit.status == "active" and not Path(stale_unit.path).is_dir():
                        logger.info(f"单元路径已不存在，标记排除: {stale_unit.name} ({stale_unit.path})")
                        q.mark_unit_excluded(session, stale_unit.id)

                # 清理：删除所有活跃单元中已不存在的文件记录及对应缩略图缓存
                stale_file_count = 0
                stale_ids: list[int] = []
                affected_units: set[int] = set()
                for unit in q.get_units_by_root(session, root.id):
                    if unit.status != "active":
                        continue
                    if not Path(unit.path).is_dir():
                        continue
                    for mf in q.get_files_by_unit(session, unit.id):
                        if not Path(mf.path).is_file():
                            # 删除缩略图缓存
                            thumb_dir = Path(unit.path) / ".thumbnails"
                            for suffix in ("", ".meta"):
                                thumb = thumb_dir / f"{mf.id}_thumb.jpg{suffix}"
                                thumb.unlink(missing_ok=True)
                            stale_ids.append(mf.id)
                            stale_file_count += 1
                            affected_units.add(unit.id)
                if stale_ids:
                    # 批量删除（SQLite 外键 ON DELETE CASCADE 清理人脸/帧/匹配记录）
                    session.query(MediaFile).filter(
                        MediaFile.id.in_(stale_ids)
                    ).delete(synchronize_session=False)
                if stale_file_count:
                    logger.info(f"清理了 {stale_file_count} 个已不存在的文件记录及缩略图")
                    # 更新受影响单元的 file_count / total_size / 内容日期
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
                            content_modified_at=(
                                datetime.fromtimestamp(latest)
                                if latest is not None else None
                            ),
                        )

                # 更新扫描会话
                q.update_scan_session(session, scan_id,
                    files_scanned=result.total_files,
                    new_files=new_files,
                    updated_files=updated_files,
                    errors_count=len(errors),
                )
                q.finish_scan_session(session, scan_id, status="completed", errors=errors)

                logger.info(
                    f"数据库写入完成: 新增 {new_files} 个文件, "
                    f"更新 {updated_files} 个文件"
                )

        except Exception as e:
            logger.error(f"数据库写入失败: {e}")
            self.error_occurred.emit(f"数据库写入失败: {e}")
