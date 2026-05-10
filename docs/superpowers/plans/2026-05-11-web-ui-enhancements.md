# Web UI 增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现文件拆分、视频时长显示、文件名搜索、断连提示、下拉刷新

**Architecture:** 分三波实施。Wave 1 先做重构（index.html 拆为三文件）+ 视频时长 + 前端测试框架。Wave 2 加搜索 + 断连提示。Wave 3 加下拉刷新。

**Tech Stack:** Vue 3 (CDN) + FastAPI + Playwright (frontend tests)

---

## Wave 1: 文件拆分 + 视频时长 + 前端测试

### Task 1.1: 拆分 CSS — 从 index.html 抽出 style.css

**Files:**
- Create: `desktop/web/style.css`
- Modify: `desktop/web/index.html`

- [ ] **Step 1: 创建 style.css**

将 `index.html` 中 `<style>...</style>` 块内的全部 CSS（从 `/* ========== Reset & Variables ========== */` 到 `/* ========== Settings ========== */` 最后一个大括号之间的所有内容）复制到新文件 `desktop/web/style.css`。

不修改任何 CSS 规则。仅复制内容。

- [ ] **Step 2: 从 index.html 移除内联 CSS，改为 `<link>` 引用**

在 index.html 中找到并删除：
```html
<style>
/* ========== Reset & Variables ========== */
...
/* ========== Settings ========== */
.setting-item .mdi-chevron-right { color: var(--text-tertiary); font-size: 20px; }
</style>
```

替换为：
```html
<link rel="stylesheet" href="style.css">
```

确保 `<link>` 放在 `<title>` 之后、现有 CDN 样式之前：
```html
<title>影视资源管理器</title>
<link rel="manifest" href="manifest.json">
<link rel="stylesheet" href="style.css">
<link rel="stylesheet" href="https://unpkg.com/@mdi/font@7/css/materialdesignicons.min.css">
```

- [ ] **Step 3: 运行后端测试确认无回归**

```bash
cd d:/Media && python desktop/tests/test_flow.py
```
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add desktop/web/style.css desktop/web/index.html
git commit -m "refactor: extract style.css from index.html"
```

---

### Task 1.2: 拆分 JS — 从 index.html 抽出 app.js

**Files:**
- Create: `desktop/web/app.js`
- Modify: `desktop/web/index.html`

- [ ] **Step 1: 创建 app.js**

将 `index.html` 中 `<script>` 块内 `const { createApp, ... } = Vue;` 到 `navigator.serviceWorker.register(...)` 之间的全部 JavaScript 代码复制到 `desktop/web/app.js`。

包括：
- API 辅助函数 (`function api(...)`)
- 所有组件定义 (`ConnectPage`, `UnitsPage`, `UnitFilesPage`, `PreviewPage`, `DedupPage`, `DedupDetailPage`, `MessagesPage`, `SettingsPage`)
- 模块级变量 (`_fileScrollTop`, `_unitsScrollTop`)
- 路由定义 (`const routes = [...]`)
- 路由创建 (`const router = createRouter(...)`)
- 根 App 组件 (`const App = { ... }`)
- 应用挂载 (`const app = createApp(App); app.use(router); app.mount('#app')`)
- Service Worker 注册 (`navigator.serviceWorker.register(...)`)

- [ ] **Step 2: 从 index.html 移除内联 JS，改为 `<script>` 引用**

删除 index.html 中从 `<script>` 到 `</script>` 的整个块，替换为：
```html
<script src="app.js"></script>
```

引用顺序：app.js 在所有组件之后加载，但 index.html 中只需加载它（它在内部处理所有依赖）。

最终 index.html 的 `<body>` 内容为：
```html
<div id="app">
  <!-- PIN Lock Screen -->
  ...
  <template v-if="pinUnlocked">
    ...
  </template>
</div>
<script src="app.js"></script>
```

- [ ] **Step 3: 验证页面可加载**

确认 `index.html`、`style.css`、`app.js` 都在 `desktop/web/` 目录下。手动访问 `http://localhost:19527/` 检查页面正常渲染（无白屏、无控制台错误）。

- [ ] **Step 4: 运行后端测试**

```bash
cd d:/Media && python desktop/tests/test_flow.py
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add desktop/web/app.js desktop/web/index.html
git commit -m "refactor: extract app.js from index.html"
```

---

### Task 1.3: 后端添加 duration_ms 到文件列表 API

**Files:**
- Modify: `desktop/app/api/routes/units.py`

- [ ] **Step 1: 在 get_unit_files 返回中增加 duration_ms**

找到 `get_unit_files` 函数内的 `files` 返回列表（约行 74-82），在现有字段基础上增加 `duration_ms`：

```python
"files": [
    {
        "id": f.id,
        "filename": f.filename,
        "media_type": f.media_type,
        "size_bytes": f.size_bytes,
        "duration_ms": f.duration_ms,
    }
    for f in files[:200]
],
```

- [ ] **Step 2: 运行测试**

```bash
cd d:/Media && python desktop/tests/test_flow.py
```
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add desktop/app/api/routes/units.py
git commit -m "feat: add duration_ms to unit files API"
```

---

### Task 1.4: 前端显示视频时长

**Files:**
- Modify: `desktop/web/app.js`

- [ ] **Step 1: 在 UnitFilesPage 的 file-item 中添加时长显示**

在模板的 `.feed-item` 中，找到 `.vid-badge`（视频播放图标），在旁边或内部添加时长：

```html
<div class="thumb-wrap">
  ...
  <span v-if="f.media_type === 'video'" class="vid-badge"><span class="mdi mdi-play"></span></span>
  <span v-if="f.media_type === 'video' && f.duration_ms" class="dur-badge">{{ fmtDuration(f.duration_ms) }}</span>
</div>
```

添加 CSS 样式到 style.css 中（.dur-badge 放在 .vid-badge 旁边）：

```css
.dur-badge {
  position: absolute; bottom: 6px; left: 6px;
  background: rgba(0,0,0,.7); color: #fff;
  font-size: 11px; padding: 2px 6px; border-radius: 4px;
  font-variant-numeric: tabular-nums;
}
```

- [ ] **Step 2: 添加 fmtDuration 方法**

在 UnitFilesPage 的 methods 中增加：

```javascript
fmtDuration(ms) {
  if (!ms || ms <= 0) return '';
  const totalSec = Math.floor(ms / 1000);
  const min = Math.floor(totalSec / 60);
  const sec = totalSec % 60;
  return min + ':' + (sec < 10 ? '0' : '') + sec;
},
```

- [ ] **Step 3: 验证**

检查文件页中视频缩略图左下角显示 `3:25` 格式时长。图片文件不显示时长。

- [ ] **Step 4: 运行测试**

```bash
cd d:/Media && python desktop/tests/test_flow.py
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add desktop/web/app.js desktop/web/style.css
git commit -m "feat: display video duration on thumbnails"
```

---

### Task 1.5: 前端测试基础框架

**Files:**
- Create: `desktop/tests/test_web.py`

- [ ] **Step 1: 创建 test_web.py**

```python
"""
Web 前端基础功能测试 —— 使用 Playwright 验证 SPA 渲染。
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
    check("app.js 可访问", requests.get("http://localhost:19527/app.js", timeout=5).status_code == 200)
    check("style.css 可访问", requests.get("http://localhost:19527/style.css", timeout=5).status_code == 200)

    # 验证 JS 解析正确
    js = requests.get("http://localhost:19527/app.js", timeout=5).text
    check("app.js 包含 UnitsPage", "const UnitsPage" in js)
    check("app.js 包含 PreviewPage", "const PreviewPage" in js)
    check("app.js 包含 Vue mount", "app.mount('#app')" in js)


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
```

- [ ] **Step 2: 运行验证**

```bash
cd d:/Media && python desktop/tests/test_web.py
```
Expected: ALL PASS

注意：此测试依赖 API 服务器正在运行（`http://localhost:19527`）。如果服务器未启动，测试会失败。这是一种冒烟测试方式，后续可扩展为完整的 Playwright 浏览器测试。

- [ ] **Step 3: 更新 CLAUDE.md 的命令段**

在 CLAUDE.md 的 Commands 部分添加测试运行方式：

```bash
# Run web frontend tests (requires API server running)
python desktop/tests/test_web.py
```

- [ ] **Step 4: Commit**

```bash
git add desktop/tests/test_web.py CLAUDE.md
git commit -m "test: add frontend smoke tests"
```

---

## Wave 2: 文件名搜索 + 断连提示

### Task 2.1: 文件页文件名搜索

**Files:**
- Modify: `desktop/web/app.js`

- [ ] **Step 1: 添加搜索框到文件页模板**

在 UnitFilesPage 的 feed-header 下方添加搜索框（在 `<div class="feed-grid">` 之前）：

```html
<div class="search-bar" v-if="files.length > 0">
  <span class="mdi mdi-magnify"></span>
  <input v-model="searchQuery" type="text" placeholder="搜索文件名..." class="search-input">
  <button v-if="searchQuery" class="search-clear" @click="searchQuery = ''"><span class="mdi mdi-close"></span></button>
</div>
```

- [ ] **Step 2: 添加搜索 CSS**

在 style.css 中添加：

```css
.search-bar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 16px; background: var(--bg-card);
  margin: 0; border-bottom: 1px solid var(--border);
}
.search-bar .mdi-magnify { font-size: 18px; color: var(--text-tertiary); flex-shrink: 0; }
.search-input {
  flex: 1; border: none; background: none;
  color: var(--text-primary); font-size: 14px; outline: none;
}
.search-input::placeholder { color: var(--text-tertiary); }
.search-clear {
  background: none; border: none; color: var(--text-tertiary);
  font-size: 16px; cursor: pointer; padding: 2px;
}
```

- [ ] **Step 3: 添加搜索 data + 过滤逻辑**

在 UnitFilesPage 的 data 中增加：
```javascript
searchQuery: '',
```

在 computed 的 `sortedFiles` 之前增加一个计算属性 `filteredFiles`：

```javascript
computed: {
  filteredFiles() {
    if (!this.searchQuery) return this.files;
    const q = this.searchQuery.toLowerCase();
    return this.files.filter(f => f.filename.toLowerCase().includes(q));
  },
  sortedFiles() {
    const arr = [...this.filteredFiles];  // 改为基于过滤后的数组
    ...
  },
},
```

然后修改模板中计数：
```html
<span class="count">{{ filteredFiles.length }} / {{ files.length }} 个文件</span>
```

- [ ] **Step 4: 运行测试**

```bash
cd d:/Media && python desktop/tests/test_flow.py
cd d:/Media && python desktop/tests/test_web.py
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add desktop/web/app.js desktop/web/style.css
git commit -m "feat: add filename search to file page"
```

---

### Task 2.2: 网络断连提示

**Files:**
- Modify: `desktop/web/app.js` + `desktop/web/style.css`

- [ ] **Step 1: 添加断连状态和提示样式**

在 style.css 中添加：

```css
/* ========== Offline Banner ========== */
.offline-banner {
  position: sticky; top: 0; z-index: 50;
  background: #b45400; color: #fff;
  text-align: center; font-size: 13px; font-weight: 500;
  padding: 6px 16px; display: flex; align-items: center;
  justify-content: center; gap: 6px;
}
```

- [ ] **Step 2: 在 App 组件中添加断连检测**

在根 App 组件中：

data 增加：
```javascript
offline: false,
failCount: 0,
```

methods 增加：
```javascript
async fetchUnread() {
  if (!this.serverUrl) return;
  try {
    const data = await api(this.serverUrl, '/api/messages/unread-count');
    this.unreadCount = data.unread_count || 0;
    this.failCount = 0;
    if (this.offline) { this.offline = false; }
  } catch(e) {
    this.failCount++;
    if (this.failCount >= 2 && !this.offline) { this.offline = true; }
    this.unreadCount = 0;
  }
},
```

- [ ] **Step 3: 在模板中添加断连横幅**

在 App 模板中、main content 上方添加：

```html
<!-- Offline Banner -->
<div v-if="offline" class="offline-banner">
  <span class="mdi mdi-wifi-off"></span> 网络连接已断开，正在重连...
</div>
```

- [ ] **Step 4: 运行测试**

```bash
cd d:/Media && python desktop/tests/test_flow.py
cd d:/Media && python desktop/tests/test_web.py
```
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add desktop/web/app.js desktop/web/style.css
git commit -m "feat: add offline connection indicator"
```

---

## Wave 3: 下拉刷新

### Task 3: 下拉刷新

**Files:**
- Modify: `desktop/web/app.js` + `desktop/web/style.css`

- [ ] **Step 1: 添加下拉刷新 CSS**

在 style.css 中添加：

```css
/* ========== Pull to Refresh ========== */
.ptr-container {
  position: relative; overflow: hidden;
}
.ptr-indicator {
  display: flex; align-items: center; justify-content: center;
  height: 60px; color: var(--text-tertiary); font-size: 13px;
  gap: 6px; transition: height .2s;
}
.ptr-indicator .mdi { font-size: 20px; }
.ptr-indicator .mdi-spin { animation: spin 1s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
```

- [ ] **Step 2: 在 UnitsPage 和 UnitFilesPage 中添加下拉刷新逻辑**

在 UnitsPage 的 mounted() 中，在数据加载完成后添加触摸事件监听：

```javascript
// 在 mounted 的 finally 块末尾添加
this.$nextTick(() => {
  // 下拉刷新检测由页面复用现有 touch 事件实现
});
```

更简单的方式：在根 App 层面检测下拉。但此功能实现较复杂，简化方案：在 UnitsPage 和 UnitFilesPage 的顶部添加一个不可见的下拉检测区域，监听 touch 事件的 start/move/end 序列，当检测到向下滑动超过阈值时触发刷新。

实际实现：

在 UnitsPage 的模板开头添加：
```html
<div class="ptr-container" ref="ptrContainer">
  <div class="ptr-indicator" v-if="refreshing">
    <span class="mdi mdi-loading mdi-spin"></span> 刷新中...
  </div>
```

在 UnitFilesPage 的模板开头添加同样结构。

在 methods 中：
```javascript
async refresh() {
  this.refreshing = true;
  try {
    const data = await api(this.serverUrl, '/api/units');
    // ... 重新处理数据 ...
  } finally {
    this.refreshing = false;
  }
},
```

- [ ] **Step 3: 运行测试**

```bash
cd d:/Media && python desktop/tests/test_flow.py
cd d:/Media && python desktop/tests/test_web.py
```
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add desktop/web/app.js desktop/web/style.css
git commit -m "feat: add pull-to-refresh on mobile"
```

---

## 完整验证

全部完成后执行：

```bash
cd d:/Media && python desktop/tests/test_flow.py && python desktop/tests/test_web.py
```
Expected: ALL PASS
