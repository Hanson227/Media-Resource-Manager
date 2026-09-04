# 手机 APP API 集成指南

> 影视资源管理器内置 HTTP API 服务（默认 `0.0.0.0:19527`），局域网内手机 APP 可通过 RESTful 接口查询媒体库数据。
>
> 首次运行后访问 `http://<电脑IP>:19527/docs` 查看 Swagger 交互式文档。

---

## 目录

- [1. 连接](#1-连接)
- [2. 数据结构](#2-数据结构)
- [3. 端点参考](#3-端点参考)
- [4. 缩略图与媒体文件](#4-缩略图与媒体文件)
- [5. SMB 文件共享](#5-smb-文件共享手机访问原始文件)
- [6. 移动端集成示例](#6-移动端集成示例)
- [7. 限制与注意事项](#7-限制与注意事项)

---

## 1. 连接

### 1.1 发现服务器

应用启动后 API 自动运行（`config.json` 中 `api_enabled: true`）。手机需要知道电脑的局域网 IP：

```
http://192.168.x.x:19527/
```

健康检查端点可用于发现：

```
GET /api/health
```

响应：

```json
{"status": "ok"}
```

### 1.2 基础 URL

```
http://<电脑IP>:19527
```

> 如果手机与电脑不在同一子网（如跨 VLAN），需要防火墙放行 19527 端口。

### 1.3 鉴权

当桌面端设置 Web 访问密码（4 位 PIN）后，除以下免鉴权端点外，所有 `/api/*`
请求都必须在请求头携带会话令牌：

```
Authorization: Bearer <token>
```

令牌通过 `POST /api/auth/verify`（密码正确）获取，有效期 12 小时；
修改/取消密码会使全部已签发令牌立即失效。令牌仅存于桌面端进程内存。

未鉴权访问返回 `401 {"detail": "未授权或会话已过期"}`。

桌面端未设置密码时，API 保持局域网完全开放；仍不建议将端口暴露到公网。

### 1.4 响应格式

所有端点返回 JSON。错误时返回 HTTP 4xx/5xx 状态码 + `{"detail": "错误说明"}`。

---

## 2. 数据结构

理解以下核心概念有助于 API 集成。

### 2.1 媒体库根目录（Library Root）

用户添加的顶级文件夹，不含媒体文件信息，仅记录路径。

```
media_library_roots { id, path, enabled }
```

### 2.2 资源单元（Resource Unit）

**核心概念**。一个文件夹，如果**直接包含**媒体文件（图片/视频），则被识别为一个"资源单元"。手机 APP 通常以"资源单元"为入口来展示内容。

```
resource_units {
  id,             // 单元 ID，API 的主要查询键
  name,           // 文件夹名称，用于显示
  path,           // 文件系统绝对路径（手机端通常不需要）
  file_count,     // 该单元内媒体文件数
  total_size,     // 总字节数
  is_manual,      // 用户手动标记的
  is_starred,     // 用户收藏
  status,         // "active" | "merged" | "excluded"
}
```

### 2.3 媒体文件（Media File）

单元内的具体文件，分为 image 和 video 两类。

```
media_files {
  id,             // 文件 ID
  filename,       // 文件名（含扩展名）
  media_type,     // "image" | "video"
  size_bytes,     // 文件大小
  width, height,  // 分辨率（可能为 null）
  md5_hash,       // MD5 哈希
  phash,          // 感知哈希（用于相似匹配）
  resource_unit_id,
}
```

### 2.4 查重结果（Dedup Result）

两个资源单元之间的相似比对结果。杰卡德指数 >= 0.80（默认阈值）视为重复。

```
dedup_results {
  id,
  unit_a_id, unit_b_id,     // 比对的双方单元 ID
  unit_a_name, unit_b_name,
  similarity_score,          // 杰卡德指数 [0, 1]
  match_count,               // 匹配的文件对数
  match_types,               // 如 "md5,phash"
  is_resolved,               // 是否已处理
  resolution,                // "pending" | "keep_a" | "keep_b" | "whitelist"
}
```

### 2.5 关系图

```
媒体库根目录
  └── 资源单元（文件夹级）
        └── 媒体文件（图片/视频）
              └── 人脸向量（可选）
              └── 视频帧哈希（仅视频）

查重结果（单元 × 单元）
  └── 文件匹配对（文件 × 文件）
```

---

## 3. 端点参考

### 3.1 资源单元

#### 获取所有单元

```
GET /api/units
```

响应：

```json
{
  "units": [
    {
      "id": 1,
      "name": "片段A",
      "path": "D:\\Media\\2025-01-01\\片段A",
      "file_count": 8,
      "total_size": 2457600,
      "is_manual": false,
      "is_starred": false,
      "status": "active"
    }
  ]
}
```

#### 获取单个单元详情

```
GET /api/units/{unit_id}
```

#### 获取单元内文件列表

```
GET /api/units/{unit_id}/files
```

响应：

```json
{
  "unit_id": 1,
  "unit_name": "片段A",
  "file_count": 8,
  "files": [
    {
      "id": 101,
      "filename": "照片01.jpg",
      "media_type": "image",
      "size_bytes": 524288
    }
  ]
}
```

### 3.2 媒体文件

#### 文件列表

```
GET /api/files?unit_id=1&page=1&per_page=50
```

参数：

| 参数 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `unit_id` | int | - | 按资源单元筛选（可选） |
| `page` | int | 1 | 页码（>= 1） |
| `per_page` | int | 50 | 每页数量（<= 200） |

#### 文件详情

```
GET /api/files/{file_id}
```

返回完整字段：路径、哈希值、分辨率、视频时长等。手机 APP 可据此判断是否要显示"视频"角标。

#### 获取缩略图路径

```
GET /api/files/{file_id}/thumbnail
```

> ⚠️ **注意**：此端点返回的是**本地文件系统路径**（如 `D:\data\.thumbnails\101_thumb.jpg`），手机端无法直接使用。
>
> **手机端方案**：参见第 4 节"缩略图与媒体文件"。

#### 删除文件记录

```
DELETE /api/files/{file_id}
```

响应：

```json
{"success": true, "message": "已删除: 照片01.jpg"}
```

### 3.3 查重

#### 查重结果列表

```
GET /api/dedup/results?unresolved_only=true&page=1&per_page=20
```

参数：

| 参数 | 类型 | 默认 | 说明 |
|--------|------|--------|--------|
| `unresolved_only` | bool | `true` | 仅显示未处理的 |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量 |

#### 查重详情

```
GET /api/dedup/results/{result_id}
```

返回匹配文件对列表，供 APP 展示哪些文件被判定为重复：

```json
{
  "id": 1,
  "unit_a": { "id": 1, "name": "片段A" },
  "unit_b": { "id": 2, "name": "片段B" },
  "similarity_score": 1.0,
  "match_count": 2,
  "file_matches": [
    { "file_a_id": 101, "file_b_id": 103, "match_type": "md5", "similarity_score": 1.0 }
  ]
}
```

#### 处理查重结果

```
POST /api/dedup/results/{result_id}/resolve
```

请求体：

```json
{"resolution": "keep_a"}
```

`resolution` 可选值：

| 值 | 含义 |
|-----|---------|
| `keep_a` | 保留单元 A |
| `keep_b` | 保留单元 B |
| `merge` | 合并 |
| `whitelist` | 加入白名单，不再提示（同时写入白名单表，重跑自动跳过） |
| `ignore` | 暂时忽略 |

#### 触发查重（异步）

```
POST /api/dedup/run
```

请求体：

```json
{"unit_ids": [1, 2, 3], "threshold": 0.80}
```

立即返回 `202`：

```json
{"status": "accepted", "task_id": "dedup-1"}
```

随后轮询任务状态：

```
GET /api/dedup/run/{task_id}
```

```json
{
  "task_id": "dedup-1",
  "status": "completed",           // queued | running | completed | failed
  "progress": [12, 30],            // 比对对数进度（可选）
  "result": { "total_compared": 4, "duplicates_found": 1,
              "saved_count": 1, "skipped_pairs": 0, "elapsed_seconds": 2.1 },
  "error": null
}
```

说明：查重与桌面端“一键查重”共用进程内互斥门（同时仅一个任务）；
已处置/白名单的单元对在重跑时自动跳过，不再重复告警。

### 3.4 消息

#### 消息列表

```
GET /api/messages?unread_only=false&limit=50
```

响应：

```json
{
  "messages": [
    {
      "id": 1,
      "msg_type": "dedup_alert",
      "title": "发现重复单元",
      "body": "片段A ↔ 片段B (相似度: 100.0%)",
      "is_read": false,
      "created_at": "2026-05-08T20:00:00"
    }
  ],
  "unread_count": 1
}
```

`msg_type` 可选：`info` / `warning` / `error` / `dedup_alert`

#### 未读消息数

```
GET /api/messages/unread-count
```

用于 APP 首页显示红点角标。

#### 标记已读

```
POST /api/messages/{msg_id}/read
```

#### 全部标记已读

```
POST /api/messages/read-all
```

#### 关闭消息

```
DELETE /api/messages/{msg_id}
```

### 3.5 系统

#### 根端点

```
GET /
```

```json
{
  "name": "影视资源管理器 API",
  "version": "0.1.0",
  "status": "running",
  "docs": "/docs"
}
```

### 3.6 鉴权（设置访问密码后启用）

| 端点 | 说明 |
|------|------|
| `GET  /api/auth/status` | 查询是否启用密码 → `{"pin_required": true/false}` |
| `POST /api/auth/verify` | 请求体 `{"pin": "1234"}` → `{"verified": true, "token": "..."}`；错误返回 403 |
| `POST /api/auth/change-pin` | 请求体 `{"old_pin": "...", "new_pin": "..."}`；成功后旧令牌全部失效 |

免鉴权端点：`GET /api/health`、`GET /api/auth/status`、`POST /api/auth/verify`、
`POST /api/auth/change-pin`、CORS 预检（OPTIONS）。

### 3.7 未读事件简报（角标轮询）

```
GET /api/events/unread
```

返回：

```json
{
  "unread_count": 1,
  "has_dedup_alerts": true,
  "latest": { "id": 1, "msg_type": "dedup_alert", "title": "..." }
}
```

---

## 4. 缩略图与媒体文件

### 4.1 缩略图 HTTP 服务（已实现）

```
GET /api/files/{file_id}/thumbnail
```

返回缩略图图片二进制（`image/jpeg`），带 `Cache-Control: private, max-age=300`；
集中缓存（`data/.thumbnails/`）未命中时按需生成。HEIC/HEIF 源文件在服务端
转码为 JPEG 返回。不存在时返回 404。

### 4.2 客户端直接读取（同局域网文件共享）

手机 APP 可以通过 SMB/CIFS 协议直接访问电脑共享文件夹中的缩略图文件。

### 4.3 缩略图缓存位置

缩略图优先存储在 `data/.thumbnails/`（集中缓存），命名格式为 `{file_id}_thumb.jpg`：

```
data/.thumbnails/
├── 101_thumb.jpg
├── 102_thumb.jpg
└── ...
```

如果集中缓存不存在，则回退到资源单元文件夹下的 `.thumbnails/` 子目录。

---

## 5. SMB 文件共享（手机访问原始文件）

手机 APP 通过 REST API 获取元数据后，如果需要访问原始媒体文件（如播放视频、保存图片），推荐通过 **SMB 协议**直接读取电脑共享文件夹。

### 5.1 在电脑端创建共享

在桌面应用中选择「工具 → SMB 共享」打开管理对话框：

```
工具(T) → SMB 共享(S)...
```

填入：

| 字段 | 说明 |
|-------|--------|
| **文件夹** | 要共享的媒体库根目录（如 `D:\Media`） |
| **共享名** | 手机端访问时使用的名称（如 `Media_Files`） |
| **权限** | 默认设为 `everyone` 只读，手机端仅需读取 |

创建后，当前系统的所有 SMB 共享会显示在列表中。

> ⚠️ 创建/删除 SMB 共享需要**管理员权限**。如果提示失败，请右键以管理员身份运行本应用，或按对话框中的手动操作说明设置。

### 5.2 手机端连接

手机 APP 通过 SMB 客户端库连接共享：

```kotlin
// Android — JCIFS 或 SmbFile
val smbUrl = "smb://192.168.1.100/Media_Files/"
val auth = NtlmPasswordAuthentication(null, "guest", null as String?)
val dir = SmbFile(smbUrl, auth)
for (file in dir.listFiles()) {
    // 浏览文件
}
```

```swift
// iOS — 需要第三方库或通过 Files.app 手动连接
// 1. 打开「文件」APP
// 2. 点击右上角「⋯」→「连接服务器」
// 3. 地址: smb://192.168.1.100
// 4. 以「访客」身份连接
// 5. 找到对应的共享名称即可访问
```

### 5.3 媒体库根目录自动共享建议

如果媒体库有多个根目录，可依次为每个根创建共享，或直接共享它们的共同父文件夹。

手机 APP 可同时调用 REST API 获取元数据 + SMB 读取原始文件：

```
1. GET  /api/units                    → 获取单元列表及文件元数据
2. 根据单元 path 构造 SMB URL
3. 拼接文件名 → smb://192.168.1.100/Media_Files/2025-01-01/片段A/照片01.jpg
4. 通过 SMB URL 直接加载图片或流式播放视频
```

## 6. 移动端集成示例（手机 APP）

### 6.1 APP 首页加载

```
1. GET  /api/units           → 单元列表（使用 file_count 排序）
2. GET  /api/messages/unread-count  → 红点角标
```

### 6.2 浏览单元内容

```
1. GET  /api/units/{id}/files  → 文件列表
2. 对每个文件，构造缩略图 URL
3. 点击图片预览 → GET /api/files/{id}
```

### 6.3 查重通知与处理

```
1. GET  /api/messages?unread_only=true  → 获取未读 dedup_alert
2. GET  /api/dedup/results/{id}         → 获取重复匹配详情
3. POST /api/dedup/results/{id}/resolve  → 用户决策后提交处理结果
```

### 6.4 网络请求封装示例（Kotlin）

```kotlin
// Retrofit 接口定义
interface MediaApi {
    @GET("/api/health")
    suspend fun health(): HealthResponse

    @GET("/api/units")
    suspend fun getUnits(): UnitListResponse

    @GET("/api/units/{id}/files")
    suspend fun getUnitFiles(@Path("id") unitId: Int): UnitFilesResponse

    @GET("/api/files")
    suspend fun getFiles(
        @Query("unit_id") unitId: Int? = null,
        @Query("page") page: Int = 1,
        @Query("per_page") perPage: Int = 50,
    ): FileListResponse

    @GET("/api/messages")
    suspend fun getMessages(
        @Query("unread_only") unreadOnly: Boolean = false,
    ): MessageListResponse

    @POST("/api/messages/{id}/read")
    suspend fun markRead(@Path("id") msgId: Int): StatusResponse
}
```

### 6.5 网络请求封装示例（Swift）

```swift
// iOS URLSession 封装
struct MediaApi {
    let baseURL: String

    func getUnits() async throws -> [UnitItem] {
        let data = try await URLSession.shared.data(
            from: URL(string: "\(baseURL)/api/units")!
        ).0
        return try JSONDecoder().decode(UnitListResponse.self, from: data).units
    }
}
```

---

## 7. 限制与注意事项

| 限制 | 说明 | 缓解方案 |
|-----------|-------|----------------|
| **缩略图 HTTP 服务** | 已支持（见 4.1），`/api/files/{id}/thumbnail` 直接返回图片二进制 | 无需额外处理 |
| **文件下载/流媒体** | `/api/files/{id}/stream` 支持 Range 请求与常见容器格式 | HEVC/AV1 等需浏览器原生解码支持，或走 SMB |
| **鉴权** | 设置 4 位 Web 密码后 `/api/*` 强制 Bearer 令牌 | token 由 verify 签发，12h 有效；修改密码即失效 |
| **搜索** | API 不支持模糊搜索文件名 | 可在 APP 端拉取数据后本地过滤 |
| **分页** | 部分端点有 hard limit（如 per_page <= 200） | 如有更大需求可回调参 |
| **变更通知** | 无 WebSocket 推送 | APP 侧定时轮询 `/api/events/unread` |

### 推荐的 API 轮询策略

APP 在后台时，建议每 30-60 秒轮询一个轻量端点来检测状态变化：

```
GET /api/messages/unread-count  →  {"unread_count": N}
```

如果计数变化，再拉取详细数据。避免对 `/api/units` 和 `/api/files` 频繁轮询。
