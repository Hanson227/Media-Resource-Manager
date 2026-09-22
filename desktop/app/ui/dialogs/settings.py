# -*- coding: utf-8 -*-
"""
应用设置对话框 —— 全局配置的图形化管理界面。

管理：
- 扫描与数据库设置
- 缩略图设置
- API 服务设置
- 文件监控设置
"""

from pathlib import Path

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QWidget, QFormLayout,
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox,
    QDialogButtonBox, QFileDialog, QPushButton, QHBoxLayout,
    QGroupBox,
)

from config import AppConfig
from app.ui.theme import BASE, TEXT, SURFACE_0, OVERLAY_0, INDIGO


class SettingsDialog(QDialog):
    """应用全局设置对话框。

    信号:
        settings_saved: 用户保存新配置时发出 (new_config: AppConfig)。
    """

    settings_saved = Signal(object)  # AppConfig

    def __init__(self, config: AppConfig, parent=None) -> None:
        """初始化设置对话框。

        参数:
            config: 当前应用配置。
        """
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("设置")
        self.resize(500, 450)

        self._setup_ui()
        self._load_values()

    def _setup_ui(self) -> None:
        """构建选项卡式 UI。"""
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(BASE))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
        palette.setColor(QPalette.ColorRole.Base, QColor(SURFACE_0))
        palette.setColor(QPalette.ColorRole.Button, QColor(SURFACE_0))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
        palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(OVERLAY_0))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(INDIGO))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT))
        self.setPalette(palette)

        layout = QVBoxLayout(self)

        tabs = QTabWidget()
        # 修复标签栏文字在深色主题下不可见的问题
        tabs.setPalette(palette)

        # ==== 数据库选项卡 ====
        db_tab = QWidget()
        db_form = QFormLayout(db_tab)

        db_path_layout = QHBoxLayout()
        self._db_path_edit = QLineEdit()
        db_path_layout.addWidget(self._db_path_edit)
        db_browse = QPushButton("浏览...")
        db_browse.clicked.connect(lambda: self._browse_file(self._db_path_edit, "数据库文件 (*.db)"))
        db_path_layout.addWidget(db_browse)
        db_form.addRow("数据库路径:", db_path_layout)

        tabs.addTab(db_tab, "数据库")

        # ==== 缩略图选项卡 ====
        thumb_tab = QWidget()
        thumb_form = QFormLayout(thumb_tab)

        self._thumb_size_spin = QSpinBox()
        self._thumb_size_spin.setRange(64, 512)
        self._thumb_size_spin.setSuffix(" px")
        thumb_form.addRow("缩略图最大边长:", self._thumb_size_spin)

        self._thumb_quality_spin = QSpinBox()
        self._thumb_quality_spin.setRange(10, 100)
        self._thumb_quality_spin.setSuffix(" %")
        thumb_form.addRow("缩略图质量:", self._thumb_quality_spin)

        tabs.addTab(thumb_tab, "缩略图")

        # ==== API 选项卡 ====
        api_tab = QWidget()
        api_form = QFormLayout(api_tab)

        self._api_enabled_check = QCheckBox("程序启动时自动启动 API 服务")
        api_form.addRow(self._api_enabled_check)

        self._api_port_spin = QSpinBox()
        self._api_port_spin.setRange(1024, 65535)
        api_form.addRow("API 监听端口:", self._api_port_spin)

        self._api_host_edit = QLineEdit()
        api_form.addRow("API 监听地址:", self._api_host_edit)

        self._web_pin_edit = QLineEdit()
        self._web_pin_edit.setMaxLength(4)
        self._web_pin_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._web_pin_edit.setPlaceholderText("留空表示不启用密码")
        api_form.addRow("Web 访问密码 (4位):", self._web_pin_edit)

        tabs.addTab(api_tab, "API 服务")

        # ==== 文件监控选项卡 ====
        watcher_tab = QWidget()
        watcher_form = QFormLayout(watcher_tab)

        self._watcher_enabled_check = QCheckBox("启用实时文件监控")
        watcher_form.addRow(self._watcher_enabled_check)

        self._watcher_debounce_spin = QSpinBox()
        self._watcher_debounce_spin.setRange(500, 10000)
        self._watcher_debounce_spin.setSingleStep(500)
        self._watcher_debounce_spin.setSuffix(" ms")
        watcher_form.addRow("去抖动间隔:", self._watcher_debounce_spin)

        tabs.addTab(watcher_tab, "文件监控")

        # ==== 预览选项卡 ====
        preview_tab = QWidget()
        preview_form = QFormLayout(preview_tab)

        self._seek_percent_spin = QSpinBox()
        self._seek_percent_spin.setRange(1, 50)
        self._seek_percent_spin.setSuffix(" %")
        preview_form.addRow("方向键跳转比例:", self._seek_percent_spin)

        tabs.addTab(preview_tab, "预览")

        # ==== HEIC 选项卡 ====
        heic_tab = QWidget()
        heic_form = QFormLayout(heic_tab)

        self._heic_quality_spin = QSpinBox()
        self._heic_quality_spin.setRange(1, 100)
        self._heic_quality_spin.setSuffix(" %")
        heic_form.addRow("JPG 输出质量:", self._heic_quality_spin)

        self._heic_source_combo = QComboBox()
        for value, label in (
            ("trash", "移至回收站（推荐）"),
            ("delete", "永久删除（不可恢复）"),
            ("keep", "保留源 HEIC"),
        ):
            self._heic_source_combo.addItem(label, value)
        self._heic_source_combo.setToolTip(
            "回收站不可用（网络盘 / 无回收站分区）时一律保留源文件，绝不静默永久删除")
        heic_form.addRow("转换后源文件:", self._heic_source_combo)

        self._heic_conflict_combo = QComboBox()
        for value, label in (
            ("skip", "跳过（推荐：两边都保留）"),
            ("rename", "自动改名（name (1).jpg）"),
            ("overwrite", "覆盖同名 JPG"),
        ):
            self._heic_conflict_combo.addItem(label, value)
        heic_form.addRow("同名 JPG 冲突:", self._heic_conflict_combo)

        self._heic_reindex_check = QCheckBox("转换后立即重算哈希与人脸")
        heic_form.addRow(self._heic_reindex_check)

        tabs.addTab(heic_tab, "HEIC")

        layout.addWidget(tabs)

        # ==== 按钮 ====
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _load_values(self) -> None:
        """从当前配置加载值到界面。"""
        cfg = self._config
        self._db_path_edit.setText(str(cfg.db_path))
        self._thumb_size_spin.setValue(cfg.thumbnail_max_size)
        self._thumb_quality_spin.setValue(cfg.thumbnail_quality)
        self._api_enabled_check.setChecked(cfg.api_enabled)
        self._api_port_spin.setValue(cfg.api_port)
        self._api_host_edit.setText(cfg.api_host)
        self._web_pin_edit.setText(cfg.web_pin)
        self._watcher_enabled_check.setChecked(cfg.watcher_enabled)
        self._watcher_debounce_spin.setValue(cfg.watcher_debounce_ms)
        self._seek_percent_spin.setValue(cfg.preview_seek_percent)
        self._heic_quality_spin.setValue(int(getattr(cfg, "heic_jpeg_quality", 95)))
        idx = self._heic_source_combo.findData(
            getattr(cfg, "heic_source_action", "trash"))
        self._heic_source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        idx = self._heic_conflict_combo.findData(
            getattr(cfg, "heic_on_conflict", "skip"))
        self._heic_conflict_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._heic_reindex_check.setChecked(
            bool(getattr(cfg, "heic_reindex_after_convert", True)))

    @Slot()
    def _on_save(self) -> None:
        """保存设置并发出信号（仅携带界面可改字段，其余沿用原配置）。"""
        new_config = self._config.with_updates(
            db_path=Path(self._db_path_edit.text()),
            thumbnail_max_size=self._thumb_size_spin.value(),
            thumbnail_quality=self._thumb_quality_spin.value(),
            api_enabled=self._api_enabled_check.isChecked(),
            api_port=self._api_port_spin.value(),
            api_host=self._api_host_edit.text(),
            watcher_enabled=self._watcher_enabled_check.isChecked(),
            watcher_debounce_ms=self._watcher_debounce_spin.value(),
            preview_seek_percent=self._seek_percent_spin.value(),
            web_pin=self._web_pin_edit.text(),
            heic_jpeg_quality=self._heic_quality_spin.value(),
            heic_source_action=self._heic_source_combo.currentData(),
            heic_on_conflict=self._heic_conflict_combo.currentData(),
            heic_reindex_after_convert=self._heic_reindex_check.isChecked(),
        )
        self.settings_saved.emit(new_config)
        self.accept()

    @staticmethod
    def _browse_file(line_edit: QLineEdit, filter_str: str) -> None:
        """打开文件选择对话框并将路径写入 QLineEdit。"""
        file_path, _ = QFileDialog.getSaveFileName(
            None, "选择文件", line_edit.text(), filter_str,
        )
        if file_path:
            line_edit.setText(file_path)
