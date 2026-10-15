# -*- coding: utf-8 -*-
"""
便携版（无桌面 GUI）入口 —— 只启动 HTTP API + Web 前端。

与 `main.py`（桌面完整档）的区别：
- **不导入任何 Qt/PySide6**，因此 PyInstaller 打包时不会收录桌面 GUI（体积大头）；
- 默认运行在**轻量档**：不注册查重路由、不加载哈希/人脸/查重管线；
- 数据（数据库 / 缩略图 / Web PIN）默认跟着**数据根**走，可放在移动硬盘上，
  盘符变化时自动定位（见 `app/utils/paths.resolve_paths`）。

用法:
    python main_web.py                      # 便携档，默认数据根=应用目录
    python main_web.py --data-root E:\\MediaData
    python main_web.py --host 0.0.0.0 --port 19527 --no-browser

退出：Ctrl+C，或直接关闭控制台窗口。
"""

import argparse
import io
import logging
import os
import socket
import sys
import time
from pathlib import Path

# 让 `python desktop/main_web.py` 与 `cd desktop && python main_web.py` 都能导入 app.*
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.utils import capabilities
from app.utils import paths as app_paths  # noqa: E402

# 抑制 OpenCV/FFmpeg 解码日志噪音；同时触发 HEIC 解码器注册。
# 必须走 image_helpers.silence_ffmpeg_logs（CRT _putenv_s），
# 直接改 os.environ 在 Windows 上传不进 cv2 的 DLL —— 见 docs/pitfalls.md。
from app.utils.image_helpers import ensure_heif_support, silence_ffmpeg_logs  # noqa: E402

silence_ffmpeg_logs()
_HEIF_OK = ensure_heif_support()

logger = logging.getLogger("main_web")


def _fix_windows_encoding() -> None:
    """修复 Windows 终端 GBK 编码导致的乱码（与 main.py 同款）。"""
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def _setup_logging(log_directory: Path, verbose: bool = False) -> None:
    """同时输出到控制台与文件（便携版常无控制台，文件是唯一事后依据）。"""
    handlers: list[logging.Handler] = []
    stream = logging.StreamHandler(sys.stdout)
    handlers.append(stream)
    log_file = None
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
        log_file = log_directory / "app.log"
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError as e:  # 只读介质等情况：仍要能跑
        print(f"[warn] 无法写日志文件 ({log_directory}): {e}", file=sys.stderr)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    if log_file:
        logger.info(f"日志文件: {log_file}")


def _port_available(host: str, port: int) -> bool:
    """端口是否可用（先探测再启动，避免 uvicorn 在子线程里静默退出）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # 不设 SO_REUSEADDR：Windows 上它会允许"已占用"的端口再次绑定，
        # 探测就失去意义（真实冲突要等 uvicorn 报错才发现）。
        probe.bind((host if host not in ("0.0.0.0", "") else "0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _find_free_port(host: str, preferred: int, tries: int = 20) -> int:
    """从 preferred 起找一个可用端口；全被占用时抛 RuntimeError。"""
    for offset in range(tries):
        candidate = preferred + offset
        if _port_available(host, candidate):
            if offset:
                logger.warning(f"端口 {preferred} 已被占用，改用 {candidate}")
            return candidate
    raise RuntimeError(f"端口 {preferred}~{preferred + tries - 1} 均被占用，请用 --port 指定其它端口")


def _parse_args(argv) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="main_web",
        description="影视资源管理器 便携版（Web/API，无桌面界面）",
    )
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认：应用目录下 config.json）")
    parser.add_argument("--data-root", default=None,
                        help="数据根目录（数据库/缩略图/PIN 的存放位置），优先于配置")
    parser.add_argument("--profile", default=None, choices=["lite", "full"],
                        help="运行档位：lite=不加载查重（默认）；full=完整后端能力")
    parser.add_argument("--host", default=None, help="监听地址（默认取配置 api_host）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取配置 api_port）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def _config_file_host(config_path: Path) -> str:
    """落盘到 config.json 用的监听地址。

    已经显式写过就沿用用户的；否则写便携版默认的 `0.0.0.0`。

    为什么必须这样：首次运行会**先把配置落盘**（记住数据根），而 `AppConfig` 的
    `api_host` dataclass 默认值是 `127.0.0.1`。如果照抄落盘，下一行解析监听地址时
    读到的就是"文件里显式写了 127.0.0.1"，于是把"刚落盘的默认值"当成用户选择 ——
    手机永远连不上（真实报障的根因，首次运行时自相矛盾）。
    """
    return str(app_paths.read_explicit_config_value(config_path, "api_host") or "0.0.0.0")


def _resolve_host(args, config, config_path: Path) -> str:
    """决定监听地址 —— 便携版默认监听 0.0.0.0，否则手机根本连不上。

    为什么不能直接 `args.host or config.api_host`：`AppConfig.api_host` 的 dataclass
    默认值是 `127.0.0.1`（回环，只有本机能访问）。便携版首次运行**没有 config.json**，
    于是回落到该默认值 —— 平板自己打得开、手机永远连不上（真实报障）。
    因此"默认值"必须当作用户没表态，只有 config.json 里**显式写的**才认。

    优先级：--host > MEDIA_API_HOST 环境变量 > config.json 显式 api_host > 0.0.0.0
    """
    explicit = app_paths.read_explicit_config_value(config_path, "api_host")
    if args.host:
        return str(args.host)
    env_host = os.environ.get("MEDIA_API_HOST")
    if env_host:
        return env_host
    if explicit:
        return str(explicit)
    return "0.0.0.0"


def _load_config(config_path: Path):
    """加载配置；文件不存在时返回默认配置（并写出一份默认配置）。"""
    from config import AppConfig

    if config_path.exists():
        try:
            return AppConfig.from_file(config_path), True
        except Exception as e:  # noqa: BLE001 — 配置坏了也必须能启动并告知用户
            logger.error(f"配置文件加载失败，使用默认配置: {e}")
            return AppConfig(), False
    return AppConfig(), False


def main(argv=None) -> int:
    """便携版主入口。返回进程退出码。"""
    _fix_windows_encoding()
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    # 切到应用目录：config.json / 相对路径 / 旧代码里的 Path("config.json")
    # 全部以应用目录为基准（与 main.py 一致）。
    app_dir = app_paths.app_dir()
    try:
        os.chdir(app_dir)
    except OSError as e:
        print(f"[warn] 无法切换到应用目录 {app_dir}: {e}", file=sys.stderr)

    _setup_logging(app_paths.log_dir(), verbose=args.verbose)

    logger.info("=" * 56)
    logger.info("影视资源管理器 · 便携版（Web/API）启动中")
    logger.info("=" * 56)
    logger.info(f"应用目录: {app_dir}")
    logger.info(f"HEIC 支持: {'已加载' if _HEIF_OK else '未安装 pillow-heif'}")

    # ---- 1. 配置 ----
    config_path = Path(args.config) if args.config else app_paths.default_config_path()
    config, loaded = _load_config(config_path)
    original_root = config.data_root
    original_id = config.data_root_id
    if not loaded:
        logger.info(f"未找到配置文件，将使用默认配置: {config_path}")

    # 档位优先级：--profile > MEDIA_PROFILE > config.json 里**用户显式写的** profile
    # > 本入口默认 lite。
    # 之所以要区分"显式写的"：`profile` 缺省值是 full，与"用户就是要 full"无法区分；
    # 若不区分，桌面端生成的 config.json 会让便携版入口跑成完整档。
    explicit_profile = app_paths.read_explicit_profile(config_path)
    profile = (args.profile or os.environ.get("MEDIA_PROFILE")
               or explicit_profile or "lite").strip().lower()
    config = config.with_updates(profile=profile)
    # 落盘时以"用户原样配置 + 档案"为基线（解析出的绝对路径只在本次运行时用）
    base_config = config
    logger.info(f"运行档位: {profile}（仅表示这是哪个包；功能由能力探测决定，"
                f"两者共用同一套代码）")
    caps = capabilities.detect(config.face_model_dir)
    logger.info(f"能力: 查重={'有' if caps.dedup else '无'} "
                f"视频={'有' if caps.video_thumbnail else '无'} "
                f"人脸={'有' if caps.face else '无'} "
                f"HEIC={'有' if caps.pillow_heif else '无'} "
                f"RAW={'有' if caps.rawpy else '无'} "
                f"桌面界面={'有' if caps.qt else '无'}")
    for note in caps.notes():
        logger.info(f"  · {note}")

    # ---- 2. 数据根解析（数据跟着数据盘走）----
    explicit_root = Path(args.data_root) if args.data_root else None
    resolved = app_paths.resolve_paths(
        config, explicit_data_root=explicit_root,
        # "插盘即用"：config.json 里**从未写过** data_root（平板首次运行）时，允许去
        # 各卷搜 marker 并采纳唯一命中 —— 否则用户会看到一个空库，而数据就在盘上。
        # 桌面档入口（main.py）不开这个开关，避免开发机上被别的盘"顺手接管"。
        discover_when_unset=app_paths.read_explicit_config_value(
            config_path, "data_root") is None,
    )
    for message in resolved.warnings:
        logger.warning(message)
    config = app_paths.apply_resolved_paths(config, resolved)

    logger.info(f"数据根: {resolved.data_root or '（未启用，使用应用目录）'}"
                f"  来源={resolved.data_root_source}")
    logger.info(f"数据库: {config.db_path}")
    logger.info(f"缩略图缓存: {config.thumbnail_cache_dir}")
    if not resolved.ok:
        logger.error(
            f"未找到数据盘上的数据根（期望: {resolved.expected_data_root}）。"
            "已临时改用应用目录下的空库；插入数据盘后重启即可恢复，"
            "或在 Web 设置里指定数据目录。")

    # 记住本次发现的数据根/身份：否则每次启动都要重新搜索，且换盘符后
    # 无法凭 data_root_id 认定"还是同一个数据盘"。
    # 注意落盘的是"仅更新数据根字段"的那份配置：db_path / thumbnail_cache_dir
    # 保持用户写的相对形式（相对数据根），不写进本次解析出的绝对路径 ——
    # 否则 config.json 会被钉死在某个盘符上，换机器就失效。
    need_persist = ((config.data_root != original_root)
                    or (config.data_root_id != original_id)
                    or not loaded)
    if need_persist:
        persist = base_config.with_updates(
            data_root=config.data_root, data_root_id=config.data_root_id,
            # 监听地址见 _config_file_host：绝不把 dataclass 的 127.0.0.1 默认值
            # 写进文件（否则等于替用户做了"只有本机能访问"的决定）。
            api_host=_config_file_host(config_path),
            api_port=int(config.api_port or 19527))
        try:
            # profile 只在用户显式写过时才落盘（见 _read_explicit_profile）：
            # 否则跑一次便携版就把共享的 config.json 改成轻量档了。
            persist.to_file(config_path,
                            omit=() if explicit_profile else ("profile",))
            logger.info(f"已写入配置: {config_path}"
                        f"（数据根: {config.data_root or '未启用'}）")
        except OSError as e:
            logger.warning(f"配置写入失败（只读介质？）: {e}")

    # ---- 3. 数据库 ----
    from app.db.engine import DatabaseManager
    from app.db.migrations import init_db, migrate_db

    try:
        init_db(config.db_path)
        migrate_db()
    except Exception as e:  # noqa: BLE001
        logger.error(f"数据库初始化失败: {e}")
        if not resolved.ok:
            logger.error("数据盘未连接时无法初始化数据库，请先插入数据盘。")
        return 1
    logger.info("数据库就绪")

    # ---- 4. 缩略图缓存目录注入 + 孤儿清理 ----
    try:
        from app.services.cleanup_service import (
            CleanupService, configure_thumbnail_cache_dir,
        )
        configure_thumbnail_cache_dir(config.thumbnail_cache_dir)
        purged = CleanupService.purge_orphaned_thumbnails()
        if purged:
            logger.info(f"已清理 {purged} 个孤儿缩略图缓存文件")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"孤儿缩略图清理失败: {e}")

    # ---- 5. 启动 HTTP API（Web 前端由它一起提供）----
    from app.api.server import APIServer
    from app.utils.net import is_loopback_host, lan_urls, portal_url

    host = _resolve_host(args, config, config_path)
    preferred_port = args.port or config.api_port or 19527
    try:
        port = _find_free_port(host, preferred_port)
    except RuntimeError as e:
        logger.error(str(e))
        return 1
    config = config.with_updates(api_enabled=True, api_host=host, api_port=port)
    logger.info(f"监听地址: {host}"
                f"（{'所有网卡，手机可访问' if host == '0.0.0.0' else '注意：非 0.0.0.0 时外部设备可能访问不到'}）")

    server = APIServer(config, runtime={
        "data_root": str(resolved.data_root or ""),
        "data_root_status": resolved.data_root_source,
        "expected_data_root": str(resolved.expected_data_root or ""),
        "candidates": [str(c) for c in resolved.candidates],
        "config_path": str(config_path),
    })
    try:
        server.start()
    except Exception as e:  # noqa: BLE001
        logger.error(f"API 服务器启动失败: {e}")
        return 1

    local_url = portal_url(host, port)
    lan = lan_urls(host, port)[1:]

    logger.info("=" * 56)
    logger.info(f"本机打开:      {local_url}")
    if lan:
        for index, url in enumerate(lan):
            logger.info(f"{'手机/平板访问: ' if index == 0 else '  备用地址:    '}{url}")
        logger.info("手机需与平板在同一 WiFi；浏览器里直接输入上面的地址即可")
        logger.info("若手机打不开：① 首次启动的防火墙弹窗必须勾选「专用网络」并允许访问；")
        logger.info("              ② 多网卡/开了 VPN 时换上面的『备用地址』试；")
        logger.info("              ③ 补一条入站规则（管理员 CMD）：")
        logger.info(f"                 netsh advfirewall firewall add rule name=\"MediaManager {port}\" "
                    f"dir=in action=allow protocol=TCP localport={port}")
    elif not is_loopback_host(host):
        # 想开 0.0.0.0 却没探测到任何局域网地址（网卡没连上/只有虚拟网卡）
        logger.warning("未探测到局域网 IP，手机可能访问不了；"
                       "可在平板上用 ipconfig 查无线网卡的 IPv4 地址后手动拼 http://<IP>:%d", port)
    else:
        logger.warning(f"当前只监听本机回环地址（host={host}），手机/平板访问不了！")
        logger.warning("要让手机访问：去掉 --host 参数（便携版默认监听 0.0.0.0 全部网卡），"
                       "或显式写 --host 0.0.0.0；同时确保防火墙允许入站。")
    if config.web_pin:
        logger.info("已启用 Web 访问密码（4 位）")
    elif host == "0.0.0.0":
        logger.warning("未设置 Web 访问密码：同一 WiFi 下的任何设备都能打开这个库；"
                       "建议在 Web 界面「设置」里设一个 4 位密码")
    logger.info("按 Ctrl+C 退出（关闭这个窗口程序就停了）")
    logger.info("=" * 56)

    if not args.no_browser:
        try:
            import webbrowser
            webbrowser.open(portal_url(host, port))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"自动打开浏览器失败: {e}")

    # ---- 6. 常驻：Ctrl+C 退出；后台线程意外死亡时也要退出（不静默假死）----
    try:
        while True:
            time.sleep(1.0)
            if not server.is_thread_alive:
                logger.error("API 服务器线程已退出，进程结束")
                return 1
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
    finally:
        server.stop()
        DatabaseManager.dispose()
    logger.info("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
