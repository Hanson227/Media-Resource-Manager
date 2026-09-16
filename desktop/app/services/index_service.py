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


def _detect_faces_for_file(engine: HashEngine, file_path: Path, media_type: str):
    """对文件执行人脸检测。图片直接检测，视频先抽帧。

    参数:
        engine: 哈希引擎。
        file_path: 文件路径。
        media_type: 'image' 或 'video'。

    返回:
        FaceVector 列表。
    """
    if media_type == "image":
        return engine.detect_faces(file_path)

    # 视频：提取中间帧进行人脸检测
    try:
        import cv2
    except ImportError:
        return []

    cap = None
    tmp_path = None
    try:
        cap = VideoCapture_unicode(file_path)
        if not cap.isOpened():
            return []

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            return []

        # 取中间帧
        mid = total_frames // 2
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
        ret, frame = cap.read()

        if not ret or frame is None:
            # 回退到首帧
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = cap.read()

        if not ret or frame is None:
            return []

        # 保存为临时图片文件
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil_img = PILImage.fromarray(rgb)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="vface_")
        os.close(tmp_fd)  # 立即关闭 FD，PIL 会用自己打开的 FD 写入
        pil_img.save(tmp_path, format="JPEG", quality=85)

        return engine.detect_faces(Path(tmp_path))

    except Exception:
        return []
    finally:
        if cap is not None:
            cap.release()
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


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

                # 人脸检测：图片直接检测，视频先抽中间帧再检测
                if config.face_detection_enabled:
                    try:
                        face_vectors = _detect_faces_for_file(engine, fpath, ftype)
                        if face_vectors:
                            with DatabaseManager.session() as session:
                                for fv in face_vectors:
                                    q.insert_face_vector(
                                        session, fid,
                                        vector_data=fv.vector_bytes,
                                        face_index=fv.face_index,
                                        bbox=fv.bbox,
                                    )
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
