# -*- coding: utf-8 -*-
"""
已排除文件夹管理对话框 —— 查看和恢复被排除的资源单元。
"""

import logging
from pathlib import Path

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

        header = QLabel("以下文件夹已被排除，不会参与扫描和查重：\n（路径已删除的项显示为灰色）")
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
        self._clean_btn = QPushButton("清理无效记录")
        self._clean_btn.setToolTip("删除所有路径已不存在的排除记录")
        self._clean_btn.clicked.connect(self._on_clean_invalid)
        btn_layout.addWidget(self._clean_btn)
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
                    self._clean_btn.setEnabled(False)
                    return
                has_invalid = False
                for unit in units:
                    path_exists = Path(unit.path).is_dir()
                    label = f"{unit.name}\n{unit.path}"
                    if not path_exists:
                        label += "\n[路径已不存在]"
                        has_invalid = True
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, unit.id)
                    if not path_exists:
                        from PySide6.QtGui import QColor
                        item.setForeground(QColor("#6c7086"))  # 灰色标记
                    self._list_widget.addItem(item)
                self._restore_btn.setEnabled(True)
                self._clean_btn.setEnabled(has_invalid)
        except Exception as e:
            logger.error(f"加载已排除列表失败: {e}")

    @Slot()
    def _on_clean_invalid(self) -> None:
        """清理所有路径已不存在的排除记录。"""
        try:
            with DatabaseManager.session() as session:
                units = q.get_excluded_units(session)
                invalid = [u for u in units if not Path(u.path).is_dir()]
                if not invalid:
                    QMessageBox.information(self, "清理完成", "没有无效记录需要清理。")
                    return
                reply = QMessageBox.question(
                    self, "确认清理",
                    f"将删除 {len(invalid)} 条路径已不存在的排除记录。\n\n确定继续？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                for unit in invalid:
                    q.delete_resource_unit(session, unit.id)
                    logger.info(f"已清理无效排除记录: {unit.name} (id={unit.id})")
            self.excluded_changed.emit()
            self._load_excluded()
        except Exception as e:
            QMessageBox.critical(self, "清理失败", str(e))

    @Slot()
    def _on_restore(self) -> None:
        selected = self._list_widget.currentItem()
        if not selected or not selected.data(Qt.ItemDataRole.UserRole):
            return
        unit_id = selected.data(Qt.ItemDataRole.UserRole)
        name = selected.text().split("\n")[0]
        try:
            with DatabaseManager.session() as session:
                unit = q.get_unit_by_id(session, unit_id)
                if unit is None:
                    QMessageBox.warning(self, "恢复失败", "资源单元不存在")
                    return
                path_exists = Path(unit.path).is_dir()
                if not path_exists:
                    reply = QMessageBox.question(
                        self, "路径不存在",
                        f"文件夹路径已不存在:\n{unit.path}\n\n"
                        f"取消排除后该记录会保留，但下次刷新时会因路径不存在而被重新排除。\n\n"
                        f"是否改为直接删除该记录？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Yes,
                    )
                    if reply == QMessageBox.StandardButton.Cancel:
                        return
                    if reply == QMessageBox.StandardButton.Yes:
                        q.delete_resource_unit(session, unit_id)
                        logger.info(f"已删除路径不存在的单元记录: {name} (id={unit_id})")
                        self.excluded_changed.emit()
                        self._load_excluded()
                        return
                # reply == No → 强制恢复（即使路径不存在）
                q.unexclude_unit(session, unit_id)
            self.excluded_changed.emit()
            self._load_excluded()
        except Exception as e:
            QMessageBox.critical(self, "恢复失败", str(e))
