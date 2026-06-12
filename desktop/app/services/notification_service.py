# -*- coding: utf-8 -*-
"""
通知服务 —— 系统托盘图标和气泡通知。

使用 pystray 管理托盘图标，QSystemTrayIcon 作为 PySide6 备选方案。
在发现重复文件或完成扫描等事件时弹出提醒。
"""

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from app.services.message_center import MessageCenter

logger = logging.getLogger(__name__)


class NotificationService:
    """系统通知服务。

    管理系统托盘图标、气泡通知和 Toast 消息。
    优先使用 pystray，若不可用则回退到内置方案。

    用法:
        notifier = NotificationService(on_show=show_main_window)
        notifier.start()
        notifier.notify("发现重复文件", "影片A 与 影片B 存在重复")
    """

    def __init__(
        self,
        app_name: str = "影视资源管理器",
        on_show: Optional[Callable[[], None]] = None,
        on_exit: Optional[Callable[[], None]] = None,
    ) -> None:
        """初始化通知服务。

        参数:
            app_name: 应用名称（显示在托盘提示中）。
            on_show: 双击托盘图标时的回调（显示主窗口）。
            on_exit: 点击退出时的回调。
        """
        self._app_name = app_name
        self._on_show = on_show
        self._on_exit = on_exit
        self._tray_icon = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """启动系统托盘图标（在后台线程中运行）。"""
        if self._running:
            return

        self._running = True
        # 优先尝试 pystray
        self._thread = threading.Thread(target=self._run_pystray, daemon=True)
        self._thread.start()
        logger.info("系统托盘通知服务已启动")

    def stop(self) -> None:
        """停止系统托盘图标。"""
        self._running = False
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None
        logger.info("系统托盘通知服务已停止")

    def notify(self, title: str, message: str, timeout: int = 5) -> None:
        """弹出气泡通知。

        参数:
            title: 通知标题。
            message: 通知正文。
            timeout: 显示时长（秒）。
        """
        if self._tray_icon and hasattr(self._tray_icon, 'notify'):
            try:
                self._tray_icon.notify(title, message)
            except Exception:
                self._fallback_notify(title, message)
        else:
            self._fallback_notify(title, message)

        # 同时写入消息中心
        MessageCenter.create_info(title, message)

    def notify_dedup(
        self, unit_a_name: str, unit_b_name: str,
        similarity: float, match_count: int, dedup_result_id: int,
    ) -> None:
        """弹出查重提醒并写入消息中心。

        参数:
            unit_a_name: 资源单元 A 名称。
            unit_b_name: 资源单元 B 名称。
            similarity: 相似度。
            match_count: 匹配数。
            dedup_result_id: 查重结果 ID。
        """
        title = f"发现重复单元: {unit_a_name} ⟷ {unit_b_name}"
        message = (
            f"相似度 {similarity * 100:.1f}%，"
            f"共 {match_count} 对匹配文件。点击查看详情。"
        )
        # 仅显示气泡通知，不通过 self.notify() 创建 info 消息（避免重复）
        if self._tray_icon and hasattr(self._tray_icon, 'notify'):
            try:
                self._tray_icon.notify(message, title)
            except Exception:
                self._fallback_notify(title, message)
        else:
            self._fallback_notify(title, message)
        # 创建持久化消息
        MessageCenter.create_dedup_alert(
            unit_a_name=unit_a_name,
            unit_b_name=unit_b_name,
            similarity=similarity,
            match_count=match_count,
            dedup_result_id=dedup_result_id,
        )

    def update_tooltip(self, text: str) -> None:
        """更新系统托盘悬停提示文字。"""
        if self._tray_icon and hasattr(self._tray_icon, 'title'):
            self._tray_icon.title = text

    def _run_pystray(self) -> None:
        """使用 pystray 运行系统托盘。"""
        try:
            import pystray
            from PIL import Image, ImageDraw

            # 创建一个简单的图标（绿色圆点）
            def _create_icon():
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.ellipse([8, 8, 56, 56], fill=(0, 180, 80, 255))
                return img

            # 构建菜单
            def _on_show(icon, item):
                if self._on_show:
                    self._on_show()

            def _on_exit(icon, item):
                self.stop()
                if self._on_exit:
                    self._on_exit()

            menu = pystray.Menu(
                pystray.MenuItem("显示主窗口", _on_show, default=True),
                pystray.MenuItem("退出", _on_exit),
            )

            self._tray_icon = pystray.Icon(
                name="media_manager",
                icon=_create_icon(),
                title=self._app_name,
                menu=menu,
            )
            self._tray_icon.run()
        except ImportError:
            logger.warning("pystray 不可用，托盘通知功能将使用简化方案")
            self._fallback_notify("", "")
        except Exception as e:
            logger.error(f"系统托盘启动失败: {e}")

    @staticmethod
    def _fallback_notify(title: str, message: str) -> None:
        """回退通知方案（日志 + win10toast 尝试）。"""
        logger.info(f"通知: [{title}] {message}")
        try:
            from win10toast import ToastNotifier
            toaster = ToastNotifier()
            toaster.show_toast(title, message, duration=5, threaded=True)
        except Exception:
            pass
