# -*- coding: utf-8 -*-
"""
设置路由 —— /api/settings/*：数据目录（数据根）读写与迁移、缩略图缓存目录。

为什么 Web 端必须有这些入口：便携版没有桌面设置对话框，而"数据放哪块盘"
恰恰是便携版最核心的配置。这里提供的是**同一套 config.json**，桌面端设置
对话框改的也是它（单一事实源，避免两个界面各写一份配置）。
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.api import schemas
from app.services import data_relocation_service as relocation
from app.services import path_remap_service as remap_svc
from app.services.cleanup_service import CleanupService, configure_thumbnail_cache_dir
from app.services.dedup_gate import acquire_dedup_gate, is_dedup_busy, release_dedup_gate
from app.utils import net as net_utils
from app.utils import capabilities
from app.utils import paths as app_paths

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["设置"])

THUMB_PATTERNS = ("*_thumb.jpg", "*_thumb.jpg.meta")


def _cfg(request: Request):
    return getattr(request.app.state, "config", None)


def _config_path(request: Request = None) -> Path:
    """config.json 的落点（**只读用途**，如展示给界面）。

    优先取运行时注入的路径（main.py / main_web.py 把实际使用的配置文件放进去），
    否则回退到应用目录下的 config.json。
    """
    if request is not None:
        runtime = getattr(request.app.state, "runtime", None) or {}
        if runtime.get("config_path"):
            return Path(runtime["config_path"])
    return app_paths.default_config_path()


def _writable_config_path(request: Request) -> Optional[Path]:
    """可写的 config.json 落点：**必须**由入口显式注入。

    为什么不让它回退到 `default_config_path()`：进程的"应用目录"在测试或嵌入式
    用法里可能是**开发机/用户的真实仓库**，一次误写就把用户的真实配置覆盖成默认值
    （实测发生过：跑测试把 desktop/config.json 重写了）。入口（main.py /
    main_web.py）都会注入 config_path，所以这里取不到就说明调用方来路不明，
    宁可直接拒绝写并明确报错。
    """
    runtime = getattr(request.app.state, "runtime", None) or {}
    value = runtime.get("config_path")
    if not value:
        logger.error("拒绝写入配置：运行态未注入 config_path（避免误写真实配置文件）")
        return None
    return Path(value)


def _cache_stats(cache_dir) -> tuple:
    """缩略图缓存目录的文件数与字节数。"""
    cache_dir = Path(cache_dir) if cache_dir else None
    files = 0
    total = 0
    if cache_dir and cache_dir.is_dir():
        for pattern in THUMB_PATTERNS:
            for entry in cache_dir.glob(pattern):
                try:
                    total += entry.stat().st_size
                    files += 1
                except OSError:
                    continue
    return files, total


def _persist_config(config, config_path: Path) -> str:
    """把配置写回 config.json；失败返回错误说明（空串表示成功）。

    **不写 `profile`**（除非用户本来就在文件里显式写过）：档位是入口/命令行
    决定的运行属性，不该被一次"改缩略图目录"的操作顺手固化下来 ——
    否则用便携版入口跑一次，共享的 config.json 就永久变成轻量档了。
    """
    omit = () if app_paths.read_explicit_profile(config_path) else ("profile",)
    try:
        config.to_file(config_path, omit=omit)
        return ""
    except OSError as e:
        logger.warning(f"配置写入失败: {e}")
        return f"配置写入失败（只读介质？）: {e}"


def _apply_runtime_config(request: Request, config) -> None:
    """把新配置同步给运行中的应用与清理服务（缩略图目录可热切换）。

    缩略图缓存目录在 `routes/files.py` 里是**每次请求**从 `app.state.config`
    读取的，因此这里一改就生效；但清理服务（CleanupService）用的是模块级
    全局变量，必须显式重新注入，否则清缓存仍打向旧目录。
    """
    app = getattr(request, "app", None)
    if app is not None:
        app.state.config = config
    try:
        configure_thumbnail_cache_dir(config.thumbnail_cache_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"缩略图缓存目录注入失败: {e}")


def _mark_data_root_ready(request: Request, data_root, *,
                          source: str = "configured") -> None:
    """接管/迁移成功后解除“未加载数据目录”闸门（无需重启）。

    不解除的后果：便携版从 blocked_no_root 启动后，用户明明已经选好了盘上的数据，
    数据端点却继续 409，只能重启 —— 那正是我们要避免的“黑盒”体验。
    """
    app = getattr(request, "app", None)
    runtime = getattr(getattr(app, "state", None), "runtime", None)
    if not isinstance(runtime, dict):
        return
    runtime["data_root"] = str(data_root or "")
    runtime["data_root_status"] = source
    runtime["expected_data_root"] = ""
    runtime["candidates"] = []


@router.get("", response_model=schemas.SettingsResponse)
def get_settings(request: Request):
    """当前运行设置与数据位置（Web 设置页展示）。"""
    cfg = _cfg(request)
    runtime = getattr(request.app.state, "runtime", None) or {}
    if cfg is None:
        raise HTTPException(status_code=500, detail="配置不可用")
    files, total = _cache_stats(cfg.thumbnail_cache_dir)
    db_bytes = 0
    try:
        db_path = Path(cfg.db_path)
        if db_path.is_file():
            db_bytes = db_path.stat().st_size
    except OSError:
        db_bytes = 0

    # 手机访问地址：客户端（平板上的浏览器）自己算不出服务端的局域网 IP，
    # 因此由服务端算好一并返回，让用户在 Web 界面里就能看到该输什么。
    host = str(getattr(cfg, "api_host", "") or "127.0.0.1")
    port = int(getattr(cfg, "api_port", 0) or 0)
    urls = net_utils.lan_urls(host, port) if port else []
    caps = capabilities.detect(getattr(cfg, "face_model_dir", None))

    return schemas.SettingsResponse(
        profile=getattr(cfg, "profile", "full"),
        data_root=str(cfg.data_root) if cfg.data_root else "",
        data_root_status=str(runtime.get("data_root_status") or ""),
        expected_data_root=str(runtime.get("expected_data_root") or ""),
        db_path=str(cfg.db_path),
        thumbnail_cache_dir=str(cfg.thumbnail_cache_dir),
        thumbnail_cache_files=files,
        thumbnail_cache_bytes=total,
        db_bytes=db_bytes,
        config_path=str(_config_path(request)),
        restart_required_hint="修改数据根后需要重启程序才会切换数据库",
        api_host=host,
        api_port=port,
        lan_urls=urls,
        loopback_only=net_utils.is_loopback_host(host),
        capabilities=caps.as_dict(),
        capability_notes=caps.notes(),
    )


@router.get("/data-root/probe", response_model=schemas.DataRootProbeResponse)
def probe_data_root(path: str, request: Request):
    """探测目标目录：是"已有数据的数据根"（→ 接管）还是空目录（→ 迁移）。

    典型场景：电脑上把数据迁到移动硬盘，插到平板后要接着用同一份数据 ——
    界面在用户选完目录后必须先问清楚，否则"迁移"会用平板上的空库覆盖它。
    """
    cfg = _cfg(request)
    raw = (path or "").strip().strip('"')
    if not raw:
        raise HTTPException(status_code=400, detail="路径为空")
    target = Path(raw)
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="必须是绝对路径")
    info = relocation.inspect_data_root(
        target, marker_name=getattr(cfg, "data_root_marker", "") or "")
    current = Path(cfg.data_root) if cfg and cfg.data_root else None
    if current is not None:
        try:
            info["is_current"] = target.resolve() == current.resolve()
        except OSError:
            info["is_current"] = False
    return schemas.DataRootProbeResponse(**info)


@router.get("/data-root/volumes", response_model=schemas.DataRootVolumesResponse)
def list_data_root_volumes(request: Request, deep: bool = Query(
        False, description="是否深扫（目录层级更大，稍慢）"),
        volume: str = Query("", description="只扫描这个卷（如 F:\\）；空=全部")):
    """列出每个就绪卷，以及卷上找到的数据根（便携版“选择数据盘”用）。

    为什么需要它：平板插上硬盘后盘符经常变，用户只知道“数据在这块盘上”。
    这里把“这块盘上有没有数据根 / 是哪一份（uuid 与配置里的相同就标 same_identity）/
    有多少单元与文件”直接列出来，点一下就能接管 —— 不用让用户猜路径。

    `deep=true` 时搜索深度从默认 2 提到 4（“扫描这块盘”按钮用）：
    数据根目录名起得深时浅扫找不到，但手动深扫只影响这一次请求，不拖慢每次开机。
    """
    cfg = _cfg(request)
    marker_name = getattr(cfg, "data_root_marker", "") or ""
    depth = 4 if deep else int(
        getattr(cfg, "data_root_search_depth", None) or 2)
    current = Path(cfg.data_root) if cfg is not None and cfg.data_root else None
    expected_id = str(getattr(cfg, "data_root_id", "") or "")
    marker_file = marker_name or app_paths.MARKER_FILENAME

    volumes: list[schemas.DataRootVolumeItem] = []
    wanted = str(volume or "").strip().lower().rstrip("\\/")
    for info in remap_svc.list_volume_infos():
        if wanted and str(info.path).strip().lower().rstrip("\\/") != wanted:
            continue
        hits: list[schemas.DataRootHit] = []
        for root in app_paths.scan_volume_data_roots(
                info.path, depth=depth, filename=marker_file):
            probe = relocation.inspect_data_root(root, marker_name=marker_name)
            marker = probe.get("marker") or {}
            is_current = False
            if current is not None:
                try:
                    is_current = root.resolve() == current.resolve()
                except OSError:
                    is_current = False
            hit_id = str(probe.get("data_root_id") or "")
            hits.append(schemas.DataRootHit(
                path=str(root),
                data_root_id=hit_id,
                last_known_path=str(marker.get("last_known_path") or ""),
                volume_serial=str(marker.get("volume_serial") or ""),
                units=int(probe.get("units") or 0),
                files=int(probe.get("files") or 0),
                db_bytes=int(probe.get("db_bytes") or 0),
                thumbnail_files=int(probe.get("thumbnail_files") or 0),
                db_ok=bool(probe.get("db_ok")),
                is_current=is_current,
                is_same_identity=bool(expected_id and hit_id == expected_id),
            ))
        volumes.append(schemas.DataRootVolumeItem(
            path=str(info.path), label=info.label, serial=info.serial,
            total=int(info.total or 0), free=int(info.free or 0),
            data_roots=hits))

    runtime = getattr(request.app.state, "runtime", None) or {}
    return schemas.DataRootVolumesResponse(
        volumes=volumes, depth=depth,
        current_data_root=str(current) if current is not None else "",
        expected_data_root_id=expected_id,
        data_root_status=str(runtime.get("data_root_status") or ""),
    )


@router.post("/data-root", response_model=schemas.DataMigrationResponse)
def set_data_root(body: schemas.DataRootRequest, request: Request):
    """迁移（copy/move）或接管（adopt）数据根。

    安全约定：
      - 有扫描/转换/查重任务在跑时拒绝（数据正在被写）；
      - 默认 `copy` 模式保留源文件；`move` 只在目标库校验通过后才删源；
      - **目标目录已有数据时必须用 `adopt`**：copy/move 一律 409 拒绝，
        否则会用当前（可能是空的）库覆盖用户辛苦迁过去的数据；
      - `adopt` 不复制、不覆盖，只在接管前给目标库做一次备份；
      - copy 模式需重启生效；move / adopt 会释放引擎并立刻在新库上初始化。
    """
    cfg = _cfg(request)
    if cfg is None:
        raise HTTPException(status_code=500, detail="配置不可用")
    if is_dedup_busy():
        raise HTTPException(status_code=409, detail="已有扫描/查重/转换任务在运行，请稍后再试")
    if not acquire_dedup_gate():
        raise HTTPException(status_code=409, detail="已有重任务在运行，请稍后再试")

    try:
        raw = (body.path or "").strip().strip('"')
        if not raw:
            raise HTTPException(status_code=400, detail="路径为空")
        target = Path(raw)
        if not target.is_absolute():
            raise HTTPException(status_code=400, detail="必须是绝对路径")
        volume = app_paths.volume_of(target)
        if volume and not Path(volume).exists():
            raise HTTPException(status_code=409,
                                detail=f"磁盘未连接: {volume}，请先插入数据盘")
        current_root = Path(cfg.data_root) if cfg.data_root else None
        if current_root is not None:
            try:
                if target.resolve() == current_root.resolve():
                    raise HTTPException(status_code=400, detail="目标目录与当前数据根相同")
            except OSError:
                pass

        probe = relocation.inspect_data_root(
            target, marker_name=getattr(cfg, "data_root_marker", "") or "")
        if body.mode != "adopt" and probe["is_data_root"]:
            raise HTTPException(
                status_code=409,
                detail=(f"该目录已有一份数据（{probe['units']} 个单元 / "
                        f"{probe['files']} 个文件）。请改用接管（mode=adopt），"
                        "否则会覆盖它"))
        if body.mode == "adopt" and not probe["is_data_root"]:
            raise HTTPException(
                status_code=400,
                detail="该目录里没有可接管的数据（无数据根标记、无可用的数据库）")

        if body.mode == "adopt":
            result = relocation.adopt_data_root(
                cfg, target, marker_name=getattr(cfg, "data_root_marker", "") or "")
        elif body.mode == "create":
            # 在空目录里新建数据根（向导兜底用）：只接受空目录，不碰已有数据
            result = relocation.create_data_root(cfg, target)
        else:
            result = relocation.relocate_data_root(cfg, target, mode=body.mode)
        if not result["ok"]:
            return schemas.DataMigrationResponse(
                success=False, target=str(target), mode=body.mode,
                steps=result.get("steps", {}), message=result["message"])

        # 数据已落到新根：把配置指向"实际落点"（绝对路径），PIN 落点搬到新根，
        # 否则重启后相对路径会重新解析、可能又指回旧位置。
        from config import configure_pin_dir

        configure_pin_dir(Path(result["pin_path"]).parent)
        new_cfg = cfg.with_updates(
            data_root=target,
            data_root_id=str((result.get("marker") or {}).get("data_root_id")
                             or getattr(cfg, "data_root_id", "")),
            db_path=Path(result["db_path"]),
            thumbnail_cache_dir=Path(result["thumbnail_cache_dir"]),
        )
        config_path = _writable_config_path(request)
        warning = ("配置未写入：运行态缺少 config_path"
                   "（请通过 main.py / main_web.py 启动）") if config_path is None \
            else _persist_config(new_cfg, config_path)
        _apply_runtime_config(request, new_cfg)
        # 闸门解除：数据目录已就绪，数据端点立即可用（无需重启）
        _mark_data_root_ready(request, target, source="configured")
        restart_required = True
        message = f"{result['message']}；重启程序后生效"
        if body.mode == "copy":
            message += "（源目录数据保留，确认无误后可自行删除）"
        elif result.get("engine_released"):
            # move / adopt 必须释放引擎（move 要删源库，adopt 要换到另一个库），
            # 因此立刻在新库上重新初始化：服务继续可用，用户也不用重启。
            try:
                from app.db.engine import DatabaseManager
                from app.db.migrations import init_db, migrate_db
                DatabaseManager.dispose()
                init_db(new_cfg.db_path)
                migrate_db()
                restart_required = False
                message = f"{result['message']}；已切换到新位置（无需重启）"
            except Exception as e:  # noqa: BLE001
                logger.error(f"新数据根初始化失败: {e}", exc_info=True)
                message = (f"{result['message']}；但新库初始化失败（{e}），"
                           "请立即重启程序")
        if warning:
            message += f"；{warning}"
        return schemas.DataMigrationResponse(
            success=True, target=str(target), mode=body.mode,
            restart_required=restart_required, steps=result.get("steps", {}),
            message=message)
    finally:
        release_dedup_gate()


@router.post("/thumbnail-cache", response_model=schemas.DataMigrationResponse)
def set_thumbnail_cache(body: schemas.ThumbnailCacheRequest, request: Request):
    """迁移缩略图缓存目录（**可热切换**：无需重启，下次请求即从新目录取图）。"""
    cfg = _cfg(request)
    if cfg is None:
        raise HTTPException(status_code=500, detail="配置不可用")
    if is_dedup_busy():
        raise HTTPException(status_code=409, detail="已有扫描/查重/转换任务在运行，请稍后再试")
    if not acquire_dedup_gate():
        raise HTTPException(status_code=409, detail="已有重任务在运行，请稍后再试")

    try:
        raw = (body.path or "").strip().strip('"')
        if not raw:
            raise HTTPException(status_code=400, detail="路径为空")
        target = Path(raw)
        if not target.is_absolute():
            raise HTTPException(status_code=400, detail="必须是绝对路径")
        volume = app_paths.volume_of(target)
        if volume and not Path(volume).exists():
            raise HTTPException(status_code=409,
                                detail=f"磁盘未连接: {volume}，请先插入数据盘")
        if not relocation.path_writable(target):
            raise HTTPException(status_code=400, detail=f"目录不可写: {target}")

        source = Path(cfg.thumbnail_cache_dir)
        steps = {}
        if source.resolve() != target.resolve():
            steps["thumbnails"] = relocation.copy_tree(
                source, target, move=(body.mode == "move"))
        else:
            steps["thumbnails"] = {"ok": True, "copied": 0, "message": "目录未变化"}

        new_cfg = cfg.with_updates(thumbnail_cache_dir=target)
        config_path = _writable_config_path(request)
        warning = ("配置未写入：运行态缺少 config_path"
                   "（请通过 main.py / main_web.py 启动）") if config_path is None \
            else _persist_config(new_cfg, config_path)
        _apply_runtime_config(request, new_cfg)
        message = f"缩略图缓存目录已切换到 {target}（已拷贝 {steps['thumbnails'].get('copied', 0)} 个文件）"
        if body.mode == "move":
            message += "，源目录文件已删除"
        if warning:
            message += f"；{warning}"
        return schemas.DataMigrationResponse(
            success=True, target=str(target), mode=body.mode,
            restart_required=False, steps=steps, message=message)
    finally:
        release_dedup_gate()


@router.post("/clear-thumbnail-cache", response_model=schemas.StatusResponse)
def clear_thumbnail_cache(request: Request):
    """清空当前缩略图缓存目录（下次浏览按需重新生成）。"""
    cfg = _cfg(request)
    if cfg is None:
        raise HTTPException(status_code=500, detail="配置不可用")
    try:
        removed = CleanupService.clear_thumbnail_cache(Path(cfg.thumbnail_cache_dir))
    except Exception as e:  # noqa: BLE001
        logger.error(f"清空缩略图缓存失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="内部服务器错误")
    return schemas.StatusResponse(success=True, message=f"已清理 {removed} 个缓存文件")
