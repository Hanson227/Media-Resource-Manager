# -*- coding: utf-8 -*-
"""
哈希引擎 —— 协调多种哈希算法的批量计算。

负责：
- 对文件计算 MD5、pHash、dHash
- 对视频提取关键帧并计算帧级 pHash
- 可选的人脸检测与特征向量提取
"""

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np
from PIL import Image

from app.utils.constants import VIDEO_MIN_FRAME_BRIGHTNESS
from app.utils.image_helpers import imread_unicode, VideoCapture_unicode
from app.utils.media_types import is_perceptual_image_extension, is_video_extension
from app.registry.hash_registry import HashAlgorithmRegistry, create_default_registry
from app.core.exceptions import HashComputationError, FaceDetectionError

logger = logging.getLogger(__name__)


# ============================================================
# 不可变结果类型
# ============================================================

@dataclass(frozen=True)
class FileHashes:
    """单个文件的所有哈希计算结果。"""

    file_path: Path
    """文件路径。"""

    md5: Optional[str] = None
    """MD5 哈希值（32位十六进制），None 表示未计算。"""

    phash: Optional[str] = None
    """感知哈希值，None 表示未计算或不可用。"""

    dhash: Optional[str] = None
    """差异哈希值，None 表示未计算或不可用。"""

    video_frame_hashes: Optional[tuple] = None
    """视频帧级 pHash 列表（每帧一个字符串），图片文件为 None。"""

    width: Optional[int] = None
    """图片/视频宽度。"""

    height: Optional[int] = None
    """图片/视频高度。"""

    duration_ms: Optional[int] = None
    """视频时长（毫秒），图片为 None。"""


@dataclass(frozen=True)
class FaceVector:
    """一张检测到的人脸及其特征向量。"""

    face_index: int
    """人脸序号（0=第一张）。"""

    vector: tuple
    """128 维 float 特征向量（Python tuple）。"""

    bbox: tuple = (0, 0, 0, 0)
    """边界框 (x, y, w, h)。"""

    @property
    def vector_bytes(self) -> bytes:
        """将向量打包为 BLOB 存储格式（128 × float32 小端字节序）。"""
        return struct.pack("<128f", *self.vector)

    @classmethod
    def from_bytes(cls, data: bytes, face_index: int = 0,
                   bbox: tuple = (0, 0, 0, 0)) -> "FaceVector":
        """从 BLOB 数据还原 FaceVector。

        参数:
            data: 512 字节的 BLOB 数据。
            face_index: 人脸序号。
            bbox: 边界框。
        """
        vector = struct.unpack("<128f", data)
        return cls(face_index=face_index, vector=tuple(vector), bbox=bbox)


# ============================================================
# 哈希引擎
# ============================================================

class HashEngine:
    """哈希计算协调器。

    管理多个哈希算法，对文件批量计算哈希值。
    支持图片和视频的差异化处理。
    """

    def __init__(
        self,
        algorithms: Sequence[str] = ("md5", "phash", "dhash"),
        registry: Optional[HashAlgorithmRegistry] = None,
        phash_size: int = 8,
        dhash_size: int = 8,
        video_frame_interval: int = 5,
        face_detection_enabled: bool = False,
        model_dir: Optional[Path] = None,
        face_confidence: float = 0.6,
    ) -> None:
        """初始化哈希引擎。

        参数:
            algorithms: 需要计算的哈希算法列表。
            registry: 哈希算法注册器，默认使用预设注册器。
            phash_size: pHash 哈希尺寸。
            dhash_size: dHash 哈希尺寸。
            video_frame_interval: 视频帧提取间隔（秒）。
            face_detection_enabled: 是否启用人脸检测。
            model_dir: 人脸模型目录（Caffe SSD + OpenFace），默认相对当前目录的 models/。
            face_confidence: 人脸检测置信度阈值。
        """
        self._algorithms = algorithms
        self._registry = registry or create_default_registry(phash_size, dhash_size)
        self._video_frame_interval = video_frame_interval
        self._face_enabled = face_detection_enabled
        self._model_dir = Path(model_dir) if model_dir else Path("models")
        self._face_confidence = face_confidence
        self._cancelled = False

        # 延迟导入 OpenCV（仅视频和人脸时需要）
        self._cv2 = None

    def cancel(self) -> None:
        """取消当前哈希计算任务。"""
        self._cancelled = True

    def _reset_cancel(self) -> None:
        """重置取消状态，允许实例复用。"""
        self._cancelled = False

    @property
    def _cv(self):
        """延迟加载 OpenCV。"""
        if self._cv2 is None:
            try:
                import cv2
                self._cv2 = cv2
            except ImportError:
                logger.warning("OpenCV 未安装，视频处理和人脸识别功能不可用")
                self._cv2 = False
        return self._cv2

    # ============================================================
    # 公开方法
    # ============================================================

    def hash_file(self, file_path: Path) -> FileHashes:
        """对单个文件计算所有已配置的哈希值。

        参数:
            file_path: 文件路径。

        返回:
            FileHashes: 包含所有计算结果的不可变对象。
        """
        if not file_path.is_file():
            raise HashComputationError(str(file_path), "all", "文件不存在")

        ext = file_path.suffix.lower()
        # 类型判定统一走 utils.media_types（单一事实源），此处不再维护内联扩展名表
        is_image = is_perceptual_image_extension(ext)
        is_video = is_video_extension(ext)

        # 若扩展名被 config 扩展为媒体类型但既非可哈希图片也非视频（如 RAW），
        # 仅计算 MD5，宽高/感知哈希留空，避免对不可解码文件刷错误日志。
        if not is_image and not is_video:
            logger.debug(f"非感知哈希/视频类型，仅计算 MD5: {file_path}")

        md5_val = None
        phash_val = None
        dhash_val = None
        frame_hashes = None
        width = None
        height = None
        duration_ms = None

        # MD5（所有文件类型）
        if "md5" in self._algorithms:
            try:
                md5_algo = self._registry.get("md5")
                if md5_algo:
                    md5_val = md5_algo.compute(file_path)
            except Exception as e:
                logger.warning(f"MD5 计算失败: {file_path} - {e}")

        # 感知哈希（图片直接计算；视频抽取帧计算）
        if is_image:
            width, height = self._get_image_dimensions(file_path)
            if "phash" in self._algorithms:
                try:
                    phash_algo = self._registry.get("phash")
                    if phash_algo:
                        phash_val = phash_algo.compute(file_path)
                except Exception as e:
                    logger.warning(f"pHash 计算失败: {file_path} - {e}")
            if "dhash" in self._algorithms:
                try:
                    dhash_algo = self._registry.get("dhash")
                    if dhash_algo:
                        dhash_val = dhash_algo.compute(file_path)
                except Exception as e:
                    logger.warning(f"dHash 计算失败: {file_path} - {e}")

        elif is_video:
            video_info = self._extract_video_info(file_path)
            if video_info:
                width, height, duration_ms, frame_hashes = video_info

        return FileHashes(
            file_path=file_path,
            md5=md5_val,
            phash=phash_val,
            dhash=dhash_val,
            video_frame_hashes=frame_hashes,
            width=width,
            height=height,
            duration_ms=duration_ms,
        )

    def hash_batch(
        self,
        file_paths: list[Path],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> list[FileHashes]:
        """批量计算文件哈希值。

        参数:
            file_paths: 文件路径列表。
            progress_callback: 进度回调 (已完成数, 总数)。

        返回:
            FileHashes 结果列表（保持与输入顺序一致）。
        """
        results: list[FileHashes] = []
        total = len(file_paths)

        for i, path in enumerate(file_paths):
            if self._cancelled:
                break
            try:
                result = self.hash_file(path)
                results.append(result)
            except Exception as e:
                logger.error(f"哈希计算失败: {path} - {e}")
                results.append(FileHashes(file_path=path))

            if progress_callback:
                progress_callback(i + 1, total)

        return results

    def extract_video_frames(self, video_path: Path) -> list[tuple[int, str]]:
        """从视频中每隔指定秒数提取一帧，计算各帧的 pHash。

        参数:
            video_path: 视频文件路径。

        返回:
            [(时间戳_毫秒, pHash_十六进制), ...] 列表。
        """
        cv2 = self._cv
        if not cv2:
            logger.warning("OpenCV 不可用，无法处理视频")
            return []

        frames: list[tuple[int, str]] = []
        cap = None
        phash_algo = self._registry.get("phash")
        if not phash_algo:
            return []

        try:
            cap = VideoCapture_unicode(video_path)
            if not cap.isOpened():
                logger.error(f"无法打开视频: {video_path}")
                return []

            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            if fps <= 0 or total_frames <= 0:
                fps = 30
                total_frames = 1

            duration_ms = int((total_frames / fps) * 1000)
            interval_ms = self._video_frame_interval * 1000

            # 每隔 interval_ms 提取一帧
            extracted = set()
            for ts_ms in range(0, duration_ms, interval_ms):
                if self._cancelled:
                    break
                # 去重（避免提取时间戳过于接近的帧）
                ts_key = ts_ms // 1000
                if ts_key in extracted:
                    continue
                extracted.add(ts_key)

                # Seek 到目标位置
                cap.set(cv2.CAP_PROP_POS_MSEC, ts_ms)
                ret, frame = cap.read()

                if not ret:
                    # 尝试向前微调读取
                    cap.set(cv2.CAP_PROP_POS_MSEC, ts_ms + 200)
                    ret, frame = cap.read()

                if ret and frame is not None:
                    # 跳过黑帧
                    if np.mean(frame) < VIDEO_MIN_FRAME_BRIGHTNESS:
                        continue

                    # 将帧转为 Pillow Image 后计算 pHash
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(rgb)
                    try:
                        ph = phash_algo.compute_from_image(pil_img)
                        frames.append((ts_ms, ph))
                    except Exception as e:
                        logger.debug(f"帧哈希计算失败: {video_path} @ {ts_ms}ms - {e}")

        except Exception as e:
            logger.error(f"视频帧提取失败: {video_path} - {e}")
        finally:
            if cap is not None:
                cap.release()

        return frames

    # ============================================================
    # 人脸识别
    # ============================================================

    # 模型加载状态（类级别缓存）
    _face_detector: Optional[object] = None    # cv2.dnn.Net
    _face_recognizer: Optional[object] = None  # cv2.dnn.Net
    _models_loaded: bool = False
    _models_path: Optional[str] = None
    _models_unavailable: bool = False          # 已确认不可用（缺模型/API 不兼容）→ 不再重试

    @classmethod
    def _load_models(cls, model_dir: Path) -> bool:
        """加载人脸检测和识别模型（类级别缓存，只加载一次）。

        参数:
            model_dir: 模型文件目录。

        返回:
            True 表示加载成功。
        """
        if cls._models_loaded and cls._models_path == str(model_dir):
            return True
        if cls._models_unavailable and cls._models_path == str(model_dir):
            return False  # 已确认不可用，避免逐文件重复告警

        try:
            import cv2
        except ImportError:
            logger.warning("OpenCV 不可用，无法加载人脸模型")
            cls._models_unavailable = True
            cls._models_path = str(model_dir)
            return False

        # 人脸检测模型（Caffe SSD）
        proto_path = model_dir / "deploy.prototxt"
        weights_path = model_dir / "res10_300x300_ssd_iter_140000_fp16.caffemodel"
        if not proto_path.exists() or not weights_path.exists():
            logger.warning(f"人脸检测模型缺失: {model_dir}")
            cls._models_unavailable = True
            cls._models_path = str(model_dir)
            return False

        # OpenCV 5.x 移除了 Caffe/Torch 模型加载 API：必须显式检测，
        # 否则 AttributeError 会逃出 detect_faces 的异常契约，
        # 被 hash_worker 吞成“检测不到人脸”（功能静默失效）。
        if not hasattr(cv2.dnn, "readNetFromCaffe"):
            logger.warning(
                f"OpenCV {getattr(cv2, '__version__', '?')} 缺少 cv2.dnn.readNetFromCaffe"
                "（Caffe 模型加载 API 已在 OpenCV 5 移除），人脸检测不可用；"
                "请安装 opencv-python-headless<5"
            )
            cls._models_unavailable = True
            cls._models_path = str(model_dir)
            return False

        try:
            cls._face_detector = cv2.dnn.readNetFromCaffe(
                str(proto_path), str(weights_path),
            )
            logger.info(f"已加载人脸检测模型: {weights_path}")

            # 人脸特征提取模型（OpenFace nn4.small2）
            openface_path = model_dir / "nn4.small2.v1.t7"
            if openface_path.exists() and hasattr(cv2.dnn, "readNetFromTorch"):
                cls._face_recognizer = cv2.dnn.readNetFromTorch(str(openface_path))
                logger.info(f"已加载人脸特征模型: {openface_path}")
            else:
                if openface_path.exists():
                    logger.warning("OpenCV 缺少 readNetFromTorch，人脸特征提取不可用")
                else:
                    logger.warning("OpenFace 模型缺失，人脸特征提取不可用")
                cls._face_recognizer = None
        except Exception as e:
            logger.warning(f"人脸模型加载失败: {type(e).__name__}: {e}")
            cls._models_unavailable = True
            cls._models_path = str(model_dir)
            return False

        cls._models_loaded = True
        cls._models_path = str(model_dir)
        return True

    def detect_faces(self, image_path: Path) -> list[FaceVector]:
        """检测图片中的人脸，返回特征向量列表。

        使用 Caffe SSD 检测人脸 + OpenFace nn4 提取 128 维特征。

        参数:
            image_path: 图片文件路径。

        返回:
            每张检测到的人脸对应一个 FaceVector。
        """
        if not self._face_enabled:
            return []

        cv2 = self._cv
        if not cv2:
            return []

        model_dir = self._model_dir
        if not self._load_models(model_dir):
            return []

        face_vectors: list[FaceVector] = []
        try:
            # 读取图片（兼容中文路径）
            image = imread_unicode(image_path)
            if image is None:
                return []

            h, w = image.shape[:2]

            # 1. 人脸检测
            blob = cv2.dnn.blobFromImage(
                image, 1.0, (300, 300),
                (104.0, 177.0, 123.0),  # 均值减法
            )
            self._face_detector.setInput(blob)
            detections = self._face_detector.forward()

            face_index = 0
            for i in range(detections.shape[2]):
                confidence = detections[0, 0, i, 2]
                if confidence < self._face_confidence:  # 过滤低置信度（阈值来自配置）
                    continue

                # 边界框
                box = detections[0, 0, i, 3:7] * [w, h, w, h]
                x1, y1, x2, y2 = box.astype("int").tolist()
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w, x2), min(h, y2)

                if x2 <= x1 or y2 <= y1:
                    continue

                # 2. 提取人脸区域并计算特征向量
                face_roi = image[y1:y2, x1:x2]
                if face_roi.size == 0:
                    continue

                if self._face_recognizer is None:
                    logger.debug("人脸特征模型未加载，跳过特征提取")
                    continue

                # OpenFace 需要 96x96 输入
                face_blob = cv2.dnn.blobFromImage(
                    face_roi, 1.0 / 255.0, (96, 96),
                    (0, 0, 0), swapRB=True, crop=False,
                )
                self._face_recognizer.setInput(face_blob)
                embedding = self._face_recognizer.forward()

                vector_128 = tuple(embedding.flatten().tolist()[:128])

                face_vectors.append(FaceVector(
                    face_index=face_index,
                    vector=vector_128,
                    bbox=(x1, y1, x2 - x1, y2 - y1),
                ))
                face_index += 1

            if face_vectors:
                logger.debug(f"检测到 {len(face_vectors)} 张人脸: {image_path.name}")

        except Exception as e:
            logger.error(f"人脸检测失败: {image_path} - {e}")
            raise FaceDetectionError(str(image_path), str(e))

        return face_vectors

    # ============================================================
    # 内部辅助方法
    # ============================================================

    @staticmethod
    def _get_image_dimensions(file_path: Path) -> tuple[Optional[int], Optional[int]]:
        """获取图片尺寸（宽度, 高度）。"""
        try:
            from PIL import Image as PILImage
            with PILImage.open(file_path) as img:
                return img.size
        except Exception:
            return None, None

    def _extract_video_info(self, video_path: Path) -> Optional[tuple]:
        """提取视频信息并计算帧哈希。

        返回:
            (宽度, 高度, 时长毫秒, (帧哈希元组)) 或 None。
        """
        cv2 = self._cv
        if not cv2:
            return None

        cap = None
        try:
            cap = VideoCapture_unicode(video_path)
            if not cap.isOpened():
                return None

            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            if fps <= 0:
                fps = 30
            duration_ms = int((total_frames / fps) * 1000) if fps > 0 else None

            # 提取帧哈希
            frame_hashes = self.extract_video_frames(video_path)

            return width, height, duration_ms, tuple(frame_hashes)

        except Exception as e:
            logger.error(f"视频信息提取失败: {video_path} - {e}")
            return None
        finally:
            if cap is not None:
                cap.release()


