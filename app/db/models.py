# -*- coding: utf-8 -*-
"""
数据库 ORM 模型定义。

使用 SQLAlchemy DeclarativeBase 定义全部 10 张表。
所有字段名使用英文，注释使用中文。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, Float,
    DateTime, Text, BLOB, ForeignKey, Index, UniqueConstraint,
    CheckConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""
    pass


# ============================================================
# 1. 媒体库根目录表
# ============================================================
class MediaLibraryRoot(Base):
    """用户添加的媒体库顶级目录。"""

    __tablename__ = "media_library_roots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    path = Column(String(2048), nullable=False, unique=True)
    """媒体库根目录的绝对路径。"""

    enabled = Column(Boolean, nullable=False, default=True)
    """是否参与扫描和文件监控。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    # 关联
    resource_units = relationship("ResourceUnit", back_populates="library_root", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<MediaLibraryRoot id={self.id} path='{self.path}'>"


# ============================================================
# 2. 资源单元表
# ============================================================
class ResourceUnit(Base):
    """检测到的或用户手动定义的资源单元。"""

    __tablename__ = "resource_units"
    __table_args__ = (
        Index("ix_resource_units_path", "path"),
        Index("ix_resource_units_status", "status"),
        Index("ix_resource_units_library_root", "library_root_id"),
        CheckConstraint(
            "status IN ('active', 'merged', 'excluded')",
            name="ck_resource_units_status",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    path = Column(String(2048), nullable=False, unique=True)
    """资源单元文件夹的绝对路径。"""

    name = Column(String(512), nullable=False)
    """文件夹名称，用于界面显示。"""

    parent_id = Column(Integer, ForeignKey("resource_units.id", ondelete="SET NULL"), nullable=True)
    """父单元 ID，非空表示该单元已被合并到父单元。"""

    is_manual = Column(Boolean, nullable=False, default=False)
    """是否由用户手动创建（标记为资源单元）。"""

    is_starred = Column(Boolean, nullable=False, default=False)
    """用户星标标记，界面显示 ⭐ 图标。"""

    status = Column(String(16), nullable=False, default="active")
    """状态：active / merged / excluded。"""

    file_count = Column(Integer, nullable=False, default=0)
    """该单元内媒体文件总数。"""

    total_size = Column(BigInteger, nullable=False, default=0)
    """该单元内所有媒体文件的总字节数。"""

    library_root_id = Column(Integer, ForeignKey("media_library_roots.id", ondelete="CASCADE"), nullable=False)
    """所属媒体库根目录 ID。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    # 关联
    library_root = relationship("MediaLibraryRoot", back_populates="resource_units")
    media_files = relationship("MediaFile", back_populates="resource_unit", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<ResourceUnit id={self.id} name='{self.name}' files={self.file_count}>"


# ============================================================
# 3. 媒体文件表
# ============================================================
class MediaFile(Base):
    """发现的每一个媒体文件。"""

    __tablename__ = "media_files"
    __table_args__ = (
        Index("ix_media_files_path", "path"),
        Index("ix_media_files_md5", "md5_hash"),
        Index("ix_media_files_unit", "resource_unit_id"),
        Index("ix_media_files_type", "media_type"),
        Index("ix_media_files_ext", "extension"),
        CheckConstraint("media_type IN ('image', 'video')", name="ck_media_files_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    path = Column(String(2048), nullable=False, unique=True)
    """文件完整路径。"""

    filename = Column(String(512), nullable=False)
    """文件名（含扩展名）。"""

    extension = Column(String(16), nullable=False)
    """小写扩展名，如 '.jpg'。"""

    media_type = Column(String(8), nullable=False)
    """媒体类型：'image' 或 'video'。"""

    size_bytes = Column(BigInteger, nullable=False, default=0)
    """文件大小（字节）。"""

    width = Column(Integer, nullable=True)
    """图片/视频宽度（像素）。"""

    height = Column(Integer, nullable=True)
    """图片/视频高度（像素）。"""

    duration_ms = Column(Integer, nullable=True)
    """视频时长（毫秒），图片为空。"""

    md5_hash = Column(String(32), nullable=True)
    """MD5 哈希值（32位十六进制），NULL 表示未计算。"""

    phash = Column(String(64), nullable=True)
    """感知哈希（pHash）十六进制字符串。"""

    dhash = Column(String(64), nullable=True)
    """差异哈希（dHash）十六进制字符串。"""

    resource_unit_id = Column(Integer, ForeignKey("resource_units.id", ondelete="CASCADE"), nullable=False)
    """所属资源单元 ID。"""

    indexed_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    # 关联
    resource_unit = relationship("ResourceUnit", back_populates="media_files")
    face_vectors = relationship("FaceVector", back_populates="media_file", cascade="all, delete-orphan")
    video_frames = relationship("VideoFrame", back_populates="media_file", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<MediaFile id={self.id} filename='{self.filename}' type={self.media_type}>"


# ============================================================
# 4. 人脸向量表
# ============================================================
class FaceVector(Base):
    """每张检测到的人脸的 128 维特征嵌入。"""

    __tablename__ = "face_vectors"
    __table_args__ = (
        Index("ix_face_vectors_file", "file_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    file_id = Column(Integer, ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False)
    """关联的媒体文件 ID。"""

    vector_data = Column(BLOB, nullable=False)
    """128 维 float32 向量，小端字节序存储（128 × 4 = 512 字节）。"""

    face_index = Column(Integer, nullable=False, default=0)
    """人脸序号（0=第一张脸，1=第二张脸...）。"""

    bbox_x = Column(Integer, nullable=True)
    bbox_y = Column(Integer, nullable=True)
    bbox_w = Column(Integer, nullable=True)
    bbox_h = Column(Integer, nullable=True)
    """人脸边界框坐标（像素）。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)

    # 关联
    media_file = relationship("MediaFile", back_populates="face_vectors")

    def __repr__(self) -> str:
        return f"<FaceVector id={self.id} file_id={self.file_id} face_index={self.face_index}>"


# ============================================================
# 5. 视频帧表
# ============================================================
class VideoFrame(Base):
    """视频每 5 秒抽取帧的感知哈希。"""

    __tablename__ = "video_frames"
    __table_args__ = (
        Index("ix_video_frames_file", "file_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    file_id = Column(Integer, ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False)
    """关联的视频文件 ID。"""

    timestamp_ms = Column(Integer, nullable=False)
    """帧在视频中的时间位置（毫秒）。"""

    phash = Column(String(64), nullable=False)
    """该帧的感知哈希值。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)

    # 关联
    media_file = relationship("MediaFile", back_populates="video_frames")

    def __repr__(self) -> str:
        return f"<VideoFrame id={self.id} file_id={self.file_id} ts={self.timestamp_ms}ms>"


# ============================================================
# 6. 查重结果表
# ============================================================
class DedupResult(Base):
    """两个资源单元的查重比对结果。"""

    __tablename__ = "dedup_results"
    __table_args__ = (
        Index("ix_dedup_results_units", "unit_a_id", "unit_b_id"),
        UniqueConstraint("unit_a_id", "unit_b_id", name="uq_dedup_pair"),
        Index("ix_dedup_results_score", "similarity_score"),
        CheckConstraint(
            "resolution IN ('pending', 'keep_a', 'keep_b', 'merge', 'whitelist', 'ignore')",
            name="ck_dedup_results_resolution",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    unit_a_id = Column(Integer, ForeignKey("resource_units.id", ondelete="CASCADE"), nullable=False)
    """资源单元 A 的 ID。"""

    unit_b_id = Column(Integer, ForeignKey("resource_units.id", ondelete="CASCADE"), nullable=False)
    """资源单元 B 的 ID。"""

    similarity_score = Column(Float, nullable=False)
    """杰卡德指数 [0.0, 1.0]。"""

    match_count = Column(Integer, nullable=False, default=0)
    """匹配到的文件对数。"""

    total_files_a = Column(Integer, nullable=False, default=0)
    total_files_b = Column(Integer, nullable=False, default=0)
    """两个单元的文件总数。"""

    match_types = Column(String(128), nullable=True)
    """逗号分隔的匹配类型，如 'md5,phash,face'。"""

    is_resolved = Column(Boolean, nullable=False, default=False)
    """是否已处理。"""

    resolution = Column(String(16), nullable=False, default="pending")
    """处理结果。"""

    resolved_by = Column(String(64), nullable=True)
    """处理人：'auto' 或 'user'。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    # 关联
    file_matches = relationship("DedupFileMatch", back_populates="dedup_result", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<DedupResult id={self.id} A={self.unit_a_id} B={self.unit_b_id} score={self.similarity_score:.3f}>"


# ============================================================
# 7. 查重文件匹配表
# ============================================================
class DedupFileMatch(Base):
    """查重结果中具体的文件级匹配对。"""

    __tablename__ = "dedup_file_matches"
    __table_args__ = (
        Index("ix_dedup_file_matches_result", "dedup_result_id"),
        UniqueConstraint("dedup_result_id", "file_a_id", "file_b_id", name="uq_file_pair"),
        CheckConstraint(
            "match_type IN ('md5', 'phash', 'dhash', 'face')",
            name="ck_dedup_file_matches_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    dedup_result_id = Column(Integer, ForeignKey("dedup_results.id", ondelete="CASCADE"), nullable=False)
    """所属查重结果 ID。"""

    file_a_id = Column(Integer, ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False)
    file_b_id = Column(Integer, ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False)
    """文件 A 和文件 B 的 ID。"""

    similarity_score = Column(Float, nullable=False)
    """相似度得分。"""

    match_type = Column(String(8), nullable=False)
    """匹配类型：md5 / phash / dhash / face。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)

    # 关联
    dedup_result = relationship("DedupResult", back_populates="file_matches")

    def __repr__(self) -> str:
        return f"<DedupFileMatch id={self.id} type={self.match_type} score={self.similarity_score:.3f}>"


# ============================================================
# 8. 消息表
# ============================================================
class Message(Base):
    """消息中心/通知收件箱。"""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_unread", "is_read", "is_dismissed"),
        Index("ix_messages_type", "msg_type"),
        Index("ix_messages_created", "created_at"),
        CheckConstraint(
            "msg_type IN ('info', 'warning', 'error', 'dedup_alert')",
            name="ck_messages_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    msg_type = Column(String(16), nullable=False)
    """消息类型：info / warning / error / dedup_alert。"""

    title = Column(String(256), nullable=False)
    """消息标题。"""

    body = Column(Text, nullable=True)
    """消息正文。"""

    related_unit_id = Column(Integer, ForeignKey("resource_units.id", ondelete="SET NULL"), nullable=True)
    """关联的资源单元 ID。"""

    action_data = Column(Text, nullable=True)
    """JSON 格式的操作按钮数据。"""

    is_read = Column(Boolean, nullable=False, default=False)
    """是否已读。"""

    is_dismissed = Column(Boolean, nullable=False, default=False)
    """是否已忽略/关闭。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)

    def __repr__(self) -> str:
        return f"<Message id={self.id} type={self.msg_type} title='{self.title[:30]}'>"


# ============================================================
# 9. 白名单表
# ============================================================
class Whitelist(Base):
    """用户白名单，匹配的路径/单元/扩展名不会被触发查重提醒。"""

    __tablename__ = "whitelist"
    __table_args__ = (
        CheckConstraint(
            "match_type IN ('path', 'unit', 'extension')",
            name="ck_whitelist_type",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    pattern = Column(String(1024), nullable=False)
    """匹配模式（路径或正则表达式）。"""

    is_regex = Column(Boolean, nullable=False, default=False)
    """是否为正则表达式。"""

    match_type = Column(String(8), nullable=False, default="path")
    """匹配类型：path / unit / extension。"""

    note = Column(String(256), nullable=True)
    """用户备注。"""

    created_at = Column(DateTime, nullable=False, default=datetime.now)

    def __repr__(self) -> str:
        return f"<Whitelist id={self.id} pattern='{self.pattern[:50]}'>"


# ============================================================
# 10. 扫描记录表
# ============================================================
class ScanSession(Base):
    """扫描操作审计日志。"""

    __tablename__ = "scan_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled')",
            name="ck_scan_sessions_status",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    """自增主键。"""

    started_at = Column(DateTime, nullable=False, default=datetime.now)
    """扫描开始时间。"""

    completed_at = Column(DateTime, nullable=True)
    """扫描完成时间。"""

    files_scanned = Column(Integer, nullable=False, default=0)
    """已扫描文件数。"""

    new_files = Column(Integer, nullable=False, default=0)
    """新发现文件数。"""

    updated_files = Column(Integer, nullable=False, default=0)
    """更新文件数。"""

    errors_count = Column(Integer, nullable=False, default=0)
    """错误文件数。"""

    status = Column(String(12), nullable=False, default="running")
    """扫描状态：running / completed / failed / cancelled。"""

    error_log = Column(Text, nullable=True)
    """JSON 格式的错误列表。"""

    def __repr__(self) -> str:
        return f"<ScanSession id={self.id} status={self.status} files={self.files_scanned}>"
