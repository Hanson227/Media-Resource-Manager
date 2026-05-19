# 左侧面板排序 + 布局优化设计

## 概要

三项 UI 优化：左侧树列标题可点击排序、收藏星标移到名称前、标签列收窄 + 工具栏排序按钮调整。

## 1. 左侧树列表标题排序

### 当前状态

树有四列：名称 | 大小 | 日期 | 标签。标题不可点击，单元在 `refresh()` 中按 `(is_starred, name)` 固定排序。

### 改动

- `FolderTreeView` 初始化时连接 `header().sectionClicked` → `_on_header_sort(col)`
- `FolderTreeModel` 新增 `set_sort(field, order)` 方法，对每个根目录下的子节点重新排序
- 排序字段：`name` | `total_size` | `created_at`
- 排序方向：点击同列切换 asc/desc，切换列时按 asc 开始
- 视觉反馈：标题旁边显示 ` ↑` / ` ↓`（通过在 `headerData()` 中返回带箭头的标签文本实现）
- 收藏标记不影响排序顺序（与其他条目一起排，仅展示 ⭐ 图标）

### 数据流

```
用户点击标题 → header().sectionClicked 信号 → FolderTreeView._on_header_sort()
→ model.set_sort(field, order) → 各 root 下重新排序 children → beginResetModel/endResetModel
```

### 涉及文件

- `desktop/app/ui/left_panel/folder_tree.py` — FolderTreeModel 新增 set_sort()、FolderTreeView 连接 sectionClicked

## 2. 收藏星标移到名称前

### 改动

`FolderTreeModel.data()` 中 COL_NAME 分支修改：

- 当前：`f"{node.name}{suffix}"` → suffix 为 ` ⭐`
- 改为：`f"{suffix}{node.name}"` → prefix 为 `⭐ `（只有星标，merged 标记仍放在后面）

### 涉及文件

- `desktop/app/ui/left_panel/folder_tree.py` — `data()` 方法 COL_NAME 分支

## 3. 标签列宽度收窄

### 改动

- `COL_TAGS` 默认宽度 120px → 80px
- 保持 `setStretchLastSection(True)`，收窄后大小列获得更多空间

### 涉及文件

- `desktop/app/ui/left_panel/folder_tree.py` — `resizeSection(model.COL_TAGS, 80)`

## 4. 右侧工具栏排序按钮调整

### 改动

- 移除"时间↑"按钮及其相关代码（`_sort_date_btn` 及其 `_on_sort("date")`）
- "名称↑"和"大小↑"从工具栏左侧移到消息按钮右侧
- `_update_sort_buttons()` 中移除时间按钮的更新逻辑
- `_on_sort()` 中移除 `"date"` 分支

### 工具栏布局变化

```
当前: [索引] | [搜索] [筛选] [名称↑] [大小↑] [时间↑] | [消息]
改为: [索引] | [搜索] [筛选] | [消息] [名称↑] [大小↑]
```

### 涉及文件

- `desktop/app/ui/main_window.py` — `_setup_toolbar()` 中调整按钮顺序和移除时间按钮；`_update_sort_buttons()`；`_on_sort()`

## 排除范围

- 不修改右侧网格的排序逻辑
- 不修改数据库
- 不修改左侧树的右键菜单
- 收藏筛选按钮此次不做
