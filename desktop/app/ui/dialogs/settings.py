# -*- coding: utf-8 -*-
"""
应用设置对话框 —— 全局配置的图形化管理界面。

管理：
- 扫描与数据库设置
- 缩略图设置
- API 服务设置
- 文件监控设置
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QPalette, QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QTabWidget, QWidget, QFormLayout,
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox,
    QDialogButtonBox, QFileDialog, QPushButton, QHBoxLayout,
    QGroupBox, QLabel, QMessageBox,
)

from config import AppConfig
from app.ui.theme import BASE, TEXT, SURFACE_0, OVERLAY_0, INDIGO

logger = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    """应用全局设置对话框。

    信号:
        settings_saved: 用户保存新配置时发出 (new_config: AppConfig)。
        data_root_migrated: 「迁移...」成功后**立刻**发出（携带新配置），
            调用方必须马上落盘 —— 否则用户只点了迁移结果提示框的确定就重启，
            会看到"迁移完成但还指向旧位置"（真实事故）。
    """

    settings_saved = Signal(object)  # AppConfig
    data_root_migrated = Signal(object)  # AppConfig

    def __init__(self, config: AppConfig, parent=None) -> None:
        """初始化设置对话框。

        参数:
            config: 当前应用配置。
        """
        super().__init__(parent)
        self._config = config
        # 数据根迁移的目标（用户点了「迁移...」并成功后才有值）
        self._pending_data_root = None
        self._pending_db_path = None
        self._pending_thumb_dir = None
        self._migrate_worker = None
        self.setWindowTitle("设置")
        self.resize(560, 520)

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

        # ==== 数据位置选项卡（便携版核心：数据可以放在移动盘上）====
        data_tab = QWidget()
        data_form = QFormLayout(data_tab)

        root_layout = QHBoxLayout()
        self._data_root_label = QLineEdit()
        self._data_root_label.setReadOnly(True)
        self._data_root_label.setToolTip(
            "数据根目录：数据库/缩略图/Web 密码的存放位置。留空表示使用应用目录。")
        root_layout.addWidget(self._data_root_label)
        migrate_btn = QPushButton("迁移...")
        migrate_btn.setToolTip("把当前数据库、缩略图缓存与访问密码复制到另一块盘/另一个目录")
        migrate_btn.clicked.connect(self._on_migrate_data_root)
        root_layout.addWidget(migrate_btn)
        data_form.addRow("数据根目录:", root_layout)

        thumb_dir_layout = QHBoxLayout()
        self._thumb_dir_edit = QLineEdit()
        self._thumb_dir_edit.setToolTip(
            "缩略图集中缓存目录。保存后立即生效（无需重启），旧缓存会一并复制过去。")
        thumb_dir_layout.addWidget(self._thumb_dir_edit)
        thumb_dir_btn = QPushButton("浏览...")
        thumb_dir_btn.clicked.connect(
            lambda: self._browse_dir(self._thumb_dir_edit))
        thumb_dir_layout.addWidget(thumb_dir_btn)
        data_form.addRow("缩略图缓存目录:", thumb_dir_layout)

        self._cache_stat_label = QLabel("—")
        data_form.addRow("缓存占用:", self._cache_stat_label)

        data_note = QLabel(
            "便携用法：把数据根指向移动硬盘上的目录，程序会在该目录留下标记文件；\n"
            "换机器后即使盘符变了，也会自动找到它（媒体库路径可在 Web 端「媒体库」页修复）。")
        data_note.setWordWrap(True)
        data_form.addRow(data_note)

        tabs.addTab(data_tab, "数据位置")

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
        self._data_root_label.setText(
            str(cfg.data_root) if cfg.data_root else "（未设置：使用应用目录）")
        self._thumb_dir_edit.setText(str(cfg.thumbnail_cache_dir))
        self._refresh_cache_stat(str(cfg.thumbnail_cache_dir))
        idx = self._heic_source_combo.findData(
            getattr(cfg, "heic_source_action", "trash"))
        self._heic_source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        idx = self._heic_conflict_combo.findData(
            getattr(cfg, "heic_on_conflict", "skip"))
        self._heic_conflict_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._heic_reindex_check.setChecked(
            bool(getattr(cfg, "heic_reindex_after_convert", True)))

    def _refresh_cache_stat(self, cache_dir: str) -> None:
        """显示缩略图缓存目录的占用（让用户知道搬过去要多少空间）。"""
        try:
            from app.utils.file_helpers import format_size

            directory = Path(cache_dir)
            files = 0
            total = 0
            if directory.is_dir():
                for pattern in ("*_thumb.jpg", "*_thumb.jpg.meta"):
                    for entry in directory.glob(pattern):
                        try:
                            total += entry.stat().st_size
                            files += 1
                        except OSError:
                            continue
            self._cache_stat_label.setText(f"{files} 个文件 · {format_size(total)}")
        except Exception:  # noqa: BLE001 — 只是展示信息
            self._cache_stat_label.setText("—")

    def _on_migrate_data_root(self) -> None:
        """把数据（数据库/缩略图/PIN）迁移到用户选择的目录。

        复制在**后台线程**里跑 + 模态进度框：几千个缩略图文件要复制很久，同步跑
        会让窗口假死、用户以为没反应而关掉程序，最后留下半份数据（真实事故）。

        只在对话框里做"选目录 + 复制 + 记下结果"，真正的路径解析与重启生效
        由 main 入口负责（避免在这里热切数据库）。
        """
        from PySide6.QtWidgets import QProgressDialog
        from app.ui.workers.data_relocation_worker import DataRelocationWorker

        folder = QFileDialog.getExistingDirectory(
            self, "选择新的数据根目录（建议选移动硬盘上的文件夹）",
            self._data_root_label.text() if str(self._config.data_root or "") else str(Path.home()))
        if not folder:
            return
        target = Path(folder)
        reply = QMessageBox.question(
            self, "迁移数据",
            f"将把当前数据库、缩略图缓存和 Web 访问密码复制到:\n  {target}\n\n"
            "复制完成后需要重启程序才会切换到新位置（源文件保留）。\n\n确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        progress = QProgressDialog("正在复制数据库…", "", 0, 1, self)
        progress.setWindowTitle("迁移数据")
        progress.setCancelButton(None)      # 不允许"取消"：半途取消就是半份数据
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()

        self._migrate_worker = DataRelocationWorker(self._config, target, mode="copy", parent=self)
        self._migrate_worker.progress_changed.connect(
            lambda phase, done, total: self._on_migrate_progress(progress, phase, done, total))
        self._migrate_worker.finished_with.connect(
            lambda result: self._on_migrate_finished(result, progress))
        self._migrate_worker.start()

    _PHASE_LABELS = {
        "database": "正在复制数据库…",
        "thumbnails": "正在复制缩略图",
        "pin": "正在复制访问密码…",
        "marker": "正在写入数据盘标记…",
    }

    @Slot(str, int, int)
    def _on_migrate_progress(self, progress, phase: str, done: int, total: int) -> None:
        """把服务层进度映射到进度条（缩略图阶段给出 x/y 与百分比）。"""
        label = self._PHASE_LABELS.get(phase, phase)
        if total > 1 and phase == "thumbnails":
            percent = int(done * 100 / total) if total else 0
            progress.setLabelText(f"{label} {done}/{total}（{percent}%）")
            progress.setMaximum(total)
            progress.setValue(done)
        else:
            progress.setLabelText(label)
            progress.setMaximum(1)
            progress.setValue(1 if done >= total else 0)

    @Slot(dict, object)
    def _on_migrate_finished(self, result: dict, progress=None) -> None:
        """迁移完成（主线程）：关进度框、给明确结果，并**立刻持久化**。

        为什么必须立刻落盘：迁移成功后若只把结果挂在对话框里等用户再点一次
        「确定」，用户很容易直接关掉对话框（或只点了结果提示框的确定），
        结果就是"迁移显示完成了、重启还是旧位置"（真实事故）。
        因此这里直接发 `data_root_migrated` 信号让主窗口立刻写配置。
        """
        if progress is not None:
            progress.close()
        if not result.get("ok"):
            QMessageBox.critical(
                self, "迁移失败",
                f"{result.get('message', '未知错误')}\n\n"
                "本次未改动配置；已复制的文件会被自动回滚（目标目录本来是空的）。")
            return
        target = Path(result["target_root"])
        db_step = result["steps"].get("database", {})
        thumb = result["steps"].get("thumbnails", {})
        self._pending_data_root = target
        self._pending_db_path = Path(result["db_path"])
        self._pending_thumb_dir = Path(result["thumbnail_cache_dir"])
        self._data_root_label.setText(str(target))
        self._thumb_dir_edit.setText(str(result["thumbnail_cache_dir"]))
        self._refresh_cache_stat(str(result["thumbnail_cache_dir"]))

        # 立刻保存（不依赖用户再点一次「确定」）
        new_config = self._config.with_updates(
            data_root=self._pending_data_root,
            db_path=self._pending_db_path,
            thumbnail_cache_dir=self._pending_thumb_dir,
        )
        self._config = new_config
        self.data_root_migrated.emit(new_config)

        QMessageBox.information(
            self, "迁移完成",
            f"数据已复制到:\n  {target}\n\n"
            f"数据库校验: {db_step.get('message', '')}\n"
            f"缩略图: {thumb.get('copied', 0)} 个文件\n\n"
            "**配置已保存**：重启程序后就会使用这个新位置。")

    @Slot()
    def _on_save(self) -> None:
        """保存设置并发出信号（仅携带界面可改字段，其余沿用原配置）。"""
        updates = dict(
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

        # 缩略图缓存目录：改动时先把旧缓存复制过去（否则等于让用户白白重生成一遍）
        new_thumb_dir = Path(self._thumb_dir_edit.text().strip() or
                             str(self._config.thumbnail_cache_dir))
        old_thumb_dir = Path(self._config.thumbnail_cache_dir)
        if new_thumb_dir != old_thumb_dir:
            try:
                from app.services.data_relocation_service import copy_tree
                copied = copy_tree(old_thumb_dir, new_thumb_dir, move=False)
                logger.info(f"缩略图缓存迁移: 复制 {copied.get('copied', 0)} 个文件 → {new_thumb_dir}")
            except Exception as e:  # noqa: BLE001 — 复制失败不阻塞设置保存
                logger.warning(f"缩略图缓存迁移失败（将按需重新生成）: {e}")
            updates["thumbnail_cache_dir"] = new_thumb_dir

        # 数据根迁移（对话框里的「迁移...」成功后才有值）：以迁移服务的实际落点为准
        if self._pending_data_root is not None:
            updates["data_root"] = self._pending_data_root
            updates["db_path"] = self._pending_db_path
            updates["thumbnail_cache_dir"] = self._pending_thumb_dir

        new_config = self._config.with_updates(**updates)
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

    @staticmethod
    def _browse_dir(line_edit: QLineEdit) -> None:
        """打开目录选择对话框并将路径写入 QLineEdit。"""
        folder = QFileDialog.getExistingDirectory(
            None, "选择目录", line_edit.text() or str(Path.home()))
        if folder:
            line_edit.setText(folder)
