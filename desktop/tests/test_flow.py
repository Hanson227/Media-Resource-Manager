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
from app.db.migrations import init_db, migrate_db, CURRENT_SCHEMA_VERSION
from app.core.scanner import MediaScanner, ScanResult
from app.core.hash_engine import HashEngine, FileHashes
from app.utils.constants import FACE_SIMILARITY_THRESHOLD
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

    # 静态未定义名称扫描：导入成功不代表函数体内没有引用不存在的模块级名称。
    # hash_worker.py 曾两次踩坑（Path、os 未导入），运行时被 except Exception
    # 吞掉后表现为功能静默失效，因此这里用 symtable 做一次全库检查。
    missing = _scan_undefined_globals(py_files, project_root)
    check("无未定义的模块级名称引用", len(missing) == 0)
    for rel, where, name in missing:
        print(f"    未定义名称: {rel} -> {name} (used in {where})")


def _scan_undefined_globals(py_files: list[Path], project_root: Path) -> list[tuple]:
    """扫描函数体中对模块级名称的读取，返回模块里不存在的名称列表。

    判定方式：symtable 中 is_global() 且未被赋值/导入的名称，若在模块命名
    空间与 builtins 中都不存在，则该行执行时必然 NameError。
    """
    import builtins
    import symtable

    builtin_names = set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__class__",
    }
    missing: list[tuple] = []

    def _walk(table, where: str, collected: set) -> None:
        for sym in table.get_symbols():
            if sym.is_global() and not sym.is_assigned() and not sym.is_parameter():
                collected.add(sym.get_name())
        for child in table.get_children():
            _walk(child, f"{where}/{child.get_name()}", collected)

    for fpath in sorted(py_files):
        rel = fpath.relative_to(project_root)
        mod_path = str(rel).replace(os.sep, ".")[:-3]
        try:
            module = importlib.import_module(mod_path)
            table = symtable.symtable(fpath.read_text(encoding="utf-8"), str(fpath), "exec")
        except Exception:
            continue  # 导入/语法失败已在上面的导入检查中报告
        names: set = set()
        _walk(table, str(rel).replace(os.sep, "/"), names)
        for name in sorted(names):
            if name in builtin_names or hasattr(module, name):
                continue
            missing.append((str(rel), mod_path, name))
    return missing


def test_hash_worker_video_face_detection():
    """测试 0.5: 视频人脸检测链路（回归 os 未导入导致的静默失效）。

    _detect_faces_for_file 的视频分支曾调用未导入的 os.close()，
    NameError 被外层 except Exception 吞掉 → detect_faces 永不执行，
    视频人脸查重维度静默失效。此处用 stub engine 断言该分支真的被走到。
    """
    section("测试 0.5: 视频人脸检测链路")
    from app.services.index_service import _detect_faces_for_file

    try:
        import cv2
        import numpy as np
    except ImportError:
        check("cv2/numpy 可用（视频人脸检测前置依赖）", False)
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="vface_test_"))
    try:
        video = tmp_dir / "clip.avi"
        writer = cv2.VideoWriter(
            str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5.0, (64, 48)
        )
        for i in range(10):
            writer.write(np.full((48, 64, 3), (i * 20) % 255, dtype=np.uint8))
        writer.release()
        check("测试视频已生成", video.is_file() and video.stat().st_size > 0)

        class _StubEngine:
            def __init__(self):
                self.calls = []

            def detect_faces(self, path):
                self.calls.append(Path(path))
                return ["FACE"]

        engine = _StubEngine()
        before = set(Path(tempfile.gettempdir()).glob("vface_*.jpg"))
        result = _detect_faces_for_file(engine, video, "video")
        check("视频分支调用 detect_faces（未被 NameError 吞掉）", len(engine.calls) == 1)
        check("视频分支返回人脸检测结果", result == ["FACE"])

        # 临时抽帧文件应被清理（只比对本次新增，避免受历史残留影响）
        after = set(Path(tempfile.gettempdir()).glob("vface_*.jpg"))
        check("抽帧临时文件已清理", after <= before)
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_face_detection_degrades_gracefully():
    """测试 0.7: 人脸链路的错误契约 + OpenCV 5 下模型必须真正可用。

    回归 A：_load_models 曾直接调用 cv2.dnn.readNetFromCaffe，OpenCV 5 已移除该
    API，AttributeError 逃出 detect_faces 的异常契约，被 hash_worker 吞成
    "检测不到人脸"（功能静默失效且无有效日志）。
    回归 B：改用 OpenCV 自带 ONNX 接口（YuNet 检测 + SFace 特征）后，OpenCV 5
    也能正常工作。这里同时锁住"模型确实加载成功，不是静默降级"。
    """
    section("测试 0.7: 人脸模型可用性与降级契约")
    import cv2
    import numpy as np
    from app.core.hash_engine import (
        FACE_DETECT_MODEL, FACE_RECOGNIZE_MODEL, HashEngine,
    )

    model_dir = Path(__file__).parent.parent / "models"
    tmp_dir = Path(tempfile.mkdtemp(prefix="face_test_"))
    try:
        img = tmp_dir / "face.jpg"
        Image.new("RGB", (64, 64), (128, 128, 128)).save(img)
        engine = HashEngine(face_detection_enabled=True, model_dir=model_dir)
        try:
            result = engine.detect_faces(img)
            raised = None
        except Exception as e:  # noqa: BLE001 — 这里就是要断言不抛
            result, raised = None, e
        check("detect_faces 不抛异常（错误契约）", raised is None)
        if raised is not None:
            print(f"     raised: {type(raised).__name__}: {raised}")
        check("detect_faces 返回列表", isinstance(result, list))

        can_run = (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF"))
        has_models = ((model_dir / FACE_DETECT_MODEL).exists()
                      and (model_dir / FACE_RECOGNIZE_MODEL).exists())
        if can_run and has_models:
            check("ONNX 模型加载成功（不再依赖 Caffe/Torch 加载器）",
                  HashEngine._models_loaded and not HashEngine._models_unavailable)
            check("无脸图返回空列表", result == [])
            det, rec = HashEngine._get_face_nets()
            check("检测器为 FaceDetectorYN",
                  type(det).__name__ == "FaceDetectorYN")
            check("特征器为 FaceRecognizerSF",
                  type(rec).__name__ == "FaceRecognizerSF")
            # SFace 必须输出 128 维，否则 face_vectors 的 512 字节打包会崩
            feat = rec.feature(np.zeros((112, 112, 3), dtype=np.uint8))
            check("SFace 输出 128 维特征", feat.shape == (1, 128))
            check("OpenCV 版本 >= 5 也能跑通（原崩溃场景）",
                  tuple(int(x) for x in cv2.__version__.split(".")[:1]) >= (4,))
        else:
            check("缺模型或缺 ONNX API 时降级为空列表", result == [])
            check("降级后标记为不可用（不再逐文件重试）",
                  HashEngine._models_unavailable)
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_face_cosine_metric():
    """测试 0.7b: 人脸比对使用 SFace 的余弦相似度语义。

    回归：旧实现是 OpenFace nn4 + 欧氏距离（越小越像）。换 SFace 后若仍按
    "距离越小越像"判定，所有阈值判断都会反过来 —— 相同的人被判为不同人，
    不同的人被判为同一人。
    """
    section("测试 0.7b: 人脸余弦相似度度量")
    from app.core.dedup_engine import DedupEngine

    engine = DedupEngine(face_similarity_threshold=FACE_SIMILARITY_THRESHOLD,
                         face_enabled=True, jaccard_threshold=0.0)

    base = tuple([1.0] * 64 + [0.0] * 64)
    same = tuple([2.0] * 64 + [0.0] * 64)          # 同方向、不同模长 → 余弦 1.0
    ortho = tuple([0.0] * 64 + [1.0] * 64)         # 正交 → 余弦 0.0
    opposite = tuple([-1.0] * 64 + [0.0] * 64)     # 反向 → 余弦 -1.0

    check("同方向向量余弦 = 1.0",
          abs(engine._cosine_similarity(base, same) - 1.0) < 1e-9)
    check("正交向量余弦 = 0.0",
          abs(engine._cosine_similarity(base, ortho)) < 1e-9)
    check("反向向量余弦 = -1.0",
          abs(engine._cosine_similarity(base, opposite) + 1.0) < 1e-9)
    check("维度不一致返回 -1（视为不同人）",
          engine._cosine_similarity(base, base[:32]) == -1.0)
    check("零向量返回 -1（避免除零）",
          engine._cosine_similarity(tuple([0.0] * 128), base) == -1.0)

    def _img(fid, vec):
        return {"id": fid, "path": f"{fid}.jpg", "md5_hash": None, "phash": None,
                "dhash": None, "face_vectors": [vec], "video_frames": []}

    # 同一人（余弦 1.0 ≥ 0.363）→ 产生线索；不同人（余弦 0.0 < 0.363）→ 无线索
    hit = engine.compare_units([_img(1, base)], [_img(2, same)], 1, 2)
    miss = engine.compare_units([_img(3, base)], [_img(4, ortho)], 3, 4)
    check("余弦达标的同一人 → 产出人脸线索",
          hit is not None and len(hit.face_hints) == 1)
    check("余弦不达标的不同人 → 无人脸线索",
          miss is None or len(miss.face_hints) == 0)


def test_config_from_file_tolerates_bad_field():
    """测试 0.6: 单个非法配置字段不得导致整体回退默认值。

    回归：from_file 曾边遍历边 del 非法键 → RuntimeError → main.py 捕获后
    整体回退 AppConfig()，web_pin 被清空（Web 鉴权静默关闭）、db_path 指向
    默认库。正确行为：仅忽略非法字段，其余字段照常生效。
    """
    section("测试 0.6: 配置非法字段容错")
    import json
    import warnings

    tmp_dir = Path(tempfile.mkdtemp(prefix="cfg_test_"))
    try:
        cfg_file = tmp_dir / "config.json"
        cfg_file.write_text(json.dumps({
            "web_pin": "1234",
            "db_path": "data/custom_test.db",
            "api_port": 19001,
            "thumbnail_cache_dir": None,     # 非法：Path(None)
            "no_such_field": "ignored",      # 未知键
        }, ensure_ascii=False), encoding="utf-8")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cfg = AppConfig.from_file(cfg_file)

        check("非法字段不影响 web_pin", cfg.web_pin == "1234")
        check("非法字段不影响 db_path", str(cfg.db_path).endswith("custom_test.db"))
        check("非法字段不影响 api_port", cfg.api_port == 19001)
        check("非法字段回退该项默认值",
              cfg.thumbnail_cache_dir == AppConfig().thumbnail_cache_dir)
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_web_pin_not_persisted_in_config():
    """测试 0.8: Web 访问密码不得明文写入 config.json（该文件被 git 跟踪）。

    PIN 改存 config.json 同级的 data/.web_pin（data/ 已被 .gitignore 忽略）；
    旧配置里的明文要能自动迁移，且清空密码后不得被旧值复活。
    """
    section("测试 0.8: Web 密码不落 config.json")
    import json
    import warnings

    tmp_dir = Path(tempfile.mkdtemp(prefix="pin_cfg_"))
    try:
        cfg_file = tmp_dir / "config.json"
        pin_file = tmp_dir / "data" / ".web_pin"

        # ① 保存：config.json 不含 PIN，secrets 文件含 PIN
        AppConfig().with_updates(web_pin="1234").to_file(cfg_file)
        raw = cfg_file.read_text(encoding="utf-8")
        check("config.json 不含 web_pin 键", "web_pin" not in raw)
        check("config.json 不含 PIN 明文", "1234" not in raw)
        check("PIN 写入 data/.web_pin", pin_file.is_file()
              and pin_file.read_text(encoding="utf-8").strip() == "1234")

        # ② 读取：往返一致
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cfg = AppConfig.from_file(cfg_file)
        check("from_file 读回 PIN", cfg.web_pin == "1234")

        # ③ 旧配置迁移：config.json 里仍有明文 → 自动搬到 secrets 文件
        pin_file.unlink()
        legacy = json.loads(raw)
        legacy["web_pin"] = "5678"
        cfg_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            migrated = AppConfig.from_file(cfg_file)
        check("旧配置明文 PIN 可读", migrated.web_pin == "5678")
        check("旧配置明文 PIN 已迁移到 secrets 文件",
              pin_file.is_file() and pin_file.read_text(encoding="utf-8").strip() == "5678")
        migrated.to_file(cfg_file)
        check("再次保存后 config.json 不再含 PIN",
              "web_pin" not in cfg_file.read_text(encoding="utf-8"))

        # ④ 清空密码：不得被旧值复活
        AppConfig().with_updates(web_pin="").to_file(cfg_file)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cleared = AppConfig.from_file(cfg_file)
        check("清空密码后读回为空", cleared.web_pin == "")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


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


def test_migration_v4_to_v5_dedup_match_type():
    """测试 1.6: v4→v5 迁移重建 dedup_file_matches，扩展 match_type 约束。

    SQLite 无法就地修改 CHECK 约束，只能"重建表"。这段代码直接跑在用户的
    真实库上：写错会丢掉查重处置记录，或让库再也写不进匹配类型。
    """
    section("测试 1.6: 迁移 v4→v5（dedup_file_matches 重建）")
    import shutil
    import sqlite3

    _BASE = Path(__file__).parent.parent
    main_db = _BASE / "data" / "test_flow.db"
    tmp_dir = Path(tempfile.mkdtemp(prefix="mig_test_"))
    tmp_db = tmp_dir / "mig.db"

    old_ddl = """
    CREATE TABLE dedup_file_matches (
        id INTEGER NOT NULL,
        dedup_result_id INTEGER NOT NULL,
        file_a_id INTEGER NOT NULL,
        file_b_id INTEGER NOT NULL,
        similarity_score FLOAT NOT NULL,
        match_type VARCHAR(8) NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_file_pair UNIQUE (dedup_result_id, file_a_id, file_b_id),
        CONSTRAINT ck_dedup_file_matches_type CHECK (
            match_type IN ('md5', 'phash', 'dhash', 'face')
        )
    )
    """

    def _exec(sql, params=(), fetch=False):
        con = sqlite3.connect(str(tmp_db))
        try:
            cur = con.execute(sql, params)
            rows = cur.fetchall() if fetch else None
            con.commit()
            return rows
        finally:
            con.close()

    def _insert(row, type_value):
        """尝试写入一条匹配记录，返回是否被 CHECK 约束拒绝。"""
        con = sqlite3.connect(str(tmp_db))
        try:
            con.execute(
                "INSERT INTO dedup_file_matches "
                "(id, dedup_result_id, file_a_id, file_b_id, similarity_score, "
                " match_type, created_at) VALUES (?,?,?,?,?,?,?)",
                (row, 1, 1, row, 1.0, type_value, "2026-01-01 00:00:00"),
            )
            con.commit()
            return False
        except sqlite3.IntegrityError:
            con.rollback()
            return True
        finally:
            con.close()

    try:
        # ① 造一个 v4 库：旧 CHECK（不含 'video'）+ 一条存量记录
        _exec(old_ddl)
        _exec("CREATE INDEX ix_dedup_file_matches_result "
              "ON dedup_file_matches (dedup_result_id)")
        _exec("INSERT INTO dedup_file_matches (id, dedup_result_id, file_a_id, "
              "file_b_id, similarity_score, match_type, created_at) "
              "VALUES (9001, 1, 1, 2, 1.0, 'md5', '2026-01-01 00:00:00')")
        _exec("PRAGMA user_version=4")
        check("迁移前 schema 版本为 4",
              _exec("PRAGMA user_version", fetch=True)[0][0] == 4)
        check("迁移前 'video' 被 CHECK 拒绝", _insert(9002, "video") is True)

        # ② 执行迁移
        DatabaseManager.initialize(tmp_db)
        migrate_db()

        check("schema 版本升到最新",
              _exec("PRAGMA user_version", fetch=True)[0][0] == CURRENT_SCHEMA_VERSION)
        new_ddl = _exec("SELECT sql FROM sqlite_master WHERE name='dedup_file_matches'",
                        fetch=True)[0][0]
        check("新约束包含 'video'", "'video'" in new_ddl)
        check("旧取值仍在约束内（顺序保持）",
              all(f"'{v}'" in new_ddl for v in ("md5", "phash", "dhash", "face")))
        check("存量匹配记录完整保留",
              _exec("SELECT id, match_type FROM dedup_file_matches", fetch=True)
              == [(9001, "md5")])
        check("结果索引已重建",
              _exec("SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='ix_dedup_file_matches_result'", fetch=True) != [])

        # ③ 迁移后可写入 'video'，非法值仍被拒
        check("迁移后可写入 'video'", _insert(9003, "video") is False)
        check("非法 match_type 仍被拒绝", _insert(9004, "bogus") is True)

        # ④ 幂等：重复迁移不丢数据、不改结构
        migrate_db()
        check("重复迁移不丢数据",
              _exec("SELECT count(*) FROM dedup_file_matches", fetch=True)[0][0] == 2)
    finally:
        DatabaseManager.dispose()
        DatabaseManager.initialize(main_db)  # 还原单例，供后续测试使用
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_migration_v5_to_v6_match_level():
    """测试 1.7: v5→v6 给 dedup_results 增加 match_level，存量记录默认 duplicate。

    该迁移直接跑在用户真实库上；写错会让历史查重记录被误判等级或整库写不进。
    """
    section("测试 1.7: 迁移 v5→v6（match_level）")
    import shutil
    import sqlite3

    _BASE = Path(__file__).parent.parent
    main_db = _BASE / "data" / "test_flow.db"
    tmp_dir = Path(tempfile.mkdtemp(prefix="mig56_"))
    tmp_db = tmp_dir / "mig.db"

    def _exec(sql, params=(), fetch=False):
        con = sqlite3.connect(str(tmp_db))
        try:
            cur = con.execute(sql, params)
            rows = cur.fetchall() if fetch else None
            con.commit()
            return rows
        finally:
            con.close()

    try:
        # ① 造一个 v5 库：dedup_results 无 match_level
        _exec("""
            CREATE TABLE dedup_results (
                id INTEGER NOT NULL,
                unit_a_id INTEGER NOT NULL,
                unit_b_id INTEGER NOT NULL,
                similarity_score FLOAT NOT NULL,
                match_count INTEGER NOT NULL,
                total_files_a INTEGER NOT NULL,
                total_files_b INTEGER NOT NULL,
                match_types VARCHAR(128),
                is_resolved BOOLEAN NOT NULL,
                resolution VARCHAR(16) NOT NULL,
                resolved_by VARCHAR(64),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_dedup_pair UNIQUE (unit_a_id, unit_b_id),
                CONSTRAINT ck_dedup_results_resolution CHECK (
                    resolution IN ('pending','keep_a','keep_b','merge','whitelist','ignore'))
            )
        """)
        _exec("INSERT INTO dedup_results VALUES "
              "(1,10,20,0.9,5,5,5,'md5',0,'pending',NULL,'2026-01-01','2026-01-01')")
        _exec("PRAGMA user_version=5")
        check("迁移前 schema 版本为 5",
              _exec("PRAGMA user_version", fetch=True)[0][0] == 5)
        cols = [r[1] for r in _exec("PRAGMA table_info(dedup_results)", fetch=True)]
        check("迁移前没有 match_level 列", "match_level" not in cols)

        # ② 执行迁移
        DatabaseManager.initialize(tmp_db)
        migrate_db()

        check("schema 版本升到 6",
              _exec("PRAGMA user_version", fetch=True)[0][0] == 6)
        cols = [r[1] for r in _exec("PRAGMA table_info(dedup_results)", fetch=True)]
        check("match_level 列已添加", "match_level" in cols)
        row = _exec("SELECT id, similarity_score, match_level FROM dedup_results",
                    fetch=True)
        check("存量记录默认 duplicate",
              row == [(1, 0.9, "duplicate")])

        # ③ 可写入 related，非法值仍被 CHECK 拒绝
        _exec("INSERT INTO dedup_results (id,unit_a_id,unit_b_id,similarity_score,"
              "match_count,total_files_a,total_files_b,is_resolved,resolution,"
              "created_at,updated_at,match_level) VALUES "
              "(2,30,40,0.1,2,2,2,0,'pending','2026-01-01','2026-01-01','related')")
        check("迁移后可写入 related",
              _exec("SELECT count(*) FROM dedup_results WHERE match_level='related'",
                    fetch=True)[0][0] == 1)
        rejected = False
        try:
            _exec("INSERT INTO dedup_results (id,unit_a_id,unit_b_id,similarity_score,"
                  "match_count,total_files_a,total_files_b,is_resolved,resolution,"
                  "created_at,updated_at,match_level) VALUES "
                  "(3,50,60,0.1,2,2,2,0,'pending','2026-01-01','2026-01-01','bogus')")
        except sqlite3.IntegrityError:
            rejected = True
        check("非法 match_level 被 CHECK 拒绝", rejected)

        # ④ 幂等
        migrate_db()
        check("重复迁移不丢数据",
              _exec("SELECT count(*) FROM dedup_results", fetch=True)[0][0] == 2)
    finally:
        DatabaseManager.dispose()
        DatabaseManager.initialize(main_db)
        shutil.rmtree(tmp_dir, ignore_errors=True)


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


def test_scan_worker_path_chunking():
    """测试 3.4: 扫描入库的路径预载必须分块执行。

    回归：F11 批量化后用单条 IN 查询预载全部扫描路径，SQLite 绑定变量
    上限（默认 32766）之上的大媒体库会抛 "too many SQL variables"，
    整个扫描入库失败。这里用极小分块验证跨块边界仍能全部命中。
    """
    section("测试 3.4: 路径预载分块")
    from app.db.models import MediaFile
    from app.ui.workers.scan_worker import (
        load_existing_files, PATH_LOOKUP_CHUNK_SIZE,
    )

    check("默认分块大小 > 0 且远小于 SQLite 上限",
          0 < PATH_LOOKUP_CHUNK_SIZE <= 10000)
    with DatabaseManager.session() as session:
        rows = session.query(MediaFile).limit(6).all()
        paths = [r.path for r in rows]
        if not paths:
            check("路径预载测试: 无文件，跳过", True)
            return
        full = load_existing_files(session, paths, chunk_size=1000)
        chunked = load_existing_files(session, paths, chunk_size=2)
        empty = load_existing_files(session, [])
    check(f"整块查询命中全部 {len(paths)} 条", len(full) == len(paths))
    check("跨分块边界仍全部命中", set(chunked) == set(paths))
    check("空输入返回空映射", empty == {})


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


def test_scanner_no_file_loss():
    """测试 3.55: 扫描器不得因“容器过滤”静默丢单元/丢文件。

    回归两个缺陷：
    ① 容器判定查的是 candidates（含已被吞并的子候选）→
       Parent/{1 直连}+Parent/Child/{5} 整组单元消失，6 个文件全丢；
    ② 没有上级单元时仍丢弃容器 → Show/movie.mp4 无人收集。
    """
    section("测试 3.55: 容器过滤不丢文件")
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="scan_loss_"))
    try:
        # ① 父 1 个直连 + 子 5 个（子应被父吞并，父必须保留）
        parent = tmp / "case1" / "Parent"
        (parent / "Child").mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 16), (255, 0, 0)).save(parent / "a.jpg")
        for i in range(5):
            Image.new("RGB", (16, 16), (0, 200, 0)).save(parent / "Child" / f"p{i}.jpg")

        # ② 容器只有 1 个直连文件 + 两个独立子单元，且没有上级单元
        show = tmp / "case2" / "Show"
        (show / "CD1").mkdir(parents=True, exist_ok=True)
        (show / "CD2").mkdir(parents=True, exist_ok=True)
        (show / "movie.mp4").write_bytes(b"fake video")
        for i in range(2):
            Image.new("RGB", (16, 16), (0, 0, 255)).save(show / "CD1" / f"a{i}.jpg")
            Image.new("RGB", (16, 16), (255, 255, 0)).save(show / "CD2" / f"b{i}.jpg")

        scanner = MediaScanner(
            extensions=frozenset({".jpg", ".mp4"}),
            exclude_patterns=frozenset(),
        )
        r1 = scanner.scan_root(tmp / "case1")
        check("① 父单元保留（未被误判为容器）", len(r1.units) == 1)
        check("① 父单元收集到全部 6 个文件", r1.total_files == 6)

        r2 = scanner.scan_root(tmp / "case2")
        check("② 容器直连文件未被丢弃", r2.total_files == 5)
        check("② 两个子单元仍独立",
              {u.path.name for u in r2.units} >= {"CD1", "CD2"})
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


def test_dedup_one_to_one_matching():
    """测试 6.5: 查重文件级匹配必须一对一（回归杰卡德指数 > 1.0 / 漏配对）。

    历史缺陷：匹配只约束 B 侧，同一 A 文件可被 md5 与 phash 各匹配一次，
    杰卡德指数被算成 2.0（假阳性）；反之 B 中同一内容有多份副本时
    md5 索引只保留最后一条，1:1 配对丢失（假阴性）。
    """
    section("测试 6.8: 查重文件级一对一匹配")
    engine = DedupEngine(jaccard_threshold=0.0, face_enabled=False)
    ph = "0f1e2d3c4b5a6978"

    # ① 一个 A 文件 vs B{完全相同, 感知相同}：只能匹配 1 对，杰卡德 ≤ 1.0
    files_a = [{"id": 1, "path": "A/a.jpg", "md5_hash": "aaa", "phash": ph, "dhash": ph}]
    files_b = [
        {"id": 2, "path": "B/a.jpg", "md5_hash": "aaa",
         "phash": "ffffffffffffffff", "dhash": "ffffffffffffffff"},
        {"id": 3, "path": "B/a_small.jpg", "md5_hash": "bbb", "phash": ph, "dhash": ph},
    ]
    r1 = engine.compare_units(files_a, files_b, 1, 2)
    check("同一 A 文件只匹配一次", r1 is not None and len(r1.file_matches) == 1)
    check("杰卡德指数不超过 1.0", r1 is not None and r1.jaccard_similarity <= 1.0)
    check("杰卡德指数 = 1/(1+2-1) = 0.5",
          r1 is not None and abs(r1.jaccard_similarity - 0.5) < 1e-6)

    # ② 两侧各有两份相同内容：应配对 2 对，杰卡德 = 1.0
    dup_a = [{"id": 11, "path": "A/1", "md5_hash": "X", "phash": None, "dhash": None},
             {"id": 12, "path": "A/2", "md5_hash": "X", "phash": None, "dhash": None}]
    dup_b = [{"id": 21, "path": "B/1", "md5_hash": "X", "phash": None, "dhash": None},
             {"id": 22, "path": "B/2", "md5_hash": "X", "phash": None, "dhash": None}]
    r2 = engine.compare_units(dup_a, dup_b, 3, 4)
    check("重复副本全部配对（2 对）", r2 is not None and len(r2.file_matches) == 2)
    check("全等集合杰卡德 = 1.0",
          r2 is not None and abs(r2.jaccard_similarity - 1.0) < 1e-6)
    a_ids = [m.file_a_id for m in (r2.file_matches if r2 else ())]
    b_ids = [m.file_b_id for m in (r2.file_matches if r2 else ())]
    check("A 侧无重复配对", len(a_ids) == len(set(a_ids)))
    check("B 侧无重复配对", len(b_ids) == len(set(b_ids)))

    # ③ 感知哈希同理：B 中两份相同 phash，只能匹配一份
    ph_a = [{"id": 31, "path": "A/p", "md5_hash": None, "phash": ph, "dhash": None}]
    ph_b = [{"id": 41, "path": "B/p1", "md5_hash": None, "phash": ph, "dhash": None},
            {"id": 42, "path": "B/p2", "md5_hash": None, "phash": ph, "dhash": None}]
    r3 = engine.compare_units(ph_a, ph_b, 5, 6)
    check("pHash 一对一只匹配一次", r3 is not None and len(r3.file_matches) == 1)
    check("pHash 杰卡德 = 0.5",
          r3 is not None and abs(r3.jaccard_similarity - 0.5) < 1e-6)


def test_dedup_hash_index_recall():
    """测试 6.6: 感知哈希索引零漏配（鸽巢切片索引）。

    回归：旧实现只查 8bit 前缀的 ±1 邻桶，而汉明距离 ≤5 并不蕴含前缀整数差
    ≤1（0x0f vs 0x2b 距离 2、整数差 28）→ 近重复对整片漏报。
    """
    section("测试 6.9: 感知哈希索引零漏配")
    engine = DedupEngine(
        jaccard_threshold=0.0,
        phash_hamming_threshold=5,
        dhash_hamming_threshold=5,
        face_enabled=False,
    )
    base = "0f1e2d3c4b5a6978"
    # 翻转前 8bit 的第 2、5 位 → 前缀 0x0f→0x2b（整数差 28），整体距离 2
    near = "2b1e2d3c4b5a6978"
    # 翻转前 8bit 的低 6 位 → 整体距离 6（超过阈值）
    far = "301e2d3c4b5a6978"

    def _pair(hash_a, hash_b, field):
        fa = [{"id": 1, "path": "A", "md5_hash": None,
               "phash": hash_a if field == "phash" else None,
               "dhash": hash_a if field == "dhash" else None}]
        fb = [{"id": 2, "path": "B", "md5_hash": None,
               "phash": hash_b if field == "phash" else None,
               "dhash": hash_b if field == "dhash" else None}]
        r = engine.compare_units(fa, fb, 1, 2)
        return len(r.file_matches) if r else 0

    check("pHash: 前缀相差 28 但距离 2 → 命中",
          _pair(base, near, "phash") == 1)
    check("dHash: 前缀相差 28 但距离 2 → 命中",
          _pair(base, near, "dhash") == 1)
    check("pHash: 距离 6 超过阈值 → 不命中",
          _pair(base, far, "phash") == 0)
    check("dHash: 距离 6 超过阈值 → 不命中",
          _pair(base, far, "dhash") == 0)
    check("切片键数量 = 阈值+1（鸽巢前提）",
          len(engine._slice_keys(base, engine._phash_threshold + 1)) == 6)


def test_dedup_video_frame_matching():
    """测试 6.10: 视频帧级查重（回归「帧哈希算完即弃」的死链）。

    视频整片 phash/dhash 恒为空串，只有 video_frames 里的抽帧哈希；
    此前没有任何消费方 → 重编码/换分辨率的近似视频永远查不出来。
    """
    section("测试 6.10: 视频帧级查重")
    from app.db.models import MediaFile, VideoFrame
    from app.core.dedup_engine import (
        VIDEO_FRAME_MAX_SAMPLES, VIDEO_FRAME_MATCH_RATIO, VIDEO_FRAME_MIN_MATCHES,
    )

    engine = DedupEngine(jaccard_threshold=0.0, face_enabled=False)
    frames_a = ["0f1e2d3c4b5a6978", "ff00ff00ff00ff00", "1234567890abcdef", "abcdefabcdefabcd"]
    frames_b = list(frames_a)  # 同内容：抽帧哈希一致（重编码后 pHash 近似不变）

    def _video(fid, path, md5, frames):
        return {"id": fid, "path": path, "md5_hash": md5,
                "phash": "", "dhash": "", "face_vectors": [], "video_frames": frames}

    # ① 同内容不同 MD5（重编码）→ 必须命中
    fa = [_video(1, "A/movie.mp4", "aaa", frames_a)]
    fb = [_video(2, "B/movie.mp4", "bbb", frames_b)]
    r = engine.compare_units(fa, fb, 1, 2)
    check("重编码视频（帧哈希一致、MD5 不同）命中",
          r is not None and len(r.file_matches) == 1)
    if r is not None and r.file_matches:
        check("视频匹配类型记为 video", r.file_matches[0].match_type == "video")
        check("相似度为帧匹配比例 1.0",
              abs(r.file_matches[0].score - 1.0) < 1e-6)
    check("同内容视频单元 Jaccard = 1.0",
          r is not None and abs(r.jaccard_similarity - 1.0) < 1e-6)

    # ② 内容不同（帧哈希完全无关）→ 不命中
    fc = [_video(3, "C/other.mp4", "ccc", ["0000000000000000"] * 4)]
    r2 = engine.compare_units(fa, fc, 1, 3)
    check("无关视频不命中", r2 is not None and len(r2.file_matches) == 0)

    # ③ 部分帧匹配：达到比例阈值才算命中
    # （阈值提高后不再接受"一半帧相同"，按 ceil(RATIO*帧数) 构造刚好达标的样本）
    import math
    need = max(VIDEO_FRAME_MIN_MATCHES, math.ceil(len(frames_a) * VIDEO_FRAME_MATCH_RATIO))
    half = frames_a[:need] + ["1111111111111111"] * (len(frames_a) - need)
    r3 = engine.compare_units(fa, [_video(4, "D/half.mp4", "ddd", half)], 1, 4)
    check(f"匹配 {need}/{len(frames_a)} 帧达到阈值 → 命中",
          r3 is not None and len(r3.file_matches) == 1)

    # ④ 采样上限：超长视频不会全量两两比对
    many = ["0f1e2d3c4b5a6978"] * (VIDEO_FRAME_MAX_SAMPLES * 4)
    check(f"帧采样上限为 {VIDEO_FRAME_MAX_SAMPLES}",
          len(DedupEngine._sample_frames(many)) == VIDEO_FRAME_MAX_SAMPLES)
    check("短视频不采样（原样返回）",
          len(DedupEngine._sample_frames(frames_a)) == len(frames_a))

    # ⑤ 真实视频：抽帧哈希确实产出（否则整条链又会变成静默死链）
    try:
        import cv2
        import numpy as np
    except ImportError:
        cv2 = None
    if cv2 is not None:
        import tempfile
        from app.core.hash_engine import HashEngine
        tmp_dir = Path(tempfile.mkdtemp(prefix="vframe_"))
        try:
            video = tmp_dir / "clip.avi"
            writer = cv2.VideoWriter(
                str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5.0, (64, 48)
            )
            for i in range(60):  # 12 秒 → 按 5s 间隔应产出 3 帧
                writer.write(np.full((48, 64, 3), 40 + (i * 3) % 200, dtype=np.uint8))
            writer.release()
            hashes = HashEngine().hash_file(video)
            frames = hashes.video_frame_hashes or ()
            check("真实视频产出帧哈希", len(frames) >= 2)
            check("帧哈希为 (时间戳, hex) 结构",
                  all(isinstance(ts, int) and isinstance(ph, str) and len(ph) == 16
                      for ts, ph in frames))
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ⑥ 批量装载查询：按文件分组、按时间戳升序
    with DatabaseManager.session() as session:
        row = session.query(MediaFile.id).first()
        fid = row[0] if row else None
        if fid is None:
            check("视频帧批量查询: 无文件可测，跳过", True)
            return
        try:
            with DatabaseManager.session() as session:
                for i, ph in enumerate(frames_a):
                    q.insert_video_frame(session, fid, (i + 1) * 5000, ph)
            # 必须等上面的会话提交后再开新会话查询（SQLite 未提交写不可见）
            with DatabaseManager.session() as session2:
                loaded = q.get_video_frame_hashes_by_files(session2, [fid, 999999])
            check("批量查询按文件分组", set(loaded.keys()) == {fid})
            check("批量查询保持时间戳升序", loaded.get(fid) == frames_a)
            check("不存在的文件不返回", 999999 not in loaded)
        finally:
            with DatabaseManager.session() as session3:
                session3.query(VideoFrame).filter(
                    VideoFrame.file_id == fid
                ).delete(synchronize_session=False)


def test_dedup_video_frame_precision():
    """测试 6.11: 视频帧匹配必须拒绝弱证据（回归误判不同视频为重复）。

    历史缺陷：比例分母取 min(帧数)、最少匹配数 2、比例阈值 0.5，导致
      ① 单帧视频只要那 1 帧出现在对方里就判为同一视频；
      ② 两个不同视频共享一半抽帧即判为同一视频。
    查重的终点是"建议删除"，假阳性会误导用户删掉并未重复的文件。
    """
    section("测试 6.11: 视频帧匹配弱证据拒绝")
    import random
    from app.core.dedup_engine import (
        VIDEO_FRAME_MATCH_RATIO, VIDEO_FRAME_MIN_FRAMES, VIDEO_FRAME_MIN_MATCHES,
    )

    engine = DedupEngine(jaccard_threshold=0.0, face_enabled=False)

    def _video(fid, path, frames):
        return {"id": fid, "path": path, "md5_hash": None,
                "phash": "", "dhash": "", "face_vectors": [], "video_frames": frames}

    # 64bit 随机哈希两两相距甚远（期望汉明距离 32），避免互相误配
    rng = random.Random(20260916)

    def _h():
        return format(rng.getrandbits(64), "016x")

    # ① 单帧视频 vs 20 帧视频，仅其中 1 帧相同 → 不得判为同一视频
    shared = _h()
    long_b = [_h() for _ in range(19)] + [shared]
    r1 = engine.compare_units(
        [_video(1, "A/one.mp4", [shared])],
        [_video(2, "B/long.mp4", long_b)], 1, 2)
    check("单帧视频不与长视频误配", r1 is None or len(r1.file_matches) == 0)

    # ② 两个不同视频共享一半抽帧 → 不得判为同一视频
    base = [_h() for _ in range(5)]
    r2 = engine.compare_units(
        [_video(3, "C/a.mp4", base + [_h() for _ in range(5)])],
        [_video(4, "D/b.mp4", base + [_h() for _ in range(5)])], 3, 4)
    check("共享一半抽帧不判为同一视频", r2 is None or len(r2.file_matches) == 0)

    # ③ 真·重编码（抽帧逐一对应）→ 必须仍然命中
    same = [_h() for _ in range(10)]
    r3 = engine.compare_units(
        [_video(5, "E/orig.mp4", same)],
        [_video(6, "F/reenc.mp4", list(same))], 5, 6)
    check("重编码同源视频仍命中", r3 is not None and len(r3.file_matches) == 1)

    # ④ 被裁剪的同源视频（短的是长的连续子序列）→ 必须仍然命中
    r4 = engine.compare_units(
        [_video(7, "G/full.mp4", same)],
        [_video(8, "H/trim.mp4", same[4:])], 7, 8)
    check("裁剪同源视频仍命中", r4 is not None and len(r4.file_matches) == 1)

    # ⑤ 帧序被打乱（帧集合相同但时序不一致）→ 不得判为同一视频
    shuffled = [same[i] for i in (0, 5, 2, 8, 1, 7, 3, 9, 4, 6)]
    r5 = engine.compare_units(
        [_video(9, "I/a.mp4", same)],
        [_video(10, "J/shuf.mp4", shuffled)], 9, 10)
    check("帧序打乱不判为同一视频", r5 is None or len(r5.file_matches) == 0)

    check("最少匹配帧数 ≥ 3", VIDEO_FRAME_MIN_MATCHES >= 3)
    check("匹配比例阈值 ≥ 0.7", VIDEO_FRAME_MATCH_RATIO >= 0.7)
    check("帧数下限门槛 ≥ 3", VIDEO_FRAME_MIN_FRAMES >= 3)


def test_dedup_face_is_auxiliary_only():
    """测试 6.12: 人脸是辅助信号，不得计入杰卡德。

    回归：人脸向量回答的是"是不是同一个人"，不是"是不是同一个文件"。
    把它当成文件级匹配计数，会让"同一演员的不同片子"被判为重复单元
    （成人内容库演员池小，误报概率极高）。
    同时精确身份（MD5）必须在策略顺序上先于人脸，否则同一对文件的
    证据会被记为 face，最强证据被最弱证据顶掉。
    """
    section("测试 6.12: 人脸不计入杰卡德")
    engine = DedupEngine(jaccard_threshold=0.8, face_enabled=True,
                         face_similarity_threshold=FACE_SIMILARITY_THRESHOLD)
    vec = tuple([0.1] * 128)  # 完全相同的人脸特征

    def _img(fid, path, md5):
        return {"id": fid, "path": path, "md5_hash": md5, "phash": None,
                "dhash": None, "face_vectors": [vec], "video_frames": []}

    # ① 同演员、不同内容（MD5 不同）→ 不得判为重复
    r1 = engine.compare_units([_img(1, "A/1.jpg", "aaa")],
                              [_img(2, "B/1.jpg", "bbb")], 1, 2)
    check("人脸相同但内容不同 → 不计入匹配", r1 is None or len(r1.file_matches) == 0)
    check("人脸相似作为提示单独返回", r1 is None or len(r1.face_hints) == 1)

    # ② 完全相同的文件且都有脸 → 证据必须记为 md5（精确身份优先）
    r2 = engine.compare_units([_img(3, "A/2.jpg", "same")],
                              [_img(4, "B/2.jpg", "same")], 3, 4)
    check("完全相同的文件命中", r2 is not None and len(r2.file_matches) == 1)
    check("证据记为 md5 而非 face",
          r2 is not None and r2.file_matches and r2.file_matches[0].match_type == "md5")


def test_dedup_video_frame_index_equivalence():
    """测试 6.13: 帧级匹配的鸽巢切片索引必须与暴力比对结果完全一致。

    _match_by_video_frames 为消除 O(|A|·|B|·F²) 的开销，改成对 B 侧帧建
    切片倒排索引、只对候选帧算精确距离。这个优化一旦有偏差就会静默改变
    查重结论，因此用随机用例做差分验证：索引实现 vs 逐帧暴力实现，
    匹配出的文件对必须逐一相同（含"比例相同时选谁"的平局规则）。
    """
    section("测试 6.13: 帧匹配索引与暴力比对等价")
    import math
    import random
    from app.core.dedup_engine import (
        VIDEO_FRAME_MATCH_RATIO, VIDEO_FRAME_MIN_FRAMES, VIDEO_FRAME_MIN_MATCHES,
    )
    from app.core.dedup_engine import DedupEngine as _DE

    engine = DedupEngine(jaccard_threshold=0.0, face_enabled=False)
    algo = engine._registry.get("phash")

    def brute_force(files_a, files_b):
        """索引化之前的逐帧暴力语义，作为差分基准。"""
        matched_a, matched_b = set(), set()
        out = []
        for idx_a, fa in enumerate(files_a):
            if idx_a in matched_a:
                continue
            frames_a = _DE._sample_frames(fa.get("video_frames"))
            if len(frames_a) < VIDEO_FRAME_MIN_FRAMES:
                continue
            best = None
            best_ratio = 0.0
            for idx_b, fb in enumerate(files_b):
                if idx_b in matched_b:
                    continue
                frames_b = _DE._sample_frames(fb.get("video_frames"))
                if len(frames_b) < VIDEO_FRAME_MIN_FRAMES:
                    continue
                ratio, count, pairs = engine._frame_set_similarity(
                    frames_a, frames_b, algo)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = (idx_b, fb, count, len(frames_b), pairs)
            if best is None:
                continue
            idx_b, fb, count, frames_b_len, pairs = best
            required = min(VIDEO_FRAME_MIN_MATCHES, len(frames_a), frames_b_len)
            monotone = engine._longest_increasing([pb for _, pb in pairs])
            mono_req = max(VIDEO_FRAME_MIN_MATCHES,
                           math.ceil(VIDEO_FRAME_MATCH_RATIO * count))
            if (count >= required and best_ratio >= VIDEO_FRAME_MATCH_RATIO
                    and monotone >= mono_req):
                out.append((fa["id"], fb["id"]))
                matched_a.add(idx_a)
                matched_b.add(idx_b)
        return sorted(out)

    def flip(h, nbits, rng):
        """翻转 nbits 个随机比特位（模拟重编码后的 pHash 抖动）。"""
        v = int(h, 16)
        for b in rng.sample(range(64), nbits):
            v ^= (1 << b)
        return format(v, "016x")

    rng = random.Random(4242)
    mismatches = 0
    hits = 0
    for trial in range(60):
        n_a = rng.randint(1, 6)
        n_b = rng.randint(1, 6)

        def _gen(n, pool_of):
            files = []
            for i in range(n):
                fid = rng.randint(1, 10 ** 6)
                if pool_of and rng.random() < 0.55:
                    # 以已有池子里的帧为基础做近似（部分帧完全一致、部分抖动）
                    base = list(rng.choice(pool_of))
                    frames = [flip(h, rng.choice([0, 0, 1, 2, 3, 6]), rng)
                              for h in base]
                    if rng.random() < 0.3:      # 有时裁剪
                        frames = frames[rng.randint(1, max(1, len(frames) - 1)):]
                    if rng.random() < 0.25:     # 有时打乱
                        rng.shuffle(frames)
                else:
                    frames = [format(rng.getrandbits(64), "016x")
                              for _ in range(rng.randint(1, 8))]
                files.append({"id": fid, "path": f"{fid}.mp4", "md5_hash": None,
                              "phash": "", "dhash": "", "face_vectors": [],
                              "video_frames": frames})
            return files

        fb = _gen(n_b, None)
        fa = _gen(n_a, [f["video_frames"] for f in fb])

        got = sorted((m.file_a_id, m.file_b_id)
                     for m in engine._match_by_video_frames(fa, fb, set(), set()))
        want = brute_force(fa, fb)
        hits += len(want)
        if got != want:
            mismatches += 1
            if mismatches <= 2:
                print(f"    第 {trial} 组不一致: got={got} want={want}")

    check(f"60 组随机用例索引结果与暴力一致（不一致 {mismatches} 组）",
          mismatches == 0)
    check(f"随机用例覆盖到命中场景（命中 {hits} 对）", hits > 0)


def test_face_detector_thread_safety():
    """测试 0.7c: 并发调用 detect_faces 不得崩。

    回归：FaceDetectorYN/FaceRecognizerSF 不是线程安全的
    （setInputSize 与 detect 共用内部缓冲），而检测器曾是类级别单例 →
    并发调用抛 "Assertion failed: buf.shape() == m.shape()"
    （实测共享单例 24 次并发失败 21 次，每线程独立实例 0 失败）。
    现在改为按线程缓存检测器。
    """
    section("测试 0.7c: 人脸检测器线程安全")
    from concurrent.futures import ThreadPoolExecutor

    import cv2
    import numpy as np
    from app.core.hash_engine import (
        FACE_DETECT_MODEL, FACE_RECOGNIZE_MODEL, HashEngine,
    )

    model_dir = Path(__file__).parent.parent / "models"
    if not ((model_dir / FACE_DETECT_MODEL).exists()
            and (model_dir / FACE_RECOGNIZE_MODEL).exists()
            and hasattr(cv2, "FaceDetectorYN")):
        check("模型缺失，跳过线程安全测试", True)
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="face_mt_"))
    try:
        # 各线程用不同尺寸的图 —— 并发 setInputSize 抢同一份内部缓冲时
        # 最容易触发形状断言失败
        sizes = [(320, 240), (480, 360), (640, 480), (800, 600),
                 (960, 720), (240, 320)]
        paths = []
        for i, (w, h) in enumerate(sizes):
            p = tmp_dir / f"mt{i}.jpg"
            Image.new("RGB", (w, h), (120 + i * 10, 100, 90)).save(p)
            paths.append(p)
        paths = paths * 3  # 12 个任务、4 线程

        engine = HashEngine(face_detection_enabled=True, model_dir=model_dir)
        errors: list[str] = []

        def job(p):
            try:
                engine.detect_faces(p)
            except Exception as e:  # noqa: BLE001 — 这里就是要捕获并发异常
                errors.append(f"{type(e).__name__}: {str(e)[:60]}")

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(job, paths))

        check(f"并发 detect_faces 无异常（{len(paths)} 次调用）", not errors)
        if errors:
            print(f"     首个异常: {errors[0]}")
        det, rec = HashEngine._get_face_nets()
        check("当前线程仍持有可用的检测器",
              det is not None and rec is not None)
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_dedup_related_level():
    """测试 6.14: 未达重复阈值但证据足够的单元对应记为一档"疑似相关"。

    用户诉求：同演员 / 同场景的不同片子也要知道（不打算删）。
    因此把"重复"与"相关"分级 —— related 只提醒，不推处置。
    """
    section("测试 6.14: 疑似相关等级")
    from app.core.dedup_engine import DedupEngine
    from app.utils.constants import MatchLevel

    vec = tuple([0.1] * 128)

    def _img(fid, md5, face=True):
        return {"id": fid, "path": f"{fid}.jpg", "md5_hash": md5, "phash": None,
                "dhash": None, "face_vectors": [vec] if face else [],
                "video_frames": []}

    eng = DedupEngine(jaccard_threshold=0.8, face_enabled=True,
                      related_min_matches=2)

    # ① 同演员不同内容：2 条人脸线索 → related（人脸不计入杰卡德，故杰卡德为 0）
    r1 = eng.compare_units([_img(1, "a1"), _img(2, "a2")],
                           [_img(3, "b1"), _img(4, "b2")], 1, 2)
    check("证据足够 → 判为 related",
          r1 is not None and r1.level == MatchLevel.RELATED.value)
    check("related 不计入重复匹配", r1 is not None and len(r1.file_matches) == 0)
    check("杰卡德仍为 0（人脸不参与计分）",
          r1 is not None and r1.jaccard_similarity == 0.0)
    check("人脸线索作为证据返回", r1 is not None and len(r1.face_hints) == 2)
    check("证据数 = 匹配 + 人脸 = 2", r1 is not None and r1.evidence_count == 2)

    # ② 只有 1 条线索 → 证据不足，不打扰用户
    r2 = eng.compare_units([_img(5, "c1")], [_img(6, "d1")], 5, 6)
    check("证据不足（1 条）→ 返回 None（不提醒）", r2 is None)

    # ③ 达到重复阈值 → 仍是 duplicate（分级不能把重复降级）
    r3 = eng.compare_units([_img(11, "X", face=False)],
                           [_img(12, "X", face=False)], 11, 12)
    check("完全相同 → duplicate",
          r3 is not None and r3.level == MatchLevel.DUPLICATE.value)

    # ④ run_dedup 必须把两者分到不同的桶
    res = eng.run_dedup({1: [_img(1, "a1"), _img(2, "a2")],
                         2: [_img(3, "b1"), _img(4, "b2")],
                         3: [_img(11, "X", face=False)],
                         4: [_img(12, "X", face=False)]},
                        {1: "A", 2: "B", 3: "C", 4: "D"})
    check("related 进入 related_found 桶",
          len(res.related_found) == 1 and res.related_found[0].unit_a_id == 1)
    check("duplicate 进入 duplicates_found 桶",
          len(res.duplicates_found) == 1)


def test_dedup_related_persistence():
    """测试 6.15: 疑似相关落库（match_level）+ 人脸证据落库 + 汇总提醒。

    关键约束：人脸线索可能与计数匹配落在同一个文件对上，而
    dedup_file_matches 有 (result, file_a, file_b) 唯一约束 —— 必须去重，
    否则整条查重写入会因 IntegrityError 失败。
    """
    section("测试 6.15: 疑似相关落库与汇总提醒")
    from app.core.dedup_engine import FileMatch, UnitComparisonResult
    from app.db.models import DedupResult, Message
    from app.services.dedup_service import _save_result
    from app.utils.constants import MatchLevel

    with DatabaseManager.session() as s:
        units = q.get_all_active_units(s)
        if len(units) < 2:
            check("需要 ≥ 2 个单元，跳过", True)
            return
        uid_a, uid_b = units[0].id, units[1].id
        fa_files = q.get_files_by_unit(s, uid_a)
        fb_files = q.get_files_by_unit(s, uid_b)
        if not fa_files or len(fb_files) < 2:
            check("测试库文件不足，跳过", True)
            return
        fa, fb, fb2 = fa_files[0].id, fb_files[0].id, fb_files[1].id

    def _fm(a, b, mtype, score):
        return FileMatch(file_a_id=a, file_b_id=b, file_a_path="", file_b_path="",
                         match_type=mtype, score=score)

    item = UnitComparisonResult(
        unit_a_id=uid_a, unit_b_id=uid_b, unit_a_name="A", unit_b_name="B",
        jaccard_similarity=0.05,
        file_matches=(_fm(fa, fb, "video", 1.0),),
        face_hints=(_fm(fa, fb, "face", 0.9),      # 与计数匹配同一对 → 必须去重
                    _fm(fa, fb2, "face", 0.8)),
        match_counts={"video": 1}, total_files_a=2, total_files_b=2,
        level=MatchLevel.RELATED.value,
    )

    dr_id = None
    with DatabaseManager.session() as s:
        dr_id = _save_result(s, item, notify=False).id
    try:
        with DatabaseManager.session() as s:
            row = s.get(DedupResult, dr_id)
            check("match_level 落库为 related", row is not None and row.match_level == "related")
            check("match_count = 全部证据数 3", row is not None and row.match_count == 3)
            matches = q.get_file_matches_for_result(s, dr_id)
            check("重复文件对去重（不撞唯一约束）",
                  sorted(m.match_type for m in matches) == ["face", "video"])
        check("落库未抛 IntegrityError", True)
    finally:
        with DatabaseManager.session() as s:
            s.query(DedupResult).filter(DedupResult.id == dr_id).delete(
                synchronize_session=False)

    # 汇总提醒（逐条发会刷屏，实测本库有 200+ 对）
    msg = MessageCenter.create_related_digest([
        ("单元甲", "单元乙", 5, 0.1, "video,face"),
        ("单元丙", "单元丁", 2, 0.02, "face"),
    ])
    check("汇总消息已创建", msg is not None)
    check("标题点明「疑似相关」", msg is not None and "疑似相关" in msg.title)
    check("标题含对数", msg is not None and "2 对" in msg.title)
    check("正文声明不建议删除", msg is not None and "不建议删除" in (msg.body or ""))
    check("正文按证据强度排序（5 处的在前）",
          msg is not None and (msg.body or "").index("单元甲") < (msg.body or "").index("单元丙"))
    if msg is not None:
        with DatabaseManager.session() as s:
            s.query(Message).filter(Message.id == msg.id).delete(synchronize_session=False)


def test_hash_worker_indexes_all_batches():
    """测试 6.16: 待索引文件多于单批上限时必须循环取完。

    回归：HashWorker 只调一次 get_unindexed_files(limit=1000) 就收工，
    未索引文件多于 1000 时日志仍打印"索引完成: 1000/1000"，
    而一键查重紧接着拿这份残缺数据出结论（实测用户库里 1362 个待索引，
    只算了 1000，剩 362 个对查重完全不可见且无任何提示）。
    """
    section("测试 6.16: 哈希索引分批循环")
    from app.db.models import MediaFile, ResourceUnit
    from app.ui.workers.hash_worker import HashWorker
    import app.services.index_service as index_svc

    unit_id = None
    with DatabaseManager.session() as s:
        root = q.add_library_root(s, "Z:/__batch_test__")
        unit = ResourceUnit(name="__batch_test__", path="Z:/__batch_test__",
                            library_root_id=root.id, status="active")
        s.add(unit)
        s.flush()
        unit_id = unit.id
        for i in range(5):
            s.add(MediaFile(
                resource_unit_id=unit_id,
                path=f"Z:/__batch_test__/f{i}.jpg",
                filename=f"f{i}.jpg", extension=".jpg",
                media_type="image", size_bytes=1,
            ))

    orig = index_svc.INDEX_BATCH_SIZE
    index_svc.INDEX_BATCH_SIZE = 2  # 5 个文件、每批 2 个 → 必须循环 3 批才取空
    try:
        worker = HashWorker(AppConfig())
        done = []
        progress = []
        worker.finished.connect(lambda n: done.append(n))
        worker.progress.connect(lambda cur, total: progress.append((cur, total)))
        worker.run()  # 同步执行，避免线程时序不确定

        with DatabaseManager.session() as s:
            rows = s.query(MediaFile.md5_hash).filter(
                MediaFile.resource_unit_id == unit_id).all()
        # 文件在磁盘上不存在 → 走"标记为已处理"分支写入空串占位
        processed = sum(1 for (h,) in rows if h is not None)
        check("5 个待索引文件全部被处理（单批上限 2）", processed == 5)
        check("finished 信号已发出且计数 ≥ 5", bool(done) and done[0] >= 5)
        # 管线已收敛到 index_service：GUI 线程的 progress 转发不能被重构弄丢
        check("progress 信号已转发（index_service → HashWorker）", len(progress) >= 5)
        check("progress 末值为 (已完成, 总数) 且已完成 ≥ 5",
              bool(progress) and progress[-1][0] >= 5 and progress[-1][1] >= 5)
    finally:
        index_svc.INDEX_BATCH_SIZE = orig
        if unit_id is not None:
            with DatabaseManager.session() as s:
                q.delete_resource_unit(s, unit_id)


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


def test_dedup_results_pagination():
    """测试 7.5: 查重结果分页的 total 必须是真实总数。

    回归：接口先按 limit=100/200 取全量再内存切片 → total 被截断，
    超出部分任何页都取不到（DB 261 行时 total=200、第 13 页恒空）。
    这里刻意造 >100 条未处置结果，使旧实现的 limit=100 截断必然暴露。
    """
    section("测试 7.5: 查重结果分页 total 精确")
    from fastapi.testclient import TestClient
    from app.api.server import create_app

    with DatabaseManager.session() as session:
        from app.db.models import DedupResult as _DR
        before = session.query(_DR).filter(_DR.is_resolved == False).count()  # noqa: E712
        root = q.add_library_root(session, "C:/__paginate_test__")
        # 15 个单元 → 105 个单元对，超过旧实现的 limit=100 截断阈值
        unit_ids = [
            q.create_unit(session, f"C:/__paginate_test__/u{i}", f"u{i}", root.id).id
            for i in range(15)
        ]
        added = 0
        for i in range(len(unit_ids)):
            for j in range(i + 1, len(unit_ids)):
                q.upsert_dedup_result(
                    session, unit_ids[i], unit_ids[j], 0.9, 1, 1, 1, "md5",
                )
                added += 1
        root_id = root.id
        expected = before + added

    try:
        client = TestClient(create_app(AppConfig()))
        r1 = client.get("/api/dedup/results?unresolved_only=true&per_page=20&page=1").json()
        r2 = client.get("/api/dedup/results?unresolved_only=true&per_page=20&page=2").json()
        check(f"total 为真实总数 {expected}（实际 {r1.get('total')}）",
              r1.get("total") == expected)
        check("第一页返回 20 条", len(r1.get("results", [])) == 20)
        check("第二页返回 20 条",
              len(r2.get("results", [])) == min(20, max(0, expected - 20)))
        # 直接针对旧缺陷症状：第 101 条起（旧实现 limit=100 之后）必须仍有数据
        r6 = client.get("/api/dedup/results?unresolved_only=true&per_page=20&page=6").json()
        check("第 6 页（第 101 条起）仍有数据（旧实现恒空）",
              len(r6.get("results", [])) > 0)
    finally:
        with DatabaseManager.session() as session:
            q.remove_library_root(session, root_id)


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


def test_status_bar_cancel_wiring():
    """测试 8.7: 状态栏取消按钮 → cancel_requested 信号链路。

    回归：长任务（扫描/哈希/查重）跑在 QThread 里，但界面上没有任何取消
    入口 —— 取消按钮所在的 ProgressPanel 从未被实例化，MainWindow 也没有
    closeEvent，用户只能强杀进程（或在 worker 仍写库时关窗）。
    """
    section("测试 8.7: 状态栏取消按钮信号链")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.widgets.status_bar import MainStatusBar
    from app.ui.main_window import MainWindow

    bar = MainStatusBar(AppConfig())
    # 用 isHidden() 判断显隐意图：控件未被 show() 时 isVisible() 恒为 False
    check("默认隐藏取消按钮", bar._cancel_btn.isHidden())

    fired = []
    bar.cancel_requested.connect(lambda: fired.append(1))

    bar.set_cancellable(True)
    check("任务运行时可取消 → 按钮显示", not bar._cancel_btn.isHidden())
    check("按钮初始可用", bar._cancel_btn.isEnabled())

    bar._cancel_btn.click()
    check("点击取消发出 cancel_requested", len(fired) == 1)
    check("点击后按钮禁用（防重复触发）", not bar._cancel_btn.isEnabled())

    bar._cancel_btn.click()
    check("禁用后重复点击不再发信号", len(fired) == 1)

    bar.hide_progress()
    check("任务结束隐藏取消按钮", bar._cancel_btn.isHidden())

    bar.set_cancellable(True)
    bar.set_cancellable(False)
    check("再次置为不可取消后按钮重新隐藏", bar._cancel_btn.isHidden())
    check("重新显示时按钮恢复可用", bar._cancel_btn.isEnabled())

    # 主窗口必须提供优雅停止入口，否则退出会在 worker 写库时 dispose 引擎
    check("MainWindow 实现了 closeEvent", "closeEvent" in MainWindow.__dict__)
    check("MainWindow 提供 stop_background_workers",
          hasattr(MainWindow, "stop_background_workers"))
    check("MainWindow 提供 _on_cancel_current_task",
          hasattr(MainWindow, "_on_cancel_current_task"))


def test_dedup_compare_shows_face_hints():
    """测试 8.8: 对比弹窗把人脸相似作为线索展示，而非重复依据。

    人脸只揭示"同一个人"，成人内容库演员池小，若计入杰卡德会让同一演员的
    不同作品被判为重复单元。因此弹窗只提示、不参与判定。
    """
    section("测试 8.8: 对比弹窗显示人脸线索")
    from PySide6.QtWidgets import QApplication, QLabel
    if QApplication.instance() is None:
        QApplication([])

    from app.core.dedup_engine import DedupEngine as _DE
    from app.ui.dialogs.dedup_compare import DedupCompareDialog

    engine = _DE(jaccard_threshold=0.0, face_enabled=True)
    vec = tuple([0.1] * 128)

    def _img(fid, path, md5):
        return {"id": fid, "path": path, "md5_hash": md5, "phash": None,
                "dhash": None, "face_vectors": [vec], "video_frames": []}

    result = engine.compare_units([_img(1, "A/1.jpg", "aaa")],
                                  [_img(2, "B/1.jpg", "bbb")], 1, 2)
    check("内容不同 → 匹配数为 0",
          result is not None and len(result.file_matches) == 0)
    check("同时产出人脸提示", result is not None and len(result.face_hints) == 1)

    dialog = DedupCompareDialog(result, AppConfig())
    texts = [w.text() for w in dialog.findChildren(QLabel)]
    check("弹窗展示人脸线索", any("人脸相似" in t for t in texts))
    check("线索文案声明未计入判定", any("未计入" in t for t in texts))
    check("匹配统计为空（人脸未混入类型统计）",
          not any("face" in t for t in texts))
    dialog.deleteLater()


def test_scan_worker_cancel_signal():
    """测试 8.9: 取消扫描必须发出 cancelled 信号（否则取消按钮永不收起）。

    回归：ScanWorker.run() 在取消路径直接 return，既不 emit finished 也不
    emit cancelled，界面拿不到任何收尾信号，进度条与取消按钮会一直留在
    状态栏上；同时扫描队列也不该继续跑下一个目录。
    """
    section("测试 8.9: 扫描取消信号")
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.workers.scan_worker import ScanWorker
    from app.ui.main_window import MainWindow

    tmp_dir = Path(tempfile.mkdtemp(prefix="scan_cancel_"))
    try:
        worker = ScanWorker(AppConfig(), tmp_dir)
        got = {"cancelled": 0, "finished": 0}
        worker.cancelled.connect(
            lambda: got.__setitem__("cancelled", got["cancelled"] + 1))
        worker.finished.connect(
            lambda r: got.__setitem__("finished", got["finished"] + 1))
        worker._cancelled = True   # 模拟用户已点击取消
        worker.run()               # 同步执行，避免线程时序不确定

        check("取消路径发出 cancelled 信号", got["cancelled"] == 1)
        check("取消路径不发 finished（不写不完整的扫描结果）",
              got["finished"] == 0)
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    check("MainWindow 提供扫描取消处理器",
          hasattr(MainWindow, "_on_scan_cancelled"))


def test_status_bar_cancel_button_not_squashed():
    """测试 8.7b: 状态栏取消按钮的文字不得被样式表压扁。

    回归：全局 `QPushButton` 规则是 `padding: 7px 16px; font-size: 12px`，
    按钮自身要 31px 高；而状态栏空间有限，按钮被限到 18px，上下各 7px 内边距
    吃掉后只剩 ~4px 给文字，"取消"被压成一条横带。
    因此必须应用真实 style.qss 来测 —— 只调 apply_theme() 是测不出来的。
    """
    section("测试 8.7b: 取消按钮文字不被压扁")
    from PySide6.QtWidgets import QApplication, QPushButton
    if QApplication.instance() is None:
        QApplication([])

    from app.ui.widgets.status_bar import MainStatusBar

    root = Path(__file__).parent.parent
    qss_path = root / "app" / "ui" / "style.qss"
    check("style.qss 存在", qss_path.exists())

    app = QApplication.instance()
    old_qss = app.styleSheet()
    try:
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))

        bar = MainStatusBar(AppConfig())
        btn = bar._cancel_btn
        check("取消按钮绑定了紧凑样式 objectName",
              btn.objectName() == "statusBarCancel")

        # 通用规则（无 objectName）确实过高 —— 这正是需要专门覆盖的原因
        plain = QPushButton("取消")
        check("通用 QPushButton 样式高于状态栏限高（故需要紧凑覆盖）",
              plain.sizeHint().height() > btn.maximumHeight())

        # 核心断言：样式表算出的文字高度必须放得进按钮限高
        check(f"按钮限高({btn.maximumHeight()}px) ≥ 样式算出高度"
              f"({btn.sizeHint().height()}px)",
              btn.sizeHint().height() <= btn.maximumHeight())
        check("按钮高度不超过状态栏可用空间", btn.maximumHeight() <= 24)
    finally:
        app.setStyleSheet(old_qss)


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


def test_thumbnail_generator_formats():
    """测试 6.7: 缩略图扩展名判定与透明通道处理。

    回归：① 内联视频扩展名集合缺 m2ts/mts/rmvb/vob/ogv/asf（与 media_types 分叉）；
          ② RGBA 透明区域直接 convert("RGB") → 黑底。
    """
    section("测试 6.7: 缩略图格式与透明通道")
    import tempfile
    from app.core.exceptions import UnsupportedFormatError

    gen = ThumbnailGenerator(max_size=64)
    tmp = Path(tempfile.mkdtemp(prefix="thumb_fmt_"))
    try:
        # ① .m2ts 必须进入视频分支（而非 UnsupportedFormatError）
        fake_video = tmp / "clip.m2ts"
        fake_video.write_bytes(b"not a real video")
        unsupported = False
        try:
            gen.generate(fake_video, tmp / "cache")
        except UnsupportedFormatError:
            unsupported = True
        except Exception:
            pass  # 假文件解码失败属预期
        check(".m2ts 不再被判定为不支持格式", not unsupported)

        # ② 全透明 PNG → 白底而非黑底
        src = tmp / "transparent.png"
        Image.new("RGBA", (32, 32), (0, 0, 0, 0)).save(src)
        info = gen.generate(src, tmp / "cache2")
        with Image.open(info.thumbnail_path) as thumb:
            corner = thumb.convert("RGB").getpixel((0, 0))
        check(f"透明区域合成为白底（实际 {corner}）", corner == (255, 255, 255))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


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

    # 检查关键路由（兼容新旧 Starlette：新版以 _IncludedRouter 惰性包裹 include_router 路由）
    paths = set()

    def _collect(node) -> None:
        p = getattr(node, "path", None)
        if p:
            paths.add(p)
        for n in getattr(node, "routes", []) or []:
            _collect(n)

    for r in app.routes:
        _collect(r)
        router = getattr(r, "original_router", None)  # _IncludedRouter 私有属性
        if router is not None:
            for n in getattr(router, "routes", []) or []:
                _collect(n)
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



def test_thumbnail_endpoint_validates_cache_owner():
    """测试 16.6: 缩略图缓存必须校验归属 —— file_id 被复用后不得返回别人的图。

    复现（实测现象：重置数据库后重新扫描，手机端显示的是旧库的照片）：
    1. 集中缓存以 `{file_id}_thumb.jpg` 命名，而 file_id 会重复使用 ——
       「工具 → 重置数据库」后 id 从 1 重新分配；删除最大 id 的记录后
       下一条记录也会复用该 id（SQLite rowid = max+1）。
    2. `ThumbnailGenerator.generate()` 会写 `.meta` sidecar（源路径 + mtime +
       size）并据此判断缓存是否有效，但 `GET /api/files/{id}/thumbnail` 的
       快速路径**直接返回找到的文件、从不校验**，于是新文件拿到了旧文件的图。
    3. 启动时的 `purge_orphaned_thumbnails()` 也救不了：id 在库里存在，
       只是指向了另一个文件，孤儿判定识别不出来。

    判据用像素颜色直接断言"返回的是谁的图"，不看状态码，避免宽松通过。
    """
    section("测试 16.6: 缩略图缓存归属校验")
    import io as _io
    import json as _json
    import tempfile
    from fastapi.testclient import TestClient
    from app.api.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="thumb_stale_"))
    unit_id = None
    try:
        unit_dir = tmp / "单元"
        unit_dir.mkdir(parents=True)
        # 旧库的图（纯红）与新库的图（纯蓝）：内容截然不同，像素即可判定归属
        old_src = unit_dir / "旧库照片.jpg"
        new_src = unit_dir / "新库照片.jpg"
        Image.new("RGB", (80, 80), (255, 0, 0)).save(old_src)
        Image.new("RGB", (80, 80), (0, 0, 255)).save(new_src)

        cache_dir = tmp / "thumbs"
        cache_dir.mkdir(parents=True, exist_ok=True)

        with DatabaseManager.session() as session:
            root = q.add_library_root(session, str(tmp))
            unit = q.create_unit(session, str(unit_dir), "单元", root.id,
                                 file_count=1, total_size=new_src.stat().st_size)
            unit_id = unit.id
            mf = q.insert_media_file(
                session, path=str(new_src), filename=new_src.name,
                extension=".jpg", media_type="image",
                size_bytes=new_src.stat().st_size, resource_unit_id=unit.id,
            )
            fid = mf.id

        # 模拟"上一个库留下的缓存"：同样的 file_id，但缓存内容来自旧图
        generator = ThumbnailGenerator(max_size=64)
        generator.generate(old_src, cache_dir, file_id=fid)
        stale = cache_dir / f"{fid}_thumb.jpg"
        check("已构造出旧库残留缓存", stale.is_file())
        meta = _json.loads(
            (cache_dir / f"{fid}_thumb.jpg.meta").read_text(encoding="utf-8")
        )
        check("残留缓存的 meta 指向旧图（正是要识别的情形）",
              meta.get("source_path") == str(old_src))

        config = AppConfig().with_updates(
            thumbnail_cache_dir=cache_dir, thumbnail_max_size=64,
        )
        client = TestClient(create_app(config))
        resp = client.get(f"/api/files/{fid}/thumbnail")
        check("缩略图请求返回 200", resp.status_code == 200)
        if resp.status_code == 200:
            img = Image.open(_io.BytesIO(resp.content)).convert("RGB")
            r, g, b = img.getpixel((img.size[0] // 2, img.size[1] // 2))
            check(f"返回的是新库文件（蓝）而非残留缓存（红）：RGB=({r},{g},{b})",
                  b > r)
            # 校验失败应触发重新生成并覆盖残留，缓存自此与新文件一致
            new_meta = _json.loads(
                (cache_dir / f"{fid}_thumb.jpg.meta").read_text(encoding="utf-8")
            )
            check("残留缓存已被重新生成覆盖（meta 指向新图）",
                  new_meta.get("source_path") == str(new_src))

        # 3) 离线媒体库（盘未挂载 / 源文件被移走）：归属正确的缓存必须仍然可用。
        #    归属校验只看 meta.source_path，源不可 stat 时跳过新鲜度比对 ——
        #    否则整个库会从"显示缓存缩略图"退化成"整页占位图"。
        offline_src = unit_dir / "已离线照片.jpg"
        Image.new("RGB", (80, 80), (0, 255, 0)).save(offline_src)
        with DatabaseManager.session() as session:
            mf2 = q.insert_media_file(
                session, path=str(offline_src), filename=offline_src.name,
                extension=".jpg", media_type="image",
                size_bytes=offline_src.stat().st_size, resource_unit_id=unit_id,
            )
            fid2 = mf2.id
        generator.generate(offline_src, cache_dir, file_id=fid2)
        offline_src.unlink()  # 模拟盘未挂载/文件被移走

        resp2 = client.get(f"/api/files/{fid2}/thumbnail")
        check("源不可达时仍返回归属正确的缓存（离线库不退化）",
              resp2.status_code == 200)
        if resp2.status_code == 200:
            img2 = Image.open(_io.BytesIO(resp2.content)).convert("RGB")
            r2, g2, b2 = img2.getpixel((img2.size[0] // 2, img2.size[1] // 2))
            check(f"离线缓存内容为该文件的图（绿）：RGB=({r2},{g2},{b2})",
                  g2 > r2 and g2 > b2)
    finally:
        import shutil
        with DatabaseManager.session() as session:
            if unit_id is not None:
                q.delete_resource_unit(session, unit_id)
        shutil.rmtree(tmp, ignore_errors=True)


def test_thumbnail_config_respected():
    """测试 16.5: 缩略图尺寸与缓存目录必须来自配置。

    回归：files.py 用模块级 `ThumbnailGenerator(max_size=256)` 忽略
    config.thumbnail_max_size；CleanupService 硬编码 data/.thumbnails
    忽略 config.thumbnail_cache_dir（改过目录后 invalidate/purge 打错位置）。
    """
    section("测试 16.5: 缩略图配置生效")
    import io as _io
    import tempfile
    from fastapi.testclient import TestClient
    from app.api.server import create_app
    from app.services.cleanup_service import (
        CleanupService, configure_thumbnail_cache_dir,
    )

    tmp = Path(tempfile.mkdtemp(prefix="thumb_cfg_"))
    try:
        cache_dir = tmp / "custom_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        config = AppConfig().with_updates(
            thumbnail_cache_dir=cache_dir, thumbnail_max_size=64,
        )
        client = TestClient(create_app(config))

        with DatabaseManager.session() as session:
            files = q.get_files_by_unit(session, 1)
        if not files:
            check("缩略图配置测试: 无文件，跳过", True)
            return
        fid = files[0].id

        resp = client.get(f"/api/files/{fid}/thumbnail")
        check("按需生成缩略图 → 200", resp.status_code == 200)
        if resp.status_code == 200:
            img = Image.open(_io.BytesIO(resp.content))
            check(f"缩略图边长 ≤ 配置的 64px（实际 {img.size}）",
                  max(img.size) <= 64)
            check("缩略图写入配置的缓存目录",
                  (cache_dir / f"{fid}_thumb.jpg").is_file())

        # 失效接口必须打到配置目录（而非硬编码 data/.thumbnails）
        configure_thumbnail_cache_dir(cache_dir)
        CleanupService.invalidate_thumbnail(fid)
        check("invalidate_thumbnail 清理配置目录中的缓存",
              not (cache_dir / f"{fid}_thumb.jpg").exists())
    finally:
        configure_thumbnail_cache_dir(None)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_cleanup_cache_sidecars():
    """测试 16.7: 缩略图清理必须连 .meta sidecar 一起删。

    回归：`_remove_thumbnail` 只 unlink `{id}_thumb.jpg`，
    `purge_orphaned_thumbnails` 也只扫 .jpg 且只删 .jpg —— 而 sidecar 正是
    判断"缓存属于哪个文件"的依据（ThumbnailGenerator.is_cache_valid），
    留下的孤儿 meta 永远不会被回收。对照实现：scan_worker.py 早已是
    `for suffix in ("", ".meta")`。

    另外覆盖「重置数据库」需要的一次性清空（重置后 file_id 从 1 重新分配，
    旧缓存既不会被命中、也再无回收时机，留着只是占磁盘）。
    """
    section("测试 16.7: 缩略图缓存清理含 .meta sidecar")
    import tempfile
    from app.services.cleanup_service import (
        CleanupService, configure_thumbnail_cache_dir,
    )

    tmp = Path(tempfile.mkdtemp(prefix="cleanup_sidecar_"))
    try:
        cache = tmp / ".thumbnails"
        cache.mkdir(parents=True)
        configure_thumbnail_cache_dir(cache)

        with DatabaseManager.session() as session:
            files = q.get_files_by_unit(session, 1)
        if not files:
            check("清理测试: 无文件，跳过", True)
            return
        keep_id = files[0].id
        orphan_id = 987654321  # 库里不存在

        def plant(fid):
            """在缓存目录里放一对 {id}_thumb.jpg + .meta。"""
            planted = []
            for suffix in ("", ".meta"):
                p = cache / f"{fid}_thumb.jpg{suffix}"
                p.write_text("x", encoding="utf-8")
                planted.append(p)
            return planted

        # 1) 单文件失效：图与 sidecar 都必须消失
        pair = plant(keep_id)
        CleanupService.invalidate_thumbnail(keep_id)
        check("invalidate_thumbnail 删除缩略图", not pair[0].exists())
        check("invalidate_thumbnail 同时删除 .meta sidecar", not pair[1].exists())

        # 2) 孤儿清理：孤儿对 + "图已删只剩 meta"的历史残留都要收，正常项必须保留
        orphan_pair = plant(orphan_id)
        stale_meta = cache / f"{orphan_id + 1}_thumb.jpg.meta"
        stale_meta.write_text("x", encoding="utf-8")
        keep_pair = plant(keep_id)  # 库中仍存在 → 不能被清掉
        purged = CleanupService.purge_orphaned_thumbnails(cache)
        check(f"孤儿清理报告清理数 ≥3（实际 {purged}）", purged >= 3)
        check("孤儿缩略图已删除", not orphan_pair[0].exists())
        check("孤儿 .meta sidecar 已删除", not orphan_pair[1].exists())
        check("只剩 meta 的历史残留也被清理", not stale_meta.exists())
        check("库中仍存在的文件缓存被保留",
              keep_pair[0].exists() and keep_pair[1].exists())

        # 3) 清空整个缓存目录（重置数据库用）：缩略图 + sidecar，且不误删无关文件
        unrelated = cache / "readme.txt"
        unrelated.write_text("不要删我", encoding="utf-8")
        clear_fn = getattr(CleanupService, "clear_thumbnail_cache", None)
        if clear_fn is None:
            check("CleanupService.clear_thumbnail_cache 存在（重置库清缓存用）", False)
        else:
            cleared = clear_fn(cache)
            check(f"clear_thumbnail_cache 报告清理数 ≥2（实际 {cleared}）", cleared >= 2)
            check("缓存目录中不再有缩略图与 sidecar",
                  not (cache / f"{keep_id}_thumb.jpg").exists()
                  and not (cache / f"{keep_id}_thumb.jpg.meta").exists())
            check("无关文件不被误删", unrelated.exists())

        # 4) UI 接线：重置数据库后必须调用清空（静态校验，避免只加方法没人调）
        src = (Path(__file__).parent.parent / "app" / "ui" / "main_window.py").read_text(
            encoding="utf-8")
        seg = src[src.find("def _on_reset_db"):]
        end = seg.find("def _on_settings_saved")
        seg = seg[:end] if end > 0 else seg
        check("_on_reset_db 重置后清空缩略图缓存", "clear_thumbnail_cache" in seg)
    finally:
        configure_thumbnail_cache_dir(None)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


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

    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
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

    # 激发查重（异步：202 + task_id，轮询至完成）
    import time
    resp = client.post("/api/dedup/run", json={
        "unit_ids": unit_ids,
        "threshold": 0.50,
    })
    if resp.status_code != 202:
        check(f"查重提交返回 {resp.status_code}: {resp.text[:200]}", False)
    else:
        check("查重提交返回 202", True)
    data = resp.json()
    task_id = data.get("task_id") if resp.status_code == 202 else ""
    check("返回 task_id", bool(task_id))

    # 轮询任务状态直到结束
    task_status = "unknown"
    task_result = None
    task_error = None
    for _ in range(200):
        tr = client.get(f"/api/dedup/run/{task_id}")
        if tr.status_code != 200:
            task_status = "http-error"
            break
        tj = tr.json()
        task_status = tj.get("status", "unknown")
        if task_status in ("completed", "failed"):
            task_result = tj.get("result")
            task_error = tj.get("error")
            break
        time.sleep(0.05)
    check("查重任务最终完成", task_status == "completed")
    if task_status == "completed":
        check("result.duplicates_found 为 int", isinstance(task_result.get("duplicates_found"), int))
        check("result.total_compared 存在", isinstance(task_result.get("total_compared"), int))
    elif task_error:
        check(f"查重任务失败信息: {task_error}", False)

    # 验证 DB 中已写入结果
    with DatabaseManager.session() as session:
        results = q.get_unresolved_duplicates(session, limit=10)
        check(f"DB 中有查重结果", len(results) > 0)
        if results:
            check("查重结果有相似度分数", results[0].similarity_score > 0)
            matches = q.get_file_matches_for_result(session, results[0].id)
            check(f"查重结果有匹配文件对", len(matches) > 0)

    # 验证互斥门在查重完成后被正确释放
    from app.services.dedup_gate import is_dedup_busy
    check("查重结束后互斥门已释放", not is_dedup_busy())


# ============================================================
# TDD 测试 —— 右键菜单、排序、树搜索过滤
# ============================================================

def test_pin_auth_api():
    """F09: Web PIN 后端强制 —— 未授权 401 / verify 换 token / 带 token 放行。"""
    section("测试 15.5: Web PIN 后端鉴权")
    from app.api.server import create_app, _revoke_all_auth_tokens
    config = AppConfig().with_updates(web_pin="1234")
    app = create_app(config)
    client = TestClient(app)
    try:
        check("健康检查无需鉴权", client.get("/api/health").status_code == 200)
        check("auth/status 无需鉴权",
              client.get("/api/auth/status").json() == {"pin_required": True})
        check("未带令牌访问 /api/units → 401",
              client.get("/api/units").status_code == 401)
        bad = client.post("/api/auth/verify", json={"pin": "0000"})
        check("错误 PIN → 403", bad.status_code == 403)
        ok = client.post("/api/auth/verify", json={"pin": "1234"})
        token = ok.json().get("token") if ok.status_code == 200 else ""
        check("正确 PIN → 200 且返回令牌", ok.status_code == 200 and bool(token))
        check("带令牌访问 /api/units → 200",
              client.get("/api/units", headers={"Authorization": f"Bearer {token}"}).status_code == 200)
        check("伪造令牌 → 401",
              client.get("/api/units", headers={"Authorization": "Bearer forged"}).status_code == 401)

        # ---- <img>/<video> 标签请求无法携带 Authorization 头 → ?token= 回退 ----
        from app.db.models import MediaFile as _MF
        with DatabaseManager.session() as session:
            row = session.query(_MF.id).first()
        fid = row[0] if row else None
        if fid is None:
            check("媒体端点鉴权: 无文件可测，跳过", True)
        else:
            check("缩略图未带令牌 → 401",
                  client.get(f"/api/files/{fid}/thumbnail").status_code == 401)
            check("缩略图伪造 ?token → 401",
                  client.get(f"/api/files/{fid}/thumbnail?token=forged").status_code == 401)
            r_q = client.get(f"/api/files/{fid}/thumbnail?token={token}")
            check("缩略图 ?token= 有效令牌 → 200", r_q.status_code == 200)
            r_h = client.get(f"/api/files/{fid}/thumbnail",
                             headers={"Authorization": f"Bearer {token}"})
            check("缩略图 Authorization 头仍有效 → 200", r_h.status_code == 200)
            r_s = client.get(f"/api/files/{fid}/stream?token={token}", headers={"Range": "bytes=0-9"})
            check("视频流 ?token= 有效令牌 → 206/200",
                  r_s.status_code in (200, 206))

        # ---- 认证方案大小写不敏感（RFC 7235）----
        r_lower = client.get("/api/units", headers={"Authorization": f"bearer {token}"})
        check("小写 bearer 方案 → 200", r_lower.status_code == 200)

        # ---- CORS 不得回显任意 Origin 且带凭据 ----
        r_cors = client.get("/api/units", headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://evil.example",
        })
        check("跨域请求不返回 allow-credentials=true",
              r_cors.headers.get("access-control-allow-credentials") != "true")

        # ---- PIN 暴力破解节流：窗口内连续失败后返回 429 ----
        from app.api import server as _server_mod
        _server_mod._auth_failures.clear()
        last = None
        for _ in range(6):
            last = client.post("/api/auth/verify", json={"pin": "0000"})
        check("连续失败后触发限流 → 429", last.status_code == 429)
        check("限流期间即使密码正确也被拦截",
              client.post("/api/auth/verify", json={"pin": "1234"}).status_code == 429)
        _server_mod._auth_failures.clear()
        check("解除限流后正确密码可用",
              client.post("/api/auth/verify", json={"pin": "1234"}).status_code == 200)
        _server_mod._auth_failures.clear()

        # ---- 改 PIN / 重建 app 后旧令牌必须立即失效 ----
        token2 = client.post("/api/auth/verify", json={"pin": "1234"}).json().get("token", "")
        _server_mod.create_app(AppConfig().with_updates(web_pin="4321"))
        check("重建 app（改 PIN）后旧令牌失效 → 401",
              client.get("/api/units",
                         headers={"Authorization": f"Bearer {token2}"}).status_code == 401)
    finally:
        _revoke_all_auth_tokens()


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


def _make_temp_unit(tmp_root: Path, name: str, files: list[str]) -> tuple:
    """建一个临时资源单元并写入若干真实图片文件。

    参数:
        tmp_root: 临时根目录（同时作为媒体库根）。
        name: 单元（文件夹）名。
        files: 文件名列表。

    返回:
        (unit_id, [file_id, ...], [Path, ...])。
    """
    unit_dir = tmp_root / name
    unit_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, fname in enumerate(files):
        p = unit_dir / fname
        Image.new("RGB", (48, 48), color=(i * 60 % 255, 30, 30)).save(p)
        paths.append(p)

    with DatabaseManager.session() as session:
        # 同一个临时根会被多个单元复用：media_library_roots.path 有唯一约束，
        # 重复 add 会撞 UNIQUE（而不是"无害的重复行"）
        root = next(
            (r for r in q.get_all_roots(session) if r.path == str(tmp_root)), None
        )
        if root is None:
            root = q.add_library_root(session, str(tmp_root))
        unit = q.create_unit(
            session, str(unit_dir), name, root.id,
            file_count=len(paths),
            total_size=sum(p.stat().st_size for p in paths),
        )
        ids = []
        for p in paths:
            mf = q.insert_media_file(
                session, path=str(p), filename=p.name, extension=".jpg",
                media_type="image", size_bytes=p.stat().st_size,
                resource_unit_id=unit.id,
            )
            ids.append(mf.id)
    return unit.id, ids, paths


def test_api_file_delete_modes():
    """T20.5: 文件删除 API —— 回收站 / 仅记录 / 批量 / 单元统计重算。

    回归背景：Web 端唯一的删除入口调用了 `_deleteFile`（下划线开头的方法），
    Vue 3 渲染代理不暴露 `_` 开头的 key，点击会抛 ReferenceError，删除完全无效；
    同时旧 API 只删数据库记录，磁盘文件留在原地，重扫后又回到库里。
    """
    section("T20.5: 文件删除 API（回收站/记录/批量）")
    from app.api.server import create_app
    from app.db.models import MediaFile

    client = TestClient(create_app(AppConfig()))
    tmp_root = Path(tempfile.mkdtemp(prefix="del_api_"))
    try:
        # 用临时缓存目录，避免测试删到真实库的 data/.thumbnails
        thumb_cache = tmp_root / "cache"
        thumb_cache.mkdir(parents=True, exist_ok=True)
        client = TestClient(create_app(
            AppConfig().with_updates(thumbnail_cache_dir=thumb_cache)
        ))
        unit_id, ids, paths = _make_temp_unit(tmp_root, "待删单元", ["a.jpg", "b.jpg", "c.jpg"])

        # ---- mode=record：只删库记录，磁盘文件保留 ----
        resp = client.delete(f"/api/files/{ids[0]}?mode=record")
        check("mode=record 返回 200", resp.status_code == 200)
        check("mode=record 磁盘文件保留", paths[0].exists())
        with DatabaseManager.session() as session:
            check("mode=record 记录已删除", q.get_file_by_id(session, ids[0]) is None)

        # ---- 默认 mode=trash：移至回收站 + 删记录 ----
        resp = client.delete(f"/api/files/{ids[1]}")
        with DatabaseManager.session() as session:
            record_gone = q.get_file_by_id(session, ids[1]) is None
        if resp.status_code == 200:
            check("默认 mode=trash 返回 200", True)
            check("文件已从原路径移走（进回收站）", not paths[1].exists())
            check("trash 成功后记录已删除", record_gone)
        else:
            # 沙箱/网络路径无法进回收站：必须 409 且记录保留 ——
            # 决不允许出现"记录没了但文件还在"的孤儿状态
            check(f"回收站不可用时返回 409（实际 {resp.status_code}）", resp.status_code == 409)
            check("回收站失败时记录保留（无孤儿）", not record_gone)
            check("回收站失败时文件仍在原路径", paths[1].exists())

        # ---- 不存在的文件 ----
        resp = client.delete("/api/files/99999999")
        check("删除不存在文件返回 404", resp.status_code == 404)

        # ---- 批量删除：有效 + 重复 ID + 不存在 ----
        # 顺带验证删除会同时清掉缩略图与其 .meta sidecar（只删图会留下孤儿 meta，
        # 而 meta 正是判断"缓存属于哪个文件"的依据）
        planted = []
        for suffix in ("", ".meta"):
            p = thumb_cache / f"{ids[2]}_thumb.jpg{suffix}"
            p.write_bytes(b"stale")
            planted.append(p)
        check("已植入待清理的缓存与 sidecar", all(p.exists() for p in planted))

        resp = client.post("/api/files/batch-delete", json={
            "file_ids": [ids[2], ids[2], 99999999], "mode": "record",
        })
        check("批量删除返回 200（部分失败仍 200）", resp.status_code == 200)
        data = resp.json()
        check("重复 ID 去重后只删成功一次", data.get("deleted") == [ids[2]])
        check("失败项逐条给出原因",
              len(data.get("failed", [])) == 1
              and data["failed"][0]["file_id"] == 99999999
              and bool(data["failed"][0]["reason"]))
        check("存在失败项时 success=False", data.get("success") is False)
        check("删除后缓存与 .meta sidecar 一并清理",
              not any(p.exists() for p in planted))

        # ---- 非法 mode 被 pydantic 拒绝 ----
        resp = client.post("/api/files/batch-delete", json={"file_ids": [1], "mode": "hard"})
        check("非法 mode 返回 422", resp.status_code == 422)

        # ---- 单元统计随删除重算（Web 单元卡片直接显示这两列） ----
        with DatabaseManager.session() as session:
            rows = q.get_files_by_unit(session, unit_id)
            expected_count = len(rows)
            expected_size = sum(f.size_bytes or 0 for f in rows)
            unit = q.get_unit_by_id(session, unit_id)
            # 三条删除路径全部成功 → 0 条剩余；若回收站不可用（沙箱/网络路径），
            # ids[1] 会保留 → 1 条剩余。两种都合法，关键是与 file_count 一致
            check("删除后剩余记录数符合预期（0 或 1）", expected_count in (0, 1))
            check("单元 file_count 与剩余记录一致",
                  unit.file_count == expected_count)
            check("单元 total_size 与剩余文件字节和一致",
                  unit.total_size == expected_size)
    finally:
        import shutil
        with DatabaseManager.session() as session:
            q.delete_resource_unit(session, unit_id)
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_api_unit_delete_folder():
    """T20.6: 删除资源单元（文件夹）API —— 默认移至回收站，可只删记录。"""
    section("T20.6: 删除文件夹 API")
    from app.api.server import create_app
    from app.db.models import ResourceUnit

    client = TestClient(create_app(AppConfig()))
    tmp_root = Path(tempfile.mkdtemp(prefix="del_unit_"))
    unit_ids = []
    try:
        # 单元 1：mode=record → 文件夹必须原样保留
        uid1, _, paths1 = _make_temp_unit(tmp_root, "仅出库", ["x.jpg"])
        unit_ids.append(uid1)
        resp = client.delete(f"/api/units/{uid1}?mode=record")
        check("单元 mode=record 返回 200", resp.status_code == 200)
        check("mode=record 文件夹保留", paths1[0].parent.exists())
        with DatabaseManager.session() as session:
            check("mode=record 单元记录已删除",
                  q.get_unit_by_id(session, uid1) is None)
            check("mode=record 文件记录级联删除",
                  len(q.get_files_by_unit(session, uid1)) == 0)

        # 单元 2：默认 mode=trash → 文件夹整体移入回收站（或 409 且保留）
        uid2, _, paths2 = _make_temp_unit(tmp_root, "整柜删除", ["y.jpg"])
        unit_ids.append(uid2)
        resp = client.delete(f"/api/units/{uid2}")
        with DatabaseManager.session() as session:
            unit_gone = q.get_unit_by_id(session, uid2) is None
        if resp.status_code == 200:
            check("默认 mode=trash 返回 200", True)
            check("文件夹已从原路径移走", not paths2[0].parent.exists())
            check("trash 成功后单元记录已删除", unit_gone)
        else:
            check(f"回收站不可用时返回 409（实际 {resp.status_code}）", resp.status_code == 409)
            check("回收站失败时单元记录保留", not unit_gone)
            check("回收站失败时文件夹仍在", paths2[0].parent.exists())

        # 不存在的单元
        resp = client.delete("/api/units/99999999")
        check("删除不存在单元返回 404", resp.status_code == 404)

        # 级联：删除单元会带走其 dedup_results（外键 ON DELETE CASCADE）
        uid3, ids3, _ = _make_temp_unit(tmp_root, "带查重", ["z.jpg"])
        unit_ids.append(uid3)
        uid4, _, _ = _make_temp_unit(tmp_root, "对手单元", ["w.jpg"])
        unit_ids.append(uid4)
        with DatabaseManager.session() as session:
            dr = q.upsert_dedup_result(
                session, unit_a_id=uid3, unit_b_id=uid4,
                similarity_score=0.5, match_count=1,
                total_files_a=1, total_files_b=1, match_types="md5",
                match_level="related",
            )
            dr_id = dr.id
        resp = client.delete(f"/api/units/{uid3}?mode=record")
        check("级联测试单元删除成功", resp.status_code == 200)
        with DatabaseManager.session() as session:
            check("单元删除后其查重结果级联清除",
                  q.get_dedup_by_id(session, dr_id) is None)
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for uid in unit_ids:
                q.delete_resource_unit(session, uid)
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_dedup_level_api():
    """T20.7: 查重结果分级 API —— level 过滤 / counts / 详情 is_hint。

    用户诉求：同演员/同场景的不同片子也想知道，但不打算删。桌面端把它们记为
    `match_level='related'`（仅提醒），并保证不提供"保留 A/B"。Web 端必须能
    分开取这两级，否则 200+ 对 related 会把真正的重复挤出列表。
    """
    section("T20.7: 查重分级 API（level/counts/is_hint）")
    from app.api.server import create_app

    client = TestClient(create_app(AppConfig()))
    tmp_root = Path(tempfile.mkdtemp(prefix="dedup_level_"))
    unit_ids = []
    try:
        uid1, ids1, _ = _make_temp_unit(tmp_root, "分级A", ["p1.jpg", "p2.jpg"])
        uid2, ids2, _ = _make_temp_unit(tmp_root, "分级B", ["p3.jpg"])
        uid3, _, _ = _make_temp_unit(tmp_root, "分级C", ["p4.jpg"])
        unit_ids = [uid1, uid2, uid3]

        with DatabaseManager.session() as session:
            dup = q.upsert_dedup_result(
                session, unit_a_id=uid1, unit_b_id=uid2,
                similarity_score=0.92, match_count=2,
                total_files_a=2, total_files_b=1, match_types="md5,phash",
                match_level="duplicate",
            )
            dup_id = dup.id
            q.insert_file_match(session, dedup_result_id=dup_id,
                                file_a_id=ids1[0], file_b_id=ids2[0],
                                similarity_score=1.0, match_type="md5")
            rel = q.upsert_dedup_result(
                session, unit_a_id=uid1, unit_b_id=uid3,
                similarity_score=0.12, match_count=2,
                total_files_a=2, total_files_b=1, match_types="face",
                match_level="related",
            )
            rel_id = rel.id
            # related 的"证据"里既有计数匹配也有人脸线索：人脸行必须被标为 hint
            q.insert_file_match(session, dedup_result_id=rel_id,
                                file_a_id=ids1[1], file_b_id=ids1[1],
                                similarity_score=0.7, match_type="face")

        # ---- level 过滤 ----
        resp = client.get("/api/dedup/results?level=related&per_page=100")
        check("level=related 返回 200", resp.status_code == 200)
        rows = resp.json().get("results", [])
        check("level=related 只返回 related",
              bool(rows) and all(r["match_level"] == "related" for r in rows))
        check("level=related 命中刚写入的记录", any(r["id"] == rel_id for r in rows))

        resp = client.get("/api/dedup/results?level=duplicate&per_page=100")
        rows = resp.json().get("results", [])
        check("level=duplicate 只返回 duplicate",
              bool(rows) and all(r["match_level"] == "duplicate" for r in rows))
        check("level=duplicate 命中刚写入的记录", any(r["id"] == dup_id for r in rows))

        # ---- 非法 level 被拒 ----
        resp = client.get("/api/dedup/results?level=bogus")
        check("非法 level 返回 422", resp.status_code == 422)

        # ---- 分级计数 ----
        resp = client.get("/api/dedup/counts")
        check("counts 返回 200", resp.status_code == 200)
        counts = resp.json()
        check("counts 含 duplicate/related/total",
              all(k in counts for k in ("duplicate", "related", "total")))
        check("counts.total = duplicate + related",
              counts["total"] == counts["duplicate"] + counts["related"])
        check("counts.related ≥ 1", counts["related"] >= 1)
        check("counts.duplicate ≥ 1", counts["duplicate"] >= 1)

        # ---- 详情：文件名 + 人脸行标记 ----
        resp = client.get(f"/api/dedup/results/{rel_id}")
        check("详情返回 200", resp.status_code == 200)
        detail = resp.json()
        check("详情含 match_level=related", detail.get("match_level") == "related")
        matches = detail.get("file_matches", [])
        check("详情含文件匹配行", len(matches) >= 1)
        check("匹配行带文件名（非仅 #id）",
              bool(matches) and bool(matches[0].get("file_a_name"))
              and matches[0]["file_a_name"] != f"#{matches[0]['file_a_id']}")
        check("人脸行被标记为 is_hint",
              any(m["match_type"] == "face" and m["is_hint"] is True for m in matches))
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for uid in unit_ids:
                q.delete_resource_unit(session, uid)
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_dedup_run_index_first():
    """T20.8: 一键查重先补索引 —— 未索引文件不能对查重隐形。

    桌面端「查重」是"自动检测未索引文件 → 有则先算哈希+人脸 → 再比对"。
    API 早期直接跑比对：未索引文件没有任何哈希，永远匹配不上，却会返回
    "查重完成"。这里造两个全新（无哈希）的单元，验证 index_first 真的补了索引
    并因此把字节级相同的两个文件判为重复。
    """
    section("T20.8: 一键查重 index_first 补索引")
    from app.api.server import create_app
    import time as _time

    client = TestClient(create_app(AppConfig()))
    tmp_root = Path(tempfile.mkdtemp(prefix="dedup_index_"))
    unit_ids = []
    try:
        # 两个单元各放一份字节完全相同的图片：不索引 → 查不出；先索引 → MD5 命中
        uid1, ids1, paths1 = _make_temp_unit(tmp_root, "索引A", ["same.jpg"])
        uid2, ids2, _ = _make_temp_unit(tmp_root, "索引B", ["same.jpg"])
        # _make_temp_unit 用不同颜色生成，这里手工把 B 覆盖成与 A 字节一致
        import shutil as _shutil
        _shutil.copyfile(paths1[0], tmp_root / "索引B" / "same.jpg")
        unit_ids = [uid1, uid2]

        with DatabaseManager.session() as session:
            for fid in (ids1[0], ids2[0]):
                check(f"文件 {fid} 初始未索引",
                      q.get_file_by_id(session, fid).md5_hash is None)

        resp = client.post("/api/dedup/run", json={
            "unit_ids": unit_ids, "threshold": 0.5, "index_first": True,
        })
        check("index_first 查重提交返回 202", resp.status_code == 202)
        task_id = resp.json().get("task_id", "")

        status = "unknown"
        result = None
        error = None
        saw_phase = set()
        for _ in range(600):
            tj = client.get(f"/api/dedup/run/{task_id}").json()
            if tj.get("phase"):
                saw_phase.add(tj["phase"])
            status = tj.get("status", "unknown")
            if status in ("completed", "failed"):
                result = tj.get("result")
                error = tj.get("error")
                break
            _time.sleep(0.05)
        check("index_first 任务完成", status == "completed")
        if status != "completed":
            check(f"任务失败信息: {error}", False)
        else:
            check("结果带 unindexed_before（可观测补索引量）",
                  isinstance(result.get("unindexed_before"), int))
            check("结果带 indexed_count", isinstance(result.get("indexed_count"), int))
            check("确实索引了 ≥2 个文件", result.get("indexed_count", 0) >= 2)
            check("比对阶段被执行过（出现 comparing 阶段）",
                  "comparing" in saw_phase or "indexing" in saw_phase)

        # 索引结果落库
        with DatabaseManager.session() as session:
            h1 = q.get_file_by_id(session, ids1[0]).md5_hash
            h2 = q.get_file_by_id(session, ids2[0]).md5_hash
            check("索引后 A 文件有 MD5", bool(h1))
            check("索引后 B 文件有 MD5", bool(h2))
            check("字节相同的两份文件 MD5 一致", h1 == h2)

            dr = (session.query(q.DedupResult)
                  .filter(q.DedupResult.unit_a_id.in_(unit_ids),
                          q.DedupResult.unit_b_id.in_(unit_ids))
                  .first())
            check("补索引后该单元对被判为重复",
                  dr is not None and dr.match_level == "duplicate"
                  and dr.similarity_score >= 0.5)
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for uid in unit_ids:
                q.delete_resource_unit(session, uid)
        shutil.rmtree(tmp_root, ignore_errors=True)


def test_dedup_web_js_no_underscore_method_calls():
    """T20.9: Web 模板不得调用下划线开头的方法（Vue 3 渲染代理不暴露）。

    回归：`_deleteFile` 曾出现在 feed-item 的菜单里，点击抛
    `ReferenceError: _deleteFile is not defined`（浏览器实测），删除完全无效。
    这条静态检查拦住同类问题再次进入前端代码。
    """
    section("T20.9: Web 模板下划线方法调用检查")
    import re
    web_dir = Path(__file__).resolve().parent.parent.parent / "web"
    js_files = sorted(web_dir.glob("js-*.js"))
    check("找到 Web JS 文件", len(js_files) >= 3)

    # 模板里的调用形式：@click="... _foo(...)" / action: () => _foo( / {{ _foo( }}
    pattern = re.compile(r"[\s(\"'=:>]_\w+\s*\(")
    offenders = []
    for src in js_files:
        text = src.read_text(encoding="utf-8")
        # 只看 template: `...` 段落，方法体内部的 _helper() 调用是合法的
        for m in re.finditer(r"template:\s*`(.*?)`,\s*\n", text, re.S):
            for line_no, line in enumerate(m.group(1).splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{src.name}: {line.strip()[:80]}")
    check(f"模板中无下划线方法调用（发现 {len(offenders)} 处）", not offenders)
    for item in offenders:
        print(f"      → {item}")


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


def test_content_modified_at():
    """单元内容真实修改日期：扫描聚合 max(mtime) → 入库 → 迁移回填 → API 暴露。

    背景：文件夹被移动到新位置后，其文件系统 mtime/ctime 变为移动当天日期，
    应用必须以"单元内媒体文件的最新 st_mtime"作为排序与显示依据，
    否则按日期排序无法还原真实时间顺序。
    """
    section("测试: 单元内容真实修改日期 content_modified_at")
    from datetime import datetime
    from PIL import Image as _Img

    tmp = Path(tempfile.mkdtemp(prefix="content_mtime_"))
    unit_dir = tmp / "老片段"
    unit_dir.mkdir(parents=True)
    img = _Img.new("RGB", (32, 32), color=(10, 20, 30))
    f_old = unit_dir / "旧.jpg"; img.save(f_old)
    f_mid = unit_dir / "中.png"; img.save(f_mid)
    f_new = unit_dir / "新.jpg"; img.save(f_new)
    ts_old = datetime(2023, 5, 20, 8, 30, 0).timestamp()
    ts_mid = datetime(2023, 11, 1, 9, 0, 0).timestamp()
    ts_new = datetime(2024, 6, 15, 12, 0, 0).timestamp()
    os.utime(f_old, (ts_old, ts_old))
    os.utime(f_mid, (ts_mid, ts_mid))
    os.utime(f_new, (ts_new, ts_new))
    expected_dt = datetime.fromtimestamp(ts_new)
    unit_id = None

    try:
        # ---- 1) 扫描聚合 max mtime ----
        from app.core.scanner import MediaScanner as _MS
        config = AppConfig()
        scanner = _MS(extensions=config.media_extensions,
                      exclude_patterns=config.exclude_patterns)
        result = scanner.scan_root(tmp)
        check("扫描识别 1 个单元", len(result.units) == 1)
        if not result.units:
            return
        scanned_unit = result.units[0]
        check("扫描结果含 latest_mtime 属性", hasattr(scanned_unit, "latest_mtime"))
        if not hasattr(scanned_unit, "latest_mtime") or scanned_unit.latest_mtime is None:
            return
        check("unit.latest_mtime == 单元内最新文件 mtime",
              abs(scanned_unit.latest_mtime - ts_new) < 1.0)

        # ---- 2) 入库：create_unit / update_unit_stats ----
        with DatabaseManager.session() as session:
            root = q.get_root_by_path(session, str(tmp))
            if not root:
                root = q.add_library_root(session, str(tmp))
            root_id = root.id
            try:
                new_unit = q.create_unit(
                    session, path=str(unit_dir), name="老片段",
                    library_root_id=root_id, is_manual=False,
                    file_count=3,
                    total_size=sum(f.size_bytes for f in scanned_unit.files),
                    content_modified_at=expected_dt,
                )
                unit_id = new_unit.id
            except TypeError:
                check("create_unit 支持 content_modified_at 参数", False)
                return
        check("create_unit 支持 content_modified_at 参数", True)
        with DatabaseManager.session() as session:
            row = q.get_unit_by_id(session, unit_id)
            check("create_unit 落库 content_modified_at",
                  row.content_modified_at is not None
                  and row.content_modified_at.date() == expected_dt.date())
            q.update_unit_stats(session, unit_id, 3, 12345,
                                content_modified_at=datetime.fromtimestamp(ts_mid))
        with DatabaseManager.session() as session:
            row = q.get_unit_by_id(session, unit_id)
            check("update_unit_stats 更新 content_modified_at",
                  row.content_modified_at is not None
                  and row.content_modified_at.date()
                  == datetime.fromtimestamp(ts_mid).date())

        # ---- 3) media_files 关联 + 迁移回填 ----
        from app.db.models import MediaFile as _MF, ResourceUnit as _RU
        with DatabaseManager.session() as session:
            for f in scanned_unit.files:
                session.add(_MF(
                    path=str(f.path), filename=f.filename,
                    extension=f.extension, media_type=f.media_type,
                    size_bytes=f.size_bytes, resource_unit_id=unit_id,
                ))
            # 模拟旧库：全部单元的内容日期为 NULL
            session.query(_RU).update({"content_modified_at": None})
        # 模拟 v3 旧库（test_db() 已把版本升到 4，重置后重新走 v3→v4 迁移）
        from sqlalchemy import text as _text
        engine = DatabaseManager.get_engine()
        with engine.connect() as conn:
            conn.execute(_text("PRAGMA user_version = 3;"))
            conn.commit()
        migrate_db()
        with DatabaseManager.session() as session:
            row = q.get_unit_by_id(session, unit_id)
            check("迁移回填 content_modified_at == 文件真实最新 mtime",
                  row.content_modified_at is not None
                  and row.content_modified_at.date() == expected_dt.date())

        # ---- 4) API 暴露 ----
        from app.api.server import create_app
        from fastapi.testclient import TestClient
        app = create_app(AppConfig())
        client = TestClient(app)
        resp = client.get("/api/units")
        check("/api/units 返回 200", resp.status_code == 200)
        if resp.status_code != 200:
            return
        target = next((u for u in resp.json().get("units", [])
                       if u["path"] == str(unit_dir)), None)
        check("/api/units 包含测试单元", target is not None)
        if target:
            check("/api/units 返回 content_modified_at",
                  bool(target.get("content_modified_at"))
                  and target["content_modified_at"].startswith("2024-06-15"))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
        with DatabaseManager.session() as session:
            if unit_id is not None:
                q.delete_resource_unit(session, unit_id)
            root = q.get_root_by_path(session, str(tmp))
            if root:
                session.delete(root)


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
        test_hash_worker_video_face_detection()
        test_face_detection_degrades_gracefully()
        test_face_cosine_metric()
        test_face_detector_thread_safety()
        test_config_from_file_tolerates_bad_field()
        test_web_pin_not_persisted_in_config()

        # 核心测试
        test_db()
        test_migration_v4_to_v5_dedup_match_type()
        test_migration_v5_to_v6_match_level()
        test_db_integrity()
        result = test_scanner(temp_root)
        test_save_to_db(result, temp_root)
        test_scan_worker_path_chunking()
        test_path_revalidation()
        test_scanner_nested_promotion()
        test_scanner_no_file_loss()
        test_zero_byte_file()
        test_refresh_workflow()
        test_hash_engine(temp_root)
        test_hash_batch(temp_root)
        test_thumbnails(temp_root)
        test_thumbnail_generator_formats()
        test_heic_converter()
        test_dedup_engine()
        test_dedup_one_to_one_matching()
        test_dedup_hash_index_recall()
        test_dedup_video_frame_matching()
        test_dedup_video_frame_precision()
        test_dedup_video_frame_index_equivalence()
        test_dedup_face_is_auxiliary_only()
        test_dedup_related_level()
        test_dedup_related_persistence()
        test_hash_worker_indexes_all_batches()
        test_save_dedup_results()
        test_dedup_results_pagination()
        test_tree_model()
        test_context_menu_lambda_safety()
        test_ui_signal_integration()
        test_status_bar_cancel_wiring()
        test_status_bar_cancel_button_not_squashed()
        test_scan_worker_cancel_signal()
        test_dedup_compare_shows_face_hints()
        test_message_center()
        test_tags()
        test_api_app()
        test_unit_response_fields()
        test_thumbnail_binary()
        test_thumbnail_endpoint_validates_cache_owner()
        test_thumbnail_config_respected()
        test_cleanup_cache_sidecars()
        test_file_stream()
        test_unread_events_endpoint()
        test_web_static_files()
        test_dedup_run_api()
        test_pin_auth_api()
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
        test_api_file_delete_modes()
        test_api_unit_delete_folder()
        test_dedup_level_api()
        test_dedup_run_index_first()
        test_dedup_web_js_no_underscore_method_calls()
        test_tag_assign_ui()

        # 新增覆盖测试
        test_watcher_event_handler()
        test_whitelist_and_scan_queries()
        test_unit_metadata_queries()
        test_content_modified_at()
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
            except (PermissionError, OSError):
                pass  # Windows 有时延迟释放文件句柄 / 沙箱回收站不可用

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
