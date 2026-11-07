# -*- coding: utf-8 -*-
"""
单元合并服务 —— 查重「合并重复单元」动作的唯一实现。

语义（keep='a' 为例，keep='b' 对称交换）：
  1. 被并入侧与本结果匹配（内容相同 / 画面相似）的文件 → 移至回收站；
     `retain_matches` 里的例外**不删除**，改为同样移动到保留侧（用户想两份都保留）。
  2. 被并入侧其余（独有）文件 → 物理移动到保留侧文件夹，同名冲突自动改名。
  3. 收尾：被并入目录若已空则删除空目录；被并入单元标记 merged（parent=保留侧）；
     本查重结果 resolution='merge'；两侧单元统计重算；
     其它涉及两侧的未处置查重结果标 stale（computed_version=0，由重跑查重收敛）。

失败哲学与 batch-delete 一致：每文件先磁盘后库、部分失败不回滚、
失败明细逐条返回、**幂等可重试**（重试时按 DB 现状重算集合，
已移走/已删除的文件自然不在集合里）。

事务边界（2026-11 修复）：
  旧实现由 API 路由开一个 `with DatabaseManager.session()` 包住整个合并，
  磁盘 `shutil.move` / 回收站操作都在写事务里，几百个文件会长时间锁库，
  且异常时 DB 回滚而磁盘已动，造成幽灵记录与幂等失效。
  现在拆成：
    - `build_merge_plan()`：短读事务，把要移动/回收的文件快照成普通 dict；
    - `execute_merge_plan()`：磁盘操作在事务外，每个文件“磁盘→短事务提交”；
    - 收尾也走独立短事务。
  路由不再包事务；旧 `perform_merge(session, ...)` 保留为测试兼容包装。
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import queries as q
from app.db.engine import DatabaseManager
from app.db.models import MediaFile
from app.services.cleanup_service import CleanupService
from app.services.file_ops import (
    OFFLINE_REASON,
    move_to_trash,
    volume_available,
    volume_label,
)
from app.utils.constants import MatchType, ResolutionStatus

logger = logging.getLogger(__name__)


class MergeError(Exception):
    """合并前置校验失败；路由据此映射 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class MergeOutcome:
    """合并结果（failed 非空 = 部分失败，成功项照常落库，可重试）。"""
    kept_unit_id: int
    merged_unit_id: int
    moved_count: int = 0
    trashed_count: int = 0
    renamed: list = field(default_factory=list)   # [{"file_id","original","renamed"}]
    failed: list = field(default_factory=list)    # [{"file_id","filename","reason"}]
    merged_dir_removed: bool = False

    @property
    def success(self) -> bool:
        return not self.failed


@dataclass(frozen=True)
class MergeFile:
    """参与合并的单个文件快照（不持有 ORM / Session）。"""
    file_id: int
    path: str
    filename: str


@dataclass(frozen=True)
class MergePlan:
    """一次合并的完整快照；所有字段都是普通值，可安全跨事务使用。"""
    result_id: int
    kept_unit_id: int
    merged_unit_id: int
    kept_path: str
    merged_path: str
    move_files: tuple[MergeFile, ...]
    trash_files: tuple[MergeFile, ...]


def _resolve_dest_name(dest_dir: Path, filename: str) -> str:
    """为移动目标挑选不冲突的文件名：Windows 风格 `stem (2).ext` 递增。

    必须同时校验磁盘与数据库（media_files.path 全局 unique，撞库会
    IntegrityError）：磁盘上已删除但库记录还在的"幽灵"文件也要避开。
    每次只开一个短读事务，返回候选名后立刻关闭，绝不把查询事务带到磁盘操作里。
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = filename
    n = 2
    while True:
        dest = dest_dir / candidate
        on_disk = os.path.lexists(dest)
        with DatabaseManager.session() as session:
            in_db = session.query(MediaFile.id).filter(
                MediaFile.path == str(dest)).first() is not None
        if not on_disk and not in_db:
            return candidate
        candidate = f"{stem} ({n}){suffix}"
        n += 1


def build_merge_plan(result_id: int, keep: str,
                     retain_matches: set[int]) -> MergePlan:
    """在短读事务内完成全部前置校验并生成不可变计划。

    抛 MergeError（带 HTTP 状态码）表示不能合并；调用方据此返回 4xx。
    """
    if keep not in ("a", "b"):
        raise MergeError(422, "keep 只能是 'a' 或 'b'")

    with DatabaseManager.session() as session:
        dr = q.get_dedup_by_id(session, result_id)
        if not dr:
            raise MergeError(404, f"查重结果不存在: {result_id}")
        # resolution='merge' 允许重入（上次收尾失败后的幂等重试）
        if dr.is_resolved and dr.resolution != ResolutionStatus.MERGE.value:
            raise MergeError(
                409, f"该结果已处理（{dr.resolution}），不能执行合并")

        unit_a = q.get_unit_by_id(session, dr.unit_a_id)
        unit_b = q.get_unit_by_id(session, dr.unit_b_id)
        if not unit_a or not unit_b:
            raise MergeError(404, "单元记录不存在（请先重新扫描）")

        kept = unit_a if keep == "a" else unit_b
        other = unit_b if keep == "a" else unit_a
        for unit in (unit_a, unit_b):
            if not volume_available(unit.path):
                raise MergeError(
                    409,
                    f"磁盘{OFFLINE_REASON}: {volume_label(unit.path)}"
                    f"（请先连接该磁盘再合并）",
                )
        if Path(unit_a.path).anchor != Path(unit_b.path).anchor:
            raise MergeError(
                409, "两个文件夹不在同一磁盘卷，暂不支持跨盘合并（请手动移动文件）")

        if not Path(kept.path).is_dir():
            raise MergeError(409, f"目标文件夹不存在: {kept.path}")

        other_files = q.get_files_by_unit(session, other.id)
        if not Path(other.path).is_dir():
            # 重入重试时被并入目录可能已被上次收尾删除（空目录 rmdir）。
            # 只要该单元在库里已无文件，就没什么可移/可删的，直接进收尾；
            # 还有文件却找不到目录才是真异常（被外部删除），拒绝并说明。
            if other_files:
                raise MergeError(409, f"被并入文件夹已不存在: {other.path}")

        matches = q.get_file_matches_for_result(session, dr.id)
        counted = [fm for fm in matches if fm.match_type != MatchType.FACE.value]
        matched_other_ids = (
            {fm.file_b_id for fm in counted} if keep == "a"
            else {fm.file_a_id for fm in counted}
        )
        trash_ids = matched_other_ids - set(retain_matches)

        trash_files = [
            MergeFile(f.id, f.path, f.filename)
            for f in other_files if f.id in trash_ids
        ]
        move_files = [
            MergeFile(f.id, f.path, f.filename)
            for f in other_files if f.id not in trash_ids
        ]

        return MergePlan(
            result_id=dr.id,
            kept_unit_id=kept.id,
            merged_unit_id=other.id,
            kept_path=kept.path,
            merged_path=other.path,
            move_files=tuple(move_files),
            trash_files=tuple(trash_files),
        )


def _commit_moved_file(file: MergeFile, dest: Path, dest_name: str,
                       kept_unit_id: int) -> None:
    """一个短事务：把已落盘的移动结果写进库。"""
    with DatabaseManager.session() as session:
        row = session.get(MediaFile, file.file_id)
        if row is None:
            raise RuntimeError("文件记录不存在")
        row.path = str(dest)
        row.filename = dest_name
        row.resource_unit_id = kept_unit_id
        row.updated_at = datetime.now()


def _commit_trashed_file(file_id: int) -> None:
    """一个短事务：回收站成功后删库记录（级联清人脸/帧/文件匹配）。"""
    with DatabaseManager.session() as session:
        if not q.delete_media_file(session, file_id):
            raise RuntimeError("文件记录不存在")


def _refresh_after_merge(plan: MergePlan) -> None:
    """短事务：重算两侧统计 + 标 stale。失败只记日志，不回滚已完成的磁盘操作。"""
    try:
        with DatabaseManager.session() as session:
            q.refresh_unit_stats(session, [plan.kept_unit_id, plan.merged_unit_id])
            q.mark_dedup_results_stale_for_units(
                session,
                [plan.kept_unit_id, plan.merged_unit_id],
                exclude_result_id=plan.result_id,
            )
    except Exception as e:
        logger.error("合并后重算统计/标 stale 失败: %s", e, exc_info=True)


def _finalize(plan: MergePlan) -> tuple[bool, str]:
    """收尾：删空目录 + 标 merged/resolve，走独立短事务。

    返回 (merged_dir_removed, error_message)。DB 收尾失败时返回错误，
    文件移动结果已经落库，调用方可重试收尾。
    """
    merged_dir_removed = False
    other_dir = Path(plan.merged_path)
    try:
        # rmdir 只能删空目录：目录里残留非媒体文件时保留，不强删
        if other_dir.is_dir() and not os.listdir(other_dir):
            other_dir.rmdir()
            merged_dir_removed = True
    except OSError as e:
        logger.warning("删除被并入侧空目录失败 %s: %s", other_dir, e)

    try:
        with DatabaseManager.session() as session:
            q.mark_unit_merged(session, plan.merged_unit_id, plan.kept_unit_id)
            q.resolve_dedup(session, plan.result_id, ResolutionStatus.MERGE.value)
    except Exception as e:
        logger.error("合并收尾写库失败: %s", e, exc_info=True)
        return merged_dir_removed, f"收尾失败: {e}"
    return merged_dir_removed, ""


def execute_merge_plan(plan: MergePlan, cfg=None) -> MergeOutcome:
    """执行合并计划：磁盘操作在 DB 事务外，每个文件完成后立即短事务提交。"""
    outcome = MergeOutcome(
        kept_unit_id=plan.kept_unit_id,
        merged_unit_id=plan.merged_unit_id,
    )
    dest_dir = Path(plan.kept_path)

    # ---- 阶段1：移动（磁盘先动，成功才改库；每文件一个短事务）----
    for f in plan.move_files:
        original_name = f.filename
        src = Path(f.path)
        try:
            dest_name = _resolve_dest_name(dest_dir, original_name)
            dest = dest_dir / dest_name
            shutil.move(str(src), str(dest))
            try:
                _commit_moved_file(f, dest, dest_name, plan.kept_unit_id)
            except Exception:
                # 库没写进去就把文件搬回去，避免"磁盘已动、库没动"
                try:
                    shutil.move(str(dest), str(src))
                except Exception as rollback_error:
                    logger.error(
                        "合并库写入失败且回滚磁盘失败 file_id=%s: %s",
                        f.file_id, rollback_error, exc_info=True)
                raise
            outcome.moved_count += 1
            if dest_name != original_name:
                outcome.renamed.append({
                    "file_id": f.file_id, "original": original_name,
                    "renamed": dest_name,
                })
        except Exception as e:
            logger.error("合并移动失败 file_id=%s: %s", f.file_id, e, exc_info=True)
            outcome.failed.append({
                "file_id": f.file_id, "filename": original_name,
                "reason": f"移动失败: {e}",
            })

    # ---- 阶段2：回收（先磁盘后库，不降级永久删除；每文件一个短事务）----
    for f in plan.trash_files:
        try:
            ok, reason = move_to_trash(f.path)
            if not ok:
                outcome.failed.append({
                    "file_id": f.file_id, "filename": f.filename, "reason": reason,
                })
                continue
            try:
                _commit_trashed_file(f.file_id)
            except Exception as e:
                # 文件已在回收站、库没删掉：记录失败，重试时会走"磁盘已不存在"幂等分支
                logger.error("合并回收后删库失败 file_id=%s: %s",
                             f.file_id, e, exc_info=True)
                outcome.failed.append({
                    "file_id": f.file_id, "filename": f.filename,
                    "reason": f"回收后删库失败: {e}",
                })
                continue
            CleanupService.remove_thumbnails_for_files(
                [f.file_id],
                cache_dir=(getattr(cfg, "thumbnail_cache_dir", None)
                           if cfg is not None else None),
                unit_path=plan.merged_path,
            )
            outcome.trashed_count += 1
        except Exception as e:
            logger.error("合并回收失败 file_id=%s: %s", f.file_id, e, exc_info=True)
            outcome.failed.append({
                "file_id": f.file_id, "filename": f.filename,
                "reason": f"回收失败: {e}",
            })

    # ---- 阶段3：收尾（仅全部成功；部分失败保持现状供幂等重试）----
    if outcome.success:
        removed, error = _finalize(plan)
        outcome.merged_dir_removed = removed
        if error:
            outcome.failed.append({
                "file_id": 0, "filename": "", "reason": error,
            })

    # 两种情况都要重算统计（部分失败时另一侧文件数也变了）
    _refresh_after_merge(plan)
    return outcome


def perform_merge_by_ids(result_id: int, keep: str,
                         retain_matches: set[int], cfg=None) -> MergeOutcome:
    """合并的唯一入口：短读事务建计划 → 事务外执行 → 短事务收尾。"""
    plan = build_merge_plan(result_id, keep, retain_matches)
    return execute_merge_plan(plan, cfg)


def perform_merge(session: Session, dr, unit_a, unit_b, keep: str,
                  retain_matches: set[int], cfg=None) -> MergeOutcome:
    """兼容旧调用（测试）：仅用传入对象的 id 重新建计划并安全执行。

    注意：不再使用传入 session 做任何写操作；调用方的长事务不会包住磁盘操作。
    """
    # 入参保留是为了不破坏既有测试/调用点；实际执行以 DB 现状为准。
    _ = (session, unit_a, unit_b)
    return perform_merge_by_ids(dr.id, keep, retain_matches, cfg)
