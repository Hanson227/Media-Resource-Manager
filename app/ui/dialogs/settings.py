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
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QDialogButtonBox, QFileDialog, QPushButton, QHBoxLayout,
    QGroupBox,
)

from config import AppConfig
from app.ui.theme import BASE, TEXT, SURFACE_0, OVERLAY_0


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
        self.setPalette(palette)

        layout = QVBoxLayout(self)

        tabs = QTabWidget()

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
        self._watcher_enabled_check.setChecked(cfg.watcher_enabled)
        self._watcher_debounce_spin.setValue(cfg.watcher_debounce_ms)

    @Slot()
    def _on_save(self) -> None:
        """保存设置并发出信号。"""
        # 构建新的配置（由于 AppConfig 是 frozen，需要创建新实例）
        new_config = AppConfig(
            db_path=Path(self._db_path_edit.text()),
            thumbnail_max_size=self._thumb_size_spin.value(),
            thumbnail_quality=self._thumb_quality_spin.value(),
            api_enabled=self._api_enabled_check.isChecked(),
            api_port=self._api_port_spin.value(),
            api_host=self._api_host_edit.text(),
            watcher_enabled=self._watcher_enabled_check.isChecked(),
            watcher_debounce_ms=self._watcher_debounce_spin.value(),
            # 以下保持原值
            media_extensions=self._config.media_extensions,
            exclude_patterns=self._config.exclude_patterns,
            hash_algorithms=self._config.hash_algorithms,
            phash_size=self._config.phash_size,
            dhash_size=self._config.dhash_size,
            video_frame_interval_sec=self._config.video_frame_interval_sec,
            jaccard_threshold=self._config.jaccard_threshold,
            phash_hamming_threshold=self._config.phash_hamming_threshold,
            dhash_hamming_threshold=self._config.dhash_hamming_threshold,
            face_distance_threshold=self._config.face_distance_threshold,
            face_detection_enabled=self._config.face_detection_enabled,
            face_model_dir=self._config.face_model_dir,
            face_confidence_threshold=self._config.face_confidence_threshold,
            window_title=self._config.window_title,
            window_width=self._config.window_width,
            window_height=self._config.window_height,
            grid_column_count=self._config.grid_column_count,
            grid_spacing=self._config.grid_spacing,
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
