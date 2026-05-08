# -*- coding: utf-8 -*-
"""
消息中心对话框 —— 展示所有系统提醒的收件箱。

支持：
- 按未读/已读筛选
- 查看正文
- 标记已读 / 全部已读
- 忽略（删除）消息
"""

import logging
from datetime import datetime

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QSplitter,
    QListWidget, QListWidgetItem, QTextBrowser,
    QPushButton, QLabel, QCheckBox, QGroupBox,
)

from app.db.models import Message
from app.services.message_center import MessageCenter

logger = logging.getLogger(__name__)


class MessageCenterDialog(QDialog):
    """消息中心对话框。

    信号:
        messages_updated: 消息状态变更后发出。
    """

    messages_updated = Signal()

    def __init__(self, parent=None) -> None:
        """初始化消息中心对话框。"""
        super().__init__(parent)
        self.setWindowTitle("消息中心")
        self.resize(700, 500)

        self._messages: list[Message] = []
        self._setup_ui()
        self._load_messages()

    def _setup_ui(self) -> None:
        """构建 UI。"""
        layout = QVBoxLayout(self)

        # 工具栏
        toolbar = QHBoxLayout()

        self._unread_check = QCheckBox("仅显示未读")
        self._unread_check.setChecked(True)
        self._unread_check.toggled.connect(self._load_messages)
        toolbar.addWidget(self._unread_check)

        toolbar.addStretch()

        mark_all_btn = QPushButton("全部已读")
        mark_all_btn.clicked.connect(self._mark_all_read)
        toolbar.addWidget(mark_all_btn)

        dismiss_btn = QPushButton("忽略选中")
        dismiss_btn.clicked.connect(self._dismiss_selected)
        toolbar.addWidget(dismiss_btn)

        layout.addLayout(toolbar)

        # 分隔区域
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧消息列表
        self._msg_list = QListWidget()
        self._msg_list.currentRowChanged.connect(self._on_message_selected)
        splitter.addWidget(self._msg_list)

        # 右侧消息详情
        self._detail_browser = QTextBrowser()
        self._detail_browser.setOpenExternalLinks(False)
        splitter.addWidget(self._detail_browser)

        splitter.setSizes([300, 400])
        layout.addWidget(splitter)

        # 底部
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #888; padding: 4px;")
        layout.addWidget(self._status_label)

        # 关闭
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

    def _load_messages(self) -> None:
        """加载消息列表。"""
        only_unread = self._unread_check.isChecked()

        if only_unread:
            self._messages = MessageCenter.get_unread()
        else:
            self._messages = MessageCenter.get_all()

        self._msg_list.clear()
        for msg in self._messages:
            prefix = "● " if not msg.is_read else "  "
            text = f"{prefix}{msg.title}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, msg.id)

            if msg.msg_type == "dedup_alert":
                item.setForeground(Qt.GlobalColor.red)
            elif msg.msg_type == "warning":
                item.setForeground(Qt.GlobalColor.darkYellow)
            elif msg.msg_type == "error":
                item.setForeground(Qt.GlobalColor.red)

            self._msg_list.addItem(item)

        unread_count = MessageCenter.get_unread_count()
        self._status_label.setText(
            f"共 {len(self._messages)} 条消息，{unread_count} 条未读"
        )

    @Slot(int)
    def _on_message_selected(self, row: int) -> None:
        """选中消息时显示详情。"""
        if row < 0 or row >= len(self._messages):
            return

        msg = self._messages[row]

        html = f"""
        <h3>{msg.title}</h3>
        <p style="color: #888; font-size: 11px;">
            时间: {msg.created_at.strftime('%Y-%m-%d %H:%M:%S') if msg.created_at else ''}
            &nbsp;|&nbsp; 类型: {msg.msg_type}
            &nbsp;|&nbsp; {'未读' if not msg.is_read else '已读'}
        </p>
        <hr>
        <p>{msg.body or '(无正文内容)'}</p>
        """
        self._detail_browser.setHtml(html)

        # 自动标记为已读
        if not msg.is_read:
            MessageCenter.mark_read(msg.id)
            msg.is_read = True

    @Slot()
    def _mark_all_read(self) -> None:
        """标记所有消息为已读。"""
        MessageCenter.mark_all_read()
        self._load_messages()
        self.messages_updated.emit()

    @Slot()
    def _dismiss_selected(self) -> None:
        """忽略选中的消息。"""
        current_row = self._msg_list.currentRow()
        if current_row < 0 or current_row >= len(self._messages):
            return

        msg = self._messages[current_row]
        if MessageCenter.dismiss(msg.id):
            self._load_messages()
            self.messages_updated.emit()
