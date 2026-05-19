# 左侧面板排序 + 布局优化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现左侧树列标题排序、收藏星标前移、标签列收窄、工具栏按钮重排

**Architecture:** 所有修改集中在 `folder_tree.py`（树模型新增排序方法 + 视图连接信号）和 `main_window.py`（工具栏按钮调整）。不修改 DB、不修改右侧网格逻辑。

**Tech Stack:** PySide6 (QTreeView, QAbstractItemModel, QHeaderView)

---

## File Structure

- Modify: `desktop/app/ui/left_panel/folder_tree.py` — 4 处改动
- Modify: `desktop/app/ui/main_window.py` — 1 处改动

---

### Task 1: 收藏星标移到名称前

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py:275-283`

**改动:** `FolderTreeModel.data()` 的 COL_NAME 分支中，将星标后缀改为前缀。

- [ ] **Step 1: 修改后缀为前缀**

```python
# data() 方法 COL_NAME 分支，修改前：
if col == self.COL_NAME:
    suffix = ""
    if node.node_type == "unit" and node.node_subtype != "file":
        if node.is_starred:
            suffix = " ⭐"
        elif node.status == "merged":
            suffix = " ▷"
    return f"{node.name}{suffix}"

# 修改后：
if col == self.COL_NAME:
    prefix = ""
    suffix = ""
    if node.node_type == "unit" and node.node_subtype != "file":
        if node.is_starred:
            prefix = "⭐ "  # 星标作为前缀
        if node.status == "merged":
            suffix = " ▷"  # merged 标记保持后缀
    return f"{prefix}{node.name}{suffix}"
```

- [ ] **Step 2: 运行测试确认通过**

```bash
cd desktop && python tests/test_flow.py
```
Expected: 全部通过

---

### Task 2: 标签列宽度收窄

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py:446`

- [ ] **Step 1: 修改列宽**

```python
# 修改前：
header.resizeSection(model.COL_TAGS, 120)

# 修改后：
header.resizeSection(model.COL_TAGS, 80)
```

- [ ] **Step 2: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```
Expected: 全部通过

---

### Task 3: 左侧树列标题排序

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py`

**改动:** FolderTreeModel 新增 `_sort_field`/`_sort_asc` 字段和 `set_sort()`/`_re_sort()`/`_sort_key()` 方法。`headerData()` 显示排序箭头。FolderTreeView 连接 `sectionClicked` 信号。

设计要点：
- `set_sort(field, asc)` 对外接口：保存字段 → `beginResetModel` → `_re_sort()` → `endResetModel`
- `_re_sort()` 内部分组重排：不调 reset，供 `refresh()` 结束后调用（refresh 已在 reset 块中）
- `_sort_key(node)` 根据 `_sort_field` 返回比较键
- 收藏标记置顶的逻辑保留（优先级高于排序字段）

- [ ] **Step 1: FolderTreeModel 添加排序字段和方法**

在 `__init__()` 末尾添加排序状态字段：

```python
# __init__() 末尾添加
self._sort_field = "name"
self._sort_asc = True
```

在 `expand_unit()` 之前新增三个方法：

```python
def _sort_key(self, node: TreeNode):
    """返回排序用的比较键。"""
    if self._sort_field == "name":
        return (node.name or "").lower()
    elif self._sort_field == "size":
        return node.total_size if node.node_type == "unit" else 0
    elif self._sort_field == "date":
        return node.created_at or ""
    return ""

def _re_sort(self) -> None:
    """内部重排（不触发 reset，由调用方保证在 reset 之外执行）。"""
    for root in self._roots:
        root.children.sort(key=lambda u: (
            0 if u.is_starred else 1,
            self._sort_key(u),
        ), reverse=not self._sort_asc)

def set_sort(self, field: str, asc: bool = True) -> None:
    """设置排序字段和方向并重排（触发 reset）。"""
    if field not in ("name", "size", "date"):
        return
    self._sort_field = field
    self._sort_asc = asc
    self.beginResetModel()
    try:
        self._re_sort()
    except Exception as e:
        logger.error(f"树排序失败: {e}")
    self.endResetModel()
```

- [ ] **Step 2: refresh() 末尾调用 `_re_sort()`**

`FolderTreeModel.refresh()` 末尾（`endResetModel` 之后）加一行：

```python
def refresh(self) -> None:
    self.beginResetModel()
    try:
        # ... 原有加载逻辑不变 ...
    except Exception as e:
        logger.error(f"刷新文件夹树失败: {e}")
        self._roots = []
    self.endResetModel()
    self._re_sort()  # 新数据按当前排序条件重排
```

- [ ] **Step 3: headerData() 显示排序箭头**

```python
def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
    if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
        labels = {self.COL_NAME: "名称", self.COL_SIZE: "大小",
                  self.COL_DATE: "日期", self.COL_TAGS: "标签"}
        label = labels.get(section, "")
        col_map = {self.COL_NAME: "name", self.COL_SIZE: "size", self.COL_DATE: "date"}
        if section in col_map and col_map[section] == self._sort_field:
            label += " ↑" if self._sort_asc else " ↓"
        return label
    return None
```

- [ ] **Step 4: FolderTreeView 连接 sectionClicked 信号 + handler**

`FolderTreeView.__init__()` 末尾添加：

```python
self.header().sectionClicked.connect(self._on_header_sort)
```

新增 handler 方法（放在 `_on_double_clicked` 之前）：

```python
@Slot(int)
def _on_header_sort(self, col: int) -> None:
    """点击列标题切换排序。"""
    col_map = {self._model.COL_NAME: "name", self._model.COL_SIZE: "size", self._model.COL_DATE: "date"}
    field = col_map.get(col)
    if field is None:
        return  # 标签列不可排序
    asc = True
    if field == self._model._sort_field:
        asc = not self._model._sort_asc
    self._model.set_sort(field, asc)
```

- [ ] **Step 5: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```
Expected: 全部通过

---

### Task 4: 右侧工具栏排序按钮调整

**Files:**
- Modify: `desktop/app/ui/main_window.py`

**改动:** 移除"时间↑"按钮，名称/大小按钮移到消息按钮右侧。

- [ ] **Step 1: 移除时间按钮 + 重排顺序**

在 `_setup_toolbar()` 中：
1. 删除 `_sort_date_btn` 相关的 5 行（创建、setCheckable、setToolTip、connect、addWidget）
2. 将 `_sort_name_btn` 和 `_sort_size_btn` 的定义从原位置删除，移到 `_msg_btn` 之后

修改后的顺序片段：

```python
toolbar.addSeparator()

self._msg_btn = QPushButton("消息")
self._msg_btn.setToolTip("打开消息中心 (Ctrl+M)")
self._msg_btn.clicked.connect(self._on_open_messages)
toolbar.addWidget(self._msg_btn)

toolbar.addSeparator()

self._sort_name_btn = QPushButton("名称↑")
self._sort_name_btn.setCheckable(True)
self._sort_name_btn.setToolTip("按文件名排序")
self._sort_name_btn.clicked.connect(lambda: self._on_sort("name"))
toolbar.addWidget(self._sort_name_btn)

self._sort_size_btn = QPushButton("大小↑")
self._sort_size_btn.setCheckable(True)
self._sort_size_btn.setToolTip("按文件大小排序")
self._sort_size_btn.clicked.connect(lambda: self._on_sort("size"))
toolbar.addWidget(self._sort_size_btn)
```

- [ ] **Step 2: 更新 `_update_sort_buttons()`**

移除所有 `_sort_date_btn` 引用，只更新名称和大小两个按钮：

```python
def _update_sort_buttons(self) -> None:
    """同步排序按钮的选中状态和图标。"""
    current = self._grid_view.model()
    if isinstance(current, FolderCardModel):
        field = self._last_sort_field
        asc = self._last_sort_asc
    else:
        field = self._grid_model.sort_field
        asc = self._grid_model._sort_asc
    self._sort_name_btn.blockSignals(True)
    self._sort_size_btn.blockSignals(True)
    self._sort_name_btn.setChecked(field == "name")
    self._sort_size_btn.setChecked(field == "size")
    self._sort_name_btn.blockSignals(False)
    self._sort_size_btn.blockSignals(False)
    arrow = "↑" if asc else "↓"
    self._sort_name_btn.setText(f"名称{arrow if field == 'name' else '↑'}")
    self._sort_size_btn.setText(f"大小{arrow if field == 'size' else '↑'}")
```

- [ ] **Step 3: 运行测试验证**

```bash
cd desktop && python tests/test_flow.py
```
Expected: 全部通过
