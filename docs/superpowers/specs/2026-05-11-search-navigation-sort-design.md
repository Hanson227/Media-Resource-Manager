# 搜索·联动·排序 功能设计

> 优化搜索行为、实现左右面板双向联动、扩展排序功能到文件夹列表。

---

## 1. 搜索 Bug 修复

### 根因

`_apply_current_filter` 只过滤了右侧文件网格模型（`ThumbnailGridModel`），不处理右侧文件夹卡片视图（`FolderCardModel`）。搜索后左侧树正确过滤，但右侧缩略图区域无内容可显示。

### 修复方案

搜索后有匹配结果时，自动选中左侧第一个可见单元，触发 `_on_unit_selected` 加载右侧内容。

```python
# main_window.py
def _apply_current_filter(self) -> None:
    search_text = self._search_input.text().strip()
    # 1. 过滤左侧树
    self._tree_view.filter_by_name(search_text)
    # 2. 过滤右侧文件网格（如果处于文件视图）
    media_data = self._filter_combo.currentData()
    self._grid_model.apply_filter(
        search_text=search_text,
        media_filter=media_data if media_data else "",
    )
    # 3. 搜索时自动选中第一个可见单元
    if search_text:
        first = self._tree_view.find_first_visible_unit()
        if first:
            self._on_unit_selected([first])
```

**新增方法：**
- `FolderTreeView.find_first_visible_unit() -> Optional[int]` — 遍历树中找到第一个未隐藏的单元节点 ID

---

## 2. 双向联动

### 2.1 文件夹卡片级联动（根目录/外层视图）

左右侧同步选中状态：单击右侧文件夹卡片 ⇄ 左侧高亮对应单元。

**右侧单击 → 左侧高亮：**

```python
# ThumbnailGridView 新增
folder_selected = Signal(int)  # unit_id

# 监听文件夹卡片模式下的选中变化
def _on_folder_selection_changed(self, selected, deselected):
    idxs = selected.indexes()
    if not idxs:
        return
    model = self.model()
    if isinstance(model, FolderCardModel):
        unit_id = model.data(idxs[0], Qt.ItemDataRole.UserRole + 1)
        if unit_id:
            self.folder_selected.emit(unit_id)
```

```python
# main_window.py 连接
self._grid_view.folder_selected.connect(self._tree_view.select_unit)
```

**左侧单击 → 右侧滚动：** 已有 `_on_unit_selected`，增强为：如果右侧在文件夹卡片模式，滚动到对应卡片位置。

### 2.2 文件级联动（进入单元后）

左侧树展开单元后，文件节点与右侧缩略图网格双向同步。

**左侧单击文件 → 右侧选择缩略图：**

```python
# FolderTreeView 新增
file_selected_from_tree = Signal(int)  # file_id

def _on_tree_file_selected(self, index):
    node = index.internalPointer()
    if node and node.node_subtype == "file":
        self.file_selected_from_tree.emit(node.node_id)
```

```python
# main_window.py 连接
self._tree_view.file_selected_from_tree.connect(self._grid_view.select_file_by_id)
```

**右侧单击文件 → 左侧高亮文件节点：**

```python
# ThumbnailGridView 新增
file_selected_in_grid = Signal(int)  # file_id

# 监听文件模式下的选中变化
def _on_file_selection_changed(self, selected, deselected):
    idxs = selected.indexes()
    if not idxs:
        return
    model = self.model()
    if not isinstance(model, FolderCardModel):
        fid = model.data(idxs[0], Qt.ItemDataRole.UserRole + 1)
        if fid:
            self.file_selected_in_grid.emit(fid)
```

```python
# main_window.py 连接
self._grid_view.file_selected_in_grid.connect(self._tree_view.select_tree_node_by_file_id)
```

---

## 3. 左侧树展开单元内部结构

### 3.1 当前限制

树模型 `FolderTreeModel` 只有 2 层（根 → 单元）。`TreeNode.children` 已存在但只用于根下的单元列表，未加载文件。

### 3.2 设计方案

**TreeNode 扩展：**

```python
@dataclass
class TreeNode:
    node_type: str        # "root" | "unit" | "favorites"
    node_subtype: str     # "" | "file"（当 node_type 为 "unit" 且需展示内部文件时）
    node_id: int          # unit_id 或 file_id
    name: str
    ...
    created_at: Optional[str] = None   # 创建时间（新增）
```

**展开时机：** 双击进入单元时（`_on_unit_double_clicked`），触发生成子节点。

```python
# FolderTreeModel 新增
def expand_unit(self, unit_id: int, files: list) -> None:
    """加载单元的子文件到树节点下。"""
    node = self.get_node_by_unit_id(unit_id)
    if not node:
        return
    node.children = [
        TreeNode(
            node_type="unit",
            node_subtype="file",
            node_id=f.id,
            name=f.filename,
            path=f.path,
            file_count=0,
            total_size=f.size_bytes,
        )
        for f in files[:200]  # 最多 200 个文件
    ]
    self.layoutChanged.emit()

def collapse_unit(self, unit_id: int) -> None:
    """卸载单元的子节点，收起。"""
    node = self.get_node_by_unit_id(unit_id)
    if node:
        node.children = []
        self.layoutChanged.emit()
```

**树模型支持 3 层：**

```python
# rowCount 扩展
def rowCount(self, parent=QModelIndex()) -> int:
    if not parent.isValid():
        return len(self._roots)
    node = parent.internalPointer()
    if node and node.node_type in ("root", "favorites"):
        return len(node.children)
    # 新增：单元节点展开后有子节点
    if node and node.node_type == "unit" and node.children:
        return len(node.children)
    return 0

# data 扩展
def data(self, index, role):
    ...
    if role == Qt.ItemDataRole.DisplayRole and col == self.COL_NAME:
        if node.node_subtype == "file":
            return node.name  # 显示文件名
        ...
    elif col == self.COL_META:
        if node.node_subtype == "file":
            return format_size(node.total_size)
        ...
```

**收起时机：** 面包屑返回时（`_on_breadcrumb_back`），调用 `collapse_unit` 卸载子节点。

---

## 4. 排序功能扩展到文件夹列表

### 4.1 当前限制

排序按钮只对 `ThumbnailGridModel`（文件视图）生效。`FolderCardModel`（文件夹卡片视图）不支持排序。

### 4.2 方案

**FolderCardModel 添加 sort 方法：**

```python
class FolderCardModel(QAbstractListModel):
    def __init__(self, data):
        super().__init__()
        self._data = data
        self._sort_field = ""
        self._sort_asc = True

    def set_sort(self, field: str, ascending: bool = True) -> None:
        self._sort_field = field
        self._sort_asc = ascending
        self.beginResetModel()
        self._sort_field in ("name", "size", "date"):
            rev = not ascending
            if field == "name":
                self._data.sort(key=lambda x: x.get("name", "").lower(), reverse=rev)
            elif field == "size":
                self._data.sort(key=lambda x: x.get("total_size", 0), reverse=rev)
            elif field == "date":
                self._data.sort(key=lambda x: x.get("created_at", ""), reverse=rev)
        self.endResetModel()
```

**排序按钮根据当前视图切换到对应模型：**

```python
# main_window.py _on_sort
def _on_sort(self, field: str) -> None:
    model = self._grid_view.model()
    if isinstance(model, FolderCardModel):
        model.set_sort(field, ...)
    else:
        self._grid_model.set_sort(field, ...)
    self._update_sort_buttons()
```

---

## 5. 按创建时间排序 + 显示

### 5.1 数据来源

`ResourceUnit.created_at` 和 `MediaFile.created_at` 已在 ORM 模型中存在，但 `TreeNode` 未携带。

### 5.2 修改

**TreeNode 新增字段：**

```python
created_at: Optional[str] = None  # ISO 格式时间字符串
```

**数据加载时填充：**

```python
# FolderTreeModel.refresh() 中
unit_node = TreeNode(
    ...
    created_at=unit.created_at.isoformat() if unit.created_at else None,
)
```

**COL_META 列显示：** 右侧信息栏改为显示创建时间（若当前排序依据是时间）或默认显示文件数。

**排序按钮新增「时间」选项：**

工具栏排序按钮区增加第三个按钮「时间↑」，点击按 `created_at` 排序。短按切换升降序。

> 时间排序对文件和文件夹卡片都生效（依赖第 4 节的统一排序接口）。

---

## 6. 文件修改清单

| 文件 | 改动 |
|------|------|
| `desktop/app/ui/left_panel/folder_tree.py` | `TreeNode` 加 `node_subtype`/`created_at`；`expand_unit()`/`collapse_unit()`；`find_first_visible_unit()`；`select_tree_node_by_file_id()`；`rowCount/data/index` 支持第 3 层 |
| `desktop/app/ui/right_panel/thumbnail_grid.py` | 新增 `folder_selected`/`file_selected_in_grid` 信号；`select_file_by_id()`；`FolderCardModel.set_sort()` |
| `desktop/app/ui/main_window.py` | `_apply_current_filter` 自动选首个；连接联动信号；`_on_sort` 适配文件夹卡片；`expand_unit`/`collapse_unit` 调用 |
| `desktop/tests/test_flow.py` | 添加新测试覆盖联动信号和排序 |

---

## 7. 增量实现顺序

| 步 | 内容 | 测试 |
|----|------|------|
| 1 | 搜索 Bug 修复：find_first_visible_unit + 自动选中 | 搜索后右侧不为空 |
| 2 | 文件夹卡片级联动：folder_selected 信号 + 连接 | 单击卡片→左侧高亮 |
| 3 | 树展开：TreeNode 扩展 + expand_unit/collapse_unit | 双击进入→树展开3层 |
| 4 | 文件级联动：文件节点双向选中 | 单击文件→双向高亮 |
| 5 | FolderCardModel 排序 + 排序按钮适配 | 外层视图排序生效 |
| 6 | 时间排序 + COL_META 显示 | 时间排序正确 |
