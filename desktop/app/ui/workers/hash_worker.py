# -*- coding: utf-8 -*-
"""
哈希工作线程 —— 后台计算未索引文件的哈希值。

遍历所有缺失 MD5/pHash 的文件，调用 HashEngine 批量计算并写入数据库。
"""

import logging
import tempfile
import time
from pathlib import Path
from typing import Optional

from PIL import Image as PILImage
from PySide6.QtCore import QThread, Signal, Slot
from sqlalchemy.exc import OperationalError

from config import AppConfig
from app.core.hash_engine import HashEngine, FileHashes
from app.utils.image_helpers import VideoCapture_unicode
from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)


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
            # 创建哈希引擎
            engine = HashEngine(
                algorithms=list(self._config.hash_algorithms),
                phash_size=self._config.phash_size,
                dhash_size=self._config.dhash_size,
                video_frame_interval=self._config.video_frame_interval_sec,
                face_detection_enabled=self._config.face_detection_enabled,
            )

            # 获取未索引文件
            with DatabaseManager.session() as session:
                unindexed = q.get_unindexed_files(session)
                file_ids = [f.id for f in unindexed]
                file_paths = [Path(f.path) for f in unindexed]
                file_types = [f.media_type for f in unindexed]  # 'image' / 'video'

            total = len(file_ids)
            if total == 0:
                logger.info("无文件需要索引")
                self.finished.emit(0)
                return

            logger.info(f"开始索引 {total} 个文件")
            hashed_count = 0

            for i, (fid, fpath, ftype) in enumerate(zip(file_ids, file_paths, file_types)):
                if self._cancelled:
                    break

                # 跳过已不存在的文件（写入空哈希避免重复处理）
                if not fpath.is_file():
                    try:
                        logger.warning(f"文件不存在，标记为已处理: {fpath}")
                        _retry_db(lambda: self._mark_hashed(fid))
                    except Exception:
                        pass
                    hashed_count += 1
                    self.progress.emit(i + 1, total)
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

                    _retry_db(lambda: self._do_db_write(
                        fid, update_data, result.video_frame_hashes
                    ))

                    # 人脸检测：图片直接检测，视频先抽中间帧再检测
                    if self._config.face_detection_enabled:
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
                        except BaseException as e:
                            logger.warning(f"人脸检测跳过 [{fpath.name}]: {type(e).__name__}: {e}")

                    hashed_count += 1
                    self.file_hashed.emit(fid, "md5" if result.md5 else "phash")

                except Exception as e:
                    logger.error(f"哈希计算失败 [file_id={fid}]: {e}")

                self.progress.emit(i + 1, total)

            self.finished.emit(hashed_count)
            logger.info(f"索引完成: {hashed_count}/{total} 个文件")

        except Exception as e:
            logger.error(f"哈希线程错误: {e}")
            self.error_occurred.emit(str(e))

    @staticmethod
    def _do_db_write(fid: int, update_data: dict, video_frame_hashes) -> None:
        """将哈希更新和视频帧写入合并到一个事务中（减少锁竞争）。"""
        with DatabaseManager.session() as session:
            q.update_file_hash(session, fid, **update_data)
            if video_frame_hashes:
                for ts_ms, ph in video_frame_hashes:
                    q.insert_video_frame(session, fid, ts_ms, ph)

    @staticmethod
    def _mark_hashed(fid: int) -> None:
        """标记文件为已处理（写入空哈希），避免重复检出。"""
        with DatabaseManager.session() as session:
            q.update_file_hash(session, fid, md5_hash="", phash="", dhash="")

    @Slot()
    def cancel(self) -> None:
        """取消哈希任务。"""
        self._cancelled = True
        logger.info("哈希任务已取消")
