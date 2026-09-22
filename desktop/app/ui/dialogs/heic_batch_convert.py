# -*- coding: utf-8 -*-
"""
HEIC 检查与转换对话框 —— 检查媒体库 → 勾选 → 批量转 JPG → 同步数据库。

与 Web 端共用 `app.services.heic_service`（同一条编排），因此：
- 源文件默认进回收站；回收站不可用（网络盘 / exFAT）时**保留源文件**并说明原因，
  绝不静默降级成永久删除；
- 转换后就地改写 `media_files` 记录（file_id 与标签保留）并重算哈希/人脸；
- 「取消」是真的会停（旧实现只在全部转换完成后才检查中断标志，等于没取消）。

支持三种入口（都由 main_window 打开本对话框）：
- 工具菜单 → 全部媒体库（默认）；
- 文件夹右键 → 预置"指定文件夹"；
- 文件右键 → 预置一个源文件并自动勾选。
"""

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QMessageBox, QWidget, QCheckBox, QComboBox, QSpinBox,
    QGroupBox, QTextEdit, QRadioButton, QButtonGroup,
)

from config import AppConfig
from app.services import heic_service as svc

logger = logging.getLogger(__name__)

_STATUS_PENDING = "等待"
_STATUS_DONE = "已转换"
_STATUS_FAILED = "失败"
_STATUS_SKIPPED = "跳过"

_CONFLICT_LABELS = [
    ("skip", "跳过（推荐：两边都保留）"),
    ("rename", "自动改名（name (1).jpg）"),
    ("overwrite", "覆盖同名 JPG"),
]
_SOURCE_LABELS = [
    ("trash", "移至回收站（推荐）"),
    ("delete", "永久删除（不可恢复）"),
    ("keep", "保留源 HEIC"),
]


def _human_size(num: int) -> str:
    """人类可读大小。"""
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _color(kind: str):
    from PySide6.QtGui import QColor
    return {
        "green": QColor(46, 204, 113),
        "red": QColor(231, 76, 60),
        "gray": QColor(150, 150, 150),
        "amber": QColor(241, 196, 15),
    }[kind]


class HeicScanWorker(QThread):
    """后台检查线程：遍历媒体库/指定文件夹里的 HEIC。"""

    finished = Signal(object)   # HeicScanResult
    error = Signal(str)

    def __init__(self, config: AppConfig, folder: Optional[str] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._folder = folder

    def run(self) -> None:
        try:
            roots = [Path(self._folder)] if self._folder else None
            result = svc.scan_candidates(self._config, roots=roots, include_disk=True)
        except Exception as e:
            logger.error(f"HEIC 检查失败: {e}", exc_info=True)
            self.error.emit(str(e))
            return
        if not self.isInterruptionRequested():
            self.finished.emit(result)


class HeicConvertWorker(QThread):
    """后台转换线程（走共用服务层，与 Web 端同一条路径）。"""

    progress = Signal(str, int, int)    # phase, cur, total
    finished = Signal(object)           # HeicOutcome
    error = Signal(str)

    def __init__(self, config: AppConfig, targets: list[str], options,
                 parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._targets = list(targets)
        self._options = options

    def cancel(self) -> None:
        """请求取消：服务层在每个文件前检查，当前文件处理完就停。"""
        self.requestInterruption()

    def run(self) -> None:
        try:
            outcome = svc.run_heic_pipeline(
                self._config, self._targets, self._options,
                progress_callback=lambda phase, cur, total: self.progress.emit(
                    phase, cur, total),
                cancelled=self.isInterruptionRequested,
            )
        except Exception as e:
            logger.error(f"HEIC 转换失败: {e}", exc_info=True)
            self.error.emit(str(e))
            return
        if not self.isInterruptionRequested() or outcome:
            self.finished.emit(outcome)


class HeicBatchConvertDialog(QDialog):
    """HEIC 检查与转换对话框。"""

    def __init__(self, config: AppConfig, parent=None, *,
                 folder: Optional[str] = None,
                 sources: Optional[list[str]] = None) -> None:
        """
        参数:
            config: 应用配置（默认选项来源）。
            folder: 预置的"指定文件夹"（右键文件夹入口）。
            sources: 预置的源文件列表（右键单个/多个文件入口），默认全部勾选。
        """
        super().__init__(parent)
        self.setWindowTitle("HEIC 检查与转换")
        self.setMinimumSize(860, 620)
        self.setModal(True)

        self._config = config
        self._preset_folder = folder
        self._preset_sources = [str(s) for s in (sources or [])]
        self._candidates: list = []
        self._row_index: dict[str, int] = {}
        self._scan_worker: Optional[HeicScanWorker] = None
        self._convert_worker: Optional[HeicConvertWorker] = None
        self._running = False

        self._build_ui()
        self._load_defaults()

        if self._preset_folder:
            self._mode_folder.setChecked(True)
            self._folder_label.setText(self._preset_folder)
        if self._preset_sources:
            # 右键单文件入口：直接按预置文件填充，省掉一次全库检查
            self._fill_preset_sources()

    # ------------------------------------------------------------
    # UI
    # ------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- 扫描范围 ----
        scope_box = QGroupBox("检查范围")
        scope_layout = QVBoxLayout(scope_box)
        row = QHBoxLayout()
        self._mode_lib = QRadioButton("全部媒体库")
        self._mode_lib.setChecked(True)
        self._mode_folder = QRadioButton("指定文件夹")
        self._group = QButtonGroup(self)
        self._group.addButton(self._mode_lib)
        self._group.addButton(self._mode_folder)
        row.addWidget(self._mode_lib)
        row.addWidget(self._mode_folder)
        self._folder_label = QLabel("（未选择）")
        self._folder_label.setStyleSheet("color: #888;")
        row.addWidget(self._folder_label, 1)
        self._browse_btn = QPushButton("浏览...")
        self._browse_btn.clicked.connect(self._on_browse)
        row.addWidget(self._browse_btn)
        self._scan_btn = QPushButton("开始检查")
        self._scan_btn.clicked.connect(self._on_scan)
        row.addWidget(self._scan_btn)
        scope_layout.addLayout(row)

        self._scan_label = QLabel("点击「开始检查」扫描媒体库里全部 HEIC 文件")
        scope_layout.addWidget(self._scan_label)
        self._warn_label = QLabel("")
        self._warn_label.setWordWrap(True)
        self._warn_label.setStyleSheet("color: #f1c40f;")
        scope_layout.addWidget(self._warn_label)
        layout.addWidget(scope_box)

        # ---- 文件表 ----
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["", "文件名", "所在目录", "大小", "感知哈希", "状态"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col in (3, 4, 5):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(0, 30)
        self._table.verticalHeader().hide()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table, 1)

        table_btns = QHBoxLayout()
        self._check_all_btn = QPushButton("全选")
        self._check_all_btn.clicked.connect(lambda: self._set_all_checked(True))
        self._uncheck_all_btn = QPushButton("全不选")
        self._uncheck_all_btn.clicked.connect(lambda: self._set_all_checked(False))
        table_btns.addWidget(self._check_all_btn)
        table_btns.addWidget(self._uncheck_all_btn)
        table_btns.addStretch()
        layout.addLayout(table_btns)

        # ---- 转换选项 ----
        opts_box = QGroupBox("转换选项")
        opts_layout = QHBoxLayout(opts_box)

        opts_layout.addWidget(QLabel("JPEG 质量:"))
        self._quality_spin = QSpinBox()
        self._quality_spin.setRange(1, 100)
        opts_layout.addWidget(self._quality_spin)

        opts_layout.addWidget(QLabel("源文件:"))
        self._source_combo = QComboBox()
        for value, label in _SOURCE_LABELS:
            self._source_combo.addItem(label, value)
        opts_layout.addWidget(self._source_combo)

        opts_layout.addWidget(QLabel("同名冲突:"))
        self._conflict_combo = QComboBox()
        for value, label in _CONFLICT_LABELS:
            self._conflict_combo.addItem(label, value)
        opts_layout.addWidget(self._conflict_combo)

        self._reindex_check = QCheckBox("转换后重算哈希/人脸")
        opts_layout.addWidget(self._reindex_check)
        self._fix_check = QCheckBox("修复旧版无法解码的哈希")
        self._fix_check.setToolTip(
            "旧版解不开 HEIC，把这些文件的 pHash/dHash 写成了失败占位（空串），"
            "它们永远不会被自动重算 —— 保留此勾选可一并修复。")
        opts_layout.addWidget(self._fix_check)
        opts_layout.addStretch()
        layout.addWidget(opts_box)

        # ---- 进度与日志 ----
        self._phase_label = QLabel("")
        layout.addWidget(self._phase_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(120)
        layout.addWidget(self._log)

        self._summary_label = QLabel("")
        layout.addWidget(self._summary_label)

        # ---- 底部按钮 ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._convert_btn = QPushButton("开始转换")
        self._convert_btn.clicked.connect(self._on_start_convert)
        self._convert_btn.setEnabled(False)
        btn_row.addWidget(self._convert_btn)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._on_cancel_convert)
        self._cancel_btn.setEnabled(False)
        btn_row.addWidget(self._cancel_btn)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

    def _load_defaults(self) -> None:
        """用配置里的默认值预填选项。"""
        opts = svc.default_options(self._config)
        self._quality_spin.setValue(int(opts.quality))
        idx = self._source_combo.findData(opts.source_action)
        self._source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        idx = self._conflict_combo.findData(opts.on_conflict)
        self._conflict_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._reindex_check.setChecked(bool(opts.reindex))
        self._fix_check.setChecked(True)

    # ------------------------------------------------------------
    # 检查
    # ------------------------------------------------------------

    @Slot()
    def _on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择包含 HEIC 文件的文件夹")
        if not folder:
            return
        self._mode_folder.setChecked(True)
        self._folder_label.setText(folder)
        self._folder_label.setStyleSheet("color: #fff;")

    @Slot()
    def _on_scan(self) -> None:
        if self._scan_worker and self._scan_worker.isRunning():
            return
        folder = None
        if self._mode_folder.isChecked():
            folder = self._folder_label.text()
            if not folder or not Path(folder).is_dir():
                QMessageBox.information(self, "HEIC 检查", "请先选择一个有效的文件夹")
                return

        self._scan_btn.setEnabled(False)
        self._scan_label.setText("正在检查…")
        # 上一次的预置勾选状态在重新检查后作废
        self._preset_sources = []
        self._scan_worker = HeicScanWorker(self._config, folder, parent=self)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error.connect(self._on_scan_error)
        self._scan_worker.start()

    @Slot(object)
    def _on_scan_finished(self, result) -> None:
        self._scan_btn.setEnabled(True)
        self._candidates = list(result.candidates)
        self._populate_table(self._candidates)

        parts = [f"共 {result.total} 个 HEIC（{_human_size(result.total_bytes)}）"]
        if result.from_disk_only:
            parts.append(f"其中 {result.from_disk_only} 个尚未入库")
        self._scan_label.setText(" · ".join(parts))

        warns: list[str] = []
        if result.skipped_volume_roots:
            warns.append("磁盘未连接，已跳过：" + "、".join(result.skipped_volume_roots)
                         + "（插回磁盘后重新检查）")
        if result.broken_hash_count:
            warns.append(f"{result.broken_hash_count} 个 HEIC 的感知哈希/人脸是空的"
                         "（旧版解不开 HEIC），勾选「修复旧版无法解码的哈希」可一并修复")
        if result.invalid_header:
            warns.append(f"{result.invalid_header} 个文件扩展名是 HEIC 但内容不是"
                         "（多为改名的 JPEG），已跳过")
        if result.missing_count:
            warns.append(f"{result.missing_count} 条库记录在磁盘上已找不到")
        self._warn_label.setText("\n".join(warns))

        self._convert_btn.setEnabled(bool(self._candidates))
        self._convert_btn.setText("开始转换")

    @Slot(str)
    def _on_scan_error(self, message: str) -> None:
        self._scan_btn.setEnabled(True)
        self._scan_label.setText("检查失败")
        QMessageBox.warning(self, "HEIC 检查失败", message)

    def _fill_preset_sources(self) -> None:
        """右键单文件入口：不走全库检查，直接把预置文件填进表。"""
        rows = []
        for path in self._preset_sources:
            p = Path(path)
            try:
                size = p.stat().st_size if p.is_file() else 0
            except OSError:
                size = 0
            rows.append(svc.HeicCandidate(
                path=str(p), size_bytes=size, exists=p.is_file(), volume_ok=True))
        self._candidates = rows
        self._populate_table(rows)
        self._scan_label.setText(f"已选择 {len(rows)} 个文件（未做全库检查）")
        self._convert_btn.setEnabled(bool(rows))

    def _populate_table(self, candidates: list) -> None:
        self._table.setRowCount(0)
        self._table.setRowCount(len(candidates))
        self._row_index = {}
        for i, c in enumerate(candidates):
            cb = QCheckBox()
            # 磁盘未连接 / 文件已丢失 / 内容不是 HEIC 的不能勾选
            cb.setChecked(bool(c.exists and c.volume_ok))
            cb.setEnabled(bool(c.exists and c.volume_ok))
            holder = QWidget()
            box = QHBoxLayout(holder)
            box.addWidget(cb)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setContentsMargins(0, 0, 0, 0)
            self._table.setCellWidget(i, 0, holder)

            self._table.setItem(i, 1, QTableWidgetItem(Path(c.path).name))
            self._table.setItem(i, 2, QTableWidgetItem(
                c.unit_name or str(Path(c.path).parent)))

            size_item = QTableWidgetItem(_human_size(c.size_bytes))
            if not c.volume_ok:
                size_item.setText("磁盘未连接")
                size_item.setForeground(_color("red"))
            self._table.setItem(i, 3, size_item)

            hash_item = QTableWidgetItem("正常" if c.has_perceptual_hash else "缺失")
            hash_item.setForeground(_color("gray" if c.has_perceptual_hash else "amber"))
            if not c.has_perceptual_hash:
                hash_item.setToolTip("旧版解不开 HEIC 时留下的失败占位，查重看不见这张图")
            self._table.setItem(i, 4, hash_item)

            status = _STATUS_PENDING
            if not c.volume_ok:
                status = "磁盘未连接"
            elif not c.exists:
                status = "文件已丢失"
            self._table.setItem(i, 5, QTableWidgetItem(status))
            self._row_index[str(c.path).lower()] = i

    def _set_all_checked(self, checked: bool) -> None:
        for i in range(self._table.rowCount()):
            holder = self._table.cellWidget(i, 0)
            if holder is None:
                continue
            cb = holder.findChild(QCheckBox)
            if cb and cb.isEnabled():
                cb.setChecked(checked)

    def _checked_paths(self) -> list[str]:
        paths: list[str] = []
        for i in range(self._table.rowCount()):
            holder = self._table.cellWidget(i, 0)
            if holder is None:
                continue
            cb = holder.findChild(QCheckBox)
            if cb and cb.isChecked():
                paths.append(self._candidates[i].path)
        return paths

    # ------------------------------------------------------------
    # 转换
    # ------------------------------------------------------------

    @Slot()
    def _on_start_convert(self) -> None:
        if self._running:
            return
        targets = self._checked_paths()
        if not targets:
            QMessageBox.information(self, "HEIC 转换", "没有勾选需要转换的文件")
            return

        options = svc.HeicConvertOptions(
            quality=self._quality_spin.value(),
            source_action=self._source_combo.currentData(),
            on_conflict=self._conflict_combo.currentData(),
            reindex=self._reindex_check.isChecked(),
            fix_broken=self._fix_check.isChecked(),
        )
        if (options.source_action == svc.SOURCE_DELETE and not self._confirm_permanent()):
            return

        self._running = True
        self._log.clear()
        self._summary_label.setText("")
        self._progress.setValue(0)
        self._progress.setMaximum(len(targets))
        self._phase_label.setText("正在转换…")
        self._convert_btn.setEnabled(False)
        self._scan_btn.setEnabled(False)
        self._browse_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)

        for path in targets:
            self._set_status(path, "转换中", "gray")

        self._convert_worker = HeicConvertWorker(
            self._config, targets, options, parent=self)
        self._convert_worker.progress.connect(self._on_convert_progress)
        self._convert_worker.finished.connect(self._on_convert_finished)
        self._convert_worker.error.connect(self._on_convert_error)
        self._convert_worker.start()

    def _confirm_permanent(self) -> bool:
        """永久删除是不可恢复操作：必须用户显式确认。"""
        answer = QMessageBox.warning(
            self, "确认永久删除源文件",
            "你选择了「永久删除」源 HEIC 文件，删除后无法从回收站恢复。\n"
            "确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    @Slot(str, int, int)
    def _on_convert_progress(self, phase: str, cur: int, total: int) -> None:
        labels = {
            "converting": "正在转换",
            "source": "正在处理源文件",
            "db": "正在同步媒体库",
            "indexing": "正在重算哈希/人脸",
        }
        label = labels.get(phase, phase)
        self._phase_label.setText(f"{label} {cur}/{total}" if total else label)
        if total:
            self._progress.setMaximum(total)
            self._progress.setValue(cur)

    @Slot(object)
    def _on_convert_finished(self, outcome) -> None:
        self._running = False
        self._cancel_btn.setEnabled(False)
        self._scan_btn.setEnabled(True)
        self._browse_btn.setEnabled(True)
        self._convert_btn.setEnabled(True)

        for item in outcome.items:
            if item.status == "converted":
                note = "源文件保留" if item.source_kept_reason else ""
                self._set_status(item.path, _STATUS_DONE + (f"（{note}）" if note else ""),
                                 "green")
                self._log.append(f"✓ {Path(item.path).name} → {Path(item.target or '').name}")
            elif item.status == "skipped":
                self._set_status(item.path, _STATUS_SKIPPED, "gray")
                self._log.append(f"− {Path(item.path).name} 跳过：{item.error}")
            else:
                self._set_status(item.path, _STATUS_FAILED, "red")
                self._log.append(f"✗ {Path(item.path).name} 失败：{item.error}")

        summary = (f"成功 {outcome.converted} · 跳过 {outcome.skipped} · "
                   f"失败 {outcome.failed}；源文件移除 {outcome.sources_removed} / "
                   f"保留 {outcome.sources_kept}")
        if outcome.db_updated:
            summary += f"；媒体库记录更新 {outcome.db_updated}"
        if outcome.healed:
            summary += f"；修复坏哈希 {outcome.healed}"
        if outcome.reindexed:
            summary += f"；重算 {outcome.reindexed}"
        if outcome.cancelled:
            summary += "（已取消）"
        summary += f"；耗时 {outcome.elapsed_seconds}s"
        self._summary_label.setText(summary)
        self._phase_label.setText("完成" if not outcome.cancelled else "已取消")

        # 转换过的文件不再需要出现在待办列表里
        self._convert_btn.setText("再次转换")
        if outcome.converted and not outcome.cancelled and self._mode_lib.isChecked():
            self._scan_label.setText("提示：可再次点击「开始检查」刷新列表")

    @Slot(str)
    def _on_convert_error(self, message: str) -> None:
        self._running = False
        self._cancel_btn.setEnabled(False)
        self._scan_btn.setEnabled(True)
        self._browse_btn.setEnabled(True)
        self._convert_btn.setEnabled(True)
        self._phase_label.setText("转换失败")
        QMessageBox.warning(self, "HEIC 转换失败", message)

    @Slot()
    def _on_cancel_convert(self) -> None:
        if self._convert_worker and self._convert_worker.isRunning():
            self._convert_worker.cancel()
            self._phase_label.setText("正在停止（当前文件处理完就停）…")
            self._cancel_btn.setEnabled(False)

    def _set_status(self, path: str, text: str, color: str) -> None:
        row = self._row_index.get(str(path).lower())
        if row is None:
            return
        item = self._table.item(row, 5)
        if item is None:
            return
        item.setText(text)
        item.setForeground(_color(color))

    # ------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------

    def _on_close(self) -> None:
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.requestInterruption()
            self._scan_worker.wait(3000)
        if self._convert_worker and self._convert_worker.isRunning():
            if not self._confirm_close_while_running():
                return
            self._convert_worker.cancel()
            self._convert_worker.wait(5000)
        self.accept()

    def _confirm_close_while_running(self) -> bool:
        """转换中关闭：明确告诉用户会停下，由用户确认。"""
        answer = QMessageBox.question(
            self, "转换尚未结束",
            "转换仍在进行。关闭窗口会请求停止（已转换的文件保留）。\n继续关闭吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def closeEvent(self, event) -> None:
        if self._convert_worker and self._convert_worker.isRunning():
            if not self._confirm_close_while_running():
                event.ignore()
                return
            self._convert_worker.cancel()
            self._convert_worker.wait(5000)
        super().closeEvent(event)

    def reject(self) -> None:
        if self._convert_worker and self._convert_worker.isRunning():
            if not self._confirm_close_while_running():
                return
            self._convert_worker.cancel()
            self._convert_worker.wait(5000)
        super().reject()
