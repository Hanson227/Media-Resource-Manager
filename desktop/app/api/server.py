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
from fastapi.responses import FileResponse
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
    # 静态文件（Web 前端 SPA）
    # ============================================================
    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
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
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
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
            loop.default_exception_handler(context)
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
