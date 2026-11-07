# -*- coding: utf-8 -*-
"""
媒体类型判定工具。

提供扩展名集合查询、媒体文件判定、类型映射等纯函数。
"""

from pathlib import Path
from typing import FrozenSet, Optional

from app.utils.constants import MediaType


# ========== 扩展名到媒体类型的映射 ==========
_IMAGE_EXTENSIONS: FrozenSet[str] = frozenset({
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp',
    '.tiff', '.tif', '.heic', '.heif', '.ico', '.jp2',
    '.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2',
    '.pef', '.raf', '.3fr', '.x3f',
})

_RAW_EXTENSIONS: FrozenSet[str] = frozenset({
    # RAW 相机格式：无法被 Pillow 直接解码，需要 rawpy 等专用库
    '.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2',
    '.pef', '.raf', '.3fr', '.x3f',
})

_VIDEO_EXTENSIONS: FrozenSet[str] = frozenset({
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv',
    '.webm', '.m4v', '.mpg', '.mpeg', '.3gp', '.ts',
    '.m2ts', '.mts', '.rmvb', '.vob', '.ogv', '.asf',
})


def is_media_file(file_path: Path, extensions: FrozenSet[str]) -> bool:
    """判断给定路径是否为指定集合内的媒体文件。

    参数:
        file_path: 待判定的文件路径。
        extensions: 有效的媒体扩展名集合。

    返回:
        True 如果文件扩展名在集合内且文件存在。
    """
    if not file_path.is_file():
        return False
    return file_path.suffix.lower() in extensions


def get_media_type(extension: str) -> Optional[MediaType]:
    """根据扩展名映射到媒体类型。

    参数:
        extension: 文件扩展名（含点号，如 '.jpg'）。

    返回:
        MediaType.IMAGE、MediaType.VIDEO，或 None（非媒体文件）。
    """
    ext = extension.lower()
    if ext in _IMAGE_EXTENSIONS:
        return MediaType.IMAGE
    if ext in _VIDEO_EXTENSIONS:
        return MediaType.VIDEO
    return None


def is_image_extension(extension: str) -> bool:
    """判断扩展名是否属于图片类型。"""
    return extension.lower() in _IMAGE_EXTENSIONS


def is_video_extension(extension: str) -> bool:
    """判断扩展名是否属于视频类型。"""
    return extension.lower() in _VIDEO_EXTENSIONS


def is_raw_extension(extension: str) -> bool:
    """判断扩展名是否为 RAW 相机格式（Pillow 无法直接解码）。"""
    return extension.lower() in _RAW_EXTENSIONS


def get_raw_extensions() -> FrozenSet[str]:
    """返回所有 RAW 相机扩展名集合。"""
    return _RAW_EXTENSIONS


def is_perceptual_image_extension(extension: str) -> bool:
    """判断扩展名是否可进行像素级感知哈希（图片且非 RAW）。

    RAW 文件（.cr2/.nef/...）未经 rawpy 解码无法计算 pHash/dHash，
    由调用方决定是否走专用解码路径；此处统一排除以免各处重复维护清单。
    """
    ext = extension.lower()
    return ext in _IMAGE_EXTENSIONS and ext not in _RAW_EXTENSIONS


def get_image_extensions() -> FrozenSet[str]:
    """返回所有支持的图片扩展名集合。"""
    return _IMAGE_EXTENSIONS


def get_video_extensions() -> FrozenSet[str]:
    """返回所有支持的视频扩展名集合。"""
    return _VIDEO_EXTENSIONS


def get_all_media_extensions() -> FrozenSet[str]:
    """返回所有支持的媒体文件扩展名集合。"""
    return _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS


# 浏览器可播放/可展示的 MIME 唯一来源（API 流式端与前端提示共用）。
# 注意：mkv/avi/flv/ts 多数浏览器不原生支持，仍返回真实 MIME，前端据此提示
# 「建议在桌面端播放」，而不是伪装成 mp4 让播放器静默失败。
MIME_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".flv": "video/x-flv", ".ts": "video/mp2t",
    ".wmv": "video/x-ms-wmv",
    ".heic": "image/heic", ".heif": "image/heif",
}


def get_mime_type(extension: str) -> str:
    """按扩展名取 MIME；未知返回 application/octet-stream。"""
    return MIME_TYPES.get(str(extension or "").lower(), "application/octet-stream")


def is_browser_playable_video(extension: str) -> bool:
    """浏览器原生能播的容器（mkv/avi/flv/ts/wmv 明确不行）。"""
    return str(extension or "").lower() in {".mp4", ".m4v", ".mov", ".webm"}
