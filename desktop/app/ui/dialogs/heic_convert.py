# -*- coding: utf-8 -*-
"""
HEIC 转换对话框 —— 进度显示 + 后台线程处理。
"""

import logging
from pathlib import Path

from PySide6.QtCore import QThread, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTextEdit, QMessageBox,
)

from app.core.heic_converter import convert_batch, ConvertSummary, ConversionResult

logger = logging.getLogger(__name__)


class HeicConvertWorker(QThread):
    """后台 HEIC 转换线程。"""

    progress = Signal(int, int, object)  # completed, total, ConversionResult
    finished = Signal(object)            # ConvertSummary

    def __init__(self, sources: list[Path], overwrite: bool = False,
                 parent=None) -> None:
        super().__init__(parent)
        self._sources = sources
        self._overwrite = overwrite

    def run(self) -> None:
        summary = convert_batch(
            self._sources,
            overwrite=self._overwrite,
            progress_callback=lambda c, t, r: self.progress.emit(c, t, r),
        )
        self.finished.emit(summary)


class HeicConvertDialog(QDialog):
    """HEIC 转换进度对话框。"""

    def __init__(self, sources: list[Path], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("HEIC 转 JPG")
        self.setMinimumWidth(500)
        self.setModal(True)
        self._results: list[ConversionResult] = []
        self._summary: ConvertSummary | None = None

        layout = QVBoxLayout(self)

        # 状态文字
        self._status_label = QLabel(f"正在转换 {len(sources)} 个 HEIC 文件...")
        layout.addWidget(self._status_label)

        # 进度条
        self._progress = QProgressBar()
        self._progress.setRange(0, len(sources))
        layout.addWidget(self._progress)

        # 日志区域
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(200)
        layout.addWidget(self._log)

        # 按钮
        btn_layout = QHBoxLayout()
        self._close_btn = QPushButton("关闭")
        self._close_btn.clicked.connect(self._on_close)
        self._close_btn.setEnabled(False)
        btn_layout.addStretch()
        btn_layout.addWidget(self._close_btn)
        layout.addLayout(btn_layout)

        # 启动工作线程
        self._worker = HeicConvertWorker(sources)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    @Slot(int, int, object)
    def _on_progress(self, completed: int, total: int, result: ConversionResult) -> None:
        self._progress.setValue(completed)
        if result.success:
            self._log.append(f"✓ {result.source.name} → {result.target.name}")
        elif result.skipped:
            self._log.append(f"− {result.source.name} 已存在（跳过）")
        else:
            self._log.append(f"✗ {result.source.name} 失败: {result.error}")

    @Slot(object)
    def _on_finished(self, summary: ConvertSummary) -> None:
        self._summary = summary
        self._status_label.setText(
            f"转换完成: {summary.success} 成功, "
            f"{summary.failed} 失败, {summary.skipped} 跳过"
        )
        self._close_btn.setEnabled(True)
        self._close_btn.setText("关闭")
        self._worker.deleteLater()

    def _on_close(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait(3000)
        self.accept()

    @staticmethod
    def run_for_files(file_paths: list[str], parent=None) -> bool:
        """便捷方法：传入文件路径列表，打开转换对话框。"""
        sources = [Path(p) for p in file_paths if p.lower().endswith(".heic")]
        if not sources:
            QMessageBox.information(parent or None, "HEIC 转换", "没有选中 HEIC 文件")
            return False

        dialog = HeicConvertDialog(sources, parent)
        dialog.exec()
        return True
