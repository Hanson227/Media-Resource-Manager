# -*- coding: utf-8 -*-
"""
图像辅助函数 —— PIL/OpenCV 便捷封装。

提供常用的图像处理操作，统一异常处理。
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)


def open_image_safe(image_path: Path) -> Optional[Image.Image]:
    """安全地打开图片文件，捕获常见错误。

    参数:
        image_path: 图片文件路径。

    返回:
        PIL Image 对象，失败返回 None。
    """
    try:
        img = Image.open(image_path)
        img.load()  # 立即加载以捕获错误
        return img
    except (UnidentifiedImageError, OSError, IOError) as e:
        logger.debug(f"无法打开图片: {image_path} - {e}")
        return None


def get_image_info(image_path: Path) -> dict:
    """获取图片的基本信息。

    参数:
        image_path: 图片文件路径。

    返回:
        {'width': int, 'height': int, 'format': str, 'mode': str}，
        失败返回空 dict。
    """
    try:
        with Image.open(image_path) as img:
            return {
                "width": img.width,
                "height": img.height,
                "format": img.format or "unknown",
                "mode": img.mode,
            }
    except Exception as e:
        logger.debug(f"获取图片信息失败: {image_path} - {e}")
        return {}


def resize_keep_aspect(image: Image.Image, max_size: int) -> Image.Image:
    """等比缩放图片，保持长宽比。

    参数:
        image: PIL Image 对象。
        max_size: 最大边长。

    返回:
        缩放后的新 Image 对象。
    """
    img_copy = image.copy()
    img_copy.thumbnail((max_size, max_size), Image.LANCZOS)
    return img_copy


def convert_to_rgb(image: Image.Image) -> Image.Image:
    """将图片转为 RGB 模式（处理 RGBA、P 等模式）。"""
    if image.mode == "RGBA":
        # 用白色背景填充透明区域
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[3])
        return background
    elif image.mode in ("P", "LA", "L"):
        return image.convert("RGB")
    return image


def _to_opencv_path(file_path: Path) -> str:
    """将路径转为 OpenCV 可读的格式（Windows 上处理中文路径）。

    OpenCV 的 C++ 内部使用 fopen，不支持 Windows 上的 UTF-8 路径。
    通过获取 8.3 短路径名（纯 ASCII）来绕过此限制。

    参数:
        file_path: 原始文件路径。

    返回:
        OpenCV 兼容的路径字符串。
    """
    path_str = str(file_path)
    if not path_str:
        return path_str
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.kernel32.GetShortPathNameW(path_str, buf, 512):
            short_path = buf.value
            if short_path and all(ord(c) < 128 for c in short_path):
                return short_path
    except Exception:
        pass
    return path_str


def imread_unicode(file_path: Path) -> "Optional[np.ndarray]":
    """cv2.imread 的中文路径兼容替代。

    参数:
        file_path: 图片文件路径（可含中文字符）。

    返回:
        BGR 图像数组，失败返回 None。
    """
    import numpy as np
    import cv2
    try:
        img_bytes = np.fromfile(str(file_path), dtype=np.uint8)
        img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
        return img if img is not None else None
    except Exception:
        # 回退：尝试短路径
        try:
            return cv2.imread(_to_opencv_path(file_path))
        except Exception:
            return None


def VideoCapture_unicode(file_path: Path):
    """cv2.VideoCapture 的中文路径兼容替代。

    参数:
        file_path: 视频文件路径（可含中文字符）。

    返回:
        cv2.VideoCapture 对象。
    """
    import cv2
    opencv_path = _to_opencv_path(file_path)
    return cv2.VideoCapture(opencv_path)


def get_video_frame_opencv(video_path: Path, seek_ms: int = 0) -> Optional[Image.Image]:
    """使用 OpenCV 提取视频指定时间点的帧并转为 PIL Image。

    参数:
        video_path: 视频文件路径。
        seek_ms: 要提取的时间点（毫秒），0 表示首帧。

    返回:
        PIL Image 对象，失败返回 None。
    """
    try:
        import cv2
        cap = VideoCapture_unicode(video_path)
        if not cap.isOpened():
            return None

        cap.set(cv2.CAP_PROP_POS_MSEC, seek_ms)
        ret, frame = cap.read()
        cap.release()

        if ret and frame is not None:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb)

        return None
    except Exception as e:
        logger.debug(f"提取视频帧失败: {video_path} - {e}")
        return None
