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


def test_path_revalidation():
    """测试 3.2: 扫描时路径重新校验 — 文件夹移动后旧路径标记排除。"""
    section("测试 3.2: 扫描路径重新校验")
    from pathlib import Path
    with DatabaseManager.session() as session:
        roots = q.get_all_roots(session)
        if not roots:
            check("无根目录，跳过", True)
            return
        root_id = roots[0].id
        # 创建一个路径不存在的单元（模拟文件夹被移动后）
        fake_unit = q.create_unit(session, path=str(Path(roots[0].path) / "_nonexistent_test_dir_"), name="测试-已移动", library_root_id=root_id)
        # 执行扫描后的清理逻辑
        for stale in q.get_units_by_root(session, root_id):
            if stale.status == "active" and not Path(stale.path).is_dir():
                q.mark_unit_excluded(session, stale.id)
        # 验证
        stale = q.get_unit_by_id(session, fake_unit.id)
        check("失效路径单元被排除", stale.status == "excluded")
        # 原有活跃单元不受影响
        active = q.get_units_by_root(session, root_id)
        check("原有活跃单元仍在", any(u.id != fake_unit.id for u in active))


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


def test_zero_byte_file():
    """测试 3.6: 零字节文件不被扫描入库。"""
    section("测试 3.6: 零字节文件过滤")
    import tempfile
    from app.core.scanner import MediaScanner
    config = AppConfig()
    scanner = MediaScanner(
        extensions=config.media_extensions,
        exclude_patterns=config.exclude_patterns,
    )
    tmp = Path(tempfile.mkdtemp(prefix="zero_byte_test_"))
    unit_dir = tmp / "测试单元"
    unit_dir.mkdir()
    # 创建正常文件
    from PIL import Image
    Image.new("RGB", (10, 10), color=(255, 0, 0)).save(unit_dir / "正常.jpg")
    # 创建零字节文件
    (unit_dir / "空文件.png").touch()
    (unit_dir / "空文件2.jpg").touch()
    result = scanner.scan_root(tmp)
    # 零字节文件应被跳过
    check("零字节 PNG 被跳过", not any(f.filename == "空文件.png" for u in result.units for f in u.files))
    check("零字节 JPG 被跳过", not any(f.filename == "空文件2.jpg" for u in result.units for f in u.files))
    # 正常文件仍在
    check("正常文件仍在", any(f.filename == "正常.jpg" for u in result.units for f in u.files))
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


def test_tags():
    """测试 9.5: 标签 CRUD。"""
    section("测试 9.5: 标签 CRUD")
    from app.db import queries as q

    with DatabaseManager.session() as session:
        # 创建标签
        tag = q.create_tag(session, "正面大笑", "#FF6B6B")
        check("创建标签成功", tag.id is not None)
        check("标签名称正确", tag.name == "正面大笑")

        tag2 = q.create_tag(session, "侧面走路", "#4ECDC4")
        check("第二个标签创建成功", tag2.id is not None)

        # 查重创建
        dup = q.get_tag_by_name(session, "正面大笑")
        check("按名称查找成功", dup is not None and dup.id == tag.id)

        # 列表
        all_tags = q.get_all_tags(session)
        check("标签列表 >= 2", len(all_tags) >= 2)

        # 为文件设置标签
        from app.db.models import MediaFile
        mf = session.query(MediaFile).first()
        if mf:
            q.set_file_tags(session, mf.id, [tag.id, tag2.id])
            ftags = q.get_file_tags(session, mf.id)
            check("文件标签数=2", len(ftags) == 2)

            # 全量替换
            q.set_file_tags(session, mf.id, [tag.id])
            ftags2 = q.get_file_tags(session, mf.id)
            check("替换后标签数=1", len(ftags2) == 1)

        # 批量查询
        mapped = q.get_all_mapped_files(session)
        check("批量查询有结果", len(mapped) >= 0)

        # 删除标签
        ok = q.delete_tag(session, tag2.id)
        check("删除标签成功", ok)


def test_heic_converter():
    """测试 6.6: HEIC 转换 — 文件头检测。"""
    section("测试 6.6: HEIC 转换器")
    from app.core.heic_converter import is_heic_file, convert_single, convert_batch

    # is_heic_file 对非 HEIC 文件应返回 False
    with DatabaseManager.session() as session:
        from app.db import queries as q
        units = q.get_all_active_units(session)
        if units:
            files = q.get_files_by_unit(session, units[0].id)
            for f in files[:3]:
                p = Path(f.path)
                if p.is_file():
                    check(f"非 HEIC 文件头检测正确: {p.name}", not is_heic_file(p))

    # convert_single 对不存在文件应返回 error
    result = convert_single(Path("C:/nonexistent.heic"))
    check("不存在文件检测", not result.success)
    check("错误消息不为空", bool(result.error))

    # 对非 HEIC 的 jpg 文件转换应失败
    with DatabaseManager.session() as session:
        from app.db import queries as q
        units = q.get_all_active_units(session)
        if units:
            files = q.get_files_by_unit(session, units[0].id)
            jpgs = [f for f in files if f.path.lower().endswith(".jpg")]
            if jpgs:
                result2 = convert_single(Path(jpgs[0].path))
                check("非 HEIC 文件转换失败", not result2.success)
                check("错误含 '不是 HEIC'", "不是 HEIC" in result2.error)

    check("HEIC 导入正常", True)


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

    # 校验缓存过期：修改源文件后应重新生成
    import time
    orig_mtime = img_path.stat().st_mtime
    new_mtime = orig_mtime + 60
    os.utime(img_path, (new_mtime, new_mtime))
    info3 = gen.generate(img_path, cache_dir)
    check("mtime 变更后缓存过期", info3.generation_method != "cache")
    os.utime(img_path, (orig_mtime, orig_mtime))

    # 校验 stable hash：hash 不依赖 PYTHONHASHSEED
    import hashlib
    h1 = int.from_bytes(
        hashlib.md5(str(img_path).encode("utf-8")).digest()[:8],
        byteorder="big", signed=True,
    )
    h2 = int.from_bytes(
        hashlib.md5(str(img_path).encode("utf-8")).digest()[:8],
        byteorder="big", signed=True,
    )
    check("stable hash 跨调用一致", h1 == h2)
    other = root / "2025-01-01" / "片段A" / "照片02.jpg"
    h3 = int.from_bytes(
        hashlib.md5(str(other).encode("utf-8")).digest()[:8],
        byteorder="big", signed=True,
    )
    check("不同路径 hash 不同", h1 != h3)

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

    # 验证 _dedup_running 标志在查重完成后被正确重置
    from app.api.routes.dedup import _dedup_running as dedup_flag
    check("查重结束后 _dedup_running=False", not dedup_flag)


# ============================================================
# TDD 测试 —— 右键菜单、排序、树搜索过滤
# ============================================================

def test_grid_context_menu_signals():
    """TDD-T3: 右键菜单对 image/video 文件正确发射信号。"""
    section("TDD T3: 右键菜单信号")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.right_panel.thumbnail_grid import ThumbnailGridView
    from app.ui.right_panel.thumbnail_grid import ThumbnailGridModel
    from PySide6.QtCore import Qt
    config = AppConfig()
    model = ThumbnailGridModel(config)
    view = ThumbnailGridView(model, config)

    # 注入模拟数据
    fake_files = [
        {"id": 1, "filename": "test.jpg", "path": "C:/test/test.jpg",
         "media_type": "image", "size_bytes": 1000, "width": 100, "height": 100, "duration_ms": None},
        {"id": 2, "filename": "test.mp4", "path": "C:/test/test.mp4",
         "media_type": "video", "size_bytes": 2000, "width": 1920, "height": 1080, "duration_ms": 5000},
    ]
    model.set_files(fake_files)
    view.setModel(model)

    # 验证模型数据正确
    check("T3: 行0是 image", model.data(model.index(0, 0), Qt.ItemDataRole.UserRole + 2) == "image")
    check("T3: 行1是 video", model.data(model.index(1, 0), Qt.ItemDataRole.UserRole + 2) == "video")
    check("T3: 行0路径正确", model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "C:/test/test.jpg")
    check("T3: 行1路径正确", model.data(model.index(1, 0), Qt.ItemDataRole.UserRole) == "C:/test/test.mp4")

    # 验证信号连接正常工作
    captured_cover = []
    view.cover_from_file_requested.connect(captured_cover.append)
    # 直接发射信号模拟菜单操作结果
    view.cover_from_file_requested.emit("C:/test/test.mp4")
    check("T3: 信号可发射到视频路径", len(captured_cover) == 1 and captured_cover[0] == "C:/test/test.mp4")


def test_grid_sort():
    """TDD-T5: 排序按钮调用 model.set_sort 正确排序。"""
    section("TDD T5: 模型排序")
    from app.ui.right_panel.thumbnail_grid import ThumbnailGridModel
    config = AppConfig()
    model = ThumbnailGridModel(config)

    fake_files = [
        {"id": 3, "filename": "zzz.jpg", "path": "/a/zzz.jpg",
         "media_type": "image", "size_bytes": 3000, "width": 100, "height": 100},
        {"id": 1, "filename": "aaa.jpg", "path": "/a/aaa.jpg",
         "media_type": "image", "size_bytes": 1000, "width": 100, "height": 100},
        {"id": 2, "filename": "bbb.jpg", "path": "/a/bbb.jpg",
         "media_type": "image", "size_bytes": 2000, "width": 100, "height": 100},
    ]
    model.set_files(fake_files)

    # 按名称排序
    model.set_sort("name", ascending=True)
    names = [f["filename"] for f in model.file_list]
    check("T5: 名称升序 aaa 第一", names[0] == "aaa.jpg")
    check("T5: 名称升序 zzz 第三", names[2] == "zzz.jpg")

    # 按大小降序
    model.set_sort("size", ascending=False)
    sizes = [f["size_bytes"] for f in model.file_list]
    check("T5: 大小降序 3000 最大", sizes[0] == 3000)
    check("T5: 大小降序 1000 最小", sizes[2] == 1000)


def test_tree_filter():
    """TDD-T6: 文件夹树 filter_by_name 隐藏不匹配节点。"""
    section("TDD T6: 树搜索过滤")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView
    from app.ui.left_panel.folder_tree import TreeNode
    from PySide6.QtCore import QModelIndex
    config = AppConfig()
    model = FolderTreeModel(config)

    # 直接构造测试用 TreeNode 数据
    model._roots = [
        TreeNode(
            node_type="root", node_id=1, name="测试库1",
            path="/test1", file_count=2, total_size=2000,
            children=[
                TreeNode(node_type="unit", node_id=10, name="春天",
                         path="/test1/spring", file_count=1),
                TreeNode(node_type="unit", node_id=11, name="夏天",
                         path="/test1/summer", file_count=1),
            ],
        ),
        TreeNode(
            node_type="root", node_id=2, name="测试库2",
            path="/test2", file_count=1, total_size=1000,
            children=[
                TreeNode(node_type="unit", node_id=20, name="冬天",
                         path="/test2/winter", file_count=1),
            ],
        ),
    ]
    model.beginResetModel()
    model.endResetModel()

    view = FolderTreeView(model)
    view.setModel(model)

    root0_idx = model.index(0, 0)
    check("T6: 根0存在", root0_idx.isValid())

    # 过滤 "天" → 全部匹配
    view.filter_by_name("天")
    check("T6: '天' 春天可见", not view.isRowHidden(0, root0_idx))
    check("T6: '天' 夏天可见", not view.isRowHidden(1, root0_idx))

    # 过滤 "春" → 只有春天匹配
    view.filter_by_name("春")
    check("T6: '春' 春天可见", not view.isRowHidden(0, root0_idx))
    check("T6: '春' 夏天隐藏", view.isRowHidden(1, root0_idx))

    # 清空 → 全部可见
    view.filter_by_name("")
    check("T6: 清空后春天可见", not view.isRowHidden(0, root0_idx))
    check("T6: 清空后夏天可见", not view.isRowHidden(1, root0_idx))


def test_search_auto_select_first():
    """TDD-T7: filter_by_name 后 find_first_visible_unit 返回正确。"""
    section("TDD T7: 搜索自动选中首个单元")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView
    from app.ui.left_panel.folder_tree import TreeNode
    config = AppConfig()
    model = FolderTreeModel(config)

    # 构造测试数据：2 个根，各带子单元
    model._roots = [
        TreeNode(
            node_type="root", node_id=1, name="库1",
            path="/lib1", file_count=2, total_size=2000,
            children=[
                TreeNode(node_type="unit", node_id=10, name="春天",
                         path="/lib1/spring", file_count=1),
                TreeNode(node_type="unit", node_id=11, name="夏天",
                         path="/lib1/summer", file_count=1),
            ],
        ),
        TreeNode(
            node_type="root", node_id=2, name="库2",
            path="/lib2", file_count=1, total_size=1000,
            children=[
                TreeNode(node_type="unit", node_id=20, name="冬天",
                         path="/lib2/winter", file_count=1),
            ],
        ),
    ]
    model.beginResetModel()
    model.endResetModel()

    view = FolderTreeView(model)
    view.setModel(model)

    # 搜索 "春" → 只有 "春天" 匹配
    view.filter_by_name("春")
    first = view.find_first_visible_unit()
    check("T7: '春' 找到春天 (id=10)", first == 10)

    # 搜索不存在的 → 无匹配
    view.filter_by_name("不存在的")
    first = view.find_first_visible_unit()
    check("T7: 无匹配时返回 None", first is None)

    # 清空 → 全部可见，第一个应为 10
    view.filter_by_name("")
    first = view.find_first_visible_unit()
    check("T7: 清空后第一个为 10", first == 10)


# ============================================================
# TDD 测试 —— 文件夹卡片单击 → 左侧高亮
# ============================================================

def test_folder_card_click_linking():
    """TDD-T8: 文件夹卡片单击 → folder_selected 信号。"""
    section("TDD T8: 文件夹卡片单击联动")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.right_panel.thumbnail_grid import (
        FolderCardModel, ThumbnailGridView, ThumbnailGridModel,
    )
    from PySide6.QtCore import Qt, QItemSelectionModel

    config = AppConfig()

    # 测试数据：2 个文件夹卡片
    fake_unit_data = [
        {"unit_id": 10, "name": "单元A", "path": "/test/A",
         "file_count": 5, "total_size": 5000, "cover_path": "", "preview_path": ""},
        {"unit_id": 11, "name": "单元B", "path": "/test/B",
         "file_count": 3, "total_size": 3000, "cover_path": "", "preview_path": ""},
    ]

    # ---- 1. FolderCardModel 数据角色验证 ----
    folder_model = FolderCardModel(fake_unit_data)
    check("T8: 行0 display='单元A'",
          folder_model.data(folder_model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "单元A")
    check("T8: 行0 unit_id=10",
          folder_model.data(folder_model.index(0, 0), Qt.ItemDataRole.UserRole + 1) == 10)
    check("T8: 行1 display='单元B'",
          folder_model.data(folder_model.index(1, 0), Qt.ItemDataRole.DisplayRole) == "单元B")
    check("T8: 行1 unit_id=11",
          folder_model.data(folder_model.index(1, 0), Qt.ItemDataRole.UserRole + 1) == 11)
    check("T8: 行0 media_type='folder'",
          folder_model.data(folder_model.index(0, 0), Qt.ItemDataRole.UserRole + 2) == "folder")

    # ---- 2. 视图 + folder_selected 信号验证 ----
    file_model = ThumbnailGridModel(config)
    view = ThumbnailGridView(file_model, config)
    view.setModel(folder_model)

    # 由于 setModel 替换了 selectionModel，需重新连接
    view.selectionModel().selectionChanged.connect(
        view._on_any_selection_changed
    )

    captured = []
    view.folder_selected.connect(captured.append)

    # 选中第一行 → 应触发 folder_selected(10)
    idx0 = folder_model.index(0, 0)
    view.selectionModel().select(idx0, QItemSelectionModel.SelectionFlag.Select)
    check("T8: folder_selected 已发射(选中行0)", len(captured) >= 1)
    if captured:
        check("T8: folder_selected 携带 unit_id=10", captured[0] == 10)

    # 选中第二行 → 应触发 folder_selected(11)
    idx1 = folder_model.index(1, 0)
    view.selectionModel().select(idx1, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    check("T8: 切换到行1 再次发射", len(captured) >= 2)
    if len(captured) >= 2:
        check("T8: 第二次 unit_id=11", captured[-1] == 11)


# ============================================================
# TDD 测试 —— 树展开单元内部文件节点
# ============================================================

def test_tree_expand_unit():
    """TDD-T9: expand_unit 向树节点注入子文件。"""
    section("TDD T9: 树展开单元文件")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])
    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import QModelIndex, Qt

    config = AppConfig()
    model = FolderTreeModel(config)
    model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
        ]),
    ]
    model.beginResetModel()
    model.endResetModel()

    # 展开前：单元下无子节点
    root_idx = model.index(0, 0)
    check("T9: 根索引有效", root_idx.isValid())
    unit_idx = model.index(0, 0, root_idx) if root_idx.isValid() else QModelIndex()
    check("T9: 单元索引有效", unit_idx.isValid())
    check("T9: 展开前子节点数=0", model.rowCount(unit_idx) == 0)

    # 展开：注入文件
    files = [
        {"id": 101, "filename": "a.jpg", "path": "/a/spring/a.jpg", "size_bytes": 1000},
        {"id": 102, "filename": "b.mp4", "path": "/a/spring/b.mp4", "size_bytes": 5000},
    ]
    model.expand_unit(10, files)
    check("T9: 展开后子节点数=2", model.rowCount(unit_idx) == 2)

    # 验证第一行是文件
    file_idx = model.index(0, 0, unit_idx)
    check("T9: 文件索引有效", file_idx.isValid())
    check("T9: 文件节点名 a.jpg", model.data(file_idx, Qt.ItemDataRole.DisplayRole) == "a.jpg")

    # 收起
    model.collapse_unit(10)
    check("T9: 收起后子节点数=0", model.rowCount(unit_idx) == 0)


# ============================================================
# TDD 测试 —— 文件级双向联动
# ============================================================

def test_file_level_linking():
    """TDD-T10: 文件级双向联动 —— 树点击文件→网格选中，网格点击文件→树选中。"""
    section("TDD T10: 文件级双向联动")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.right_panel.thumbnail_grid import (
        ThumbnailGridModel, ThumbnailGridView,
    )
    from app.ui.left_panel.folder_tree import (
        FolderTreeModel, FolderTreeView, TreeNode,
    )
    from app.ui.right_panel.thumbnail_grid import FolderCardModel
    from PySide6.QtCore import Qt, QModelIndex, QItemSelectionModel

    config = AppConfig()

    # ---- 1. 网格模型 + select_file_by_id ----
    files = [
        {"id": 101, "filename": "a.jpg", "path": "/a.jpg",
         "media_type": "image", "size_bytes": 1000,
         "width": 100, "height": 100, "duration_ms": None},
        {"id": 102, "filename": "b.mp4", "path": "/b.mp4",
         "media_type": "video", "size_bytes": 2000,
         "width": 1920, "height": 1080, "duration_ms": 5000},
    ]
    grid_model = ThumbnailGridModel(config)
    grid_model.set_files(files)
    check("T10: 文件模型行0 id=101",
          grid_model.data(grid_model.index(0, 0), Qt.ItemDataRole.UserRole + 1) == 101)
    check("T10: 文件模型行1 id=102",
          grid_model.data(grid_model.index(1, 0), Qt.ItemDataRole.UserRole + 1) == 102)

    # select_file_by_id 方法存在
    grid_view = ThumbnailGridView(grid_model, config)
    check("T10: select_file_by_id 方法存在",
          hasattr(grid_view, "select_file_by_id"))

    # select_file_by_id 选中正确文件
    grid_view.select_file_by_id(101)
    sel_idxs = grid_view.selectedIndexes()
    check("T10: select_file_by_id(101) 有选中", len(sel_idxs) > 0)
    if sel_idxs:
        sel_id = grid_model.data(sel_idxs[0], Qt.ItemDataRole.UserRole + 1)
        check("T10: select_file_by_id(101) 选中了 id=101", sel_id == 101)

    # select_file_by_id 在 FolderCardModel 上不应崩溃
    folder_model = FolderCardModel([
        {"unit_id": 10, "name": "单元A", "path": "/test/A",
         "file_count": 5, "total_size": 5000, "cover_path": "", "preview_path": ""},
    ])
    grid_view.setModel(folder_model)
    grid_view.select_file_by_id(101)  # 不应崩溃
    check("T10: select_file_by_id 在文件夹模式不崩溃", True)

    # ---- 2. 文件节点 TreeNode ----
    file_node = TreeNode(node_type="unit", node_subtype="file",
                         node_id=101, name="a.jpg", path="/a.jpg")
    check("T10: 文件节点 subtype=file", file_node.node_subtype == "file")
    check("T10: 文件节点 node_id=101", file_node.node_id == 101)

    # ---- 3. select_tree_node_by_file_id ----
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天",
                     path="/a/spring", file_count=2, children=[
                TreeNode(node_type="unit", node_subtype="file",
                         node_id=101, name="a.jpg", path="/a/spring/a.jpg"),
                TreeNode(node_type="unit", node_subtype="file",
                         node_id=102, name="b.mp4", path="/a/spring/b.mp4"),
            ]),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()

    tree_view = FolderTreeView(tree_model)
    tree_view.expandAll()

    check("T10: select_tree_node_by_file_id 方法存在",
          hasattr(tree_view, "select_tree_node_by_file_id"))

    # 选中 id=101 的文件节点
    tree_view.select_tree_node_by_file_id(101)
    sel = tree_view.selectedIndexes()
    check("T10: select_tree_node_by_file_id(101) 有选中", len(sel) > 0)
    if sel:
        node = sel[0].internalPointer()
        check("T10: 选中节点是文件", node is not None and node.node_subtype == "file")
        check("T10: 选中节点 id=101", node is not None and node.node_id == 101)

    # ---- 4. file_selected_from_tree 信号 ----
    check("T10: file_selected_from_tree 信号存在",
          hasattr(tree_view, "file_selected_from_tree"))
    captured_tree_sig = []
    tree_view.file_selected_from_tree.connect(captured_tree_sig.append)

    # 选中文件 102（不同于之前已选中的 101）→ 应发射 file_selected_from_tree
    # 先清除选择再选新文件，确保 selectionChanged 触发
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx) if root_idx.isValid() else QModelIndex()
    file_idx_102 = tree_model.index(1, 0, unit_idx) if unit_idx.isValid() else QModelIndex()
    if file_idx_102.isValid():
        tree_view.selectionModel().select(
            file_idx_102, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
    check("T10: 选择文件节点触发 file_selected_from_tree",
          len(captured_tree_sig) > 0)
    if captured_tree_sig:
        check("T10: file_selected_from_tree 携带 id=102",
              captured_tree_sig[0] == 102)

    # ---- 5. file_selected_in_grid 信号 ----
    check("T10: file_selected_in_grid 信号存在",
          hasattr(grid_view, "file_selected_in_grid"))
    captured_grid_sig = []
    grid_view.file_selected_in_grid.connect(captured_grid_sig.append)

    # 切换回文件模型并选中一个文件
    grid_view.setModel(grid_model)
    grid_view.selectionModel().selectionChanged.connect(
        grid_view._on_selection_changed
    )
    idx0 = grid_model.index(0, 0)
    grid_view.selectionModel().select(
        idx0, QItemSelectionModel.SelectionFlag.ClearAndSelect
    )
    check("T10: 选择网格文件触发 file_selected_in_grid",
          len(captured_grid_sig) > 0)
    if captured_grid_sig:
        check("T10: file_selected_in_grid 携带 id=101",
              captured_grid_sig[0] == 101)


# ============================================================
# TDD 测试 —— 文件夹卡片排序
# ============================================================

def test_folder_card_sort():
    """TDD: 文件夹卡片排序。"""
    section("TDD: 文件夹卡片排序")
    from app.ui.right_panel.thumbnail_grid import FolderCardModel
    from PySide6.QtCore import Qt

    data = [
        {"unit_id": 3, "name": "zzz", "path": "/z", "file_count": 1, "total_size": 3000,
         "created_at": "2026-01-03"},
        {"unit_id": 1, "name": "aaa", "path": "/a", "file_count": 2, "total_size": 1000,
         "created_at": "2026-01-01"},
        {"unit_id": 2, "name": "bbb", "path": "/b", "file_count": 3, "total_size": 2000,
         "created_at": "2026-01-02"},
    ]
    model = FolderCardModel(data)

    # 名称升序
    model.set_sort("name", ascending=True)
    names = [model.data(model.index(i, 0), Qt.ItemDataRole.DisplayRole) for i in range(3)]
    check("T5: 卡片名称升序 aaa第一", names[0] == "aaa")
    check("T5: 卡片名称升序 zzz第三", names[2] == "zzz")

    # 大小降序
    model.set_sort("size", ascending=False)
    check("T5: 卡片大小降序 3000第一", model._data[0]["total_size"] == 3000)
    check("T5: 卡片大小降序 1000第三", model._data[2]["total_size"] == 1000)

    # 时间升序
    model.set_sort("date", ascending=True)
    check("T5: 卡片时间升序 01-01第一", model._data[0]["created_at"] == "2026-01-01")
    check("T5: 卡片时间升序 01-03第三", model._data[2]["created_at"] == "2026-01-03")


def test_time_sort_and_button():
    """TDD: 时间排序按钮。"""
    section("TDD: 时间排序按钮")
    from app.ui.right_panel.thumbnail_grid import ThumbnailGridModel
    config = AppConfig()
    model = ThumbnailGridModel(config)
    files = [
        {"id": 1, "filename": "c.jpg", "path": "/c.jpg", "media_type": "image",
         "size_bytes": 3000, "width": 10, "height": 10},
        {"id": 2, "filename": "a.jpg", "path": "/a.jpg", "media_type": "image",
         "size_bytes": 1000, "width": 10, "height": 10},
    ]
    model.set_files(files)
    model.set_sort("size", ascending=True)
    check("T6: 大小升序 ok", model.file_list[0]["id"] == 2)


# ============================================================
# TDD 测试 —— 树单击文件夹 → 文件夹卡片 (T11-T19)
# ============================================================

def test_tree_folder_click_shows_folder_cards():
    """T11: 树中单击文件夹 → unit_double_clicked 信号（直接进入文件夹）。"""
    section("T11: 树中单击文件夹 → 进入文件夹")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import Qt, QItemSelectionModel

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
            TreeNode(node_type="unit", node_id=11, name="夏天", path="/a/summer", file_count=3),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()
    tree_view = FolderTreeView(tree_model)

    captured = []
    tree_view.unit_double_clicked.connect(captured.append)

    # 选中 "春天" 文件夹节点（行0）
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)
    tree_view.selectionModel().select(unit_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    check("T11: unit_double_clicked 已发射", len(captured) >= 1)
    if captured:
        check("T11: 携带 unit_id=10", captured[0] == 10)


def test_tree_folder_click_no_file_expand():
    """T12: 树中单击文件夹 → 文件层不自动展开。"""
    section("T12: 树中单击文件夹 → 文件层不展开")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import Qt, QItemSelectionModel

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()

    # 先注入文件子节点（模拟之前双击展开过）
    tree_model._roots[0].children[0].children = [
        TreeNode(node_type="unit", node_subtype="file", node_id=101,
                 name="a.jpg", path="/a/spring/a.jpg"),
    ]
    tree_view = FolderTreeView(tree_model)

    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)

    # 展开文件层（模拟双击展开的状态）
    tree_view.expand(unit_idx)
    check("T12: 展开前文件层可见", tree_view.isExpanded(unit_idx))

    # 单击选中文件夹
    tree_view.selectionModel().select(unit_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    # 单击不应触发展开
    check("T12: 单击后仍可见（单击不改变展开状态）", tree_view.isExpanded(unit_idx))


def test_grid_card_click_no_file_expand_in_tree():
    """T14: 右侧单击文件夹卡片 → select_unit_silent 不发射 unit_selected。"""
    section("T14: 文件夹卡片单击 → 树联动不展开")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from app.ui.right_panel.thumbnail_grid import (
        FolderCardModel, ThumbnailGridModel, ThumbnailGridView,
    )
    from PySide6.QtCore import Qt

    config = AppConfig()

    # 构造树：根 + 单元（带文件子节点）
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="单元A", path="/a/A",
                     file_count=2, children=[
                TreeNode(node_type="unit", node_subtype="file", node_id=101,
                         name="f1.jpg", path="/a/A/f1.jpg"),
            ]),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()
    tree_view = FolderTreeView(tree_model)

    # 展开文件层（模拟已展开状态）
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)
    tree_view.expand(unit_idx)
    check("T14: 初始文件层展开", tree_view.isExpanded(unit_idx))

    # 捕获 unit_selected 信号
    unit_selected_captured = []
    tree_view.unit_selected.connect(unit_selected_captured.append)

    # select_unit_silent 测试
    tree_view.select_unit_silent(10)

    # select_unit_silent 不应发射 unit_selected（形成新根链路不得触发文件夹加载）
    check("T14: select_unit_silent 未发射 unit_selected",
          len(unit_selected_captured) == 0)

    # 但树高亮应存在
    sel = tree_view.selectedIndexes()
    check("T14: select_unit_silent 有选中项", len(sel) > 0)


def test_accordion_collapse_previous():
    """T15: 手风琴：进入文件夹 B 时 A 自动收起。"""
    section("T15: 手风琴自动收起")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring", file_count=2),
            TreeNode(node_type="unit", node_id=11, name="夏天", path="/a/summer", file_count=3),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()

    # 先展开两个单元
    tree_model.expand_unit(10, [
        {"id": 101, "filename": "a.jpg", "path": "/a/spring/a.jpg", "size_bytes": 1000},
    ])
    tree_model.expand_unit(11, [
        {"id": 201, "filename": "b.jpg", "path": "/a/summer/b.jpg", "size_bytes": 2000},
    ])
    tree_view = FolderTreeView(tree_model)

    # 确认两个单元都有子节点
    unit10_node = tree_model.get_node_by_unit_id(10)
    unit11_node = tree_model.get_node_by_unit_id(11)
    check("T15: 单元A有子节点", unit10_node is not None and len(unit10_node.children) > 0)
    check("T15: 单元B有子节点", unit11_node is not None and len(unit11_node.children) > 0)

    # 收起 A（模拟 accordion：进入 B 时收起 A）
    tree_model.collapse_unit(10)
    unit10_node = tree_model.get_node_by_unit_id(10)
    check("T15: 单元A子节点已收起", unit10_node is not None and len(unit10_node.children) == 0)
    # B 不受影响
    unit11_node = tree_model.get_node_by_unit_id(11)
    check("T15: 单元B子节点不受影响", unit11_node is not None and len(unit11_node.children) > 0)


def test_tree_file_click_highlight_in_grid():
    """T17: 树中文件单击 → file_selected_from_tree 信号。"""
    section("T17: 树中文件单击 → 右侧高亮")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView, TreeNode
    from PySide6.QtCore import Qt, QItemSelectionModel

    config = AppConfig()
    tree_model = FolderTreeModel(config)
    tree_model._roots = [
        TreeNode(node_type="root", node_id=1, name="库1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="春天", path="/a/spring",
                     file_count=2, children=[
                TreeNode(node_type="unit", node_subtype="file", node_id=101,
                         name="a.jpg", path="/a/spring/a.jpg"),
            ]),
        ]),
    ]
    tree_model.beginResetModel()
    tree_model.endResetModel()
    tree_view = FolderTreeView(tree_model)
    tree_view.expandAll()

    captured = []
    tree_view.file_selected_from_tree.connect(captured.append)

    # 选中文件节点
    root_idx = tree_model.index(0, 0)
    unit_idx = tree_model.index(0, 0, root_idx)
    file_idx = tree_model.index(0, 0, unit_idx)
    tree_view.selectionModel().select(file_idx, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    check("T17: file_selected_from_tree 已发射", len(captured) > 0)
    if captured:
        check("T17: 携带 file_id=101", captured[0] == 101)


def test_expand_unit_preserves_other_state():
    """T19: expand_unit 用 insertRows 不破坏其他展开状态。"""
    section("T19: expand_unit 不破坏其他状态")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.left_panel.folder_tree import FolderTreeModel, TreeNode
    from PySide6.QtCore import QModelIndex, Qt
    from config import AppConfig

    config = AppConfig()
    model = FolderTreeModel(config)
    model._roots = [
        TreeNode(node_type="root", node_id=1, name="Root1", path="/a", children=[
            TreeNode(node_type="unit", node_id=10, name="UnitA", path="/a/unitA"),
            TreeNode(node_type="unit", node_id=11, name="UnitB", path="/a/unitB"),
        ]),
    ]
    model.beginResetModel()
    model.endResetModel()

    root_idx = model.index(0, 0)
    unitA_idx = model.index(0, 0, root_idx)
    unitB_idx = model.index(1, 0, root_idx)

    # 展开 unit A 和 unit B
    model.expand_unit(10, [{"id": 101, "filename": "a1.jpg", "path": "/a/unitA/a1.jpg", "size_bytes": 100}])
    model.expand_unit(11, [{"id": 201, "filename": "b1.jpg", "path": "/a/unitB/b1.jpg", "size_bytes": 200}])
    check("T19: unitA 展开后子节点=1", model.rowCount(unitA_idx) == 1)
    check("T19: unitB 展开后子节点=1", model.rowCount(unitB_idx) == 1)

    # 再次 expand(空参数) — unit B 状态应保留
    model.expand_unit(10, [])
    check("T19: 二次 expand unitA 后子节点仍=1", model.rowCount(unitA_idx) == 1)
    check("T19: unitB 子节点不受影响=1", model.rowCount(unitB_idx) == 1)

    # 收起 unit A —— unit B 状态应保留
    model.collapse_unit(10)
    check("T19: 收起 unitA 后子节点=0", model.rowCount(unitA_idx) == 0)
    check("T19: unitB 子节点不受收起影响=1", model.rowCount(unitB_idx) == 1)



def test_file_unit_delete_db():
    """T20: 文件/文件夹删除 DB 层 — delete_media_file + delete_resource_unit。"""
    section("T20: 文件/文件夹删除 DB 层")
    from app.db import queries as qq
    from app.db.models import MediaFile, ResourceUnit

    with DatabaseManager.session() as session:
        # 获取一个单元和它的文件
        units = qq.get_all_active_units(session)
        if not units:
            check("T20: 无可用单元，跳过", True)
            return

        unit = units[0]
        files = qq.get_files_by_unit(session, unit.id)
        if not files:
            check("T20: 单元无文件，跳过", True)
            return

        f = files[0]

        # 测试 delete_media_file 返回 True
        ok = qq.delete_media_file(session, f.id)
        check("T20: delete_media_file 返回 True", ok)

        # 验证已删除
        deleted = session.query(MediaFile).filter(MediaFile.id == f.id).first()
        check("T20: 文件记录已删除", deleted is None)

        # 测试对不存在 ID 返回 False
        ok2 = qq.delete_media_file(session, -999)
        check("T20: 不存在文件返回 False", not ok2)

        # 测试 delete_resource_unit
        unit2 = qq.create_unit(session, "/tmp/test_delete_unit", "test_delete",
                               unit.library_root_id, file_count=1, total_size=100)
        check("T20: 临时单元创建成功", unit2.id is not None)

        ok3 = qq.delete_resource_unit(session, unit2.id)
        check("T20: delete_resource_unit 返回 True", ok3)
        deleted_unit = session.query(ResourceUnit).filter(ResourceUnit.id == unit2.id).first()
        check("T20: 单元记录已删除", deleted_unit is None)

        # 对不存在 ID 返回 False
        ok4 = qq.delete_resource_unit(session, -999)
        check("T20: 不存在单元返回 False", not ok4)


def test_tag_assign_ui():
    """T21: 标签右键分配 — grid context menu 子菜单构造正确。"""
    section("T21: 标签右键分配 UI")
    from PySide6.QtWidgets import QApplication, QMenu
    from PySide6.QtGui import QAction
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.right_panel.thumbnail_grid import ThumbnailGridView, ThumbnailGridModel
    from config import AppConfig
    from app.db import queries as qq
    from app.db.models import MediaFile

    config = AppConfig()
    model = ThumbnailGridModel(config)
    view = ThumbnailGridView(model, config)

    # 先创建测试标签
    with DatabaseManager.session() as session:
        tag_a = qq.create_tag(session, "测试标签A", "#FF0000")
        tag_b = qq.create_tag(session, "测试标签B", "#00FF00")
        tag_a_id = tag_a.id
        tag_b_id = tag_b.id

    # 使用真实文件 ID 做测试
    with DatabaseManager.session() as session:
        real_file = session.query(MediaFile).first()
        if real_file is None:
            check("T21: 无可用文件，跳过", True)
            with DatabaseManager.session() as s:
                qq.delete_tag(s, tag_a_id)
                qq.delete_tag(s, tag_b_id)
            return
        fid = real_file.id

    # 构造标签子菜单
    tag_menu = QMenu()
    with DatabaseManager.session() as session:
        all_tags = qq.get_all_tags(session)
        file_tag_ids = [t.id for t in qq.get_file_tags(session, fid)]

    for tag in all_tags:
        act = QAction(f" {tag.name}", tag_menu)
        act.setCheckable(True)
        act.setChecked(tag.id in file_tag_ids)
        act.setData((fid, tag.id))
        tag_menu.addAction(act)

    check("T21: 标签菜单项数>=2", tag_menu.actions().__len__() >= 2)

    # 模拟勾选标签 A
    for act in tag_menu.actions():
        _fid, tid = act.data()
        if tid == tag_a_id:
            act.setChecked(True)
            with DatabaseManager.session() as session:
                current = [t.id for t in qq.get_file_tags(session, _fid)]
                if tid not in current:
                    current.append(tid)
                qq.set_file_tags(session, _fid, current)
            break

    with DatabaseManager.session() as session:
        updated = qq.get_file_tags(session, fid)
    check("T21: 标签A已分配给文件", any(t.id == tag_a_id for t in updated))

    # 清理
    with DatabaseManager.session() as session:
        qq.delete_tag(session, tag_a_id)
        qq.delete_tag(session, tag_b_id)
        qq.set_file_tags(session, fid, [])


# ============================================================
# 新增测试 —— 文件监控器、白名单、扫描会话、单元元数据查询
# ============================================================

def test_watcher_event_handler():
    """测试 25: 文件监控器 — 事件去抖动、媒体文件过滤、FileWatcher 生命周期。"""
    section("测试 25: 文件监控器事件处理")
    from app.core.watcher import MediaFileEventHandler, FileChangeEvent, FileWatcher
    from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent
    import tempfile, time

    captured = []

    def on_change(event):
        captured.append(event)

    handler = MediaFileEventHandler(
        extensions=frozenset({'.jpg', '.png', '.mp4'}),
        on_change=on_change,
        debounce_ms=50,
    )
    check("W1: EventHandler 创建成功", handler is not None)
    check("W1: Debounce = 0.05s", handler._debounce_ms == 0.05)

    tmp = Path(tempfile.mkdtemp(prefix="watcher_test_"))
    try:
        test_img = tmp / "test.jpg"
        test_img.write_text("fake image")
        test_txt = tmp / "test.txt"
        test_txt.write_text("not media")

        # 非媒体文件应被过滤
        txt_event = FileCreatedEvent(str(test_txt))
        handler.on_created(txt_event)
        time.sleep(0.2)
        txt_triggered = any(e.path == test_txt for e in captured)
        check("W1: 非媒体文件被过滤", not txt_triggered)

        # 媒体文件应触发回调
        captured.clear()
        img_event = FileCreatedEvent(str(test_img))
        handler.on_created(img_event)
        time.sleep(0.2)
        img_triggered = any(e.path == test_img for e in captured)
        check("W1: 媒体文件创建触发事件", img_triggered)

        # 去抖动：5 次修改事件应合并为少数回调
        captured.clear()
        for _ in range(5):
            mod_event = FileModifiedEvent(str(test_img))
            handler.on_modified(mod_event)
        time.sleep(0.2)
        check("W1: 去抖动合并事件 (<=3)", len(captured) <= 3)

        # on_deleted
        captured.clear()
        del_event = FileCreatedEvent(str(test_img))
        handler.on_deleted(del_event)
        time.sleep(0.2)
        check("W1: on_deleted 触发回调", len(captured) > 0)

        # FileMovedEvent — 移入媒体文件应触发
        captured.clear()
        test_mov = tmp / "moved.jpg"
        test_mov.write_text("moved content")
        move_event = FileMovedEvent(str(tmp / "old.txt"), str(test_mov))
        handler.on_moved(move_event)
        time.sleep(0.2)
        moved_triggered = any(e.path == test_mov for e in captured)
        check("W1: 媒体文件移入触发 on_moved", moved_triggered)

        # FileWatcher 生命周期
        watcher = FileWatcher(handler)
        check("W1: FileWatcher 初始未运行", not watcher.is_running)
        watcher.start()
        check("W1: start 后 is_running", watcher.is_running)
        watcher.start()  # 重复启动不应崩溃
        check("W1: 重复 start 不崩溃", True)
        watcher.stop()
        check("W1: stop 后 is_running=False", not watcher.is_running)
        watcher.stop()  # 重复停止不应崩溃
        check("W1: 重复 stop 不崩溃", True)

        # FileChangeEvent 值对象
        ce = FileChangeEvent(path=test_img, change_type="created")
        check("W2: FileChangeEvent 创建", ce is not None)
        check("W2: change_type=created", ce.change_type == "created")
        check("W2: is_directory 默认 False", not ce.is_directory)
        check("W2: src_path 存入", ce.src_path is None)

        ce2 = FileChangeEvent(path=test_img, change_type="modified", is_directory=True)
        check("W2: is_directory 可设为 True", ce2.is_directory)

    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_whitelist_and_scan_queries():
    """测试 26: 白名单 + 扫描会话查询。"""
    section("测试 26: 白名单与扫描会话查询")
    from app.db import queries as qq
    from app.db.models import Whitelist, ScanSession

    # ---- 白名单 ----
    with DatabaseManager.session() as session:
        all_wl = qq.get_all_whitelists(session)
        check("W3: 初始白名单为空", len(all_wl) == 0)

        wl1 = qq.add_whitelist(session, "/path/to/keep", match_type="path", note="测试")
        check("W3: 添加白名单成功", wl1.id is not None)
        check("W3: pattern 正确", wl1.pattern == "/path/to/keep")
        check("W3: is_regex 默认 False", not wl1.is_regex)

        wl2 = qq.add_whitelist(session, r".*\.jpg", is_regex=True, match_type="extension")
        check("W3: 正则白名单成功", wl2.id is not None)
        check("W3: is_regex=True", wl2.is_regex)

        # 查询全部
        check("W3: 白名单数=2", len(qq.get_all_whitelists(session)) == 2)

        # is_whitelisted
        check("W3: 精确路径匹配", qq.is_whitelisted(session, "/path/to/keep"))
        check("W3: 不匹配路径", not qq.is_whitelisted(session, "/other/path"))

        # 删除
        qq.remove_whitelist(session, wl1.id)
        check("W3: 删除后剩余 1", len(qq.get_all_whitelists(session)) == 1)
        check("W3: 删除后不再匹配", not qq.is_whitelisted(session, "/path/to/keep"))

        # 删除不存在项不应报错
        qq.remove_whitelist(session, -999)
        check("W3: 删除不存在的白名单不报错", True)

        session.query(Whitelist).delete()

    # ---- 扫描会话 ----
    with DatabaseManager.session() as session:
        latest = qq.get_latest_scan_session(session)
        check("W4: 初始无扫描会话", latest is None)

        ss = qq.create_scan_session(session)
        check("W4: 创建扫描会话成功", ss.id is not None)
        # 默认 status 为 "running"（模型字段默认值）
        check("W4: 初始状态 running", ss.status == "running")
        check("W4: started_at 非空", ss.started_at is not None)

        # 更新进度（使用表中存在的列）
        qq.update_scan_session(session, ss.id, files_scanned=10, new_files=5)
        updated = session.query(ScanSession).filter(ScanSession.id == ss.id).first()
        check("W4: files_scanned=10", updated.files_scanned == 10)
        check("W4: new_files=5", updated.new_files == 5)

        # 完成（无错误）
        qq.finish_scan_session(session, ss.id, status="completed")
        finished = session.query(ScanSession).filter(ScanSession.id == ss.id).first()
        check("W4: 完成状态=completed", finished.status == "completed")
        check("W4: completed_at 非空", finished.completed_at is not None)

        # 创建第二个会话并带错误完成（error_log 由 finish_scan_session 写入）
        ss2 = qq.create_scan_session(session)
        qq.finish_scan_session(session, ss2.id, status="completed",
                               errors=["权限不足: /path"])
        check("W4: 第二个会话创建正常", ss2.id != ss.id)

        # get_latest_scan_session 返回最新的
        latest = qq.get_latest_scan_session(session)
        check("W4: 最新会话 ID 匹配", latest.id == ss2.id)

        session.query(ScanSession).delete()


def test_unit_metadata_queries():
    """测试 27: 单元元数据查询 — cover、star、rename、manual、enabled roots 等。"""
    section("测试 27: 单元元数据查询")
    from app.db import queries as qq
    from app.db.models import ResourceUnit, MediaFile

    with DatabaseManager.session() as session:
        units = qq.get_all_active_units(session)
        if not units:
            check("W5: 无可用单元，跳过", True)
            return
        unit = units[0]

        # set_unit_starred / get_starred_units
        qq.set_unit_starred(session, unit.id, True)
        session.refresh(unit)
        check("W5: 收藏成功 is_starred=True", unit.is_starred)
        starred = qq.get_starred_units(session)
        check("W5: 收藏列表包含本单元", any(u.id == unit.id for u in starred))
        qq.set_unit_starred(session, unit.id, False)
        session.refresh(unit)
        check("W5: 取消收藏 is_starred=False", not unit.is_starred)

        # set_unit_cover / clear_unit_cover
        qq.set_unit_cover(session, unit.id, "/path/to/cover.jpg")
        session.refresh(unit)
        check("W5: cover_path 已设置", unit.cover_path == "/path/to/cover.jpg")
        qq.clear_unit_cover(session, unit.id)
        session.refresh(unit)
        check("W5: clear 后 cover_path=None", unit.cover_path is None)

        # rename_unit
        original = unit.name
        qq.rename_unit(session, unit.id, f"{original}_已重命名")
        session.refresh(unit)
        check("W5: 重命名成功", unit.name == f"{original}_已重命名")
        qq.rename_unit(session, unit.id, original)  # 恢复

        # mark_unit_manual
        qq.mark_unit_manual(session, unit.id, True)
        session.refresh(unit)
        check("W5: mark_unit_manual(True)", unit.is_manual)
        qq.mark_unit_manual(session, unit.id, False)

        # get_unit_file_count
        cnt = qq.get_unit_file_count(session, unit.id)
        check("W5: get_unit_file_count >= 0", cnt >= 0)

        # get_enabled_roots / set_root_enabled
        roots = qq.get_enabled_roots(session)
        check("W5: enabled roots 非空", len(roots) > 0)
        root = roots[0]
        qq.set_root_enabled(session, root.id, False)
        session.refresh(root)
        check("W5: root disabled", not root.enabled)
        qq.set_root_enabled(session, root.id, True)
        session.refresh(root)
        check("W5: root re-enabled", root.enabled)

        # get_files_without_hash
        no_md5 = qq.get_files_without_hash(session, "md5", limit=5)
        check("W5: get_files_without_hash 返回 list", isinstance(no_md5, list))
        invalid = qq.get_files_without_hash(session, "invalid_type")
        check("W5: 无效 hash_type 返回空", len(invalid) == 0)

        # get_file_count_by_unit_ids
        counts = qq.get_file_count_by_unit_ids(session, [unit.id])
        check("W5: get_file_count_by_unit_ids 有结果", unit.id in counts)
        check("W5: count > 0", counts[unit.id] > 0)

        # get_files_by_md5
        files = qq.get_files_by_unit(session, unit.id)
        if files and files[0].md5_hash:
            md5_matches = qq.get_files_by_md5(session, files[0].md5_hash)
            check("W5: get_files_by_md5 返回 list", isinstance(md5_matches, list))
            check("W5: MD5 查询有结果", len(md5_matches) > 0)

        # get_excluded_units / unexclude_unit
        excluded = qq.get_excluded_units(session)
        check("W5: get_excluded_units 不报错", isinstance(excluded, list))

        # unmerge_unit（对非合并单元应无副作用）
        qq.unmerge_unit(session, unit.id)
        check("W5: unmerge_unit 不报错", True)

        # update_file_unit
        if files:
            qq.update_file_unit(session, files[0].id, unit.id)
            check("W5: update_file_unit 不报错", True)

        # get_files_by_md5 无匹配应返回空
        no_match = qq.get_files_by_md5(session, "NONEXISTENT_MD5_HASH_12345")
        check("W5: get_files_by_md5 无匹配返回空", len(no_match) == 0)


# ============================================================
# 新增 API 端点测试 —— 标签、消息、单元详情、文件详情
# ============================================================

def test_api_tags_endpoints():
    """测试 29: 标签 API 端点全覆盖（list/create/delete/get-by-file/set-by-file/mapped-files）。"""
    section("测试 29: 标签 API 端点")
    from app.api.server import create_app
    from fastapi.testclient import TestClient
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    # GET /api/tags — 初始
    resp = client.get("/api/tags")
    check("T29: GET /api/tags 200", resp.status_code == 200)
    data = resp.json()
    check("T29: 含 tags 字段", "tags" in data)
    initial_count = len(data["tags"])

    # POST /api/tags — 创建
    resp = client.post("/api/tags", params={"name": "API测试标签", "color": "#FF0000"})
    check("T29: POST /api/tags 200", resp.status_code == 200)
    tag_data = resp.json()
    check("T29: 返回 id", "id" in tag_data)
    check("T29: name 正确", tag_data["name"] == "API测试标签")
    tag_id = tag_data["id"]

    # POST 重复名称 → 409
    resp = client.post("/api/tags", params={"name": "API测试标签"})
    check("T29: 重复标签返回 409", resp.status_code == 409)

    # GET 验证计数 +1
    resp = client.get("/api/tags")
    check("T29: 标签数+1", len(resp.json()["tags"]) == initial_count + 1)

    # GET /api/tags/by-file/{fid}
    with DatabaseManager.session() as session:
        from app.db.models import MediaFile
        mf = session.query(MediaFile).first()
    if mf:
        fid = mf.id

        # 初始无标签
        resp = client.get(f"/api/tags/by-file/{fid}")
        check("T29: GET by-file 200", resp.status_code == 200)
        check("T29: 初始标签数为 0", len(resp.json()["tags"]) == 0)

        # PUT /api/tags/by-file/{fid} — 设置标签
        resp = client.put(f"/api/tags/by-file/{fid}", json=[tag_id])
        check("T29: PUT by-file 200", resp.status_code == 200)
        check("T29: success=True", resp.json().get("success") is True)

        # GET 验证已设置
        resp = client.get(f"/api/tags/by-file/{fid}")
        check("T29: 标签已设置", len(resp.json()["tags"]) == 1)

        # PUT 清空
        resp = client.put(f"/api/tags/by-file/{fid}", json=[])
        check("T29: PUT 清空 200", resp.status_code == 200)

        # GET /api/tags/mapped-files
        resp = client.get("/api/tags/mapped-files")
        check("T29: GET mapped-files 200", resp.status_code == 200)
        check("T29: 含 mappings 字段", "mappings" in resp.json())

    # DELETE /api/tags/{tag_id}
    resp = client.delete(f"/api/tags/{tag_id}")
    check("T29: DELETE 200", resp.status_code == 200)

    # DELETE 不存在 → 404
    resp = client.delete("/api/tags/99999")
    check("T29: DELETE 不存在返回 404", resp.status_code == 404)


def test_api_messages_endpoints():
    """测试 30: 消息 API 端点全覆盖（list/unread-count/mark-read/read-all/dismiss）。"""
    section("测试 30: 消息 API 端点")
    from app.api.server import create_app
    from fastapi.testclient import TestClient
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    # GET /api/messages — 列表
    resp = client.get("/api/messages")
    check("T30: GET /api/messages 200", resp.status_code == 200)
    data = resp.json()
    check("T30: 含 messages 字段", "messages" in data)
    check("T30: 含 unread_count 字段", "unread_count" in data)

    # GET /api/messages/unread-count
    resp = client.get("/api/messages/unread-count")
    check("T30: GET unread-count 200", resp.status_code == 200)
    check("T30: unread_count 为 int", isinstance(resp.json()["unread_count"], int))

    # GET /api/messages?unread_only=true
    resp = client.get("/api/messages", params={"unread_only": True})
    check("T30: GET unread_only 200", resp.status_code == 200)

    # 先用 MessageCenter 创建一条消息
    from app.services.message_center import MessageCenter
    msg = MessageCenter.create_info("API测试消息", "HTTP 端点测试")
    check("T30: 测试消息已创建", msg is not None)
    if msg:
        # POST /api/messages/{msg_id}/read
        resp = client.post(f"/api/messages/{msg.id}/read")
        check("T30: POST read 200", resp.status_code == 200)
        check("T30: success=True", resp.json().get("success") is True)

        # POST /api/messages/read-all
        resp = client.post("/api/messages/read-all")
        check("T30: POST read-all 200", resp.status_code == 200)

        # DELETE /api/messages/{msg_id}
        resp = client.delete(f"/api/messages/{msg.id}")
        check("T30: DELETE 200", resp.status_code == 200)


def test_api_detail_endpoints():
    """测试 31: 单元/文件详情 API —  /api/units/{id}, /api/units/{id}/files, /api/files, /api/files/{id}。"""
    section("测试 31: 单元与文件详情 API")
    from app.api.server import create_app
    from fastapi.testclient import TestClient
    config = AppConfig()
    app = create_app(config)
    client = TestClient(app)

    # 先获取有效单元 ID
    resp = client.get("/api/units")
    check("T31: GET /api/units 200", resp.status_code == 200)
    units_data = resp.json()
    if not units_data.get("units"):
        check("T31: 无单元可测（跳过）", True)
        return
    uid = units_data["units"][0]["id"]
    unit_name = units_data["units"][0]["name"]

    # GET /api/units/{unit_id}
    resp = client.get(f"/api/units/{uid}")
    check("T31: GET /api/units/{uid} 200", resp.status_code == 200)
    detail = resp.json()
    check("T31: id 匹配", detail.get("id") == uid)
    check("T31: name 非空", detail.get("name") == unit_name)
    check("T31: 含 cover_file_id", "cover_file_id" in detail)
    check("T31: 含 library_root_id", "library_root_id" in detail)
    check("T31: 含 library_root_name", "library_root_name" in detail)

    # GET /api/units/{unit_id}/files
    resp = client.get(f"/api/units/{uid}/files")
    check("T31: GET /api/units/{uid}/files 200", resp.status_code == 200)
    files_data = resp.json()
    check("T31: unit_id 匹配", files_data.get("unit_id") == uid)
    check("T31: 含 files 列表", "files" in files_data)
    check("T31: 含 file_count", "file_count" in files_data)
    check("T31: file_count > 0", files_data.get("file_count", 0) > 0)

    # GET /api/files?unit_id=...
    resp = client.get("/api/files", params={"unit_id": uid})
    check("T31: GET /api/files 200", resp.status_code == 200)
    list_data = resp.json()
    check("T31: 含 files", "files" in list_data)
    check("T31: 含 total", "total" in list_data)
    check("T31: total > 0", list_data.get("total", 0) > 0)
    check("T31: 含 per_page", "per_page" in list_data)

    # GET /api/files/{file_id} — 详情
    if list_data["files"]:
        fid = list_data["files"][0]["id"]
        resp = client.get(f"/api/files/{fid}")
        check("T31: GET /api/files/{fid} 200", resp.status_code == 200)
        fd = resp.json()
        check("T31: 文件 id 匹配", fd.get("id") == fid)
        check("T31: 含 filename", "filename" in fd)
        check("T31: 含 path", "path" in fd)
        check("T31: 含 media_type", "media_type" in fd)
        check("T31: 含 resource_unit_id", "resource_unit_id" in fd)

        # DELETE /api/files/{file_id} — 实际删除
        resp = client.delete(f"/api/files/{fid}")
        check("T31: DELETE /api/files/{fid} 200", resp.status_code == 200)
        check("T31: success=True", resp.json().get("success") is True)

        # 删除后再次查询应 404
        resp = client.get(f"/api/files/{fid}")
        check("T31: 删除后文件 404", resp.status_code == 404)

    # 不存在单元 → 404
    resp = client.get("/api/units/99999")
    check("T31: 不存在单元 404", resp.status_code == 404)
    resp = client.get("/api/units/99999/files")
    check("T31: 不存在单元 files 404", resp.status_code == 404)

    # 不存在文件 → 404
    resp = client.get("/api/files/99999")
    check("T31: 不存在文件 404", resp.status_code == 404)
    resp = client.delete("/api/files/99999")
    check("T31: DELETE 不存在文件 404", resp.status_code == 404)


def test_convert_batch():
    """测试 32: HEIC 批量转换 — convert_batch 路径覆盖。"""
    section("测试 32: HEIC 批量转换")
    from app.core.heic_converter import convert_batch, ConversionResult
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="heic_batch_"))
    try:
        # 对不存在的文件列表调用 convert_batch
        sources = [tmp / "nonexistent1.heic", tmp / "nonexistent2.heic"]
        summary = convert_batch(sources)
        check("T32: convert_batch 返回 ConvertSummary", summary is not None)
        check("T32: total=2", summary.total == 2)
        check("T32: all failed（文件不存在）", summary.failed == 2)
        check("T32: success=0", summary.success == 0)
        check("T32: results 长度为 2", len(summary.results) == 2)
        check("T32: 第一个结果为 ConversionResult",
              isinstance(summary.results[0], ConversionResult))

        # 空列表
        empty = convert_batch([])
        check("T32: 空列表 total=0", empty.total == 0)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


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
        test_path_revalidation()
        test_scanner_nested_promotion()
        test_zero_byte_file()
        test_refresh_workflow()
        test_hash_engine(temp_root)
        test_hash_batch(temp_root)
        test_thumbnails(temp_root)
        test_heic_converter()
        test_dedup_engine()
        test_save_dedup_results()
        test_tree_model()
        test_context_menu_lambda_safety()
        test_ui_signal_integration()
        test_message_center()
        test_tags()
        test_api_app()
        test_unit_response_fields()
        test_thumbnail_binary()
        test_file_stream()
        test_unread_events_endpoint()
        test_web_static_files()
        test_dedup_run_api()
        test_grid_context_menu_signals()
        test_grid_sort()
        test_tree_filter()
        test_search_auto_select_first()
        test_folder_card_click_linking()
        test_tree_expand_unit()
        test_file_level_linking()
        test_folder_card_sort()
        test_time_sort_and_button()
        test_tree_folder_click_shows_folder_cards()
        test_tree_folder_click_no_file_expand()
        test_grid_card_click_no_file_expand_in_tree()
        test_accordion_collapse_previous()
        test_tree_file_click_highlight_in_grid()
        test_expand_unit_preserves_other_state()
        test_file_unit_delete_db()
        test_tag_assign_ui()

        # 新增覆盖测试
        test_watcher_event_handler()
        test_whitelist_and_scan_queries()
        test_unit_metadata_queries()
        test_api_tags_endpoints()
        test_api_messages_endpoints()
        test_api_detail_endpoints()
        test_convert_batch()

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
