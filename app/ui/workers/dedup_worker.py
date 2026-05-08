# -*- coding: utf-8 -*-
"""
查重工作线程 —— 后台执行资源单元两两比对。

拉取选中单元的文件数据，调用 DedupEngine 执行全量比对，
将结果写入数据库并发出通知。
"""

import logging
import struct
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot

from config import AppConfig
from app.core.dedup_engine import DedupEngine, UnitComparisonResult, DedupSession
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services.message_center import MessageCenter

logger = logging.getLogger(__name__)


def _load_face_vectors(session, file_id: int, enabled: bool) -> list[tuple[float, ...]]:
    """从数据库加载文件的人脸特征向量（解包为浮点元组）。

    参数:
        session: 数据库会话。
        file_id: 媒体文件 ID。
        enabled: 是否启用人脸检测。

    返回:
        128 维浮点元组列表，人脸未启用或无数据时返回空列表。
    """
    if not enabled:
        return []
    vectors = []
    for fv in q.get_face_vectors_by_file(session, file_id):
        try:
            if isinstance(fv.vector_data, bytes) and len(fv.vector_data) == 512:
                vectors.append(struct.unpack("<128f", fv.vector_data))
        except Exception:
            continue
    return vectors


class DedupWorker(QThread):
    """后台查重比对线程。

    信号:
        progress: 查重进度 (已完成对数, 总对数)。
        pair_compared: 一对单元比对完成 (unit_a_id, unit_b_id, jaccard)。
        duplicate_found: 发现重复 (UnitComparisonResult)。
        finished: 全部完成 (DedupSession)。
        error_occurred: 错误 (消息)。
    """

    progress = Signal(int, int)
    pair_compared = Signal(int, int, float)
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
        """执行查重比对任务。"""
        try:
            # 拉取所有参与单元的文件数据
            unit_files_map: dict[int, list[dict]] = {}
            unit_names: dict[int, str] = {}

            with DatabaseManager.session() as session:
                for uid in self._unit_ids:
                    unit = q.get_unit_by_id(session, uid)
                    if unit:
                        unit_names[uid] = unit.name
                        files = q.get_files_by_unit(session, uid)
                        unit_files_map[uid] = [
                            {
                                "id": f.id,
                                "path": f.path,
                                "md5_hash": f.md5_hash,
                                "phash": f.phash,
                                "dhash": f.dhash,
                                "face_vectors": _load_face_vectors(
                                    session, f.id, self._config.face_detection_enabled
                                ),
                            }
                            for f in files
                        ]

            if len(unit_files_map) < 2:
                self.error_occurred.emit("至少需要 2 个资源单元才能执行查重")
                return

            # 创建查重引擎（人脸识别优先，哈希备用）
            engine = DedupEngine(
                jaccard_threshold=self._config.jaccard_threshold,
                phash_hamming_threshold=self._config.phash_hamming_threshold,
                dhash_hamming_threshold=self._config.dhash_hamming_threshold,
                face_distance_threshold=self._config.face_distance_threshold,
                face_enabled=self._config.face_detection_enabled,
            )

            # 执行比对
            session = engine.run_dedup(
                unit_files_map=unit_files_map,
                unit_names=unit_names,
                progress_callback=lambda cur, total: self.progress.emit(cur, total),
            )

            if self._cancelled:
                return

            # 写入数据库 + 发出通知
            with DatabaseManager.session() as db_session:
                for dup in session.duplicates_found:
                    self._save_duplicate(db_session, dup)

            self.finished.emit(session)
            logger.info(
                f"查重完成: {session.total_units_compared} 个单元, "
                f"发现 {len(session.duplicates_found)} 组重复"
            )

        except Exception as e:
            logger.error(f"查重线程错误: {e}")
            self.error_occurred.emit(str(e))

    @Slot()
    def cancel(self) -> None:
        """取消查重任务。"""
        self._cancelled = True
        logger.info("查重任务已取消")

    def _save_duplicate(self, db_session, dup: UnitComparisonResult) -> None:
        """将一组重复结果写入数据库并创建消息提醒。

        参数:
            db_session: 数据库会话。
            dup: 单元比对结果。
        """
        # 写入/更新 dedup_results
        result = q.upsert_dedup_result(
            db_session,
            unit_a_id=dup.unit_a_id,
            unit_b_id=dup.unit_b_id,
            similarity_score=dup.jaccard_similarity,
            match_count=len(dup.file_matches),
            total_files_a=dup.total_files_a,
            total_files_b=dup.total_files_b,
            match_types=dup.match_types_str,
        )

        # 写入每条文件匹配
        for fm in dup.file_matches:
            q.insert_file_match(
                db_session,
                dedup_result_id=result.id,
                file_a_id=fm.file_a_id,
                file_b_id=fm.file_b_id,
                similarity_score=fm.score,
                match_type=fm.match_type,
            )

        # 创建消息提醒（在独立事务中）
        db_session.commit()  # 先提交查重结果

        MessageCenter.create_dedup_alert(
            unit_a_name=dup.unit_a_name,
            unit_b_name=dup.unit_b_name,
            similarity=dup.jaccard_similarity,
            match_count=len(dup.file_matches),
            dedup_result_id=result.id,
        )

        self.duplicate_found.emit(dup)
