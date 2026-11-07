# -*- coding: utf-8 -*-
"""按需转码后端测试 —— 缓存键 / 进度解析 / 失败落库 / HTTP 契约。

运行方式（不依赖 test_flow 的全局库）：
    python desktop/tests/test_transcode.py

设计原则：
- 自带临时 DB + 临时媒体目录 + 临时缓存目录，结束全部清理，绝不碰真实库；
- 绝大多数用例用假的 Popen / 纯函数，不依赖真实 ffmpeg；
- 只用 imageio-ffmpeg 生成一个 1 秒的小视频跑一次**真实**转码，
  验证端到端（生成 + 转码合计控制在 10 秒内）。
"""

import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Windows 控制台默认 GBK，打印中文/箭头会 UnicodeEncodeError，统一切到 UTF-8
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

from config import AppConfig
from app.db.engine import DatabaseManager
from app.db.migrations import init_db
from app.db import queries as q
from app.services import transcode_service as ts
from app.services.dedup_gate import is_dedup_busy

# ============================================================
# 测试工具
# ============================================================

_passed = 0
_failed = 0


def check(desc: str, condition: bool) -> None:
    """记录一条断言（打印 PASS/FAIL，不抛异常，一次跑完看全貌）。"""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [PASS] {desc}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc}")


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


# ============================================================
# 临时环境（模块级，main 结束统一清理）
# ============================================================

_TMP = Path(tempfile.mkdtemp(prefix="transcode_test_"))
_MEDIA_DIR = _TMP / "media"
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
_CACHE_DIR = _TMP / "data" / ".thumbnails"
_CONFIG = AppConfig().with_updates(
    db_path=_TMP / "data" / "transcode_test.db",
    thumbnail_cache_dir=_CACHE_DIR,
    web_pin="",
)
_ROOT_ID = 0
_UNIT_ID = 0


def _setup_db() -> None:
    """建临时库 + 一个媒体库根/单元（MediaFile.resource_unit_id 非空 FK）。"""
    global _ROOT_ID, _UNIT_ID
    init_db(_CONFIG.db_path)
    with DatabaseManager.session() as session:
        root = q.add_library_root(session, str(_MEDIA_DIR))
        unit = q.create_unit(
            session, path=str(_MEDIA_DIR), name="转码测试单元",
            library_root_id=root.id, is_manual=True, file_count=0, total_size=0,
        )
        _ROOT_ID = root.id
        _UNIT_ID = unit.id


def _insert_file(path: Path, media_type: str = "video",
                 duration_ms=None) -> int:
    """把磁盘文件登记进临时库，返回 file_id。"""
    with DatabaseManager.session() as session:
        media = q.insert_media_file(
            session,
            path=str(path),
            filename=path.name,
            extension=path.suffix.lower().lstrip("."),
            media_type=media_type,
            size_bytes=path.stat().st_size if path.exists() else 0,
            resource_unit_id=_UNIT_ID,
            duration_ms=duration_ms,
        )
        return media.id


def _write_bytes(path: Path, data: bytes = b"not a real video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _entry_for(file_id: int) -> ts.TranscodeCache:
    """按已登记文件推导缓存路径（测试用）。"""
    return ts.cache_entry(ts.load_source(file_id), _CONFIG)


def _generate_test_video(path: Path, seconds: float = 1.0) -> bool:
    """用自带的 ffmpeg 生成 1 秒彩色测试视频（有音频轨道，验证 AAC 分支）。"""
    exe = ts.ffmpeg_executable()
    if not exe:
        return False
    command = [
        exe, "-y", "-nostdin",
        "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k",
        str(path),
    ]
    try:
        proc = subprocess.run(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=ts._CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and path.is_file() and path.stat().st_size > 0


class _FakePopen:
    """替身 Popen：伪造 ffmpeg 的 stdout（progress 协议）与 stderr。"""

    def __init__(self, returncode: int = 0, stdout_lines=None,
                 stderr_lines=None):
        self.returncode = returncode
        self.stdout = iter(list(stdout_lines or []))
        self.stderr = iter(list(stderr_lines or []))
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class _PatchPopen:
    """临时替换 subprocess.Popen + 时长探测（退出时必定还原，不污染其他用例）。

    注意：``subprocess.run`` 内部也用 ``Popen``，只换掉 Popen 会让时长探测
    拿到假对象；所以这里同时把 ``_probe_duration`` 换成「探测不到」，
    让服务走 ``duration_ms`` 兜底。真实探测路径由真实转码用例覆盖。
    """

    def __init__(self, factory):
        self._factory = factory
        self._original_popen = None
        self._original_probe = None

    def __enter__(self):
        self._original_popen = subprocess.Popen
        self._original_probe = ts._probe_duration
        subprocess.Popen = self._factory  # type: ignore[assignment]
        ts._probe_duration = lambda executable, path: None  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info):
        subprocess.Popen = self._original_popen  # type: ignore[assignment]
        ts._probe_duration = self._original_probe  # type: ignore[assignment]
        return False


# ============================================================
# 测试 1: 纯函数（不碰磁盘/进程）
# ============================================================

def test_cache_key() -> None:
    """缓存键：{file_id}_{size}_{mtime_ns}，目录与缩略图缓存同级。"""
    section("测试 1: 缓存键与目录")
    source = ts.TranscodeSource(
        file_id=42, path=Path("D:/media/a.mkv"), filename="a.mkv",
        size_bytes=1234567, mtime_ns=1700000000123456789,
    )
    check("缓存根目录在缩略图目录的父级下",
          ts.cache_dir(_CONFIG) == Path(_CONFIG.thumbnail_cache_dir).parent / "transcode_cache")
    entry = ts.cache_entry(source, _CONFIG)
    check("缓存 key = file_id_size_mtime_ns",
          entry.key == "42_1234567_1700000000123456789")
    check("输出文件名为 {key}.mp4", entry.output_path.name == entry.key + ".mp4")
    check("meta 文件名为 {key}.meta.json",
          entry.meta_path.name == entry.key + ".meta.json")
    check("临时文件与输出同目录（保证 os.replace 原子）",
          entry.tmp_path.parent == entry.output_path.parent)
    check("临时文件扩展名仍为 .mp4（ffmpeg 不吃未知扩展名）",
          entry.tmp_path.suffix == ".mp4")

    # 同一文件重复计算必须得到同一个 key（否则每次都重转）
    again = ts.cache_entry(source, _CONFIG)
    check("同一源文件重复计算 key 稳定", again == entry)

    # size / mtime 任一变化 -> 新 key（文件被替换后旧缓存自然失效）
    replaced = ts.TranscodeSource(
        file_id=42, path=source.path, filename=source.filename,
        size_bytes=source.size_bytes + 1, mtime_ns=source.mtime_ns,
    )
    check("文件大小变化 -> 新缓存 key",
          ts.cache_entry(replaced, _CONFIG).key != entry.key)
    touched = ts.TranscodeSource(
        file_id=42, path=source.path, filename=source.filename,
        size_bytes=source.size_bytes, mtime_ns=source.mtime_ns + 1,
    )
    check("mtime 变化 -> 新缓存 key",
          ts.cache_entry(touched, _CONFIG).key != entry.key)


def test_parse_helpers() -> None:
    """进度解析：Duration、out_time_ms（微秒）、百分比换算。"""
    section("测试 2: 进度解析纯函数")

    stderr_sample = (
        "ffmpeg version 9.0.2 Copyright (c) 2000-2025 the FFmpeg developers\n"
        "  Input #0, matroska,webm, from 'a.mkv':\n"
        "    Duration: 00:02:05.50, start: 0.000000, bitrate: 1234 kb/s\n"
        "  Stream #0:0: Video: h264\n"
    )
    check("解析 Duration: 00:02:05.50 = 125.5 秒",
          ts.parse_duration_seconds(stderr_sample) == 125.5)
    check("无 Duration 时返回 None", ts.parse_duration_seconds("no duration here") is None)
    check("空文本返回 None", ts.parse_duration_seconds("") is None)
    check("Duration 00:00:00.00 视为不可用",
          ts.parse_duration_seconds("Duration: 00:00:00.00") is None)

    check("out_time_ms 按微秒解析（1920000us = 1.92s）",
          ts.parse_out_time_us("out_time_ms=1920000") == 1920000)
    check("非 out_time_ms 行返回 None", ts.parse_out_time_us("frame=12") is None)
    check("缺值/非数字返回 None", ts.parse_out_time_us("out_time_ms=N/A") is None)

    check("50% 进度换算为 50",
          ts.compute_progress_percent(1_000_000, 2.0) == 50)
    check("进度封顶 99（100 只由 progress=end 触发）",
          ts.compute_progress_percent(9_000_000, 2.0) == 99)
    check("总时长未知时进度为 0",
          ts.compute_progress_percent(1_000_000, None) == 0)
    check("负/零输入不崩",
          ts.compute_progress_percent(0, 2.0) == 0 and
          ts.compute_progress_percent(-5, 2.0) == 0)


def test_ffmpeg_availability() -> None:
    """imageio-ffmpeg 自带的 ffmpeg 应可解析（环境事实）。"""
    section("测试 3: ffmpeg 可用性")
    exe = ts.ffmpeg_executable()
    check("ffmpeg_executable() 返回可执行路径", bool(exe))
    check("路径文件存在", bool(exe) and Path(exe).is_file())
    check("is_available() 与之一致", ts.is_available() == bool(exe))
    # 缺包降级：探测函数返回 None 时 is_available() 必须为 False（端点靠它报 available）
    import imageio_ffmpeg
    original = imageio_ffmpeg.get_ffmpeg_exe
    try:
        imageio_ffmpeg.get_ffmpeg_exe = lambda: None
        check("ffmpeg 缺失时 is_available() 为 False", ts.is_available() is False)
    finally:
        imageio_ffmpeg.get_ffmpeg_exe = original
    check("恢复后 available 为 True", ts.is_available() is True)


# ============================================================
# 测试 4: 失败 / 取消（假 Popen，不跑真实 ffmpeg）
# ============================================================

def test_transcode_failure_writes_meta() -> None:
    """ffmpeg 非零退出：返回 failed、meta 落 failed+error、tmp 清理。"""
    section("测试 4: ffmpeg 失败落库")
    bad = _write_bytes(_MEDIA_DIR / "broken.mp4", b"definitely not a video")
    file_id = _insert_file(bad, "video")
    entry = _entry_for(file_id)

    fake = _FakePopen(
        returncode=1,
        stdout_lines=["out_time_ms=500000", "progress=continue"],
        stderr_lines=["Invalid data found when processing input\n"],
    )
    with _PatchPopen(lambda command, **kwargs: fake):
        result = ts.transcode(file_id, _CONFIG)

    check("失败返回 status=failed", result.status == ts.STATUS_FAILED)
    check("失败不产出输出文件",
          not entry.output_path.exists())
    check("临时文件被清理", not entry.tmp_path.exists())
    meta = json.loads(entry.meta_path.read_text(encoding="utf-8"))
    check("meta.status = failed", meta["status"] == "failed")
    check("meta.error 含 ffmpeg 原因", "Invalid data" in meta["error"])
    check("meta 记录了源文件身份（大小/路径/mtime）",
          meta["source_size"] == bad.stat().st_size
          and meta["source_path"] == str(bad)
          and int(meta["source_mtime_ns"]) == bad.stat().st_mtime_ns)
    check("失败状态不会被误判为缓存命中", ts.is_cache_hit(ts.load_source(file_id), _CONFIG) is False)

    with DatabaseManager.session() as session:
        exists = q.get_file_by_id(session, file_id) is not None
    check("失败不影响库里的文件记录", exists)


def test_transcode_canceled() -> None:
    """协作式取消：terminate ffmpeg、返回 canceled、meta 落 canceled。"""
    section("测试 5: 取消转码")
    src = _write_bytes(_MEDIA_DIR / "cancel_me.mp4", b"garbage but never really decoded")
    file_id = _insert_file(src, "video")
    entry = _entry_for(file_id)

    fake = _FakePopen(
        returncode=0,
        stdout_lines=["out_time_ms=250000", "out_time_ms=500000", "progress=continue"],
        stderr_lines=[],
    )
    state = {"calls": 0}

    def cancel_check() -> bool:
        state["calls"] += 1
        return state["calls"] > 1  # 第一次检查放行，第二次要求取消

    with _PatchPopen(lambda command, **kwargs: fake):
        result = ts.transcode(file_id, _CONFIG, cancel_check=cancel_check)

    check("取消返回 status=canceled", result.status == ts.STATUS_CANCELED)
    check("取消时已要求 ffmpeg terminate", fake.terminated is True)
    check("取消后不产出输出文件", not entry.output_path.exists())
    meta = json.loads(entry.meta_path.read_text(encoding="utf-8"))
    check("meta.status = canceled", meta["status"] == "canceled")
    check("取消状态不会被误判为缓存命中",
          ts.is_cache_hit(ts.load_source(file_id), _CONFIG) is False)


# ============================================================
# 测试 6: 已 done 命中不重跑
# ============================================================

def test_done_cache_hit_skips_ffmpeg() -> None:
    """meta=done 且输出存在：直接命中，绝不启动 ffmpeg。"""
    section("测试 6: 命中缓存不重跑")
    src = _write_bytes(_MEDIA_DIR / "cached.mp4", b"fake source bytes for cache hit")
    file_id = _insert_file(src, "video")
    source = ts.load_source(file_id)
    entry = ts.cache_entry(source, _CONFIG)
    entry.output_path.parent.mkdir(parents=True, exist_ok=True)
    entry.output_path.write_bytes(b"FAKE_MP4_PAYLOAD")
    ts._write_meta(entry, source, status=ts.STATUS_DONE, progress=100)

    check("is_cache_hit 判定为命中", ts.is_cache_hit(source, _CONFIG) is True)
    check("get_cached_output 返回输出路径",
          ts.get_cached_output(source, _CONFIG) == entry.output_path)

    def _explode(command, **kwargs):
        raise AssertionError("命中缓存时不应启动 ffmpeg")

    with _PatchPopen(_explode):
        result = ts.transcode(file_id, _CONFIG)

    check("命中返回 status=done", result.status == ts.STATUS_DONE)
    check("命中标记 cached=True", result.cached is True)
    check("进度直接为 100", result.progress == 100)
    check("输出路径指向缓存文件", result.output_path == entry.output_path)

    # 文件被替换（size 变了）→ 缓存必须失效，不能拿旧 MP4 播新内容
    src.write_bytes(b"fake source bytes for cache hit -- now longer")
    invalidated = ts.is_cache_hit(ts.load_source(file_id), _CONFIG)
    check("源文件被替换后缓存失效", invalidated is False)


def test_clear_cache() -> None:
    """dispose 缓存：按 file_id 清掉所有历史 key。"""
    section("测试 7: 清理缓存")
    src = _write_bytes(_MEDIA_DIR / "clear_me.mp4", b"clear me source")
    file_id = _insert_file(src, "video")
    root = ts.cache_dir(_CONFIG)
    root.mkdir(parents=True, exist_ok=True)
    stale = root / f"{file_id}_999_111.mp4"
    stale.write_bytes(b"old")
    other = root / "999999_1_1.mp4"
    other.write_bytes(b"other")

    removed = ts.clear_cache(file_id, _CONFIG)
    check("返回删除条目数", removed == 1)
    check("目标文件的旧缓存被删", not stale.exists())
    check("其他文件的缓存不受影响", other.exists())
    other.unlink()
    check("未知 file_id 清理返回 0", ts.clear_cache(987654, _CONFIG) == 0)


# ============================================================
# 测试 8: 真实转码（1 秒测试视频，端到端）
# ============================================================

_REAL_VIDEO_ID = 0
_REAL_ELAPSED = 0.0


def test_real_transcode() -> None:
    """用 imageio-ffmpeg 生成 1 秒视频并真实转码一次（H.264/AAC/faststart）。"""
    global _REAL_VIDEO_ID, _REAL_ELAPSED
    section("测试 8: 真实转码（1 秒测试视频）")
    video = _MEDIA_DIR / "real_clip.mp4"
    started = time.monotonic()
    generated = _generate_test_video(video, seconds=1.0)
    check("生成 1 秒测试视频", generated)
    if not generated:
        _REAL_ELAPSED = time.monotonic() - started
        return

    _REAL_VIDEO_ID = _insert_file(video, "video", duration_ms=1000)
    updates = []

    def on_progress(percent: int, message: str) -> None:
        updates.append((percent, message))

    result = ts.transcode(_REAL_VIDEO_ID, _CONFIG, on_progress=on_progress)
    _REAL_ELAPSED = time.monotonic() - started

    check("真实转码返回 done", result.status == ts.STATUS_DONE)
    check("输出文件存在且非空",
          bool(result.output_path) and result.output_path.is_file()
          and result.output_path.stat().st_size > 0)
    check("输出是 MP4（ftyp 头）",
          bool(result.output_path) and result.output_path.read_bytes()[4:8] == b"ftyp")
    check("进度回调收到多次更新", len(updates) >= 2)
    check("进度回调最终到达 100",
          bool(updates) and updates[-1][0] == 100)
    check("进度单调不回退",
          all(b[0] >= a[0] for a, b in zip(updates, updates[1:])))
    meta = json.loads(_entry_for(_REAL_VIDEO_ID).meta_path.read_text(encoding="utf-8"))
    check("meta.status = done", meta["status"] == "done")
    check("meta 记录耗时 > 0", int(meta["elapsed_ms"]) > 0)
    check(f"生成+转码耗时 < 10 秒（实测 {_REAL_ELAPSED:.2f}s）", _REAL_ELAPSED < 10.0)

    # faststart：moov 应在 mdat 之前（浏览器可边下边播）
    data = result.output_path.read_bytes()
    check("moov 在 mdat 之前（faststart 生效）",
          data.find(b"moov") != -1 and data.find(b"mdat") != -1
          and data.find(b"moov") < data.find(b"mdat"))


# ============================================================
# 测试 9: HTTP 契约（TestClient）
# ============================================================

def test_api_status_and_stream() -> None:
    """status / stream / start / cancel / active 的字段与状态码。"""
    section("测试 9: HTTP 契约")
    if not _REAL_VIDEO_ID:
        check("缺少真实转码产物，跳过 HTTP 契约测试", True)
        return

    from app.api.server import create_app
    client = TestClient(create_app(_CONFIG))

    data = client.get(f"/api/transcode/status?file_id={_REAL_VIDEO_ID}").json()
    check("status 返回 available", data["available"] is True)
    check("status 为 done", data["status"] == "done")
    check("progress 为 100", data["progress"] == 100)
    check("done 时 stream_url 非空且指向 /api/transcode/stream",
          isinstance(data["stream_url"], str) and data["stream_url"].startswith("/api/transcode/stream"))
    check("响应字段集稳定",
          set(data) == {"available", "status", "progress", "message", "stream_url"})

    stream = client.get(f"/api/transcode/stream?file_id={_REAL_VIDEO_ID}")
    check("stream 返回 200", stream.status_code == 200)
    check("stream Content-Type 为 video/mp4",
          stream.headers.get("content-type", "").startswith("video/mp4"))
    check("stream 带 inline 处置头",
          "inline" in stream.headers.get("content-disposition", ""))
    check("stream 带私有缓存头",
          "private" in stream.headers.get("cache-control", ""))
    check("stream 返回非空字节流", len(stream.content) > 0)

    # 未转码的文件：status=none，stream 404
    pending = _write_bytes(_MEDIA_DIR / "pending.mp4", b"pending video bytes")
    pending_id = _insert_file(pending, "video")
    pending_data = client.get(f"/api/transcode/status?file_id={pending_id}").json()
    check("未转码文件 status=none", pending_data["status"] == "none")
    check("未转码文件 stream_url 为 None", pending_data["stream_url"] is None)
    check("未转码文件 stream 返回 404",
          client.get(f"/api/transcode/stream?file_id={pending_id}").status_code == 404)

    # 不存在的 file_id -> 404；非视频 -> 400（只接受媒体库内的视频记录）
    check("未知 file_id 的 status 返回 404",
          client.get("/api/transcode/status?file_id=99999999").status_code == 404)
    image = _MEDIA_DIR / "photo.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    image_id = _insert_file(image, "image")
    check("图片文件的 status 返回 400",
          client.get(f"/api/transcode/status?file_id={image_id}").status_code == 400)
    check("图片文件的 stream 返回 400",
          client.get(f"/api/transcode/stream?file_id={image_id}").status_code == 400)

    # ffmpeg 缺失时 available=False（前端据此隐藏转码入口，而不是报错）
    original = ts.ffmpeg_executable
    try:
        ts.ffmpeg_executable = lambda: None
        missing = client.get(f"/api/transcode/status?file_id={pending_id}").json()
        check("无 ffmpeg 时 available=false", missing["available"] is False)
        blocked = client.post("/api/transcode/start", json={"file_id": pending_id})
        check("无 ffmpeg 时 start 返回 503", blocked.status_code == 503)
    finally:
        ts.ffmpeg_executable = original

    # 已 done：start（非 force）直接回 done，不重复起任务
    reused = client.post("/api/transcode/start",
                         json={"file_id": _REAL_VIDEO_ID, "force": False})
    check("已转码文件 start 返回 200", reused.status_code == 200)
    reused_data = reused.json()
    check("已转码文件 start 返回 done", reused_data["status"] == "done")
    check("已转码文件 start 不新建任务（task_id 为 None）",
          reused_data["task_id"] is None)
    check("start 响应字段稳定",
          set(reused_data) == {"success", "task_id", "status", "message"})

    # 取消：没有在跑的任务 -> success=False（不报 500）
    canceled = client.post("/api/transcode/cancel", json={"file_id": _REAL_VIDEO_ID})
    check("空闲时 cancel 返回 200", canceled.status_code == 200)
    check("空闲时 cancel success=False", canceled.json()["success"] is False)
    check("cancel 响应字段稳定",
          set(canceled.json()) == {"success", "message"})

    # active：空闲时为空列表
    active = client.get("/api/transcode/active").json()
    check("空闲时 active 返回空列表", active == {"tasks": [], "count": 0})

    # 互斥门被占用 -> 409（不能与扫描/查重同时写盘）
    from app.api.routes import transcode as transcode_routes
    original_gate = transcode_routes.acquire_dedup_gate
    try:
        transcode_routes.acquire_dedup_gate = lambda: False
        busy = client.post("/api/transcode/start",
                           json={"file_id": pending_id, "force": False})
        check("互斥门被占用时 start 返回 409", busy.status_code == 409)
        check("409 提示有其它任务在执行", "任务" in busy.json().get("detail", ""))
    finally:
        transcode_routes.acquire_dedup_gate = original_gate


def test_api_background_task() -> None:
    """force 重转走后台线程：进度可查、结束释放互斥门、stream 可播。"""
    section("测试 10: 后台任务全链路")
    if not _REAL_VIDEO_ID:
        check("缺少真实转码产物，跳过后台任务测试", True)
        return

    from app.api.server import create_app
    client = TestClient(create_app(_CONFIG))

    started = client.post("/api/transcode/start",
                          json={"file_id": _REAL_VIDEO_ID, "force": True})
    check("force 重转返回 running", started.json()["status"] == "running")
    task_id = started.json()["task_id"]
    check("返回了 task_id", isinstance(task_id, str) and task_id.startswith("transcode-"))

    deadline = time.monotonic() + 30
    final = None
    saw_running = False
    while time.monotonic() < deadline:
        data = client.get(f"/api/transcode/status?file_id={_REAL_VIDEO_ID}").json()
        if data["status"] == "running":
            saw_running = True
        if data["status"] in ("done", "failed", "canceled"):
            final = data
            break
        time.sleep(0.1)

    check("轮询期间观察到 running（或转得极快直接 done）",
          saw_running or (final is not None and final["status"] == "done"))
    check("后台任务最终 done", final is not None and final["status"] == "done")
    check("后台任务结束后互斥门已释放", is_dedup_busy() is False)
    check("后台任务结束后 active 为空",
          client.get("/api/transcode/active").json()["count"] == 0)
    check("重转后 stream 仍可播",
          client.get(f"/api/transcode/stream?file_id={_REAL_VIDEO_ID}").status_code == 200)

    with DatabaseManager.session() as session:
        still_there = q.get_file_by_id(session, _REAL_VIDEO_ID) is not None
    check("转码不改动/不删除库记录", still_there)


# ============================================================
# main
# ============================================================

def main() -> int:
    global _passed, _failed
    print("=" * 70)
    print("按需转码后端测试（transcode_service / routes.transcode）")
    print(f"临时目录: {_TMP}")
    print("=" * 70)
    try:
        _setup_db()
        test_cache_key()
        test_parse_helpers()
        test_ffmpeg_availability()
        test_transcode_failure_writes_meta()
        test_transcode_canceled()
        test_done_cache_hit_skips_ffmpeg()
        test_clear_cache()
        test_real_transcode()
        test_api_status_and_stream()
        test_api_background_task()
    except Exception as exc:  # noqa: BLE001 — 测试异常要报出来并计失败
        import traceback
        _failed += 1
        print(f"\n[FAIL] 测试执行异常: {exc}")
        traceback.print_exc()
    finally:
        try:
            DatabaseManager.dispose()
        except Exception:
            pass
        shutil.rmtree(_TMP, ignore_errors=True)

    print("\n" + "=" * 70)
    if _failed:
        print(f"结果: {_passed} 通过, {_failed} 失败")
    else:
        print(f"结果: 全部 {_passed} 项通过 ✔")
    print("=" * 70)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
