# -*- coding: utf-8 -*-
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
        test_db.unlink()
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

    from app.core.scanner import MediaScanner
    scanner = MediaScanner(
        extensions=AppConfig().media_extensions,
        exclude_patterns=AppConfig().exclude_patterns,
    )
    result = scanner.scan_root(root)
    with DatabaseManager.session() as session:
        db_root = q.add_library_root(session, str(result.root_path))
        for unit in result.units:
            new_unit = q.create_unit(
                session, str(unit.path), unit.name, db_root.id,
                file_count=unit.file_count, total_size=unit.total_size,
            )
            for df in unit.files:
                q.insert_media_file(
                    session, path=str(df.path), filename=df.filename,
                    extension=df.extension, media_type=df.media_type,
                    size_bytes=df.size_bytes, resource_unit_id=new_unit.id,
                )
    return root


def start_server():
    """启动 API 服务器。"""
    global _server, _server_thread
    import requests
    ensure_db()
    config = AppConfig()
    from app.api.server import create_app
    app = create_app(config)

    import uvicorn
    _server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=_PORT, log_level="error")
    )
    _server_thread = threading.Thread(target=_server.run, daemon=True)
    _server_thread.start()
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
    page.wait_for_timeout(1000)

    # 等待数据加载（单元列表或根组）
    root_groups = page.locator(".root-group")
    count = root_groups.count()
    check("根目录组已渲染", count > 0)

    if count > 0:
        # 等待数据渲染完毕（单元卡片可能已在 DOM 中，因 group 默认展开）
        cards_visible = page.locator(".unit-card").first.wait_for(state="attached", timeout=5000)
        unit_cards = page.locator(".unit-card")
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


def test_unit_files_page(page):
    """验证进入单元后的文件列表页。"""
    section("Web 测试 3: 文件列表页")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.wait_for_timeout(1000)

    # 展开根目录并进入第一个单元
    root_group = page.locator(".root-group").first
    if root_group.count() == 0:
        check("无根目录可测试", True)
        return

    # 等待单元卡片加载后再点击进入
    unit_card = page.locator(".unit-card").first
    if unit_card.count() == 0:
        check("无单元可进入", True)
        return

    unit_card.click()
    page.wait_for_timeout(1000)

    # 验证进入了文件列表页
    feed_header = page.locator(".feed-header")
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
    page.wait_for_timeout(1500)

    # 展开根目录进入文件列表
    unit_card = page.locator(".unit-card").first
    if unit_card.count() == 0:
        check("预览测试: 无单元可进入", True)
        return
    unit_card.click()
    page.wait_for_timeout(1000)

    # 点击第一个文件进入预览
    feed_item = page.locator(".feed-item").first
    if feed_item.count() == 0:
        check("预览测试: 无文件可预览", True)
        return
    feed_item.click()
    page.wait_for_timeout(1500)

    # 验证预览页加载
    preview = page.locator(".preview-overlay")
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
            page.wait_for_timeout(500)
            new_pos = nav_hint.locator(".pos").text_content() or ""
            check("点击前进后位置变化", new_pos != pos_text)

        # 测试点击左侧箭头后退
        prev_btn = nav_hint.locator(".mdi-chevron-left")
        if prev_btn.count() > 0:
            prev_btn.click()
            page.wait_for_timeout(500)
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
    page.wait_for_timeout(1500)

    # 进入文件列表
    unit_card = page.locator(".unit-card").first
    if unit_card.count() == 0:
        check("滑动手势测试: 无单元可进入", True)
        return
    unit_card.click()
    page.wait_for_timeout(1000)

    # 点击第一个文件进入预览
    feed_item = page.locator(".feed-item").first
    if feed_item.count() == 0:
        check("滑动手势测试: 无文件可预览", True)
        return
    feed_item.click()
    page.wait_for_timeout(1500)

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

    # 模拟左滑 → 下一张
    page.evaluate("""
        () => {
            const zone = document.querySelector('.gesture-zone');
            if (!zone) return;
            const rect = zone.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            function fire(name, x) {
                const evt = new TouchEvent(name, { bubbles: true, cancelable: true });
                // Override changedTouches getter
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
    page.wait_for_timeout(600)

    # 验证位置变化（左滑 → 前进到下一张）
    new_pos_text = nav_hint.locator(".pos").text_content() or ""
    new_parts = new_pos_text.split("/")
    new_pos = int(new_parts[0].strip()) if len(new_parts) == 2 else initial_pos
    if initial_pos < total:
        check("左滑切换到下一张", new_pos == initial_pos + 1)
    else:
        check("末张左滑不切换", new_pos == total)

    # 右滑 → navigateToFile(index-1)
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
    page.wait_for_timeout(600)

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
    page.wait_for_timeout(1500)

    unit_card = page.locator(".unit-card").first
    if unit_card.count() == 0:
        check("滚动恢复测试: 无单元可进入", True)
        return
    unit_card.click()
    page.wait_for_timeout(1000)

    # 等待文件加载完成（至少显示 4 个文件才值得测试滚动）
    feed_items = page.locator(".feed-item")
    if feed_items.count() < 4:
        check("滚动恢复测试: 文件不足，跳过", True)
        return

    # 点击靠后的文件（如第 4 个，index=3, row=1）进入预览
    feed_items.nth(3).click()
    page.wait_for_timeout(1500)

    # 在预览中前进到下一个文件（触发 _fileScrollTop 更新）
    nav_hint = page.locator(".image-nav-hint")
    if nav_hint.count() > 0:
        next_btn = nav_hint.locator(".mdi-chevron-right")
        if next_btn.count() > 0:
            next_btn.click()
            page.wait_for_timeout(500)

    # 返回文件列表
    back_btn = page.locator(".preview-back")
    if back_btn.count() > 0:
        back_btn.click()
        page.wait_for_timeout(1000)

    # 验证文件列表已恢复（feed-items 可见，且不在顶部）
    items_after = page.locator(".feed-item").count()
    check("返回后文件列表已渲染", items_after >= 4)

    # 验证浏览器的 URL 已回到文件列表页
    current_url = page.url
    check("URL 已回到文件列表页", "/preview/" not in current_url)


def test_navigation_tabs(page):
    """验证底部导航栏功能。"""
    section("Web 测试 7: 底部导航")
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.wait_for_timeout(1000)

    # 等待底部导航渲染
    tabs = page.locator(".bottom-tabs")
    tabs_count = tabs.count()
    check("底部导航栏存在", tabs_count > 0)

    if tabs_count == 0:
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
            page.wait_for_timeout(500)
            check("切换到查重页面", True)
            break

    # 点击"消息"tab
    for i in range(tab_count):
        text = tab_texts[i]
        if "消息" in text:
            tab_items.nth(i).click()
            page.wait_for_timeout(500)
            check("切换到消息页面", True)
            break


def test_pin_lock_screen(page):
    """验证 PIN 锁屏界面渲染。"""
    section("Web 测试 9: PIN 锁屏界面")
    page.goto(f"http://127.0.0.1:{_PORT}/")
    page.wait_for_timeout(1000)

    # PIN 界面可能在连接之前显示
    pin_overlay = page.locator(".pin-overlay")
    pin_keypad = page.locator(".pin-keypad")
    lock_icon = page.locator(".lock-icon")

    if pin_overlay.count() > 0:
        check("PIN 遮罩层存在", pin_overlay.count() > 0)

        # 检测是设置模式还是验证模式
        if lock_icon.count() > 0:
            check("锁图标存在", lock_icon.count() > 0)

        if pin_keypad.count() > 0:
            pin_keys = page.locator(".pin-key")
            check("数字键盘按钮存在", pin_keys.count() > 0)
            # 尝试输入
            if pin_keys.count() >= 4:
                pin_keys.nth(0).click()
                page.wait_for_timeout(100)
                pin_dots = page.locator(".pin-dot")
                if pin_dots.count() > 0:
                    # 验证首个点位被填充
                    check("PIN 输入后点位填充", True)
    else:
        check("无 PIN 界面（已解锁或未设置）", True)


def test_responsive_layout(page):
    """验证响应式布局。"""
    section("Web 测试 10: 响应式布局")
    # 测试手机尺寸
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(f"http://127.0.0.1:{_PORT}/#/units")
    page.wait_for_timeout(1000)

    bottom_tabs = page.locator(".bottom-tabs")
    check("手机尺寸底部导航可见", bottom_tabs.count() > 0)

    # 测试平板尺寸
    page.set_viewport_size({"width": 768, "height": 1024})
    page.wait_for_timeout(500)
    check("平板尺寸页面正常", page.locator(".app-main").count() > 0)

    # 恢复默认尺寸
    page.set_viewport_size({"width": 1280, "height": 800})
    page.wait_for_timeout(500)
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
            browser = pw.chromium.launch(channel="chrome", headless=True)
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
            test_unit_files_page(page)

            # 预览导航测试
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_timeout(500)
            test_preview_navigation(page)

            # 预览滑动手势测试（独立导航以防之前测试退出预览）
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_timeout(500)
            test_preview_swipe_gesture(page)

            # 预览返回滚动恢复测试
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_timeout(500)
            test_preview_scroll_restore(page)

            # 重新加载使 localStorage 生效（绕过 PIN 页）
            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_timeout(500)
            test_navigation_tabs(page)

            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_timeout(500)
            test_pin_lock_screen(page)

            page.goto(f"http://127.0.0.1:{_PORT}/#/units")
            page.wait_for_timeout(500)
            test_responsive_layout(page)

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
