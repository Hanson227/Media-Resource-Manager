# -*- coding: utf-8 -*-
"""
查重任务互斥门 —— 跨入口（GUI worker / HTTP API）协调单实例查重。

桌面端与 Web 端可以同时触发"一键查重"；两条路径此前互不感知，
可能并发写 dedup_results/messages 造成 SQLite 锁竞争与重复告警。
此模块提供进程内原子化的占用/释放，供两条路径共用。

用法:
    if not acquire_dedup_gate():
        # 忙：GUI 提示 / API 返回 409
        return
    try:
        ...执行查重...
    finally:
        release_dedup_gate()
"""

import threading

__all__ = [
    "acquire_dedup_gate", "release_dedup_gate", "is_dedup_busy",
]


class _DedupGate:
    """进程内查重互斥门（线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False

    def acquire(self, blocking: bool = False) -> bool:
        """尝试占用查重任务。

        返回:
            True 表示占用成功；False 表示已有查重在运行。
        """
        if not self._lock.acquire(blocking=blocking):
            return False
        try:
            if self._active:
                return False
            self._active = True
            return True
        finally:
            self._lock.release()

    def release(self) -> None:
        """释放查重任务（幂等）。"""
        with self._lock:
            self._active = False

    def is_busy(self) -> bool:
        """是否有查重任务正在运行。"""
        with self._lock:
            return self._active


_gate = _DedupGate()


def acquire_dedup_gate() -> bool:
    """占用查重互斥门（非阻塞）。"""
    return _gate.acquire(blocking=False)


def release_dedup_gate() -> None:
    """释放查重互斥门。"""
    _gate.release()


def is_dedup_busy() -> bool:
    """是否已有查重在运行。"""
    return _gate.is_busy()
