# -*- coding: utf-8 -*-
"""
应用全局配置模块。

使用冻结数据类（frozen dataclass）实现不可变配置，
从 JSON 文件加载，支持运行时覆盖。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import FrozenSet, Optional
import json


@dataclass(frozen=True)
class AppConfig:
    """不可变应用配置，所有字段通过构造时赋值，运行期不可修改。

    设计原则：
    - 所有默认值在此定义，JSON 文件可覆盖
    - 使用 frozenset 确保集合类型不可变
    - 使用 Path 对象表示文件系统路径
    """

    # ========== 数据库 ==========
    db_path: Path = Path("data/media_manager.db")
    """SQLite 数据库文件路径（相对于项目根目录）。"""

    # ========== 扫描 ==========
    media_extensions: FrozenSet[str] = field(default_factory=lambda: frozenset({
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif',
        '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
        '.mpg', '.mpeg', '.3gp', '.ts', '.heic', '.heif',
        '.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2', '.pef', '.raf', '.3fr', '.x3f',
    }))
    """参与索引的媒体文件扩展名集合。"""

    exclude_patterns: FrozenSet[str] = field(default_factory=lambda: frozenset({
        '.thumbnails', '__MACOSX', '.DS_Store', 'Thumbs.db',
        '$RECYCLE.BIN', 'System Volume Information', '.git', '.svn',
    }))
    """扫描时需排除的文件夹名称集合。"""

    # ========== 哈希 ==========
    hash_algorithms: FrozenSet[str] = field(default_factory=lambda: frozenset({
        'md5', 'phash', 'dhash',
    }))
    """需要计算的哈希算法列表。"""

    phash_size: int = 8
    """感知哈希（pHash）的尺寸，值越大越精确但计算越慢。"""

    dhash_size: int = 8
    """差异哈希（dHash）的尺寸。"""

    video_frame_interval_sec: int = 5
    """视频帧提取间隔（秒），每隔 N 秒抽一帧用于感知哈希。"""

    # ========== 查重 ==========
    jaccard_threshold: float = 0.80
    """杰卡德相似度阈值 [0.0, 1.0]，达到此值视为资源单元级重复。"""

    phash_hamming_threshold: int = 5
    """pHash 汉明距离阈值，≤ 此值视为匹配。"""

    dhash_hamming_threshold: int = 5
    """dHash 汉明距离阈值，≤ 此值视为匹配。"""

    face_distance_threshold: float = 0.6
    """人脸向量欧氏距离阈值，≤ 此值视为同一人物。"""

    # ========== 人脸识别 ==========
    face_detection_enabled: bool = True
    """是否启用人脸识别（模型已就绪，默认开启）。"""

    face_model_dir: Path = Path("models")
    """人脸识别预训练模型存放目录。"""

    face_confidence_threshold: float = 0.7
    """人脸检测置信度阈值。"""

    # ========== 缩略图 ==========
    thumbnail_max_size: int = 256
    """缩略图最大边长（像素），等比缩放后不超过此值。"""

    thumbnail_cache_subdir: str = ".thumbnails"
    """缩略图缓存子目录名，各资源单元下自动创建（旧模式）。"""

    thumbnail_cache_dir: Path = Path("data/.thumbnails")
    """缩略图集中缓存目录，默认存放在软件 data 目录下。"""

    thumbnail_format: str = "jpg"
    """缩略图输出格式（jpg/png）。"""

    thumbnail_quality: int = 80
    """缩略图 JPEG 质量（1-100）。"""

    # ========== GUI ==========
    window_title: str = "影视资源管理器"
    """主窗口标题。"""

    window_width: int = 1400
    window_height: int = 900
    """默认窗口尺寸。"""

    splitter_ratio_left: int = 30
    """左右分割比例（左侧百分比，默认 30%）。"""

    grid_column_count: int = 4
    """缩略图网格每行列数。"""

    grid_spacing: int = 8
    """缩略图间距（像素）。"""

    # ========== 文件监控 ==========
    watcher_enabled: bool = True
    """是否启用实时文件监控。"""

    watcher_debounce_ms: int = 2000
    """文件监控去抖动时间（毫秒），同一文件在此时段内多次变化合并为一次。"""

    # ========== API 服务 ==========
    api_enabled: bool = True
    """是否在程序启动时一并启动 HTTP API 服务。"""

    api_host: str = "127.0.0.1"
    """API 监听地址，默认仅本机访问；如需局域网访问可改为 0.0.0.0。"""

    api_port: int = 19527
    """API 监听端口。"""

    web_pin: str = ""
    """Web 前端访问密码（空字符串表示不启用密码）。"""

    # ========== 预览 ==========
    preview_seek_percent: int = 5
    """预览对话框中左右方向键每次跳转的视频时长百分比。"""

    # ========== SMB 共享 ==========
    smb_share_name_prefix: str = "Media_"
    """SMB 共享名的默认前缀。"""

    # ========== 工厂方法 ==========

    @classmethod
    def from_file(cls, path: Path) -> "AppConfig":
        """从 JSON 文件加载配置，文件中的值覆盖默认值。

        参数:
            path: JSON 配置文件路径。

        返回:
            AppConfig: 合并后的配置对象。

        异常:
            FileNotFoundError: 配置文件不存在。
            ValueError: JSON 解析失败。
        """
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 将字符串路径转为 Path 对象
        if "db_path" in data:
            data["db_path"] = Path(data["db_path"])
        if "face_model_dir" in data:
            data["face_model_dir"] = Path(data["face_model_dir"])
        if "thumbnail_cache_dir" in data:
            data["thumbnail_cache_dir"] = Path(data["thumbnail_cache_dir"])

        # 将列表转为 frozenset
        if "media_extensions" in data:
            data["media_extensions"] = frozenset(data["media_extensions"])
        if "exclude_patterns" in data:
            data["exclude_patterns"] = frozenset(data["exclude_patterns"])
        if "hash_algorithms" in data:
            data["hash_algorithms"] = frozenset(data["hash_algorithms"])

        # 使用默认配置为底，JSON 配置覆盖
        default = cls()
        merged = {**default.__dict__, **data}
        return cls(**merged)

    def to_file(self, path: Path) -> None:
        """将当前配置保存为 JSON 文件。

        参数:
            path: 目标 JSON 文件路径。
        """
        data = {
            "db_path": str(self.db_path),
            "media_extensions": sorted(self.media_extensions),
            "exclude_patterns": sorted(self.exclude_patterns),
            "hash_algorithms": sorted(self.hash_algorithms),
            "phash_size": self.phash_size,
            "dhash_size": self.dhash_size,
            "video_frame_interval_sec": self.video_frame_interval_sec,
            "jaccard_threshold": self.jaccard_threshold,
            "phash_hamming_threshold": self.phash_hamming_threshold,
            "dhash_hamming_threshold": self.dhash_hamming_threshold,
            "face_distance_threshold": self.face_distance_threshold,
            "face_detection_enabled": self.face_detection_enabled,
            "face_model_dir": str(self.face_model_dir),
            "face_confidence_threshold": self.face_confidence_threshold,
            "thumbnail_max_size": self.thumbnail_max_size,
            "thumbnail_cache_subdir": self.thumbnail_cache_subdir,
            "thumbnail_cache_dir": str(self.thumbnail_cache_dir),
            "thumbnail_format": self.thumbnail_format,
            "thumbnail_quality": self.thumbnail_quality,
            "window_title": self.window_title,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "splitter_ratio_left": self.splitter_ratio_left,
            "grid_column_count": self.grid_column_count,
            "grid_spacing": self.grid_spacing,
            "watcher_enabled": self.watcher_enabled,
            "watcher_debounce_ms": self.watcher_debounce_ms,
            "api_enabled": self.api_enabled,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "smb_share_name_prefix": self.smb_share_name_prefix,
            "preview_seek_percent": self.preview_seek_percent,
            "web_pin": self.web_pin,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
