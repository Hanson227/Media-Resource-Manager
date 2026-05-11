# -*- coding: utf-8 -*-
"""
文件夹树视图 —— 显示媒体库根目录和资源单元的树形结构。

数据安全设计：
- ORM 对象只在 refresh() 的 session 内访问，立即转为普通 dict
- 树模型存储 dict 而非 SQLAlchemy ORM 对象，避免 DetachedInstanceError
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, QAbstractItemModel, QModelIndex, Signal, Slot,
)
from PySide6.QtWidgets import QTreeView, QAbstractItemView, QHeaderView

from config import AppConfig
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.ui.left_panel.context_menu import FolderTreeContextMenu

logger = logging.getLogger(__name__)


# ============================================================
# 纯数据节点（无 session 依赖）
# ============================================================

@dataclass
class TreeNode:
    """树节点的纯数据容器，不从 SQLAlchemy 继承。"""
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
    node_subtype: str = ""   # "" 普通 / "file" 文件节点
    created_at: Optional[str] = None  # ISO 时间

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
    COL_META = 1

    def __init__(self, config: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._roots: list[TreeNode] = []

    def refresh(self) -> None:
        """从数据库重新加载所有数据，转为 TreeNode 纯数据。"""
        self.beginResetModel()
        try:
            with DatabaseManager.session() as session:
                roots = q.get_all_roots(session)
                starred_units = q.get_starred_units(session)
                self._roots = []

                # 收藏虚拟根节点（仅在有收藏时显示）
                if starred_units:
                    fav_node = TreeNode(
                        node_type="favorites",
                        node_id=-1,
                        name=f"★ 收藏  ({len(starred_units)} 个片段)",
                        path="",
                    )
                    for unit in starred_units:
                        fav_node.children.append(TreeNode(
                            node_type="unit",
                            node_id=unit.id,
                            name=unit.name,
                            path=unit.path,
                            file_count=unit.file_count or 0,
                            total_size=unit.total_size or 0,
                            is_manual=unit.is_manual or False,
                            is_starred=True,
                            status=unit.status or "active",
                            library_root_id=-1,
                            created_at=unit.created_at.isoformat() if unit.created_at else None,
                        ))
                    self._roots.append(fav_node)

                for root in roots:
                    units = q.get_units_by_root(session, root.id)
                    root_name = Path(root.path).name or root.path
                    root_node = TreeNode(
                        node_type="root",
                        node_id=root.id,
                        name=f"{root_name}  ({len(units)} 个片段)",
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
                            created_at=unit.created_at.isoformat() if unit.created_at else None,
                        )
                        root_node.children.append(unit_node)
                    self._roots.append(root_node)
        except Exception as e:
            logger.error(f"刷新文件夹树失败: {e}")
            self._roots = []
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
        if node.node_type in ("root", "favorites"):
            return [c.node_id for c in node.children if c.status == "active" and c.node_id > 0]
        return []

    def get_node_by_unit_id(self, unit_id: int) -> Optional[TreeNode]:
        """根据单元 ID 查找节点。"""
        for root in self._roots:
            for child in root.children:
                if child.node_id == unit_id:
                    return child
        return None

    def expand_unit(self, unit_id: int, files: list) -> None:
        """加载单元下的文件作为树节点子项。"""
        node = self.get_node_by_unit_id(unit_id)
        if not node:
            return
        self.beginResetModel()
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
        self.endResetModel()

    def collapse_unit(self, unit_id: int) -> None:
        """卸载文件子节点。"""
        node = self.get_node_by_unit_id(unit_id)
        if node:
            self.beginResetModel()
            node.children = []
            self.endResetModel()

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
                if pnode.node_type in ("root", "favorites") and row < len(pnode.children):
                    return self.createIndex(row, column, pnode.children[row])
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
                # 收藏节点下的单元：library_root_id == -1
                if node.library_root_id == -1 and root.node_type == "favorites":
                    return self.createIndex(r_idx, 0, root)
                if root.node_id == node.library_root_id:
                    return self.createIndex(r_idx, 0, root)
        return QModelIndex()

    def rowCount(self, parent=QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._roots)
        node = parent.internalPointer()
        if node and node.node_type in ("root", "favorites"):
            return len(node.children)
        if node and node.node_type == "unit" and node.children:
            return len(node.children)
        return 0

    def columnCount(self, parent=QModelIndex()) -> int:
        return 2

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
                if node.node_type == "unit" and node.node_subtype != "file":
                    if node.is_starred or node.is_manual:
                        prefix = "★ "
                    elif node.status == "merged":
                        prefix = "▷ "
                return f"{prefix}{node.name}"
            elif col == self.COL_META:
                if node.node_subtype == "file":
                    from app.utils.file_helpers import format_size
                    return format_size(node.total_size)
                if node.node_type == "unit":
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
            return "名称" if section == 0 else "详情"
        return None

    # ============================================================
    # 辅助
    # ============================================================

    @staticmethod
    def _build_tooltip_text(node: TreeNode) -> str:
        if node.node_type == "root":
            return f"媒体库根目录: {node.path}"
        if node.node_type == "favorites":
            return "收藏的文件夹，可快速访问常用位置"
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
# 树视图
# ============================================================

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
    unit_double_clicked = Signal(int)
    merge_requested = Signal(int, list)
    split_requested = Signal(int)
    mark_requested = Signal(str)
    unmark_requested = Signal(int)
    star_requested = Signal(int)
    unstar_requested = Signal(int)
    exclude_requested = Signal(int)
    cover_requested = Signal(int)
    clear_cover_requested = Signal(int)
    remove_root_requested = Signal(int)
    rename_requested = Signal(int)        # F2: 重命名单元
    copy_path_requested = Signal(str)     # Ctrl+C: 复制路径
    file_selected_from_tree = Signal(int) # file_id — 树中单击文件时发射

    def __init__(self, model: FolderTreeModel, parent=None) -> None:
        super().__init__(parent)
        self._model = model
        self.setModel(model)
        self.setHeaderHidden(True)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(model.COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(model.COL_META, QHeaderView.ResizeMode.ResizeToContents)
        self.setAnimated(True)
        self.setExpandsOnDoubleClick(True)
        self.setIndentation(16)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.expandAll()
        self.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.selectionModel().selectionChanged.connect(self._on_tree_file_selected)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.doubleClicked.connect(self._on_double_clicked)
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

    def selected_unit_ids(self) -> list[int]:
        """获取当前选中项对应的所有资源单元 ID（不触发信号）。"""
        unit_ids: set[int] = set()
        for idx in self.selectedIndexes():
            if idx.column() != 0:
                continue
            ids = self._model.get_selected_units(idx)
            unit_ids.update(ids)
        return list(unit_ids)

    @Slot(QModelIndex)
    def _on_double_clicked(self, index: QModelIndex) -> None:
        """双击单元节点 → 直接加载该单元的文件列表。"""
        if not index.isValid():
            return
        node = index.internalPointer()
        if node is None or node.node_type != "unit":
            return
        self.unit_double_clicked.emit(node.node_id)

    @Slot()
    def _on_selection_changed(self) -> None:
        indexes = self.selectedIndexes()
        if not indexes:
            return
        unit_ids: set[int] = set()
        for idx in indexes:
            if idx.column() != 0:
                continue
            ids = self._model.get_selected_units(idx)
            unit_ids.update(ids)
        if unit_ids:
            self.unit_selected.emit(list(unit_ids))

    @Slot()
    def _on_tree_file_selected(self, selected, deselected) -> None:
        """检测文件节点选中并发射 file_selected_from_tree 信号。"""
        indexes = selected.indexes()
        if not indexes:
            return
        idx = indexes[0]
        if idx.column() != 0:
            return
        node = idx.internalPointer()
        if node and node.node_subtype == "file":
            self.file_selected_from_tree.emit(node.node_id)

    def select_tree_node_by_file_id(self, file_id: int) -> None:
        """在展开的树中选中指定的文件节点。"""
        model = self._model
        for root_row, root in enumerate(model._roots):
            root_idx = model.index(root_row, 0)
            if not root_idx.isValid():
                continue
            for child_row, child in enumerate(root.children):
                if child.node_type == "unit" and child.children:
                    for file_row, file_node in enumerate(child.children):
                        if file_node.node_id == file_id:
                            unit_idx = model.index(child_row, 0, root_idx)
                            if not unit_idx.isValid():
                                continue
                            file_idx = model.index(file_row, 0, unit_idx)
                            if file_idx.isValid():
                                sel = self.selectionModel()
                                if sel:
                                    sel.blockSignals(True)
                                self.setCurrentIndex(file_idx)
                                if sel:
                                    sel.blockSignals(False)
                                return

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
