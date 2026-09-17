# -*- coding: utf-8 -*-
"""
文件系统操作服务 —— HTTP API 删除语义的唯一实现。

桌面端删除一律"移至回收站"（send2trash），而不是直接 unlink：误删可恢复。
HTTP API 早期只删数据库记录、磁盘文件原样留在原地（重扫后又会回来），
与桌面端行为分叉。本模块把 API 侧的删除语义收敛到一处（GUI 的
`_delete_file` / `_on_delete_unit` 走自己的 QMessageBox 交互流程，语义一致）；
后续若要让 GUI 复用它，只替换"取路径 → 调 apply_mode"这两步即可。

删除模式（API 的 mode 参数，DELETE_MODES 是唯一来源，pattern 由它拼出）：
  - trash ：移至回收站 + 删数据库记录（默认，与桌面端一致）
  - record：仅删数据库记录，磁盘文件保留（"从媒体库移除"）
  - delete：**永久删除**磁盘文件（不可恢复）。只在回收站不可用（网络盘、
    exFAT 等无 $RECYCLE.BIN 的卷）时，由用户在客户端明确选择后使用 ——
    绝不能作为 trash 失败后的自动降级，否则等于无声永久销毁数据。

删除前的可删性判断（_volume_available）：
  路径不存在有两种完全不同的原因，必须区分：
    ① 文件被别的程序/用户删掉了 → 要达成的终态已满足，按成功处理；
    ② 所在磁盘没连接（移动盘未插 / 网络盘掉线）→ 文件其实还在，
       此时删库记录会让它"消失"但一重扫就回来（用户报的"删不了"）。
"""

import logging
import shutil
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

MODE_TRASH = "trash"
"""移至回收站 + 删库记录（默认）。"""

MODE_RECORD = "record"
"""仅删库记录，磁盘文件保留。"""

MODE_DELETE = "delete"
"""永久删除磁盘文件（不可恢复），仅由用户明确选择。"""

DELETE_MODES = (MODE_TRASH, MODE_RECORD, MODE_DELETE)
"""合法的删除模式（API 的 pattern 由此拼出，避免两处枚举漂移）。"""

MODE_PATTERN = "^(" + "|".join(DELETE_MODES) + ")$"

OFFLINE_REASON = "未连接"
"""磁盘不可用时错误原因里的关键词（客户端据此提示"先连接磁盘"，
而不是弹出"永久删除"选项 —— 盘都没连，永久删除同样做不到）。"""


def volume_label(target: Union[str, Path]) -> str:
    """给用户看的卷标识（`G:\\` 或 `\\\\server\\share`）。"""
    path = Path(target)
    return path.anchor or str(path)


def volume_available(target: Union[str, Path]) -> bool:
    """目标所在磁盘/共享当前是否可用。

    用于区分"文件被删掉了"与"磁盘没连"。相对路径（无卷概念）一律返回 True，
    由 Path.exists() 自行判断。
    """
    path = Path(target)
    if not path.anchor:
        return True
    try:
        return Path(path.anchor).exists()
    except OSError:
        return False


def _missing_path_result(target: Path) -> tuple[bool, str]:
    """路径不存在时的结论：磁盘掉线算失败，文件真没了算成功。"""
    if not volume_available(target):
        return False, (f"磁盘{OFFLINE_REASON}: {volume_label(target)}"
                       f"（请先连接该磁盘再删除）")
    return True, "路径已不存在，无需删除"


def move_to_trash(path: Union[str, Path]) -> tuple[bool, str]:
    """把文件或文件夹移至系统回收站。

    参数:
        path: 目标路径。

    返回:
        (是否成功, 说明)。路径本就不存在时视为成功（终态已满足），
        但所在**磁盘未连接**时视为失败 —— 见模块文档。
    """
    target = Path(path)
    if not target.exists():
        return _missing_path_result(target)

    try:
        import send2trash
    except ImportError:
        logger.error("send2trash 未安装，拒绝直接删除: %s", target)
        return False, "send2trash 未安装，无法安全删除（请执行 pip install send2trash）"

    try:
        send2trash.send2trash(str(target))
        return True, "已移至回收站"
    except Exception as e:
        # 网络路径（UNC / 映射盘）与部分外置卷无法进回收站，需人工处理；
        # 直接 unlink 会造成不可恢复的数据丢失，因此这里只报错、不降级
        # （客户端可据此让用户显式选择 mode=delete）。
        logger.error("移至回收站失败 %s: %s", target, e)
        return False, f"无法移至回收站: {e}"


def move_to_delete(path: Union[str, Path]) -> tuple[bool, str]:
    """永久删除文件/文件夹（不进回收站，不可恢复）。

    只在回收站不可用时由用户明确选择；同时复用"磁盘是否连接"的判断，
    避免盘掉线时把记录删掉而文件其实还在。
    """
    target = Path(path)
    if not target.exists():
        return _missing_path_result(target)

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True, "已永久删除（不可恢复）"
    except Exception as e:
        logger.error("永久删除失败 %s: %s", target, e)
        return False, f"永久删除失败: {e}"


def apply_mode(path: Optional[str], mode: str) -> tuple[bool, str]:
    """按删除模式处理磁盘文件。

    参数:
        path: 文件/文件夹的磁盘路径（None 或空串时跳过磁盘操作）。
        mode: MODE_TRASH / MODE_RECORD / MODE_DELETE。

    返回:
        (是否继续删除数据库记录, 说明)。mode=record 时不做任何磁盘操作。
    """
    if mode == MODE_RECORD or not path:
        return True, "仅移除媒体库记录"
    if mode == MODE_DELETE:
        return move_to_delete(path)
    return move_to_trash(path)
