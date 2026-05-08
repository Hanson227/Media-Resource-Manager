# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
代码修改后请运行 `python tests/test_flow.py` 验证确认无误。请使用中文进行回答，代码、文件名、API 名等保持英文。

## Testing Policy

所有新增功能必须附带对应的测试用例，合并到 `tests/test_flow.py` 中。

### 覆盖层级

| 层级 | 测试内容 | 示例 |
|------|---------|------|
| **后端逻辑** | core/*, services/*, registry/* | 扫描、哈希、查重、消息中心 |
| **数据库查询** | db/queries.py | 新增查询函数、状态变更 |
| **UI 控件信号** | left_panel/, dialogs/, main_window | 右键菜单 emit、弹窗、按钮 |
| **API 路由** | api/routes/* | 新增 endpoint 的响应 |

### 测试方式

- **后端/DB/API**: 直接调用函数，验证返回值
- **UI 信号**: 创建无窗口 `QApplication([])`，程序化触发 `action.trigger()`，capture 信号验证参数正确性
- **不要求**: 截图对比、视觉回归、全链路 GUI 点击流

### 执行

- 新增功能实现后，运行 `python tests/test_flow.py` 确认全部通过
- 所有测试按 `test_TAG()` 函数组织，在 `main()` 中按顺序调用

## Project Overview

影视资源管理器 (Media Resource Manager) — a Windows desktop app for managing local media collections (movies/TV). Built with PySide6 + SQLAlchemy + FastAPI. v0.1.0, Chinese UI.

## Commands

```bash
# Activate conda environment
conda activate media-manager

# Run the application (Python 3.10)
python main.py

# Run the test suite (single file with 10 test sections)
python tests/test_flow.py

# Install dependencies
pip install -r requirements.txt
```

## Architecture

```
main.py                  # Entry point: config → db init → API server → GUI → watcher
config.py                # Frozen dataclass AppConfig, loaded from config.json
app/
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

## Key Design Patterns

- **Frozen dataclasses** for all result/value types (ScanResult, FileHashes, UnitComparisonResult, etc.) — immutability first
- **Registry pattern** for hash algorithms — `HashAlgorithmRegistry` extends `Registry[str, HashAlgorithm]`, pluggable via `registry.register(name, algo)`
- **Session injection** for DB queries — every query function takes `session: Session` as first arg; `DatabaseManager.session()` context manager handles commit/rollback
- **Worker threads** for long operations — scanning, hashing, and dedup run in `QThread` subclasses emitting signals to the main window
- **Debounced file watcher** — `MediaFileEventHandler` uses `threading.Timer` with configurable `watcher_debounce_ms` (default 2000ms)

## Resource Unit Auto-Detection

The scanner (`app/core/scanner.py`) walks bottom-up: a folder is a "resource unit" if it directly contains media files. If a parent also contains media files directly, both become units. Sub-folders without media files don't create separate units.

## Dedup Pipeline

1. Scan → discover resource units and their files
2. Hash → compute MD5 (exact), pHash (perceptual), dHash (difference) for each file
3. Compare → for each unit pair, run multi-strategy greedy matching: MD5 first, then pHash, then dHash (each file B matched at most once)
4. Score → Jaccard similarity = matches / (|A| + |B| - matches); threshold default 0.80
5. Alert → create `dedup_alert` messages and DedupCompareDialog for user resolution

## Database

SQLite with WAL mode + foreign keys enabled. Single-file at `data/media_manager.db` (configurable via `config.json`). 10 tables with CHECK constraints enforcing enum values. `expire_on_commit=False` so ORM objects survive session close.

Runtime settings (db path, watcher debounce, dedup threshold, etc.) are in `config.json`, loaded into a frozen `AppConfig` dataclass at startup.

## Limitations / Work-in-Progress

- Face detection (`app/core/hash_engine.py:detect_faces`) is stubbed out — returns `[]`, waiting for OpenCV DNN model files
- Search and media-type filtering in the grid are placeholder slots
- No user authentication (API is open on LAN)
- Database migration framework exists but only v1 (no incremental migrations yet)
- Config is mutable at runtime via settings dialog but frozen everywhere else
