# -*- coding: utf-8 -*-
"""
状态栏组件 —— 主窗口底部的状态信息栏。

显示：
- 当前状态文字（就绪/扫描中.../查重中...）
- 进度条（扫描/哈希/查重进度）
- 上次扫描时间
- API 服务运行状态指示灯
- 消息未读计数
"""

import logging
import socket
from datetime import datetime

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QStatusBar, QLabel, QProgressBar, QHBoxLayout, QApplication, QPushButton,
)

from config import AppConfig
from app.ui.theme import SUBTEXT_0, GREEN, RED, BLUE

logger = logging.getLogger(__name__)


def _get_local_ip() -> str:
    """获取本机局域网 IPv4 地址。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class MainStatusBar(QStatusBar):
    """主窗口状态栏。

    提供进度显示、状态文字更新、API 指示灯等功能。

    信号:
        cancel_requested: 用户点击「取消」按钮，请求停止当前后台任务。
    """

    cancel_requested = Signal()

    def __init__(self, config: AppConfig, parent=None) -> None:
        """初始化状态栏。

        参数:
            config: 应用配置。
        """
        super().__init__(parent)
        self._config = config
        self._api_running: bool | None = None
        self._unread_count: int = 0
        self._cancellable: bool = False

        # ==== 状态文字 ====
        self._status_label = QLabel("就绪")
        self._status_label.setMinimumWidth(120)
        self.addWidget(self._status_label)

        # ==== 进度条（默认隐藏） ====
        self._progress_bar = QProgressBar()
        self._progress_bar.setMaximumWidth(200)
        self._progress_bar.setMaximumHeight(18)
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.hide()
        self.addWidget(self._progress_bar)

        # ==== 取消按钮（仅长任务运行期间显示） ====
        # 扫描/哈希/查重都跑在 QThread 里，没有取消入口时用户只能强杀进程
        # （或关窗口，那会在 worker 仍写库时 dispose 数据库引擎）。
        # objectName 关联 style.qss 里的紧凑样式：通用 QPushButton 规则是
        # padding:7px 16px + 12px 字号，在限高的状态栏里会把文字压扁。
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setObjectName("statusBarCancel")
        self._cancel_btn.setFixedHeight(20)
        self._cancel_btn.setMinimumWidth(48)
        self._cancel_btn.setMaximumWidth(64)
        self._cancel_btn.setToolTip("停止当前后台任务")
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        self._cancel_btn.hide()
        self.addWidget(self._cancel_btn)

        # ==== 上次扫描时间 ====
        self._last_scan_label = QLabel("尚未扫描")
        self._last_scan_label.setStyleSheet(f"color: {SUBTEXT_0}; padding-right: 8px;")
        self.addPermanentWidget(self._last_scan_label)

        # ==== API 状态指示灯 ====
        self._api_label = QLabel("API: 未启动")
        self._api_label.setStyleSheet(f"color: {SUBTEXT_0}; padding-right: 8px;")
        self.addPermanentWidget(self._api_label)

        # ==== 局域网 IP 地址（可点击复制） ====
        ip = _get_local_ip()
        self._ip_label = QLabel(f"📋 {ip}:{config.api_port}")
        self._ip_label.setStyleSheet(
            f"color: {BLUE}; padding-right: 12px; font-weight: 500;"
        )
        self._ip_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._ip_label.setToolTip("点击复制地址，手机浏览器打开即可访问 Web 端")
        self._ip_label.mousePressEvent = lambda e: self._copy_ip()
        self.addPermanentWidget(self._ip_label)

        # ==== 消息未读计数 ====
        self._msg_label = QLabel("消息: 0")
        self._msg_label.setStyleSheet(f"color: {SUBTEXT_0};")
        self.addPermanentWidget(self._msg_label)

    def _copy_ip(self) -> None:
        """复制局域网地址到剪贴板。"""
        ip = _get_local_ip()
        url = f"http://{ip}:{self._config.api_port}"
        QApplication.clipboard().setText(url)
        self._ip_label.setText(f"📋 {ip}:{self._config.api_port} ✅ 已复制")
        self._ip_label.setStyleSheet(f"color: {GREEN}; padding-right: 12px; font-weight: 500;")
        # 1.5 秒后恢复
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1500, lambda: self._ip_label.setStyleSheet(
            f"color: {BLUE}; padding-right: 12px; font-weight: 500;"
        ))

    # ============================================================
    # 公开方法
    # ============================================================

    @Slot(str)
    def set_status(self, text: str) -> None:
        """更新状态文字。"""
        self._status_label.setText(text)

    @Slot(int, int)
    def set_progress(self, current: int, total: int) -> None:
        """更新进度条。

        参数:
            current: 当前进度值。
            total: 总数。
        """
        if total <= 0:
            return
        self._progress_bar.show()
        self._progress_bar.setRange(0, total)
        self._progress_bar.setValue(current)
        if current >= total:
            self._progress_bar.hide()

    @Slot()
    def hide_progress(self) -> None:
        """隐藏进度条。"""
        self._progress_bar.hide()
        self._progress_bar.setValue(0)
        self.set_cancellable(False)

    def _on_cancel_clicked(self) -> None:
        """点击取消：立即禁用按钮，避免重复触发，然后发出请求。"""
        self._cancel_btn.setEnabled(False)
        self.set_status("正在停止当前任务...")
        self.cancel_requested.emit()

    @Slot(bool)
    def set_cancellable(self, enabled: bool) -> None:
        """显示或隐藏取消按钮。

        参数:
            enabled: True 表示当前任务可取消。
        """
        self._cancellable = enabled
        self._cancel_btn.setEnabled(True)
        self._cancel_btn.setVisible(enabled)

    @Slot(str)
    def set_scan_time(self, time_str: str) -> None:
        """设置上次扫描时间显示。"""
        self._last_scan_label.setText(f"上次扫描: {time_str}")

    @Slot(bool)
    def set_api_status(self, running: bool) -> None:
        """设置 API 状态指示。

        参数:
            running: True 表示 API 正在运行。
        """
        if running == self._api_running:
            return
        self._api_running = running
        if running:
            self._api_label.setText(f"API: 运行中 :{self._config.api_port}")
            self._api_label.setStyleSheet(f"color: {GREEN}; padding-right: 8px;")
        else:
            self._api_label.setText("API: 未启动")
            self._api_label.setStyleSheet(f"color: {RED}; padding-right: 8px;")

    @Slot(int)
    def set_unread_count(self, count: int) -> None:
        """更新消息未读计数。

        参数:
            count: 未读消息数量。
        """
        if count == self._unread_count:
            return
        self._unread_count = count
        if count > 0:
            self._msg_label.setText(f"消息: {count}")
            self._msg_label.setStyleSheet(f"color: {RED}; font-weight: bold;")
        else:
            self._msg_label.setText("消息: 0")
            self._msg_label.setStyleSheet(f"color: {SUBTEXT_0};")

    def record_scan_time(self) -> None:
        """记录当前时间为上次扫描时间。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.set_scan_time(now)
