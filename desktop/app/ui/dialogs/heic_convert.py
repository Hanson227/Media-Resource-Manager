# -*- coding: utf-8 -*-
"""
HEIC 转换入口（兼容层）—— 单/多文件右键入口都汇聚到同一个对话框。

历史背景：这里曾有一套自己的 QThread + convert_batch 调用，与「HEIC 检查与转换」
对话框是两份实现 —— 同名冲突、取消、源文件处置（回收站 / 永久删除 / 保留）
的行为必然漂移，而源文件处置是会丢数据的。现在统一走
`app.ui.dialogs.heic_batch_convert.HeicBatchConvertDialog`（其内部调
`app.services.heic_service`，与 Web 端同一条编排）。
"""

import logging
from pathlib import Path
from typing import Optional

from config import AppConfig

logger = logging.getLogger(__name__)


def _resolve_config(config: Optional[AppConfig]) -> AppConfig:
    """调用方没给配置时，回落到磁盘上的 config.json（再不行用默认值）。"""
    if config is not None:
        return config
    try:
        return AppConfig.from_file(Path("config.json"))
    except Exception as e:
        logger.debug(f"读取 config.json 失败，使用默认配置: {e}")
        return AppConfig()


class HeicConvertDialog:
    """单文件/多文件转换入口（薄封装）。"""

    @staticmethod
    def run_for_files(file_paths: list, parent=None,
                      config: Optional[AppConfig] = None) -> bool:
        """打开转换对话框并预置给定文件（默认勾选）。

        参数:
            file_paths: 文件路径列表（非 HEIC 扩展名的会被忽略）。
            parent: 父窗口。
            config: 应用配置；None 时回落到 config.json。

        返回:
            True 表示打开了对话框（对话框内是否真的转换由用户决定）。
        """
        from PySide6.QtWidgets import QMessageBox

        from app.core.heic_converter import HEIC_EXTENSIONS
        from app.ui.dialogs.heic_batch_convert import HeicBatchConvertDialog

        sources = [str(p) for p in file_paths
                   if Path(str(p)).suffix.lower() in HEIC_EXTENSIONS]
        if not sources:
            QMessageBox.information(parent, "HEIC 转换", "没有选中 HEIC 文件")
            return False

        dialog = HeicBatchConvertDialog(_resolve_config(config), parent,
                                        sources=sources)
        dialog.exec()
        return True
