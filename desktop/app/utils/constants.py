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
    # 视频抽帧级匹配。追加在末尾以保持既有 CHECK 约束顺序不变。
    # 独立取值是为了让界面能区分"整图 pHash 命中"与"视频帧命中"——
    # 后者证据最弱，混记为 phash 会让用户看不出结论来源。
    VIDEO = "video"


class MatchLevel(str, Enum):
    """查重命中等级 —— 决定是否建议处置。

    用户诉求：同演员/同场景的不同片子也想知道，但不打算删。
    因此把"重复"与"相关"分成两级：
      - DUPLICATE：单元级重复（杰卡德达标）→ 建议保留一份
      - RELATED  ：疑似相关（人脸/帧/部分重叠）→ 仅提醒，不建议处置
    """
    DUPLICATE = "duplicate"
    RELATED = "related"


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

# ========== 人脸相关常量 ==========
FACE_DETECT_MODEL = "face_detection_yunet_2023mar.onnx"
"""YuNet 人脸检测模型文件名（OpenCV Zoo，约 230 KB）。"""

FACE_RECOGNIZE_MODEL = "face_recognition_sface_2021dec.onnx"
"""SFace 人脸特征模型文件名，输出 128 维向量（约 37 MB）。"""

FACE_SIMILARITY_THRESHOLD = 0.363
"""人脸特征余弦相似度阈值（SFace 官方值，LFW 上 99.40% 准确率）。

旧实现是 OpenFace nn4 + 欧氏距离；换 SFace 后度量改为余弦相似度，
因此阈值语义从"欧氏距离越小越像"变为"余弦越大越像"。
"""

FACE_DETECT_MAX_SIDE = 1280
"""送进 YuNet 的图像长边上限（像素），超过则先等比缩小。

YuNet 的感受野是固定的，输入尺寸既决定耗时也影响召回 —— 大图反而不该原样喂进去。
在真实图片上的实测（30 张样本，均取自本库）：

    长边上限   人脸总数   单张耗时
    原尺寸        11      838.7ms     ← 长边>2560 的大图
    1280          16      118.3ms     ← 更快 **且** 检出更多

    原尺寸        51      126.8ms     ← 长边≈1280 的手机照片
    1280          50        —          （-2%）
    960           45        —          （-12%）
    640           40        —          （-22%）

即：大图（本库占 27%，最大 8064）缩到 1280 是双赢；小图（占 47%）不受影响；
只有 1281~2560 这一带的图会被缩小并损失约 2% 召回。
人脸只用于"疑似相关"线索，故优先保召回而不是追求极限速度。
"""

# ========== API 相关常量 ==========
API_PREFIX = "/api"
API_TITLE = "影视资源管理器 API"
API_VERSION = "0.1.0"
API_DESCRIPTION = "本地影视资源管理查重工具的 HTTP API，为安卓手机端预留。"
