# -*- coding: utf-8 -*-
"""
媒体库路径改库服务 —— 换盘符 / 换挂载点后把库里记录的绝对路径整体改写。

背景（为什么不能"丢给重扫"）：
    媒体库路径以绝对路径落库（`media_library_roots` / `resource_units` /
    `media_files` 的 path 列都带 unique 约束）。盘符一变，`D:\\Media` 变成
    `E:\\Media`，旧记录全部不可达；直接重扫会**新建一套根+单元**，旧记录变成
    幽灵（列表里两套同名单元），用户的星标/标签/封面也都孤立在旧 id 上。
    因此换盘后正确的动作是"改库"，而不是重扫。

安全纪律（与合并处置同级）：
    1. **先 dry-run**：`plan_remap` 只读，给出影响行数、冲突与缩略图缓存命中；
    2. **冲突即拒绝**：目标位置已存在另一套记录时不许改（unique 约束会炸，
       即使不炸也会让两套记录指向同一目录）；
    3. **单事务 + 幂等**：全部改写在一个事务里，失败整体回滚；重复执行 0 行；
    4. **改库前先备份 DB**：出问题至少能退回（`backup_database`）；
    5. **缩略图 `.meta` 同步改写**：`.meta` 里记的是源文件绝对路径，
       不改写的话盘符一变全部缓存判定失效（弱机上等于全量重生成）。
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from app.db.engine import DatabaseManager
from app.db import queries as q
from app.utils import paths as app_paths
from app.utils.media_types import get_all_media_extensions

logger = logging.getLogger(__name__)

MAX_SEARCH_DEPTH = 3
"""按末级目录名搜索候选时的最大深度（卷根为 0）。"""

MAX_SEARCH_ENTRIES = 3000
"""单次候选搜索浏览的最大目录数，避免在超大卷上卡死。"""

_SKIP_DIR_NAMES = frozenset({
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "recovery", "perflogs",
    "node_modules", ".git", ".svn", "__pycache__", "appdata",
})


class RemapError(RuntimeError):
    """改库失败（冲突 / 参数非法 / 文件系统错误）。"""


@dataclass(frozen=True)
class VolumeInfo:
    """一个就绪卷的信息（界面展示 + 候选来源）。"""

    path: Path
    label: str = ""
    serial: str = ""
    total: int = 0
    free: int = 0

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "label": self.label,
            "serial": self.serial,
            "total": self.total,
            "free": self.free,
        }


@dataclass(frozen=True)
class RemapCandidate:
    """一个"旧前缀可能被搬到这里"的候选位置。"""

    path: Path
    kind: str
    """same_relative（盘符变了、目录结构不变）或 name_match（按目录名找到）。"""

    media_count: int = 0
    """该目录浅层（不递归）的媒体文件数，用于判断"是不是空壳同名目录"。"""

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "media_count": self.media_count,
            "confidence": "high" if self.kind == "same_relative" else "medium",
        }


@dataclass(frozen=True)
class RemapPlan:
    """一次改库的影响预演（dry-run 结果，不改任何数据）。"""

    old_prefix: str
    new_prefix: str
    counts: dict = field(default_factory=dict)
    conflicts: tuple = ()
    thumbnail_meta_hits: int = 0
    old_exists: bool = False
    new_exists: bool = False
    reason: str = ""

    @property
    def same(self) -> bool:
        """新旧前缀是否等价（无需改库）。"""
        return _norm(self.old_prefix) == _norm(self.new_prefix)

    @property
    def blocking(self) -> bool:
        """是否不可执行（存在冲突或新旧等价）。"""
        return bool(self.conflicts) or self.same or bool(self.reason)

    @property
    def total_rows(self) -> int:
        """会被改写的路径行数（三张主表，不含封面/白名单）。"""
        return sum(int(self.counts.get(k, 0))
                   for k in ("library_roots", "units", "files"))

    def to_dict(self) -> dict:
        return {
            "old_prefix": self.old_prefix,
            "new_prefix": self.new_prefix,
            "counts": dict(self.counts),
            "total_rows": self.total_rows,
            "conflicts": list(self.conflicts),
            "thumbnail_meta_hits": self.thumbnail_meta_hits,
            "old_exists": self.old_exists,
            "new_exists": self.new_exists,
            "same": self.same,
            "blocking": self.blocking,
            "reason": self.reason,
        }


def _norm(path) -> str:
    """路径归一化比较键（Windows 大小写/分隔符不敏感）。"""
    return os.path.normcase(os.path.normpath(str(path)))


def normalize_prefix(path) -> str:
    """把用户给的路径规范化成"可做前缀比较"的形式（去掉结尾分隔符）。

    `D:\\Media\\` 与 `D:\\Media` 是同一个前缀；不归一化会让 LIKE 匹配失效。
    卷根（`D:\\`）保持原样，否则会变成 `D:`（相对路径语义）。
    """
    text = str(path).strip().strip('"')
    if not text:
        return ""
    norm = os.path.normpath(text)
    if len(norm) == 2 and norm.endswith(":"):
        norm += os.sep
    return norm


def _tail_of(prefix: str) -> str:
    """前缀里"卷根之后"的相对部分（`D:\\Media\\Photos` → `Media\\Photos`）。"""
    norm = normalize_prefix(prefix)
    anchor = Path(norm).anchor
    return norm[len(anchor):].strip("\\/") if anchor else norm.strip("\\/")


def list_volume_infos(volumes: Optional[Sequence[Path]] = None) -> list[VolumeInfo]:
    """列出就绪卷（盘符、卷标、卷序列号、容量）。"""
    infos: list[VolumeInfo] = []
    for root in (volumes if volumes is not None else app_paths.list_volumes()):
        info = VolumeInfo(path=Path(root),
                          label=app_paths.volume_label(root),
                          serial=app_paths.volume_serial(root))
        try:
            usage = shutil.disk_usage(str(root))
            info = VolumeInfo(path=info.path, label=info.label, serial=info.serial,
                              total=usage.total, free=usage.free)
        except OSError:
            pass
        infos.append(info)
    return infos


def _count_shallow_media(directory: Path, extensions=None) -> int:
    """统计目录浅层的媒体文件数（判断候选是不是空壳同名目录）。"""
    exts = extensions or get_all_media_extensions()
    count = 0
    try:
        for entry in directory.iterdir():
            try:
                if entry.is_file() and entry.suffix.lower() in exts:
                    count += 1
            except OSError:
                continue
    except OSError:
        return 0
    return count


def _search_dirs_by_name(volume: Path, name: str, depth: int,
                         limit: int = 5) -> list[Path]:
    """在卷内按目录名找候选（BFS，深度受限，跳过系统目录）。"""
    target = name.lower()
    found: list[Path] = []
    queue: list[tuple[Path, int]] = [(Path(volume), 0)]
    visited = 0
    while queue and len(found) < limit:
        current, level = queue.pop(0)
        if level >= depth:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            visited += 1
            if visited > MAX_SEARCH_ENTRIES:
                logger.warning("候选搜索达到目录数上限，提前结束")
                return found
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            lowered = entry.name.lower()
            if lowered in _SKIP_DIR_NAMES or lowered.startswith("$"):
                continue
            if lowered == target:
                found.append(entry)
                if len(found) >= limit:
                    return found
            queue.append((entry, level + 1))
    return found


def find_remap_candidates(old_prefix, *, volumes: Optional[Sequence[Path]] = None,
                          depth: int = MAX_SEARCH_DEPTH,
                          limit: int = 10) -> list[RemapCandidate]:
    """找出旧前缀可能被搬去的候选位置。

    两级策略（先强后弱）：
    1. `same_relative`：卷根之后的结构不变，只是盘符变了（最常见，可信度最高）；
    2. `name_match`：按旧前缀末级目录名在卷内搜（用户改了目录层级时用）。

    只返回**确实存在的目录**；调用方（界面）负责让用户确认。
    """
    old_norm = normalize_prefix(old_prefix)
    if not old_norm:
        return []
    tail = _tail_of(old_norm)
    last_name = Path(old_norm).name

    candidates: list[RemapCandidate] = []
    seen: set[str] = set()

    for volume in (volumes if volumes is not None else app_paths.list_volumes()):
        volume = Path(volume)
        if tail:
            same_relative = Path(volume.anchor or str(volume)) / tail
            # 同一个位置本身就是旧路径，不算候选（盘符没变、目录被搬走时，
            # 同卷内的新位置由下面的 name_match 找出来）
            if same_relative.is_dir() and _norm(same_relative) != _norm(old_norm):
                key = _norm(same_relative)
                if key not in seen:
                    seen.add(key)
                    candidates.append(RemapCandidate(
                        path=same_relative, kind="same_relative",
                        media_count=_count_shallow_media(same_relative)))
        if last_name and tail and len(candidates) < limit:
            for found in _search_dirs_by_name(volume, last_name, depth):
                key = _norm(found)
                if key in seen or key == _norm(old_norm):
                    continue
                seen.add(key)
                candidates.append(RemapCandidate(
                    path=found, kind="name_match",
                    media_count=_count_shallow_media(found)))
                if len(candidates) >= limit:
                    break

    # 有媒体文件的候选排前面：空壳同名目录最容易认错盘
    candidates.sort(key=lambda c: (c.kind != "same_relative", -c.media_count))
    return candidates


def rewrite_thumbnail_meta(cache_dir, old_prefix, new_prefix) -> int:
    """改写缩略图缓存 `.meta` 里的 source_path 前缀，返回改写条数。

    `.meta` 是"缓存属于哪个源文件"的唯一依据（源路径 + mtime + size），
    盘符变了不改写，所有缓存都会被判为不属于该文件 → 全部重新生成。
    只改以 old_prefix 开头（大小写不敏感）的记录，其它字段原样保留。
    """
    if not cache_dir:
        return 0
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return 0
    old_norm = normalize_prefix(old_prefix)
    new_norm = normalize_prefix(new_prefix)
    if not old_norm or old_norm == new_norm:
        return 0

    import json

    changed = 0
    for meta_file in cache_dir.glob("*.meta"):
        try:
            raw = meta_file.read_text(encoding="utf-8")
            meta = json.loads(raw)
        except (OSError, ValueError):
            continue
        source = meta.get("source_path")
        if not isinstance(source, str) or not source:
            continue
        if _norm(source)[:len(_norm(old_norm))] != _norm(old_norm):
            continue
        # 保留原路径的大小写风格：只替换前缀长度对应的那一段
        meta["source_path"] = new_norm + source[len(old_norm):]
        try:
            meta_file.write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            changed += 1
        except OSError as e:
            logger.warning(f"缩略图元数据改写失败 {meta_file}: {e}")
    if changed:
        logger.info(f"缩略图缓存元数据已改写: {changed} 条（{old_prefix} → {new_prefix}）")
    return changed


def plan_remap(old_prefix, new_prefix, *, include_whitelist: bool = False,
               cache_dir=None) -> RemapPlan:
    """改库预演（只读，不写任何数据）。

    参数:
        old_prefix: 旧路径前缀（如 `D:\\Media`）。
        new_prefix: 新路径前缀（如 `E:\\Media`）。
        include_whitelist: 是否把白名单路径规则一起算进影响范围。
        cache_dir: 缩略图缓存目录（用于统计有多少条 `.meta` 会命中）。

    返回:
        RemapPlan（含冲突列表与影响行数）。
    """
    old_norm = normalize_prefix(old_prefix)
    new_norm = normalize_prefix(new_prefix)
    if not old_norm or not new_norm:
        return RemapPlan(old_prefix=str(old_prefix), new_prefix=str(new_prefix),
                         reason="路径前缀为空")

    plan_kwargs = dict(old_prefix=old_norm, new_prefix=new_norm)
    if _norm(old_norm) == _norm(new_norm):
        return RemapPlan(**plan_kwargs, reason="新旧路径相同，无需改库",
                         old_exists=Path(old_norm).is_dir(),
                         new_exists=Path(new_norm).is_dir())

    with DatabaseManager.session() as session:
        counts = q.count_paths_with_prefix(session, old_norm)
        # 冲突只在本轮**确实有东西要改**时才算阻塞：改完之后的第二次调用
        # 会在目标位置看到刚刚改过去的同一批记录，那属于"已经改好了"，
        # 不能反过来判成冲突（否则改库天然不可重入，重试必炸）。
        migrating = sum(int(counts.get(name, 0))
                        for name in ("library_roots", "units", "files")) > 0
        conflicts = q.find_path_conflicts(session, old_norm, new_norm) if migrating else []
    if not include_whitelist:
        counts["whitelist"] = 0

    meta_hits = 0
    if cache_dir:
        cache_dir = Path(cache_dir)
        if cache_dir.is_dir():
            old_key = _norm(old_norm)
            import json
            for meta_file in cache_dir.glob("*.meta"):
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                source = meta.get("source_path")
                if isinstance(source, str) and _norm(source)[:len(old_key)] == old_key:
                    meta_hits += 1

    return RemapPlan(**plan_kwargs, counts=counts, conflicts=tuple(conflicts),
                     thumbnail_meta_hits=meta_hits,
                     old_exists=Path(old_norm).is_dir(),
                     new_exists=Path(new_norm).is_dir(),
                     reason="目标位置已存在另一套记录，请先删除重复的媒体库/单元"
                            if conflicts else "")


def apply_remap(old_prefix, new_prefix, *, include_whitelist: bool = False,
                cache_dir=None, backup: bool = True,
                db_path=None) -> dict:
    """执行改库（单事务 + 改前备份 + 缩略图 `.meta` 同步改写）。

    参数:
        old_prefix / new_prefix: 路径前缀。
        include_whitelist: 是否一并改写白名单里的路径规则。
        cache_dir: 缩略图缓存目录（None = 不改写 meta）。
        backup: 改库前是否备份数据库文件。
        db_path: 数据库路径（备份用；None = 取 DatabaseManager 已初始化的库）。

    返回:
        {"plan": plan.to_dict(), "updated": {...}, "backup": str|None,
         "thumbnail_meta": n}

    异常:
        RemapError: 新旧等价、前缀为空、存在冲突。
    """
    from app.services.data_relocation_service import backup_database

    plan = plan_remap(old_prefix, new_prefix,
                      include_whitelist=include_whitelist, cache_dir=cache_dir)
    if plan.reason:
        raise RemapError(plan.reason)
    if plan.conflicts:
        paths = ", ".join(c["path"] for c in plan.conflicts[:3])
        raise RemapError(f"目标位置已存在 {len(plan.conflicts)} 条记录（如 {paths}），"
                         "请先删除重复的媒体库/单元再改库")

    # 幂等：没有任何行需要改（例如重复调用、或旧路径本来就不存在）
    # 直接返回 0，不备份、不改库 —— 重试不该产生副作用。
    if plan.total_rows == 0:
        logger.info(f"改库无需改动: {plan.old_prefix}（旧路径下没有记录）")
        return {"plan": plan.to_dict(), "updated": plan.counts,
                "backup": None, "thumbnail_meta": 0}

    backup_path = None
    if backup:
        backup_path = backup_database(db_path or DatabaseManager.current_db_path(),
                                      reason="remap")

    with DatabaseManager.session() as session:
        updated = q.rebase_path_prefix(
            session, plan.old_prefix, plan.new_prefix,
            include_whitelist=include_whitelist)

    meta_changed = rewrite_thumbnail_meta(cache_dir, plan.old_prefix, plan.new_prefix)
    logger.info(f"改库完成: {plan.old_prefix} → {plan.new_prefix} {updated}")
    return {
        "plan": plan.to_dict(),
        "updated": updated,
        "backup": str(backup_path) if backup_path else None,
        "thumbnail_meta": meta_changed,
    }


def find_broken_roots(volumes: Optional[Sequence[Path]] = None,
                      *, with_candidates: bool = True) -> list[dict]:
    """列出"路径已不可达"的媒体库根目录及其候选修复位置。

    区分两种不可达（与删除语义同源，见 docs/pitfalls.md 铁律 3）：
      - `volume_offline`：整块盘没连（忘插盘）—— 文件其实还在，改库也没意义；
      - `path_missing`：盘在、目录没了 —— 十有八九是盘符变了，需要改库。

    只报告，不改动；由界面展示后交给 `apply_remap` 执行。
    """
    from app.services.file_ops import (OFFLINE_REASON, volume_available,
                                       volume_label)

    with DatabaseManager.session() as session:
        roots = q.get_all_roots(session)
        items = [{"id": r.id, "path": r.path, "enabled": bool(r.enabled)}
                 for r in roots]

    result: list[dict] = []
    for item in items:
        path = Path(item["path"])
        item["path_exists"] = path.is_dir()
        item["volume_present"] = volume_available(item["path"])
        item["volume"] = volume_label(item["path"])
        if item["path_exists"]:
            continue
        item["reason"] = ("volume_offline" if not item["volume_present"]
                          else "path_missing")
        item["reason_text"] = (
            f"磁盘{OFFLINE_REASON}: {item['volume']}（请先连接该磁盘）"
            if not item["volume_present"] else "目录不存在（盘符可能已变化）")
        item["candidates"] = (
            [c.to_dict() for c in find_remap_candidates(item["path"], volumes=volumes)]
            if with_candidates and item["volume_present"] else []
        )
        result.append(item)
    result.sort(key=lambda r: (r["reason"] != "path_missing", r["path"]))
    return result


def _tail_key(path) -> tuple:
    """返回 (卷根之后的完整相对路径, 末级目录名) 的小写归一化形式。

    后者是"换盘后同一份媒体库"的弱判据：`D:\\Media` 与 `E:\\Media` 的完整
    相对路径（相对各自卷根）其实都是 `Media`，但**同卷内搬家**时两份的相对
    路径必然不同、只有末级目录名相同 —— 两者都要能认出来。
    """
    norm_tail = _norm(_tail_of(str(path)))
    last = _norm(Path(str(path).rstrip("\\/")).name)
    return norm_tail, last


def find_equivalent_unreachable_root(path, volumes=None) -> Optional[dict]:
    """判断某个待扫描路径是否"等于某个不可达的旧根"（只是卷根不同）。

    用途：扫描守卫。若不拦，用户在换盘后重新扫描会在库里多出一套
    "同名不同盘符"的根与单元，旧记录永远清不掉（列表里两套同名单元）。

    判据（先强后弱，命中即可）：
      1. 卷根之后的完整相对路径相同（不同盘符下就是同一份媒体库）；
      2. 末级目录名相同（盘符与上层目录都变了，例如 `D:\\Media\\合集` →
         `E:\\合集`）。判据故意放宽：命中只是**弹确认**，用户随时可以选
         "按新库扫描"，而漏判的代价是库里多出一套清不掉的幽灵单元。

    返回:
        {"root_id", "old_path", "new_path", "candidates": [...]} 或 None。
    """
    target_tail, target_last = _tail_key(path)
    if not target_tail:
        return None   # 路径就是卷根，没有可比的"库名"
    with DatabaseManager.session() as session:
        rows = [{"id": r.id, "path": r.path} for r in q.get_all_roots(session)]
    for row in rows:
        old_path = Path(row["path"])
        if old_path.is_dir():
            continue
        old_tail, old_last = _tail_key(row["path"])
        if not old_tail:
            continue
        if old_tail != target_tail and not (target_last and old_last == target_last):
            continue
        return {
            "root_id": row["id"],
            "old_path": row["path"],
            "new_path": str(path),
            "match": "same_relative" if old_tail == target_tail else "same_name",
            "candidates": [c.to_dict() for c in
                           find_remap_candidates(row["path"], volumes=volumes)],
        }
    return None
