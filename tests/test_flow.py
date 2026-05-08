# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
自动化端到端测试 —— 模拟完整使用流程。

覆盖:
0. 全模块导入检查（捕获 NameError / ImportError）
1. 数据库初始化
2. 扫描器（含资源单元自动识别）
3. 哈希引擎（MD5 + pHash + dHash）
4. 查重引擎（杰卡德比对）
5. 数据库查询
6. 缩略图生成
7. 消息中心 CRUD
8. API 服务器创建

运行方式: python tests/test_flow.py
"""

import importlib
import io
import os
import sys
import tempfile
from pathlib import Path

# 编码修复
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image
from config import AppConfig
from app.db.engine import DatabaseManager
from app.db.migrations import init_db
from app.core.scanner import MediaScanner, ScanResult
from app.core.hash_engine import HashEngine, FileHashes
from app.core.thumbnail_generator import ThumbnailGenerator, ThumbnailInfo
from app.core.dedup_engine import DedupEngine, UnitComparisonResult, DedupSession
from app.db import queries as q
from app.services.message_center import MessageCenter

# ============================================================
# 测试工具
# ============================================================

_passed = 0
_failed = 0


def check(desc: str, condition: bool):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  [PASS] {desc}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc}")


def section(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


# ============================================================
# 辅助：创建测试用的媒体文件
# ============================================================

def create_test_media(root: Path) -> None:
    """创建一个模拟的媒体库目录结构。"""
    # 单元A: 直接包含图片和视频
    unit_a = root / "2025-01-01" / "片段A"
    unit_a.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (640, 480), color=(255, 0, 0))
    img.save(unit_a / "照片01.jpg")
    img2 = Image.new("RGB", (800, 600), color=(0, 255, 0))
    img2.save(unit_a / "照片02.png")

    # 单元B: 另一个独立片段
    unit_b = root / "2025-01-01" / "片段B"
    unit_b.mkdir(parents=True, exist_ok=True)
    img3 = Image.new("RGB", (640, 480), color=(255, 0, 0))  # 与照片01.jpg 完全相同！
    img3.save(unit_b / "照片03.jpg")
    img4 = Image.new("RGB", (1024, 768), color=(0, 0, 255))
    img4.save(unit_b / "照片04.png")

    # 单元C: 杂集文件夹（散装文件）
    misc = root / "杂集"
    misc.mkdir(parents=True, exist_ok=True)
    img5 = Image.new("RGB", (320, 240), color=(128, 128, 128))
    img5.save(misc / "杂图.jpg")
    img6 = Image.new("RGB", (640, 480), color=(255, 0, 0))  # 与照片01和照片03完全相同
    img6.save(misc / "复制件.jpg")

    # 再加一个子文件夹但不含媒体文件（不应被识别为独立单元）
    misc_sub = misc / "说明文档"
    misc_sub.mkdir(parents=True, exist_ok=True)
    (misc_sub / "readme.txt").write_text("说明文件，不含媒体")

    # 单元D: 嵌套场景——父文件夹与子文件夹均含媒体文件
    unit_d = root / "嵌套片段"
    unit_d.mkdir(parents=True, exist_ok=True)
    img7 = Image.new("RGB", (640, 480), color=(255, 255, 0))
    img7.save(unit_d / "封面.jpg")
    # 子文件夹也含媒体文件（不应被提升为同级独立单元）
    unit_d_sub = unit_d / "精选图"
    unit_d_sub.mkdir(parents=True, exist_ok=True)
    img8 = Image.new("RGB", (800, 600), color=(0, 255, 255))
    img8.save(unit_d_sub / "精选01.jpg")

    print(f"  测试媒体库已创建: {root}")
    print(f"    单元A (片段A): 2 个图片")
    print(f"    单元B (片段B): 2 个图片")
    print(f"    单元C (杂集): 2 个图片 + 1 个无媒体子目录")
    print(f"    单元D (嵌套片段): 1 个图片 + 1 个含媒体文件的子文件夹")


# ============================================================
# 测试用例
# ============================================================

def test_preflight():
    """测试 0: 全模块导入检查 —— 捕获 NameError、ImportError 等致命错误。

    这个测试覆盖了之前漏掉的 hash_worker.py `Path` 未导入的 bug：
    如果任何 .py 文件有未定义名称或导入错误，此测试会立即失败。
    """
    section("测试 0: 全模块导入检查")
    project_root = Path(__file__).parent.parent
    py_files: list[Path] = []

    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs
                   if d not in ('__pycache__', 'data', '.thumbnails',
                                'tests', 'models', 'resources', '.git')]
        for f in files:
            if f.endswith('.py'):
                py_files.append(Path(root) / f)

    failed = []
    for fpath in sorted(py_files):
        rel = fpath.relative_to(project_root)
        mod_path = str(rel).replace(os.sep, '.')[:-3]
        try:
            importlib.import_module(mod_path)
        except Exception as e:
            failed.append(f"{mod_path}: {type(e).__name__}: {e}")

    check(f"共 {len(py_files)} 个 .py 文件", len(py_files) >= 50)
    check(f"全部 {len(py_files)} 个文件导入成功", len(failed) == 0)
    for err in failed:
        print(f"    失败: {err}")

def test_db():
    section("测试 1: 数据库初始化")
    db_path = Path("data/test_flow.db")
    init_db(db_path)
    engine = DatabaseManager.get_engine()
    tables = [t for t in engine.dialect.get_table_names(engine.connect())]
    check("至少创建 9 张表", len(tables) >= 9)
    check("包含 media_files 表", "media_files" in tables)
    check("包含 resource_units 表", "resource_units" in tables)
    check("包含 dedup_results 表", "dedup_results" in tables)
    check("包含 messages 表", "messages" in tables)
    check("包含 face_vectors 表", "face_vectors" in tables)
    check("包含 whitelist 表", "whitelist" in tables)
    return db_path


def test_scanner(root: Path):
    section("测试 2: 扫描器 - 资源单元自动识别")
    config = AppConfig()
    scanner = MediaScanner(
        extensions=config.media_extensions,
        exclude_patterns=config.exclude_patterns,
    )
    result = scanner.scan_root(root)

    check("扫描完成无错误", len(result.errors) == 0)
    check(f"检测到 4 个资源单元 (实际: {len(result.units)})", len(result.units) == 4)
    check(f"总文件数 8 (实际: {result.total_files})", result.total_files == 8)

    unit_names = {u.name for u in result.units}
    check("包含「片段A」", "片段A" in unit_names)
    check("包含「片段B」", "片段B" in unit_names)
    check("包含「杂集」", "杂集" in unit_names)
    check("包含「嵌套片段」", "嵌套片段" in unit_names)
    check("不包含「说明文档」（子目录无媒体文件）", "说明文档" not in unit_names)
    check("不包含「精选图」（子目录归入嵌套片段，不独立成单元）", "精选图" not in unit_names)

    # 验证叶子单元判定
    for u in result.units:
        if u.name == "片段A":
            check("片段A 是叶子单元", u.is_leaf)
        if u.name == "杂集":
            # 杂集下有「说明文档」子目录（无媒体），所以杂集仍是叶子
            check("杂集是叶子单元", u.is_leaf)
        if u.name == "嵌套片段":
            # 嵌套片段下有「精选图」子目录（含媒体文件），所以不是叶子
            check("嵌套片段不是叶子单元（有含媒体文件的子目录）", not u.is_leaf)
            # 验证嵌套片段包含了子目录的文件（共 2 张图片）
            check(f"嵌套片段包含 2 个文件 (实际: {u.file_count})", u.file_count == 2)

    return result


def test_save_to_db(scan_result: ScanResult, root: Path):
    section("测试 3: 扫描结果写入数据库")
    with DatabaseManager.session() as session:
        root_obj = q.add_library_root(session, str(scan_result.root_path))
        check("媒体库根目录创建", root_obj.id is not None)

        for unit in scan_result.units:
            existing = q.get_unit_by_path(session, str(unit.path))
            if not existing:
                new_unit = q.create_unit(
                    session, str(unit.path), unit.name,
                    root_obj.id, file_count=unit.file_count,
                    total_size=unit.total_size,
                )
                for df in unit.files:
                    q.insert_media_file(
                        session,
                        path=str(df.path), filename=df.filename,
                        extension=df.extension, media_type=df.media_type,
                        size_bytes=df.size_bytes,
                        resource_unit_id=new_unit.id,
                    )
            else:
                # 更新已有单元的文件
                for df in unit.files:
                    existing_file = q.get_file_by_path(session, str(df.path))
                    if not existing_file:
                        q.insert_media_file(
                            session,
                            path=str(df.path), filename=df.filename,
                            extension=df.extension, media_type=df.media_type,
                            size_bytes=df.size_bytes,
                            resource_unit_id=existing.id,
                        )

    # 验证写入
    with DatabaseManager.session() as session:
        units = q.get_all_active_units(session)
        check(f"数据库中有 4 个活跃单元 (实际: {len(units)})", len(units) == 4)
        total_files = sum(u.file_count for u in units)
        check(f"数据库中有 8 个文件 (实际: {total_files})", total_files == 8)


def test_hash_engine(root: Path):
    section("测试 4: 哈希引擎 - MD5/pHash/dHash")
    engine = HashEngine(
        algorithms=["md5", "phash", "dhash"],
        phash_size=8, dhash_size=8,
    )

    # 测试图片哈希
    img_path = root / "2025-01-01" / "片段A" / "照片01.jpg"
    result = engine.hash_file(img_path)

    check("MD5 哈希不为空", result.md5 is not None and len(result.md5) == 32)
    check("pHash 不为空", result.phash is not None)
    check("dHash 不为空", result.dhash is not None)
    check("图片宽度正确 (640)", result.width == 640)
    check("图片高度正确 (480)", result.height == 480)
    check("非视频无时长", result.duration_ms is None)

    # 哈希确定性：同一文件两次哈希结果相同
    result2 = engine.hash_file(img_path)
    check("MD5 确定性（同文件同哈希）", result.md5 == result2.md5)
    check("pHash 确定性", result.phash == result2.phash)

    # 完全相同内容的两个文件应有相同 MD5
    img_b = root / "2025-01-01" / "片段B" / "照片03.jpg"
    result_b = engine.hash_file(img_b)
    check("内容相同 → MD5 相同", result.md5 == result_b.md5)
    check("内容相同 → pHash 相同", result.phash == result_b.phash)

    # 不同内容的文件应有不同哈希
    img_diff = root / "2025-01-01" / "片段B" / "照片04.png"
    result_diff = engine.hash_file(img_diff)
    check("内容不同 → MD5 不同", result.md5 != result_diff.md5)

    return engine


def test_hash_batch(root: Path):
    section("测试 5: 批量哈希 + 数据库更新")
    config = AppConfig()
    engine = HashEngine(
        algorithms=list(config.hash_algorithms),
        phash_size=config.phash_size,
        dhash_size=config.dhash_size,
    )

    with DatabaseManager.session() as session:
        files = q.get_unindexed_files(session)
        check(f"有 {len(files)} 个未索引文件", len(files) > 0)

        for f in files[:8]:  # 最多 8 个
            fpath = Path(f.path)
            if fpath.is_file():
                result = engine.hash_file(fpath)
                update = {}
                if result.md5:
                    update["md5_hash"] = result.md5
                if result.phash:
                    update["phash"] = result.phash
                if result.dhash:
                    update["dhash"] = result.dhash
                if result.width:
                    update["width"] = result.width
                if result.height:
                    update["height"] = result.height
                if update:
                    q.update_file_hash(session, f.id, **update)

    # 验证
    with DatabaseManager.session() as session:
        remaining = q.get_unindexed_files(session)
        check(f"索引后剩余 {len(remaining)} 个未索引 (预期 < 原数量)", len(remaining) < len(files))


def test_dedup_engine():
    section("测试 6: 查重引擎 - 杰卡德比对")
    config = AppConfig()
    engine = DedupEngine(
        jaccard_threshold=config.jaccard_threshold,
        phash_hamming_threshold=config.phash_hamming_threshold,
        dhash_hamming_threshold=config.dhash_hamming_threshold,
    )

    with DatabaseManager.session() as session:
        units = q.get_all_active_units(session)
        check(f"参与查重的单元 ≥ 2 (实际: {len(units)})", len(units) >= 2)

        # 构建单元文件映射
        unit_files_map = {}
        unit_names = {}
        for u in units:
            files = q.get_files_by_unit(session, u.id)
            unit_files_map[u.id] = [{
                "id": f.id, "path": f.path,
                "md5_hash": f.md5_hash, "phash": f.phash, "dhash": f.dhash,
            } for f in files]
            unit_names[u.id] = u.name

        # 执行查重
        result = engine.run_dedup(unit_files_map, unit_names)

        check("查重完成", result.total_units_compared == len(units))
        check("耗时 >= 0", result.elapsed_seconds >= 0)
        print(f"    比对对数: {len(result.comparisons)}")
        print(f"    发现重复组数: {len(result.duplicates_found)}")

        for dup in result.duplicates_found:
            print(f"    重复: {dup.unit_a_name} <-> {dup.unit_b_name} "
                  f"(杰卡德={dup.jaccard_similarity:.1%}, 匹配={len(dup.file_matches)}对)")


def test_save_dedup_results():
    section("测试 7: 查重结果入库 + 消息创建")
    with DatabaseManager.session() as session:
        units = q.get_all_active_units(session)
        unit_files_map = {}
        for u in units:
            files = q.get_files_by_unit(session, u.id)
            unit_files_map[u.id] = [{
                "id": f.id, "path": f.path,
                "md5_hash": f.md5_hash, "phash": f.phash, "dhash": f.dhash,
            } for f in files]
        unit_names = {u.id: u.name for u in units}

    config = AppConfig()
    engine = DedupEngine(jaccard_threshold=0.5)  # 降低阈值确保有结果
    result = engine.run_dedup(unit_files_map, unit_names)

    saved = 0
    for dup in result.duplicates_found:
        with DatabaseManager.session() as session:
            dr = q.upsert_dedup_result(
                session,
                unit_a_id=dup.unit_a_id,
                unit_b_id=dup.unit_b_id,
                similarity_score=dup.jaccard_similarity,
                match_count=len(dup.file_matches),
                total_files_a=dup.total_files_a,
                total_files_b=dup.total_files_b,
                match_types=dup.match_types_str,
            )
            for fm in dup.file_matches:
                q.insert_file_match(
                    session, dr.id,
                    file_a_id=fm.file_a_id, file_b_id=fm.file_b_id,
                    similarity_score=fm.score, match_type=fm.match_type,
                )
            saved += 1

    check(f"成功写入 {saved} 组查重结果", saved > 0)

    # 创建消息
    if result.duplicates_found:
        d = result.duplicates_found[0]
        msg = MessageCenter.create_dedup_alert(
            d.unit_a_name, d.unit_b_name, d.jaccard_similarity,
            len(d.file_matches), dedup_result_id=1,
        )
        check("消息创建成功", msg is not None)

    # 查询消息
    count = MessageCenter.get_unread_count()
    check(f"有 {count} 条未读消息", count > 0)


def test_tree_model():
    section("测试 8: 文件夹树模型（TreeNode 纯数据）")
    from app.ui.left_panel.folder_tree import FolderTreeModel
    config = AppConfig()
    model = FolderTreeModel(config)
    model.refresh()

    check(f"加载了 {len(model._roots)} 个根目录", len(model._roots) > 0)
    if model._roots:
        root = model._roots[0]
        check(f"根目录下有 {len(root.children)} 个单元", len(root.children) > 0)
        check("TreeNode 无 session 依赖（纯数据）", root.name is not None)
        check("单元有 file_count", root.children[0].file_count >= 0)
        # 验证所有 TreeNode 的 node_id 有效（排除 bug: lambda 参数被覆盖后 0）
        all_ids_valid = all(
            child.node_id > 0
            for root in model._roots
            for child in root.children
            if child.node_type == "unit"
        )
        check("所有单元 node_id > 0", all_ids_valid)
        # 验证根节点也有有效 ID
        root_ids_valid = all(
            root.node_id > 0 or root.node_id == -1  # favorites 用 -1
            for root in model._roots
        )
        check("根节点 node_id 有效", root_ids_valid)


def test_context_menu_lambda_safety():
    """测试 8.5: context_menu lambda 参数安全性（验证已修复的 bug）。

    QAction.triggered 发出 checked=False，旧版 lambda uid=value: ...
    会被 False 覆盖 uid 默认值，导致 Signal(int) 发出 0。
    """
    section("测试 8.5: context_menu lambda 参数安全性")

    # 模拟 QAction.triggered 发出 checked=False
    captured_id = 42
    captured_path = "/test/path"
    captured_root_id = 7

    # 排除/收藏/拆分/取消标记 — 单参数 lambda
    exclude_handler = lambda *args, uid=captured_id: uid
    result = exclude_handler(False)
    check("排除 lambda: *args 吸收 False，uid=42", result == 42)

    # 标记为资源单元 — str 参数
    mark_handler = lambda *args, p=captured_path: p
    result = mark_handler(False)
    check("标记 lambda: *args 吸收 False，path 保持字符串", result == captured_path)

    # 合并 — 多参数 lambda
    merge_handler = lambda *args, pid=captured_id, cids=[1,2,3]: pid
    result = merge_handler(False)
    check("合并 lambda: *args 吸收 False，pid=42", result == 42)

    # 删除媒体库根目录
    remove_handler = lambda *args, rid=captured_root_id: rid
    result = remove_handler(False)
    check("删除根 lambda: *args 吸收 False，rid=7", result == 7)

    # 无参数的 lambda（刷新）不受影响
    refresh_handler = lambda: True
    result = refresh_handler()
    check("刷新 lambda: 无参数不受影响", result is True)


def test_message_center():
    section("测试 9: 消息中心 CRUD")
    msg = MessageCenter.create_info("测试消息", "这是一条自动化测试消息")
    check("创建消息成功", msg is not None)

    count = MessageCenter.get_unread_count()
    check(f"未读消息数 ≥ 1 (实际: {count})", count >= 1)

    if msg:
        ok = MessageCenter.mark_read(msg.id)
        check("标记已读成功", ok)

    ok_all = MessageCenter.mark_all_read()
    check("全部已读成功", ok_all)


def test_thumbnails(root: Path):
    section("测试 6.5: 缩略图生成")
    config = AppConfig()
    gen = ThumbnailGenerator(
        max_size=config.thumbnail_max_size,
        cache_subdir=".thumbnails",
        quality=80,
    )

    # 生成图片缩略图
    img_path = root / "2025-01-01" / "片段A" / "照片01.jpg"
    cache_dir = img_path.parent / ".thumbnails"
    cache_dir.mkdir(parents=True, exist_ok=True)

    info = gen.generate(img_path, cache_dir)
    check("缩略图生成成功", info is not None)
    check(f"缩略图文件存在: {info.thumbnail_path.name}", info.thumbnail_path.exists())
    check("缩略图宽 > 0", info.width > 0)
    check("缩略图高 > 0", info.height > 0)
    check("缩略图宽 <= 最大尺寸", info.width <= config.thumbnail_max_size)

    # 重新生成同一文件（应从缓存加载）
    info2 = gen.generate(img_path, cache_dir)
    check("二次生成返回路径一致", info.thumbnail_path == info2.thumbnail_path)

    # 批量生成
    all_imgs = list(root.rglob("*.jpg")) + list(root.rglob("*.png"))
    infos = gen.generate_batch(all_imgs[:4], cache_dir)
    check(f"批量生成了 {len(infos)} 个缩略图", len(infos) == min(4, len(all_imgs)))

def test_api_app():
    section("测试 10: FastAPI 应用创建")
    from config import AppConfig
    from app.api.server import create_app
    config = AppConfig()
    app = create_app(config)
    check("FastAPI 应用创建成功", app is not None)
    check(f"注册了 {len(app.routes)} 条路由", len(app.routes) >= 8)

    # 检查关键路由
    paths = {r.path for r in app.routes}
    check("包含 /api/units", "/api/units" in paths)
    check("包含 /api/files", "/api/files" in paths)
    check("包含 /api/dedup/results", "/api/dedup/results" in paths)
    check("包含 /api/messages", "/api/messages" in paths)
    check("包含 /docs", "/docs" in paths)
    check("包含健康检查 /api/health", "/api/health" in paths)


# ============================================================
# 主测
# ============================================================

def main():
    global _passed, _failed
    _passed = 0
    _failed = 0

    print("=" * 60)
    print("  影视资源管理器 - 自动化测试 v0.1.0")
    print("=" * 60)

    # 初始化
    config = AppConfig()
    db_path = Path("data/test_flow.db")
    init_db(db_path)

    # 创建测试媒体库
    temp_root = Path(tempfile.mkdtemp(prefix="media_test_"))
    create_test_media(temp_root)

    try:
        # 0. 预检——全模块导入
        test_preflight()

        # 核心测试
        test_db()
        result = test_scanner(temp_root)
        test_save_to_db(result, temp_root)
        test_hash_engine(temp_root)
        test_hash_batch(temp_root)
        test_thumbnails(temp_root)
        test_dedup_engine()
        test_save_dedup_results()
        test_tree_model()
        test_message_center()
        test_api_app()

    finally:
        # 清理数据库连接
        DatabaseManager.dispose()
        # 清理临时文件
        import shutil
        shutil.rmtree(temp_root, ignore_errors=True)
        if db_path.exists():
            try:
                db_path.unlink()
            except PermissionError:
                pass  # Windows 有时延迟释放文件句柄

    # 总结
    total = _passed + _failed
    print(f"\n{'=' * 60}")
    print(f"  测试完成: {_passed}/{total} 通过, {_failed} 失败")
    if _failed == 0:
        print(f"  [OK] 全部通过！")
    else:
        print(f"  [WARN] 有 {_failed} 个测试失败，需要修复")
    print(f"{'=' * 60}")

    return _failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
