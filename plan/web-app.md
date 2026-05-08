# Web 端方案：手机浏览器直接访问

## 方案概要

放弃 Flet Android 编译方案，改为桌面端内置 Web 服务器 + 手机浏览器访问。手机与电脑同 Wi‑Fi 下打开 Chrome 输入 `http://电脑IP:19527` 即可使用全部功能。

**优势：** 零安装、零编译、PWA 可添加主屏幕、视频原生 H5 播放。

---

## 改动清单

### 1. 桌面端：挂载静态文件（1 行关键代码）

**文件：** `desktop/app/api/server.py`（`create_app` 函数内）

```python
from fastapi.staticfiles import StaticFiles

# 在 app.include_router(...) 之后添加：
app.mount("/", StaticFiles(directory=root_path / "web", html=True), name="web")
```

- `html=True` 让所有未知路径 fallback 到 `index.html`，SPA 路由可用
- 注意 `mount` 必须在 `include_router` **之后**，否则会吞掉 API 路由
- API 端点都在 `/api/` 前缀下，与静态文件路径不冲突

### 2. 新建 `desktop/web/` 目录（单页应用）

#### 文件结构

```
desktop/web/
├── index.html              # 主页面（Vue 3 SPA + 所有逻辑内联）
├── manifest.json            # PWA 清单（可选）
└── sw.js                    # Service Worker（可选，离线缓存）
```

**推荐方案：** 全部内联到 `index.html`，用 CDN 加载 Vue 3 + 组件库。

#### CDN 依赖

```html
<script src="https://unpkg.com/vue@3/dist/vue.global.prod.js"></script>
<link rel="stylesheet" href="https://unpkg.com/@mdi/font@7/css/materialdesignicons.min.css">
```

#### 页面结构（单页，底部 Tab 导航）

```
┌──────────────────────┐
│     AppBar: 标题      │
├──────────────────────┤
│                      │
│   <router-view />     │
│                      │
├──────────────────────┤
│  单元  │ 查重 │ 消息  │   ← 底部导航栏
└──────────────────────┘
```

### 3. 页面与对应 API

| 页面 | 路由 | API 调用 | 说明 |
|------|------|----------|------|
| 连接/设置 | `/#/connect` | `GET /api/health` | 首次输入 IP:Port，存在 localStorage |
| 单元列表 | `/#/` 或 `/#/units` | `GET /api/units` | 卡片网格，点击进文件列表 |
| 文件网格 | `/#/units/{id}` | `GET /api/units/{id}/files` | 缩略图网格，竖屏 3 列，横屏 5 列 |
| 图片预览 | `/#/preview/{id}` | `/api/files/{id}/stream` | `<img>` 全屏显示，手势缩放 |
| 视频播放 | `/#/preview/{id}` | `/api/files/{id}/stream` | `<video>` 控件，支持 H5 播放 |
| 查重列表 | `/#/dedup` | `GET /api/dedup/results` | 查重结果列表 |
| 消息中心 | `/#/messages` | `GET /api/messages` | 消息列表 |

### 4. 关键实现细节

#### 缩略图显示

```html
<img :src="`/api/files/${file.id}/thumbnail`"
     @error="onError"
     loading="lazy"
     class="thumbnail">
```

- `loading="lazy"` 实现懒加载
- `onerror` 时替换为类型图标占位

#### 视频播放

```html
<video controls preload="metadata" width="100%">
  <source :src="`/api/files/${fileId}/stream`">
</video>
```

- `preload="metadata"` 只加载头部信息，不预下载全文
- `controls` 提供原生播放、进度条、倍速、全屏
- Range 请求由浏览器自动处理，与桌面端 `FileResponse` 兼容

#### PWA（可选）

```json
// manifest.json
{
  "name": "Media Manager",
  "short_name": "Media",
  "display": "standalone",
  "start_url": "/",
  "icons": [...]
}
```

```js
// sw.js — 缓存静态资源，API 请求不缓存
self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) return;
  e.respondWith(caches.match(e.request) || fetch(e.request));
});
```

---

## 增量实现顺序

| 步骤 | 内容 | 验证方式 |
|------|------|----------|
| 1 | `server.py` 挂载静态文件 + 创建 `web/index.html` 空壳 | 浏览器打开 `http://localhost:19527` 看到页面 |
| 2 | Vue 3 路由 + 单元列表页 | 看到单元卡片列表 |
| 3 | 文件网格页（缩略图） | 看到缩略图，懒加载正常 |
| 4 | 图片/视频预览页 | 图片放大、视频播放正常 |
| 5 | 底部 Tab 导航 + 查重/消息页 | 切换正常 |
| 6 | PWA manifest + service worker（可选） | 可添加到主屏幕 |

---

## 与现有代码的关系

- **API 端点：** 全部复用，不需要改
- **测试：** 不受影响（`test_flow.py` 116/116 通过）
- **Flet 安卓项目：** 保留 `android/` 不动，两者共存
- **数据流：** 完全一致（缩略图二进制、流式 Range、查重触发）

---

## 验证

```bash
cd desktop
python main.py
# 浏览器打开 http://localhost:19527
# 手机同 Wi‑Fi 打开 http://电脑IP:19527
```
