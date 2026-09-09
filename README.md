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
│   ├── main.py
│   ├── app/
│   ├── models/          # OpenCV DNN 人脸模型文件
│   ├── tests/
│   └── config.json
├── web/                 # Web 前端 SPA（Vue 3）
├── API.md               # HTTP API 契约
├── README.md
└── .gitignore
```

## 功能

- **文件夹级资源管理** — 自动扫描本地文件夹，将包含媒体文件的目录识别为"资源单元"，支持嵌套场景
- **一键智能查重** — 自动计算哈希+人脸特征+视频抽帧，多策略匹配：人脸识别 → MD5（精确）→ 视频帧 → pHash（感知）→ dHash（差异），基于杰卡德指数判定单元级重复
- **人脸识别** — Caffe SSD 检测 + OpenFace 128 维特征提取，同一人物在不同照片中也能识别
- **缩略图预览** — 图像/视频自动生成缩略图，支持网格浏览、空格键快速预览
- **实时文件监控** — watchdog 监测文件变更，支持去抖合并
- **Web 前端** — Vue 3 SPA，支持手机/平板/桌面端响应式布局，左右滑动预览、视频播放、手势快进快退
- **Web 访问密码** — 桌面端设置 4 位 PIN，手机浏览器输入后进入
- **局域网 API** — 内置 FastAPI 服务（默认 `127.0.0.1:19527` 仅本机；将 `api_host` 改为 `0.0.0.0` 即可局域网/手机访问），含认证端点
- **深色主题** — Slate-Indigo 设计体系，全局 QSS + QPalette 统一渲染

## 桌面应用架构

```
desktop/main.py          # 入口：配置 → 数据库 → API → GUI → 文件监控
desktop/config.py        # frozen dataclass，从 config.json 加载
desktop/app/
  core/                  # 纯业务逻辑（无 UI/DB 依赖）
    scanner.py           #   递归扫描，自动识别资源单元
    hash_engine.py       #   协调 MD5/pHash/dHash 计算
    dedup_engine.py      #   多策略匹配（人脸→MD5→视频帧→pHash→dHash）+ 切片索引
    thumbnail_generator.py  # Pillow(图片) + OpenCV(视频)，磁盘缓存
    watcher.py           #   watchdog 实时监控 + 去抖
  db/
    engine.py            #   DatabaseManager 单例（SQLite WAL 模式）
    models.py            #   12 张 ORM 表
    queries.py           #   所有持久化查询（session 注入）
    migrations.py        #   Schema 版本管理与迁移
  ui/                    # PySide6 桌面界面
    main_window.py       #   QMainWindow 信号中枢
    left_panel/          #   文件夹树 + 右键菜单（合并/拆分/标记）
    right_panel/         #   缩略图网格 + 自定义委托
    dialogs/             #   设置、查重对比、消息中心、预览等
    widgets/             #   状态栏、进度面板、缩略图加载器
    workers/             #   QThread 工作线程（扫描/哈希/查重）
    style.qss            #   深色样式表
  api/                   # FastAPI HTTP 接口
    server.py            #   守护线程运行，内嵌 Web 前端静态文件服务
    routes/              #   /api/files, /api/units, /api/dedup, /api/messages
    schemas.py           #   Pydantic 响应模型
  registry/              # 注册器 + HashAlgorithm 抽象
  services/              # 消息中心、清理服务
  utils/                 # 枚举常量、文件辅助函数
desktop/tests/
  test_flow.py           # 后端 + API 端到端测试（465 项断言，含 PIN 鉴权）
  test_web_playwright.py # Web 前端浏览器测试（47 项）
```

### Web 前端

```
web/
├── index.html           # Vue 3 SPA 入口
├── js-core.js           # API/令牌等基础工具（最先加载）
├── js-pages.js          # 全部页面组件与滚动辅助
├── js-app.js            # 路由 + 根组件 + 挂载（最后加载）
├── style.css            # Slate-Indigo 设计体系
├── manifest.json        # PWA 清单
├── sw.js                # Service Worker（离线缓存）
├── icon-192.png
└── icon-512.png
```

### 数据库

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

### 查重流程

```
点击 [查重] → 自动检测未索引文件 → 有则先计算 MD5/pHash/dHash/人脸/视频抽帧
  → 逐对单元进行多策略贪婪匹配：人脸 → MD5 → 视频帧 → pHash → dHash
  → pHash/dHash 使用鸽巢切片索引加速（零漏配）
  → 视频按抽帧 pHash 集合比对，匹配帧数 ≥ 较短视频帧数 50% 判为同一视频
  → 杰卡德相似度 = |匹配| / (|A| + |B| - |匹配|)
  → ≥ 阈值（默认 0.80）→ 创建 dedup_alert 消息
```

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
# 后端 + API 端到端测试
cd desktop && python tests/test_flow.py

# Web 前端浏览器测试（自带 19528 测试服务与独立数据库；无系统 Chrome 时自动回退 Playwright Chromium）
PYTHONIOENCODING=utf-8 python tests/test_web_playwright.py
```

### 配置

首次运行会在 `desktop/` 下自动生成 `config.json`，可手动编辑：

```json
{
  "db_path": "data/media_manager.db",
  "api_port": 19527,
  "jaccard_threshold": 0.80,
  "thumbnail_max_size": 256,
  "watcher_enabled": true,
  "watcher_debounce_ms": 2000,
  "web_pin": "",
  "face_detection_enabled": true
}
```

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
| GET | `/api/events/unread` | 未读事件简报（角标轮询） |
| GET | `/api/messages` | 消息列表 |
| POST | `/api/messages/{id}/read` | 标记已读 |
| POST | `/api/messages/read-all` | 全部标记已读 |
| DELETE | `/api/messages/{id}` | 忽略/关闭消息 |
| GET | `/api/tags` | 标签列表 |
| POST/DELETE | `/api/tags` `/api/tags/{id}` | 标签 CRUD |
| GET/PUT | `/api/tags/by-file/{id}` | 文件标签查询 / 全量替换 |
| GET | `/api/tags/mapped-files` | 标签-文件批量映射 |

> 鉴权：桌面端设置 4 位 Web 密码后，除 `/api/health`、`/api/auth/*` 外
> 所有 `/api/*` 请求必须携带 `Authorization: Bearer <token>`
> （token 由 `POST /api/auth/verify` 签发，12 小时有效，修改密码后全部失效）。

完整文档在应用运行后访问 `http://localhost:19527/docs`。
API 详细契约见 [API.md](API.md)。

## Web 前端

Web 前端是基于 Vue 3 的 SPA，通过桌面端内置的 FastAPI 服务器提供静态文件服务。

- 响应式布局：手机底部导航、平板/桌面侧边栏
- 资源单元浏览、文件网格、左右滑动预览
- 视频播放：播放/暂停、进度拖动、倍速播放、手势快进快退
- 查重结果查看和处理
- 消息中心
- 访问密码锁屏

## 已知限制

- **人脸检测性能** — CPU 上每张图 ~200-300ms，大量文件时请耐心等待索引完成；可考虑 GPU 加速
- **Web 视频预览** — 依赖浏览器原生解码器，HEVC/Dolby Vision 等编码可能不兼容
- **桌面端视频预览** — QMediaPlayer 依赖系统解码器，部分编码（HEVC/AV1）可能需额外安装
- **非视频类型** — RAW 格式依赖 `rawpy`、HEIC 格式依赖 `pillow-heif`

## 开发

```bash
# 后端 + API 测试
cd desktop && python tests/test_flow.py

# Web 前端浏览器测试
PYTHONIOENCODING=utf-8 python tests/test_web_playwright.py
```

### 设计原则

- **不可变结果类型** — `ScanResult`、`FileHashes`、`UnitComparisonResult` 等使用 frozen dataclass
- **Session 注入** — 所有查询函数以 `session: Session` 为首参数
- **注册器模式** — 哈希算法通过 `HashAlgorithmRegistry` 可插拔注册
- **工作线程** — 耗时操作（扫描/哈希/查重）运行在 `QThread` 中，通过信号与主线程通信

## 许可

本项目基于 MIT 许可证开源。
