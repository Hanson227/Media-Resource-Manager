# -*- coding: utf-8 -*-
"""
右键菜单 —— 资源单元和文件夹树的右键操作菜单。
"""

import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)


class FolderTreeContextMenu(QMenu):
    """文件夹树右键菜单。

    信号:
        merge_requested: 合并 (parent_unit_id, child_unit_ids)
        split_requested: 拆分 (unit_id)
        mark_requested: 标记 (folder_path)
        unmark_requested: 取消标记 (unit_id)
        exclude_requested: 排除单元 (unit_id)
        remove_root_requested: 删除根目录 (root_id)
        refresh_requested: 刷新
    """

    merge_requested = Signal(int, list)
    split_requested = Signal(int)
    mark_requested = Signal(str)
    unmark_requested = Signal(int)
    star_requested = Signal(int)
    unstar_requested = Signal(int)
    exclude_requested = Signal(int)
    cover_requested = Signal(int)       # 设置封面 (unit_id)
    clear_cover_requested = Signal(int) # 清除封面 (unit_id)
    remove_root_requested = Signal(int)
    refresh_requested = Signal()

    def __init__(self, parent, unit_ids: list[int], model,
                 root_id: int | None = None) -> None:
        super().__init__(parent)
        self._unit_ids = unit_ids
        self._model = model
        self._root_id = root_id
        self._build_menu()

    def _build_menu(self) -> None:
        # 根目录右键菜单
        if self._root_id is not None:
            self._build_root_menu()
            return

        if len(self._unit_ids) == 1:
            self._build_single_menu()
        else:
            self._build_multi_menu()
        self.addSeparator()
        refresh_action = QAction("刷新", self)
        refresh_action.triggered.connect(lambda: self.refresh_requested.emit())
        self.addAction(refresh_action)

    def _build_single_menu(self) -> None:
        unit_id = self._unit_ids[0]
        node = self._model.get_node_by_unit_id(unit_id)

        if node is None:
            return

        # ---- 资源单元节点 ----
        # 合并
        try:
            with DatabaseManager.session() as session:
                children = q.get_child_units(session, unit_id)
                if children:
                    child_ids = [c.id for c in children]
                    merge_action = QAction("合并为单个资源单元", self)
                    merge_action.setToolTip(f"将 {len(children)} 个子单元合并到「{node.name}」下")
                    merge_action.triggered.connect(
                        lambda *args, pid=unit_id, cids=child_ids: self.merge_requested.emit(pid, cids)
                    )
                    self.addAction(merge_action)
        except Exception as e:
            logger.error(f"查询子单元失败: {e}")

        # 拆分
        if node.status == "merged":
            split_action = QAction("拆分资源单元", self)
            split_action.setToolTip("取消合并，恢复各子单元的独立状态")
            split_action.triggered.connect(lambda *args, uid=unit_id: self.split_requested.emit(uid))
            self.addAction(split_action)

        # 标记/取消标记
        if node.is_manual or node.is_starred:
            unmark_action = QAction("取消标记", self)
            unmark_action.triggered.connect(lambda *args, uid=unit_id: self.unmark_requested.emit(uid))
            self.addAction(unmark_action)
        else:
            mark_action = QAction("标记为资源单元", self)
            mark_action.triggered.connect(lambda *args, p=node.path: self.mark_requested.emit(p))
            self.addAction(mark_action)

        # 收藏 / 取消收藏
        if node.is_starred:
            unstar_action = QAction("取消收藏", self)
            unstar_action.setToolTip("将此文件夹从收藏中移除")
            unstar_action.triggered.connect(
                lambda *args, uid=unit_id: self.unstar_requested.emit(uid)
            )
            self.addAction(unstar_action)
        else:
            star_action = QAction("添加到收藏", self)
            star_action.setToolTip("收藏此文件夹以便快速访问")
            star_action.triggered.connect(
                lambda *args, uid=unit_id: self.star_requested.emit(uid)
            )
            self.addAction(star_action)

        self.addSeparator()

        # 排除
        exclude_action = QAction("排除此文件夹", self)
        exclude_action.setToolTip(f"将「{node.name}」标记为已排除，不再参与扫描和查重")
        exclude_action.triggered.connect(lambda *args, uid=unit_id: self.exclude_requested.emit(uid))
        self.addAction(exclude_action)

        self.addSeparator()

        # 设置封面
        cover_action = QAction("设置封面...", self)
        cover_action.setToolTip("为此文件夹选择一张封面图片")
        cover_action.triggered.connect(lambda *args, uid=unit_id: self.cover_requested.emit(uid))
        self.addAction(cover_action)

        if node.is_manual or node.is_starred:
            clear_cover_action = QAction("清除封面", self)
            clear_cover_action.setToolTip("恢复为默认缩略图")
            clear_cover_action.triggered.connect(lambda *args, uid=unit_id: self.clear_cover_requested.emit(uid))
            self.addAction(clear_cover_action)

    def _build_root_menu(self) -> None:
        """根目录节点的右键菜单。"""
        remove_action = QAction("删除此媒体库", self)
        remove_action.setToolTip("删除此媒体库根目录及其所有数据（资源单元和文件记录）")
        remove_action.triggered.connect(
            lambda *args, rid=self._root_id: self.remove_root_requested.emit(rid)
        )
        self.addAction(remove_action)

    def _build_multi_menu(self) -> None:
        merge_action = QAction(f"合并 {len(self._unit_ids)} 个资源单元", self)
        merge_action.setToolTip("将选中的资源单元合并到它们的公共父文件夹")
        self.addAction(merge_action)
