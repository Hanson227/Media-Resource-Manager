# -*- coding: utf-8 -*-
"""
后台长任务登记表 —— 查重 / 人脸重扫 / HEIC 转换共用。

为什么需要（而不是各路由各存一份字典）：
- 客户端的"跨页存活"依赖 `GET /api/*/active-task`：页面丢了 task_id 就只能靠
  风扇判断任务在不在跑。三个路由各写一份必然漂移（字段名、清理策略、取消语义）。
- 互斥门（dedup_gate）保证同时只有一个重活，但任务**登记**与互斥是两件事：
  登记表还要负责 404 判定、超期清理、取消标志。

线程安全：所有读写都在 `_lock` 内；任务体只通过 update() 改自己那一行。
"""

import itertools
import threading
import time
from typing import Optional, Sequence

TASK_CAP = 20
"""保留的任务记录上限（超出先淘汰最旧的已完成任务）。"""

TASK_TTL_SECONDS = 3600
"""已完成任务的保留时间（超期清理，避免进程内无限增长）。"""

_ACTIVE_STATUSES = ("queued", "running")

_counter = itertools.count(1)
_lock = threading.Lock()
_tasks: dict[str, dict] = {}


def next_task_id(kind: str) -> str:
    """生成任务 ID，如 `heic-3`（kind 前缀让日志/前端一眼看出任务类型）。"""
    return f"{kind}-{next(_counter)}"


def prune(now: Optional[float] = None) -> None:
    """清理超期已完成任务；超额时先淘汰最旧已完成任务。"""
    now = time.time() if now is None else now
    with _lock:
        expired = [
            tid for tid, t in _tasks.items()
            if t.get("finished_at") and now - t["finished_at"] > TASK_TTL_SECONDS
        ]
        for tid in expired:
            _tasks.pop(tid, None)
        finished = sorted(
            (tid for tid, t in _tasks.items()
             if t.get("status") in ("completed", "failed", "cancelled")),
            key=lambda tid: _tasks[tid].get("finished_at") or 0,
        )
        while len(_tasks) > TASK_CAP and finished:
            _tasks.pop(finished.pop(0), None)


def register(task_id: str, kind: str, *, phase: Optional[str] = None,
             progress=None) -> dict:
    """登记一个新任务（status=queued）。"""
    now = time.time()
    entry = {
        "task_id": task_id,
        "kind": kind,
        "status": "queued",
        "phase": phase,
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "progress": progress,
        "result": None,
        "error": None,
        "cancel_requested": False,
    }
    with _lock:
        _tasks[task_id] = entry
    return entry


def update(task_id: str, **fields) -> None:
    """更新任务字段（任务体在执行过程中调用）。"""
    with _lock:
        if task_id in _tasks:
            _tasks[task_id].update(fields)


def get(task_id: str) -> Optional[dict]:
    """取任务快照（dict 拷贝，避免调用方改到登记表）。"""
    with _lock:
        t = _tasks.get(task_id)
        return dict(t) if t else None


def active(kinds: Sequence[str], now: Optional[float] = None) -> Optional[dict]:
    """取仍在排队/运行的任务（多个时返回最新创建的那个）。

    参数:
        kinds: 关注的 kind 集合（如 ("dedup", "face")）——
            不看 kind 就会把 HEIC 转换误报成"查重在跑"。
    """
    prune(now)
    wanted = set(kinds)
    with _lock:
        items = [
            dict(t) for t in _tasks.values()
            if t.get("status") in _ACTIVE_STATUSES and t.get("kind") in wanted
        ]
    if not items:
        return None
    items.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
    return items[0]


def request_cancel(task_id: str) -> bool:
    """请求取消任务（协作式：任务体在下一个检查点停止）。"""
    with _lock:
        t = _tasks.get(task_id)
        if not t or t.get("status") not in _ACTIVE_STATUSES:
            return False
        t["cancel_requested"] = True
    return True


def is_cancel_requested(task_id: str) -> bool:
    """任务是否已被请求取消。"""
    with _lock:
        t = _tasks.get(task_id)
        return bool(t and t.get("cancel_requested"))


def cancel_active(kinds: Sequence[str]) -> Optional[str]:
    """请求取消当前活跃任务（返回被取消的 task_id；没有则 None）。"""
    t = active(kinds)
    if not t:
        return None
    return t["task_id"] if request_cancel(t["task_id"]) else None


def clear_all() -> None:
    """清空登记表（仅测试使用）。"""
    with _lock:
        _tasks.clear()


def drop(task_id: str) -> None:
    """移除一条任务记录（测试注入/清理用）。"""
    with _lock:
        _tasks.pop(task_id, None)
