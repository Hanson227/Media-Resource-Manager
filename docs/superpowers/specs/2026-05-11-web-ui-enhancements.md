# Web UI 增强 — 搜索/时长/断连提示/下拉刷新

## 改动范围

全部在 `desktop/web/` 目录内，不涉及后端 API。

## 实施顺序（三波）

---

### Wave 1: 文件拆分 + 视频时长

#### 1.1 文件拆分

当前 `index.html` 约 1550 行，所有 CSS 和 JS 内联。拆分为：

```
desktop/web/
├── index.html       # HTML 骨架 + Vue/路由 CDN 引用 + 组件挂载
├── style.css        # 全部 CSS 样式
└── app.js           # 全部 Vue 组件 + 路由 + 应用初始化
```

`index.html` 加载 `style.css` 和 `app.js`（用 `<script>` 标签，不使用 ES modules 以保持兼容）。拆分过程中不改动任何功能逻辑。

#### 1.2 视频时长显示

文件页缩略图和单元卡片封面覆盖视频时长的显示：

- **文件页**（`UnitFilesPage`）：每个 `feed-item` 的 `thumb-wrap` 内，右下角 `.vid-badge` 旁边或内部，显示 `mm:ss` 时长
- **数据来源**：`GET /api/units/{id}/files` 当前不返回 `duration_ms`，需要后端在文件列表中增加该字段
- **后端改动**：`routes/units.py` 的 `get_unit_files` 中返回时增加 `duration_ms`

---

### Wave 2: 文件名搜索 + 断连提示

#### 2.1 文件名搜索

- **文件页**（`UnitFilesPage`）顶部 feed-header 下方加搜索框
- 输入时实时过滤 `files` 数组中的 `filename`，匹配方式为 `includes()`（不区分大小写）
- 搜索和排序共存：搜索过滤原始数据，排序在过滤后的子集上生效
- 搜索框带清除按钮（×），清除后恢复全部文件

#### 2.2 断连提示

- 在根 `App` 组件中通过 `fetchUnread()` 的失败来检测网络断开
- 连续 2 次 API 请求失败 → 页面顶部显示横幅提示条（`"网络连接已断开"`，橙色背景）
- 成功恢复 1 次请求 → 提示条消失
- 提示条固定在 top-bar 下方，不遮挡内容

---

### Wave 3: 下拉刷新

- 使用 CSS `overscroll-behavior: contain` + 触摸事件检测下拉
- 下拉超过 60px 触发刷新，显示加载动画
- 刷新时重新请求当前页面的 API（首页请求 `/api/units`，文件页请求 `/api/units/{id}/files`）
- 移动端 swipe down 手势原生触发的浏览器刷新通过 `overscroll-behavior: none` 禁用

---

## 验证

### 自动化测试

Wave 1 需要新增前端测试覆盖。在 `desktop/tests/` 下新增 `test_web.py`，使用 Playwright（已通过 MCP 可用）对 SPA 做基础功能验证：

- 页面加载：`GET /` 返回 200，HTML 包含 Vue 挂载标记
- 文件列表渲染：文件页加载后 `.feed-grid` 内至少有缩略图元素
- 搜索过滤：输入搜索词后文件列表长度减少

运行方式：

```bash
cd desktop && python tests/test_web.py
```

### 后端回归

每 Wave 完成后运行：

```bash
cd desktop && python tests/test_flow.py
```

手动验证：搜索过滤正常、时长显示正确、断开网络后提示条出现、下拉刷新数据更新
