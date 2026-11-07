# -*- coding: utf-8 -*-
"""查重结果列表对话框 —— 一次展示本轮所有重复对，逐条打开对比处置。

Web 端一直有「结果列表 → 点进详情处置」的流程；桌面端原来只弹第一对，
其余结果只能靠消息中心翻。这里补上同一套入口，避免两端功能不对称。
"""

import logging

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from config import AppConfig
from app.core.dedup_engine import UnitComparisonResult
from app.ui.theme import SUBTEXT_0

logger = logging.getLogger(__name__)


class DedupResultsDialog(QDialog):
    """本轮查重结果列表。

    信号:
        action_requested: (unit_a_id, unit_b_id, resolution)
            resolution 取 keep_a / keep_b / whitelist / ignore，由主窗口统一落库处置。
    """

    action_requested = Signal(int, int, str)

    def __init__(self, results, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._results: list[UnitComparisonResult] = list(results)
        self._config = config
        self._resolved_rows: set[int] = set()

        self.setWindowTitle(f"查重结果（{len(self._results)} 组重复）")
        self.resize(980, 560)
        self._setup_ui()
        self._fill_table()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "双击一行或点「查看并处置」打开并排对比；处置后该行会标记为已处理。"
        )
        hint.setStyleSheet(f"color: {SUBTEXT_0}; padding: 2px;")
        layout.addWidget(hint)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["单元 A", "单元 B", "相似度", "匹配文件", "状态"]
        )
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.cellDoubleClicked.connect(lambda *_: self._open_current())
        layout.addWidget(self._table, 1)

        button_row = QHBoxLayout()
        self._open_btn = QPushButton("查看并处置")
        self._open_btn.clicked.connect(self._open_current)
        button_row.addWidget(self._open_btn)
        button_row.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        button_row.addWidget(box)
        layout.addLayout(button_row)

    def _fill_table(self) -> None:
        self._table.setRowCount(len(self._results))
        for row, result in enumerate(self._results):
            a_item = QTableWidgetItem(result.unit_a_name)
            b_item = QTableWidgetItem(result.unit_b_name)
            a_item.setToolTip(result.unit_a_name)
            b_item.setToolTip(result.unit_b_name)
            score_item = QTableWidgetItem(f"{result.jaccard_similarity * 100:.1f}%")
            match_item = QTableWidgetItem(str(len(result.file_matches)))
            status_item = QTableWidgetItem("未处理")
            for col, item in enumerate(
                (a_item, b_item, score_item, match_item, status_item)
            ):
                if col >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._table.setItem(row, col, item)

    def _current_row(self) -> int:
        rows = self._table.selectionModel().selectedRows(0)
        return rows[0].row() if rows else -1

    @Slot()
    def _open_current(self) -> None:
        row = self._current_row()
        if row < 0 or row >= len(self._results):
            return
        from app.ui.dialogs.dedup_compare import DedupCompareDialog

        result = self._results[row]
        dialog = DedupCompareDialog(result, self._config, self)
        dialog.keep_a_requested.connect(
            lambda a, b: self._handle_action(row, a, b, "keep_a")
        )
        dialog.keep_b_requested.connect(
            lambda a, b: self._handle_action(row, a, b, "keep_b")
        )
        dialog.whitelist_requested.connect(
            lambda a, b: self._handle_action(row, a, b, "whitelist")
        )
        dialog.ignore_requested.connect(
            lambda a, b: self._handle_action(row, a, b, "ignore")
        )
        dialog.exec()

    def _handle_action(self, row: int, unit_a: int, unit_b: int, resolution: str) -> None:
        self.action_requested.emit(unit_a, unit_b, resolution)
        self._resolved_rows.add(row)
        item = self._table.item(row, 4)
        if item is not None:
            labels = {
                "keep_a": "已保留 A",
                "keep_b": "已保留 B",
                "whitelist": "已加白名单",
                "ignore": "已忽略",
            }
            item.setText(labels.get(resolution, "已处理"))
