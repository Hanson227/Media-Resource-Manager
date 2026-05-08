# -*- coding: utf-8 -*-
"""
信息浮窗 —— 鼠标悬停文件缩略图时显示的元数据提示面板。

显示文件路径、大小、分辨率、哈希等详细信息。
"""

import logging
from PySide6.QtCore import Qt, QPoint, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QFrame,
)

from app.utils.file_helpers import format_size

logger = logging.getLogger(__name__)


class InfoOverlay(QFrame):
    """鼠标悬停时弹出的文件信息卡片。"""

    def __init__(self, parent=None) -> None:
        """初始化信息浮窗。"""
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFrameStyle(QFrame.Shape.Box | QFrame.Shadow.Raised)
        self.setStyleSheet("""
            QFrame {
                background-color: #fff;
                border: 1px solid #ccc;
                border-radius: 4px;
                padding: 8px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self._filename_label = QLabel("")
        self._filename_label.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(self._filename_label)

        self._path_label = QLabel("")
        self._path_label.setStyleSheet("color: #888; font-size: 11px;")
        self._path_label.setWordWrap(True)
        layout.addWidget(self._path_label)

        self._meta_label = QLabel("")
        self._meta_label.setStyleSheet("color: #444; font-size: 11px;")
        layout.addWidget(self._meta_label)

        self._hash_label = QLabel("")
        self._hash_label.setStyleSheet("color: #aaa; font-size: 10px;")
        layout.addWidget(self._hash_label)

        self.setMaximumWidth(320)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_info(self, file_data: dict, pos: QPoint) -> None:
        """显示文件信息。

        参数:
            file_data: 包含 filename、path、size_bytes、width、height、md5_hash 等字段。
            pos: 弹出的屏幕坐标位置。
        """
        filename = file_data.get("filename", "未知文件")
        path = file_data.get("path", "")
        size_bytes = file_data.get("size_bytes", 0)
        width = file_data.get("width")
        height = file_data.get("height")
        md5 = file_data.get("md5_hash", "")

        self._filename_label.setText(filename)
        self._path_label.setText(f"路径: {path}")
        self._path_label.setMaximumWidth(300)

        meta_parts = [f"大小: {format_size(size_bytes)}"]
        if width and height:
            meta_parts.append(f"分辨率: {width}×{height}")
        self._meta_label.setText(" | ".join(meta_parts))

        if md5:
            self._hash_label.setText(f"MD5: {md5[:16]}...")
        else:
            self._hash_label.setText("")

        self.adjustSize()
        self.move(pos)
        self.show()
        self._hide_timer.start(3000)  # 3 秒后自动隐藏
