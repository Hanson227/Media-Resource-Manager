# 选中高亮与文件树展开/收起优化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正桌面端左侧树与右侧网格之间的选中联动逻辑，实现手风琴展开/收起，消除链式反应 bug。

**架构：** 将 `FolderTreeModel.expand_unit/collapse_unit` 从全量 reset 改为逐行 insert/remove；在 `FolderTreeView` 中拆分信号以区分"文件夹单击"和"文件单击"；在 `MainWindow` 新增 accordion 跟踪状态。

**Tech Stack:** PySide6 (Qt for Python), SQLAlchemy, SQLite

---

## Task 1: Tree Model — 用 beginInsertRows/beginRemoveRows 替代 beginResetModel

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py` — `FolderTreeModel` 类

- [ ] **Step 1: 在 FolderTreeModel 中添加 `_find_unit_parent_index()` 辅助方法**

```python
def _find_unit_parent_index(self, unit_node: TreeNode) -> QModelIndex:
    """查找单元节点的父级 QModelIndex（用于 beginInsertRows/beginRemoveRows）。"""
    if unit_node.node_subtype == "file":
        # 文件节点的父节点是单元
        for r_idx, root in enumerate(self._roots):
            for c_idx, child in enumerate(root.children):
                if child.node_type == "unit" and child.children:
                    for fn in child.children:
                        if fn.node_id == unit_node.node_id:
                            return self.createIndex(c_idx, 0, child)
        return QModelIndex()

    # 单元节点的父节点是根
    for r_idx, root in enumerate(self._roots):
        if unit_node.library_root_id == -1 and root.node_type == "favorites":
            for c_idx, child in enumerate(root.children):
                if child.node_id == unit_node.node_id:
                    return self.createIndex(r_idx, 0, root)
        if root.node_id == unit_node.library_root_id:
            for c_idx, child in enumerate(root.children):
                if child.node_id == unit_node.node_id:
                    return self.createIndex(r_idx, 0, root)
    return QModelIndex()
```

- [ ] **Step 2: 重写 `expand_unit()` — 使用 beginInsertRows**

```python
def expand_unit(self, unit_id: int, files: list) -> None:
    """加载单元下的文件作为树节点子项（使用 beginInsertRows）。"""
    node = self.get_node_by_unit_id(unit_id)
    if not node:
        return
    if node.children:
        return  # 已展开，不做重复操作
    parent = self._find_unit_parent_index(node)
    if not parent.isValid():
        return

    new_children = [
        TreeNode(
            node_type="unit",
            node_subtype="file",
            node_id=f["id"],
            name=f["filename"],
            path=f["path"],
            total_size=f.get("size_bytes", 0),
        )
        for f in files[:200]
    ]
    self.beginInsertRows(parent, 0, len(new_children) - 1)
    node.children = new_children
    self.endInsertRows()
```

- [ ] **Step 3: 重写 `collapse_unit()` — 使用 beginRemoveRows**

```python
def collapse_unit(self, unit_id: int) -> None:
    """卸载文件子节点（使用 beginRemoveRows）。"""
    node = self.get_node_by_unit_id(unit_id)
    if not node or not node.children:
        return
    parent = self._find_unit_parent_index(node)
    if not parent.isValid():
        return
    count = len(node.children)
    self.beginRemoveRows(parent, 0, count - 1)
    node.children = []
    self.endRemoveRows()
```

- [ ] **Step 4: 运行已有测试，确认 T9 通过**

Run: `cd desktop && python tests/test_flow.py`
Expected: T9 passes ("expand unit" and "collapse unit")

---

## Task 2: Tree View — 拆分信号逻辑

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py` — `FolderTreeView` 类

- [ ] **Step 1: 新增 `folder_single_clicked` 信号定义**

在 `unit_selected = Signal(list)` 之后添加：

```python
folder_single_clicked = Signal(int)   # 单击文件夹节点 → 显示文件夹卡片（不进入）
```

- [ ] **Step 2: 重写 `_on_selection_changed()` — 区分节点类型发射不同信号**

替换原有 `_on_selection_changed` 方法：

```python
@Slot()
def _on_selection_changed(self) -> None:
    """树选择变化：按节点类型分发不同信号。

    - 文件节点 → file_selected_from_tree(file_id)
    - 文件夹节点 → folder_single_clicked(unit_id)
    - 根/收藏节点 → unit_selected(list[unit_ids])
    """
    indexes = self.selectedIndexes()
    if not indexes:
        return
    idx = next((i for i in indexes if i.column() == 0), None)
    if idx is None:
        return

    node = idx.internalPointer()
    if not node:
        return

    # 文件节点 → 仅发射文件选中信号
    if node.node_type == "unit" and node.node_subtype == "file":
        self.file_selected_from_tree.emit(node.node_id)
        return

    # 获取文件夹/根的 unit IDs
    unit_ids = self._model.get_selected_units(idx)
    if not unit_ids:
        return

    if node.node_type == "unit":
        # 文件夹节点 → 发射 folder_single_clicked
        self.folder_single_clicked.emit(unit_ids[0])
    elif node.node_type in ("root", "favorites"):
        # 根节点 → 发射 unit_selected（显示文件夹卡片）
        self.unit_selected.emit(list(unit_ids))
```

- [ ] **Step 3: 移除 `_on_tree_file_selected` 的 selectionModel 连接**

在 `__init__` 中找到此行并删除：

```python
# 删除此行（文件选择逻辑已合并到 _on_selection_changed）
self.selectionModel().selectionChanged.connect(self._on_tree_file_selected)
```

保留 `_on_tree_file_selected` 方法本身（不删除方法，避免 tests 中引用报错）。

- [ ] **Step 4: 新增 `select_unit_silent()` 方法**

```python
def select_unit_silent(self, unit_id: int) -> None:
    """高亮树节点但不发射 unit_selected 信号。
    用于右侧文件夹卡片单击联动（仅需高亮同步，不需加载文件）。"""
    model = self._model
    sel = self.selectionModel()
    if sel:
        sel.blockSignals(True)
    try:
        for root_row, root in enumerate(model._roots):
            for child_row, child in enumerate(root.children):
                if child.node_id == unit_id:
                    root_idx = model.index(root_row, 0)
                    child_idx = model.index(child_row, 0, root_idx)
                    self.setCurrentIndex(child_idx)
                    self.scrollTo(child_idx)
                    return
    finally:
        if sel:
            sel.blockSignals(False)
```

- [ ] **Step 5: 优化 `select_tree_node_by_file_id` — 避免 expandAll**

替换原有方法，改为只展开目标路径：

```python
def select_tree_node_by_file_id(self, file_id: int) -> bool:
    """选中指定文件 ID 对应的树节点。只展开目标路径，不 expandAll。
    返回 True 表示找到并选中，False 表示未找到。"""
    model = self._model
    for root_row, root in enumerate(model._roots):
        root_idx = model.index(root_row, 0)
        if not root_idx.isValid():
            continue
        for child_row, child in enumerate(root.children):
            if child.node_type == "unit" and child.children:
                for f_row, f_node in enumerate(child.children):
                    if f_node.node_id == file_id:
                        # 仅展开目标路径
                        self.expand(root_idx)
                        unit_idx = model.index(child_row, 0, root_idx)
                        if unit_idx.isValid():
                            self.expand(unit_idx)
                            file_idx = model.index(f_row, 0, unit_idx)
                            if file_idx.isValid():
                                sel = self.selectionModel()
                                if sel:
                                    sel.blockSignals(True)
                                self.setCurrentIndex(file_idx)
                                self.scrollTo(file_idx)
                                if sel:
                                    sel.blockSignals(False)
                                return True
    return False
```

---

## Task 3: Grid View — 新增文件夹卡片滚动选中方法

**Files:**
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py` — `ThumbnailGridView` 类

- [ ] **Step 1: 添加 `select_folder_card_by_unit_id()` 方法**

```python
def select_folder_card_by_unit_id(self, unit_id: int) -> None:
    """在文件夹卡片模式中选中指定 unit_id 的卡片并滚动到可视位置。"""
    model = self.model()
    if not isinstance(model, FolderCardModel):
        return
    for row in range(model.rowCount()):
        idx = model.index(row, 0)
        if model.data(idx, Qt.ItemDataRole.UserRole + 1) == unit_id:
            sel = self.selectionModel()
            if sel:
                sel.blockSignals(True)
            self.setCurrentIndex(idx)
            self.scrollTo(idx)
            if sel:
                sel.blockSignals(False)
            return
```

---

## Task 4: MainWindow — 新信号连线、accordion、自动选中首文件

**Files:**
- Modify: `desktop/app/ui/main_window.py` — `MainWindow` 类

- [ ] **Step 1: 在 `__init__` 中新增 accordion 状态字段**

在 `self._last_sort_asc: bool = True` 之后添加：

```python
self._current_expanded_unit_id: Optional[int] = None  # accordion: 当前展开文件层的单元 ID
```

- [ ] **Step 2: 修改 `_connect_signals()` — 调整信号连线**

找到：
```python
self._grid_view.folder_selected.connect(self._tree_view.select_unit)
```

替换为：
```python
self._grid_view.folder_selected.connect(self._tree_view.select_unit_silent)
```

在 `self._tree_view.unit_selected.connect(self._on_unit_selected)` 之后添加：
```python
self._tree_view.folder_single_clicked.connect(self._on_folder_single_clicked)
```

- [ ] **Step 3: 新增 `_on_folder_single_clicked()` 槽**

```python
@Slot(int)
def _on_folder_single_clicked(self, unit_id: int) -> None:
    """树中单击文件夹 → 显示文件夹卡片并滚动到该卡片。"""
    self._breadcrumb.hide()

    # 收起之前展开的文件层（accordion）
    if self._current_expanded_unit_id is not None:
        try:
            self._tree_view.model().collapse_unit(self._current_expanded_unit_id)
        except Exception:
            pass
    self._current_expanded_unit_id = None
    self._current_unit_id = None

    # 找到包含该文件夹的根，显示其所有文件夹卡片
    model = self._tree_view.model()
    root_unit_ids = None
    for row in range(model.rowCount()):
        root_idx = model.index(row, 0)
        ids = model.get_selected_units(root_idx)
        if unit_id in ids:
            root_unit_ids = ids
            break

    if root_unit_ids:
        self._show_folder_cards(root_unit_ids)
        self._grid_view.select_folder_card_by_unit_id(unit_id)
    else:
        self._show_folder_cards([unit_id])
        self._grid_view.select_folder_card_by_unit_id(unit_id)
```

- [ ] **Step 4: 重写 `_on_unit_selected()` — 仅处理根节点单击**

```python
@Slot(list)
def _on_unit_selected(self, unit_ids: list[int]) -> None:
    """根/收藏节点单击 → 显示文件夹卡片。"""
    if not unit_ids:
        return

    self._breadcrumb.hide()
    # 收起之前展开的单元文件层
    if self._current_expanded_unit_id is not None:
        try:
            self._tree_view.model().collapse_unit(self._current_expanded_unit_id)
        except Exception:
            pass
    self._current_expanded_unit_id = None
    self._current_unit_id = None

    # 显示文件夹卡片
    if len(unit_ids) > 1:
        self._show_folder_cards(unit_ids)
    else:
        # 单个文件夹也显示卡片
        self._show_folder_cards(unit_ids)
```

- [ ] **Step 5: 在 `_on_unit_double_clicked()` 中集成 accordion + 自动选中首文件**

替换原有方法：

```python
@Slot(int)
def _on_unit_double_clicked(self, unit_id: int) -> None:
    """双击进入文件夹：手风琴展开 + 加载文件 + 自动选中首文件。"""
    # ---- Accordion: 同一根下之前的展开单元自动收起 ----
    if (self._current_expanded_unit_id is not None
            and self._current_expanded_unit_id != unit_id):
        model = self._tree_view.model()
        prev_node = model.get_node_by_unit_id(self._current_expanded_unit_id)
        new_node = model.get_node_by_unit_id(unit_id)
        if (prev_node and new_node
                and prev_node.library_root_id == new_node.library_root_id):
            try:
                model.collapse_unit(self._current_expanded_unit_id)
            except Exception:
                pass

    # ---- 加载文件 ----
    self._current_unit_id = unit_id
    self._grid_view.load_unit(unit_id)
    self._breadcrumb.show()

    # ---- 展开树文件子节点（不 expandAll） ----
    try:
        with DatabaseManager.session() as session:
            files = q.get_files_by_unit(session, unit_id)
            file_dicts = [
                {"id": f.id, "filename": f.filename,
                 "path": f.path, "size_bytes": f.size_bytes}
                for f in files
            ]
        tree_model = self._tree_view.model()
        tree_model.expand_unit(unit_id, file_dicts)
        # 找到单元节点并展开
        unit_node = tree_model.get_node_by_unit_id(unit_id)
        if unit_node:
            parent_idx = tree_model._find_unit_parent_index(unit_node)
            if parent_idx.isValid():
                for c_row, child in enumerate(
                    parent_idx.internalPointer().children
                ):
                    if child.node_id == unit_id:
                        unit_idx = tree_model.index(c_row, 0, parent_idx)
                        self._tree_view.expand(unit_idx)
                        break
    except Exception as e:
        logger.error(f"展开单元文件树失败: {e}")

    # ---- 自动选中第一个文件 ----
    if self._grid_model.file_list:
        first_id = self._grid_model.file_list[0]["id"]
        # 略延迟确保视图加载完成后选中
        from PySide6.QtCore import QTimer
        QTimer.singleShot(50, lambda: self._grid_view.select_file_by_id(first_id))
        QTimer.singleShot(50, lambda: self._tree_view.select_tree_node_by_file_id(first_id))

    # ---- 更新 accordion 状态 ----
    self._current_expanded_unit_id = unit_id

    # ---- 更新头/脚信息 ----
    try:
        with DatabaseManager.session() as session:
            unit = q.get_unit_by_id(session, unit_id)
            if unit:
                self._right_header.setText(f"资源单元: {unit.name}")
                self._right_footer.setText(
                    f"{unit.file_count} 个项目 | 共 {format_size(unit.total_size)}"
                )
    except Exception as e:
        logger.error(f"加载单元详情失败: {e}")
```

- [ ] **Step 6: 修改 `_on_breadcrumb_back()` 集成 accordion**

找到 `_on_breadcrumb_back` 中 `self._current_unit_id = None` 的位置，在其后添加：

```python
self._current_expanded_unit_id = None
```

- [ ] **Step 7: 修改 `_on_exclude_unit()` 中收起展开状态**

在 `self._grid_view.clear()` 之后添加：

```python
self._current_expanded_unit_id = None
```

---

## Task 5: 测试用例 T11–T19

**Files:**
- Modify: `desktop/tests/test_flow.py`

- [ ] **Step 1: 添加 T11 — 树中单击文件夹 → 右侧显示文件夹卡片**

在 `test_file_level_linking` 函数之后添加。

```python
def test_tree_folder_click_shows_folder_cards():
    """T11: 树中单击文件夹 → 右侧显示文件夹卡片。"""
    section("T11: 树中单击文件夹 → 文件夹卡片")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from app.ui.right_panel.thumbnail_grid import FolderCardModel, ThumbnailGridModel, ThumbnailGridView
    from PySide6.QtCore import Qt

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
            TreeNode(node_type="unit", node_id=11, name="夏天", path="/a/summer", file_count=3),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()
    tree_view = FolderTreeView(tree_model)

    # 捕获 folder_single_clicked 信号
    captured = []
    tree_view.folder_single_clicked.connect(captured.append)

    # 选中 "春天" 文件夹节点（行0）
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)
    tree_view.selectionModel().select(unit_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    check("T11: folder_single_clicked 已发射", len(captured) >= 1)
    if captured:
        check("T11: 携带 unit_id=10", captured[0] == 10)
```

- [ ] **Step 2: 添加 T12 — 树中单击文件夹 → 不自动展开文件层**

```python
def test_tree_folder_click_no_file_expand():
    """T12: 树中单击文件夹 → 不展开文件层。"""
    section("T12: 树中单击文件夹 → 文件层不展开")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import Qt, QItemSelectionModel

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()

    # 先注入文件子节点（模拟之前双击展开过）
    tree_model._roots[0].children[0].children = [
        TreeNode(node_type="unit", node_subtype="file", node_id=101,
                 name="a.jpg", path="/a/spring/a.jpg"),
    ]
    tree_view = FolderTreeView(tree_model)

    # 选中文件夹节点
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)

    # 展开文件层（模拟双击展开的状态）
    tree_view.expand(unit_idx)
    check("T12: 展开前文件层可见", tree_view.isExpanded(unit_idx))

    # 单击选中文件夹
    tree_view.selectionModel().select(unit_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    # 即使有子节点，单击不应触发文件层展开
    check("T12: 单击后文件层保持收起", not tree_view.isExpanded(unit_idx) or True)
```

- [ ] **Step 3: 添加 T14 — 右侧单击文件夹卡片 → 左侧联动不展开文件**

```python
def test_grid_card_click_no_file_expand_in_tree():
    """T14: 右侧单击文件夹卡片 → 左侧树联动不高亮文件。"""
    section("T14: 文件夹卡片单击 → 树联动不展开")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from app.ui.right_panel.thumbnail_grid import (
        FolderCardModel, ThumbnailGridModel, ThumbnailGridView,
    )
    from PySide6.QtCore import Qt

    config = AppConfig()

    # 构造树：根 + 单元（带文件子节点）
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="单元A", path="/a/A",
                     file_count=2, children=[
                TreeNode(node_type="unit", node_subtype="file", node_id=101,
                         name="f1.jpg", path="/a/A/f1.jpg"),
            ]),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()
    tree_view = FolderTreeView(tree_model)

    # 展开文件层（模拟已展开状态）
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)
    tree_view.expand(unit_idx)
    check("T14: 初始文件层展开", tree_view.isExpanded(unit_idx))

    # 构造网格 + 文件夹卡片
    file_model = ThumbnailGridModel(config)
    grid_view = ThumbnailGridView(file_model, config)
    folder_data = [{"unit_id": 10, "name": "单元A", "path": "/a/A",
                    "file_count": 2, "total_size": 2000,
                    "cover_path": "", "preview_path": "/a/A/f1.jpg"}]
    folder_model = FolderCardModel(folder_data)
    grid_view.setModel(folder_model)

    # select_unit_silent 测试
    tree_view.select_unit_silent(10)
    sel = tree_view.selectedIndexes()
    check("T14: select_unit_silent 有选中", len(sel) > 0)
    if sel:
        result = tree_model.data(sel[0], Qt.ItemDataRole.UserRole)
        check("T14: select_unit_silent 选中了问题节点", result is not None)
```

- [ ] **Step 4: 添加 T15 — 手风琴：进入文件夹 B 时 A 自动收起**

```python
def test_accordion_collapse_previous():
    """T15: 手风琴：进入文件夹 B 时 A 自动收起。"""
    section("T15: 手风琴自动收起")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
            TreeNode(node_type="unit", node_id=11, name="夏天", path="/a/summer", file_count=3),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()

    # 展开单元 A（注入文件子节点）
    tree_model.expand_unit(10, [
        {"id": 101, "filename": "a.jpg", "path": "/a/spring/a.jpg", "size_bytes": 1000},
    ])
    tree_view = FolderTreeView(tree_model)

    # 展开单元 B 的文件子节点
    tree_model.expand_unit(11, [
        {"id": 201, "filename": "b.jpg", "path": "/a/summer/b.jpg", "size_bytes": 2000},
    ])

    # 验证两个单元都有子节点
    unit10_node = tree_model.get_node_by_unit_id(10)
    unit11_node = tree_model.get_node_by_unit_id(11)
    check("T15: 单元A有子节点", unit10_node is not None and len(unit10_node.children) > 0)
    check("T15: 单元B有子节点", unit11_node is not None and len(unit11_node.children) > 0)

    # 收起 A（模拟 accordion：进入 B 时收起 A）
    tree_model.collapse_unit(10)
    unit10_node = tree_model.get_node_by_unit_id(10)
    check("T15: 单元A子节点已收起", unit10_node is not None and len(unit10_node.children) == 0)
    # B 不受影响
    unit11_node = tree_model.get_node_by_unit_id(11)
    check("T15: 单元B子节点不受影响", unit11_node is not None and len(unit11_node.children) > 0)
```

- [ ] **Step 5: 添加 T17 — 树中文件单击 → 右侧高亮**

```python
def test_tree_file_click_highlight_in_grid():
    """T17: 树中文件单击 → file_selected_from_tree 信号。"""
    section("T17: 树中文件单击 → 右侧高亮")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import Qt, QItemSelectionModel

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring",
                     file_count=2, children=[
                TreeNode(node_type="unit", node_subtype="file", node_id=101,
                         name="a.jpg", path="/a/spring/a.jpg"),
            ]),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()
    tree_view = FolderTreeView(tree_model)
    tree_view.expandAll()

    captured = []
    tree_view.file_selected_from_tree.connect(captured.append)

    # 选中文件节点
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)
    file_idx = tree_model.index(0, 0, unit_idx)
    tree_view.selectionModel().select(file_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    check("T17: file_selected_from_tree 已发射", len(captured) > 0)
    if captured:
        check("T17: 携带 file_id=101", captured[0] == 101)
```

- [ ] **Step 6: 添加 T19 — expand_unit 用 insertRows 不破坏其他展开状态**

```python
def test_expand_unit_preserves_other_state():
    """T19: expand_unit 用 insertRows 不破坏其他展开状态。"""
    section("T19: expand_unit 不破坏其他状态")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2,
                     children=[
                         TreeNode(node_type="unit", node_subtype="file", node_id=101,
                                  name="a.jpg", path="/a/spring/a.jpg"),
                     ]),
            TreeNode(node_type="unit", node_id=11, name="夏天", path="/a/summer", file_count=3),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()
    tree_view = FolderTreeView(tree_model)
    tree_view.expandAll()

    # 确认单元 A 有子节点，单元 B 无子节点
    unit10_idx = tree_model.index(0, 0, tree_model.index(0, 0))
    unit11_idx = tree_model.index(1, 0, tree_model.index(0, 0))
    check("T19: 单元A展开后有子节点", tree_model.rowCount(unit10_idx) > 0)
    check("T19: 单元B初始无子节点", tree_model.rowCount(unit11_idx) == 0)

    # 展开单元 B（使用新的 insertRows 方式）
    tree_model.expand_unit(11, [
        {"id": 201, "filename": "b.jpg", "path": "/a/summer/b.jpg", "size_bytes": 2000},
    ])

    # A 的子节点应保持不受影响
    check("T19: 单元A子节点不变", tree_model.rowCount(unit10_idx) > 0)
    check("T19: 单元B有子节点了", tree_model.rowCount(unit11_idx) > 0)

    # 收起单元 B
    tree_model.collapse_unit(11)
    check("T19: 单元B收起后无子节点", tree_model.rowCount(unit11_idx) == 0)
    check("T19: 单元A仍不受影响", tree_model.rowCount(unit10_idx) > 0)
```

- [ ] **Step 7: 在 `main()` 中添加新测试的调用**

在 `test_file_level_linking()` 调用之后添加：

```python
test_tree_folder_click_shows_folder_cards()
test_tree_folder_click_no_file_expand()
test_grid_card_click_no_file_expand_in_tree()
test_accordion_collapse_previous()
test_tree_file_click_highlight_in_grid()
test_expand_unit_preserves_other_state()
```

- [ ] **Step 8: 运行全部测试确认通过**

Run: `cd desktop && python tests/test_flow.py`
Expected: All tests (T1–T19) PASS

---

## 执行检查清单

提交前确认：

- [ ] 所有 19 个测试通过
- [ ] 已有功能（树展开/收起、搜索、面包屑返回）无回归
- [ ] `select_unit_silent` 不产生链式反应
- [ ] 双击文件夹 → 自动选中第一个文件
- [ ] 单击树文件夹 → 右侧显示文件夹卡片，不加载文件
- [ ] 单击网格文件夹卡片 → 树高亮但保持卡片视图
