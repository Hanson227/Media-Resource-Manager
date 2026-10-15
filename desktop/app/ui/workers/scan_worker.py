# -*- coding: utf-8 -*-
"""
扫描工作线程 —— 后台执行文件夹扫描并将结果写入数据库。

落库逻辑已收敛到 `app/services/scan_service.py`（无 Qt 实现，GUI 与 HTTP API
共用一份），本线程只负责：驱动扫描器、把进度转发成 Qt 信号、处理取消。

信号:
    progress: 扫描进度 (当前文件夹, 总数)。
    file_found: 发现文件时发出 (file_path)。
    unit_found: 发现资源单元时发出 (unit_name, file_count)。
    finished: 扫描完成 (ScanResult)。
    error_occurred: 发生错误 (错误消息)。
    cancelled: 扫描被取消。
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot

from config import AppConfig
from app.core.scanner import MediaScanner, ScanResult
from app.services.scan_service import (
    PATH_LOOKUP_CHUNK_SIZE, RootRemapRequired, load_existing_files,
    save_scan_result,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ScanWorker", "load_existing_files", "PATH_LOOKUP_CHUNK_SIZE",
    "RootRemapRequired",
]


class ScanWorker(QThread):
    """后台扫描线程（信号转发层，落库走 scan_service）。"""

    progress = Signal(int, int)
    file_found = Signal(str)
    unit_found = Signal(str, int)
    finished = Signal(object)
    error_occurred = Signal(str)
    cancelled = Signal()

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
            scanner = MediaScanner(
                extensions=self._config.media_extensions,
                exclude_patterns=self._config.exclude_patterns,
                progress_callback=lambda cur, total: self.progress.emit(cur, total),
            )

            result = scanner.scan_root(self._root_path)

            if self._cancelled:
                # 必须显式通知：取消路径不 emit finished，界面无法据此收起
                # 进度条与取消按钮（也不会误触发队列里的下一次扫描）。
                self.cancelled.emit()
                logger.info(f"扫描已取消: {self._root_path}")
                return

            self._save_to_database(result)

            self.finished.emit(result)
            logger.info(f"扫描完成: {self._root_path}")

        except RootRemapRequired as e:
            # 换盘符后直接扫描会多出一套同名根+单元（旧记录永远清不掉），
            # 因此由守卫拦下并把候选位置交给「工具 → 修复媒体库路径」。
            msg = (f"{e}\n\n可用工具菜单「修复媒体库路径」先改库，"
                   f"或确认按新库扫描。\n候选位置: "
                   + "、".join(c.get("path", "") for c in e.candidates[:3]))
            logger.warning(f"扫描被路径守卫拦下: {e}")
            self.error_occurred.emit(msg)

        except Exception as e:
            logger.error(f"扫描失败: {e}")
            self.error_occurred.emit(str(e))

    @Slot()
    def cancel(self) -> None:
        """取消扫描。"""
        self._cancelled = True
        logger.info("扫描已取消")

    def _save_to_database(self, result: ScanResult) -> None:
        """将扫描结果写入数据库（委托 scan_service，保持与 API 侧同一实现）。"""
        try:
            save_scan_result(
                result,
                thumbnail_cache_dir=self._config.thumbnail_cache_dir,
                file_callback=self.file_found.emit,
                unit_callback=self.unit_found.emit,
            )
        except RootRemapRequired:
            raise
        except Exception as e:
            logger.error(f"数据库写入失败: {e}")
            self.error_occurred.emit(f"数据库写入失败: {e}")
