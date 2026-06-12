# -*- coding: utf-8 -*-
"""
API 服务器 —— FastAPI 应用创建和 uvicorn 线程管理。

在后台线程中启动 HTTP 服务，监听 0.0.0.0:19527。
当前版本所有端点返回模拟数据，后续可通过替换 route 实现对接真实数据库。
"""

import logging
import threading
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import AppConfig
from app.api.routes import files, units, dedup, messages, events, tags

logger = logging.getLogger(__name__)

# 全局 FastAPI 应用实例
_app: Optional[FastAPI] = None


def create_app(config: AppConfig) -> FastAPI:
    """创建并配置 FastAPI 应用。

    参数:
        config: 应用配置。

    返回:
        配置好的 FastAPI 应用实例。
    """
    app = FastAPI(
        title="影视资源管理器 API",
        description="本地影视资源管理查重工具的 HTTP API，为安卓手机端预留。当前版本返回模拟数据。",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS 中间件：允许局域网内 Android 设备访问
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 将配置存入 app.state，路由可通过 request.app.state.config 访问
    app.state.config = config

    # 注册路由
    app.include_router(files.router)
    app.include_router(units.router)
    app.include_router(dedup.router)
    app.include_router(messages.router)
    app.include_router(events.router)
    app.include_router(tags.router)

    # 健康检查
    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    # ============================================================
    # Web 访问认证
    # ============================================================
    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        cfg = getattr(request.app.state, "config", None)
        pin = cfg.web_pin if cfg else ""
        return {"pin_required": bool(pin)}

    @app.post("/api/auth/verify")
    async def auth_verify(request: Request):
        cfg = getattr(request.app.state, "config", None)
        if not cfg or not cfg.web_pin:
            return JSONResponse(status_code=400, content={"verified": False, "error": "未配置访问密码"})
        body = await request.json()
        if body.get("pin", "") == cfg.web_pin:
            return {"verified": True}
        return JSONResponse(status_code=403, content={"verified": False, "error": "密码错误"})

    @app.post("/api/auth/change-pin")
    async def auth_change_pin(request: Request):
        """修改 Web 访问密码。需要提供当前密码验证身份。"""
        import hmac
        cfg = getattr(request.app.state, "config", None)
        if not cfg:
            return JSONResponse(status_code=500, content={"success": False, "error": "配置不可用"})
        body = await request.json()
        # 如果已设置密码，必须验证旧密码
        if cfg.web_pin:
            old_pin = body.get("old_pin", "")
            if not hmac.compare_digest(old_pin, cfg.web_pin):
                return JSONResponse(status_code=403, content={"success": False, "error": "当前密码错误"})
        new_pin = body.get("new_pin", "")
        if len(new_pin) > 4:
            return JSONResponse(status_code=400, content={"success": False, "error": "密码最长4位"})
        # 更新配置
        new_config = AppConfig(
            db_path=cfg.db_path,
            thumbnail_cache_dir=cfg.thumbnail_cache_dir,
            media_extensions=cfg.media_extensions,
            exclude_patterns=cfg.exclude_patterns,
            hash_algorithms=cfg.hash_algorithms,
            phash_size=cfg.phash_size,
            dhash_size=cfg.dhash_size,
            video_frame_interval_sec=cfg.video_frame_interval_sec,
            jaccard_threshold=cfg.jaccard_threshold,
            phash_hamming_threshold=cfg.phash_hamming_threshold,
            dhash_hamming_threshold=cfg.dhash_hamming_threshold,
            face_distance_threshold=cfg.face_distance_threshold,
            face_detection_enabled=cfg.face_detection_enabled,
            face_model_dir=cfg.face_model_dir,
            face_confidence_threshold=cfg.face_confidence_threshold,
            window_title=cfg.window_title,
            window_width=cfg.window_width,
            window_height=cfg.window_height,
            splitter_ratio_left=cfg.splitter_ratio_left,
            grid_column_count=cfg.grid_column_count,
            grid_spacing=cfg.grid_spacing,
            watcher_enabled=cfg.watcher_enabled,
            watcher_debounce_ms=cfg.watcher_debounce_ms,
            api_enabled=cfg.api_enabled,
            api_host=cfg.api_host,
            api_port=cfg.api_port,
            smb_share_name_prefix=cfg.smb_share_name_prefix,
            preview_seek_percent=cfg.preview_seek_percent,
            web_pin=new_pin,
        )
        # 持久化到磁盘
        try:
            new_config.to_file(Path("config.json"))
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return JSONResponse(status_code=500, content={"success": False, "error": "保存配置失败"})
        # 更新内存中的配置
        request.app.state.config = new_config
        return {"success": True, "pin_required": bool(new_pin)}

    # ============================================================
    # 静态文件（Web 前端 SPA）
    # ============================================================
    web_dir = Path(__file__).resolve().parent.parent.parent.parent / "web"
    if web_dir.is_dir():
        # 根端点：浏览器返回 index.html，API 客户端返回 JSON
        @app.get("/")
        async def root(request: Request):
            accept = request.headers.get("accept", "")
            if "text/html" in accept:
                return FileResponse(str(web_dir / "index.html"), media_type="text/html")
            return {
                "name": "影视资源管理器 API",
                "version": "0.1.0",
                "status": "running",
                "docs": "/docs",
            }

        # SPA 路由兜底：所有非 API 路径返回 index.html
        # 静态文件启用 no-cache 防止浏览器缓存旧版 JS/CSS
        class NoCacheStaticFiles(StaticFiles):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
            async def get_response(self, path: str, scope):
                resp = await super().get_response(path, scope)
                if path.endswith((".js", ".css", ".html")):
                    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                return resp
        app.mount("/", NoCacheStaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        # 没有 web 目录时回退
        @app.get("/")
        async def root():
            return {
                "name": "影视资源管理器 API",
                "version": "0.1.0",
                "status": "running",
                "docs": "/docs",
            }

    return app


def _make_proactor_handler(original_handler):
    """创建忽略 Windows proactor 清理错误的 asyncio 异常处理器。

    手机端断开流式连接时，ProactorEventLoop 内部 socket shutdown
    可能抛出异常，破坏事件循环状态。此处理器静默忽略此类错误。
    """
    def handler(loop, context):
        msg = context.get("message", "")
        # 仅忽略连接关闭时的传输层清理错误
        if "connection_lost" in msg or "shutdown" in msg or "Transport" in msg:
            return
        if original_handler:
            original_handler(context)
    return handler


class APIServer:
    """API 服务器管理器。

    负责在独立线程中启动和停止 uvicorn 服务器。

    用法:
        server = APIServer(config)
        server.start()
        # ... 程序运行中 ...
        server.stop()
    """

    def __init__(self, config: AppConfig) -> None:
        """初始化 API 服务器。

        参数:
            config: 应用配置。
        """
        self._config = config
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None
        self._running = False

    def start(self) -> None:
        """在后台线程中启动 API 服务器。"""
        if self._running:
            logger.warning("API 服务器已在运行")
            return

        global _app
        _app = create_app(self._config)

        import asyncio

        config = uvicorn.Config(
            app=_app,
            host=self._config.api_host,
            port=self._config.api_port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        # 注入 asyncio 异常处理器，防止客户端断开时 ProactorEventLoop 崩溃
        original_serve = self._server.serve
        async def _safe_serve(sockets=None):
            loop = asyncio.get_event_loop()
            loop.set_exception_handler(
                _make_proactor_handler(loop.get_exception_handler())
            )
            return await original_serve(sockets=sockets)
        self._server.serve = _safe_serve

        self._thread = threading.Thread(
            target=self._server.run,
            daemon=True,
            name="api-server",
        )
        self._thread.start()
        self._running = True
        logger.info(
            f"API 服务器已启动: http://{self._config.api_host}:{self._config.api_port}"
        )
        logger.info(f"API 文档: http://localhost:{self._config.api_port}/docs")

    def stop(self) -> None:
        """停止 API 服务器。"""
        if not self._running or self._server is None:
            return

        self._server.should_exit = True
        self._running = False
        logger.info("API 服务器已停止")

    @property
    def is_running(self) -> bool:
        """服务器是否正在运行。"""
        return self._running
