# Flet 安卓 APP 骨架

## 项目结构

```
android/
├── main.py                  # Flet 入口
├── server_discovery.py      # 局域网发现桌面服务器
├── pages/
│   ├── __init__.py
│   ├── connect_page.py      # 连接服务器页（IP/端口）
│   ├── unit_list_page.py    # 资源单元列表
│   ├── file_grid_page.py    # 文件网格（缩略图）
│   └── preview_page.py      # 图片查看 / 视频播放
├── services/
│   ├── __init__.py
│   └── api_client.py        # HTTP 请求封装
├── requirements.txt         # 依赖
└── .gitignore
```

## 依赖

```
flet>=0.25.0
httpx>=0.26.0                 # HTTP 客户端
```

## 逐页说明

### 1. 连接页（connect_page.py）

```python
# 用户输入电脑 IP:Port 或自动扫描
# 布局：
#   ├── 标题: "影视资源管理器"
#   ├── IP 输入框（默认 192.168.1.100）
#   ├── 端口输入框（默认 19527）
#   ├── "连接" 按钮
#   └── 连接状态文字
#
# 点击连接 → GET /api/health → 成功则跳转 unit_list_page
# 失败显示 "无法连接，请检查IP和端口"
```

**数据持久化：** 连接成功后用 `page.client_storage.set("server_url", url)` 存储，下次启动自动填充。

### 2. 资源单元列表（unit_list_page.py）

```python
# 显示所有资源单元
# 布局：
#   ├── AppBar 标题: "资源单元"
#   ├── GridView（每行2列）
#   │   └── Card（每个单元）
#   │       ├── 单元名称（粗体）
#   │       ├── 文件数量 / 大小
#   │       └── 星标/手动标记标记
#   └── 底部: "扫描状态" 小文字
#
# on_enter → GET /api/units → 渲染卡片
# 点击卡片 → page.go("/files?unit_id={id}&name={name}")
```

Flet 控件参考：`Card`、`ListTile`、`GridView`、`Text`

### 3. 文件网格（file_grid_page.py）

```python
# 显示指定单元内的文件缩略图
# 布局：
#   ├── AppBar 标题: 单元名称
#   ├── GridView（动态列数，自适应）
#   │   └── 文件卡片
#   │       ├── Image（缩略图，HTTP URL）
#   │       └── 文件名（两行省略）
#   └── 底部状态: "共 N 个文件"
#
# on_enter → GET /api/units/{unit_id}/files → 获取文件列表
# 缩略图 URL: f"{server_url}/api/files/{f['id']}/thumbnail"
# Image 控件直接用 src 加载
# 点击图片 → page.go("/preview?file_id={id}&media_type={type}")
```

**关键问题：** Flet 的 `Image` 控件支持 `src` 直接传 HTTP URL，会自动异步加载。但如果缩略图返回 404，`Image` 会显示破损图标。需要在 `Image` 上设置 `error_content` 占位。

### 4. 预览页（preview_page.py）

```python
# 图片或视频的预览页面
# 逻辑：
#   获取 file_id，调 GET /api/files/{file_id} 获取 media_type
#
#   如果是图片：
#     Image(src=f"{server_url}/api/files/{file_id}/stream", fit=ImageFit.CONTAIN)
#     + 双指缩放 / 滑动切换
#
#   如果是视频：
#     方案 A（推荐）：
#       用 Flet 的 VideoPlayer 控件
#       VideoPlayer(
#           src=f"{server_url}/api/files/{file_id}/stream",
#           format=根据扩展名设置,
#       )
#
#     方案 B（备选）：
#       用 WebView 加载 HTML5 <video> 标签
#       VideoPlayer 在 flet-android 上依赖原生 ExoPlayer
```

**视频播放的关键：** Flet 0.25+ 的 `VideoPlayer` 控件在 Android 上使用 ExoPlayer，传入 `src` 为 HTTP URL 即可支持流式播放。`Range` 请求头由底层播放器自动处理，拖进度条不需要额外实现。

## 数据流（通用逻辑）

所有 HTTP 请求通过 `api_client.py` 统一封装：

```python
import httpx

class ApiClient:
    def __init__(self, server_url: str):
        self.base = server_url.rstrip("/")

    async def get_units(self) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}/api/units", timeout=5)
            r.raise_for_status()
            return r.json()["units"]

    async def get_unit_files(self, unit_id: int) -> list[dict]:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}/api/units/{unit_id}/files", timeout=10)
            r.raise_for_status()
            return r.json()["files"]

    async def get_file_detail(self, file_id: int) -> dict:
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{self.base}/api/files/{file_id}", timeout=10)
            r.raise_for_status()
            return r.json()

    def thumbnail_url(self, file_id: int) -> str:
        return f"{self.base}/api/files/{file_id}/thumbnail"

    def stream_url(self, file_id: int) -> str:
        return f"{self.base}/api/files/{file_id}/stream"
```

## 页面路由（main.py）

```python
import flet as ft

def main(page: ft.Page):
    page.title = "影视资源管理器"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0

    # 从 client_storage 读取上次服务器地址
    saved_url = page.client_storage.get("server_url")

    def route_change(e: ft.RouteChangeEvent):
        page.views.clear()
        if page.route == "/" or page.route == "/connect":
            from pages.connect_page import ConnectPage
            page.views.append(ConnectPage(page))
        elif page.route.startswith("/units"):
            from pages.unit_list_page import UnitListPage
            page.views.append(UnitListPage(page))
        elif page.route.startswith("/files"):
            from pages.file_grid_page import FileGridPage
            page.views.append(FileGridPage(page))
        elif page.route.startswith("/preview"):
            from pages.preview_page import PreviewPage
            page.views.append(PreviewPage(page))
        page.update()

    page.on_route_change = route_change

    # 首次路由
    if saved_url:
        page.go("/units")
    else:
        page.go("/connect")

ft.app(target=main)
```

## 关键注意事项

| 问题 | 处理方式 |
|------|---------|
| **页面切换** | 用 Flet 的 `View` 路由 + `page.views` 栈，支持返回手势 |
| **深色主题** | `page.theme_mode = ft.ThemeMode.DARK` 全局设置 |
| **缩略图显示不全** | 在 `Image` 上设置 `fit=ft.ImageFit.COVER`、`border_radius` 圆角 |
| **视频播放** | Flet >= 0.25 有 `VideoPlayer`（Android 用 ExoPlayer），低版本回退 `WebView` |
| **网络错误** | `api_client.py` 中 `try/except httpx.TimeoutException`，显示 SnackBar |
| **加载指示** | 每个页面用 `ft.ProgressRing()` 或 `ft.ProgressBar()` |
| **本地状态** | `page.client_storage` 存 server_url，类 `UserPreferences` |

## 实现顺序

1. `main.py` + 路由 + `api_client.py` → 可运行空壳
2. `connect_page.py` → 能连上服务器
3. `unit_list_page.py` → 看到单元列表
4. `file_grid_page.py` → 看到缩略图
5. `preview_page.py` → 看图片、播视频
6. 调试与美化
