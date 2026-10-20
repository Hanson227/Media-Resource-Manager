# -*- coding: utf-8 -*-
"""端到端：平板插上硬盘 → 接上盘上已有的数据（绝不新建空库）。

场景（用户真实报障）：
  电脑上已把数据迁到移动硬盘（`...\\MediaData` 下是数据库+缩略图+marker）。
  平板外设多、盘符经常变；便携版启动时没认出这块盘，于是**回退到应用目录的空库**，
  用户在空库里点扫描/建库，以为"数据得重新建一遍"，而其实数据就在盘上。

这个脚本用**打包产物**（不是源码）验证完整链路：
  ① 数据根埋在 4 层目录下（开机自动搜索只看 2 层）→ 启动必须是 blocked_no_root，
     数据端点 409，绝不能在应用目录创建空库；
  ② `/api/settings/data-root/volumes` 能列出这块盘；`deep=true` 能深扫出数据根；
  ③ 接管后立刻可用（无需重启），单元/文件数正确；
  ④ 重启后配置已持久化，自动认出同一份数据（不再需要操作）。

用法（会自己建 subst 盘符，跑完清理）：
    cd desktop && python tests/e2e_tablet_drive_letter.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent.parent
REPO = DESKTOP.parent
PKG = DESKTOP / "dist" / "win32" / "影视资源管理器-便携版"
SIM = Path(r"D:\_dsh_sim\tablet")
DRIVE_PHYS = SIM / "drive"          # subst 成 X:
APP_DIR = SIM / "tablet_app"
PORT = 19571
LETTER = "X"
EXE = "影视资源管理器-便携版.exe"

_passed = 0
_failed = 0


def check(desc: str, condition: bool, extra: str = "") -> bool:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [PASS] {desc}")
        return True
    _failed += 1
    print(f"  [FAIL] {desc}" + (f"  <-- {extra}" if extra else ""))
    return False


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n  {title}\n{'=' * 62}")


def http(method: str, path: str, body=None, timeout=20):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            return json.loads(payload)
        except ValueError:
            return {"_status": e.code, "_body": payload}


def wait_health(timeout=60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def subst(letter: str, target: Path) -> None:
    subprocess.run(["subst", f"{letter}:", str(target)], check=True)


def unsubst(letter: str) -> None:
    subprocess.run(["subst", f"{letter}:", "/D"], capture_output=True)


def make_drive_data(deep_root: Path) -> str:
    """在"硬盘"上造一份电脑迁过去的数据（DB + 1 个单元 + marker）。"""
    sys.path.insert(0, str(DESKTOP))
    from PIL import Image
    from app.db.engine import DatabaseManager
    from app.db.migrations import init_db, migrate_db
    from app.services.scan_service import run_scan
    from app.utils import paths as app_paths
    from config import AppConfig

    media = deep_root.parent / "媒体" / "假期"
    media.mkdir(parents=True, exist_ok=True)
    for i in range(3):
        Image.new("RGB", (40, 40), (i * 60, 120, 200)).save(media / f"p{i}.jpg")

    db_path = deep_root / "data" / "media_manager.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    DatabaseManager.dispose()
    init_db(db_path)
    migrate_db()
    cfg = AppConfig().with_updates(db_path=db_path,
                                   thumbnail_cache_dir=deep_root / "data" / ".thumbnails")
    run_scan(cfg, media)
    DatabaseManager.dispose()
    meta = app_paths.write_marker(deep_root, db_name="media_manager.db")
    return str(meta.get("data_root_id") or "")


def start_app() -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(SIM / "app.log", "w", encoding="utf-8")
    kwargs = {"cwd": str(APP_DIR), "env": env,
              "stdout": log, "stderr": subprocess.STDOUT}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    return subprocess.Popen([str(APP_DIR / EXE), "--host", "127.0.0.1",
                             "--port", str(PORT), "--no-browser"], **kwargs)


def stop_app(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    section("前置检查")
    if not PKG.is_dir():
        print(f"  [FAIL] 找不到打包产物: {PKG}\n  先跑: pyinstaller build_web.spec")
        return 1
    shutil.rmtree(SIM, ignore_errors=True)
    SIM.mkdir(parents=True, exist_ok=True)
    DRIVE_PHYS.mkdir(parents=True, exist_ok=True)
    unsubst(LETTER)   # 清掉可能残留的同名盘符
    shutil.copytree(PKG, APP_DIR)
    for junk in ("config.json",):
        (APP_DIR / junk).unlink(missing_ok=True)
    check("打包产物已复制为独立的平板程序目录", APP_DIR.is_dir())
    check("平板程序目录没有 config.json（全新机器）",
          not (APP_DIR / "config.json").is_file())

    section("步骤 1：准备硬盘上的数据（电脑迁过去的那一份，埋 4 层深）")
    deep_root = DRIVE_PHYS / "a" / "b" / "c" / "MediaData"
    data_root_id = make_drive_data(deep_root)
    subst(LETTER, DRIVE_PHYS)
    x_root = Path(f"{LETTER}:\\a\\b\\c\\MediaData")
    check("盘符挂载成功（模拟盘符变化）", x_root.is_dir())
    check("数据根有 marker", (x_root / ".media_manager_data.json").is_file())
    check("数据根有数据库", (x_root / "data" / "media_manager.db").is_file())

    section("步骤 2：平板首次启动（浅扫 2 层看不到深目录）")
    proc = start_app()
    try:
        if not wait_health():
            print("  [FAIL] 便携版未能启动，日志：")
            print((SIM / "app.log").read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        features = http("GET", "/api/features")
        check("状态为 blocked_no_root（没有加载任何数据目录）",
              features.get("data_root_status") == "blocked_no_root",
              str(features.get("data_root_status")))
        check("应用目录没有被偷偷建成一个空库",
              not (APP_DIR / "data" / "media_manager.db").is_file())
        units = http("GET", "/api/units")
        check("数据端点被闸门挡住（409）",
              units.get("_status") == 409 or "detail" in units,
              str(units)[:120])

        section("步骤 3：Web 里选择数据盘（浅扫 → 深扫 → 接管）")
        vols = http("GET", "/api/settings/data-root/volumes")
        vol_paths = [v["path"].upper() for v in vols.get("volumes", [])]
        check(f"列出本机磁盘（{LETTER}: 在其中）",
              any(p.startswith(f"{LETTER}:".upper()) for p in vol_paths),
              str(vol_paths))
        shallow = [r["path"] for v in vols.get("volumes", [])
                   if v["path"].upper().startswith(f"{LETTER}:")
                   for r in v.get("data_roots", [])]
        check("浅扫看不到钻进深目录的数据根（符合预期）", not shallow, str(shallow))

        deep = http("GET", "/api/settings/data-root/volumes?deep=true&volume="
                    + urllib.request.quote(f"{LETTER}:\\"))
        deep_hits = [r for v in deep.get("volumes", []) for r in v.get("data_roots", [])]
        hit = next((r for r in deep_hits
                    if r["path"].lower().endswith("mediadata")), None)
        check("深扫找到盘上的数据根", hit is not None, str(deep_hits)[:200])
        if hit:
            check("数据根里识别出单元/文件数", int(hit.get("units") or 0) >= 1,
                  str(hit.get("units")))
            # 全新机器（没写过 config）本来就没有“上次用的那份”身份，所以这里是 False
            check("全新机器无历史身份（不冒充同一份）",
                  hit.get("is_same_identity") is False
                  and not features.get("expected_data_root_id"))
            adopted = http("POST", "/api/settings/data-root",
                           {"path": hit["path"], "mode": "adopt"})
            check("接管成功", adopted.get("success") is True, str(adopted)[:200])
            check("接管后无需重启（restart_required=false）",
                  adopted.get("restart_required") is False)
            units = http("GET", "/api/units")
            unit_list = units.get("units") or units.get("items") or []
            check("接管后立刻能读到盘上的单元", len(unit_list) >= 1,
                  str(units)[:200])
            check("状态变为 configured",
                  http("GET", "/api/features").get("data_root_status") == "configured")
            cfg_text = (APP_DIR / "config.json").read_text(encoding="utf-8")
            try:
                cfg_json = json.loads(cfg_text)
            except ValueError:
                cfg_json = {}
            check("配置已写明数据根（重启后能自动认）",
                  str(cfg_json.get("data_root") or "").lower()
                  == str(hit["path"]).lower(), str(cfg_json.get("data_root")))
    finally:
        stop_app(proc)

    section("步骤 4：重启便携版（模拟第二天再插上同一个盘）")
    proc2 = start_app()
    try:
        if not wait_health():
            print("  [FAIL] 第二次启动失败")
            return 1
        features = http("GET", "/api/features")
        check("重启后自动认出数据根（不再是 blocked）",
              features.get("data_root_status") in ("configured", "discovered"),
              str(features.get("data_root_status")))
        units = http("GET", "/api/units")
        unit_list = units.get("units") or units.get("items") or []
        check("重启后单元直接可见（不用再操作）", len(unit_list) >= 1)
    finally:
        stop_app(proc2)
        unsubst(LETTER)

    print(f"\n{'=' * 62}")
    print(f"  结论：{_passed} 项通过 / {_failed} 项失败")
    print(f"{'=' * 62}")
    if os.environ.get("KEEP_SIM") != "1":
        shutil.rmtree(SIM, ignore_errors=True)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
