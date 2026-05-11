# -*- coding: utf-8 -*-
"""
Quick Look 预览对话框 —— 空格键快速预览图片和视频。

图片：QPixmap 自适应缩放
视频：QMediaPlayer（含音频），备选 OpenCV 逐帧
键盘：
  ← →    短按跳转 N%，长按 2x 倍速播放
  ↑ ↓    切换上一个/下一个文件
  Space   关闭
  Escape  关闭
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QUrl, QElapsedTimer
from PySide6.QtGui import QPixmap, QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QHBoxLayout,
    QApplication, QSlider, QStackedWidget,
)

from app.ui.theme import BASE, TEXT, SUBTEXT_0, OVERLAY_0

logger = logging.getLogger(__name__)

_HAS_MULTIMEDIA = False
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
    _HAS_MULTIMEDIA = True
except ImportError:
    QMediaPlayer = None
    QAudioOutput = None
    QVideoWidget = None
    logger.info("QtMultimedia 不可用，视频预览将回退到无音频模式")


class _KeyHoldTimer:
    """按键长按检测：长按时控制播放速度。"""

    def __init__(self, player_getter, target_rate: float):
        self._get_player = player_getter
        self._target_rate = target_rate
        self._active = False

    def start(self) -> None:
        player = self._get_player()
        if not player:
            return
        self._active = True
        player.setPlaybackRate(self._target_rate)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        player = self._get_player()
        if player:
            player.setPlaybackRate(1.0)

    @property
    def is_active(self) -> bool:
        return self._active


class QuickLookPreviewDialog(QDialog):
    """空格快速预览对话框。"""

    def __init__(self, file_list: list[dict], current_index: int = 0,
                 seek_percent: int = 5, parent=None) -> None:
        super().__init__(parent)
        self._file_list = file_list
        self._current_index = current_index
        self._seek_percent = seek_percent
        self._seeking = False
        self._hold_timer = QTimer()
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(300)  # 300ms 区分短按和长按
        self._hold_key = 0  # 0=none, Qt.Key_Left, Qt.Key_Right

        # 长按变速（左=0.5x慢放，右=2.0x快放）
        self._left_hold = _KeyHoldTimer(lambda: self._player, target_rate=0.5)
        self._right_hold = _KeyHoldTimer(lambda: self._player, target_rate=2.0)

        # QMediaPlayer
        self._player = None
        self._has_multimedia = _HAS_MULTIMEDIA
        if _HAS_MULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio_output = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.positionChanged.connect(self._update_seek_bar)
            self._player.durationChanged.connect(self._on_duration_changed)

        # OpenCV 备选
        self._cv2 = None
        self._video_timer = QTimer(self)
        self._video_timer.timeout.connect(self._next_video_frame)
        self._cap = None
        self._total_frames = 0
        self._fps = 0.0

        self.setWindowTitle("预览")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setStyleSheet(f"background-color: {BASE};")
        self._setup_ui()
        self._load_current()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 12)

        # 标题栏
        self._title_label = QLabel()
        self._title_label.setStyleSheet(
            f"color: {TEXT}; font-size: 14px; font-weight: bold; "
            f"background: transparent; padding: 4px 0;"
        )
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title_label)

        # 媒体显示区
        self._media_stack = QStackedWidget()
        self._media_stack.setMinimumSize(320, 240)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background: transparent;")
        self._media_stack.addWidget(self._image_label)

        if _HAS_MULTIMEDIA and QVideoWidget is not None:
            self._video_widget = QVideoWidget()
            self._video_widget.setStyleSheet("background: transparent;")
            self._media_stack.addWidget(self._video_widget)
        else:
            self._video_widget = None
            self._video_label = QLabel()
            self._video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._video_label.setStyleSheet("background: transparent;")
            self._media_stack.addWidget(self._video_label)

        layout.addWidget(self._media_stack, 1)

        # 进度条（禁止获取焦点，避免拦截方向键）
        seek_layout = QHBoxLayout()
        seek_layout.setContentsMargins(0, 0, 0, 0)
        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setStyleSheet(
            f"color: {SUBTEXT_0}; font-size: 11px; background: transparent;"
        )
        self._time_label.setFixedWidth(100)
        seek_layout.addWidget(self._time_label)
        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 1000)
        self._seek_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 不抢键盘
        self._seek_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ background: {OVERLAY_0}; height: 4px; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: #c6a0f6; width: 12px; margin: -5px 0; border-radius: 6px; }}"
            f"QSlider::sub-page:horizontal {{ background: #c6a0f6; border-radius: 2px; }}"
        )
        # 拖动 & 点击进度条的处理
        self._seek_slider.sliderPressed.connect(lambda: setattr(self, '_seeking', True))
        self._seek_slider.sliderReleased.connect(self._on_slider_released)
        self._seek_slider.sliderMoved.connect(self._on_slider_seek)
        self._seek_slider.hide()
        seek_layout.addWidget(self._seek_slider, 1)
        layout.addLayout(seek_layout)

        # 底部信息栏
        bottom_layout = QHBoxLayout()
        self._info_label = QLabel()
        self._info_label.setStyleSheet(
            f"color: {SUBTEXT_0}; font-size: 11px; background: transparent;"
        )
        bottom_layout.addWidget(self._info_label)
        bottom_layout.addStretch()
        hint_label = QLabel("← → 短按跳转 长按变速  |  ↑ ↓ 切换文件  |  Space/Esc 关闭")
        hint_label.setStyleSheet(
            f"color: {OVERLAY_0}; font-size: 11px; background: transparent;"
        )
        bottom_layout.addWidget(hint_label)
        layout.addLayout(bottom_layout)

        # 确保对话框能接收到按键
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _load_current(self) -> None:
        """加载当前索引的文件。"""
        self._stop_video()
        if 0 <= self._current_index < len(self._file_list):
            f = self._file_list[self._current_index]
            self._title_label.setText(
                f"[{self._current_index + 1}/{len(self._file_list)}]  {f['filename']}"
            )
            media_type = f.get("media_type", "image")
            file_path = f.get("path", "")

            if media_type == "image":
                self._show_image(file_path)
            elif media_type == "video":
                self._show_video(file_path)
            else:
                self._media_stack.setCurrentWidget(self._image_label)
                self._image_label.setText("不支持的文件格式")
        self._update_info()

    def _detect_video_size(self, path_str: str) -> tuple[int, int]:
        """检测视频分辨率，返回 (width, height)。"""
        try:
            from app.utils.image_helpers import VideoCapture_unicode
            import cv2
            cap = VideoCapture_unicode(Path(path_str))
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                cap.release()
                return w, h
        except Exception:
            pass
        return 0, 0

    def _show_image(self, path_str: str) -> None:
        """显示图片。"""
        self._media_stack.setCurrentWidget(self._image_label)
        self._seek_slider.hide()
        self._time_label.setText("")
        if self._player:
            self._player.stop()
        pixmap = QPixmap(path_str)
        if pixmap.isNull():
            self._image_label.setText("无法加载图片")
            return
        # 根据图片比例调整窗口
        self._adjust_dialog_for_media(pixmap.width(), pixmap.height())
        available = self._media_stack.size()
        scaled = pixmap.scaled(
            available, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)

    def _adjust_dialog_for_media(self, w: int, h: int) -> None:
        """根据媒体宽高比调整对话框尺寸。"""
        if w <= 0 or h <= 0:
            return
        screen = QApplication.primaryScreen().availableGeometry()
        max_w = int(screen.width() * 0.85)
        max_h = int(screen.height() * 0.85)
        ratio = w / h
        dialog_w = min(max_w, int(max_h * ratio))
        dialog_h = min(max_h, int(max_w / ratio))
        dialog_w = max(dialog_w, 360)
        dialog_h = max(dialog_h, 240)
        if ratio < 1.0:  # 竖屏
            dialog_w = min(int(max_h * ratio * 0.7), max_w)
        self.resize(dialog_w, dialog_h)
        self.move(
            screen.center().x() - dialog_w // 2,
            screen.center().y() - dialog_h // 2,
        )

    def _show_video(self, path_str: str) -> None:
        """播放视频。"""
        w, h = self._detect_video_size(path_str)
        self._adjust_dialog_for_media(w, h)

        if self._player and self._video_widget is not None:
            self._media_stack.setCurrentWidget(self._video_widget)
            self._player.setSource(QUrl.fromLocalFile(path_str))
            self._player.setVideoOutput(self._video_widget)
            self._seek_slider.show()
            self._seek_slider.setValue(0)
            self._player.play()
        else:
            self._play_video_opencv(path_str)

    def _play_video_opencv(self, path_str: str) -> None:
        """OpenCV 逐帧播放。"""
        try:
            import cv2
            self._cv2 = cv2
        except ImportError:
            self._media_stack.setCurrentWidget(self._video_label)
            self._video_label.setText("OpenCV 不可用，无法预览视频")
            return
        try:
            from app.utils.image_helpers import VideoCapture_unicode
            self._cap = VideoCapture_unicode(Path(path_str))
            if not self._cap.isOpened():
                self._media_stack.setCurrentWidget(self._video_label)
                self._video_label.setText("无法打开视频")
                return
            self._total_frames = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self._fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
            self._seek_slider.show()
            self._media_stack.setCurrentWidget(self._video_label)
            self._update_seek_bar()
            self._video_timer.start(33)
        except Exception as e:
            logger.error(f"打开视频失败: {path_str} - {e}")
            self._media_stack.setCurrentWidget(self._video_label)
            self._video_label.setText("视频加载失败")

    def _next_video_frame(self) -> None:
        """OpenCV 模式：读取下一帧。"""
        if self._cap is None or not self._cap.isOpened():
            self._video_timer.stop()
            return
        ret, frame = self._cap.read()
        if not ret:
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = self._cap.read()
            if not ret:
                self._video_timer.stop()
                return
        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        available = self._media_stack.size()
        from PySide6.QtGui import QImage
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        scaled = pixmap.scaled(
            available, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._video_label.setPixmap(scaled)
        if not self._seeking:
            self._update_seek_bar()

    def _update_info(self) -> None:
        if 0 <= self._current_index < len(self._file_list):
            f = self._file_list[self._current_index]
            size = f.get("size_formatted", "")
            path = f.get("path", "")
            if len(path) > 60:
                path = "..." + path[-57:]
            self._info_label.setText(f"{size}  |  {path}")

    def _stop_video(self) -> None:
        """停止视频播放。"""
        if self._player:
            self._player.stop()
        self._video_timer.stop()
        self._seek_slider.hide()
        self._time_label.setText("")
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

    def _update_seek_bar(self) -> None:
        """更新进度条（仅在用户未拖拽时）。"""
        if self._seeking:
            return
        if self._player and self._player.duration() > 0:
            duration = self._player.duration()
            position = self._player.position()
        elif self._cap is not None and self._total_frames > 0:
            current_frame = self._cap.get(self._cv2.CAP_PROP_POS_FRAMES)
            position = int(current_frame / self._fps * 1000) if self._fps > 0 else 0
            duration = int(self._total_frames / self._fps * 1000) if self._fps > 0 else 0
        else:
            return
        if duration <= 0:
            return
        pos = int(1000 * position / duration)
        self._seek_slider.blockSignals(True)
        self._seek_slider.setValue(pos)
        self._seek_slider.blockSignals(False)
        current_sec = int(position / 1000)
        total_sec = int(duration / 1000)
        self._time_label.setText(
            f"{current_sec // 60:02d}:{current_sec % 60:02d} / "
            f"{total_sec // 60:02d}:{total_sec % 60:02d}"
        )

    def _on_duration_changed(self, duration: int) -> None:
        if duration > 0:
            self._seek_slider.setRange(0, 1000)

    def _on_slider_seek(self, pos: int) -> None:
        """拖拽或点击进度条时跳转。"""
        if self._player and self._player.duration() > 0:
            target_ms = int(pos * self._player.duration() / 1000)
            self._player.setPosition(target_ms)
        elif self._cap is not None and self._total_frames > 0:
            target_frame = int(pos * self._total_frames / 1000)
            target_frame = max(0, min(target_frame, self._total_frames - 1))
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, target_frame)

    def _on_slider_released(self) -> None:
        """进度条释放：根据最终位置跳转并恢复进度更新。"""
        pos = self._seek_slider.value()
        self._on_slider_seek(pos)
        self._seeking = False

    def _navigate(self, delta: int) -> None:
        """切换文件。"""
        new_idx = self._current_index + delta
        if 0 <= new_idx < len(self._file_list):
            self._current_index = new_idx
            self._load_current()

    def _seek_video(self, delta_sec: int) -> None:
        """按指定秒数跳转视频进度。"""
        if self._player:
            duration = self._player.duration()
            if duration <= 0:
                self._navigate(-1 if delta_sec < 0 else 1)
                return
            target = self._player.position() + delta_sec * 1000
            target = max(0, min(target, duration))
            self._player.setPosition(target)
        elif self._cap is not None and self._fps > 0:
            current_frame = self._cap.get(self._cv2.CAP_PROP_POS_FRAMES)
            target_frame = current_frame + int(delta_sec * self._fps)
            target_frame = max(0, min(target_frame, self._total_frames - 1))
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, target_frame)
            self._next_video_frame()
        else:
            self._navigate(-1 if delta_sec < 0 else 1)

    def _seek_video_proportional(self, direction: int) -> None:
        """按视频时长比例跳转（方向键：跳转 5% 时长，最小 3 秒）。"""
        if self._player:
            duration = self._player.duration()
            if duration <= 0:
                self._navigate(-1 if direction < 0 else 1)
                return
            step_ms = max(3000, int(duration * self._seek_percent / 100))
            target = self._player.position() + direction * step_ms
            target = max(0, min(target, duration))
            self._player.setPosition(target)
        elif self._cap is not None and self._fps > 0:
            duration_frames = self._total_frames
            if duration_frames <= 0:
                self._navigate(-1 if direction < 0 else 1)
                return
            step_frames = max(int(self._fps * 3), int(duration_frames * self._seek_percent / 100))
            current_frame = self._cap.get(self._cv2.CAP_PROP_POS_FRAMES)
            target_frame = current_frame + direction * step_frames
            target_frame = max(0, min(target_frame, duration_frames - 1))
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, target_frame)
            self._next_video_frame()
        else:
            self._navigate(-1 if direction < 0 else 1)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            self._hold_timer.stop()
            self._stop_video()
            self.close()
        elif key == Qt.Key.Key_Up:
            self._hold_timer.stop()
            self._navigate(-1)
        elif key == Qt.Key.Key_Down:
            self._hold_timer.stop()
            self._navigate(1)
        elif key == Qt.Key.Key_Left:
            self._hold_timer.stop()
            if not self._player and self._cap is None:
                self._navigate(-1)
            else:
                self._hold_key = Qt.Key.Key_Left
                self._seek_video_proportional(-1)
                self._hold_timer.timeout.connect(self._on_hold_triggered, Qt.ConnectionType.SingleShotConnection)
                self._hold_timer.start()
        elif key == Qt.Key.Key_Right:
            self._hold_timer.stop()
            if not self._player and self._cap is None:
                self._navigate(1)
            else:
                self._hold_key = Qt.Key.Key_Right
                self._seek_video_proportional(1)
                self._hold_timer.timeout.connect(self._on_hold_triggered, Qt.ConnectionType.SingleShotConnection)
                self._hold_timer.start()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:
        key = event.key()
        if key == Qt.Key.Key_Left:
            self._hold_key = 0
            self._hold_timer.stop()
            self._left_hold.stop()
        elif key == Qt.Key.Key_Right:
            self._hold_key = 0
            self._hold_timer.stop()
            self._right_hold.stop()
        super().keyReleaseEvent(event)

    def _on_hold_triggered(self) -> None:
        """长按触发：启动倍速播放。"""
        if self._hold_key == Qt.Key.Key_Left:
            self._left_hold.start()
        elif self._hold_key == Qt.Key.Key_Right:
            self._right_hold.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, '_media_stack'):
            self._load_current()

    def closeEvent(self, event) -> None:
        self._stop_video()
        super().closeEvent(event)
