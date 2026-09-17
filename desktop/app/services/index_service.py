# -*- coding: utf-8 -*-
"""
索引管线服务 —— 为未索引文件计算哈希（MD5/pHash/dHash + 视频帧 + 人脸）。

此前这段逻辑只存在于 GUI 的 HashWorker（QThread）里，HTTP API 想"先索引再查重"
就只能重写一遍 —— 与 dedup_service 收敛前的分叉是同一类问题。本模块把它
抽成不依赖 Qt 的实现，GUI 线程与 API 后台任务共用：

    run_index_pipeline(config, progress_callback=..., file_hashed_callback=...)

一键查重的语义（见 CLAUDE.md「Dedup Pipeline」第 1 步）是"先索引再比对"：
不索引就比对，未索引文件对查重完全不可见，却会报告"查重完成"。
"""

import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image as PILImage
from sqlalchemy.exc import OperationalError

from app.core.hash_engine import HashEngine
from app.utils.image_helpers import VideoCapture_unicode
from app.utils.constants import FACE_SCAN_VERSION, FACE_VIDEO_MAX_FRAMES
from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)

# 每批从数据库取多少个待索引文件。
# get_unindexed_files 有 limit 参数，若只取一批就收工，未索引文件超过该值时
# 索引会"看起来完成"（日志打印 1000/1000）而实际还剩一堆没算 —— 一键查重会
# 拿着残缺数据出结论。因此必须循环取批直到取空。
INDEX_BATCH_SIZE = 1000


@dataclass(frozen=True)
class IndexRunResult:
    """一次索引管线运行的结果。"""

    hashed_count: int
    """已处理（含失败占位）的文件数。"""

    total: int
    """开始时的待索引文件总数。"""

    cancelled: bool = False
    """是否被取消。"""


def _detect_faces_for_file(engine: HashEngine, file_path: Path, media_type: str,
                           config=None):
    """对文件执行人脸检测。图片直接检测（全图），视频按定间隔多帧检测。

    参数:
        engine: 哈希引擎。
        file_path: 文件路径。
        media_type: 'image' 或 'video'。
        config: 应用配置（读视频抽帧间隔与抽帧上限）；None 时取默认值。

    返回:
        FaceVector 列表。

    说明:
        视频原先只取"中间那一帧"：抽到哪一帧取决于片长、正脸不在中点就整片漏检
        （实测 1142 个视频只有 198 个检出人脸），而且结果不可复现。
        现改为等间隔、均匀铺满全片、最多 face_video_max_frames 帧。
    """
    if media_type == "image":
        return engine.detect_faces(file_path)

    interval = int(getattr(config, "video_frame_interval_sec", 5) or 5)
    max_frames = int(getattr(config, "face_video_max_frames",
                             FACE_VIDEO_MAX_FRAMES) or FACE_VIDEO_MAX_FRAMES)
    return engine.extract_video_faces(
        file_path, interval_sec=interval, max_frames=max_frames)


def _retry_db(func, max_retries=3):
    """遇到 database locked 时自动重试（指数退避）。"""
    for attempt in range(max_retries):
        try:
            return func()
        except OperationalError as e:
            msg = str(e)
            if "locked" in msg.lower() and attempt < max_retries - 1:
                wait = (2 ** attempt) * 0.5  # 0.5s, 1s, 2s
                logger.warning(
                    f"数据库繁忙，{wait:.1f}s 后重试（第{attempt+1}/{max_retries}次）"
                )
                time.sleep(wait)
            else:
                raise


def _do_db_write(fid: int, update_data: dict, video_frame_hashes) -> None:
    """将哈希更新和视频帧写入合并到一个事务中（减少锁竞争）。"""
    with DatabaseManager.session() as session:
        q.update_file_hash(session, fid, **update_data)
        if video_frame_hashes:
            for ts_ms, ph in video_frame_hashes:
                q.insert_video_frame(session, fid, ts_ms, ph)


def _mark_hashed(fid: int) -> None:
    """标记文件为已处理（写入空哈希），避免重复检出。"""
    with DatabaseManager.session() as session:
        q.update_file_hash(session, fid, md5_hash="", phash="", dhash="")


@dataclass(frozen=True)
class FaceRescanResult:
    """一次人脸重扫（精查 / 补扫）的结果。"""

    scanned: int
    """实际重扫的视频数。"""

    faces: int
    """写回的人脸向量总数。"""

    total: int
    """开始时的待扫数量。"""

    scope: str = "candidates"
    """'candidates'（只精查出现线索的候选）或 'all'（全部过期视频）。"""

    cancelled: bool = False


def _rescan_faces_for_file(engine: HashEngine, fid: int, fpath: Path, config) -> int:
    """重扫单个视频的人脸：清空旧向量 → 定间隔多帧检测 → 写回并打版本号。

    返回写入的向量条数。

    顺序很重要：**先删后写**，否则新旧向量会叠加（旧的单帧向量留着，
    证据数凭空翻倍）；版本号在写入成功后设置，中途失败的文件保持旧版本，
    下次补扫会重新处理（幂等）。
    """
    interval = int(getattr(config, "video_frame_interval_sec", 5) or 5)
    max_frames = int(getattr(config, "face_video_max_frames",
                             FACE_VIDEO_MAX_FRAMES) or FACE_VIDEO_MAX_FRAMES)
    faces = engine.extract_video_faces(
        fpath, interval_sec=interval, max_frames=max_frames)
    with DatabaseManager.session() as session:
        q.delete_face_vectors_for_file(session, fid)
        for fv in faces:
            q.insert_face_vector(
                session, fid,
                vector_data=fv.vector_bytes,
                face_index=fv.face_index,
                bbox=fv.bbox,
                source_ms=fv.source_ms,
            )
        q.set_face_scan_version(session, fid, FACE_SCAN_VERSION)
    return len(faces)


def run_face_rescan_pipeline(
    config,
    *,
    scope: str = "candidates",
    progress_callback: Optional[Callable[[int, int], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> FaceRescanResult:
    """按新版抽帧策略重扫视频人脸（精查候选 / 补扫全部）。

    参数:
        config: AppConfig（人脸与抽帧相关字段）。
        scope: 'candidates' = 只重扫上一轮出现过人脸线索的候选视频（快，约 2~3 分钟）；
               'all' = 重扫全部抽帧策略过期的视频（慢，本库约十几分钟）。
        progress_callback: (已完成, 总数) 进度回调。
        cancelled: 取消探测。

    返回:
        FaceRescanResult。

    说明:
        只处理视频：图片的人脸检测是全图检测，不受抽帧策略影响
        （迁移时已把图片的 face_scan_version 置为最新）。
    """
    is_cancelled = cancelled or (lambda: False)
    if not getattr(config, "face_detection_enabled", False):
        logger.info("人脸识别未启用，跳过人脸重扫")
        return FaceRescanResult(scanned=0, faces=0, total=0, scope=scope)

    with DatabaseManager.session() as session:
        if scope == "all":
            total = q.count_videos_needing_face_scan(session, FACE_SCAN_VERSION)
        else:
            total = q.count_candidate_videos_for_face_rescan(
                session, FACE_SCAN_VERSION)
    if total == 0:
        logger.info(f"人脸重扫: 无需处理（scope={scope}）")
        return FaceRescanResult(scanned=0, faces=0, total=0, scope=scope)

    logger.info(f"开始人脸重扫: {total} 个视频（scope={scope}）")
    engine = build_hash_engine(config)
    scanned = 0
    faces_total = 0
    seen: set[int] = set()

    # 分批取到取空：精查/补扫都可能比单批上限多（与 run_index_pipeline 同理）
    while not is_cancelled():
        with DatabaseManager.session() as session:
            if scope == "all":
                batch = q.get_videos_needing_face_scan(
                    session, FACE_SCAN_VERSION, limit=INDEX_BATCH_SIZE)
            else:
                batch = q.get_candidate_videos_for_face_rescan(
                    session, FACE_SCAN_VERSION, limit=INDEX_BATCH_SIZE)
        batch = [f for f in batch if f.id not in seen]
        if not batch:
            break

        for f in batch:
            if is_cancelled():
                break
            seen.add(f.id)
            fpath = Path(f.path)
            if not fpath.is_file():
                # 文件不在了：直接打版本号，避免每次补扫都重试同一个幽灵文件
                try:
                    _retry_db(lambda fid=f.id: _mark_face_scan_version(fid))
                except Exception:
                    pass
                scanned += 1
                if progress_callback:
                    progress_callback(min(scanned, total), total)
                continue
            try:
                n = _retry_db(lambda fid=f.id, p=fpath: _rescan_faces_for_file(
                    engine, fid, p, config))
                faces_total += n
            except Exception as e:
                logger.warning(f"人脸重扫失败 [{fpath.name}]: {type(e).__name__}: {e}")
            scanned += 1
            if progress_callback:
                progress_callback(min(scanned, total), total)

    was_cancelled = is_cancelled()
    logger.info(f"人脸重扫完成: {scanned}/{total} 个视频，共 {faces_total} 条人脸向量")
    return FaceRescanResult(scanned=scanned, faces=faces_total, total=total,
                            scope=scope, cancelled=was_cancelled)


def _mark_face_scan_version(fid: int) -> None:
    """只更新抽帧版本号（文件不存在时的收尾，避免反复重试）。"""
    with DatabaseManager.session() as session:
        q.set_face_scan_version(session, fid, FACE_SCAN_VERSION)


def build_hash_engine(config) -> HashEngine:
    """按配置构造哈希引擎（GUI 与 API 同一套参数）。"""
    return HashEngine(
        algorithms=list(config.hash_algorithms),
        phash_size=config.phash_size,
        dhash_size=config.dhash_size,
        video_frame_interval=config.video_frame_interval_sec,
        face_detection_enabled=config.face_detection_enabled,
        model_dir=config.face_model_dir,
        face_confidence=config.face_confidence_threshold,
    )


def run_index_pipeline(
    config,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    file_hashed_callback: Optional[Callable[[int, str], None]] = None,
    cancelled: Optional[Callable[[], bool]] = None,
) -> IndexRunResult:
    """计算全部未索引文件的哈希并写入数据库。

    参数:
        config: AppConfig（哈希/人脸相关字段）。
        progress_callback: (已完成, 总数) 进度回调。
        file_hashed_callback: (file_id, hash_type) 单文件完成回调。
        cancelled: 取消探测，返回 True 时停止取下一批。

    返回:
        IndexRunResult。

    异常:
        仅在数据库/引擎无法构造等致命情况下抛出（调用方决定如何提示）。
       单个文件的哈希失败只记日志，不影响其余文件。
    """
    is_cancelled = cancelled or (lambda: False)
    engine = build_hash_engine(config)

    # 先取总数用于进度分母
    with DatabaseManager.session() as session:
        total = q.get_unindexed_file_count(session)

    if total == 0:
        logger.info("无文件需要索引")
        return IndexRunResult(hashed_count=0, total=0)

    logger.info(f"开始索引 {total} 个文件")
    hashed_count = 0
    seen: set[int] = set()

    # 分批取直到取空。单次 get_unindexed_files 有 limit，只取一批会让
    # 未索引文件多于该值时"提前收工"，而一键查重紧接着就会拿这份残缺
    # 数据出结论（用户看到"查重完成"却不知还有文件没算）。
    while not is_cancelled():
        with DatabaseManager.session() as session:
            batch = [
                (f.id, Path(f.path), f.media_type)
                for f in q.get_unindexed_files(session, limit=INDEX_BATCH_SIZE)
            ]
        # 跳过本轮已尝试过的（计算失败的文件不会被写入占位，会反复被查到）
        batch = [b for b in batch if b[0] not in seen]
        if not batch:
            break

        for fid, fpath, ftype in batch:
            if is_cancelled():
                break
            seen.add(fid)

            # 跳过已不存在的文件（写入空哈希避免重复处理）
            if not fpath.is_file():
                try:
                    logger.warning(f"文件不存在，标记为已处理: {fpath}")
                    _retry_db(lambda: _mark_hashed(fid))
                except Exception:
                    pass
                hashed_count += 1
                if progress_callback:
                    progress_callback(min(hashed_count, total), total)
                continue

            try:
                # 计算哈希
                result = engine.hash_file(fpath)

                # 写入数据库：始终写入 md5/phash/dhash，
                # 失败时写 "" 而非 None，避免被 get_unindexed_files 反复检出
                update_data = {}
                update_data["md5_hash"] = result.md5 or ""
                update_data["phash"] = result.phash or ""
                update_data["dhash"] = result.dhash or ""
                if result.width:
                    update_data["width"] = result.width
                if result.height:
                    update_data["height"] = result.height
                if result.duration_ms:
                    update_data["duration_ms"] = result.duration_ms

                _retry_db(lambda: _do_db_write(
                    fid, update_data, result.video_frame_hashes
                ))

                # 人脸检测：图片全图检测，视频定间隔多帧检测
                if config.face_detection_enabled:
                    try:
                        face_vectors = _detect_faces_for_file(
                            engine, fpath, ftype, config)
                        with DatabaseManager.session() as session:
                            for fv in face_vectors:
                                q.insert_face_vector(
                                    session, fid,
                                    vector_data=fv.vector_bytes,
                                    face_index=fv.face_index,
                                    bbox=fv.bbox,
                                    source_ms=fv.source_ms,
                                )
                            # 无论是否检出人脸都要打版本号：视频里没有人脸也是
                            # 一次有效扫描，否则补扫任务会把它们反复扫一遍。
                            q.set_face_scan_version(session, fid, FACE_SCAN_VERSION)
                        if face_vectors:
                            logger.debug(
                                f"人脸检测: {fpath.name} → {len(face_vectors)} 张人脸"
                            )
                        else:
                            logger.debug(f"人脸检测: {fpath.name} → 未检测到人脸")
                    except Exception as e:
                        logger.warning(f"人脸检测跳过 [{fpath.name}]: {type(e).__name__}: {e}")

                hashed_count += 1
                if file_hashed_callback:
                    file_hashed_callback(fid, "md5" if result.md5 else "phash")

            except Exception as e:
                logger.error(f"哈希计算失败 [file_id={fid}]: {e}")

            if progress_callback:
                progress_callback(min(hashed_count, total), total)

    was_cancelled = is_cancelled()
    logger.info(f"索引完成: {hashed_count}/{total} 个文件")
    return IndexRunResult(hashed_count=hashed_count, total=total,
                          cancelled=was_cancelled)
