# -*- coding: utf-8 -*-
"""
缩略图生成器 —— 为图片和视频生成缩略图，支持本地缓存。

图片缩略图：Pillow 等比缩放
视频缩略图：OpenCV 提取首帧或中段帧
缓存：存储在各资源单元的 .thumbnails/ 子目录中
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, UnidentifiedImageError

from app.core.exceptions import ThumbnailGenerationError, UnsupportedFormatError
from app.utils.image_helpers import VideoCapture_unicode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailInfo:
    """缩略图生成结果。"""

    source_path: Path
    """原始文件路径。"""

    thumbnail_path: Path
    """生成的缩略图文件路径。"""

    width: int
    """缩略图宽度（像素）。"""

    height: int
    """缩略图高度（像素）。"""

    generation_method: str
    """生成方式：'pillow' / 'opencv_first' / 'opencv_mid'。"""


class ThumbnailGenerator:
    """缩略图生成器，支持磁盘缓存。

    图片：使用 Pillow 等比缩放，保持长宽比
    视频：使用 OpenCV 提取首帧或中点帧

    缓存规则：
    - 缩略图存储在各资源单元的 .thumbnails/ 子目录中
    - 文件命名格式：<file_id>_thumb.<格式>
    - 生成前检查缓存是否存在，避免重复生成
    """

    def __init__(
        self,
        max_size: int = 256,
        cache_subdir: str = ".thumbnails",
        format: str = "jpg",
        quality: int = 80,
    ) -> None:
        """初始化缩略图生成器。

        参数:
            max_size: 缩略图最大边长（像素）。
            cache_subdir: 缓存子目录名。
            format: 输出格式（jpg/png）。
            quality: JPEG 质量（1-100）。
        """
        self._max_size = max_size
        self._cache_subdir = cache_subdir
        self._format = format
        self._quality = quality
        self._cv2 = None  # 延迟加载

    @property
    def _cv(self):
        """延迟加载 OpenCV。"""
        if self._cv2 is None:
            try:
                import cv2
                self._cv2 = cv2
            except ImportError:
                logger.warning("OpenCV 未安装，视频缩略图功能不可用")
                self._cv2 = False
        return self._cv2

    def get_thumbnail_path(self, file_id: int, cache_dir: Path) -> Path:
        """根据文件 ID 预测缩略图缓存路径（不生成）。"""
        cache_path = cache_dir / self._cache_subdir
        return cache_path / f"{file_id}_thumb.{self._format}"

    def exists(self, file_id: int, cache_dir: Path) -> bool:
        """检查缩略图缓存是否已存在。"""
        return self.get_thumbnail_path(file_id, cache_dir).is_file()

    def generate(self, source_path: Path, cache_dir: Path,
                 file_id: Optional[int] = None) -> ThumbnailInfo:
        """为单个文件生成缩略图。

        参数:
            source_path: 原始媒体文件路径。
            cache_dir: 缓存目录（通常为资源单元路径）。
            file_id: 文件 ID，用于缓存文件名（若不提供则从路径生成）。

        返回:
            ThumbnailInfo: 缩略图信息。
        """
        if not source_path.is_file():
            raise ThumbnailGenerationError(str(source_path), "文件不存在")

        # 确定缓存路径
        if file_id is None:
            # 使用 MD5 摘要代替 hash()，避免 PYTHONHASHSEED 随机化导致跨进程不稳定
            file_id = int.from_bytes(
                hashlib.md5(str(source_path).encode("utf-8")).digest()[:8],
                byteorder="big", signed=True,
            )
        cache_path = self.get_thumbnail_path(file_id, cache_dir)

        # 确保缓存目录存在
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # 检查缓存（同时校验源文件匹配）
        if cache_path.is_file() and self._check_cache_valid(cache_path, source_path):
            try:
                with Image.open(cache_path) as im:
                    w, h = im.size
                return ThumbnailInfo(
                    source_path=source_path,
                    thumbnail_path=cache_path,
                    width=w, height=h,
                    generation_method="cache",
                )
            except Exception:
                pass  # 缓存损坏，重新生成

        ext = source_path.suffix.lower()
        _image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif',
                       '.heic', '.heif', '.ico', '.jp2'}
        _raw_exts = {'.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2',
                     '.pef', '.raf', '.3fr', '.x3f'}
        if ext in _image_exts:
            w, h = self._generate_image_thumb(source_path, cache_path)
            method = "pillow"
        elif ext in _raw_exts:
            w, h = self._generate_raw_thumb(source_path, cache_path)
            method = "rawpy"
        elif ext in {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp', '.ts'}:
            w, h = self._generate_video_thumb(source_path, cache_path)
            method = "opencv_mid"
        else:
            raise UnsupportedFormatError(str(source_path), "无法生成缩略图")

        # 缓存元数据写入（生成后才写，确保sidecar与缩略图一致）
        try:
            self._write_cache_meta(cache_path, source_path)
        except Exception as e:
            logger.warning(f"写入缩略图元数据失败: {cache_path} - {e}")

        return ThumbnailInfo(
            source_path=source_path,
            thumbnail_path=cache_path,
            width=w, height=h,
            generation_method=method,
        )

    def generate_batch(
        self,
        source_paths: list[Path],
        cache_dir: Path,
        file_ids: Optional[list[int]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> list[ThumbnailInfo]:
        """批量生成缩略图。

        参数:
            source_paths: 原始文件路径列表。
            cache_dir: 缓存目录。
            file_ids: 文件 ID 列表（与 source_paths 一一对应）。
            progress_callback: 进度回调 (已完成, 总数)。

        返回:
            ThumbnailInfo 列表。
        """
        results: list[ThumbnailInfo] = []
        total = len(source_paths)

        for i, src in enumerate(source_paths):
            fid = file_ids[i] if file_ids else None
            try:
                info = self.generate(src, cache_dir, file_id=fid)
                results.append(info)
            except Exception as e:
                logger.error(f"缩略图生成失败: {src} - {e}")
                # 返回一个占位记录
                dummy_path = self.get_thumbnail_path(fid or 0, cache_dir)
                results.append(ThumbnailInfo(
                    source_path=src,
                    thumbnail_path=dummy_path,
                    width=0, height=0,
                    generation_method="failed",
                ))

            if progress_callback:
                progress_callback(i + 1, total)

        return results

    # ============================================================
    # 内部方法
    # ============================================================

    @staticmethod
    def _get_meta_path(thumbnail_path: Path) -> Path:
        """返回缓存元数据文件路径（同目录，扩展名附加 .meta）。"""
        return thumbnail_path.with_name(thumbnail_path.name + ".meta")

    @staticmethod
    def _check_cache_valid(thumbnail_path: Path, source_path: Path) -> bool:
        """校验缓存是否与源文件匹配（通过 sidecar meta 对比 mtime 和 size）。"""
        meta_path = ThumbnailGenerator._get_meta_path(thumbnail_path)
        if not meta_path.is_file():
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            st = source_path.stat()
            return (
                meta.get("source_path") == str(source_path)
                and meta.get("source_mtime") == st.st_mtime
                and meta.get("source_size") == st.st_size
            )
        except Exception:
            return False

    @staticmethod
    def _write_cache_meta(thumbnail_path: Path, source_path: Path) -> None:
        """写入缓存元数据 sidecar，记录源文件路径、mtime 和 size。"""
        st = source_path.stat()
        meta = {
            "source_path": str(source_path),
            "source_mtime": st.st_mtime,
            "source_size": st.st_size,
        }
        meta_path = ThumbnailGenerator._get_meta_path(thumbnail_path)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _generate_image_thumb(self, source: Path, dest: Path) -> tuple[int, int]:
        """生成图片缩略图（Pillow）。"""
        try:
            with Image.open(source) as img:
                # 移除 EXIF 方向信息以避免旋转问题
                img = self._fix_orientation(img)
                # 转为 RGB（确保 JPEG 兼容）
                if img.mode in ("RGBA", "P", "LA"):
                    img = img.convert("RGB")
                # 等比缩放
                img.thumbnail((self._max_size, self._max_size), Image.LANCZOS)
                # 保存结果
                save_kwargs = {}
                if self._format.lower() == "jpg":
                    save_kwargs["quality"] = self._quality
                    save_kwargs["optimize"] = True
                # 确定 PIL 保存格式（jpg → JPEG）
                pil_format = "JPEG" if self._format.lower() == "jpg" else self._format.upper()
                img.save(dest, format=pil_format, **save_kwargs)
                return img.size
        except Exception as e:
            raise ThumbnailGenerationError(str(source), str(e))

    def _generate_raw_thumb(self, source: Path, dest: Path) -> tuple[int, int]:
        """生成 RAW 图片缩略图（rawpy → Pillow）。"""
        try:
            import rawpy
            import numpy as np
            with rawpy.imread(str(source)) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=True,
                    no_auto_bright=True,
                    output_bps=8,
                )
            img = Image.fromarray(rgb)
            img.thumbnail((self._max_size, self._max_size), Image.LANCZOS)
            save_kwargs = {}
            if self._format.lower() == "jpg":
                save_kwargs["quality"] = self._quality
                save_kwargs["optimize"] = True
            pil_format = "JPEG" if self._format.lower() == "jpg" else self._format.upper()
            img.save(dest, format=pil_format, **save_kwargs)
            return img.size
        except Exception as e:
            raise ThumbnailGenerationError(str(source), str(e))

    def _generate_video_thumb(self, source: Path, dest: Path) -> tuple[int, int]:
        """生成视频缩略图（OpenCV）。

        策略：尝试提取总时长的 30% 处帧（代表内容），
        若失败则提取第 1 秒处帧。
        """
        cv2 = self._cv
        if not cv2:
            raise ThumbnailGenerationError(str(source), "OpenCV 不可用")

        cap = None
        try:
            cap = VideoCapture_unicode(source)
            if not cap.isOpened():
                raise ThumbnailGenerationError(str(source), "无法打开视频")

            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            if fps <= 0:
                fps = 30

            # 尝试 30% 位置
            target_frame = int(total_frames * 0.3)
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ret, frame = cap.read()

            # 回退到第 1 秒
            if not ret or frame is None or frame.mean() < 10:
                cap.set(cv2.CAP_PROP_POS_MSEC, 1000)
                ret, frame = cap.read()

            # 再回退到首帧
            if not ret or frame is None or frame.mean() < 10:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ret, frame = cap.read()

            if not ret or frame is None:
                raise ThumbnailGenerationError(str(source), "无法读取任何帧")

            # 转为 RGB 后使用 Pillow 缩放保存
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            pil_img.thumbnail((self._max_size, self._max_size), Image.LANCZOS)

            save_kwargs = {}
            pil_format = "JPEG" if self._format.lower() == "jpg" else self._format.upper()
            if self._format.lower() == "jpg":
                save_kwargs["quality"] = self._quality
                save_kwargs["optimize"] = True
            pil_img.save(dest, format=pil_format, **save_kwargs)
            return pil_img.size

        except ThumbnailGenerationError:
            raise
        except Exception as e:
            raise ThumbnailGenerationError(str(source), str(e))
        finally:
            if cap is not None:
                cap.release()

    @staticmethod
    def _fix_orientation(img: Image.Image) -> Image.Image:
        """根据 EXIF 方向信息修正图片旋转。"""
        try:
            exif = img._getexif()
            if exif is None:
                return img
            orientation = exif.get(0x0112)  # EXIF Orientation tag
            if orientation is None:
                return img

            # 根据 EXIF 方向值进行旋转/翻转
            if orientation == 3:
                return img.rotate(180, expand=True)
            elif orientation == 6:
                return img.rotate(270, expand=True)
            elif orientation == 8:
                return img.rotate(90, expand=True)
        except Exception:
            pass
        return img
