# -*- coding: utf-8 -*-
"""
数据库查询函数集合。

所有持久化操作集中在此，提供命名查询函数而非在业务代码中直接写 SQL。
每个函数接收 SQLAlchemy Session 作为第一个参数（依赖注入模式）。
"""

from datetime import datetime
from typing import Optional, Sequence, Any

from sqlalchemy import func, and_, or_
from sqlalchemy.orm import Session

from app.db.models import (
    MediaLibraryRoot, ResourceUnit, MediaFile, FaceVector,
    VideoFrame, DedupResult, DedupFileMatch, Message,
    Whitelist, ScanSession,
)


# ============================================================
# 媒体库根目录
# ============================================================

def add_library_root(session: Session, path: str) -> MediaLibraryRoot:
    """添加一个媒体库根目录。"""
    root = MediaLibraryRoot(path=path)
    session.add(root)
    session.flush()
    return root


def get_all_roots(session: Session) -> list[MediaLibraryRoot]:
    """获取所有媒体库根目录。"""
    return session.query(MediaLibraryRoot).all()


def get_enabled_roots(session: Session) -> list[MediaLibraryRoot]:
    """获取所有启用的媒体库根目录。"""
    return session.query(MediaLibraryRoot).filter(MediaLibraryRoot.enabled == True).all()


def get_root_by_path(session: Session, path: str) -> Optional[MediaLibraryRoot]:
    """按路径查找媒体库根目录。"""
    return session.query(MediaLibraryRoot).filter(MediaLibraryRoot.path == path).first()


def remove_library_root(session: Session, root_id: int) -> None:
    """删除媒体库根目录及其关联的所有数据（级联删除）。"""
    root = session.query(MediaLibraryRoot).filter(MediaLibraryRoot.id == root_id).first()
    if root:
        session.delete(root)


def set_root_enabled(session: Session, root_id: int, enabled: bool) -> None:
    """启用或禁用媒体库根目录。"""
    session.query(MediaLibraryRoot).filter(
        MediaLibraryRoot.id == root_id
    ).update({"enabled": enabled, "updated_at": datetime.now()})


# ============================================================
# 资源单元
# ============================================================

def get_all_active_units(session: Session) -> list[ResourceUnit]:
    """获取所有活跃状态的资源单元。"""
    return session.query(ResourceUnit).filter(ResourceUnit.status == "active").all()


def get_unit_by_id(session: Session, unit_id: int) -> Optional[ResourceUnit]:
    """按 ID 获取资源单元。"""
    return session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).first()


def get_unit_by_path(session: Session, path: str) -> Optional[ResourceUnit]:
    """按路径获取资源单元。"""
    return session.query(ResourceUnit).filter(ResourceUnit.path == path).first()


def get_units_by_root(session: Session, root_id: int) -> list[ResourceUnit]:
    """获取指定根目录下的所有资源单元。"""
    return session.query(ResourceUnit).filter(
        ResourceUnit.library_root_id == root_id,
        ResourceUnit.status == "active",
    ).all()


def get_child_units(session: Session, parent_id: int) -> list[ResourceUnit]:
    """获取被合并到指定父单元的所有子单元。"""
    return session.query(ResourceUnit).filter(ResourceUnit.parent_id == parent_id).all()


def create_unit(session: Session, path: str, name: str, library_root_id: int,
                is_manual: bool = False, file_count: int = 0,
                total_size: int = 0) -> ResourceUnit:
    """创建新的资源单元记录。"""
    unit = ResourceUnit(
        path=path, name=name, library_root_id=library_root_id,
        is_manual=is_manual, file_count=file_count, total_size=total_size,
    )
    session.add(unit)
    session.flush()
    return unit


def update_unit_stats(session: Session, unit_id: int, file_count: int, total_size: int) -> None:
    """更新资源单元的文件统计信息。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "file_count": file_count,
        "total_size": total_size,
        "updated_at": datetime.now(),
    })


def mark_unit_merged(session: Session, unit_id: int, parent_id: int) -> None:
    """将资源单元标记为已合并到父单元。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": "merged",
        "parent_id": parent_id,
        "updated_at": datetime.now(),
    })


def mark_unit_manual(session: Session, unit_id: int, is_manual: bool = True) -> None:
    """设置资源单元的手动标记状态。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "is_manual": is_manual,
        "updated_at": datetime.now(),
    })


def set_unit_cover(session: Session, unit_id: int, cover_path: str) -> None:
    """设置资源单元的封面图片。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "cover_path": cover_path,
        "updated_at": datetime.now(),
    })


def clear_unit_cover(session: Session, unit_id: int) -> None:
    """清除资源单元的手动封面。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "cover_path": None,
        "updated_at": datetime.now(),
    })


def set_unit_starred(session: Session, unit_id: int, starred: bool = True) -> None:
    """设置资源单元的星标状态。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "is_starred": starred,
        "updated_at": datetime.now(),
    })


def rename_unit(session: Session, unit_id: int, new_name: str) -> None:
    """重命名资源单元（仅修改显示名称，不影响磁盘路径）。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "name": new_name,
        "updated_at": datetime.now(),
    })


def get_starred_units(session: Session) -> list[ResourceUnit]:
    """获取所有已收藏的资源单元。"""
    return session.query(ResourceUnit).filter(
        ResourceUnit.is_starred == True,
        ResourceUnit.status == "active",
    ).all()


def mark_unit_excluded(session: Session, unit_id: int) -> None:
    """将资源单元标记为排除状态。"""
    unit = session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).first()
    if unit:
        unit.status = "excluded"
        unit.updated_at = datetime.now()
        session.flush()


def get_excluded_units(session: Session) -> list[ResourceUnit]:
    """获取所有已排除的资源单元。"""
    return session.query(ResourceUnit).filter(
        ResourceUnit.status == "excluded"
    ).all()


def unexclude_unit(session: Session, unit_id: int) -> None:
    """将已排除的资源单元恢复为活跃状态。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": "active",
        "updated_at": datetime.now(),
    })


def unmerge_unit(session: Session, unit_id: int) -> None:
    """取消合并，将资源单元恢复为活跃状态。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": "active",
        "parent_id": None,
        "updated_at": datetime.now(),
    })


def get_unit_file_count(session: Session, unit_id: int) -> int:
    """获取指定资源单元内的文件数量。"""
    return session.query(func.count(MediaFile.id)).filter(
        MediaFile.resource_unit_id == unit_id
    ).scalar() or 0


# ============================================================
# 媒体文件
# ============================================================

def get_files_by_unit(session: Session, unit_id: int) -> list[MediaFile]:
    """获取指定资源单元内的所有媒体文件。"""
    return session.query(MediaFile).filter(MediaFile.resource_unit_id == unit_id).all()


def get_file_by_id(session: Session, file_id: int) -> Optional[MediaFile]:
    """按 ID 获取媒体文件。"""
    return session.query(MediaFile).filter(MediaFile.id == file_id).first()


def get_file_by_path(session: Session, path: str) -> Optional[MediaFile]:
    """按路径获取媒体文件。"""
    return session.query(MediaFile).filter(MediaFile.path == path).first()


def get_files_by_md5(session: Session, md5_hash: str) -> list[MediaFile]:
    """按 MD5 哈希查找所有匹配的文件（用于精确查重）。"""
    return session.query(MediaFile).filter(MediaFile.md5_hash == md5_hash).all()


def get_files_without_hash(session: Session, hash_type: str = "md5",
                           limit: int = 1000) -> list[MediaFile]:
    """获取缺少指定哈希类型的文件列表。

    参数:
        hash_type: 'md5'/'phash'/'dhash'。
        limit: 最大返回数量。
    """
    column = getattr(MediaFile, f"{hash_type}_hash", None)
    if column is None:
        return []
    return session.query(MediaFile).filter(column == None).limit(limit).all()


def get_unindexed_files(session: Session, limit: int = 1000) -> list[MediaFile]:
    """获取所有活跃单元中未计算任何哈希的文件。"""
    return session.query(MediaFile).join(
        ResourceUnit, MediaFile.resource_unit_id == ResourceUnit.id
    ).filter(
        ResourceUnit.status == "active",
        or_(
            MediaFile.md5_hash == None,
            MediaFile.phash == None,
        )
    ).limit(limit).all()


def insert_media_file(session: Session, **kwargs: Any) -> MediaFile:
    """插入一条新媒体文件记录。"""
    mf = MediaFile(**kwargs)
    session.add(mf)
    session.flush()
    return mf


def update_file_hash(session: Session, file_id: int, **kwargs: Any) -> None:
    """更新媒体文件的哈希字段。

    关键字参数:
        md5_hash, phash, dhash, width, height, duration_ms 等。
    """
    kwargs["updated_at"] = datetime.now()
    session.query(MediaFile).filter(MediaFile.id == file_id).update(kwargs)


def update_file_unit(session: Session, file_id: int, unit_id: int) -> None:
    """更新媒体文件所属的资源单元。"""
    session.query(MediaFile).filter(MediaFile.id == file_id).update({
        "resource_unit_id": unit_id,
        "updated_at": datetime.now(),
    })


def delete_media_file(session: Session, file_id: int) -> None:
    """删除一条媒体文件记录（级联删除关联的人脸向量和视频帧）。"""
    mf = session.query(MediaFile).filter(MediaFile.id == file_id).first()
    if mf:
        session.delete(mf)


def get_file_count_by_unit_ids(session: Session, unit_ids: list[int]) -> dict[int, int]:
    """批量查询多个资源单元的文件数量。"""
    results = session.query(
        MediaFile.resource_unit_id,
        func.count(MediaFile.id),
    ).filter(MediaFile.resource_unit_id.in_(unit_ids)).group_by(MediaFile.resource_unit_id).all()
    return {unit_id: count for unit_id, count in results}


# ============================================================
# 人脸向量
# ============================================================

def insert_face_vector(session: Session, file_id: int, vector_data: bytes,
                       face_index: int = 0, bbox: Optional[tuple] = None) -> FaceVector:
    """插入一条人脸向量记录。

    参数:
        file_id: 关联的媒体文件 ID。
        vector_data: 128 维 float32 字节数据。
        face_index: 人脸序号。
        bbox: 边界框 (x, y, w, h)。
    """
    kwargs = {
        "file_id": file_id,
        "vector_data": vector_data,
        "face_index": face_index,
    }
    if bbox:
        kwargs.update({"bbox_x": bbox[0], "bbox_y": bbox[1],
                       "bbox_w": bbox[2], "bbox_h": bbox[3]})

    fv = FaceVector(**kwargs)
    session.add(fv)
    session.flush()
    return fv


def get_face_vectors_by_file(session: Session, file_id: int) -> list[FaceVector]:
    """获取指定文件的所有人脸向量。"""
    return session.query(FaceVector).filter(FaceVector.file_id == file_id).all()


def get_all_face_vectors(session: Session) -> list[FaceVector]:
    """获取数据库中所有人脸向量（用于全局人脸比对）。"""
    return session.query(FaceVector).all()


# ============================================================
# 视频帧
# ============================================================

def insert_video_frame(session: Session, file_id: int, timestamp_ms: int,
                       phash: str) -> VideoFrame:
    """插入视频帧记录。"""
    vf = VideoFrame(file_id=file_id, timestamp_ms=timestamp_ms, phash=phash)
    session.add(vf)
    session.flush()
    return vf


def insert_video_frames_bulk(session: Session, frames: list[dict]) -> None:
    """批量插入视频帧记录。"""
    for frame in frames:
        vf = VideoFrame(**frame)
        session.add(vf)
    session.flush()


def get_video_frames_by_file(session: Session, file_id: int) -> list[VideoFrame]:
    """获取指定视频的所有帧记录。"""
    return session.query(VideoFrame).filter(VideoFrame.file_id == file_id).all()


def get_video_frame_hashes_by_file(session: Session, file_id: int) -> list[str]:
    """获取指定视频所有帧的 pHash 值列表。"""
    frames = session.query(VideoFrame.phash).filter(
        VideoFrame.file_id == file_id
    ).all()
    return [f[0] for f in frames]


# ============================================================
# 查重结果
# ============================================================

def upsert_dedup_result(session: Session, unit_a_id: int, unit_b_id: int,
                        similarity_score: float, match_count: int,
                        total_files_a: int, total_files_b: int,
                        match_types: str) -> DedupResult:
    """插入或更新查重结果（按单元对去重）。"""
    # 统一顺序：确保小 ID 在前，大 ID 在后
    if unit_a_id > unit_b_id:
        unit_a_id, unit_b_id = unit_b_id, unit_a_id

    existing = session.query(DedupResult).filter(
        DedupResult.unit_a_id == unit_a_id,
        DedupResult.unit_b_id == unit_b_id,
    ).first()

    if existing:
        existing.similarity_score = similarity_score
        existing.match_count = match_count
        existing.total_files_a = total_files_a
        existing.total_files_b = total_files_b
        existing.match_types = match_types
        existing.is_resolved = False
        existing.resolution = "pending"
        existing.updated_at = datetime.now()
        session.flush()
        return existing
    else:
        dr = DedupResult(
            unit_a_id=unit_a_id,
            unit_b_id=unit_b_id,
            similarity_score=similarity_score,
            match_count=match_count,
            total_files_a=total_files_a,
            total_files_b=total_files_b,
            match_types=match_types,
        )
        session.add(dr)
        session.flush()
        return dr


def get_unresolved_duplicates(session: Session, limit: int = 100) -> list[DedupResult]:
    """获取未处理的查重结果。"""
    return session.query(DedupResult).filter(
        DedupResult.is_resolved == False,
        DedupResult.similarity_score >= 0.0,
    ).order_by(DedupResult.similarity_score.desc()).limit(limit).all()


def resolve_dedup(session: Session, result_id: int, resolution: str) -> None:
    """将查重结果标记为已处理。"""
    session.query(DedupResult).filter(DedupResult.id == result_id).update({
        "is_resolved": True,
        "resolution": resolution,
        "resolved_by": "user",
        "updated_at": datetime.now(),
    })


def get_dedup_by_id(session: Session, result_id: int) -> Optional[DedupResult]:
    """按 ID 获取查重结果。"""
    return session.query(DedupResult).filter(DedupResult.id == result_id).first()


# ============================================================
# 查重文件匹配
# ============================================================

def insert_file_match(session: Session, dedup_result_id: int, file_a_id: int,
                      file_b_id: int, similarity_score: float,
                      match_type: str) -> DedupFileMatch:
    """插入一条文件匹配记录。"""
    dfm = DedupFileMatch(
        dedup_result_id=dedup_result_id,
        file_a_id=file_a_id,
        file_b_id=file_b_id,
        similarity_score=similarity_score,
        match_type=match_type,
    )
    session.add(dfm)
    session.flush()
    return dfm


def get_file_matches_for_result(session: Session, dedup_result_id: int) -> list[DedupFileMatch]:
    """获取一次查重结果中的所有文件匹配对。"""
    return session.query(DedupFileMatch).filter(
        DedupFileMatch.dedup_result_id == dedup_result_id
    ).all()


# ============================================================
# 消息
# ============================================================

def create_message(session: Session, msg_type: str, title: str,
                   body: Optional[str] = None, related_unit_id: Optional[int] = None,
                   action_data: Optional[str] = None) -> Message:
    """创建一条新消息。"""
    msg = Message(
        msg_type=msg_type,
        title=title,
        body=body,
        related_unit_id=related_unit_id,
        action_data=action_data,
    )
    session.add(msg)
    session.flush()
    return msg


def get_unread_messages(session: Session, limit: int = 50) -> list[Message]:
    """获取未读且未忽略的消息。"""
    return session.query(Message).filter(
        Message.is_read == False,
        Message.is_dismissed == False,
    ).order_by(Message.created_at.desc()).limit(limit).all()


def get_all_messages(session: Session, limit: int = 200) -> list[Message]:
    """获取所有消息（按时间倒序）。"""
    return session.query(Message).order_by(Message.created_at.desc()).limit(limit).all()


def mark_message_read(session: Session, msg_id: int) -> None:
    """标记消息为已读。"""
    session.query(Message).filter(Message.id == msg_id).update({"is_read": True})


def mark_all_messages_read(session: Session) -> None:
    """标记所有消息为已读。"""
    session.query(Message).filter(Message.is_read == False).update({"is_read": True})


def dismiss_message(session: Session, msg_id: int) -> None:
    """忽略一条消息。"""
    session.query(Message).filter(Message.id == msg_id).update({"is_dismissed": True})


def get_unread_message_count(session: Session) -> int:
    """获取未读消息数。"""
    return session.query(func.count(Message.id)).filter(
        Message.is_read == False,
        Message.is_dismissed == False,
    ).scalar() or 0


# ============================================================
# 白名单
# ============================================================

def is_whitelisted(session: Session, path: str) -> bool:
    """判断指定路径是否在白名单中。"""
    # 路径精确匹配
    exact = session.query(Whitelist).filter(
        Whitelist.pattern == path,
        Whitelist.is_regex == False,
    ).first()
    return exact is not None


def add_whitelist(session: Session, pattern: str, is_regex: bool = False,
                  match_type: str = "path", note: Optional[str] = None) -> Whitelist:
    """添加一条白名单规则。"""
    wl = Whitelist(pattern=pattern, is_regex=is_regex, match_type=match_type, note=note)
    session.add(wl)
    session.flush()
    return wl


def remove_whitelist(session: Session, whitelist_id: int) -> None:
    """删除一条白名单规则。"""
    wl = session.query(Whitelist).filter(Whitelist.id == whitelist_id).first()
    if wl:
        session.delete(wl)


def get_all_whitelists(session: Session) -> list[Whitelist]:
    """获取所有白名单规则。"""
    return session.query(Whitelist).all()


# ============================================================
# 扫描记录
# ============================================================

def create_scan_session(session: Session) -> ScanSession:
    """创建一个新的扫描会话记录。"""
    ss = ScanSession()
    session.add(ss)
    session.flush()
    return ss


def update_scan_session(session: Session, session_id: int, **kwargs: Any) -> None:
    """更新扫描会话的进度信息。"""
    session.query(ScanSession).filter(ScanSession.id == session_id).update(kwargs)


def finish_scan_session(session: Session, session_id: int,
                        status: str = "completed",
                        errors: Optional[list[str]] = None) -> None:
    """完成扫描会话。"""
    import json
    update_data = {
        "status": status,
        "completed_at": datetime.now(),
    }
    if errors:
        update_data["error_log"] = json.dumps(errors, ensure_ascii=False)
    session.query(ScanSession).filter(ScanSession.id == session_id).update(update_data)


def get_latest_scan_session(session: Session) -> Optional[ScanSession]:
    """获取最近一次扫描记录。"""
    return session.query(ScanSession).order_by(ScanSession.started_at.desc()).first()
