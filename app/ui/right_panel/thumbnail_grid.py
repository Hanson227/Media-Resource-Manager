# -*- coding: utf-8 -*-
"""
缩略图网格视图 —— 右侧面板的媒体文件/文件夹卡片展示区域。
缩略图通过后台线程异步加载，不阻塞 UI。
"""

import logging
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, QAbstractListModel, QModelIndex, Signal, Slot, QSize, QThread,
)
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QListView, QAbstractItemView

from config import AppConfig
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.core.thumbnail_generator import ThumbnailGenerator
from app.ui.right_panel.thumbnail_delegate import ThumbnailDelegate
from app.utils.file_helpers import format_size

logger = logging.getLogger(__name__)


# ============================================================
# 后台缩略图加载线程
# ============================================================

class ThumbLoadWorker(QThread):
    thumb_ready = Signal(int, str)
    all_done = Signal(int)

    def __init__(self, files: list[dict], cache_dir: Path,
                 config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._files = files
        self._cache_dir = Path(cache_dir)
        self._generator = ThumbnailGenerator(
            max_size=config.thumbnail_max_size,
            cache_subdir=".thumbnails",
        )

    def run(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for fdict in self._files:
            if self.isInterruptionRequested():
                break
            fid = fdict["id"]
            fpath = Path(fdict["path"])
            if not fpath.is_file():
                continue

            cache_file = self._cache_dir / f"{fid}_thumb.jpg"
            if cache_file.exists():
                self.thumb_ready.emit(fid, str(cache_file))
                count += 1
                continue

            try:
                thumb_info = self._generator.generate(fpath, self._cache_dir, file_id=fid)
                if thumb_info.thumbnail_path.exists():
                    self.thumb_ready.emit(fid, str(thumb_info.thumbnail_path))
                    count += 1
            except Exception:
                if self.isInterruptionRequested():
                    break

        self.all_done.emit(count)


class FolderPreviewWorker(QThread):
    preview_ready = Signal(int, str)

    def __init__(self, unit_data: list[dict], config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._data = unit_data
        self._gen = ThumbnailGenerator(
            max_size=config.thumbnail_max_size,
            cache_subdir=".thumbnails",
        )

    def run(self) -> None:
        for row, d in enumerate(self._data):
            if self.isInterruptionRequested():
                break
            src = Path(d["preview_path"])
            if not src.is_file():
                continue
            try:
                cache_dir = Path(d["path"]) / ".thumbnails"
                cache_dir.mkdir(parents=True, exist_ok=True)
                info = self._gen.generate(src, cache_dir, file_id=hash(str(src)))
                if info.thumbnail_path.exists():
                    self.preview_ready.emit(row, str(info.thumbnail_path))
            except Exception:
                pass


# ============================================================
# 文件列表模型
# ============================================================

class ThumbnailGridModel(QAbstractListModel):
    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._files: list[dict] = []
        self._full_files: list[dict] = []  # 未过滤的完整列表
        self._thumb_cache: dict[int, QPixmap] = {}
        self._search_text: str = ""
        self._media_filter: str = ""

    def set_files(self, files: list) -> None:
        self.beginResetModel()
        self._files = []
        self._full_files = []
        self._thumb_cache.clear()
        for f in files:
            entry = (
                {"id": f.id, "filename": f.filename, "path": f.path,
                 "media_type": f.media_type, "size_bytes": f.size_bytes,
                 "width": getattr(f, "width", None),
                 "height": getattr(f, "height", None)}
                if hasattr(f, 'id') else f
            )
            self._full_files.append(entry)
        self._apply_filter_in_place()
        self.endResetModel()

    def _apply_filter_in_place(self) -> None:
        """根据当前搜索/筛选条件过滤文件列表。"""
        filtered = self._full_files
        if self._search_text:
            keyword = self._search_text.lower()
            filtered = [f for f in filtered if keyword in f["filename"].lower()]
        if self._media_filter == "image":
            filtered = [f for f in filtered if f.get("media_type") == "image"]
        elif self._media_filter == "video":
            filtered = [f for f in filtered if f.get("media_type") == "video"]
        self._files = filtered

    def apply_filter(self, search_text: str | None = None,
                     media_filter: str | None = None) -> None:
        """应用搜索/媒体类型筛选。None 参数表示保持原值不变。"""
        if search_text is not None:
            self._search_text = search_text
        if media_filter is not None:
            self._media_filter = media_filter
        self.beginResetModel()
        self._apply_filter_in_place()
        self.endResetModel()

    @Slot(int, str)
    def on_thumb_ready(self, file_id: int, thumb_path: str) -> None:
        pixmap = QPixmap(thumb_path)
        if pixmap.isNull():
            return
        self._thumb_cache[file_id] = pixmap
        for row, f in enumerate(self._files):
            if f["id"] == file_id:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])
                break

    @property
    def file_list(self) -> list[dict]:
        return self._files

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._files)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._files):
            return None
        mf = self._files[row]
        fid = mf["id"]

        if role == Qt.ItemDataRole.DisplayRole:
            return mf["filename"]
        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [f"文件名: {mf['filename']}",
                     f"类型: {'视频' if mf.get('media_type') == 'video' else '图片'}",
                     f"大小: {format_size(mf.get('size_bytes', 0))}"]
            if mf.get("width") and mf.get("height"):
                parts.append(f"分辨率: {mf['width']} x {mf['height']}")
            return "\n".join(parts)
        if role == Qt.ItemDataRole.UserRole:
            return mf["path"]
        if role == Qt.ItemDataRole.UserRole + 1:
            return fid
        if role == Qt.ItemDataRole.UserRole + 2:
            return mf.get("media_type", "")
        if role == Qt.ItemDataRole.UserRole + 3:
            return format_size(mf.get("size_bytes", 0))
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumb_cache.get(fid)
        return None


# ============================================================
# 文件夹卡片模型
# ============================================================

class FolderCardModel(QAbstractListModel):
    def __init__(self, data: list[dict], parent=None) -> None:
        super().__init__(parent)
        self._data = data
        self._pixmaps: dict[int, QPixmap] = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._data)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._data):
            return None
        d = self._data[row]

        if role == Qt.ItemDataRole.DisplayRole:
            return d["name"]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{d['name']}\n{d['file_count']} 个文件\n共 {format_size(d['total_size'])}"
        if role == Qt.ItemDataRole.UserRole:
            return d["path"]
        if role == Qt.ItemDataRole.UserRole + 1:
            return d["unit_id"]
        if role == Qt.ItemDataRole.UserRole + 2:
            return "folder"
        if role == Qt.ItemDataRole.UserRole + 3:
            return f"{d['file_count']} 个文件"
        if role == Qt.ItemDataRole.DecorationRole:
            return self._pixmaps.get(row)
        return None

    def add_thumb(self, row: int, pixmap: QPixmap) -> None:
        self._pixmaps[row] = pixmap
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])


# ============================================================
# 网格视图
# ============================================================

class ThumbnailGridView(QListView):
    """缩略图网格视图。支持文件列表和文件夹卡片两种模式。"""

    file_double_clicked = Signal(int)
    file_selected = Signal(int)
    folder_entered = Signal(int)  # 双击文件夹卡片 → 进入该单元
    preview_requested = Signal(int, str, str)  # file_id, file_path, media_type

    def __init__(self, model: ThumbnailGridModel, config: AppConfig,
                 parent=None) -> None:
        super().__init__(parent)
        self._file_model = model       # 始终持有文件模型引用
        self._config = config
        self._thumb_worker: Optional[QThread] = None
        self._folder_worker: Optional[QThread] = None

        self.setModel(model)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setUniformItemSizes(False)
        self.setWordWrap(True)
        self.setWrapping(True)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setItemDelegate(ThumbnailDelegate(config))

        ts = config.thumbnail_max_size
        spacing = config.grid_spacing
        self.setIconSize(QSize(ts, ts))
        self.setGridSize(QSize(ts + spacing * 2 + 8, ts + 56 + spacing))
        self.setSpacing(spacing)

        self.doubleClicked.connect(self._on_double_clicked)
        self.selectionModel().selectionChanged.connect(self._on_selection_changed)

    # ---- 公开方法 ----

    def load_unit(self, unit_id: int) -> None:
        """加载指定单元的文件列表。"""
        self._cancel_all_workers()
        self.setModel(self._file_model)  # 恢复文件模型
        try:
            with DatabaseManager.session() as session:
                files = q.get_files_by_unit(session, unit_id)
                unit = q.get_unit_by_id(session, unit_id)
                unit_path = unit.path if unit else ""
            self._file_model.set_files(files)
            self._start_thumb_worker(unit_path)
            logger.info(f"加载单元 {unit_id}: {len(files)} 个文件")
        except Exception as e:
            logger.error(f"加载单元失败 {unit_id}: {e}")
            self._file_model.set_files([])

    def load_folder_cards(self, unit_data: list[dict], config: AppConfig) -> None:
        """显示文件夹卡片视图。"""
        self._cancel_all_workers()

        folder_model = FolderCardModel(unit_data)
        self.setModel(folder_model)

        ts = config.thumbnail_max_size
        self.setIconSize(QSize(ts, ts))
        self.setGridSize(QSize(ts + 24, ts + 60))

        self._folder_worker = FolderPreviewWorker(unit_data, config)
        self._folder_worker.preview_ready.connect(
            lambda row, path: folder_model.add_thumb(row, QPixmap(path))
        )
        self._folder_worker.start()

    def clear(self) -> None:
        """清空视图。"""
        self._cancel_all_workers()
        self.setModel(self._file_model)
        self._file_model.set_files([])

    def refresh(self) -> None:
        self.update()
        self.viewport().update()

    # ---- 内部 ----

    def _start_thumb_worker(self, unit_path: str) -> None:
        files = self._file_model.file_list
        if not files:
            return
        cache_dir = Path(unit_path) / ".thumbnails" if unit_path else Path(".thumbnails")
        self._thumb_worker = ThumbLoadWorker(files, cache_dir, self._config)
        self._thumb_worker.thumb_ready.connect(self._file_model.on_thumb_ready)
        self._thumb_worker.start()

    def _cancel_all_workers(self) -> None:
        for w in (self._thumb_worker, self._folder_worker):
            if w and w.isRunning():
                w.requestInterruption()
                w.quit()
                w.wait(5000)
        self._thumb_worker = None
        self._folder_worker = None

    @Slot(QModelIndex)
    def _on_double_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        # 使用视图当前模型（非 self._file_model）
        model = index.model() or self.model()
        media_type = model.data(index, Qt.ItemDataRole.UserRole + 2)

        if media_type == "folder":
            unit_id = model.data(index, Qt.ItemDataRole.UserRole + 1)
            if unit_id:
                self.folder_entered.emit(unit_id)
            return

        # 普通文件
        fp = model.data(index, Qt.ItemDataRole.UserRole)
        if fp and os.path.exists(fp):
            try:
                os.startfile(fp)
            except Exception as e:
                logger.error(f"打开失败: {fp} - {e}")
        fid = model.data(index, Qt.ItemDataRole.UserRole + 1)
        if fid:
            self.file_double_clicked.emit(fid)

    @Slot()
    def _on_selection_changed(self) -> None:
        idxs = self.selectedIndexes()
        if not idxs:
            return
        model = self.model()
        fid = model.data(idxs[0], Qt.ItemDataRole.UserRole + 1)
        if fid:
            self.file_selected.emit(fid)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            idxs = self.selectedIndexes()
            if idxs:
                model = self.model()
                fid = model.data(idxs[0], Qt.ItemDataRole.UserRole + 1)
                fp = model.data(idxs[0], Qt.ItemDataRole.UserRole)
                mt = model.data(idxs[0], Qt.ItemDataRole.UserRole + 2)
                if fid and fp and mt:
                    self.preview_requested.emit(fid, fp, mt)
                    event.accept()
                    return
        super().keyPressEvent(event)
