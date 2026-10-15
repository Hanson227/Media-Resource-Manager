# -*- coding: utf-8 -*-
"""
数据迁移服务 —— 备份 / 拷贝数据库、搬迁数据根与缩略图缓存目录。

便携版的核心诉求是"数据跟着数据盘走，平板上不留数据"，因此需要：
  1. 换数据根时把 SQLite（含 WAL）和缩略图缓存搬过去，并给出校验结果；
  2. 任何**破坏性改库**（路径改库）之前先留一份数据库备份。

三条纪律：
  - **先 checkpoint 再拷贝**：SQLite WAL 模式下最新数据可能在 `-wal` 里，
    只拷 `.db` 会丢最近的扫描结果；
  - **先校验再删除源**：move 模式必须确认目标库完整（integrity_check + 行数）
    才允许删源，绝不"复制失败也把源删掉"；
  - **正在跑长任务时不许搬**：调用方需先过 dedup_gate（扫描/HEIC/查重），
    否则搬完的文件与仍在写的库不一致。
"""

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from app.db.engine import DatabaseManager
from app.utils import paths as app_paths

logger = logging.getLogger(__name__)

BACKUP_KEEP = 3
"""每个数据库最多保留的历史备份份数（超过则删最旧的）。"""

_DB_SIDECARS = ("-wal", "-shm")


def checkpoint(db_path) -> bool:
    """把 WAL 内容合并进主库文件（拷贝/备份前必须先做这一步）。

    优先复用已初始化的引擎连接（它开着同一个文件）；没有引擎时直接
    用 sqlite3 打开检查点。文件不存在或不是 SQLite 时返回 False（不算错误）。
    """
    if not db_path:
        return False
    db_path = Path(db_path)
    if not db_path.is_file():
        return False

    if DatabaseManager.is_initialized() and DatabaseManager.current_db_path() == db_path:
        try:
            from sqlalchemy import text
            with DatabaseManager.get_engine().connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE);")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"WAL 检查点失败（改用直连重试）: {e}")

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            conn.close()
        return True
    except sqlite3.Error as e:
        logger.warning(f"WAL 检查点失败 {db_path}: {e}")
        return False


def verify_database(db_path) -> tuple:
    """校验数据库文件：返回 (是否完好, 说明)。

    判据：`PRAGMA integrity_check` 为 ok；且能查到 media_files 行数
    （防止"文件能打开但其实是个空壳/半截文件"被当成复制成功）。
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return False, "文件不存在"
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as e:
        return False, f"无法打开: {e}"
    try:
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if integrity != "ok":
            return False, f"完整性检查失败: {integrity}"
        try:
            files = conn.execute("SELECT COUNT(*) FROM media_files;").fetchone()[0]
            units = conn.execute("SELECT COUNT(*) FROM resource_units;").fetchone()[0]
        except sqlite3.Error as e:
            return False, f"缺少关键表: {e}"
        return True, f"完整（{units} 个单元 / {files} 个文件）"
    except sqlite3.Error as e:
        return False, f"校验异常: {e}"
    finally:
        conn.close()


def _prune_backups(db_path: Path) -> int:
    """删除超出保留份数的历史备份（`<name>.bak_<时间戳>`）。"""
    backups = sorted(db_path.parent.glob(db_path.name + ".bak_*"),
                     key=lambda p: p.name, reverse=True)
    removed = 0
    for old in backups[BACKUP_KEEP:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def backup_database(db_path=None, *, reason: str = "backup",
                    keep: int = BACKUP_KEEP) -> Optional[Path]:
    """备份数据库文件（破坏性操作前的安全网）。

    参数:
        db_path: 数据库路径；None 时取已初始化的库。
        reason: 备份原因（写进文件名，便于事后区分）。
        keep: 保留份数。

    返回:
        备份文件路径；库不存在或复制失败返回 None（调用方决定是否继续）。
    """
    if db_path is None:
        db_path = DatabaseManager.current_db_path()
    if not db_path:
        return None
    db_path = Path(db_path)
    if not db_path.is_file():
        return None

    checkpoint(db_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = db_path.with_name(f"{db_path.name}.bak_{stamp}_{reason}")
    try:
        shutil.copy2(str(db_path), str(target))
    except OSError as e:
        logger.error(f"数据库备份失败 {db_path}: {e}")
        return None
    logger.info(f"数据库已备份: {target}")
    try:
        _prune_backups(db_path)
    except Exception as e:  # noqa: BLE001 — 清理失败不影响备份本身
        logger.debug(f"清理旧备份失败: {e}")
    return target


def copy_database(src_db, dst_db) -> dict:
    """拷贝数据库到新位置（含 WAL 合并与完整性校验）。

    返回:
        {"ok": bool, "src": str, "dst": str, "message": str, "bytes": int}
    """
    src_db = Path(src_db)
    dst_db = Path(dst_db)
    if not src_db.is_file():
        return {"ok": False, "src": str(src_db), "dst": str(dst_db),
                "message": "源数据库不存在", "bytes": 0}
    if src_db.resolve() == dst_db.resolve():
        return {"ok": True, "src": str(src_db), "dst": str(dst_db),
                "message": "源与目标相同，无需拷贝", "bytes": 0}

    checkpoint(src_db)
    try:
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_db), str(dst_db))
    except OSError as e:
        return {"ok": False, "src": str(src_db), "dst": str(dst_db),
                "message": f"拷贝失败: {e}", "bytes": 0}

    ok, message = verify_database(dst_db)
    size = dst_db.stat().st_size if dst_db.is_file() else 0
    return {"ok": bool(ok), "src": str(src_db), "dst": str(dst_db),
            "message": message, "bytes": size}


def copy_tree(src_dir, dst_dir, *, move: bool = False,
              progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """把目录内容合并拷贝到目标目录（同名文件覆盖），返回统计。

    参数:
        move: True 时在拷贝成功后删除源文件（逐文件删，保留目录结构）。
        progress: 可选回调 `(已完成, 总数)`；缩略图动辄几千个文件，界面靠它显示
            百分比进度（否则用户面对的是一个黑盒，等久了会以为死机）。
    """
    src_dir = Path(src_dir)
    dst_dir = Path(dst_dir)
    result = {"ok": True, "copied": 0, "bytes": 0, "skipped": 0, "total": 0,
              "message": ""}
    if not src_dir.is_dir():
        result["message"] = "源目录不存在"
        return result
    if src_dir.resolve() == dst_dir.resolve():
        result["message"] = "源与目标相同，无需拷贝"
        return result

    try:
        files = [entry for entry in src_dir.iterdir() if entry.is_file()]
    except OSError as e:
        result["ok"] = False
        result["message"] = str(e)
        return result
    result["total"] = len(files)
    if progress:
        try:
            progress(0, len(files))
        except Exception as e:  # noqa: BLE001 — 进度回调绝不拖垮迁移
            logger.debug(f"进度回调异常（忽略）: {e}")

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        for index, entry in enumerate(files, start=1):
            target = dst_dir / entry.name
            try:
                shutil.copy2(str(entry), str(target))
                result["copied"] += 1
                result["bytes"] += target.stat().st_size
                if move:
                    entry.unlink(missing_ok=True)
            except OSError as e:
                result["skipped"] += 1
                logger.warning(f"拷贝失败 {entry}: {e}")
            if progress:
                try:
                    progress(index, len(files))
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"进度回调异常（忽略）: {e}")
    except OSError as e:
        result["ok"] = False
        result["message"] = str(e)
    return result


def path_writable(directory) -> bool:
    """目录是否可写（不存在则尝试创建）。用于迁移前的目标校验。"""
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def relative_under(path, root) -> Path:
    """把 `path` 表示成相对 `root` 的路径；不在 root 下时退回"仅文件名"。

    用于迁移时保持"数据根内部的相对结构"（`data/media_manager.db` 到了新根
    仍是 `data/media_manager.db`，与 config 里相对路径的解析规则一致）。
    绝对/跨盘路径求不出相对时退化成文件名，并由调用方把新的绝对路径写回配置，
    避免重启后又指回旧位置。
    """
    path = Path(path)
    root = Path(root)
    try:
        return path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return Path(path.name)


def relocate_data_root(config, target_root, *, mode: str = "copy",
                       progress: Optional[Callable[[str, int, int], None]] = None) -> dict:
    """把当前数据根（数据库 / 缩略图 / PIN）迁移到新的数据根目录。

    参数:
        config: 当前 AppConfig（读 db_path / thumbnail_cache_dir / data_root）。
        target_root: 目标数据根目录（如 `E:\\MediaData`）。
        mode: "copy"（默认，保留源）或 "move"（校验通过后删除源文件）。
        progress: 可选回调 `(阶段, 已完成, 总数)`，阶段取值
            `database` / `thumbnails` / `pin` / `marker`。界面据此显示百分比
            （缩略图动辄几千个文件，没有进度就是个黑盒）。回调异常被忽略。

    返回:
        {"ok", "target_root", "db_path", "thumbnail_cache_dir", "pin_path",
         "mode", "steps", "marker", "message"}

    说明:
        - 只搬"数据"，不搬程序；目标目录留下 marker 供换盘符后自动识别；
        - 目录结构保持一致（`data/media_manager.db`、`data/.thumbnails`），
          返回的三个绝对路径应写回配置（否则重启又会指回旧位置）；
        - 任何一步失败都不删源（copy 语义），move 模式也只在**校验通过**后删；
        - **异常一律转成 ok=False**（不再往外抛）：调用方可能是 GUI 槽函数，
          抛出去就变成"点了没反应、也没有提示"，用户只会以为程序死了；
        - 失败时**回滚本次新建的文件**（目标库/缩略图），并清掉"复制中"哨兵，
          绝不给用户留一个"看起来有数据、其实只复制了一半"的目录。
    """
    from app.utils import paths as _paths

    target_root = Path(target_root)
    mode = "move" if str(mode).lower() == "move" else "copy"
    marker_name = getattr(config, "data_root_marker", None) or _paths.MARKER_FILENAME

    if not path_writable(target_root):
        return {"ok": False, "target_root": str(target_root), "mode": mode,
                "steps": {}, "marker": None,
                "message": f"目标目录不可写: {target_root}"}

    src_root = Path(config.data_root) if getattr(config, "data_root", None) else app_paths.app_dir()
    src_db = Path(config.db_path)
    src_thumb = Path(config.thumbnail_cache_dir)
    target_db = target_root / relative_under(src_db, src_root)
    target_thumb = target_root / relative_under(src_thumb, src_root)

    steps: dict = {}
    logger.info(f"开始迁移数据 → {target_root}（mode={mode}）")

    def _report(phase: str, done: int, total: int) -> None:
        if progress is None:
            return
        try:
            progress(phase, int(done), int(total))
        except Exception as e:  # noqa: BLE001 — 进度回调绝不拖垮迁移
            logger.debug(f"进度回调异常（忽略）: {e}")

    # 本次是否"从零建库"：决定失败时能不能把目标文件删掉
    fresh_target = not target_db.exists()
    sentinel = target_root / _COPYING_SENTINEL
    _write_copy_sentinel(sentinel, mode=mode)

    def _fail(message: str) -> dict:
        steps["rollback"] = _rollback_partial_target(
            target_db, target_thumb, fresh=fresh_target)
        _clear_copy_sentinel(sentinel)
        return {"ok": False, "target_root": str(target_root), "mode": mode,
                "steps": steps, "marker": None, "message": message}

    try:
        # 1) 数据库（copy_database 内部已含完整性校验）
        _report("database", 0, 1)
        db_result = copy_database(src_db, target_db)
        steps["database"] = db_result
        if not db_result["ok"]:
            return _fail(f"数据库迁移失败: {db_result['message']}")
        _report("database", 1, 1)

        # 2) 缩略图缓存（最耗时的一步：几千个文件，必须给出进度）
        def _thumb_progress(done: int, total: int) -> None:
            _report("thumbnails", done, total)
            if done and done % 300 == 0:
                logger.info(f"缩略图复制进度 {done}/{total}")

        thumb_result = copy_tree(src_thumb, target_thumb, move=False,
                                 progress=_thumb_progress)
        steps["thumbnails"] = thumb_result
        if not thumb_result.get("ok", True):
            return _fail(f"缩略图复制失败: {thumb_result.get('message', '')}")
        if thumb_result.get("skipped"):
            logger.warning(f"缩略图有 {thumb_result['skipped']} 个文件未复制（不阻塞迁移）")

        # 3) PIN（跟数据走，平板上不留）
        #    只复制、**绝不删除**源：PIN 文件默认在应用目录的 data/ 下（不属于数据根），
        #    删掉它等于把用户锁在门外；而新位置在下次 to_file 时会被写回。
        _report("pin", 0, 1)
        from config import pin_file_path
        src_pin = pin_file_path(app_paths.default_config_path())
        target_pin = target_root / relative_under(src_pin, src_root)
        steps["pin"] = {"moved": False}
        try:
            if src_pin.is_file():
                target_pin.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(src_pin), str(target_pin))
                steps["pin"] = {"moved": True, "path": str(target_pin)}
        except OSError as e:
            steps["pin"] = {"moved": False, "message": str(e)}
        _report("pin", 1, 1)

        # 4) marker（换盘符后的身份依据）—— 全部复制成功后才写
        _report("marker", 0, 1)
        marker = _paths.write_marker(target_root, filename=marker_name,
                                     db_name=target_db.name)
        steps["marker"] = {"path": str(_paths.marker_path(target_root, marker_name))}
        _report("marker", 1, 1)
    except Exception as e:  # noqa: BLE001 — 见 docstring：异常必须转成 ok=False
        logger.error(f"数据迁移异常: {e}", exc_info=True)
        return _fail(f"数据迁移中断: {type(e).__name__}: {e}")
    finally:
        _clear_copy_sentinel(sentinel)

    # 5) move：校验通过后才删源（数据库 + 缩略图）
    engine_released = False
    if mode == "move":
        ok, detail = verify_database(target_db)
        if not ok:
            return {"ok": False, "target_root": str(target_root), "mode": mode,
                    "steps": steps, "marker": marker,
                    "message": f"目标库校验失败，未删除源: {detail}"}
        # 源库正被数据库引擎打开着，Windows 上删不掉（SQLite 不是以"允许删除共享"
        # 打开的）。先释放引擎连接再删，并在返回值里告诉调用方"引擎已释放" ——
        # 调用方必须在**新库**上重新初始化，否则之后所有查询都会报未初始化。
        current = DatabaseManager.current_db_path()
        if current and os.path.normcase(str(current)) == os.path.normcase(str(src_db)):
            try:
                DatabaseManager.dispose()
                engine_released = True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"释放数据库引擎失败（源库可能删不掉）: {e}")
        removed = {"database": False, "thumbnails": 0}
        try:
            if os.path.normcase(str(src_db.resolve())) != os.path.normcase(str(target_db.resolve())) \
                    and src_db.is_file():
                for suffix in ("",) + _DB_SIDECARS:
                    Path(str(src_db) + suffix).unlink(missing_ok=True)
                removed["database"] = not src_db.exists()
        except OSError as e:
            steps["remove_source_error"] = str(e)
        if thumb_result.get("ok") and src_thumb.resolve() != target_thumb.resolve():
            removed["thumbnails"] = copy_tree(src_thumb, target_thumb, move=True)["copied"]
        steps["removed_source"] = removed

    logger.info(
        f"迁移完成: {target_root}"
        f"（数据库 {steps.get('database', {}).get('bytes', 0):,} 字节，"
        f"缩略图 {steps.get('thumbnails', {}).get('copied', 0)} 个文件）")
    return {
        "ok": True,
        "target_root": str(target_root),
        "db_path": str(target_db),
        "thumbnail_cache_dir": str(target_thumb),
        "pin_path": str(target_pin),
        "mode": mode,
        "steps": steps,
        "marker": marker,
        "engine_released": engine_released,
        "message": f"数据已{'迁移' if mode == 'move' else '复制'}到 {target_root}",
    }

# ============================================================
# 接管已有数据根（电脑 → 平板 的核心动作）
# ============================================================

_COPYING_SENTINEL = ".media_manager_copying"
"""迁移进行中的哨兵文件。

为什么需要它：迁移要写几千个缩略图文件，用户中途关掉程序（或断电）就会留下
"有数据库、缩略图只有一半"的目录 —— 看起来有数据，实际是半成品。有了哨兵，
`inspect_data_root` 就能识别"这是一次没做完的迁移"并**拒绝接管**，
避免把一个残缺数据根当成完整数据用（真实事故：用户以为迁移完成了）。
"""


def _write_copy_sentinel(sentinel: Path, *, mode: str) -> None:
    """写"复制中"哨兵（失败不阻塞迁移 —— 它只是额外保险）。"""
    try:
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            json.dumps({"app": "media-manager", "mode": mode, "pid": os.getpid(),
                        "started_at": datetime.now().isoformat(timespec="seconds")},
                       ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"写复制哨兵失败（不影响迁移）: {e}")


def _clear_copy_sentinel(sentinel: Path) -> None:
    try:
        sentinel.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"清理复制哨兵失败: {e}")


def _rollback_partial_target(target_db: Path, target_thumb: Path, *,
                            fresh: bool) -> dict:
    """回滚本次迁移写进目标的文件（只删"本来就是空的目标"）。

    只在 `fresh`（迁移前目标库不存在）时删，避免误删用户已有的数据。
    """
    result = {"db_removed": False, "thumb_removed": False}
    if not fresh:
        result["skipped"] = "目标目录原本已有数据库，未做删除"
        return result
    try:
        if target_db.is_file():
            for suffix in ("",) + _DB_SIDECARS:
                Path(str(target_db) + suffix).unlink(missing_ok=True)
            result["db_removed"] = not target_db.exists()
    except OSError as e:
        result["db_error"] = str(e)
    try:
        if target_thumb.is_dir():
            shutil.rmtree(target_thumb, ignore_errors=True)
            result["thumb_removed"] = not target_thumb.exists()
    except OSError as e:
        result["thumb_error"] = str(e)
    return result


def _find_database_in(target_root: Path, preferred: Path) -> Optional[Path]:
    """在目标数据根里找数据库文件（优先按当前相对布局，其次 data/*.db）。"""
    candidates = [preferred]
    candidates += sorted(target_root.glob("data/*.db"))
    candidates.append(target_root / "media_manager.db")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def inspect_data_root(target_root, *, marker_name: str = "") -> dict:
    """判断一个目录"是不是已经有一份数据的数据根"，供界面决定接管还是迁移。

    为什么需要它：用户典型流程是"电脑上把数据迁到移动硬盘 → 插到平板"。
    这时目标目录里**已经有完整数据**，若还按"迁移"去复制，会拿平板上那个
    空库把电脑迁过来的库覆盖掉。界面必须先问清楚。
    """
    from app.utils import paths as _paths

    root = Path(target_root)
    marker_name = marker_name or _paths.MARKER_FILENAME
    info: dict = {
        "path": str(root), "exists": False, "is_dir": False, "writable": False,
        "has_marker": False, "marker": {}, "data_root_id": "",
        "db_path": "", "db_exists": False, "db_bytes": 0,
        "db_ok": False, "db_detail": "", "units": 0, "files": 0,
        "thumbnail_files": 0, "is_data_root": False, "copy_incomplete": False,
    }
    try:
        info["exists"] = root.exists()
        info["is_dir"] = root.is_dir()
    except OSError:
        return info
    if not info["is_dir"]:
        return info

    info["writable"] = path_writable(root)
    meta = _paths.read_marker(root, marker_name) or {}
    info["has_marker"] = bool(meta)
    info["marker"] = meta
    info["data_root_id"] = str(meta.get("data_root_id") or "")

    # 按"当前相对布局"推断目标库位置：data_root/data/media_manager.db
    rel_db = Path("data") / "media_manager.db"
    db_path = _find_database_in(root, root / rel_db)
    if db_path is not None:
        info["db_path"] = str(db_path)
        info["db_exists"] = True
        try:
            info["db_bytes"] = db_path.stat().st_size
        except OSError:
            info["db_bytes"] = 0
        ok, detail = verify_database(db_path)
        info["db_ok"] = ok
        info["db_detail"] = detail
        if ok:
            try:
                import re
                units = re.search(r"(\d+) 个单元", detail)
                files = re.search(r"(\d+) 个文件", detail)
                info["units"] = int(units.group(1)) if units else 0
                info["files"] = int(files.group(1)) if files else 0
            except (ValueError, AttributeError):
                pass
    thumb_dir = root / "data" / ".thumbnails"
    try:
        if thumb_dir.is_dir():
            info["thumbnail_files"] = sum(1 for _ in thumb_dir.glob("*.jpg"))
    except OSError:
        pass

    # 有 marker 或有一份完好的库，都算"已有数据的数据根"；
    # 但若残留"复制中"哨兵 → 这是一次没做完的迁移，**拒绝当成数据根**
    # （否则用户会把半份数据当完整的用：库在、缩略图只有一半）。
    incomplete = (root / _COPYING_SENTINEL).is_file()
    info["copy_incomplete"] = incomplete
    if incomplete:
        info["db_detail"] = (info["db_detail"] + "；" if info["db_detail"] else "") \
            + f"检测到未完成的迁移（存在 {_COPYING_SENTINEL}），请重新迁移"
    info["is_data_root"] = bool(info["has_marker"] or info["db_ok"]) and not incomplete
    return info


def adopt_data_root(config, target_root, *, marker_name: str = "") -> dict:
    """**接管**一个已有数据的数据根：不复制、不覆盖，只校验 + 登记身份。

    与 `relocate_data_root` 的区别：
      - relocate = 把"当前这份数据"搬到新目录（copy/move）；
      - adopt   = 承认"这个目录里本来就有一份完整数据"（电脑迁到硬盘 → 平板接着用）。

    因此这里**绝不写入/覆盖目标数据库**，只在接管前给它做一次备份（保险），
    并把 marker 补全（换成同一个身份 uuid），返回应写回配置的绝对路径。

    返回结构同 `relocate_data_root`（多一个 `mode="adopt"` 与 `inspect` 字段）。
    """
    from app.utils import paths as _paths

    root = Path(target_root)
    marker_name = marker_name or getattr(config, "data_root_marker", None) \
        or _paths.MARKER_FILENAME
    info = inspect_data_root(root, marker_name=marker_name)
    if not info["is_dir"]:
        return {"ok": False, "target_root": str(root), "mode": "adopt",
                "steps": {}, "marker": None, "inspect": info,
                "message": f"目录不存在: {root}"}
    if info.get("copy_incomplete"):
        return {"ok": False, "target_root": str(root), "mode": "adopt",
                "steps": {}, "marker": None, "inspect": info,
                "message": (f"该目录是一次**未完成的迁移**（存在 {_COPYING_SENTINEL}）："
                            "数据可能只复制了一半。请重新执行迁移；"
                            f"确认数据完整的话，删除该文件后即可接管")}
    if not info["is_data_root"]:
        return {"ok": False, "target_root": str(root), "mode": "adopt",
                "steps": {}, "marker": None, "inspect": info,
                "message": f"该目录里没有数据根标记、也没有可用的数据库: {root}"}
    if info["db_exists"] and not info["db_ok"]:
        return {"ok": False, "target_root": str(root), "mode": "adopt",
                "steps": {}, "marker": None, "inspect": info,
                "message": f"该目录的数据库不可用（{info['db_detail']}），拒绝接管"}
    if not info["writable"]:
        return {"ok": False, "target_root": str(root), "mode": "adopt",
                "steps": {}, "marker": None, "inspect": info,
                "message": f"目标目录不可写: {root}"}

    steps: dict = {}
    db_path = Path(info["db_path"]) if info["db_path"] else None
    if db_path is not None:
        steps["backup"] = backup_database(db_path, reason="adopt")

    # 补/刷新 marker：沿用原有 uuid，别人再插这块盘也能认出来
    marker = _paths.write_marker(root, data_root_id=str(info["data_root_id"] or ""),
                                 filename=marker_name,
                                 db_name=db_path.name if db_path else "media_manager.db")
    steps["marker"] = {"path": str(_paths.marker_path(root, marker_name)),
                       "data_root_id": marker.get("data_root_id", "")}

    # 缩略图目录：按当前布局推断（目标里没有就用约定位置，后续生成会创建）
    src_thumb = Path(config.thumbnail_cache_dir)
    src_root = Path(config.data_root) if getattr(config, "data_root", None) \
        else app_paths.app_dir()
    thumb_dir = root / relative_under(src_thumb, src_root)

    from config import pin_file_path
    pin_candidate = root / relative_under(pin_file_path(app_paths.default_config_path()),
                                          src_root)
    steps["pin"] = {"path": str(pin_candidate), "exists": pin_candidate.is_file()}

    return {
        "ok": True,
        "target_root": str(root),
        "db_path": str(db_path) if db_path else str(root / "data" / "media_manager.db"),
        "thumbnail_cache_dir": str(thumb_dir),
        "pin_path": str(pin_candidate),
        "mode": "adopt",
        "steps": steps,
        "marker": marker,
        "inspect": info,
        "engine_released": True,
        "message": (f"已接管 {root} 里的数据"
                    f"（{info['units']} 个单元 / {info['files']} 个文件）"),
    }
