# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
代码修改后请运行 `python desktop/tests/test_flow.py` 验证确认无误。涉及 web 前端修改 (`web/` 下的文件) 还需额外运行 `PYTHONIOENCODING=utf-8 python desktop/tests/test_web_playwright.py` 验证浏览器测试全通过。请使用中文进行回答，代码、文件名、API 名等保持英文。

## Repo Structure

```
Media/                          # Git root — monorepo
├── desktop/                    # Windows 桌面应用 (PySide6)
│   ├── main.py                 # Entry point
│   ├── app/                    # Python source
│   ├── tests/
│   └── ...
├── web/                        # Web 前端 SPA (Vue 3)
├── docs/                       # 设计文档和计划
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
    dedup_engine.py      #   Multi-strategy dedup (MD5→pHash→dHash→face), Jaccard scoring
    thumbnail_generator.py # Pillow (images) + OpenCV (videos), disk-cached
    watcher.py           #   watchdog-based real-time filesystem monitoring with debounce
  db/
    engine.py            #   DatabaseManager singleton (SQLAlchemy, SQLite WAL mode)
    models.py            #   10 ORM tables: MediaLibraryRoot, ResourceUnit, MediaFile, etc.
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
  utils/
    constants.py         #   All enums (MediaType, UnitStatus, MatchType, etc.)
    media_types.py       #   Extension-to-type mapping, is_media_file() helper
```

## PySide6 Signal 陷阱

- **`selectionChanged.connect()` 会累积** — 每次 `connect` 都新增一个 handler。切换 model 后必须先 `disconnect` 再 `reconnect`，否则同一次点击会触发 N 次
- **`blockSignals(True)` 不只是阻止信号** — 在 `QTreeView` 上会连带阻止选中状态的视觉刷新。防信号循环优先用 guard flag（`_syncing: bool`）而不是 `blockSignals`
- **`setAnimated(True)` + `scrollTo` 不兼容** — 展开动画期间 `scrollTo` 定位到错误位置。程序化展开选中前先 `setAnimated(False)`，完事再恢复
- **`rglob()` 在 Windows 不区分大小写** — `rglob("*.HEIC")` 和 `rglob("*.heic")` 匹配相同文件。多 pattern 扫描需要用 `set()` 去重

## 历史重写注意事项

- `git filter-branch` 可能复活遗留的合并冲突标记（`<<<<<<<` / `>>>>>>>`）。跑完后必须 `grep -rn "<<<<<<"` 检查所有文件，否则 SyntaxError 潜伏在代码中
- 受影响文件可能通过 `ast.parse()` 语法检查但 JS 文件需要额外验证

## Key Design Patterns

- **Frozen dataclasses** for all result/value types (ScanResult, FileHashes, UnitComparisonResult, etc.) — immutability first
- **Registry pattern** for hash algorithms — `HashAlgorithmRegistry` extends `Registry[str, HashAlgorithm]`, pluggable via `registry.register(name, algo)`
- **Session injection** for DB queries — every query function takes `session: Session` as first arg; `DatabaseManager.session()` context manager handles commit/rollback
- **Worker threads** for long operations — scanning, hashing, and dedup run in `QThread` subclasses emitting signals to the main window
- **Debounced file watcher** — `MediaFileEventHandler` uses `threading.Timer` with configurable `watcher_debounce_ms` (default 2000ms)

## Resource Unit Auto-Detection

The scanner (`desktop/app/core/scanner.py`) walks bottom-up: a folder is a "resource unit" if it directly contains media files. If a parent also contains media files directly, both become units. Sub-folders without media files don't create separate units.

## Dedup Pipeline

1. Scan → discover resource units and their files
2. Hash → compute MD5 (exact), pHash (perceptual), dHash (difference) for each file
3. Compare → for each unit pair, run multi-strategy greedy matching: MD5 first, then pHash, then dHash (each file B matched at most once)
4. Score → Jaccard similarity = matches / (|A| + |B| - matches); threshold default 0.80
5. Alert → create `dedup_alert` messages and DedupCompareDialog for user resolution

## Database

SQLite with WAL mode + foreign keys enabled. Single-file at `desktop/data/media_manager.db` (configurable via `config.json`). 10 tables with CHECK constraints enforcing enum values. `expire_on_commit=False` so ORM objects survive session close.

Runtime settings (db path, watcher debounce, dedup threshold, etc.) are in `desktop/config.json`, loaded into a frozen `AppConfig` dataclass at startup.

## Limitations / Work-in-Progress

- Face detection (`desktop/app/core/hash_engine.py:detect_faces`) is stubbed out — returns `[]`, waiting for OpenCV DNN model files
- Search and media-type filtering in the grid are placeholder slots
- No user authentication (API is open on LAN)
- Config is mutable at runtime via settings dialog but frozen everywhere else


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
