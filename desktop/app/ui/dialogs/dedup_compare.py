# -*- coding: utf-8 -*-
"""
查重对比对话框 —— 并排展示两个资源单元及其匹配文件。

三栏布局：
- 左栏：单元 A 的文件缩略图列表
- 中栏：匹配文件对表格 + 杰卡德得分 + 操作按钮
- 右栏：单元 B 的文件缩略图列表

用户可以在此对话框中：
- 逐对查看匹配文件的缩略图对比
- 选择保留单元 A 或单元 B 的文件
- 标记为白名单
- 执行批量删除或移动
"""

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QVBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QPushButton, QGroupBox,
    QHeaderView, QSplitter, QListWidget, QListWidgetItem,
    QDialogButtonBox, QMessageBox, QWidget,
)

from config import AppConfig
from app.core.dedup_engine import UnitComparisonResult, FileMatch
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.ui.theme import SUBTEXT_0, RED, TEXT

logger = logging.getLogger(__name__)


class DedupCompareDialog(QDialog):
    """查重对比对话框。

    信号:
        keep_a_requested: 保留单元 A 的所有文件 (unit_a_id, unit_b_id)。
        keep_b_requested: 保留单元 B 的所有文件 (unit_a_id, unit_b_id)。
        whitelist_requested: 加入白名单 (unit_a_id, unit_b_id)。
        ignore_requested: 忽略此对比结果 (unit_a_id, unit_b_id)。

    注意：对话框持有的是内存中的 UnitComparisonResult（尚未持久化），
    因此处置信号携带单元对 ID，由调用方按 (unit_a, unit_b) 解析数据库中的
    dedup_results 记录（参见 queries.resolve_dedup_pair）。
    """

    keep_a_requested = Signal(int, int)
    keep_b_requested = Signal(int, int)
    whitelist_requested = Signal(int, int)
    ignore_requested = Signal(int, int)

    def __init__(self, result: UnitComparisonResult, config: AppConfig,
                 parent=None) -> None:
        """初始化查重对比对话框。

        参数:
            result: 单元比对结果。
            config: 应用配置。
        """
        super().__init__(parent)
        self._result = result
        self._config = config

        self.setWindowTitle(f"查重对比: {result.unit_a_name} ⟷ {result.unit_b_name}")
        self.resize(1200, 700)

        self._setup_ui()

    def _setup_ui(self) -> None:
        """构建三栏布局 UI。"""
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # ==== 左栏：单元 A ====
        left_panel = QGroupBox(f"单元 A: {self._result.unit_a_name}")
        left_layout = QVBoxLayout(left_panel)

        left_info = QLabel(
            f"{self._result.total_files_a} 个文件\n"
            f"匹配: {len(self._result.file_matches)} 对"
        )
        left_info.setStyleSheet(f"color: {SUBTEXT_0}; padding: 4px;")
        left_layout.addWidget(left_info)

        left_list = QListWidget()
        left_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        for i in range(self._result.total_files_a):
            left_list.addItem(f"文件 {i + 1}")
        left_layout.addWidget(left_list)

        main_splitter.addWidget(left_panel)

        # ==== 中栏：匹配列表 + 操作 ====
        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)

        # 杰卡德得分 / 命中等级
        # "疑似相关"不是重复：不推"保留/删除"，只给"知道了/不再提醒"。
        is_related = getattr(self._result, "level", "duplicate") == "related"
        if is_related:
            head_text = (
                f"疑似相关（非重复）· 杰卡德 {self._result.jaccard_similarity * 100:.1f}%"
            )
            head_color = "#f9e2af"
        else:
            head_text = f"杰卡德相似度: {self._result.jaccard_similarity * 100:.1f}%"
            head_color = RED
        jaccard_label = QLabel(head_text)
        jaccard_label.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {head_color}; padding: 8px;"
        )
        jaccard_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_layout.addWidget(jaccard_label)

        if is_related:
            note = QLabel(
                "这两个单元有同演员 / 同场景 / 部分文件重叠的迹象，"
                "但未达到「重复」标准，仅供了解，不建议删除。"
            )
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {SUBTEXT_0}; padding: 2px 8px;")
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            center_layout.addWidget(note)

        # 匹配统计
        stats = []
        for mtype, count in self._result.match_counts.items():
            stats.append(f"{mtype}: {count} 对")
        stats_label = QLabel(" | ".join(stats))
        stats_label.setStyleSheet(f"color: {SUBTEXT_0}; padding: 4px;")
        stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center_layout.addWidget(stats_label)

        # 人脸相似提示（不计入杰卡德）
        # 人脸向量只能说明"是同一个人"，不能说明"是同一份文件"：演员池小的
        # 内容库用它会让人把同一演员的不同作品误判为重复。故仅作线索展示。
        face_hints = getattr(self._result, "face_hints", ()) or ()
        if face_hints:
            hint_label = QLabel(
                f"另有 {len(face_hints)} 对文件人脸相似（疑似同一人），"
                f"未计入上述重复判定"
            )
            hint_label.setStyleSheet(f"color: {SUBTEXT_0}; padding: 2px;")
            hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint_label.setToolTip(
                "人脸相似只说明是同一个人，不能说明是同一份文件，请人工判断"
            )
            center_layout.addWidget(hint_label)

        # 匹配文件表格
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["文件 A", "文件 B", "匹配类型", "相似度"])
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        table.setRowCount(len(self._result.file_matches))
        for i, fm in enumerate(self._result.file_matches):
            table.setItem(i, 0, QTableWidgetItem(fm.file_a_path or str(fm.file_a_id)))
            table.setItem(i, 1, QTableWidgetItem(fm.file_b_path or str(fm.file_b_id)))
            table.setItem(i, 2, QTableWidgetItem(fm.match_type))
            table.setItem(i, 3, QTableWidgetItem(f"{fm.score * 100:.1f}%"))

        center_layout.addWidget(table)

        # 操作按钮
        btn_layout = QVBoxLayout()

        # "疑似相关"不提供"保留 A / 保留 B"：那等于暗示要删掉另一侧。
        # 用户对这类命中只想知情，因此只给"不再提醒 / 暂时忽略"。
        if not is_related:
            keep_a_btn = QPushButton(f"保留单元 A 的全部文件 ({self._result.unit_a_name})")
            keep_a_btn.clicked.connect(
                lambda: self.keep_a_requested.emit(self._result.unit_a_id, self._result.unit_b_id)
            )
            btn_layout.addWidget(keep_a_btn)

            keep_b_btn = QPushButton(f"保留单元 B 的全部文件 ({self._result.unit_b_name})")
            keep_b_btn.clicked.connect(
                lambda: self.keep_b_requested.emit(self._result.unit_a_id, self._result.unit_b_id)
            )
            btn_layout.addWidget(keep_b_btn)

        whitelist_btn = QPushButton("加入白名单（不再提醒）")
        whitelist_btn.clicked.connect(
            lambda: self.whitelist_requested.emit(self._result.unit_a_id, self._result.unit_b_id)
        )
        btn_layout.addWidget(whitelist_btn)

        ignore_btn = QPushButton("暂时忽略")
        ignore_btn.clicked.connect(
            lambda: self.ignore_requested.emit(self._result.unit_a_id, self._result.unit_b_id)
        )
        btn_layout.addWidget(ignore_btn)

        center_layout.addLayout(btn_layout)
        main_splitter.addWidget(center_panel)

        # ==== 右栏：单元 B ====
        right_panel = QGroupBox(f"单元 B: {self._result.unit_b_name}")
        right_layout = QVBoxLayout(right_panel)

        right_info = QLabel(
            f"{self._result.total_files_b} 个文件\n"
            f"匹配: {len(self._result.file_matches)} 对"
        )
        right_info.setStyleSheet(f"color: {SUBTEXT_0}; padding: 4px;")
        right_layout.addWidget(right_info)

        right_list = QListWidget()
        right_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        for i in range(self._result.total_files_b):
            right_list.addItem(f"文件 {i + 1}")
        right_layout.addWidget(right_list)

        main_splitter.addWidget(right_panel)

        # 设置分割比例
        main_splitter.setSizes([300, 600, 300])

        # 总布局
        layout = QVBoxLayout(self)
        layout.addWidget(main_splitter)

        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)
