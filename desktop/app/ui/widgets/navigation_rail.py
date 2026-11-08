# -*- coding: utf-8 -*-
"""左侧一级导航 rail。

固定窄栏：品牌 → 5 个一级页面 → 弹性空白 → 底部全局入口（消息）。
比顶部 QToolBar 更稳定：不会因为页面切换而重新排布，
也让内容区从窗口最上方开始，视觉上更像一个「放映室」而不是设置面板。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QLabel, QSizePolicy, QToolButton, QVBoxLayout,
    QWidget,
)

from app.ui.icons import gear_icon


class NavigationRail(QFrame):
    """固定宽度的一级导航栏。"""

    page_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("navRail")
        self.setFixedWidth(96)
        self._buttons: list[QToolButton] = []
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 18, 8, 14)
        outer.setSpacing(0)

        # ---- 品牌 ----
        brand = QLabel("影库")
        brand.setObjectName("navBrand")
        brand.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(brand)

        tagline = QLabel("MEDIA ARCHIVE")
        tagline.setObjectName("navTagline")
        tagline.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(tagline)

        outer.addSpacing(22)

        # ---- 一级入口 ----
        items = QWidget()
        items.setObjectName("navItems")
        self._items_layout = QVBoxLayout(items)
        self._items_layout.setContentsMargins(0, 0, 0, 0)
        self._items_layout.setSpacing(6)
        outer.addWidget(items)

        outer.addStretch(1)

        # ---- 底部全局入口 ----
        self._footer_layout = QVBoxLayout()
        self._footer_layout.setContentsMargins(0, 0, 0, 0)
        self._footer_layout.setSpacing(6)
        outer.addLayout(self._footer_layout)

    # ------------------------------------------------------------
    def add_item(self, label: str, icon: Optional[QIcon] = None,
                 tooltip: str = "") -> QToolButton:
        index = len(self._buttons)
        button = QToolButton()
        button.setObjectName("navButton")
        button.setCheckable(True)
        button.setAutoExclusive(False)  # 交给 QButtonGroup 统一互斥
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(24, 24))
        button.setMinimumHeight(62)
        # QToolButton 默认水平策略是 Fixed：宽度只按 sizeHint（44~57px），
        # 仍靠左摆 → 各入口图标/文字不在同一条中线上，看起来"完全不对齐"。
        # 撑满 rail 内容宽（80px），文字与图标才会统一居中。
        button.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        button.setText(label)
        if icon is not None:
            button.setIcon(icon)
        if tooltip:
            button.setToolTip(tooltip)
        button.clicked.connect(lambda _=False, i=index: self.set_current(i))
        self._group.addButton(button, index)
        self._buttons.append(button)
        self._items_layout.addWidget(button)
        return button

    def add_footer_action(self, label: str, icon: Optional[QIcon] = None,
                          tooltip: str = "") -> QToolButton:
        """底部全局按钮（消息等），不参与页面互斥。"""
        button = QToolButton()
        button.setObjectName("railFooterButton")
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(22, 22))
        button.setMinimumHeight(52)
        # 与上面 5 个入口同宽同中线，否则底部「消息」会缩在左边
        button.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Fixed)
        button.setText(label)
        if icon is not None:
            button.setIcon(icon)
        if tooltip:
            button.setToolTip(tooltip)
        self._footer_layout.addWidget(button)
        return button

    def count(self) -> int:
        return len(self._buttons)

    def set_current(self, index: int, emit: bool = True) -> None:
        if index < 0 or index >= len(self._buttons):
            return
        button = self._buttons[index]
        if not button.isChecked():
            button.setChecked(True)
        if emit:
            self.page_changed.emit(index)


__all__ = ["NavigationRail", "gear_icon"]
