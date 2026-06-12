# -*- coding: utf-8 -*-
"""
文件夹树视图 —— 显示媒体库根目录和资源单元的树形结构。

数据安全设计：
- ORM 对象只在 refresh() 的 session 内访问，立即转为普通 dict
- 树模型存储 dict 而非 SQLAlchemy ORM 对象，避免 DetachedInstanceError
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, QPointF, QAbstractItemModel, QModelIndex, Signal, Slot,
)
from PySide6.QtGui import QPainter, QColor, QPolygonF
from PySide6.QtWidgets import QTreeView, QAbstractItemView, QHeaderView, QStyle, QProxyStyle

from config import AppConfig
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.ui.left_panel.context_menu import FolderTreeContextMenu

logger = logging.getLogger(__name__)


def _get_ctime(path: str) -> str:
    """获取文件系统创建日期（YYYY-MM-DD），路径不存在时返回空串。"""
    try:
        ts = os.path.getctime(path)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except OSError:
        return ""


# ============================================================
# 纯数据节点（无 session 依赖）
# ============================================================

@dataclass
class TreeNode:
    """树节点的纯数据容器，不从 SQLAlchemy 继承。"""
    node_type: str           # "root" | "unit"
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
    node_subtype: str = ""   # "" 普通 / "file" 文件节点
    created_at: Optional[str] = None  # ISO 时间
    tags_str: str = ""                # 文件标签（逗号分隔，仅文件节点）

    def __post_init__(self):
        if self.children is None:
            self.children = []


# ============================================================
# 树模型
# ============================================================

class FolderTreeModel(QAbstractItemModel):
    """文件夹树模型 —— 存储 TreeNode 纯数据。

    树结构:
    - 第 0 层: 媒体库根目录 (TreeNode node_type="root")
    - 第 1 层: 资源单元 (TreeNode node_type="unit")
    """

    COL_NAME = 0
    COL_SIZE = 1
    COL_DATE = 2
    COL_TAGS = 3

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._roots: list[TreeNode] = []
        self._sort_field = "name"
        self._sort_asc = True

    def refresh(self) -> None:
        """从数据库重新加载所有数据，转为 TreeNode 纯数据。"""
        self.beginResetModel()
        try:
            with DatabaseManager.session() as session:
                roots = q.get_all_roots(session)
                self._roots = []

                for root in roots:
                    units = q.get_units_by_root(session, root.id)
                    root_name = Path(root.path).name or root.path
                    star_count = sum(1 for u in units if u.is_starred)
                    star_suffix = f" ⭐{star_count}" if star_count else ""
                    root_node = TreeNode(
                        node_type="root",
                        node_id=root.id,
                        name=f"{root_name}  ({len(units)} 个片段{star_suffix})",
                        path=root.path,
                    )
                    for unit in units:
                        unit_node = TreeNode(
                            node_type="unit",
                            node_id=unit.id,
                            name=unit.name,
                            path=unit.path,
                            file_count=unit.file_count or 0,
                            total_size=unit.total_size or 0,
                            is_manual=unit.is_manual or False,
                            is_starred=unit.is_starred or False,
                            status=unit.status or "active",
                            library_root_id=root.id,
                            created_at=_get_ctime(unit.path),
                        )
                        root_node.children.append(unit_node)
                    self._roots.append(root_node)
        except Exception as e:
            logger.error(f"刷新文件夹树失败: {e}")
            self._roots = []
        # 在 reset 块内排序，避免视图渲染后数据突变
        self._re_sort()
        self.endResetModel()

    def get_selected_units(self, index: QModelIndex) -> list[int]:
        """获取指定索引对应的资源单元 ID 列表。"""
        if not index.isValid():
            return []
        node = index.internalPointer()
        if node is None:
            return []
        if node.node_type == "unit" and node.node_subtype != "file":
            return [node.node_id] if node.node_id > 0 else []
        if node.node_type in ("root",):
            return [c.node_id for c in node.children if c.status == "active" and c.node_id > 0]
        return []

    def get_node_by_unit_id(self, unit_id: int) -> Optional[TreeNode]:
        """根据单元 ID 查找节点。"""
        for root in self._roots:
            for child in root.children:
                if child.node_id == unit_id:
                    return child
        return None

    def _find_unit_parent_index(self, unit_node: TreeNode) -> QModelIndex:
        """查找用于 beginInsertRows/beginRemoveRows 的父级 QModelIndex。

        对文件节点返回单元索引，对单元节点返回单元自身的索引（作为文件子节点的 parent）。
        """
        if unit_node.node_subtype == "file":
            # 文件节点 → 父节点是单元
            for r_idx, root in enumerate(self._roots):
                for c_idx, child in enumerate(root.children):
                    if child.node_type == "unit" and child.children:
                        for fn in child.children:
                            if fn.node_id == unit_node.node_id:
                                return self.createIndex(c_idx, 0, child)
            return QModelIndex()
        # 单元节点 → 返回单元自身的 QModelIndex（作为文件子节点的父节点）
        for r_idx, root in enumerate(self._roots):
            for c_idx, child in enumerate(root.children):
                if child.node_id == unit_node.node_id:
                    return self.createIndex(c_idx, 0, child)
        return QModelIndex()

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
        """内部重排（不触发 reset，由调用方保证在 reset 之外执行）。

        使用稳定排序（stable sort）保证：
        1. 收藏的单元永远排在顶部
        2. 同一收藏组内按 _sort_field 排序
        """
        for root in self._roots:
            root.children.sort(key=self._sort_key, reverse=not self._sort_asc)
            root.children.sort(key=lambda u: 0 if u.is_starred else 1)

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

    def expand_unit(self, unit_id: int, files: list) -> None:
        """加载单元下的文件作为树节点子项（使用 beginInsertRows）。"""
        node = self.get_node_by_unit_id(unit_id)
        if not node:
            return
        if node.children:
            return  # 已展开，不做重复操作
        if not files:
            return
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
                created_at=f.get("created_at", ""),
                tags_str=f.get("tags_str", ""),
            )
            for f in files[:200]
        ]
        self.beginInsertRows(parent, 0, len(new_children) - 1)
        node.children = new_children
        self.endInsertRows()

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

    # ============================================================
    # QAbstractItemModel 实现
    # ============================================================

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            if row < len(self._roots):
                return self.createIndex(row, column, self._roots[row])
        else:
            pnode = parent.internalPointer()
            if pnode:
                if pnode.node_type in ("root",) and row < len(pnode.children):                    return self.createIndex(row, column, pnode.children[row])
                if pnode.node_type == "unit" and pnode.children and row < len(pnode.children):
                    return self.createIndex(row, column, pnode.children[row])
        return QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        if not node:
            return QModelIndex()
        if node.node_subtype == "file":
            # 文件节点 → 搜索父单元（遍历所有根的子节点）
            for r_idx, root in enumerate(self._roots):
                for c_idx, child in enumerate(root.children):
                    if child.node_type == "unit" and child.children:
                        for fn in child.children:
                            if fn.node_id == node.node_id:
                                return self.createIndex(c_idx, 0, child)
            return QModelIndex()
        if node.node_type == "unit":
            for r_idx, root in enumerate(self._roots):
                if root.node_id == node.library_root_id:
                    return self.createIndex(r_idx, 0, root)
        return QModelIndex()

    def rowCount(self, parent=QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._roots)
        node = parent.internalPointer()
        if node and node.node_type in ("root",):
            return len(node.children)
        if node and node.node_type == "unit" and node.children:
            return len(node.children)
        return 0

    def columnCount(self, parent=QModelIndex()) -> int:
        return 4

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if not node:
            return None

        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == self.COL_NAME:
                prefix = ""
                suffix = ""
                if node.node_type == "unit" and node.node_subtype != "file":
                    if node.is_starred:
                        prefix = "⭐ "  # 星标作为前缀
                    if node.status == "merged":
                        suffix = " ▷"  # merged 标记保持后缀
                return f"{prefix}{node.name}{suffix}"
            elif col == self.COL_SIZE:
                from app.utils.file_helpers import format_size
                if node.node_subtype == "file":
                    return format_size(node.total_size)
                if node.node_type == "unit":
                    if node.status == "merged":
                        return ""
                    return f"{node.file_count} 个 · {format_size(node.total_size)}"
                elif node.node_type == "root":
                    active = sum(1 for c in node.children if c.status == "active")
                    return f"{active} 个" if active else ""
                return ""
            elif col == self.COL_DATE:
                if node.node_subtype == "file" and node.created_at:
                    return node.created_at[:10]
                if node.node_type == "unit" and node.created_at:
                    return node.created_at[:10]
                return ""
            elif col == self.COL_TAGS:
                if node.node_subtype == "file" and node.tags_str:
                    return node.tags_str
                return ""

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._build_tooltip_text(node)

        if role == Qt.ItemDataRole.UserRole:
            return node.node_id

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            labels = {self.COL_NAME: "名称", self.COL_SIZE: "大小",
                      self.COL_DATE: "日期", self.COL_TAGS: "标签"}
            label = labels.get(section, "")
            return label
        return None

    # ============================================================
    # 辅助
    # ============================================================

    @staticmethod
    def _build_tooltip_text(node: TreeNode) -> str:
        if node.node_type == "root":
            return f"媒体库根目录: {node.path}"
        if node.node_subtype == "file":
            from app.utils.file_helpers import format_size
            return (
                f"文件名: {node.name}\n"
                f"路径: {node.path}\n"
                f"大小: {format_size(node.total_size)}"
            )
        from app.utils.file_helpers import format_size
        lines = [
            f"名称: {node.name}",
            f"路径: {node.path}",
            f"文件数: {node.file_count}",
            f"总大小: {format_size(node.total_size)}",
        ]
        if node.is_manual:
            lines.append("手动标记")
        if node.is_starred:
            lines.append("已收藏")
        if node.status == "merged":
            lines.append("已合并到父单元")
        return "\n".join(lines)


# ============================================================
# 自定义分支指示器（绘制干净的三角形展开/收起标识）
# ============================================================

class _BranchIndicatorStyle(QProxyStyle):
    """为树节点绘制干净的三角形展开/收起指示器。

    折叠态 ▶（向右），展开态 ▼（向下）。
    使用 Catppuccin Overlay_1 柔和灰色，避免与背景冲突。
    """

    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PE_IndicatorBranch:
            has_children = bool(option.state & QStyle.StateFlag.State_Children)
            if has_children:
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                c = option.rect.center()
                is_open = bool(option.state & QStyle.StateFlag.State_Open)
                color = QColor("#7f849c")
                painter.setBrush(color)
                painter.setPen(Qt.PenStyle.NoPen)
                if is_open:
                    # ▼ 向下三角形
                    painter.drawPolygon(QPolygonF([
                        QPointF(c.x(), c.y() + 3),
                        QPointF(c.x() - 4, c.y() - 3),
                        QPointF(c.x() + 4, c.y() - 3),
                    ]))
                else:
                    # ▶ 向右三角形
                    painter.drawPolygon(QPolygonF([
                        QPointF(c.x() + 3, c.y()),
                        QPointF(c.x() - 3, c.y() - 4),
                        QPointF(c.x() - 3, c.y() + 4),
                    ]))
                painter.restore()
            return  # 不画树连接线，缩进已足够表明层级
        super().drawPrimitive(element, option, painter, widget)


class FolderTreeView(QTreeView):
    """文件夹树视图 —— 嵌入到主窗口左侧面板。

    信号:
        unit_selected: 选中资源单元 (list[int])
        merge_requested: 请求合并 (parent_id, child_ids)
        split_requested: 请求拆分 (unit_id)
        mark_requested: 请求标记 (folder_path)
        unmark_requested: 请求取消标记 (unit_id)
        exclude_requested: 请求排除单元 (unit_id)
        remove_root_requested: 请求删除根目录 (root_id)
    """

    unit_selected = Signal(list)
    unit_double_clicked = Signal(int)     # 单击/双击文件夹节点 → 进入文件夹
    merge_requested = Signal(int, list)
    split_requested = Signal(int)
    mark_requested = Signal(str)
    unmark_requested = Signal(int)
    star_requested = Signal(int)
    unstar_requested = Signal(int)
    exclude_requested = Signal(int)
    cover_requested = Signal(int)
    clear_cover_requested = Signal(int)
    delete_requested = Signal(int)        # 删除资源单元
    remove_root_requested = Signal(int)
    rename_requested = Signal(int)        # F2: 重命名单元
    copy_path_requested = Signal(str)     # Ctrl+C: 复制路径
    heic_convert_requested = Signal(str)  # 目录下 HEIC 批量转 JPG (folder_path)
    file_selected_from_tree = Signal(int) # file_id — 树中单击文件时发射

    def __init__(self, model: FolderTreeModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self._syncing_file: bool = False  # 防同步循环
        self.setModel(model)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(model.COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(model.COL_SIZE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(model.COL_DATE, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(model.COL_TAGS, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        # 默认列宽
        header.resizeSection(model.COL_NAME, 200)
        header.resizeSection(model.COL_SIZE, 150)
        header.resizeSection(model.COL_DATE, 100)
        header.resizeSection(model.COL_TAGS, 80)
        header.setSectionsMovable(True)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.setAnimated(True)
        self.setExpandsOnDoubleClick(False)
        self.setIndentation(20)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # 自定义三角形展开/收起指示器
        self._branch_style = _BranchIndicatorStyle()
        self.setStyle(self._branch_style)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.expandAll()
        self.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.doubleClicked.connect(self._on_double_clicked)
        self.header().sectionClicked.connect(self._on_header_sort)
        logger.info("文件夹树视图初始化完成")

    def refresh_model(self) -> None:
        """刷新模型并从数据库重新加载。"""
        self._model.refresh()
        self.expandAll()

    def filter_by_name(self, keyword: str) -> None:
        """按名称过滤树节点（隐藏不匹配的单元）。"""
        model = self._model
        keyword = keyword.strip().lower()
        self.expandAll()
        for root_row, root in enumerate(model._roots):
            root_idx = model.index(root_row, 0)
            any_visible = False
            for child_row, child in enumerate(root.children):
                child_idx = model.index(child_row, 0, root_idx)
                if keyword and keyword not in child.name.lower():
                    self.setRowHidden(child_row, root_idx, True)
                else:
                    self.setRowHidden(child_row, root_idx, False)
                    any_visible = True
            # 隐藏空的根节点
            self.setRowHidden(root_row, QModelIndex(), bool(keyword) and not any_visible)

    def find_first_visible_unit(self) -> Optional[int]:
        """返回第一个可见的单元 node_id，没有则返回 None。"""
        model = self._model
        for root_row, root in enumerate(model._roots):
            root_idx = model.index(root_row, 0)
            for child_row, child in enumerate(root.children):
                if not self.isRowHidden(child_row, root_idx):
                    return child.node_id
        return None

    def select_unit(self, unit_id: int) -> None:
        """选中指定单元 ID 对应的树节点（用于右侧面板联动）。"""
        model = self._model
        for root_row, root in enumerate(model._roots):
            for child_row, child in enumerate(root.children):
                if child.node_id == unit_id:
                    root_idx = model.index(root_row, 0)
                    child_idx = model.index(child_row, 0, root_idx)
                    self.setCurrentIndex(child_idx)
                    return

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
                        self.expand(root_idx)  # 确保父根节点展开
                        child_idx = model.index(child_row, 0, root_idx)
                        self.setCurrentIndex(child_idx)
                        self.scrollTo(child_idx, QAbstractItemView.ScrollHint.PositionAtCenter)
                        return
        finally:
            if sel:
                sel.blockSignals(False)

    def selected_unit_ids(self) -> list[int]:
        """获取当前选中项对应的所有资源单元 ID（不触发信号）。"""
        unit_ids: set[int] = set()
        for idx in self.selectedIndexes():
            if idx.column() != 0:
                continue
            ids = self._model.get_selected_units(idx)
            unit_ids.update(ids)
        return list(unit_ids)

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
        order = Qt.SortOrder.AscendingOrder if asc else Qt.SortOrder.DescendingOrder
        self.header().setSortIndicator(col, order)
        # reset 会折叠所有节点，排序后重新展开
        self.expandAll()

    @Slot(QModelIndex)
    def _on_double_clicked(self, index: QModelIndex) -> None:
        """双击单元节点 → Qt 默认处理展开/折叠，不重复发射 unit_double_clicked。
        _on_selection_changed 已经在单击时处理过进入文件夹的逻辑。"""
        pass

    @Slot()
    def _on_selection_changed(self) -> None:
        """树选择变化：按节点类型分发不同信号。

        - 文件节点 → file_selected_from_tree(file_id)
        - 文件夹节点 → unit_double_clicked（单击进入文件夹加载文件列表）
        - 根/收藏节点 → unit_selected(list[unit_ids])
        """
        if self._syncing_file:
            return
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
            # 文件夹节点 → 直接进入文件夹（加载文件列表）
            self.unit_double_clicked.emit(unit_ids[0])
        elif node.node_type == "root":            # 根节点 → 发射 unit_selected（显示文件夹卡片）
            self.unit_selected.emit(list(unit_ids))

    def select_tree_node_by_file_id(self, file_id: int) -> bool:
        """选中指定文件 ID 对应的树节点。只展开目标路径，不 expandAll。
        返回 True 表示找到并选中，False 表示未找到。"""
        if self._syncing_file:
            return False
        self._syncing_file = True
        try:
            model = self._model
            for root_row, root in enumerate(model._roots):
                root_idx = model.index(root_row, 0)
                if not root_idx.isValid():
                    continue
                for child_row, child in enumerate(root.children):
                    if child.node_type == "unit" and child.children:
                        for f_row, f_node in enumerate(child.children):
                            if f_node.node_id == file_id:
                                self.setAnimated(False)
                                self.expand(root_idx)
                                unit_idx = model.index(child_row, 0, root_idx)
                                if unit_idx.isValid():
                                    self.expand(unit_idx)
                                    file_idx = model.index(f_row, 0, unit_idx)
                                    if file_idx.isValid():
                                        self.setCurrentIndex(file_idx)
                                        self.scrollTo(file_idx)
                                self.setAnimated(True)
                                self.viewport().update()
                                return True
            return False
        finally:
            self._syncing_file = False

    @Slot()
    def _on_context_menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return

        node = index.internalPointer()
        if node is None:
            return

        unit_ids = self._model.get_selected_units(index)
        if not unit_ids and node.node_type != "root":
            return

        # 传递节点类型信息，使菜单能区分根目录与资源单元
        root_id = node.node_id if node.node_type == "root" else None

        menu = FolderTreeContextMenu(self, unit_ids, self._model, root_id=root_id)
        menu.merge_requested.connect(self.merge_requested.emit)
        menu.split_requested.connect(self.split_requested.emit)
        menu.mark_requested.connect(self.mark_requested.emit)
        menu.unmark_requested.connect(self.unmark_requested.emit)
        menu.star_requested.connect(self.star_requested.emit)
        menu.unstar_requested.connect(self.unstar_requested.emit)
        menu.exclude_requested.connect(self.exclude_requested.emit)
        menu.cover_requested.connect(self.cover_requested.emit)
        menu.clear_cover_requested.connect(self.clear_cover_requested.emit)
        menu.delete_requested.connect(self.delete_requested.emit)
        menu.heic_convert_requested.connect(self.heic_convert_requested.emit)
        menu.remove_root_requested.connect(self.remove_root_requested.emit)
        menu.refresh_requested.connect(self.refresh_model)
        menu.exec(self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event) -> None:
        """处理键盘快捷键：F2 重命名，Ctrl+C 复制路径。"""
        if event.key() == Qt.Key.Key_F2:
            ids = self.selected_unit_ids()
            if ids:
                self.rename_requested.emit(ids[0])
            return
        if event.key() == Qt.Key.Key_C and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            ids = self.selected_unit_ids()
            if ids:
                node = self._model.get_node_by_unit_id(ids[0])
                if node and node.path:
                    self.copy_path_requested.emit(node.path)
            return
        super().keyPressEvent(event)
