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
│   ├── models/                 # 人脸模型（OpenCV Zoo ONNX: YuNet + SFace）
│   └── config.json             # 运行时配置
├── web/                        # Web 前端 SPA (Vue 3, 无构建; js-core→js-pages→js-app 顺序加载)
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
  utils/
    constants.py         #   All enums (MediaType, UnitStatus, MatchType, etc.)
    media_types.py       #   Extension-to-type mapping, is_media_file() helper
```

## Web 前端陷阱（Vue 3 全局构建，无打包）

- **模板不能调用 `_` 开头的方法** — Vue 3 渲染代理不暴露下划线 key，模板里的
  `_deleteFile(f)` 会在点击时抛 `ReferenceError: _deleteFile is not defined`
  （方法体完全不执行）。历史上 `_toggleStar`、`_deleteFile` 都踩过。
  `web/js-*.js` 里的模板调用必须用无前缀名字；`test_dedup_web_js_no_underscore_method_calls`
  做静态拦截
- **模板字符串里不能再用反引号** — 组件 `template` 本身就是 JS 模板字面量，
  内部只能用字符串拼接（如 `'确定删除「' + f.filename + '」？'`）
- **`<img>`/`<video>` 的请求带不了 Authorization 头** — 缩略图/视频流走
  `mediaUrl()` 拼 `?token=` 回退（服务端两种方式等价）
- **JS/CSS 走网络优先，index.html 走缓存优先** — 改 `index.html`（或应用壳）必须
  bump `sw.js` 的 `CACHE` 版本号，否则 PWA 永远拿到旧壳

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
- **缩略图缓存必须校验归属** — 缓存名是 `{file_id}_thumb.jpg`，而 file_id 会被复用
  （「重置数据库」后从 1 重新分配；删除最大 id 的记录后下一条也拿到同一 id）。
  任何"直接返回缓存文件"的地方都必须先过 `ThumbnailGenerator.is_cache_valid()`
  （读 sidecar meta：先比归属路径，可比时再比 mtime/size；源不可达时只验归属，
  保证离线媒体库仍能显示缓存）。否则新文件会显示上一个文件/上一个库的图。
  清理同样要成对处理：`CleanupService` 的失效/孤儿清理/重置清空都同时处理
  `.jpg` 与 `.jpg.meta`（只删图会留下永远回收不掉的碎 meta）

- **查重"相关"必须区分证据来源** — `dedup_results.evidence_kind`
  （`file`=有文件重叠 / `face`=只有人脸线索）。人脸不计入杰卡德，所以"只有人脸线索"
  的行杰卡德恒为 0；两类混在一起时实测 380 条 related 里 373 条是 0% 的同演员噪音，
  真正值得看的 7 条被淹没。`/api/dedup/results?evidence=file|face` 分开取，
  `/counts` 返回 `{duplicate, related, face_only, total}` 供三个 Tab 显示角标。

- **分类规则变化后必须清理陈旧结果** — `queries.prune_stale_dedup_results`：
  只删"两单元都在本轮比对范围内 + 未处置 + 本轮未再命中"的行。upsert 只更新仍命中的
  单元对，不清理的话旧的 380 条 related 会永远留在列表里（规则改了、结论没变）。

- **升级不重算，必须能认出"旧结论"** — `dedup_results.computed_version`
  （`DEDUP_RESULT_VERSION`）：迁移只能搬分类，重算需要哈希/人脸向量与引擎，
  只能在应用里跑。所以旧行标 `0`，列表/详情/分组都用 `stale` 标记并**不要展示旧数字**。
  双保险判据：`computed_version` 落后 **或** `match_count > min(|A|,|B|)`
  （新口径下不可能）。实测踩到：旧行 `match_count=46` 配 33 个文件的单元，
  界面渲染成"46/33 个文件重叠"，用户以为界面递归了。`/counts` 的 `stale`
  与列表项的 `stale` 必须同口径，否则角标与卡片对不上。

- **删除必须区分"文件没了"与"磁盘没连"** — `file_ops.volume_available()`：
  媒体库常在可移动盘/网络盘上（本库是 `G:\Guofu`）。盘没插时 `Path.exists()` 为
  False，若按"文件已不存在"处理就会**删掉库记录而磁盘文件还在** ——
  插回盘一重扫文件又全回来，用户看到的就是"删不掉"（实测报障）。
  现在：卷不可达 → `409` + 原因含"未连接" + 记录保留；卷可达但文件真没了 →
  仍算成功（终态已满足）。

- **回收站不可用绝不静默降级成永久删除** — 网络盘 / exFAT 等没有 `$RECYCLE.BIN`
  的卷上 `send2trash` 必然失败，`mode=trash` 恒回 `409`。
  `DELETE_MODES = (trash, record, delete)` 是唯一来源（API 的 `pattern` 由它拼出），
  `delete`（`shutil.rmtree` / `unlink`，不可恢复）只能由**用户明确选择**；
  客户端把 409 变成"永久删除 / 仅从媒体库移除"的弹层（`showDeleteFallback`），
  失败时保留选中状态以便重试。桌面 GUI 仍是"提示手动处理"，未接这个出口。

- **服务端长任务的"运行态"不能存在组件 data 里** — 查重/人脸重扫跑在服务端后台
  线程，页面组件却随路由卸载重建：运行态/进度/当前 Tab/已展开分组放在组件里，
  切走再回来就全丢（实测报障：进度条消失但风扇还在转；看完详情返回跳回默认 Tab）。
  Web 端统一放**模块级**：`_dedupRun`（任务镜像）+ `_dedupLevel` / `_dedupExpanded`
  / `_dedupScrollTop`（`js-pages.js`），mount 时恢复、`beforeUnmount` 只停本地轮询；
  再配 `GET /api/dedup/active-task`，整页刷新/换设备也能重新接上进度。
  改这些逻辑时注意：**同一个 URL 的 `page.goto` 是同文档导航**，模块级状态不会重置
  （测试里要显式点 Tab，或 `reload()` 才会回到默认档）。

- **Windows 上 `os.environ` 改不到 cv2 的环境** — `OPENCV_FFMPEG_LOGLEVEL` /
  `OPENCV_LOG_LEVEL` 这类变量是 OpenCV 的 `opencv_videoio_ffmpeg*.dll` 用 CRT 的
  `getenv` 读的，而 `os.environ[k]=v` / `os.putenv` / `SetEnvironmentVariableW` 都只改
  进程环境块，改不到 UCRT 缓存的那份 —— 抑制会**静默失效**（实测：三种写法解码损坏
  视频仍打 9~396 行 `[h264 @ ...]`，只有 `ctypes.CDLL("msvcrt")._putenv_s` 能静音）。
  统一走 `app/utils/image_helpers.silence_ffmpeg_logs()`（模块导入即调用，早于任何
  延迟 `import cv2`）；QtMultimedia 预览另有自己的一套 ffmpeg（`avutil-*.dll`），
  用 `av_log_set_level(8)` 关（`preview_dialog._silence_qt_ffmpeg_logs`）。
  噪音只是表象：这些告警说明**视频文件本身损坏/截断**（解码到一半就报错），
  静音不等于文件没问题。

## Resource Unit Auto-Detection

The scanner (`desktop/app/core/scanner.py`) walks bottom-up: a folder is a "resource unit" if it directly contains media files. If a parent also contains media files directly, both become units. Sub-folders without media files don't create separate units.

## Dedup Pipeline (一键查重)

1. 点击「查重」→ 自动检测未索引文件 → 有则先计算哈希+人脸
   - 索引按 `INDEX_BATCH_SIZE`(1000) **分批循环取到取空**。`get_unindexed_files`
     有 limit，只取一批会让未索引文件多于该值时"提前收工"，而查重紧接着就会拿这份
     残缺数据出结论（实测真实库 1362 个待索引只算了 1000，剩 362 个完全不可见且无提示）
2. Hash → compute MD5 (exact), pHash (perceptual), dHash (difference), face vectors (128-d),
   video key-frame pHashes (every `video_frame_interval_sec`)
   - 人脸走 OpenCV 自带 ONNX 接口：**YuNet** 检测 + **SFace** 128 维特征
     (`cv2.FaceDetectorYN` / `cv2.FaceRecognizerSF`，需 OpenCV ≥ 4.5.4，4.x/5.x 通用）。
     比对度量为**余弦相似度**，阈值 `face_similarity_threshold`（默认 **0.45**）。
     官方 SFace 值是 0.363，但它面向"同一人的两张正脸照片"的身份验证，而这里是在
     两个单元的全部文件间两两比对（数万次），0.363 会把大量"长得像"的人凑成对。
     实测本库：0.363→380 对 / 0.45→57 对 / 0.50→19 对。
   - **视频人脸按定间隔多帧检测**（`plan_video_face_timestamps`：最小间距
     `video_frame_interval_sec`、上限 `face_video_max_frames`(10)、均匀铺满全片，
     长视频自动放大间距）。旧实现只取"中间那一帧"：正脸不在中点就整片漏检
     （实测 1142 个视频只有 198 个检出人脸）、且同一视频重扫结果不可复现。
     每行向量带 `source_ms`，用于"多帧吻合"可信度。检测长边仍用 1280
     （`FACE_DETECT_MAX_SIDE_VIDEO`）：640 会掉约 22% 召回，而多帧抽帧的目的正是提召回
3. Compare → for each unit pair, run multi-strategy greedy matching: MD5 → video frames → pHash → dHash
   - 精确身份优先：MD5 排在模糊策略之前。matched 集合跨策略共享，顺序错了会让
     字节级一致的铁证被记成模糊匹配，甚至抢占到错误的对手方
   - 人脸不计入杰卡德：人脸向量回答的是"是不是同一个人"，不是"是不是同一个文件"，
     只在 `UnitComparisonResult.face_hints` 里作为线索返回并显示在对比弹窗
     - 人脸匹配用 **numpy 余弦矩阵**（`rows_a @ rows_b.T`）做文件对级比对：多帧后每个
       文件可能有 10 条向量，原来的 Python 双层循环会变成 10×10 次纯 Python 点积。
       匹配语义（按 A 顺序、全局取最大、文件级一对一、≥阈值）由
       `test_face_matching_semantics_and_support` 用独立参考实现差分锁定
     - 每条人脸线索带 `frame_support`（A 侧多少个采样帧都能找到 B 的同一张脸）：
       ≥2 帧吻合远比单帧偶然相似可信，界面显示"多帧吻合 N 帧"
   - pHash/dHash 使用鸽巢切片索引（切 threshold+1 段，任一段完全相同才进候选），零漏配
   - 视频帧级匹配：抽帧 pHash 一对一贪心配对 + 帧级鸽巢切片索引（与暴力比对等价，
     由 `test_dedup_video_frame_index_equivalence` 差分验证）。判为同一视频需同时满足：
     匹配帧数 ≥ `VIDEO_FRAME_MIN_MATCHES`(3)、匹配比例 ≥ `VIDEO_FRAME_MATCH_RATIO`(0.75，
     分母为较短视频帧数)、匹配对时序同序（LIS ≥ 比例的帧数）；抽帧数 <
     `VIDEO_FRAME_MIN_FRAMES`(3) 的视频不参与判定（精确重复仍由 MD5 兜住）
   - 匹配严格一对一（A/B 两侧各最多出现一次），保证杰卡德指数 ≤ 1.0
   - 文件数比例 ≥ 20x 的单元对自动跳过
4. Score → Jaccard = matches / (|A| + |B| - matches)；**包含度** overlap = matches / min(|A|, |B|)
   - 只认杰卡德会漏掉"一边是另一边的子集"这种最常见的真重复：实测 29 个文件 MD5
     完全一致、另一侧多 12 个文件时杰卡德只有 0.644（够不到 0.80），包含度却是 0.879
5. Classify → 两级命中（`MatchLevel`）+ 证据来源（`EvidenceKind`）：
   - **duplicate**：杰卡德 ≥ `jaccard_threshold`(0.80)，或
     **子集重复**（包含度 ≥ `CONTAINMENT_MATCH_THRESHOLD`(0.85) 且匹配数 ≥ 5
     且杰卡德 ≥ 0.10 且至少一条匹配是 md5/video 强证据）→ 建议保留一份，
     逐条发 `dedup_alert` + 弹对比框
     - 杰卡德下限挡住"9 个文件的目录被 200 个文件的目录完全包含"这种伪重复；
       强证据门槛挡住纯 pHash 近似图（截图/相似构图）触发"建议保留一份"
   - **related（有文件重叠）**：文件匹配数 ≥ max(`related_min_matches`(2),
     ceil(`RELATED_MIN_OVERLAP`(0.02) × min(|A|,|B|))) → 部分重叠，**仅提醒不建议删除**。
     比例地板是必需的：大单元里凑巧相近的 2 个文件（0.2%）不该算"相关"
   - **related + evidence_kind='face'（同演员）**：0 个文件匹配、人脸线索 ≥ 2。
     杰卡德恒为 0，因此单独归入「同演员」，不混进"疑似相关"
   - 实测本库 373 对同演员 + 7 对重叠，故不逐条发消息，而是合并成一条汇总提醒
     （`MessageCenter.create_related_digest(pairs, face_pairs)`，两段分开陈述）；
     逐条明细仍落库供查询。落库后调用 `prune_stale_dedup_results` 清理陈旧行
6. Alert → `dedup_alert` 消息 + DedupCompareDialog。related 的弹窗不提供"保留 A/B"，
   只给"加入白名单/暂时忽略"（避免暗示要删掉另一侧）
7. Refine（可选，`refine_faces=true` / 桌面端默认开启）→ 对"上一轮出现人脸线索的视频"
   做主间隔多帧重扫（`run_face_rescan_pipeline(scope='candidates')`，实测 165 个视频
   约 2~3 分钟），然后**重新比对一次**。`media_files.face_scan_version` 记录抽帧策略版本：
   - 只有写入成功后版本号才更新为 `FACE_SCAN_VERSION`(1)，中途取消/失败的文件保持 0，
     下次补扫会重新处理（幂等）
   - 候选重扫过之后再次查重会瞬间跳过（不是每次都花这几分钟）
   - 全库补扫走显式动作（Web「补扫全部视频人脸」/ 桌面「工具 → 重扫视频人脸」），
     与查询共用互斥门

## Database

SQLite with WAL mode + foreign keys enabled. Single-file at `desktop/data/media_manager.db` (configurable via `config.json`). 12 tables with CHECK constraints enforcing enum values. `expire_on_commit=False` so ORM objects survive session close.

Dedup semantics: a pair marked keep_a/keep_b/whitelist/ignore stays resolved across re-runs (`upsert_dedup_result` does not reset `is_resolved`); re-running dedup skips resolved/whitelisted unit pairs (`queries.build_dedup_skip_pairs`), so user decisions are one-shot. "加入白名单" additionally writes unit-level rows into the `whitelist` table (same transaction).

Runtime settings (db path, watcher debounce, dedup threshold, etc.) are in `desktop/config.json`, loaded into a frozen `AppConfig` dataclass at startup. `web_pin` is the exception: it is persisted to the gitignored `desktop/data/.web_pin` (never to config.json), with one-shot migration from the legacy plaintext key.

## Current State

- **Face detection** — 模型在 `desktop/models/`：`face_detection_yunet_2023mar.onnx`（YuNet 检测）
  + `face_recognition_sface_2021dec.onnx`（SFace 128 维特征），均来自 OpenCV Zoo 并经官方
  LFS sha256 校验。走 `cv2.FaceDetectorYN`/`cv2.FaceRecognizerSF`，**不依赖** OpenCV 5 已移除的
  `readNetFromCaffe`/`readNetFromTorch`，`requirements.txt` 因此不再钉 `<5`。
  人脸**不参与**查重判定，仅作 `face_hints` 线索展示（见 Dedup Pipeline）。
  旧的 Caffe/Torch 模型（`deploy.prototxt`、`res10_*.caffemodel`、`nn4.small2.v1.t7`，约 40 MB）
  已从 `desktop/models/` 删除；`build.spec` 按 `('models/', 'models')` 整目录收集，无需改动
- **Web PIN auth** — 桌面端设置 4 位密码，Web 端输入验证后进入
- **API auth** — `/api/auth/status` + `/api/auth/verify` 端点
- **One-click dedup** — 查重按钮自动衔接哈希索引 → 查重，无需手动分步；
  Web 端「一键查重」调 `POST /api/dedup/run`（`index_first=true`）走同一编排，
  进度按 `phase`（indexing/comparing/faces）+ `progress` 轮询展示；
  `refine_faces=true` 时中间插入"候选视频多帧人脸精查"并重跑一次比对
- **Web 文件管理** — 文件列表页「管理」进入多选，底部操作栏支持全选 + 批量删除
  （`POST /api/files/batch-delete`）；单元卡片菜单支持「删除文件夹」
  （`DELETE /api/units/{id}`）。默认 `mode=trash`：**移至回收站** + 删库记录，
  与桌面端语义一致（`mode=record` 才只出库不删盘）；删除后重算单元
  `file_count`/`total_size`
- **Web 删除补救路径** — 回收站不可用（网络盘 / 无回收站分区）或磁盘未连接时服务端回
  409，客户端弹出「永久删除（不可恢复）/ 仅从媒体库移除」二选一
  （`showDeleteFallback` → `mode=delete|record` 重试）；磁盘未连接时只提示插盘。
  永久删除永远由用户点选，不静默降级
- **Web 查重分级** — 列表页三个 Tab：**疑似重复**（`level=duplicate`）/ **疑似相关**
  （`level=related&evidence=file`）/ **同演员**（`level=related&evidence=face`），
  计数来自 `/api/dedup/counts` 的 `duplicate/related/face_only`
- **Web 查重二级分组** — 列表按**左侧单元**折叠（`GET /api/dedup/groups`，分组键 =
  存储时较小的 `unit_a_id`，与卡片左侧一致），展开时才拉该组子项
  （`/results?...&unit_a_id=<anchor>`）。380 条平铺 → 52 组
- **Web 查重详情缩略图对比** — 每条匹配/线索渲染左右两张缩略图
  （`/api/files/{id}/thumbnail`，`loading="lazy"` + 失败占位），点图进 `/preview/{id}`；
  人脸线索段默认折叠，行内显示"多帧吻合 N 帧"。卡片数字不再显示无意义的 `0%`：
  重复/相关显示「N/M 个文件重叠」，同演员显示人脸线索条数
- **Web 人脸重扫** — `GET /api/dedup/face-scan/status` 显示待扫数量，
  `POST /api/dedup/face-scan {scope}` 精查候选或补扫全部（完成后自动重跑查重）；
  桌面端对应「工具 → 重扫视频人脸」。查重页还有人脸阈值下拉
  （0.363/0.45/0.50/0.60，localStorage 记忆）与「精查疑似同演员视频」开关
- **Web 查重"旧结论"提示** — `counts.stale > 0` 时页面顶部出琥珀色提示条
  （"有 N 条结果是用旧规则算出来的" + 「立即重算」），卡片显示「旧规则结果 / 待重算」
  而不是旧口径的数字（旧行 `match_count=46` 配 33 个文件的单元曾被渲染成
  "46/33 个文件重叠"，用户以为界面递归了）；分组头标"含旧规则结果"
- **Web 查重进度跨页存活** — 切到别的 Tab 再回来（或整页刷新）时自动接上仍在跑的
  任务：模块级 `_dedupRun` 镜像 + `GET /api/dedup/active-task`，进度条/「查重中…」/
  阶段文案立刻恢复（实测报障：进度条消失只剩风扇转）；任务在别处跑完，回来显示
  完成摘要并刷新列表
- **Web 查重状态记忆** — 当前 Tab（`_dedupLevel`）、已展开的分组（`_dedupExpanded`，
  按 Tab 分开记）、列表滚动位置（`_dedupScrollTop`）跨路由保留：看完详情返回仍停在
  「疑似相关」且原分组是展开的（实测报障：返回跳回「疑似重复」且收起）。
  分组的子项不缓存，返回时重新拉一次
- **控制台安静** — OpenCV/Qt 的 FFmpeg 解码日志统一静音
  （`image_helpers.silence_ffmpeg_logs` / `preview_dialog._silence_qt_ffmpeg_logs`），
  否则损坏视频会逐帧刷红色 h264 告警；静音只去噪音，不改变"这些文件确实损坏"的事实
- **Config** — 设置对话框支持 web_pin、缩略图、API 等全部字段


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
