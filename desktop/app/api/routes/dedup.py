# -*- coding: utf-8 -*-
"""
查重路由 —— /api/dedup 真实数据库查询端点。

注意：处理函数一律使用同步 def（FastAPI 自动放入线程池执行），
避免长任务（run_dedup）阻塞事件循环；查重入口经 dedup_gate 与 GUI 互斥。
"""

import logging
import threading
import time
from typing import Optional

from fastapi import APIRouter, Query, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func

from app.api import task_registry as registry
from app.api.schemas import (
    DedupResultItem, DedupListResponse, DedupResolveRequest, DedupGroupItem,
    DedupGroupListResponse, DedupFaceScanRequest, StatusResponse,
    DedupMergeRequest, DedupMergeResponse,
)
from app.db.engine import DatabaseManager
from app.db.models import FaceVector, MediaFile
from app.db import queries as q
from app.services.dedup_gate import acquire_dedup_gate, release_dedup_gate
from app.services.dedup_service import run_dedup_pipeline
from app.utils.constants import DEDUP_RESULT_VERSION, FACE_SCAN_VERSION, MatchType

logger = logging.getLogger(__name__)

DEDUP_TASK_KINDS = ("dedup", "face")
"""本路由负责的任务类型（HEIC 转换走自己的路由，不能混进 active-task）。"""


class DedupRunRequest(BaseModel):
    unit_ids: list[int] = Field(..., min_length=2)
    threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    index_first: bool = Field(
        default=False,
        description="先为未索引文件计算哈希（MD5/视频帧/人脸）再比对。"
                    "与桌面端「一键查重」一致；为 False 时未索引文件对查重不可见。",
    )
    face_similarity_threshold: Optional[float] = Field(
        default=None, ge=0.0, le=1.0,
        description="本次查重的人脸相似度阈值（覆盖服务端配置）。"
                    "不影响人脸抽帧，只影响「同演员线索」的松紧。",
    )
    refine_faces: bool = Field(
        default=False,
        description="比对后对「上一轮出现人脸线索的候选视频」做定间隔多帧重扫，"
                    "再重跑一次比对（更准但更慢，实测约 2~3 分钟）。",
    )


# ============================================================
# 查重任务后台执行 + 轮询（POST /run 立即返回 task_id）
# ============================================================
# 任务登记表统一在 app.api.task_registry：与 /api/heic 路由共用，
# 避免各路由各写一份字典导致字段名/清理策略/取消语义漂移。


def _run_dedup_task(task_id: str, unit_ids: list[int], threshold: float,
                    cfg_fields: dict, config, index_first: bool = False,
                    refine_faces: bool = False) -> None:
    """后台执行查重管线，并把状态/进度/结果写回任务表。

    参数:
        index_first: 为 True 时先跑索引管线（与桌面端「一键查重」一致）。
            不索引就比对，未索引文件对查重完全不可见，却会返回"查重完成"。
        cfg_fields: 引擎参数（含本次的人脸阈值覆盖值）。
        refine_faces: 比对后对候选视频做多帧人脸精查并重新比对。
    """
    def update(**kw) -> None:
        registry.update(task_id, **kw)

    update(status="running", started_at=time.time())
    try:
        if not acquire_dedup_gate():
            update(status="failed",
                   error="已有查重任务正在运行（桌面端或其它请求），请稍后再试",
                   finished_at=time.time())
            return
        try:
            indexed_count = 0
            unindexed_before = 0
            if index_first:
                from app.db.engine import DatabaseManager
                from app.db import queries as q
                from app.services.index_service import run_index_pipeline
                with DatabaseManager.session() as session:
                    unindexed_before = q.get_unindexed_file_count(session)
                if unindexed_before > 0:
                    update(phase="indexing", progress=[0, unindexed_before])
                    index_result = run_index_pipeline(
                        config,
                        progress_callback=lambda cur, total: update(
                            phase="indexing", progress=[cur, total]),
                    )
                    indexed_count = index_result.hashed_count
                    update(phase="comparing", progress=None)

            update(phase="comparing")
            outcome = run_dedup_pipeline(
                unit_ids,
                threshold=threshold,
                phash_hamming_threshold=cfg_fields["phash_hamming_threshold"],
                dhash_hamming_threshold=cfg_fields["dhash_hamming_threshold"],
                face_similarity_threshold=cfg_fields["face_similarity_threshold"],
                face_enabled=cfg_fields["face_enabled"],
                related_min_matches=cfg_fields["related_min_matches"],
                load_face=cfg_fields["face_enabled"],  # 与 GUI 行为一致
                progress_callback=lambda cur, total: update(progress=[cur, total]),
                config=config,
                refine_faces=refine_faces,
                face_progress_callback=lambda cur, total: update(
                    phase="faces", progress=[cur, total]),
            )
        finally:
            release_dedup_gate()
        update(
            status="completed",
            phase=None,
            progress=None,
            finished_at=time.time(),
            result={
                "total_compared": outcome.session.total_units_compared,
                "duplicates_found": len(outcome.session.duplicates_found),
                "related_found": len(outcome.session.related_found),
                "face_only_found": len(outcome.session.face_only_found),
                "saved_count": outcome.saved_count,
                "related_saved": outcome.related_saved,
                "face_only_saved": outcome.face_only_saved,
                "pruned_stale": outcome.pruned_stale,
                "skipped_pairs": outcome.skipped_pairs,
                "elapsed_seconds": outcome.elapsed_seconds,
                "unindexed_before": unindexed_before,
                "indexed_count": indexed_count,
                "face_similarity_threshold": cfg_fields["face_similarity_threshold"],
            },
        )
    except ValueError as e:
        update(status="failed", error=str(e), finished_at=time.time())
    except Exception as e:
        logger.error(f"查重任务 {task_id} 执行失败: {e}", exc_info=True)
        update(status="failed", error="内部错误，详见服务端日志", finished_at=time.time())


router = APIRouter(prefix="/api/dedup", tags=["查重"])


@router.get("/results", response_model=DedupListResponse)
def list_dedup_results(
    unresolved_only: bool = Query(True, description="仅显示未处理"),
    level: Optional[str] = Query(
        None, pattern="^(duplicate|related)$",
        description="按命中等级过滤：duplicate=建议处置的重复；related=仅提醒的疑似相关",
    ),
    evidence: Optional[str] = Query(
        None, pattern="^(file|face)$",
        description="按证据来源过滤：file=有文件重叠（真正的疑似相关）；"
                    "face=只有人脸线索（同演员，杰卡德恒为 0）",
    ),
    unit_a_id: Optional[int] = Query(
        None, description="只看以该单元为左侧（较小 ID）的结果，用于按来源单元展开分组",
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """列出查重比对结果（SQL 分页 + 精确总数；单元名批量查询，避免 N+1）。"""
    try:
        with DatabaseManager.session() as session:
            page_results, total = q.get_dedup_results_page(
                session, unresolved_only=unresolved_only,
                page=page, per_page=per_page, level=level,
                evidence=evidence, unit_a_id=unit_a_id,
            )

            # 批量取涉及单元名
            unit_ids = {dr.unit_a_id for dr in page_results} | {dr.unit_b_id for dr in page_results}
            unit_map = {u.id: u.name for u in q.get_units_by_ids(session, list(unit_ids))}
            # 批量取人脸线索条数（match_count 已不含人脸，前端不能再拿它当线索数）
            hint_map = q.get_face_hint_counts(session, [dr.id for dr in page_results])

            items = [
                DedupResultItem(
                    id=dr.id,
                    unit_a_id=dr.unit_a_id, unit_b_id=dr.unit_b_id,
                    unit_a_name=unit_map.get(dr.unit_a_id, f"单元{dr.unit_a_id}"),
                    unit_b_name=unit_map.get(dr.unit_b_id, f"单元{dr.unit_b_id}"),
                    similarity_score=dr.similarity_score,
                    match_count=dr.match_count,
                    match_types=dr.match_types or "",
                    match_level=dr.match_level,
                    evidence_kind=dr.evidence_kind,
                    face_hint_count=hint_map.get(dr.id, 0),
                    total_files_a=dr.total_files_a,
                    total_files_b=dr.total_files_b,
                    overlap_ratio=_overlap_ratio(dr),
                    stale=_is_stale(dr),
                    is_resolved=dr.is_resolved,
                    resolution=dr.resolution,
                    created_at=dr.created_at,
                )
                for dr in page_results
            ]
            return DedupListResponse(results=items, total=total)
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


def _overlap_ratio(dr) -> float:
    """包含度 = 文件匹配数 / min(|A|, |B|)（服务端算，避免各端重复实现）。"""
    min_files = max(1, min(dr.total_files_a or 0, dr.total_files_b or 0))
    return round((dr.match_count or 0) / min_files, 4)


def _is_stale(dr) -> bool:
    """这条结果是否由旧规则算出（升级后未重跑查重）。

    两个判据任一命中即为旧结论：
    1. computed_version 落后于当前算法版本；
    2. match_count > 文件数（新口径下"计入判定的文件对"不可能超过较小单元的文件数）——
       实测踩到：旧行 match_count=46 配 33 个文件的单元，界面显示"46/33 个文件重叠"。
    """
    if int(getattr(dr, "computed_version", 0) or 0) < DEDUP_RESULT_VERSION:
        return True
    min_files = max(0, min(dr.total_files_a or 0, dr.total_files_b or 0))
    return (dr.match_count or 0) > min_files


@router.get("/groups", response_model=DedupGroupListResponse)
def list_dedup_groups(
    unresolved_only: bool = Query(True, description="仅统计未处理"),
    level: Optional[str] = Query(None, pattern="^(duplicate|related)$"),
    evidence: Optional[str] = Query(None, pattern="^(file|face)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
):
    """按左侧单元分组统计（列表页二级分组的父级）。

    "左边相同、右边不同"的成百条线索折叠成几十个文件夹分组 ——
    分组键与卡片左侧显示的是同一个单元（存储时较小的 unit_id）。
    """
    try:
        with DatabaseManager.session() as session:
            groups, total = q.get_dedup_groups(
                session, unresolved_only=unresolved_only,
                level=level, evidence=evidence, page=page, per_page=per_page,
            )
            anchors = [g["anchor_unit_id"] for g in groups]
            unit_map = {u.id: u.name for u in q.get_units_by_ids(session, anchors)}
            cover_map = _cover_file_ids(session, anchors)
            items = [
                DedupGroupItem(
                    anchor_unit_id=g["anchor_unit_id"],
                    anchor_unit_name=unit_map.get(
                        g["anchor_unit_id"], f"单元{g['anchor_unit_id']}"),
                    anchor_cover_file_id=cover_map.get(g["anchor_unit_id"]),
                    pair_count=g["pair_count"],
                    max_similarity=g["max_similarity"],
                    match_types=g["match_types"],
                    match_level=g["level"] or "related",
                    evidence_kind=g["evidence_kind"] or "file",
                    stale=bool(g.get("stale")),
                )
                for g in groups
            ]
            return DedupGroupListResponse(groups=items, total=total)
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


def _cover_file_ids(session, unit_ids: list[int]) -> dict[int, int]:
    """取每个分组单元的首个文件 ID 作封面（单次聚合查询，避免 N+1）。"""
    if not unit_ids:
        return {}
    from app.db.models import MediaFile
    rows = (
        session.query(MediaFile.resource_unit_id, func.min(MediaFile.id))
        .filter(MediaFile.resource_unit_id.in_(unit_ids))
        .group_by(MediaFile.resource_unit_id)
        .all()
    )
    return {int(uid): int(fid) for uid, fid in rows}


@router.get("/counts")
def get_dedup_counts():
    """按命中等级统计未处置结果数量（列表页分组标题用）。"""
    try:
        with DatabaseManager.session() as session:
            return q.count_dedup_results_by_level(session)
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.get("/results/{result_id}")
def get_dedup_detail(result_id: int):
    """获取查重结果详情（含文件匹配列表）。"""
    try:
        with DatabaseManager.session() as session:
            dr = q.get_dedup_by_id(session, result_id)
            if not dr:
                raise HTTPException(status_code=404, detail=f"查重结果不存在: {result_id}")

            matches = q.get_file_matches_for_result(session, result_id)
            unit_a = q.get_unit_by_id(session, dr.unit_a_id)
            unit_b = q.get_unit_by_id(session, dr.unit_b_id)
            hint_count = sum(1 for fm in matches if fm.match_type == MatchType.FACE.value)

            # 文件名：只显示 "#101 ↔ #103" 无法让用户判断该不该处理。
            # 一次 IN 查询取出本结果涉及的全部文件名（避免 N+1）。
            file_ids = {fm.file_a_id for fm in matches} | {fm.file_b_id for fm in matches}
            name_map: dict[int, str] = {}
            if file_ids:
                from app.db.models import MediaFile
                rows = (
                    session.query(MediaFile.id, MediaFile.filename)
                    .filter(MediaFile.id.in_(list(file_ids)))
                    .all()
                )
                name_map = {fid: fname for fid, fname in rows}

            # 两侧独有文件（不在任何 counted 匹配对里的文件）：
            # 合并/逐文件处理的界面需要它们 —— 匹配网格只显示"成对"的文件，
            # 不显示独有的，用户就看不见合并时会被移动的那部分。
            # face 线索涉及的两侧行内容本就不同，归入 unmatched（不算重复证据）。
            counted = [fm for fm in matches if fm.match_type != MatchType.FACE.value]
            matched_a = {fm.file_a_id for fm in counted}
            matched_b = {fm.file_b_id for fm in counted}

            def _unmatched(files) -> list[dict]:
                return [
                    {"file_id": f.id, "filename": f.filename,
                     "media_type": f.media_type, "size_bytes": int(f.size_bytes or 0)}
                    for f in files if f.id not in (matched_a | matched_b)
                ]

            unmatched_a = _unmatched(q.get_files_by_unit(session, dr.unit_a_id))
            unmatched_b = _unmatched(q.get_files_by_unit(session, dr.unit_b_id))

            return {
                "id": dr.id,
                "unit_a": {"id": dr.unit_a_id, "name": unit_a.name if unit_a else ""},
                "unit_b": {"id": dr.unit_b_id, "name": unit_b.name if unit_b else ""},
                "similarity_score": dr.similarity_score,
                "match_count": dr.match_count,
                "match_types": dr.match_types,
                "match_level": dr.match_level,
                "evidence_kind": dr.evidence_kind,
                "face_hint_count": hint_count,
                "total_files_a": dr.total_files_a,
                "total_files_b": dr.total_files_b,
                "overlap_ratio": _overlap_ratio(dr),
                "stale": _is_stale(dr),
                "is_resolved": dr.is_resolved,
                "resolution": dr.resolution,
                "created_at": dr.created_at.isoformat() if dr.created_at else "",
                "unmatched_a": unmatched_a,
                "unmatched_b": unmatched_b,
                "file_matches": [
                    {"id": fm.id, "file_a_id": fm.file_a_id, "file_b_id": fm.file_b_id,
                     "file_a_name": name_map.get(fm.file_a_id, f"#{fm.file_a_id}"),
                     "file_b_name": name_map.get(fm.file_b_id, f"#{fm.file_b_id}"),
                     "similarity_score": fm.similarity_score, "match_type": fm.match_type,
                     # 人脸匹配是"线索"而非重复证据（不计入杰卡德），客户端要能区分
                     "is_hint": fm.match_type == MatchType.FACE.value,
                     # 支持这条线索的帧数：多帧吻合远比单帧偶然相似可信
                     "frame_support": fm.frame_support or 1}
                    for fm in matches
                ],
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/results/{result_id}/resolve", response_model=StatusResponse)
def resolve_dedup(result_id: int, body: DedupResolveRequest):
    """处理查重结果。"""
    try:
        with DatabaseManager.session() as session:
            dr = q.get_dedup_by_id(session, result_id)
            if not dr:
                raise HTTPException(status_code=404, detail=f"查重结果不存在: {result_id}")
            q.resolve_dedup(session, result_id, body.resolution)
            return StatusResponse(success=True, message=f"已标记: {body.resolution}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/results/{result_id}/merge", response_model=DedupMergeResponse)
def merge_dedup_result(result_id: int, body: DedupMergeRequest, request: Request):
    """合并重复单元：保留一侧文件夹，另一侧独有文件移入、匹配文件进回收站。

    与 resolve('merge')（只打标记）不同，本端点是**真正执行文件操作**的入口。
    所有 match_level 均可合并：
      - duplicate ：匹配对 = 内容相同 → 回收站（安全默认）
      - related   ：匹配对 = 画面相似（内容可能不同）→ 回收站，客户端须警示
      - face-only ：无 counted 匹配 → 纯移动，零删除
    部分失败返回 200（success=false + failed 明细），成功项照常落库，可重试。
    """
    from pathlib import Path as _Path

    from app.services.file_ops import OFFLINE_REASON, volume_available, volume_label
    from app.services.merge_service import perform_merge
    from app.utils.constants import ResolutionStatus

    cfg = getattr(request.app.state, "config", None)
    try:
        with DatabaseManager.session() as session:
            dr = q.get_dedup_by_id(session, result_id)
            if not dr:
                raise HTTPException(status_code=404, detail=f"查重结果不存在: {result_id}")
            # resolution='merge' 允许重入（上次收尾失败后的幂等重试）
            if dr.is_resolved and dr.resolution != ResolutionStatus.MERGE.value:
                raise HTTPException(
                    status_code=409,
                    detail=f"该结果已处理（{dr.resolution}），不能执行合并",
                )
            unit_a = q.get_unit_by_id(session, dr.unit_a_id)
            unit_b = q.get_unit_by_id(session, dr.unit_b_id)
            if not unit_a or not unit_b:
                raise HTTPException(status_code=404, detail="单元记录不存在（请先重新扫描）")

            kept = unit_a if body.keep == "a" else unit_b
            other = unit_b if body.keep == "a" else unit_a
            for unit in (unit_a, unit_b):
                if not volume_available(unit.path):
                    raise HTTPException(
                        status_code=409,
                        detail=f"磁盘{OFFLINE_REASON}: {volume_label(unit.path)}"
                               f"（请先连接该磁盘再合并）",
                    )
            if _Path(unit_a.path).anchor != _Path(unit_b.path).anchor:
                raise HTTPException(
                    status_code=409,
                    detail="两个文件夹不在同一磁盘卷，暂不支持跨盘合并（请手动移动文件）",
                )
            if not _Path(kept.path).is_dir():
                raise HTTPException(
                    status_code=409, detail=f"目标文件夹不存在: {kept.path}")
            if not _Path(other.path).is_dir():
                # 重入重试时被并入目录可能已被上次收尾删除（空目录 rmdir）。
                # 只要该单元在库里已无文件，就没什么可移/可删的，直接进收尾；
                # 还有文件却找不到目录才是真异常（被外部删除），拒绝并说明。
                if q.get_files_by_unit(session, other.id):
                    raise HTTPException(
                        status_code=409, detail=f"被并入文件夹已不存在: {other.path}")

            if not acquire_dedup_gate():
                raise HTTPException(
                    status_code=409, detail="已有查重任务正在运行，请稍后再试")
            try:
                outcome = perform_merge(
                    session, dr, unit_a, unit_b, body.keep,
                    set(body.retain_matches or []), cfg,
                )
            finally:
                release_dedup_gate()

            return DedupMergeResponse(
                success=outcome.success,
                message=("合并完成" if outcome.success
                         else f"合并部分完成，{len(outcome.failed)} 个文件失败（可重试）"),
                kept_unit_id=outcome.kept_unit_id,
                merged_unit_id=outcome.merged_unit_id,
                moved_count=outcome.moved_count,
                trashed_count=outcome.trashed_count,
                renamed=outcome.renamed,
                failed=outcome.failed,
                merged_dir_removed=outcome.merged_dir_removed,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/run", status_code=202)
def run_dedup(body: DedupRunRequest, request: Request):
    """触发查重任务（异步）：立即返回 task_id，客户端轮询
    GET /api/dedup/run/{task_id} 获取进度/结果。与 GUI 入口共用互斥门。"""
    cfg = request.app.state.config
    task_id = registry.next_task_id("dedup")
    registry.prune()
    registry.register(task_id, "dedup")

    cfg_fields = {
        "phash_hamming_threshold": cfg.phash_hamming_threshold,
        "dhash_hamming_threshold": cfg.dhash_hamming_threshold,
        # 人脸阈值可被本次请求覆盖（Web 查重页的下拉），不影响其它配置项
        "face_similarity_threshold": (
            body.face_similarity_threshold
            if body.face_similarity_threshold is not None
            else cfg.face_similarity_threshold
        ),
        "face_enabled": cfg.face_detection_enabled,
        "related_min_matches": cfg.related_min_matches,
    }
    threading.Thread(
        target=_run_dedup_task,
        args=(task_id, list(body.unit_ids), float(body.threshold), cfg_fields,
              cfg, bool(body.index_first), bool(body.refine_faces)),
        daemon=True,
        name=f"dedup-task-{task_id}",
    ).start()

    return {"status": "accepted", "task_id": task_id}


# ============================================================
# 人脸重扫（精查候选 / 补扫全部）
# ============================================================

def _run_face_scan_task(task_id: str, scope: str, run_dedup: bool,
                        config) -> None:
    """后台执行人脸重扫；可选地接着跑一次查重（否则结果仍是旧的）。

    与查重共用互斥门：重扫会改动 face_vectors，必须避免与查重读向量并发。
    """
    def update(**kw) -> None:
        registry.update(task_id, **kw)

    update(status="running", started_at=time.time(), phase="faces")
    try:
        if not acquire_dedup_gate():
            update(status="failed",
                   error="已有查重/人脸任务正在运行，请稍后再试",
                   finished_at=time.time())
            return
        try:
            from app.services.index_service import run_face_rescan_pipeline
            rescan = run_face_rescan_pipeline(
                config, scope=scope,
                progress_callback=lambda cur, total: update(progress=[cur, total]),
            )
            dedup_summary = None
            if run_dedup and rescan.scanned:
                with DatabaseManager.session() as session:
                    unit_ids = [u.id for u in q.get_all_active_units(session)]
                if len(unit_ids) >= 2:
                    update(phase="comparing", progress=None)
                    cfg_fields = {
                        "phash_hamming_threshold": config.phash_hamming_threshold,
                        "dhash_hamming_threshold": config.dhash_hamming_threshold,
                        "face_similarity_threshold": config.face_similarity_threshold,
                        "face_enabled": config.face_detection_enabled,
                        "related_min_matches": config.related_min_matches,
                    }
                    outcome = run_dedup_pipeline(
                        unit_ids,
                        threshold=config.jaccard_threshold,
                        phash_hamming_threshold=cfg_fields["phash_hamming_threshold"],
                        dhash_hamming_threshold=cfg_fields["dhash_hamming_threshold"],
                        face_similarity_threshold=cfg_fields["face_similarity_threshold"],
                        face_enabled=cfg_fields["face_enabled"],
                        related_min_matches=cfg_fields["related_min_matches"],
                        load_face=cfg_fields["face_enabled"],
                        progress_callback=lambda cur, total: update(progress=[cur, total]),
                        config=config,
                    )
                    dedup_summary = {
                        "duplicates_found": len(outcome.session.duplicates_found),
                        "related_found": len(outcome.session.related_found),
                        "face_only_found": len(outcome.session.face_only_found),
                        "pruned_stale": outcome.pruned_stale,
                    }
        finally:
            release_dedup_gate()
        update(
            status="completed", phase=None, progress=None,
            finished_at=time.time(),
            result={
                "scope": rescan.scope,
                "scanned": rescan.scanned,
                "faces": rescan.faces,
                "total": rescan.total,
                "dedup": dedup_summary,
            },
        )
    except Exception as e:
        logger.error(f"人脸重扫任务 {task_id} 执行失败: {e}", exc_info=True)
        update(status="failed", error="内部错误，详见服务端日志", finished_at=time.time())


@router.get("/face-scan/status")
def get_face_scan_status(request: Request):
    """人脸重扫待办统计（按钮上显示"约几分钟"的依据）。"""
    try:
        with DatabaseManager.session() as session:
            pending = q.count_videos_needing_face_scan(session, FACE_SCAN_VERSION)
            candidates = q.count_candidate_videos_for_face_rescan(
                session, FACE_SCAN_VERSION)
            videos = int(
                session.query(func.count(MediaFile.id))
                .filter(MediaFile.media_type == "video").scalar() or 0)
            faces = int(
                session.query(func.count(FaceVector.id)).scalar() or 0)
        return {
            "videos_total": videos,
            "videos_pending": pending,
            "candidates": candidates,
            "faces_total": faces,
            "scan_version": FACE_SCAN_VERSION,
        }
    except Exception as e:
        logger.error(f"操作失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")


@router.post("/face-scan", status_code=202)
def run_face_scan(body: DedupFaceScanRequest, request: Request):
    """触发人脸重扫任务（异步）：立即返回 task_id，轮询 /run/{task_id}。"""
    cfg = request.app.state.config
    if not cfg.face_detection_enabled:
        raise HTTPException(status_code=409, detail="人脸识别未启用（config.json）")
    task_id = registry.next_task_id("face")
    registry.prune()
    registry.register(task_id, "face", phase="faces")

    threading.Thread(
        target=_run_face_scan_task,
        args=(task_id, body.scope, bool(body.run_dedup), cfg),
        daemon=True,
        name=f"face-scan-{task_id}",
    ).start()
    return {"status": "accepted", "task_id": task_id}


@router.get("/run/{task_id}")
def get_dedup_task(task_id: str):
    """查询查重任务状态（轮询用）。"""
    registry.prune()
    task = registry.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"查重任务不存在: {task_id}")
    return task


@router.get("/active-task")
def get_active_task():
    """当前仍在排队/运行的查重或人脸任务（没有则 task_id 为 None）。

    客户端据此在"整页刷新 / 换设备 / 刚打开页面"时重新接上进度：
    任务跑在服务端后台线程里，页面丢了 task_id 就只能靠风扇判断在不在跑
    （实测报障：切走再回来进度条消失）。

    只看 dedup/face 两类：HEIC 转换是另一条路由的任务，
    混进来会让查重页误显示"查重中"。
    """
    task = registry.active(DEDUP_TASK_KINDS)
    if not task:
        return {"task_id": None, "status": None, "kind": None}
    return task
