# -*- coding: utf-8 -*-
"""
哈希工作线程 —— 后台计算未索引文件的哈希值。

真正的管线（分批取未索引文件 → 哈希/视频帧/人脸 → 落库）统一收敛在
services/index_service.py（与 HTTP /api/dedup/run 的 index_first 共用），
本类只负责 QThread 生命周期与信号汇报。
"""

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot

from config import AppConfig
from app.services.index_service import run_index_pipeline

logger = logging.getLogger(__name__)


class HashWorker(QThread):
    """后台哈希计算线程。

    信号:
        progress: 哈希进度 (已完成, 总数)。
        file_hashed: 一个文件完成哈希 (file_id, hash_type)。
        finished: 全部完成 (hashed_count: int)。
        error_occurred: 错误 (消息)。
    """

    progress = Signal(int, int)
    file_hashed = Signal(int, str)
    finished = Signal(int)
    error_occurred = Signal(str)

    def __init__(self, config: AppConfig, parent: Optional[QThread] = None) -> None:
        """初始化哈希线程。

        参数:
            config: 应用配置。
        """
        super().__init__(parent)
        self._config = config
        self._cancelled = False

    def run(self) -> None:
        """执行哈希计算任务。"""
        try:
            result = run_index_pipeline(
                self._config,
                progress_callback=lambda cur, total: self.progress.emit(cur, total),
                file_hashed_callback=lambda fid, kind: self.file_hashed.emit(fid, kind),
                cancelled=lambda: self._cancelled,
            )
            self.finished.emit(result.hashed_count)
        except Exception as e:
            logger.error(f"哈希线程错误: {e}")
            self.error_occurred.emit(str(e))

    @Slot()
    def cancel(self) -> None:
        """取消哈希任务。"""
        self._cancelled = True
        logger.info("哈希任务已取消")
