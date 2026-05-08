# -*- coding: utf-8 -*-
"""
SMB 共享管理对话框 —— 管理 Windows 文件夹的局域网共享。

提供：
- 创建新共享
- 查看当前共享列表
- 删除已有共享
- 手动共享操作说明
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QSpinBox, QPushButton, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QDialogButtonBox,
    QTextEdit,
)
from PySide6.QtGui import QFont

from config import AppConfig
from app.services.smb_service import SMBService, ShareInfo
from app.core.exceptions import SMBShareError

logger = logging.getLogger(__name__)


class SMBDialog(QDialog):
    """SMB 共享管理对话框。

    信号:
        share_created: 共享创建成功 (share_name, folder_path)。
        share_deleted: 共享已删除 (share_name)。
    """

    share_created = Signal(str, str)
    share_deleted = Signal(str)

    def __init__(self, config: AppConfig, parent=None) -> None:
        """初始化 SMB 对话框。

        参数:
            config: 应用配置。
        """
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("SMB 共享管理")
        self.resize(550, 450)

        self._setup_ui()
        self._refresh_share_list()

    def _setup_ui(self) -> None:
        """构建 UI。"""
        layout = QVBoxLayout(self)

        # ==== 创建共享 ====
        create_group = QGroupBox("创建新共享")
        create_form = QFormLayout(create_group)

        # 文件夹路径
        path_layout = QHBoxLayout()
        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("选择要共享的文件夹...")
        path_layout.addWidget(self._path_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._on_browse_folder)
        path_layout.addWidget(browse_btn)
        create_form.addRow("文件夹:", path_layout)

        # 共享名称
        self._share_name_edit = QLineEdit()
        self._share_name_edit.setPlaceholderText("共享名称（如: Media_Movies）")
        create_form.addRow("共享名:", self._share_name_edit)

        # 描述
        self._desc_edit = QLineEdit()
        self._desc_edit.setPlaceholderText("共享描述（可选）")
        create_form.addRow("描述:", self._desc_edit)

        # 最大用户数
        self._max_users_spin = QSpinBox()
        self._max_users_spin.setRange(1, 100)
        self._max_users_spin.setValue(20)
        create_form.addRow("最大用户:", self._max_users_spin)

        # 创建按钮
        create_btn = QPushButton("创建共享")
        create_btn.clicked.connect(self._on_create_share)
        create_form.addRow(create_btn)

        layout.addWidget(create_group)

        # ==== 当前共享列表 ====
        list_group = QGroupBox("当前共享")
        list_layout = QVBoxLayout(list_group)

        self._share_list = QListWidget()
        list_layout.addWidget(self._share_list)

        # 删除按钮
        delete_btn = QPushButton("取消共享")
        delete_btn.clicked.connect(self._on_delete_share)
        list_layout.addWidget(delete_btn)

        layout.addWidget(list_group)

        # ==== 手动说明 ====
        help_group = QGroupBox("手动共享说明")
        help_layout = QVBoxLayout(help_group)

        help_text = QTextEdit()
        help_text.setReadOnly(True)
        help_text.setMaximumHeight(100)
        help_text.setPlainText(SMBService.get_share_instructions())
        help_layout.addWidget(help_text)

        layout.addWidget(help_group)

        # ==== 关闭按钮 ====
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

    def _refresh_share_list(self) -> None:
        """刷新当前共享列表。"""
        self._share_list.clear()
        try:
            shares = SMBService.list_shares()
            for s in shares:
                text = f"{s.share_name} → {s.folder_path}"
                if s.description:
                    text += f" ({s.description})"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, s.share_name)
                self._share_list.addItem(item)
        except SMBShareError as e:
            logger.error(f"获取共享列表失败: {e}")
            self._share_list.addItem(
                "⚠️ 无法获取共享列表（可能需要管理员权限）"
            )

    @Slot()
    def _on_browse_folder(self) -> None:
        """选择文件夹。"""
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder:
            self._path_edit.setText(folder)
            # 自动建议共享名
            folder_name = Path(folder).name
            if folder_name and not self._share_name_edit.text():
                self._share_name_edit.setText(
                    self._config.smb_share_name_prefix + folder_name
                )

    @Slot()
    def _on_create_share(self) -> None:
        """创建 SMB 共享。"""
        folder_path = self._path_edit.text().strip()
        share_name = self._share_name_edit.text().strip()

        if not folder_path or not share_name:
            QMessageBox.warning(self, "提示", "请填写文件夹路径和共享名称。")
            return

        try:
            success = SMBService.create_share(
                share_name=share_name,
                folder_path=Path(folder_path),
                description=self._desc_edit.text().strip(),
                max_users=self._max_users_spin.value(),
            )
            if success:
                QMessageBox.information(self, "成功", f"共享 '{share_name}' 已创建。")
                self.share_created.emit(share_name, folder_path)
                self._refresh_share_list()
        except SMBShareError as e:
            QMessageBox.critical(
                self, "创建失败",
                f"{str(e)}\n\n{SMBService.get_share_instructions()}"
            )

    @Slot()
    def _on_delete_share(self) -> None:
        """删除选中的共享。"""
        current = self._share_list.currentItem()
        if not current:
            QMessageBox.information(self, "提示", "请先选择一个共享。")
            return

        share_name = current.data(Qt.ItemDataRole.UserRole)
        confirm = QMessageBox.question(
            self, "确认",
            f"确定要删除共享 '{share_name}' 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        if confirm == QMessageBox.StandardButton.Yes:
            try:
                SMBService.delete_share(share_name)
                QMessageBox.information(self, "成功", f"共享 '{share_name}' 已删除。")
                self.share_deleted.emit(share_name)
                self._refresh_share_list()
            except SMBShareError as e:
                QMessageBox.critical(self, "错误", str(e))
