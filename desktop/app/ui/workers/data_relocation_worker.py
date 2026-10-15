# -*- coding: utf-8 -*-
"""数据迁移工作线程 —— 让「迁移数据」不再卡死界面。

为什么必须放线程里：迁移要把**几千个缩略图文件**复制到另一块盘，同步跑在主线程
上时窗口完全无响应（没有进度、没有提示），用户会以为程序坏了 → 关掉/重启 →
**留下半份数据**（真实事故：DB 复制完、缩略图只复制了 428/2916，marker 和配置
都没写，用户还以为"数据已经在里面了"）。

线程只负责调用服务层并把结果原样发回主线程；界面更新一律在主线程做。
"""

import logging

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


class DataRelocationWorker(QThread):
    """后台执行 `relocate_data_root`，把进度与结果都发回主线程。"""

    progress_changed = Signal(str, int, int)
    """(阶段, 已完成, 总数)。阶段：database / thumbnails / pin / marker。"""

    finished_with = Signal(dict)
    """参数是服务层返回的 result（含 ok / message / steps / db_path ...）。"""

    def __init__(self, config, target, *, mode: str = "copy", parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._target = target
        self._mode = mode

    def run(self) -> None:  # pragma: no cover — Qt 线程入口，逻辑在服务层
        from app.services import data_relocation_service as relocation

        def _on_progress(phase: str, done: int, total: int) -> None:
            self.progress_changed.emit(phase, int(done), int(total))

        try:
            result = relocation.relocate_data_root(
                self._config, self._target, mode=self._mode, progress=_on_progress)
        except Exception as e:  # noqa: BLE001 — 线程里绝不能把异常抛出去
            logger.error(f"数据迁移线程异常: {e}", exc_info=True)
            result = {"ok": False, "steps": {}, "marker": None,
                      "target_root": str(self._target), "mode": self._mode,
                      "message": f"迁移异常: {type(e).__name__}: {e}"}
        self.finished_with.emit(result)
