# -*- coding: utf-8 -*-
"""
进度面板组件 —— 可复用的进度条 + 取消按钮 + 任务描述。

用于扫描、哈希、查重等长时间后台任务的进度展示。
"""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QProgressBar, QPushButton,
)
from app.ui.theme import SUBTEXT_0


class ProgressPanel(QWidget):
    """可复用的进度面板。

    信号:
        cancelled: 用户点击取消按钮时发出。
    """

    cancelled = Signal()

    def __init__(self, parent=None) -> None:
        """初始化进度面板。"""
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        # 任务描述文字
        self._task_label = QLabel("就绪")
        self._task_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._task_label)

        # 进度条 + 取消按钮
        bar_layout = QHBoxLayout()

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(True)
        bar_layout.addWidget(self._progress_bar)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setMaximumWidth(60)
        self._cancel_btn.clicked.connect(self.cancelled.emit)
        self._cancel_btn.hide()
        bar_layout.addWidget(self._cancel_btn)

        layout.addLayout(bar_layout)

        # 详情文字
        self._detail_label = QLabel("")
        self._detail_label.setStyleSheet(f"color: {SUBTEXT_0}; font-size: 11px;")
        layout.addWidget(self._detail_label)

    @Slot(str)
    def set_task(self, text: str) -> None:
        """设置任务描述。"""
        self._task_label.setText(text)

    @Slot(int, int)
    def set_progress(self, current: int, total: int) -> None:
        """更新进度。

        参数:
            current: 当前值。
            total: 上限值。
        """
        if total > 0:
            self._progress_bar.setRange(0, total)
            self._progress_bar.setValue(current)
            self._detail_label.setText(f"{current} / {total}")

    @Slot(str)
    def set_detail(self, text: str) -> None:
        """更新详情文字。"""
        self._detail_label.setText(text)

    @Slot(bool)
    def set_cancellable(self, enabled: bool) -> None:
        """显示或隐藏取消按钮。"""
        self._cancel_btn.setVisible(enabled)

    def reset(self) -> None:
        """重置进度面板到初始状态。"""
        self._task_label.setText("就绪")
        self._progress_bar.setValue(0)
        self._progress_bar.setRange(0, 100)
        self._detail_label.setText("")
        self._cancel_btn.hide()
