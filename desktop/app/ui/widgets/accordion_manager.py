# -*- coding: utf-8 -*-
"""
手风琴管理器 —— 统一管理文件夹树的展开/收起逻辑和状态。

职责：
- 同一根下只允许一个单元展开（手风琴互斥）
- 同一时间只展开一个媒体库根节点
- 集中维护 _current_expanded_unit_id / _current_root_id 状态
"""

import logging
from typing import Optional

from app.ui.left_panel.folder_tree import FolderTreeView

logger = logging.getLogger(__name__)


class AccordionManager:
    """手风琴展开/收起逻辑管理器。"""

    def __init__(self, tree_view: FolderTreeView) -> None:
        self._tree_view = tree_view
        self._current_expanded_unit_id: Optional[int] = None
        self._current_root_id: Optional[int] = None

    # ---- 公开 API ----

    def expand_unit(self, unit_id: int) -> bool:
        """展开指定单元，执行手风琴互斥逻辑，折叠其他根节点。

        Returns:
            True 表示成功展开，False 表示节点不存在。
        """
        model = self._tree_view.model()
        new_node = model.get_node_by_unit_id(unit_id)
        if not new_node:
            return False

        # 收起同一根下的之前展开单元
        self._collapse_previous_unit(new_node.library_root_id, unit_id)

        # 折叠其他根节点，只展开当前根
        self._collapse_other_roots(new_node.library_root_id)

        # 更新状态
        self._current_expanded_unit_id = unit_id
        self._current_root_id = new_node.library_root_id
        return True

    def collapse_current(self) -> None:
        """收起当前展开的单元（文件子节点级别）。"""
        if self._current_expanded_unit_id is None:
            return
        try:
            self._tree_view.model().collapse_unit(self._current_expanded_unit_id)
        except Exception:
            pass
        finally:
            self._current_expanded_unit_id = None

    def expand_root_only(self, root_id: int) -> None:
        """只展开指定根节点，折叠其他根。不改变展开单元状态。"""
        self._collapse_other_roots(root_id)
        self._current_root_id = root_id

    def reset(self) -> None:
        """重置所有手风琴状态。"""
        self._current_expanded_unit_id = None
        self._current_root_id = None

    # ---- 属性 ----

    @property
    def current_expanded_unit_id(self) -> Optional[int]:
        return self._current_expanded_unit_id

    @property
    def current_root_id(self) -> Optional[int]:
        return self._current_root_id

    # ---- 内部方法 ----

    def _collapse_previous_unit(self, root_id: int, new_unit_id: int) -> None:
        """收起同一根下的之前展开单元。"""
        if self._current_expanded_unit_id is None:
            return
        if self._current_expanded_unit_id == new_unit_id:
            return

        model = self._tree_view.model()
        prev_node = model.get_node_by_unit_id(self._current_expanded_unit_id)
        if prev_node and prev_node.library_root_id == root_id:
            try:
                model.collapse_unit(self._current_expanded_unit_id)
            except Exception:
                pass

    def _collapse_other_roots(self, keep_root_id: int) -> None:
        """折叠除指定根外的其他根节点。"""
        model = self._tree_view.model()
        roots = model.get_roots()
        for row in range(model.rowCount()):
            root_idx = model.index(row, 0)
            root = roots[row]
            if root and root.node_id != keep_root_id:
                self._tree_view.collapse(root_idx)
