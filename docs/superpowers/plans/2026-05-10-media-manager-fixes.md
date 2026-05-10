# Media Manager 桌面端修复计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 6 个代码问题（pillow-heif 安装、视频封面、右键预览、长按速度、排序、搜索范围）+ 标记 2 个非代码 FFmpeg 警告。

**Architecture:** 改动集中在 5 个文件：main.py（pillow-heif 检测）、thumbnail_grid.py（排序修复+右键预览+搜索增强）、preview_dialog.py（长按速度模式重写）、folder_tree.py（搜索过滤）、thumbnail_generator.py（视频封面支持）。

**Tech Stack:** PySide6, SQLAlchemy, pillow-heif, OpenCV

---

## 范围说明

其中 2 个报错属于 FFmpeg/Qt 底层解码警告，非代码问题，只需记录说明：

| 报错 | 性质 | 措施 |
|------|------|------|
| `[aac ...] Could not update timestamps for skipped samples` | FFmpeg 解码器警告，来自源文件 AAC 流非标准 | 不影响功能，记录说明 |
| `[h264 ...] corrupted macroblock ... error while decoding MB` | 源文件 H.264 数据损坏 | 不影响功能，记录说明 |

## 文件修改清单

| 文件 | 修改内容 |
|------|---------|
| `desktop/main.py` | 安装后检测 pillow-heif 是否可用 |
| `desktop/app/ui/right_panel/thumbnail_grid.py` | 排序按钮仅在文件视图生效+禁用态、右键菜单加"预览"、搜索扩展 |
| `desktop/app/ui/right_panel/thumbnail_delegate.py` | 略（无修改） |
| `desktop/app/ui/dialogs/preview_dialog.py` | 重写长按逻辑为播放速度控制 |
| `desktop/app/ui/left_panel/folder_tree.py` | 新增搜索过滤方法 |
| `desktop/app/core/thumbnail_generator.py` | 支持视频文件提取首帧作缩略图 |
| `desktop/app/ui/main_window.py` | 连接搜索到树视图过滤 |

---

### Task 1: 安装 pillow-heif 并验证

**Files:**
- Modify: `desktop/requirements.txt`
- Modify: `desktop/main.py`

- [ ] **Step 1: 检查 requirements.txt 中 pillow-heif 版本**

当前文件已有 `pillow-heif>=0.15.0`，如果用户环境中未安装，直接运行安装：

```bash
cd desktop && pip install pillow-heif
```

- [ ] **Step 2: 验证 main.py 的 HEIC 检测逻辑**

当前 main.py 已有启动检测代码（`try: __import__("pillow_heif")`）和 `pillow_heif.register_heif_opener()` 调用。安装后重启即可。

验证方式：运行 Python 交互式检查：
```python
import pillow_heif
pillow_heif.register_heif_opener()
from PIL import Image
img = Image.open("test.heic")  # 不再报错
```

注意：如果安装 `pillow-heif` 后仍需额外安装 `libheif` 动态库，参考 https://github.com/homm/pillow-heif#installation。

---

### Task 2: 视频文件可以作为封面

**Files:**
- Modify: `desktop/app/core/thumbnail_generator.py`
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py`（FolderPreviewWorker）

**Root cause:** `ThumbnailGenerator.generate()` 使用 `PIL.Image.open()` 处理图片，遇到视频文件直接抛出异常。`FolderPreviewWorker` 遍历单元中所有文件，遇到第一个视频文件就失败。

- [ ] **Step 1: 修改 ThumbnailGenerator 支持视频首帧提取**

在 `desktop/app/core/thumbnail_generator.py` 的 `generate()` 方法中，当 `source_path` 是视频时，用 OpenCV 提取首帧生成缩略图：

```python
# 在 generate() 中，try PIL 之前检测文件类型
from app.utils.media_types import get_media_type
media_type = get_media_type(source_path)
if media_type == "video":
    try:
        import cv2
        from app.utils.image_helpers import VideoCapture_unicode
        cap = VideoCapture_unicode(source_path)
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                from PIL import Image as PILImage
                import numpy as np
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                pil_img = PILImage.fromarray(rgb)
                # 缩放到 max_size
                pil_img.thumbnail((self._max_size, self._max_size))
                # 保存
                cache_path = self._ensure_cache_dir(cache_dir) / f"{file_id}_thumb.jpg"
                pil_img.save(cache_path, "JPEG", quality=85)
                cap.release()
                return ThumbnailInfo(thumbnail_path=cache_path, width=pil_img.width, height=pil_img.height)
        cap.release()
    except ImportError:
        logger.warning("OpenCV 不可用，视频缩略图跳过")
    except Exception as e:
        logger.warning(f"视频缩略图生成失败: {source_path} - {e}")
```

注意 `_ensure_cache_dir` 需要改为直接使用 `cache_dir`（当前方法签名已有 `cache_dir` 参数）。需要确认 `ThumbnailInfo` 的导入已经存在。

- [ ] **Step 2: 运行测试确认**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 3: 修复右键行为（不打开文件 + 丰富菜单）

**Files:**
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py`

**Root cause:** `mouseDoubleClickEvent` 不区分左右键，右键双击也触发了 `os.startfile()` 打开文件。同时 `_on_context_menu` 限制只有 image/folder 才显示菜单，video 没有右键菜单。

**需求：**
- 右键双击 → 只弹上下文菜单，不打开文件
- 右键菜单包含：预览（image/video）、设为此文件夹封面（image）、删除（所有类型）

- [ ] **Step 1: `mouseDoubleClickEvent` 过滤右键双击**

```python
def mouseDoubleClickEvent(self, event) -> None:
    # 右键双击 → 只弹菜单，不打开文件
    if event.button() == Qt.MouseButton.RightButton:
        self._on_context_menu(event.pos())
        event.accept()
        return
    idx = self.indexAt(event.pos())
    if not idx.isValid():
        self.back_requested.emit()
        event.accept()
        return
    super().mouseDoubleClickEvent(event)
```

- [ ] **Step 2: 丰富右键菜单（预览+封面+删除）**

```python
@Slot()
def _on_context_menu(self, pos) -> None:
    from PySide6.QtWidgets import QMenu, QMessageBox
    idx = self.indexAt(pos)
    if not idx.isValid():
        return
    model = idx.model()
    media_type = model.data(idx, Qt.ItemDataRole.UserRole + 2) or ""
    file_path = model.data(idx, Qt.ItemDataRole.UserRole)
    if not file_path:
        return

    menu = QMenu(self)

    # 预览
    if media_type in ("image", "video"):
        fid = model.data(idx, Qt.ItemDataRole.UserRole + 1)
        if fid:
            act_preview = menu.addAction("预览")
            act_preview.triggered.connect(
                lambda *args, fid=fid, fp=file_path, mt=media_type:
                    self.preview_requested.emit(fid, fp, mt)
            )

    # 设置为封面（仅图片）
    if media_type == "image":
        menu.addSeparator()
        act_cover = menu.addAction("设为此文件夹封面")
        act_cover.triggered.connect(
            lambda *args, fp=file_path: self.cover_from_file_requested.emit(fp)
        )

    # 删除
    menu.addSeparator()
    act_delete = menu.addAction("删除文件")
    act_delete.setToolTip("从磁盘删除此文件（放入回收站）")
    act_delete.triggered.connect(
        lambda *args, fp=file_path: self._request_delete_file(fp)
    )

    menu.exec(self.viewport().mapToGlobal(pos))

def _request_delete_file(self, file_path: str) -> None:
    """请求删除文件（通过主窗口处理）。"""
    from PySide6.QtWidgets import QMessageBox
    parent = self.window() if self.window() else None
    reply = QMessageBox.warning(
        parent, "确认删除",
        f"确定要从磁盘删除此文件吗？\n\n{file_path}\n\n文件将被移动到回收站。",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if reply == QMessageBox.StandardButton.Yes:
        # 删除逻辑通过主窗口操作，TODO: 添加 delete_requested 信号
        pass
```

注意：删除功能需要新增信号 `delete_file_requested = Signal(str)`，并在 `main_window.py` 中连接，但目前先保留骨架，最小改动只实现右键菜单+预览。

- [ ] **Step 3: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 4: 长按左右键改为播放速度控制

**Files:**
- Modify: `desktop/app/ui/dialogs/preview_dialog.py`

**Root cause:** 当前 `_KeyHoldTimer` 长按时不断调用 `_seek_video()` 实现"跳得越来越快"。用户希望改为控制播放速度（长按左=0.5x，长按右=2x/3x）。

- [ ] **Step 1: 重写 `_KeyHoldTimer` 为速度控制模式**

将 `_KeyHoldTimer` 改为调用 `setPlaybackRate()` 而非 `_seek_video`。新增 `playback_rate` 而非 `step`：

```python
class _KeyHoldTimer:
    """按键长按检测：长按左/右控制播放速度。"""

    def __init__(self, player_getter, target_rate: float):
        """
        Args:
            player_getter: 返回 QMediaPlayer 对象的可调用对象（或 None）。
            target_rate: 长按时目标速度（2.0 = 2x, 0.5 = 半速）。
        """
        self._player_getter = player_getter
        self._target_rate = target_rate
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply)
        self._active = False
        self._original_rate = 1.0

    def start(self) -> None:
        """按下按键时启动：立即设为目标速度。"""
        player = self._player_getter()
        if player is None:
            return
        self._original_rate = player.playbackRate()
        player.setPlaybackRate(self._target_rate)
        self._active = True

    def stop(self) -> None:
        """松开按键时恢复原速度。"""
        if not self._active:
            return
        self._active = False
        self._timer.stop()
        player = self._player_getter()
        if player is not None:
            player.setPlaybackRate(self._original_rate)

    def _apply(self) -> None:
        """已改为非定时模式，速度在 start() 时立即设置。"""
        pass
```

- [ ] **Step 2: 修改 `QuickLookPreviewDialog.__init__` 中的初始化**

```python
# 长按加速
self._left_hold = _KeyHoldTimer(
    lambda: self._player,
    target_rate=0.5,  # 长按左 = 半速
)
self._right_hold = _KeyHoldTimer(
    lambda: self._player,
    target_rate=2.0,  # 长按右 = 2x
)
```

注意：需要一个 `player_getter` 回调，因为 `_player` 可能在 __init__ 之后才初始化。用 lambda 延迟获取。

- [ ] **Step 3: 修改 `keyPressEvent` 和 `keyReleaseEvent` 中的调用**

```python
elif key == Qt.Key.Key_Left:
    self._right_hold.stop()
    if not self._player and self._cap is None:
        self._navigate(-1)
    else:
        self._left_hold.start()
```

```python
elif key == Qt.Key.Key_Right:
    self._left_hold.stop()
    if not self._player and self._cap is None:
        self._navigate(1)
    else:
        self._right_hold.start()
```

- [ ] **Step 4: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 5: 修复排序按钮无反应

**Files:**
- Modify: `desktop/app/ui/main_window.py`
- Modify: `desktop/app/ui/right_panel/thumbnail_grid.py`

**Root cause 分析：** 排序按钮操作 `self._grid_model`（`ThumbnailGridModel`），但当前视图可能在显示 `FolderCardModel`（文件夹卡片模式）。此时排序不生效且按钮无视觉反馈。

- [ ] **Step 1: 修改 `_on_sort` 仅在文件视图生效**

```python
def _on_sort(self, field: str) -> None:
    """切换排序。仅在文件列表视图生效。"""
    model = self._grid_model
    if not model.sortable:
        self._status_bar.set_status("文件夹卡片视图不支持排序")
        return
    if model.sort_field == field:
        model.set_sort(field, not model._sort_asc)
    else:
        model.set_sort(field, True)
    sort_label = {"name": "名称", "size": "大小"}.get(field, field)
    arrow = "↑" if model._sort_asc else "↓"
    self._status_bar.set_status(f"排序: {sort_label}{arrow}")
    self._update_sort_buttons()
```

- [ ] **Step 2: 给 ThumbnailGridModel 添加 `sortable` 属性**

```python
@property
def sortable(self) -> bool:
    return True
```

（`FolderCardModel` 不实现该属性，但未来如果需要可扩展）

- [ ] **Step 3: 让排序按钮在不适用时进入 disabled 态**

在 `_update_sort_buttons` 中添加对当前视图模式的检测。但为了稳妥，直接让按钮始终可点击（点击时提示）。

同时检查按钮的 `setChecked` 循环触发问题——在 `_update_sort_buttons` 中：

```python
def _update_sort_buttons(self) -> None:
    """同步排序按钮的选中状态和图标。"""
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

- [ ] **Step 4: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

### Task 6: 搜索功能扩展到文件夹名

**Files:**
- Modify: `desktop/app/ui/left_panel/folder_tree.py`
- Modify: `desktop/app/ui/main_window.py`

**Root cause:** 当前搜索只过滤右侧文件网格（`_grid_model.apply_filter(search_text=...)`），不搜索左侧文件夹树和文件夹卡片名称。

- [ ] **Step 1: 给 FolderTreeModel 添加搜索过滤方法**

```python
# 在 FolderTreeModel 类中添加
def set_search_filter(self, keyword: str) -> None:
    """按关键词过滤树节点显示（隐藏不匹配的节点）。"""
    self.beginResetModel()
    try:
        with DatabaseManager.session() as session:
            # 重新加载全部数据（保留原有的 refresh 逻辑）
            self._load_from_db(session)
            # 标记匹配/不匹配
            if keyword:
                keyword = keyword.lower()
                for root in self._roots:
                    for child in root.children:
                        child._hidden = keyword not in child.name.lower()
    except Exception:
        pass
    self.endResetModel()
```

实际实现时，利用已有的 `TreeNode` 结构加 `_hidden` 属性，并在 `rowCount()` 中排除隐藏节点：

```python
# 修改 rowCount
def rowCount(self, parent=QModelIndex()) -> int:
    if not parent.isValid():
        return len(self._roots)
    node = parent.internalPointer()
    if node and node.node_type in ("root", "favorites"):
        visible = [c for c in node.children if not getattr(c, '_hidden', False)]
        return len(visible)
    return 0

# 修改 index() 也要跳过隐藏节点
def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
    if not self.hasIndex(row, column, parent):
        return QModelIndex()
    if not parent.isValid():
        if row < len(self._roots):
            return self.createIndex(row, column, self._roots[row])
    else:
        pnode = parent.internalPointer()
        if pnode and pnode.node_type in ("root", "favorites"):
            visible = [c for c in pnode.children if not getattr(c, '_hidden', False)]
            if row < len(visible):
                return self.createIndex(row, column, visible[row])
    return QModelIndex()
```

- [ ] **Step 2: 修改 main_window 的搜索处理**

```python
# 修改 _apply_current_filter
def _apply_current_filter(self) -> None:
    media_data = self._filter_combo.currentData()
    search_text = self._search_input.text().strip()

    # 搜索右侧文件网格
    self._grid_model.apply_filter(
        search_text=search_text,
        media_filter=media_data if media_data else "",
    )

    # 搜索左侧文件夹树（只针对文本搜索，不过滤类型）
    self._tree_view.model().set_search_filter(search_text)
```

- [ ] **Step 3: 运行测试**

```bash
cd desktop && python tests/test_flow.py
```

Expected: ALL PASS

---

## 非代码问题说明

以下 2 个报错来自 FFmpeg/Qt 底层，非应用代码可控，记录在 `docs/superpowers/plans/known-issues.md`（可选）：

1. `[aac ...] Could not update timestamps for skipped samples` — 源文件的 AAC 音频流时间戳非标准，FFmpeg 解码时发出警告，不影响播放
2. `[h264 ...] corrupted macroblock ... error while decoding MB 7 31` — 源文件的 H.264 视频数据存在损坏宏块，对应视频片段会出现花屏/绿块，属于文件本身损坏，非代码可修复

## 增量实现顺序建议

| 顺序 | 任务 | 复杂度 | 说明 |
|------|------|--------|------|
| 1 | Task 1: 安装 pillow-heif | 低 | 先保证基础格式支持 |
| 2 | Task 2: 视频封面 | 中 | 安装后视频缩略图才生效 |
| 3 | Task 4: 长按速度控制 | 中 | 独立改动，不影响其他 |
| 4 | Task 3: 右键预览 | 低 | 小改动，安全 |
| 5 | Task 5: 排序修复 | 低 | 小改动，安全 |
| 6 | Task 6: 搜索扩展 | 中 | 树模型改动大，放最后 |
