# -*- coding: utf-8 -*-
"""
API 服务器 —— FastAPI 应用创建和 uvicorn 线程管理。

在后台线程中启动 HTTP 服务，监听 config.api_host:config.api_port（默认 127.0.0.1:19527）。
路由接入真实数据库查询；内嵌 Web 前端静态文件服务。
"""

import hmac
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
from app.utils import capabilities
from app.utils import paths as app_paths
from app.api.routes import files, units, messages, events, tags, heic

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

# PIN 尝试节流：4 位 PIN 全空间仅 1 万种，无限流时可秒级穷举
_AUTH_FAIL_WINDOW = 300.0                    # 统计窗口（秒）
_AUTH_FAIL_LIMIT = 5                         # 窗口内允许的失败次数
_auth_failures: dict[str, list[float]] = {}  # client_ip -> 失败时间戳列表
_auth_failures_lock = threading.Lock()


def _auth_retry_after(client_ip: str) -> float:
    """返回该 IP 还需等待的秒数（0 表示未被节流）。"""
    now = time.time()
    with _auth_failures_lock:
        stamps = [t for t in _auth_failures.get(client_ip, [])
                  if now - t < _AUTH_FAIL_WINDOW]
        if stamps:
            _auth_failures[client_ip] = stamps
        else:
            _auth_failures.pop(client_ip, None)
        if len(stamps) >= _AUTH_FAIL_LIMIT:
            return max(1.0, _AUTH_FAIL_WINDOW - (now - stamps[0]))
        return 0.0


def _record_auth_failure(client_ip: str) -> None:
    """记录一次鉴权失败（供节流统计）。"""
    now = time.time()
    with _auth_failures_lock:
        stamps = [t for t in _auth_failures.get(client_ip, [])
                  if now - t < _AUTH_FAIL_WINDOW]
        stamps.append(now)
        _auth_failures[client_ip] = stamps


def _clear_auth_failures(client_ip: str) -> None:
    """鉴权成功后清除该 IP 的失败记录。"""
    with _auth_failures_lock:
        _auth_failures.pop(client_ip, None)


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
    HTTP 认证方案大小写不敏感（RFC 7235），bearer/BEARER 同样接受。
    """
    header = request.headers.get("authorization", "")
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() == "bearer" and credentials:
        return credentials.strip()
    return (request.query_params.get("token") or "").strip()


class AuthVerifyBody(BaseModel):
    """Web PIN 校验请求体。"""
    pin: str = Field(default="", max_length=16)


class ChangePinBody(BaseModel):
    """修改 Web PIN 请求体。"""
    old_pin: str = Field(default="", max_length=16)
    new_pin: str = Field(default="", max_length=16)


def _features_payload(request: Request) -> dict:
    """能力开关（/api/health、/api/features 共用一份口径）。

    前端只认这里返回的开关来显示/隐藏导航，避免"服务端其实没有这个能力、
    前端却仍把入口摆出来"。

    **这些开关来自真实能力探测（`app.utils.capabilities`），不是运行档位**：
    同一份代码里不允许出现"轻量档 → 不注册查重路由"这种功能分叉（会让 HTTP
    契约分叉、前端按档位分支、测试与文档写两遍）。体积差异一律由打包开关决定。
    `profile` 仅作"这是哪个包"的展示标签，不参与任何功能判断。
    """
    cfg = getattr(request.app.state, "config", None)
    runtime = getattr(request.app.state, "runtime", None) or {}
    caps = capabilities.detect(getattr(cfg, "face_model_dir", None) if cfg else None)
    face_on = bool(getattr(cfg, "face_detection_enabled", False)) and caps.face
    return {
        "app": "media-manager",
        "version": "0.1.0",
        "profile": getattr(cfg, "profile", "full") if cfg else "full",
        "dedup": caps.dedup,
        "dedup_video": caps.dedup_video,
        "hash_index": caps.dedup,
        "face": face_on,
        "video": caps.video_thumbnail,
        "heic": caps.pillow_heif,
        "raw": caps.rawpy,
        "scan": True,
        "desktop": caps.qt,
        "capabilities": caps.as_dict(),
        "capability_notes": caps.notes(),
        "data_root": str(runtime.get("data_root") or (cfg.data_root if cfg and cfg.data_root else "")),
        "data_root_status": str(runtime.get("data_root_status") or ""),
        "expected_data_root": str(runtime.get("expected_data_root") or ""),
        "data_root_candidates": [str(c) for c in (runtime.get("candidates") or [])],
        "db_path": str(cfg.db_path) if cfg else "",
        "thumbnail_cache_dir": str(cfg.thumbnail_cache_dir) if cfg else "",
    }


def create_app(config: AppConfig, runtime: Optional[dict] = None) -> FastAPI:
    """创建并配置 FastAPI 应用。

    参数:
        config: 应用配置（`profile` 只是"这是哪个包"的展示标签，不参与功能判断；
            功能可用性一律由 `app.utils.capabilities` 的真实探测决定）。
        runtime: 运行时信息（数据根状态等），供 /api/features 与 /api/health 透出。

    返回:
        配置好的 FastAPI 应用实例。
    """
    # 令牌表是模块级全局：重建应用时清空，避免上一实例签发的令牌继续有效
    _revoke_all_auth_tokens()

    app = FastAPI(
        title="影视资源管理器 API",
        description="本地影视资源管理工具的 HTTP API，供 Web 前端与局域网移动端访问。",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS 中间件：允许局域网内 Android 设备访问
    # 注意：allow_origins=["*"] 与 allow_credentials=True 并存时，Starlette 会
    # 回显任意 Origin，等于允许任意网页跨域读写本机 API；前端用 Bearer 令牌
    # 而非 Cookie，因此关闭凭据模式即可（不影响手机端/SPA）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 将配置存入 app.state，路由可通过 request.app.state.config 访问
    app.state.config = config
    app.state.runtime = dict(runtime or {})

    # 注册路由
    # 查重路由**总是**注册：一套代码不允许按运行档位分叉。包里没有 cv2 / 模型时
    # 由能力探测降级（视频帧与人脸线索不可用），端点本身依然在（契约稳定）。
    from app.api.routes import dedup
    app.include_router(files.router)
    app.include_router(units.router)
    app.include_router(dedup.router)
    app.include_router(messages.router)
    app.include_router(events.router)
    app.include_router(tags.router)
    app.include_router(heic.router)
    from app.api.routes import library, settings as settings_routes
    app.include_router(library.router)
    app.include_router(settings_routes.router)

    # 健康检查（含运行档位与数据根状态：便携版排障第一入口）
    @app.get("/api/health")
    def health(request: Request):
        return {"status": "ok", **_features_payload(request)}

    # 前端启动时拉取：决定导航显示哪些页面、隐藏哪些入口
    @app.get("/api/features")
    def features(request: Request):
        return _features_payload(request)

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
        client_ip = request.client.host if request.client else "unknown"
        retry_after = _auth_retry_after(client_ip)
        if retry_after > 0:
            return JSONResponse(
                status_code=429,
                content={"verified": False,
                         "error": f"尝试过于频繁，请 {int(retry_after)} 秒后再试"},
            )
        # 常量时间比较，避免通过响应耗时逐位推断 PIN
        if hmac.compare_digest(body.pin, cfg.web_pin):
            _clear_auth_failures(client_ip)
            return {"verified": True, "token": _issue_auth_token()}
        _record_auth_failure(client_ip)
        return JSONResponse(status_code=403, content={"verified": False, "error": "密码错误"})

    @app.post("/api/auth/change-pin")
    def auth_change_pin(body: ChangePinBody, request: Request):
        """修改 Web 访问密码。需要提供当前密码验证身份。"""
        cfg = getattr(request.app.state, "config", None)
        if not cfg:
            return JSONResponse(status_code=500, content={"success": False, "error": "配置不可用"})
        client_ip = request.client.host if request.client else "unknown"
        # 如果已设置密码，必须验证旧密码
        if cfg.web_pin:
            retry_after = _auth_retry_after(client_ip)
            if retry_after > 0:
                return JSONResponse(
                    status_code=429,
                    content={"success": False,
                             "error": f"尝试过于频繁，请 {int(retry_after)} 秒后再试"},
                )
            if not hmac.compare_digest(body.old_pin, cfg.web_pin):
                _record_auth_failure(client_ip)
                return JSONResponse(status_code=403, content={"success": False, "error": "当前密码错误"})
            _clear_auth_failures(client_ip)
        new_pin = body.new_pin
        if len(new_pin) > 4:
            return JSONResponse(status_code=400, content={"success": False, "error": "密码最长4位"})
        # 更新配置（frozen dataclass 仅替换 web_pin 字段）
        try:
            new_config = cfg.with_updates(web_pin=new_pin)
            runtime = getattr(request.app.state, "runtime", None) or {}
            # **必须**用入口注入的 config_path：回退到"应用目录"在测试/嵌入式用法里
            # 会写到开发机或用户的真实配置文件上（实测把 desktop/config.json 覆盖过）。
            raw_path = runtime.get("config_path")
            if not raw_path:
                logger.error("拒绝写入配置：运行态未注入 config_path")
                return JSONResponse(status_code=500,
                                    content={"success": False,
                                             "error": "配置未写入：运行态缺少 config_path"})
            config_path = Path(raw_path)
            # profile 只在文件里已显式写过时才落盘（否则会把入口默认档固化），
            # 与 /api/settings 同一口径。
            new_config.to_file(
                config_path,
                omit=() if app_paths.read_explicit_profile(config_path)
                else ("profile",))
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
    # 目录解析必须走 app_paths.web_dir()：PyInstaller 下 __file__ 上溯会落到
    # _internal/ 而不是 _internal/web（打出来的包会没有前端）。
    web_dir = app_paths.web_dir()
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

    def __init__(self, config: AppConfig, runtime: Optional[dict] = None) -> None:
        """初始化 API 服务器。

        参数:
            config: 应用配置。
            runtime: 运行时信息（数据根解析结果），透出给 /api/features。
        """
        self._config = config
        self._runtime = dict(runtime or {})
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[uvicorn.Server] = None
        self._running = False

    def start(self) -> None:
        """在后台线程中启动 API 服务器。"""
        if self._running:
            logger.warning("API 服务器已在运行")
            return

        global _app
        _app = create_app(self._config, self._runtime)

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

    @property
    def is_thread_alive(self) -> bool:
        """服务线程是否还活着。

        无界面入口必须靠这个判断"进程是不是在空转"：uvicorn 绑定端口失败等
        情况下线程会自己退出，而 `is_running` 仍为 True（进程不报错也不服务）。
        """
        return bool(self._thread and self._thread.is_alive())
