# -*- coding: utf-8 -*-
"""
Quick Look 预览对话框 —— 空格键快速预览图片和视频。

功能：
- 全屏半透明遮罩，居中显示媒体内容
- 图片：QLabel 加载全分辨率 QPixmap
- 视频：优先 QMediaPlayer（含音频），不可用时回退 OpenCV
- 左右方向键切换文件，上下键也可
- 空格/Escape 关闭
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QPixmap, QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QHBoxLayout,
    QApplication, QSlider, QStackedWidget,
)

from app.ui.theme import BASE, TEXT, SUBTEXT_0, OVERLAY_0

logger = logging.getLogger(__name__)

# 条件导入 QMediaPlayer（部分 PySide6 发行版不含 QtMultimedia）
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


class QuickLookPreviewDialog(QDialog):
    """空格快速预览对话框。

    用法:
        dlg = QuickLookPreviewDialog(file_list, current_index, parent)
        dlg.exec()
    """

    def __init__(self, file_list: list[dict], current_index: int = 0,
                 seek_step_sec: int = 5, parent=None) -> None:
        """初始化预览对话框。

        参数:
            file_list: [{"path": str, "filename": str, "media_type": str, ...}]
            current_index: 当前显示文件在列表中的索引。
            seek_step_sec: 左右方向键跳转视频的步长（秒）。
        """
        super().__init__(parent)
        self._file_list = file_list
        self._current_index = current_index
        self._seek_step_sec = seek_step_sec

        # QMediaPlayer 视频/音频播放（仅在可用时初始化）
        self._player = None
        self._has_multimedia = _HAS_MULTIMEDIA
        if _HAS_MULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio_output = QAudioOutput(self)
            self._player.setAudioOutput(self._audio_output)
            self._player.positionChanged.connect(self._update_seek_bar)
            self._player.durationChanged.connect(self._on_duration_changed)

        # OpenCV 备选播放（无音频）
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
        self.setStyleSheet(
            f"background-color: {BASE};"
        )
        self._setup_ui()
        self._load_current()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 12)

        # 标题栏：文件名 + 索引
        self._title_label = QLabel()
        self._title_label.setStyleSheet(
            f"color: {TEXT}; font-size: 14px; font-weight: bold; "
            f"background: transparent; padding: 4px 0;"
        )
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._title_label)

        # 媒体显示区：QStackedWidget 切换图片/视频
        self._media_stack = QStackedWidget()
        self._media_stack.setMinimumSize(640, 480)

        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setStyleSheet("background: transparent;")
        self._media_stack.addWidget(self._image_label)

        if _HAS_MULTIMEDIA and QVideoWidget is not None:
            self._video_widget = QVideoWidget()
            self._video_widget.setStyleSheet("background: transparent;")
            self._media_stack.addWidget(self._video_widget)
        else:
            # 无 QMediaPlayer 时，用 QLabel 显示 OpenCV 帧
            self._video_widget = None
            self._video_label = QLabel()
            self._video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._video_label.setStyleSheet("background: transparent;")
            self._media_stack.addWidget(self._video_label)

        layout.addWidget(self._media_stack, 1)

        # 视频进度条
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
        self._seek_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ background: {OVERLAY_0}; height: 4px; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: #c6a0f6; width: 12px; margin: -5px 0; border-radius: 6px; }}"
            f"QSlider::sub-page:horizontal {{ background: #c6a0f6; border-radius: 2px; }}"
        )
        self._seek_slider.sliderMoved.connect(self._on_slider_seek)
        self._seek_slider.hide()
        seek_layout.addWidget(self._seek_slider, 1)
        self._seek_layout = seek_layout
        layout.addLayout(seek_layout)

        # 底部信息栏
        bottom_layout = QHBoxLayout()
        self._info_label = QLabel()
        self._info_label.setStyleSheet(
            f"color: {SUBTEXT_0}; font-size: 11px; background: transparent;"
        )
        bottom_layout.addWidget(self._info_label)
        bottom_layout.addStretch()
        hint_label = QLabel("← → 视频进度 | ↑ ↓ 切换文件 | Space/Esc 关闭")
        hint_label.setStyleSheet(
            f"color: {OVERLAY_0}; font-size: 11px; background: transparent;"
        )
        bottom_layout.addWidget(hint_label)
        layout.addLayout(bottom_layout)

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
        available = self._media_stack.size()
        scaled = pixmap.scaled(
            available, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._image_label.setPixmap(scaled)

    def _show_video(self, path_str: str) -> None:
        """播放视频（优先 QMediaPlayer 含音频，不可用时 OpenCV 无音频）。"""
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
        """OpenCV 逐帧播放视频（无音频，备选方案）。"""
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
        """OpenCV 模式：读取下一帧并显示。"""
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
        """停止视频播放（两种模式）。"""
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
        """更新进度条和时间标签。"""
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
        """视频总时长变化时更新进度条范围。"""
        if duration > 0:
            self._seek_slider.setRange(0, 1000)

    def _on_slider_seek(self, pos: int) -> None:
        """通过拖动进度条跳转视频。"""
        if self._player and self._player.duration() > 0:
            target_ms = int(pos * self._player.duration() / 1000)
            self._player.setPosition(target_ms)
        elif self._cap is not None and self._total_frames > 0:
            target_frame = int(pos * self._total_frames / 1000)
            target_frame = max(0, min(target_frame, self._total_frames - 1))
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, target_frame)

    def _navigate(self, delta: int) -> None:
        """切换文件。"""
        new_idx = self._current_index + delta
        if 0 <= new_idx < len(self._file_list):
            self._current_index = new_idx
            self._load_current()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Escape):
            self._stop_video()
            self.close()
        elif event.key() == Qt.Key.Key_Up:
            self._navigate(-1)
        elif event.key() == Qt.Key.Key_Down:
            self._navigate(1)
        elif event.key() == Qt.Key.Key_Left:
            self._seek_video(-self._seek_step_sec)
        elif event.key() == Qt.Key.Key_Right:
            self._seek_video(self._seek_step_sec)
        else:
            super().keyPressEvent(event)

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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, '_media_stack'):
            self._load_current()

    def closeEvent(self, event) -> None:
        self._stop_video()
        super().closeEvent(event)
