# -*- coding: utf-8 -*-
"""
影视资源管理器 —— 程序入口。

初始化顺序：
1. 加载配置
2. 初始化数据库
3. 启动 API 服务器（可选）
4. 启动 GUI 主窗口
5. 设置文件监控（可选）
"""

import os
import sys
import logging
from pathlib import Path

# 抑制 OpenCV/FFmpeg 的 h264 解码警告噪音
os.environ["OPENCV_FFMPEG_LOGLEVEL"] = "-8"
os.environ["OPENCV_LOG_LEVEL"] = "OFF"

# ============================================================
# 编码修复 —— 必须在所有其他导入之前执行
# ============================================================

def _fix_windows_encoding():
    """修复 Windows 终端 GBK 编码导致的乱码问题。"""
    import io
    # 确保标准输出使用 UTF-8 编码
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding='utf-8', errors='replace'
            )
    if sys.stderr.encoding != 'utf-8':
        try:
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding='utf-8', errors='replace'
            )

_fix_windows_encoding()

# 注册 HEIC/HEIF 图片格式支持（必须在 PIL 使用前）
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

from config import AppConfig
from app.db.engine import DatabaseManager
from app.db.migrations import init_db, migrate_db
from app.api.server import APIServer

# 日志配置 —— 指定 UTF-8 编码的 StreamHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# 确保 root logger 的处理器也使用 UTF-8
for handler in logging.root.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.setStream(sys.stdout)

logger = logging.getLogger(__name__)

# 确认 HEIC 支持状态
try:
    __import__("pillow_heif")
    logger.info("HEIC/HEIF 格式支持已加载")
except ImportError:
    logger.warning("pillow-heif 未安装，HEIC/HEIF 文件无法识别")


def main() -> None:
    """程序主入口。"""
    # 切换到脚本所在目录，确保相对路径（config.json, data/ 等）正确
    os.chdir(Path(__file__).parent)

    logger.info("=" * 50)
    logger.info("影视资源管理器 v0.1.0 启动中...")
    logger.info("=" * 50)

    # ============================================================
    # 1. 加载配置
    # ============================================================
    config_path = Path("config.json")
    if config_path.exists():
        try:
            config = AppConfig.from_file(config_path)
            logger.info(f"已加载配置文件: {config_path}")
        except Exception as e:
            logger.warning(f"配置文件加载失败: {e}，使用默认配置")
            config = AppConfig()
    else:
        config = AppConfig()
        # 保存默认配置
        try:
            config.to_file(config_path)
            logger.info(f"已保存默认配置到: {config_path}")
        except Exception as e:
            logger.warning(f"无法保存配置文件: {e}")

    # ============================================================
    # 2. 初始化数据库
    # ============================================================
    try:
        init_db(config.db_path)
        migrate_db()
        logger.info(f"数据库已初始化: {config.db_path}")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        sys.exit(1)

        # 启动后自动清理孤儿缩略图（DB 中已删除文件的残留缓存）
        try:
            from app.services.cleanup_service import CleanupService
            purged = CleanupService.purge_orphaned_thumbnails()
            if purged > 0:
                logger.info(f"已清理 {purged} 个孤儿缩略图")
        except Exception as e:
            logger.warning(f"孤儿缩略图清理失败: {e}")

    # ============================================================
    # 3. 启动 API 服务器
    # ============================================================
    api_server: APIServer | None = None
    if config.api_enabled:
        try:
            api_server = APIServer(config)
            api_server.start()
        except Exception as e:
            logger.error(f"API 服务器启动失败: {e}")

    # ============================================================
    # 4. 启动 GUI
    # ============================================================
    app = QApplication(sys.argv)
    app.setApplicationName(config.window_title)
    app.setApplicationVersion("0.1.0")
    # 树展开/收起指示器由 FolderTreeView 自定义绘制（干净三角形 ▶/▼）

    # 应用主题调色板
    from app.ui.theme import apply_theme
    apply_theme(app)

    # 设置应用图标
    icon_path = Path(__file__).parent / "app" / "resources" / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
        logger.info(f"已加载应用图标: {icon_path}")

    # 加载现代化样式表
    style_path = Path(__file__).parent / "app" / "ui" / "style.qss"
    if style_path.exists():
        app.setStyleSheet(style_path.read_text(encoding="utf-8"))
        logger.info(f"已加载样式表: {style_path}")

    from app.ui.main_window import MainWindow
    main_window = MainWindow(config)
    main_window.show()

    logger.info("GUI 主窗口已显示")

    # ============================================================
    # 5. 程序退出清理
    # ============================================================
    def on_exit() -> None:
        """程序退出时的清理工作。"""
        logger.info("程序退出，执行清理...")
        if api_server:
            api_server.stop()
        DatabaseManager.dispose()
        logger.info("清理完成，再见！")

    app.aboutToQuit.connect(on_exit)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
