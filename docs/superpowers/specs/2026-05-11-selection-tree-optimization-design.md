# 选中高亮与文件树展开/收起优化设计

## 背景

桌面端右侧缩略图视图与左侧文件夹树之间存在选中+高亮联动、树自动展开/收起两套行为，当前实现存在 6 处严重 bug 和结构性缺陷（链式反应、beginResetModel 滥用、文件级联动静默失效等）。

## 全局交互原则

1. **单击 = 选中+高亮**，不产生文件打开行为。是否触发导航取决于单击位置（见下表）。
2. **双击 = 执行动作**：对文件夹进入（加载文件），对文件用系统默认程序打开。
3. **高亮状态全局唯一**：同一时刻只有一个项目处于选中高亮状态，左右两侧严格同步。
4. **手风琴展开规则**：进入某个演员文件夹时，同一媒体库下其他已展开文件列表的演员文件夹自动收起。

## 交互规范

### 左侧树单击

| 单击对象 | 树效果 | 右侧效果 |
|---------|--------|---------|
| 媒体库节点 | 展开到演员文件夹层，不展开文件 | 显示该媒体库的文件夹卡片。自动选中第一个文件夹卡片并滚动到可视位置 |
| 演员文件夹节点 | 标记为高亮 | 右侧切换到文件夹卡片模式，自动滚动到该文件夹的卡片并高亮。不加载文件列表 |
| 文件节点 | 展开该文件所在文件夹（如未展开），高亮该文件 | 滚动到该文件缩略图并高亮 |

### 右侧网格单击

| 单击对象 | 网格效果 | 左侧树效果 |
|---------|---------|-----------|
| 文件夹卡片 | 高亮该卡片 | 滚动到该文件夹节点并高亮，不展开文件层 |
| 文件缩略图 | 高亮该缩略图 | 展开该文件所在文件夹（如未展开），高亮文件条目，滚动到可视 |

### 双击

| 双击对象 | 效果 |
|---------|------|
| 文件夹卡片 / 树中文件夹名 | 进入文件夹：树展开该文件夹到文件层，右侧加载文件列表，自动选中第一个文件并同步高亮。同一媒体库下其他已展开文件的文件夹自动收起 |
| 文件缩略图 / 树中文件名 | 系统默认程序打开 |

### 搜索

搜索输入时自动过滤，命中第一个可见单元后：右侧显示文件夹卡片，左侧高亮对应树节点。

### 面包屑返回

返回文件夹卡片模式，收起当前文件夹的文件层，树恢复到演员文件夹层。

### 树展开/收起（手风琴）

- 单击媒体库节点 → 该媒体库展开到演员文件夹层；上一个媒体库的展开状态全部收起
- 双击进入演员文件夹 → 该文件夹的文件节点展开；同一根下其他有文件子节点的演员文件夹全部自动收起
- 用户手动点击树箭头展开/收起不被程序覆盖

## 架构变更

### 树模型：用 beginInsertRows 替代 beginResetModel

当前问题：`expand_unit()` 和 `collapse_unit()` 使用 `beginResetModel() / endResetModel()`，导致全树状态丢失。

变更：

```python
def expand_unit(self, unit_id, files):
    node = self.get_node_by_unit_id(unit_id)
    if not node:
        return
    if node.children:  # 已展开
        return
    # 获取父索引
    parent_index = self._find_parent_index(node)
    self.beginInsertRows(parent_index, 0, len(files) - 1)
    node.children = [TreeNode(...) for f in files]
    self.endInsertRows()

def collapse_unit(self, unit_id):
    node = self.get_node_by_unit_id(unit_id)
    if not node or not node.children:
        return
    parent_index = self._find_parent_index(node)
    self.beginRemoveRows(parent_index, 0, len(node.children) - 1)
    node.children = []
    self.endRemoveRows()
```

### 消除链式反应

引入 `select_unit_silent()` 方法在树端，仅调用 `setCurrentIndex` 但不触发 `unit_selected` 信号：

```python
def select_unit_silent(self, unit_id):
    """
    高亮树节点但不发射 unit_selected 信号。
    用于来自右侧卡片单击的联动（仅需高亮同步，不需加载文件）。
    """
    ...
    self.blockSignals(True)
    self.setCurrentIndex(child_idx)
    self.blockSignals(False)
```

对应地，`MainWindow._connect_signals` 中 `folder_selected` 连接改为 `select_unit_silent` 而不是 `select_unit`。

### 树中单击文件夹：不展开文件层

移除 `FolderTreeView` 中 `setExpandsOnDoubleClick(True)` 的影响——单击左侧文件夹节点时，右侧切换到文件夹卡片模式并高亮该卡片，树的文件层不自动展开。

### 进入文件夹后自动选中第一个文件

`_on_unit_double_clicked` 和 `_on_unit_selected`（当来自双击导航时）加载文件后，调用 `grid_view.select_file_by_id(first_file_id)` 选中第一个文件，并同步高亮树中的文件节点。

### 手风琴机制

在 `MainWindow` 新增状态跟踪当前展开的文件夹单元 ID。双击进入新文件夹时：

1. 查出上一个展开文件的文件夹
2. 如果其与当前文件夹同根且不同 ID → `model.collapse_unit(prev_unit_id)`
3. 构造当前文件夹的文件子节点 → `model.expand_unit(current_unit_id)`
4. 更新跟踪状态

## 涉及的组件

| 组件 | 变更类型 |
|------|---------|
| `folder_tree.py` - `FolderTreeModel` | 修改：`expand_unit/collapse_unit` 从 reset → insert/remove |
| `folder_tree.py` - `FolderTreeView` | 新增：`select_unit_silent`，修改信号触发逻辑 |
| `main_window.py` - `MainWindow` | 新增手风琴状态跟踪，修改信号连线，新增双击导航后选中第一个文件 |
| `thumbnail_grid.py` - `ThumbnailGridView` | 新增选中文件夹卡片后的滚动逻辑 |
| `test_flow.py` | 增补所有新行为的测试 |

## 测试用例

| 标签 | 场景 | 通过条件 |
|------|------|---------|
| T11 | 树中单击文件夹 → 右侧显示文件夹卡片 | 右侧保持卡片模式，卡片滚动到目标文件夹 |
| T12 | 树中单击文件夹 → 左侧不高亮文件层 | 树文件子节点不自动展开 |
| T13 | 卡片双击 → 进入文件夹并选中第一个文件 | 右侧为文件列表，第一个文件高亮，树对应文件节点高亮 |
| T14 | 右侧单击文件夹卡片 → 左侧树联动不高亮文件 | 树高亮该文件夹节点，不展开文件 |
| T15 | 手风琴：进入文件夹 B 时 A 自动收起 | A 的文件子节点消失，B 的文件子节点出现 |
| T16 | 手风琴：切换媒体库时前一个收起 | 旧根的展开全部折叠，新根展开 |
| T17 | 树中文件单击 → 右侧高亮 | 右侧滚动到目标文件 |
| T18 | 右侧文件单击 → 树展开并高亮 | 树展开该文件所在文件夹，高亮该文件 |
| T19 | expand_unit 用 insertRows 不破坏其他展开状态 | 其他单元的展开状态保持 |
