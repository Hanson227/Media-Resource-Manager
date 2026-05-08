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


def get_image_extensions() -> FrozenSet[str]:
    """返回所有支持的图片扩展名集合。"""
    return _IMAGE_EXTENSIONS


def get_video_extensions() -> FrozenSet[str]:
    """返回所有支持的视频扩展名集合。"""
    return _VIDEO_EXTENSIONS


def get_all_media_extensions() -> FrozenSet[str]:
    """返回所有支持的媒体文件扩展名集合。"""
    return _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS
