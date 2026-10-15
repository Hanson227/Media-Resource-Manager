# -*- coding: utf-8 -*-
"""
应用全局配置模块。

使用冻结数据类（frozen dataclass）实现不可变配置，
从 JSON 文件加载，支持运行时覆盖。
"""

from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import FrozenSet, Optional, Union, get_args, get_origin, get_type_hints
import json
import warnings

# 仅依赖无第三方依赖的常量模块，保持 config 可在早期安全导入
from app.utils.constants import FACE_SIMILARITY_THRESHOLD, FACE_VIDEO_MAX_FRAMES

# 敏感字段不写入 config.json（后者在版本库中被跟踪，明文 PIN 会被提交）。
_SENSITIVE_FIELDS = frozenset({"web_pin"})
_PIN_FILENAME = ".web_pin"

# 运行档位：完整档（桌面 GUI + 查重）/ 轻量档（便携版：只跑 Web+API，不加载查重）
PROFILE_FULL = "full"
PROFILE_LITE = "lite"
VALID_PROFILES = (PROFILE_FULL, PROFILE_LITE)

# PIN 存放目录覆盖（由 app/utils/paths.resolve_paths 注入）：
# 启用数据根时 PIN 必须跟着数据盘走，否则"平板上不留数据"这条就破了。
_pin_override_dir: Optional[Path] = None


def configure_pin_dir(directory: Optional[Path]) -> None:
    """覆盖 PIN 文件存放目录（None = 回退到 config.json 同级的 data/）。"""
    global _pin_override_dir
    _pin_override_dir = Path(directory) if directory else None


def pin_file_path(config_path: Path) -> Path:
    """返回敏感字段（Web 访问密码）的存放路径。"""
    base = _pin_override_dir if _pin_override_dir is not None else (Path(config_path).parent / "data")
    return Path(base) / _PIN_FILENAME


def _read_pin_file(config_path: Path) -> "Optional[str]":
    """读取 Web 访问密码；文件不存在返回 None（区别于“已设置为空”）。"""
    try:
        return pin_file_path(config_path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _write_pin_file(config_path: Path, pin: str) -> None:
    """写入 Web 访问密码（空字符串表示不启用密码）。"""
    target = pin_file_path(config_path)
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

    # ========== 运行档位 ==========
    profile: str = PROFILE_FULL
    """运行档位：full（桌面 GUI + 查重）/ lite（便携版：只跑 Web+API，不加载查重）。

    轻量档不删除任何代码：只是不导入哈希/人脸/查重管线、不注册查重路由，
    打包时这些模块自然不会被收录。环境变量 MEDIA_PROFILE 可覆盖。
    """

    # ========== 数据根（便携版） ==========
    data_root: Optional[Path] = None
    """数据根目录（数据库 / 缩略图 / PIN 都放这里，可指向移动硬盘）。

    None = 未启用：路径相对应用目录解析（老行为，升级后语义不变）。
    注意不能用 Path("") 表示"未设置"——`Path("") == Path(".")`，与"当前目录"
    无法区分，会把路径解析悄悄改成相对 CWD。细节见 `app/utils/paths.resolve_paths`。
    """

    data_root_id: str = ""
    """数据根身份（marker 文件里的 uuid），换盘符后据此认出"还是那个数据盘"。"""

    data_root_marker: str = ".media_manager_data.json"
    """数据根标记文件名：在卷上搜到它才认作数据根，不靠目录名硬编码。"""

    data_root_search_depth: int = 2
    """换盘符后自动搜索数据根的目录深度（卷根为 0）。"""

    # ========== 数据库 ==========
    db_path: Path = Path("data/media_manager.db")
    """SQLite 数据库文件路径（未启用数据根时相对应用目录，启用后相对数据根）。"""

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

    face_similarity_threshold: float = FACE_SIMILARITY_THRESHOLD
    """人脸特征余弦相似度阈值，≥ 此值视为同一人物（SFace 官方推荐 0.363）。"""

    related_min_matches: int = 2
    """"疑似相关"的最少证据数（计数匹配 + 人脸线索之和）。

    达不到"重复"阈值、但证据数 ≥ 此值的单元对会被记为一档"疑似相关"仅作提醒。
    实测：本库 166 个单元中，证据 ≥1 有 672 对（含大量单条人脸误配），
    ≥2 有 234 对，≥3 有 99 对。取 2 以过滤"只有一条人脸凑巧相似"的噪声。
    """

    # ========== 人脸识别 ==========
    face_detection_enabled: bool = True
    """是否启用人脸识别（模型已就绪，默认开启）。"""

    face_model_dir: Path = Path("models")
    """人脸识别预训练模型存放目录。"""

    face_confidence_threshold: float = 0.7
    """人脸检测置信度阈值。"""

    face_video_max_frames: int = FACE_VIDEO_MAX_FRAMES
    """单个视频做多少次人脸检测（默认 10）。

    视频人脸改用"等间隔均匀铺满全片"后，这个值直接决定准确率与耗时：
    YuNet 单帧约 82ms（长边 1280），10 帧 ≈ 0.8s/视频。
    调大更准（长视频覆盖更密），调小更快。图片不受影响（全图检测只做一次）。
    """

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

    # ========== HEIC 转换 ==========
    heic_jpeg_quality: int = 95
    """HEIC → JPG 的输出质量（1-100）。95 接近视觉无损，体积约为 HEIC 的 2~3 倍。"""

    heic_source_action: str = "trash"
    """转换成功后如何处理源 HEIC：trash（移至回收站，默认）/ delete（永久删除）/
    keep（保留源文件）。默认回收站：回收站不可用时**保留源文件**并如实上报，
    绝不静默降级成永久删除（与 file_ops 的删除纪律一致）。"""

    heic_on_conflict: str = "skip"
    """同名 JPG 已存在时的策略：skip（跳过并上报，默认）/ rename（自动加序号）/
    overwrite（覆盖）。默认 skip —— 已存在的同名 JPG 很可能就是这个 HEIC 的
    历史转换产物，改名会凭空多出重复图，覆盖可能毁掉有价值的那张。"""

    heic_reindex_after_convert: bool = True
    """转换完成后是否立即为受影响的文件重算哈希与人脸（否则要等下一次
    一键查重/索引才会补上）。"""

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
        """将 JSON 值按字段类型注解转换（Path / Optional[Path] / frozenset / 原样）。

        只有 Optional[Path] 字段接受 None（表示"未设置"）；普通 Path 字段给 None
        仍按非法处理（回退该项默认值）—— `Path(None)` 抛 TypeError，
        由 from_file 捕获后剔除该键。这条区分是"数据根未设置"能表达出来的前提。
        """
        origin = get_origin(hint)
        optional_path = origin is Union and type(None) in get_args(hint)
        if hint is Path or optional_path:
            if value is None:
                if not optional_path:
                    raise TypeError(f"{name} 不接受 None")
                return None
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
                warnings.warn(f"Web 访问密码迁移到 {pin_file_path(path)} 失败: {e}")
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
        # profile 是运行档位开关：拼错时必须显式回退并告警，
        # 不能因为一个笔误让"轻量档"悄悄变成完整档（或反过来）。
        if str(merged.get("profile", "")).lower() not in VALID_PROFILES:
            warnings.warn(
                f"配置字段 profile 非法: {merged.get('profile')!r}，回退 {PROFILE_FULL}"
            )
            merged["profile"] = PROFILE_FULL
        return cls(**merged)

    def to_file(self, path: Path, omit: "tuple[str, ...]" = ()) -> None:
        """将当前配置保存为 JSON 文件（按 dataclass 字段驱动，避免手抄遗漏）。

        敏感字段（web_pin）不写入该文件，单独落到同级的 data/.web_pin，
        防止明文密码被提交进版本库。

        参数:
            path: 目标 JSON 文件路径。
            omit: 额外不写入的字段名。给便携版入口用：**没被用户显式写过**
                `profile` 时不要把它落盘 —— 否则 main_web.py 跑一次就把
                `profile: lite` 写进共享的 config.json，桌面端下次启动会
                跟着变成轻量档（查重入口消失，点了就 404）。
        """
        def _to_json(value):
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, frozenset):
                return sorted(value)
            return value

        skip = set(_SENSITIVE_FIELDS) | set(omit)
        data = {
            f.name: _to_json(getattr(self, f.name))
            for f in fields(self) if f.name not in skip
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        try:
            _write_pin_file(path, self.web_pin)
        except OSError as e:
            warnings.warn(f"保存 Web 访问密码失败 ({pin_file_path(path)}): {e}")

    def with_updates(self, **updates) -> "AppConfig":
        """返回应用部分字段更新后的新实例（frozen dataclass 不可变语义）。"""
        return replace(self, **updates)

    @property
    def is_lite(self) -> bool:
        """是否轻量档（**仅表示"这是便携版那个包"，不参与任何功能分支**）。

        功能可用性一律由 `app.utils.capabilities` 的真实探测决定（包里有没有
        Pillow / cv2 / pillow-heif / rawpy / 人脸模型 / PySide6）。历史教训：曾用
        这个档位决定"不注册查重路由"，结果同一份代码里长出两套功能语义 ——
        HTTP 契约、前端导航、测试、文档全部要分叉。体积差异请用打包开关
        （`MEDIA_BUILD_NO_CV2` / `MEDIA_BUILD_WITH_FACE`）。
        """
        return str(self.profile).strip().lower() == PROFILE_LITE
