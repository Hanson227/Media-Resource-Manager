# -*- coding: utf-8 -*-
"""数据位置选择对话框 —— 启动期与运行期都能「换一个数据根」。

为什么必须有它：数据可能放在移动硬盘上，拔盘后旧实现只有「重试/退出」
两条路 —— 用户进不了软件，也就进不了设置去改数据位置（死锁）。这里提供
第三条路：就地接管盘上已有的数据根，或在空目录新建一个数据根。

两种动作（对应 `data_relocation_service` 的两个函数）：
  - **接管**（adopt）：目录里已有一份完整数据 + marker，只登记身份，
    **绝不写入/覆盖**目标数据库（接管前会备份）。
  - **新建**（create）：目录为空，建 marker + 空库；已有数据的目录会被拒绝。

本模块只负责"选哪个目录、要不要接管/新建、写回 config.json"，真正的
落盘动作全部走服务层，避免第二套实现。
"""

import logging
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from config import AppConfig
from app.services import data_relocation_service as relocate_svc
from app.utils import paths as app_paths

logger = logging.getLogger(__name__)


def _format_root(path: str) -> str:
    """候选列表的一行文本：目录 + 数据概况（单元/文件数、是否有 marker）。"""
    info = relocate_svc.inspect_data_root(path)
    if not info["is_dir"]:
        return f"{path}\n    目录不存在"
    if info.get("copy_incomplete"):
        return f"{path}\n    未完成的迁移（存在复制哨兵），不可直接接管"
    parts: List[str] = []
    if info["is_data_root"]:
        if info["db_ok"]:
            parts.append(f"{info['units']} 个单元 / {info['files']} 个文件")
        parts.append("已有数据根")
    elif info["db_exists"]:
        parts.append("有数据库但无法确认可用")
    else:
        parts.append("空目录（可新建数据根）")
    if not info["writable"]:
        parts.append("不可写")
    return f"{path}\n    " + " · ".join(parts)


class DataLocationDialog(QDialog):
    """选择/新建/接管数据根的对话框。

    用法：
        dlg = DataLocationDialog(config, resolved, parent)
        if dlg.exec() == QDialog.Accepted and dlg.result_config:
            config = dlg.result_config
    """

    def __init__(self, config: AppConfig, resolved=None, parent=None,
                 *, title: str = "选择数据位置",
                 message: str = "") -> None:
        super().__init__(parent)
        self._config = config
        self._resolved = resolved
        self._chosen: str = ""
        self.result_config: Optional[AppConfig] = None
        self.result_message: str = ""
        # 切库失败的原因由这里传给调用方，不要在这一层弹模态框：
        # 测试/后台线程调用时没有事件循环，一个 QMessageBox 会把整个进程挂住。
        self._last_switch_error: str = ""

        self.setWindowTitle(title)
        self.resize(760, 460)
        self._setup_ui(message)
        self._load_candidates()

    # ============================================================
    # UI
    # ============================================================

    def _setup_ui(self, message: str) -> None:
        layout = QVBoxLayout(self)

        hint = QLabel(
            message or (
                "媒体数据（数据库 / 缩略图 / Web PIN）可以放在移动硬盘上的一个目录里。\n"
                "下面列出在本机各磁盘上找到的数据根；也可以点「浏览…」手动指定目录：\n"
                "· 目录里已有数据 → 接管这份数据（不复制、不覆盖）\n"
                "· 目录为空 → 在这里新建一个空数据根"))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self._list = QListWidget()
        self._list.currentItemChanged.connect(lambda *_: self._sync_buttons())
        self._list.itemDoubleClicked.connect(lambda *_: self._on_ok())
        row.addWidget(self._list, 1)
        layout.addLayout(row, 1)

        actions = QHBoxLayout()
        self._browse_btn = QPushButton("浏览…")
        self._browse_btn.clicked.connect(self._on_browse)
        actions.addWidget(self._browse_btn)
        self._rescan_btn = QPushButton("重新扫描")
        self._rescan_btn.clicked.connect(self._load_candidates)
        actions.addWidget(self._rescan_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        self._buttons.button(QDialogButtonBox.Ok).setText("使用这个位置")
        self._buttons.button(QDialogButtonBox.Cancel).setText("取消")
        layout.addWidget(self._buttons)

    # ============================================================
    # 候选
    # ============================================================

    def _load_candidates(self) -> None:
        self._list.clear()
        seen: List[str] = []
        expected = ""
        if self._resolved is not None and getattr(self._resolved, "expected_data_root", None):
            expected = str(self._resolved.expected_data_root)
        candidates: List[str] = []
        if expected:
            candidates.append(expected)
        if self._resolved is not None:
            for candidate in getattr(self._resolved, "candidates", ()) or ():
                candidates.append(str(candidate))
        # 配置里当前写的数据根即使盘没接也列出来，让用户看得到"原本在哪"
        cfg_root = getattr(self._config, "data_root", None)
        if cfg_root and str(cfg_root) not in candidates:
            candidates.append(str(cfg_root))

        for path in candidates:
            key = str(Path(path)).lower()
            if key in seen:
                continue
            seen.append(key)
            item = QListWidgetItem(_format_root(str(path)))
            item.setData(Qt.UserRole, str(path))
            self._list.addItem(item)

        if self._list.count():
            self._list.setCurrentRow(0)
        else:
            placeholder = QListWidgetItem(
                "没有在本机磁盘上找到数据根。\n"
                "请点「浏览…」指定数据目录，或插入移动硬盘后点「重新扫描」。")
            placeholder.setFlags(Qt.NoItemFlags)
            self._list.addItem(placeholder)
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        item = self._list.currentItem()
        has = bool(item and (item.flags() & Qt.ItemIsSelectable))
        self._buttons.button(QDialogButtonBox.Ok).setEnabled(has)

    def _add_path(self, path: str) -> None:
        key = str(Path(path)).lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            if str(item.data(Qt.UserRole) or "").lower() == key:
                self._list.setCurrentRow(i)
                return
        item = QListWidgetItem(_format_root(path))
        item.setData(Qt.UserRole, path)
        self._list.addItem(item)
        self._list.setCurrentItem(item)

    def _on_browse(self) -> None:
        start = self._chosen or ""
        if not start:
            cfg_root = getattr(self._config, "data_root", None)
            start = str(cfg_root) if cfg_root else app_paths.app_dir()
        chosen = QFileDialog.getExistingDirectory(
            self, "选择数据目录（可新建或接管已有数据）", start)
        if chosen:
            self._add_path(chosen)

    # ============================================================
    # 确定
    # ============================================================

    def _on_ok(self) -> None:
        item = self._list.currentItem()
        if not item:
            return
        path = str(item.data(Qt.UserRole) or "")
        if not path:
            return
        self._chosen = path
        info = relocate_svc.inspect_data_root(path)
        if not info["is_dir"]:
            QMessageBox.warning(self, "目录不可用", f"目录不存在：{path}")
            return
        if info.get("copy_incomplete"):
            QMessageBox.warning(
                self, "迁移未完成",
                f"这个目录里有未完成的迁移残留（{info['db_detail']}）。\n"
                "请先重新执行迁移，或确认数据完整后手动删除复制哨兵文件再试。")
            return
        if not info["writable"]:
            QMessageBox.warning(self, "目录不可写", f"没有写入权限：{path}")
            return

        if info["is_data_root"]:
            result = self._adopt(path, info)
        else:
            result = self._create(path)
        if result is None:
            return
        self.result_config = result
        self.accept()

    def _adopt(self, path: str, info: dict) -> Optional[AppConfig]:
        detail = (f"{info['units']} 个单元 / {info['files']} 个文件"
                  if info["db_ok"] else "有数据根标记")
        if QMessageBox.question(
            self, "接管已有数据",
            f"这个目录里已经有一份数据（{detail}）：\n{path}\n\n"
            "「接管」= 直接使用这份数据，不复制、不覆盖；接管前会自动备份数据库。\n"
            "对，使用这份数据？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        ) != QMessageBox.Yes:
            return None
        result = relocate_svc.adopt_data_root(self._config, path)
        if not result.get("ok"):
            QMessageBox.critical(self, "接管失败", result.get("message") or "未知错误")
            return None
        marker = result.get("marker") or {}
        new_config = self._config.with_updates(
            data_root=str(result["target_root"]),
            data_root_id=str(marker.get("data_root_id") or ""),
            db_path=str(result["db_path"]),
            thumbnail_cache_dir=str(result["thumbnail_cache_dir"]),
        )
        if not self._apply_location(new_config):
            return None
        self.result_message = result.get("message") or f"已接管 {path}"
        return new_config

    def _create(self, path: str) -> Optional[AppConfig]:
        from app.db.engine import DatabaseManager

        if QMessageBox.question(
            self, "新建数据根",
            f"这个目录是空的（或没有数据）：\n{path}\n\n"
            "在这里新建一个空数据根（数据库 / 缩略图会存到这个目录）吗？\n"
            "注意：不会删除目录里已有的任何文件。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return None
        # 引擎已经初始化 = 程序正在运行（设置页里切换）：此时只登记位置，
        # 空库留给下次启动建 —— 否则 dispose 会把正在用的库关掉，界面瞬间变空。
        result = relocate_svc.create_data_root(
            self._config, path,
            init_database=not DatabaseManager.is_initialized())
        if not result.get("ok"):
            QMessageBox.critical(self, "新建失败", result.get("message") or "未知错误")
            return None
        marker = result.get("marker") or {}
        new_config = self._config.with_updates(
            data_root=str(result["target_root"]),
            data_root_id=str(marker.get("data_root_id") or ""),
            db_path=str(result["db_path"]),
            thumbnail_cache_dir=str(result["thumbnail_cache_dir"]),
        )
        if not self._apply_location(new_config):
            return None
        self.result_message = f"已在 {path} 新建数据根（空库）"
        return new_config

    def _apply_location(self, new_config: AppConfig) -> bool:
        """切引擎 → 落盘；任何一步失败都回到原状态（不留半套配置）。"""
        from app.db.engine import DatabaseManager
        previous_db = DatabaseManager.current_db_path()
        if not self._switch_runtime_database(new_config):
            QMessageBox.critical(
                self, "切换数据库失败",
                self._last_switch_error or "未知错误")
            return False
        if not self._persist(new_config):
            # 配置没写成功就别把引擎留在新库，否则这次会话写进新库、重启又回旧库
            self._restore_engine(previous_db)
            return False
        return True

    @staticmethod
    def _restore_engine(previous_db) -> None:
        from app.db.engine import DatabaseManager
        from app.db.migrations import migrate_db
        if previous_db is None or not DatabaseManager.is_initialized():
            return
        try:
            DatabaseManager.switch_to(Path(previous_db))
            migrate_db()
        except Exception as e:  # noqa: BLE001 — 回滚失败只能记日志，界面已不可用
            logger.error(f"回滚原数据库失败: {e}", exc_info=True)

    def _switch_runtime_database(self, new_config: AppConfig) -> bool:
        """运行期把数据库引擎切到新库；不切就会「配置指向新库、写入落旧库」。

        为什么不只改配置等重启：缩略图缓存目录会立刻跟到新根，
        `_apply_thumbnail_cache_dir` 也换掉清理服务的目标；此时若引擎还是旧库，
        扫描/删除写进旧库、缓存写进新目录，等于造出一批孤儿缓存。
        做法与 `/api/settings/data-root` 完全一致（先切库再初始化，失败自动回滚）。
        """
        from app.db.engine import DatabaseManager

        if not DatabaseManager.is_initialized():
            return True          # 启动期（库还没开）：下次启动自然用新位置
        from app.db.migrations import init_db, migrate_db

        previous_db = DatabaseManager.current_db_path()
        target_db = Path(new_config.db_path)
        try:
            DatabaseManager.switch_to(target_db)
            init_db(target_db)   # 新建的空根在这里建表；接管的库只做迁移
            migrate_db()
            logger.info(f"数据库引擎已切到: {target_db}")
            return True
        except Exception as e:  # noqa: BLE001 — 切库失败不能把界面留在坏状态
            logger.error(f"切换到新数据根的数据库失败: {e}", exc_info=True)
            # switch_to 只在"引擎建不起来"时自动回滚；这里是建表/迁移阶段失败，
            # 引擎可能已经指向一个打不开的库，必须手动切回来，否则界面全挂。
            self._restore_engine(previous_db)
            # 这里还没落盘配置（_apply_location 先切库后写配置），只需回滚引擎。
            # 提示由 _apply_location 统一弹，避免底层被测试/后台线程调用时挂住。
            self._last_switch_error = (
                f"新数据位置的数据库打不开：{e}\n\n"
                "已保留原来的数据位置。")
            return False

    def _persist(self, new_config: AppConfig) -> bool:
        error = app_paths.save_config_preserving_profile(new_config)
        if error:
            QMessageBox.critical(self, "写入配置失败", error)
            return False
        logger.info(f"数据位置已切换: {new_config.data_root}")
        return True


def choose_data_location(config: AppConfig, resolved=None, parent=None,
                         *, title: str = "选择数据位置",
                         message: str = "") -> Optional[AppConfig]:
    """弹出选择框；用户确认并成功切换时返回新的 AppConfig，否则 None。"""
    dialog = DataLocationDialog(config, resolved, parent, title=title, message=message)
    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.result_config
