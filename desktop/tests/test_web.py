# -*- coding: utf-8 -*-
"""
Web 前端基础功能测试 —— 通过 HTTP 验证 SPA 可加载和 API 字段完整性。
运行前确保 API 服务器已启动（http://localhost:19527）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def test_page_loads():
    """验证 SPA 首页可正常加载。"""
    section("测试前端: 页面加载")
    import requests
    resp = requests.get("http://localhost:19527/", timeout=5,
                        headers={"Accept": "text/html"})
    check("首页返回 200", resp.status_code == 200)
    check("响应包含 HTML", "text/html" in resp.headers.get("content-type", ""))
    check("响应包含 Vue 挂载标记", 'id="app"' in resp.text)
    check("js-core.js 可访问", requests.get("http://localhost:19527/js-core.js", timeout=5).status_code == 200)
    check("js-pages.js 可访问", requests.get("http://localhost:19527/js-pages.js", timeout=5).status_code == 200)
    check("js-app.js 可访问", requests.get("http://localhost:19527/js-app.js", timeout=5).status_code == 200)
    check("style.css 可访问", requests.get("http://localhost:19527/style.css", timeout=5).status_code == 200)

    # 验证拆分后的 JS 解析正确（按 index.html 加载顺序 js-core → js-pages → js-app）
    core = requests.get("http://localhost:19527/js-core.js", timeout=5).text
    check("js-core 包含 api()", "function api(" in core)
    check("js-core 包含令牌工具", "function getAuthToken" in core)

    pages = requests.get("http://localhost:19527/js-pages.js", timeout=5).text
    check("js-pages 包含 UnitsPage", "const UnitsPage" in pages)
    check("js-pages 包含 PreviewPage", "const PreviewPage" in pages)
    check("js-pages 包含 SettingsPage", "const SettingsPage" in pages)

    app_js = requests.get("http://localhost:19527/js-app.js", timeout=5).text
    check("js-app 包含 router", "createRouter" in app_js)
    check("js-app 包含 Vue mount", "app.mount('#app')" in app_js)


def test_api_units_response():
    """验证单元 API 包含必要的封面字段。"""
    section("测试前端: API 字段完整性")
    import requests
    resp = requests.get("http://localhost:19527/api/units", timeout=5)
    check("单元 API 返回 200", resp.status_code == 200)
    data = resp.json()
    if data.get("units"):
        u = data["units"][0]
        check("包含 cover_file_id", "cover_file_id" in u)
        check("包含 library_root_name", "library_root_name" in u)


def main():
    print("\n" + "=" * 60)
    print("  Web 前端功能测试")
    print("=" * 60)
    try:
        import requests
    except ImportError:
        print("  [SKIP] 缺少 requests 库，跳过前端测试")
        return True

    global _passed, _failed
    _passed = 0
    _failed = 0

    test_page_loads()
    test_api_units_response()

    print(f"\n  测试完成: {_passed}/{_passed + _failed} 通过, {_failed} 失败")
    if _failed > 0:
        print("  [FAIL] 存在问题")
        return False
    print("  [OK] 全部通过！")
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
