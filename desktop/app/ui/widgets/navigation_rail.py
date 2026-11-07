# -*- coding: utf-8 -*-
"""左侧一级导航栏。

桌面端原来把入口散落在 13 个菜单项、8 个工具按钮和 9 个并列对话框里。
这里只保留五个一级入口，具体功能放到各页面卡片 / 设置分区中。
"""

from math import cos, pi, sin

from PySide6.QtCore import Qt, QSize, QPointF, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QLabel, QToolButton, QVBoxLayout,
)


def gear_icon(color: str = "#c0c0cc", size: int = 22) -> QIcon:
    """画一个简洁的齿轮图标（Qt 标准图标里没有设置/齿轮，用信息圈不贴切）。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(color), 2, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    painter.translate(size / 2, size / 2)
    radius = size * 0.24
    painter.drawEllipse(QPointF(0, 0), radius, radius)
    for i in range(8):
        angle = i * pi / 4
        inner = radius + size * 0.06
        outer = size * 0.42
        painter.drawLine(
            QPointF(cos(angle) * inner, sin(angle) * inner),
            QPointF(cos(angle) * outer, sin(angle) * outer),
        )
    painter.end()
    return QIcon(pixmap)


class NavigationRail(QFrame):
    """固定宽度的竖向导航栏。

    信号:
        page_changed: 用户点击某个入口时发出页索引。
    """

    page_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("navRail")
        self.setFixedWidth(88)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QToolButton] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(6)

        title = QLabel("影库")
        title.setObjectName("navRailTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(4)

        self._layout = layout

    def add_item(self, label: str, icon: QIcon | None = None,
                 tooltip: str = "") -> int:
        """追加一个入口，返回索引。"""
        index = len(self._buttons)
        button = QToolButton()
        button.setObjectName("navButton")
        button.setCheckable(True)
        button.setAutoRaise(True)
        button.setText(label)
        button.setToolTip(tooltip or label)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        if icon is not None and not icon.isNull():
            button.setIcon(icon)
        button.setIconSize(QSize(22, 22))
        button.setMinimumHeight(56)
        button.clicked.connect(lambda _checked=False, i=index: self.set_current(i))
        self._group.addButton(button, index)
        self._layout.addWidget(button)
        self._buttons.append(button)
        return index

    def set_current(self, index: int, emit: bool = True) -> None:
        """切换选中项；索引越界时忽略。"""
        if index < 0 or index >= len(self._buttons):
            return
        self._buttons[index].setChecked(True)
        if emit:
            self.page_changed.emit(index)

    def count(self) -> int:
        return len(self._buttons)
