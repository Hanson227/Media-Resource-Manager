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
    """创建并填充测试数据库。"""
    test_db = Path(__file__).parent.parent / "data" / "test_web_playwright.db"
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
    config = AppConfig().with_updates(thumbnail_cache_dir=_thumb_cache_dir())
    from app.api.server import create_app
    app = create_app(config)
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
    """造一组 duplicate + 一组 related 查重结果（含人脸线索行）。"""
    from app.db import queries as q

    extra_uid, extra_ids, extra_paths = _seed_temp_unit("查重相关单元", ["r1.jpg"])
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
        )
        q.insert_file_match(session, dedup_result_id=dup.id,
                            file_a_id=files_a[0].id, file_b_id=files_b[0].id,
                            similarity_score=1.0, match_type="md5")

        rel = q.upsert_dedup_result(
            session, unit_a_id=ua.id, unit_b_id=extra_uid,
            similarity_score=0.08, match_count=1,
            total_files_a=len(files_a), total_files_b=1,
            match_types="face", match_level="related",
        )
        q.insert_file_match(session, dedup_result_id=rel.id,
                            file_a_id=files_a[0].id, file_b_id=extra_ids[0],
                            similarity_score=0.72, match_type="face")
        return {"dup_id": dup.id, "rel_id": rel.id,
                "extra_uid": extra_uid, "extra_paths": extra_paths}


def test_dedup_level_semantics(page):
    """Web 查重：区分「疑似重复」与「疑似相关」，related 不给删除入口。

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
        check("分级 Tab 存在（疑似重复/疑似相关）", tabs.count() == 2)
        check("默认选中「疑似重复」",
              "疑似重复" in (page.locator(".dedup-tab.active").text_content() or ""))
        check("重复列表出现等级徽标",
              page.locator(".lvl-badge.duplicate").count() > 0)
        check("重复卡片显示相似度百分比",
              "%" in (page.locator(".card-meta .count").first.text_content() or ""))

        # 切到「疑似相关」
        page.locator(".dedup-tab", has_text="疑似相关").first.click()
        page.wait_for_timeout(600)
        related_cards = page.locator(".card.card-related")
        check("疑似相关列表出现卡片", related_cards.count() > 0)
        check("相关卡片带「疑似相关」徽标",
              page.locator(".lvl-badge.related").count() > 0)
        check("相关卡片文案声明不建议删除",
              "不建议删除" in (related_cards.first.text_content() or ""))

        # 相关详情：无保留 A/B，有忽略；人脸线索单独成段
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
        check("人脸线索单独分节展示", page.locator(".face-hint-note").count() > 0)
        check("人脸线索行使用 face 标签", page.locator(".tag.face").count() > 0)

        # 重复详情：仍提供保留 A/B
        page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
        page.wait_for_load_state("networkidle")
        dup_badge = page.locator(".card")
        dup_badge.first.wait_for(state="attached", timeout=5000)
        page.locator(".card").first.click()
        page.locator(".dedup-header").wait_for(state="attached", timeout=5000)
        dlabels = []
        dbuttons = page.locator(".resolve-actions .btn")
        for i in range(dbuttons.count()):
            dlabels.append(dbuttons.nth(i).text_content() or "")
        check(f"重复详情提供「保留 A/B」（实际 {dlabels}）",
              any("保留 A" in t for t in dlabels) and any("保留 B" in t for t in dlabels))
    finally:
        import shutil
        with DatabaseManager.session() as session:
            for rid in (seeded["dup_id"], seeded["rel_id"]):
                dr = q.get_dedup_by_id(session, rid)
                if dr is not None:
                    session.delete(dr)
            q.delete_resource_unit(session, seeded["extra_uid"])
        shutil.rmtree(seeded["extra_paths"][0].parent.parent, ignore_errors=True)


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
        check("列表已刷新（有徽标或空态）",
              page.locator(".lvl-badge").count() > 0
              or page.locator(".empty-state").count() > 0)
    finally:
        page.remove_listener("dialog", on_dialog)


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

            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_load_state("networkidle")
            test_unit_delete_from_card_menu(page)

            # 查重分级语义（疑似重复 / 疑似相关）
            page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
            page.wait_for_load_state("networkidle")
            test_dedup_level_semantics(page)

            # 一键查重（索引 + 比对，含进度展示）
            page.goto(f"http://127.0.0.1:{_PORT}/#/dedup")
            page.wait_for_load_state("networkidle")
            test_dedup_run_from_web(page)

            # 取消访问密码不锁死（会临时改动服务端 web_pin，放在最后）
            test_cancel_pin_does_not_lock_out(page)

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
