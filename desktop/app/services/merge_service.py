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

级别语义差异（match_level）：
  - duplicate：匹配对 = 内容相同（md5/video）→ 回收站是安全默认
  - related  ：匹配对 = 画面相似（phash/dhash，内容可能不同）→ 回收站，
    客户端 confirm 必须警示；不确定就用「逐文件微调」保留两份
  - face-only：evidence_kind='face'，没有 counted 匹配 → 纯移动零删除
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.db import queries as q
from app.db.models import MediaFile
from app.services.file_ops import move_to_trash
from app.utils.constants import MatchType, ResolutionStatus

logger = logging.getLogger(__name__)


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


def _resolve_dest(session: Session, dest_dir: Path, filename: str) -> str:
    """为移动目标挑选不冲突的文件名：Windows 风格 `stem (2).ext` 递增。

    必须同时校验磁盘与数据库（media_files.path 全局 unique，撞库会
    IntegrityError）：磁盘上已删除但库记录还在的"幽灵"文件也要避开。
    """
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = filename
    n = 2
    while True:
        dest = dest_dir / candidate
        on_disk = os.path.lexists(dest)
        in_db = session.query(MediaFile.id).filter(
            MediaFile.path == str(dest)).first() is not None
        if not on_disk and not in_db:
            return candidate
        candidate = f"{stem} ({n}){suffix}"
        n += 1


def _purge_thumb_cache(file_id: int, unit_path: str, cfg) -> None:
    """删除某文件的缩略图缓存（与 files.py _purge_thumbnail_cache 同一逻辑）。

    只在文件被回收站删除时需要（file_id 可能被新文件复用，旧缓存会张冠李戴）；
    被移动的文件保留缓存 —— .meta 里源路径过期会让缓存自动 miss 重生成。
    """
    dirs = []
    if cfg is not None and getattr(cfg, "thumbnail_cache_dir", None):
        dirs.append(Path(cfg.thumbnail_cache_dir))
    if unit_path:
        dirs.append(Path(unit_path) / ".thumbnails")
    for d in dirs:
        for suffix in ("", ".meta"):
            target = d / f"{file_id}_thumb.jpg{suffix}"
            try:
                if target.exists():
                    target.unlink()
            except OSError as e:
                logger.warning("清理缩略图缓存失败 %s: %s", target, e)


def perform_merge(session: Session, dr, unit_a, unit_b, keep: str,
                  retain_matches: set, cfg=None) -> MergeOutcome:
    """执行合并。调用方需已完成全部前置校验并持有 dedup_gate。

    参数:
        dr: 查重结果行（dedup_results）。
        unit_a / unit_b: 两侧单元行（ResourceUnit）。
        keep: 'a' | 'b' —— 保留哪一侧的文件夹作为合并目标。
        retain_matches: 「两份都保留」的匹配对中，被并入侧文件 ID 集合。
        cfg: 应用配置（缩略图缓存目录用；None 时跳过集中缓存清理）。

    返回:
        MergeOutcome。failed 为空表示全部成功（收尾已完成）；
        非空时成功项照常生效，但**不**做收尾（单元保持 active 供重试）。
    """
    kept, other = (unit_a, unit_b) if keep == "a" else (unit_b, unit_a)
    outcome = MergeOutcome(kept_unit_id=kept.id, merged_unit_id=other.id)

    # ---- 计算集合（幂等：以 DB 现状为准，已移走/已删除的文件不在其中）----
    matches = q.get_file_matches_for_result(session, dr.id)
    counted = [fm for fm in matches if fm.match_type != MatchType.FACE.value]
    matched_other_ids = (
        {fm.file_b_id for fm in counted} if keep == "a" else {fm.file_a_id for fm in counted}
    )
    trash_ids = matched_other_ids - set(retain_matches)

    other_files = q.get_files_by_unit(session, other.id)
    trash_files = [f for f in other_files if f.id in trash_ids]
    move_files = [f for f in other_files if f.id not in trash_ids]

    # ---- 阶段1：移动（磁盘先动，成功才改库）----
    dest_dir = Path(kept.path)
    for f in move_files:
        original_name = f.filename
        try:
            dest_name = _resolve_dest(session, dest_dir, original_name)
            dest = dest_dir / dest_name
            shutil.move(f.path, str(dest))
            f.path = str(dest)
            f.filename = dest_name
            f.resource_unit_id = kept.id
            f.updated_at = datetime.now()
            outcome.moved_count += 1
            if dest_name != original_name:
                outcome.renamed.append({
                    "file_id": f.id, "original": original_name, "renamed": dest_name,
                })
        except Exception as e:
            logger.error("合并移动失败 file_id=%s: %s", f.id, e, exc_info=True)
            outcome.failed.append({
                "file_id": f.id, "filename": original_name, "reason": f"移动失败: {e}",
            })

    # ---- 阶段2：回收（次序与 _delete_one 相同：先磁盘后库，不降级永久删除）----
    for f in trash_files:
        try:
            ok, reason = move_to_trash(f.path)
            if ok:
                _purge_thumb_cache(f.id, other.path, cfg)
                q.delete_media_file(session, f.id)   # 级联清人脸/帧/文件匹配
                outcome.trashed_count += 1
            else:
                outcome.failed.append({
                    "file_id": f.id, "filename": f.filename, "reason": reason,
                })
        except Exception as e:
            logger.error("合并回收失败 file_id=%s: %s", f.id, e, exc_info=True)
            outcome.failed.append({
                "file_id": f.id, "filename": f.filename, "reason": f"回收失败: {e}",
            })

    # ---- 阶段3：收尾（仅全部成功；部分失败保持现状供幂等重试）----
    if outcome.success:
        other_dir = Path(other.path)
        try:
            # rmdir 只能删空目录：目录里残留非媒体文件时保留，不强删
            if other_dir.is_dir() and not os.listdir(other_dir):
                other_dir.rmdir()
                outcome.merged_dir_removed = True
        except OSError as e:
            logger.warning("删除被并入侧空目录失败 %s: %s", other_dir, e)

        q.mark_unit_merged(session, other.id, kept.id)
        q.resolve_dedup(session, dr.id, ResolutionStatus.MERGE.value)

    # 两种情况都要重算统计（部分失败时另一侧文件数也变了）
    q.refresh_unit_stats(session, [kept.id, other.id])
    # 其它涉及两侧的未处置结果已失真 → 标 stale，由重跑查重自然收敛
    q.mark_dedup_results_stale_for_units(
        session, [kept.id, other.id], exclude_result_id=dr.id)

    return outcome
