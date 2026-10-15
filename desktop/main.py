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

# 抑制 OpenCV/FFmpeg 的解码日志噪音（损坏视频会逐帧刷红色 h264 告警，
# 把真正的日志淹掉）。**不能**在这里直接写 os.environ —— 在 Windows 上它
# 传不进 OpenCV 的 ffmpeg DLL，必须走 CRT 的 _putenv_s，
# 细节见 app/utils/image_helpers.silence_ffmpeg_logs。
from app.utils.image_helpers import ensure_heif_support, silence_ffmpeg_logs  # noqa: E402

silence_ffmpeg_logs()

# 导入 app.utils.image_helpers 时已自动注册 HEIC/HEIF 解码器
# （ensure_heif_support，幂等）—— 这里只是取回状态用于日志。
_HEIF_OK = ensure_heif_support()

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
if _HEIF_OK:
    logger.info("HEIC/HEIF 格式支持已加载")
else:
    logger.warning("pillow-heif 未安装，HEIC/HEIF 文件无法识别")


def _parse_data_root_arg(argv) -> "Path | None":
    """从命令行/环境变量取数据根覆盖值（便携部署时用来显式指定）。

    只认 `--data-root <路径>` 与 `--data-root=<路径>`；其余参数原样留给 Qt
    （QApplication 会处理自己的参数）。优先级：命令行 > MEDIA_DATA_ROOT 环境变量。
    """
    override = None
    for index, arg in enumerate(list(argv)):
        if arg == "--data-root" and index + 1 < len(argv):
            override = argv[index + 1]
            del argv[index:index + 2]
            break
        if arg.startswith("--data-root="):
            override = arg.split("=", 1)[1]
            del argv[index]
            break
    if not override:
        override = os.environ.get("MEDIA_DATA_ROOT") or None
    return Path(override) if override else None


def main() -> None:
    """程序主入口。"""
    from app.utils import paths as app_paths

    # 切换到应用目录，确保相对路径（config.json, data/ 等）正确；
    # 打包态必须是 exe 所在目录（__file__ 在冻结包里不可靠）。
    os.chdir(app_paths.app_dir())

    data_root_override = _parse_data_root_arg(sys.argv)

    logger.info("=" * 50)
    logger.info("影视资源管理器 v0.1.0 启动中...")
    logger.info("=" * 50)

    # ============================================================
    # 1. 加载配置
    # ============================================================
    config_path = app_paths.default_config_path()
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
    # 1.1 数据根解析（数据库 / 缩略图 / PIN 可能放在移动盘上）
    # ============================================================
    original_root = config.data_root
    original_id = config.data_root_id
    base_config = config   # 落盘基线：保留用户写的相对路径，不写解析出的绝对路径

    resolved = app_paths.resolve_paths(config, explicit_data_root=data_root_override)
    for message in resolved.warnings:
        logger.warning(message)
    config = app_paths.apply_resolved_paths(config, resolved)
    logger.info(f"数据根: {resolved.data_root or '（未启用，使用应用目录）'}"
                f"  来源={resolved.data_root_source}")
    if not resolved.ok:
        logger.error(
            f"未找到数据盘上的数据根（期望: {resolved.expected_data_root}）；"
            "已临时使用应用目录下的空库，插入数据盘后重启即可恢复")
    # 记住本次发现的数据根/身份（换盘符后才能认出"还是同一个数据盘"）。
    # 只更新数据根字段：db_path / thumbnail_cache_dir 保持相对形式，
    # 否则 config.json 会被钉死在当前盘符上（换机器/换盘即失效）。
    if (config.data_root != original_root) or (config.data_root_id != original_id):
        try:
            base_config.with_updates(
                data_root=config.data_root,
                data_root_id=config.data_root_id).to_file(
                    config_path,
                    omit=() if app_paths.read_explicit_profile(config_path)
                    else ("profile",))
            logger.info(f"已记住数据根: {config.data_root}")
        except OSError as e:
            logger.warning(f"配置写入失败: {e}")

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
        from app.services.cleanup_service import (
            CleanupService, configure_thumbnail_cache_dir,
        )
        # 注入配置的缓存目录，否则清理/失效会打到错误的目录
        configure_thumbnail_cache_dir(config.thumbnail_cache_dir)
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
            api_server = APIServer(config, runtime={
                "data_root": str(resolved.data_root or ""),
                "data_root_status": resolved.data_root_source,
                "expected_data_root": str(resolved.expected_data_root or ""),
                "config_path": str(config_path),
            })
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
        # 先停后台线程：worker 仍在写库时 dispose 引擎会丢数据甚至崩溃。
        # 正常关窗由 MainWindow.closeEvent 处理，这里覆盖托盘退出等其它路径。
        try:
            main_window.stop_background_workers()
        except Exception as e:  # noqa: BLE001 — 退出流程不应再抛错
            logger.warning(f"停止后台任务失败: {e}")
        if api_server:
            api_server.stop()
        DatabaseManager.dispose()
        logger.info("清理完成，再见！")

    app.aboutToQuit.connect(on_exit)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
