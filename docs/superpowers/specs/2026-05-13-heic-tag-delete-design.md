# HEIC 批量转换 + 标签管理 + 文件删除设计文档

## 1. HEIC 批量转换对话框

### 需求

导入文件夹 → 自动递归扫描所有 HEIC/HEIF 文件 → 文件列表展示 → 批量转 JPG → 自动删除源 HEIC。

### 架构

```
main_window.py: 工具栏 HEIC 按钮 / 菜单
  └─→ HeicBatchConvertDialog (新文件)
        ├─ 选择文件夹 (QFileDialog.getExistingDirectory)
        ├─ 递归扫描HEIC (扫描器独立, 复用 is_heic_file 文件头检测)
        ├─ 文件列表 (QTableWidget)
        │   列: 文件名 | 大小 | 状态 (pending/转换中/成功/失败/跳过)
        ├─ 批量转换 (复用 convert_batch)
        └─ 进度条 + 统计摘要
```

### 详细设计

**对话框布局:**

```
┌──────────────────────────────────────────────────────────┐
│  HEIC 批量转 JPG                               [×] 关闭  │
├──────────────────────────────────────────────────────────┤
│  文件夹: [___________当前路径___________] [浏览...]       │
│                                                          │
│  找到 42 个 HEIC 文件                                     │
│                                                          │
│  ┌─ 文件列表 ────────────────────────────────────────────┐│
│  │ ☐  IMG_001.HEIC    3.2 MB   ✓ 已完成                ││
│  │ ☐  IMG_002.HEIC    2.8 MB   ⟳ 转换中...             ││
│  │ ☐  IMG_003.HEIF    4.1 MB   ⏳ 等待                  ││
│  │ ☐  IMG_004.HEIC    1.5 MB   ✗ 文件头不是 HEIC       ││
│  └───────────────────────────────────────────────────────┘│
│                                                          │
│  总进度: ████████████████░░░░░░░  12/42                   │
│                                                          │
│  统计: 总计 42 | 成功 10 | 失败 1 | 跳过 0               │
│                                                          │
│              [开始转换]              [关闭]               │
└──────────────────────────────────────────────────────────┘
```

**选择文件夹流程:**
1. 点"浏览..." → `QFileDialog.getExistingDirectory()`
2. 递归扫描目录下所有文件，调用 `is_heic_file()` 基于文件头检测
3. 填充到 QTableWidget
4. 显示 "找到 N 个 HEIC 文件"

**转换流程:**
1. 点"开始转换" → 禁用按钮防重复点击
2. 对每个文件调用 `convert_single(source)` (转换 + 自动删源文件已在 core 中实现)
3. 每完成一个更新表格状态行 + 进度条
4. 完成后显示统计摘要，启用"关闭"按钮

**文件列表设计:**
- 第一列: checkbox (默认全选，可取消勾选跳过某个文件)
- 第二列: 文件名 (含扩展名)
- 第三列: 文件大小 (自动格式化 KB/MB)
- 第四列: 状态 (带颜色: ⏳等待/⟳转换中/✓成功/✗失败/−跳过)

### 修改文件

| 文件 | 操作 |
|------|------|
| `desktop/app/ui/dialogs/heic_batch_convert.py` | **新增** — 批量转换对话框 |
| `desktop/app/ui/main_window.py` | 修改 — HEIC 按钮/菜单指向新对话框 (保留右键单文件转换入口) |

### 不变

- `desktop/app/core/heic_converter.py` — Core logic unchanged (already handles conversion + source deletion)
- HEIC 右键菜单 → 单文件转换 → 保持现有逻辑

---

## 2. 标签管理

### 需求

- 管理标签: 工具菜单 → "管理标签" → 弹出对话框，新建/编辑/删除标签 (名称 + 颜色)
- 分配标签: 右键文件 → "分配标签" → 子菜单勾选标签
- TagFilterBar 已有，有标签时自动显示

### 架构

```
main_window.py:
  工具 → 管理标签  ──→ TagManageDialog (新文件)

thumbnail_grid.py:
  右键 → 分配标签  ──→ QMenu (动态构建标签子菜单)
```

### 2.1 TagManageDialog

**对话框布局:**

```
┌──────────────────────────────────────────────┐
│  管理标签                                     │
├──────────────────────────────────────────────┤
│  ┌─ 标签列表 ───────────────────────────────┐│
│  │  ● 重要 (#E74C3C)           [编辑] [删除] ││
│  │  ● 待整理 (#F39C12)         [编辑] [删除] ││
│  │  ● 已备份 (#2ECC71)         [编辑] [删除] ││
│  └───────────────────────────────────────────┘│
│                                              │
│  ┌─ 新建/编辑 ──────────────────────────────┐│
│  │  名称: [___________]                     ││
│  │  颜色: [■ 选择颜色...]                   ││
│  │         [保存]  [取消]                   ││
│  └───────────────────────────────────────────┘│
│                                              │
│              [+ 新建标签]                     │
└──────────────────────────────────────────────┘
```

**功能:**
- 左侧列表展示所有标签 (名称 + 颜色指示器)
- 选中一个标签 → 右侧显示编辑表单
- "新建标签" → 清空表单 → 输入名称 + 选择颜色 → 保存
- "删除" → 确认后删除标签及所有映射记录
- 颜色选择: `QColorDialog.getColor()` 或预置颜色面板

**数据库调用:**
- `create_tag(session, name, color)` — 新建
- `get_all_tags(session)` — 列表
- `delete_tag(session, tag_id)` — 删除

### 2.2 右键分配标签

**右键菜单改动:**

```
预览
设为此文件夹封面
──────────
HEIC 转换
──────────
分配标签 ▶
  ☐ 重要
  ☑ 待整理
  ☐ 已备份
──────────
删除文件
```

**实现:**
1. 在 `thumbnail_grid.py` 的 `_on_context_menu()` 中加一个子 QMenu
2. 动态查询所有标签，每个标签加一个 `QAction(checkable=True)`
3. 勾选/取消时调用 `set_file_tags(session, file_id, tag_ids[])`
4. 初始勾选状态由 `get_file_tags(session, file_id)` 决定

### 修改文件

| 文件 | 操作 |
|------|------|
| `desktop/app/ui/dialogs/tag_manager.py` | **新增** — 标签管理对话框 |
| `desktop/app/ui/right_panel/thumbnail_grid.py` | 修改 — 右键菜单加"分配标签"子菜单 |
| `desktop/app/ui/main_window.py` | 修改 — 工具菜单加"管理标签"入口 |

### 不变

- `TagFilterBar` — 已有，不需要改动
- 数据库 models/queries — 已有完整 CRUD，不需要改动

---

## 3. 文件/文件夹删除

### 需求

- 右键文件 → 删除 → 移至回收站 + 删除 DB 记录
- 右键文件夹 (资源单元) → 删除 → 整个目录移至回收站 + 级联删除子文件/单元

### 实现方案

使用 `send2trash` 库实现跨平台回收站功能。若不可用则回退到 Windows Shell API (`ctypes` 调用 `SHFileOperationW`)。

**文件删除流程:**

```
右键 → 删除 → 确认对话框 (QMessageBox.question)
  → 回收站: send2trash.send2trash(file_path)
  → DB: delete_media_file(session, file_id)
  → 刷新网格
```

**文件夹删除流程:**

```
右键 → 删除文件夹 → 确认对话框 (显示文件数警告)
  → 回收站: send2trash.send2trash(folder_path)
  → DB: 级联删除 (resource_units ON DELETE CASCADE 会自动删关联 media_files)
  → 刷新树
```

### 安全设计

- 必须确认: `QMessageBox.question(..., "确定要删除?")`
- 文件夹删除时显示 "该文件夹包含 N 个文件"
- 删除失败时显示错误消息 (权限不足/文件被占用)
- 非空文件夹必须整体移到回收站，不能只删数据库

### 库依赖

`send2trash` (跨平台回收站) — 添加到 `requirements.txt`

### 修改文件

| 文件 | 操作 |
|------|------|
| `desktop/app/ui/right_panel/thumbnail_grid.py` | 修改 — 右键菜单加"删除文件" |
| `desktop/app/ui/left_panel/context_menu.py` | 修改 — 加"删除文件夹" |
| `desktop/app/ui/left_panel/folder_tree.py` | 修改 — 处理删除信号 |
| `desktop/app/ui/dialogs/batch_operations.py` | 可能需要修改 — 添加删除信号处理 |
| `desktop/requirements.txt` | 修改 — 添加 `send2trash` |

---

## 4. 修改文件完整清单

| 文件 | 操作类型 | 目的 |
|------|---------|------|
| `desktop/app/ui/dialogs/heic_batch_convert.py` | **新增** | HEIC 批量转换对话框 |
| `desktop/app/ui/dialogs/tag_manager.py` | **新增** | 标签管理对话框 |
| `desktop/app/ui/main_window.py` | **修改** | HEIC 按钮指向新对话框 + 管理标签菜单 |
| `desktop/app/ui/right_panel/thumbnail_grid.py` | **修改** | 右键加删除 + 分配标签 |
| `desktop/app/ui/left_panel/context_menu.py` | **修改** | 加删除文件夹 |
| `desktop/app/ui/left_panel/folder_tree.py` | **修改** | 处理删除文件夹信号 |
| `desktop/requirements.txt` | **修改** | 添加 send2trash |

## 5. 不变的文件

- `desktop/app/core/heic_converter.py` — 核心转换逻辑完整且正确，不动
- `desktop/app/db/models.py` — 标签/文件模型已有
- `desktop/app/db/queries.py` — 标签 CRUD + delete_media_file 已有
- `desktop/app/ui/widgets/tag_filter.py` — TagFilterBar 已有
- `desktop/app/api/routes/tags.py` — API 路由不变
