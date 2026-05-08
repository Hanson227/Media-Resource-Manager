# -*- coding: utf-8 -*-
"""
查重设置对话框 —— 让用户调整查重阈值和匹配策略。
"""

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QDialogButtonBox,
    QDoubleSpinBox, QSpinBox, QCheckBox, QLabel, QGroupBox,
)

from config import AppConfig
from app.ui.theme import SUBTEXT_0


class DedupSettingsDialog(QDialog):
    """查重设置对话框。

    信号:
        settings_changed: 用户确认修改后发出 (new_jaccard, new_phash, new_dhash, new_face, face_enabled)
    """

    settings_changed = Signal(float, int, int, float, bool)

    def __init__(self, config: AppConfig, parent=None) -> None:
        """初始化查重设置对话框。

        参数:
            config: 当前应用配置。
        """
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("查重设置")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        # ==== 资源单元级阈值 ====
        unit_group = QGroupBox("资源单元级查重")
        unit_form = QFormLayout(unit_group)

        self._jaccard_spin = QDoubleSpinBox()
        self._jaccard_spin.setRange(0.0, 1.0)
        self._jaccard_spin.setSingleStep(0.05)
        self._jaccard_spin.setValue(config.jaccard_threshold)
        self._jaccard_spin.setToolTip("两个资源单元的杰卡德相似度达到此值即视为重复")
        unit_form.addRow("杰卡德阈值:", self._jaccard_spin)

        layout.addWidget(unit_group)

        # ==== 文件级匹配阈值 ====
        file_group = QGroupBox("文件级匹配")
        file_form = QFormLayout(file_group)

        self._phash_spin = QSpinBox()
        self._phash_spin.setRange(0, 64)
        self._phash_spin.setValue(config.phash_hamming_threshold)
        self._phash_spin.setToolTip("pHash 汉明距离 ≤ 此值视为匹配（0=完全相同）")
        file_form.addRow("pHash 汉明距离阈值:", self._phash_spin)

        self._dhash_spin = QSpinBox()
        self._dhash_spin.setRange(0, 64)
        self._dhash_spin.setValue(config.dhash_hamming_threshold)
        self._dhash_spin.setToolTip("dHash 汉明距离 ≤ 此值视为匹配")
        file_form.addRow("dHash 汉明距离阈值:", self._dhash_spin)

        layout.addWidget(file_group)

        # ==== 人脸设置 ====
        face_group = QGroupBox("人脸识别（实验性）")
        face_form = QFormLayout(face_group)

        self._face_check = QCheckBox("启用人脸识别查重")
        self._face_check.setChecked(config.face_detection_enabled)
        self._face_check.setToolTip("需要下载预训练模型后才能使用")
        face_form.addRow(self._face_check)

        self._face_spin = QDoubleSpinBox()
        self._face_spin.setRange(0.0, 1.0)
        self._face_spin.setSingleStep(0.05)
        self._face_spin.setValue(config.face_distance_threshold)
        self._face_spin.setToolTip("人脸欧氏距离 ≤ 此值视为同一人物")
        face_form.addRow("人脸距离阈值:", self._face_spin)

        layout.addWidget(face_group)

        # ==== 提示 ====
        hint = QLabel(
            "提示: 阈值越低越严格（减少误报但可能漏报），"
            "阈值越高越宽松（发现更多重复但可能误判）。"
        )
        hint.setStyleSheet(f"color: {SUBTEXT_0}; font-size: 11px; padding: 4px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ==== 按钮 ====
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @Slot()
    def _on_accept(self) -> None:
        """确认并发出设置变更信号。"""
        self.settings_changed.emit(
            self._jaccard_spin.value(),
            self._phash_spin.value(),
            self._dhash_spin.value(),
            self._face_spin.value(),
            self._face_check.isChecked(),
        )
        self.accept()
