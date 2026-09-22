# -*- coding: utf-8 -*-
"""
HEIC 检查与转换服务 —— 桌面端 GUI 与 HTTP API 共用的唯一实现。

职责：
1. 检查（`scan_candidates`）：把"媒体库里已入库的 HEIC"与"磁盘上还没入库的
   HEIC"合成一份候选清单 —— 只看数据库会漏掉从未扫描到的文件，只看磁盘则
   拿不到 file_id/单元归属（转换后无法就地把记录改成 .jpg）。
2. 转换（`run_heic_pipeline`）：转换 → 源文件处置 → 数据库同步 → 重索引 → 汇总消息。

数据安全纪律（与 file_ops / dedup_service 同一套）：
- 源文件默认**移至回收站**；回收站不可用（网络盘/exFAT）时**保留源文件**并如实
  上报，绝不静默降级成永久删除。
- 转换失败不删源；同名冲突默认跳过（不覆盖、不凭空造重复图）。
- 卷不可达（盘没插）与"文件真没了"必须区分，否则会把掉线报成"没有 HEIC"。

不依赖 Qt：GUI 线程与 FastAPI 后台线程共用同一份编排，避免两套实现漂移。
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from app.core.heic_converter import (
    CONFLICT_MODES, CONFLICT_SKIP, HEIC_EXTENSIONS, DEFAULT_JPEG_QUALITY,
    convert_batch, is_heic_file,
)
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services import file_ops
from app.services.dedup_gate import (
    acquire_dedup_gate, release_dedup_gate,
)

logger = logging.getLogger(__name__)


# ============================================================
# 源文件处置策略
# ============================================================

SOURCE_KEEP = "keep"
SOURCE_TRASH = "trash"
SOURCE_DELETE = "delete"
SOURCE_ACTIONS = (SOURCE_KEEP, SOURCE_TRASH, SOURCE_DELETE)
"""转换成功后对源 HEIC 的处置：保留 / 移至回收站（默认）/ 永久删除。"""

SOURCE_ACTION_PATTERN = "^(" + "|".join(SOURCE_ACTIONS) + ")$"
"""API 校验用正则（由 SOURCE_ACTIONS 拼出，避免枚举漂移）。"""

VOLUME_OFFLINE_NOTE = "磁盘未连接"


class HeicBusyError(RuntimeError):
    """已有查重/人脸重扫/HEIC 转换在运行（共用同一道重活互斥门）。"""


# ============================================================
# 不可变结果类型
# ============================================================

@dataclass(frozen=True)
class HeicCandidate:
    """一个待处理（或已检查到）的 HEIC 文件。"""

    path: str
    file_id: Optional[int] = None
    """媒体库中的文件 ID；None 表示磁盘上有但还没入库。"""

    size_bytes: int = 0
    unit_id: Optional[int] = None
    unit_name: str = ""
    in_library: bool = False
    """是否已在媒体库（决定转换后是否需要同步数据库行）。"""

    exists: bool = True
    """磁盘上是否存在（卷可达但文件没了 → False）。"""

    volume_ok: bool = True
    """所在卷是否可达（False = 磁盘没插，不能当成"没有 HEIC"）。"""

    has_perceptual_hash: bool = False
    """是否已有非空 pHash/dHash。False 多为旧版解不开 HEIC 留下的坏行，
    需要"重算哈希"才会被查重看见。"""


@dataclass(frozen=True)
class HeicScanResult:
    """一次 HEIC 检查的结果。"""

    candidates: tuple = ()
    total: int = 0
    total_bytes: int = 0
    from_db: int = 0
    from_disk_only: int = 0
    broken_hash_count: int = 0
    """哈希为失败占位空串的 HEIC 数量（永久坏行，需显式重算）。"""

    missing_count: int = 0
    """库里记录仍在、磁盘上已找不到的数量。"""

    invalid_header: int = 0
    """扩展名是 HEIC 但文件头不是的数量（改扩展名的假货，跳过）。"""

    skipped_volume_roots: tuple = ()
    """因磁盘未连接而没能扫描的根目录（UI 必须显式提示）。"""

    roots_scanned: tuple = ()


@dataclass(frozen=True)
class HeicConvertOptions:
    """一次转换的选项（GUI/Web 只传选项，不传实现细节）。"""

    quality: int = DEFAULT_JPEG_QUALITY
    source_action: str = SOURCE_TRASH
    on_conflict: str = CONFLICT_SKIP
    reindex: bool = True
    fix_broken: bool = True
    """转换后是否一并修复"哈希为空串"的永久坏行（重置后会被同一轮索引补上）。"""


def default_options(config) -> HeicConvertOptions:
    """从 AppConfig 取默认选项（GUI 与 API 的默认值单一来源）。"""
    action = str(getattr(config, "heic_source_action", SOURCE_TRASH) or SOURCE_TRASH)
    conflict = str(getattr(config, "heic_on_conflict", CONFLICT_SKIP) or CONFLICT_SKIP)
    return HeicConvertOptions(
        quality=int(getattr(config, "heic_jpeg_quality", DEFAULT_JPEG_QUALITY)
                    or DEFAULT_JPEG_QUALITY),
        source_action=action if action in SOURCE_ACTIONS else SOURCE_TRASH,
        on_conflict=conflict if conflict in CONFLICT_MODES else CONFLICT_SKIP,
        reindex=bool(getattr(config, "heic_reindex_after_convert", True)),
    )


@dataclass
class HeicItemResult:
    """单个文件的处理结果。"""

    path: str
    target: Optional[str] = None
    status: str = "failed"
    """converted / skipped / failed。"""

    error: str = ""

    source_kept_reason: str = ""
    """源文件被保留的原因（按设置保留 / 回收站不可用 …）。"""


@dataclass
class HeicOutcome:
    """一次 HEIC 转换的汇总。"""

    total: int = 0
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    sources_removed: int = 0
    sources_kept: int = 0
    db_updated: int = 0
    """就地改写为 .jpg 的媒体库记录数（file_id 保持不变）。"""

    healed: int = 0
    """被重置以便重算的坏哈希行数（原先 phash/dhash 为空串）。"""

    reindexed: int = 0
    items: list = field(default_factory=list)
    elapsed_seconds: float = 0.0
    cancelled: bool = False


# ============================================================
# 检查
# ============================================================

def _norm_prefix(path) -> str:
    """路径前缀（小写、去尾部分隔符），用于"是否在扫描范围内"的判断。"""
    return str(path).lower().replace("/", "\\").rstrip("\\")


def _in_scope(path_str: str, scope: Sequence[str]) -> bool:
    """路径是否落在任一范围内（含相等）。"""
    p = path_str.lower().replace("/", "\\")
    return any(p == s or p.startswith(s + "\\") for s in scope)


def _iter_disk_heic(root: Path, exclude_patterns: Sequence[str],
                    skipped_dirs: set) -> list[tuple[str, int]]:
    """遍历一个根目录下的 HEIC/HEIF 文件，返回 [(路径, 大小)]。"""
    found: list[tuple[str, int]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(str(root)):
            current = Path(dirpath)
            # 与 scanner 同一套排除语义：排除列表 + 隐藏目录
            dirnames[:] = [
                d for d in dirnames
                if d not in exclude_patterns and not d.startswith(".")
                and str(current / d) not in skipped_dirs
            ]
            for fname in filenames:
                if Path(fname).suffix.lower() not in HEIC_EXTENSIONS:
                    continue
                full = current / fname
                try:
                    st = full.stat()
                except OSError:
                    continue
                found.append((str(full), st.st_size))
    except OSError as e:
        logger.warning(f"扫描 HEIC 失败 {root}: {e}")
    return found


def scan_candidates(config, roots: Optional[Sequence] = None, *,
                    extra_folders: Optional[Sequence] = None,
                    include_disk: bool = True) -> HeicScanResult:
    """检查媒体库（或指定目录）里的 HEIC 文件。

    参数:
        config: AppConfig（只用 exclude_patterns）。
        roots: 显式指定要扫描的根目录；None 时取所有启用中的媒体库根。
        extra_folders: 额外要扫描的目录（"指定文件夹"入口），可与媒体库无关。
        include_disk: 是否做磁盘遍历（False 时只回库里的记录，快但会漏未入库文件）。

    返回:
        HeicScanResult（候选按路径排序）。
    """
    exclude = set(getattr(config, "exclude_patterns", frozenset()) or ())
    candidates: dict[str, HeicCandidate] = {}
    broken_hash_count = 0
    missing_count = 0
    invalid_header = 0
    from_db = 0
    from_disk_only = 0
    skipped_roots: list[str] = []
    scanned_roots: list[str] = []

    # ---- 1) 数据库侧：已入库的 HEIC（带 file_id / 单元归属）----
    db_rows: dict[str, tuple] = {}
    with DatabaseManager.session() as session:
        rows = q.get_files_by_extensions(session, HEIC_EXTENSIONS)
        unit_ids = {r.resource_unit_id for r in rows}
        unit_names = {u.id: u.name for u in q.get_units_by_ids(session, list(unit_ids))}
        for r in rows:
            key = str(r.path).lower()
            db_rows[key] = (
                str(r.path), r.id, r.resource_unit_id,
                unit_names.get(r.resource_unit_id, ""),
                bool(r.phash),          # 感知哈希是否已算出（空串/NULL 都算没有）
            )
        broken_hash_count = q.count_files_with_empty_hash(session, HEIC_EXTENSIONS)

    # ---- 2) 磁盘侧：补齐未入库的文件，并复核库里的文件是否还在 ----
    walk_roots: list[Path] = []
    if roots is not None:
        walk_roots.extend(Path(r) for r in roots)
    if extra_folders:
        walk_roots.extend(Path(f) for f in extra_folders)
    if roots is None:
        with DatabaseManager.session() as session:
            walk_roots.extend(Path(r.path) for r in q.get_enabled_roots(session))

    # 指定了扫描范围时，库里的候选也必须落在范围内 —— 否则"只检查某个文件夹"
    # 会把其它目录的 HEIC 一起列出来（用户的直觉是范围内的）。
    scope: Optional[list[str]] = None
    if roots is not None or extra_folders:
        scope = [_norm_prefix(p) for p in walk_roots]

    seen_dirs: set[str] = set()
    disk_entries: dict[str, tuple] = {}
    for root in walk_roots:
        resolved = str(root)
        if resolved in seen_dirs:
            continue
        seen_dirs.add(resolved)
        if not file_ops.volume_available(root):
            skipped_roots.append(resolved)
            logger.warning(f"HEIC 检查跳过（{VOLUME_OFFLINE_NOTE}）: {resolved}")
            continue
        scanned_roots.append(resolved)
        if not include_disk:
            continue
        for path_str, size in _iter_disk_heic(root, exclude, set()):
            key = path_str.lower()
            if key not in disk_entries:
                # 保留磁盘上的原始大小写：候选路径要能直接喂回转换流程，
                # 全部小写化在大小写敏感的场景（网络共享/外置卷）会打不开文件
                disk_entries[key] = (path_str, size)

    # ---- 3) 合并 ----
    for key, (orig_path, fid, uid, uname, has_hash) in db_rows.items():
        # 显示/操作一律用库里的原始大小写路径；lower 只用于跨盘去重
        path = Path(orig_path)
        if scope is not None and not _in_scope(str(path), scope):
            continue
        exists = path.is_file()
        volume_ok = file_ops.volume_available(path)
        size = 0
        if exists:
            try:
                size = path.stat().st_size
                if not is_heic_file(path):
                    invalid_header += 1
                    continue
            except OSError:
                exists = False
        else:
            missing_count += 1
        candidates[key] = HeicCandidate(
            path=str(path), file_id=fid, size_bytes=size, unit_id=uid,
            unit_name=uname, in_library=True, exists=exists,
            volume_ok=volume_ok, has_perceptual_hash=has_hash,
        )
        from_db += 1

    for key, (orig_path, size) in disk_entries.items():
        if key in candidates:
            continue
        path = Path(orig_path)
        if not is_heic_file(path):
            invalid_header += 1
            continue
        candidates[key] = HeicCandidate(
            path=str(path), size_bytes=size, in_library=False,
            exists=True, volume_ok=True, has_perceptual_hash=False,
        )
        from_disk_only += 1

    ordered = tuple(sorted(candidates.values(), key=lambda c: c.path.lower()))
    result = HeicScanResult(
        candidates=ordered,
        total=len(ordered),
        total_bytes=sum(c.size_bytes for c in ordered),
        from_db=from_db,
        from_disk_only=from_disk_only,
        broken_hash_count=broken_hash_count,
        missing_count=missing_count,
        invalid_header=invalid_header,
        skipped_volume_roots=tuple(skipped_roots),
        roots_scanned=tuple(scanned_roots),
    )
    logger.info(
        f"HEIC 检查完成: {result.total} 个（库内 {from_db} / 仅磁盘 {from_disk_only}，"
        f"坏哈希 {broken_hash_count}，磁盘未连接 {len(skipped_roots)} 个根）"
    )
    return result


def _load_db_heic_map() -> dict[str, tuple]:
    """{小写路径: (file_id, unit_id)}，用于转换后就地改写记录。"""
    with DatabaseManager.session() as session:
        rows = q.get_files_by_extensions(session, HEIC_EXTENSIONS)
        return {str(r.path).lower(): (r.id, r.resource_unit_id) for r in rows}


def _load_known_target_paths() -> set[str]:
    """已入库的 JPG 路径集合（小写）。

    用于 `on_conflict='rename'` 时不撞 `media_files.path` 唯一约束：
    仅看磁盘是否存在还不够 —— 库里可能有一条指向已被删除文件的 JPG 记录。
    只取 jpg/jpeg，量级远小于全库（实测全库 3349 行）。
    """
    with DatabaseManager.session() as session:
        rows = q.get_files_by_extensions(session, (".jpg", ".jpeg"))
        return {str(r.path).lower() for r in rows}


def _sync_db_after_convert(item: HeicItemResult, source: Path,
                           source_file_id: Optional[int]) -> bool:
    """把转换产物写回媒体库（就地改写，保留 file_id）。

    并发兜底：转换期间 watcher 触发的扫描可能已经把新的 .jpg 插成一条新记录，
    此时再 UPDATE 旧行会撞 `media_files.path` 唯一约束 —— 那就删掉旧的 .heic
    行，保留扫描插入的新行（记录比标签重要，且这种情况很罕见）。
    """
    if source_file_id is None or item.target is None:
        return False
    target = Path(item.target)
    try:
        with DatabaseManager.session() as session:
            if not q.update_file_after_convert(
                session, source_file_id,
                path=str(target), filename=target.name,
                extension=target.suffix.lower(),
                size_bytes=target.stat().st_size,
            ):
                return False
        return True
    except Exception as e:
        logger.warning(f"转换后改写记录失败（改为删除旧行，保留扫描新建的行）: {source} - {e}")
        try:
            with DatabaseManager.session() as session:
                q.delete_media_file(session, source_file_id)
        except Exception as e2:
            logger.error(f"删除旧 HEIC 记录也失败: {source} - {e2}")
        return False


def run_heic_pipeline(config,
                      targets: Sequence,
                      options: Optional[HeicConvertOptions] = None, *,
                      progress_callback: Optional[Callable[[str, int, int], None]] = None,
                      cancelled: Optional[Callable[[], bool]] = None,
                      notify: bool = True) -> HeicOutcome:
    """执行 HEIC 转换全流程。

    参数:
        config: AppConfig。
        targets: 待转换的 HEIC 路径列表。
        options: 转换选项（None 时取 config 默认值）。
        progress_callback: (阶段, 已完成, 总数)；阶段取值
            converting / source / db / indexing。
        cancelled: 取消探测；返回 True 时停止处理剩余文件。
        notify: 是否写入消息中心汇总。

    返回:
        HeicOutcome。

    异常:
        HeicBusyError: 已有查重/人脸重扫/HEIC 转换占用互斥门。
    """
    from app.services.index_service import run_index_pipeline  # 延迟导入避免循环

    opts = options or default_options(config)
    is_cancelled = cancelled or (lambda: False)
    started = time.time()
    outcome = HeicOutcome(total=len(targets))

    def report(phase: str, cur: int, total: int) -> None:
        if progress_callback:
            try:
                progress_callback(phase, cur, total)
            except Exception as e:  # 回调出错不该影响转换本身
                logger.debug(f"进度回调异常: {e}")

    if not acquire_dedup_gate():
        raise HeicBusyError("已有查重/人脸重扫/HEIC 转换在运行，请稍后再试")

    try:
        sources: list[Path] = []
        for t in targets:
            p = Path(t)
            if is_cancelled():
                break
            if not p.is_file():
                # 卷掉线 vs 文件真没了：文案必须区分（盘没插时不要误导用户以为文件丢了）
                reason = ("文件不存在" if file_ops.volume_available(p)
                          else f"{VOLUME_OFFLINE_NOTE}: {file_ops.volume_label(p)}")
                outcome.items.append(HeicItemResult(path=str(p), status="failed", error=reason))
                outcome.failed += 1
            elif not is_heic_file(p):
                outcome.items.append(HeicItemResult(
                    path=str(p), status="failed", error="不是 HEIC 格式（文件头不符）"))
                outcome.failed += 1
            else:
                sources.append(p)

        # ---- 阶段 1：转换（源文件由下一阶段统一处置）----
        db_map = _load_db_heic_map()
        known_targets = _load_known_target_paths()
        has_taken = bool(known_targets)

        def is_taken(p: Path) -> bool:
            return has_taken and str(p).lower() in known_targets

        report("converting", 0, len(sources))
        summary = convert_batch(
            sources,
            quality=opts.quality,
            on_conflict=opts.on_conflict,
            delete_source=False,          # 源的处置走 file_ops（回收站优先）
            cancelled=is_cancelled,
            is_taken=is_taken,
            progress_callback=lambda cur, total, _r: report("converting", cur, total),
        )
        outcome.cancelled = summary.cancelled

        converted: list[tuple[Path, HeicItemResult]] = []
        for src, res in zip(sources, summary.results):
            if res.success:
                item = HeicItemResult(
                    path=str(src), target=str(res.target), status="converted")
                outcome.converted += 1
                converted.append((src, item))
            elif res.skipped:
                item = HeicItemResult(
                    path=str(src), target=str(res.target) if res.target else None,
                    status="skipped", error=res.error or "已跳过")
                outcome.skipped += 1
            else:
                item = HeicItemResult(path=str(src), status="failed",
                                      error=res.error or "转换失败")
                outcome.failed += 1
            outcome.items.append(item)

        # 被取消时未处理的文件如实记录（不静默当成功）
        if summary.cancelled:
            for src in sources[len(summary.results):]:
                outcome.items.append(HeicItemResult(
                    path=str(src), status="skipped", error="已取消"))

        # ---- 阶段 2：源文件处置（回收站优先，不可用则保留）----
        if converted:
            report("source", 0, len(converted))
            for i, (src, item) in enumerate(converted, 1):
                if not is_cancelled():
                    if opts.source_action == SOURCE_KEEP:
                        outcome.sources_kept += 1
                        item.source_kept_reason = "按设置为保留源文件"
                    else:
                        ok, msg = file_ops.apply_mode(str(src), opts.source_action)
                        if ok:
                            outcome.sources_removed += 1
                        else:
                            outcome.sources_kept += 1
                            item.source_kept_reason = msg
                            logger.warning(f"源文件未能移除（保留）: {src} - {msg}")
                else:
                    outcome.sources_kept += 1
                    item.source_kept_reason = "已取消"
                report("source", i, len(converted))

        # ---- 阶段 3：数据库同步 ----
        affected_units: set[int] = set()
        if converted:
            report("db", 0, len(converted))
            for i, (src, item) in enumerate(converted, 1):
                entry = db_map.get(str(src).lower())
                fid = entry[0] if entry else None
                if _sync_db_after_convert(item, src, fid):
                    outcome.db_updated += 1
                if fid:
                    _invalidate_thumbnail(fid)
                if entry and entry[1]:
                    affected_units.add(entry[1])
                report("db", i, len(converted))
            if affected_units:
                with DatabaseManager.session() as session:
                    q.refresh_unit_stats(session, sorted(affected_units))

        # ---- 阶段 4：重算哈希/人脸（含修复坏行）----
        if opts.reindex and (converted or (opts.fix_broken and _broken_count())):
            if opts.fix_broken:
                outcome.healed = heal_broken_heic_hashes(reset_only=True)
            report("indexing", 0, 0)
            index_result = run_index_pipeline(
                config,
                progress_callback=lambda cur, total: report("indexing", cur, total),
                cancelled=is_cancelled,
            )
            outcome.reindexed = index_result.hashed_count

        outcome.elapsed_seconds = round(time.time() - started, 1)
        logger.info(
            f"HEIC 转换完成: 成功 {outcome.converted} / 跳过 {outcome.skipped} / "
            f"失败 {outcome.failed}；源移除 {outcome.sources_removed}、"
            f"源保留 {outcome.sources_kept}；记录改写 {outcome.db_updated}、"
            f"重算 {outcome.reindexed}（耗时 {outcome.elapsed_seconds}s）"
        )
        if notify:
            _notify(outcome)
        return outcome
    finally:
        release_dedup_gate()


def _invalidate_thumbnail(file_id: int) -> None:
    """让缩略图缓存失效（源路径变了，缓存名仍是 {file_id}_thumb.jpg）。"""
    try:
        from app.services.cleanup_service import CleanupService
        CleanupService.invalidate_thumbnail(file_id)
    except Exception as e:
        logger.debug(f"缩略图缓存失效失败 file_id={file_id}: {e}")


def _broken_count() -> int:
    """当前有多少 HEIC 的哈希是失败占位空串。"""
    with DatabaseManager.session() as session:
        return q.count_files_with_empty_hash(session, HEIC_EXTENSIONS)


def heal_broken_heic_hashes(reset_only: bool = False) -> int:
    """修复"哈希为空串"的 HEIC 坏行。

    这些行是旧版**解不开 HEIC** 时写入的失败占位（非 NULL），
    `get_unindexed_files` 只认 NULL，因此永远不会被重试 —— 实测真实库
    40 个 HEIC 里 19 个如此，pHash/dHash/宽高全空、人脸 0 条。

    参数:
        reset_only: True 时只重置为 NULL（由调用方的索引阶段接着算），
            由本函数自己跑索引会与 pipeline 的互斥门冲突。

    返回:
        被重置的行数。
    """
    with DatabaseManager.session() as session:
        rows = q.get_files_with_empty_hash(session, HEIC_EXTENSIONS)
        ids = [r.id for r in rows]
        if ids:
            q.reset_file_hashes(session, ids)
    if ids:
        logger.info(f"已重置 {len(ids)} 个 HEIC 的失败哈希占位，等待重新索引")
    return len(ids)


def _notify(outcome: HeicOutcome) -> None:
    """把转换结果写入消息中心（失败/源保留都要如实说）。"""
    try:
        from app.services.message_center import MessageCenter
        body_lines = [
            f"成功 {outcome.converted} 个，跳过 {outcome.skipped} 个，失败 {outcome.failed} 个。",
            f"源文件：已移除 {outcome.sources_removed} 个，保留 {outcome.sources_kept} 个"
            "（回收站不可用时一律保留，不会永久删除）。",
        ]
        if outcome.db_updated:
            body_lines.append(f"媒体库记录已改写 {outcome.db_updated} 条（文件 ID 与标签保留）。")
        if outcome.healed:
            body_lines.append(f"修复旧版无法解码的哈希 {outcome.healed} 条。")
        if outcome.reindexed:
            body_lines.append(f"重算哈希/人脸 {outcome.reindexed} 个文件。")
        failed = [i for i in outcome.items if i.status == "failed"]
        if failed:
            body_lines.append("失败示例：" + "；".join(
                f"{Path(i.path).name}（{i.error}）" for i in failed[:3]))
        kept = [i for i in outcome.items if i.source_kept_reason
                and i.source_kept_reason != "按设置为保留源文件"]
        if kept:
            body_lines.append("源文件保留原因示例：" + "；".join(
                f"{Path(i.path).name}（{i.source_kept_reason}）" for i in kept[:3]))
        title = ("HEIC 转换完成" if not outcome.failed else "HEIC 转换完成（有失败）")
        text = "\n".join(body_lines)
        if outcome.failed:
            MessageCenter.create_warning(title, text)
        else:
            MessageCenter.create_info(title, text)
    except Exception as e:
        logger.warning(f"HEIC 转换消息写入失败: {e}")
