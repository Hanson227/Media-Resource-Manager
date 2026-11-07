# -*- coding: utf-8 -*-
"""按需转码服务 —— Web 端视频播放的 H.264/AAC MP4 缓存生成。

为什么需要这一层：
    浏览器（尤其是手机 Safari / 旧版 Chrome）不认 MKV/AVI/HEVC/AC3 等封装与编码，
    直接 `GET /api/files/{id}/stream` 原文件经常是「黑屏只有声音」或干脆放不了。
    这里用 imageio-ffmpeg 自带的 ffmpeg 把**正在播放的那个文件**按需转成
    H.264 + AAC + faststart 的 MP4 放进缓存目录，播放时只读缓存文件，
    不修改、不移动媒体库里的源文件。

缓存契约（与缩略图缓存同一套「按文件身份命名」的思路）：
    缓存目录 = ``Path(config.thumbnail_cache_dir).parent / "transcode_cache"``
    文件名   = ``{file_id}_{size}_{mtime_ns}.mp4``
    ``.meta.json`` 记录源路径 / 大小 / mtime / 状态 / 错误 / 耗时。
    文件名里带 size + mtime：文件被替换后旧缓存自然失效，不需要额外的版本字段。
    只有 ``status == "done"`` **且**输出文件仍存在（且元数据与当前源文件一致）
    才算命中 —— 与缩略图缓存一样，「缓存存在」不等于「缓存有效」。

并发：
    转码是重磁盘/CPU 任务，启动前必须由调用方拿 dedup_gate（扫描/查重/合并/
    HEIC 转换共用同一把门），本模块只负责转码本身，不再重复加锁；
    任务登记（task_registry）与取消标志也由路由层负责，服务层只通过
    ``on_progress`` / ``cancel_check`` 两个回调与调用方协作。

环境：
    ffmpeg 来自 ``imageio-ffmpeg`` 包自带的二进制（不依赖系统 PATH），
    拉取失败时 ``ffmpeg_executable()`` 返回 None，端点降级为 available=false。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.db.engine import DatabaseManager
from app.db import queries as q

logger = logging.getLogger(__name__)

__all__ = [
    "TranscodeSource", "TranscodeCache", "TranscodeStatus", "TranscodeResult",
    "TranscodeError", "STATUS_NONE", "STATUS_RUNNING", "STATUS_DONE",
    "STATUS_FAILED", "STATUS_CANCELED", "CACHE_DIRNAME",
    "ffmpeg_executable", "is_available", "cache_dir", "cache_entry",
    "load_source", "read_meta_status", "is_cache_hit", "get_cached_output",
    "transcode", "clear_cache", "mark_canceled", "parse_duration_seconds",
    "compute_progress_percent", "parse_out_time_us",
    "TRANSCODE_HEIGHT_OPTIONS", "DEFAULT_MAX_HEIGHT", "normalize_max_height",
    "height_tag",
]

# ============================================================
# 状态常量（与 HTTP 契约里的 status 字段一一对应）
# ============================================================
STATUS_NONE = "none"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELED = "canceled"

CACHE_DIRNAME = "transcode_cache"
"""缓存子目录名（与缩略图缓存目录同级）。"""

TRANSCODE_HEIGHT_OPTIONS = (0, 480, 720, 1080, 1440, 2160)
"""允许的转码目标高度（像素）：0 = 原画，其余为最大高度（只降不升）。"""

DEFAULT_MAX_HEIGHT = 1080
"""默认转码高度（用户 2026-11-08 指定：默认 1080p）。"""


def normalize_max_height(value=None, config=None) -> int:
    """把用户/配置里的画质值收敛成合法高度。

    非法值回退默认值而不是报错：旧 config.json 没有这个字段、前端传了
    历史版本不认识的字符串，都不该让播放直接失败。
    """
    if value is None:
        value = getattr(config, "transcode_max_height", DEFAULT_MAX_HEIGHT)
    try:
        height = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_HEIGHT
    return height if height in TRANSCODE_HEIGHT_OPTIONS else DEFAULT_MAX_HEIGHT


def height_tag(height: int) -> str:
    """缓存 key 里的画质标记：0 → ``orig``，1080 → ``1080p``。"""
    return "orig" if int(height) <= 0 else f"{int(height)}p"


_FFMPEG_TIMEOUT_SEC = 30
"""探测时长的子进程超时（秒）；损坏文件也不该把任务卡死。"""

_TERMINATE_WAIT_SEC = 5
"""取消时等待 ffmpeg 自行退出的时间（秒），超时后 kill。"""

_ERROR_TAIL_CHARS = 400
"""meta 里保留的 ffmpeg stderr 尾部长度（足够定位原因，又不至于撑爆 JSON）。"""

# ffmpeg 的 `out_time_ms` 字段名有误导性：它的单位其实是**微秒**（历史遗留命名）。
# 进度换算必须按微秒处理，否则 1 秒会被算成 0.0001%，进度条永远不动。
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

# Windows 下不让 ffmpeg 弹黑框；非 Windows 上该常量不存在，取 0。
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class TranscodeError(Exception):
    """按需转码的输入/前置条件错误（status_code 供 API 转成 HTTP 状态码）。

    与 ``LibraryError`` 同一套约定：路由层把状态码与 detail 原样透出。
    任务体内部的 ffmpeg 失败**不**抛这个异常，而是写进 meta 返回 failed，
    否则后台线程里的异常没人接。
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ============================================================
# 不可变值对象
# ============================================================

@dataclass(frozen=True)
class TranscodeSource:
    """一次转码的源文件身份（文件名/大小/mtime 决定缓存 key）。"""

    file_id: int
    path: Path
    filename: str
    size_bytes: int
    mtime_ns: int
    duration_ms: Optional[int] = None
    """数据库里记录的视频时长（毫秒）；ffmpeg 探测失败时作为进度分母兜底。"""


@dataclass(frozen=True)
class TranscodeCache:
    """某个源文件对应的缓存文件与元数据路径。"""

    output_path: Path
    meta_path: Path
    key: str
    max_height: int = DEFAULT_MAX_HEIGHT
    """该缓存对应的目标高度（0=原画）；meta 与 key 都带上它，切画质不会串缓存。"""

    @property
    def tmp_path(self) -> Path:
        """转码中的临时文件：成功后原子替换为 output_path，失败/取消则删除。"""
        return self.output_path.with_name(f"{self.key}.tmp.mp4")


@dataclass(frozen=True)
class TranscodeStatus:
    """`GET /api/transcode/status` 的领域层结果（stream_url 由路由层拼接）。"""

    available: bool
    status: str = STATUS_NONE
    progress: int = 0
    message: str = ""
    output_path: Optional[Path] = None
    task_id: Optional[str] = None


@dataclass(frozen=True)
class TranscodeResult:
    """`transcode()` 的返回值（后台线程与测试共用）。"""

    file_id: int
    status: str
    message: str = ""
    progress: int = 0
    output_path: Optional[Path] = None
    cached: bool = False
    elapsed_ms: int = 0


# ============================================================
# ffmpeg 可执行文件与缓存目录
# ============================================================

def ffmpeg_executable() -> Optional[str]:
    """返回 imageio-ffmpeg 自带的 ffmpeg 路径；不可用时返回 None。

    刻意不查系统 PATH：项目约定只认包内二进制（无系统 ffmpeg/ffprobe 的机器
    也能工作），系统 ffmpeg 版本差异反而会制造新的兼容问题。
    """
    try:
        import imageio_ffmpeg  # 延迟导入：没装这个包时应用仍能正常启动
        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 — 缺包/下载失败都只影响转码能力
        logger.warning(f"按需转码不可用：未找到 imageio-ffmpeg 的 ffmpeg: {exc}")
        return None
    return exe or None


def is_available() -> bool:
    """服务端是否具备转码能力（imageio-ffmpeg 的 ffmpeg 可执行）。"""
    return ffmpeg_executable() is not None


def cache_dir(config) -> Path:
    """缓存根目录：与缩略图缓存同级，避免源文件所在卷只读时无处可写。"""
    return Path(config.thumbnail_cache_dir).parent / CACHE_DIRNAME


def cache_entry(source: TranscodeSource, config,
                max_height: Optional[int] = None) -> TranscodeCache:
    """由源文件身份 + 目标画质生成缓存路径。

    key = ``{file_id}_{size}_{mtime_ns}_{orig|1080p}``：file_id 会复用
    （删库/重扫），所以必须带 size + mtime；文件被替换后旧 key 自动失效。
    画质也进 key：同一文件切 1080p/720p 各自缓存，互不覆盖。
    """
    height = normalize_max_height(max_height, config)
    key = (f"{source.file_id}_{source.size_bytes}_{source.mtime_ns}"
           f"_{height_tag(height)}")
    root = cache_dir(config)
    return TranscodeCache(
        output_path=root / f"{key}.mp4",
        meta_path=root / f"{key}.meta.json",
        key=key,
        max_height=height,
    )


# ============================================================
# 源文件解析
# ============================================================

def load_source(file_id: int) -> TranscodeSource:
    """按 file_id 取源文件并做安全校验。

    只接受**媒体库内已入库**的 file_id（路径永远来自数据库，不拼接用户输入）；
    记录不存在或磁盘文件不在 → 404；非视频 → 400。

    异常:
        TranscodeError: 400/404（路由层转 HTTPException）。
    """
    if not DatabaseManager.is_ready():
        raise TranscodeError(503, "数据库不可用")
    with DatabaseManager.session() as session:
        media = q.get_file_by_id(session, file_id)
        if media is None:
            raise TranscodeError(404, f"文件不存在: {file_id}")
        path = Path(media.path)
        filename = media.filename
        media_type = media.media_type
        duration_ms = media.duration_ms

    if media_type != "video":
        raise TranscodeError(400, f"仅视频文件支持转码: {filename}")
    try:
        stat = path.stat()
    except OSError:
        raise TranscodeError(404, f"源文件已在磁盘上移除: {filename}")
    if not path.is_file():
        raise TranscodeError(404, f"源文件已在磁盘上移除: {filename}")

    return TranscodeSource(
        file_id=file_id,
        path=path,
        filename=filename,
        size_bytes=int(stat.st_size),
        mtime_ns=int(stat.st_mtime_ns),
        duration_ms=duration_ms,
    )


# ============================================================
# 元数据读写与命中判定
# ============================================================

def _same_path(left: str, right: Path) -> bool:
    """Windows 路径比较（大小写/分隔符不敏感）。"""
    try:
        return os.path.normcase(os.path.abspath(str(left))) == \
            os.path.normcase(os.path.abspath(str(right)))
    except (OSError, ValueError):
        return False


def _read_meta(meta_path: Path) -> Optional[dict]:
    """读 meta JSON；损坏/缺失一律返回 None（缓存视为无效）。"""
    try:
        raw = meta_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _meta_matches_source(meta: dict, source: TranscodeSource,
                         max_height: Optional[int] = None) -> bool:
    """meta 记录的源文件身份/画质是否与当前请求一致。"""
    if max_height is not None:
        try:
            if int(meta.get("max_height") or 0) != int(max_height):
                return False
        except (TypeError, ValueError):
            return False
    return (
        int(meta.get("source_size") or -1) == source.size_bytes
        and int(meta.get("source_mtime_ns") or -1) == source.mtime_ns
        and _same_path(str(meta.get("source_path") or ""), source.path)
    )


def _write_meta(entry: TranscodeCache, source: TranscodeSource, *,
                status: str, progress: int, error: str = "",
                elapsed_ms: int = 0) -> None:
    """原子写 meta（先写临时文件再 os.replace，读方不会读到半个 JSON）。"""
    payload = {
        "file_id": source.file_id,
        "source_path": str(source.path),
        "source_size": source.size_bytes,
        "source_mtime_ns": source.mtime_ns,
        "max_height": int(entry.max_height),
        "status": status,
        "progress": int(progress),
        "error": error or "",
        "elapsed_ms": int(elapsed_ms),
        "output": str(entry.output_path),
        "updated_at": time.time(),
    }
    try:
        entry.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = entry.meta_path.with_name(entry.meta_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, entry.meta_path)
    except OSError as exc:  # 缓存目录不可写不该把任务本身拖崩
        logger.warning(f"写转码 meta 失败 {entry.meta_path}: {exc}")


def is_cache_hit(source: TranscodeSource, config,
                 max_height: Optional[int] = None) -> bool:
    """缓存是否有效：meta.status=done **且**输出存在 **且**源文件/画质一致。"""
    entry = cache_entry(source, config, max_height)
    meta = _read_meta(entry.meta_path)
    if not meta or str(meta.get("status")) != STATUS_DONE:
        return False
    if not _meta_matches_source(meta, source, entry.max_height):
        return False
    try:
        return entry.output_path.is_file() and entry.output_path.stat().st_size > 0
    except OSError:
        return False


def get_cached_output(source: TranscodeSource, config,
                      max_height: Optional[int] = None) -> Optional[Path]:
    """命中缓存时返回输出文件路径，否则 None（stream 端点唯一判据）。"""
    if not is_cache_hit(source, config, max_height):
        return None
    return cache_entry(source, config, max_height).output_path


def read_meta_status(source: TranscodeSource, config,
                     max_height: Optional[int] = None) -> TranscodeStatus:
    """只读缓存目录，得出该文件当前状态（不查任务登记表）。

    路由层会再用 task_registry 的活跃任务覆盖 running 进度；
    这里只负责磁盘事实。meta 里 ``running`` 是进程崩溃前的残影时，
    路由层会把它呈现为「已中断」，而不是永远转圈。

    参数:
        source: 已校验的源文件。
        config: 应用配置（决定缓存目录）。
    """
    available = is_available()
    entry = cache_entry(source, config, max_height)
    meta = _read_meta(entry.meta_path)
    if not meta or not _meta_matches_source(meta, source, entry.max_height):
        return TranscodeStatus(available=available, status=STATUS_NONE,
                               progress=0, message="")

    disk_status = str(meta.get("status") or "")
    progress = max(0, min(100, int(meta.get("progress") or 0)))
    error = str(meta.get("error") or "")
    if disk_status == STATUS_DONE:
        try:
            hit = entry.output_path.is_file() and entry.output_path.stat().st_size > 0
        except OSError:
            hit = False
        if hit:
            return TranscodeStatus(available=available, status=STATUS_DONE,
                                   progress=100, message="转码完成",
                                   output_path=entry.output_path)
        # 「done 但没有输出」= 缓存被外部清理，按未转码处理（不是失败）
        return TranscodeStatus(available=available, status=STATUS_NONE,
                               progress=0, message="")
    if disk_status == STATUS_RUNNING:
        return TranscodeStatus(available=available, status=STATUS_RUNNING,
                               progress=progress, message="转码中",
                               output_path=None)
    if disk_status == STATUS_FAILED:
        return TranscodeStatus(available=available, status=STATUS_FAILED,
                               progress=0,
                               message=error or "转码失败")
    if disk_status == STATUS_CANCELED:
        return TranscodeStatus(available=available, status=STATUS_CANCELED,
                               progress=progress, message="已取消")
    return TranscodeStatus(available=available, status=STATUS_NONE,
                           progress=0, message="")


def mark_canceled(source: TranscodeSource, config, *,
                  message: str = "已取消",
                  max_height: Optional[int] = None) -> None:
    """把残留的 running meta 标成 canceled（任务已不在登记表时用）。"""
    entry = cache_entry(source, config, max_height)
    meta = _read_meta(entry.meta_path) or {}
    _write_meta(entry, source, status=STATUS_CANCELED,
                progress=int(meta.get("progress") or 0), error="",
                elapsed_ms=int(meta.get("elapsed_ms") or 0))


def clear_cache(file_id: int, config,
                max_height: Optional[int] = None) -> int:
    """删除某个文件的转码缓存，返回删除条目数。

    默认清理该 file_id 的**全部画质**；传了 max_height 就只清这个画质
    （force 重转当前画质时不该顺带删掉用户已经能看的 720p 缓存）。
    除了当前 key，还清理同一 file_id 的历史 key（mtime/size 变过的旧缓存）：
    否则每次文件被替换都会留下一份永远没人认领的 mb 级孤儿。
    """
    try:
        load_source(file_id)
    except TranscodeError:
        return 0
    root = cache_dir(config)
    if not root.is_dir():
        return 0
    tag = None if max_height is None else height_tag(normalize_max_height(max_height, config))
    removed = 0
    for path in root.glob(f"{file_id}_*"):
        if not path.is_file():
            continue
        if tag is not None and f"_{tag}." not in path.name:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning(f"清理转码缓存失败 {path}: {exc}")
    return removed


# ============================================================
# 纯解析函数（单测直接调用，不依赖 ffmpeg）
# ============================================================

def parse_duration_seconds(stderr_text: str) -> Optional[float]:
    """从 ``ffmpeg -i <input>`` 的 stderr 里解析 ``Duration: HH:MM:SS.xx``。

    没有 ffprobe，只能用 ffmpeg 打印的输入信息；解析失败返回 None
    （此时进度只能显示不确定状态，最后统一到 100）。
    """
    match = _DURATION_RE.search(stderr_text or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return total if total > 0 else None


def parse_out_time_us(line: str) -> Optional[int]:
    """解析 ``-progress`` 输出的 ``out_time_ms=<微秒>`` 行（字段名有误导性）。"""
    text = (line or "").strip()
    if not text.startswith("out_time_ms="):
        return None
    value = text.partition("=")[2].strip()
    if not value.isdigit():
        return None
    return int(value)


def compute_progress_percent(out_time_us: int, total_seconds: Optional[float]) -> int:
    """把已转码微秒数换算成 0–99 的整数百分比。

    封顶 99：``progress=end`` 才是 100 —— 否则最后一帧会把进度条停在 100
    却还没写盘完成，前端会以为已经可以播了。
    """
    if not total_seconds or total_seconds <= 0 or out_time_us <= 0:
        return 0
    percent = out_time_us / 1_000_000.0 / total_seconds * 100.0
    return max(0, min(99, int(percent)))


# ============================================================
# ffmpeg 执行
# ============================================================

def _probe_duration(executable: str, source_path: Path) -> Optional[float]:
    """用 ``ffmpeg -i`` 的输出解析总时长（无 ffprobe 环境的唯一办法）。"""
    command = [executable, "-hide_banner", "-i", str(source_path)]
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FFMPEG_TIMEOUT_SEC,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"探测视频时长失败 {source_path}: {exc}")
        return None
    return parse_duration_seconds(proc.stderr or "")


def _tail(text: str, limit: int = _ERROR_TAIL_CHARS) -> str:
    """取最后一段非空错误文本。"""
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def _terminate(proc: subprocess.Popen) -> None:
    """先 terminate，宽限期后仍不退出则 kill（ffmpeg 会处理 SIGTERM）。"""
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 — 进程可能已退出
        return
    try:
        proc.wait(timeout=_TERMINATE_WAIT_SEC)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
            proc.wait(timeout=_TERMINATE_WAIT_SEC)
        except Exception:  # noqa: BLE001
            pass


def _emit_progress(on_progress: Optional[Callable], percent: int,
                   message: str) -> None:
    """安全地回调进度（回调异常不能弄死转码线程）。"""
    if on_progress is None:
        return
    try:
        on_progress(int(percent), message)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"转码进度回调失败: {exc}")


def _cleanup_stale_entries(source: TranscodeSource, keep: TranscodeCache,
                           config) -> None:
    """新转码开始前清掉同一 file_id、同一画质的旧 key。

    只清同一画质：用户切到 720p 重转时，已经转好的 1080p 缓存必须保留，
    否则每次切画质都把上一份几 GB 的缓存删掉，等于永远只能留一个画质。
    """
    root = cache_dir(config)
    if not root.is_dir():
        return
    tag = height_tag(keep.max_height)
    for path in root.glob(f"{source.file_id}_*"):
        if path.name in (keep.output_path.name, keep.meta_path.name) or not path.is_file():
            continue
        if f"_{tag}." not in path.name:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(f"清理旧转码缓存失败 {path}: {exc}")


# ============================================================
# 主流程
# ============================================================

def transcode(file_id: int, config,
              on_progress: Optional[Callable[[int, str], None]] = None,
              cancel_check: Optional[Callable[[], bool]] = None,
              max_height: Optional[int] = None) -> TranscodeResult:
    """把指定文件转成 H.264/AAC/faststart 的 MP4 缓存。

    参数:
        file_id: 媒体库内的文件 ID（路径只从数据库取，不拼接用户输入）。
        config: 应用配置（缓存目录由 ``config.thumbnail_cache_dir`` 推导）。
        on_progress: 进度回调 ``(percent 0-99, message)``；完成时单独回调 100。
        cancel_check: 取消探针，返回 True 时 terminate ffmpeg 并返回 canceled。
        max_height: 目标最大高度（0=原画）；None 时读 config.transcode_max_height。

    返回:
        TranscodeResult（status = done / failed / canceled）；不抛 ffmpeg 异常，
        失败信息写进返回值和 meta，后台线程据此更新任务登记表。

    异常:
        TranscodeError: 文件不存在/非视频等前置条件错误（路由层转 HTTP）。
    """
    started = time.monotonic()
    height = normalize_max_height(max_height, config)
    source = load_source(file_id)

    # 命中缓存直接返回：已转好的文件不因为「这次是强制刷新」等理由重跑，
    # force 由路由层先 clear_cache 再调用本函数。
    if is_cache_hit(source, config, height):
        entry = cache_entry(source, config, height)
        _emit_progress(on_progress, 100, "命中转码缓存")
        return TranscodeResult(
            file_id=file_id, status=STATUS_DONE, message="命中转码缓存",
            progress=100, output_path=entry.output_path, cached=True,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    entry = cache_entry(source, config, height)
    executable = ffmpeg_executable()
    if not executable:
        message = "服务端缺少 ffmpeg，无法转码"
        _write_meta(entry, source, status=STATUS_FAILED, progress=0, error=message)
        return TranscodeResult(
            file_id=file_id, status=STATUS_FAILED, message=message,
            progress=0, elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        entry.output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        message = f"无法创建转码缓存目录: {exc}"
        logger.error(message)
        return TranscodeResult(
            file_id=file_id, status=STATUS_FAILED, message=message,
            progress=0, elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    _cleanup_stale_entries(source, entry, config)
    # 先把 running 落盘：进程若在 ffmpeg 中途被杀，meta 能留下痕迹，
    # 路由层据此提示「上次转码被中断」而不是永远 running。
    _write_meta(entry, source, status=STATUS_RUNNING, progress=0)
    _emit_progress(on_progress, 0, "准备转码")

    if cancel_check is not None and _safe_cancel(cancel_check):
        _write_meta(entry, source, status=STATUS_CANCELED, progress=0,
                    elapsed_ms=int((time.monotonic() - started) * 1000))
        return TranscodeResult(
            file_id=file_id, status=STATUS_CANCELED, message="已取消",
            progress=0, elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    duration = _probe_duration(executable, source.path)
    if duration is None and source.duration_ms:
        # ffmpeg 解析不出 Duration 时退回数据库里记录的视频时长
        duration = float(source.duration_ms) / 1000.0

    command = [
        executable, "-y", "-nostdin",
        "-i", str(source.path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",  # 手机/旧浏览器只认 yuv420p，10bit 源必须转 8bit
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        str(entry.tmp_path),
    ]
    if height > 0:
        # 只降不升：min(ih, height) 保证低分辨率源不被放大。
        # 逗号是 filtergraph 的分隔符，表达式内的逗号必须转义。
        scale = f"scale=-2:min(ih\\,{height})"
        command[command.index("-c:v"):command.index("-c:v")] = ["-vf", scale]

    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        message = f"启动 ffmpeg 失败: {exc}"
        logger.error(message)
        entry.tmp_path.unlink(missing_ok=True)
        _write_meta(entry, source, status=STATUS_FAILED, progress=0, error=message,
                    elapsed_ms=int((time.monotonic() - started) * 1000))
        return TranscodeResult(
            file_id=file_id, status=STATUS_FAILED, message=message,
            progress=0, elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # stderr 必须并发排空：stdout 与 stderr 双管道且只读 stdout 时，
    # stderr 写满管道缓冲区（64KB）会让 ffmpeg 阻塞，转码永远不结束。
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_chunks.append(line)
        except Exception:  # noqa: BLE001 — 进程被终止时读取会中断
            pass

    drain_thread = threading.Thread(
        target=_drain_stderr, daemon=True, name=f"transcode-stderr-{file_id}")
    drain_thread.start()

    canceled = False
    progress = 0
    try:
        for raw_line in proc.stdout:  # type: ignore[union-attr]
            if cancel_check is not None and _safe_cancel(cancel_check):
                canceled = True
                break
            out_time_us = parse_out_time_us(raw_line)
            if out_time_us is not None:
                progress = compute_progress_percent(out_time_us, duration)
                _emit_progress(on_progress, progress, f"转码中 {progress}%")
                continue
            if raw_line.strip() == "progress=end":
                progress = 100
                _emit_progress(on_progress, progress, "转码收尾")
    finally:
        if canceled:
            _terminate(proc)
        drain_thread.join(timeout=_TERMINATE_WAIT_SEC)
        if proc.poll() is None:
            _terminate(proc)
        proc.wait()

    stderr_text = "".join(stderr_chunks)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if canceled:
        try:
            entry.tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"删除取消的临时文件失败 {entry.tmp_path}: {exc}")
        _emit_progress(on_progress, progress, "已取消")
        _write_meta(entry, source, status=STATUS_CANCELED, progress=progress,
                    error="", elapsed_ms=elapsed_ms)
        return TranscodeResult(
            file_id=file_id, status=STATUS_CANCELED, message="已取消",
            progress=progress, elapsed_ms=elapsed_ms,
        )

    returncode = proc.returncode
    if returncode != 0:
        error = _tail(stderr_text) or f"ffmpeg 退出码 {returncode}"
        logger.warning(f"转码失败 file_id={file_id} rc={returncode}: {error}")
        try:
            entry.tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"删除失败任务的临时文件失败 {entry.tmp_path}: {exc}")
        _write_meta(entry, source, status=STATUS_FAILED, progress=0, error=error,
                    elapsed_ms=elapsed_ms)
        return TranscodeResult(
            file_id=file_id, status=STATUS_FAILED,
            message="转码失败（详见服务端日志）", progress=0,
            elapsed_ms=elapsed_ms,
        )

    # 原子发布：tmp → 正式缓存名，避免播放端点读到半截文件。
    try:
        if not entry.tmp_path.is_file() or entry.tmp_path.stat().st_size <= 0:
            raise OSError("ffmpeg 输出为空")
        os.replace(entry.tmp_path, entry.output_path)
    except OSError as exc:
        message = f"发布转码缓存失败: {exc}"
        logger.error(message)
        try:
            entry.tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        _write_meta(entry, source, status=STATUS_FAILED, progress=0, error=message,
                    elapsed_ms=elapsed_ms)
        return TranscodeResult(
            file_id=file_id, status=STATUS_FAILED, message=message,
            progress=0, elapsed_ms=elapsed_ms,
        )

    _emit_progress(on_progress, 100, "转码完成")
    _write_meta(entry, source, status=STATUS_DONE, progress=100, error="",
                elapsed_ms=elapsed_ms)
    logger.info(
        f"转码完成 file_id={file_id} -> {entry.output_path} ({elapsed_ms}ms)")
    return TranscodeResult(
        file_id=file_id, status=STATUS_DONE, message="转码完成", progress=100,
        output_path=entry.output_path, cached=False, elapsed_ms=elapsed_ms,
    )


def _safe_cancel(cancel_check: Callable[[], bool]) -> bool:
    """取消探针异常时按「不取消」处理，避免把转码线程弄崩。"""
    try:
        return bool(cancel_check())
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"转码取消探针失败: {exc}")
        return False
