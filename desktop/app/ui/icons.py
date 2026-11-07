# -*- coding: utf-8 -*-
"""自绘线性图标。

Qt 自带图标（文件夹/信息圈）与这套深色界面风格割裂，MDI 字体在 Qt 里
又依赖字体加载，不可靠。这里用 QPainter 统一画 1.8px 圆头描边的线性图标，
颜色、尺寸、线宽都可控。

用法：
    from app.ui.icons import icon
    button.setIcon(icon("library", theme.ACCENT, 22))
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF,
)

from app.ui import theme

_STROKE = 1.8  # 逻辑像素；绘制时按 scale 放大


def _new_pixmap(size: int) -> tuple[QPixmap, QPainter, float]:
    scale = 2.0  # 2x 位图，缩小后边缘更干净
    pm = QPixmap(int(size * scale), int(size * scale))
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(scale)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pm, painter, scale


def _pen(color: str, scale: float, width: float = _STROKE) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw(name: str, painter: QPainter, color: str, scale: float, size: int) -> None:
    """在 size×size 的逻辑坐标里绘制图标。"""
    painter.setPen(_pen(color, scale))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    s = float(size)
    p = s / 24.0  # 以 24 网格设计，按 size 缩放

    def pt(x: float, y: float) -> QPointF:
        return QPointF(x * p, y * p)

    if name == "library":
        painter.drawRoundedRect(QRectF(4 * p, 5 * p, 12 * p, 14 * p), 2 * p, 2 * p)
        painter.drawLine(pt(16, 8), pt(20, 8))
        painter.drawLine(pt(16, 12), pt(20, 12))
        painter.drawLine(pt(16, 16), pt(20, 16))
        painter.drawLine(pt(7, 9), pt(7, 15))
    elif name == "units":
        for x, y in ((4, 4), (13, 4), (4, 13), (13, 13)):
            painter.drawRoundedRect(QRectF(x * p, y * p, 7 * p, 7 * p), 1.5 * p, 1.5 * p)
    elif name == "dedup":
        painter.drawEllipse(QRectF(4 * p, 6 * p, 12 * p, 12 * p))
        painter.drawEllipse(QRectF(8 * p, 6 * p, 12 * p, 12 * p))
        painter.drawLine(pt(12, 12), pt(12, 12))
    elif name == "tools":
        for y, knob in ((6, 9), (12, 15), (18, 7)):
            painter.drawLine(pt(4, y), pt(20, y))
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF((knob - 1.6) * p, (y - 1.6) * p, 3.2 * p, 3.2 * p))
            painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "settings":
        painter.drawEllipse(QRectF(8 * p, 8 * p, 8 * p, 8 * p))
        painter.drawEllipse(QRectF(10.5 * p, 10.5 * p, 3 * p, 3 * p))
        for i in range(8):
            import math
            a = math.radians(i * 45)
            x1 = 12 + math.cos(a) * 7.2
            y1 = 12 + math.sin(a) * 7.2
            x2 = 12 + math.cos(a) * 9.4
            y2 = 12 + math.sin(a) * 9.4
            painter.drawLine(pt(x1, y1), pt(x2, y2))
    elif name == "message":
        path = QPainterPath()
        path.addRoundedRect(QRectF(4 * p, 5 * p, 16 * p, 12 * p), 3 * p, 3 * p)
        painter.drawPath(path)
        painter.drawLine(pt(8, 17), pt(8, 21))
        painter.drawLine(pt(8, 21), pt(12, 17))
        painter.drawLine(pt(8, 9), pt(16, 9))
        painter.drawLine(pt(8, 12.5), pt(14, 12.5))
    elif name == "plus":
        painter.drawLine(pt(12, 5), pt(12, 19))
        painter.drawLine(pt(5, 12), pt(19, 12))
    elif name == "tag":
        path = QPainterPath()
        path.moveTo(4 * p, 5 * p)
        path.lineTo(12 * p, 5 * p)
        path.lineTo(20 * p, 13 * p)
        path.lineTo(13 * p, 20 * p)
        path.lineTo(4 * p, 11 * p)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawEllipse(QRectF(7 * p, 8 * p, 2.2 * p, 2.2 * p))
    elif name == "path":
        painter.drawLine(pt(5, 18), pt(10, 18))
        painter.drawLine(pt(10, 18), pt(10, 7))
        painter.drawLine(pt(10, 7), pt(18, 7))
        painter.drawLine(pt(15, 4), pt(18, 7))
        painter.drawLine(pt(15, 10), pt(18, 7))
    elif name == "network":
        painter.drawEllipse(QRectF(4 * p, 4 * p, 5 * p, 5 * p))
        painter.drawEllipse(QRectF(15 * p, 4 * p, 5 * p, 5 * p))
        painter.drawEllipse(QRectF(9.5 * p, 15 * p, 5 * p, 5 * p))
        painter.drawLine(pt(9, 7), pt(15, 7))
        painter.drawLine(pt(7, 9), pt(10.5, 15))
        painter.drawLine(pt(17, 9), pt(13.5, 15))
    elif name == "face":
        painter.drawEllipse(QRectF(4 * p, 4 * p, 16 * p, 16 * p))
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(9 * p, 9 * p, 1.6 * p, 1.6 * p))
        painter.drawEllipse(QRectF(13.4 * p, 9 * p, 1.6 * p, 1.6 * p))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(8 * p, 9 * p, 8 * p, 8 * p), 200 * 16, 140 * 16)
    elif name == "database":
        painter.drawEllipse(QRectF(5 * p, 4 * p, 14 * p, 5 * p))
        painter.drawLine(pt(5, 6.5), pt(5, 18.5))
        painter.drawLine(pt(19, 6.5), pt(19, 18.5))
        painter.drawArc(QRectF(5 * p, 16 * p, 14 * p, 5 * p), 180 * 16, 180 * 16)
        painter.drawArc(QRectF(5 * p, 11 * p, 14 * p, 5 * p), 180 * 16, 180 * 16)
    elif name == "folder":
        path = QPainterPath()
        path.moveTo(4 * p, 7 * p)
        path.lineTo(10 * p, 7 * p)
        path.lineTo(12 * p, 9.5 * p)
        path.lineTo(20 * p, 9.5 * p)
        path.lineTo(20 * p, 18 * p)
        path.lineTo(4 * p, 18 * p)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "image":
        painter.drawRoundedRect(QRectF(4 * p, 5 * p, 16 * p, 14 * p), 2 * p, 2 * p)
        painter.drawEllipse(QRectF(8 * p, 8 * p, 2.5 * p, 2.5 * p))
        painter.drawLine(pt(5, 17), pt(10, 12))
        painter.drawLine(pt(10, 12), pt(14, 16))
        painter.drawLine(pt(14, 16), pt(17, 13))
        painter.drawLine(pt(17, 13), pt(20, 16))
    elif name == "search":
        painter.drawEllipse(QRectF(5 * p, 5 * p, 11 * p, 11 * p))
        painter.drawLine(pt(14.5, 14.5), pt(20, 20))
    elif name == "reset":
        painter.drawArc(QRectF(4 * p, 4 * p, 16 * p, 16 * p), 40 * 16, 280 * 16)
        painter.drawLine(pt(16.5, 3.5), pt(17.5, 8))
        painter.drawLine(pt(16.5, 3.5), pt(12, 4.5))
    elif name == "play":
        painter.setBrush(QColor(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(QPolygonF([pt(8, 5), pt(19, 12), pt(8, 19)]))
    elif name == "chevron":
        painter.drawLine(pt(9, 5), pt(16, 12))
        painter.drawLine(pt(16, 12), pt(9, 19))
    elif name == "trash":
        painter.drawLine(pt(5, 7), pt(19, 7))
        painter.drawLine(pt(9, 7), pt(9, 4.5))
        painter.drawLine(pt(15, 7), pt(15, 4.5))
        painter.drawLine(pt(9, 4.5), pt(15, 4.5))
        painter.drawRoundedRect(QRectF(7 * p, 7 * p, 10 * p, 12 * p), 1.5 * p, 1.5 * p)
        painter.drawLine(pt(10.5, 10), pt(10.5, 16))
        painter.drawLine(pt(13.5, 10), pt(13.5, 16))
    elif name == "star":
        import math
        points = []
        for i in range(10):
            a = math.radians(-90 + i * 36)
            r = 8 if i % 2 == 0 else 3.6
            points.append(pt(12 + math.cos(a) * r, 12 + math.sin(a) * r))
        painter.drawPolygon(QPolygonF(points))
    elif name == "scan":
        painter.drawRoundedRect(QRectF(3.5 * p, 4.5 * p, 12 * p, 15 * p), 1.5 * p, 1.5 * p)
        painter.drawEllipse(QRectF(10 * p, 9 * p, 7 * p, 7 * p))
        painter.drawLine(pt(15.5, 14.5), pt(20.5, 19.5))
    elif name == "refresh":
        painter.drawArc(QRectF(5 * p, 5 * p, 14 * p, 14 * p), 30 * 16, 300 * 16)
        painter.drawLine(pt(17.5, 4), pt(19, 9))
        painter.drawLine(pt(17.5, 4), pt(12.5, 5))
    else:  # 兜底：圆点，避免空图标
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(10 * p, 10 * p, 4 * p, 4 * p))


def icon(name: str, color: str = theme.SUBTEXT_1, size: int = 22) -> QIcon:
    """返回一枚自绘线性图标。"""
    pm, painter, scale = _new_pixmap(size)
    try:
        _draw(name, painter, color, scale, size)
    finally:
        painter.end()
    return QIcon(pm)


def gear_icon(color: str = theme.SUBTEXT_1, size: int = 22) -> QIcon:
    """兼容旧调用：设置齿轮图标。"""
    return icon("settings", color, size)
