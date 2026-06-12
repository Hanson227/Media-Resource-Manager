# -*- coding: utf-8 -*-
"""
文件监控器 —— 封装 watchdog 实现对媒体库文件夹的实时变动监控。

当监控目录中有文件创建、修改、删除或移动时：
- 去抖动后触发回调
- 自动更新数据库中的文件索引
- 调用查重引擎比对新增文件
"""

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, FrozenSet

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from app.utils.media_types import is_media_file

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileChangeEvent:
    """标准化的文件变更事件（与 watchdog 解耦）。"""

    path: Path
    """发生变更的文件路径。"""

    change_type: str
    """变更类型：'created'/'modified'/'deleted'/'moved'。"""

    src_path: Optional[Path] = None
    """原始路径（仅 move 事件）。"""

    is_directory: bool = False
    """是否为目录事件。"""


class MediaFileEventHandler(FileSystemEventHandler):
    """watchdog 事件处理器 —— 只关心媒体文件变更。

    具备去抖动机制：同一文件在 debounce_ms 内多次事件合并为一次。
    """

    def __init__(
        self,
        extensions: FrozenSet[str],
        on_change: Callable[[FileChangeEvent], None],
        debounce_ms: int = 2000,
    ) -> None:
        """初始化事件处理器。

        参数:
            extensions: 关注的媒体扩展名集合。
            on_change: 当媒体文件变动时的回调函数。
            debounce_ms: 去抖动时间（毫秒）。
        """
        super().__init__()
        self._extensions = extensions
        self._on_change = on_change
        self._debounce_ms = debounce_ms / 1000.0
        self._pending_events: dict[str, tuple[threading.Timer, int]] = {}
        self._timer_counter = 0
        self._lock = threading.Lock()

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_event(event, "created")

    def on_modified(self, event: FileSystemEvent) -> None:
        # 文件修改事件非常频繁，只处理媒体文件
        if not event.is_directory:
            path = Path(event.src_path)
            if is_media_file(path, self._extensions):
                self._handle_event(event, "modified")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle_event(event, "deleted")

    def on_moved(self, event: FileSystemEvent) -> None:
        """文件移动/重命名事件。

        如果目标在监控范围内且是媒体文件，视为移动/创建；
        如果源文件是媒体文件但目标不在监控范围内，视为删除。
        """
        src = Path(event.src_path)
        dest = Path(event.dest_path)
        src_is_media = is_media_file(src, self._extensions) if not event.is_directory else False
        dest_is_media = is_media_file(dest, self._extensions) if not event.is_directory else False

        if dest_is_media:
            # 移入或重命名为媒体文件
            change_event = FileChangeEvent(
                path=dest,
                change_type="moved",
                src_path=src,
                is_directory=event.is_directory,
            )
            self._debounce_and_emit(str(dest), change_event)
        elif src_is_media and not dest_is_media:
            # 移出监控范围或重命名为非媒体文件 → 视为删除
            change_event = FileChangeEvent(
                path=src,
                change_type="deleted",
                is_directory=event.is_directory,
            )
            self._debounce_and_emit(str(src), change_event)

    def _handle_event(self, event: FileSystemEvent, change_type: str) -> None:
        """处理文件系统事件。"""
        path = Path(event.src_path)
        if not event.is_directory and not is_media_file(path, self._extensions):
            return

        change_event = FileChangeEvent(
            path=path,
            change_type=change_type,
            is_directory=event.is_directory,
        )
        self._debounce_and_emit(str(path), change_event)

    def _debounce_and_emit(self, key: str, event: FileChangeEvent) -> None:
        """对同一文件的连续事件进行去抖动处理。

        使用自增 token 避免多线程竞态：当旧 timer 在新 timer 创建后
        才触发回调时，旧 timer 的 token 已过时，不会误删新 timer。
        """
        with self._lock:
            # 取消之前的定时器
            if key in self._pending_events:
                old_timer, _ = self._pending_events[key]
                old_timer.cancel()

            # 创建新的定时器，带唯一 token
            self._timer_counter += 1
            token = self._timer_counter
            timer = threading.Timer(
                self._debounce_ms,
                lambda k=key, ev=event, t=token: self._emit(k, ev, t),
            )
            self._pending_events[key] = (timer, token)
            timer.start()

    def _emit(self, key: str, event: FileChangeEvent, token: int) -> None:
        """触发回调并从待处理列表中移除（仅当 token 匹配时）。"""
        with self._lock:
            stored = self._pending_events.get(key)
            if stored is None or stored[1] != token:
                return  # 此 timer 已被更新的 timer 替代
            del self._pending_events[key]
        try:
            self._on_change(event)
        except Exception as e:
            logger.error(f"文件变更回调异常: {event.path} - {e}")


    def cancel_all_pending(self) -> None:
        """取消所有待处理的去抖动定时器。"""
        with self._lock:
            for timer, _ in self._pending_events.values():
                timer.cancel()
            self._pending_events.clear()


class FileWatcher:
    """文件系统监控器。

    管理 watchdog Observer 的生命周期，
    支持动态添加/移除监控目录。
    """

    def __init__(
        self,
        event_handler: FileSystemEventHandler,
    ) -> None:
        """初始化监控器。

        参数:
            event_handler: watchdog 事件处理器。
        """
        self._observer = Observer()
        self._event_handler = event_handler
        self._watched_paths: dict[str, object] = {}
        """{path_str: watch_handle} 映射，用于后续取消监控。"""
        self._running = False

    def start(self) -> None:
        """启动文件监控。"""
        if self._running:
            return
        self._observer.start()
        self._running = True
        logger.info("文件监控已启动")

    def stop(self) -> None:
        """停止文件监控。"""
        if not self._running:
            return
        # 清理待处理的去抖动定时器
        if isinstance(self._event_handler, MediaFileEventHandler):
            self._event_handler.cancel_all_pending()
        self._observer.stop()
        self._observer.join(timeout=5)
        self._running = False
        logger.info("文件监控已停止")

    def add_root(self, root: Path) -> None:
        """添加监控目录。

        参数:
            root: 要监控的根目录路径。
        """
        root_str = str(root)
        if root_str in self._watched_paths:
            return
        if not root.is_dir():
            logger.warning(f"监控目录不存在: {root}")
            return

        watch_handle = self._observer.schedule(
            self._event_handler, root_str, recursive=True,
        )
        self._watched_paths[root_str] = watch_handle
        logger.info(f"已添加监控: {root}")

    def remove_root(self, root: Path) -> None:
        """移除监控目录。

        参数:
            root: 要移除的根目录路径。
        """
        root_str = str(root)
        if root_str in self._watched_paths:
            watch_handle = self._watched_paths.pop(root_str)
            self._observer.unschedule(watch_handle)
            logger.info(f"已移除监控: {root}")

    @property
    def is_running(self) -> bool:
        """是否正在运行。"""
        return self._running
