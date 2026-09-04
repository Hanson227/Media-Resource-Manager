# -*- coding: utf-8 -*-
"""
API 数据模式（Pydantic 请求/响应模型）。

作为 Web/移动端 API 的契约定义，路由层将数据库结果映射到这些模型返回。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

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
    match_types: str = ""
    is_resolved: bool = False
    resolution: str = "pending"
    created_at: Optional[datetime] = None


class DedupListResponse(BaseModel):
    """查重结果列表。"""
    results: list[DedupResultItem] = []
    total: int = 0


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
    """扫描请求。"""
    path: str = Field(..., description="要扫描的目录路径")


class ScanResponse(BaseModel):
    """扫描响应。"""
    status: str = "not_implemented"
    message: str = "扫描功能尚未通过 API 对外开放"


# ============================================================
# 缩略图响应
# ============================================================

class ThumbnailResponse(BaseModel):
    """缩略图访问响应。"""
    available: bool = False
    url: Optional[str] = None
    message: str = "缩略图服务尚未实现"
