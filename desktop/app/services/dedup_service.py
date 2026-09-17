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
from app.utils.constants import EvidenceKind

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

    related_saved: int = 0
    """写入数据库的"疑似相关"单元对数（不逐条发消息，走汇总提醒）。"""

    face_only_saved: int = 0
    """写入数据库的"只有人脸线索"单元对数（同演员，展示在同演员 Tab）。"""

    pruned_stale: int = 0
    """清理掉的陈旧结果行数（本轮范围内未再命中且用户未处置）。"""


def _load_face_vectors(session, file_id: int, enabled: bool) -> list[tuple]:
    """从数据库加载文件的人脸特征向量。

    返回 [(source_ms, vector), ...]：source_ms 是视频帧时间位置（图片为 None），
    引擎据此统计"多帧吻合"的帧数（frame_support）。
    """
    if not enabled:
        return []
    vectors = []
    for fv in q.get_face_vectors_by_file(session, file_id):
        try:
            if isinstance(fv.vector_data, bytes) and len(fv.vector_data) == 512:
                vectors.append((fv.source_ms, struct.unpack("<128f", fv.vector_data)))
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


def _load_unit_files(unit_ids: list[int], load_face: bool, face_enabled: bool
                     ) -> tuple[dict[int, list[dict]], dict[int, str]]:
    """加载参与比对的单元文件记录（含视频帧与人脸向量）。

    抽成独立函数是为了让"人脸精查后重新比对"能重新读一遍向量：
    精查会删掉旧的单帧向量、写入多帧向量，不重读就等于白精查。
    """
    unit_files_map: dict[int, list[dict]] = {}
    unit_names: dict[int, str] = {}
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
    return unit_files_map, unit_names


def _compare_once(unit_files_map: dict[int, list[dict]],
                  unit_names: dict[int, str], engine: DedupEngine,
                  skip_pairs: set,
                  progress_callback: Optional[Callable[[int, int], None]]
                  ) -> DedupSession:
    """跑一次两两比对（精查前后各跑一次，参数完全一致）。"""
    return engine.run_dedup(
        unit_files_map=unit_files_map,
        unit_names=unit_names,
        progress_callback=progress_callback,
        skip_pairs=skip_pairs,
    )


def run_dedup_pipeline(
    unit_ids: list[int],
    *,
    threshold: float,
    phash_hamming_threshold: int,
    dhash_hamming_threshold: int,
    face_similarity_threshold: float,
    face_enabled: bool,
    related_min_matches: int = 2,
    load_face: bool = True,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    duplicate_found_callback: Optional[Callable[[UnitComparisonResult], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
    config=None,
    refine_faces: bool = False,
    face_progress_callback: Optional[Callable[[int, int], None]] = None,
) -> DedupRunResult:
    """执行完整查重管线：加载 → 跳过已处置/白名单对 → 比对 → 落库 → 提醒。

    参数:
        unit_ids: 参与比对的资源单元 ID 列表。
        threshold: 杰卡德相似度阈值。
        phash_hamming_threshold / dhash_hamming_threshold: 感知哈希汉明阈值。
        face_similarity_threshold: 人脸余弦相似度阈值。
        face_enabled: 是否启用人脸维度。
        load_face: 是否加载人脸向量（统一为 True 可保证 GUI/API 行为一致）。
        progress_callback: 引擎比对进度 (已完成对数, 总对数)。
        duplicate_found_callback: 每组入库的重复 (UnitComparisonResult)。
        cancelled: 取消探测（返回 True 时不再落库/提醒）。
        config: 应用配置。精查人脸（refine_faces）时必传 —— 需要抽帧参数。
        refine_faces: 比对后对"上一轮出现人脸线索的候选视频"做多帧精查，
            然后**重新比对一次**。旧库的视频人脸是"只取中间一帧"扫出来的，
            不精查就只能拿不准的线索下结论。
        face_progress_callback: 人脸精查进度 (已完成, 总数)。

    返回:
        DedupRunResult。

    异常:
        ValueError: 有效单元不足 2 个时抛出（由调用方决定如何提示）。
    """
    start = time.time()
    is_cancelled = cancelled or (lambda: False)

    unit_files_map, unit_names = _load_unit_files(unit_ids, load_face, face_enabled)
    with DatabaseManager.session() as session:
        # 跳过已处置/白名单的单元对（避免重复告警）
        skip_pairs = q.build_dedup_skip_pairs(session, unit_ids)

    if len(unit_files_map) < 2:
        raise ValueError("至少需要 2 个有效资源单元才能执行查重")

    engine = DedupEngine(
        jaccard_threshold=threshold,
        phash_hamming_threshold=phash_hamming_threshold,
        dhash_hamming_threshold=dhash_hamming_threshold,
        face_similarity_threshold=face_similarity_threshold,
        face_enabled=face_enabled,
        related_min_matches=related_min_matches,
    )
    session_result = _compare_once(unit_files_map, unit_names, engine,
                                   skip_pairs, progress_callback)

    # ---- 人脸精查：候选视频多帧重扫 → 重新比对（结论必须基于精查后的向量）----
    if refine_faces and config is not None and face_enabled and not is_cancelled():
        from app.services.index_service import run_face_rescan_pipeline
        rescan = run_face_rescan_pipeline(
            config, scope="candidates",
            progress_callback=face_progress_callback,
            cancelled=is_cancelled,
        )
        if rescan.scanned and not is_cancelled():
            logger.info(
                f"人脸精查完成（{rescan.scanned} 个视频 / {rescan.faces} 条向量），重新比对"
            )
            unit_files_map, unit_names = _load_unit_files(
                unit_ids, load_face, face_enabled)
            session_result = _compare_once(unit_files_map, unit_names, engine,
                                           skip_pairs, progress_callback)

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
    related_saved = 0
    face_only_saved = 0
    pruned_stale = 0
    with DatabaseManager.session() as db_session:
        for dup in session_result.duplicates_found:
            _save_result(db_session, dup, notify=True)
            saved += 1
            if duplicate_found_callback:
                duplicate_found_callback(dup)

        # "疑似相关"逐条落库（可在界面/接口里查看），但不逐条发消息 ——
        # 实测本库 166 个单元会产生 200+ 对，逐条提醒会变成刷屏。
        related_results = []
        for rel in session_result.related_found:
            result = _save_result(db_session, rel, notify=False)
            related_results.append((rel, result))
            related_saved += 1

        # "同演员"（只有人脸线索、零文件匹配）同样落库，但单独归一类：
        # 它们杰卡德恒为 0，混进"疑似相关"就是用户看到的 380 条 0% 噪音。
        face_only_results = []
        for only in session_result.face_only_found:
            result = _save_result(db_session, only, notify=False)
            face_only_results.append((only, result))
            face_only_saved += 1

        # 清理陈旧结果：规则/阈值一变，旧行不会自己消失（见 prune_stale_dedup_results）
        keep_pairs = {
            q._pair_key(item.unit_a_id, item.unit_b_id)
            for item in (session_result.duplicates_found
                         + session_result.related_found
                         + session_result.face_only_found)
        }
        pruned_stale = q.prune_stale_dedup_results(
            db_session, list(unit_ids_sorted), keep_pairs)

    if related_results:
        MessageCenter.create_related_digest(
            [(rel.unit_a_name, rel.unit_b_name, len(rel.file_matches),
              rel.jaccard_similarity, rel.match_types_str or "video")
             for rel, _ in related_results],
            face_pairs=[
                (rel.unit_a_name, rel.unit_b_name, len(rel.face_hints))
                for rel, _ in face_only_results
            ],
        )

    logger.info(
        f"查重完成: {session_result.total_units_compared} 个单元, "
        f"跳过 {skipped_pairs} 对, 入库重复 {saved} 组 / 疑似相关 {related_saved} 对 / "
        f"同演员 {face_only_saved} 对, 清理陈旧 {pruned_stale} 条"
    )
    return DedupRunResult(
        session=session_result, saved_count=saved, related_saved=related_saved,
        face_only_saved=face_only_saved, pruned_stale=pruned_stale,
        skipped_pairs=skipped_pairs, elapsed_seconds=round(time.time() - start, 2),
    )


def _save_result(db_session, item: UnitComparisonResult, *, notify: bool):
    """将一条命中结果（重复或疑似相关）写入数据库。

    参数:
        db_session: 数据库会话。
        item: 引擎给出的一条命中。
        notify: 是否创建逐条消息提醒（重复=是，疑似相关=否，走汇总）。

    返回:
        落库后的 DedupResult。
    """
    result = q.upsert_dedup_result(
        db_session,
        unit_a_id=item.unit_a_id,
        unit_b_id=item.unit_b_id,
        similarity_score=item.jaccard_similarity,
        # 只统计**计入判定**的文件对。旧实现存 evidence_count（含人脸线索），
        # 于是卡片写"46 处线索"、详情写"匹配 46 个文件"，而实际入库只有 38 条。
        match_count=len(item.file_matches),
        total_files_a=item.total_files_a,
        total_files_b=item.total_files_b,
        match_types=item.match_types_str,
        match_level=item.level,
        evidence_kind=(EvidenceKind.FILE.value if item.has_file_evidence
                       else EvidenceKind.FACE.value),
    )
    # 清除旧匹配（upsert 可能返回已有 result，旧匹配需要替换）
    q.delete_file_matches_for_result(db_session, result.id)
    seen_pairs: set[tuple[int, int]] = set()
    for fm in item.file_matches:
        q.insert_file_match(
            db_session,
            dedup_result_id=result.id,
            file_a_id=fm.file_a_id,
            file_b_id=fm.file_b_id,
            similarity_score=fm.score,
            match_type=fm.match_type,
            frame_support=fm.frame_support,
        )
        seen_pairs.add((fm.file_a_id, fm.file_b_id))
    # 人脸线索也落库：它是"同演员"这一档的**唯一**依据，界面要能说明为什么提醒。
    # 必须跳过与计数匹配重复的文件对，否则会撞 uq_file_pair 唯一约束。
    for fm in item.face_hints:
        pair = (fm.file_a_id, fm.file_b_id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        q.insert_file_match(
            db_session,
            dedup_result_id=result.id,
            file_a_id=fm.file_a_id,
            file_b_id=fm.file_b_id,
            similarity_score=fm.score,
            match_type=fm.match_type,
            frame_support=fm.frame_support,
        )
    db_session.commit()  # 先提交查重结果

    if notify:
        MessageCenter.create_dedup_alert(
            unit_a_name=item.unit_a_name,
            unit_b_name=item.unit_b_name,
            similarity=item.jaccard_similarity,
            match_count=len(item.file_matches),
            dedup_result_id=result.id,
        )
    return result
