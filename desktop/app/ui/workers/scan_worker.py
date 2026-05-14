# -*- coding: utf-8 -*-
"""
扫描工作线程 —— 后台执行文件夹扫描并将结果写入数据库。

使用 QThread 避免阻塞 GUI 线程。
发出进度信号供状态栏和进度条更新。
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot

from config import AppConfig
from app.core.scanner import MediaScanner, ScanResult
from app.db.engine import DatabaseManager
from app.db import queries as q

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
                processed_units: dict[int, str] = {}  # unit_id → unit_path

                # 处理每个资源单元
                for unit in result.units:
                    # 检查已存在的单元
                    existing_unit = q.get_unit_by_path(session, str(unit.path))

                    if existing_unit:
                        unit_id = existing_unit.id
                        processed_units[unit_id] = str(unit.path)
                        q.update_unit_stats(
                            session, unit_id,
                            unit.file_count, unit.total_size,
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
                        )
                        unit_id = new_unit.id
                    processed_units[unit_id] = str(unit.path)

                    # 处理每个文件
                    for df in unit.files:
                        existing_file = q.get_file_by_path(session, str(df.path))
                        if existing_file:
                            # 更新已有文件的归属
                            if existing_file.resource_unit_id != unit_id:
                                q.update_file_unit(session, existing_file.id, unit_id)
                            updated_files += 1
                        else:
                            q.insert_media_file(
                                session,
                                path=str(df.path),
                                filename=df.filename,
                                extension=df.extension,
                                media_type=df.media_type,
                                size_bytes=df.size_bytes,
                                resource_unit_id=unit_id,
                            )
                            new_files += 1
                            self.file_found.emit(df.filename)

                    self.unit_found.emit(unit.name, unit.file_count)

                # 清理：标记当前根下路径已不存在的单元为 excluded
                for stale_unit in q.get_units_by_root(session, root.id):
                    if stale_unit.status == "active" and not Path(stale_unit.path).is_dir():
                        logger.info(f"单元路径已不存在，标记排除: {stale_unit.name} ({stale_unit.path})")
                        q.mark_unit_excluded(session, stale_unit.id)

                # 清理：删除活跃单元中已不存在的文件记录及对应缩略图缓存
                stale_file_count = 0
                for uid, upath in processed_units.items():
                    if Path(upath).is_dir():
                        for mf in q.get_files_by_unit(session, uid):
                            if not Path(mf.path).is_file():
                                # 删除缩略图缓存
                                thumb_dir = Path(upath) / ".thumbnails"
                                for suffix in ("", ".meta"):
                                    thumb = thumb_dir / f"{mf.id}_thumb.jpg{suffix}"
                                    thumb.unlink(missing_ok=True)
                                # 删除 DB 记录
                                q.delete_media_file(session, mf.id)
                                stale_file_count += 1
                if stale_file_count:
                    logger.info(f"清理了 {stale_file_count} 个已不存在的文件记录及缩略图")

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
