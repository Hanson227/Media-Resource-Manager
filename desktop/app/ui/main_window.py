# -*- coding: utf-8 -*-
"""
主窗口 —— 应用主界面框架，GUI 信号总中枢。

连接所有组件：
- 左侧：FolderTreeView（文件夹树 + 右键菜单）
- 右侧：ThumbnailGridView（缩略图网格）
- 状态栏：进度、API 状态、扫描时间
- 工作线程：扫描、哈希、查重
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow, QMenu, QToolBar, QSplitter, QWidget, QVBoxLayout,
    QLabel, QMessageBox, QSystemTrayIcon, QApplication,
    QFileDialog, QPushButton, QLineEdit, QComboBox,
    QInputDialog,
)

from config import AppConfig
from app.db.engine import DatabaseManager
from app.db import queries as q
from app.ui.widgets.status_bar import MainStatusBar
from app.ui.left_panel.folder_tree import FolderTreeModel, FolderTreeView
from app.ui.right_panel.thumbnail_grid import (
    ThumbnailGridModel, ThumbnailGridView, FolderCardModel,
)
from app.ui.right_panel.thumbnail_delegate import ThumbnailDelegate
from app.ui.widgets.progress_panel import ProgressPanel
from app.ui.workers.scan_worker import ScanWorker
from app.ui.workers.hash_worker import HashWorker
from app.ui.workers.dedup_worker import DedupWorker
from app.utils.constants import MediaType
from app.utils.file_helpers import format_size

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """应用主窗口 —— GUI 信号总中枢。"""

    # ---- 信号 ----
    scan_requested = Signal(str)
    root_added = Signal(str)
    dedup_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._current_unit_id: Optional[int] = None
        self._last_sort_field: str = ""
        self._last_sort_asc: bool = True
        self._scan_worker: Optional[ScanWorker] = None
        self._hash_worker: Optional[HashWorker] = None
        self._dedup_worker: Optional[DedupWorker] = None
        self._current_expanded_unit_id: Optional[int] = None  # accordion: 当前展开文件层的单元 ID

        # 搜索防抖定时器 — 每次按键重置，150ms 空闲后触发放行
        self._filter_timer = QTimer()
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(150)
        self._filter_timer.timeout.connect(self._apply_current_filter)

        # 窗口基本属性
        self.setWindowTitle(config.window_title)
        self.resize(config.window_width, config.window_height)

        # 构建 UI 组件
        self._setup_menu_bar()
        self._setup_tool_bar()
        self._setup_central_area()
        self._setup_status_bar()
        self._setup_system_tray()

        # 核心：信号连线
        self._connect_signals()

        # 初始加载文件夹树
        self._tree_view.refresh_model()

        logger.info("主窗口初始化完成")

    # ============================================================
    # 菜单栏
    # ============================================================

    def _setup_menu_bar(self) -> None:
        menubar = self.menuBar()

        # 文件
        file_menu = menubar.addMenu("文件(&F)")
        add_root_action = QAction("添加媒体库(&A)...", self)
        add_root_action.setShortcut(QKeySequence("Ctrl+O"))
        add_root_action.setStatusTip("选择一个文件夹作为媒体库根目录")
        add_root_action.triggered.connect(self._on_add_root)
        file_menu.addAction(add_root_action)

        rescan_action = QAction("重新扫描(&R)", self)
        rescan_action.setShortcut(QKeySequence("F5"))
        rescan_action.setStatusTip("重新扫描所有媒体库")
        rescan_action.triggered.connect(self._on_refresh_all)
        file_menu.addAction(rescan_action)
        file_menu.addSeparator()

        exit_action = QAction("退出(&X)", self)
        exit_action.setShortcut(QKeySequence("Alt+F4"))
        exit_action.triggered.connect(self._on_exit)
        file_menu.addAction(exit_action)

        # 工具
        tools_menu = menubar.addMenu("工具(&T)")
        dedup_action = QAction("查重(&D)...", self)
        dedup_action.setShortcut(QKeySequence("Ctrl+D"))
        dedup_action.setStatusTip("对选中的资源单元执行查重比对")
        dedup_action.triggered.connect(self._on_start_dedup)
        tools_menu.addAction(dedup_action)

        tools_menu.addSeparator()

        excluded_action = QAction("管理已排除文件夹(&L)...", self)
        excluded_action.setStatusTip("查看和管理已被排除的文件夹列表")
        excluded_action.triggered.connect(self._on_manage_excluded)
        tools_menu.addAction(excluded_action)

        smb_action = QAction("SMB 共享(&S)...", self)
        smb_action.setStatusTip("设置局域网 SMB 文件共享，供手机访问媒体库")
        smb_action.triggered.connect(self._on_open_smb)
        tools_menu.addAction(smb_action)

        reset_db_action = QAction("重置数据库(&Z)...", self)
        reset_db_action.setStatusTip("删除所有扫描数据并重新初始化数据库")
        reset_db_action.triggered.connect(self._on_reset_db)
        tools_menu.addAction(reset_db_action)

        tools_menu.addSeparator()
        settings_action = QAction("设置(&E)...", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.triggered.connect(self._on_open_settings)
        tools_menu.addAction(settings_action)

        # 帮助
        help_menu = menubar.addMenu("帮助(&H)")
        messages_action = QAction("消息中心(&M)", self)
        messages_action.setShortcut(QKeySequence("Ctrl+M"))
        messages_action.triggered.connect(self._on_open_messages)
        help_menu.addAction(messages_action)
        help_menu.addSeparator()
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ============================================================
    # 工具栏
    # ============================================================

    def _setup_tool_bar(self) -> None:
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(24, 24))
        self.addToolBar(toolbar)

        add_btn = QPushButton("添加根目录")
        add_btn.clicked.connect(self._on_add_root)
        add_btn.setToolTip("添加媒体库根目录 (Ctrl+O)")
        toolbar.addWidget(add_btn)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._on_refresh_all)
        refresh_btn.setToolTip("重新扫描所有媒体库 (F5)")
        toolbar.addWidget(refresh_btn)

        dedup_btn = QPushButton("查重")
        dedup_btn.clicked.connect(self._on_start_dedup)
        dedup_btn.setToolTip("对选中的资源单元执行查重 (Ctrl+D)")
        toolbar.addWidget(dedup_btn)

        toolbar.addSeparator()

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("搜索文件名...")
        self._search_input.setMaximumWidth(180)
        self._search_input.textChanged.connect(self._on_search)
        toolbar.addWidget(self._search_input)

        self._filter_combo = QComboBox()
        self._filter_combo.addItem("全部", None)
        self._filter_combo.addItem("仅图片", MediaType.IMAGE.value)
        self._filter_combo.addItem("仅视频", MediaType.VIDEO.value)
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        self._filter_combo.setMaximumWidth(80)
        toolbar.addWidget(self._filter_combo)

        # 排序按钮
        self._sort_name_btn = QPushButton("名称↑")
        self._sort_name_btn.setCheckable(True)
        self._sort_name_btn.setToolTip("按文件名排序")
        self._sort_name_btn.clicked.connect(lambda: self._on_sort("name"))
        toolbar.addWidget(self._sort_name_btn)

        self._sort_size_btn = QPushButton("大小↑")
        self._sort_size_btn.setCheckable(True)
        self._sort_size_btn.setToolTip("按文件大小排序")
        self._sort_size_btn.clicked.connect(lambda: self._on_sort("size"))
        toolbar.addWidget(self._sort_size_btn)

        self._sort_date_btn = QPushButton("时间↑")
        self._sort_date_btn.setCheckable(True)
        self._sort_date_btn.setToolTip("按创建时间排序")
        self._sort_date_btn.clicked.connect(lambda: self._on_sort("date"))
        toolbar.addWidget(self._sort_date_btn)

        toolbar.addSeparator()

        self._msg_btn = QPushButton("消息")
        self._msg_btn.setToolTip("打开消息中心 (Ctrl+M)")
        self._msg_btn.clicked.connect(self._on_open_messages)
        toolbar.addWidget(self._msg_btn)

    # ============================================================
    # 中央区域：左树 + 右网格
    # ============================================================

    def _setup_central_area(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_pct = self._config.splitter_ratio_left
        total_w = self._config.window_width
        splitter.setSizes([int(total_w * left_pct / 100), int(total_w * (100 - left_pct) / 100)])

        # ---- 左侧：文件夹树 ----
        left_panel = QWidget()
        left_panel.setObjectName("leftPanel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 2, 4)
        left_layout.setSpacing(4)

        self._tree_model = FolderTreeModel(self._config)
        self._tree_view = FolderTreeView(self._tree_model)
        left_layout.addWidget(self._tree_view)

        splitter.addWidget(left_panel)

        # ---- 右侧：缩略图网格 ----
        right_panel = QWidget()
        right_panel.setObjectName("rightPanel")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(2, 4, 4, 4)
        right_layout.setSpacing(4)

        self._right_header = QLabel("资源单元视图")
        self._right_header.setObjectName("rightHeader")
        right_layout.addWidget(self._right_header)

        # 面包屑导航栏（返回按钮）
        self._breadcrumb = QPushButton("← 返回文件夹列表")
        self._breadcrumb.setObjectName("breadcrumb")
        self._breadcrumb.clicked.connect(self._on_breadcrumb_back)
        self._breadcrumb.hide()
        right_layout.addWidget(self._breadcrumb)

        self._grid_model = ThumbnailGridModel(self._config)
        self._grid_view = ThumbnailGridView(self._grid_model, self._config)
        right_layout.addWidget(self._grid_view)

        self._right_footer = QLabel("0 个项目 | 共 0 B")
        self._right_footer.setObjectName("rightFooter")
        right_layout.addWidget(self._right_footer)

        splitter.addWidget(right_panel)

        self._splitter = splitter
        self.setCentralWidget(splitter)

    # ============================================================
    # 状态栏
    # ============================================================

    def _setup_status_bar(self) -> None:
        self._status_bar = MainStatusBar(self._config)
        self.setStatusBar(self._status_bar)
        self._status_bar.set_api_status(True)

    # ============================================================
    # 系统托盘
    # ============================================================

    def _setup_system_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray_icon = QSystemTrayIcon(self)
        self._tray_icon.setToolTip(self._config.window_title)
        app_icon = self.windowIcon()
        if not app_icon.isNull():
            self._tray_icon.setIcon(app_icon)
        tray_menu = QMenu()
        show_a = QAction("显示主窗口", tray_menu)
        show_a.triggered.connect(self._on_tray_show)
        tray_menu.addAction(show_a)
        tray_menu.addSeparator()
        quit_a = QAction("退出", tray_menu)
        quit_a.triggered.connect(self._on_exit)
        tray_menu.addAction(quit_a)
        self._tray_icon.setContextMenu(tray_menu)
        self._tray_icon.activated.connect(self._on_tray_activated)
        self._tray_icon.show()

    # ============================================================
    # 信号连线（核心）
    # ============================================================

    def _connect_signals(self) -> None:
        """连接所有组件之间的信号。"""
        # ---- 主窗口信号 → 扫描 ----
        self.root_added.connect(self._start_scan)

        # ---- 文件树 → 缩略图网格 ----
        self._tree_view.unit_selected.connect(self._on_unit_selected)
        self._tree_view.unit_double_clicked.connect(self._on_unit_double_clicked)
        self._tree_view.folder_single_clicked.connect(self._on_folder_single_clicked)

        # ---- 网格文件夹卡片 → 进入单元 ----
        self._grid_view.folder_entered.connect(self._on_unit_double_clicked)
        self._grid_view.folder_selected.connect(self._tree_view.select_unit_silent)
        self._grid_view.back_requested.connect(self._on_breadcrumb_back)

        # ---- 空格键预览 ----
        self._grid_view.preview_requested.connect(self._on_show_preview)

        # ---- 网格右键 → 封面设置 ----
        self._grid_view.cover_from_file_requested.connect(self._on_set_cover_from_file)

        # ---- 文件级联动 ----
        self._tree_view.file_selected_from_tree.connect(self._grid_view.select_file_by_id)
        self._grid_view.file_selected_in_grid.connect(self._tree_view.select_tree_node_by_file_id)

        # ---- 右键菜单：合并/拆分 ----
        self._tree_view.merge_requested.connect(self._on_merge_units)
        self._tree_view.split_requested.connect(self._on_split_unit)
        self._tree_view.mark_requested.connect(self._on_mark_unit)
        self._tree_view.unmark_requested.connect(self._on_unmark_unit)
        self._tree_view.star_requested.connect(self._on_star_unit)
        self._tree_view.unstar_requested.connect(self._on_unstar_unit)
        self._tree_view.exclude_requested.connect(self._on_exclude_unit)
        self._tree_view.cover_requested.connect(self._on_set_cover)
        self._tree_view.clear_cover_requested.connect(self._on_clear_cover)
        self._tree_view.remove_root_requested.connect(self._on_remove_root)

        # ---- F2 重命名 / Ctrl+C 复制路径 ----
        self._tree_view.rename_requested.connect(self._on_rename_unit)
        self._tree_view.copy_path_requested.connect(self._on_copy_path)

        # ---- 刷新 ----
        self.refresh_requested.connect(self._on_refresh_all)

    # ============================================================
    # 扫描流程
    # ============================================================

    @Slot(str)
    def _start_scan(self, folder: str) -> None:
        """启动后台扫描线程。"""
        root_path = Path(folder)
        if not root_path.is_dir():
            QMessageBox.warning(self, "错误", f"目录不存在: {folder}")
            return

        self._status_bar.set_status(f"正在扫描: {root_path.name}...")
        self._status_bar.set_progress(0, 0)

        self._scan_worker = ScanWorker(self._config, root_path)
        self._scan_worker.progress.connect(self._status_bar.set_progress)
        self._scan_worker.unit_found.connect(
            lambda name, cnt: self._status_bar.set_status(f"发现: {name} ({cnt} 个文件)")
        )
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.error_occurred.connect(self._on_scan_error)
        self._scan_worker.start()

    @Slot(object)
    def _on_scan_finished(self, result) -> None:
        """扫描完成：刷新树，立即显示内容，后台启动哈希索引。"""
        self._status_bar.set_status(f"扫描完成: {result.total_files} 个文件")
        self._status_bar.hide_progress()
        self._status_bar.record_scan_time()

        # 刷新文件夹树
        self._tree_view.refresh_model()

        # 自动选中第一个根目录，使内容立即可见
        root_idx = self._tree_view.model().index(0, 0)
        if root_idx.isValid():
            self._tree_view.setCurrentIndex(root_idx)

        # 后台启动哈希索引（不阻塞 UI）
        self._status_bar.set_status("正在后台计算文件哈希与人脸索引...")
        self._status_bar.set_progress(0, 0)

        self._hash_worker = HashWorker(self._config)
        self._hash_worker.progress.connect(self._status_bar.set_progress)
        self._hash_worker.finished.connect(self._on_hash_finished)
        self._hash_worker.error_occurred.connect(
            lambda e: self._status_bar.set_status(f"哈希错误: {e}")
        )
        self._hash_worker.start()

    @Slot(int)
    def _on_hash_finished(self, count: int) -> None:
        """哈希索引完成。"""
        self._status_bar.set_status(f"索引完成: {count} 个文件")
        self._status_bar.hide_progress()
        self._tree_view.refresh_model()

    @Slot(str)
    def _on_scan_error(self, msg: str) -> None:
        QMessageBox.critical(self, "扫描错误", msg)
        self._status_bar.set_status("扫描失败")
        self._status_bar.hide_progress()

    # ============================================================
    # 文件夹树交互
    # ============================================================

    @Slot(int)
    def _on_unit_double_clicked(self, unit_id: int) -> None:
        """双击进入文件夹：手风琴展开 + 加载文件 + 自动选中首文件。"""
        # ---- Accordion: 同一根下之前的展开单元自动收起 ----
        if (self._current_expanded_unit_id is not None
                and self._current_expanded_unit_id != unit_id):
            model_ref = self._tree_view.model()
            prev_node = model_ref.get_node_by_unit_id(self._current_expanded_unit_id)
            new_node = model_ref.get_node_by_unit_id(unit_id)
            if (prev_node and new_node
                    and prev_node.library_root_id == new_node.library_root_id):
                try:
                    model_ref.collapse_unit(self._current_expanded_unit_id)
                except Exception:
                    pass

        # ---- 加载文件 ----
        self._current_unit_id = unit_id
        self._grid_view.load_unit(unit_id)
        self._breadcrumb.show()

        # ---- 展开树文件子节点（不 expandAll） ----
        try:
            with DatabaseManager.session() as session:
                files = q.get_files_by_unit(session, unit_id)
                file_dicts = [
                    {"id": f.id, "filename": f.filename,
                     "path": f.path, "size_bytes": f.size_bytes}
                    for f in files
                ]
            tree_model = self._tree_view.model()
            tree_model.expand_unit(unit_id, file_dicts)
            # 找到单元节点并展开
            for root_row, root in enumerate(tree_model._roots):
                root_idx = tree_model.index(root_row, 0)
                for child_row, child in enumerate(root.children):
                    if child.node_id == unit_id and child.node_type == "unit" and child.node_subtype != "file":
                        unit_idx = tree_model.index(child_row, 0, root_idx)
                        self._tree_view.expand(unit_idx)
                        break
        except Exception as e:
            logger.error(f"展开单元文件树失败: {e}")

        # ---- 自动选中第一个文件 ----
        if self._grid_model.file_list:
            first_id = self._grid_model.file_list[0]["id"]
            # 50ms 延迟确保 load_unit 后的模型切换完成布局
            QTimer.singleShot(50, lambda fid=first_id: self._grid_view.select_file_by_id(fid))
            QTimer.singleShot(50, lambda fid=first_id: self._tree_view.select_tree_node_by_file_id(fid))

        # ---- 更新 accordion 状态 ----
        self._current_expanded_unit_id = unit_id

        # ---- 更新头/脚信息 ----
        try:
            with DatabaseManager.session() as session:
                unit = q.get_unit_by_id(session, unit_id)
                if unit:
                    self._right_header.setText(f"资源单元: {unit.name}")
                    self._right_footer.setText(
                        f"{unit.file_count} 个项目 | 共 {format_size(unit.total_size)}"
                    )
        except Exception as e:
            logger.error(f"加载单元详情失败: {e}")

    @Slot(int)
    def _on_folder_single_clicked(self, unit_id: int) -> None:
        """树中单击文件夹 → 显示文件夹卡片并滚动到该卡片。"""
        self._breadcrumb.hide()

        # 收起之前展开的文件层（accordion）
        if self._current_expanded_unit_id is not None:
            try:
                self._tree_view.model().collapse_unit(self._current_expanded_unit_id)
            except Exception:
                pass
        self._current_expanded_unit_id = None
        self._current_unit_id = None

        # 找到包含该文件夹的根，显示其所有文件夹卡片
        model = self._tree_view.model()
        root_unit_ids = None
        for row in range(model.rowCount()):
            root_idx = model.index(row, 0)
            ids = model.get_selected_units(root_idx)
            if unit_id in ids:
                root_unit_ids = ids
                break

        if root_unit_ids:
            self._show_folder_cards(root_unit_ids)
            self._grid_view.select_folder_card_by_unit_id(unit_id)
        else:
            self._show_folder_cards([unit_id])
            self._grid_view.select_folder_card_by_unit_id(unit_id)

    @Slot()
    def _on_breadcrumb_back(self) -> None:
        """点击面包屑 → 返回当前单元所属媒体库的文件夹卡片。"""
        if not self._breadcrumb.isVisible():
            return
        self._breadcrumb.hide()
        model = self._tree_view.model()

        # 收起树的文件子节点，并展开根层级显示所有单元
        unit_id = self._current_unit_id
        if unit_id:
            try:
                model.collapse_unit(unit_id)
                QTimer.singleShot(0, lambda: self._tree_view.expandToDepth(1))
            except Exception as e:
                logger.error(f"收起单元文件树失败: {e}")
            finally:
                self._current_unit_id = None
                self._current_expanded_unit_id = None

        # 遍历所有根，找到包含当前单元的根
        for row in range(model.rowCount()):
            root_idx = model.index(row, 0)
            unit_ids = model.get_selected_units(root_idx)
            if unit_id and unit_id in unit_ids:
                # 阻塞信号避免 setCurrentIndex 触发 unit_selected 导致重复加载
                sel = self._tree_view.selectionModel()
                if sel:
                    sel.blockSignals(True)
                try:
                    self._tree_view.setCurrentIndex(root_idx)
                finally:
                    if sel:
                        sel.blockSignals(False)
                self._show_folder_cards(unit_ids)
                return
        # 回退：选第一个根
        root_idx = model.index(0, 0)
        if root_idx.isValid():
            sel = self._tree_view.selectionModel()
            if sel:
                sel.blockSignals(True)
            try:
                self._tree_view.setCurrentIndex(root_idx)
            finally:
                if sel:
                    sel.blockSignals(False)
            # 补偿信号阻塞：手动显示文件夹卡片
            unit_ids = model.get_selected_units(root_idx)
            if unit_ids:
                self._show_folder_cards(unit_ids)

    @Slot(list)
    def _on_unit_selected(self, unit_ids: list[int]) -> None:
        """根/收藏节点单击 → 显示文件夹卡片。"""
        if not unit_ids:
            return

        self._breadcrumb.hide()
        if self._current_expanded_unit_id is not None:
            try:
                self._tree_view.model().collapse_unit(self._current_expanded_unit_id)
            except Exception:
                pass
        self._current_expanded_unit_id = None
        self._current_unit_id = None

        self._show_folder_cards(unit_ids)

    @Slot(int, list)
    def _on_merge_units(self, parent_id: int, child_ids: list[int]) -> None:
        """合并资源单元。"""
        try:
            with DatabaseManager.session() as session:
                parent = q.get_unit_by_id(session, parent_id)
                for cid in child_ids:
                    q.mark_unit_merged(session, cid, parent_id)
                # 重新统计父单元
                total_files = 0
                total_size = 0
                all_units = [parent_id] + child_ids
                for uid in all_units:
                    files = q.get_files_by_unit(session, uid)
                    total_files += len(files)
                    total_size += sum(f.size_bytes for f in files)
                q.update_unit_stats(session, parent_id, total_files, total_size)
            self._tree_view.refresh_model()
            self._status_bar.set_status(f"已合并 {len(child_ids)} 个单元")
        except Exception as e:
            QMessageBox.critical(self, "合并失败", str(e))

    @Slot(int)
    def _on_split_unit(self, unit_id: int) -> None:
        """拆分资源单元。"""
        try:
            with DatabaseManager.session() as session:
                children = q.get_child_units(session, unit_id)
                for child in children:
                    q.unmerge_unit(session, child.id)
                q.unmerge_unit(session, unit_id)
            self._tree_view.refresh_model()
            self._status_bar.set_status("已拆分")
        except Exception as e:
            QMessageBox.critical(self, "拆分失败", str(e))

    @Slot(str)
    def _on_mark_unit(self, folder_path: str) -> None:
        """标记为资源单元。"""
        try:
            with DatabaseManager.session() as session:
                unit = q.get_unit_by_path(session, folder_path)
                if unit:
                    q.mark_unit_manual(session, unit.id, True)
                    q.set_unit_starred(session, unit.id, True)
                else:
                    # 创建新的手动单元
                    p = Path(folder_path)
                    roots = q.get_all_roots(session)
                    root_id = roots[0].id if roots else None
                    if root_id:
                        q.create_unit(session, str(p), p.name, root_id,
                                     is_manual=True, file_count=0, total_size=0)
            self._tree_view.refresh_model()
            self._status_bar.set_status(f"已标记: {Path(folder_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "标记失败", str(e))

    @Slot(int)
    def _on_unmark_unit(self, unit_id: int) -> None:
        """取消标记。"""
        try:
            with DatabaseManager.session() as session:
                q.mark_unit_manual(session, unit_id, False)
                q.set_unit_starred(session, unit_id, False)
            self._tree_view.refresh_model()
            self._status_bar.set_status("已取消标记")
        except Exception as e:
            QMessageBox.critical(self, "取消标记失败", str(e))

    @Slot(int)
    def _on_star_unit(self, unit_id: int) -> None:
        """添加到收藏。"""
        try:
            with DatabaseManager.session() as session:
                unit = q.get_unit_by_id(session, unit_id)
                name = unit.name if unit else str(unit_id)
                q.set_unit_starred(session, unit_id, True)
            self._tree_view.refresh_model()
            self._status_bar.set_status(f"已收藏: {name}")
        except Exception as e:
            QMessageBox.critical(self, "收藏失败", str(e))

    @Slot(int)
    def _on_unstar_unit(self, unit_id: int) -> None:
        """取消收藏。"""
        try:
            with DatabaseManager.session() as session:
                unit = q.get_unit_by_id(session, unit_id)
                name = unit.name if unit else str(unit_id)
                q.set_unit_starred(session, unit_id, False)
            self._tree_view.refresh_model()
            self._status_bar.set_status(f"已取消收藏: {name}")
        except Exception as e:
            QMessageBox.critical(self, "取消收藏失败", str(e))

    @Slot(int)
    def _on_rename_unit(self, unit_id: int) -> None:
        """重命名资源单元。"""
        try:
            with DatabaseManager.session() as session:
                unit = q.get_unit_by_id(session, unit_id)
                if not unit:
                    return
                old_name = unit.name
        except Exception as e:
            logger.error(f"获取单元信息失败: {e}")
            return

        new_name, ok = QInputDialog.getText(
            self, "重命名", "新名称:", text=old_name,
        )
        if not ok or not new_name.strip() or new_name.strip() == old_name:
            return

        try:
            with DatabaseManager.session() as session:
                q.rename_unit(session, unit_id, new_name.strip())
            self._tree_view.refresh_model()
            self._status_bar.set_status(f"已重命名: {old_name} → {new_name.strip()}")
        except Exception as e:
            QMessageBox.critical(self, "重命名失败", str(e))

    @Slot(str)
    def _on_copy_path(self, path: str) -> None:
        """复制文件夹路径到剪贴板。"""
        clipboard = QApplication.clipboard()
        clipboard.setText(path)
        self._status_bar.set_status(f"已复制路径: {path}")

    @Slot(int)
    def _on_exclude_unit(self, unit_id: int) -> None:
        """排除资源单元：标记为 excluded。"""
        if unit_id <= 0:
            logger.error(f"排除失败：无效的单元 ID {unit_id}")
            QMessageBox.critical(self, "排除失败", f"无效的单元 ID：{unit_id}")
            return
        try:
            with DatabaseManager.session() as session:
                unit = q.get_unit_by_id(session, unit_id)
                if unit is None:
                    logger.error(f"排除失败：找不到单元 {unit_id}")
                    QMessageBox.critical(self, "排除失败", f"找不到资源单元 (ID={unit_id})")
                    return
                name = unit.name
                unit.status = "excluded"
                unit.updated_at = datetime.now()
                session.flush()
                logger.info(f"已标记排除: {name} (id={unit_id}, status={unit.status})")
            self._grid_view.clear()
            self._breadcrumb.hide()
            self._current_unit_id = None
            self._current_expanded_unit_id = None
            self._tree_view.refresh_model()
            root_idx = self._tree_view.model().index(0, 0)
            if root_idx.isValid():
                self._tree_view.setCurrentIndex(root_idx)
            self._status_bar.set_status(f"已排除: {name}")
        except Exception as e:
            logger.error(f"排除失败: {e}")
            QMessageBox.critical(self, "排除失败", str(e))

    @Slot(int)
    def _on_set_cover(self, unit_id: int) -> None:
        """为资源单元设置封面图片。"""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "选择封面图片", "",
            "媒体文件 (*.jpg *.jpeg *.png *.bmp *.webp *.mp4 *.mkv *.avi *.mov);;所有文件 (*)",
        )
        if not path:
            return
        self._apply_cover(unit_id, path)

    @Slot(str)
    def _on_set_cover_from_file(self, file_path: str) -> None:
        """从网格右键直接将某图片设为当前单元的封面。"""
        if self._current_unit_id is None:
            return
        self._apply_cover(self._current_unit_id, file_path)

    def _apply_cover(self, unit_id: int, cover_path: str) -> None:
        """通用：将指定路径设为单元的封面。"""
        try:
            with DatabaseManager.session() as session:
                # 先查旧封面路径，用于失效缓存
                old = q.get_unit_by_id(session, unit_id)
                old_cover = old.cover_path if old else None
                q.set_unit_cover(session, unit_id, cover_path)

            # 封面文件变更时，使旧封面的缩略图缓存失效
            if old_cover and old_cover != cover_path:
                from app.services.cleanup_service import CleanupService
                from app.db.models import MediaFile
                with DatabaseManager.session() as session:
                    old_file = session.query(MediaFile).filter(
                        MediaFile.path == old_cover,
                        MediaFile.resource_unit_id == unit_id,
                    ).first()
                    if old_file:
                        CleanupService.invalidate_thumbnail(old_file.id)

            self._tree_view.refresh_model()
            self._status_bar.set_status(f"封面已设置")
        except Exception as e:
            QMessageBox.critical(self, "设置封面失败", str(e))

    @Slot(int)
    def _on_clear_cover(self, unit_id: int) -> None:
        """清除资源单元的手动封面。"""
        try:
            with DatabaseManager.session() as session:
                q.clear_unit_cover(session, unit_id)
            self._tree_view.refresh_model()
            self._status_bar.set_status("封面已清除")
        except Exception as e:
            QMessageBox.critical(self, "清除封面失败", str(e))

    @Slot(int)
    def _on_remove_root(self, root_id: int) -> None:
        """删除媒体库根目录及其所有数据。"""
        reply = QMessageBox.warning(
            self, "确认删除",
            "此操作将删除该媒体库根目录下的所有资源单元和文件记录。\n\n确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            self._grid_view.clear()
            self._current_unit_id = None
            self._current_expanded_unit_id = None
            # 先清理缓存，再删数据库
            from app.services.cleanup_service import CleanupService
            CleanupService.remove_root_thumbnails(root_id)
            with DatabaseManager.session() as session:
                q.remove_library_root(session, root_id)
            self._tree_view.refresh_model()
            self._right_header.setText("资源单元视图")
            self._right_footer.setText("0 个项目 | 共 0 B")
            self._status_bar.set_status("媒体库已删除")
        except Exception as e:
            QMessageBox.critical(self, "删除失败", str(e))

    # ============================================================
    # 查重流程
    # ============================================================

    @Slot()
    def _on_start_dedup(self) -> None:
        """启动查重。先检查哈希是否已完成。"""
        try:
            with DatabaseManager.session() as session:
                units = q.get_all_active_units(session)
                unit_ids = [u.id for u in units]
                # 检查未索引文件数量
                unindexed = q.get_unindexed_files(session, limit=1)
                pending_count = len(unindexed)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"无法获取资源单元: {e}")
            return

        if len(unit_ids) < 2:
            QMessageBox.information(self, "提示", "至少需要 2 个资源单元才能执行查重，请先扫描媒体库。")
            return

        if pending_count > 0:
            reply = QMessageBox.question(
                self, "哈希未完成",
                f"还有文件的哈希值未计算（至少 {pending_count} 个），这会导致查重漏检。\n\n"
                "建议等后台哈希索引完成后（状态栏不再显示进度）再查重。\n\n"
                "是否仍要继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        reply = QMessageBox.question(
            self, "确认查重",
            f"将对 {len(unit_ids)} 个资源单元执行全量比对。\n"
            f"策略：人脸识别 → MD5 → pHash → dHash\n\n"
            f"确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self._status_bar.set_status("正在查重...")
        self._status_bar.set_progress(0, 0)

        self._dedup_worker = DedupWorker(self._config, unit_ids)
        self._dedup_worker.progress.connect(self._status_bar.set_progress)
        self._dedup_worker.duplicate_found.connect(self._on_duplicate_found)
        self._dedup_worker.finished.connect(self._on_dedup_finished)
        self._dedup_worker.error_occurred.connect(
            lambda e: self._status_bar.set_status(f"查重错误: {e}")
        )
        self._dedup_worker.start()

    @Slot(object)
    def _on_duplicate_found(self, dup) -> None:
        """发现重复单元时弹出系统通知。"""
        self.show_notification(
            "发现重复单元",
            f"{dup.unit_a_name} ⟷ {dup.unit_b_name}\n"
            f"相似度: {dup.jaccard_similarity * 100:.1f}%"
        )

    @Slot(object)
    def _on_dedup_finished(self, session) -> None:
        """查重完成。"""
        dup_count = len(session.duplicates_found)
        self._status_bar.set_status(
            f"查重完成: {session.total_units_compared} 个单元, 发现 {dup_count} 组重复"
        )
        self._status_bar.hide_progress()

        if dup_count > 0:
            # 打开第一个重复结果的对比对话框
            from app.ui.dialogs.dedup_compare import DedupCompareDialog
            first_dup = session.duplicates_found[0]
            dialog = DedupCompareDialog(first_dup, self._config, self)
            dialog.keep_a_requested.connect(
                lambda rid: self._resolve_dedup(rid, "keep_a")
            )
            dialog.keep_b_requested.connect(
                lambda rid: self._resolve_dedup(rid, "keep_b")
            )
            dialog.whitelist_requested.connect(
                lambda rid: self._resolve_dedup(rid, "whitelist")
            )
            dialog.ignore_requested.connect(
                lambda rid: self._resolve_dedup(rid, "ignore")
            )
            dialog.exec()
        else:
            QMessageBox.information(self, "查重完成", "未发现重复的资源单元。")

    def _resolve_dedup(self, result_id: int, resolution: str) -> None:
        """处理查重结果。"""
        try:
            with DatabaseManager.session() as session:
                q.resolve_dedup(session, result_id, resolution)
            self._status_bar.set_status(f"已处理: {resolution}")
        except Exception as e:
            logger.error(f"处理查重结果失败: {e}")

    # ============================================================
    # 快速预览
    # ============================================================

    @Slot(int, str, str)
    def _on_show_preview(self, file_id: int, file_path: str, media_type: str) -> None:
        """打开 Quick Look 预览对话框。"""
        from app.ui.dialogs.preview_dialog import QuickLookPreviewDialog
        from app.utils.file_helpers import format_size

        # 从当前网格模型收集文件列表
        model = self._grid_view.model()
        file_list = []
        current_index = -1
        for row in range(model.rowCount()):
            idx = model.index(row, 0)
            fid = model.data(idx, Qt.ItemDataRole.UserRole + 1)
            fp = model.data(idx, Qt.ItemDataRole.UserRole)
            mt = model.data(idx, Qt.ItemDataRole.UserRole + 2)
            fn = model.data(idx, Qt.ItemDataRole.DisplayRole)
            fs = model.data(idx, Qt.ItemDataRole.UserRole + 3)
            if fp and mt:
                item = {
                    "path": fp, "filename": fn, "media_type": mt,
                    "file_id": fid, "size_formatted": fs,
                }
                if fid == file_id:
                    current_index = len(file_list)
                file_list.append(item)

        if not file_list:
            return
        if current_index < 0:
            current_index = 0

        dlg = QuickLookPreviewDialog(
            file_list, current_index,
            seek_step_sec=self._config.preview_seek_step,
            parent=self,
        )
        screen = self.screen().availableGeometry()
        dlg.resize(int(screen.width() * 0.75), int(screen.height() * 0.75))
        dlg.move(screen.center() - dlg.rect().center())
        dlg.exec()

    # ============================================================
    # 对话框
    # ============================================================

    @Slot()
    def _on_open_messages(self) -> None:
        from app.ui.dialogs.message_center import MessageCenterDialog
        dlg = MessageCenterDialog(self)
        dlg.messages_updated.connect(self._update_message_badge)
        dlg.exec()

    @Slot()
    def _on_manage_excluded(self) -> None:
        from app.ui.dialogs.excluded_folders import ExcludedFoldersDialog
        dlg = ExcludedFoldersDialog(self)
        dlg.excluded_changed.connect(self._tree_view.refresh_model)
        dlg.exec()

    @Slot()
    def _on_open_settings(self) -> None:
        from app.ui.dialogs.settings import SettingsDialog
        dlg = SettingsDialog(self._config, self)
        dlg.settings_saved.connect(self._on_settings_saved)
        dlg.exec()

    @Slot()
    def _on_open_smb(self) -> None:
        """打开 SMB 共享管理对话框。"""
        from app.ui.dialogs.smb_share import SMBDialog
        dlg = SMBDialog(self._config, self)
        dlg.exec()

    @Slot()
    def _on_reset_db(self) -> None:
        """重置数据库：确认后删除所有数据并重建。"""
        reply = QMessageBox.warning(
            self, "确认重置数据库",
            "此操作将删除所有扫描数据、查重结果和消息记录。\n\n"
            "重置后需要重新添加媒体库并扫描。\n\n"
            "确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            from app.db.migrations import reset_db
            # 先停止后台 worker
            if self._scan_worker and self._scan_worker.isRunning():
                self._scan_worker.cancel()
                self._scan_worker.wait(3000)
            if self._hash_worker and self._hash_worker.isRunning():
                self._hash_worker.cancel()
                self._hash_worker.wait(3000)

            reset_db(self._config.db_path)
            self._current_unit_id = None
            self._current_expanded_unit_id = None
            self._tree_view.refresh_model()
            self._grid_view.load_unit(0)  # 清空网格
            self._right_header.setText("资源单元视图")
            self._right_footer.setText("0 个项目 | 共 0 B")
            self._status_bar.set_status("数据库已重置，请重新添加媒体库并扫描")
            QMessageBox.information(self, "完成", "数据库已重置。\n请重新添加媒体库根目录并执行扫描。")
        except Exception as e:
            QMessageBox.critical(self, "重置失败", f"数据库重置失败:\n{e}")
            logger.error(f"重置数据库失败: {e}")

    @Slot(object)
    def _on_settings_saved(self, new_config: AppConfig) -> None:
        self._config = new_config
        self._status_bar.set_status("设置已保存")

    def _update_message_badge(self) -> None:
        from app.services.message_center import MessageCenter
        count = MessageCenter.get_unread_count()
        self._msg_btn.setText(f"消息 ({count})" if count > 0 else "消息")

    # ============================================================
    # 搜索与筛选
    # ============================================================

    def _show_folder_cards(self, unit_ids: list[int]) -> None:
        """显示文件夹卡片：将指定 unit_ids 的文件夹显示为缩略图卡片。"""
        try:
            with DatabaseManager.session() as session:
                unit_data = []
                total_files = 0
                total_size = 0
                for uid in unit_ids:
                    u = q.get_unit_by_id(session, uid)
                    if not u:
                        continue
                    # 优先使用手动设置的封面
                    if u.cover_path:
                        preview_path = u.cover_path
                    else:
                        files = q.get_files_by_unit(session, uid)
                        preview_path = files[0].path if files else ""
                    unit_data.append({
                        "unit_id": u.id,
                        "name": u.name,
                        "path": u.path,
                        "cover_path": u.cover_path or "",
                        "file_count": u.file_count or 0,
                        "total_size": u.total_size or 0,
                        "preview_path": preview_path,
                        "created_at": u.created_at.isoformat() if u.created_at else None,
                    })
                    total_files += u.file_count or 0
                    total_size += u.total_size or 0
            if unit_data:
                self._grid_view.load_folder_cards(unit_data, self._config)
                self._right_header.setText(f"文件夹 ({len(unit_data)} 个片段)")
                self._right_footer.setText(
                    f"{total_files} 个项目 | 共 {format_size(total_size)}"
                )
        except Exception as e:
            logger.error(f"加载文件夹卡片失败: {e}")

    @Slot(str)
    def _on_search(self, text: str) -> None:
        self._filter_timer.start()

    @Slot(int)
    def _on_filter_changed(self, index: int) -> None:
        self._apply_current_filter()

    def _on_sort(self, field: str) -> None:
        """切换排序。再次点击同字段切换升降序。"""
        current = self._grid_view.model()
        if isinstance(current, FolderCardModel):
            # 文件夹卡片模式
            if current._data:
                asc = True
                if self._last_sort_field == field:
                    asc = not self._last_sort_asc
                current.set_sort(field, asc)
                self._last_sort_field = field
                self._last_sort_asc = asc
            sort_label = {"name": "名称", "size": "大小", "date": "时间"}.get(field, field)
            arrow = "↑" if self._last_sort_asc else "↓"
            self._status_bar.set_status(f"排序: {sort_label}{arrow}")
            self._update_sort_buttons()
            return

        # 文件模式
        model = self._grid_model
        if model.sort_field == field:
            model.set_sort(field, not model._sort_asc)
        else:
            model.set_sort(field, True)
        sort_label = {"name": "名称", "size": "大小", "date": "时间"}.get(field, field)
        arrow = "↑" if model._sort_asc else "↓"
        self._status_bar.set_status(f"排序: {sort_label}{arrow}")
        self._update_sort_buttons()

    def _update_sort_buttons(self) -> None:
        """同步排序按钮的选中状态和图标。"""
        current = self._grid_view.model()
        if isinstance(current, FolderCardModel):
            field = self._last_sort_field
            asc = self._last_sort_asc
        else:
            field = self._grid_model.sort_field
            asc = self._grid_model._sort_asc
        self._sort_name_btn.blockSignals(True)
        self._sort_size_btn.blockSignals(True)
        self._sort_date_btn.blockSignals(True)
        self._sort_name_btn.setChecked(field == "name")
        self._sort_size_btn.setChecked(field == "size")
        self._sort_date_btn.setChecked(field == "date")
        self._sort_name_btn.blockSignals(False)
        self._sort_size_btn.blockSignals(False)
        self._sort_date_btn.blockSignals(False)
        arrow = "↑" if asc else "↓"
        self._sort_name_btn.setText(f"名称{arrow if field == 'name' else '↑'}")
        self._sort_size_btn.setText(f"大小{arrow if field == 'size' else '↑'}")
        self._sort_date_btn.setText(f"时间{arrow if field == 'date' else '↑'}")

    def _apply_current_filter(self) -> None:
        """读取搜索框和筛选下拉的当前值，应用到网格+文件夹树。"""
        media_data = self._filter_combo.currentData()
        search_text = self._search_input.text()
        self._grid_model.apply_filter(
            search_text=search_text,
            media_filter=media_data if media_data else "",
        )
        # 搜索文件夹树：按名称过滤
        self._tree_view.filter_by_name(search_text)
        # 搜索时自动选中第一个可见单元
        if search_text.strip():
            first = self._tree_view.find_first_visible_unit()
            if first:
                self._on_unit_selected([first])
                self._tree_view.select_unit_silent(first)

    # ============================================================
    # 刷新
    # ============================================================

    @Slot()
    def _on_refresh_all(self) -> None:
        """刷新全部：重新扫描所有媒体库根目录。"""
        # 先停止所有后台线程
        self._cancel_all_workers()

        try:
            with DatabaseManager.session() as session:
                roots = q.get_all_roots(session)
                paths = [r.path for r in roots]
        except Exception as e:
            logger.error(f"获取根目录失败: {e}")
            return

        if not paths:
            QMessageBox.information(self, "提示", "请先添加媒体库根目录。")
            return

        if len(paths) == 1:
            self._start_scan(paths[0])
        else:
            # 多根目录：逐个串行扫描，避免 worker 覆盖
            self._start_scan(paths[0])
            for path in paths[1:]:
                if self._scan_worker:
                    self._scan_worker.finished.connect(
                        lambda *args, _p=path: self._start_scan(_p),
                        Qt.ConnectionType.SingleShotConnection,
                    )

    def _cancel_all_workers(self) -> None:
        """安全停止所有后台工作线程。"""
        # 先解除引用，确保 worker 信号不会访问过期指针
        workers = [self._scan_worker, self._hash_worker, self._dedup_worker]
        self._scan_worker = self._hash_worker = self._dedup_worker = None

        for w in workers:
            if w and w.isRunning():
                if hasattr(w, 'cancel'):
                    w.cancel()
                if not w.wait(10000):
                    logger.warning(f"Worker {type(w).__name__} 未在 10s 内停止，强制终止")
                    w.terminate()
                    w.wait()
        self._grid_view._cancel_all_workers()

    # ============================================================
    # 键盘快捷键
    # ============================================================

    def keyPressEvent(self, event) -> None:
        """全局键盘快捷键。"""
        mod = event.modifiers()
        key = event.key()

        # Alt+↑ / Backspace → 返回上一级
        if key == Qt.Key.Key_Backspace or (key == Qt.Key.Key_Up and mod & Qt.KeyboardModifier.AltModifier):
            if self._breadcrumb.isVisible():
                self._on_breadcrumb_back()
                event.accept()
                return
        # F5 → 刷新
        elif key == Qt.Key.Key_F5:
            self._on_refresh_all()
            event.accept()
            return
        super().keyPressEvent(event)

    # ============================================================
    # 通知
    # ============================================================

    def show_notification(self, title: str, message: str) -> None:
        if hasattr(self, '_tray_icon') and self._tray_icon.supportsMessages():
            self._tray_icon.showMessage(
                title, message,
                QSystemTrayIcon.MessageIcon.Information, 5000,
            )

    # ============================================================
    # 插槽
    # ============================================================

    @Slot()
    def _on_add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择媒体库根目录", str(Path.home()))
        if folder:
            self.root_added.emit(folder)
            self._status_bar.set_status(f"已添加: {folder}")

    @Slot()
    def _on_exit(self) -> None:
        if hasattr(self, '_tray_icon'):
            self._tray_icon.hide()
        QApplication.quit()

    @Slot()
    def _on_tray_show(self) -> None:
        self.show()
        self.activateWindow()

    @Slot()
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._on_tray_show()

    @Slot()
    def _show_about(self) -> None:
        QMessageBox.about(
            self, "关于 影视资源管理器",
            "影视资源管理器 v0.1.0\n\n"
            "纯本地运行的 Windows 桌面应用，"
            "用于管理电脑上的影视资源文件夹。\n\n"
            "核心功能：\n"
            "• 资源单元自动识别与管理\n"
            "• 智能文件查重（MD5 / 感知哈希 / 人脸识别）\n"
            "• 实时文件监控\n"
            "• 局域网 SMB 共享\n"
            "• 安卓端 API 预留",
        )
