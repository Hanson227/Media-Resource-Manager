# -*- coding: utf-8 -*-
"""
批量操作对话框 —— 对查重结果执行批量保留/删除/移动操作。

操作选项：
- 保留最优（按文件大小或分辨率）
- 保留单元 A 的所有文件，删除单元 B 的重复文件
- 保留单元 B 的所有文件，删除单元 A 的重复文件
- 将重复文件移动到备份目录
- 自定义逐对操作
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QCheckBox, QDialogButtonBox, QGroupBox,
    QMessageBox,
)

from config import AppConfig
from app.services.cleanup_service import CleanupService
from app.ui.theme import SUBTEXT_0, BLUE

logger = logging.getLogger(__name__)


class BatchOperationsDialog(QDialog):
    """批量操作对话框。

    信号:
        operations_completed: 操作完成 (result: dict)。
    """

    operations_completed = Signal(dict)

    def __init__(self, file_matches: list[dict], config: AppConfig,
                 parent=None) -> None:
        """初始化批量操作对话框。

        参数:
            file_matches: 匹配文件列表，每项:
                {'file_a': {'id': int, 'path': str, 'size': int},
                 'file_b': {'id': int, 'path': str, 'size': int},
                 'match_type': str, 'score': float}
            config: 应用配置。
        """
        super().__init__(parent)
        self._matches = file_matches
        self._config = config
        self._backup_dir: Optional[Path] = None

        self.setWindowTitle("批量操作")
        self.resize(700, 500)

        self._setup_ui()

    def _setup_ui(self) -> None:
        """构建 UI。"""
        layout = QVBoxLayout(self)

        # ==== 操作选择 ====
        action_group = QGroupBox("选择操作")
        action_layout = QVBoxLayout(action_group)

        self._action_combo = QComboBox()
        self._action_combo.addItems([
            "保留最优（按文件大小）",
            "保留单元 A 的文件，删除单元 B 的重复文件",
            "保留单元 B 的文件，删除单元 A 的重复文件",
            "将重复文件移动到备份目录",
        ])
        action_layout.addWidget(QLabel("操作类型:"))
        action_layout.addWidget(self._action_combo)

        layout.addWidget(action_group)

        # ==== 备份目录选择 ====
        backup_layout = QHBoxLayout()
        backup_layout.addWidget(QLabel("备份目录:"))
        self._backup_label = QLabel("（未选择）")
        self._backup_label.setStyleSheet(f"color: {SUBTEXT_0};")
        backup_layout.addWidget(self._backup_label)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._on_browse_backup)
        backup_layout.addWidget(browse_btn)
        layout.addLayout(backup_layout)

        # ==== 文件预览 ====
        preview_group = QGroupBox("将影响的文件")
        preview_layout = QVBoxLayout(preview_group)

        self._file_table = QTableWidget()
        self._file_table.setColumnCount(4)
        self._file_table.setHorizontalHeaderLabels([
            "文件 A", "大小 A", "文件 B", "大小 B",
        ])
        self._file_table.horizontalHeader().setStretchLastSection(True)
        self._file_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._file_table.setRowCount(min(len(self._matches), 20))

        for i, m in enumerate(self._matches[:20]):
            fa = m.get("file_a", {})
            fb = m.get("file_b", {})
            self._file_table.setItem(i, 0, QTableWidgetItem(fa.get("path", "")))
            self._file_table.setItem(i, 1, QTableWidgetItem(str(fa.get("size", 0))))
            self._file_table.setItem(i, 2, QTableWidgetItem(fb.get("path", "")))
            self._file_table.setItem(i, 3, QTableWidgetItem(str(fb.get("size", 0))))

        preview_layout.addWidget(self._file_table)

        count_label = QLabel(f"共 {len(self._matches)} 对匹配文件")
        count_label.setStyleSheet(f"color: {SUBTEXT_0}; padding: 4px;")
        preview_layout.addWidget(count_label)

        layout.addWidget(preview_group)

        # ==== 安全选项 ====
        self._use_recycle = QCheckBox("删除操作使用回收站（推荐）")
        self._use_recycle.setChecked(True)
        layout.addWidget(self._use_recycle)

        self._delete_empty = QCheckBox("清理空文件夹")
        self._delete_empty.setChecked(True)
        layout.addWidget(self._delete_empty)

        # ==== 按钮 ====
        buttons = QDialogButtonBox()
        execute_btn = QPushButton("执行")
        execute_btn.setStyleSheet("font-weight: bold; padding: 6px 20px;")
        execute_btn.clicked.connect(self._on_execute)
        buttons.addButton(execute_btn, QDialogButtonBox.ButtonRole.AcceptRole)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        buttons.addButton(cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)

        layout.addWidget(buttons)

    @Slot()
    def _on_browse_backup(self) -> None:
        """选择备份目录。"""
        folder = QFileDialog.getExistingDirectory(self, "选择备份目录")
        if folder:
            self._backup_dir = Path(folder)
            self._backup_label.setText(str(self._backup_dir))
            self._backup_label.setStyleSheet(f"color: {BLUE};")

    @Slot()
    def _on_execute(self) -> None:
        """执行批量操作。"""
        action = self._action_combo.currentIndex()
        send_to_trash = self._use_recycle.isChecked()
        clean_empty = self._delete_empty.isChecked()

        # 根据操作类型构建文件操作列表
        file_actions: list[dict] = []

        for m in self._matches:
            fa = m.get("file_a", {})
            fb = m.get("file_b", {})

            if action == 0:  # 保留最优（按文件大小）
                if fa.get("size", 0) >= fb.get("size", 0):
                    target = fb
                else:
                    target = fa
            elif action == 1:  # 保留 A
                target = fb
            elif action == 2:  # 保留 B
                target = fa
            else:
                # 移动模式：两个文件都处理
                target = None

            if target and target.get("path"):
                if action == 3 and self._backup_dir:
                    file_actions.append({
                        "action": "move",
                        "path": Path(target["path"]),
                        "file_id": target.get("id"),
                        "backup_dir": self._backup_dir,
                    })
                else:
                    file_actions.append({
                        "action": "trash" if send_to_trash else "delete",
                        "path": Path(target["path"]),
                        "file_id": target.get("id"),
                    })

        if not file_actions:
            QMessageBox.information(self, "提示", "没有需要操作的文件。")
            return

        # 确认
        confirm = QMessageBox.question(
            self, "确认操作",
            f"将对 {len(file_actions)} 个文件执行操作，确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        # 执行
        result = CleanupService.batch_operation(file_actions)
        self.operations_completed.emit(result)

        QMessageBox.information(
            self, "操作完成",
            f"成功: {result['success']} 个\n失败: {result['failed']} 个"
        )
        self.accept()
