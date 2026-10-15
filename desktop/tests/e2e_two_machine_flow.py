# -*- coding: utf-8 -*-
"""端到端验证"电脑 → 平板"搬运流程（用 subst 模拟移动硬盘与盘符变化）。

场景（完全按用户的两步走）：
  1) 电脑：插上"移动硬盘"(X:)，扫描媒体、把数据（库+缩略图）迁移到 X:\MediaData，然后关机；
  2) 平板：同一块盘在新盘符(Y:)下插入，程序首次运行（没有 config.json）——
     应当自动找到 Y:\MediaData；旧媒体路径 X:\... 已失效 → 改库 → 恢复正常。

用 subst 把同一个真实目录挂成不同盘符，等价于"盘没变、盘符变了"。

**两台机器各用一份 x86 打包产物的拷贝**（app 目录互相独立）：
因为"没找到数据根"时程序会退回应用目录 —— 若直接在仓库里跑源码版，
"应用目录"就是 desktop/，会打开仓库里那个真实库（第一次实测踩到过）。
"""
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DESKTOP = Path(r"D:\Claude-code-Project\Media\desktop")
PACKAGE = DESKTOP / "dist" / "win32" / "影视资源管理器-便携版"
EXE_NAME = "影视资源管理器-便携版.exe"
PORT = 19555
DRIVE_DIR = Path(r"D:\_dsh_sim\deep\drive")
LETTER_PC, LETTER_TABLET = "X:", "Y:"

passed = failed = 0


def check(desc: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {desc}")
    else:
        failed += 1
        print(f"  [FAIL] {desc} {extra}")


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n  {title}\n{'=' * 62}")


def subst(letter: str, target) -> None:
    subprocess.run(["subst", letter, str(target)], check=False, capture_output=True)


def unsubst(letter: str) -> None:
    subprocess.run(["subst", letter, "/D"], check=False, capture_output=True)


def http(method: str, path: str, payload=None, timeout: float = 20.0):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def wait_up(timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health",
                                        timeout=2) as r:
                if json.loads(r.read().decode()).get("status") == "ok":
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.4)
    return False


def free_port() -> int:
    """确保端口没被别的进程占用（否则会连到别人的服务）。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", PORT))
        return PORT
    finally:
        s.close()


def start_app(app_dir: Path, extra_args=None):
    """启动一份打包产物（app 目录独立；日志写在该目录里）。"""
    log_path = app_dir.parent / (app_dir.name + ".out.log")
    log = open(log_path, "w", encoding="utf-8")
    args = [str(app_dir / EXE_NAME), "--port", str(PORT),
            "--no-browser", "--host", "127.0.0.1"] + list(extra_args or [])
    proc = subprocess.Popen(args, cwd=str(app_dir), stdout=log,
                            stderr=subprocess.STDOUT)
    if not wait_up():
        proc.kill()
        raise RuntimeError(f"服务未起来，看日志: {log_path}")
    return proc


def stop_app(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    free_port()
    work = Path(r"D:\_dsh_flow_tmp")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shutil.rmtree(Path(r"D:\_dsh_sim"), ignore_errors=True)
    DRIVE_DIR.mkdir(parents=True)
    if not (PACKAGE / EXE_NAME).is_file():
        print(f"缺少打包产物: {PACKAGE / EXE_NAME}")
        return 2

    # 前置条件：整机不能有其它 data root marker，否则"唯一命中才自动采纳"的安全
    # 规则会（正确地）拒绝自动发现 —— 那是环境问题，不是功能问题。
    sys.path.insert(0, str(DESKTOP))
    from app.utils import paths as app_paths
    stale = app_paths.iter_marker_dirs()
    if stale:
        print("前置条件失败：机器上还有残留数据根，会干扰本测试，请先清理：")
        for path in stale:
            print(f"  - {path}")
        return 2

    # 两份独立拷贝 = 两台机器（各有自己的程序目录与 config.json）
    pc_app = work / "pc_app"
    tablet_app = work / "tablet_app"
    shutil.copytree(PACKAGE, pc_app)
    shutil.copytree(PACKAGE, tablet_app)

    try:
        return _run(pc_app, tablet_app, work)
    finally:
        # 无论成败都别留下 subst 盘符（否则占着盘符影响后续）
        unsubst(LETTER_PC)
        unsubst(LETTER_TABLET)


def _run(pc_app: Path, tablet_app: Path, work: Path) -> int:
    # ---------- 步骤 1：电脑侧 ----------
    section("步骤 1：电脑上把数据迁到移动硬盘")
    subst(LETTER_PC, DRIVE_DIR)
    drive_pc = Path(f"{LETTER_PC}\\")
    check("模拟硬盘已挂载为 X:", drive_pc.exists())

    media = drive_pc / "Media" / "相册A"
    media.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    for i in range(4):
        Image.new("RGB", (48, 48), (i * 40, 90, 150)).save(media / f"照片{i}.jpg")

    proc = start_app(pc_app)
    try:
        heal = http("GET", "/api/health")
        check("电脑侧启动成功（全新程序目录，数据暂在程序目录）", heal["status"] == "ok")
        roots = http("GET", "/api/library/roots").get("roots", [])
        check("电脑侧初始没有媒体库", len(roots) == 0)

        scan = http("POST", "/api/library/scan", {"path": str(media)})
        for _ in range(60):
            st = http("GET", f"/api/library/scan/{scan['task_id']}")
            if st["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.5)
        check("电脑侧扫描完成", st["status"] == "completed"
              and st.get("result", {}).get("total_files") == 4, str(st.get("result")))
        units = http("GET", "/api/units").get("units", [])
        check("电脑侧库里有 1 个单元", len(units) == 1, str(units))

        # 迁移数据到移动硬盘（用户点「迁移数据」→ 探测到是空目录 → copy）
        probe = http("GET", f"/api/settings/data-root/probe?path="
                            + urllib.parse.quote(f"{LETTER_PC}\\MediaData"))
        check("电脑侧探测目标目录=空（走迁移）", probe.get("is_data_root") is False)
        mig = http("POST", "/api/settings/data-root",
                   {"path": f"{LETTER_PC}\\MediaData", "mode": "copy"})
        check("迁移到 X:\\MediaData 成功", mig.get("success") is True, str(mig)[:200])
        check("提示需要重启（copy 模式）", mig.get("restart_required") is True)
    finally:
        stop_app(proc)

    marker = DRIVE_DIR / "MediaData" / ".media_manager_data.json"
    db_on_drive = DRIVE_DIR / "MediaData" / "data" / "media_manager.db"
    check("硬盘上有数据根 marker", marker.is_file())
    check("硬盘上有数据库", db_on_drive.is_file())

    # ---------- "关机、拔盘、插到平板"（换盘符） ----------
    section("步骤 2：平板侧（换盘符后首次运行）")
    unsubst(LETTER_PC)
    check("电脑侧盘符已拔出（X: 不存在）", not Path(f"{LETTER_PC}\\").exists())
    subst(LETTER_TABLET, DRIVE_DIR)
    check("平板侧挂载为 Y:", Path(f"{LETTER_TABLET}\\").exists())
    check("平板侧没有 config.json（全新机器）",
          not (tablet_app / "config.json").exists())
    # 平板上的媒体路径仍然是电脑上的 X:\... → 属于"盘符变了"的常态
    proc = start_app(tablet_app)
    try:
        feats = http("GET", "/api/features")
        check("平板侧自动发现了盘上的数据根",
              str(feats.get("data_root", "")).upper().startswith(LETTER_TABLET),
              f"data_root={feats.get('data_root')!r}")
        check("数据根来源=discovered（不是新建空库）",
              feats.get("data_root_status") == "discovered",
              f"status={feats.get('data_root_status')!r}")
        check("用的是盘上那个库",
              Path(feats.get("db_path", "")).name == "media_manager.db"
              and str(db_on_drive).upper() == str(feats.get("db_path", "")).upper(),
              feats.get("db_path"))
        units = http("GET", "/api/units").get("units", [])
        check("接管后能看到电脑上扫出的单元", len(units) == 1, str(units)[:200])
        roots = http("GET", "/api/library/roots").get("roots", [])
        check("平板侧读到的是同一份媒体库记录", len(roots) == 1, str(roots)[:200])

        # 模拟"平板这边媒体文件的位置和电脑上不一样"（真机上就是盘符变了）。
        # 注：subst 会被 Path.resolve() 解析成物理路径，所以在同一台机器上
        # "换盘符"不可复现；用重命名目录来表达同一件事（路径变了）。
        media_old = DRIVE_DIR / "Media"
        media_new = DRIVE_DIR / "MediaMoved"
        if media_new.exists():
            shutil.rmtree(media_new, ignore_errors=True)
        media_old.rename(media_new)
        roots = http("GET", "/api/library/roots").get("roots", [])
        reachable = [r for r in roots if r.get("reachable") is not False]
        check("媒体路径已失效（需要改库）", len(roots) == 1 and not reachable,
              str(roots)[:300])
        cands = roots[0].get("candidates") or []
        check("不可达根自动给出候选位置（用户点一下就能修）",
              any(c.get("path", "").endswith("相册A") for c in cands),
              json.dumps(cands, ensure_ascii=False)[:300])
        target = next((c["path"] for c in cands
                       if c.get("path", "").endswith("相册A")), str(media_new))

        # 用户点候选 → 先预演（默认 dry_run=true）→ 再真改
        plan = http("POST", "/api/library/remap",
                    {"old_prefix": roots[0]["path"], "new_prefix": target,
                     "dry_run": True})
        check("改库预演给出计划（≥2 行要改）",
              plan.get("success") is True and plan.get("dry_run") is True
              and plan.get("plan", {}).get("total_rows", 0) >= 2, str(plan)[:250])
        applied = http("POST", "/api/library/remap",
                       {"old_prefix": roots[0]["path"], "new_prefix": target,
                        "dry_run": False})
        check("改库成功且不是预演", applied.get("success") is True
              and applied.get("dry_run") is not True,
              json.dumps(applied, ensure_ascii=False)[:400])
        print("     [诊断] remap updated =",
              json.dumps(applied.get("updated"), ensure_ascii=False))
        roots = http("GET", "/api/library/roots").get("roots", [])
        check("改库后媒体路径可达",
              len(roots) == 1 and roots[0].get("reachable") is not False, str(roots)[:200])
        units = http("GET", "/api/units").get("units", [])
        files = http("GET", f"/api/units/{units[0]['id']}/files").get("files", [])
        check("单元文件仍指向盘上的文件", len(files) == 4, str(len(files)))
        thumb = urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}/api/files/{files[0]['id']}/thumbnail", timeout=20)
        body = thumb.read()
        check(f"缩略图能生成（{len(body)} bytes）", thumb.status == 200 and len(body) > 100)
    finally:
        stop_app(proc)

    section("结论")
    print(f"  {passed} 项通过 / {failed} 项失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
