# -*- coding: utf-8 -*-
"""
HEIC 批量转换对话框 —— 选择文件夹 → 自动扫描 HEIC → 批量转 JPG → 自动删源文件。
"""

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QHeaderView,
    QFileDialog, QMessageBox, QWidget, QCheckBox,
)

from app.core.heic_converter import convert_batch, ConversionResult, is_heic_file

logger = logging.getLogger(__name__)

_STATUS_PENDING = "等待"
_STATUS_SUCCESS = "已完成"
_STATUS_FAILED = "失败"
_STATUS_SKIPPED = "跳过"


def _green():
    from PySide6.QtGui import QColor
    return QColor(46, 204, 113)


def _red():
    from PySide6.QtGui import QColor
    return QColor(231, 76, 60)


def _gray():
    from PySide6.QtGui import QColor
    return QColor(150, 150, 150)


class _HeicBatchWorker(QThread):
    """后台 HEIC 批量转换线程。"""

    progress = Signal(int, int, object)  # completed, total, ConversionResult
    finished = Signal(object)            # ConvertSummary

    def __init__(self, sources: list[Path], parent=None) -> None:
        super().__init__(parent)
        self._sources = sources

    def run(self) -> None:
        try:
            summary = convert_batch(
                self._sources,
                overwrite=False,
                progress_callback=lambda c, t, r: self.progress.emit(c, t, r),
            )
        except Exception as e:
            from app.core.heic_converter import ConvertSummary
            logger.error(f"HEIC 批量转换异常: {e}")
            summary = ConvertSummary(
                total=len(self._sources), failed=len(self._sources)
            )
        if not self.isInterruptionRequested():
            self.finished.emit(summary)


class HeicBatchConvertDialog(QDialog):
    """HEIC 批量转 JPG 对话框。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("HEIC 批量转 JPG")
        self.setMinimumSize(680, 480)
        self.setModal(True)

        self._sources: list[Path] = []
        self._worker: _HeicBatchWorker | None = None
        self._converting = False

        layout = QVBoxLayout(self)

        # ── 文件夹选择行 ──
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("文件夹:"))
        self._path_label = QLabel("（未选择）")
        self._path_label.setStyleSheet("color: #888;")
        folder_row.addWidget(self._path_label, 1)
        self._browse_btn = QPushButton("浏览...")
        self._browse_btn.clicked.connect(self._on_browse)
        folder_row.addWidget(self._browse_btn)
        layout.addLayout(folder_row)

        # ── 状态文字 ──
        self._status_label = QLabel("")
        layout.addWidget(self._status_label)

        # ── 文件列表 ──
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["", "文件名", "大小", "状态"])
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setColumnWidth(0, 30)
        self._table.verticalHeader().hide()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        # ── 进度条 ──
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # ── 统计摘要 ──
        self._summary_label = QLabel("")
        layout.addWidget(self._summary_label)

        # ── 按钮行 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._convert_btn = QPushButton("开始转换")
        self._convert_btn.clicked.connect(self._on_start_convert)
        self._convert_btn.setEnabled(False)
        btn_row.addWidget(self._convert_btn)
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self._on_close)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

    # ── slots ──

    @Slot()
    def _on_browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择包含 HEIC 文件的文件夹")
        if not folder:
            return

        folder_path = Path(folder)
        self._path_label.setText(str(folder_path))
        self._path_label.setStyleSheet("color: #fff;")

        # 递归扫描 HEIC 文件
        heic_files: list[Path] = []
        for pattern in ("*.heic", "*.HEIC", "*.heif", "*.HEIF"):
            for f in folder_path.rglob(pattern):
                if f.is_file() and is_heic_file(f):
                    heic_files.append(f)

        self._sources = heic_files
        self._populate_table()
        self._status_label.setText(f"找到 {len(heic_files)} 个 HEIC 文件")
        self._convert_btn.setEnabled(len(heic_files) > 0)

    def _populate_table(self) -> None:
        self._table.setRowCount(0)
        self._table.setRowCount(len(self._sources))
        for i, src in enumerate(self._sources):
            # checkbox
            cb = QCheckBox()
            cb.setChecked(True)
            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.addWidget(cb)
            cb_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb_layout.setContentsMargins(0, 0, 0, 0)
            self._table.setCellWidget(i, 0, cb_widget)

            # 文件名
            self._table.setItem(i, 1, QTableWidgetItem(src.name))
            # 大小
            size_kb = src.stat().st_size / 1024
            size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
            self._table.setItem(i, 2, QTableWidgetItem(size_str))
            # 状态
            self._table.setItem(i, 3, QTableWidgetItem(_STATUS_PENDING))

    @Slot()
    def _on_start_convert(self) -> None:
        if self._converting:
            return

        sources = [
            self._sources[i]
            for i in range(self._table.rowCount())
            if self._table.cellWidget(i, 0).findChild(QCheckBox).isChecked()
        ]
        if not sources:
            QMessageBox.information(self, "HEIC 转换", "没有勾选需要转换的文件")
            return

        self._converting = True
        self._convert_btn.setEnabled(False)
        self._browse_btn.setEnabled(False)
        self._progress.setValue(0)
        self._summary_label.setText("")

        self._worker = _HeicBatchWorker(sources, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    @Slot(int, int, object)
    def _on_progress(self, completed: int, total: int, result: ConversionResult) -> None:
        self._progress.setMaximum(total)
        self._progress.setValue(completed)

        # 更新对应行的状态
        for i in range(self._table.rowCount()):
            src_path = self._sources[i]
            if src_path == result.source:
                status_item = self._table.item(i, 3)
                if result.success:
                    status_item.setText(_STATUS_SUCCESS)
                    status_item.setForeground(_green())
                elif result.skipped:
                    status_item.setText(_STATUS_SKIPPED)
                    status_item.setForeground(_gray())
                else:
                    status_item.setText(f"{_STATUS_FAILED}: {result.error}")
                    status_item.setForeground(_red())
                break

    @Slot(object)
    def _on_finished(self, summary) -> None:
        self._summary_label.setText(
            f"总计 {summary.total} | 成功 {summary.success} | 失败 {summary.failed} | 跳过 {summary.skipped}"
        )
        self._convert_btn.setText("已完成")
        self._browse_btn.setEnabled(True)
        self._converting = False

    def _on_close(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait(3000)
        self.accept()

    def closeEvent(self, event) -> None:
        self._on_close()
        super().closeEvent(event)

    def reject(self) -> None:
        self._on_close()
        super().reject()
