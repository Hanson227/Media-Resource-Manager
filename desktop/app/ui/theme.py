# -*- coding: utf-8 -*-
"""
「私人放映室」主题色板 —— 石墨黑 + 黄铜。

设计意图：这是一个离线媒体档案室，不是又一个蓝紫色 AI 控制台。
底色压到接近黑（把注意力留给海报纸），用低饱和石墨灰分层，
只用黄铜色做「可操作 / 当前选中」的强调，避免满屏彩色。

QSS 样式表位于 style.qss；Python 层颜色用于程序化绘制
（缩略图 delegate、自绘图标、动态 QPalette）。
"""

# ---- 底色 ----
CRUST = "#07080A"        # 最外层 / 窗口边框
BASE = "#0B0D10"         # 窗口底色
MANTLE = "#101317"       # 导航栏、状态栏
SURFACE_0 = "#15191E"    # 卡片、输入框
SURFACE_1 = "#1B2027"    # 卡片悬停、次级面板
SURFACE_2 = "#272E37"    # 分隔线、边框

# ---- 覆盖层 / 次要文字 ----
OVERLAY_0 = "#5A6470"
OVERLAY_1 = "#808B98"
SUBTEXT_0 = "#8F99A6"
SUBTEXT_1 = "#B8C0CA"

# ---- 文字 ----
TEXT = "#ECE8E1"         # 暖白，比纯白柔和

# ---- 强调色 ----
ACCENT = "#D9A441"       # 黄铜：主操作、当前选中
ACCENT_HOVER = "#E8B65E"
ACCENT_SOFT = "rgba(217, 164, 65, 0.14)"
INDIGO = ACCENT          # 兼容旧代码里的名字；新代码请用 ACCENT
BLUE = "#7FB2D9"
GREEN = "#79B98A"
PEACH = ACCENT
RED = "#D46A6A"

# 字体
FONT_FAMILY = "Microsoft YaHei UI"


def apply_theme(app) -> None:
    """对 QApplication 应用全局主题（深色 QPalette + 字体）。"""
    from PySide6.QtGui import QPalette, QColor, QFont

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
        (QPalette.ColorRole.Highlight, ACCENT),
        (QPalette.ColorRole.HighlightedText, CRUST),
        (QPalette.ColorRole.Link, ACCENT),
        (QPalette.ColorRole.PlaceholderText, OVERLAY_1),
    ):
        palette.setColor(role, QColor(color_str))
    # 禁用态颜色
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(OVERLAY_0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(OVERLAY_0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(OVERLAY_0))

    app.setPalette(palette)
    app.setFont(QFont(FONT_FAMILY, 10))
