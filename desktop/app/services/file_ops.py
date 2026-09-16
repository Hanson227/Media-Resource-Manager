# -*- coding: utf-8 -*-
"""
文件系统操作服务 —— HTTP API 删除语义的唯一实现。

桌面端删除一律"移至回收站"（send2trash），而不是直接 unlink：误删可恢复。
HTTP API 早期只删数据库记录、磁盘文件原样留在原地（重扫后又会回来），
与桌面端行为分叉。本模块把 API 侧的删除语义收敛到一处（GUI 的
`_delete_file` / `_on_delete_unit` 走自己的 QMessageBox 交互流程，语义一致）；
后续若要让 GUI 复用它，只替换"取路径 → 调 apply_mode"这两步即可。

删除模式（API 的 mode 参数）：
  - trash ：移至回收站 + 删数据库记录（默认，与桌面端一致）
  - record：仅删数据库记录，磁盘文件保留（"从媒体库移除"）
"""

import logging
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

MODE_TRASH = "trash"
"""移至回收站 + 删库记录（默认）。"""

MODE_RECORD = "record"
"""仅删库记录，磁盘文件保留。"""

DELETE_MODES = (MODE_TRASH, MODE_RECORD)
"""合法的删除模式（API 的 pattern 由此拼出，避免两处枚举漂移）。"""

MODE_PATTERN = "^(" + "|".join(DELETE_MODES) + ")$"


def move_to_trash(path: Union[str, Path]) -> tuple[bool, str]:
    """把文件或文件夹移至系统回收站。

    参数:
        path: 目标路径。

    返回:
        (是否成功, 说明)。路径本就不存在时视为成功 —— 调用方要达成的
        终态（该路径不再存在）已经满足，不应报错。
    """
    target = Path(path)
    if not target.exists():
        return True, "路径已不存在，无需删除"

    try:
        import send2trash
    except ImportError:
        logger.error("send2trash 未安装，拒绝直接删除: %s", target)
        return False, "send2trash 未安装，无法安全删除（请执行 pip install send2trash）"

    try:
        send2trash.send2trash(str(target))
        return True, "已移至回收站"
    except Exception as e:
        # 网络路径（UNC / 映射盘）无法进回收站，需人工处理；直接 unlink 会
        # 造成不可恢复的数据丢失，因此这里只报错、不降级。
        logger.error("移至回收站失败 %s: %s", target, e)
        return False, f"无法移至回收站: {e}"


def apply_mode(path: Optional[str], mode: str) -> tuple[bool, str]:
    """按删除模式处理磁盘文件。

    参数:
        path: 文件/文件夹的磁盘路径（None 或空串时跳过磁盘操作）。
        mode: MODE_TRASH 或 MODE_RECORD。

    返回:
        (是否继续删除数据库记录, 说明)。mode=record 时不做任何磁盘操作。
    """
    if mode == MODE_RECORD or not path:
        return True, "仅移除媒体库记录"
    return move_to_trash(path)
