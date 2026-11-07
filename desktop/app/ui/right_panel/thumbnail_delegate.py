# -*- coding: utf-8 -*-
"""缩略图委托 —— 卡片式单元格。

设计：深色卡片 + 圆角缩略图 + 暖白文件名；选中时用黄铜描边，
悬停时轻微提亮。避免默认 QListView 那种「一格一格的黑洞」。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from config import AppConfig
from app.ui import theme

logger = logging.getLogger(__name__)

_CARD_RADIUS = 9
_THUMB_RADIUS = 6


class ThumbnailDelegate(QStyledItemDelegate):
    """缩略图卡片绘制委托。"""

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._thumb_size = config.thumbnail_max_size

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        cell = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # ---- 卡片底 ----
        card_path = QPainterPath()
        card_path.addRoundedRect(QRectF(cell), _CARD_RADIUS, _CARD_RADIUS)
        if selected:
            painter.fillPath(card_path, QColor(217, 164, 65, 34))
        elif hovered:
            painter.fillPath(card_path, QColor(theme.SURFACE_1))
        else:
            painter.fillPath(card_path, QColor(theme.SURFACE_0))

        if selected:
            painter.setPen(QPen(QColor(theme.ACCENT), 1.4))
            painter.drawPath(card_path)
        elif hovered:
            painter.setPen(QPen(QColor(theme.SURFACE_2), 1.0))
            painter.drawPath(card_path)

        # ---- 缩略图（圆角裁剪 + 居中裁切） ----
        pad = 7
        thumb = QRect(cell.x() + pad, cell.y() + pad,
                      cell.width() - pad * 2, self._thumb_size)
        thumb_path = QPainterPath()
        thumb_path.addRoundedRect(QRectF(thumb), _THUMB_RADIUS, _THUMB_RADIUS)
        painter.setClipPath(thumb_path)

        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(
                thumb.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            sx = thumb.x() + (thumb.width() - scaled.width()) // 2
            sy = thumb.y() + (thumb.height() - scaled.height()) // 2
            painter.drawPixmap(sx, sy, scaled)
        else:
            painter.fillRect(thumb, QColor(theme.SURFACE_1))
            painter.setPen(QColor(theme.OVERLAY_1))
            font = QFont(option.font)
            font.setPixelSize(12)
            painter.setFont(font)
            media_type = index.data(Qt.ItemDataRole.UserRole + 2) or ""
            hint = {"video": "VIDEO", "folder": "DIR"}.get(media_type, "IMAGE")
            painter.drawText(thumb, Qt.AlignmentFlag.AlignCenter, hint)

        # 视频角标：半透明圆角底 + 白色三角
        media_type = index.data(Qt.ItemDataRole.UserRole + 2) or ""
        if media_type == "video":
            badge = QRect(thumb.right() - 30, thumb.bottom() - 24, 24, 18)
            painter.setClipping(False)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setBrush(QColor("#FFFFFF"))
            cx, cy = badge.center().x(), badge.center().y()
            painter.drawPolygon(QPolygonF([
                QPointF(cx - 3, cy - 5),
                QPointF(cx + 5, cy),
                QPointF(cx - 3, cy + 5),
            ]))

        painter.setClipping(False)

        # ---- 文件名 / 副标题 ----
        text_top = thumb.bottom() + 9
        width = cell.width() - pad * 2
        font_name = QFont(option.font)
        font_name.setPixelSize(12)
        font_name.setWeight(QFont.Weight.Medium)
        painter.setFont(font_name)
        painter.setPen(QColor(theme.TEXT))
        fm = QFontMetrics(font_name)
        filename = index.data(Qt.ItemDataRole.DisplayRole) or "???"
        elided = fm.elidedText(filename, Qt.TextElideMode.ElideMiddle, width)
        painter.drawText(
            cell.x() + pad, text_top, width, fm.height(),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided,
        )

        size_text = index.data(Qt.ItemDataRole.UserRole + 3) or ""
        if size_text:
            font_sub = QFont(option.font)
            font_sub.setPixelSize(10)
            painter.setFont(font_sub)
            painter.setPen(QColor(theme.SUBTEXT_0))
            painter.drawText(
                cell.x() + pad, text_top + fm.height() + 2, width, 16,
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                size_text,
            )

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        return QSize(self._thumb_size + 22, self._thumb_size + 54)
