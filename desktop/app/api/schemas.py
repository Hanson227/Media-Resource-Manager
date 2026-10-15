# -*- coding: utf-8 -*-
"""
API 数据模式（Pydantic 请求/响应模型）。

作为 Web/移动端 API 的契约定义，路由层将数据库结果映射到这些模型返回。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.services.file_ops import MODE_PATTERN, MODE_TRASH
from app.utils.constants import ResolutionStatus


# ============================================================
# 通用
# ============================================================

class StatusResponse(BaseModel):
    """通用状态响应。"""
    success: bool = True
    message: str = "操作成功"


class PaginationParams(BaseModel):
    """分页参数。"""
    page: int = Field(default=1, ge=1, description="页码（从 1 开始）")
    per_page: int = Field(default=50, ge=1, le=200, description="每页数量")


# ============================================================
# 文件相关
# ============================================================

class FileItem(BaseModel):
    """文件列表项。"""
    id: int
    filename: str
    media_type: str              # 'image' | 'video'
    size_bytes: int
    width: Optional[int] = None
    height: Optional[int] = None
    md5_hash: Optional[str] = None
    thumbnail_url: Optional[str] = None  # 缩略图访问路径


class FileListResponse(BaseModel):
    """文件列表响应。"""
    files: list[FileItem] = []
    total: int = 0
    page: int = 1
    per_page: int = 50


class BatchDeleteRequest(BaseModel):
    """批量删除请求。"""
    file_ids: list[int] = Field(..., min_length=1, description="要删除的文件 ID 列表")
    mode: str = Field(
        default=MODE_TRASH,
        pattern=MODE_PATTERN,
        description="trash=移至回收站（默认，与桌面端一致）；record=仅移除媒体库记录；"
                    "delete=永久删除磁盘文件（不可恢复，仅在回收站不可用时由用户明确选择）",
    )


class BatchDeleteFailure(BaseModel):
    """批量删除中单条失败的原因。"""
    file_id: int
    reason: str


class BatchDeleteResponse(BaseModel):
    """批量删除响应（部分成功也返回 200，逐条给出失败原因）。"""
    success: bool = True
    deleted: list[int] = []
    failed: list[BatchDeleteFailure] = []
    message: str = ""


class FileDetailResponse(BaseModel):
    """单个文件详情。"""
    id: int
    filename: str
    path: str
    media_type: str
    extension: str
    size_bytes: int
    width: Optional[int] = None
    height: Optional[int] = None
    duration_ms: Optional[int] = None
    md5_hash: Optional[str] = None
    phash: Optional[str] = None
    dhash: Optional[str] = None
    resource_unit_id: Optional[int] = None
    thumbnail_url: Optional[str] = None
    indexed_at: Optional[datetime] = None


# ============================================================
# 资源单元相关
# ============================================================

class UnitItem(BaseModel):
    """资源单元列表项。"""
    id: int
    name: str
    path: str
    file_count: int
    total_size: int
    is_manual: bool = False
    is_starred: bool = False
    status: str = "active"
    library_root_id: Optional[int] = None
    library_root_name: Optional[str] = None
    cover_file_id: Optional[int] = None
    created_at: Optional[str] = None
    content_modified_at: Optional[str] = None
    """单元内媒体文件的最新真实修改时间（ISO 格式）；None 表示未采集。"""


class UnitListResponse(BaseModel):
    """资源单元列表。"""
    units: list[UnitItem] = []


# ============================================================
# 查重相关
# ============================================================

class DedupResultItem(BaseModel):
    """查重结果列表项。"""
    id: int
    unit_a_id: int
    unit_b_id: int
    unit_a_name: str
    unit_b_name: str
    similarity_score: float
    match_count: int
    """计入判定的文件对数（不含人脸线索）。"""
    match_types: str = ""
    match_level: str = "duplicate"
    evidence_kind: str = "file"
    """证据来源：file（有文件重叠）/ face（只有人脸线索）。"""
    face_hint_count: int = 0
    """人脸线索条数（match_count 已不含人脸，故单独给出）。"""
    total_files_a: int = 0
    total_files_b: int = 0
    overlap_ratio: float = 0.0
    """包含度 = match_count / min(|A|, |B|)，用于展示"29/33 个文件重叠"。"""
    stale: bool = False
    """是否是旧规则（升级前）算出来的结论：True 时客户端应提示"需要重新查重"，
    且不要展示 match_count/overlap（旧口径的数字没有意义）。"""
    is_resolved: bool = False
    resolution: str = "pending"
    created_at: Optional[datetime] = None


class DedupListResponse(BaseModel):
    """查重结果列表。"""
    results: list[DedupResultItem] = []
    total: int = 0


class DedupGroupItem(BaseModel):
    """查重结果的"左侧单元"分组（列表页二级分组的父级）。"""
    anchor_unit_id: int
    anchor_unit_name: str = ""
    anchor_cover_file_id: Optional[int] = None
    pair_count: int = 0
    max_similarity: float = 0.0
    match_types: str = ""
    match_level: str = "related"
    evidence_kind: str = "file"
    stale: bool = False
    """组内是否含旧规则算出的结论（升级后未重跑查重）。"""


class DedupGroupListResponse(BaseModel):
    """查重结果分组列表。"""
    groups: list[DedupGroupItem] = []
    total: int = 0


class DedupFaceScanRequest(BaseModel):
    """人脸精查 / 补扫请求体。"""
    scope: str = Field(
        default="candidates",
        pattern="^(candidates|all)$",
        description="candidates=只重扫上一轮出现人脸线索的候选视频；all=全部未按新策略扫描的视频",
    )
    run_dedup: bool = Field(
        default=True,
        description="重扫完成后是否自动重跑一次查重比对（否则结果仍是旧的）",
    )


class DedupMergeRequest(BaseModel):
    """合并重复单元请求体：保留一侧文件夹，另一侧独有文件移入、重复文件进回收站。"""
    keep: str = Field(
        ...,
        pattern="^(a|b)$",
        description="保留哪一侧的文件夹作为合并目标（另一侧的独有文件移动过去）",
    )
    retain_matches: list[int] = Field(
        default=[],
        description="「两份都保留」的匹配对中，被并入侧的文件 ID 列表 —— "
                    "这些文件不进回收站，而是同样移动到保留侧（同名时自动重命名）",
    )


class DedupMergeResponse(BaseModel):
    """合并结果（部分失败也返回 200，失败明细供客户端重试/展示）。"""
    success: bool = True
    message: str = "合并完成"
    kept_unit_id: int
    merged_unit_id: int
    moved_count: int = 0
    trashed_count: int = 0
    renamed: list[dict] = []
    """[{"file_id", "original", "renamed"}] 重名冲突自动改名的明细。"""
    failed: list[dict] = []
    """[{"file_id", "filename", "reason"}] 逐文件失败明细。"""
    merged_dir_removed: bool = False
    """被并入侧的空目录是否已删除（目录非空时保留，不会强删）。"""


class DedupResolveRequest(BaseModel):
    """处理查重结果的请求体（可选值以 ResolutionStatus 枚举为单一来源）。"""
    resolution: str = Field(
        ...,
        pattern="^(" + "|".join(
            e.value for e in ResolutionStatus if e.value != ResolutionStatus.PENDING.value
        ) + ")$",
    )
    """处理方式。"""


# ============================================================
# 消息相关
# ============================================================

class MessageItem(BaseModel):
    """消息列表项。"""
    id: int
    msg_type: str
    title: str
    body: Optional[str] = None
    is_read: bool = False
    created_at: Optional[datetime] = None


class MessageListResponse(BaseModel):
    """消息列表。"""
    messages: list[MessageItem] = []
    unread_count: int = 0


# ============================================================
# 扫描相关
# ============================================================

class ScanRequest(BaseModel):
    """扫描请求。

    二选一：`path`（直接给服务端目录）或 `root_id`（扫已有媒体库根）。
    """
    path: Optional[str] = Field(default=None, description="要扫描的目录（服务端绝对路径）")
    root_id: Optional[int] = Field(default=None, description="已有媒体库根 ID")
    force_new_root: bool = Field(
        default=False,
        description="同尾旧根不可达时，是否明确按新库扫描（默认 False=返回 409 要求先修路径）")


class ScanTaskResponse(BaseModel):
    """长任务受理响应（配合 /api/library/scan/{task_id} 轮询）。"""
    task_id: str
    status: str = "queued"
    path: str = ""
    message: str = ""


# ============================================================
# 媒体库 / 卷 / 路径修复
# ============================================================

class VolumeItem(BaseModel):
    """一个就绪的卷（盘符）。"""
    path: str
    label: str = ""
    serial: str = ""
    total: int = 0
    free: int = 0
    contains_media: bool = False


class VolumeListResponse(BaseModel):
    volumes: list[VolumeItem] = []


class BrowseEntry(BaseModel):
    """目录浏览项。"""
    name: str
    path: str
    is_media_dir: bool = False
    """该目录浅层是否直接含媒体文件（用来快速挑选媒体根目录）。"""
    media_count: int = 0


class BrowseResponse(BaseModel):
    """目录浏览响应（服务端文件系统，供 Web 端选目录）。"""
    path: str
    parent: Optional[str] = None
    entries: list[BrowseEntry] = []
    is_media_dir: bool = False
    media_count: int = 0
    error: str = ""


class RemapCandidateItem(BaseModel):
    """一个可能的"旧路径新位置"候选。"""
    path: str
    kind: str = "same_relative"
    media_count: int = 0
    confidence: str = "high"


class LibraryRootItem(BaseModel):
    """媒体库根目录（含可达性与修复候选）。"""
    id: int
    path: str
    enabled: bool = True
    reachable: bool = True
    volume_present: bool = True
    reason: str = ""
    reason_text: str = ""
    unit_count: int = 0
    candidates: list[RemapCandidateItem] = []


class LibraryRootListResponse(BaseModel):
    roots: list[LibraryRootItem] = []


class AddRootRequest(BaseModel):
    """添加媒体库根目录。"""
    path: str = Field(..., description="服务端绝对路径")


class AddRootResponse(BaseModel):
    """添加结果；需要先修路径时返回 409 + remap 提示。"""
    success: bool = True
    root_id: Optional[int] = None
    created: bool = False
    needs_remap: bool = False
    old_path: Optional[str] = None
    candidates: list[RemapCandidateItem] = []
    message: str = ""


class RemapRequest(BaseModel):
    """路径改库请求（dry_run=True 只预演）。"""
    old_prefix: str = Field(..., description="旧路径前缀，如 D:\\Media")
    new_prefix: str = Field(..., description="新路径前缀，如 E:\\Media")
    dry_run: bool = Field(default=True, description="只预演不改动")
    include_whitelist: bool = Field(default=False, description="是否一并改写白名单路径规则")


class RemapPlanModel(BaseModel):
    """改库预演结果。"""
    old_prefix: str = ""
    new_prefix: str = ""
    counts: dict = {}
    total_rows: int = 0
    conflicts: list[dict] = []
    thumbnail_meta_hits: int = 0
    old_exists: bool = False
    new_exists: bool = False
    same: bool = False
    blocking: bool = False
    reason: str = ""


class RemapResponse(BaseModel):
    """改库响应（dry_run 时 updated 为空）。"""
    success: bool = False
    dry_run: bool = True
    plan: Optional[RemapPlanModel] = None
    updated: dict = {}
    thumbnail_meta: int = 0
    backup: Optional[str] = None
    error: str = ""


# ============================================================
# 设置 / 数据目录
# ============================================================

class SettingsResponse(BaseModel):
    """运行设置与数据位置。"""
    profile: str = "full"
    data_root: str = ""
    data_root_status: str = ""
    expected_data_root: str = ""
    db_path: str = ""
    thumbnail_cache_dir: str = ""
    thumbnail_cache_files: int = 0
    thumbnail_cache_bytes: int = 0
    db_bytes: int = 0
    config_path: str = ""
    restart_required_hint: str = ""
    api_host: str = ""
    """当前监听地址：0.0.0.0 表示所有网卡（手机可访问），127.0.0.1 只有本机能访问。"""
    api_port: int = 0
    lan_urls: list[str] = []
    """手机/平板可访问的完整 URL（第一个恒为本机地址）。"""
    loopback_only: bool = False
    """只监听回环地址时为 True —— 此时手机一定连不上，界面必须提示。"""
    capabilities: dict = {}
    """能力快照（哪个包、有哪些可选能力）。"""
    capability_notes: list[str] = []
    """缺失能力的可读说明（"未打包 OpenCV：视频缩略图不可用"）。"""


class DataRootRequest(BaseModel):
    """迁移/接管数据根。"""
    path: str = Field(..., description="新的数据根目录（如 E:\\MediaData）")
    mode: str = Field(default="copy", pattern="^(copy|move|adopt)$",
                      description=("copy=保留源（默认）；move=校验通过后删除源；"
                                   "adopt=接管该目录里已有的数据（不复制、不覆盖）。"
                                   "目标目录已有数据根时只接受 adopt"))


class DataRootProbeResponse(BaseModel):
    """探测一个目录"是不是已有数据的数据根"（界面据此决定接管还是迁移）。"""
    path: str = ""
    exists: bool = False
    is_dir: bool = False
    writable: bool = False
    has_marker: bool = False
    data_root_id: str = ""
    db_path: str = ""
    db_exists: bool = False
    db_bytes: int = 0
    db_ok: bool = False
    db_detail: str = ""
    units: int = 0
    files: int = 0
    thumbnail_files: int = 0
    is_data_root: bool = False
    is_current: bool = False
    """就是当前正在用的数据根（此时不需要迁移/接管）。"""


class ThumbnailCacheRequest(BaseModel):
    """迁移缩略图缓存目录（可热切换，无需重启）。"""
    path: str = Field(..., description="新的缩略图缓存目录")
    mode: str = Field(default="copy", pattern="^(copy|move)$",
                      description="copy=保留源（默认）；move=拷贝成功后删除源文件")


class DataMigrationResponse(BaseModel):
    """数据目录迁移结果。"""
    success: bool = False
    target: str = ""
    mode: str = "copy"
    restart_required: bool = False
    steps: dict = {}
    message: str = ""


# ============================================================
# 缩略图响应
# ============================================================

class ThumbnailResponse(BaseModel):
    """缩略图访问响应。"""
    available: bool = False
    url: Optional[str] = None
    message: str = "缩略图服务尚未实现"
