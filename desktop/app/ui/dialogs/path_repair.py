# -*- coding: utf-8 -*-
"""
修复媒体库路径对话框 —— 盘符/挂载点变化后把库内路径整体改到新位置。

为什么必须有这个入口：库里存的是绝对路径，换盘后如果直接重新扫描，
会凭空多出一套"同名不同盘符"的根与单元，旧记录永远清不掉、标签星标全部
孤立。正确动作是"改库"（`path_remap_service`），而改库是不可逆操作，
必须让用户看到候选位置与影响行数后再确认。

信号:
    paths_repaired: 成功改库后发出（结果字典，含 updated / thumbnail_meta）。
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from config import AppConfig
from app.services import path_remap_service as remap_svc

logger = logging.getLogger(__name__)


class PathRepairDialog(QDialog):
    """媒体库路径修复对话框。"""

    paths_repaired = Signal(object)

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._broken: list = []
        self.setWindowTitle("修复媒体库路径")
        self.resize(720, 520)
        self._setup_ui()
        self.reload()

    # ============================================================
    # UI
    # ============================================================

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            "当移动硬盘/存储卡换了盘符时，库里的绝对路径会失效。\n"
            "在下面选中受影响的媒体库，确认候选位置后点「修复路径」——"
            "程序会改写数据库里的路径（改前自动备份），不会移动或删除任何文件。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_select_root)
        layout.addWidget(self._list, 1)

        # ---- 修复参数 ----
        form_box = QGroupBox("修复到")
        form = QFormLayout(form_box)

        self._candidate_combo = QComboBox()
        self._candidate_combo.currentIndexChanged.connect(self._on_candidate_changed)
        form.addRow("候选位置:", self._candidate_combo)

        target_layout = QHBoxLayout()
        self._target_edit = QLineEdit()
        self._target_edit.setPlaceholderText("新路径前缀，如 E:\\Media")
        self._target_edit.textChanged.connect(self._update_preview)
        target_layout.addWidget(self._target_edit)
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse_target)
        target_layout.addWidget(browse_btn)
        form.addRow("新路径:", target_layout)

        self._preview = QLabel("—")
        self._preview.setWordWrap(True)
        form.addRow("影响预览:", self._preview)

        layout.addWidget(form_box)

        # ---- 按钮 ----
        buttons = QHBoxLayout()
        refresh_btn = QPushButton("重新检测")
        refresh_btn.clicked.connect(self.reload)
        buttons.addWidget(refresh_btn)
        buttons.addStretch(1)

        self._repair_btn = QPushButton("修复路径（改库）")
        self._repair_btn.setEnabled(False)
        self._repair_btn.clicked.connect(self._apply_repair)
        buttons.addWidget(self._repair_btn)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        buttons.addWidget(close_box)
        layout.addLayout(buttons)

    # ============================================================
    # 数据加载
    # ============================================================

    @Slot()
    def reload(self) -> None:
        """重新检测不可达的媒体库根。"""
        try:
            self._broken = remap_svc.find_broken_roots()
        except Exception as e:  # noqa: BLE001
            logger.error(f"检测媒体库路径失败: {e}")
            self._broken = []
        self._list.clear()
        for item in self._broken:
            text = f"{item['path']}    —— {item['reason_text']}"
            if item["reason"] == "volume_offline":
                text += "（盘不在，插上后再试）"
            entry = QListWidgetItem(text)
            entry.setData(Qt.ItemDataRole.UserRole, item["id"])
            self._list.addItem(entry)
        if self._broken:
            self._list.setCurrentRow(0)
        else:
            self._on_select_root(-1)

    def _current(self) -> dict:
        row = self._list.currentRow()
        if 0 <= row < len(self._broken):
            return self._broken[row]
        return {}

    @Slot(int)
    def _on_select_root(self, row: int) -> None:
        item = self._current()
        self._candidate_combo.clear()
        if not item:
            self._target_edit.setText("")
            self._update_preview()
            return
        for candidate in item.get("candidates", []):
            label = f"{candidate['path']}（{candidate['kind']}，浅层媒体 {candidate['media_count']}）"
            self._candidate_combo.addItem(label, candidate["path"])
        if not item.get("candidates"):
            self._candidate_combo.addItem("（未找到候选，请手动指定路径）", "")
        self._on_candidate_changed(self._candidate_combo.currentIndex())

    @Slot(int)
    def _on_candidate_changed(self, index: int) -> None:
        value = self._candidate_combo.currentData() or ""
        if value:
            self._target_edit.setText(str(value))
        else:
            self._update_preview()

    def _browse_target(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "选择原来的媒体库目录现在的位置",
            self._target_edit.text() or str(Path.home()))
        if folder:
            self._target_edit.setText(folder)

    @Slot()
    def _update_preview(self) -> None:
        item = self._current()
        text = self._target_edit.text().strip()
        if not item or not text:
            self._preview.setText("—")
            self._repair_btn.setEnabled(False)
            return
        try:
            plan = remap_svc.plan_remap(item["path"], text)
        except Exception as e:  # noqa: BLE001
            self._preview.setText(f"预演失败: {e}")
            self._repair_btn.setEnabled(False)
            return
        if plan.reason:
            self._preview.setText(f"不可执行: {plan.reason}")
            self._repair_btn.setEnabled(False)
            return
        self._preview.setText(
            f"将改写 {plan.total_rows} 条路径记录"
            f"（媒体库 {plan.counts.get('library_roots', 0)} / "
            f"单元 {plan.counts.get('units', 0)} / 文件 {plan.counts.get('files', 0)}），"
            f"缩略图缓存 {plan.thumbnail_meta_hits} 条；"
            f"目标目录{'存在' if plan.new_exists else '不存在'}")
        self._repair_btn.setEnabled(plan.total_rows > 0)

    # ============================================================
    # 执行修复
    # ============================================================

    @Slot()
    def _apply_repair(self) -> None:
        item = self._current()
        target = self._target_edit.text().strip()
        if not item or not target:
            return
        reply = QMessageBox.question(
            self, "确认修复路径",
            f"把以下旧路径\n  {item['path']}\n改写为\n  {target}\n\n"
            "只会改写数据库里的路径记录（改前自动备份数据库），"
            "不会移动、重命名或删除磁盘上的任何文件。\n\n确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            result = remap_svc.apply_remap(
                item["path"], target,
                cache_dir=self._config.thumbnail_cache_dir)
        except remap_svc.RemapError as e:
            QMessageBox.warning(self, "无法修复", str(e))
            return
        except Exception as e:  # noqa: BLE001
            logger.error(f"修复路径失败: {e}", exc_info=True)
            QMessageBox.critical(self, "修复失败", f"改库失败:\n{e}")
            return

        updated = result.get("updated", {})
        backup = result.get("backup") or "（未生成）"
        QMessageBox.information(
            self, "修复完成",
            "路径已改写:\n"
            f"  媒体库 {updated.get('library_roots', 0)} 条\n"
            f"  资源单元 {updated.get('units', 0)} 条\n"
            f"  文件 {updated.get('files', 0)} 条\n"
            f"  缩略图缓存 {result.get('thumbnail_meta', 0)} 条\n\n"
            f"数据库备份: {backup}\n\n"
            "建议随后执行一次「重新扫描」以校准单元统计。")
        self.paths_repaired.emit(result)
        self.reload()
