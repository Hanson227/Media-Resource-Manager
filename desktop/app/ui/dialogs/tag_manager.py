# -*- coding: utf-8 -*-
"""
标签管理对话框 —— 新建/编辑/删除标签。
"""

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QLineEdit, QColorDialog,
    QMessageBox, QGroupBox, QFormLayout,
)
from PySide6.QtGui import QColor

from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)


class TagManageDialog(QDialog):
    """标签管理对话框。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理标签")
        self.setMinimumSize(520, 400)
        self.setModal(True)

        self._current_tag_id: Optional[int] = None
        self._selected_color: str = "#888888"

        layout = QVBoxLayout(self)

        # ── 标签列表 ──
        layout.addWidget(QLabel("标签列表:"))
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self._list)

        # ── 编辑区域 ──
        edit_group = QGroupBox("新建 / 编辑标签")
        edit_layout = QFormLayout(edit_group)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("输入标签名称...")
        edit_layout.addRow("名称:", self._name_input)

        color_row = QHBoxLayout()
        self._color_preview = QLabel("■")
        self._color_preview.setStyleSheet("font-size: 20px; color: #888888;")
        color_row.addWidget(self._color_preview)
        self._color_btn = QPushButton("选择颜色...")
        self._color_btn.clicked.connect(self._on_pick_color)
        color_row.addWidget(self._color_btn)
        color_row.addStretch()
        edit_layout.addRow("颜色:", color_row)

        btn_row = QHBoxLayout()
        self._save_btn = QPushButton("保存")
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._clear_form)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch()
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setStyleSheet("color: #e74c3c;")
        self._delete_btn.clicked.connect(self._on_delete)
        self._delete_btn.setEnabled(False)
        btn_row.addWidget(self._delete_btn)
        edit_layout.addRow(btn_row)

        layout.addWidget(edit_group)

        # ── 新建按钮 ──
        self._new_btn = QPushButton("+ 新建标签")
        self._new_btn.clicked.connect(self._on_new)
        layout.addWidget(self._new_btn)

        # ── 关闭按钮 ──
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        self._load_tags()

    def _load_tags(self) -> None:
        """从数据库加载标签列表。"""
        self._list.clear()
        try:
            with DatabaseManager.session() as session:
                tags = q.get_all_tags(session)
        except Exception as e:
            logger.warning(f"加载标签失败: {e}")
            tags = []

        for tag in tags:
            item = QListWidgetItem()
            color = tag.color or "#888888"
            item.setText(f"{tag.name}  ({color})")
            item.setData(Qt.ItemDataRole.UserRole, tag.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, tag.name)
            item.setData(Qt.ItemDataRole.UserRole + 2, color)
            self._list.addItem(item)

    def _on_selection_changed(self, current: Optional[QListWidgetItem],
                              previous: Optional[QListWidgetItem]) -> None:
        if current is None:
            self._clear_form()
            return
        self._current_tag_id = current.data(Qt.ItemDataRole.UserRole)
        self._name_input.setText(current.data(Qt.ItemDataRole.UserRole + 1))
        self._selected_color = current.data(Qt.ItemDataRole.UserRole + 2) or "#888888"
        self._color_preview.setStyleSheet(f"font-size: 20px; color: {self._selected_color};")
        self._delete_btn.setEnabled(True)

    def _on_pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._selected_color), self, "选择标签颜色")
        if color.isValid():
            self._selected_color = color.name()
            self._color_preview.setStyleSheet(f"font-size: 20px; color: {self._selected_color};")

    def _on_save(self) -> None:
        name = self._name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "标签管理", "请输入标签名称")
            return

        try:
            with DatabaseManager.session() as session:
                existing = q.get_tag_by_name(session, name)
                if existing and existing.id != self._current_tag_id:
                    QMessageBox.warning(self, "标签管理", f"标签已存在: {name}")
                    return

                if self._current_tag_id:
                    tag = q.get_tag_by_id(session, self._current_tag_id)
                    if tag:
                        tag.name = name
                        tag.color = self._selected_color
                else:
                    q.create_tag(session, name, self._selected_color)
        except Exception as e:
            QMessageBox.critical(self, "标签管理", f"保存失败: {e}")
            return

        self._clear_form()
        self._load_tags()

    def _on_delete(self) -> None:
        if self._current_tag_id is None:
            return
        reply = QMessageBox.question(
            self, "确认删除", "确定要删除此标签？\n（已分配此标签的文件不会受影响，仅标签被移除）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            with DatabaseManager.session() as session:
                q.delete_tag(session, self._current_tag_id)
        except Exception as e:
            QMessageBox.critical(self, "标签管理", f"删除失败: {e}")
            return

        self._clear_form()
        self._load_tags()

    def _on_new(self) -> None:
        self._clear_form()
        self._name_input.setFocus()

    def _clear_form(self) -> None:
        self._current_tag_id = None
        self._name_input.clear()
        self._selected_color = "#888888"
        self._color_preview.setStyleSheet("font-size: 20px; color: #888888;")
        self._delete_btn.setEnabled(False)
        self._list.clearSelection()
