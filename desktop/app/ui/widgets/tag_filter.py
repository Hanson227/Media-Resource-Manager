# -*- coding: utf-8 -*-
"""
标签筛选栏 —— 文件标签的显示和筛选控件。
"""

import logging
from typing import Optional

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QSizePolicy

from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)


class TagChip(QPushButton):
    """单个标签芯片按钮（可切换选中状态）。"""

    def __init__(self, tag_id: int, name: str, color: Optional[str] = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._tag_id = tag_id
        self._tag_name = name
        self._tag_color = color or "#888888"
        self.setCheckable(True)
        self.setText(f" {name} ")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(self._build_style(False))
        self.toggled.connect(self._on_toggled)

    def _build_style(self, checked: bool) -> str:
        c = self._tag_color
        if checked:
            return (
                f"QPushButton {{ background: {c}; color: white; "
                f"border: 1px solid {c}; border-radius: 10px; "
                f"padding: 2px 8px; font-size: 12px; font-weight: bold; }}"
            )
        return (
            "QPushButton { background: transparent; color: #ccc; "
            f"border: 1px solid #555; border-radius: 10px; "
            f"padding: 2px 8px; font-size: 12px; }}"
            f"QPushButton:hover {{ border-color: {c}; color: {c}; }}"
        )

    def _on_toggled(self, checked: bool) -> None:
        self.setStyleSheet(self._build_style(checked))

    @property
    def tag_id(self) -> int:
        return self._tag_id


class TagFilterBar(QWidget):
    """标签筛选栏 —— 显示所有可用标签，点击筛选。"""

    tag_filter_changed = Signal(list)  # 选中的 tag_id 列表

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 2, 4, 2)
        self._layout.setSpacing(4)
        self._chips: list[TagChip] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hide()

    def reload_tags(self) -> None:
        """从数据库重新加载标签列表。"""
        for chip in self._chips:
            self._layout.removeWidget(chip)
            chip.deleteLater()
        self._chips.clear()

        try:
            with DatabaseManager.session() as session:
                tags = q.get_all_tags(session)
        except Exception as e:
            logger.warning(f"加载标签失败: {e}")
            tags = []

        if not tags:
            self.hide()
            return

        for tag in tags:
            chip = TagChip(tag.id, tag.name, tag.color)
            chip.toggled.connect(self._on_chip_toggled)
            self._layout.addWidget(chip)
            self._chips.append(chip)

        self._layout.addStretch()
        self.show()

    def _on_chip_toggled(self) -> None:
        selected = [c.tag_id for c in self._chips if c.isChecked()]
        self.tag_filter_changed.emit(selected)

    def clear_selection(self) -> None:
        """清除所有筛选。"""
        for chip in self._chips:
            chip.setChecked(False)
