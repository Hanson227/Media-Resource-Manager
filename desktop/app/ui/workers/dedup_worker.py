# -*- coding: utf-8 -*-
"""
查重工作线程 —— 后台执行资源单元两两比对。

真正的管线（加载 → 跳过已处置/白名单对 → 引擎比对 → 落库 → 提醒）
统一收敛在 services/dedup_service.py（与 HTTP /api/dedup/run 共用），
本类只负责 QThread 生命周期、互斥门与信号汇报。
"""

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot

from config import AppConfig
from app.services.dedup_gate import acquire_dedup_gate, release_dedup_gate
from app.services.dedup_service import run_dedup_pipeline

logger = logging.getLogger(__name__)


class DedupWorker(QThread):
    """后台查重比对线程。

    信号:
        progress: 查重进度 (已完成对数, 总对数)。
        duplicate_found: 一组重复结果入库 (UnitComparisonResult)。
        finished: 全部完成 (DedupSession)。
        error_occurred: 错误 (消息)。
    """

    progress = Signal(int, int)
    duplicate_found = Signal(object)
    finished = Signal(object)
    error_occurred = Signal(str)

    def __init__(self, config: AppConfig, unit_ids: list[int],
                 parent: Optional[QThread] = None) -> None:
        """初始化查重线程。

        参数:
            config: 应用配置。
            unit_ids: 要参与比对的资源单元 ID 列表。
        """
        super().__init__(parent)
        self._config = config
        self._unit_ids = unit_ids
        self._cancelled = False

    def run(self) -> None:
        """执行查重比对任务（与 HTTP /api/dedup/run 入口互斥）。"""
        if not acquire_dedup_gate():
            self.error_occurred.emit("已有查重任务正在运行，请稍后再试")
            return
        try:
            outcome = run_dedup_pipeline(
                self._unit_ids,
                threshold=self._config.jaccard_threshold,
                phash_hamming_threshold=self._config.phash_hamming_threshold,
                dhash_hamming_threshold=self._config.dhash_hamming_threshold,
                face_distance_threshold=self._config.face_distance_threshold,
                face_enabled=self._config.face_detection_enabled,
                load_face=True,
                progress_callback=lambda cur, total: self.progress.emit(cur, total),
                duplicate_found_callback=lambda dup: self.duplicate_found.emit(dup),
                cancelled=lambda: self._cancelled,
            )
            self.finished.emit(outcome.session)
        except ValueError as e:
            self.error_occurred.emit(str(e))
        except Exception as e:
            logger.error(f"查重线程错误: {e}")
            self.error_occurred.emit(str(e))
        finally:
            release_dedup_gate()

    @Slot()
    def cancel(self) -> None:
        """取消查重任务（管线停止落库与提醒）。"""
        self._cancelled = True
        logger.info("查重任务已取消")
