# Flet 安卓 APP 功能规格说明书

## 概述

安卓手机客户端，通过 HTTP API 连接局域网内的桌面端「影视资源管理器」，实现远程浏览、预览、播放和查重管理。文件传输走 HTTP 流，不使用 SMB。

---

## 1. 连接与配置

### 1.1 首次使用引导

- 输入电脑 IP 地址（默认 `192.168.1.100`）
- 输入 API 端口（默认 `19527`）
- 点击「连接」→ `GET /api/health` 验证
- 成功后存入 `client_storage`，下次自动连接

### 1.2 连接修复与切换

- 连接失败时显示友好错误页面
- 提供「重新连接」入口，可修改 IP/端口

### 1.3 可选：局域网自动发现

桌面端启动时每隔 30s 发送 UDP 广播（`255.255.255.255:19528`），内容为 `MEDIA_MANAGER:19527`。手机端收到后自动填入 IP。

---

## 2. 数据模型

```python
# 资源单元（桌面端识别的文件夹级媒体集合）
Unit = {
    "id": int,
    "name": str,           # 文件夹名
    "path": str,           # 电脑端绝对路径（APP 仅用于展示）
    "file_count": int,
    "total_size": int,
    "is_manual": bool,
    "is_starred": bool,
    "status": str,          # active / merged / excluded
    "cover_url": str,       # 封面缩略图 URL
}

# 文件列表项
FileItem = {
    "id": int,
    "filename": str,
    "media_type": str,      # "image" | "video"
    "size_bytes": int,
    "width": int | None,
    "height": int | None,
    "thumbnail_url": str | None,
}

# 文件详情
FileDetail = {
    "id": int,
    "filename": str,
    "path": str,
    "media_type": str,
    "extension": str,
    "size_bytes": int,
    "width": int | None,
    "height": int | None,
    "duration_ms": int | None,
    "resource_unit_id": int,
}

# 查重结果
DedupResult = {
    "id": int,
    "unit_a_name": str,
    "unit_b_name": str,
    "similarity_score": float,
    "match_count": int,
    "is_resolved": bool,
}

# 消息
Message = {
    "id": int,
    "msg_type": str,        # info / warning / error / dedup_alert
    "title": str,
    "body": str | None,
    "is_read": bool,
}
```

---

## 3. 页面与导航

```
/connect          ← 连接页（首次 / 连接失败）
/units            ← 资源单元列表（默认首页）
/units/{id}/files ← 文件网格
/units/{id}/tree  ← 文件夹树视图（可选）
/preview?file_id={id}&type={media_type}  ← 图片/视频预览
/dedup            ← 查重结果列表
/dedup/{id}       ← 查重详情（对比）
/messages         ← 消息中心
/settings         ← 设置（服务器地址、缓存管理等）
```

所有页面支持 Android 返回键自动 `page.go(-1)`。

---

## 4. 浏览媒体库

### 4.1 资源单元视图（默认）

- `GET /api/units` → 返回所有活跃单元列表
- 每个单元显示为 `Card`：
  - 封面图：`GET /api/files/{id}/thumbnail`（取该单元首个文件缩略图）
  - 单元名称（粗体，16sp）
  - 文件数量 / 总大小（副标题，13sp，灰色）
  - 星标 / 手动标记视觉指示
- 下拉刷新 → 重新拉取列表
- 点击 → 跳转 `/units/{id}/files`

### 4.2 文件夹视图（可选）

- `GET /api/units/{id}/tree` → 返回该单元的目录树结构
- 用 `ExpansionTile` 渲染树形控件
- 点击目录 → 显示该目录下的文件

### 4.3 文件网格

- `GET /api/units/{unit_id}/files` → 文件列表
- `GridView` 动态列数（手机竖屏 3 列，横屏 5 列）
- 每个文件卡片：
  - 缩略图：`GET /api/files/{id}/thumbnail`
  - 文件名（一行省略）
  - 图片/视频角标
- 点击 → `/preview?file_id={id}&type={media_type}`
- 长按 → 上下文菜单（查重、删除）

---

## 5. 文件预览

### 5.1 缩略图加载策略

| 优先级 | 方式 | URL |
|--------|------|-----|
| 1 | 请求桌面端缩略图 | `GET /api/files/{id}/thumbnail` |
| 2 | 失败时显示占位图标 | 内置图标，按类型显示 IMG/VID |

### 5.2 图片查看器

- `InteractiveViewer` 包裹 `Image`，支持双指缩放/平移
- 单指左右滑动切换上一张/下一张（用 `PageView` 实现画廊模式）
- 顶部 AppBar 显示文件名
- 底部显示文件信息（分辨率、大小）

### 5.3 视频播放器（核心交互）

#### 5.3.1 技术选型

Flet `VideoPlayer` 控件，底层 Android ExoPlayer。

#### 5.3.2 播放器布局

```
┌──────────────────────────────────┐
│  ← 长按降速         长按加速 →    │  (GestureDetector 覆盖层)
│                                  │
│          视频画面区域              │
│                                  │
├──────────────────────────────────┤
│  ▸‖          ⏪ ⟐ ⏩          📶  │  顶栏：暂停/播放、后退/前进、倍速显示
│  ████████████░░░░░░░░  00:12/01:30│  进度条（Slider，可拖动）
│  0.5x │ 1.0x │ 1.5x │ 2.0x     │  倍速选择按钮（高亮当前值）
│  全屏按钮                         │
└──────────────────────────────────┘
```

#### 5.3.3 功能清单

| 功能 | 实现 |
|------|------|
| **暂停/播放** | 点击画面中央 ↔ `player.play()` / `player.pause()`，顶栏按钮同步 |
| **进度条** | `Slider` 绑定 `player.position` / `player.duration`，拖动时 `player.seek(position)` |
| **倍速 0.5x / 1.0x / 1.5x / 2.0x** | 底部按钮行，`player.playback_rate = 1.5` 原生支持 |
| **快进/后退** | 顶栏按钮 `player.seek(position - 5000)` / `+5000` |
| **倍速长按交互** | `GestureDetector` 检测长按位置：左半屏 `playback_rate=0.5x`，右半屏 `playback_rate=2.0x`，松手恢复 `1.0x` |
| **全屏** | 点击全屏按钮 → `page.window.fullscreen = True` |
| **自动旋转** | 监听 `page.on_resize`，横屏自动全屏 |
| **锁屏继续播放** | `VideoPlayer` 默认后台运行，无需额外处理 |

#### 5.3.4 流播放机制

```
手机播放器 → GET /api/files/{id}/stream → HTTP FileResponse
```

ExoPlayer 自动处理：
- `Range` 请求头 → 拖动进度条时发 range request
- 分块传输 → 边缓冲边播
- 支持 mp4、mkv、avi、mov 等常见格式

---

## 6. 远程查重管理

### 6.1 发起查重

- 在文件网格长按菜单 → 「查重此单元」
- 弹窗设置阈值（默认 0.80）
- `POST /api/dedup/run`（需桌面端新增）

```json
// 请求
{ "unit_ids": [1, 2, 3], "threshold": 0.80 }

// 响应（同步/快速时直接返回）
{ "task_id": "dedup_001", "status": "running" }
```

### 6.2 结果展示

- `GET /api/dedup/results` → 列出所有未处理的查重结果
- 每个结果卡片：
  - 单元 A ↔ 单元 B 名称
  - 杰卡德相似度（百分比，大号字体）
  - 匹配文件对数
  - 是否已处理
- 点击 → `/dedup/{id}` 查看详情
- 详情页并排显示两个单元的文件列表，匹配对高亮

### 6.3 处理操作

- 选择处理方式：保留A / 保留B / 合并 / 忽略
- `POST /api/dedup/results/{id}/resolve` → `{"resolution": "keep_a"}`

### 6.4 消息中心

- `GET /api/messages` → 拉取消息列表
- 首页显示未读数角标
- 未读 `dedup_alert` 点击直接跳转到对应的查重详情页

---

## 7. 离线缓存

| 数据 | 策略 | 存储方式 |
|------|------|---------|
| 服务器地址 | 永久保存 | `client_storage.set("server_url", url)` |
| 单元列表 | 每次成功拉取后缓存 | 本地 JSON 文件 |
| 文件列表 | 进入页面时缓存该单元 | 本地 JSON 文件 |
| 缩略图 | HTTP 请求自动缓存 | 依赖 `Image` 控件内部缓存 |

- 启动时检测网络连通性
- 不可达时显示离线标记（横幅：「离线模式 - 显示缓存数据」）
- 网络恢复后自动刷新

---

## 8. APP 全局配置

在 `/settings` 页面中：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| 服务器地址 | 末次成功值 | IP:Port |
| 缩略图质量 | 高 | 加载缩略图时是否请求原图 |
| 查重阈值 | 0.80 | 发起查重时的默认阈值 |
| 离线模式 | 自动 | 断网后显示缓存 |
| 主题 | 跟随系统 | Dark / Light / 系统 |
| 缓存清理 | - | 清理本地缓存的 JSON 数据 |

---

## 9. 依赖清单

```
flet>=0.25.0
httpx>=0.26.0
```

无其他第三方依赖。视频播放使用 Flet 内置的 `VideoPlayer`（Android 底层 ExoPlayer），图片浏览使用 Flet `Image` + `InteractiveViewer`。

---

## 10. 增量实现顺序

| 阶段 | 内容 | 可验证目标 |
|------|------|-----------|
| 1 | 桌面端新增 `stream` 和 `thumbnail` 端点（改返回图片二进制） | 浏览器打开 `http://电脑:19527/api/files/1/thumbnail` 能看到图 |
| 2 | Flet 项目骨架 + 路由 + 连接页 + api_client | APP 能连接上桌面端 |
| 3 | 单元列表页 + 文件网格页 | APP 能看到单元和文件缩略图 |
| 4 | 图片预览（InteractiveViewer 画廊） | 能双指缩放看大图 |
| 5 | 视频播放器（全功能：暂停/进度/倍速/长按变速） | 能播视频，拖进度，切换倍速 |
| 6 | 查重结果查看 + 处理 | 能看到查重结果并选择保留 |
| 7 | 消息中心 + 离线缓存 | 能看消息，断网显示历史数据 |
| 8 | 局域网自动发现 | 手机自动找到电脑 |

---

## 11. 桌面端 API 新增端点汇总

| 方法 | 路径 | 说明 | 状态 |
|------|------|------|------|
| GET | `/api/files/{id}/thumbnail` | **改造**：直接返回图片二进制 | 待实现 |
| GET | `/api/files/{id}/stream` | **新增**：流式传输原文件，支持 Range | 待实现 |
| GET | `/api/units/{id}/tree` | **新增**：单元目录树结构 | 待实现 |
| POST | `/api/dedup/run` | **新增**：触发查重，接受 unit_ids + threshold | 待实现 |
| GET | `/api/events/unread` | **新增**：未读事件简报（首页角标用） | 待实现 |

已有端点在 `API.md` 中完整记录，无需改动。
