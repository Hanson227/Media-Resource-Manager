# -*- coding: utf-8 -*-
"""
文件操作辅助函数。

提供路径标准化、文件大小格式化等纯函数工具。
"""

import os
from pathlib import Path
from typing import Optional

from app.utils.constants import SIZE_UNITS


def normalize_path(path: Path) -> Path:
    """将路径标准化为绝对路径并统一大小写（Windows）。

    参数:
        path: 原始路径。

    返回:
        标准化后的绝对路径。
    """
    return path.resolve()


def format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读的字符串。

    参数:
        size_bytes: 文件字节数。

    返回:
        如 '2.4 MB'、'156 KB' 的格式化字符串。
    """
    if size_bytes == 0:
        return "0 B"

    import math
    index = min(
        int(math.log(abs(size_bytes), 1024)),
        len(SIZE_UNITS) - 1,
    )
    value = size_bytes / (1024 ** index)
    if index == 0:
        return f"{value:.0f} {SIZE_UNITS[index]}"
    return f"{value:.1f} {SIZE_UNITS[index]}"


def get_file_count_and_size(directory: Path, extensions: frozenset[str]) -> tuple[int, int]:
    """统计指定目录下所有媒体文件的数量和总大小（仅单层，不递归）。

    参数:
        directory: 目标目录。
        extensions: 文件扩展名集合。

    返回:
        (文件数量, 总字节数) 元组。
    """
    count = 0
    total_size = 0
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix.lower() in extensions:
                count += 1
                try:
                    total_size += entry.stat().st_size
                except OSError:
                    pass
    except PermissionError:
        pass
    return count, total_size


def safe_filename(filename: str) -> str:
    """去除文件名中的非法字符，确保可用于文件系统。

    参数:
        filename: 原始文件名。

    返回:
        清理后的安全文件名。
    """
    illegal_chars = '<>:"/\\|?*'
    for char in illegal_chars:
        filename = filename.replace(char, '_')
    return filename.strip()


def ensure_dir(path: Path) -> None:
    """确保目录存在，不存在则创建。

    参数:
        path: 目录路径。
    """
    path.mkdir(parents=True, exist_ok=True)


def is_hidden_dir(dirname: str) -> bool:
    """判断目录是否为隐藏目录（以点号开头或位于排除列表中）。

    参数:
        dirname: 目录名称。

    返回:
        True 如果目录以 '.' 开头。
    """
    return dirname.startswith('.')
