# -*- coding: utf-8 -*-
"""
查重编排服务 —— GUI 工作线程与 HTTP API 共用的唯一实现。

此前 GUI（ui/workers/dedup_worker.py）与 API（api/routes/dedup.py）
各自实现了一套完整管线（加载文件 → skip 集合 → 引擎比对 → 落库 → 发消息），
已出现行为分叉（如 API 路径漏加载人脸向量导致人脸匹配静默失效）。
本模块收敛为单一实现，两条入口只负责"参数化 + 汇报 + 互斥"。
"""

import logging
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.core.dedup_engine import DedupEngine, DedupSession, UnitComparisonResult
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services.message_center import MessageCenter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DedupRunResult:
    """一次完整查重编排的结果。"""

    session: DedupSession
    """引擎比对会话（含全部重复项）。"""

    saved_count: int
    """实际写入数据库并发送提醒的重复组数。"""

    skipped_pairs: int
    """因已处置/白名单而跳过的单元对数。"""

    elapsed_seconds: float
    """编排总耗时（秒）。"""


def _load_face_vectors(session, file_id: int, enabled: bool) -> list[tuple[float, ...]]:
    """从数据库加载文件的人脸特征向量（解包为浮点元组）。"""
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


def _file_record(f, session, load_face: bool, face_enabled: bool,
                 frame_map: Optional[dict] = None) -> dict:
    """将 ORM 文件行转为引擎比对所需的记录 dict。"""
    return {
        "id": f.id,
        "path": f.path,
        "md5_hash": f.md5_hash,
        "phash": f.phash,
        "dhash": f.dhash,
        "face_vectors": (
            _load_face_vectors(session, f.id, face_enabled) if load_face else []
        ),
        # 视频关键帧 pHash 列表（图片为空）：供引擎做帧级近似匹配
        "video_frames": (frame_map or {}).get(f.id, []),
    }


def run_dedup_pipeline(
    unit_ids: list[int],
    *,
    threshold: float,
    phash_hamming_threshold: int,
    dhash_hamming_threshold: int,
    face_distance_threshold: float,
    face_enabled: bool,
    load_face: bool = True,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    duplicate_found_callback: Optional[Callable[[UnitComparisonResult], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> DedupRunResult:
    """执行完整查重管线：加载 → 跳过已处置/白名单对 → 比对 → 落库 → 提醒。

    参数:
        unit_ids: 参与比对的资源单元 ID 列表。
        threshold: 杰卡德相似度阈值。
        phash_hamming_threshold / dhash_hamming_threshold: 感知哈希汉明阈值。
        face_distance_threshold: 人脸向量欧氏距离阈值。
        face_enabled: 是否启用人脸维度。
        load_face: 是否加载人脸向量（统一为 True 可保证 GUI/API 行为一致）。
        progress_callback: 引擎比对进度 (已完成对数, 总对数)。
        duplicate_found_callback: 每组入库的重复 (UnitComparisonResult)。
        cancelled: 取消探测（返回 True 时不再落库/提醒）。

    返回:
        DedupRunResult。

    异常:
        ValueError: 有效单元不足 2 个时抛出（由调用方决定如何提示）。
    """
    start = time.time()
    is_cancelled = cancelled or (lambda: False)

    unit_files_map: dict[int, list[dict]] = {}
    unit_names: dict[int, str] = {}
    skip_pairs = set()

    with DatabaseManager.session() as session:
        for uid in unit_ids:
            unit = q.get_unit_by_id(session, uid)
            if unit:
                unit_names[uid] = unit.name
                files = q.get_files_by_unit(session, uid)
                # 一次性装载本单元视频帧哈希（图片无帧，返回空），避免 N+1
                frame_map = q.get_video_frame_hashes_by_files(
                    session, [f.id for f in files]
                )
                unit_files_map[uid] = [
                    _file_record(f, session, load_face, face_enabled, frame_map)
                    for f in files
                ]
        # 跳过已处置/白名单的单元对（避免重复告警）
        skip_pairs = q.build_dedup_skip_pairs(session, unit_ids)

    if len(unit_files_map) < 2:
        raise ValueError("至少需要 2 个有效资源单元才能执行查重")

    engine = DedupEngine(
        jaccard_threshold=threshold,
        phash_hamming_threshold=phash_hamming_threshold,
        dhash_hamming_threshold=dhash_hamming_threshold,
        face_distance_threshold=face_distance_threshold,
        face_enabled=face_enabled,
    )
    session_result = engine.run_dedup(
        unit_files_map=unit_files_map,
        unit_names=unit_names,
        progress_callback=progress_callback,
        skip_pairs=skip_pairs,
    )
    # 统计实际跳过的对数（skip_pairs 中确属于本次参与单元的）
    unit_ids_sorted = sorted(unit_files_map.keys())
    possible_pairs = {
        (unit_ids_sorted[i], unit_ids_sorted[j])
        for i in range(len(unit_ids_sorted))
        for j in range(i + 1, len(unit_ids_sorted))
    }
    skipped_pairs = len(skip_pairs & possible_pairs)

    if is_cancelled():
        logger.info("查重已取消，跳过落库与提醒")
        return DedupRunResult(
            session=session_result, saved_count=0,
            skipped_pairs=skipped_pairs, elapsed_seconds=round(time.time() - start, 2),
        )

    saved = 0
    with DatabaseManager.session() as db_session:
        for dup in session_result.duplicates_found:
            _save_duplicate(db_session, dup)
            saved += 1
            if duplicate_found_callback:
                duplicate_found_callback(dup)

    logger.info(
        f"查重完成: {session_result.total_units_compared} 个单元, "
        f"跳过 {skipped_pairs} 对, 入库 {saved} 组"
    )
    return DedupRunResult(
        session=session_result, saved_count=saved,
        skipped_pairs=skipped_pairs, elapsed_seconds=round(time.time() - start, 2),
    )


def _save_duplicate(db_session, dup: UnitComparisonResult) -> None:
    """将一组重复结果写入数据库并创建消息提醒（独立提交，避免长事务）。"""
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
    # 清除旧匹配（upsert 可能返回已有 result，旧匹配需要替换）
    q.delete_file_matches_for_result(db_session, result.id)
    for fm in dup.file_matches:
        q.insert_file_match(
            db_session,
            dedup_result_id=result.id,
            file_a_id=fm.file_a_id,
            file_b_id=fm.file_b_id,
            similarity_score=fm.score,
            match_type=fm.match_type,
        )
    db_session.commit()  # 先提交查重结果

    MessageCenter.create_dedup_alert(
        unit_a_name=dup.unit_a_name,
        unit_b_name=dup.unit_b_name,
        similarity=dup.jaccard_similarity,
        match_count=len(dup.file_matches),
        dedup_result_id=result.id,
    )
