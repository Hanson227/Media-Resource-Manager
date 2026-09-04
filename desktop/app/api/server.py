# -*- coding: utf-8 -*-
"""
API 服务器 —— FastAPI 应用创建和 uvicorn 线程管理。

在后台线程中启动 HTTP 服务，监听 config.api_host:config.api_port（默认 127.0.0.1:19527）。
路由接入真实数据库查询；内嵌 Web 前端静态文件服务。
"""

import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import AppConfig
from app.api.routes import files, units, dedup, messages, events, tags

logger = logging.getLogger(__name__)

# 全局 FastAPI 应用实例
_app: Optional[FastAPI] = None

# ============================================================
# Web PIN 会话令牌（进程内，简单过期）
# ============================================================
_AUTH_TOKEN_TTL = 12 * 3600  # 12 小时
_AUTH_ALLOWED_UNAUTH_PREFIXES = (
    "/api/health", "/api/auth/status", "/api/auth/verify", "/api/auth/change-pin",
)
_auth_tokens: dict[str, float] = {}          # token -> 过期时间戳
_auth_tokens_lock = threading.Lock()


def _issue_auth_token() -> str:
    """签发新令牌并记录过期时间。"""
    token = secrets.token_urlsafe(32)
    with _auth_tokens_lock:
        _auth_tokens[token] = time.time() + _AUTH_TOKEN_TTL
    return token


def _validate_auth_token(token: str) -> bool:
    """校验令牌是否有效（存在且未过期），顺带清理过期项。"""
    now = time.time()
    with _auth_tokens_lock:
        expired = [t for t, exp in _auth_tokens.items() if exp < now]
        for t in expired:
            _auth_tokens.pop(t, None)
        exp = _auth_tokens.get(token)
        return exp is not None and exp > now


def _revoke_all_auth_tokens() -> None:
    """使全部令牌失效（PIN 变更后调用）。"""
    with _auth_tokens_lock:
        _auth_tokens.clear()


def _extract_bearer_token(request: Request) -> str:
    """从请求提取访问令牌：优先 Authorization 头，回退 ?token= 查询参数。

    <img>/<video> 等标签发起的请求（缩略图/视频流）无法携带自定义
    Authorization 头，因此允许以 ?token= 查询参数传递令牌。
    """
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return (request.query_params.get("token") or "").strip()


class AuthVerifyBody(BaseModel):
    """Web PIN 校验请求体。"""
    pin: str = Field(default="", max_length=16)


class ChangePinBody(BaseModel):
    """修改 Web PIN 请求体。"""
    old_pin: str = Field(default="", max_length=16)
    new_pin: str = Field(default="", max_length=16)


def create_app(config: AppConfig) -> FastAPI:
    """创建并配置 FastAPI 应用。

    参数:
        config: 应用配置。

    返回:
        配置好的 FastAPI 应用实例。
    """
    app = FastAPI(
        title="影视资源管理器 API",
        description="本地影视资源管理查重工具的 HTTP API，供 Web 前端与局域网移动端访问。",
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
    def health():
        return {"status": "ok"}

    # ============================================================
    # Web 访问认证
    # ============================================================
    @app.get("/api/auth/status")
    def auth_status(request: Request):
        cfg = getattr(request.app.state, "config", None)
        pin = cfg.web_pin if cfg else ""
        return {"pin_required": bool(pin)}

    @app.post("/api/auth/verify")
    def auth_verify(body: AuthVerifyBody, request: Request):
        cfg = getattr(request.app.state, "config", None)
        if not cfg or not cfg.web_pin:
            return JSONResponse(status_code=400, content={"verified": False, "error": "未配置访问密码"})
        if body.pin == cfg.web_pin:
            return {"verified": True, "token": _issue_auth_token()}
        return JSONResponse(status_code=403, content={"verified": False, "error": "密码错误"})

    @app.post("/api/auth/change-pin")
    def auth_change_pin(body: ChangePinBody, request: Request):
        """修改 Web 访问密码。需要提供当前密码验证身份。"""
        import hmac
        cfg = getattr(request.app.state, "config", None)
        if not cfg:
            return JSONResponse(status_code=500, content={"success": False, "error": "配置不可用"})
        # 如果已设置密码，必须验证旧密码
        if cfg.web_pin:
            if not hmac.compare_digest(body.old_pin, cfg.web_pin):
                return JSONResponse(status_code=403, content={"success": False, "error": "当前密码错误"})
        new_pin = body.new_pin
        if len(new_pin) > 4:
            return JSONResponse(status_code=400, content={"success": False, "error": "密码最长4位"})
        # 更新配置（frozen dataclass 仅替换 web_pin 字段）
        try:
            new_config = cfg.with_updates(web_pin=new_pin)
            new_config.to_file(Path("config.json"))
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            return JSONResponse(status_code=500, content={"success": False, "error": "保存配置失败"})
        # 更新内存中的配置
        request.app.state.config = new_config
        # PIN 已变更：使旧令牌全部失效，需重新验证
        _revoke_all_auth_tokens()
        return {"success": True, "pin_required": bool(new_pin)}

    # ============================================================
    # Web PIN 强制（配置 web_pin 时 /api/* 均需携带有效令牌）
    # ============================================================
    @app.middleware("http")
    async def enforce_web_pin(request: Request, call_next):
        cfg = getattr(request.app.state, "config", None)
        pin = cfg.web_pin if cfg else ""
        path = request.url.path
        if (pin and path.startswith("/api")
                and not path.startswith(_AUTH_ALLOWED_UNAUTH_PREFIXES)
                and request.method != "OPTIONS"):
            if not _validate_auth_token(_extract_bearer_token(request)):
                return JSONResponse(status_code=401, content={"detail": "未授权或会话已过期"})
        return await call_next(request)

    # ============================================================
    # 静态文件（Web 前端 SPA）
    # ============================================================
    web_dir = Path(__file__).resolve().parent.parent.parent.parent / "web"
    if web_dir.is_dir():
        # 根端点：浏览器返回 index.html，API 客户端返回 JSON
        @app.get("/")
        def root(request: Request):
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
        def root():
            return {
                "name": "影视资源管理器 API",
                "version": "0.1.0",
                "status": "running",
                "docs": "/docs",
            }

    # ============================================================
    # 全局异常兜底：保证错误响应始终为 JSON
    # ============================================================
    from app.core.exceptions import MediaManagerError as _MediaManagerError

    @app.exception_handler(_MediaManagerError)
    def _handle_media_error(request: Request, exc):
        logger.error(f"业务异常 {request.url.path}: {exc}")
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.exception_handler(HTTPException)
    def _handle_http_error(request: Request, exc):
        # 路由层显式抛出的 HTTPException：透传状态码与 detail
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    def _handle_unexpected(request: Request, exc):
        logger.exception(f"未捕获异常 {request.url.path}: {exc!r}")
        return JSONResponse(status_code=500, content={"detail": "内部服务器错误"})

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
