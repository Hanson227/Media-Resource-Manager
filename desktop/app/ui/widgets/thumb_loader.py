# -*- coding: utf-8 -*-
"""
缩略图异步加载器 —— 管理缩略图的磁盘加载和内存缓存。

功能：
- 从 .thumbnails 缓存目录读取缩略图
- 使用 QPixmapCache 和内存字典双重缓存
- 按需异步加载（避免阻塞 UI 线程）
- 支持为未缓存的图片实时缩放生成
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot, QObject
from PySide6.QtGui import QPixmap, QPixmapCache

from config import AppConfig
from app.core.thumbnail_generator import ThumbnailGenerator

logger = logging.getLogger(__name__)


class ThumbLoader(QObject):
    """缩略图加载器 —— 管理缩略图的加载和缓存。

    用法:
        loader = ThumbLoader(config)
        pixmap = loader.get_thumbnail(file_id, cache_dir)
    """

    def __init__(self, config: AppConfig) -> None:
        """初始化加载器。

        参数:
            config: 应用配置。
        """
        super().__init__()
        self._config = config
        self._generator = ThumbnailGenerator(
            max_size=config.thumbnail_max_size,
            cache_subdir=config.thumbnail_cache_subdir,
            format=config.thumbnail_format,
            quality=config.thumbnail_quality,
        )
        # 内存缓存 {cache_key: QPixmap}
        self._pixmap_cache: dict[str, QPixmap] = {}
        # 最大内存缓存条目
        self._max_cache_size = 500

    def get_thumbnail(self, file_id: int, cache_dir: Path,
                      source_path: Optional[Path] = None) -> QPixmap:
        """获取缩略图的 QPixmap。

        检查顺序：
        1. 内存缓存
        2. QPixmapCache
        3. 磁盘缓存文件
        4. 实时生成

        参数:
            file_id: 文件 ID。
            cache_dir: 缓存目录（资源单元路径）。
            source_path: 原始文件路径（仅在需要生成时使用）。

        返回:
            QPixmap（如果不可用，返回空白占位图）。
        """
        # 1. 内存缓存
        cache_key = self._make_cache_key(file_id, cache_dir)
        if cache_key in self._pixmap_cache:
            return self._pixmap_cache[cache_key]

        # 2. QPixmapCache
        pixmap = QPixmapCache.find(cache_key)
        if pixmap:
            self._pixmap_cache[cache_key] = pixmap
            return pixmap

        # 3. 磁盘缓存
        thumb_path = self._generator.get_thumbnail_path(file_id, cache_dir)
        if thumb_path.is_file():
            pixmap = QPixmap(str(thumb_path))
            if not pixmap.isNull():
                self._cache_pixmap(cache_key, pixmap)
                return pixmap

        # 4. 实时生成
        if source_path and source_path.is_file():
            try:
                info = self._generator.generate(source_path, cache_dir, file_id=file_id)
                if info.thumbnail_path.is_file():
                    pixmap = QPixmap(str(info.thumbnail_path))
                    if not pixmap.isNull():
                        self._cache_pixmap(cache_key, pixmap)
                        return pixmap
            except Exception as e:
                logger.debug(f"缩略图生成失败: {source_path} - {e}")

        # 5. 回退：空白占位
        return QPixmap()

    def clear_cache(self) -> None:
        """清空内存缓存。"""
        self._pixmap_cache.clear()
        QPixmapCache.clear()

    def _cache_pixmap(self, key: str, pixmap: QPixmap) -> None:
        """将 QPixmap 加入缓存。"""
        self._pixmap_cache[key] = pixmap
        QPixmapCache.insert(key, pixmap)

        # 控制缓存大小
        if len(self._pixmap_cache) > self._max_cache_size:
            # 移除最早的条目
            oldest = next(iter(self._pixmap_cache))
            del self._pixmap_cache[oldest]

    @staticmethod
    def _make_cache_key(file_id: int, cache_dir: Path) -> str:
        """生成缓存键。"""
        return f"thumb_{cache_dir.stem}_{file_id}"
