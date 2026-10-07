# -*- coding: utf-8 -*-
"""
已排除文件夹管理对话框 —— 查看和恢复被排除的资源单元。

排除有"用户手动"与"扫描自动（目录不可达 / 目录已空）"两种起因，恢复语义相反
（见 `constants.ExcludeReason`）：这里逐条显示起因，并提供批量恢复 ——
磁盘掉线导致的整库自动排除就是靠「全部恢复」一次性救回来的。
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox, QAbstractItemView,
)

from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services.file_ops import volume_available, volume_label
from app.utils.constants import ExcludeReason
from app.ui.theme import SUBTEXT_0, TEXT

logger = logging.getLogger(__name__)

# 排除起因 → 界面文案（NULL = 升级前的旧记录，起因不可考）
_REASON_LABELS = {
    ExcludeReason.USER.value: "手动排除",
    ExcludeReason.AUTO_MISSING.value: "自动排除：目录已不可达",
    ExcludeReason.AUTO_EMPTY.value: "自动排除：目录内已无媒体文件",
}


def _reason_label(reason) -> str:
    """把 exclude_reason 转成给用户看的原因文案。"""
    if not reason:
        return "排除（升级前的记录）"
    return _REASON_LABELS.get(reason, f"排除（{reason}）")


class ExcludedFoldersDialog(QDialog):
    """已排除文件夹管理对话框。

    信号:
        excluded_changed: 排除列表发生变化时发出。
    """

    excluded_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理已排除文件夹")
        self.resize(500, 400)
        self._setup_ui()
        self._load_excluded()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QLabel(
            "以下文件夹已被排除，不会参与扫描和查重：\n"
            "「自动排除」的是扫描判定内容消失时下的，内容回来会自动恢复；\n"
            "「手动排除」的保持到你在这里取消为止。")
        header.setStyleSheet(f"color: {SUBTEXT_0}; padding: 4px;")
        header.setWordWrap(True)
        layout.addWidget(header)

        self._list_widget = QListWidget()
        # 支持多选：磁盘掉线一次会让整库进这里，逐个点不现实
        self._list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list_widget.setStyleSheet(
            f"QListWidget {{ background-color: #313244; color: {TEXT}; "
            f"border: 1px solid #3b3b4e; border-radius: 6px; }}"
            f"QListWidget::item {{ padding: 6px; }}"
            f"QListWidget::item:selected {{ background-color: #3b3b4e; }}"
        )
        layout.addWidget(self._list_widget)

        btn_layout = QHBoxLayout()
        self._restore_btn = QPushButton("取消排除")
        self._restore_btn.setToolTip("将选中的文件夹恢复为活跃状态（可多选）")
        self._restore_btn.clicked.connect(self._on_restore)
        btn_layout.addWidget(self._restore_btn)
        self._restore_all_btn = QPushButton("全部恢复")
        self._restore_all_btn.setToolTip("一键取消全部排除（磁盘掉线被批量误排除时用）")
        self._restore_all_btn.clicked.connect(self._on_restore_all)
        btn_layout.addWidget(self._restore_all_btn)
        self._clean_btn = QPushButton("清理无效记录")
        self._clean_btn.setToolTip("删除所有路径已不存在的排除记录")
        self._clean_btn.clicked.connect(self._on_clean_invalid)
        btn_layout.addWidget(self._clean_btn)
        btn_layout.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.close)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _load_excluded(self) -> None:
        self._list_widget.clear()
        try:
            with DatabaseManager.session() as session:
                units = q.get_excluded_units(session)
                if not units:
                    empty_item = QListWidgetItem("暂无已排除的文件夹")
                    empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
                    self._list_widget.addItem(empty_item)
                    self._restore_btn.setEnabled(False)
                    self._restore_all_btn.setEnabled(False)
                    self._clean_btn.setEnabled(False)
                    return
                has_invalid = False
                for unit in units:
                    path_exists = Path(unit.path).is_dir()
                    label = f"{unit.name}\n{unit.path}\n[{_reason_label(unit.exclude_reason)}]"
                    if not path_exists:
                        # 路径不可达：分清"磁盘没连"（插回盘就好）与"真没了"
                        if volume_available(unit.path):
                            label += " [路径已不存在]"
                            has_invalid = True
                        else:
                            label += f" [磁盘{volume_label(unit.path)}未连接]"
                    item = QListWidgetItem(label)
                    item.setData(Qt.ItemDataRole.UserRole, unit.id)
                    if not path_exists:
                        from PySide6.QtGui import QColor
                        item.setForeground(QColor("#6c7086"))  # 灰色标记
                    self._list_widget.addItem(item)
                self._restore_btn.setEnabled(True)
                self._restore_all_btn.setEnabled(True)
                self._clean_btn.setEnabled(has_invalid)
        except Exception as e:
            logger.error(f"加载已排除列表失败: {e}")

    def _selected_unit_ids(self) -> list[int]:
        """当前选中的单元 ID 列表（多选）。"""
        return [
            it.data(Qt.ItemDataRole.UserRole)
            for it in self._list_widget.selectedItems()
            if it.data(Qt.ItemDataRole.UserRole)
        ]

    @Slot()
    def _on_clean_invalid(self) -> None:
        """清理所有路径已不存在的排除记录。

        卷不可达（磁盘没连）的不算"无效"：那些记录一会儿插回盘还要用
        （见 `file_ops.volume_available`，与删除语义同源）。
        """
        try:
            with DatabaseManager.session() as session:
                units = q.get_excluded_units(session)
                invalid = [u for u in units
                           if not Path(u.path).is_dir() and volume_available(u.path)]
                if not invalid:
                    QMessageBox.information(self, "清理完成", "没有无效记录需要清理。")
                    return
                reply = QMessageBox.question(
                    self, "确认清理",
                    f"将删除 {len(invalid)} 条路径已不存在的排除记录。\n\n确定继续？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                for unit in invalid:
                    q.delete_resource_unit(session, unit.id)
                    logger.info(f"已清理无效排除记录: {unit.name} (id={unit.id})")
            self.excluded_changed.emit()
            self._load_excluded()
        except Exception as e:
            QMessageBox.critical(self, "清理失败", str(e))

    @Slot()
    def _on_restore(self) -> None:
        """取消排除选中的文件夹（支持多选）。"""
        unit_ids = self._selected_unit_ids()
        if not unit_ids:
            return
        try:
            with DatabaseManager.session() as session:
                units = [u for u in (q.get_unit_by_id(session, i) for i in unit_ids)
                         if u is not None]
                unreachable = [u for u in units if not Path(u.path).is_dir()]
                offline = [u for u in unreachable if not volume_available(u.path)]
                gone = [u for u in unreachable if volume_available(u.path)]

                if offline:
                    QMessageBox.information(
                        self, "磁盘未连接",
                        "以下目录所在磁盘当前未连接：\n\n"
                        + "\n".join(u.path for u in offline)
                        + "\n\n取消排除后记录会保留，不会被重新排除；插回磁盘刷新即可。")

                delete_ids: set[int] = set()
                if gone:
                    reply = QMessageBox.question(
                        self, "路径已不存在",
                        f"以下 {len(gone)} 个目录在磁盘上确实已不存在：\n\n"
                        + "\n".join(u.path for u in gone)
                        + "\n\n取消排除后它们会在下次刷新时被重新排除。\n"
                          "是否改为直接删除这些记录？",
                        QMessageBox.StandardButton.Yes
                        | QMessageBox.StandardButton.No
                        | QMessageBox.StandardButton.Cancel,
                        QMessageBox.StandardButton.Yes,
                    )
                    if reply == QMessageBox.StandardButton.Cancel:
                        return
                    if reply == QMessageBox.StandardButton.Yes:
                        delete_ids = {u.id for u in gone}

                restored = 0
                for unit in units:
                    if unit.id in delete_ids:
                        q.delete_resource_unit(session, unit.id)
                        logger.info(f"已删除路径不存在的单元记录: {unit.name}")
                    else:
                        q.unexclude_unit(session, unit.id)
                        restored += 1
            logger.info(f"取消排除完成: 恢复 {restored} 个, 删除 {len(delete_ids)} 个")
            self.excluded_changed.emit()
            self._load_excluded()
        except Exception as e:
            QMessageBox.critical(self, "恢复失败", str(e))

    @Slot()
    def _on_restore_all(self) -> None:
        """一键恢复全部排除项。

        磁盘掉线会让整库被自动排除，逐个点不现实；但列表里也可能混着用户的
        手动排除，所以先确认再动手（不做任何自动判断）。
        """
        try:
            with DatabaseManager.session() as session:
                units = q.get_excluded_units(session)
                if not units:
                    QMessageBox.information(self, "无需恢复", "当前没有已排除的文件夹。")
                    return
                manual = sum(
                    1 for u in units
                    if u.exclude_reason in (None, ExcludeReason.USER.value))
                reply = QMessageBox.question(
                    self, "确认恢复",
                    f"将取消排除全部 {len(units)} 个文件夹。"
                    + (f"\n其中 {manual} 个是手动排除或升级前的记录。" if manual else "")
                    + "\n\n确定继续？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                count = q.unexclude_all_units(session)
            logger.info(f"已恢复全部排除项: {count} 个")
            self.excluded_changed.emit()
            self._load_excluded()
            QMessageBox.information(
                self, "恢复完成",
                f"已恢复 {count} 个文件夹。\n"
                "若媒体库在未连接的磁盘上，插回磁盘后点「刷新全部」即可看到内容。")
        except Exception as e:
            QMessageBox.critical(self, "恢复失败", str(e))
