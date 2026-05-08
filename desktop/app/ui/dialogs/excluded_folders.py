# -*- coding: utf-8 -*-
"""
已排除文件夹管理对话框 —— 查看和恢复被排除的资源单元。
"""

import logging

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox,
)

from app.db.engine import DatabaseManager
from app.db import queries as q
from app.ui.theme import SUBTEXT_0, TEXT

logger = logging.getLogger(__name__)


class ExcludedFoldersDialog(QDialog):
    """已排除文件夹管理对话框。

    信号:
        excluded_changed: 排除列表发生变化时发出。
    """

    excluded_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理已排除文件夹")
        self.resize(500, 400)
        self._setup_ui()
        self._load_excluded()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QLabel("以下文件夹已被排除，不会参与扫描和查重：")
        header.setStyleSheet(f"color: {SUBTEXT_0}; padding: 4px;")
        layout.addWidget(header)

        self._list_widget = QListWidget()
        self._list_widget.setStyleSheet(
            f"QListWidget {{ background-color: #313244; color: {TEXT}; "
            f"border: 1px solid #3b3b4e; border-radius: 6px; }}"
            f"QListWidget::item {{ padding: 6px; }}"
            f"QListWidget::item:selected {{ background-color: #3b3b4e; }}"
        )
        layout.addWidget(self._list_widget)

        btn_layout = QHBoxLayout()
        self._restore_btn = QPushButton("取消排除")
        self._restore_btn.setToolTip("将选中的文件夹恢复为活跃状态")
        self._restore_btn.clicked.connect(self._on_restore)
        btn_layout.addWidget(self._restore_btn)
        btn_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _load_excluded(self) -> None:
        self._list_widget.clear()
        try:
            with DatabaseManager.session() as session:
                units = q.get_excluded_units(session)
                if not units:
                    empty_item = QListWidgetItem("暂无已排除的文件夹")
                    empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                    self._list_widget.addItem(empty_item)
                    self._restore_btn.setEnabled(False)
                    return
                for unit in units:
                    item = QListWidgetItem(f"{unit.name}\n{unit.path}")
                    item.setData(Qt.ItemDataRole.UserRole, unit.id)
                    self._list_widget.addItem(item)
                self._restore_btn.setEnabled(True)
        except Exception as e:
            logger.error(f"加载已排除列表失败: {e}")

    @Slot()
    def _on_restore(self) -> None:
        selected = self._list_widget.currentItem()
        if not selected or not selected.data(Qt.ItemDataRole.UserRole):
            return
        unit_id = selected.data(Qt.ItemDataRole.UserRole)
        name = selected.text().split("\n")[0]
        try:
            with DatabaseManager.session() as session:
                q.unexclude_unit(session, unit_id)
            self.excluded_changed.emit()
            self._load_excluded()
        except Exception as e:
            QMessageBox.critical(self, "恢复失败", str(e))
