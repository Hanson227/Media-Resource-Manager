# -*- coding: utf-8 -*-
"""
路径与卷基础设施 —— 冻结态（PyInstaller）路径解析 + 数据根（data_root）解析。

为什么需要本模块（解决三个真实问题）：

1. **冻结态下 `Path(__file__).parent` 是错的**。PyInstaller onedir 里源码位于
   `dist/app/_internal/app/...`，`__file__` 上溯得到的不是"程序目录"，
   `server.py` 用四级上溯找 `web/` 会落到 `_internal/` 而不是 `_internal/web`。
   规则：**用户数据与应用资源一律经本模块取路径**，不要在别处 __file__ 上溯。

2. **便携版的数据要跟着数据盘走，而不是跟着程序目录走**。用户在
   `<数据盘>\MediaData\.media_manager_data.json` 放一个 marker 文件，
   程序在任何机器/任何盘符插入该盘都能自动定位它（见 `resolve_paths`）。

3. **`Path("")` 等于 `Path(".")`**，"未设置数据根"与"当前目录"无法区分 ——
   因此 `AppConfig.data_root` 用 `Optional[Path]`（None = 未设置），
   绝不用空字符串表达"没配"。

解析优先级（`resolve_paths`）：
    CLI/环境变量 > 应用目录自带 marker（便携布局） > 配置里的 data_root
    > 在所有就绪卷上按 marker 搜索（用 data_root_id 消歧） > 应用目录兜底
找不到"本该存在"的数据根时**不静默新建库**：回退到应用目录并把状态标成
`missing_expected`，由界面横幅提示用户（否则用户会在平板上默默扫出一个空库）。
"""

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

MARKER_FILENAME = ".media_manager_data.json"
"""数据根 marker 文件名（配置可覆盖，见 AppConfig.data_root_marker）。"""

MARKER_SCHEMA = 1
"""marker 文件结构版本。"""

DEFAULT_DATA_ROOT_SEARCH_DEPTH = 2
"""自动搜索数据根的目录深度（卷根为 0，逐级向下）。"""

_MAX_SEARCH_ENTRIES = 4000
"""单次搜索最多浏览的目录数，避免在超大卷上卡住启动。"""

_SKIP_DIR_NAMES = frozenset({
    "$recycle.bin", "system volume information", "windows",
    "program files", "program files (x86)", "programdata",
    "recovery", "perflogs", "msocache", "$sysreset", "config.msi",
    "node_modules", ".git", ".svn", "__pycache__",
})
"""搜索时跳过的目录名（小写比较）。"""


# ============================================================
# 冻结态 / 应用目录 / 资源目录
# ============================================================

def is_frozen() -> bool:
    """当前是否运行在 PyInstaller 打出的包里。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """应用目录：冻结态取 exe 所在目录，源码态取 desktop/。

    config.json / 日志 / 相对路径都以此为基准（便携版即"exe 旁边"）。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # 本文件位于 <repo>/desktop/app/utils/paths.py
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """只读资源目录：冻结态取 `sys._MEIPASS`，源码态取仓库根。

    打包时 `web/` 作为 datas 放进 `_MEIPASS/web`。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return app_dir().parent


def web_dir() -> Path:
    """Web 前端（SPA）目录，找不到时返回期望路径（调用方自行判 is_dir）。"""
    candidate = resource_dir() / "web"
    if candidate.is_dir():
        return candidate
    # 冻结态下若打包时把 datas 放在 exe 同级，也接受
    return app_dir() / "web"


def default_config_path() -> Path:
    """默认配置文件位置：应用目录下的 config.json。"""
    return app_dir() / "config.json"


def read_explicit_config_value(config_path: Path, key: str):
    """读取 config.json 里**显式写出**的某个键；文件不存在/无该键/解析失败返回 None。

    为什么要区分"显式写出"与"默认值"：`AppConfig` 的默认值不代表用户选择。
    典型受害者是 `api_host` —— 它的 dataclass 默认是 `127.0.0.1`（只监听本机），
    而便携版首次运行没有 config.json，若把默认值当作用户选择，手机就永远连不上
    （用户报障："手机输 127 的地址进不去"）。`profile` 同理。
    """
    try:
        if not Path(config_path).exists():
            return None
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or key not in data:
        return None
    value = data[key]
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def read_explicit_profile(config_path: Path) -> Optional[str]:
    """读取 config.json 里**显式写出**的 `profile`；没有该键或非法时返回 None。"""
    value = str(read_explicit_config_value(config_path, "profile") or "").strip().lower()
    return value if value in ("lite", "full") else None


def log_dir(fallback: Optional[Path] = None) -> Path:
    """日志目录：冻结态优先应用目录下的 logs/，不可写时退回 fallback/临时目录。"""
    candidates = [app_dir() / "logs", fallback, Path(os.environ.get("TEMP", ".")) / "media-manager"]
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            Path(candidate).mkdir(parents=True, exist_ok=True)
            probe = Path(candidate) / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return Path(candidate)
        except OSError:
            continue
    return Path(os.environ.get("TEMP", "."))


# ============================================================
# 卷（盘符）枚举与标识
# ============================================================

def list_volumes() -> list[Path]:
    """列出当前就绪的卷根（Windows 为 `E:\\` 形式）。

    不用 `os.listdrives()`：那是 3.12+ 才有的 API，本仓库环境是 Python 3.10。
    """
    if os.name != "nt":
        root = Path("/")
        return [root] if root.exists() else []

    volumes: list[Path] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                volumes.append(root)
        except OSError:
            continue
    return volumes


def volume_label(volume: Path) -> str:
    """卷标（如 `Media`）；取不到返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            str(volume), buf, 260, None, None, None, None, 0)
        return buf.value if ok else ""
    except Exception:  # noqa: BLE001 — 卷标只是展示信息，取不到不算错误
        return ""


def volume_serial(volume: Path) -> str:
    """卷序列号（32 位十六进制字符串）；取不到返回空串。

    用于在同名数据盘副本之间消歧：盘符会变、卷标可被改，序列号相对稳定。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        serial = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            str(volume), None, 0, ctypes.byref(serial), None, None, None, 0)
        return f"{serial.value:08X}" if ok else ""
    except Exception:  # noqa: BLE001
        return ""


def volume_of(path: Path) -> str:
    """返回路径所在卷根字符串（`E:\\` / `\\\\server\\share`）；无卷概念时返回空串。"""
    try:
        return Path(path).anchor or ""
    except Exception:  # noqa: BLE001
        return ""


# ============================================================
# 数据根 marker
# ============================================================

def marker_path(data_root: Path, filename: str = MARKER_FILENAME) -> Path:
    """数据根里 marker 文件的完整路径。"""
    return Path(data_root) / filename


def read_marker(data_root: Path, filename: str = MARKER_FILENAME) -> Optional[dict]:
    """读取数据根 marker；不存在或损坏返回 None（损坏不当成数据根）。"""
    target = marker_path(data_root, filename)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"数据根标记文件无法解析: {target} - {e}")
        return None
    return data if isinstance(data, dict) else None


def write_marker(data_root: Path, *, data_root_id: str = "",
                 filename: str = MARKER_FILENAME, db_name: str = "") -> dict:
    """写入（或补齐）数据根 marker，返回最终内容。

    已存在时不覆盖 `created_at` 与 `data_root_id`：这两个字段是"同一个数据根"
    的身份依据，重写会破坏跨机器识别。
    """
    data_root = Path(data_root)
    existing = read_marker(data_root, filename) or {}
    payload = {
        "app": "media-manager",
        "schema": MARKER_SCHEMA,
        "data_root_id": data_root_id or existing.get("data_root_id") or uuid.uuid4().hex,
        "created_at": existing.get("created_at") or datetime.now().isoformat(timespec="seconds"),
        "last_known_path": str(data_root),
        "volume_serial": volume_serial(data_root),
        "drive_label": volume_label(data_root),
    }
    if db_name:
        payload["db_name"] = db_name
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        marker_path(data_root, filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(f"写入数据根标记失败 {data_root}: {e}")
    return payload


def _ordered_volumes(volumes: Sequence[Path]) -> list[Path]:
    """搜索卷的顺序：**非系统卷优先，系统卷最后**。

    为什么：数据根几乎不会放在系统盘上，而系统盘的目录数最多 —— 先搜它会把
    搜索预算耗光，导致真正插着的移动盘压根没被搜到（实测事故：平板首次运行
    没发现硬盘上的数据根，反而用应用目录建了个空库）。
    """
    system = (os.environ.get("SystemDrive") or "C:").rstrip("\\").upper()

    def sort_key(volume: Path):
        text = str(volume).rstrip("\\").upper()
        return (1 if text == system else 0, text)

    return sorted(volumes, key=sort_key)


def iter_marker_dirs(
    volumes: Optional[Sequence[Path]] = None,
    *,
    filename: str = MARKER_FILENAME,
    depth: int = DEFAULT_DATA_ROOT_SEARCH_DEPTH,
    max_entries: int = _MAX_SEARCH_ENTRIES,
) -> list[Path]:
    """在所有卷上搜索含 marker 的目录（深度优先，含卷根自身）。

    跳过系统/版本库/node_modules 等明显无关目录；**每个卷**各有 `max_entries`
    的浏览上限，避免在塞满小文件的卷上把启动拖死。

    注意：上限必须是**每卷**而不是全局 —— 全局上限会让"先被搜的大盘"吃掉预算，
    后面的移动盘一个目录都不看（真实事故，见 `_ordered_volumes` 的注释）。
    """
    volumes = _ordered_volumes(list(volumes) if volumes is not None else list_volumes())
    depth = max(0, int(depth))
    found: list[Path] = []

    for volume in volumes:
        if marker_path(volume, filename).is_file():
            found.append(volume)
        visited = 0
        queue: list[tuple[Path, int]] = [(volume, 0)]
        while queue:
            current, level = queue.pop(0)
            if level >= depth:
                continue
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            over_budget = False
            for entry in entries:
                visited += 1
                if visited > max_entries:
                    logger.warning(
                        f"数据根搜索在 {volume} 达到目录数上限（{max_entries}），"
                        "跳过该卷其余目录，继续搜其它卷")
                    over_budget = True
                    break
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                name = entry.name.lower()
                if name in _SKIP_DIR_NAMES or name.startswith("$"):
                    continue
                if marker_path(entry, filename).is_file():
                    found.append(entry)
                queue.append((entry, level + 1))
            if over_budget:
                # 只放弃这个卷，绝不 return —— 否则排在后面的移动盘永远搜不到
                queue.clear()
                break
    return found


# ============================================================
# 数据根解析
# ============================================================

# data_root 解析结果的状态取值（前端/日志据此提示）
SOURCE_EXPLICIT = "explicit"        # 命令行/环境变量显式指定
SOURCE_APP_DIR = "app_dir"          # 应用目录本身就是数据根（便携布局）
SOURCE_CONFIGURED = "configured"    # 配置里的 data_root 直接可用
SOURCE_DISCOVERED = "discovered"    # 配置里的路径不可用，但在其它卷上按 marker 找到了
SOURCE_MISSING = "missing_expected"  # 配置里指定了数据根，但盘不在/找不到
SOURCE_NONE = "none"                # 从未配置数据根（沿用旧的相对路径行为）


@dataclass(frozen=True)
class ResolvedPaths:
    """一次启动解析出的全部落地路径（不可变结果对象）。"""

    app_dir: Path
    """应用目录（exe 所在目录 / desktop/）。"""

    data_root: Optional[Path]
    """数据根目录；None 表示未启用（沿用应用目录相对路径）。"""

    data_root_source: str
    """解析来源，见 SOURCE_* 常量。"""

    data_root_id: str
    """数据根身份（marker 里的 uuid）；未启用时为空串。"""

    expected_data_root: Optional[Path]
    """配置里期望但当前不可用的数据根（用于提示）。"""

    candidates: tuple[Path, ...]
    """本次在卷上找到的数据根候选（多个时表示存在同名副本）。"""

    db_path: Path
    """最终使用的数据库文件绝对路径。"""

    thumbnail_cache_dir: Path
    """最终使用的缩略图缓存目录绝对路径。"""

    pin_path: Path
    """Web 访问密码文件绝对路径。"""

    warnings: tuple[str, ...]
    """解析过程中的告警（原样透传到日志/界面）。"""

    @property
    def ok(self) -> bool:
        """数据根是否处于可用状态（未配置数据根也算可用）。"""
        return self.data_root_source != SOURCE_MISSING

    @property
    def is_relocated(self) -> bool:
        """本进程是否在使用"非应用目录"的数据根。"""
        return self.data_root is not None


def _resolve_against(base: Path, value: Path) -> Path:
    """把配置里的路径解析成绝对路径：绝对路径原样，相对路径相对 base。"""
    path = Path(value)
    if path.is_absolute():
        return path
    if not base:
        return path
    return (Path(base) / path).resolve()


def _norm(path) -> str:
    """路径归一化比较键（Windows 大小写/分隔符不敏感）。"""
    return os.path.normcase(os.path.normpath(str(path)))


def _pick_discovered(configured: Path, data_root_id: str,
                     candidates: Sequence[Path], filename: str) -> Optional[Path]:
    """在多个候选数据根中挑出"同一个数据根"。

    消歧顺序（先强后弱，避免选到另一个盘上的同名旧副本）：
    1. marker 里的 `data_root_id` 与配置记录的一致；
    2. marker 里的 `last_known_path` 与配置里的路径一致（仅盘符变化）；
    3. 只有一个候选时才接受它。
    """
    if not candidates:
        return None

    if data_root_id:
        for candidate in candidates:
            meta = read_marker(candidate, filename) or {}
            if str(meta.get("data_root_id") or "") == data_root_id:
                return candidate

    configured_key = _norm(configured)
    for candidate in candidates:
        meta = read_marker(candidate, filename) or {}
        if _norm(meta.get("last_known_path") or "") == configured_key:
            return candidate

    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_paths(
    config,
    *,
    app_dir_override: Optional[Path] = None,
    explicit_data_root: Optional[Path] = None,
    volumes: Optional[Sequence[Path]] = None,
    create_marker: bool = True,
    discover_when_unset: bool = False,
) -> ResolvedPaths:
    """解析数据根与全部落地路径（本仓库**唯一**的数据根解析入口）。

    参数:
        config: AppConfig（读 profile / data_root / data_root_id / 路径字段）。
        app_dir_override: 应用目录覆盖（测试用）。
        explicit_data_root: 命令行/环境变量指定的数据根（最高优先级）。
        volumes: 卷列表覆盖（测试用，默认探测真实盘符）。
        create_marker: 允许在数据根里补写 marker（默认是；只读介质上会失败告警）。
        discover_when_unset: `data_root` 从未配置时，也去各卷搜 marker 并采纳。
            便携版入口（main_web.py）打开它，桌面档保持关闭。
            **典型场景**：电脑上把数据迁到移动硬盘 → 插到平板首次运行（config.json
            里还没有 data_root）。若不开这个开关，程序只会沿用自己所在的目录，
            用户看到的是一个空库，而数据明明就在盘上。

    返回:
        ResolvedPaths。**不抛异常**：任何异常情况都降级为告警 + 可启动的路径，
        让 Web 界面有机会把问题告诉用户。
    """
    warnings: list[str] = []
    base_dir = Path(app_dir_override) if app_dir_override else app_dir()
    marker_name = getattr(config, "data_root_marker", None) or MARKER_FILENAME
    depth = getattr(config, "data_root_search_depth", None)
    depth = DEFAULT_DATA_ROOT_SEARCH_DEPTH if depth is None else int(depth)

    cfg_root: Optional[Path] = getattr(config, "data_root", None)
    if cfg_root is not None:
        cfg_root = Path(cfg_root)
    cfg_root_id = str(getattr(config, "data_root_id", "") or "")

    data_root: Optional[Path] = None
    source = SOURCE_NONE
    data_root_id = ""
    expected: Optional[Path] = None
    candidates: tuple[Path, ...] = ()

    def _adopt(path: Path, why: str, *, create: bool = True) -> None:
        """采纳一个数据根（就地更新外层局部变量）。"""
        nonlocal data_root, source, data_root_id
        data_root = Path(path)
        source = why
        if create and create_marker:
            meta = write_marker(
                data_root, data_root_id=cfg_root_id, filename=marker_name,
                db_name=Path(getattr(config, "db_path", "media_manager.db")).name)
            data_root_id = str(meta.get("data_root_id") or "")
        else:
            meta = read_marker(data_root, marker_name) or {}
            data_root_id = str(meta.get("data_root_id") or "")

    # ---- 1) 显式指定 ----
    if explicit_data_root:
        target = Path(explicit_data_root)
        if not volume_of(target) or Path(volume_of(target)).exists():
            _adopt(target, SOURCE_EXPLICIT)
        else:
            expected = target
            warnings.append(f"指定的数据根所在磁盘未连接: {volume_of(target)}")
            candidates = tuple(iter_marker_dirs(volumes, filename=marker_name, depth=depth))
            picked = _pick_discovered(target, cfg_root_id, candidates, marker_name)
            if picked is not None:
                _adopt(picked, SOURCE_DISCOVERED)
                warnings.append(f"已在其它卷上定位到数据根: {picked}")

    # ---- 2) 应用目录自带 marker（便携布局：exe 就在数据盘上）----
    if data_root is None and read_marker(base_dir, marker_name) is not None:
        _adopt(base_dir, SOURCE_APP_DIR, create=False)

    # ---- 3) 配置里的 data_root ----
    if data_root is None and cfg_root is not None:
        if cfg_root.is_dir():
            # 配置指向的目录就在：直接采纳（并补齐 marker），不做全卷搜索。
            _adopt(cfg_root, SOURCE_CONFIGURED)
        else:
            # ---- 4) 目录不在：可能是盘符变了，在所有就绪卷上按 marker 搜索 ----
            expected = cfg_root
            volume_root = volume_of(cfg_root)
            volume_ok = (not volume_root) or Path(volume_root).exists()
            warnings.append(
                f"数据根目录不存在，正在 {'已连接磁盘' if volume_ok else '其余磁盘'} 上搜索: {cfg_root}")
            candidates = tuple(iter_marker_dirs(volumes, filename=marker_name, depth=depth))
            picked = _pick_discovered(cfg_root, cfg_root_id, candidates, marker_name)
            if picked is not None:
                _adopt(picked, SOURCE_DISCOVERED)
                warnings.append(
                    f"数据根位置已变化: {cfg_root} → {picked}（已自动切换，"
                    f"媒体库路径可能需要修复）")
            elif candidates:
                warnings.append(
                    f"发现 {len(candidates)} 个候选数据根，无法确认是哪一个: "
                    + ", ".join(str(c) for c in candidates))
            elif volume_ok:
                # 盘在、目录还没建：按配置新建（首次使用数据根的正常路径）
                _adopt(cfg_root, SOURCE_CONFIGURED)
            else:
                warnings.append("未在任何已连接磁盘上找到数据根标记文件")

    # ---- 5) 从未配置过数据根：便携版入口允许"插盘即用"的全卷搜索 ----
    #      为什么必须放在这里：上面的搜索只覆盖"配置里写过、但目录不在"的情形；
    #      全新机器（平板首次运行）config.json 里根本没有 data_root，不搜就永远
    #      找不到盘上那份数据，用户只会看到一个空库。
    if data_root is None and cfg_root is None and discover_when_unset:
        found = tuple(iter_marker_dirs(volumes, filename=marker_name, depth=depth))
        if len(found) == 1:
            _adopt(found[0], SOURCE_DISCOVERED)
            warnings.append(f"未配置数据根，已自动采纳磁盘上唯一的数据根: {found[0]}")
        elif found:
            candidates = found
            warnings.append(
                f"发现 {len(found)} 个数据根，无法自动确认用哪一个"
                "（请在「设置 → 数据位置」里接管其中一个）: "
                + ", ".join(str(c) for c in found))
        # 一个都没找到：保持旧行为（相对应用目录），不新建数据根

    # ---- 6) 兜底 ----
    if data_root is None and expected is not None:
        source = SOURCE_MISSING
        data_root_id = cfg_root_id
    if data_root is None and source != SOURCE_MISSING:
        source = SOURCE_APP_DIR if read_marker(base_dir, marker_name) else SOURCE_NONE
        if source == SOURCE_NONE and cfg_root is None:
            # 从未配置数据根：沿用旧行为（相对应用目录），不写 marker，避免老用户
            # 升级后被凭空多出来的数据根改变路径语义。
            data_root = None
        else:
            data_root = base_dir

    # ---- 落地路径 ----
    path_base = data_root if data_root is not None else base_dir
    db_path = _resolve_against(path_base, getattr(config, "db_path", Path("data/media_manager.db")))
    thumb_dir = _resolve_against(
        path_base, getattr(config, "thumbnail_cache_dir", Path("data/.thumbnails")))
    pin_dir = (data_root / "data") if data_root is not None else (base_dir / "data")
    pin_path = pin_dir / ".web_pin"

    return ResolvedPaths(
        app_dir=base_dir,
        data_root=data_root,
        data_root_source=source,
        data_root_id=data_root_id,
        expected_data_root=expected,
        candidates=candidates,
        db_path=db_path,
        thumbnail_cache_dir=thumb_dir,
        pin_path=pin_path,
        warnings=tuple(warnings),
    )


def apply_resolved_paths(config, resolved: ResolvedPaths):
    """把解析结果写回 AppConfig（frozen dataclass，返回新实例）。

    同时把 PIN 文件落点切到数据根（便携版要求"平板上不存任何数据"）。

    注意：`missing_expected` 时**不能**把 `data_root` 清成 None —— 那会把
    "数据盘没插"变成"用户从没配过数据根"，下次插回盘程序就再也找不到它。
    """
    from config import configure_pin_dir

    configure_pin_dir(resolved.pin_path.parent)
    effective_root = (resolved.data_root if resolved.data_root is not None
                      else resolved.expected_data_root)
    return config.with_updates(
        data_root=effective_root,
        data_root_id=resolved.data_root_id or getattr(config, "data_root_id", ""),
        db_path=resolved.db_path,
        thumbnail_cache_dir=resolved.thumbnail_cache_dir,
    )


def human_volume_list(volumes: Optional[Iterable[Path]] = None) -> str:
    """把卷列表拼成给用户看的一行文本（日志/提示用）。"""
    items = list(volumes) if volumes is not None else list_volumes()
    return ", ".join(f"{v}{('(' + volume_label(v) + ')') if volume_label(v) else ''}"
                     for v in items) or "（无）"
