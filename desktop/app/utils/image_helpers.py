# -*- coding: utf-8 -*-
"""
图像辅助函数 —— PIL/OpenCV 便捷封装。

提供常用的图像处理操作，统一异常处理。
"""

import ctypes
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# HEIC/HEIF 扩展名（与 core/heic_converter.HEIC_EXTENSIONS 同口径）
_HEIC_LIKE_EXTENSIONS = frozenset({".heic", ".heif"})

_HEIF_AVAILABLE: Optional[bool] = None
"""pillow-heif 是否可用（None = 还没试过）。"""


def silence_ffmpeg_logs() -> None:
    """关掉 OpenCV(FFmpeg 后端) 的解码日志噪音 —— 幂等，可重复调用。

    为什么不能用 os.environ: Windows 上 `os.environ[k] = v` / `os.putenv` /
    `SetEnvironmentVariableW` 只更新**进程环境块**，而 OpenCV 的
    `opencv_videoio_ffmpeg*.dll` 是用 CRT 的 `getenv` 读 `OPENCV_FFMPEG_LOGLEVEL`
    —— 那是 UCRT 自己缓存的一份环境，上面三种写法都改不到它。实测：三种写法
    解码同一个损坏视频，仍然打 9 行 `[h264 @ ...]`，只有 `_putenv_s` 能静音。

    本库有不少损坏/截断的视频（实测报障：查重时控制台刷满红色 h264 告警），
    逐帧抽帧时这些 libavcodec 告警会把真正的日志淹掉。

    注意: 必须在**第一次 import cv2 之前**调用。本模块导入时会自动调一次，
    而全项目的 cv2 都是延迟导入（函数内 import），所以这是最早且必经的一点。
    """
    for key, value in (("OPENCV_FFMPEG_LOGLEVEL", "-8"),   # AV_LOG_QUIET
                       ("OPENCV_LOG_LEVEL", "OFF")):
        os.environ[key] = value                      # Python 侧也要读得到
        try:
            # UCRT 的 _putenv_s 才会写进 CRT 那份环境（cv2 读的就是它）
            ctypes.CDLL("msvcrt")._putenv_s(
                key.encode("ascii"), value.encode("ascii"))
        except (OSError, AttributeError) as e:       # 非 Windows：os.environ 够了
            logger.debug(f"无法通过 CRT 设置 {key}: {e}")


# 导入即生效：必须在任何 cv2.VideoCapture 之前
silence_ffmpeg_logs()


def ensure_heif_support() -> bool:
    """注册 HEIC/HEIF 的 Pillow 解码器（幂等）。

    为什么必须在这里统一做：pHash/dHash（registry/hash_registry.py）、缩略图
    （core/thumbnail_generator.py）、HEIC 转换（core/heic_converter.py）都走
    `Image.open()`，而 Pillow 原生不认 HEIC —— 只有
    `pillow_heif.register_heif_opener()` 之后才能解码。此前这个注册散落在
    main.py 与 api/routes/files.py，换个入口（索引服务、测试、纯 API 进程）
    就静默降级：实测真实库 40 个 HEIC 里 19 个的 phash/dhash 是失败占位空串、
    width/height 为 NULL、face_vectors 一条都没有。

    `image_helpers` 被 hash_engine / thumbnail_generator / heic_converter 导入，
    且本模块在导入时就调用一次，因此这是最早且必经的一点。

    返回:
        True 表示 HEIC/HEIF 解码可用。
    """
    global _HEIF_AVAILABLE
    if _HEIF_AVAILABLE is not None:
        return _HEIF_AVAILABLE
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _HEIF_AVAILABLE = True
    except Exception as e:  # ImportError / 插件注册异常都算不可用
        logger.warning(f"pillow-heif 不可用，HEIC/HEIF 无法解码: {e}")
        _HEIF_AVAILABLE = False
    return _HEIF_AVAILABLE


# 导入即生效：早于任何 Image.open（pHash/dHash/缩略图/转换都依赖它）
ensure_heif_support()


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
    """将图片转为 RGB 模式（透明区域用白底合成，避免变黑）。"""
    if image.mode in ("RGBA", "LA"):
        # 统一走 RGBA 后用 alpha 通道做白底合成（LA 直接 convert 会丢 alpha 变黑）
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[3])
        return background
    elif image.mode in ("P", "L"):
        return image.convert("RGB")
    return image


def open_image_rgb_oriented(image_path: Path) -> Optional[Image.Image]:
    """打开图片并返回"方向已烘焙进像素"的 RGB 副本；失败返回 None。

    统一走 `ImageOps.exif_transpose` 而不是手工读 `_getexif()`：
    Pillow 12 已移除 `Image.Image._getexif`，旧写法在 `except: pass` 里**静默
    no-op**，带 EXIF 方向的照片（iPhone HEIC/JPEG 几乎必然带）一直是歪的。

    返回的是独立对象（调用方负责 close），源文件句柄在函数内即释放。
    """
    try:
        with Image.open(image_path) as img:
            try:
                transposed = ImageOps.exif_transpose(img)
                if transposed is not None:
                    img = transposed
            except Exception as e:
                # 方向修正失败不该让整张图不可用（照旧返回未旋转的图）
                logger.debug(f"EXIF 方向修正失败 {image_path}: {e}")

            rgb = convert_to_rgb(img)
            if rgb is img:
                rgb = rgb.copy()  # 源句柄即将关闭，必须脱钩
            else:
                rgb.load()
            return rgb
    except Exception as e:
        logger.debug(f"无法解码图片: {image_path} - {e}")
        return None


def read_image_bgr(file_path: Path) -> "Optional[np.ndarray]":
    """读取图片为 OpenCV BGR 数组，HEIC/HEIF 走 PIL 兜底。

    为什么需要兜底：`cv2.imdecode` 不解 HEIC（实测 OpenCV 5.0.0 直接返回 None），
    而人脸检测（core/hash_engine.detect_faces）一直走 imread_unicode ——
    于是整个库的 HEIC 人脸向量永远是 0 条（实测 40 个 HEIC / 0 条向量）。

    参数:
        file_path: 图片文件路径。

    返回:
        BGR ndarray；无法解码返回 None（不抛异常）。
    """
    img = imread_unicode(file_path)          # 快路径：cv2 直读（JPEG/PNG/BMP…）
    if img is not None:
        return img

    if file_path.suffix.lower() not in _HEIC_LIKE_EXTENSIONS:
        return None

    rgb = open_image_rgb_oriented(file_path)
    if rgb is None:
        return None
    try:
        import numpy as np
        import cv2
        return cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
    except Exception as e:
        logger.debug(f"HEIC 转 BGR 失败 {file_path}: {e}")
        return None
    finally:
        rgb.close()


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
