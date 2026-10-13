# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
代码修改后请运行 `python desktop/tests/test_flow.py` 验证确认无误。涉及 web 前端修改 (`web/` 下的文件) 还需额外运行 `PYTHONIOENCODING=utf-8 python desktop/tests/test_web_playwright.py` 验证浏览器测试全通过。请使用中文进行回答，代码、文件名、API 名等保持英文。

## 文档地图

本文件只放**每轮都需要**的规则。下面这些**按需读取，不要预读**（先看"什么时候读"）：

| 什么时候读 | 读哪个 |
|---|---|
| 改查重 / 比对 / 人脸 / 阈值 / 分类规则 / 查重 UI | `docs/dedup-pipeline.md` |
| 改 HEIC 解码 / 转换 / 缩略图或哈希的解码路径 | `docs/heic.md` |
| 改 `web/` 下任何文件（含 `index.html` / `sw.js`） | `docs/web-frontend.md` |
| 改扫描 / 单元生命周期 / 缩略图缓存 / 删除语义 / 回收站 / OpenCV 日志 / Qt 信号 | `docs/pitfalls.md` |
| 需要 HTTP 字段级契约 | `API.md` |
| 想了解某次评审的背景（**不是规范**） | `docs/2026-*.md` |

`docs/` 里是详细复盘与实测数据，读完不必背下来 —— 需要时再读。

## Repo Structure

```
Media/                          # Git root — monorepo
├── desktop/                    # Windows 桌面应用 (PySide6)
│   ├── main.py                 # Entry point
│   ├── app/                    # Python source
│   ├── tests/
│   ├── models/                 # 人脸模型（OpenCV Zoo ONNX: YuNet + SFace）
│   └── config.json             # 运行时配置
├── web/                        # Web 前端 SPA (Vue 3, 无构建; js-core→js-gesture→js-pages→js-app 顺序加载)
├── docs/                       # 参考文档（按需读，见上方文档地图）
├── API.md                      # HTTP API 契约
├── README.md
└── CLAUDE.md
```

### 覆盖层级

| 层级 | 测试内容 | 示例 |
|------|---------|------|
| **后端逻辑** | desktop/app/core/*, services/*, registry/* | 扫描、哈希、查重、消息中心 |
| **数据库查询** | desktop/app/db/queries.py | 新增查询函数、状态变更 |
| **UI 控件信号** | desktop/app/ui/left_panel/, dialogs/, main_window | 右键菜单 emit、弹窗、按钮 |
| **API 路由** | desktop/app/api/routes/* | 新增 endpoint 的响应 |

### 测试方式

- **后端/DB/API**: 直接调用函数，验证返回值
- **UI 信号**: 创建无窗口 `QApplication([])`，程序化触发 `action.trigger()`，capture 信号验证参数正确性
- **不要求**: 截图对比、视觉回归、全链路 GUI 点击流

### 执行

- 新增功能实现后，运行 `python desktop/tests/test_flow.py` 确认全部通过
- 涉及前端页面改进的需要附带相应测试案例进入`desktop/tests/test_web_playwright.py`。
- 所有测试按 `test_TAG()` 函数组织，在 `main()` 中按顺序调用

## Commands

```bash
# Activate conda environment
conda activate media-manager

# Run the desktop application
cd desktop && python main.py

# Run the test suite
cd desktop && python tests/test_flow.py

# Run web frontend tests (requires API server running)
python desktop/tests/test_web.py

# Run web frontend Playwright browser tests
PYTHONIOENCODING=utf-8 python desktop/tests/test_web_playwright.py

# Or run from repo root (main.py auto-switches CWD)
python desktop/main.py
python desktop/tests/test_flow.py
python desktop/tests/test_web.py

# Install dependencies
cd desktop && pip install -r requirements.txt
```

## Architecture (desktop/)

```
desktop/main.py          # Entry point: config → db init → API server → GUI → watcher
desktop/config.py        # Frozen dataclass AppConfig, loaded from config.json
desktop/app/
  core/                  # Business logic (no UI/db dependencies)
    scanner.py           #   Recursive folder scanner, auto-detects "resource units"
    hash_engine.py       #   Coordinates MD5/pHash/dHash computation, video frame extraction
    dedup_engine.py      #   Multi-strategy dedup (MD5→video frames→pHash→dHash; face = hint only), Jaccard scoring
    thumbnail_generator.py # Pillow (images) + OpenCV (videos), disk-cached
    watcher.py           #   watchdog-based real-time filesystem monitoring with debounce
  db/
    engine.py            #   DatabaseManager singleton (SQLAlchemy, SQLite WAL mode)
    models.py            #   12 ORM tables: MediaLibraryRoot, ResourceUnit, MediaFile, FileTag, etc.
    queries.py           #   All persistence functions (session-injected, no raw SQL in business code)
    migrations.py        #   Schema bootstrap via Base.metadata.create_all
  ui/
    main_window.py       #   QMainWindow: menu/toolbar/splitter/signals — the GUI signal hub
    left_panel/          #   FolderTreeView + context menu (merge/split/mark units)
    right_panel/         #   ThumbnailGridView + ThumbnailDelegate
    dialogs/             #   Settings, DedupCompare, MessageCenter, etc.
    widgets/             #   StatusBar, ProgressPanel, ThumbLoader
    workers/             #   QThread workers: ScanWorker, HashWorker, DedupWorker
  api/
    server.py            #   FastAPI app on 0.0.0.0:19527, runs in daemon thread
    routes/              #   /api/files, /api/units, /api/dedup, /api/messages
    schemas.py           #   Pydantic models
  registry/
    base.py              #   Generic Registry[K, V] base class
    hash_registry.py     #   HashAlgorithm ABC + MD5Hash/PHash/DHash implementations
  services/
    message_center.py    #   Message CRUD (info/warning/error/dedup_alert)
    dedup_service.py     #   查重编排（GUI 线程与 HTTP API 共用唯一实现）
    index_service.py     #   索引编排（哈希/视频帧/人脸；HashWorker 与 API 共用）
    file_ops.py          #   删除语义（移至回收站 / 仅移出媒体库 / 永久删除），API 侧唯一实现
    merge_service.py     #   查重合并执行（移动/回收/收尾三阶段，先磁盘后库，幂等可重试）
  utils/
    constants.py         #   All enums (MediaType, UnitStatus, MatchType, etc.)
    media_types.py       #   Extension-to-type mapping, is_media_file() helper
```

## Resource Unit Auto-Detection

The scanner (`desktop/app/core/scanner.py`) walks bottom-up: a folder is a "resource unit" if it directly contains media files. If a parent also contains media files directly, both become units. Sub-folders without media files don't create separate units.

**单元的产生与清理必须同源** — 产生判据是"目录直接含媒体文件"，清理就不能只看目录还在不在，
否则会留下 "0 个 · 0 B" 的空壳单元。由 `scan_worker._save_to_database`（唯一扫描入口）双向校正：
`queries.exclude_empty_units`（在删除失效文件记录**之后**）+ `queries.restore_units_produced_by_scan`
（在单元统计写入**之前**）。顺序错了会误删或让回来的内容看不见。→ `docs/pitfalls.md`

## Key Design Patterns

- **Frozen dataclasses** for all result/value types (ScanResult, FileHashes, UnitComparisonResult, etc.) — immutability first
- **Registry pattern** for hash algorithms — `HashAlgorithmRegistry` extends `Registry[str, HashAlgorithm]`, pluggable via `registry.register(name, algo)`
- **Session injection** for DB queries — every query function takes `session: Session` as first arg; `DatabaseManager.session()` context manager handles commit/rollback
- **Worker threads** for long operations — scanning, hashing, and dedup run in `QThread` subclasses emitting signals to the main window
- **Debounced file watcher** — `MediaFileEventHandler` uses `threading.Timer` with configurable `watcher_debounce_ms` (default 2000ms)

## 铁律速查

每条都有详细复盘在 `docs/` 里（见文档地图）。**写下这段代码之前先读对应文档。**

- **产生与清理必须同源** — 单元如此（见上），缓存/结果同理：判据一变，清理逻辑必须跟着变，
  否则旧数据永远不消失。→ `docs/pitfalls.md`
- **缩略图缓存必须校验归属** — 缓存名 `{file_id}_thumb.jpg` 而 file_id 会复用；
  任何"直接返回缓存"都要先过 `ThumbnailGenerator.is_cache_valid()`，清理时
  `.jpg` 与 `.jpg.meta` 必须成对处理。→ `docs/pitfalls.md`
- **删除与排除都必须区分"文件没了"与"磁盘没连"** — 先过 `file_ops.volume_available()`；
  卷不可达 → `409` + 保留记录，绝不按"文件已不存在"删库，也绝不把单元标成已排除
  （忘插盘启动一次 = 整库"消失"）。→ `docs/pitfalls.md`
- **排除状态必须记录起因** — `resource_units.exclude_reason`（user / missing / empty）；
  恢复逻辑只看起因，不能拿 `file_count` 猜，否则掉线造成的排除永远回不来。
  → `docs/pitfalls.md`
- **回收站不可用绝不静默降级成永久删除** — `DELETE_MODES` 是唯一来源，
  `delete` 只能由用户明确选择。→ `docs/pitfalls.md`
- **Windows 上改 `os.environ` 影响不到 cv2** — 必须走
  `image_helpers.silence_ffmpeg_logs()`（CRT `_putenv_s`），否则静默失效。
  → `docs/pitfalls.md`
- **分类规则一变，必须清理陈旧结果** — 例：查重分类规则改了要跑
  `queries.prune_stale_dedup_results`；升级后旧结论靠 `computed_version` **或**
  `match_count > min(|A|,|B|)` 识别为 `stale`，**不展示旧数字**。
  → `docs/dedup-pipeline.md`
- **"相关"必须区分证据来源** — `dedup_results.evidence_kind`（`file` / `face`），
  人脸不计杰卡德；两类混在一起会把真重复淹在 0% 的同演员噪音里。
  → `docs/dedup-pipeline.md`
- **合并必须幂等可重试** — 收尾（空目录删除/标 merged）只在全部成功后执行；
  重入时"被并入目录不存在"不算错误（库里该单元已无文件即放行收尾），
  face 线索不算 counted 匹配（纯移动零删除）。→ `docs/dedup-pipeline.md` §4
- **服务端长任务的运行态不能存组件 data** — 一律放模块级 + 后端 `active-task` 接口，
  否则切走再回来进度/Tab/展开状态全丢。→ `docs/web-frontend.md`
- **HEIC 解码注册只有一个入口** — `image_helpers.ensure_heif_support()`；
  不要在任何别处 `import pillow_heif`。→ `docs/heic.md`

## Database

SQLite with WAL mode + foreign keys enabled. Single-file at `desktop/data/media_manager.db` (configurable via `config.json`). 12 tables with CHECK constraints enforcing enum values. `expire_on_commit=False` so ORM objects survive session close.

Runtime settings (db path, watcher debounce, dedup threshold, etc.) are in `desktop/config.json`, loaded into a frozen `AppConfig` dataclass at startup. `web_pin` is the exception: it is persisted to the gitignored `desktop/data/.web_pin` (never to config.json), with one-shot migration from the legacy plaintext key.

查重的已处置/白名单语义（`is_resolved` 跨重跑保留、`build_dedup_skip_pairs`）
见 `docs/dedup-pipeline.md`。

## Bug Fix 纪律

1. **追踪完整数据流** — 报 bug 时先 tracing 整条链：请求 → 端点 → 存储 → 返回，
   每步加日志验证，不准假设中间环节正确。

2. **一次只修一个 bug** — 同一轮改动不超过 2 个文件。修渲染就别动播放器。

3. **复现测试先 FAIL** — fix 前写最小测试让它 FAIL 证明 bug 存在，
   fix 后确认变 PASS。测试必须直接对应报的 bug，不准写宽松判据绕过。

4. **三次失败换思路** — 同一个 bug 修两次没好，第三次必须写根因分析文档，
   做架构性质疑（比如这次是"API 不生成缩略图"，而不是"前端显示不对"）。

### UI 视觉验证

- test_flow.py 测不到 UI 视觉效果（选中状态、滚动定位、刷新延迟）
- 涉及信号链、选中、动画的改动，必须打开软件人工验证视觉行为

### 文档维护

- 顶部"代码修改后请运行…"约束、`## Commands`、`## Bug Fix 纪律` 是**核心**，不要为了瘦身删掉。
- 新增踩坑/复盘写进 `docs/` 对应文件，本文件只加**一行铁律 + 指针**；
  不要在本文件里展开实测数据与事故过程。
