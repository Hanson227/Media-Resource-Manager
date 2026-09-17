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


class EvidenceKind(str, Enum):
    """查重结果的证据来源 —— 决定它在界面上属于哪一类。

    "疑似相关"这一档里混着两类完全不同的东西：
      - FILE：真的有文件级匹配（部分重叠），值得人看一眼
      - FACE：只有人脸线索、零文件匹配（同一演员的不同作品），纯提醒
    人脸不计入杰卡德，所以这两类必须分开存、分开展示 ——
    否则 373 条"0% 相似"的同演员线索会把 7 条真实重叠淹没。
    """
    FILE = "file"
    FACE = "face"


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

FACE_SIMILARITY_THRESHOLD = 0.45
"""人脸特征余弦相似度阈值（默认比 SFace 官方值更严）。

旧实现是 OpenFace nn4 + 欧氏距离；换 SFace 后度量改为余弦相似度，
因此阈值语义从"欧氏距离越小越像"变为"余弦越大越像"。

为什么不用 SFace 官方值 0.363：官方值面向"同一人的两张正脸照片"的身份验证，
而这里是在**两个资源单元的全部文件之间**两两比对（同一演员的不同作品也算命中），
比对次数是数万级，0.363 会把大量"长得像"的人凑成对。实测本库：

    阈值      人脸匹配行    仍会入库的单元对
    0.363      1437        380
    0.450       382        ~57
    0.500       193        ~19
    0.600        95        ~11

人脸不参与查重判定（只作"同演员"线索），因此宁可严一点：线索少而准，
比给用户 380 条 0% 的噪音更有价值。用户可在桌面「查重设置」或 Web 查重页覆盖。
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

# ========== 查重分级常量 ==========
CONTAINMENT_MATCH_THRESHOLD = 0.85
"""包含度阈值：匹配数 / min(|A|, |B|) ≥ 此值视为"子集重复"。

杰卡德表达"两个目录几乎一样"，表达不了"一边是另一边的子集"。
实测本库最典型的真重复（29 个文件 MD5 完全一致、另一侧多出 12 个文件）
杰卡德只有 0.644（够不到 0.80），包含度 29/33 = 0.879 —— 只认杰卡德时
它会被降级成"疑似相关"，于是用户看到"疑似重复 0 组"。
"""

CONTAINMENT_MIN_MATCHES = 5
"""包含度判定的最少匹配数：小目录只有 2~3 个文件时包含度天然是 1.0，不足为凭。"""

CONTAINMENT_MIN_JACCARD = 0.10
"""包含度判定的杰卡德下限：挡住"9 个文件的目录完全被 2000 个文件的目录包含"。

此时包含度=1.0，但两边只有 0.4% 的文件重叠，属于"部分重叠"而不是"重复"。
"""

RELATED_MIN_OVERLAP = 0.02
""""疑似相关"的匹配比例下限（匹配数 / min(|A|, |B|)）。

`related_min_matches` 是绝对条数，在大单元里毫无意义：2000 个文件的单元
里凑巧 pHash 相近的 2 个文件就能凑成一对"0% 相关"。因此实际门槛取
max(related_min_matches, ceil(RELATED_MIN_OVERLAP × min(|A|, |B|)))。
"""

CONTAINMENT_STRONG_MATCH_TYPES = (MatchType.MD5.value, MatchType.VIDEO.value)
"""可支撑"子集重复"结论的强证据类型（而非仅 pHash/dHash 近似）。"""

DEDUP_RESULT_VERSION = 2
"""查重结果分级算法的版本号（写入 dedup_results.computed_version）。

规则一变，库里用旧规则算出来的行不会自己消失 —— 必须让人一眼看出"这是旧结论"。
0 = v7 之前的旧规则（只看杰卡德、match_count 含人脸证据），
1/2 = 逐次规则升级（2 = 包含度子集重复 + 证据来源分级 + 多帧人脸吻合）。
界面据此提示"需要重新查重"，而不是把旧数字（如 match_count 46 > 文件数 33）当结论展示。
"""

# ========== 视频人脸抽帧常量 ==========
FACE_SCAN_VERSION = 1
"""人脸抽帧策略版本号，存于 media_files.face_scan_version。

0 = 旧策略（视频只取中间 1 帧，不可复现且漏检严重）
1 = 新策略（定间隔多帧，见 plan_video_face_timestamps）
补扫任务只处理 face_scan_version < FACE_SCAN_VERSION 的视频。
"""

FACE_VIDEO_MAX_FRAMES = 10
"""单个视频最多做多少次人脸检测（默认值，可经 config.face_video_max_frames 覆盖）。

视频抽帧不是越密越好：YuNet 单帧约 82ms（长边 1280），10 帧已让本库
1142 个视频的一次性补扫达到十几分钟。帧数按"均匀铺满全片"分配，
因此长视频会自动放大间距而不是只扫开头。
"""

FACE_DETECT_MAX_SIDE_VIDEO = 1280
"""视频帧送进 YuNet 的长边上限。

曾经想降到 640 换取 3.7× 速度，但 constants.FACE_DETECT_MAX_SIDE 的实测数据显示
640 会损失约 22% 的人脸召回（长边 1280 检出 50 张 vs 640 检出 40 张）。
视频人脸抽帧的目的正是**提高召回**（旧策略只取中间一帧，17% 的视频才检出人脸），
因此这里保持 1280：宁可多花几分钟，也不牺牲检出率。
"""

# ========== API 相关常量 ==========
API_PREFIX = "/api"
API_TITLE = "影视资源管理器 API"
API_VERSION = "0.1.0"
API_DESCRIPTION = "本地影视资源管理查重工具的 HTTP API，为安卓手机端预留。"
