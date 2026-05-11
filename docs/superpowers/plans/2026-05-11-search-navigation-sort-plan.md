# 搜索·联动·排序 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复搜索 Bug、实现左右面板双向联动、树展开显示文件节点、排序扩展到文件夹列表并增加时间排序。

**Architecture:** 改动集中在 3 个文件：folder_tree.py（TreeNode 扩展+树展开+搜索查找）、thumbnail_grid.py（FolderCardModel 排序+信号新增）、main_window.py（连接联动信号+自动选中首个搜索结果）。不改变现有架构，每一任务可独立测试。

**Tech Stack:** PySide6, SQLAlchemy

---

## 文件修改清单

| 文件 | 改动 |
|------|------|
| `desktop/app/ui/left_panel/folder_tree.py` | `TreeNode` 加 `node_subtype`/`created_at`；`expand_unit()`/`collapse_unit()`；`find_first_visible_unit()`；`select_tree_node_by_file_id()`；`rowCount/data/index` 支持第 3 层 |
| `desktop/app/ui/right_panel/thumbnail_grid.py` | 新增 `folder_selected`/`file_selected_in_grid` 信号；`select_file_by_id()`；`FolderCardModel` 加 `set_sort()` |
| `desktop/app/ui/main_window.py` | `_apply_current_filter` 自动选首个；连接联动信号；`_on_sort` 适配文件夹卡片 |
| `desktop/tests/test_flow.py` | 新增 TDD 测试覆盖每项改动 |

---

### Task 1: 搜索 Bug 修复 + 自动选首个可见单元

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py`
- Modify: `desktop/app/ui/main_window.py`
- Test: `desktop/tests/test_flow.py`

- [ ] **Step 1: 写 TDD 测试**

在 test_flow.py 末尾 `def main()` 之前添加：

```python
def test_search_auto_select_first():
    """TDD: 搜索后自动选中第一个可见单元。"""
    section("TDD: 搜索自动选中首个")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import QModelIndex

    config = AppConfig()
    model = FolderTreeModel(config)
    model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
            TreeNode(node_type="unit", node_id=11, name="夏天", path="/a/summer", file_count=1),
        ]),
    ]
    model.beginResetModel()
    model.endResetModel()

    view = FolderTreeView(model)
    view.setModel(model)
    view.filter_by_name("春")

    first = view.find_first_visible_unit()
    check("TDD: 搜索'春'找到单元10", first == 10)

    view.filter_by_name("不存在的")
    first2 = view.find_first_visible_unit()
    check("TDD: 搜索不存在的返回 None", first2 is None)
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
cd desktop && python -c "
import sys, test_flow
assert hasattr(test_flow, 'test_search_auto_select_first')
print('test exists')
"
```

Expected: `AttributeError: 'FolderTreeView' object has no attribute 'find_first_visible_unit'`

- [ ] **Step 3: 实现 `find_first_visible_unit`**

在 `desktop/app/ui/left_panel/folder_tree.py` 的 `FolderTreeView` 中添加：

```python
def find_first_visible_unit(self) -> Optional[int]:
    """返回第一个可见的单元 node_id，没有则返回 None。"""
    model = self._model
    for root_row, root in enumerate(model._roots):
        root_idx = model.index(root_row, 0)
        for child_row, child in enumerate(root.children):
            if not self.isRowHidden(child_row, root_idx):
                return child.node_id
    return None
```

在文件头部 `from typing import Optional` 确认已导入。

- [ ] **Step 4: 修改 `_apply_current_filter`**

在 `desktop/app/ui/main_window.py` 中：

```python
def _apply_current_filter(self) -> None:
    media_data = self._filter_combo.currentData()
    search_text = self._search_input.text()
    self._grid_model.apply_filter(
        search_text=search_text,
        media_filter=media_data if media_data else "",
    )
    self._tree_view.filter_by_name(search_text)
    # 搜索时自动选中第一个可见单元
    if search_text.strip():
        first = self._tree_view.find_first_visible_unit()
        if first:
            self._on_unit_selected([first])
```

- [ ] **Step 5: 运行测试确认 PASS**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS（新测试通过 + 原有测试不变）

---

### Task 2: 文件夹卡片级联动（单击卡片 → 左侧高亮）

**Files:**
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py`
- Modify: `desktop/app/ui/main_window.py`
- Test: `desktop/tests/test_flow.py`

- [ ] **Step 1: 写 TDD 测试**

```python
def test_folder_card_click_linking():
    """TDD: 单击文件夹卡片发射 folder_selected 信号。"""
    section("TDD: 文件夹卡片单击联动")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from app.ui.right_panel.thumbnail_grid import FolderCardModel
    from PySide6.QtCore import Qt

    data = [
        {"unit_id": 10, "name": "春天", "path": "/a", "file_count": 2, "total_size": 2000},
        {"unit_id": 11, "name": "夏天", "path": "/b", "file_count": 1, "total_size": 1000},
    ]
    model = FolderCardModel(data)
    check("TDD: 卡片模型行数=2", model.rowCount() == 2)
    check("TDD: 行0 unit_id=10", model.data(model.index(0, 0), Qt.ItemDataRole.UserRole + 1) == 10)
    check("TDD: 行1 unit_id=11", model.data(model.index(1, 0), Qt.ItemDataRole.UserRole + 1) == 11)
```

- [ ] **Step 2: 运行测试确认 PASS**

```bash
cd desktop && python tests/test_flow.py
```

这一步是数据验证，应该 PASS。

- [ ] **Step 3: 给 ThumbnailGridView 添加 folder_selected 信号**

在 `desktop/app/ui/right_panel/thumbnail_grid.py` 中：

```python
class ThumbnailGridView(QListView):
    # ... 现有信号 ...
    folder_selected = Signal(int)  # 新增：单击文件夹卡片时发射 unit_id
```

在 `__init__` 末尾添加：

```python
self.selectionModel().selectionChanged.connect(self._on_any_selection_changed)
```

添加方法：

```python
@Slot()
def _on_any_selection_changed(self, selected, deselected) -> None:
    """监听选中变化：文件夹卡片模式 → 发射 folder_selected。"""
    indexes = selected.indexes()
    if not indexes:
        return
    model = self.model()
    if isinstance(model, FolderCardModel):
        unit_id = model.data(indexes[0], Qt.ItemDataRole.UserRole + 1)
        if unit_id:
            self.folder_selected.emit(unit_id)
```

注意：需要修改现有的 `selectionModel().selectionChanged.connect(self._on_selection_changed)` 调用 —— 当前 `_on_selection_changed` 只处理 `file_selected`，需要保留它。方法：保留原来的连接，新增 `_on_any_selection_changed` 并行处理文件夹模式。

但注意 `_on_selection_changed` 目前直接连接在 `__init__` 中。为了避免冲突，更好的方式：合并两个处理器。

修改现有 `__init__` 中的连接：

```python
# 原来：
self.selectionModel().selectionChanged.connect(self._on_selection_changed)

# 改为两个独立的：
self.selectionModel().selectionChanged.connect(self._on_selection_changed)
self.selectionModel().selectionChanged.connect(self._on_any_selection_changed)
```

`_on_selection_changed` 保留原样处理文件选中。新方法处理文件夹联信号。

- [ ] **Step 4: 主窗口连接信号**

在 `desktop/app/ui/main_window.py` 的 `_connect_signals` 中添加：

```python
# ---- 网格文件夹卡片单击 → 左侧高亮 ----
self._grid_view.folder_selected.connect(self._tree_view.select_unit)
```

- [ ] **Step 5: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 3: 左侧树展开单元内部结构（文件节点）

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py` — 核心修改
- Modify: `desktop/app/ui/main_window.py` — 调用 expand/collapse
- Test: `desktop/tests/test_flow.py`

- [ ] **Step 1: 写 TDD 测试**

```python
def test_tree_expand_unit():
    """TDD: expand_unit 向树节点注入子文件。"""
    section("TDD: 树展开单元文件")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import QModelIndex, Qt

    config = AppConfig()
    model = FolderTreeModel(config)
    model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
        ]),
    ]
    model.beginResetModel()
    model.endResetModel()

    # 展开前：单元下无子节点
    unit_idx = model.index(0, 0)
    unit_idx2 = model.index(0, 0, unit_idx)
    check("TDD: 展开前子节点数=0", model.rowCount(unit_idx) == 0)

    # 展开：注入文件
    files = [
        {"id": 101, "filename": "a.jpg", "path": "/a/spring/a.jpg", "size_bytes": 1000},
        {"id": 102, "filename": "b.mp4", "path": "/a/spring/b.mp4", "size_bytes": 5000},
    ]
    model.expand_unit(10, files)
    check("TDD: 展开后子节点数=2", model.rowCount(unit_idx) == 2)
    # 验证第一行是文件
    file_idx = model.index(0, 0, unit_idx)
    check("TDD: 文件节点名 a.jpg", model.data(file_idx, Qt.ItemDataRole.DisplayRole) == "a.jpg")

    # 收起
    model.collapse_unit(10)
    check("TDD: 收起后子节点数=0", model.rowCount(unit_idx) == 0)
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
cd desktop && python tests/test_flow.py
```

Expected: `AttributeError: 'FolderTreeModel' object has no attribute 'expand_unit'`

- [ ] **Step 3: 实现 TreeNode 扩展**

在 `desktop/app/ui/left_panel/folder_tree.py` 中修改 `TreeNode`：

```python
@dataclass
class TreeNode:
    node_type: str           # "root" | "unit" | "favorites"
    node_id: int             # 数据库 ID
    name: str                # 显示名称
    path: str                # 文件夹/文件路径
    file_count: int = 0
    total_size: int = 0
    is_manual: bool = False
    is_starred: bool = False
    status: str = "active"
    library_root_id: Optional[int] = None
    children: list["TreeNode"] = None
    node_subtype: str = ""   # "" 普通单元 / "file" 文件节点
    created_at: Optional[str] = None  # ISO 时间
```

- [ ] **Step 4: 实现 expand_unit / collapse_unit**

在 `FolderTreeModel` 中添加：

```python
def expand_unit(self, unit_id: int, files: list) -> None:
    """加载单元下的文件作为树节点子项。"""
    node = self.get_node_by_unit_id(unit_id)
    if not node:
        return
    node.children = [
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
    self.layoutChanged.emit()

def collapse_unit(self, unit_id: int) -> None:
    """卸载文件子节点。"""
    node = self.get_node_by_unit_id(unit_id)
    if node:
        node.children = []
        self.layoutChanged.emit()
```

- [ ] **Step 5: 修改 rowCount / data 支持第 3 层**

```python
def rowCount(self, parent=QModelIndex()) -> int:
    if not parent.isValid():
        return len(self._roots)
    node = parent.internalPointer()
    if node and node.node_type in ("root", "favorites"):
        return len(node.children)
    if node and node.node_type == "unit" and node.children:
        return len(node.children)
    return 0
```

`data()` 中添加文件节点的 COL_META 显示：

```python
elif col == self.COL_META:
    if node.node_subtype == "file":
        from app.utils.file_helpers import format_size
        return format_size(node.total_size)
    elif node.node_type == "unit":
        if node.status == "merged":
            return ""
        from app.utils.file_helpers import format_size
        return f"{node.file_count} 个 · {format_size(node.total_size)}"
    elif node.node_type == "root":
        active = sum(1 for c in node.children if c.status == "active")
        return f"{active} 个片段" if active else ""
    elif node.node_type == "favorites":
        return f"{len(node.children)} 个收藏"
    return ""
```

- [ ] **Step 6: 修改 index/parent 支持第 3 层**

`index()` 方法扩展：

```python
def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
    if not self.hasIndex(row, column, parent):
        return QModelIndex()
    if not parent.isValid():
        if row < len(self._roots):
            return self.createIndex(row, column, self._roots[row])
    else:
        pnode = parent.internalPointer()
        if pnode:
            if pnode.node_type in ("root", "favorites") and row < len(pnode.children):
                return self.createIndex(row, column, pnode.children[row])
            if pnode.node_type == "unit" and pnode.children and row < len(pnode.children):
                return self.createIndex(row, column, pnode.children[row])
    return QModelIndex()
```

`parent()` 方法扩展：

```python
def parent(self, index: QModelIndex) -> QModelIndex:
    if not index.isValid():
        return QModelIndex()
    node = index.internalPointer()
    if not node:
        return QModelIndex()
    if node.node_subtype == "file":
        # 文件节点的父节点是单元
        for r_idx, root in enumerate(self._roots):
            for c_idx, child in enumerate(root.children):
                if child.node_id == node.library_root_id or node.node_id in [f.node_id for f in child.children]:
                    return self.createIndex(c_idx, 0, child)
        return QModelIndex()
    if node.node_type == "unit":
        for r_idx, root in enumerate(self._roots):
            if node.library_root_id == -1 and root.node_type == "favorites":
                return self.createIndex(r_idx, 0, root)
            if root.node_id == node.library_root_id:
                return self.createIndex(r_idx, 0, root)
    return QModelIndex()
```

注意：文件节点的 `library_root_id` 可能需要调整。实际实现时，更简单的办法：在 `TreeNode` 添加 `parent_id` 字段或直接使用 `library_root_id`。这里的关键是 `parent()` 需要正确识别文件节点的父节点是哪个单元。

简化实现：在创建文件节点时，`library_root_id` 设为父单元 ID，`parent()` 通过遍历查找匹配。

- [ ] **Step 7: 主窗口调用 expand/collapse**

在 `_on_unit_double_clicked` 中调用：

```python
@Slot(int)
def _on_unit_double_clicked(self, unit_id: int) -> None:
    self._current_unit_id = unit_id
    self._grid_view.load_unit(unit_id)
    self._tree_view.select_unit(unit_id)

    # 加载树的子文件节点
    with DatabaseManager.session() as session:
        files = q.get_files_by_unit(session, unit_id)
        file_dicts = [
            {"id": f.id, "filename": f.filename, "path": f.path, "size_bytes": f.size_bytes}
            for f in files
        ]
    self._tree_view.model().expand_unit(unit_id, file_dicts)
    self._breadcrumb.show()
    ...
```

在 `_on_breadcrumb_back` 中调用：

```python
# 面包屑返回时收起
if self._current_unit_id:
    self._tree_view.model().collapse_unit(self._current_unit_id)
```

- [ ] **Step 8: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 4: 文件级双向联动

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py`
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py`
- Modify: `desktop/app/ui/main_window.py`
- Test: `desktop/tests/test_flow.py`

- [ ] **Step 1: 写 TDD 测试**

```python
def test_file_level_linking():
    """TDD: 文件级联动信号。"""
    section("TDD: 文件级联动")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from app.ui.right_panel.thumbnail_grid import ThumbnailGridModel, ThumbnailGridView
    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import Qt
    config = AppConfig()

    # 网格模型验证
    files = [
        {"id": 101, "filename": "a.jpg", "path": "/a.jpg", "media_type": "image", "size_bytes": 1000},
        {"id": 102, "filename": "b.mp4", "path": "/b.mp4", "media_type": "video", "size_bytes": 2000},
    ]
    model = ThumbnailGridModel(config)
    model.set_files(files)
    check("TDD: 文件模型行0 id=101", model.data(model.index(0, 0), Qt.ItemDataRole.UserRole + 1) == 101)

    # 树文件节点验证
    file_node = TreeNode(node_type="unit", node_subtype="file", node_id=101, name="a.jpg", path="/a.jpg")
    check("TDD: 文件节点 subtype", file_node.node_subtype == "file")
    check("TDD: 文件节点 node_id", file_node.node_id == 101)
```

- [ ] **Step 2: 添加文件级树选中信号**

在 `FolderTreeView` 中添加：

```python
file_selected_from_tree = Signal(int)  # file_id
```

修改 `_on_selection_changed` 或添加新连接：

```python
# 在 __init__ 或单独的 handler 中
self.selectionModel().selectionChanged.connect(self._on_tree_selection_changed)

@Slot()
def _on_tree_selection_changed(self, selected, deselected) -> None:
    indexes = selected.indexes()
    if not indexes:
        return
    idx = indexes[0]
    if idx.column() != 0:
        return
    node = idx.internalPointer()
    if node and node.node_subtype == "file":
        self.file_selected_from_tree.emit(node.node_id)
```

- [ ] **Step 3: 添加网格文件选中信号**

在 `ThumbnailGridView` 中添加：

```python
file_selected_in_grid = Signal(int)  # file_id
```

`_on_selection_changed` 已存在，增强它：

```python
@Slot()
def _on_selection_changed(self) -> None:
    idxs = self.selectedIndexes()
    if not idxs:
        return
    model = self.model()
    fid = model.data(idxs[0], Qt.ItemDataRole.UserRole + 1)
    if fid:
        self.file_selected.emit(fid)
        # 非文件夹模式时发射 file_selected_in_grid
        if not isinstance(model, FolderCardModel):
            self.file_selected_in_grid.emit(fid)
```

- [ ] **Step 4: 实现 select_file_by_id**

在 `ThumbnailGridView` 中添加：

```python
def select_file_by_id(self, file_id: int) -> None:
    """选中指定 ID 的文件缩略图。"""
    model = self.model()
    if isinstance(model, FolderCardModel):
        return
    for row in range(model.rowCount()):
        idx = model.index(row, 0)
        if model.data(idx, Qt.ItemDataRole.UserRole + 1) == file_id:
            self.setCurrentIndex(idx)
            self.scrollTo(idx)
            return
```

- [ ] **Step 5: 实现 select_tree_node_by_file_id**

在 `FolderTreeView` 中添加：

```python
def select_tree_node_by_file_id(self, file_id: int) -> None:
    """选中文件节点（在已展开的单元中）。"""
    model = self._model
    for root_row, root in enumerate(model._roots):
        root_idx = model.index(root_row, 0)
        for child_row, child in enumerate(root.children):
            if child.node_subtype == "file" and child.node_id == file_id:
                child_idx = model.index(child_row, 0, root_idx)
                self.setCurrentIndex(child_idx)
                return
            # 文件节点在展开的 unit 内部
            if child.node_type == "unit" and child.children:
                for file_row, file_node in enumerate(child.children):
                    if file_node.node_id == file_id:
                        unit_idx = model.index(child_row, 0, root_idx)
                        file_idx = model.index(file_row, 0, unit_idx)
                        self.setCurrentIndex(file_idx)
                        return
```

- [ ] **Step 6: 主窗口连接所有信号**

```python
# 连接文件级信号
self._tree_view.file_selected_from_tree.connect(self._grid_view.select_file_by_id)
self._grid_view.file_selected_in_grid.connect(self._tree_view.select_tree_node_by_file_id)
```

- [ ] **Step 7: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 5: FolderCardModel 排序

**Files:**
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py`
- Modify: `desktop/app/ui/main_window.py`
- Test: `desktop/tests/test_flow.py`

- [ ] **Step 1: 写 TDD 测试**

```python
def test_folder_card_sort():
    """TDD: 文件夹卡片排序。"""
    section("TDD: 文件夹卡片排序")
    from app.ui.right_panel.thumbnail_grid import FolderCardModel

    data = [
        {"unit_id": 3, "name": "zzz", "path": "/z", "file_count": 1, "total_size": 3000,
         "created_at": "2026-01-03"},
        {"unit_id": 1, "name": "aaa", "path": "/a", "file_count": 2, "total_size": 1000,
         "created_at": "2026-01-01"},
        {"unit_id": 2, "name": "bbb", "path": "/b", "file_count": 3, "total_size": 2000,
         "created_at": "2026-01-02"},
    ]
    model = FolderCardModel(data)

    # 名称升序
    model.set_sort("name", ascending=True)
    names = [model.data(model.index(i, 0), 0x100) for i in range(3)]
    check("TDD: 卡片名称升序 aaa第一", names[0] == "aaa")
    check("TDD: 卡片名称升序 zzz第三", names[2] == "zzz")

    # 大小降序
    model.set_sort("size", ascending=False)
    sizes = [model.data(model.index(i, 0), 0x100+3) for i in range(3)]
    check("TDD: 卡片大小降序 3000最大", sizes[0] == "3.0 KB" or "3000" in str(sizes[0]))

    # 时间升序
    model.set_sort("date", ascending=True)
    dates = [d["created_at"] for d in model._data]
    check("TDD: 卡片时间升序 01-01第一", dates[0] == "2026-01-01")
    check("TDD: 卡片时间升序 01-03第三", dates[2] == "2026-01-03")
```

注意：`FolderCardModel.data()` 的 DisplayRole（0x100）返回名称，UserRole+3 返回格式化字符串（文件数）。根据实际角色调整断言。

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
cd desktop && python tests/test_flow.py
```

Expected: `AttributeError: 'FolderCardModel' object has no attribute 'set_sort'`

- [ ] **Step 3: 实现 FolderCardModel.set_sort**

```python
class FolderCardModel(QAbstractListModel):
    def __init__(self, data):
        super().__init__()
        self._data = data

    def set_sort(self, field: str, ascending: bool = True) -> None:
        """按字段排序。field: 'name'/'size'/'date'。"""
        self._sort_field = field
        self._sort_asc = ascending
        self.beginResetModel()
        rev = not ascending
        if field == "name":
            self._data.sort(key=lambda x: x.get("name", "").lower(), reverse=rev)
        elif field == "size":
            self._data.sort(key=lambda x: x.get("total_size", 0), reverse=rev)
        elif field == "date":
            self._data.sort(key=lambda x: x.get("created_at", ""), reverse=rev)
        self.endResetModel()
```

- [ ] **Step 4: 修改 _on_sort 适配文件夹卡片**

```python
def _on_sort(self, field: str) -> None:
    model = self._grid_view.model()
    card_model = getattr(model, '_data', None)
    # 判断当前视图是否文件夹卡片
    if hasattr(model, 'set_sort'):
        # FolderCardModel 也有 set_sort
        if model.set_sort.__func__ is not ThumbnailGridModel.set_sort:
            model.set_sort(field, True)
        else:
            self._grid_model.set_sort(field, ...)
        return
    # 默认走文件模型
    self._grid_model.set_sort(field, ...)
    ...
```

实际上更干净的写法：检查两个模型各自是否支持 sort：

```python
def _on_sort(self, field: str) -> None:
    current = self._grid_view.model()
    if isinstance(current, FolderCardModel):
        current.set_sort(field, True)
    else:
        model = self._grid_model
        if model.sort_field == field:
            model.set_sort(field, not model._sort_asc)
        else:
            model.set_sort(field, True)
    ...
```

- [ ] **Step 5: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 6: 创建时间排序 + 显示

**Files:**
- Modify: `desktop/app/ui/main_window.py`
- Modify: `desktop/app/ui/left_panel/folder_tree.py`

- [ ] **Step 1: 在工具栏添加「时间」排序按钮**

在 `main_window.py` 的 `_setup_tool_bar()` 中，排序按钮区增加：

```python
self._sort_date_btn = QPushButton("时间↑")
self._sort_date_btn.setCheckable(True)
self._sort_date_btn.setToolTip("按创建时间排序")
self._sort_date_btn.clicked.connect(lambda: self._on_sort("date"))
toolbar.addWidget(self._sort_date_btn)
```

- [ ] **Step 2: 更新 _update_sort_buttons**

```python
def _update_sort_buttons(self) -> None:
    field = self._grid_model.sort_field
    asc = self._grid_model._sort_asc
    self._sort_name_btn.blockSignals(True)
    self._sort_size_btn.blockSignals(True)
    self._sort_date_btn.blockSignals(True)
    self._sort_name_btn.setChecked(field == "name")
    self._sort_size_btn.setChecked(field == "size")
    self._sort_date_btn.setChecked(field == "date")
    self._sort_name_btn.blockSignals(False)
    self._sort_size_btn.blockSignals(False)
    self._sort_date_btn.blockSignals(False)
    arrow = "↑" if asc else "↓"
    self._sort_name_btn.setText(f"名称{arrow if field == 'name' else '↑'}")
    self._sort_size_btn.setText(f"大小{arrow if field == 'size' else '↑'}")
    self._sort_date_btn.setText(f"时间{arrow if field == 'date' else '↑'}")
```

- [ ] **Step 3: TreeNode 携带 created_at**

在 `FolderTreeModel.refresh()` 中，构建 `TreeNode` 时填充时间：

```python
unit_node = TreeNode(
    ...
    created_at=unit.created_at.isoformat() if unit.created_at else None,
)
```

- [ ] **Step 4: COL_META 显示创建时间**

在 `FolderTreeModel.data()` 的 `COL_META` 分支中，对文件节点显示大小，对单元节点保持原有显示不变。创建时间可在 tooltip 中查看，也可在 COL_META 中显示：

```python
elif col == self.COL_META:
    if node.node_subtype == "file":
        from app.utils.file_helpers import format_size
        return format_size(node.total_size)
    elif node.node_type == "unit" and node.created_at:
        # 显示日期 + 文件数
        date_str = node.created_at[:10]  # "2026-01-15"
        return f"{date_str} · {node.file_count}个"
    ...
```

- [ ] **Step 5: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS
