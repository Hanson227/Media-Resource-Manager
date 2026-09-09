# -*- coding: utf-8 -*-
"""
应用全局配置模块。

使用冻结数据类（frozen dataclass）实现不可变配置，
从 JSON 文件加载，支持运行时覆盖。
"""

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import FrozenSet, Optional, get_type_hints, get_origin
import json
import warnings

# 敏感字段不写入 config.json（后者在版本库中被跟踪，明文 PIN 会被提交）。
# 改为存放在 config.json 同级的 data/.web_pin —— data/ 已被 .gitignore 忽略。
_SENSITIVE_FIELDS = frozenset({"web_pin"})
_PIN_FILENAME = ".web_pin"


def _pin_file(config_path: Path) -> Path:
    """返回敏感字段（Web 访问密码）的存放路径。"""
    return Path(config_path).parent / "data" / _PIN_FILENAME


def _read_pin_file(config_path: Path) -> "Optional[str]":
    """读取 Web 访问密码；文件不存在返回 None（区别于“已设置为空”）。"""
    try:
        return _pin_file(config_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_pin_file(config_path: Path, pin: str) -> None:
    """写入 Web 访问密码（空字符串表示不启用密码）。"""
    target = _pin_file(config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pin or "", encoding="utf-8")


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

    @staticmethod
    def _coerce(name: str, value, hint):
        """将 JSON 值按字段类型注解转换（Path / frozenset / 原样）。"""
        if hint is Path:
            return Path(value)
        if get_origin(hint) is frozenset:
            return frozenset(value)
        return value

    @classmethod
    def from_file(cls, path: Path) -> "AppConfig":
        """从 JSON 文件加载配置，文件中的值覆盖默认值。

        仅接受当前字段名集合内的键；未知/遗留键会告警并忽略，
        避免旧版本或手写配置中的多余字段导致整体回退默认值。

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

        hints = get_type_hints(cls)
        unknown = [k for k in data if k not in hints]
        if unknown:
            warnings.warn(
                f"配置文件含未知字段，已忽略: {', '.join(sorted(unknown))} ({path})"
            )
            data = {k: v for k, v in data.items() if k in hints}

        # web_pin 为敏感字段：优先读 data/.web_pin（gitignored），
        # 兼容旧配置中 config.json 的明文并自动迁移一次，避免 PIN 进版本库。
        legacy_pin = data.pop("web_pin", None)
        secret_pin = _read_pin_file(path)
        if secret_pin is None and legacy_pin:
            try:
                _write_pin_file(path, legacy_pin)
            except OSError as e:
                warnings.warn(f"Web 访问密码迁移到 {_pin_file(path)} 失败: {e}")
            secret_pin = legacy_pin
        if secret_pin is not None:
            data["web_pin"] = secret_pin

        # 将 JSON 值按字段类型注解转换（Path、frozenset 等）
        # 注意：不能在遍历中 del data[key]（RuntimeError: dictionary changed
        # size during iteration 会让 main.py 整体回退默认配置 —— 表现为
        # web_pin 被清空、db_path 指向默认库）。先收集非法键，循环后统一剔除。
        bad_keys: list[str] = []
        for key, value in list(data.items()):
            try:
                data[key] = cls._coerce(key, value, hints[key])
            except (TypeError, ValueError) as e:
                warnings.warn(f"配置字段 {key} 解析失败，使用默认值: {e}")
                bad_keys.append(key)
        for key in bad_keys:
            data.pop(key, None)

        # 使用默认配置为底，JSON 配置覆盖
        default = cls()
        merged = {**default.__dict__, **data}
        return cls(**merged)

    def to_file(self, path: Path) -> None:
        """将当前配置保存为 JSON 文件（按 dataclass 字段驱动，避免手抄遗漏）。

        敏感字段（web_pin）不写入该文件，单独落到同级的 data/.web_pin，
        防止明文密码被提交进版本库。

        参数:
            path: 目标 JSON 文件路径。
        """
        def _to_json(value):
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, frozenset):
                return sorted(value)
            return value

        data = {
            f.name: _to_json(getattr(self, f.name))
            for f in fields(self) if f.name not in _SENSITIVE_FIELDS
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        try:
            _write_pin_file(path, self.web_pin)
        except OSError as e:
            warnings.warn(f"保存 Web 访问密码失败 ({_pin_file(path)}): {e}")

    def with_updates(self, **updates) -> "AppConfig":
        """返回应用部分字段更新后的新实例（frozen dataclass 不可变语义）。"""
        return replace(self, **updates)
