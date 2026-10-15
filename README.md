# 影视资源管理器 (Media Resource Manager)

> 纯本地运行的 Windows 桌面应用 + Web 前端，用于管理电脑上的本地影视/图片资源文件夹。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/GUI-PySide6-41CD52?logo=qt&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/ORM-SQLAlchemy%202.0-D71F00?logo=sqlalchemy&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)

---

## 仓库结构

```
Media/
├── desktop/             # Windows 桌面应用（Python/PySide6）
│   ├── main.py          # 桌面完整档入口（GUI + 内嵌 API）
│   ├── main_web.py      # 便携版入口（只有 Web/API，不导入 Qt；功能与桌面档一致）
│   ├── build.spec       # 桌面档打包
│   ├── build_web.spec   # 便携版打包（onedir；体积/能力由 MEDIA_BUILD_* 开关决定）
│   ├── app/
│   ├── models/          # OpenCV Zoo ONNX 人脸模型（YuNet 检测 + SFace 特征）
│   ├── tests/
│   └── config.json
├── web/                 # Web 前端 SPA（Vue 3，vendor/ 内含离线 Vue，无需外网）
├── docs/                # 参考文档（查重口径 / HEIC / Web 前端 / 便携版 / 踩坑速查），按需读
├── API.md               # HTTP API 契约
├── README.md
└── CLAUDE.md            # 仓库约定与铁律（模块职责、命令、纪律的唯一来源）
```

> 桌面端分层与各模块职责、Web 前端的加载顺序（js-core → js-gesture → js-pages →
> js-app，不可调换）都在 [CLAUDE.md](CLAUDE.md) 的 `## Repo Structure` /
> `## Architecture (desktop/)`。**那里是单一来源**，本文件不复述——两份文档写同一件事，
> 迟早各自腐烂（README 曾长期停留在 Caffe/OpenFace 时代）。

## 功能

- **文件夹级资源管理** — 自动扫描本地文件夹，将包含媒体文件的目录识别为"资源单元"，支持嵌套场景
- **一键智能查重** — 自动计算哈希+人脸特征+视频抽帧，多策略匹配：MD5（精确）→ 视频帧 → pHash（感知）→ dHash（差异），按杰卡德指数 + 包含度判定单元级重复（完整口径见 [docs/dedup-pipeline.md](docs/dedup-pipeline.md)）
- **人脸识别** — OpenCV Zoo ONNX 模型：YuNet 检测 + SFace 128 维特征。人脸**不参与**重复判定（回答的是"是不是同一个人"，不是"是不是同一个文件"），只作为「同演员」线索展示
- **缩略图预览** — 图像/视频自动生成缩略图，支持网格浏览、空格键快速预览
- **HEIC 检查与转换** — 与 Web 端同一套编排：检查全库（含未入库的）HEIC →
  批量转 JPG → 就地改写媒体库记录（`file_id`/标签保留）→ 重算哈希与人脸；
  源文件默认进回收站（不可用则保留），同名冲突默认跳过
- **实时文件监控** — watchdog 监测文件变更，支持去抖合并
- **Web 前端** — Vue 3 SPA，支持手机/平板/桌面端响应式布局，左右滑动预览、视频播放、手势快进快退
- **Web 访问密码** — 桌面端设置 4 位 PIN，手机浏览器输入后进入
- **局域网 API** — 内置 FastAPI 服务（默认 `127.0.0.1:19527` 仅本机；将 `api_host` 改为 `0.0.0.0` 即可局域网/手机访问），含认证端点
- **深色主题** — Slate-Indigo 设计体系，全局 QSS + QPalette 统一渲染

## 数据库

SQLite + WAL 模式，12 张表：

| 表 | 用途 |
|------|---------|
| `media_library_roots` | 用户添加的媒体库根目录 |
| `resource_units` | 自动/手动识别的资源单元（文件夹级） |
| `media_files` | 每个发现的媒体文件及哈希值 |
| `face_vectors` | 128 维人脸特征向量 |
| `video_frames` | 视频关键帧感知哈希 |
| `dedup_results` | 单元级查重比对结果（已处置状态持久保留，重跑不重置） |
| `dedup_file_matches` | 文件级匹配对 |
| `messages` | 系统消息/通知收件箱 |
| `whitelist` | 查重白名单（"加入白名单"后重跑自动跳过该对/单元） |
| `scan_sessions` | 扫描审计日志 |
| `file_tags` | 文件分类标签 |
| `file_tag_mappings` | 文件-标签多对多映射 |

## 查重流程

```
点击 [查重] → 自动检测未索引文件 → 有则先计算 MD5/pHash/dHash/人脸/视频抽帧
  → 逐对单元多策略贪婪匹配：MD5 → 视频帧 → pHash → dHash（人脸只作线索，不参与判定）
  → pHash/dHash 与视频帧用鸽巢切片索引加速（零漏配）
  → 杰卡德 = |匹配| / (|A| + |B| - |匹配|)；另算包含度 = |匹配| / min(|A|,|B|)
  → 分级：疑似重复（杰卡德 ≥ 0.80，或包含度 ≥ 0.85 的子集重复）
        / 疑似相关（有文件重叠，仅提醒不建议删除）/ 同演员（只有人脸线索）
  → 创建 dedup_alert 消息；「相关」合并成一条汇总提醒而不是逐条刷屏
```

完整口径（阈值常量、视频同一性判据、人脸匹配语义、旧结论的处理）见
[docs/dedup-pipeline.md](docs/dedup-pipeline.md)。

## 快速开始

### 环境要求

- Windows 10/11（仅测试此平台）
- Python 3.10+
- FFmpeg（可选，用于视频帧提取）

### 安装

```bash
git clone https://gitee.com/hanson227/Media-Resource-Manager.git
cd Media-Resource-Manager

# 创建虚拟环境（推荐）
python -m venv .venv
.venv\Scripts\activate

# 安装依赖
cd desktop
pip install -r requirements.txt

# 运行桌面端
python main.py

# Web 前端直接通过浏览器访问 http://localhost:19527
```

### 测试

```bash
# 后端 + API 端到端测试（桌面完整档）
cd desktop && python tests/test_flow.py

# 便携版（能力降级 / 数据根 / 盘符改库 / 数据迁移 / 无 Qt 断言）
cd desktop && python tests/test_lite.py

# Web 前端浏览器测试（自带 19528 测试服务与独立数据库；无系统 Chrome 时自动回退 Playwright Chromium）
PYTHONIOENCODING=utf-8 python tests/test_web_playwright.py
```

### 便携版（无桌面 GUI）

系统盘紧张、主要用手机/平板浏览器访问时，用便携版：**不含桌面 GUI（不打包 PySide6）**，
功能与桌面档**完全一致**，并可以把数据放在移动硬盘上。

```bash
# 直接运行（源码）
cd desktop && python main_web.py
cd desktop && python main_web.py --data-root E:\MediaData

# 打包成便携包（onedir）。体积按需要开关：
pip install -r requirements-web.txt pyinstaller
cd desktop && pyinstaller build_web.spec               # dist/影视资源管理器-便携版/
$env:MEDIA_BUILD_NO_CV2="1";    pyinstaller build_web.spec   # 再省 111MB（视频/人脸能力关闭）
$env:MEDIA_BUILD_WITH_FACE="1"; pyinstaller build_web.spec   # +38MB（人脸线索可用）
```

**一套代码，功能同步，体积靠编译开关**：某个功能能不能用，由
[`app/utils/capabilities.py`](desktop/app/utils/capabilities.py) 在运行时探测
（包里有没有 OpenCV / pillow-heif / rawpy / 人脸模型 / PySide6），并如实报进
`/api/features`；查重路由在任何打包组合下都存在（文件级查重只需要 Pillow）。
详见 [docs/portable-lite.md](docs/portable-lite.md)。

- 数据（数据库 / 缩略图 / Web 密码）可全部放在 `--data-root` 指定的目录
  （建议移动硬盘），程序目录不存数据；换机器后**即使盘符变了也会自动找到**它；
- 盘符变化导致媒体库路径失效时，Web「媒体库」页会列出候选位置，确认后一键改库
  （改前自动备份，不动磁盘文件）；
- 便携版没有桌面界面，添加媒体库与扫描都在 Web「媒体库」页完成；
- 手机/平板当终端：默认监听 `0.0.0.0`，启动时控制台会打印
  `手机/平板访问: http://<局域网IP>:19527`，手机连同一 WiFi 直接输入即可；
- 打包产物内置本地 Vue/vue-router，**断网也能打开页面**。

完整说明（数据根解析规则、改库边界、手机访问与防火墙、ARM 平板注意事项、排障表）见
[docs/portable-lite.md](docs/portable-lite.md)。

### 配置

首次运行会在 `desktop/` 下自动生成 `config.json`，可手动编辑：

```json
{
  "db_path": "data/media_manager.db",
  "data_root": null,
  "profile": "full",
  "api_port": 19527,
  "jaccard_threshold": 0.80,
  "thumbnail_max_size": 256,
  "watcher_enabled": true,
  "watcher_debounce_ms": 2000,
  "face_detection_enabled": true
}
```

> `data_root` 为 `null` 表示不启用数据根（路径相对程序目录，与旧版本一致）。
> 设成移动硬盘上的目录（如 `"E:\\MediaData"`）后，数据库/缩略图/Web 密码都跟着它走，
> 换盘符也能自动定位 —— 见 [docs/portable-lite.md](docs/portable-lite.md)。
> `profile` 只是"这是哪个包"的展示标签，**不影响功能**：功能可用性由
> `app/utils/capabilities.py` 的能力探测决定（见 [docs/portable-lite.md](docs/portable-lite.md)）。

> Web 访问密码（`web_pin`）**不放在这个文件里**：它持久化在 gitignore 的
> `desktop/data/.web_pin`。旧版本 config.json 里若有明文 `web_pin`，启动时会一次性迁移过去。

也可以在 GUI "工具 → 设置"中修改。

## API 端点

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| GET | `/api/health` | 健康检查（免鉴权） |
| GET | `/api/auth/status` | 查询是否启用访问密码（免鉴权） |
| POST | `/api/auth/verify` | 验证 Web 访问密码，返回会话令牌 |
| POST | `/api/auth/change-pin` | 修改访问密码（使旧令牌全部失效） |
| GET | `/api/units` | 列出资源单元 |
| GET | `/api/units/{id}` | 单元详情 |
| GET | `/api/units/{id}/files` | 单元内全部文件列表 |
| POST | `/api/units/{id}/star` / `unstar` | 收藏 / 取消收藏 |
| GET | `/api/files` | 列出媒体文件（SQL 分页，unit_id 筛选） |
| GET | `/api/files/{id}` | 文件详情 |
| GET | `/api/files/{id}/thumbnail` | 缩略图二进制（缓存缺失时按需生成） |
| GET | `/api/files/{id}/stream` | 流式传输原始文件（支持 Range，HEIC 转 JPEG） |
| DELETE | `/api/files/{id}` | 删除文件记录 |
| GET | `/api/dedup/results` | 查重结果列表（分页） |
| GET | `/api/dedup/results/{id}` | 查重详情（含文件匹配列表） |
| POST | `/api/dedup/results/{id}/resolve` | 处理查重结果（keep_a/keep_b/whitelist/ignore 等） |
| POST | `/api/dedup/run` | 触发查重（异步，202 + task_id；与桌面端互斥，跳过已处置/白名单对） |
| GET | `/api/dedup/run/{task_id}` | 轮询查重任务进度/结果 |
| GET | `/api/heic/scan` | 检查媒体库里的 HEIC（库内 + 磁盘，含"哈希为空串"的坏行计数） |
| POST | `/api/heic/convert` | 触发 HEIC → JPG 转换（异步 202 + task_id；路径限定在媒体库根内） |
| GET | `/api/heic/run/{task_id}` | 轮询 HEIC 任务进度/结果（逐项状态 + 源文件处置说明） |
| GET | `/api/heic/active-task` | 当前在跑的 HEIC 任务（切页/刷新后重新接上进度） |
| POST | `/api/heic/cancel` | 请求取消 HEIC 转换（协作式，当前文件完成后停止） |
| GET | `/api/events/unread` | 未读事件简报（角标轮询） |
| GET | `/api/messages` | 消息列表 |
| POST | `/api/messages/{id}/read` | 标记已读 |
| POST | `/api/messages/read-all` | 全部标记已读 |
| DELETE | `/api/messages/{id}` | 忽略/关闭消息 |
| GET | `/api/tags` | 标签列表 |
| POST/DELETE | `/api/tags` `/api/tags/{id}` | 标签 CRUD |
| GET/PUT | `/api/tags/by-file/{id}` | 文件标签查询 / 全量替换 |
| GET | `/api/tags/mapped-files` | 标签-文件批量映射 |
| GET | `/api/features` | 运行档位与能力开关（轻量档 `dedup=false`，前端据此隐藏查重入口） |
| GET | `/api/library/volumes` | 就绪卷（盘符/卷标/可用空间） |
| GET | `/api/library/browse` | 服务端目录浏览（便携版"选媒体库目录"用） |
| GET/POST | `/api/library/roots` | 媒体库根列表（含不可达原因与候选位置）/ 添加 |
| DELETE | `/api/library/roots/{id}` | 移除媒体库（不动磁盘文件） |
| POST | `/api/library/remap` | 盘符变化后路径改库（dry-run + 冲突检测 + 备份 + 幂等） |
| POST | `/api/library/scan` | 触发扫描（异步 202 + task_id） |
| GET | `/api/library/scan/{task_id}` / `active-task` | 轮询扫描进度 / 接上跨页任务 |
| GET/POST | `/api/settings` `/api/settings/data-root` | 数据位置 / 迁移数据根（copy\|move） |
| POST | `/api/settings/thumbnail-cache` | 切换缩略图缓存目录（热切换，无需重启） |

> 鉴权：桌面端设置 4 位 Web 密码后，除 `/api/health`、`/api/auth/*` 外
> 所有 `/api/*` 请求必须携带 `Authorization: Bearer <token>`
> （token 由 `POST /api/auth/verify` 签发，12 小时有效，修改密码后全部失效）。

完整文档在应用运行后访问 `http://localhost:19527/docs`。
API 详细契约见 [API.md](API.md)。

## Web 前端

Web 前端是基于 Vue 3 的 SPA，通过桌面端内置的 FastAPI 服务器提供静态文件服务。

- 响应式布局：手机底部导航、平板/桌面侧边栏
- 资源单元浏览、文件网格、左右滑动预览
- **媒体库管理**：卷列表 + 服务端目录浏览 + 添加媒体库 + 扫描（便携版的唯一入口）
- **数据位置**：查看数据目录占用、迁移数据根 / 缩略图缓存目录（便携版）
- **路径修复**：盘符变化后列候选位置并一键改库
- 视频播放：播放/暂停、进度拖动、倍速播放、手势快进快退
- 查重结果查看和处理（轻量档下自动隐藏）
- HEIC 检查与转换（**设置里的工具入口**，不占导航栏目：检查全库 HEIC → 勾选 → 批量转 JPG，进度跨页存活）
- 消息中心
- 访问密码锁屏
- 离线可用：本地 `web/vendor/` 内置 Vue/vue-router 与图标字体，断网也能打开

## 已知限制

- **人脸检测性能** — CPU 上每张图 ~200-300ms，大量文件时请耐心等待索引完成；可考虑 GPU 加速
- **Web 视频预览** — 依赖浏览器原生解码器，HEVC/Dolby Vision 等编码可能不兼容
- **桌面端视频预览** — QMediaPlayer 依赖系统解码器，部分编码（HEVC/AV1）可能需额外安装
- **非视频类型** — RAW 格式依赖 `rawpy`；HEIC/HEIF 依赖 `pillow-heif`（pHash/dHash/人脸/缩略图/浏览器预览/转换全链路均已支持，**不需要**先把 HEIC 转成 JPG 才能查重）
- **AVIF** — 未纳入媒体扩展名清单，不参与扫描与转换

## 开发

```bash
# 后端 + API 测试
cd desktop && python tests/test_flow.py

# Web 前端浏览器测试
PYTHONIOENCODING=utf-8 python tests/test_web_playwright.py
```

### 设计原则与开发纪律

见 [CLAUDE.md](CLAUDE.md) 的 `## Key Design Patterns`、`## 铁律速查`、`## Bug Fix 纪律`，
以及 `docs/` 下的参考文档（查重口径、HEIC、Web 前端、踩坑速查）。不在本文件重复。

**改完代码必须跑测试**，命令见上文「测试」。

## 许可

本项目基于 MIT 许可证开源。
