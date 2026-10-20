# -*- coding: utf-8 -*-
"""
媒体库路由 —— /api/library/*：卷列表、目录浏览、根目录管理、路径改库、扫描。

便携版没有桌面 GUI，这些能力必须由 Web 端提供（此前扫描只存在于桌面端）。
扫描是长任务：走 `task_registry` 登记 + 轮询，与查重/HEIC 同一套模式
（页面切走再回来靠 `/scan/active-task` 接上进度）。
"""

import logging
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app.api import schemas
from app.api import task_registry as registry
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.services import path_remap_service as remap_svc
from app.services.dedup_gate import acquire_dedup_gate, is_dedup_busy, release_dedup_gate
from app.services.file_ops import volume_available
from app.utils.media_types import get_all_media_extensions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["媒体库"])

SCAN_TASK_KINDS = ("scan",)
"""扫描任务在登记表里的 kind。"""

_MAX_BROWSE_ENTRIES = 400
"""单次目录浏览最多返回的子目录数（避免超大目录卡住浏览器）。"""


def _cfg(request: Request):
    """取当前配置（路由内多处使用，且要兼容配置热更新）。"""
    return getattr(request.app.state, "config", None)


# ============================================================
# 卷 / 目录浏览
# ============================================================

@router.get("/volumes", response_model=schemas.VolumeListResponse)
def list_volumes():
    """列出当前就绪的卷（盘符/卷标/容量），供 Web 端"选数据盘"。"""
    exts = get_all_media_extensions()
    items = []
    for info in remap_svc.list_volume_infos():
        contains_media = False
        try:
            for entry in info.path.iterdir():
                if entry.is_dir():
                    continue
                if entry.is_file() and entry.suffix.lower() in exts:
                    contains_media = True
                    break
        except OSError:
            pass
        item = schemas.VolumeItem(**info.to_dict(), contains_media=contains_media)
        items.append(item)
    return schemas.VolumeListResponse(volumes=items)


@router.get("/browse", response_model=schemas.BrowseResponse)
def browse_directory(path: str = Query(..., description="服务端绝对目录路径")):
    """浏览服务端目录（返回子目录 + 是否含媒体文件）。

    HTML 无法打开服务端原生文件夹选择框，因此便携版的"选媒体库目录"靠这个
    接口实现（配合 /volumes 让用户从盘符点进去）。
    """
    exts = get_all_media_extensions()
    raw = (path or "").strip().strip('"')
    if not raw:
        raise HTTPException(status_code=400, detail="路径为空")
    target = Path(raw)
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="必须是绝对路径")

    if not target.is_dir():
        parent = str(target.parent) if target.parent != target else None
        return schemas.BrowseResponse(
            path=str(target), parent=parent, entries=[],
            error="目录不存在" if volume_available(target) else "磁盘未连接")

    def _count_media(directory: Path) -> int:
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

    entries: list[schemas.BrowseEntry] = []
    try:
        children = [e for e in target.iterdir() if e.is_dir()]
    except OSError as e:
        raise HTTPException(status_code=403, detail=f"无法读取目录: {e}")
    children.sort(key=lambda p: p.name.lower())
    for child in children[:_MAX_BROWSE_ENTRIES]:
        media_count = _count_media(child)
        entries.append(schemas.BrowseEntry(
            name=child.name, path=str(child),
            is_media_dir=media_count > 0, media_count=media_count))

    parent = str(target.parent) if target.parent != target else None
    self_media = _count_media(target)
    return schemas.BrowseResponse(
        path=str(target), parent=parent, entries=entries,
        is_media_dir=self_media > 0, media_count=self_media)


# ============================================================
# 根目录
# ============================================================

@router.get("/roots", response_model=schemas.LibraryRootListResponse)
def list_roots():
    """列出媒体库根目录，并标注不可达原因与候选修复位置。"""
    try:
        broken = {item["id"]: item for item in remap_svc.find_broken_roots()}
        with DatabaseManager.session() as session:
            roots = q.get_all_roots(session)
            rows = []
            for root in roots:
                unit_count = len(q.get_units_by_root(session, root.id))
                rows.append((root.id, root.path, bool(root.enabled), unit_count))
    except Exception as e:  # noqa: BLE001
        logger.error(f"读取媒体库根目录失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")

    items = []
    for root_id, path, enabled, unit_count in rows:
        info = broken.get(root_id)
        if info:
            items.append(schemas.LibraryRootItem(
                id=root_id, path=path, enabled=enabled, reachable=False,
                volume_present=info["volume_present"], reason=info["reason"],
                reason_text=info["reason_text"], unit_count=unit_count,
                candidates=[schemas.RemapCandidateItem(**c)
                            for c in info.get("candidates", [])]))
        else:
            items.append(schemas.LibraryRootItem(
                id=root_id, path=path, enabled=enabled, reachable=True,
                unit_count=unit_count))
    return schemas.LibraryRootListResponse(roots=items)


@router.post("/roots", response_model=schemas.AddRootResponse)
def add_root(body: schemas.AddRootRequest):
    """添加媒体库根目录（不扫描）。

    若"同尾但不可达"的旧根存在（盘符变了），返回 409 语义的 `needs_remap`，
    而不是静默新建 —— 否则库里会多出一套同名根，旧记录永远清不掉。
    """
    raw = (body.path or "").strip().strip('"')
    if not raw:
        raise HTTPException(status_code=400, detail="路径为空")
    target = Path(raw)
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="必须是绝对路径")
    if not target.is_dir():
        detail = "目录不存在" if volume_available(target) else "磁盘未连接"
        raise HTTPException(status_code=400, detail=f"{detail}: {target}")

    guard = remap_svc.find_equivalent_unreachable_root(target)
    if guard:
        return schemas.AddRootResponse(
            success=False, created=False, needs_remap=True,
            old_path=guard["old_path"],
            candidates=[schemas.RemapCandidateItem(**c) for c in guard["candidates"]],
            message=f"库中已有同名旧路径（不可达）: {guard['old_path']}，请先修复路径")

    try:
        with DatabaseManager.session() as session:
            existing = q.get_root_by_path(session, str(target))
            if existing:
                return schemas.AddRootResponse(
                    success=True, root_id=existing.id, created=False,
                    message="该目录已在媒体库中")
            root = q.add_library_root(session, str(target))
            root_id = root.id
    except Exception as e:  # noqa: BLE001
        logger.error(f"添加媒体库根目录失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")
    return schemas.AddRootResponse(success=True, root_id=root_id, created=True,
                                  message=f"已添加媒体库: {target}")


@router.delete("/roots/{root_id}", response_model=schemas.StatusResponse)
def remove_root(root_id: int, purge_records: bool = Query(
        default=True, description="是否同时删除该根下的单元与文件记录")):
    """移除媒体库根目录（默认一并删除库内记录，不动磁盘文件）。"""
    try:
        with DatabaseManager.session() as session:
            root = q.get_root_by_id(session, root_id)
            if root is None:
                raise HTTPException(status_code=404, detail=f"媒体库不存在: {root_id}")
            path = root.path
            if purge_records:
                q.remove_library_root(session, root_id)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"移除媒体库失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")
    return schemas.StatusResponse(
        success=True,
        message=f"已移除媒体库: {path}（磁盘文件未改动）" if purge_records
        else f"已移除媒体库: {path}")


# ============================================================
# 路径改库
# ============================================================

def _vol_key(path) -> str:
    """卷根比较键（`E:\\` → `e:`，大小写/尾分隔符不敏感）。"""
    text = str(path or "")
    anchor = Path(text).anchor or text
    return anchor.lower().rstrip("\\/").rstrip(":")


@router.get("/remap/suggest", response_model=schemas.RemapSuggestResponse)
def suggest_remap(request: Request):
    """盘符变化后的一键改库建议（已跑过 dry-run，不改库）。

    为什么需要：平板/便携版上盘符经常变，5 个根逐个改库太麻烦。这里把
    “同卷内相对路径不变”的根按**卷级前缀**归并（`E:\\` → `F:\\`），一次
    dry-run 给出影响行数与冲突；前端一个按钮就能全部改完。

    执行仍走 `POST /api/library/remap`（`dry_run=false`，改前自动备份）——
    本端点只读，不会写任何数据。用户必须自己点确认（不自动改库）。
    """
    cfg = getattr(request.app.state, "config", None)
    cache_dir = getattr(cfg, "thumbnail_cache_dir", None) if cfg is not None else None
    try:
        broken = remap_svc.find_broken_roots()
    except Exception as e:  # noqa: BLE001
        logger.error(f"改库建议：读取失效根失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")

    path_missing = [b for b in broken if b.get("reason") == "path_missing"]
    groups: dict[tuple[str, str], list[dict]] = {}
    singles: list[schemas.RemapSuggestionItem] = []

    for item in path_missing:
        candidates = item.get("candidates") or []
        if not candidates:
            continue
        # 优先“盘符变了但相对路径不变”的候选（same_relative）—— 那种才能做整盘映射
        same_rel = [c for c in candidates if c.get("kind") == "same_relative"]
        pick = same_rel[0] if same_rel else max(
            candidates, key=lambda c: int(c.get("media_count") or 0))
        new_path = str(pick.get("path") or "")
        if not new_path:
            continue
        old_vol, new_vol = _vol_key(item["path"]), _vol_key(new_path)
        entry = {"id": item["id"], "path": item["path"], "new_path": new_path}
        if pick.get("kind") == "same_relative" and old_vol and new_vol and old_vol != new_vol:
            groups.setdefault((old_vol, new_vol), []).append(entry)
        else:
            # 同卷搬家 / 只按目录名找到：按单个根映射，绝不整盘改写
            plan = remap_svc.plan_remap(item["path"], new_path, cache_dir=cache_dir)
            singles.append(schemas.RemapSuggestionItem(
                old_prefix=plan.old_prefix, new_prefix=plan.new_prefix,
                scope="root", roots=[entry], counts=dict(plan.counts),
                total_rows=plan.total_rows, conflicts=list(plan.conflicts),
                thumbnail_meta_hits=plan.thumbnail_meta_hits,
                new_exists=plan.new_exists, blocking=plan.blocking,
                reason=plan.reason,
            ))

    suggestions: list[schemas.RemapSuggestionItem] = []
    for (old_vol, new_vol), entries in groups.items():
        # 卷级映射：用真实卷根字符串（保留盘符冒号与反斜杠）
        old_root = Path(str(entries[0]["path"])).anchor or str(entries[0]["path"])
        new_root = Path(str(entries[0]["new_path"])).anchor or str(entries[0]["new_path"])
        plan = remap_svc.plan_remap(old_root, new_root, cache_dir=cache_dir)
        suggestions.append(schemas.RemapSuggestionItem(
            old_prefix=plan.old_prefix, new_prefix=plan.new_prefix,
            scope="volume", roots=list(entries), counts=dict(plan.counts),
            total_rows=plan.total_rows, conflicts=list(plan.conflicts),
            thumbnail_meta_hits=plan.thumbnail_meta_hits,
            new_exists=plan.new_exists, blocking=plan.blocking,
            reason=plan.reason,
        ))

    suggestions.extend(singles)
    suggestions.sort(key=lambda s: (bool(s.blocking), -int(s.total_rows)))
    message = ""
    if suggestions:
        total = sum(int(s.total_rows) for s in suggestions)
        message = (f"检测到 {len(suggestions)} 处可能的盘符变化，"
                   f"共 {total} 行路径可修复（改前自动备份，可重复执行）")
    return schemas.RemapSuggestResponse(
        suggestions=suggestions, broken_roots=len(path_missing), message=message)


@router.post("/remap", response_model=schemas.RemapResponse)
def remap_paths(body: schemas.RemapRequest):
    """路径改库：dry_run=True 只预演；False 执行（改前自动备份数据库）。"""
    plan = remap_svc.plan_remap(
        body.old_prefix, body.new_prefix,
        include_whitelist=body.include_whitelist)
    if body.dry_run:
        return schemas.RemapResponse(
            success=not plan.blocking, dry_run=True,
            plan=schemas.RemapPlanModel(**plan.to_dict()),
            error=plan.reason)

    if plan.reason:
        return schemas.RemapResponse(
            success=False, dry_run=False,
            plan=schemas.RemapPlanModel(**plan.to_dict()), error=plan.reason)
    if is_dedup_busy():
        raise HTTPException(status_code=409, detail="已有扫描/查重/转换任务在运行，请稍后再试")
    if not acquire_dedup_gate():
        raise HTTPException(status_code=409, detail="已有重任务在运行，请稍后再试")
    try:
        result = remap_svc.apply_remap(
            body.old_prefix, body.new_prefix,
            include_whitelist=body.include_whitelist)
    except remap_svc.RemapError as e:
        return schemas.RemapResponse(success=False, dry_run=False, error=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error(f"路径改库失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")
    finally:
        release_dedup_gate()

    return schemas.RemapResponse(
        success=True, dry_run=False,
        plan=schemas.RemapPlanModel(**result["plan"]),
        updated=result["updated"], thumbnail_meta=result["thumbnail_meta"],
        backup=result["backup"])


# ============================================================
# 扫描（长任务）
# ============================================================

def _run_scan_task(task_id: str, path: str, force_new_root: bool, cfg) -> None:
    """扫描任务体（后台线程）：扫描 + 落库 + 汇报进度。

    互斥门由路由在受理时占用，**必须**在这里释放（成功/失败/取消都要）。
    整段都包在 try/except 里：哪怕只是结果拼装时写错一个字段，也要把任务
    标成 failed —— 否则它会永远停在 running，前端一直显示"正在扫描"、
    active-task 也一直返回它（用户以为卡死）。
    """
    from app.api.task_registry import is_cancel_requested
    from app.services.scan_service import RootRemapRequired, run_scan

    def progress(current: int, total: int) -> None:
        registry.update(task_id, progress=[current, total], phase="scanning")

    def cancelled() -> bool:
        return is_cancel_requested(task_id)

    registry.update(task_id, status="running", started_at=time.time(), phase="scanning")
    try:
        try:
            outcome = run_scan(
                cfg, Path(path),
                progress_callback=progress,
                cancelled=cancelled,
                enforce_root_guard=not force_new_root,
            )
        except RootRemapRequired as e:
            logger.info(f"扫描被路径守卫拦下: {e}")
            registry.update(task_id, status="failed", finished_at=time.time(),
                            phase="needs_remap", error=str(e),
                            result={"needs_remap": True, "old_path": e.old_path,
                                    "path": e.path, "candidates": e.candidates})
            return

        if outcome.cancelled:
            registry.update(task_id, status="cancelled", finished_at=time.time(),
                            phase="cancelled", error="已取消")
            return

        saved = outcome.saved
        registry.update(
            task_id, status="completed", finished_at=time.time(), phase="done",
            progress=[outcome.result.total_files, outcome.result.total_files],
            result={
                "total_files": outcome.result.total_files,
                "total_size_bytes": outcome.result.total_size_bytes,
                "unit_count": len(outcome.result.units),
                "new_files": saved.new_files if saved else 0,
                "updated_files": saved.updated_files if saved else 0,
                "stale_files": saved.stale_files if saved else 0,
                "excluded_units": list(saved.excluded_units) if saved else [],
                "errors": list(saved.errors[:20]) if saved else [],
            })
    except Exception as e:  # noqa: BLE001
        logger.error(f"扫描任务 {task_id} 执行失败: {e}", exc_info=True)
        registry.update(task_id, status="failed", finished_at=time.time(),
                        error=f"扫描失败: {e}")
    finally:
        release_dedup_gate()


@router.post("/scan", response_model=schemas.ScanTaskResponse, status_code=202)
def start_scan(body: schemas.ScanRequest, request: Request):
    """触发扫描（异步）：立即返回 task_id，轮询 /api/library/scan/{task_id}。"""
    cfg = _cfg(request)
    if is_dedup_busy():
        raise HTTPException(status_code=409, detail="已有扫描/查重/转换任务在运行，请稍后再试")

    path = (body.path or "").strip().strip('"')
    if not path and body.root_id is not None:
        with DatabaseManager.session() as session:
            root = q.get_root_by_id(session, body.root_id)
            if root is None:
                raise HTTPException(status_code=404, detail=f"媒体库不存在: {body.root_id}")
            path = root.path
    if not path:
        raise HTTPException(status_code=400, detail="必须提供 path 或 root_id")

    target = Path(path)
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="必须是绝对路径")
    if not target.is_dir():
        detail = "目录不存在" if volume_available(target) else "磁盘未连接"
        raise HTTPException(status_code=400, detail=f"{detail}: {target}")

    if not acquire_dedup_gate():
        raise HTTPException(status_code=409, detail="已有重任务在运行，请稍后再试")

    task_id = registry.next_task_id("scan")
    try:
        registry.prune()
        registry.register(task_id, "scan", phase="queued", progress=[0, 0])
        threading.Thread(
            target=_run_scan_task,
            args=(task_id, path, bool(body.force_new_root), cfg),
            daemon=True, name=f"scan-task-{task_id}",
        ).start()
    except Exception:
        # 线程没起来就必须放门，否则后续所有重任务都被卡住
        release_dedup_gate()
        raise
    return schemas.ScanTaskResponse(task_id=task_id, status="queued", path=path,
                                   message="扫描已开始")


@router.get("/scan/active-task")
def get_scan_active_task():
    """当前仍在跑/排队的扫描任务（页面切回来靠它接上进度）。"""
    task = registry.active(SCAN_TASK_KINDS)
    if not task:
        return {"task_id": None, "status": None, "kind": None}
    return task


@router.get("/scan/{task_id}")
def get_scan_task(task_id: str):
    """查询扫描任务状态（轮询用）。"""
    registry.prune()
    task = registry.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"扫描任务不存在: {task_id}")
    return task


@router.post("/scan/cancel", response_model=schemas.StatusResponse)
def cancel_scan():
    """请求取消当前扫描（协作式：扫描器在下一个回调点停止，且不落库）。"""
    task_id = registry.cancel_active(SCAN_TASK_KINDS)
    if not task_id:
        return schemas.StatusResponse(success=False, message="没有正在运行的扫描任务")
    return schemas.StatusResponse(success=True, message=f"已请求取消: {task_id}")
