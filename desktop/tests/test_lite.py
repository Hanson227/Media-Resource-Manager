# -*- coding: utf-8 -*-
"""
便携版（轻量档）测试 —— 运行档位 / 数据根 / 路径改库 / 无 GUI 扫描 / 数据迁移。

与 test_flow.py 的分工：
  - test_flow.py 覆盖**完整档**（含查重、Qt UI 信号）的既有行为；
  - 本文件覆盖便携版新增能力，且**全程不得导入 PySide6**（第一个用例就是
    断言这一点 —— 否则"便携版不含 Qt"这条约束哪天被某个 import 破坏也没人发现）。

运行方式:
    python tests/test_lite.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# 编码修复
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image  # noqa: E402

from config import AppConfig, configure_pin_dir  # noqa: E402
from app.db.engine import DatabaseManager  # noqa: E402
from app.db.migrations import init_db, migrate_db  # noqa: E402
from app.db import queries as q  # noqa: E402
from app.utils import paths as app_paths  # noqa: E402

_passed = 0
_failed = 0


def check(desc: str, condition: bool) -> None:
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [PASS] {desc}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc}")


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _make_media_tree(root: Path, unit_count: int = 2, files_per_unit: int = 2) -> None:
    """造一个最小媒体库：每个单元直接含图片文件。"""
    for index in range(unit_count):
        unit = root / f"单元{index + 1}"
        unit.mkdir(parents=True, exist_ok=True)
        for n in range(files_per_unit):
            Image.new("RGB", (32, 32), (index * 40, n * 40, 128)).save(
                unit / f"图片{n}.jpg")


def _temp_db(prefix: str) -> Path:
    """新建一个临时库文件路径（不含扩展名约束）。"""
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    return directory / "media_manager.db"


# ============================================================
# 0. 便携版不得依赖 Qt
# ============================================================

def test_no_qt_dependency():
    """便携版入口与全部后端模块不得导入 PySide6。

    这条必须最先跑：一旦某个模块在导入期拉起 Qt，"轻量档不含桌面 GUI"
    就成了空话（打包也会把 PySide6 一起收进去，白省几百 MB）。
    """
    section("测试 L0: 便携版入口不依赖 Qt")
    check("起点尚未导入 PySide6（前置条件）", "PySide6" not in sys.modules)

    import main_web  # noqa: F401 — 导入即验证整条依赖链
    from app.api.server import create_app  # noqa: F401
    from app.api.routes import library, settings  # noqa: F401
    from app.services import scan_service, path_remap_service  # noqa: F401
    from app.services import data_relocation_service  # noqa: F401

    check("导入 main_web / API / 服务后仍未导入 PySide6",
          "PySide6" not in sys.modules)
    check("main_web 暴露 main()", callable(getattr(main_web, "main", None)))


# ============================================================
# 1. 运行档位
# ============================================================

def test_profile_resolution():
    """profile 字段：默认完整档、可切轻量档、拼错必须回退并告警。"""
    section("测试 L1: 运行档位 profile")
    import warnings

    check("默认档位是完整档", AppConfig().profile == "full")
    check("默认档位 is_lite=False", AppConfig().is_lite is False)

    tmp = Path(tempfile.mkdtemp(prefix="lite_profile_"))
    try:
        cfg_file = tmp / "config.json"
        cfg_file.write_text(json.dumps({"profile": "lite"}), encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cfg = AppConfig.from_file(cfg_file)
        check("profile=lite 生效", cfg.is_lite is True)

        cfg_file.write_text(json.dumps({"profile": "LITE"}), encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            check("profile 大小写不敏感", AppConfig.from_file(cfg_file).is_lite is True)

        cfg_file.write_text(json.dumps({"profile": "portable"}), encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = AppConfig.from_file(cfg_file)
        check("非法 profile 回退完整档", cfg.profile == "full")
        check("非法 profile 有告警", any("profile" in str(w.message) for w in caught))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 2. 数据根解析
# ============================================================

def test_data_root_resolution():
    """数据根解析五条分支 + PIN 落点跟随数据根。"""
    section("测试 L2: 数据根解析")
    tmp = Path(tempfile.mkdtemp(prefix="lite_dataroot_"))
    try:
        app_dir = tmp / "app"
        app_dir.mkdir(parents=True)

        # ① 从未配置：沿用应用目录（老用户升级后路径语义不变）
        rp = app_paths.resolve_paths(AppConfig(), app_dir_override=app_dir, volumes=[])
        check("未配置数据根 → source=none",
              rp.data_root_source == app_paths.SOURCE_NONE and rp.data_root is None)
        check("未配置时数据库落在应用目录下",
              str(rp.db_path).startswith(str(app_dir.resolve())))
        check("未配置时不写 marker",
              not app_paths.marker_path(app_dir).exists())

        # ② 应用目录自带 marker（exe 直接放在数据盘上）
        app_dir2 = tmp / "app_portable"
        app_dir2.mkdir(parents=True)
        meta = app_paths.write_marker(app_dir2, data_root_id="id-app")
        rp = app_paths.resolve_paths(AppConfig(), app_dir_override=app_dir2, volumes=[])
        check("应用目录 marker → source=app_dir",
              rp.data_root_source == app_paths.SOURCE_APP_DIR)
        check("识别出 marker 身份", rp.data_root_id == "id-app")

        # ③ 配置的数据根存在 → 直接采纳
        vol_e = tmp / "volE"
        data_root = vol_e / "MediaData"
        data_root.mkdir(parents=True)
        cfg = AppConfig().with_updates(data_root=data_root, data_root_id="id-1")
        rp = app_paths.resolve_paths(cfg, app_dir_override=app_dir, volumes=[vol_e])
        check("配置的数据根存在 → source=configured",
              rp.data_root_source == app_paths.SOURCE_CONFIGURED)
        check("数据库相对数据根解析",
              rp.db_path == data_root / "data" / "media_manager.db")
        check("缩略图相对数据根解析",
              rp.thumbnail_cache_dir == data_root / "data" / ".thumbnails")
        check("PIN 落在数据根内",
              str(rp.pin_path).startswith(str(data_root)))
        cfg2 = app_paths.apply_resolved_paths(cfg, rp)
        from config import pin_file_path as _pin_path
        check("apply 后 PIN 落点切到数据根",
              _pin_path(app_paths.default_config_path()) == data_root / "data" / ".web_pin")
        check("apply 后数据根写回配置", cfg2.data_root == data_root)
        configure_pin_dir(None)   # 复位，避免影响后续用例

        # ④ 配置的路径不存在，但在别的"卷"上按 marker 找到了（换盘符）
        other_vol = tmp / "volF"
        moved = other_vol / "MediaData"
        moved.mkdir(parents=True)
        app_paths.write_marker(moved, data_root_id="id-1")
        # 原路径真的消失（模拟盘符变了：盘还在，但旧盘符指不到这份数据）
        shutil.rmtree(data_root)
        rp = app_paths.resolve_paths(cfg, app_dir_override=app_dir, volumes=[other_vol])
        check("换盘符后按 marker 找到数据根 → discovered",
              rp.data_root_source == app_paths.SOURCE_DISCOVERED
              and rp.data_root == moved)
        check("找到后给出路径变化告警",
              any("变化" in w for w in rp.warnings))

        # ⑤ 多个候选且身份都不匹配 → 不自动选，状态为 missing
        decoy = tmp / "volG" / "MediaData"
        decoy.mkdir(parents=True)
        app_paths.write_marker(decoy, data_root_id="id-other")
        cfg_missing = AppConfig().with_updates(
            data_root=tmp / "not_here" / "MediaData", data_root_id="id-unknown")
        rp = app_paths.resolve_paths(cfg_missing, app_dir_override=app_dir,
                                     volumes=[other_vol, tmp / "volG"])
        check("无法消歧时不自动选（missing_expected）",
              rp.data_root_source == app_paths.SOURCE_MISSING and not rp.ok)
        check("记录期望位置供界面提示",
              rp.expected_data_root == tmp / "not_here" / "MediaData")
        kept = app_paths.apply_resolved_paths(cfg_missing, rp)
        check("missing 时不清空配置里的数据根（插回盘仍能找回）",
              kept.data_root == tmp / "not_here" / "MediaData")
    finally:
        configure_pin_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 3. 路径改库
# ============================================================

def _seed_library(db_path: Path, old_prefix: Path, thumb_dir: Path) -> None:
    """造一份"路径都在 old_prefix 下"的最小库（含封面与白名单）。"""
    DatabaseManager.dispose()
    init_db(db_path)
    migrate_db()
    unit_dir = old_prefix / "片段A"
    unit_dir.mkdir(parents=True, exist_ok=True)
    file_path = unit_dir / "a.jpg"
    file_path.write_bytes(b"x")
    with DatabaseManager.session() as session:
        root = q.add_library_root(session, str(old_prefix))
        unit = q.create_unit(session, str(unit_dir), "片段A", root.id,
                             file_count=1, total_size=1)
        q.set_unit_cover(session, unit.id, str(file_path))
        q.insert_media_file(session, path=str(file_path), filename="a.jpg",
                            extension=".jpg", media_type="image",
                            size_bytes=1, resource_unit_id=unit.id)
        q.add_whitelist(session, str(file_path))
    # 缩略图缓存 + meta（改库必须同步改写，否则缓存全部失效）
    thumb_dir.mkdir(parents=True, exist_ok=True)
    (thumb_dir / "1_thumb.jpg").write_bytes(b"jpeg")
    (thumb_dir / "1_thumb.jpg.meta").write_text(json.dumps({
        "source_path": str(file_path), "source_mtime": 1.0, "source_size": 1,
    }), encoding="utf-8")


def test_path_remap():
    """改库：dry-run 只读、apply 生效、幂等、冲突拒绝、meta 同步。"""
    section("测试 L3: 路径改库（盘符变化）")
    from app.services import path_remap_service as remap

    tmp = Path(tempfile.mkdtemp(prefix="lite_remap_"))
    db_path = tmp / "db" / "m.db"
    old_prefix = tmp / "volD" / "Media"
    thumb_dir = tmp / "thumbs"
    try:
        _seed_library(db_path, old_prefix, thumb_dir)

        counts = None
        with DatabaseManager.session() as session:
            counts = q.count_paths_with_prefix(session, str(old_prefix))
        check("统计命中根/单元/文件",
              counts["library_roots"] == 1 and counts["units"] == 1
              and counts["files"] == 1)
        check("统计封面路径", counts["unit_covers"] == 1)
        check("统计白名单路径", counts["whitelist"] == 1)

        new_prefix = tmp / "volE" / "Media"
        plan = remap.plan_remap(old_prefix, new_prefix, include_whitelist=True,
                                cache_dir=thumb_dir)
        check("dry-run 报告影响行数", plan.total_rows == 3)
        check("dry-run 命中缩略图 meta", plan.thumbnail_meta_hits == 1)
        check("dry-run 不判定阻塞", plan.blocking is False)
        with DatabaseManager.session() as session:
            check("dry-run 未改动数据库",
                  q.count_paths_with_prefix(session, str(old_prefix))["files"] == 1)

        # 先造一个"目标位置已存在"的冲突
        with DatabaseManager.session() as session:
            conflict_unit = q.create_unit(session, str(new_prefix / "片段A"),
                                          "片段A", 1, file_count=0, total_size=0)
        plan2 = remap.plan_remap(old_prefix, new_prefix, cache_dir=thumb_dir)
        check("目标已有同路径 → 判定冲突", len(plan2.conflicts) >= 1)
        try:
            remap.apply_remap(old_prefix, new_prefix, cache_dir=thumb_dir)
            check("冲突时 apply 必须拒绝", False)
        except remap.RemapError:
            check("冲突时 apply 必须拒绝", True)
        # 清掉冲突行再继续
        with DatabaseManager.session() as session:
            q.delete_resource_unit(session, conflict_unit.id)

        result = remap.apply_remap(old_prefix, new_prefix,
                                   include_whitelist=True, cache_dir=thumb_dir)
        check("apply 返回各表行数",
              result["updated"]["library_roots"] == 1
              and result["updated"]["units"] == 1
              and result["updated"]["files"] == 1)
        check("apply 一并改写封面", result["updated"]["unit_covers"] == 1)
        check("apply 一并改写白名单", result["updated"]["whitelist"] == 1)
        check("apply 改写缩略图 meta", result["thumbnail_meta"] == 1)
        check("apply 前自动备份数据库", bool(result["backup"])
              and Path(result["backup"]).is_file())

        with DatabaseManager.session() as session:
            check("库里已无旧前缀",
                  q.count_paths_with_prefix(session, str(old_prefix))["files"] == 0)
            check("新前缀下有 1 个文件",
                  q.count_paths_with_prefix(session, str(new_prefix))["files"] == 1)

        meta = json.loads((thumb_dir / "1_thumb.jpg.meta").read_text(encoding="utf-8"))
        check("meta.source_path 已指向新位置",
              meta["source_path"] == str(new_prefix / "片段A" / "a.jpg"))
        check("meta 其它字段保留", meta["source_size"] == 1)

        second = remap.apply_remap(old_prefix, new_prefix, cache_dir=thumb_dir)
        check("重复改库幂等（0 行）", second["updated"]["files"] == 0)

        same = remap.plan_remap(new_prefix, new_prefix)
        check("新旧相同时判定无需改库", same.reason != "" and same.same)

        # 候选搜索：盘符/挂载点变化后按相对结构或目录名找回
        # （这里把 tmp 当成"卷根"：真实盘符在测试里造不出来）
        new_prefix.mkdir(parents=True, exist_ok=True)
        candidates = remap.find_remap_candidates(old_prefix, volumes=[tmp])
        check("按目录名找到候选位置",
              any(c.path == new_prefix for c in candidates))
        check("前缀归一化去掉结尾分隔符",
              remap.normalize_prefix(str(new_prefix) + os.sep)
              == remap.normalize_prefix(str(new_prefix)))
        check("候选里不含旧路径自身",
              all(remap.normalize_prefix(c.path)
                  != remap.normalize_prefix(old_prefix) for c in candidates))
    finally:
        DatabaseManager.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_broken_root_and_guard():
    """不可达根的报告 + 扫描守卫（同尾旧根必须先决定改库）。"""
    section("测试 L4: 不可达根与扫描守卫")
    from app.services import path_remap_service as remap

    tmp = Path(tempfile.mkdtemp(prefix="lite_guard_"))
    db_path = tmp / "m.db"
    try:
        old_root = tmp / "volD" / "Media"
        old_root.mkdir(parents=True)
        (old_root / "片段A").mkdir(parents=True)
        # 每个单元至少 2 个媒体文件：扫描器把"只有 1 个零散文件"的叶子视为噪声
        # （MIN_FILES_FOR_LEAF_UNIT），造 1 个文件会扫出 0 个单元。
        for n in range(2):
            Image.new("RGB", (16, 16), (1, 2, n)).save(old_root / "片段A" / f"a{n}.jpg")
        DatabaseManager.dispose()
        init_db(db_path)
        migrate_db()
        from app.services.scan_service import run_scan

        outcome = run_scan(AppConfig().with_updates(
            db_path=db_path, thumbnail_cache_dir=tmp / "th"), old_root)
        check("首次扫描成功落库", outcome.ok and outcome.saved.new_files == 2)

        # 模拟"换盘符"：目录搬到另一个卷路径
        new_root = tmp / "volE" / "Media"
        new_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_root), str(new_root))
        check("旧路径已不存在（模拟盘符变化）", not old_root.is_dir())

        broken = remap.find_broken_roots(volumes=[tmp])
        check("报告 1 个不可达根", len(broken) == 1)
        check("不可达原因标为目录不存在",
              broken[0]["reason"] == "path_missing")
        check("给出候选位置",
              any(c["path"] == str(new_root) for c in broken[0]["candidates"]))

        guard = remap.find_equivalent_unreachable_root(new_root, volumes=[tmp])
        check("扫描守卫识别出同尾旧根", guard is not None and guard["root_id"] is not None)

        from app.services.scan_service import RootRemapRequired
        try:
            run_scan(AppConfig().with_updates(
                db_path=db_path, thumbnail_cache_dir=tmp / "th"), new_root)
            check("守卫必须拦下扫描（否则会多出一套同名单元）", False)
        except RootRemapRequired as e:
            check("守卫必须拦下扫描（否则会多出一套同名单元）", True)
            check("守卫携带候选位置", bool(e.candidates))

        # 改库后即可正常扫描
        remap.apply_remap(old_root, new_root, cache_dir=tmp / "th")
        outcome2 = run_scan(AppConfig().with_updates(
            db_path=db_path, thumbnail_cache_dir=tmp / "th"), new_root)
        check("改库后扫描恢复正常", outcome2.ok)
        with DatabaseManager.session() as session:
            check("库里仍然只有一个根", len(q.get_all_roots(session)) == 1)
            check("库里仍然只有一个单元", len(q.get_all_active_units(session)) == 1)
    finally:
        DatabaseManager.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 4. 数据迁移服务
# ============================================================

def test_lan_access_urls():
    """手机访问地址：回环 vs 0.0.0.0 的区别必须清晰（用户的真实困惑点）。

    `127.0.0.1` 是回环地址，手机永远连不上；只有监听 `0.0.0.0` 并给出本机
    局域网 IP 时手机才能访问。这里守住"地址列表里不得出现回环/自分配地址"这件事。
    """
    section("测试 L8: 手机访问地址")
    from app.utils import net

    check("识别回环地址", net.is_loopback_host("127.0.0.1")
          and net.is_loopback_host("localhost") and net.is_loopback_host("::1"))
    check("0.0.0.0 不算回环", not net.is_loopback_host("0.0.0.0"))

    only_local = net.lan_urls("127.0.0.1", 19527)
    check("只监听本机时仅返回本机地址",
          only_local == ["http://127.0.0.1:19527"])

    all_ifaces = net.lan_urls("0.0.0.0", 19527)
    check("监听 0.0.0.0 时第一个是本机地址",
          all_ifaces and all_ifaces[0] == "http://127.0.0.1:19527")
    check("地址列表无重复", len(all_ifaces) == len(set(all_ifaces)))
    check("不含回环之外的 127.* 地址",
          all(not u.startswith("http://127.") for u in all_ifaces[1:]))
    check("不含 APIPA 自分配地址（169.254.*）",
          all("169.254." not in u for u in all_ifaces))
    check("portal_url 把 0.0.0.0 显示成 127.0.0.1",
          net.portal_url("0.0.0.0", 19527) == "http://127.0.0.1:19527"
          and net.portal_url("192.168.1.5", 19527) == "http://192.168.1.5:19527")


def test_api_settings_lan_urls():
    """设置端点必须把"手机该输什么地址"告诉客户端（平板浏览器算不出服务端 IP）。"""
    section("测试 L9: 设置端点透出访问地址")
    from fastapi.testclient import TestClient
    from app.api.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="lite_lan_"))
    db_path = tmp / "m.db"
    try:
        DatabaseManager.dispose()
        init_db(db_path)
        migrate_db()
        # ① 只监听本机：必须如实上报 loopback_only，界面据此提示手机连不上
        cfg = AppConfig().with_updates(profile="lite", db_path=db_path,
                                       thumbnail_cache_dir=tmp / "th",
                                       api_host="127.0.0.1", api_port=19527)
        client = TestClient(create_app(cfg, {"config_path": str(tmp / "c.json")}))
        data = client.get("/api/settings").json()
        check("透出监听地址与端口",
              data["api_host"] == "127.0.0.1" and data["api_port"] == 19527)
        check("回环监听被标记", data["loopback_only"] is True)
        check("回环时只给本机地址",
              data["lan_urls"] == ["http://127.0.0.1:19527"])

        # ② 监听全部网卡：应给出局域网地址（多网卡时可能是多个）
        cfg2 = cfg.with_updates(api_host="0.0.0.0")
        client2 = TestClient(create_app(cfg2, {"config_path": str(tmp / "c.json")}))
        data2 = client2.get("/api/settings").json()
        check("0.0.0.0 不标记回环", data2["loopback_only"] is False)
        check("0.0.0.0 时第一个地址仍是本机地址",
              data2["lan_urls"][0] == "http://127.0.0.1:19527")
        check("地址都带端口",
              all(f":{data2['api_port']}" in u for u in data2["lan_urls"]))
    finally:
        DatabaseManager.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_portable_default_host():
    """便携版默认必须监听 0.0.0.0 —— 回归"手机输 IP 进不去"这个真实报障。

    根因：`AppConfig.api_host` 的 dataclass 默认值是 `127.0.0.1`（回环），而便携版
    首次运行没有 config.json，于是回落到它 → 平板自己能开、手机永远连不上。
    所以入口必须把"默认值"当成用户没表态，只有 config.json 显式写的才认。
    """
    section("测试 L10: 便携版默认监听地址")
    import argparse
    from main_web import _resolve_host

    tmp = Path(tempfile.mkdtemp(prefix="lite_host_"))
    try:
        ns = argparse.Namespace(host=None)
        none_cfg = tmp / "missing.json"
        check("无 config.json 时默认监听 0.0.0.0（不是回环）",
              _resolve_host(ns, AppConfig(), none_cfg) == "0.0.0.0")

        blank = tmp / "blank.json"
        blank.write_text('{"api_port": 19527}', encoding="utf-8")
        check("config.json 没写 api_host 时仍默认 0.0.0.0",
              _resolve_host(ns, AppConfig(), blank) == "0.0.0.0")

        explicit = tmp / "explicit.json"
        explicit.write_text('{"api_host": "127.0.0.1"}', encoding="utf-8")
        check("config.json 显式写回环时尊重用户选择",
              _resolve_host(ns, AppConfig(), explicit) == "127.0.0.1")

        check("--host 优先级最高",
              _resolve_host(argparse.Namespace(host="192.168.1.9"),
                            AppConfig(), explicit) == "192.168.1.9")

        os.environ["MEDIA_API_HOST"] = "10.0.0.8"
        try:
            check("环境变量 MEDIA_API_HOST 次之",
                  _resolve_host(ns, AppConfig(), explicit) == "10.0.0.8")
        finally:
            os.environ.pop("MEDIA_API_HOST", None)
        check("清理后回到 config.json 显式值",
              _resolve_host(ns, AppConfig(), explicit) == "127.0.0.1")

        # 首次运行会先落盘配置（记住数据根）。落盘时必须写 0.0.0.0，否则
        # "刚落盘的 dataclass 默认值"会在同一轮里被当成用户选择（自相矛盾）。
        from main_web import _config_file_host
        check("落盘用的监听地址默认写 0.0.0.0（不写 127.0.0.1）",
              _config_file_host(none_cfg) == "0.0.0.0"
              and _config_file_host(blank) == "0.0.0.0")
        check("已显式配置时落盘沿用用户值",
              _config_file_host(explicit) == "127.0.0.1")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_unset_data_root():
    """平板首次运行（config.json 里从未写过 data_root）也要能找到盘上的数据。

    这是"电脑迁到移动硬盘 → 插到平板接着用"的第一步：不搜就只能看到空库。
    搜到多个候选时**不许猜**，必须让用户去设置里接管。
    """
    section("测试 L12: 首次运行自动发现数据根")
    tmp = Path(tempfile.mkdtemp(prefix="lite_discover_"))
    try:
        app_dir = tmp / "app"
        app_dir.mkdir(parents=True)
        vol = tmp / "volX"
        found_root = vol / "MediaData"
        found_root.mkdir(parents=True)
        app_paths.write_marker(found_root, data_root_id="id-travel")

        # 默认（桌面档）不开启搜索：开发机不会被别的盘"顺手接管"
        rp = app_paths.resolve_paths(AppConfig(), app_dir_override=app_dir, volumes=[vol])
        check("默认不搜索（桌面档行为不变）",
              rp.data_root is None and rp.data_root_source == app_paths.SOURCE_NONE)

        # 便携版入口开启搜索：唯一命中 → 采纳
        rp = app_paths.resolve_paths(AppConfig(), app_dir_override=app_dir, volumes=[vol],
                                     discover_when_unset=True)
        check("开启搜索后可发现唯一数据根",
              rp.data_root == found_root
              and rp.data_root_source == app_paths.SOURCE_DISCOVERED)
        check("发现后给出可读告警",
              any("自动采纳" in w for w in rp.warnings))
        check("识别出 marker 身份", rp.data_root_id == "id-travel")
        check("数据库落在发现的数据根内",
              rp.db_path == found_root / "data" / "media_manager.db")

        # 两个候选：不许猜（否则可能接到另一个盘上的旧副本）
        other_root = (tmp / "volY" / "MediaData")
        other_root.mkdir(parents=True)
        app_paths.write_marker(other_root, data_root_id="id-other")
        rp = app_paths.resolve_paths(AppConfig(), app_dir_override=app_dir,
                                     volumes=[tmp / "volX", tmp / "volY"],
                                     discover_when_unset=True)
        check("多个候选时不自动选择", rp.data_root is None)
        check("多个候选时列出候选并提示接管",
              any("2 个数据根" in w for w in rp.warnings)
              and len(rp.candidates) == 2)

        # 一个都没有：保持旧行为，不凭空造数据根
        empty_vol = tmp / "volZ"
        empty_vol.mkdir(parents=True)
        rp = app_paths.resolve_paths(AppConfig(), app_dir_override=app_dir,
                                     volumes=[empty_vol], discover_when_unset=True)
        check("无候选时不新建数据根",
              rp.data_root is None and rp.data_root_source == app_paths.SOURCE_NONE)

        # 搜索顺序：非系统卷优先（数据根不会放系统盘，而系统盘目录最多）
        ordered = app_paths._ordered_volumes([Path("C:/"), Path("Y:/"), Path("D:/")])
        check("系统卷排在最后搜索", ordered[-1] == Path("C:/")
              and ordered[0] == Path("D:/"))

        # 关键回归：某个卷把浏览预算耗光，不得让后面的卷"一个目录都不看"
        big_vol = tmp / "big_vol"
        big_vol.mkdir(parents=True)
        for i in range(12):
            (big_vol / f"d{i}").mkdir()
        real_vol = tmp / "real_vol"
        real_root = real_vol / "MediaData"
        real_root.mkdir(parents=True)
        app_paths.write_marker(real_root, data_root_id="id-budget")
        found = app_paths.iter_marker_dirs([big_vol, real_vol], max_entries=3)
        check("单卷超预算不影响其它卷（真实事故回归）", real_root in found)
    finally:
        configure_pin_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_adopt_data_root():
    """接管已有数据根：**不复制、不覆盖**，原数据一个单元都不能少。

    对应真实流程：电脑上「迁移数据」到移动硬盘 → 插到平板 → 平板接管。
    这里直接把 relocate 的产物当"电脑迁过去的那份数据"，再用另一份空配置接管它。
    """
    section("测试 L13: 接管已有数据根")
    from app.db.engine import DatabaseManager
    from app.db.migrations import init_db, migrate_db
    from app.services import data_relocation_service as relocation

    tmp = Path(tempfile.mkdtemp(prefix="lite_adopt_"))
    try:
        # ① 造"电脑上那份数据"：一个含 1 个单元（2 个文件）的库
        pc_root = tmp / "pc_data"
        pc_db = pc_root / "data" / "media_manager.db"
        pc_db.parent.mkdir(parents=True, exist_ok=True)
        DatabaseManager.dispose()
        init_db(pc_db)
        migrate_db()
        unit_dir = tmp / "media" / "旅行照片"
        unit_dir.mkdir(parents=True)
        _make_media_tree(unit_dir, unit_count=1, files_per_unit=2)
        from app.services.scan_service import run_scan
        outcome = run_scan(AppConfig().with_updates(
            db_path=pc_db, thumbnail_cache_dir=pc_root / "data" / ".thumbnails"), unit_dir)
        check("电脑侧：库里已有 1 个单元", outcome.ok and outcome.saved.new_files == 2)
        pc_cfg = AppConfig().with_updates(
            data_root=pc_root, db_path=pc_db,
            thumbnail_cache_dir=pc_root / "data" / ".thumbnails")
        # ② 迁到"移动硬盘"（这就是电脑上点「迁移数据」做的事）
        drive_root = tmp / "driveE" / "MediaData"
        drive_root.mkdir(parents=True)
        moved = relocation.relocate_data_root(pc_cfg, drive_root, mode="copy")
        check("电脑迁移成功", moved["ok"] is True)
        check("移动硬盘上留下 marker",
              app_paths.read_marker(drive_root) is not None)
        check("移动硬盘上有数据库",
              Path(moved["db_path"]).is_file())

        # ③ 平板视角：一份全新的空库 + 没有 data_root
        tablet_root = tmp / "tablet_app"
        tablet_root.mkdir(parents=True)
        tablet_db = tablet_root / "data" / "media_manager.db"
        DatabaseManager.dispose()
        init_db(tablet_db)
        migrate_db()
        tablet_cfg = AppConfig().with_updates(
            db_path=tablet_db, thumbnail_cache_dir=tablet_root / "data" / ".thumbnails")

        probe = relocation.inspect_data_root(drive_root)
        check("探测出这是已有数据的数据根", probe["is_data_root"] is True)
        check("探测出库里的单元数（1）", probe["units"] == 1)
        check("探测出数据库可用", probe["db_ok"] is True)
        empty_dir = tmp / "empty_dir"
        empty_dir.mkdir()
        check("空目录不会被误判为数据根",
              relocation.inspect_data_root(empty_dir)["is_data_root"] is False)
        check("只含空库的目录也算可接管的数据根",
              relocation.inspect_data_root(tablet_root)["is_data_root"] is True)

        adopted = relocation.adopt_data_root(tablet_cfg, drive_root)
        check("接管成功", adopted["ok"] is True and adopted["mode"] == "adopt")
        check("接管后指向盘上的库",
              Path(adopted["db_path"]) == Path(moved["db_path"]))
        check("接管身份不变（uuid 沿用）",
              (adopted["marker"] or {}).get("data_root_id")
              == (moved["marker"] or {}).get("data_root_id"))
        # 关键：盘上的库不能被平板的空库覆盖
        ok, detail = relocation.verify_database(adopted["db_path"])
        check(f"接管后原数据仍在（{detail}）", ok and "1 个单元" in detail)
        check("接管前做了备份",
              any(Path(str(Path(adopted["db_path"]).parent)).glob("*.bak_*")))
        check("接管空目录被拒绝",
              relocation.adopt_data_root(tablet_cfg, empty_dir)["ok"] is False)
        bad = relocation.adopt_data_root(tablet_cfg, tmp / "not_there")
        check("接管不存在的目录被拒绝（有可读原因）",
              bad["ok"] is False and "不存在" in bad["message"])
    finally:
        DatabaseManager.dispose()
        configure_pin_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_root_probe_and_adopt_api():
    """端点层：探测 → 拒绝覆盖 → 接管 → 立刻能读到接管过来的单元（无需重启）。"""
    section("测试 L14: 数据根探测/接管端点")
    from fastapi.testclient import TestClient
    from app.api.server import create_app
    from app.db.engine import DatabaseManager
    from app.db.migrations import init_db, migrate_db
    from app.services import data_relocation_service as relocation

    tmp = Path(tempfile.mkdtemp(prefix="lite_probe_"))
    try:
        # 造一份"盘上的数据"（1 个单元）
        src = tmp / "pc"
        src_db = src / "data" / "media_manager.db"
        src_db.parent.mkdir(parents=True, exist_ok=True)
        DatabaseManager.dispose()
        init_db(src_db)
        migrate_db()
        unit_dir = tmp / "media" / "假期"
        unit_dir.mkdir(parents=True)
        # 每单元 ≥2 个文件：扫描器有"跳过单文件叶子单元"规则（MIN_FILES_FOR_LEAF_UNIT）
        _make_media_tree(unit_dir, unit_count=1, files_per_unit=2)
        from app.services.scan_service import run_scan
        run_scan(AppConfig().with_updates(
            db_path=src_db, thumbnail_cache_dir=src / "data" / ".thumbnails"), unit_dir)
        src_cfg = AppConfig().with_updates(
            data_root=src, db_path=src_db,
            thumbnail_cache_dir=src / "data" / ".thumbnails")
        drive = tmp / "drive" / "MediaData"
        drive.mkdir(parents=True)
        relocation.relocate_data_root(src_cfg, drive, mode="copy")

        # 平板：全新空库
        app_root = tmp / "tablet"
        app_db = app_root / "data" / "media_manager.db"
        app_db.parent.mkdir(parents=True, exist_ok=True)
        DatabaseManager.dispose()
        init_db(app_db)
        migrate_db()
        cfg = AppConfig().with_updates(
            profile="lite", db_path=app_db,
            thumbnail_cache_dir=app_root / "data" / ".thumbnails",
            data_root=app_root)
        client = TestClient(create_app(cfg, {"config_path": str(tmp / "c.json")}))

        probed = client.get("/api/settings/data-root/probe",
                            params={"path": str(drive)}).json()
        check("探测端点识别已有数据根", probed["is_data_root"] is True)
        check("探测端点报出单元数", probed["units"] == 1)
        check("探测端点标记文件来源", probed["has_marker"] is True)
        empty_dir = tmp / "empty_dir"
        empty_dir.mkdir()
        check("探测空目录 → 不是数据根",
              client.get("/api/settings/data-root/probe",
                         params={"path": str(empty_dir)}).json()["is_data_root"] is False)
        check("探测拒绝相对路径",
              client.get("/api/settings/data-root/probe",
                         params={"path": "rel"}).status_code == 400)

        # 关键：copy 到"已有数据"的目录必须被拒绝（否则覆盖用户数据）
        refused = client.post("/api/settings/data-root",
                              json={"path": str(drive), "mode": "copy"})
        check("迁移到已有数据目录被拒绝（409）", refused.status_code == 409)
        check("拒绝原因写明请用接管", "接管" in refused.json()["detail"])

        # 接管空目录也拒绝
        check("接管空目录被拒绝（400）",
              client.post("/api/settings/data-root",
                          json={"path": str(empty_dir), "mode": "adopt"}
                          ).status_code in (400, 409))

        adopted = client.post("/api/settings/data-root",
                              json={"path": str(drive), "mode": "adopt"}).json()
        check("接管成功且无需重启", adopted["success"] is True
              and adopted["restart_required"] is False)
        check("响应里的 mode=adopt", adopted["mode"] == "adopt")
        units = client.get("/api/units").json().get("units", [])
        check("接管后立刻能读到盘上的单元（引擎已切库）", len(units) == 1)
    finally:
        DatabaseManager.dispose()
        configure_pin_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_relocation_robustness():
    """迁移的容错：异常必须变成 ok=False，且**回滚半份数据**（真实事故回归）。

    事故现场：用户在 GUI 里点迁移，几千个缩略图复制中途关掉程序 → 目标目录里
    数据库在、缩略图只有 428/2916、没有 marker、配置也没改 —— 而用户看到"数据
    也在里面了"，以为迁移成功了。这里守住三件事：
      ① 迁移过程中有"复制中"哨兵，半成品目录**不能被当成数据根接管**；
      ② 复制失败/异常 → ok=False（不外抛，GUI 才有机可乘提示用户）；
      ③ 失败时把本次新建的目标文件回滚掉，不留半份数据。
    """
    section("测试 L15: 迁移容错与回滚")
    from app.services import data_relocation_service as relocation

    tmp = Path(tempfile.mkdtemp(prefix="lite_robust_"))
    src_root = tmp / "src"
    db_path = src_root / "data" / "media_manager.db"
    thumb_dir = src_root / "data" / ".thumbnails"
    try:
        src_root.mkdir(parents=True)
        DatabaseManager.dispose()
        init_db(db_path)
        migrate_db()
        thumb_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (thumb_dir / f"{i}_thumb.jpg").write_bytes(b"jpeg")
        cfg = AppConfig().with_updates(data_root=src_root, db_path=db_path,
                                       thumbnail_cache_dir=thumb_dir)

        # ① 正常迁移：哨兵用完即清，marker 落地
        target = tmp / "drive" / "MediaData"
        target.mkdir(parents=True)
        ok_result = relocation.relocate_data_root(cfg, target, mode="copy")
        check("正常迁移成功", ok_result["ok"] is True)
        check("完成后不残留复制哨兵",
              not (target / relocation._COPYING_SENTINEL).exists())
        check("缩略图全部复制", ok_result["steps"]["thumbnails"]["copied"] == 5)

        # ② 复制中途失败（模拟关程序/磁盘错误）：必须回滚，不留半份数据
        target2 = tmp / "drive2" / "MediaData"
        target2.mkdir(parents=True)
        original_copy_tree = relocation.copy_tree

        def _boom(*args, **kwargs):
            raise OSError("模拟复制中断")

        relocation.copy_tree = _boom
        try:
            failed = relocation.relocate_data_root(cfg, target2, mode="copy")
        finally:
            relocation.copy_tree = original_copy_tree
        check("异常被转成 ok=False（不外抛，GUI 才有机会提示）",
              failed["ok"] is False and "中断" in failed["message"])
        check("失败后目标数据库已回滚删除",
              not (target2 / "data" / "media_manager.db").exists())
        check("失败后不残留复制哨兵",
              not (target2 / relocation._COPYING_SENTINEL).exists())
        check("回滚信息写进 steps",
              failed["steps"].get("rollback", {}).get("db_removed") is True)

        # ③ 半成品目录（有库 + 有哨兵）不得被当成数据根接管
        half = tmp / "half" / "MediaData"
        (half / "data").mkdir(parents=True)
        shutil.copy2(str(db_path), str(half / "data" / "media_manager.db"))
        (half / relocation._COPYING_SENTINEL).write_text("{}", encoding="utf-8")
        probe = relocation.inspect_data_root(half)
        check("探测识别出未完成的迁移", probe["copy_incomplete"] is True)
        check("未完成的迁移不被当成数据根", probe["is_data_root"] is False)
        check("探测详情写明原因", "未完成的迁移" in probe["db_detail"])
        adopted = relocation.adopt_data_root(cfg, half)
        check("拒绝接管未完成的迁移",
              adopted["ok"] is False and "未完成的迁移" in adopted["message"])

        # ④ 手动删掉哨兵（用户确认数据完整）后即可接管
        (half / relocation._COPYING_SENTINEL).unlink()
        probe2 = relocation.inspect_data_root(half)
        check("删掉哨兵后可识别为数据根", probe2["is_data_root"] is True)
    finally:
        DatabaseManager.dispose()
        configure_pin_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_volume_scan_and_gate():
    """盘符扫描 + “未加载数据”闸门（平板换盘符场景的核心）。

    事故背景：平板插上硬盘（盘符经常变），老版本只会在“配置过、但目录不在”时
    搜索，且找不到就回退到应用目录建空库，界面也无处可选数据 —— 用户只能看到
    一个空库，以为要重新建库。这里守住：
      ① 卷序列号/卷标对**目录**参数也能拿到（之前传目录给 Win32 恒返回空）；
      ② 单卷扫描能控深度（深层目录名靠“深扫”才能找到）；
      ③ /data-root/volumes 如实列出卷与卷上的数据根，并标出“上次用的那份”；
      ④ blocked_no_root = 数据端点全部 409，但向导/能力查询可用；
      ⑤ 接管后闸门立即解除，无需重启。
    """
    section("测试 L16: 盘符扫描 + 未加载数据闸门")
    from fastapi.testclient import TestClient
    from app.api import server as server_mod
    from app.api.server import create_app
    from app.db.engine import DatabaseManager
    from app.db.migrations import init_db, migrate_db
    from app.services import data_relocation_service as relocation
    from app.services import path_remap_service as remap_svc

    tmp = Path(tempfile.mkdtemp(prefix="lite_volscan_"))
    try:
        # ① 卷序列号：传目录与传卷根结果一致且非空（Windows）
        drive = tmp / "drive"
        drive.mkdir(parents=True, exist_ok=True)
        vol_root = Path(app_paths.volume_of(drive) or str(drive))
        check("卷序列号对目录参数也能取到（不再恒为空）",
              bool(app_paths.volume_serial(drive)) == bool(app_paths.volume_serial(vol_root)))
        check("卷标同理不因传目录而丢",
              app_paths.volume_label(drive) == app_paths.volume_label(vol_root))

        # ② 单卷扫描：深 4 层的数据根浅扫找不到、深扫能找到
        deep_root = drive / "a" / "b" / "c" / "MediaData"
        deep_root.mkdir(parents=True)
        app_paths.write_marker(deep_root, db_name="media_manager.db")
        check("浅扫（depth=2）找不到深层数据根",
              deep_root not in app_paths.scan_volume_data_roots(drive, depth=2))
        check("深扫（depth=4）能找到深层数据根",
              deep_root in app_paths.scan_volume_data_roots(drive, depth=4))

        # ③ volumes 端点：把 fake 卷注入进去，避免依赖真实盘
        # 造一个带数据的根（有 marker + 可用数据库）
        src_db = drive / "data" / "media_manager.db"
        DatabaseManager.dispose()
        init_db(src_db)
        migrate_db()
        cfg = AppConfig().with_updates(data_root=drive, db_path=src_db,
                                      thumbnail_cache_dir=drive / "data" / ".thumbnails")
        meta = app_paths.read_marker(deep_root) or {}

        original_infos = remap_svc.list_volume_infos
        remap_svc.list_volume_infos = lambda volumes=None: [
            remap_svc.VolumeInfo(path=drive, label="TestVol",
                                 serial=app_paths.volume_serial(drive),
                                 total=0, free=0)]
        try:
            rt = {"config_path": str(tmp / "config.json"),
                  "data_root": str(drive), "data_root_status": "configured",
                  "data_root_id": str((app_paths.read_marker(drive) or {}).get("data_root_id") or "")}
            client = TestClient(create_app(cfg, rt))
            data = client.get("/api/settings/data-root/volumes?deep=true").json()
            found = [r["path"] for v in data["volumes"] for r in v["data_roots"]]
            check("volumes 端点列出卷上的数据根", any(Path(p) == deep_root for p in found))
            check("深扫结果里带单元/文件数（供界面判断是不是同一份）",
                  all("units" in r for v in data["volumes"] for r in v["data_roots"]))
            check("磁盘容量信息透出",
                  data["volumes"] and data["volumes"][0]["label"] == "TestVol")
        finally:
            remap_svc.list_volume_infos = original_infos

        # ④ 闸门：blocked_no_root → 数据端点 409，向导/能力查询 200
        db2 = tmp / "app2" / "data" / "media_manager.db"
        cfg2 = AppConfig().with_updates(db_path=db2, thumbnail_cache_dir=tmp / "app2" / "data" / ".thumbnails")
        rt2 = {"config_path": str(tmp / "app2" / "config.json"), "data_root": "",
               "data_root_status": "blocked_no_root"}
        client2 = TestClient(create_app(cfg2, rt2))
        check("blocked: /api/units 被挡（409）", client2.get("/api/units").status_code == 409)
        check("blocked: /api/library/roots 被挡（409）",
              client2.get("/api/library/roots").status_code == 409)
        check("blocked: /api/features 可用（前端靠它进向导）",
              client2.get("/api/features").status_code == 200)
        check("blocked: 数据盘扫描可用",
              client2.get("/api/settings/data-root/volumes").status_code == 200)
        check("blocked: /api/health 可用", client2.get("/api/health").status_code == 200)

        # ⑤ 接管后：闸门立即解除、状态变 configured
        adopt_target = tmp / "drive2" / "MediaData"
        adopt_target.mkdir(parents=True)
        # 用前面造好的那份数据（cfg 指向 drive）迁过去，模拟“电脑上迁到硬盘”
        relocation.relocate_data_root(cfg, adopt_target, mode="copy")
        res = client2.post("/api/settings/data-root",
                           json={"path": str(adopt_target), "mode": "adopt"})
        check("接管成功", res.status_code == 200 and res.json().get("success") is True)
        check("接管后闸门解除（/api/units 不再 409）",
              client2.get("/api/units").status_code == 200)
        check("接管后状态为 configured",
              client2.get("/api/features").json().get("data_root_status") == "configured")

        # ⑥ 新建数据根：空目录可以，已有数据的目录拒绝（不覆盖）
        fresh = tmp / "fresh" / "MediaData"
        fresh.mkdir(parents=True)
        created = relocation.create_data_root(cfg2, fresh)
        check("空目录新建数据根成功", created["ok"] is True)
        check("新建后有 marker", (fresh / app_paths.MARKER_FILENAME).is_file())
        check("新建后有空库", Path(created["db_path"]).is_file())
        again = relocation.create_data_root(cfg2, fresh)
        check("已有数据的目录拒绝新建（不覆盖）", again["ok"] is False)
    finally:
        DatabaseManager.dispose()
        configure_pin_dir(None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_remap_suggest_api():
    """盘符变化后的一键改库建议端点（盘内搬家 → 单根映射）。"""
    section("测试 L17: 改库建议端点")
    from fastapi.testclient import TestClient
    from app.api.server import create_app
    from app.db.engine import DatabaseManager
    from app.db.migrations import init_db, migrate_db
    from app.db import queries as q
    from app.db.models import MediaFile, ResourceUnit
    from app.services import path_remap_service as remap_svc

    tmp = Path(tempfile.mkdtemp(prefix="lite_suggest_"))
    try:
        db = tmp / "data" / "media_manager.db"
        DatabaseManager.dispose()
        init_db(db)
        migrate_db()
        old_prefix = r"D:\__lite_old__"
        new_prefix = str(tmp / "__lite_new__")
        with DatabaseManager.session() as session:
            root = q.add_library_root(session, old_prefix)
            unit = ResourceUnit(name="u", path=old_prefix + r"\u",
                                library_root_id=root.id, status="active")
            session.add(unit)
            session.flush()
            for i in range(3):
                session.add(MediaFile(
                    resource_unit_id=unit.id,
                    path=old_prefix + rf"\u\f{i}.jpg", filename=f"f{i}.jpg",
                    extension=".jpg", media_type="image", size_bytes=1))
        root_id = root.id

        fake_broken = [{
            "id": root_id, "path": old_prefix + r"\u", "enabled": True,
            "path_exists": False, "volume_present": True, "reason": "path_missing",
            "candidates": [{"path": new_prefix + r"\u", "kind": "same_relative",
                            "media_count": 3}],
        }]
        original = remap_svc.find_broken_roots
        remap_svc.find_broken_roots = lambda *a, **k: fake_broken
        try:
            cfg = AppConfig().with_updates(db_path=db, thumbnail_cache_dir=tmp / ".thumbnails")
            client = TestClient(create_app(cfg, {"config_path": str(tmp / "config.json")}))
            payload = client.get("/api/library/remap/suggest").json()
            suggestions = payload.get("suggestions") or []
            check("识出一条盘符变化映射", len(suggestions) == 1)
            if suggestions:
                item = suggestions[0]
                check("映射给出旧→新前缀", item["old_prefix"] and item["new_prefix"])
                check("影响行数已算好（库根+单元+文件）", int(item["total_rows"]) >= 3)
                check("附带了受影响的根", len(item["roots"]) == 1)
                check("预演无冲突才允许一键执行", item["blocking"] is False)
        finally:
            remap_svc.find_broken_roots = original
    finally:
        DatabaseManager.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_data_relocation():
    """备份 / 拷贝 / 数据根迁移（copy 保留源、move 校验后删源）。"""
    section("测试 L5: 数据迁移服务")
    from app.services import data_relocation_service as reloc

    tmp = Path(tempfile.mkdtemp(prefix="lite_reloc_"))
    src_root = tmp / "src"
    db_path = src_root / "data" / "media_manager.db"
    thumb_dir = src_root / "data" / ".thumbnails"
    try:
        src_root.mkdir(parents=True)
        DatabaseManager.dispose()
        init_db(db_path)
        migrate_db()
        with DatabaseManager.session() as session:
            q.add_library_root(session, str(tmp / "volD" / "Media"))
        thumb_dir.mkdir(parents=True, exist_ok=True)
        (thumb_dir / "9_thumb.jpg").write_bytes(b"jpeg")
        (thumb_dir / "9_thumb.jpg.meta").write_text(
            json.dumps({"source_path": "x"}), encoding="utf-8")

        ok, message = reloc.verify_database(db_path)
        check("verify_database 通过", ok)

        backup = reloc.backup_database(db_path, reason="test")
        check("备份文件已生成", backup is not None and Path(backup).is_file())
        check("备份名带原因与时间戳", "bak_" in backup.name and "test" in backup.name)

        copied = reloc.copy_database(db_path, tmp / "copy" / "m.db")
        check("拷贝并校验成功", copied["ok"])

        cfg = AppConfig().with_updates(data_root=src_root, db_path=db_path,
                                       thumbnail_cache_dir=thumb_dir)
        target_root = tmp / "volE" / "MediaData"
        result = reloc.relocate_data_root(cfg, target_root, mode="copy")
        check("迁移成功", result["ok"])
        check("目标库结构保持 data/ 层级",
              Path(result["db_path"]) == target_root / "data" / "media_manager.db")
        check("目标缩略图目录正确",
              Path(result["thumbnail_cache_dir"]) == target_root / "data" / ".thumbnails")
        check("写出数据根 marker",
              app_paths.marker_path(target_root).is_file())
        check("缩略图已复制过去",
              (target_root / "data" / ".thumbnails" / "9_thumb.jpg").is_file())
        check("copy 模式保留源库", db_path.is_file())
        ok2, _ = reloc.verify_database(result["db_path"])
        check("目标库完整", ok2)

        # move 模式：校验通过后删源
        target2 = tmp / "volF" / "MediaData"
        result2 = reloc.relocate_data_root(cfg, target2, mode="move")
        check("move 模式成功", result2["ok"])
        check("move 后源库已删除", not db_path.is_file())
        check("move 后源缩略图已删除",
              not (thumb_dir / "9_thumb.jpg").is_file())
    finally:
        DatabaseManager.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_capabilities():
    """能力探测：同一条代码路径，功能随"包里有什么"自动降级。

    这是"便携版与普通版功能同步、差异只由编译选项决定"的技术底座：
    打包时排除 cv2 或人脸模型后，对应能力必须变 False，而**查重路由依然可用**
    （文件级查重只需要 Pillow）—— 不允许出现"整个功能消失、端点 404"。
    """
    section("测试 L11: 能力探测与降级")
    from app.utils import capabilities as caps_mod

    tmp = Path(tempfile.mkdtemp(prefix="lite_caps_"))
    try:
        caps_mod.reset_cache()
        real = caps_mod.detect()
        check("真实环境：Pillow 可用（查重前提）", real.image is True)
        check("真实环境：查重能力可用", real.dedup is True)
        check("源码态能找到人脸模型", real.face_models is True
              and real.face_model_dir.endswith("models"))
        check("face 需要模型与 cv2 同时具备",
              real.face == (real.cv2 and real.face_models))
        check("能力字典键固定", set(real.as_dict()) ==
              {"image", "cv2", "heic", "raw", "face_models", "qt"})

        # 源码态：模型目录解析必须能找到仓库里的 models/
        resolved = caps_mod.resolve_face_model_dir()
        check("模型目录解析命中且含两个模型文件",
              resolved is not None
              and (resolved / caps_mod.FACE_DETECT_MODEL).is_file()
              and (resolved / caps_mod.FACE_RECOGNIZE_MODEL).is_file())

        # 模拟"打包时排除了 cv2 与人脸模型"（编译开关的结果）
        original = caps_mod._has_module
        original_resolver = caps_mod.resolve_face_model_dir
        try:
            caps_mod._has_module = lambda name: name in ("PIL",)
            # 包内任何候选目录都没有模型文件 → 用打桩表达"这个包里没有模型"
            caps_mod.resolve_face_model_dir = lambda configured=None: None
            caps_mod.reset_cache()
            stripped = caps_mod.detect()
            check("无 cv2 包：查重仍然可用（不能连端点都消失）",
                  stripped.dedup is True)
            check("无 cv2 包：视频能力关闭", stripped.video_thumbnail is False
                  and stripped.dedup_video is False)
            check("无模型包：人脸线索关闭", stripped.face is False
                  and stripped.face_models is False)
            check("降级原因可读（含 OpenCV 与模型两条）",
                  any("OpenCV" in n for n in stripped.notes())
                  and any("人脸模型" in n for n in stripped.notes()))
        finally:
            caps_mod._has_module = original
            caps_mod.resolve_face_model_dir = original_resolver
            caps_mod.reset_cache()
        check("恢复后能力回到真实值", caps_mod.detect().dedup is True)

        # 候选优先级：配置目录里只要有完整模型文件，就必须优先用它
        custom = tmp / "custom_models"
        custom.mkdir()
        for name in (caps_mod.FACE_DETECT_MODEL, caps_mod.FACE_RECOGNIZE_MODEL):
            (custom / name).write_bytes(b"stub")
        check("配置目录含完整模型时优先命中",
              caps_mod.resolve_face_model_dir(custom) == custom)
        check("配置目录不完整时回落到其它候选（不误报）",
              caps_mod.resolve_face_model_dir(tmp / "no_models")
              == resolved)  # 源码态会回落到仓库 models/
        (custom / caps_mod.FACE_RECOGNIZE_MODEL).unlink()
        check("配置目录缺文件时不命中该目录",
              caps_mod.resolve_face_model_dir(custom) != custom)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 6. 便携版 API：与完整档同一套功能
# ============================================================

def test_lite_api():
    """便携版与完整档**同一条代码路径**：查重路由照常注册，功能由能力探测决定。

    历史教训：曾用 `profile=lite` 决定"不注册查重路由"，等于同一份代码里长出两套
    功能语义（HTTP 契约分叉、前端按档位分支、测试与文档写两遍）。现在
    `profile` 只是"这是哪个包"的标签，能力（cv2/模型/HEIC/RAW/Qt）由探测决定。
    """
    section("测试 L6: 便携版 API（与完整档同一套功能）")
    from fastapi.testclient import TestClient
    from app.api.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="lite_api_"))
    db_path = tmp / "m.db"
    try:
        DatabaseManager.dispose()
        init_db(db_path)
        migrate_db()
        cfg = AppConfig().with_updates(profile="lite", db_path=db_path,
                                       thumbnail_cache_dir=tmp / "th")
        app = create_app(cfg, {"data_root_status": "configured",
                               "data_root": str(tmp),
                               "config_path": str(tmp / "config.json")})
        client = TestClient(app)

        features = client.get("/api/features").json()
        check("features.profile 仅作标签", features["profile"] == "lite")
        check("查重可用（有 Pillow 即可，与档位无关）", features["dedup"] is True)
        check("hash_index 随查重", features["hash_index"] is True)
        check("features.scan=true", features["scan"] is True)
        check("features 透出能力快照", isinstance(features.get("capabilities"), dict)
              and {"cv2", "heic", "raw", "face_models", "qt"} <= set(features["capabilities"]))
        check("features 透出能力说明（缺失项可读）",
              isinstance(features.get("capability_notes"), list))
        check("features 透出数据根状态",
              features["data_root_status"] == "configured")

        # 关键回归：查重路由必须注册（曾因轻量档而 404，导致两套 API 契约）
        check("查重路由已注册（200）", client.get("/api/dedup/results").status_code == 200)
        check("查重计数可用", client.get("/api/dedup/counts").status_code == 200)
        check("健康检查含档位标签", client.get("/api/health").json()["profile"] == "lite")
        check("健康检查含查重能力",
              client.get("/api/health").json()["dedup"] is True)

        volumes = client.get("/api/library/volumes")
        check("卷列表可用", volumes.status_code == 200
              and len(volumes.json()["volumes"]) >= 1)
        browse = client.get("/api/library/browse", params={"path": str(tmp)})
        check("目录浏览可用", browse.status_code == 200
              and browse.json()["path"] == str(tmp))
        check("拒绝相对路径", client.get("/api/library/browse",
                                        params={"path": "relative"}).status_code == 400)
        check("相对路径不可添加", client.post("/api/library/roots",
                                          json={"path": "relative"}).status_code == 400)
        check("不存在的目录不可添加",
              client.post("/api/library/roots",
                          json={"path": str(tmp / "nope")}).status_code == 400)

        root_dir = tmp / "Media"
        root_dir.mkdir()
        _make_media_tree(root_dir, unit_count=1, files_per_unit=1)
        added = client.post("/api/library/roots", json={"path": str(root_dir)})
        check("添加媒体库成功", added.status_code == 200
              and added.json()["created"] is True)
        check("添加到库的根出现在列表里",
              any(r["path"] == str(root_dir)
                  for r in client.get("/api/library/roots").json()["roots"]))

        settings = client.get("/api/settings").json()
        check("设置端点透出数据库路径", settings["db_path"] == str(db_path))
        check("设置端点透出缓存目录", settings["thumbnail_cache_dir"] == str(tmp / "th"))

        # 缩略图缓存目录可热切换
        new_cache = tmp / "cache2"
        resp = client.post("/api/settings/thumbnail-cache",
                           json={"path": str(new_cache), "mode": "copy"})
        check("切换缩略图缓存目录成功", resp.status_code == 200
              and resp.json()["success"] is True
              and resp.json()["restart_required"] is False)
        check("切换后 config 已生效",
              str(client.get("/api/settings").json()["thumbnail_cache_dir"]) == str(new_cache))
        from app.services.cleanup_service import _resolve_cache_dir
        check("清理服务已重新注入缓存目录", _resolve_cache_dir() == new_cache)
    finally:
        DatabaseManager.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def test_scan_via_api():
    """通过 API 触发扫描长任务（便携版没有桌面 GUI，这是唯一入口）。"""
    section("测试 L7: API 扫描长任务")
    from fastapi.testclient import TestClient
    from app.api.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="lite_scan_api_"))
    db_path = tmp / "m.db"
    try:
        DatabaseManager.dispose()
        init_db(db_path)
        migrate_db()
        cfg = AppConfig().with_updates(profile="lite", db_path=db_path,
                                       thumbnail_cache_dir=tmp / "th")
        client = TestClient(create_app(cfg, {"config_path": str(tmp / "config.json")}))

        root_dir = tmp / "Media"
        _make_media_tree(root_dir, unit_count=2, files_per_unit=2)
        resp = client.post("/api/library/scan", json={"path": str(root_dir)})
        check("扫描受理（202）", resp.status_code == 202 and resp.json()["task_id"])
        task_id = resp.json()["task_id"]

        deadline = time.time() + 60
        task = None
        while time.time() < deadline:
            task = client.get(f"/api/library/scan/{task_id}").json()
            if task["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.3)
        check("扫描任务完成", task is not None and task["status"] == "completed")
        check("扫描结果含文件数", (task.get("result") or {}).get("total_files") == 4)

        roots = client.get("/api/library/roots").json()["roots"]
        check("扫描后根目录已入库",
              any(r["path"] == str(root_dir) and r["unit_count"] == 2 for r in roots))
        units = client.get("/api/units").json()["units"]
        check("扫描后单元可查询", len(units) == 2)

        with DatabaseManager.session() as session:
            check("扫描会话已收尾",
                  q.get_latest_scan_session(session).status == "completed")
    finally:
        DatabaseManager.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# main
# ============================================================

def main() -> bool:
    """按顺序执行全部便携版用例。"""
    print("=" * 60)
    print("  影视资源管理器 - 便携版（轻量档）测试")
    print("=" * 60)

    try:
        test_no_qt_dependency()
        test_profile_resolution()
        test_data_root_resolution()
        test_path_remap()
        test_broken_root_and_guard()
        test_lan_access_urls()
        test_api_settings_lan_urls()
        test_portable_default_host()
        test_capabilities()
        test_discover_unset_data_root()
        test_adopt_data_root()
        test_data_root_probe_and_adopt_api()
        test_relocation_robustness()
        test_volume_scan_and_gate()
        test_remap_suggest_api()
        test_data_relocation()
        test_lite_api()
        test_scan_via_api()
    finally:
        DatabaseManager.dispose()
        configure_pin_dir(None)
        # 清理测试可能落在仓库里的库文件（本文件只应使用临时目录）
        leftovers = [
            Path(__file__).parent.parent / "data" / "test_lite.db",
        ]
        for path in leftovers:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    total = _passed + _failed
    print(f"\n{'=' * 60}")
    print(f"  测试完成: {_passed}/{total} 通过, {_failed} 失败")
    print("  [OK] 全部通过！" if _failed == 0 else "  [WARN] 有失败项，需要修复")
    print("=" * 60)
    return _failed == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
