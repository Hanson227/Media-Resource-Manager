# 代码审查记录

**审查日期**: 2026-06-12
**审查范围**: 全项目代码
**审查工具**: TRAE-code-review

---

## 统计概览

| 严重程度 | 数量 |
|---------|------|
| Critical | 5 |
| Major | 19 |
| Minor | 若干（未逐一修复） |

---

## Critical 级别问题

### #1 批量删除回收站选项无效

- **文件**: `desktop/app/ui/dialogs/batch_operations.py:190`
- **问题**: 三元表达式 `"delete" if send_to_trash else "delete"` 两分支完全相同，UI 上的"移至回收站"选项形同虚设，文件永远被永久删除
- **修复**: 改为 `"trash" if send_to_trash else "delete"`
- **影响**: 用户数据不可恢复丢失

### #2 孤儿缩略图检测逻辑永远匹配不到

- **文件**: `desktop/app/services/cleanup_service.py:242-243`
- **问题**: `f.stem.isdigit()` 对缩略图文件名 `123_thumb` 永远返回 False（因含 `_thumb` 后缀），导致 `purge_orphaned_thumbnails()` 永远无法清理任何孤儿文件
- **修复**: 改为 `f.stem.endswith("_thumb")` + `f.stem.rsplit("_thumb", 1)[0].isdigit()` 提取 ID
- **影响**: 磁盘空间持续泄漏，孤儿缩略图无法清理

### #3 main.py 死代码导致启动清理永不执行

- **文件**: `desktop/main.py:125-134`
- **问题**: `sys.exit(1)` 在第 125 行终止进程，其后的孤儿缩略图清理代码（第 127-134 行）属于不可达死代码
- **修复**: 将清理代码移到 `try/except` 块之外（与 try/except 同级）
- **影响**: 应用启动后不会自动清理孤儿缩略图

### #4 查重对比白名单/忽略信号硬编码 emit(0)

- **文件**: `desktop/app/ui/dialogs/dedup_compare.py:143-148`
- **问题**: `whitelist_requested.emit(0)` 和 `ignore_requested.emit(0)` 硬编码为 0，接收端无法识别是哪个查重结果，白名单和忽略功能完全失效
- **修复**: 改为 `emit(self._result.unit_a_id)`
- **影响**: 查重核心功能（白名单/忽略）不可用

### #5 QImage 引用 numpy 数据悬空指针

- **文件**: `desktop/app/ui/dialogs/preview_dialog.py:324-326`
- **问题**: `QImage(rgb.data, ...)` 使用 numpy 数组内存指针但不持有引用，numpy 数组被 GC 回收后 QImage 指向已释放内存
- **修复**: 添加 `qimg._numpy_ref = rgb` 保持引用
- **影响**: 视频预览可能崩溃或画面损坏

---

## Major 级别问题

### #6 API 默认绑定 0.0.0.0 无认证

- **文件**: `desktop/config.py:126-133`
- **问题**: `api_host` 默认 `"0.0.0.0"` 监听所有网络接口，`web_pin` 默认空字符串，局域网内任何设备可无认证访问 API
- **修复**: 默认改为 `"127.0.0.1"`（仅本机访问）

### #7 所有 API 端点无认证保护

- **文件**: `desktop/app/api/server.py` 及所有路由
- **问题**: 除 `/api/auth/verify` 外所有端点无认证，PIN 验证形同虚设
- **修复**: 需实现基于 token/session 的认证中间件（本次审查标记，待后续实现）

### #8 _cancelled 标志永不重置（3处）

- **文件**: `desktop/app/core/scanner.py:130-134`、`hash_engine.py:127-134`、`dedup_engine.py:132-136`
- **问题**: `cancel()` 设置 `_cancelled = True` 后永不重置，引擎实例不可复用
- **修复**: 添加 `_reset_cancel()` 方法，在每次操作开始时调用

### #9 HEIC 转换后源文件删除验证不足

- **文件**: `desktop/app/core/heic_converter.py:114-123`
- **问题**: 仅检查 `st_size > 0` 就删除源 HEIC 文件，非零但损坏的文件会导致原始数据丢失
- **修复**: 增加 `Image.open(target).verify()` 验证目标文件完整性

### #10 mkstemp 文件描述符泄漏

- **文件**: `desktop/app/ui/workers/hash_worker.py:74`
- **问题**: `tempfile.mkstemp()` 返回的 FD 未调用 `os.close()` 关闭，每个视频人脸检测泄漏一个 FD
- **修复**: `mkstemp` 后立即 `os.close(tmp_fd)`

### #11 VACUUM 在事务中执行必然失败

- **文件**: `desktop/app/db/engine.py:150-153`
- **问题**: SQLite 的 VACUUM 不能在事务内执行，当前写法会抛出 `OperationalError`
- **修复**: 使用 `execution_options(isolation_level="AUTOCOMMIT")`

### #12 is_whitelisted 忽略正则白名单规则

- **文件**: `desktop/app/db/queries.py:574-581`
- **问题**: 仅查询 `is_regex == False` 的精确匹配，完全忽略 `is_regex == True` 的正则白名单
- **修复**: 添加正则匹配逻辑，遍历 `is_regex == True` 的规则执行 `re.search()`

### #13 Content-Disposition 头注入

- **文件**: `desktop/app/api/routes/files.py:208`
- **问题**: `f.filename` 未转义直接拼入 HTTP 头，文件名含双引号或换行符可导致头注入
- **修复**: 使用 `urllib.parse.quote` RFC 5987 编码

### #14 Worker 未取消就覆盖引用

- **文件**: `desktop/app/ui/main_window.py:392-399`
- **问题**: 创建新 ScanWorker 前不取消旧 Worker，旧 Worker 信号仍触发导致状态混乱
- **修复**: 创建新 Worker 前先 `cancel()` + `wait(5000)`

### #15 FileWatcher 停止时未取消待处理定时器

- **文件**: `desktop/app/core/watcher.py:189-196`
- **问题**: `stop()` 不取消 `_pending_events` 中的 `threading.Timer`，回调在监控停止后仍触发
- **修复**: 在 `MediaFileEventHandler` 添加 `cancel_all_pending()` 方法，`stop()` 时调用

### #16 缩略图缓存无界增长

- **文件**: `desktop/app/ui/right_panel/thumbnail_grid.py:129`
- **问题**: `_thumb_cache` 只增不减，大量缩略图 QPixmap 累积可消耗数百 MB 内存
- **修复**: 添加 `_max_cache_size = 500` 上限，`add_thumb` 时淘汰旧条目

### #17 FolderPreviewWorker lambda 捕获局部 folder_model

- **文件**: `desktop/app/ui/right_panel/thumbnail_grid.py:487-491`
- **问题**: lambda 捕获局部变量 `folder_model`，视图切换后信号仍更新旧模型
- **修复**: lambda 中检查 `self.model() is folder_model` 再更新

### #18 notify_dedup 创建两条重复消息

- **文件**: `desktop/app/services/notification_service.py:92-118`
- **问题**: 先调用 `self.notify()` 创建 info 消息，再创建 dedup_alert 消息，用户看到两条内容相同的消息
- **修复**: 直接显示气泡通知，不通过 `self.notify()` 创建 info 消息

### #19 message_center 返回 detached ORM 对象

- **文件**: `desktop/app/services/message_center.py:136-153`
- **问题**: 在 `with DatabaseManager.session()` 块内查询 ORM 对象并返回，session 关闭后对象变为 detached 状态
- **修复**: 添加 `_msg_to_dict()` 辅助函数，返回 dict 而非 ORM 对象

### #20 delete_safe 回退到永久删除

- **文件**: `desktop/app/services/cleanup_service.py:111-114`
- **问题**: `send2trash` 不可用时静默永久删除，与"安全删除"语义矛盾
- **修复**: 改为抛出 `RuntimeError`，拒绝永久删除

### #21 @classmethod @property Python 3.13 不兼容

- **文件**: `desktop/app/db/engine.py:166-168`
- **问题**: `@classmethod @property` 在 Python 3.13 已移除，调用会抛出 TypeError
- **修复**: 移除 `@property`，改为普通 `@classmethod` 方法

### #22 hash_worker 捕获 BaseException

- **文件**: `desktop/app/ui/workers/hash_worker.py:215`
- **问题**: `except BaseException` 会吞掉 `KeyboardInterrupt` 和 `SystemExit`，阻止程序退出
- **修复**: 改为 `except Exception`

### #23 所有路由异常信息泄露

- **文件**: 所有 API 路由文件（6个文件，26处）
- **问题**: `raise HTTPException(status_code=500, detail=str(e))` 将完整异常信息返回客户端，可能泄露数据库结构、文件路径等
- **修复**: 统一改为 `detail="内部服务器错误"`，实际异常通过 `logger.error()` 记录

### #24 Socket 异常路径未关闭

- **文件**: `desktop/app/ui/widgets/status_bar.py:29-39`
- **问题**: `_get_local_ip()` 中 `s.connect()` 抛异常时 `s.close()` 不执行，socket FD 泄漏
- **修复**: 使用 `try/finally` 确保 `s.close()`

---

## 未修复的已知问题

以下问题在本次审查中发现但未修复，需后续处理：

1. **API 端点无认证中间件** (#7): 需设计完整的 token/session 认证机制
2. **PIN 验证时序攻击**: 建议使用 `hmac.compare_digest()` 替代 `==`
3. **PIN 无速率限制**: 建议添加 `slowapi` 或类似中间件
4. **CORS `allow_origins=["*"]` 与 `allow_credentials=True` 矛盾**: 需根据实际需求调整
5. **N+1 查询问题**: `files.py` 和 `units.py` 中的全量加载+内存分页
6. **hash_engine 类级别状态竞态**: `_face_detector` 等类变量无锁保护
7. **dedup_engine MD5 索引冲突**: `dict[str, int]` 覆盖同 MD5 文件
8. **thumbnail_generator LOAD_TRUNCATED_IMAGES 全局副作用**: 影响所有 PIL 操作
9. **scan_worker 在 DB 事务内发射信号**: 可能导致 "database is locked"
10. **dedup_worker 变量名遮蔽 DB session**: 维护风险

---

## 修改文件清单

| 文件 | 修改类型 |
|------|---------|
| `desktop/main.py` | 死代码修复 |
| `desktop/config.py` | 安全配置 |
| `desktop/app/ui/dialogs/batch_operations.py` | 逻辑 Bug |
| `desktop/app/ui/dialogs/dedup_compare.py` | 信号硬编码 |
| `desktop/app/ui/dialogs/preview_dialog.py` | 悬空指针 |
| `desktop/app/core/scanner.py` | 取消标志重置 |
| `desktop/app/core/hash_engine.py` | 取消标志重置 |
| `desktop/app/core/dedup_engine.py` | 取消标志重置 |
| `desktop/app/core/heic_converter.py` | 文件验证增强 |
| `desktop/app/core/watcher.py` | 定时器清理 |
| `desktop/app/db/engine.py` | VACUUM + Python 3.13 兼容 |
| `desktop/app/db/queries.py` | 正则白名单支持 |
| `desktop/app/api/routes/files.py` | 头注入 + 异常泄露 |
| `desktop/app/api/routes/tags.py` | 异常泄露 |
| `desktop/app/api/routes/dedup.py` | 异常泄露 |
| `desktop/app/api/routes/events.py` | 异常泄露 |
| `desktop/app/api/routes/units.py` | 异常泄露 |
| `desktop/app/api/routes/messages.py` | 异常泄露 |
| `desktop/app/ui/main_window.py` | Worker 取消 |
| `desktop/app/ui/right_panel/thumbnail_grid.py` | 缓存淘汰 + lambda 修复 |
| `desktop/app/ui/workers/hash_worker.py` | FD 泄漏 + BaseException |
| `desktop/app/ui/widgets/status_bar.py` | Socket 泄漏 |
| `desktop/app/services/cleanup_service.py` | 孤儿检测 + 删除安全 |
| `desktop/app/services/notification_service.py` | 重复消息 |
| `desktop/app/services/message_center.py` | Detached ORM |
