# -*- coding: utf-8 -*-
"""
Slate-Indigo 主题色板 —— 与 Web 端设计系统统一。
QSS 样式表位于 style.qss，Python 层颜色用于动态/程序化设置。
"""

# ---- 底色 ----
BASE = "#0e0e12"
MANTLE = "#16161d"
CRUST = "#0a0a0e"

# ---- 表面 ----
SURFACE_0 = "#1c1c26"
SURFACE_1 = "#252530"
SURFACE_2 = "#363648"

# ---- 覆盖 ----
OVERLAY_0 = "#5e5e72"
OVERLAY_1 = "#7c7c8a"
SUBTEXT_0 = "#a0a0b0"
SUBTEXT_1 = "#c0c0cc"

# ---- 文字 ----
TEXT = "#f0f0f4"

# ---- 强调色 ----
INDIGO = "#6366f1"
BLUE = "#60a5fa"
GREEN = "#22c55e"
PEACH = "#f59e0b"
RED = "#ef4444"


def apply_theme(app) -> None:
    """对 QApplication 应用全局主题。

    设置深色调色板，适用于 Slate-Indigo + style.qss。
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
