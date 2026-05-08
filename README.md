# 影视资源管理器 (Media Resource Manager)

> 纯本地运行的 Windows 桌面应用，用于管理电脑上的本地影视/图片资源文件夹。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![PySide6](https://img.shields.io/badge/GUI-PySide6-41CD52?logo=qt&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/ORM-SQLAlchemy%202.0-D71F00?logo=sqlalchemy&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)

---

## 功能

- **文件夹级资源管理** — 自动扫描本地文件夹，将包含媒体文件的目录识别为"资源单元"，支持嵌套场景
- **多策略文件查重** — 逐层匹配：MD5（精确）→ pHash（感知）→ dHash（差异）→ 人脸识别，基于杰卡德指数判定单元级重复
- **缩略图预览** — 图像/视频自动生成缩略图，支持网格浏览、空格键快速预览
- **实时文件监控** — watchdog 监测文件变更，支持去抖合并
- **局域网 API** — 内置 FastAPI 服务（默认 `0.0.0.0:19527`），可通过 HTTP 查询媒体库数据
- **深色主题** — Catppuccin Mocha 配色，全局 QSS + QPalette 统一渲染

## 架构

```
main.py                  # 入口：配置 → 数据库 → API → GUI → 文件监控
config.py                # frozen dataclass，从 config.json 加载
app/
  core/                  # 纯业务逻辑（无 UI/DB 依赖）
    scanner.py           #   递归扫描，自动识别资源单元
    hash_engine.py       #   协调 MD5/pHash/dHash 计算
    dedup_engine.py      #   多策略匹配 + 杰卡德评分
    thumbnail_generator.py  # Pillow(图片) + OpenCV(视频)，磁盘缓存
    watcher.py           #   watchdog 实时监控 + 去抖
  db/
    engine.py            #   DatabaseManager 单例（SQLite WAL 模式）
    models.py            #   10 张 ORM 表
    queries.py           #   所有持久化查询（session 注入）
    migrations.py        #   Schema 版本管理与迁移
  ui/                    # PySide6 桌面界面
    main_window.py       #   QMainWindow 信号中枢
    left_panel/          #   文件夹树 + 右键菜单（合并/拆分/标记）
    right_panel/         #   缩略图网格 + 自定义委托
    dialogs/             #   设置、查重对比、消息中心、预览等
    widgets/             #   状态栏、进度面板、缩略图加载器
    workers/             #   QThread 工作线程（扫描/哈希/查重）
    style.qss            #   Catppuccin Mocha 深色样式表
  api/                   # FastAPI HTTP 接口
    server.py            #   守护线程运行
    routes/              #   /api/files, /api/units, /api/dedup, /api/messages
    schemas.py           #   Pydantic 响应模型
  registry/              # 注册器 + HashAlgorithm 抽象
  services/              # 消息中心、清理服务、SMB 共享
  utils/                 # 枚举常量、文件辅助函数
tests/
  test_flow.py           # 端到端测试（93 项，覆盖导入/DB/扫描/哈希/查重/UI/API）
```

### 数据库

SQLite + WAL 模式，10 张表：

| 表 | 用途 |
|------|---------|
| `media_library_roots` | 用户添加的媒体库根目录 |
| `resource_units` | 自动/手动识别的资源单元（文件夹级） |
| `media_files` | 每个发现的媒体文件及哈希值 |
| `face_vectors` | 128 维人脸特征向量 |
| `video_frames` | 视频关键帧感知哈希 |
| `dedup_results` | 单元级查重比对结果 |
| `dedup_file_matches` | 文件级匹配对 |
| `messages` | 系统消息/通知收件箱 |
| `whitelist` | 查重白名单 |
| `scan_sessions` | 扫描审计日志 |

### 查重流程

```
扫描 → 发现资源单元 → 计算 MD5/pHash/dHash
  → 逐对单元进行多策略贪婪匹配（每个文件 B 最多匹配一次）
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
git clone https://github.com/yourname/media-manager.git
cd media-manager

# 创建虚拟环境（推荐）
python -m venv .venv
.venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

### 配置

首次运行会在项目根目录自动生成 `config.json`，可手动编辑：

```json
{
  "db_path": "data/media_manager.db",
  "api_port": 19527,
  "jaccard_threshold": 0.80,
  "thumbnail_max_size": 256,
  "watcher_enabled": true,
  "watcher_debounce_ms": 2000
}
```

也可以在 GUI "工具 → 设置"中修改。

### 测试

```bash
python tests/test_flow.py
```

## API 端点

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/units` | 列出资源单元 |
| GET | `/api/files` | 列出媒体文件（支持 unit_id 筛选和分页） |
| GET | `/api/files/{id}` | 文件详情 |
| GET | `/api/files/{id}/thumbnail` | 缩略图路径 |
| DELETE | `/api/files/{id}` | 删除文件记录 |
| GET | `/api/dedup/results` | 查重结果列表 |
| GET | `/api/dedup/results/{id}` | 查重详情（含文件匹配列表） |
| POST | `/api/dedup/results/{id}/resolve` | 处理查重结果 |
| GET | `/api/messages` | 消息列表 |
| POST | `/api/messages/{id}/read` | 标记已读 |
| POST | `/api/messages/read-all` | 全部标记已读 |

完整文档在应用运行后访问 `http://localhost:19527/docs`。

## 已知限制

- **人脸检测** — 模型文件（OpenCV DNN）需要用户自行下载，目前返回空列表
- **搜索/筛选** — 网格视图中的搜索和媒体类型筛选为基础实现，接口已预留
- **无用户认证** — API 在局域网内完全开放
- **非视频类型** — RAW 格式依赖 `rawpy`、HEIC 格式依赖 `pillow-heif`，需确认相关库已安装

## 开发

```bash
# 代码风格
ruff check .

# 类型检查
mypy app/

# 测试
python tests/test_flow.py
```

### 设计原则

- **不可变结果类型** — `ScanResult`、`FileHashes`、`UnitComparisonResult` 等使用 frozen dataclass
- **Session 注入** — 所有查询函数以 `session: Session` 为首参数
- **注册器模式** — 哈希算法通过 `HashAlgorithmRegistry` 可插拔注册
- **工作线程** — 耗时操作（扫描/哈希/查重）运行在 `QThread` 中，通过信号与主线程通信

## 许可

本项目基于 MIT 许可证开源。
