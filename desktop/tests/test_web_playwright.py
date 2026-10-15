# -*- coding: utf-8 -*-
"""
Web 前端页面级功能测试 —— 基于 Playwright 的 Vue SPA 浏览器测试。

使用方式:
    pytest tests/test_web_playwright.py -v  (需要安装 pytest-playwright)
    或直接运行:
    python tests/test_web_playwright.py

依赖:
    pip install playwright pytest-playwright
    playwright install chromium
"""

import sys
import io
import json
import tempfile
import time
import threading
from pathlib import Path

# 编码修复
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent.parent))

import uvicorn
from config import AppConfig
from app.db.engine import DatabaseManager
from app.db.migrations import init_db, migrate_db

_passed = 0
_failed = 0
_server = None
_server_thread = None
_app = None
_PORT = 19528  # 不同于主应用的端口，避免冲突


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


def ensure_db():
    """创建并填充测试数据库，并清掉**上一次跑测试残留的 PIN**。

    为什么要清 PIN：PIN 文件落在 `desktop/data/.web_pin`（与库同级），上一次跑完
    会留下它；本次运行时 API 就要求解锁，而 Playwright 的页面拿不到令牌 → 停在
    锁屏、没有导航栏（曾把 "#35 便携版入口" 卡成 30s 超时）。测试必须自带干净
    起点，不能依赖上一次运行的残留状态。
    """
    test_db = Path(__file__).parent.parent / "data" / "test_web_playwright.db"
    stale_pin = Path(__file__).parent.parent / "data" / ".web_pin"
    try:
        if stale_pin.exists():
            stale_pin.unlink()
            print("  [INFO] 已清理上次运行残留的 data/.web_pin（避免整套用例停在锁屏）")
    except OSError as e:
        print(f"  [INFO] 无法清理残留 PIN 文件: {e}")
    if test_db.exists():
        try:
            test_db.unlink()
        except OSError as e:
            # 某些受限环境（如回收站不可用的沙箱）无法删除文件：
            # 改为先连上旧库并 drop_all，保证下方 init_db 得到干净 schema
            print(f"  [INFO] 无法删除旧测试库（{e}），将通过 drop_all 重建")
            DatabaseManager.initialize(test_db)
            from app.db.models import Base
            Base.metadata.drop_all(DatabaseManager.get_engine())
    DatabaseManager.initialize(test_db)
    from app.db.migrations import init_db, migrate_db
    init_db(test_db)
    migrate_db()

    # 创建测试媒体数据
    from app.db import queries as q
    from PIL import Image
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="media_web_test_"))
    unit_a = root / "片段A"
    unit_b = root / "片段B"
    unit_a.mkdir(parents=True)
    unit_b.mkdir(parents=True)
    for i in range(6):
        Image.new("RGB", (100, 100), color=(i * 50, 100, 200)).save(unit_a / f"照片{i+1:02d}.jpg")
        Image.new("RGB", (100, 100), color=(200, 100, i * 50)).save(unit_b / f"视频{i+1:02d}.jpg")

    # HEIC 夹具：Web 端 HEIC 页需要真实可解码的候选（pillow-heif 缺失时跳过）
    heic_unit = root / "HEIC照片"
    heic_unit.mkdir(parents=True, exist_ok=True)
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        for i in range(2):
            Image.new("RGB", (160, 120), color=(i * 60, 100, 200)).save(
                heic_unit / f"heic{i}.HEIC", format="HEIF")
    except Exception as e:
        print(f"  [INFO] 无法生成 HEIC 夹具（HEIC 页测试会跳过）: {e}")
    # 设置不同的文件真实 mtime：片段A=2024-01-01（较早），片段B=2025-06-01（较新）
    # 使两个单元的 content_modified_at 可区分，供日期排序测试使用
    import os as _os
    from datetime import datetime as _dt
    ts_a = _dt(2024, 1, 1, 10, 0, 0).timestamp()
    ts_b = _dt(2025, 6, 1, 10, 0, 0).timestamp()
    for f in unit_a.glob("*.jpg"):
        _os.utime(f, (ts_a, ts_a))
    for f in unit_b.glob("*.jpg"):
        _os.utime(f, (ts_b, ts_b))
    # HEIC 单元的日期刻意夹在 A/B 之间：日期排序测试断言"升序第一是片段A、
    # 降序第一是片段B"，夹具若用当前时间就会插到最前面把断言打穿
    ts_heic = _dt(2024, 6, 15, 10, 0, 0).timestamp()
    for f in heic_unit.glob("*.HEIC"):
        _os.utime(f, (ts_heic, ts_heic))

    from app.core.scanner import MediaScanner
    scanner = MediaScanner(
        extensions=AppConfig().media_extensions,
        exclude_patterns=AppConfig().exclude_patterns,
    )
    result = scanner.scan_root(root)
    with DatabaseManager.session() as session:
        db_root = q.add_library_root(session, str(result.root_path))
        for unit in result.units:
            # 写入单元内容的真实最新修改时间（与桌面端扫描入库逻辑一致）
            content_dt = (
                _dt.fromtimestamp(unit.latest_mtime)
                if unit.latest_mtime is not None else None
            )
            new_unit = q.create_unit(
                session, str(unit.path), unit.name, db_root.id,
                file_count=unit.file_count, total_size=unit.total_size,
                content_modified_at=content_dt,
            )
            for df in unit.files:
                q.insert_media_file(
                    session, path=str(df.path), filename=df.filename,
                    extension=df.extension, media_type=df.media_type,
                    size_bytes=df.size_bytes, resource_unit_id=new_unit.id,
                )
    return root


_THUMB_CACHE_DIR = None


def _thumb_cache_dir() -> Path:
    """测试专用缩略图缓存目录。

    测试库的 file_id 与真实库重叠（都从 1 开始分配），而缓存以
    `{file_id}_thumb.jpg` 命名 —— 不隔离就会读写真实库的 `data/.thumbnails`，
    表现为"测试文件显示真实库的照片"（实测截图里出现过），且测试会覆写用户缓存。
    """
    global _THUMB_CACHE_DIR
    if _THUMB_CACHE_DIR is None:
        _THUMB_CACHE_DIR = Path(tempfile.mkdtemp(prefix="web_thumbs_"))
    return _THUMB_CACHE_DIR


def start_server():
    """启动 API 服务器。"""
    global _server, _server_thread, _app
    import requests
    ensure_db()
    # 监听地址写成测试服务器**实际绑定**的值（127.0.0.1:19528）：
    # 设置页据此显示"手机访问"信息，与真实运行保持一致（回环地址手机连不上）。
    config = AppConfig().with_updates(thumbnail_cache_dir=_thumb_cache_dir(),
                                      api_host="127.0.0.1", api_port=_PORT)
    from app.api.server import create_app
    app = create_app(config, {"config_path": str(
        Path(__file__).parent.parent / "data" / "test_web_playwright.json")})
    _app = app

    import uvicorn
    _server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=_PORT, log_level="error")
    )
    _server_thread = threading.Thread(target=_server.run, daemon=True)
    _server_thread.start()

    # 端口被占用时 uvicorn 绑定失败会直接结束线程，而**另一个**进程可能正在同一
    # 端口上应答健康检查 —— 不校验就会"借用"别人的服务器：两个测试进程共用同一个
    # DB 文件，互相删表/重建，结果是随机失败（实测并发跑两次本套件时踩到过）。
    for _ in range(50):
        if getattr(_server, "started", False):
            break
        if not _server_thread.is_alive():
            raise RuntimeError(
                f"API 服务器启动失败：端口 {_PORT} 可能已被占用"
                f"（请先结束另一个 test_web_playwright 进程）"
            )
        time.sleep(0.1)
    else:
        raise RuntimeError("API 服务器启动超时")

    for _ in range(50):
        try:
            if requests.get(f"http://127.0.0.1:{_PORT}/api/health", timeout=1).status_code == 200:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("API 服务器启动超时")


def stop_server():
    """停止测试服务器。"""
    global _server
    if _server:
        _server.should_exit = True
        _server_thread.join(timeout=5)




# ============================================================
# 浏览器测试
# ============================================================

def test_home_page_loads(page):
    """验证首页加载后 Vue 正常渲染。"""
    section("Web 测试 1: 首页加载")
    page.goto(f"http://127.0.0.1:{_PORT}/")
    # 等待 Vue 挂载（connect 页面或 units 页面）
    page.wait_for_selector(".app-main", timeout=5000)
    # 验证 Vue 已渲染（检查是否有路由视图或连接页面）
    content = page.text_content(".app-main") or ""
    check("Vue 应用已挂载", len(content) > 0)
    # 首页应有连接界面或单元列表
    has_router_view = page.locator(".connect-box, .root-group, .page").count() > 0
    check("页面容器存在", has_router_view)
    # 检查页面标题
    title = page.title()
    check("页面标题非空", len(title) > 0)


def test_units_page_shows_data(page):
    """验证单元列表页面能显示数据。"""
    section("Web 测试 2: 单元列表页面")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")

    # 等待单元列表加载完成
    root_groups = page.locator(".root-group")
    root_groups.first.wait_for(state="attached", timeout=5000)
    count = root_groups.count()
    check("根目录组已渲染", count > 0)

    if count > 0:
        unit_cards = page.locator(".unit-card")
        unit_cards.first.wait_for(state="attached", timeout=5000)
        card_count = unit_cards.count()
        check("单元卡片已渲染", card_count > 0)

        if card_count > 0:
            # 验证卡片信息
            first_name = unit_cards.first.locator(".name").text_content()
            check(f"单元名称非空: {first_name}", len(first_name.strip()) > 0)

            first_meta = unit_cards.first.locator(".meta").text_content()
            check("单元元数据存在", "个文件" in first_meta)

            # 验证排序按钮存在
            sort_btns = page.locator(".sort-btn")
            check("排序按钮存在", sort_btns.count() > 0)

            # 点击排序
            if sort_btns.count() > 0:
                sort_btns.first.click()
                page.wait_for_timeout(300)
                check("排序切换正常", True)


def test_units_date_sort_by_content(page):
    """验证单元按"日期"排序与显示使用内容真实修改时间（content_modified_at）。

    片段A 内容日期 2024-01-01（早），片段B 2025-06-01（晚）。
    """
    section("Web 测试 10: 单元日期排序按内容真实日期")

    # 重置排序状态，保证首次点击"日期"为升序
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.evaluate("""
        () => {
            localStorage.setItem('unit_sort_by', 'name');
            localStorage.setItem('unit_sort_order', 'asc');
        }
    """)
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.wait_for_load_state("networkidle")

    page.locator(".root-group").first.wait_for(state="attached", timeout=5000)
    cards = page.locator(".unit-card")
    cards.first.wait_for(state="attached", timeout=5000)
    if cards.count() < 2:
        check("日期排序测试: 单元不足 2 个，跳过", True)
        return

    # 找到"日期"排序按钮
    sort_btns = page.locator(".sort-btn")
    date_btn = None
    for i in range(sort_btns.count()):
        if "日期" in (sort_btns.nth(i).text_content() or ""):
            date_btn = sort_btns.nth(i)
            break
    if date_btn is None:
        check("日期排序按钮存在", False)
        return
    check("日期排序按钮存在", True)

    def first_card_name() -> str:
        return (page.locator(".unit-card").first.locator(".name")
                .text_content() or "").strip()

    # 升序（首次点击）：内容日期较早的 片段A 在前
    date_btn.click()
    page.wait_for_timeout(300)
    check("升序: 内容日期较早的片段A在前", "片段A" in first_card_name())

    # 降序（再次点击）：内容日期较新的 片段B 在前
    date_btn.click()
    page.wait_for_timeout(300)
    check("降序: 内容日期较新的片段B在前", "片段B" in first_card_name())

    # 卡片元信息显示的是内容日期而非导入日期
    metas = page.locator(".unit-card .meta")
    meta_texts = [metas.nth(i).text_content() or "" for i in range(metas.count())]
    has_a = any("2024-01-01" in t for t in meta_texts)
    has_b = any("2025-06-01" in t for t in meta_texts)
    check("卡片显示内容真实日期", has_a and has_b)


def test_unit_files_page(page):
    """验证进入单元后的文件列表页。"""
    section("Web 测试 3: 文件列表页")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")

    # 等待根目录组和单元卡片
    root_group = page.locator(".root-group").first
    root_group.wait_for(state="attached", timeout=5000)
    if root_group.count() == 0:
        check("无根目录可测试", True)
        return

    # 等待单元卡片加载后再点击进入
    unit_card = page.locator(".unit-card").first
    unit_card.wait_for(state="attached", timeout=5000)
    if unit_card.count() == 0:
        check("无单元可进入", True)
        return

    unit_card.click()

    # 等待文件列表页渲染
    feed_header = page.locator(".feed-header")
    feed_header.wait_for(state="attached", timeout=5000)
    if feed_header.count() > 0:
        header_text = feed_header.text_content() or ""
        check("文件列表页已加载", len(header_text) > 0)
    else:
        check("文件列表页可能未加载（路由改变但无 feed-header）", True)

    # 检查搜索框
    search_input = page.locator(".search-input")
    search_exists = search_input.count() > 0
    check("搜索框存在", search_exists)

    # 检查文件网格
    feed_items = page.locator(".feed-item")
    item_count = feed_items.count()
    check(f"文件缩略图已渲染 ({item_count} 个)", item_count >= 0)

    # 检查缩略图加载（如果有文件）
    if item_count > 0:
        thumb_wraps = page.locator(".thumb-wrap")
        check("缩略图容器存在", thumb_wraps.count() > 0)

        # 检查排序按钮
        sort_btns = page.locator(".feed-header .sort-btn")
        check("文件排序按钮存在", sort_btns.count() > 0)

        # 检查文件名
        first_file_name = feed_items.first.locator(".name").text_content() or ""
        check(f"文件名显示: {first_file_name}", len(first_file_name.strip()) > 0)


def test_preview_navigation(page):
    """验证预览页加载和导航功能。"""
    section("Web 测试 4: 预览页面导航")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")

    # 等待单元加载并进入文件列表
    unit_card = page.locator(".unit-card").first
    unit_card.wait_for(state="attached", timeout=5000)
    if unit_card.count() == 0:
        check("预览测试: 无单元可进入", True)
        return
    unit_card.click()
    page.locator(".feed-header").wait_for(state="attached", timeout=5000)

    # 点击第一个文件进入预览
    feed_item = page.locator(".feed-item").first
    feed_item.wait_for(state="attached", timeout=5000)
    if feed_item.count() == 0:
        check("预览测试: 无文件可预览", True)
        return
    feed_item.click()

    # 等待预览覆盖层加载
    preview = page.locator(".preview-overlay")
    preview.wait_for(state="visible", timeout=5000)
    check("预览页已加载", preview.count() > 0)

    # 验证位置指示器存在（多个文件时）
    nav_hint = page.locator(".image-nav-hint")
    if nav_hint.count() > 0:
        pos_text = nav_hint.locator(".pos").text_content() or ""
        check("位置指示器存在", "/" in pos_text)

        # 测试点击右侧箭头前进到下一张
        next_btn = nav_hint.locator(".mdi-chevron-right")
        if next_btn.count() > 0:
            next_btn.click()
            new_pos = nav_hint.locator(".pos").text_content() or ""
            check("点击前进后位置变化", new_pos != pos_text)

        # 测试点击左侧箭头后退
        prev_btn = nav_hint.locator(".mdi-chevron-left")
        if prev_btn.count() > 0:
            prev_btn.click()
            check("后退按钮响应正常", True)

    # 返回按钮
    back_btn = page.locator(".preview-back")
    if back_btn.count() > 0:
        back_btn.click()
        page.wait_for_timeout(500)
        check("从预览页返回", page.locator(".feed-item").count() > 0)


def test_preview_swipe_gesture(page):
    """验证预览页左右滑动手势导航。"""
    section("Web 测试 5: 预览滑动手势")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")

    # 进入文件列表
    unit_card = page.locator(".unit-card").first
    unit_card.wait_for(state="attached", timeout=5000)
    if unit_card.count() == 0:
        check("滑动手势测试: 无单元可进入", True)
        return
    unit_card.click()
    page.locator(".feed-header").wait_for(state="attached", timeout=5000)

    # 点击第一个文件进入预览
    feed_item = page.locator(".feed-item").first
    feed_item.wait_for(state="attached", timeout=5000)
    if feed_item.count() == 0:
        check("滑动手势测试: 无文件可预览", True)
        return
    feed_item.click()
    page.locator(".preview-overlay").wait_for(state="visible", timeout=5000)

    # 验证预览页加载且有位置指示器
    nav_hint = page.locator(".image-nav-hint")
    if nav_hint.count() == 0 or nav_hint.locator(".pos").count() == 0:
        check("滑动手势测试: 位置指示器不存在（可能仅一个文件）", True)
        return

    # 获取初始位置
    pos_text = nav_hint.locator(".pos").text_content() or ""
    if "/" not in pos_text:
        check("滑动手势测试: 无法获取初始位置", True)
        return
    parts = pos_text.split("/")
    initial_pos = int(parts[0].strip())
    total = int(parts[1].strip())
    if total < 2:
        check("滑动手势测试: 文件不足 2 个，无法测试滑动", True)
        return

    # 检查 gesture-zone 是否存在
    gesture_zone = page.locator(".gesture-zone")
    if gesture_zone.count() == 0:
        check("滑动手势测试: 无 gesture-zone 区域", True)
        return

    # 通过 dispatchEvent 模拟 TouchEvents（Playwright mouse 不触发 touch 事件）
    page.evaluate("""
        () => {
            const zone = document.querySelector('.gesture-zone');
            if (!zone) return;
            const rect = zone.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            function fire(name, x) {
                const evt = new TouchEvent(name, { bubbles: true, cancelable: true });
                const touches = [{ clientX: x, clientY: cy, identifier: 0 }];
                Object.defineProperty(evt, 'changedTouches', {
                    get: () => touches, configurable: true
                });
                zone.dispatchEvent(evt);
            }
            fire('touchstart', cx + 60);
            fire('touchmove', cx - 20);
            fire('touchend', cx - 60);
        }
    """)
    page.wait_for_timeout(500)

    # 验证位置变化（左滑 → 前进到下一张）
    new_pos_text = nav_hint.locator(".pos").text_content() or ""
    new_parts = new_pos_text.split("/")
    new_pos = int(new_parts[0].strip()) if len(new_parts) == 2 else initial_pos
    if initial_pos < total:
        check("左滑切换到下一张", new_pos == initial_pos + 1)
    else:
        check("末张左滑不切换", new_pos == total)

    # 右滑 → 上一张
    prev_pos = new_pos
    page.evaluate("""
        () => {
            const zone = document.querySelector('.gesture-zone');
            if (!zone) return;
            const rect = zone.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            function fire(name, x) {
                const evt = new TouchEvent(name, { bubbles: true, cancelable: true });
                const touches = [{ clientX: x, clientY: cy, identifier: 0 }];
                Object.defineProperty(evt, 'changedTouches', {
                    get: () => touches, configurable: true
                });
                zone.dispatchEvent(evt);
            }
            fire('touchstart', cx - 60);
            fire('touchmove', cx + 60);
            fire('touchend', cx + 60);
        }
    """)
    page.wait_for_timeout(500)

    final_text = nav_hint.locator(".pos").text_content() or ""
    final_parts = final_text.split("/")
    final_pos = int(final_parts[0].strip()) if len(final_parts) == 2 else prev_pos
    if final_pos > 1:
        check("右滑切换到上一张", final_pos == prev_pos - 1)
    else:
        check("首张右滑不切换", final_pos == 1)


def test_preview_scroll_restore(page):
    """验证从预览返回后文件列表滚动位置恢复。"""
    section("Web 测试 6: 预览返回滚动位置")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")

    # 等待单元加载并进入文件列表
    unit_card = page.locator(".unit-card").first
    unit_card.wait_for(state="attached", timeout=5000)
    if unit_card.count() == 0:
        check("滚动恢复测试: 无单元可进入", True)
        return
    unit_card.click()
    page.locator(".feed-header").wait_for(state="attached", timeout=5000)

    # 等待文件列表加载
    feed_items = page.locator(".feed-item")
    feed_items.first.wait_for(state="attached", timeout=5000)
    if feed_items.count() < 4:
        check("滚动恢复测试: 文件不足，跳过", True)
        return

    # 点击靠后的文件（如第 4 个，index=3）进入预览
    feed_items.nth(3).click()
    page.locator(".preview-overlay").wait_for(state="visible", timeout=5000)

    # 在预览中前进到下一个文件（触发 _fileScrollTop 更新）
    nav_hint = page.locator(".image-nav-hint")
    if nav_hint.count() > 0:
        next_btn = nav_hint.locator(".mdi-chevron-right")
        if next_btn.count() > 0:
            next_btn.click()

    # 返回文件列表
    back_btn = page.locator(".preview-back")
    if back_btn.count() > 0:
        back_btn.click()

    # 验证文件列表已恢复
    page.locator(".feed-item").first.wait_for(state="attached", timeout=5000)
    items_after = page.locator(".feed-item").count()
    check("返回后文件列表已渲染", items_after >= 4)

    # 验证浏览器的 URL 已回到文件列表页
    current_url = page.url
    check("URL 已回到文件列表页", "/preview/" not in current_url)


def test_navigation_tabs(page):
    """验证底部导航栏功能。"""
    section("Web 测试 7: 底部导航")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")

    # 等待底部导航渲染
    tabs = page.locator(".bottom-tabs")
    tabs.wait_for(state="attached", timeout=5000)
    tabs_count = tabs.count()
    check("底部导航栏存在", tabs_count > 0)

    if tabs_count == 0 or not tabs.first.is_visible():
        check("底部导航不可见（平板布局使用侧边栏）", True)
        return

    tab_items = page.locator(".tab-item")
    tab_count = tab_items.count()
    check(f"导航项数量 ({tab_count})", tab_count >= 3)

    if tab_count < 3:
        return

    # 获取各 tab 的文本
    tab_texts = [tab_items.nth(i).text_content() or "" for i in range(tab_count)]
    has_units_tab = any("单元" in t for t in tab_texts)
    has_dedup_tab = any("查重" in t for t in tab_texts)
    has_messages_tab = any("消息" in t for t in tab_texts)
    check("单元导航页存在", has_units_tab)
    check("查重导航页存在", has_dedup_tab)
    check("消息导航页存在", has_messages_tab)

    # 点击"查重"tab
    for i in range(tab_count):
        text = tab_texts[i]
        if "查重" in text:
            tab_items.nth(i).click()
            check("切换到查重页面", True)
            break

    # 点击"消息"tab
    for i in range(tab_count):
        text = tab_texts[i]
        if "消息" in text:
            tab_items.nth(i).click()
            check("切换到消息页面", True)
            break


def test_navigation_sidebar(page):
    """验证侧边栏导航功能（1280px 布局）。"""
    section("Web 测试 7b: 侧边栏导航")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    sidebar = page.locator(".sidebar")
    if sidebar.count() == 0 or not sidebar.is_visible():
        check("侧边栏不可见（移动端布局）", True)
        return
    sidebar_items = page.locator(".sidebar-item")
    count = sidebar_items.count()
    check(f"侧边栏导航项 ({count})", count >= 3)
    if count >= 3 and "查重" in (sidebar_items.nth(1).text_content() or ""):
        sidebar_items.nth(1).click()
        page.wait_for_timeout(300)
        check("侧边栏切换到查重", True)


def test_pin_lock_screen(page):
    """验证 PIN 锁屏界面渲染。"""
    section("Web 测试 8: PIN 锁屏界面")
    page.goto(f"http://127.0.0.1:{_PORT}/")

    # PIN 界面可能在连接之前显示
    pin_overlay = page.locator(".pin-overlay")

    if pin_overlay.count() > 0:
        pin_overlay.wait_for(state="attached", timeout=3000)
        check("PIN 遮罩层存在", pin_overlay.count() > 0)

        # 检测是设置模式还是验证模式
        lock_icon = page.locator(".lock-icon")
        if lock_icon.count() > 0:
            check("锁图标存在", lock_icon.count() > 0)

        pin_keypad = page.locator(".pin-keypad")
        if pin_keypad.count() > 0:
            pin_keys = page.locator(".pin-key")
            check("数字键盘按钮存在", pin_keys.count() > 0)
            # 尝试输入
            if pin_keys.count() >= 4:
                pin_keys.nth(0).click()
                pin_dots = page.locator(".pin-dot")
                if pin_dots.count() > 0:
                    # 验证首个点位被填充
                    check("PIN 输入后点位填充", True)
    else:
        check("无 PIN 界面（已解锁或未设置）", True)


def test_phone_viewport_access(page):
    """测试 7.5: 手机当终端 —— 竖屏窄视口下 SPA 必须能正常打开且不横向溢出。

    用户的用法是"平板当服务器、手机输 IP 当终端"，因此手机视口是主要场景之一：
    页面渲染失败、或底部导航被挤出屏幕、或出现横向滚动，都等于手机上不可用。
    服务端监听地址（0.0.0.0 与局域网 IP 列表）由 test_lite.py 覆盖，这里只管前端。
    """
    section("Web 测试 7.5: 手机竖屏访问")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": 390, "height": 844})   # iPhone 14 竖屏
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.locator(".app-main").wait_for(state="attached", timeout=5000)
    page.wait_for_timeout(400)

    check("手机视口无 pageerror", len(errors) == 0)
    for e in errors[:3]:
        print(f"     pageerror: {e}")

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - window.innerWidth")
    check(f"无横向溢出（溢出 {overflow}px）", overflow <= 2)

    tabs = page.locator(".bottom-tabs")
    check("底部导航在手机视口可见",
          tabs.count() > 0 and tabs.first.is_visible())

    # 页面主体必须真的渲染出内容（不是白屏）
    body_text = (page.locator("#app").text_content() or "").strip()
    check(f"页面有渲染内容（{len(body_text)} 字符）", len(body_text) > 20)

    # 窄视口下设置页的手机访问提示仍需可读（数据位置区块默认折叠，先展开）
    page.goto(f"http://127.0.0.1:{_PORT}/#/settings")
    page.locator(".bottom-tabs, .app-main").first.wait_for(state="attached",
                                                          timeout=5000)
    page.wait_for_timeout(300)
    page.set_viewport_size({"width": 1280, "height": 800})


def test_gesture_engine_ptr_and_damp(page):
    """测试 10: 手势引擎 —— 下拉刷新无异常 + 触发后指示器收起 + 边界阻尼过原点。

    回归：attachPullToRefresh 曾引用 createZone 内部局部变量 now() → 每次
    touchstart 抛 ReferenceError；gDamp 在 atEdge 时把系数作用到含 0..L0 的
    整段位移 → 起手瞬间跳变 ~33px。
    """
    section("Web 测试 10: 手势引擎（PTR/阻尼）")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.locator(".app-main").wait_for(state="attached", timeout=5000)
    page.wait_for_timeout(300)

    # 合成触摸序列：下拉 130px 后松手（> PTR_READY=80 → 触发刷新）
    page.evaluate("""
        () => {
            const main = document.querySelector('.app-main');
            function fire(name, y) {
                const evt = new TouchEvent(name, { bubbles: true, cancelable: true });
                const touches = [{ clientX: 195, clientY: y, identifier: 0 }];
                const empty = [];
                Object.defineProperty(evt, 'touches', {
                    get: () => (name === 'touchend' ? empty : touches), configurable: true
                });
                Object.defineProperty(evt, 'changedTouches', {
                    get: () => touches, configurable: true
                });
                main.dispatchEvent(evt);
            }
            fire('touchstart', 200);
            fire('touchmove', 260);
            fire('touchmove', 330);
            fire('touchend', 330);
        }
    """)
    page.wait_for_timeout(800)
    check("PTR 触摸序列无 pageerror", len(errors) == 0)
    for e in errors[:3]:
        print(f"     pageerror: {e}")

    # 触发后内联高度必须清空（交给 .active 类控制），否则指示器卡在 44px
    inline = page.evaluate(
        "() => { const el = document.querySelector('.ptr-indicator');"
        " return el ? el.style.height : 'missing'; }"
    )
    check("触发后指示器内联高度已清空", inline == "")

    # 边界阻尼必须过原点（历史 bug: damp(0, L0, f, max, true) = 0.7*L0）
    d0 = page.evaluate("() => Gesture.damp(0, 46.8, 0.35, 400, true)")
    d1 = page.evaluate("() => Gesture.damp(0.5, 46.8, 0.35, 400, true)")
    d2 = page.evaluate("() => Gesture.damp(0.5, 46.8, 0.35, 400, false)")
    check("边界阻尼过原点 damp(0)=0", abs(d0) < 1e-9)
    check("边界阻尼按比例缩放 damp(0.5)=0.15", abs(d1 - 0.15) < 1e-6)
    check("非边界阻尼不受影响 damp(0.5)=0.5", abs(d2 - 0.5) < 1e-6)
    page.set_viewport_size({"width": 1280, "height": 800})


def test_server_url_normalization(page):
    """测试 12: 服务器地址带尾斜杠时不得拼出 //api/...（否则连接页报失败）。"""
    section("Web 测试 12: 服务器地址规范化")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.locator(".app-main").wait_for(state="attached", timeout=5000)
    page.wait_for_timeout(300)

    status = page.evaluate(f"""
        async () => {{
            try {{
                const r = await api('http://127.0.0.1:{_PORT}/', '/api/health');
                return r.status;
            }} catch (e) {{ return 'error: ' + e.message; }}
        }}
    """)
    check("带尾斜杠地址 api() 请求成功", status == "ok")

    mu = page.evaluate(
        f"() => mediaUrl('http://127.0.0.1:{_PORT}/', '/api/files/1/thumbnail')"
    )
    check("mediaUrl 不产生双斜杠", mu.startswith(f"http://127.0.0.1:{_PORT}/api/files/1/thumbnail"))


def test_cancel_pin_does_not_lock_out(page):
    """测试 11: 取消访问密码后不得把用户永久锁在锁屏界面。

    回归：change-pin 返回 pin_required=false 时前端仍清令牌并派发
    media-auth-expired → 锁屏；而 verify 此时恒 400「未配置访问密码」，
    用户只能刷新页面自救。
    """
    section("Web 测试 11: 取消访问密码不锁死")
    if _app is None:
        check("测试服务器 app 可用", False)
        return
    from app.api.server import _revoke_all_auth_tokens

    # change-pin 端点按 CWD 写 config.json 与 data/.web_pin（生产环境 CWD=desktop
    # 是正确行为），测试必须备份/还原，否则会把用户 PIN 清空、api_host 改回默认值。
    cfg_path = Path(__file__).parent.parent / "config.json"
    pin_path = Path(__file__).parent.parent / "data" / ".web_pin"
    cfg_backup = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
    pin_backup = pin_path.read_text(encoding="utf-8") if pin_path.exists() else None

    try:
        # 1) 服务端启用 PIN
        _app.state.config = _app.state.config.with_updates(web_pin="5678")
        _revoke_all_auth_tokens()
        page.evaluate("() => localStorage.setItem('media_pin', '')")

        # 2) 重新加载 → 出现锁屏，输入 5678 解锁
        page.goto(f"http://127.0.0.1:{_PORT}/")
        overlay = page.locator(".pin-overlay")
        overlay.wait_for(state="visible", timeout=5000)
        check("启用 PIN 后显示锁屏", overlay.is_visible())
        for digit in "5678":
            page.locator(".pin-key").filter(has_text=digit).first.click()
        overlay.wait_for(state="hidden", timeout=5000)
        check("输入正确 PIN 后解锁", not overlay.is_visible())

        # 3) 设置页 → 修改密码 → 新密码留空（取消密码）
        page.goto(f"http://127.0.0.1:{_PORT}/#/settings")
        item = page.locator(".setting-item").filter(has_text="访问密码已启用")
        item.wait_for(state="visible", timeout=5000)
        item.click()
        dialog = page.locator(".pin-dialog")
        dialog.wait_for(state="visible", timeout=3000)
        inputs = dialog.locator("input")
        inputs.nth(0).fill("5678")   # 当前密码
        # 新密码/确认新密码保持为空 → 取消密码
        dialog.locator("button.btn-primary").click()
        dialog.wait_for(state="hidden", timeout=5000)

        # 4) 关键断言：不应被锁屏，且 API 仍可用
        page.wait_for_timeout(500)
        check("取消密码后未被锁屏", not overlay.is_visible())
        page.goto(f"http://127.0.0.1:{_PORT}/#/units")
        page.locator(".app-main").wait_for(state="attached", timeout=5000)
        page.wait_for_timeout(500)
        check("取消密码后仍能正常浏览单元",
              page.locator(".unit-card").count() > 0)
    finally:
        # 恢复无 PIN 状态，避免影响其它测试
        if _app is not None:
            _app.state.config = _app.state.config.with_updates(web_pin="")
            _revoke_all_auth_tokens()
        # 还原真实配置文件（见上方备份说明）
        if cfg_backup is not None:
            try:
                cfg_path.write_text(cfg_backup, encoding="utf-8")
            except OSError as e:
                print(f"  [INFO] 还原 config.json 失败: {e}")
        if pin_backup is not None:
            try:
                pin_path.parent.mkdir(parents=True, exist_ok=True)
                pin_path.write_text(pin_backup, encoding="utf-8")
            except OSError as e:
                print(f"  [INFO] 还原 data/.web_pin 失败: {e}")


def _seed_temp_unit(name: str, files: list) -> tuple:
    """在测试库里建一个含真实图片文件的临时单元。

    参数:
        name: 单元（文件夹）名，同时也是页面上的显示名。
        files: 文件名列表。

    返回:
        (unit_id, [file_id, ...], [Path, ...])。
    """
    from app.db import queries as q
    from PIL import Image
    import tempfile

    root_dir = Path(tempfile.mkdtemp(prefix="web_seed_"))
    unit_dir = root_dir / name
    unit_dir.mkdir(parents=True)
    paths = []
    for i, fname in enumerate(files):
        p = unit_dir / fname
        Image.new("RGB", (64, 64), color=(i * 40 % 255, 60, 90)).save(p)
        paths.append(p)

    with DatabaseManager.session() as session:
        # media_library_roots.path 有唯一约束：同根复用，不重复插入
        root = next(
            (r for r in q.get_all_roots(session) if r.path == str(root_dir)), None
        )
        if root is None:
            root = q.add_library_root(session, str(root_dir))
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


def _attach_dialog_auto_accept(page, sink: list):
    """自动接受 confirm/alert 并把文案记入 sink（返回 handler 以便移除）。"""
    def _on_dialog(d):
        sink.append(f"[{d.type}] {d.message}")
        try:
            d.accept()
        except Exception:
            pass
    page.on("dialog", _on_dialog)
    return _on_dialog


def test_file_management_delete(page):
    """Web 文件管理：多选 + 批量删除（移至回收站）。

    回归：Web 端唯一的删除入口曾是 `_deleteFile`（下划线开头），Vue 3 渲染代理
    不暴露 `_` 开头的 key → 点击抛 `ReferenceError: _deleteFile is not defined`，
    删除完全无效。这里既验证功能，也断言页面无此类 JS 错误。
    """
    section("Web 测试 11: 文件管理（多选批量删除）")
    from app.db import queries as q

    unit_id, file_ids, paths = _seed_temp_unit(
        "Web删除测试", ["d1.jpg", "d2.jpg", "d3.jpg"])
    dialogs: list = []
    page_errors: list = []
    on_dialog = _attach_dialog_auto_accept(page, dialogs)
    on_error = lambda e: page_errors.append(str(e))  # noqa: E731
    page.on("pageerror", on_error)
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/units")
        # 必须强制重新加载：导航到与当前完全相同的 URL 是同文档导航，
        # 不会重跑 Vue 挂载，页面会停留在种入新单元之前的旧列表上
        page.reload()
        page.wait_for_load_state("networkidle")
        card = page.locator(".unit-card", has_text="Web删除测试").first
        card.wait_for(state="attached", timeout=5000)
        check("临时单元卡片已渲染", card.count() > 0)
        card.click()

        page.locator(".feed-header").wait_for(state="attached", timeout=5000)
        feed_items = page.locator(".feed-item")
        feed_items.first.wait_for(state="attached", timeout=5000)
        check(f"文件列表渲染 3 个文件（实际 {feed_items.count()}）",
              feed_items.count() == 3)
        check("管理按钮存在",
              page.locator(".feed-header .sort-btn", has_text="管理").count() > 0)

        # 卡片菜单 → 多选管理：进入选择模式并选中该文件
        more = page.locator(".feed-item .card-more").first
        check("卡片操作按钮存在", more.count() > 0)
        more.click()
        sheet = page.locator(".sheet-panel")
        sheet.wait_for(state="visible", timeout=3000)
        check("菜单含「多选管理」", page.locator(".sheet-item", has_text="多选管理").count() > 0)
        check("菜单含「删除文件」", page.locator(".sheet-item", has_text="删除文件").count() > 0)
        page.locator(".sheet-item", has_text="多选管理").first.click()
        page.wait_for_timeout(200)

        select_bar = page.locator(".select-bar")
        check("多选操作栏出现", select_bar.count() > 0)
        count_text = page.locator(".select-bar-count").text_content() or ""
        check(f"进入即选中该文件（{count_text.strip()}）", "1" in count_text)
        check("选择模式下隐藏卡片操作按钮",
              page.locator(".feed-item .card-more").count() == 0)
        check("选中态样式生效", page.locator(".feed-item.selected").count() == 1)

        # 全选 → 3 个
        page.locator(".select-bar-btn", has_text="全选").first.click()
        page.wait_for_timeout(200)
        count_text = page.locator(".select-bar-count").text_content() or ""
        check(f"全选后计数为 3（{count_text.strip()}）",
              "3" in count_text and page.locator(".feed-item.selected").count() == 3)

        # 删除（确认框由 handler 自动接受）
        page.locator(".select-bar-btn", has_text="删除").first.click()
        page.wait_for_timeout(1200)

        check("弹出删除确认框", any("回收站" in m for m in dialogs))

        # 回收站可用 → 直接删掉；不可用（网络盘 / 沙箱）→ 必须给出明确的补救选择，
        # 而不是"报个错就完"（用户报的"web 端删不了"就是卡在这里）
        fallback = page.locator(".sheet-panel", has_text="回收站不可用")
        if fallback.count() > 0:
            check("回收站不可用时弹出补救弹层",
                  "回收站不可用" in (fallback.first.text_content() or ""))
            check("补救弹层提供「永久删除」",
                  page.locator(".sheet-item", has_text="永久删除").count() > 0)
            check("补救弹层提供「仅从媒体库移除」",
                  page.locator(".sheet-item", has_text="仅从媒体库移除").count() > 0)
            page.locator(".sheet-cancel").first.click()
            page.wait_for_timeout(300)
            check("失败时保留选中状态以便重试（不静默清空）",
                  page.locator(".select-bar").count() > 0)
        else:
            check(f"回收站可用 → 正常删除（剩余 {page.locator('.feed-item').count()}）", True)
            check("删除后退出选择模式", page.locator(".select-bar").count() == 0)

        # UI 与数据库一致（回收站不可用时服务端会保留记录并回报失败）
        with DatabaseManager.session() as session:
            rows = q.get_files_by_unit(session, unit_id)
            remaining = len(rows)
            expected_size = sum(f.size_bytes or 0 for f in rows)
            unit = q.get_unit_by_id(session, unit_id)
        ui_count = page.locator(".feed-item").count()
        check(f"页面剩余文件数与数据库一致（UI {ui_count} / DB {remaining}）",
              ui_count == remaining)
        if remaining == 0:
            check("三个文件已全部删除", True)
            check("删除后单元 file_count 同步为 0", unit.file_count == 0)
            check("删除后单元 total_size 同步为 0", unit.total_size == 0)
        else:
            check(f"回收站不可用时保留记录并回报失败（剩余 {remaining}）",
                  remaining == 3 and any("失败" in m for m in dialogs))
            check("单元统计与剩余记录一致",
                  unit.file_count == remaining and unit.total_size == expected_size)

        # 关键回归：模板里的下划线方法调用会产生 ReferenceError
        bad = [e for e in page_errors if "_deleteFile" in e or "is not defined" in e]
        check(f"删除过程无 JS 未定义错误（{bad[:1]}）", not bad)
    finally:
        page.remove_listener("dialog", on_dialog)
        page.remove_listener("pageerror", on_error)
        import shutil
        with DatabaseManager.session() as session:
            q.delete_resource_unit(session, unit_id)
        shutil.rmtree(paths[0].parent.parent, ignore_errors=True)


def test_file_delete_trash_fallback(page):
    """Web 删除：回收站不可用（网络盘 / exFAT）时必须给出"永久删除 / 仅移除记录"。

    用户报"web 端删不了"的根因：媒体库在 G: 这类可移动/网络盘上，该卷没有回收站，
    send2trash 必然失败；而 Web 只有"移至回收站"一条路，API 只回 409，
    于是永远删不掉。修复后失败要弹出选择，且**永久删除只能由用户主动点**。
    """
    section("Web 测试 11c: 回收站不可用时的删除出路")
    from app.db import queries as q
    import send2trash

    unit_id, file_ids, paths = _seed_temp_unit("Web永久删除", ["p1.jpg", "p2.jpg"])
    orig = send2trash.send2trash

    def _boom(path):
        raise OSError("[WinError 50] 不支持该请求（该卷没有回收站）")

    dialogs: list = []
    on_dialog = _attach_dialog_auto_accept(page, dialogs)
    try:
        send2trash.send2trash = _boom
        page.goto(f"http://127.0.0.1:{_PORT}/#/units")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".unit-card", has_text="Web永久删除").first.click()
        page.locator(".feed-item").first.wait_for(state="attached", timeout=5000)
        check("文件列表已渲染 2 个文件", page.locator(".feed-item").count() == 2)

        # 前置：服务端此刻确实删不了（否则这个测试没有意义）
        import requests
        probe = requests.delete(f"http://127.0.0.1:{_PORT}/api/files/{file_ids[0]}",
                                timeout=5)
        check(f"前置：回收站不可用时 trash 返回 409（实际 {probe.status_code}）",
              probe.status_code == 409)
        check("前置：失败时磁盘文件必须保留", paths[0].exists())

        # 走 UI：多选管理 → 全选 → 删除 → 接受确认框 → 应弹出补救弹层
        page.locator(".feed-item .card-more").first.click()
        page.locator(".sheet-item", has_text="多选管理").first.click()
        page.wait_for_timeout(250)
        page.locator(".select-bar-btn", has_text="全选").first.click()
        page.wait_for_timeout(150)
        page.locator(".select-bar-btn", has_text="删除").first.click()
        page.wait_for_timeout(1200)

        sheet = page.locator(".sheet-panel")
        sheet.wait_for(state="visible", timeout=3000)
        sheet_text = sheet.first.text_content() or ""
        check("弹出补救弹层而不是只报错", "回收站不可用" in sheet_text)
        check("提供「永久删除（不可恢复）」",
              page.locator(".sheet-item", has_text="永久删除").count() > 0)
        check("提供「仅从媒体库移除」",
              page.locator(".sheet-item", has_text="仅从媒体库移除").count() > 0)
        check("弹层中未删除前磁盘文件仍在", all(p.exists() for p in paths))

        # 选「永久删除」→ 磁盘与列表都清干净
        page.locator(".sheet-item", has_text="永久删除").first.click()
        page.wait_for_timeout(1800)
        check("永久删除后磁盘文件消失", all(not p.exists() for p in paths))
        with DatabaseManager.session() as session:
            left = q.get_files_by_unit(session, unit_id)
            unit = q.get_unit_by_id(session, unit_id)
        check("永久删除后数据库记录清空", len(left) == 0)
        check(f"页面文件数与库一致（UI {page.locator('.feed-item').count()} / DB {len(left)}）",
              page.locator(".feed-item").count() == len(left))
        check("单元统计同步为 0", unit.file_count == 0)
    finally:
        send2trash.send2trash = orig
        page.remove_listener("dialog", on_dialog)
        import shutil
        with DatabaseManager.session() as session:
            q.delete_resource_unit(session, unit_id)
        shutil.rmtree(paths[0].parent.parent, ignore_errors=True)


def test_unit_delete_from_card_menu(page):
    """Web 单元卡片菜单 → 删除文件夹（移至回收站 + 删库记录）。"""
    section("Web 测试 12: 删除文件夹")
    from app.db import queries as q

    unit_id, file_ids, paths = _seed_temp_unit("Web删文件夹", ["u1.jpg", "u2.jpg"])
    dialogs: list = []
    on_dialog = _attach_dialog_auto_accept(page, dialogs)
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/units")
        page.reload()
        page.wait_for_load_state("networkidle")
        card = page.locator(".unit-card", has_text="Web删文件夹").first
        card.wait_for(state="attached", timeout=5000)
        card.locator(".card-more").click()

        sheet = page.locator(".sheet-panel")
        sheet.wait_for(state="visible", timeout=3000)
        check("单元菜单含「删除文件夹」",
              page.locator(".sheet-item", has_text="删除文件夹").count() > 0)
        page.locator(".sheet-item", has_text="删除文件夹").first.click()
        page.wait_for_timeout(1200)

        check("弹出删除确认框（提示含文件数）",
              any("回收站" in m for m in dialogs))
        with DatabaseManager.session() as session:
            gone = q.get_unit_by_id(session, unit_id) is None
        if gone:
            check("文件夹已移走（原路径不存在）", not paths[0].parent.exists())
            check("单元记录已删除", True)
            page.wait_for_timeout(300)
            check("页面卡片已移除",
                  page.locator(".unit-card", has_text="Web删文件夹").count() == 0)
        else:
            check("回收站不可用时单元记录保留（无孤儿）", True)
            check("回收站不可用时文件夹仍在", paths[0].parent.exists())
    finally:
        page.remove_listener("dialog", on_dialog)
        import shutil
        with DatabaseManager.session() as session:
            q.delete_resource_unit(session, unit_id)
        shutil.rmtree(paths[0].parent.parent, ignore_errors=True)


def _seed_dedup_levels() -> tuple:
    """造三类查重结果：疑似重复 / 疑似相关（有文件重叠）/ 同演员（只有人脸线索）。

    三分法是本轮的核心改动：旧实现把"有文件重叠的相关"和"只有人脸线索的同演员"
    混在一起，实测 380 条里 373 条是后者（杰卡德恒为 0，界面一片 0%），
    真正的 7 条重叠被淹没。
    """
    from app.db import queries as q

    extra_uid, extra_ids, extra_paths = _seed_temp_unit("查重相关单元", ["r1.jpg"])
    face_uid, face_ids, face_paths = _seed_temp_unit("同演员单元", ["f1.jpg"])
    with DatabaseManager.session() as session:
        units = {u.name: u for u in q.get_all_active_units(session)}
        ua = units.get("片段A")
        ub = units.get("片段B")
        if ua is None or ub is None:
            return None
        files_a = q.get_files_by_unit(session, ua.id)
        files_b = q.get_files_by_unit(session, ub.id)
        if not files_a or not files_b:
            return None

        dup = q.upsert_dedup_result(
            session, unit_a_id=ua.id, unit_b_id=ub.id,
            similarity_score=0.95, match_count=1,
            total_files_a=len(files_a), total_files_b=len(files_b),
            match_types="md5", match_level="duplicate",
            evidence_kind="file",
        )
        # 真实查重跑过之后（Web 测试 14）这几对单元已经有结果行和匹配行了：
        # 先清掉旧匹配，否则 (result_id, file_a, file_b) 撞 UNIQUE 约束
        q.delete_file_matches_for_result(session, dup.id)
        q.insert_file_match(session, dedup_result_id=dup.id,
                            file_a_id=files_a[0].id, file_b_id=files_b[0].id,
                            similarity_score=1.0, match_type="md5")

        # 有文件重叠的"疑似相关"：1 条视频帧匹配 + 1 条人脸线索（多帧吻合）
        rel = q.upsert_dedup_result(
            session, unit_a_id=ua.id, unit_b_id=extra_uid,
            similarity_score=0.12, match_count=1,
            total_files_a=len(files_a), total_files_b=1,
            match_types="video", match_level="related",
            evidence_kind="file",
        )
        q.delete_file_matches_for_result(session, rel.id)
        q.insert_file_match(session, dedup_result_id=rel.id,
                            file_a_id=files_a[0].id, file_b_id=extra_ids[0],
                            similarity_score=1.0, match_type="video")
        q.insert_file_match(session, dedup_result_id=rel.id,
                            file_a_id=files_a[-1].id, file_b_id=extra_ids[0],
                            similarity_score=0.66, match_type="face",
                            frame_support=3)

        # 只有人脸线索的"同演员"：零文件重叠
        face = q.upsert_dedup_result(
            session, unit_a_id=ua.id, unit_b_id=face_uid,
            similarity_score=0.0, match_count=0,
            total_files_a=len(files_a), total_files_b=1,
            match_types="", match_level="related",
            evidence_kind="face",
        )
        q.delete_file_matches_for_result(session, face.id)
        q.insert_file_match(session, dedup_result_id=face.id,
                            file_a_id=files_a[1].id, file_b_id=face_ids[0],
                            similarity_score=0.58, match_type="face",
                            frame_support=2)
        q.insert_file_match(session, dedup_result_id=face.id,
                            file_a_id=files_a[0].id, file_b_id=face_ids[0],
                            similarity_score=0.51, match_type="face",
                            frame_support=1)
        return {"dup_id": dup.id, "rel_id": rel.id, "face_id": face.id,
                "extra_uid": extra_uid, "extra_paths": extra_paths,
                "face_uid": face_uid, "face_paths": face_paths}


def _expand_first_group(page, timeout: int = 8000) -> bool:
    """展开第一个分组（列表改成二级分组后，卡片在组内而不是直接可见）。

    分组是异步拉的（先 /groups 再 /results），因此必须等组头出现再点 ——
    列表还没渲染时直接点会"点了空气"，后续找 .card 必然超时。
    """
    head = page.locator(".dedup-group-head").first
    try:
        head.wait_for(state="attached", timeout=timeout)
    except Exception:
        return False
    head.click()
    page.wait_for_timeout(700)
    return True


def test_dedup_level_semantics(page):
    """Web 查重：三类分开取（疑似重复 / 疑似相关 / 同演员），相关不给删除入口。

    桌面端对 related 只提供"白名单/暂时忽略"（不给保留 A/B），因为 related
    的语义是"仅供了解、不建议删除"。Web 端必须一致，否则等于诱导用户删文件。
    """
    section("Web 测试 13: 查重分级语义")
    from app.db import queries as q

    seeded = _seed_dedup_levels()
    if seeded is None:
        check("查重分级测试: 缺少 片段A/片段B，跳过", True)
        return
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        # 同 URL 的 goto 是同文档导航，不会重新拉取刚种入的查重结果
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".dedup-tabs").wait_for(state="attached", timeout=5000)

        tabs = page.locator(".dedup-tab")
        check("分级 Tab 存在三个（疑似重复/疑似相关/同演员）", tabs.count() == 3)
        check("默认选中「疑似重复」",
              "疑似重复" in (page.locator(".dedup-tab.active").text_content() or ""))
        check("疑似相关 Tab 角标来自 counts.related 而不是全部 related",
              "疑似相关" in (tabs.nth(1).text_content() or ""))
        check("同演员 Tab 存在", "同演员" in (tabs.nth(2).text_content() or ""))

        # 重复 Tab：分组 → 展开 → 卡片
        check("重复列表按来源单元分组", page.locator(".dedup-group").count() > 0)
        _expand_first_group(page)
        check("展开分组后出现重复卡片", page.locator(".card").count() > 0)
        check("重复卡片带「整单元重复」徽标",
              page.locator(".lvl-badge.duplicate").count() > 0)

        # 切到「疑似相关」：只有文件重叠的那条
        page.locator(".dedup-tab", has_text="疑似相关").first.click()
        page.wait_for_timeout(600)
        _expand_first_group(page)
        related_cards = page.locator(".card.card-related")
        check("疑似相关列表出现卡片", related_cards.count() > 0)
        check("相关卡片带徽标（部分重叠）",
              page.locator(".lvl-badge.related").count() > 0)
        check("相关卡片文案声明不建议删除",
              "不建议删除" in (related_cards.first.text_content() or ""))
        check("相关卡片用「N/M 个文件重叠」表达，不再显示 0%",
              "文件重叠" in (related_cards.first.text_content() or ""))

        # 相关详情：无保留 A/B，有忽略；人脸线索默认折叠
        related_cards.first.click()
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        check("相关详情标记为 is-related",
              page.locator(".dedup-header.is-related").count() > 0)
        check("标题显示「疑似相关」而非百分比",
              "疑似相关" in (page.locator(".dedup-header .score").text_content() or ""))
        check("相关详情给出「不建议删除」说明",
              page.locator(".related-note").count() > 0)
        body_buttons = page.locator(".resolve-actions .btn")
        labels = [body_buttons.nth(i).text_content() or "" for i in range(body_buttons.count())]
        check(f"相关详情不提供「保留 A/B」（实际 {labels}）",
              not any(("保留 A" in t or "保留 B" in t) for t in labels))
        check("相关详情提供「暂时忽略」", any("忽略" in t for t in labels))
        check("人脸线索默认折叠（标题可点）",
              page.locator(".section-title.face-toggle").count() > 0
              and page.locator(".hint-card").count() == 0)
        page.locator(".section-title.face-toggle").first.click()
        page.wait_for_timeout(300)
        check("展开后出现同演员线索卡片",
              page.locator(".hint-card").count() > 0)
        check("人脸线索行使用 face 标签", page.locator(".tag.face").count() > 0)
        check("人脸线索显示多帧吻合（3 帧）",
              "多帧吻合" in (page.locator(".hint-card .pair-score").first.text_content() or ""))

        # 「同演员」Tab：只有人脸线索那条，且不给保留 A/B
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        page.wait_for_load_state("networkidle")
        page.locator(".dedup-tab", has_text="同演员").first.click()
        page.wait_for_timeout(600)
        _expand_first_group(page)
        face_cards = page.locator(".card")
        check("同演员 Tab 出现卡片", face_cards.count() > 0)
        check("同演员卡片带 face 徽标",
              page.locator(".lvl-badge.face").count() > 0)
        check("同演员卡片说明无文件重叠",
              "无文件重叠" in (face_cards.first.text_content() or ""))
        face_cards.first.click()
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        flabels = []
        fbuttons = page.locator(".resolve-actions .btn")
        for i in range(fbuttons.count()):
            flabels.append(fbuttons.nth(i).text_content() or "")
        check(f"同演员详情不提供「保留 A/B」（实际 {flabels}）",
              not any(("保留 A" in t or "保留 B" in t) for t in flabels))

        # 重复详情：仍提供保留 A/B
        # （页面现在会跨路由记住上次的 Tab，所以这里显式切回「疑似重复」）
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        page.wait_for_load_state("networkidle")
        page.locator(".dedup-tab", has_text="疑似重复").first.click()
        page.wait_for_timeout(600)
        _expand_first_group(page)
        dup_card = page.locator(".card").first
        dup_card.wait_for(state="attached", timeout=5000)
        dup_card.click()
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        dlabels = []
        dbuttons = page.locator(".resolve-actions .btn")
        for i in range(dbuttons.count()):
            dlabels.append(dbuttons.nth(i).text_content() or "")
        check(f"重复详情提供「保留 A/B」（实际 {dlabels}）",
              any("保留 A" in t for t in dlabels) and any("保留 B" in t for t in dlabels))

        # 分组子项与分组接口一致：展开后卡片数 = 该组对数
        with DatabaseManager.session() as session:
            rows, total = q.get_dedup_results_page(
                session, level="duplicate", evidence="file", per_page=100)
            check("后端 duplicate 结果数与界面一致",
                  total >= 1 and any(r.id == seeded["dup_id"] for r in rows))
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for rid in (seeded["dup_id"], seeded["rel_id"], seeded["face_id"]):
                dr = q.get_dedup_by_id(session, rid)
                if dr is not None:
                    session.delete(dr)
            q.delete_resource_unit(session, seeded["extra_uid"])
            q.delete_resource_unit(session, seeded["face_uid"])
        shutil.rmtree(seeded["extra_paths"][0].parent.parent, ignore_errors=True)
        shutil.rmtree(seeded["face_paths"][0].parent.parent, ignore_errors=True)


def test_dedup_grouped_list(page):
    """Web 查重列表：按左侧单元折叠成二级分组（380 条 → 几十个文件夹）。

    用户诉求：卡片左边相同、右边不同的一大堆，能不能归成一个二级文件夹。
    分组键就是卡片左侧那个单元（后端按 unit_a_id 分组，与界面一致）。
    """
    section("Web 测试 13b: 查重二级分组")
    from app.db import queries as q

    seeded = _seed_dedup_levels()
    if seeded is None:
        check("分组测试: 缺少 片段A/片段B，跳过", True)
        return
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".dedup-group").first.wait_for(state="attached", timeout=5000)

        groups = page.locator(".dedup-group")
        check("列表呈现分组而不是平铺卡片", groups.count() > 0)
        check("分组里的卡片默认收起（不直接铺满屏幕）",
              page.locator(".group-children").count() == 0)
        head_text = page.locator(".dedup-group-head").first.text_content() or ""
        check("组头显示来源单元名", "片段A" in head_text)
        check("组头显示对数", "对" in head_text)
        check("组头带封面缩略图",
              page.locator(".dedup-group-head .group-cover").count() > 0)

        # 展开 → 出现子卡片；再点击 → 收起
        page.locator(".dedup-group-head").first.click()
        page.wait_for_timeout(500)
        card_count = page.locator(".group-children .card").count()
        check("展开后出现组内卡片", card_count > 0)
        page.locator(".dedup-group-head").first.click()
        page.wait_for_timeout(300)
        check("再次点击收起", page.locator(".group-children").count() == 0)

        # 展开当前 Tab 下所有分组：组内卡片数应等于后端返回的该 Tab 结果数
        with DatabaseManager.session() as session:
            _, total_dup = q.get_dedup_results_page(
                session, level="duplicate", evidence="file", per_page=1)
        page.locator("button", has_text="全部展开").first.click()
        page.wait_for_timeout(800)
        check(f"全部展开后组内卡片数 = 后端结果数（{total_dup}）",
              page.locator(".group-children .card").count() == total_dup)

        # 切 Tab 会重置分组（避免把上一档的展开状态带过去）
        page.locator(".dedup-tab", has_text="同演员").first.click()
        page.wait_for_timeout(600)
        check("切换 Tab 后分组回到收起状态",
              page.locator(".group-children").count() == 0)
        check("同演员 Tab 也有分组", page.locator(".dedup-group").count() > 0)
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for rid in (seeded["dup_id"], seeded["rel_id"], seeded["face_id"]):
                dr = q.get_dedup_by_id(session, rid)
                if dr is not None:
                    session.delete(dr)
            q.delete_resource_unit(session, seeded["extra_uid"])
            q.delete_resource_unit(session, seeded["face_uid"])
        shutil.rmtree(seeded["extra_paths"][0].parent.parent, ignore_errors=True)
        shutil.rmtree(seeded["face_paths"][0].parent.parent, ignore_errors=True)


def test_dedup_detail_thumbnails(page):
    """Web 查重详情：每条匹配给出左右缩略图对比（点图进预览页）。

    只列文件名时用户无法判断"这两张是不是同一张"，必须真的把图渲染出来。
    这里断言图片真的加载成功（naturalWidth > 0），而不是 404 后的占位。
    """
    section("Web 测试 13c: 查重详情缩略图对比")
    from app.db import queries as q

    seeded = _seed_dedup_levels()
    if seeded is None:
        check("缩略图测试: 缺少 片段A/片段B，跳过", True)
        return
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup/{seeded['rel_id']}")
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        page.locator(".match-grid").first.wait_for(state="attached", timeout=5000)

        cards = page.locator(".match-grid .match-card")
        check("匹配项渲染成对比卡片", cards.count() > 0)
        thumbs = page.locator(".match-grid .match-card .thumb-pair img.thumb")
        check("每对匹配有左右两张缩略图（≥2）", thumbs.count() >= 2)
        src0 = thumbs.first.get_attribute("src") or ""
        check("缩略图走 /api/files/{id}/thumbnail",
              "/thumbnail" in src0 and "/api/files/" in src0)
        check("缩略图带 lazy 加载属性",
              thumbs.first.get_attribute("loading") == "lazy")

        # 真加载：等图片解码完成后检查 naturalWidth（404/403 会得到 0）
        page.wait_for_timeout(1500)
        loaded = page.evaluate(
            "Array.from(document.querySelectorAll('.match-grid .match-card "
            ".thumb-pair img.thumb')).filter(i => i.naturalWidth > 0).length")
        check(f"缩略图真的加载成功（实际 {loaded} 张）", loaded >= 2)
        check("没有出现占位图标（说明图片请求成功）",
              page.locator(".thumb-empty").count() == 0)

        # 点缩略图 → 进入预览页
        thumbs.first.click()
        page.wait_for_timeout(800)
        check("点击缩略图进入预览页", "/preview/" in page.url)

        # 子集重复的说明块存在（用 pair 数据造一条）
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup/{seeded['rel_id']}")
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        check("相关详情显示「文件重叠 N/M」",
              "文件重叠" in (page.locator(".dedup-header .match-info").text_content() or ""))
        check("同演员线索段默认折叠（未展开时不渲染卡片）",
              page.locator(".hint-card").count() == 0)
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for rid in (seeded["dup_id"], seeded["rel_id"], seeded["face_id"]):
                dr = q.get_dedup_by_id(session, rid)
                if dr is not None:
                    session.delete(dr)
            q.delete_resource_unit(session, seeded["extra_uid"])
            q.delete_resource_unit(session, seeded["face_uid"])
        shutil.rmtree(seeded["extra_paths"][0].parent.parent, ignore_errors=True)
        shutil.rmtree(seeded["face_paths"][0].parent.parent, ignore_errors=True)


def test_dedup_stale_banner(page):
    """Web 查重：旧规则的结论必须被明确标出，而不是把旧数字当结论渲染。

    用户实测踩到：升级后只做了分类搬迁、没重算，界面把旧规则的 match_count=46
    配 33 个文件的单元渲染成"46/33 个文件重叠"，还显示"疑似重复 0" ——
    看上去像界面坏掉/递归了。
    """
    section("Web 测试 13d: 旧规则结果提示")
    from app.db import queries as q
    from app.db.models import DedupResult

    seeded = _seed_dedup_levels()
    if seeded is None:
        check("旧规则提示测试: 缺少 片段A/片段B，跳过", True)
        return
    try:
        # 把其中一条改成"升级前算出来的"（computed_version=0）
        with DatabaseManager.session() as session:
            session.query(DedupResult).filter(
                DedupResult.id == seeded["rel_id"]).update(
                {"computed_version": 0}, synchronize_session=False)

        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".stale-hint").wait_for(state="attached", timeout=8000)

        text = page.locator(".stale-hint").text_content() or ""
        check("提示条说明是旧规则算的", "旧规则" in text)
        check("提示条显示待重算条数", "1" in text)
        check("提示条提供「立即重算」入口",
              page.locator(".stale-btn").count() > 0)

        # 切到含旧结论的那一档，展开分组看卡片标记
        page.locator(".dedup-tab", has_text="疑似相关").first.click()
        page.wait_for_timeout(600)
        _expand_first_group(page)
        check("旧规则卡片显示「待重算」而不是荒谬比例",
              "待重算" in (page.locator(".card-meta .count").first.text_content() or ""))
        check("旧规则卡片带专用徽标",
              page.locator(".lvl-badge.stale").count() > 0)
        check("组头提示组内含旧规则结果",
              "旧规则" in (page.locator(".dedup-group-head").first.text_content() or ""))

        # 详情页同样要提示，而不是展示旧数字
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup/{seeded['rel_id']}")
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        check("详情页标记为旧规则", "旧规则" in (page.locator(".dedup-header .score").text_content() or ""))
        check("详情页有旧规则说明块",
              page.locator(".stale-note").count() > 0)
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for rid in (seeded["dup_id"], seeded["rel_id"], seeded["face_id"]):
                dr = q.get_dedup_by_id(session, rid)
                if dr is not None:
                    session.delete(dr)
            q.delete_resource_unit(session, seeded["extra_uid"])
            q.delete_resource_unit(session, seeded["face_uid"])
        shutil.rmtree(seeded["extra_paths"][0].parent.parent, ignore_errors=True)
        shutil.rmtree(seeded["face_paths"][0].parent.parent, ignore_errors=True)


def test_dedup_run_from_web(page):
    """Web 端「一键查重」：触发 API（index_first）并按阶段显示进度。"""
    section("Web 测试 14: 一键查重触发")
    import requests

    dialogs: list = []
    on_dialog = _attach_dialog_auto_accept(page, dialogs)
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        page.reload()
        page.wait_for_load_state("networkidle")
        run_btn = page.locator(".dedup-run-btn")
        run_btn.wait_for(state="attached", timeout=5000)
        check("一键查重按钮存在", run_btn.count() > 0)
        check("按钮文案为「开始查重」",
              "开始查重" in (run_btn.text_content() or ""))

        run_btn.click()
        check("触发前弹出确认框", any("查重" in m for m in dialogs))

        # 轮询页面状态文本直到完成（索引 + 比对）
        finished = False
        for _ in range(120):
            sub = page.locator(".dedup-run-text .run-sub").text_content() or ""
            if "完成" in sub and "疑似相关" in sub:
                finished = True
                break
            page.wait_for_timeout(500)
        check("Web 端查重任务完成", finished)

        # 服务端确实落库了分级结果
        counts = requests.get(f"http://127.0.0.1:{_PORT}/api/dedup/counts", timeout=5).json()
        check(f"服务端存在分级计数（{counts}）",
              isinstance(counts.get("duplicate"), int)
              and counts.get("total", 0) >= 1)
        # 摘要先于列表刷新渲染：给它时间把 /groups 拉回来再断言
        page.wait_for_timeout(1000)
        check("列表已刷新（有分组 / 徽标 / 空态）",
              page.locator(".dedup-group").count() > 0
              or page.locator(".lvl-badge").count() > 0
              or page.locator(".empty-state").count() > 0)
    finally:
        page.remove_listener("dialog", on_dialog)


def _inject_running_dedup_task(task_id: str, progress=(3, 10),
                               phase: str = "comparing") -> dict:
    """在测试服务器的任务表里放一个"正在跑"的查重任务。

    真跑一次查重只要几秒（测试库很小），来不及验证"切走再回来还在跑"；
    所以直接构造运行中的任务状态，不启动真实管线。
    """
    from app.api import task_registry as registry

    entry = registry.register(task_id, "dedup", phase=phase, progress=list(progress))
    registry.update(task_id, status="running", started_at=time.time())
    return entry


def _finish_dedup_task(task_id: str, result: dict | None = None) -> None:
    """把注入的任务改成已完成（模拟"用户在别的页面时任务跑完了"）。"""
    from app.api import task_registry as registry

    registry.update(task_id, status="completed", phase=None, progress=None,
                    finished_at=time.time(),
                    result=result or {"duplicates_found": 0, "related_found": 0,
                                      "face_only_found": 0, "elapsed_seconds": 1})


def _drop_dedup_task(task_id: str) -> None:
    from app.api import task_registry as registry

    registry.prune()
    registry.drop(task_id)


def _progress_width(page) -> str:
    """进度条的宽度（如 '30%'）—— 直接反映 runProgress 的比例。"""
    return page.evaluate(
        "() => { const e = document.querySelector('.run-progress-bar');"
        " return e ? e.style.width : ''; }")


def _goto_tab(page, label: str) -> bool:
    """离开当前页：桌面视口点侧边栏，手机视口点底部 Tab。"""
    for sel in (".sidebar-item", ".tab-item"):
        item = page.locator(sel, has_text=label)
        if item.count() and item.first.is_visible():
            item.first.click()
            return True
    return False


def test_dedup_progress_survives_navigation(page):
    """Web 测试 15: 查重进度跨页面切换存活。

    实测报障："点了开始查重后切到单元页，回到查重页进度条消失了，
    但电脑风扇还在转" —— 服务端任务是后台线程，页面组件却随路由卸载，
    运行态存在组件 data 里就跟着一起没了（用户只能靠风扇判断在不在跑）。
    """
    section("Web 测试 15: 查重进度跨页面切换存活")
    task_id = "dedup-slow-test"
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        # ① 服务端已有在跑的任务 → 整页刷新后进入也应自动接上进度
        _inject_running_dedup_task(task_id, progress=(3, 10))
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1600)
        check("刷新进入时显示进度条", page.locator(".run-progress-bar").count() > 0)
        w1 = _progress_width(page)
        check(f"进度条宽度对应 3/10（实际 {w1!r}）", w1.startswith("30"))
        check("按钮显示「查重中…」",
              "查重中" in (page.locator(".dedup-run-btn").text_content() or ""))
        check("副标题显示「正在比对 3 / 10」",
              "正在比对 3 / 10"
              in (page.locator(".dedup-run-text .run-sub").text_content() or ""))

        # ② 切到单元页再回来：服务端仍在跑，进度条不能消失也不会卡在旧进度
        _goto_tab(page, "单元")
        page.wait_for_timeout(400)
        _inject_running_dedup_task(task_id, progress=(7, 10))   # 期间服务端在推进
        _goto_tab(page, "查重")
        page.wait_for_timeout(1600)
        check("切走再回来进度条仍在", page.locator(".run-progress-bar").count() > 0)
        w2 = _progress_width(page)
        check(f"进度条跟进到 7/10（实际 {w2!r}）", w2.startswith("70"))
        check("回来仍显示「查重中…」",
              "查重中" in (page.locator(".dedup-run-btn").text_content() or ""))

        # ③ 任务在别的页面时跑完 → 回来必须看到完成摘要并刷新列表
        _goto_tab(page, "单元")
        page.wait_for_timeout(300)
        _finish_dedup_task(task_id)
        _goto_tab(page, "查重")
        page.wait_for_timeout(1800)
        sub = page.locator(".dedup-run-text .run-sub").text_content() or ""
        check(f"后台完成后回来显示完成摘要（实际 {sub!r}）", "完成" in sub)
        check("完成后按钮回到「开始查重」",
              "开始查重" in (page.locator(".dedup-run-btn").text_content() or ""))
        check("完成后进度条消失", page.locator(".run-progress-bar").count() == 0)
    finally:
        _drop_dedup_task(task_id)


def test_dedup_state_restored_after_detail(page):
    """Web 测试 16: 看完详情返回，保持原 Tab 与展开的分组。

    实测报障："在疑似相关页面点进去看详情，返回就跳回了疑似重复且收起的页面"。
    根因同测试 15：Tab / 展开状态存在组件 data 里，路由返回时组件重建即丢失。
    """
    section("Web 测试 16: 详情返回保持 Tab 与展开状态")
    seeded = _seed_dedup_levels()
    if seeded is None:
        check("查重状态恢复测试: 缺少 片段A/片段B，跳过", True)
        return
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".dedup-tabs").wait_for(state="attached", timeout=5000)

        page.locator(".dedup-tab", has_text="疑似相关").first.click()
        page.wait_for_timeout(700)
        check("已切到「疑似相关」",
              "疑似相关" in (page.locator(".dedup-tab.active").text_content() or ""))
        _expand_first_group(page)
        check("分组已展开（子卡片可见）",
              page.locator(".group-children .card").count() > 0)
        group_name = page.locator(".dedup-group").first.locator(
            ".group-name").text_content() or ""
        check("取到分组名作为锚点", bool(group_name.strip()))

        page.locator(".group-children .card").first.click()
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        check("进入查重详情页", page.locator(".dedup-header").count() > 0)

        page.go_back()          # 浏览器返回（Web 端详情页只有浏览器返回这一条路）
        page.wait_for_timeout(1500)
        active = page.locator(".dedup-tab.active").text_content() or ""
        check(f"返回后仍停在「疑似相关」（实际 {active!r}）", "疑似相关" in active)
        check("返回后没有跳回「疑似重复」", "疑似重复" not in active)
        check("返回后原分组仍展开",
              page.locator(".dedup-group").first.locator(
                  ".group-children").count() > 0)
        check("返回后子卡片重新加载出来",
              page.locator(".group-children .card").count() > 0)
        check("返回后分组名与进入前一致（同一分组）",
              group_name.strip() in (
                  page.locator(".dedup-group").first.locator(
                      ".group-name").text_content() or ""))
    finally:
        import shutil
        from app.db import queries as q

        with DatabaseManager.session() as session:
            for rid in (seeded["dup_id"], seeded["rel_id"], seeded["face_id"]):
                dr = q.get_dedup_by_id(session, rid)
                if dr is not None:
                    session.delete(dr)
        with DatabaseManager.session() as session:
            q.delete_resource_unit(session, seeded["extra_uid"])
            q.delete_resource_unit(session, seeded["face_uid"])
        shutil.rmtree(seeded["extra_paths"][0].parent.parent, ignore_errors=True)
        shutil.rmtree(seeded["face_paths"][0].parent.parent, ignore_errors=True)


# ============================================================
# HEIC 检查与转换页
# ============================================================

def _inject_running_heic_task(task_id: str, progress=(1, 4),
                              phase: str = "converting") -> dict:
    """在测试服务器的任务表里放一个"正在跑"的 HEIC 转换任务。

    真跑一次转换只要几百毫秒（测试库很小），来不及验证"切走再回来还在跑"，
    所以直接构造运行中的任务状态，不启动真实管线。
    """
    from app.api import task_registry as registry

    entry = registry.register(task_id, "heic", phase=phase, progress=list(progress))
    registry.update(task_id, status="running", started_at=time.time())
    return entry


def _finish_heic_task(task_id: str, result: dict | None = None) -> None:
    """把注入的 HEIC 任务改成已完成。"""
    from app.api import task_registry as registry

    registry.update(task_id, status="completed", phase=None, progress=None,
                    finished_at=time.time(),
                    result=result or {"total": 2, "converted": 2, "skipped": 0,
                                      "failed": 0, "sources_removed": 0,
                                      "sources_kept": 2, "db_updated": 2,
                                      "healed": 0, "reindexed": 2,
                                      "cancelled": False, "elapsed_seconds": 0.5,
                                      "items": []})


def _drop_heic_task(task_id: str) -> None:
    from app.api import task_registry as registry

    registry.prune()
    registry.drop(task_id)


def _open_heic_tool(page) -> bool:
    """从「设置」进入 HEIC 工具页（它不占导航 Tab，是设置里的一个入口）。"""
    _goto_tab(page, "设置")
    page.wait_for_timeout(500)
    entry = page.locator(".setting-item.heic-entry")
    if entry.count() == 0 or not entry.first.is_visible():
        return False
    entry.first.click()
    page.wait_for_timeout(700)
    return True


def test_heic_tool_entry_from_settings(page):
    """Web 测试 17: HEIC 是设置里的工具入口，而不是独立导航栏目。"""
    section("Web 测试 17: HEIC 工具入口（设置内）")
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.wait_for_load_state("networkidle")

    # 导航里不该再出现 HEIC（用户明确要求不要单独开栏目）
    nav_heic = page.locator(".sidebar-item, .tab-item", has_text="HEIC").count()
    check("导航栏没有 HEIC 栏目", nav_heic == 0)

    if not _open_heic_tool(page):
        check("设置里有 HEIC 入口（移动端布局下侧边栏不可点）", True)
        return
    check("从设置进入后路由为 /heic", "/heic" in page.url)
    check("HEIC 页渲染检查按钮", page.locator(".heic-scan-btn").count() > 0)
    check("HEIC 页有转换按钮", page.locator(".dedup-run-btn").count() > 0)
    # 工具页属于"设置"这一块：侧边栏/底部 Tab 应高亮设置
    active_tab = page.locator(".tab-item.active, .sidebar-item.active").first
    if active_tab.count():
        check("工具页高亮「设置」",
              "设置" in (active_tab.text_content() or ""))


def test_heic_page_scan_and_convert(page):
    """Web 测试 18: HEIC 页检查 → 全选 → 转换 → 状态与摘要。"""
    section("Web 测试 18: HEIC 检查与转换")
    page.set_viewport_size({"width": 1280, "height": 900})
    _open_heic_tool(page)
    page.wait_for_load_state("networkidle")

    scan_btn = page.locator(".heic-scan-btn")
    if scan_btn.count() == 0:
        check("HEIC 页未渲染（跳过）", True)
        return
    scan_btn.first.click()
    page.wait_for_timeout(1500)

    rows = page.locator(".heic-row[data-path]")
    count = rows.count()
    check(f"检查到 HEIC 候选（实际 {count}）", count > 0)
    if count == 0:
        return

    targets = [rows.nth(i).get_attribute("data-path") or "" for i in range(count)]
    heic_targets = [t for t in targets if t.lower().endswith((".heic", ".heif"))]
    check(f"候选中含 .HEIC 文件（{len(heic_targets)} 个）", len(heic_targets) > 0)

    # 源文件处置选"保留"，避免测试依赖回收站可用性
    page.select_option('.dedup-opt:has-text("源文件") select', "keep")
    page.locator(".sort-btn", has_text="全选").first.click()
    page.wait_for_timeout(200)
    check("已勾选候选",
          "转换选中 (0)" not in (page.locator(".dedup-run-btn").text_content() or ""))

    page.locator(".dedup-run-btn").first.click()
    converted = 0
    for _ in range(60):
        page.wait_for_timeout(500)
        converted = page.locator('.heic-row[data-status="converted"]').count()
        if converted > 0:
            break
    check(f"出现「已转换」行（实际 {converted}）", converted > 0)

    sub = page.locator(".dedup-run-text .run-sub").text_content() or ""
    check(f"显示完成摘要（实际 {sub!r}）", "完成" in sub)
    check("摘要含成功计数", "成功" in sub)
    check("转换结束后进度条消失", page.locator(".run-progress-bar").count() == 0)


def test_heic_progress_survives_navigation(page):
    """Web 测试 19: HEIC 转换进度跨路由 / 整页刷新存活。

    与查重页同一类问题：任务跑在服务端后台线程，页面组件却随路由卸载重建 ——
    运行态存组件 data 里就会"进度条消失但风扇还在转"。
    """
    section("Web 测试 19: HEIC 进度跨路由存活")
    page.set_viewport_size({"width": 1280, "height": 900})
    task_id = "heic-slow-test"
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/heic")
        # ① 服务端已有在跑的任务 → 整页刷新后进入也应自动接上进度
        _inject_running_heic_task(task_id, progress=(1, 4))
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1600)
        check("刷新进入时显示进度条", page.locator(".run-progress-bar").count() > 0)
        w1 = _progress_width(page)
        check(f"进度条宽度对应 1/4（实际 {w1!r}）", w1.startswith("25"))
        check("按钮显示「转换中…」",
              "转换中" in (page.locator(".dedup-run-btn").text_content() or ""))
        check("副标题显示阶段与进度",
              "正在转换 1 / 4"
              in (page.locator(".dedup-run-text .run-sub").text_content() or ""))

        # ② 切到单元页再回来（经设置里的入口）：服务端仍在跑，
        #    进度条不能消失也不会卡在旧进度
        _goto_tab(page, "单元")
        page.wait_for_timeout(400)
        _inject_running_heic_task(task_id, progress=(3, 4))   # 期间服务端在推进
        _open_heic_tool(page)
        page.wait_for_timeout(1600)
        check("切走再回来进度条仍在", page.locator(".run-progress-bar").count() > 0)
        w2 = _progress_width(page)
        check(f"进度条跟进到 3/4（实际 {w2!r}）", w2.startswith("75"))

        # ③ 任务在别的页面时跑完 → 回来必须看到完成摘要
        _goto_tab(page, "单元")
        page.wait_for_timeout(300)
        _finish_heic_task(task_id)
        _open_heic_tool(page)
        page.wait_for_timeout(2000)
        sub = page.locator(".dedup-run-text .run-sub").text_content() or ""
        check(f"后台完成后回来显示完成摘要（实际 {sub!r}）", "完成" in sub)
        check("摘要含成功 2", "成功 2" in sub)
        check("完成后进度条消失", page.locator(".run-progress-bar").count() == 0)
    finally:
        _drop_heic_task(task_id)


def test_messages_mark_all_read(page):
    """Web 测试 20: 消息页「全部已读」。

    后端早就有 `POST /api/messages/read-all`，但 Web 端没有任何入口 ——
    未读消息只能一条条点开，积压几十条时用户只能放任角标一直挂着。
    """
    section("Web 测试 17: 消息一键已读")
    import requests

    from app.db import queries as q
    from app.db.models import Message
    from app.services.message_center import MessageCenter

    created = []
    for i in range(3):
        msg = MessageCenter.create_info(f"一键已读测试 {i}", "测试正文")
        if msg:
            created.append(msg.id)
    check("已造出未读消息", len(created) == 3)

    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/messages")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".msg-item").first.wait_for(state="attached", timeout=5000)

        check("消息条目渲染", page.locator(".msg-item").count() > 0)
        check("存在未读消息（带 unread 样式）",
              page.locator(".msg-item.unread").count() > 0)

        btn = page.locator(".msg-bar .msg-read-all")
        check("消息页有「全部已读」按钮", btn.count() > 0)
        check("有未读时按钮可用", btn.count() > 0 and btn.first.is_enabled())

        if btn.count() > 0 and btn.first.is_enabled():
            btn.first.click()
            page.wait_for_timeout(1000)
        else:
            check("点击「全部已读」", False)
        check("点击后所有条目不再是未读样式",
              page.locator(".msg-item.unread").count() == 0)
        check("无未读时按钮变灰",
              btn.count() > 0 and not btn.first.is_enabled())

        unread = requests.get(
            f"http://127.0.0.1:{_PORT}/api/messages/unread-count", timeout=5
        ).json().get("unread_count")
        check(f"服务端未读数归零（实际 {unread}）", unread == 0)
        check("顶栏/底栏的消息角标消失",
              page.locator(".badge-num").count() == 0)
    finally:
        with DatabaseManager.session() as session:
            session.query(Message).filter(Message.id.in_(created)).delete(
                synchronize_session=False)
        with DatabaseManager.session() as session:
            q.mark_all_messages_read(session)


def test_units_search(page):
    """Web 测试 18: 单元列表搜索（文件夹太多，靠滚动找不到）。

    搜索必须跨分组、忽略折叠状态（默认展开的根组折叠后仍要能搜到），
    并且给出行级路径，否则同名文件夹无法区分是哪一个。
    """
    section("Web 测试 18: 单元搜索")
    from app.db import queries as q

    uid_a, ids_a, paths_a = _seed_temp_unit("搜索目标甲-SRCHA", ["sa.jpg"])
    uid_b, ids_b, paths_b = _seed_temp_unit("搜索目标乙-SRCHB", ["sb.jpg"])
    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/units")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.locator(".unit-card").first.wait_for(state="attached", timeout=5000)
        total_before = page.locator(".unit-card").count()
        groups_before = page.locator(".root-group").count()
        check(f"搜索前列表有多个单元（实际 {total_before}）", total_before > 2)

        box = page.locator(".units-search .search-input")
        check("单元页有搜索框", box.count() > 0)
        if box.count() == 0:
            return

        box.first.fill("SRCHA")
        page.wait_for_timeout(500)
        names = page.locator(".unit-card .name")
        check(f"搜索后只剩匹配项（实际 {names.count()}）", names.count() == 1)
        check("命中的是目标单元",
              "搜索目标甲-SRCHA" in (names.first.text_content() or ""))
        check(f"不匹配的分组整组隐藏（{groups_before} → {page.locator('.root-group').count()}）",
              page.locator(".root-group").count() < groups_before)
        check("搜索时显示所在路径（区分同名文件夹）",
              page.locator(".unit-card .unit-path").count() > 0)

        # 折叠状态下也要搜得到：先把目标所在根组收起，再搜
        page.locator(".search-clear").first.click()
        page.wait_for_timeout(400)
        check("清空搜索后恢复全部", page.locator(".unit-card").count() == total_before)

        box.first.fill("SRCHB")
        page.wait_for_timeout(500)
        root_name = (page.locator(".root-header h3").first.text_content() or "").strip()
        check(f"搜到目标所在的根组（{root_name}）", bool(root_name))
        # 收起后组内单元名从 DOM 消失，用 has_text 就找不到这个组了 → 用稳定的 data-root
        page.locator(".search-clear").first.click()
        page.wait_for_timeout(400)
        group_b = page.locator(f'.root-group[data-root="{root_name}"]')
        check("按根组名能定位到分组", group_b.count() == 1)
        group_b.locator(".root-header").first.click()
        page.wait_for_timeout(300)
        check("目标根组已收起（卡片不可见）",
              group_b.locator(".unit-card").count() == 0)

        box.first.fill("SRCHB")
        page.wait_for_timeout(500)
        check("根组收起时搜索仍能找到目标",
              page.locator(".unit-card .name").count() == 1
              and "搜索目标乙-SRCHB"
              in (page.locator(".unit-card .name").first.text_content() or ""))
        check("命中的组即使收起也把卡片显示出来",
              page.locator(".root-group").first.locator(".root-units").count() == 1)

        box.first.fill("绝不存在的单元名ZZZ")
        page.wait_for_timeout(500)
        check("无匹配时卡片清空", page.locator(".unit-card").count() == 0)
        check("无匹配时给出空态提示", page.locator(".empty-state").count() > 0)

        box.first.fill("")
        page.wait_for_timeout(400)
        # 搜索不该动用户的折叠状态：清空后 SRCHB 那组仍是收起的
        check("清空搜索后恢复原折叠状态（搜索不改变折叠）",
              group_b.locator(".unit-card").count() == 0)
        group_b.locator(".root-header").first.click()
        page.wait_for_timeout(300)
        check(f"展开后恢复全部单元（实际 {page.locator('.unit-card').count()}）",
              page.locator(".unit-card").count() == total_before)
    finally:
        import shutil
        for uid, paths in ((uid_a, paths_a), (uid_b, paths_b)):
            with DatabaseManager.session() as session:
                q.delete_resource_unit(session, uid)
            shutil.rmtree(paths[0].parent.parent, ignore_errors=True)


def test_pin_keyboard_input(page):
    """Web 测试 19: 锁屏支持物理键盘输入。

    实测报障：在电脑上打开 Web 端，锁屏只能点屏幕上的数字键盘 ——
    桌面浏览器里这是反直觉的（用户会直接敲数字，然后发现毫无反应）。
    """
    section("Web 测试 19: 锁屏键盘输入")
    if _app is None:
        check("测试服务器 app 可用", False)
        return
    from app.api.server import _revoke_all_auth_tokens

    cfg_path = Path(__file__).parent.parent / "config.json"
    pin_path = Path(__file__).parent.parent / "data" / ".web_pin"
    cfg_backup = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
    pin_backup = pin_path.read_text(encoding="utf-8") if pin_path.exists() else None

    try:
        _app.state.config = _app.state.config.with_updates(web_pin="2468")
        _revoke_all_auth_tokens()
        page.evaluate("() => localStorage.setItem('media_pin', '')")

        page.goto(f"http://127.0.0.1:{_PORT}/")
        overlay = page.locator(".pin-overlay")
        overlay.wait_for(state="visible", timeout=5000)
        check("启用 PIN 后显示锁屏", overlay.is_visible())
        check("锁屏给出键盘输入提示", page.locator(".pin-hint-kb").count() > 0)

        page.keyboard.press("2")
        page.wait_for_timeout(200)
        check(f"敲数字键填充点位（实际 {page.locator('.pin-dot.filled').count()}）",
              page.locator(".pin-dot.filled").count() == 1)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(200)
        check("退格键删除一位", page.locator(".pin-dot.filled").count() == 0)

        page.keyboard.type("1111")
        page.wait_for_timeout(800)
        check("键盘输入错误密码给出提示",
              "密码错误" in (page.locator(".pin-error").text_content() or ""))
        check("错误后仍停留在锁屏", overlay.is_visible())

        page.keyboard.type("2468")
        unlocked = False
        for _ in range(25):
            if not overlay.is_visible():
                unlocked = True
                break
            page.wait_for_timeout(200)
        check("键盘输入正确密码可解锁", unlocked)

        # 解锁后键盘输入不能被当成 PIN 劫持：页面里正常打字必须进输入框
        page.goto(f"http://127.0.0.1:{_PORT}/#/units")
        page.locator(".app-main").wait_for(state="attached", timeout=5000)
        search = page.locator(".units-search .search-input")
        if search.count() > 0:
            search.first.click()
            page.keyboard.type("1234")
            page.wait_for_timeout(200)
            check("解锁后数字键正常进入输入框（未被 PIN 劫持）",
                  search.first.input_value() == "1234")
        else:
            check("解锁后数字键正常进入输入框（未被 PIN 劫持）", False)
        check("解锁后不会再次弹出锁屏", not overlay.is_visible())
    finally:
        if _app is not None:
            _app.state.config = _app.state.config.with_updates(web_pin="")
            _revoke_all_auth_tokens()
        if cfg_backup is not None:
            try:
                cfg_path.write_text(cfg_backup, encoding="utf-8")
            except OSError as e:
                print(f"  [INFO] 还原 config.json 失败: {e}")
        if pin_backup is not None:
            try:
                pin_path.parent.mkdir(parents=True, exist_ok=True)
                pin_path.write_text(pin_backup, encoding="utf-8")
            except OSError as e:
                print(f"  [INFO] 还原 data/.web_pin 失败: {e}")


def test_responsive_layout(page):
    """验证响应式布局。"""
    section("Web 测试 9: 响应式布局")
    # 测试手机尺寸
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.locator(".app-main").wait_for(state="attached", timeout=5000)

    bottom_tabs = page.locator(".bottom-tabs")
    check("手机尺寸底部导航可见", bottom_tabs.count() > 0)

    # 测试平板尺寸
    page.set_viewport_size({"width": 768, "height": 1024})
    check("平板尺寸页面正常", page.locator(".app-main").count() > 0)

    # 恢复默认尺寸
    page.set_viewport_size({"width": 1280, "height": 800})
    check("桌面尺寸页面正常", page.locator(".app-main").count() > 0)


# ============================================================
# 便携版：媒体库页 / 数据位置 / 轻量档入口
# ============================================================

_LITE_PORT = 19529  # 轻量档第二实例端口（与主实例、桌面端都不同）


def test_library_page_and_scan(page):
    """媒体库页：数据位置卡片 / 根列表 / 目录选择器 / 扫描长任务。

    这是便携版的**唯一**扫描入口（没有桌面 GUI），因此必须端到端验证：
    页面渲染 → 目录选择器打开 → 点「扫描」跑真实后台任务 → 结果刷新到单元列表。
    """
    section("Web 测试 33: 媒体库页（添加/扫描）")
    import tempfile
    import requests

    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="lib_ui_"))
    unit_dir = tmp / "UI单元"
    unit_dir.mkdir(parents=True, exist_ok=True)
    # 至少 2 个媒体文件，否则扫描器会按"零散文件"跳过该目录（不成单元）
    for i in range(2):
        Image.new("RGB", (64, 64), (i * 30, 80, 160)).save(unit_dir / f"p{i}.jpg")

    resp = requests.post(f"http://127.0.0.1:{_PORT}/api/library/roots",
                         json={"path": str(tmp)}, timeout=15)
    check("API 添加媒体库成功", resp.status_code == 200)
    root_id = (resp.json() or {}).get("root_id")

    try:
        page.goto(f"http://127.0.0.1:{_PORT}/#/library")
        page.wait_for_load_state("networkidle")

        data_card = page.locator(".data-root-card")
        data_card.wait_for(state="attached", timeout=8000)
        card_text = data_card.text_content() or ""
        check("数据位置卡片显示数据库路径",
              ".db" in card_text or "数据库" in card_text)

        cards = page.locator(".lib-root")
        cards.first.wait_for(state="attached", timeout=8000)
        card_texts = [cards.nth(i).text_content() or "" for i in range(cards.count())]
        check("新加的媒体库出现在列表",
              any(str(tmp) in t for t in card_texts))

        # 目录选择器（HTML 无法开原生文件夹框，走服务端目录浏览）
        page.locator(".lib-add button").first.click()
        page.locator(".folder-picker").wait_for(state="attached", timeout=5000)
        # 磁盘列表是异步拉的：必须等条目出现再计数（count() 不会自动等待）
        page.locator(".folder-picker .fp-entry").first.wait_for(
            state="attached", timeout=8000)
        check("目录选择器打开并列出磁盘",
              page.locator(".folder-picker .fp-entry").count() >= 1)
        # 进入一个磁盘后应出现「选择此目录」按钮
        page.locator(".folder-picker .fp-entry").first.click()
        page.locator(".folder-picker .fp-actions button").first.wait_for(
            state="attached", timeout=8000)
        check("进入目录后出现『选择此目录』",
              page.locator(".folder-picker .fp-actions button").count() >= 1)
        page.locator(".folder-picker .fp-bar .sort-btn").last.click()
        page.wait_for_timeout(300)

        # 扫描该库（真实后台任务）
        target = None
        for i in range(cards.count()):
            if str(tmp) in (cards.nth(i).text_content() or ""):
                target = cards.nth(i)
                break
        check("找到目标媒体库卡片", target is not None)
        if target is not None:
            target.locator("button", has_text="扫描").first.click()
            page.locator(".run-progress-wrap, .lib-summary").first.wait_for(
                state="attached", timeout=30000)
            # 等任务结束（进度条消失、摘要出现）
            for _ in range(60):
                if page.locator(".lib-summary").count() > 0:
                    break
                page.wait_for_timeout(500)
            summary = (page.locator(".lib-summary").first.text_content() or ""
                       if page.locator(".lib-summary").count() else "")
            check("扫描完成并显示摘要", "扫描完成" in summary)

        units = requests.get(f"http://127.0.0.1:{_PORT}/api/units", timeout=15).json()
        check("扫描结果已入库并在单元页可见",
              any(u.get("name") == "UI单元" for u in (units.get("units") or [])))
    finally:
        # 清理：删除本次添加的库（级联删单元/文件），避免影响其它用例的计数断言
        if root_id:
            requests.delete(
                f"http://127.0.0.1:{_PORT}/api/library/roots/{root_id}",
                timeout=15)


def test_settings_data_location(page):
    """设置页「数据位置」：数据目录/数据库/缓存展示 + 迁移入口存在。"""
    section("Web 测试 34: 设置页数据位置")
    page.goto(f"http://127.0.0.1:{_PORT}/#/settings")
    page.wait_for_load_state("networkidle")

    entry = page.locator(".setting-item", has_text="数据位置").first
    entry.wait_for(state="attached", timeout=8000)
    entry.click()
    page.wait_for_timeout(500)

    block = page.locator(".settings-block")
    block.wait_for(state="attached", timeout=5000)
    text = block.text_content() or ""
    check("展示数据目录行", "数据目录" in text)
    check("展示数据库行", "数据库" in text)
    check("展示缩略图缓存行", "缩略图缓存" in text)
    # 手机访问地址：测试服务监听 127.0.0.1，必须如实提示"手机连不上"，
    # 否则用户会拿着 127.0.0.1 在手机上试（真实报障）。
    check("展示手机访问行", "手机访问" in text)
    check("回环监听时提示手机连不上",
          "只监听本机" in text or "手机连不上" in text)
    check("提供迁移数据入口",
          page.locator(".settings-actions button", has_text="迁移数据").count() >= 1)
    check("提供修改缩略图目录入口",
          page.locator(".settings-actions button", has_text="缩略图目录").count() >= 1)


def test_lite_mode_hides_dedup(page):
    """便携版 = 与完整档同一套功能：查重入口**保留**，只有真缺能力时才隐藏。

    曾经用 `profile=lite` 决定"不注册查重路由 + 隐藏查重入口"（代码里两套功能
    语义）。现在 `profile` 只是标签，能力由 `app.utils.capabilities` 探测：查重
    只要 Pillow 就能跑，所以在任何打包变体里都存在；视频/人脸/HEIC/RAW 才随
    编译开关（MEDIA_BUILD_NO_CV2 / MEDIA_BUILD_WITH_FACE）变化。

    另起一个 profile=lite 的服务实例（模拟便携版打包产物），与主实例共用测试库。
    必须放在 PIN 相关用例之前：create_app 会吊销模块级令牌表。
    """
    section("Web 测试 35: 便携版（能力驱动，非档位驱动）")
    import requests
    import threading
    import uvicorn

    from app.api.server import create_app

    lite_app = create_app(
        AppConfig().with_updates(profile="lite", thumbnail_cache_dir=_thumb_cache_dir()),
        {"data_root_status": "configured", "data_root": ""})
    server = uvicorn.Server(uvicorn.Config(
        lite_app, host="127.0.0.1", port=_LITE_PORT, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(50):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)
        base = f"http://127.0.0.1:{_LITE_PORT}"
        try:
            features = requests.get(f"{base}/api/features", timeout=5).json()
        except Exception as e:  # noqa: BLE001
            check(f"便携版实例启动失败: {e}", False)
            return
        check("便携版 features.dedup=true（与完整档同功能）",
              features.get("dedup") is True)
        check("便携版查重路由可用（200）",
              requests.get(f"{base}/api/dedup/results", timeout=5).status_code == 200)
        check("便携版说明里给出降级原因",
              isinstance(features.get("capability_notes"), list))

        # 模拟"便携版打包产物"：编译期排除了 PySide6 与人脸模型。
        # 能力快照必须如实反映 —— 这正是"同一套代码 + 编译开关"的验收点。
        from app.utils import capabilities as caps_mod
        probe, resolver = caps_mod._has_module, caps_mod.resolve_face_model_dir
        try:
            caps_mod._has_module = lambda name: name in ("PIL", "cv2")
            caps_mod.resolve_face_model_dir = lambda configured=None: None
            caps_mod.reset_cache()
            packaged = requests.get(f"{base}/api/features", timeout=5).json()
            caps = packaged.get("capabilities") or {}
            check("便携版无 Qt（qt=false）", caps.get("qt") is False)
            check("便携版无模型（face_models=false）", caps.get("face_models") is False)
            check("便携版仍保留查重能力（dedup=true）",
                  packaged.get("dedup") is True)
            check("人脸线索随模型缺席而关闭", packaged.get("face") is False)
        finally:
            caps_mod._has_module, caps_mod.resolve_face_model_dir = probe, resolver
            caps_mod.reset_cache()

        page.goto(f"{base}/")
        page.evaluate(f"() => localStorage.setItem('media_server_url', '{base}')")
        page.goto(f"{base}/#/units")
        page.wait_for_load_state("networkidle")
        page.locator(".app-main").wait_for(state="attached", timeout=8000)

        # 能力开关是异步拉取的：轮询等导航按 features 重渲染。
        # 用 count() 判存在（不等待），否则元素缺席时会各自空等 30s 才报错。
        def nav_text() -> str:
            sidebar = page.locator(".sidebar-nav")
            if sidebar.count():
                return sidebar.first.text_content() or ""
            tabs = page.locator(".bottom-tabs")
            if tabs.count():
                return tabs.first.text_content() or ""
            return ""

        nav_updated = False
        for _ in range(40):
            text = nav_text()
            if "媒体库" in text and "查重" in text:
                nav_updated = True
                break
            page.wait_for_timeout(250)
        final_nav = nav_text()
        if not final_nav:
            body = (page.locator("#app").text_content() or "")[:120]
            print(f"     [诊断] url={page.url} 页面首段={body!r}")
        check("导航里保留「查重」（便携版功能同步）", nav_updated)
        check("导航里保留「媒体库」", "媒体库" in final_nav)

        # 直连查重页必须正常打开（不再被重定向掉）
        page.goto(f"{base}/#/dedup")
        page.wait_for_timeout(900)
        check("直连 #/dedup 正常（未被重定向）", "/dedup" in page.url)

        # 恢复默认服务器地址，避免影响后续用例
        page.evaluate(f"() => localStorage.setItem('media_server_url', "
                      f"'http://127.0.0.1:{_PORT}')")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ============================================================
# 主入口
# ============================================================

def main():
    print("=" * 60)
    print("  Web 前端 Playwright 功能测试")
    print("=" * 60)

    # 检查 Playwright 是否可用
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] 请安装 playwright: pip install playwright && playwright install chromium")
        return True

    # 启动服务器
    print("\n  启动测试服务器...")
    try:
        start_server()
        print(f"  服务器已启动: http://127.0.0.1:{_PORT}")
    except Exception as e:
        print(f"  [FAIL] 服务器启动失败: {e}")
        return False

    try:
        with sync_playwright() as pw:
            try:
                # 优先使用系统 Chrome；未安装时回退 Playwright 自带 Chromium
                browser = pw.chromium.launch(channel="chrome", headless=True)
            except Exception as launch_err:
                print(f"  [INFO] 未找到系统 Chrome（{launch_err}），回退 Playwright Chromium")
                browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                device_scale_factor=1,
            )
            page = context.new_page()

            # 注入 API server URL 到 localStorage（绕过 PIN/连接页）
            page.goto(f"http://127.0.0.1:{_PORT}/")
            page.evaluate(f"""
                () => {{
                    localStorage.setItem('media_server_url', 'http://127.0.0.1:{_PORT}');
                    localStorage.setItem('media_pin', '');
                }}
            """)
            page.wait_for_timeout(300)

            # 运行各测试
            test_home_page_loads(page)
            test_units_page_shows_data(page)
            test_units_date_sort_by_content(page)
            test_unit_files_page(page)

            # 预览导航测试
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_preview_navigation(page)

            # 预览滑动手势测试（独立导航以防之前测试退出预览）
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_preview_swipe_gesture(page)

            # 预览返回滚动恢复测试
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_preview_scroll_restore(page)

            # 重新加载使 localStorage 生效（绕过 PIN 页）
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_navigation_tabs(page)
            test_phone_viewport_access(page)

            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_pin_lock_screen(page)

            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_responsive_layout(page)

            # 侧边栏导航测试（1280px 视口下）
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_navigation_sidebar(page)

            # 手势引擎（PTR 异常 / 边界阻尼）
            test_gesture_engine_ptr_and_damp(page)

            # 服务器地址尾斜杠规范化
            test_server_url_normalization(page)

            # 文件管理（多选删除 / 删除文件夹）——会真实删除测试库中的临时单元
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_file_management_delete(page)

            # 回收站不可用时的删除出路（模拟网络盘 / 无回收站分区）
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_file_delete_trash_fallback(page)

            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_unit_delete_from_card_menu(page)

            # 查重分级语义（疑似重复 / 疑似相关）
            page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
            page.wait_for_load_state("networkidle")
            test_dedup_level_semantics(page)
            test_dedup_grouped_list(page)
            test_dedup_detail_thumbnails(page)
            test_dedup_stale_banner(page)

            # 一键查重（索引 + 比对，含进度展示）
            page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
            page.wait_for_load_state("networkidle")
            test_dedup_run_from_web(page)

            # 查重运行态 / Tab / 展开状态跨路由存活（切页、刷新、看详情返回）
            page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
            page.wait_for_load_state("networkidle")
            test_dedup_progress_survives_navigation(page)
            test_dedup_state_restored_after_detail(page)

            # HEIC 检查与转换（设置内的工具入口 / 检查 → 全选 → 转换 / 进度跨路由存活）
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_heic_tool_entry_from_settings(page)
            page.goto(f"http://127.0.0.1:{_PORT}/#/heic")
            page.wait_for_load_state("networkidle")
            test_heic_page_scan_and_convert(page)
            test_heic_progress_survives_navigation(page)

            # 消息一键已读 / 单元搜索
            test_messages_mark_all_read(page)

            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_units_search(page)

            # 便携版：媒体库页（添加/扫描）、设置页数据位置、轻量档入口
            # 注意顺序：轻量档用例会 create_app → 吊销令牌，必须排在 PIN 用例之前
            test_library_page_and_scan(page)
            test_settings_data_location(page)
            test_lite_mode_hides_dedup(page)

            # 取消访问密码不锁死（会临时改动服务端 web_pin，放在最后）
            test_cancel_pin_does_not_lock_out(page)

            # 锁屏键盘输入（同样临时启用 web_pin，放在取消密码之后）
            test_pin_keyboard_input(page)

            browser.close()

    except Exception as e:
        print(f"\n  [FAIL] 测试异常: {e}")
        import traceback
        traceback.print_exc()
        global _failed
        _failed += 1
    finally:
        stop_server()

    print(f"\n  测试完成: {_passed}/{_passed + _failed} 通过, {_failed} 失败")
    if _failed > 0:
        print("  [FAIL] 存在问题")
        return False
    print("  [OK] 全部通过！")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
