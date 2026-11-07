# -*- coding: utf-8 -*-
"""入口卡片：图标方块 + 标题 + 描述 + 右侧箭头。

替代 QCommandLinkButton（系统箭头图标 + 两行文字，像安装向导）。
卡片本身只负责视觉与点击，业务动作由外部连接。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui import theme
from app.ui.icons import icon


class NavCard(QFrame):
    """可点击的入口卡片。"""

    clicked = Signal()

    def __init__(self, title: str, desc: str, icon_name: str = "folder",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMinimumHeight(84)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 14, 14, 14)
        row.setSpacing(14)

        icon_box = QLabel()
        icon_box.setObjectName("navCardIcon")
        icon_box.setFixedSize(38, 38)
        icon_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_box.setPixmap(icon(icon_name, theme.ACCENT, 22).pixmap(22, 22))
        row.addWidget(icon_box, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(3)
        title_label = QLabel(title)
        title_label.setObjectName("navCardTitle")
        desc_label = QLabel(desc)
        desc_label.setObjectName("navCardDesc")
        desc_label.setWordWrap(True)
        text_col.addWidget(title_label)
        text_col.addWidget(desc_label)
        row.addLayout(text_col, 1)

        chevron = QLabel()
        chevron.setObjectName("navCardChevron")
        chevron.setPixmap(icon("chevron", theme.OVERLAY_1, 18).pixmap(18, 18))
        row.addWidget(chevron, 0, Qt.AlignmentFlag.AlignVCenter)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 — Qt 命名
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


__all__ = ["NavCard"]
