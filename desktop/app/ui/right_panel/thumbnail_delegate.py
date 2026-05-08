# -*- coding: utf-8 -*-
"""
缩略图委托 —— 自定义 QStyledItemDelegate，绘制单个缩略图单元格。

使用系统调色板适配暗/亮主题。
"""

import logging

from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QPainter, QPixmap, QColor, QPen, QFont, QFontMetrics
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QStyle

from config import AppConfig

logger = logging.getLogger(__name__)


class ThumbnailDelegate(QStyledItemDelegate):
    """缩略图绘制委托。颜色来自 option.palette，支持暗色模式。

    派生颜色仅在 palette 变化的帧重新计算，避免每帧创建临时 QColor。
    """

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._thumb_size = config.thumbnail_max_size
        self._cache_text: QColor | None = None
        self._cache_base: QColor | None = None
        self._cache_highlight: QColor | None = None
        self._dim_color: QColor | None = None
        self._ph_bg: QColor | None = None
        self._ph_border: QColor | None = None
        self._sel_color: QColor | None = None
        self._hover_color: QColor | None = None

    def _refresh_colors(self, palette) -> None:
        t = palette.text().color()
        b = palette.base().color()
        h = palette.highlight().color()
        if t == self._cache_text and b == self._cache_base and h == self._cache_highlight:
            return
        self._cache_text = t
        self._cache_base = b
        self._cache_highlight = h
        self._dim_color = QColor(t.red(), t.green(), t.blue(), 160)
        self._ph_bg = b.lighter(130)
        self._ph_border = b.lighter(150)
        self._sel_color = QColor(h.red(), h.green(), h.blue(), 60)
        self._hover_color = QColor(h.red(), h.green(), h.blue(), 20)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        self._refresh_colors(option.palette)
        text_color = self._cache_text

        rect = option.rect
        x, y = rect.x(), rect.y()
        cell_w = rect.width()

        # ---- 选中/悬停背景 ----
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, self._sel_color)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(rect, self._hover_color)

        # ---- 缩略图区域 ----
        pad_h = 4
        thumb_w = min(cell_w - pad_h * 2, self._thumb_size)
        thumb_h = self._thumb_size
        thumb_area = QRect(
            x + (cell_w - thumb_w) // 2, y + pad_h, thumb_w, thumb_h,
        )

        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(
                thumb_area.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            px = thumb_area.x() + (thumb_area.width() - scaled.width()) // 2
            py = thumb_area.y() + (thumb_area.height() - scaled.height()) // 2
            painter.drawPixmap(px, py, scaled)
        else:
            painter.fillRect(thumb_area, self._ph_bg)
            painter.setPen(QPen(self._ph_border, 1))
            painter.drawRect(thumb_area)
            painter.setPen(self._dim_color)
            font = QFont(option.font)
            font.setPixelSize(13)
            painter.setFont(font)
            media_type = index.data(Qt.ItemDataRole.UserRole + 2) or ""
            hint = "VID" if media_type == "video" else "IMG"
            if media_type == "folder":
                hint = "DIR"
            painter.drawText(thumb_area, Qt.AlignmentFlag.AlignCenter, hint)

        # ---- 视频角标 ----
        media_type = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        if media_type == "video":
            badge_w, badge_h = 22, 16
            bx = thumb_area.right() - badge_w - 1
            by = thumb_area.bottom() - badge_h - 1
            badge_rect = QRect(bx, by, badge_w, badge_h)
            painter.fillRect(badge_rect, QColor(0, 0, 0, 140))
            painter.setPen(QColor(255, 255, 255))
            font = QFont(option.font)
            font.setPixelSize(11)
            painter.setFont(font)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, "VID")

        # ---- 文件名 ----
        text_top = y + self._thumb_size + pad_h * 2
        text_area_w = cell_w - pad_h * 2

        filename = index.data(Qt.ItemDataRole.DisplayRole) or "???"
        font_name = QFont(option.font)
        font_name.setPixelSize(13)
        painter.setFont(font_name)
        painter.setPen(text_color)

        fm = QFontMetrics(font_name)
        elided = fm.elidedText(filename, Qt.TextElideMode.ElideMiddle, text_area_w)
        painter.drawText(
            x + pad_h, text_top,
            text_area_w, fm.height() + 2,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            elided,
        )

        # ---- 第二行（文件数或大小） ----
        size_text = index.data(Qt.ItemDataRole.UserRole + 3) or ""
        font_sz = QFont(option.font)
        font_sz.setPixelSize(11)
        painter.setFont(font_sz)
        painter.setPen(self._dim_color)
        painter.drawText(
            x + pad_h, text_top + fm.height() + 2,
            text_area_w, fm.height(),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            size_text,
        )

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        return QSize(self._thumb_size + 20, self._thumb_size + 54)
