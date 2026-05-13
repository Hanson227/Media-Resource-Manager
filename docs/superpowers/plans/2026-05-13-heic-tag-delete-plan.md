# HEIC 批量转换 + 标签管理 + 文件删除 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现三个独立功能：HEIC 文件夹批量转 JPG、标签管理+右键分配、文件/文件夹删除（移至回收站）

**Architecture:** 每个功能独立实现，互不依赖。HEIC 新增对话框复用现有 `heic_converter.py`；标签复用现有 DB queries；删除复用 `delete_media_file` + 新增 `delete_resource_unit`，使用 `send2trash` 移入回收站

**Tech Stack:** PySide6, SQLAlchemy, send2trash, pillow-heif

---

### Task 0: 安装依赖

- [ ] **Step 1: 安装 send2trash**

Run:
```bash
cd d:/Media/desktop && "D:/anaconda/envs/media-manager/python.exe" -m pip install send2trash
```
Expected: `Successfully installed send2trash-1.8.3`

---

### Task 1: 新增 DB 查询函数 `delete_resource_unit`

**Files:**
- Modify: `desktop/app/db/queries.py` (在 `delete_media_file` 附近添加)

- [ ] **Step 1: 添加 `delete_resource_unit` 函数**

在 `queries.py` 的 `delete_media_file` 函数之后（约第295行），添加：

```python
def delete_resource_unit(session: Session, unit_id: int) -> bool:
    """删除一个资源单元（级联删除关联的 media_files、face_vectors、video_frames）。"""
    unit = session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).first()
    if unit:
        session.delete(unit)
        return True
    return False
```

---

### Task 2: HEIC 批量转换对话框

**Files:**
- Create: `desktop/app/ui/dialogs/heic_batch_convert.py`
- Modify: `desktop/app/ui/main_window.py` (HEIC 按钮指向新对话框)

- [ ] **Step 1: 创建 `HeicBatchConvertDialog`**

新建 `desktop/app/ui/dialogs/heic_batch_convert.py`：

```python
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

from app.core.heic_converter import convert_batch, ConvertSummary, ConversionResult, is_heic_file

logger = logging.getLogger(__name__)


_STATUS_PENDING = "等待"
_STATUS_CONVERTING = "转换中..."
_STATUS_SUCCESS = "已完成"
_STATUS_FAILED = "失败"
_STATUS_SKIPPED = "跳过"


class _HeicBatchWorker(QThread):
    """后台 HEIC 批量转换线程。"""

    progress = Signal(int, int, object)  # completed, total, ConversionResult
    finished = Signal(object)            # ConvertSummary

    def __init__(self, sources: list[Path], parent=None) -> None:
        super().__init__(parent)
        self._sources = sources

    def run(self) -> None:
        summary = convert_batch(
            self._sources,
            overwrite=False,
            progress_callback=lambda c, t, r: self.progress.emit(c, t, r),
        )
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
        for f in folder_path.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".heic", ".heif"):
                if is_heic_file(f):
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

        self._worker = _HeicBatchWorker(sources)
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
                    status_item.setForeground(self._green())
                elif result.skipped:
                    status_item.setText(_STATUS_SKIPPED)
                    status_item.setForeground(self._gray())
                else:
                    status_item.setText(f"{_STATUS_FAILED}: {result.error}")
                    status_item.setForeground(self._red())
                break

    @Slot(object)
    def _on_finished(self, summary: ConvertSummary) -> None:
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

    # ── helpers ──

    @staticmethod
    def _green():
        from PySide6.QtGui import QColor
        return QColor(46, 204, 113)

    @staticmethod
    def _red():
        from PySide6.QtGui import QColor
        return QColor(231, 76, 60)

    @staticmethod
    def _gray():
        from PySide6.QtGui import QColor
        return QColor(150, 150, 150)
```

- [ ] **Step 2: 修改 `main_window.py` 的 `_on_heic_convert`**

在 `main_window.py` 约第1010行，修改 `_on_heic_convert` 方法，把旧的文件选择逻辑替换为打开新对话框：

```python
    @Slot()
    @Slot()
    def _on_heic_convert(self) -> None:
        """打开 HEIC 批量转换对话框。"""
        from app.ui.dialogs.heic_batch_convert import HeicBatchConvertDialog
        dlg = HeicBatchConvertDialog(self)
        dlg.exec()
        self._on_refresh_all()
```

- [ ] **Step 3: 运行 test_flow.py 验证不破坏现有逻辑**

```bash
cd d:/Media/desktop && "D:/anaconda/envs/media-manager/python.exe" tests/test_flow.py
```
Expected: All tests PASS

---

### Task 3: 标签管理对话框

**Files:**
- Create: `desktop/app/ui/dialogs/tag_manager.py`
- Modify: `desktop/app/ui/main_window.py` (工具菜单加"管理标签")

- [ ] **Step 1: 创建 `TagManageDialog`**

新建 `desktop/app/ui/dialogs/tag_manager.py`：

```python
# -*- coding: utf-8 -*-
"""
标签管理对话框 —— 新建/编辑/删除标签。
"""

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QLineEdit, QColorDialog,
    QMessageBox, QGroupBox, QFormLayout,
)
from PySide6.QtGui import QColor

from app.db.engine import DatabaseManager
from app.db import queries as q
from app.db.models import FileTag

logger = logging.getLogger(__name__)


class TagManageDialog(QDialog):
    """标签管理对话框。"""

    tags_changed = Signal()  # 标签列表有变化时发出

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("管理标签")
        self.setMinimumSize(520, 400)
        self.setModal(True)

        self._current_tag_id: Optional[int] = None

        layout = QVBoxLayout(self)

        # ── 标签列表 ──
        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(QLabel("标签列表:"))
        layout.addWidget(self._list)

        # ── 编辑区域 ──
        edit_group = QGroupBox("新建 / 编辑标签")
        edit_layout = QFormLayout(edit_group)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("输入标签名称...")
        edit_layout.addRow("名称:", self._name_input)

        color_row = QHBoxLayout()
        self._color_preview = QLabel("■")
        self._color_preview.setStyleSheet("font-size: 20px; color: #888888;")
        color_row.addWidget(self._color_preview)
        self._color_btn = QPushButton("选择颜色...")
        self._color_btn.clicked.connect(self._on_pick_color)
        color_row.addWidget(self._color_btn)
        color_row.addStretch()
        edit_layout.addRow("颜色:", color_row)

        self._selected_color: str = "#888888"

        btn_row = QHBoxLayout()
        self._save_btn = QPushButton("保存")
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self._clear_form)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch()
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setStyleSheet("color: #e74c3c;")
        self._delete_btn.clicked.connect(self._on_delete)
        self._delete_btn.setEnabled(False)
        btn_row.addWidget(self._delete_btn)
        edit_layout.addRow(btn_row)

        layout.addWidget(edit_group)

        # ── 新建按钮 ──
        self._new_btn = QPushButton("+ 新建标签")
        self._new_btn.clicked.connect(self._on_new)
        layout.addWidget(self._new_btn)

        # ── 关闭按钮 ──
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        self._load_tags()

    def _load_tags(self) -> None:
        """从数据库加载标签列表。"""
        self._list.clear()
        try:
            with DatabaseManager.session() as session:
                tags = q.get_all_tags(session)
        except Exception as e:
            logger.warning(f"加载标签失败: {e}")
            tags = []

        for tag in tags:
            item = QListWidgetItem()
            color = tag.color or "#888888"
            item.setText(f"{tag.name}  ({color})")
            item.setData(Qt.ItemDataRole.UserRole, tag.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, tag.name)
            item.setData(Qt.ItemDataRole.UserRole + 2, color)
            self._list.addItem(item)

    def _on_selection_changed(self, current: Optional[QListWidgetItem],
                              previous: Optional[QListWidgetItem]) -> None:
        if current is None:
            self._clear_form()
            return
        self._current_tag_id = current.data(Qt.ItemDataRole.UserRole)
        self._name_input.setText(current.data(Qt.ItemDataRole.UserRole + 1))
        self._selected_color = current.data(Qt.ItemDataRole.UserRole + 2) or "#888888"
        self._color_preview.setStyleSheet(f"font-size: 20px; color: {self._selected_color};")
        self._delete_btn.setEnabled(True)

    def _on_pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._selected_color), self, "选择标签颜色")
        if color.isValid():
            self._selected_color = color.name()
            self._color_preview.setStyleSheet(f"font-size: 20px; color: {self._selected_color};")

    def _on_save(self) -> None:
        name = self._name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "标签管理", "请输入标签名称")
            return

        try:
            with DatabaseManager.session() as session:
                existing = q.get_tag_by_name(session, name)
                if existing and existing.id != self._current_tag_id:
                    QMessageBox.warning(self, "标签管理", f"标签已存在: {name}")
                    return

                if self._current_tag_id:
                    # 编辑已有标签 — 先删后建
                    tag = q.get_tag_by_id(session, self._current_tag_id)
                    if tag:
                        tag.name = name
                        tag.color = self._selected_color
                else:
                    q.create_tag(session, name, self._selected_color)
        except Exception as e:
            QMessageBox.critical(self, "标签管理", f"保存失败: {e}")
            return

        self._clear_form()
        self._load_tags()

    def _on_delete(self) -> None:
        if self._current_tag_id is None:
            return
        reply = QMessageBox.question(
            self, "确认删除", "确定要删除此标签？\n（已分配此标签的文件不会受影响，仅标签被移除）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            with DatabaseManager.session() as session:
                q.delete_tag(session, self._current_tag_id)
        except Exception as e:
            QMessageBox.critical(self, "标签管理", f"删除失败: {e}")
            return

        self._clear_form()
        self._load_tags()

    def _on_new(self) -> None:
        self._clear_form()
        self._name_input.setFocus()

    def _clear_form(self) -> None:
        self._current_tag_id = None
        self._name_input.clear()
        self._selected_color = "#888888"
        self._color_preview.setStyleSheet("font-size: 20px; color: #888888;")
        self._delete_btn.setEnabled(False)
        self._list.clearSelection()
```

Wait, `TagManageDialog` uses `Signal` but I forgot the import. Let me fix:

```python
from PySide6.QtCore import Qt, Signal
```

And the dialog should be self-contained. The `tags_changed` signal can be used by the main window to refresh the TagFilterBar when tags change.

- [ ] **Step 2: 在 `main_window.py` 工具菜单添加"管理标签"**

在 `main_window.py` 的 `_setup_menu_bar()` 工具菜单中，在"查重"和"HEIC 转换"之间添加：

```python
        tools_menu.addAction(dedup_action)

        # --- 添加这里 ---
        tag_action = QAction("管理标签(&T)...", self)
        tag_action.setStatusTip("创建、编辑或删除文件标签")
        tag_action.triggered.connect(self._on_manage_tags)
        tools_menu.addAction(tag_action)
        # ---

        heic_action = QAction("HEIC 转换(&H)...", self)
```

并添加对应的 slot 方法（在 `_on_heic_convert` 附近）：

```python
    @Slot()
    def _on_manage_tags(self) -> None:
        """打开标签管理对话框。"""
        from app.ui.dialogs.tag_manager import TagManageDialog
        dlg = TagManageDialog(self)
        dlg.exec()
        # 刷新标签筛选栏
        self._tag_bar.reload_tags()
```

- [ ] **Step 3: 运行 test_flow.py 验证**

```bash
cd d:/Media/desktop && "D:/anaconda/envs/media-manager/python.exe" tests/test_flow.py
```
Expected: All tests PASS

---

### Task 4: 右键分配标签

**Files:**
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py` (右键菜单加"分配标签")

- [ ] **Step 1: 修改 `_on_context_menu` 添加标签子菜单**

在 `thumbnail_grid.py` 的 `_on_context_menu()` 方法中，在"设为此文件夹封面"分隔线之后、HEIC 转换之前，添加标签分配子菜单：

在 `_on_context_menu` 中找到 `# HEIC 转换` 注释前的位置，添加：

```python
        # 分配标签
        if fid:
            menu.addSeparator()
            tag_menu = QMenu("分配标签", self)
            try:
                from app.db.engine import DatabaseManager
                with DatabaseManager.session() as session:
                    all_tags = q.get_all_tags(session)
                    file_tag_ids = [t.id for t in q.get_file_tags(session, fid)]
            except Exception:
                all_tags = []
                file_tag_ids = []

            for tag in all_tags:
                tag_action = QAction(f" {tag.name}", tag_menu)
                tag_action.setCheckable(True)
                tag_action.setChecked(tag.id in file_tag_ids)
                tag_action.setData((fid, tag.id))
                tag_action.triggered.connect(self._on_tag_toggle)
                tag_menu.addAction(tag_action)

            if all_tags:
                menu.addMenu(tag_menu)
            else:
                no_tag_action = QAction("（暂无标签）", tag_menu)
                no_tag_action.setEnabled(False)
                tag_menu.addAction(no_tag_action)
                menu.addMenu(tag_menu)
```

注意：需要确保 `from app.db import queries as q` 已导入。检查文件顶部是否有这个 import，没有则添加。

同时添加 `_on_tag_toggle` 方法：

```python
    def _on_tag_toggle(self) -> None:
        """切换文件标签。"""
        action = self.sender()
        if not action:
            return
        fid, tag_id = action.data()
        checked = action.isChecked()

        try:
            from app.db.engine import DatabaseManager
            from app.db import queries as qq
            with DatabaseManager.session() as session:
                current_ids = [t.id for t in qq.get_file_tags(session, fid)]
                if checked and tag_id not in current_ids:
                    current_ids.append(tag_id)
                elif not checked and tag_id in current_ids:
                    current_ids.remove(tag_id)
                qq.set_file_tags(session, fid, current_ids)
        except Exception as e:
            logger.warning(f"标签更新失败: {e}")
            action.setChecked(not checked)  # 回滚

        self._grid_model.reload_tags()  # 刷新网格中的标签显示
```

还需要在 `thumbnail_grid.py` 顶部添加 `from app.db import queries as q`（如果还没有的话）。

- [ ] **Step 2: 运行 test_flow.py 验证**

```bash
cd d:/Media/desktop && "D:/anaconda/envs/media-manager/python.exe" tests/test_flow.py
```
Expected: All tests PASS

---

### Task 5: 文件删除（右键 → 回收站）

**Files:**
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py` (右键菜单加"删除")
- Modify: `desktop/app/db/queries.py` (修改 `delete_media_file` 使其返回 bool)

- [ ] **Step 1: 修改 `delete_media_file` 使其返回 `bool`**

在 `queries.py` 约第291行，修改：

```python
def delete_media_file(session: Session, file_id: int) -> bool:
    """删除一条媒体文件记录（级联删除关联的人脸向量和视频帧）。"""
    mf = session.query(MediaFile).filter(MediaFile.id == file_id).first()
    if mf:
        session.delete(mf)
        return True
    return False
```

- [ ] **Step 2: 在 `thumbnail_grid.py` 右键菜单添加"删除文件"**

在 `_on_context_menu()` 中，在"分配标签"子菜单之后添加：

```python
        # 删除文件
        if file_path and fid:
            menu.addSeparator()
            del_action = menu.addAction("删除文件")
            del_action.setToolTip("将文件移至回收站并从媒体库移除")
            del_action.triggered.connect(
                lambda *args, fp=file_path, fid=fid: self._delete_file(fp, fid)
            )

        menu.exec(self.viewport().mapToGlobal(pos))
```

添加 `_delete_file` 方法：

```python
    def _delete_file(self, file_path: str, file_id: int) -> None:
        """将文件移至回收站并删除数据库记录。"""
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要将此文件移至回收站？\n{os.path.basename(file_path)}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            import send2trash
            send2trash.send2trash(file_path)
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"无法将文件移至回收站: {e}")
            return

        # 删除数据库记录
        try:
            from app.db.engine import DatabaseManager
            from app.db import queries as qq
            with DatabaseManager.session() as session:
                qq.delete_media_file(session, file_id)
        except Exception as e:
            logger.warning(f"数据库记录删除失败: {e}")

        self.refresh()
```

- [ ] **Step 3: 运行 test_flow.py 验证**

```bash
cd d:/Media/desktop && "D:/anaconda/envs/media-manager/python.exe" tests/test_flow.py
```
Expected: All tests PASS

---

### Task 6: 文件夹删除（右键 → 回收站）

**Files:**
- Modify: `desktop/app/ui/left_panel/context_menu.py` (加"删除文件夹" action + signal)
- Modify: `desktop/app/ui/left_panel/folder_tree.py` (转发信号)
- Modify: `desktop/app/ui/main_window.py` (处理信号 + 执行删除)

- [ ] **Step 1: 在 `context_menu.py` 添加 `delete_requested` 信号和菜单项**

在 `FolderTreeContextMenu` 类中，在信号定义区域添加：

```python
    delete_requested = Signal(int)  # 删除资源单元 (unit_id)
```

在 `_build_single_menu` 方法中，排除 action 之后、separator 之后、封面设置之前添加：

```python
        # 删除文件夹
        self.addSeparator()
        delete_action = QAction(f"删除「{node.name}」", self)
        delete_action.setToolTip(f"将文件夹移至回收站，并从媒体库中移除所有相关记录")
        delete_action.triggered.connect(lambda *args, uid=unit_id: self.delete_requested.emit(uid))
        self.addAction(delete_action)
```

- [ ] **Step 2: 在 `folder_tree.py` 转发 `delete_requested` 信号**

在 `folder_tree.py` 的 `FolderTreeView` 类中，添加信号定义（与 `merge_requested` 等一起）：

```python
    delete_requested = Signal(int)  # 删除资源单元
```

在 `_on_context_menu` 方法中，连接信号：

```python
        menu.delete_requested.connect(self.delete_requested.emit)
```

- [ ] **Step 3: 在 `main_window.py` 处理删除文件夹信号**

在 `_setup_tree_connections` 中添加连接（约第368行）：

```python
        self._tree_view.delete_requested.connect(self._on_delete_unit)
```

添加对应的 slot 方法：

```python
    @Slot(int)
    def _on_delete_unit(self, unit_id: int) -> None:
        """删除资源单元（移至回收站 + 删 DB）。"""
        from app.db.engine import DatabaseManager
        from app.db import queries as qq

        # 获取单元信息
        try:
            with DatabaseManager.session() as session:
                unit = qq.get_unit_by_id(session, unit_id)
                if unit is None:
                    QMessageBox.warning(self, "删除失败", "资源单元不存在")
                    return
                unit_path = unit.path
                file_count = qq.get_unit_file_count(session, unit_id)
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"查询单元信息失败: {e}")
            return

        # 确认对话框
        msg = f"确定要将文件夹「{Path(unit_path).name}」移至回收站？"
        if file_count > 0:
            msg += f"\n\n该文件夹包含 {file_count} 个文件。"
        reply = QMessageBox.question(
            self, "确认删除", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # 移至回收站
        try:
            import send2trash
            send2trash.send2trash(unit_path)
        except Exception as e:
            QMessageBox.critical(self, "删除失败", f"无法将文件夹移至回收站: {e}")
            return

        # 删除 DB 记录
        try:
            with DatabaseManager.session() as session:
                qq.delete_resource_unit(session, unit_id)
        except Exception as e:
            logger.warning(f"数据库记录删除失败: {e}")

        # 刷新
        self._tree_view.refresh_model()
```

确保文件顶部有 `from pathlib import Path`。

- [ ] **Step 4: 运行 test_flow.py 验证**

```bash
cd d:/Media/desktop && "D:/anaconda/envs/media-manager/python.exe" tests/test_flow.py
```
Expected: All tests PASS

---

### Self-Review Checklist

1. **Spec coverage:**
   - HEIC batch convert dialog → Task 2
   - Tag management dialog → Task 3
   - Right-click tag assignment → Task 4
   - File delete (recycle bin) → Task 5
   - Folder delete (recycle bin) → Task 6

2. **Placeholder scan:** No TBD, TODO, or "implement later" patterns found.

3. **Type consistency:**
   - `delete_media_file` returns `bool` in Task 5 → consistent with call site
   - `delete_resource_unit(unit_id)` takes `int` in Task 1 → consistent with signal `Signal(int)` in Task 6
   - `send2trash.send2trash(path_string)` → consistent usage across Tasks 5 and 6
