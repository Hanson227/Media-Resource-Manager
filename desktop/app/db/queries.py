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
    Whitelist, ScanSession, FileTag, FileTagMapping,
)
from app.utils.constants import (
    MessageType, ResolutionStatus, UnitStatus, WhitelistMatchType,
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
    return session.query(ResourceUnit).filter(ResourceUnit.status == UnitStatus.ACTIVE.value).all()


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
        ResourceUnit.status == UnitStatus.ACTIVE.value,
    ).all()


def get_child_units(session: Session, parent_id: int) -> list[ResourceUnit]:
    """获取被合并到指定父单元的所有子单元。"""
    return session.query(ResourceUnit).filter(ResourceUnit.parent_id == parent_id).all()


def create_unit(session: Session, path: str, name: str, library_root_id: int,
                is_manual: bool = False, file_count: int = 0,
                total_size: int = 0,
                content_modified_at: Optional[datetime] = None) -> ResourceUnit:
    """创建新的资源单元记录。

    content_modified_at: 单元内媒体文件的最新真实修改时间（None 表示未采集）。
    """
    unit = ResourceUnit(
        path=path, name=name, library_root_id=library_root_id,
        is_manual=is_manual, file_count=file_count, total_size=total_size,
        content_modified_at=content_modified_at,
    )
    session.add(unit)
    session.flush()
    return unit


def update_unit_stats(session: Session, unit_id: int, file_count: int, total_size: int,
                      content_modified_at: Optional[datetime] = None) -> None:
    """更新资源单元的文件统计信息。

    content_modified_at 非 None 时一并更新（None 不覆盖已有值）。
    """
    updates = {
        "file_count": file_count,
        "total_size": total_size,
        "updated_at": datetime.now(),
    }
    if content_modified_at is not None:
        updates["content_modified_at"] = content_modified_at
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update(updates)


def mark_unit_merged(session: Session, unit_id: int, parent_id: int) -> None:
    """将资源单元标记为已合并到父单元。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": UnitStatus.MERGED.value,
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
        ResourceUnit.status == UnitStatus.ACTIVE.value,
    ).all()


def mark_unit_excluded(session: Session, unit_id: int) -> None:
    """将资源单元标记为排除状态。"""
    unit = session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).first()
    if unit:
        unit.status = UnitStatus.EXCLUDED.value
        unit.updated_at = datetime.now()
        session.flush()


def get_excluded_units(session: Session) -> list[ResourceUnit]:
    """获取所有已排除的资源单元。"""
    return session.query(ResourceUnit).filter(
        ResourceUnit.status == UnitStatus.EXCLUDED.value
    ).all()


def unexclude_unit(session: Session, unit_id: int) -> None:
    """将已排除的资源单元恢复为活跃状态。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": UnitStatus.ACTIVE.value,
        "updated_at": datetime.now(),
    })


def unmerge_unit(session: Session, unit_id: int) -> None:
    """取消合并，将资源单元恢复为活跃状态。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": UnitStatus.ACTIVE.value,
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


def get_files_page(session: Session, unit_id: Optional[int] = None,
                   page: int = 1, per_page: int = 50) -> tuple[list[MediaFile], int]:
    """分页获取媒体文件（SQL 层 LIMIT/OFFSET，避免全量载入内存）。

    参数:
        session: 数据库会话。
        unit_id: 指定资源单元（None 时返回所有活跃单元的文件）。
        page: 页码（从 1 开始）。
        per_page: 每页数量。

    返回:
        (当前页文件列表, 总条数)。
    """
    query = session.query(MediaFile)
    if unit_id is not None:
        query = query.filter(MediaFile.resource_unit_id == unit_id)
    else:
        query = query.join(
            ResourceUnit, MediaFile.resource_unit_id == ResourceUnit.id
        ).filter(ResourceUnit.status == UnitStatus.ACTIVE.value)

    total = query.count()
    files = (
        query.order_by(MediaFile.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return files, total


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
    # 数据库列名与 hash_type 参数名不一致：
    # MediaFile.md5_hash 对应 hash_type="md5"
    # MediaFile.phash   对应 hash_type="phash"
    # MediaFile.dhash   对应 hash_type="dhash"
    column_map = {
        "md5": MediaFile.md5_hash,
        "phash": MediaFile.phash,
        "dhash": MediaFile.dhash,
    }
    column = column_map.get(hash_type)
    if column is None:
        return []
    return session.query(MediaFile).filter(column == None).limit(limit).all()


def get_unindexed_files(session: Session, limit: int = 1000) -> list[MediaFile]:
    """获取所有活跃单元中未计算任何哈希的文件。

    说明：索引侧对计算失败/文件缺失会写空串占位（''）以避开重复重试；
    占位记录由 clear_stale_hash_placeholders() 在后续扫描发现文件恢复时
    重置为 NULL，从而重新进入待索引队列。
    """
    return session.query(MediaFile).join(
        ResourceUnit, MediaFile.resource_unit_id == ResourceUnit.id
    ).filter(
        ResourceUnit.status == UnitStatus.ACTIVE.value,
        or_(
            MediaFile.md5_hash == None,
            MediaFile.phash == None,
        )
    ).limit(limit).all()


def get_unindexed_file_count(session: Session) -> int:
    """获取所有活跃单元中未计算哈希的文件数量。"""
    return session.query(MediaFile).join(
        ResourceUnit, MediaFile.resource_unit_id == ResourceUnit.id
    ).filter(
        ResourceUnit.status == UnitStatus.ACTIVE.value,
        or_(
            MediaFile.md5_hash == None,
            MediaFile.phash == None,
        )
    ).count()


def clear_stale_hash_placeholders(session: Session, file_id: int) -> None:
    """清除文件记录上的空串哈希占位（'' → NULL）。

    占位表示历史上某次计算失败/文件缺失；当文件已恢复（如再次被扫描到）
    时调用本函数，让该文件重新进入待索引队列完成自愈。
    """
    session.query(MediaFile).filter(MediaFile.id == file_id).update({
        "md5_hash": None,
        "phash": None,
        "dhash": None,
        "updated_at": datetime.now(),
    })


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


def delete_media_file(session: Session, file_id: int) -> bool:
    """删除一条媒体文件记录（级联删除关联的人脸向量和视频帧）。"""
    mf = session.query(MediaFile).filter(MediaFile.id == file_id).first()
    if mf:
        session.delete(mf)
        return True
    return False


def delete_resource_unit(session: Session, unit_id: int) -> bool:
    """删除一个资源单元（级联删除关联的 media_files、face_vectors、video_frames）。"""
    unit = session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).first()
    if unit:
        session.delete(unit)
        return True
    return False


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


def get_video_frame_hashes_by_files(session: Session, file_ids: list[int],
                                    chunk_size: int = 500) -> dict[int, list[str]]:
    """批量获取多个视频文件的帧 pHash 列表（按时间戳升序）。

    分块执行以避开 SQLite 绑定变量上限；供查重管线一次性装载帧哈希，
    避免逐文件查询的 N+1。

    返回:
        {file_id: [phash, ...]}；无帧的文件不出现在结果中。
    """
    result: dict[int, list[str]] = {}
    if not file_ids:
        return result
    for start in range(0, len(file_ids), max(1, chunk_size)):
        chunk = file_ids[start:start + chunk_size]
        rows = session.query(VideoFrame.file_id, VideoFrame.phash).filter(
            VideoFrame.file_id.in_(chunk)
        ).order_by(VideoFrame.file_id, VideoFrame.timestamp_ms).all()
        for fid, ph in rows:
            result.setdefault(fid, []).append(ph)
    return result


# ============================================================
# 查重结果
# ============================================================

def upsert_dedup_result(session: Session, unit_a_id: int, unit_b_id: int,
                        similarity_score: float, match_count: int,
                        total_files_a: int, total_files_b: int,
                        match_types: str) -> DedupResult:
    """插入或更新查重结果（按单元对去重）。

    注意：已处置（is_resolved=True）的记录不会被重置为 pending ——
    用户对某对单元的处置是一次性决定，重复查重不应推翻它。
    """
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
        if not existing.is_resolved:
            # 仅在用户尚未处置时保持 pending；已处置的保留原状态
            existing.is_resolved = False
            existing.resolution = ResolutionStatus.PENDING.value
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


def _pair_key(a: int, b: int) -> tuple[int, int]:
    """规范化单元对键（小 ID 在前），与 dedup_results 存储顺序一致。"""
    return (a, b) if a < b else (b, a)


def get_resolved_dedup_pairs(session: Session, unit_ids: list[int]) -> set[tuple[int, int]]:
    """返回给定单元集合中已被用户处置（is_resolved）的单元对。

    查重编排在重跑前用此集合跳过已处置的对，避免重复告警。
    """
    if not unit_ids:
        return set()
    rows = session.query(DedupResult.unit_a_id, DedupResult.unit_b_id).filter(
        DedupResult.is_resolved == True,  # noqa: E712
        or_(
            DedupResult.unit_a_id.in_(unit_ids),
            DedupResult.unit_b_id.in_(unit_ids),
        ),
    ).all()
    return {_pair_key(a, b) for a, b in rows}


def get_whitelisted_unit_paths(session: Session) -> set[str]:
    """返回所有白名单（match_type='unit'，精确路径）的单元路径集合。"""
    rows = session.query(Whitelist.pattern).filter(
        Whitelist.match_type == WhitelistMatchType.UNIT.value,
        Whitelist.is_regex == False,  # noqa: E712
    ).all()
    return {r[0] for r in rows}


def build_dedup_skip_pairs(session: Session, unit_ids: list[int]) -> set[tuple[int, int]]:
    """构建重跑查重时应跳过的单元对集合。

    跳过条件（任一命中即跳过，避免再次打扰用户）：
    1. 该对已有 is_resolved=True 的处置记录；
    2. 该对任一单元已被加入白名单（unit 级）。
    """
    skip = get_resolved_dedup_pairs(session, unit_ids)
    whitelisted = get_whitelisted_unit_paths(session)
    if whitelisted and unit_ids:
        units = session.query(ResourceUnit).filter(
            ResourceUnit.id.in_(unit_ids),
            ResourceUnit.path.in_(whitelisted),
        ).all()
        wl_ids = {u.id for u in units}
        for a in unit_ids:
            for b in unit_ids:
                if a < b and (a in wl_ids or b in wl_ids):
                    skip.add((a, b))
    return skip


def resolve_dedup_pair(session: Session, unit_a_id: int, unit_b_id: int,
                       resolution: str) -> Optional[int]:
    """按单元对将查重结果标记为已处理（GUI 侧处置入口）。

    返回:
        命中的查重结果 ID；无记录时返回 None。
    """
    a, b = _pair_key(unit_a_id, unit_b_id)
    dr = session.query(DedupResult).filter(
        DedupResult.unit_a_id == a,
        DedupResult.unit_b_id == b,
    ).first()
    if not dr:
        return None
    _apply_dedup_resolution(session, dr, resolution)
    return dr.id


def _apply_dedup_resolution(session: Session, dr: DedupResult, resolution: str) -> None:
    """应用处置：更新状态；resolution='whitelist' 时同时写入白名单（unit 级）。"""
    dr.is_resolved = True
    dr.resolution = resolution
    dr.resolved_by = "user"
    dr.updated_at = datetime.now()

    if resolution == ResolutionStatus.WHITELIST.value:
        unit_a = session.query(ResourceUnit).filter(ResourceUnit.id == dr.unit_a_id).first()
        unit_b = session.query(ResourceUnit).filter(ResourceUnit.id == dr.unit_b_id).first()
        for unit in (unit_a, unit_b):
            if not unit:
                continue
            dup = session.query(Whitelist).filter(
                Whitelist.pattern == unit.path,
                Whitelist.match_type == WhitelistMatchType.UNIT.value,
                Whitelist.is_regex == False,  # noqa: E712
            ).first()
            if dup is None:
                session.add(Whitelist(
                    pattern=unit.path,
                    match_type=WhitelistMatchType.UNIT.value,
                    note=f"查重白名单 (dedup_result={dr.id})",
                ))
    session.flush()


def get_unresolved_duplicates(session: Session, limit: int = 100) -> list[DedupResult]:
    """获取未处理的查重结果。"""
    return session.query(DedupResult).filter(
        DedupResult.is_resolved == False,
        DedupResult.similarity_score >= 0.0,
    ).order_by(DedupResult.similarity_score.desc()).limit(limit).all()


def get_all_dedup_results(session: Session, limit: int = 200) -> list[DedupResult]:
    """获取全部查重结果（按相似度降序）。"""
    return session.query(DedupResult).order_by(
        DedupResult.similarity_score.desc()
    ).limit(limit).all()


def get_dedup_results_page(session: Session, unresolved_only: bool = True,
                           page: int = 1, per_page: int = 20
                           ) -> tuple[list[DedupResult], int]:
    """分页查询查重结果（SQL LIMIT/OFFSET + 精确总数）。

    返回:
        (当前页结果列表, 满足条件的总条数)。

    说明：旧实现先按 limit=100/200 取全量再内存切片，导致 total 被截断
    （DB 有 261 行时 total 恒 ≤200，超出部分任何页都取不到）。
    """
    query = session.query(DedupResult)
    if unresolved_only:
        query = query.filter(DedupResult.is_resolved == False)  # noqa: E712
    total = query.count()
    rows = (
        query.order_by(DedupResult.similarity_score.desc())
        .offset(max(0, (page - 1) * per_page))
        .limit(per_page)
        .all()
    )
    return rows, total


def get_units_by_ids(session: Session, unit_ids: list[int]) -> list[ResourceUnit]:
    """按 ID 批量获取资源单元（避免逐条查询的 N+1）。"""
    if not unit_ids:
        return []
    return session.query(ResourceUnit).filter(ResourceUnit.id.in_(unit_ids)).all()


def resolve_dedup(session: Session, result_id: int, resolution: str) -> None:
    """将查重结果标记为已处理（旧版桩函数，语义见 _apply_dedup_resolution）。"""
    dr = session.query(DedupResult).filter(DedupResult.id == result_id).first()
    if dr:
        _apply_dedup_resolution(session, dr, resolution)


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


def delete_file_matches_for_result(session: Session, dedup_result_id: int) -> None:
    """删除一次查重结果中的所有文件匹配对。"""
    session.query(DedupFileMatch).filter(
        DedupFileMatch.dedup_result_id == dedup_result_id
    ).delete()


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


def get_unread_dedup_alert_count(session: Session) -> int:
    """获取未读的查重提醒消息数（角标红点判断用）。"""
    return session.query(func.count(Message.id)).filter(
        Message.msg_type == MessageType.DEDUP_ALERT.value,
        Message.is_read == False,
        Message.is_dismissed == False,
    ).scalar() or 0


# ============================================================
# 白名单
# ============================================================

def is_whitelisted(session: Session, path: str) -> bool:
    """判断指定路径是否在白名单中。"""
    import re
    # 路径精确匹配
    exact = session.query(Whitelist).filter(
        Whitelist.pattern == path,
        Whitelist.is_regex == False,
    ).first()
    if exact is not None:
        return True
    # 正则匹配
    regex_rules = session.query(Whitelist).filter(
        Whitelist.is_regex == True,
        Whitelist.match_type == "path",
    ).all()
    for rule in regex_rules:
        try:
            if re.search(rule.pattern, path):
                return True
        except re.error:
            continue
    return False


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


# ============================================================
# 文件标签
# ============================================================

def create_tag(session: Session, name: str, color: Optional[str] = None) -> FileTag:
    """创建一个新标签。"""
    tag = FileTag(name=name, color=color)
    session.add(tag)
    session.flush()
    return tag


def get_all_tags(session: Session) -> list[FileTag]:
    """获取所有标签（按名称排序）。"""
    return session.query(FileTag).order_by(FileTag.name).all()


def get_tag_by_id(session: Session, tag_id: int) -> Optional[FileTag]:
    """按 ID 获取标签。"""
    return session.query(FileTag).filter(FileTag.id == tag_id).first()


def get_tag_by_name(session: Session, name: str) -> Optional[FileTag]:
    """按名称查找标签。"""
    return session.query(FileTag).filter(FileTag.name == name).first()


def delete_tag(session: Session, tag_id: int) -> bool:
    """删除标签（级联删除关联关系）。"""
    tag = session.query(FileTag).filter(FileTag.id == tag_id).first()
    if tag:
        session.delete(tag)
        return True
    return False


def set_file_tags(session: Session, file_id: int, tag_ids: list[int]) -> None:
    """设置文件的标签（全量替换：先删旧关联，再插新关联）。"""
    session.query(FileTagMapping).filter(FileTagMapping.file_id == file_id).delete()
    for tid in tag_ids:
        mapping = FileTagMapping(file_id=file_id, tag_id=tid)
        session.add(mapping)
    session.flush()


def get_file_tags(session: Session, file_id: int) -> list[FileTag]:
    """获取指定文件的所有标签。"""
    return session.query(FileTag).join(
        FileTagMapping, FileTag.id == FileTagMapping.tag_id
    ).filter(FileTagMapping.file_id == file_id).all()


def get_all_mapped_files(session: Session, tag_ids: Optional[list[int]] = None) -> dict[int, list[dict]]:
    """批量查询文件标签。返回 {file_id: [{id, name, color}, ...], ...}。

    参数:
        tag_ids: 若提供，只返回包含这些标签中任意一个的文件。
    """
    query = session.query(
        FileTagMapping.file_id,
        FileTag.id,
        FileTag.name,
        FileTag.color,
    ).join(FileTag, FileTag.id == FileTagMapping.tag_id)
    if tag_ids:
        query = query.filter(FileTagMapping.tag_id.in_(tag_ids))
    rows = query.all()
    result: dict[int, list[dict]] = {}
    for file_id, tag_id, name, color in rows:
        result.setdefault(file_id, []).append({
            "id": tag_id, "name": name, "color": color,
        })
    return result
