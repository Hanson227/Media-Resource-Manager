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

**查询参数回退**：`<img>`/`<video>` 等标签发起的请求（缩略图 `/api/files/{id}/thumbnail`、
视频流 `/api/files/{id}/stream`）无法携带自定义请求头，这类请求可改用
查询参数传递令牌：`?token=<token>`。两种方式对服务端等价。

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
  match_count,               // 匹配的文件对数（不含人脸线索）
  match_types,               // 如 "md5,phash"；取值 md5 / phash / dhash / video / face
  match_level,               // "duplicate" | "related" —— 命中等级，决定是否建议处置
  is_resolved,               // 是否已处理
  resolution,                // "pending" | "keep_a" | "keep_b" | "merge" | "whitelist" | "ignore"
}
```

> ⚠️ **`match_level` 决定客户端的处置入口**：
> - `duplicate`：杰卡德 ≥ 阈值，**建议保留一份**，可提供"保留 A / 保留 B"。
> - `related`：未达阈值但证据足够（同演员 / 同场景 / 部分文件重叠），
>   **仅提醒、不建议删除**。桌面端对这类结果不提供"保留 A/B"，客户端也应如此，
>   否则等于暗示用户删掉另一侧。实测 166 个单元会产生 200+ 对 related，
>   建议按 `level` 分开取，不要和 duplicate 混在一页里。
>
> 人脸匹配（`match_type=face`）不计入杰卡德：人脸向量回答的是"是不是同一个人"，
> 不是"是不是同一个文件"，只作为线索返回（详见 3.3 详情接口的 `is_hint`）。

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
      "status": "active",
      "library_root_id": 1,
      "library_root_name": "Media",
      "cover_file_id": 101,
      "created_at": "2026-01-01T10:00:00",
      "content_modified_at": "2025-12-30T18:22:31"
    }
  ]
}
```

> `created_at` 为单元入库时间；`content_modified_at` 为单元内媒体文件的
> 最新真实修改时间（max st_mtime），**按日期排序/显示应使用该字段**——
> 文件夹被移动后其文件系统时间戳会失真，此字段不受影响。
> `content_modified_at` 为 null 时（未采集/文件全部不可访问）回退 `created_at`。

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

#### 删除资源单元（文件夹）

```
DELETE /api/units/{unit_id}?mode=trash
```

| 参数 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `mode` | str | `trash` | `trash`=整个文件夹移至回收站 + 删库记录（与桌面端「删除文件夹」一致）；`record`=仅删除数据库记录，磁盘文件夹保留；`delete`=**永久删除**整个文件夹（不可恢复，仅在回收站不可用时由用户明确选择） |

响应：

```json
{"success": true, "message": "已删除文件夹: 片段A（已移至回收站）"}
```

> 删除会级联清理该单元的 `media_files` / `face_vectors` / `video_frames` /
> 关联的 `dedup_results`。磁盘删除失败（如网络路径无法进回收站、或**磁盘未连接**）
> 时返回 `409`，**数据库记录保持不变** —— 不会出现"记录没了但文件还在"的孤儿状态，
> 也不会出现"文件其实还在、重扫又回来"的假删除。单元不存在返回 `404`。

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

#### 删除文件

```
DELETE /api/files/{file_id}?mode=trash
```

| 参数 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `mode` | str | `trash` | `trash`=移至回收站 + 删库记录（与桌面端一致，**默认**）；`record`=仅移除媒体库记录，磁盘文件保留；`delete`=**永久删除**磁盘文件（不可恢复，仅在回收站不可用时由用户明确选择） |

响应：

```json
{"success": true, "message": "已删除: 照片01.jpg（已移至回收站）"}
```

> 先移回收站、成功后才删数据库记录：磁盘删除失败时返回 `409` 且记录保留，
> 避免用户再也看不到这个文件。顺带清理缩略图缓存并重算所属单元的
> `file_count` / `total_size`。文件不存在返回 `404`。
>
> **磁盘未连接时也返回 409**（`detail` 含"未连接"）：库在可移动盘/网络盘上时，
> 盘没插会让 `Path.exists()` 为 False —— 若按"文件已不存在"处理，库记录会被删掉，
> 而磁盘文件其实还在，一重扫（重新插盘后）文件又全回来，用户看到的就是"删不掉"。
> 客户端据此提示"请先连接磁盘"，而不是弹出永久删除选项（盘都没连，永久删除也做不到）。
>
> **`mode=delete` 何时用**：网络盘 / exFAT 等没有 `$RECYCLE.BIN` 的卷上
> `send2trash` 必然失败（`mode=trash` 恒回 409）。此时应由客户端**询问用户**后
> 显式传 `mode=delete`（永久删除）或 `mode=record`（只出库）。
> 服务端**绝不**在 trash 失败时自动降级为永久删除 —— 那等于无声销毁数据。

#### 批量删除文件

```
POST /api/files/batch-delete
```

请求体：

```json
{"file_ids": [101, 102, 103], "mode": "trash"}
```

响应（部分失败也返回 `200`，逐条给出原因）：

```json
{
  "success": false,
  "deleted": [101, 102],
  "failed": [{"file_id": 103, "reason": "无法移至回收站: ..."}],
  "message": "已删除 2 个文件，1 个失败"
}
```

> `file_ids` 会自动去重；重复 ID 不会被记成"文件不存在"的失败项。
> 磁盘删除失败时**成功项照常落库**，失败项记录保留 —— 客户端可以对
> `failed` 里的 ID 再询问一次"永久删除 / 仅移除记录"。

### 3.3 查重

#### 查重结果列表

```
GET /api/dedup/results?unresolved_only=true&level=duplicate&page=1&per_page=20
```

参数：

| 参数 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `unresolved_only` | bool | `true` | 仅显示未处理的 |
| `level` | str | - | 按命中等级过滤：`duplicate` / `related` |
| `evidence` | str | - | 按证据来源过滤：`file`=有文件重叠（真正的疑似相关）；`face`=只有人脸线索（同演员，杰卡德恒为 0）。省略则两类混在一起 |
| `unit_a_id` | int | - | 只看以该单元为左侧（较小 ID）的结果，用于展开某个分组 |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（<= 100） |

列表项关键字段（列表页展示分档与"重叠 N/M"用的就是这些）：

```json
{
  "id": 250, "unit_a_id": 124, "unit_b_id": 161,
  "unit_a_name": "抖音颜值小姐姐", "unit_b_name": "清纯女大減爸爸",
  "similarity_score": 0.6444, "overlap_ratio": 0.8788,
  "match_count": 29, "face_hint_count": 9,
  "total_files_a": 33, "total_files_b": 41,
  "match_types": "md5", "match_level": "duplicate", "evidence_kind": "file",
  "stale": false
}
```

> - `match_count` **只统计计入判定的文件对**（不含人脸线索），人脸线索条数看
>   `face_hint_count`。历史版本把两者相加存进 `match_count`，导致"46 处线索"
>   与"匹配 46 个文件"对不上（实际入库 38 条）。
> - `overlap_ratio` = `match_count / min(total_files_a, total_files_b)`：杰卡德
>   只能表达"两个目录几乎一样"，表达不了"一边是另一边的子集"。只认杰卡德时
>   包含度 0.88 的真重复会被降级成"疑似相关"。
> - `stale=true` 表示这条是升级前的旧规则结论（或 `match_count` 超过了文件数，
>   即旧口径残留）：**不要展示比例/重叠数**，提示用户重新查重。`/groups` 的
>   每个分组也带 `stale`。

#### 未处置结果分级计数

```
GET /api/dedup/counts
```

响应：

```json
{"duplicate": 2, "related": 4, "face_only": 57, "total": 63,
 "stale": 380, "result_version": 2}
```

| 键 | 含义 |
|-----|------|
| `duplicate` | 建议处置的重复（含"子集重复"） |
| `related` | 有文件重叠的疑似相关 |
| `face_only` | 只有人脸线索的同演员（杰卡德恒为 0） |
| `stale` | **旧规则算出的结论**条数（升级后还没重跑查重） |
| `result_version` | 当前分级算法版本（与列表项的 `computed_version` 对应） |
| `total` | 前三者之和 |

> `stale > 0` 时客户端**必须**显式提示"需要重新查重"：升级只做分类搬迁，
> 重算需要跑一次比对。实测踩到：旧行 `match_count=46` 配 33 个文件的单元，
> 界面把它渲染成"46/33 个文件重叠"，用户以为界面递归/坏掉了。

#### 按来源单元分组（列表页二级分组的父级）

```
GET /api/dedup/groups?unresolved_only=true&level=related&evidence=file&page=1&per_page=50
```

```json
{
  "groups": [
    {"anchor_unit_id": 124, "anchor_unit_name": "抖音颜值小姐姐",
     "anchor_cover_file_id": 101, "pair_count": 19,
     "max_similarity": 0.6444, "match_types": "md5",
     "match_level": "duplicate", "evidence_kind": "file"}
  ],
  "total": 52
}
```

> 分组键是**存储时较小的 unit_id**，也就是卡片左侧那个单元 ——
> 用户看到的"左边相同、右边不同"直接折叠成一组。展开某组时用
> `GET /api/dedup/results?...&unit_a_id=<anchor>` 取子项。

#### 人脸重扫状态 / 触发（精查候选 · 补扫全部）

视频人脸从"只取中间一帧"改为**定间隔多帧**（`config.face_video_max_frames`，默认 10 帧、
均匀铺满全片）后，旧库里的人脸向量需要用新策略重扫才有准确线索。

```
GET /api/dedup/face-scan/status
→ {"videos_total": 1142, "videos_pending": 977, "candidates": 0,
   "faces_total": 977, "scan_version": 1}

POST /api/dedup/face-scan
{"scope": "candidates", "run_dedup": true}
→ 202 {"status": "accepted", "task_id": "face-1"}
```

| 字段 | 说明 |
|--------|------|
| `scope` | `candidates`=只重扫上一轮出现过人脸线索的视频（实测 165 个，约 2~3 分钟）；`all`=全部抽帧策略过期的视频（约十几分钟） |
| `run_dedup` | 重扫后是否自动重跑一次查重（`false` 时结果仍是旧的） |

进度与结果同样用 `GET /api/dedup/run/{task_id}` 轮询（`phase=faces`），完成后
`result` 为 `{"scope": "...", "scanned": N, "faces": M, "dedup": {...}}`。

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
  "overlap_ratio": 1.0,
  "match_count": 2,
  "face_hint_count": 1,
  "total_files_a": 2,
  "total_files_b": 3,
  "match_level": "duplicate",
  "evidence_kind": "file",
  "file_matches": [
    {
      "file_a_id": 101, "file_b_id": 103,
      "file_a_name": "照片01.jpg", "file_b_name": "照片03.jpg",
      "match_type": "md5", "similarity_score": 1.0,
      "is_hint": false, "frame_support": 1
    },
    {
      "file_a_id": 102, "file_b_id": 104,
      "file_a_name": "照片02.jpg", "file_b_name": "照片04.jpg",
      "match_type": "face", "similarity_score": 0.71,
      "is_hint": true, "frame_support": 3
    }
  ],
  "unmatched_a": [ { "file_id": 105, "filename": "独有A.jpg", "media_type": "image", "size_bytes": 204800 } ],
  "unmatched_b": [ { "file_id": 106, "filename": "独有B.jpg", "media_type": "image", "size_bytes": 198400 } ]
}
```

> `is_hint=true` 的条目是**线索而非重复证据**（人脸相似），不计入
> `similarity_score` 与 `match_count`，客户端应单独展示并说明"未计入重复判定"。
> `frame_support` 是支持这条人脸线索的**帧数**：视频改多帧抽帧后，同一文件对在
> 多个时间点都被判为同一张脸时该值 >1，比单帧偶然相似可信。
> 缩略图用 `GET /api/files/{file_id}/thumbnail`（`<img>` 无法带 Authorization 头，
> 用 `?token=` 回退）。
>
> `unmatched_a` / `unmatched_b` 是**两侧独有文件**（不在任何 counted 匹配对里，
> face 线索涉及的两侧行也归入此列）：匹配网格只展示"成对"的文件，用户看不见
> 合并时会被移动的那部分，客户端应把这两列一并展示。合并端点（见下文）正是
> 把"被并入侧"的 unmatched 移动到保留侧、counted 匹配对进回收站。

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
| `keep_a` | 保留单元 A（仅 `duplicate` 适用） |
| `keep_b` | 保留单元 B（仅 `duplicate` 适用） |
| `merge` | 合并 |
| `whitelist` | 加入白名单，不再提示（同时写入白名单表，重跑自动跳过） |
| `ignore` | 暂时忽略 |

> `match_level=related` 的记录**不要**提供"保留 A / 保留 B"入口：related 的语义是
> "仅供参考、不建议删除"，给出保留按钮等于诱导用户删掉另一侧。

#### 合并重复单元（一键合并，真正执行文件操作）

```
POST /api/dedup/results/{result_id}/merge
```

与 `resolve` 的 `resolution=merge`（**只打标记、不动文件**）不同，本端点把
两个重复文件夹真正合并成一个：

- **保留侧**（`keep` 选 `a` 或 `b`）的文件夹原样不动，作为合并目标；
- **被并入侧**的独有文件（unmatched）**移动**到保留侧文件夹；
- **counted 匹配对**中被并入侧的文件**进回收站**（可逐文件豁免，见 `retain_matches`）；
- face 线索（`is_hint=true`）不算 counted 匹配 → **纯移动、零删除**；
- 全部成功后：删除被并入侧的空目录 → 单元标记 `merged`（`status='merged'`,
  文件记录挂到保留侧单元下）→ 结果落 `resolution='merge'` → 其它涉及这两个
  单元的**未处置**结果标记为过期（`computed_version=0`，重跑查重时收敛）。

请求体：

```json
{"keep": "a", "retain_matches": [104, 108]}
```

| 字段 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `keep` | str | 必填 | 保留哪一侧：`a` / `b`。合并目标由用户在界面上选 |
| `retain_matches` | int[] | `[]` | "两份都保留"豁免清单：填**被并入侧**的文件 ID，这些匹配对文件不进回收站，而是同样移动到保留侧（重名时自动改名 `stem (2).ext`） |

响应（`200`；部分失败也是 200，靠 `success=false` + `failed` 明细表达）：

```json
{
  "success": true,
  "message": "合并完成",
  "kept_unit_id": 124, "merged_unit_id": 161,
  "moved_count": 4, "trashed_count": 29,
  "renamed": [ { "file_id": 106, "original": "same.jpg", "renamed": "same (2).jpg" } ],
  "failed": [],
  "merged_dir_removed": true
}
```

| 字段 | 说明 |
|--------|------|
| `moved_count` | 实际移动到保留侧的文件数（unmatched + retain_matches 豁免） |
| `trashed_count` | 实际进回收站的文件数 |
| `renamed` | 重名冲突自动改名的明细（磁盘与库内 `media_files.path` 均已按新名落盘） |
| `failed` | 逐文件失败明细 `[{"file_id", "filename", "reason"}]`；部分失败时成功项照常落库，**可重试**（幂等：已移走的不再重复算） |
| `merged_dir_removed` | 被并入侧空目录是否已删除（目录里还有文件时为 false，目录保留） |

错误码：

| 状态 | 场景 |
|--------|------|
| `404` | 查重结果不存在 / 单元记录不存在（提示重新扫描） |
| `409` | 已处置且 `resolution != 'merge'`（merge 允许重入，供收尾失败后的幂等重试） |
| `409` | 任一侧磁盘离线（请先连接该磁盘） |
| `409` | 两个文件夹不在同一磁盘卷（暂不支持跨盘合并） |
| `409` | 目标文件夹不存在 / 被并入文件夹不存在且库中还有该单元的文件 |
| `409` | 已有查重/人脸任务在跑（互斥门被占，稍后再试） |

> 合并是**同步**端点：文件量小时秒回，量大时客户端应适当放宽超时。
> 执行期间持有与查重相同的进程内互斥门。磁盘操作策略与单个文件删除一致：
> **先动磁盘、成功后更新库**，回收站语义（send2trash）。

#### 触发查重（异步）

```
POST /api/dedup/run
```

请求体：

```json
{"unit_ids": [1, 2, 3], "threshold": 0.80, "index_first": true,
 "face_similarity_threshold": 0.45, "refine_faces": true}
```

| 字段 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `unit_ids` | int[] | - | 参与比对的单元 ID，至少 2 个 |
| `threshold` | float | 0.80 | 杰卡德阈值 |
| `index_first` | bool | `false` | 先为**未索引**文件计算哈希（MD5/视频帧/人脸）再比对。桌面端「一键查重」即此行为；为 `false` 时未索引文件对查重完全不可见却会返回"完成"，第三方客户端建议传 `true` |
| `face_similarity_threshold` | float | - | 本次查重的人脸相似度阈值（覆盖 config.json）。不影响抽帧，只影响「同演员线索」的松紧：0.363=官方最宽松，0.45=推荐，0.50/0.60=更严 |
| `refine_faces` | bool | `false` | 比对后对"上一轮出现过人脸线索的视频"做多帧精查，然后**重新比对一次**（约 2~3 分钟，候选重扫过之后会瞬间跳过） |

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
  "kind": "dedup",                  // dedup=查重；face=人脸重扫
  "status": "running",              // queued | running | completed | failed
  "phase": "indexing",              // indexing=补索引；comparing=比对；faces=人脸精查；null=未开始/已结束
  "progress": [12, 30],             // 当前阶段进度（索引/人脸=文件数，比对=单元对数）
  "result": null,
  "error": null
}
```

完成后：

```json
{
  "task_id": "dedup-1",
  "status": "completed",
  "phase": null,
  "progress": null,
  "result": {
    "total_compared": 4,
    "duplicates_found": 1,          // 命中"重复"的单元对（含子集重复）
    "related_found": 12,            // 有文件重叠的"疑似相关"（仅提醒）
    "face_only_found": 373,         // 只有人脸线索的"同演员"（杰卡德恒为 0）
    "saved_count": 1, "related_saved": 12, "face_only_saved": 373,
    "pruned_stale": 2,              // 本轮范围内未再命中、被清理掉的旧结果行数
    "skipped_pairs": 0, "elapsed_seconds": 2.1,
    "unindexed_before": 1362,       // 本次开始前待索引文件数
    "indexed_count": 1362,          // 本次实际索引的文件数
    "face_similarity_threshold": 0.45
  },
  "error": null
}
```

说明：查重与桌面端“一键查重”共用进程内互斥门（同时仅一个任务）；
已处置/白名单的单元对在重跑时自动跳过，不再重复告警。
`index_first=true` 时的索引与桌面端哈希线程共用 `services/index_service.py`，
分批取未索引文件直到取空（`INDEX_BATCH_SIZE=1000`），不会出现"只算了 1000 个"的残缺结论。
每次落库后会清理**本轮比对范围内、未被处置、且本次未再命中**的旧结果行
（`pruned_stale`）—— 否则分类规则/阈值一变，旧的 380 条 related 会永远留在列表里。
被清理结果的 `dedup_result_id` 若还挂在旧消息的 `action_data` 上，客户端应容忍
"查重结果不存在"的空态。

#### 当前在跑的任务

```
GET /api/dedup/active-task
```

```json
{"task_id": "dedup-3", "kind": "dedup", "status": "running",
 "phase": "comparing", "progress": [12, 30]}
```

没有任务在跑时返回 `{"task_id": null, "status": null, "kind": null}`。
任务跑在服务端后台线程里，客户端刷新/换设备/重新打开页面后靠这个端点重新接上
进度条（否则页面丢了 `task_id` 就只能靠风扇判断在不在跑）。互斥门保证同时最多一个，
真出现多个时返回最新创建的那个。

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

### 3.8 HEIC 检查与转换

浏览器与 OpenCV 都不原生支持 HEIC/HEIF，而这类文件（iPhone 拍摄、Telegram 下载）
在很多媒体库里成批存在。这一组端点提供"检查全库 HEIC → 批量转 JPG → 同步数据库/
索引/缩略图"的完整能力，与桌面端「工具 → HEIC 检查与转换」共用同一套编排
（`app/services/heic_service.py`）。

#### 检查 HEIC（只读）

```
GET /api/heic/scan?include_disk=true
```

| 参数 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `include_disk` | bool | `true` | 同时遍历磁盘，补上**还没入库**的 HEIC（只看数据库会漏掉从未扫描到的文件） |
| `root_ids` | string | - | 逗号分隔的媒体库根 ID；省略则扫描全部启用根 |

```json
{
  "total": 40, "total_bytes": 24019678,
  "from_db": 39, "from_disk_only": 1,
  "broken_hash_count": 19,
  "missing_count": 0, "invalid_header": 1,
  "skipped_volume_roots": [], "roots_scanned": ["G:\\Guofu"],
  "items": [
    {"path": "G:\\Guofu\\4.15\\xx\\IMG_3103.HEIC", "filename": "IMG_3103.HEIC",
     "file_id": 476, "size_bytes": 57304, "unit_id": 25, "unit_name": "xx",
     "in_library": true, "exists": true, "volume_ok": true,
     "has_perceptual_hash": false}
  ]
}
```

字段说明：

| 字段 | 含义 |
|--------|------|
| `broken_hash_count` | pHash/dHash 是**失败占位空串**的数量。这类行不会被自动重算（`get_unindexed_files` 只认 NULL），必须由「修复坏哈希」显式重置 —— 它们此前在查重里完全不可见 |
| `invalid_header` | 扩展名是 `.heic/.heif` 但文件头不是 HEIC（实测真实库里有一个是改名的 JPEG），已从候选中剔除 |
| `missing_count` | 库里有记录但磁盘上已找不到（卷可达时才算） |
| `skipped_volume_roots` | **磁盘未连接**的根目录。客户端必须显式提示"插回磁盘"，不能把掉线显示成"没有 HEIC" |
| `has_perceptual_hash` | 是否已有非空 pHash（`false` 时该文件在查重里只能靠 MD5 被看见） |

#### 触发转换（异步）

```
POST /api/heic/convert
```

```json
{"paths": ["G:\\Guofu\\...\\IMG_3103.HEIC"],
 "options": {"quality": 95, "source_action": "trash", "on_conflict": "skip",
             "reindex": true, "fix_broken": true}}
```

| 字段 | 类型 | 默认 | 说明 |
|--------|------|--------|------|
| `paths` | string[] | - | 待转换的 HEIC 绝对路径，必须位于**启用中的媒体库根**下（越界返回 400） |
| `quality` | int | `95` | 输出 JPEG 质量 |
| `source_action` | string | `trash` | `trash`=移至回收站 / `delete`=永久删除 / `keep`=保留源 HEIC。**回收站不可用（网络盘、无回收站分区）时服务端保留源文件**并在逐项结果里给出 `source_kept_reason`，绝不静默降级为永久删除 |
| `on_conflict` | string | `skip` | 同名 JPG 已存在时：`skip`=跳过（推荐，两边都留）/ `rename`=自动加序号 / `overwrite`=覆盖 |
| `reindex` | bool | `true` | 转换后立即重算哈希与人脸 |
| `fix_broken` | bool | `true` | 同时修复"哈希为空串"的旧 HEIC 坏行 |

路径校验失败返回 `400`（不在媒体库范围内）/ `404`（文件不存在）/
`409`（磁盘未连接）/ `415`（不是 HEIC）。已有重活（查重 / 人脸重扫 / 另一次
HEIC 转换）在跑时返回 `409`：三者共用同一道进程内互斥门。

立即返回 `202`：

```json
{"status": "accepted", "task_id": "heic-3", "count": 40}
```

#### 轮询任务

```
GET /api/heic/run/{task_id}
```

```json
{
  "task_id": "heic-3", "kind": "heic", "status": "running",
  "phase": "converting",        // converting=转换 | source=处置源文件 | db=同步记录 | indexing=重算哈希/人脸
  "progress": [12, 40],
  "result": null, "error": null, "cancel_requested": false
}
```

完成后：

```json
{
  "status": "completed", "phase": null, "progress": null,
  "result": {
    "total": 40, "converted": 38, "skipped": 2, "failed": 0,
    "sources_removed": 38, "sources_kept": 0,
    "db_updated": 38, "healed": 19, "reindexed": 57,
    "cancelled": false, "elapsed_seconds": 12.4,
    "items": [{"path": "...", "target": "...jpg", "status": "converted",
               "error": "", "source_kept_reason": ""}]
  }
}
```

说明：

- `db_updated`：**就地改写**为 `.jpg` 的媒体库记录数（`file_id` 不变，因此
  `file_tags` 标签关联与星标都不丢）；库里没有的文件（`from_disk_only`）不需要同步。
- `sources_kept`：源文件被保留的数量。按设置保留、或回收站不可用都会计入，
  逐项 `source_kept_reason` 给出原因。
- 取消是**协作式**的：`POST /api/heic/cancel` 后当前文件处理完即停止，
  结果里 `cancelled=true`，未处理的文件不记为成功。

#### 当前在跑的 HEIC 任务

```
GET /api/heic/active-task
```

```json
{"task_id": "heic-3", "kind": "heic", "status": "running",
 "phase": "converting", "progress": [12, 40]}
```

没有任务时返回 `{"task_id": null, "status": null, "kind": null}`。
只返回 HEIC 类任务，不会误报查重/人脸重扫（那些走 `/api/dedup/active-task`）。

#### 取消转换

```
POST /api/heic/cancel
```

```json
{"status": "cancelling", "task_id": "heic-3"}
```

空闲时返回 `{"status": "idle", "task_id": null}`（不报错，便于客户端无脑调用）。

---

### 3.9 媒体库与数据目录（便携版）

便携版没有桌面 GUI，"添加媒体文件夹 / 扫描 / 修盘符"全部由这些端点承担。
客户端应先取 `/api/features` 决定显示哪些入口。

#### 能力开关（客户端启动时拉一次）

```
GET /api/features
```

```json
{
  "app": "media-manager",
  "version": "0.1.0",
  "profile": "lite",
  "dedup": true,
  "dedup_video": true,
  "hash_index": true,
  "face": false,
  "video": true,
  "heic": true,
  "raw": true,
  "scan": true,
  "desktop": false,
  "capabilities": {"image": true, "cv2": true, "heic": true,
                   "raw": true, "face_models": false, "qt": false},
  "capability_notes": ["未打包人脸模型：人脸线索不可用（需要时打包设 MEDIA_BUILD_WITH_FACE=1）"],
  "data_root": "E:\\MediaData",
  "data_root_status": "configured",
  "expected_data_root": "",
  "db_path": "E:\\MediaData\\data\\media_manager.db",
  "thumbnail_cache_dir": "E:\\MediaData\\data\\.thumbnails"
}
```

- **这些开关全部来自运行时能力探测**（`app/utils/capabilities.py`：包里有没有
  Pillow / cv2 / pillow-heif / rawpy / 人脸模型 / PySide6），**不是运行档位**：
  便携版与桌面档同一套代码、同一套功能，`profile` 只是"这是哪个包"的标签。
  `/api/dedup/*` **在任何打包组合下都已注册**（文件级查重只需要 Pillow）；
  只有 `dedup`/`video`/`face` 等**能力**会随编译开关（`MEDIA_BUILD_NO_CV2` /
  `MEDIA_BUILD_WITH_FACE`）变化；
- 客户端隐藏入口的规则：`dedup=false` 隐藏查重、`video=false` 不请求视频缩略图、
  `face=false` 隐藏人脸线索、`heic=false` 隐藏 HEIC 工具、`desktop=false` 提示无桌面界面；
  `capability_notes` 是可直接展示给用户的降级原因清单；
- `data_root_status` 取值：`explicit` / `app_dir` / `configured` / `discovered` /
  `missing_expected` / `none`。**`missing_expected` 必须在界面上醒目提示**
  （数据盘没插或盘符变了，当前显示的很可能是空库）；
- `desktop` 表示本机是否具备桌面 GUI 运行条件（便携版包为 `false`）。

`GET /api/health` 返回 `{"status": "ok", ...同上字段}`，可作为免鉴权的探活端点。
`GET /api/settings` 也带 `capabilities` / `capability_notes`，另含 `api_host` /
`api_port` / `lan_urls` / `loopback_only`（手机访问地址，见 §3.10）。

#### 卷（盘符）列表

```
GET /api/library/volumes
```

```json
{"volumes": [
  {"path": "E:\\", "label": "Media", "serial": "1A2B3C4D",
   "total": 1000204886016, "free": 73400320000, "contains_media": true}
]}
```

#### 目录浏览（服务端文件系统）

```
GET /api/library/browse?path=E:\Media
```

```json
{"path": "E:\\Media", "parent": "E:\\",
 "entries": [{"name": "2025", "path": "E:\\Media\\2025",
              "is_media_dir": true, "media_count": 128}],
 "is_media_dir": false, "media_count": 0, "error": ""}
```

`path` 必须是绝对路径（相对路径返回 400；目录不存在时 `error` 说明原因，
磁盘未连接时为"磁盘未连接"）。单次最多返回 400 个子目录。

#### 媒体库根目录

```
GET /api/library/roots
```

```json
{"roots": [
  {"id": 1, "path": "D:\\Media", "enabled": true, "reachable": false,
   "volume_present": true, "reason": "path_missing",
   "reason_text": "目录不存在（盘符可能已变化）", "unit_count": 42,
   "candidates": [
     {"path": "E:\\Media", "kind": "same_relative", "media_count": 0,
      "confidence": "high"}]}]}
```

- `reason`：`volume_offline`（盘没插，先插盘）或 `path_missing`（盘在、目录没了，
  通常是盘符变了，可改库）；
- `candidates[].kind`：`same_relative`（卷根之后结构一致，最可信）或
  `name_match`（按末级目录名找到）。

```
POST /api/library/roots        {"path": "E:\\Media"}
```

```json
{"success": true, "root_id": 3, "created": true, "needs_remap": false,
 "old_path": null, "candidates": [], "message": "已添加媒体库: E:\\Media"}
```

若库里存在**同尾但不可达**的旧根（换盘符后直接添加），返回
`success=false, needs_remap=true` 并附 `old_path` 与候选 —— 客户端应先引导用户
改库，而不是当作新库继续（否则库里会多出一套同名根+单元，旧记录永远清不掉）。

```
DELETE /api/library/roots/{root_id}
```

移除媒体库（默认级联删除库内单元/文件记录，**不动磁盘文件**）。

#### 路径改库（盘符变化）

```
POST /api/library/remap
{"old_prefix": "D:\\Media", "new_prefix": "E:\\Media",
 "dry_run": true, "include_whitelist": false}
```

dry-run 响应：

```json
{"success": true, "dry_run": true,
 "plan": {"old_prefix": "D:\\Media", "new_prefix": "E:\\Media",
          "counts": {"library_roots": 1, "units": 42, "files": 1280,
                     "unit_covers": 2, "whitelist": 0},
          "total_rows": 1323, "conflicts": [], "thumbnail_meta_hits": 1280,
          "old_exists": false, "new_exists": true,
          "same": false, "blocking": false, "reason": ""},
 "updated": {}, "thumbnail_meta": 0, "backup": null, "error": ""}
```

`dry_run=false` 执行（改前自动备份数据库，返回 `backup` 路径）：

- 冲突（目标位置已有另一套记录）→ `success=false` + `error`，需先删除重复的库/单元；
- 幂等：重复执行返回全 0 行、无副作用；
- 会**同步改写缩略图缓存 `.meta` 里的源路径**（`thumbnail_meta` 为改写条数），
  否则盘符一变所有缓存归属校验都会失配。

#### 扫描（长任务）

```
POST /api/library/scan         {"path": "E:\\Media"}      # 或 {"root_id": 3}
```

```json
{"task_id": "scan-1", "status": "queued", "path": "E:\\Media", "message": "扫描已开始"}
```

```json
GET /api/library/scan/{task_id}
{
  "task_id": "scan-1", "kind": "scan", "status": "running",
  "phase": "scanning", "progress": [128, 640], "result": null, "error": null
}
```

完成时 `result`：

```json
{"total_files": 640, "total_size_bytes": 7340032000, "unit_count": 42,
 "new_files": 12, "updated_files": 628, "stale_files": 3,
 "excluded_units": [], "errors": []}
```

- `GET /api/library/scan/active-task`：当前仍在跑的任务（页面切走再回来接上进度；
  空闲时 `task_id` 为 null）；
- `POST /api/library/scan/cancel`：协作式取消（扫描器在下一个回调点停止，且不落库）；
- 互斥：扫描 / HEIC 转换 / 改库 / 查重同时只允许一个，冲突时返回 `409`；
- 路径守卫：待扫描路径与"同尾不可达旧根"等价时，任务 `status=failed`、
  `phase=needs_remap`、`result.needs_remap=true` 并带 `old_path` 与 `candidates`；
  客户端应引导先改库；确实要作为新库扫描时传 `force_new_root=true`。

#### 设置与数据目录

```
GET /api/settings
```

```json
{"profile": "lite", "data_root": "E:\\MediaData", "data_root_status": "configured",
 "expected_data_root": "", "db_path": "E:\\MediaData\\data\\media_manager.db",
 "thumbnail_cache_dir": "E:\\MediaData\\data\\.thumbnails",
 "thumbnail_cache_files": 1461, "thumbnail_cache_bytes": 19922944,
 "db_bytes": 7012352, "config_path": "D:\\App\\config.json",
 "restart_required_hint": "修改数据根后需要重启程序才会切换数据库",
 "capabilities": {"image": true, "cv2": true, "heic": true, "raw": true,
                  "face_models": false, "qt": false},
 "capability_notes": ["未打包人脸模型：人脸线索不可用…"]}
```

#### 探测目标目录（决定"接管"还是"迁移"）

```
GET /api/settings/data-root/probe?path=E:\MediaData
```

```json
{"path": "E:\\MediaData", "exists": true, "is_dir": true, "writable": true,
 "has_marker": true, "data_root_id": "6b1f…",
 "db_path": "E:\\MediaData\\data\\media_manager.db",
 "db_exists": true, "db_bytes": 7012352, "db_ok": true,
 "db_detail": "完整（42 个单元 / 640 个文件）", "units": 42, "files": 640,
 "thumbnail_files": 1461, "is_data_root": true, "is_current": false}
```

`is_data_root=true` → 只能**接管**；否则才走迁移。相对路径 → 400。

#### 扫描数据盘（“选择数据盘”/换盘符后用）

```
GET /api/settings/data-root/volumes?deep=false&volume=F:\
```

每个就绪卷列出：盘符、卷标、序列号、容量，以及**卷上找到的数据根**（含单元/文件数、
是否当前在用、是否与配置里记的 `data_root_id` 同一身份）。`deep=true` 把搜索深度从
默认 2 提到 4（界面的「深扫」按钮）；`volume` 可只扫某一块盘。

```
POST /api/settings/data-root      {"path": "E:\\MediaData", "mode": "copy"}
```

把数据库 / 缩略图 / 访问密码迁移到新的数据根（`mode=copy` 保留源；`move`
校验通过后删源；`mode=adopt` 接管该目录里**已有**的数据；`mode=create` 在空目录里新建数据根——会得到空库，仅在向导里显式确认后使用）。返回：

```json
{"success": true, "target": "E:\\MediaData", "mode": "copy",
 "restart_required": true, "steps": {"database": {"ok": true, "message": "完整（42 个单元 / 640 个文件）"}},
 "message": "数据已复制到 E:\\MediaData；重启程序后生效（源目录数据保留…）"}
```

- **目标目录已有数据时 `copy`/`move` 一律 409**（`detail` 提示改用 `mode=adopt`），
  避免用当前（可能是空的）库覆盖用户迁过去的数据；
- `adopt` 要求目标目录有 marker 或一份可用数据库，**不写入 / 不覆盖**目标库，只在
  接管前备份一次（`<db>.bak_<时间戳>_adopt`），并要求目标目录可写；
- `move` / `adopt` 需要释放数据库引擎才能换库，此时服务端会立刻在新库上重新
  初始化并返回 `restart_required=false`（接管后立即可用）；
- `create` 只接受**空目录**：现场写 marker、建空库、切配置；已有数据的目录直接拒绝；
- 便携版入口**找不到数据根**时以 `data_root_status=blocked_no_root` 启动：
  上述端点与 `/api/features`、`/api/auth/*`、`/api/library/{browse,volumes}` 可用，
  其余数据端点一律 **409**（不允许在临时空库里扫描/写入/新建）。
  接管成功后闸门立即解除（`data_root_status=configured`）。

```
POST /api/settings/thumbnail-cache   {"path": "E:\\MediaData\\data\\.thumbnails", "mode": "copy"}
```

切换缩略图缓存目录（**可热切换**，无需重启；旧目录文件会先拷贝过去）。

```
POST /api/settings/clear-thumbnail-cache
```

清空当前缩略图缓存（下次浏览按需重新生成）。

---

## 4. 缩略图与媒体文件

### 4.1 缩略图 HTTP 服务（已实现）

```
GET /api/files/{file_id}/thumbnail
```

返回缩略图图片二进制（`image/jpeg`），带 `Cache-Control: private, max-age=300`；
集中缓存（`data/.thumbnails/`）未命中时按需生成。HEIC/HEIF 源文件在服务端
转码为 JPEG 返回（含 EXIF 方向烘焙，横竖不会颠倒）。不存在时返回 404。
`GET /api/files/{id}/stream` 对 `.heic` 与 `.heif` 同样转码为 JPEG。

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
| **鉴权** | 设置 4 位 Web 密码后 `/api/*` 强制 Bearer 令牌 | token 由 verify 签发，12h 有效；修改密码即失效；标签类请求可用 `?token=` 回退 |
| **搜索** | API 不支持模糊搜索文件名 | 可在 APP 端拉取数据后本地过滤 |
| **分页** | 部分端点有 hard limit（如 per_page <= 200） | 如有更大需求可回调参 |
| **变更通知** | 无 WebSocket 推送 | APP 侧定时轮询 `/api/events/unread` |
| **删除是破坏性操作** | `DELETE /api/files/{id}`、`/api/files/batch-delete`、`DELETE /api/units/{id}` 默认 `mode=trash`：文件进入**系统回收站**，可人工恢复；`mode=record` 才只删库记录 | 客户端务必二次确认；需要"只出库不删盘"时显式传 `mode=record` |
| **查重结果分级** | 未处置结果分 `duplicate` / `related` 两级，`related` 又分 `evidence=file`（有文件重叠）与 `evidence=face`（只有人脸线索，杰卡德恒为 0） | 用 `level` + `evidence` 分别取，用 `/api/dedup/counts` 的 `duplicate/related/face_only` 显示三个角标；不要只按 `level` 取，否则 0% 的同演员线索会淹没真正重叠的几条 |
| **查重结果分组** | 同一左侧单元可能有几十条结果 | `GET /api/dedup/groups` 按来源单元折叠，展开时用 `unit_a_id` 取子项（`pair_count` 之和等于对应 `total`） |
| **升级后的旧结论** | 分级规则变化不会自动重算已有结果；`computed_version` 落后的行（或 `match_count` 超过单元文件数的旧口径残留）都是旧结论 | 看 `counts.stale` 与列表项 `stale`，**提示用户重跑查重**而不是渲染旧数字；`stale` 行不要展示重叠比例 |
| **人脸线索的准确率** | 视频人脸是"定间隔多帧"抽出来的，旧库数据是"只取中间一帧"扫的，`source_ms` 为空 | `GET /api/dedup/face-scan/status` 看 `videos_pending`，用 `POST /api/dedup/face-scan` 重扫（`candidates` 快、`all` 全），完成后结果才有"多帧吻合"证据 |

### 推荐的 API 轮询策略

APP 在后台时，建议每 30-60 秒轮询一个轻量端点来检测状态变化：

```
GET /api/messages/unread-count  →  {"unread_count": N}
```

如果计数变化，再拉取详细数据。避免对 `/api/units` 和 `/api/files` 频繁轮询。
