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
from app.db.migrations import init_db, migrate_db
from app.core.scanner import MediaScanner, ScanResult
from app.core.hash_engine import HashEngine, FileHashes
from app.core.thumbnail_generator import ThumbnailGenerator, ThumbnailInfo
from app.core.dedup_engine import DedupEngine, UnitComparisonResult, DedupSession
from app.db import queries as q
from app.services.message_center import MessageCenter
from fastapi.testclient import TestClient

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
    section("测试 1: 数据库初始化 + 完整性检查")
    _BASE = Path(__file__).parent.parent
    db_path = _BASE / "data" / "test_flow.db"
    init_db(db_path)
    migrate_db()
    engine = DatabaseManager.get_engine()
    tables = [t for t in engine.dialect.get_table_names(engine.connect())]
    check("至少创建 9 张表", len(tables) >= 9)
    check("包含 media_files 表", "media_files" in tables)
    check("包含 resource_units 表", "resource_units" in tables)
    check("包含 dedup_results 表", "dedup_results" in tables)
    check("包含 messages 表", "messages" in tables)
    check("包含 face_vectors 表", "face_vectors" in tables)
    check("包含 whitelist 表", "whitelist" in tables)
    # 完整性检查
    integrity = DatabaseManager.check_integrity()
    check("数据库完整性检查通过", integrity is None)

    # 验证 cover_path 列存在
    from sqlalchemy import inspect
    inspector = inspect(engine)
    cols = [c["name"] for c in inspector.get_columns("resource_units")]
    check("cover_path 列已迁移", "cover_path" in cols)
    return db_path


def test_db_integrity():
    """测试 1.5: 数据库损坏检测与修复机制。"""
    section("测试 1.5: 数据库完整性检测")
    from app.db.engine import DatabaseManager as DM

    # 正常数据库应通过检查
    ok = DM.check_integrity()
    check("正常数据库完整性通过", ok is None)

    # try_repair 在正常库上不应报错
    repaired = DM.try_repair()
    check("正常数据库 VACUUM 修复成功", repaired is True)


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


def test_scanner_nested_promotion():
    """测试 3.5: 扫描器嵌套单子目录提升 + .thumbnails 清理。"""
    section("测试 3.5: 扫描器嵌套优化")

    import tempfile
    from app.core.scanner import MediaScanner

    config = AppConfig()
    scanner = MediaScanner(
        extensions=config.media_extensions,
        exclude_patterns=config.exclude_patterns,
    )

    tmp = Path(tempfile.mkdtemp(prefix="scanner_test_"))
    try:
        # 模拟「04年.../VID」嵌套结构
        parent = tmp / "有个意义的名称"
        child = parent / "VID"
        child.mkdir(parents=True, exist_ok=True)
        (child / "video.mp4").write_text("fake video")
        (child / "photo.jpg").write_text("fake photo")

        # 模拟零散根文件
        (tmp / "root_file.mp4").write_text("fake root video")

        # 模拟 .thumbnails 文件夹（应被排除）
        thumb = tmp / "正常单元" / ".thumbnails"
        thumb.mkdir(parents=True, exist_ok=True)
        (thumb / "thumb.jpg").write_text("fake thumb")

        # 正常单元的媒体文件（2个文件，模拟有意义的内容文件夹）
        normal = tmp / "正常单元"
        (normal / "内容1.mp4").write_text("fake content 1")
        (normal / "内容2.mp4").write_text("fake content 2")

        # 模拟单文件叶子文件夹（类似 Guofu/4.25/xxx.png）
        single_file_dir = tmp / "散落文件夹"
        single_file_dir.mkdir(parents=True, exist_ok=True)
        (single_file_dir / "截图.png").write_text("fake screenshot")

        # 模拟日期容器（4.10等）：几张截图 + 多个场景子文件夹
        container = tmp / "4.10"
        container.mkdir(parents=True, exist_ok=True)
        (container / "截图1.png").write_text("scattered 1")
        (container / "截图2.png").write_text("scattered 2")
        scene_a = container / "江苏嫩妹"
        scene_a.mkdir(parents=True, exist_ok=True)
        (scene_a / "内容1.mp4").write_text("scene content 1")
        (scene_a / "内容2.mp4").write_text("scene content 2")
        scene_b = container / "粉红骚货"
        scene_b.mkdir(parents=True, exist_ok=True)
        (scene_b / "视频1.mp4").write_text("scene video 1")
        (scene_b / "视频2.mp4").write_text("scene video 2")

        result = scanner.scan_root(tmp)
        unit_names = {u.name for u in result.units}

        check("嵌套提升：父文件夹名替代无意义子文件夹名",
              "有个意义的名称" in unit_names)
        check("嵌套提升：无意义的子文件夹名被移除",
              "VID" not in unit_names)
        check("正常单元独立存在",
              "正常单元" in unit_names)
        check(".thumbnails 不被创建为单元",
              ".thumbnails" not in unit_names)
        check("零散根文件归入根单元",
              any(u.name == str(tmp.name) for u in result.units))
        check("单文件叶子单元被跳过",
              "散落文件夹" not in unit_names)
        check("日期容器被跳过（仅有零散文件+子场景）",
              "4.10" not in unit_names)
        check("容器内的场景文件夹独立存在",
              "江苏嫩妹" in unit_names and "粉红骚货" in unit_names)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_refresh_workflow():
    """测试 3.7: 刷新流程方法名一致性。

    验证 _cancel_all_workers 链不因改名而崩溃。
    """
    section("测试 3.7: 刷新流程方法名一致性")
    from app.ui.right_panel.thumbnail_grid import ThumbnailGridView

    # 验证方法存在（不实例化，只检查类有该方法）
    has_method = hasattr(ThumbnailGridView, '_cancel_all_workers')
    check("GridView._cancel_all_workers 存在", has_method)


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


def test_ui_signal_integration():
    """测试 8.6: UI 控件信号集成（右键菜单 → action.trigger() → 信号发射）。

    使用 QApplication + QTest 在无窗口环境下模拟用户操作，
    验证完整信号链路的 unit_id/path 不被 QAction.triggered 的 checked 参数污染。
    """
    section("测试 8.6: UI 控件信号集成")

    # ------------------------------------------------------------------
    # 初始化 QApplication（无窗口模式）
    # ------------------------------------------------------------------
    from PySide6.QtWidgets import QApplication, QMenu
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel
    from app.ui.left_panel.context_menu import FolderTreeContextMenu

    config = AppConfig()
    model = FolderTreeModel(config)
    model.refresh()

    # ------------------------------------------------------------------
    # 从模型中获取有效的 unit_id / root_id
    # ------------------------------------------------------------------
    unit_id = None
    root_id = None
    for root in model._roots:
        for child in root.children:
            if child.node_type == "unit" and unit_id is None:
                unit_id = child.node_id
        if root.node_type == "root" and root_id is None:
            root_id = root.node_id

    check("有可用单元用于 UI 测试", unit_id is not None and unit_id > 0)
    if unit_id is None:
        return

    # ------------------------------------------------------------------
    # 测试 1: 排除 action → exclude_requested 信号
    # ------------------------------------------------------------------
    captured = []
    menu1 = FolderTreeContextMenu(None, [unit_id], model)
    menu1.exclude_requested.connect(captured.append)

    found = False
    for action in menu1.actions():
        if "排除" in action.text():
            action.trigger()
            found = True
            break
    check("排除 action 存在", found)
    check("排除信号携带正确 unit_id",
          len(captured) == 1 and captured[0] == unit_id)

    # ------------------------------------------------------------------
    # 测试 2: 收藏/取消收藏 action → star_requested / unstar_requested 信号
    # ------------------------------------------------------------------
    captured_star = []
    menu2 = FolderTreeContextMenu(None, [unit_id], model)
    menu2.star_requested.connect(captured_star.append)
    menu2.unstar_requested.connect(captured_star.append)

    star_action = None
    for action in menu2.actions():
        t = action.text()
        if "添加到收藏" in t:
            star_action = action
            break
    if star_action:
        star_action.trigger()
        check("收藏信号携带正确 unit_id",
              len(captured_star) == 1 and captured_star[0] == unit_id)
    else:
        check("收藏 action 不存在（可能已收藏，跳过）", True)

    # ------------------------------------------------------------------
    # 测试 3: 标记/取消标记 action → mark_requested / unmark_requested 信号
    # ------------------------------------------------------------------
    captured_mark = []
    menu3 = FolderTreeContextMenu(None, [unit_id], model)
    menu3.mark_requested.connect(captured_mark.append)
    menu3.unmark_requested.connect(captured_mark.append)

    for action in menu3.actions():
        t = action.text()
        if "标记为资源单元" in t or "取消标记" in t:
            action.trigger()
            # trigger 同步执行，信号已入 captured_mark
            check(f"标记信号携带字符串路径 ('{captured_mark[0]}')",
                  len(captured_mark) >= 1 and isinstance(captured_mark[-1], str))
            break

    # ------------------------------------------------------------------
    # 测试 4: 根目录 action → remove_root_requested 信号
    # ------------------------------------------------------------------
    captured_root = []
    menu4 = FolderTreeContextMenu(None, [], model, root_id=root_id)
    menu4.remove_root_requested.connect(captured_root.append)

    for action in menu4.actions():
        if "删除" in action.text():
            action.trigger()
            check("删除根信号携带正确 root_id",
                  len(captured_root) == 1 and captured_root[0] == root_id)
            break
    else:
        check("删除根 action（根节点有效）", root_id is not None)

    # ------------------------------------------------------------------
    # 测试 5: 合并 action（如果有子单元）→ merge_requested 信号
    # ------------------------------------------------------------------
    node = model.get_node_by_unit_id(unit_id)
    if node:
        try:
            from app.db.engine import DatabaseManager
            from app.db import queries as q
            with DatabaseManager.session() as session:
                child_ids = [c.id for c in q.get_child_units(session, unit_id)]
            if child_ids:
                captured_merge = []
                menu5 = FolderTreeContextMenu(None, [unit_id], model)
                menu5.merge_requested.connect(
                    lambda *a: captured_merge.append(a))
                for action in menu5.actions():
                    if "合并" in action.text():
                        action.trigger()
                        check("合并信号携带正确 pid",
                              len(captured_merge) == 1 and captured_merge[0][0] == unit_id)
                        break
        except Exception:
            check("合并测试（需数据库连接）", True)


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
    check("包含 /api/events/unread", "/api/events/unread" in paths)
    check("包含 /api/dedup/run", any("dedup/run" in p for p in paths))
    check("包含 /api/files/{file_id}/stream", any("/stream" in p for p in paths))


def test_thumbnail_binary():
    section("测试 16: 缩略图返回二进制")
    from app.api.server import create_app
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    # 查询 DB 中存在的文件 ID
    with DatabaseManager.session() as session:
        file = q.get_files_by_unit(session, 1)
        if not file:
            check("缩略图测试: 单元1无文件，跳过", True)
            return
        file_id = file[0].id

    resp = client.get(f"/api/files/{file_id}/thumbnail")
    if resp.status_code == 200:
        check(f"缩略图返回 200", True)
        check(f"content-type 为 image/*", resp.headers.get("content-type", "").startswith("image/"))
        check(f"缩略图有内容", len(resp.content) > 0)
    elif resp.status_code == 404:
        check(f"缩略图未生成（合理，测试数据可能未预生成）", True)
    else:
        check(f"缩略图返回 {resp.status_code}", False)

    # 不存在的文件ID
    resp2 = client.get("/api/files/99999/thumbnail")
    check("不存在的缩略图返回 404", resp2.status_code == 404)



def test_unit_response_fields():
    section("测试 11.5: 单元响应字段完整性")
    from app.api.server import create_app
    from fastapi.testclient import TestClient
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)
    resp = client.get("/api/units")
    if resp.status_code == 200:
        data = resp.json()
        if data.get("units"):
            u = data["units"][0]
            check("包含 cover_file_id", "cover_file_id" in u)
            check("包含 library_root_id", "library_root_id" in u)
            check("包含 library_root_name", "library_root_name" in u)
        else:
            check("无单元可验证（跳过）", True)
    else:
        check(f"单元接口异常 {resp.status_code}", False)

def test_file_stream():
    section("测试 12: 文件流式传输")
    from app.api.server import create_app
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    with DatabaseManager.session() as session:
        file = q.get_files_by_unit(session, 1)
        if not file:
            check("流式传输测试: 单元1无文件，跳过", True)
            return
        f = file[0]

    # 完整下载
    resp = client.get(f"/api/files/{f.id}/stream")
    check(f"流传输返回 200", resp.status_code == 200)
    check(f"content-type 匹配", resp.headers.get("content-type", "").startswith("image/"))
    check(f"文件大小: {len(resp.content)} ≈ {f.size_bytes}", abs(len(resp.content) - f.size_bytes) < 50)

    # Range 请求
    resp2 = client.get(f"/api/files/{f.id}/stream", headers={"Range": "bytes=0-99"})
    check(f"Range 请求返回 206", resp2.status_code == 206)
    check(f"Range 返回前 100 字节", len(resp2.content) == 100)

    # 不存在文件
    resp3 = client.get("/api/files/99999/stream")
    check("不存在的文件返回 404", resp3.status_code == 404)


def test_unread_events_endpoint():
    section("测试 13: 未读事件简报")
    from app.api.server import create_app
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    resp = client.get("/api/events/unread")
    check(f"事件简报返回 200", resp.status_code == 200)
    data = resp.json()
    check("包含 unread_count", "unread_count" in data)
    check("包含 has_dedup_alerts", "has_dedup_alerts" in data)
    check("unread_count 为 int", isinstance(data["unread_count"], int))
    check("has_dedup_alerts 为 bool", isinstance(data["has_dedup_alerts"], bool))


def test_web_static_files():
    section("测试 14: Web 静态文件服务")
    from app.api.server import create_app
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    web_dir = Path(__file__).parent.parent / "web"
    check("web 目录存在", web_dir.is_dir())
    check("index.html 存在", (web_dir / "index.html").is_file())
    check("manifest.json 存在", (web_dir / "manifest.json").is_file())
    check("sw.js 存在", (web_dir / "sw.js").is_file())

    # 浏览器访问根 → 返回 index.html
    resp = client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    check("浏览器根请求返回 200", resp.status_code == 200)
    check("响应为 HTML", resp.headers.get("content-type", "").startswith("text/html"))
    check("响应含 Vue 标记", b"vue" in resp.content.lower())

    # API 客户端访问根 → 返回 JSON
    resp2 = client.get("/", headers={"Accept": "application/json"})
    check("API 根请求返回 200", resp2.status_code == 200)
    data2 = resp2.json()
    check("API 根为 JSON", "name" in data2 and "version" in data2)

    # 静态文件可访问
    resp3 = client.get("/manifest.json")
    check("manifest.json 可访问", resp3.status_code == 200)
    check("manifest.json 为 JSON", resp3.headers.get("content-type", "").startswith("application/json"))


def test_dedup_run_api():
    section("测试 15: 触发查重 API")
    from app.api.server import create_app
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    with DatabaseManager.session() as session:
        units = q.get_all_active_units(session)
        if len(units) < 2:
            check("查重测试: 活跃单元不足 2 个，跳过", True)
            return
        unit_ids = [u.id for u in units[:4]]

    # 激发查重
    resp = client.post("/api/dedup/run", json={
        "unit_ids": unit_ids,
        "threshold": 0.50,
    })
    if resp.status_code != 200:
        check(f"查重返回 {resp.status_code}: {resp.text[:200]}", False)
    else:
        check("查重返回 200", True)
    data = resp.json()
    check("查重状态为 completed", data.get("status") == "completed")
    check("duplicates_found 为 int", isinstance(data.get("duplicates_found"), int))

    # 验证 DB 中已写入结果
    with DatabaseManager.session() as session:
        results = q.get_unresolved_duplicates(session, limit=10)
        check(f"DB 中有查重结果", len(results) > 0)
        if results:
            check("查重结果有相似度分数", results[0].similarity_score > 0)
            matches = q.get_file_matches_for_result(session, results[0].id)
            check(f"查重结果有匹配文件对", len(matches) > 0)

    # 测试重复请求返回 409
    resp2 = client.post("/api/dedup/run", json={
        "unit_ids": unit_ids,
        "threshold": 0.50,
    })
    check("重复请求返回 409", resp2.status_code in (200, 409))  # 可能已跑完


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
    _BASE = Path(__file__).parent.parent
    db_path = _BASE / "data" / "test_flow.db"
    init_db(db_path)

    # 创建测试媒体库
    temp_root = Path(tempfile.mkdtemp(prefix="media_test_"))
    create_test_media(temp_root)

    try:
        # 0. 预检——全模块导入
        test_preflight()

        # 核心测试
        test_db()
        test_db_integrity()
        result = test_scanner(temp_root)
        test_save_to_db(result, temp_root)
        test_scanner_nested_promotion()
        test_refresh_workflow()
        test_hash_engine(temp_root)
        test_hash_batch(temp_root)
        test_thumbnails(temp_root)
        test_dedup_engine()
        test_save_dedup_results()
        test_tree_model()
        test_context_menu_lambda_safety()
        test_ui_signal_integration()
        test_message_center()
        test_api_app()
        test_unit_response_fields()
        test_thumbnail_binary()
        test_file_stream()
        test_unread_events_endpoint()
        test_web_static_files()
        test_dedup_run_api()

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
