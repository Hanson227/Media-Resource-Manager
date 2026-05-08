# -*- coding: utf-8 -*-
"""
应用常量与枚举定义。

集中管理所有枚举类型，避免魔法字符串分散在代码各处。
"""

from enum import Enum


class MediaType(str, Enum):
    """媒体文件类型。"""
    IMAGE = "image"
    VIDEO = "video"


class UnitStatus(str, Enum):
    """资源单元状态。"""
    ACTIVE = "active"        # 正常活动状态
    MERGED = "merged"        # 已被合并到父单元
    EXCLUDED = "excluded"    # 用户手动排除


class MatchType(str, Enum):
    """文件匹配类型。"""
    MD5 = "md5"              # 精确 MD5 匹配
    PHASH = "phash"          # 感知哈希匹配
    DHASH = "dhash"          # 差异哈希匹配
    FACE = "face"            # 人脸特征匹配


class ResolutionStatus(str, Enum):
    """查重结果的处理状态。"""
    PENDING = "pending"      # 待处理
    KEEP_A = "keep_a"        # 保留单元 A 的文件
    KEEP_B = "keep_b"        # 保留单元 B 的文件
    MERGE = "merge"          # 合并两个单元
    WHITELIST = "whitelist"  # 加入白名单
    IGNORE = "ignore"        # 暂时忽略


class MessageType(str, Enum):
    """消息类型。"""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    DEDUP_ALERT = "dedup_alert"  # 查重提醒


class ScanStatus(str, Enum):
    """扫描会话状态。"""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WhitelistMatchType(str, Enum):
    """白名单匹配类型。"""
    PATH = "path"            # 路径精确匹配
    UNIT = "unit"            # 资源单元匹配
    EXTENSION = "extension"  # 扩展名匹配


# ========== 文件大小单位 ==========
SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]

# ========== 缩略图相关常量 ==========
THUMBNAIL_FILENAME_TEMPLATE = "{file_id}_thumb.{fmt}"
"""缩略图文件命名模板：<文件ID>_缩略图.<格式>"""

# ========== 哈希相关常量 ==========
MD5_HEX_LENGTH = 32
"""MD5 十六进制字符串长度。"""

HASH_READ_CHUNK_SIZE = 64 * 1024  # 64KB
"""计算 MD5 时的分块读取大小。"""

# ========== 视频处理常量 ==========
VIDEO_MIN_FRAME_BRIGHTNESS = 10.0
"""视频帧最低平均亮度阈值，低于此值的帧视为黑帧跳过。"""

# ========== API 相关常量 ==========
API_PREFIX = "/api"
API_TITLE = "影视资源管理器 API"
API_VERSION = "0.1.0"
API_DESCRIPTION = "本地影视资源管理查重工具的 HTTP API，为安卓手机端预留。"
