# -*- coding: utf-8 -*-
"""
Catppuccin Mocha 主题色板 —— 集中管理所有颜色常量。

QSS 样式表位于 style.qss，Python 层颜色用于动态/程序化设置。
"""

# ---- 底色 ----
BASE = "#1e1e2e"
MANTLE = "#181825"
CRUST = "#11111b"

# ---- 表面 ----
SURFACE_0 = "#313244"
SURFACE_1 = "#3b3b4e"
SURFACE_2 = "#52546a"

# ---- 覆盖 ----
OVERLAY_0 = "#6c7086"
OVERLAY_1 = "#7f849c"
SUBTEXT_0 = "#a6adc8"
SUBTEXT_1 = "#bac2de"

# ---- 文字 ----
TEXT = "#cdd6f4"

# ---- 强调色 ----
INDIGO = "#818cf8"
BLUE = "#89b4fa"
GREEN = "#a6e3a1"
PEACH = "#fab387"
RED = "#f38ba8"


def apply_theme(app) -> None:
    """对 QApplication 应用全局主题。

    设置深色调色板，适用于 Catppuccin Mocha + style.qss。
    """
    from PySide6.QtGui import QPalette, QColor

    palette = QPalette()
    for role, color_str in (
        (QPalette.ColorRole.Window, BASE),
        (QPalette.ColorRole.WindowText, TEXT),
        (QPalette.ColorRole.Base, SURFACE_0),
        (QPalette.ColorRole.AlternateBase, SURFACE_1),
        (QPalette.ColorRole.Button, SURFACE_0),
        (QPalette.ColorRole.ButtonText, TEXT),
        (QPalette.ColorRole.Text, TEXT),
        (QPalette.ColorRole.BrightText, TEXT),
        (QPalette.ColorRole.ToolTipBase, SURFACE_1),
        (QPalette.ColorRole.ToolTipText, TEXT),
        (QPalette.ColorRole.Highlight, INDIGO),
        (QPalette.ColorRole.HighlightedText, TEXT),
    ):
        palette.setColor(role, QColor(color_str))
    # 禁用态颜色
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(OVERLAY_0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(OVERLAY_0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(OVERLAY_0))

    app.setPalette(palette)
