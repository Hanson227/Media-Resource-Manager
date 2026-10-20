# -*- coding: utf-8 -*-
"""版本与构建信息 —— 用来一眼看出"平板上的便携包是不是旧的"。

为什么需要：便携版是解压即用的目录包，用户升级时常常只替换了 exe、或干脆还
在跑几个月前的解压副本。排查"为什么没有某个入口"之前，先看这里的版本与构建
时间，能省掉大量猜测（`/api/features.build` 与 设置→关于 都显示它）。

冻结（PyInstaller）后没有 git 信息，用 exe 的修改时间当"构建时间"—— 打包时间
就等于 exe 写入磁盘的时间，足够区分新旧。
"""

import sys
from datetime import datetime
from pathlib import Path

__version__ = "0.1.0"


def build_stamp() -> dict:
    """返回 {version, built_at, frozen, runtime}。

    - `built_at`：冻结包取 exe 的 mtime；源码运行取空串（源码没有"构建"概念）。
    - `runtime`：`frozen` / `python`，便于判断跑的是包还是源码。
    """
    frozen = bool(getattr(sys, "frozen", False))
    built_at = ""
    if frozen:
        try:
            built_at = datetime.fromtimestamp(
                Path(sys.executable).stat().st_mtime).isoformat(timespec="seconds")
        except OSError:
            built_at = ""
    return {
        "version": __version__,
        "built_at": built_at,
        "frozen": frozen,
        "runtime": "frozen" if frozen else "python",
    }
