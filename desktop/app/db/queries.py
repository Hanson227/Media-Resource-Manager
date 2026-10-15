# -*- coding: utf-8 -*-
"""
数据库查询函数集合。

所有持久化操作集中在此，提供命名查询函数而非在业务代码中直接写 SQL。
每个函数接收 SQLAlchemy Session 作为第一个参数（依赖注入模式）。
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Any

from sqlalchemy import func, and_, or_
from sqlalchemy.orm import Session

from app.db.models import (
    MediaLibraryRoot, ResourceUnit, MediaFile, FaceVector,
    VideoFrame, DedupResult, DedupFileMatch, Message,
    Whitelist, ScanSession, FileTag, FileTagMapping,
)
from app.utils.constants import (
    AUTO_EXCLUDE_REASONS, DEDUP_RESULT_VERSION, EvidenceKind, ExcludeReason,
    MatchLevel, MatchType, MessageType, ResolutionStatus, UnitStatus,
    WhitelistMatchType,
)
from app.services.file_ops import volume_available


logger = logging.getLogger(__name__)


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


def get_root_by_id(session: Session, root_id: int) -> Optional[MediaLibraryRoot]:
    """按 ID 查找媒体库根目录。"""
    return session.query(MediaLibraryRoot).filter(MediaLibraryRoot.id == root_id).first()


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


def mark_unit_excluded(session: Session, unit_id: int,
                       reason: str = ExcludeReason.USER.value) -> None:
    """将资源单元标记为排除状态，并记下起因。

    参数:
        reason: `ExcludeReason` 取值。默认 USER（用户显式排除）；
            扫描自动排除的两处调用必须传 AUTO_MISSING / AUTO_EMPTY，
            否则它们会被当成用户选择，内容回来了也永远不恢复。
    """
    unit = session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).first()
    if unit:
        unit.status = UnitStatus.EXCLUDED.value
        unit.exclude_reason = reason
        unit.updated_at = datetime.now()
        session.flush()


def is_auto_excluded(unit: ResourceUnit) -> bool:
    """该单元是否由扫描自动排除（= 内容回来时可以自动恢复）。

    NULL（升级前的旧记录）不算自动：起因不可考，宁可让用户显式恢复。
    """
    return unit.exclude_reason in AUTO_EXCLUDE_REASONS


def get_excluded_units(session: Session) -> list[ResourceUnit]:
    """获取所有已排除的资源单元。"""
    return session.query(ResourceUnit).filter(
        ResourceUnit.status == UnitStatus.EXCLUDED.value
    ).all()


def unexclude_unit(session: Session, unit_id: int) -> None:
    """将已排除的资源单元恢复为活跃状态（同时清掉排除起因）。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": UnitStatus.ACTIVE.value,
        "exclude_reason": None,
        "updated_at": datetime.now(),
    })


def unexclude_all_units(session: Session) -> int:
    """取消全部排除（「管理已排除文件夹」的批量恢复入口）。

    返回:
        被恢复的单元数。
    """
    count = session.query(ResourceUnit).filter(
        ResourceUnit.status == UnitStatus.EXCLUDED.value
    ).update({
        "status": UnitStatus.ACTIVE.value,
        "exclude_reason": None,
        "updated_at": datetime.now(),
    }, synchronize_session=False)
    return int(count or 0)


def unmerge_unit(session: Session, unit_id: int) -> None:
    """取消合并，将资源单元恢复为活跃状态。"""
    session.query(ResourceUnit).filter(ResourceUnit.id == unit_id).update({
        "status": UnitStatus.ACTIVE.value,
        "parent_id": None,
        "updated_at": datetime.now(),
    })


def restore_units_produced_by_scan(session: Session, unit_paths: Sequence[str]) -> list[str]:
    """扫描落库前调用：本轮重新产出内容的自动排除记录恢复为活跃。

    判据是 `exclude_reason`（见 `constants.ExcludeReason`），不是 `file_count`：
    用户手动「排除此文件夹」的记录起因是 `user`，必须保持排除（否则下次扫描
    就把用户的选择抹掉了）；扫描自动排除的（missing / empty）在内容回来后
    必须恢复 —— 包含"目录不可达时被排除、文件记录被刻意保留（file_count > 0）"
    这一整类，旧判据 `file_count == 0` 对它们永远不成立（实测事故：忘插盘启动
    一次整库进"已忽略"，插回盘刷新也回不来）。NULL（升级前的旧记录）不自动恢复。

    参数:
        unit_paths: 本轮扫描实际产出内容的单元路径。

    返回:
        被恢复为活跃的单元名列表（用于日志）。
    """
    restored: list[str] = []
    for path in unit_paths:
        unit = get_unit_by_path(session, str(path))
        if unit is None or unit.status != UnitStatus.EXCLUDED.value:
            continue
        if not is_auto_excluded(unit):
            continue
        unexclude_unit(session, unit.id)
        restored.append(unit.name)
    return restored


def exclude_empty_units(session: Session, root_id: int,
                        produced_paths: Sequence[str]) -> list[str]:
    """扫描落库后调用：把本根下本轮没有产出内容的活跃单元标记为排除。

    历史（见 `docs/pitfalls.md` 第 1 节）：旧判据只看目录是否存在，于是
    **目录还在、里面已经没有任何媒体文件**的文件夹（日期文件夹里的零散文件被
    删除或挪进子文件夹）永远清理不到，界面一直显示 "0 个 · 0 B"。补上第二种
    情形后判据是：

        - 目录已不存在且**卷可达** → 排除（原因 missing）
        - 目录还在但已无任何文件记录 → 排除（原因 empty；`is_manual` 的单元
          除外 —— 它是用户显式标记的，允许为空）

    **卷不可达（移动盘没插 / 网络盘掉线）时一律不动**：路径看不见不等于内容没了，
    按"目录已不存在"处理会让整个库瞬间"消失"（实测事故：忘插盘启动一次，
    全部进"已忽略"且刷新无效）。判断入口是 `file_ops.volume_available()`，
    与删除语义同源。

    仍有文件记录的单元也不动：权限异常、目录暂时不可读等情况下不该让内容消失。

    参数:
        root_id: 媒体库根目录 ID。
        produced_paths: 本轮扫描实际产出内容的单元路径。

    返回:
        被排除的单元名列表（用于日志）。
    """
    produced = {str(p) for p in produced_paths}
    excluded: list[str] = []
    for unit in get_units_by_root(session, root_id):
        if unit.status != UnitStatus.ACTIVE.value:
            continue
        if str(unit.path) in produced:
            continue
        if not Path(unit.path).is_dir():
            if not volume_available(unit.path):
                logger.warning(
                    f"磁盘未连接，保留单元不排除: {unit.path}")
                continue
            mark_unit_excluded(session, unit.id,
                               reason=ExcludeReason.AUTO_MISSING.value)
            excluded.append(unit.name)
            continue
        if unit.is_manual:
            continue
        if get_unit_file_count(session, unit.id) > 0:
            continue
        mark_unit_excluded(session, unit.id,
                           reason=ExcludeReason.AUTO_EMPTY.value)
        excluded.append(unit.name)
    return excluded


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


def get_files_by_extensions(session: Session,
                            extensions: Sequence[str]) -> list[MediaFile]:
    """按扩展名集合取活跃单元内的媒体文件（如 HEIC/HEIF 检查）。

    只查扩展名、不分页：HEIC 这类需要整体检查的格式在真实库里量级很小
    （实测 40 个），一次性取回比让上层自己拼分页简单且不会漏。
    """
    exts = [e.lower() for e in extensions]
    if not exts:
        return []
    return session.query(MediaFile).join(
        ResourceUnit, MediaFile.resource_unit_id == ResourceUnit.id
    ).filter(
        ResourceUnit.status == UnitStatus.ACTIVE.value,
        MediaFile.extension.in_(exts),
    ).order_by(MediaFile.path).all()


def count_files_with_empty_hash(session: Session,
                                extensions: Optional[Sequence[str]] = None) -> int:
    """统计哈希为"失败占位空串"的文件数。

    空串不是 NULL：索引侧把计算失败写成 ''，`get_unindexed_files` 就再也不会
    取到它 —— 这类行是**永久坏行**，必须由显式的修复动作重置回 NULL
    （实测真实库 40 个 HEIC 里 19 个 phash/dhash 为空串，改规则后不会自愈）。
    """
    query = session.query(MediaFile).filter(or_(
        MediaFile.md5_hash == "",
        MediaFile.phash == "",
        MediaFile.dhash == "",
    ))
    if extensions:
        query = query.filter(MediaFile.extension.in_([e.lower() for e in extensions]))
    return query.count()


def get_files_with_empty_hash(session: Session,
                              extensions: Optional[Sequence[str]] = None
                              ) -> list[MediaFile]:
    """取哈希为"失败占位空串"的文件行（供显式修复使用，见 count_files_with_empty_hash）。"""
    query = session.query(MediaFile).filter(or_(
        MediaFile.md5_hash == "",
        MediaFile.phash == "",
        MediaFile.dhash == "",
    ))
    if extensions:
        query = query.filter(MediaFile.extension.in_([e.lower() for e in extensions]))
    return query.order_by(MediaFile.id).all()


def reset_file_hashes(session: Session, file_ids: Sequence[int]) -> int:
    """把文件的哈希/宽高重置为 NULL，并清掉派生数据，使其重新进入待索引队列。

    同时删除人脸向量与视频帧、把 face_scan_version 归零：转换后的文件是新的
    编码产物，旧的派生数据（尤其"从 HEIC 解不出来"的空结果）没有保留价值。
    """
    ids = [int(i) for i in file_ids]
    if not ids:
        return 0
    session.query(FaceVector).filter(FaceVector.file_id.in_(ids)).delete(
        synchronize_session=False)
    session.query(VideoFrame).filter(VideoFrame.file_id.in_(ids)).delete(
        synchronize_session=False)
    return session.query(MediaFile).filter(MediaFile.id.in_(ids)).update({
        "md5_hash": None,
        "phash": None,
        "dhash": None,
        "width": None,
        "height": None,
        "face_scan_version": 0,
        "updated_at": datetime.now(),
    }, synchronize_session=False)


def update_file_after_convert(session: Session, file_id: int, *, path: str,
                              filename: str, extension: str, size_bytes: int) -> bool:
    """HEIC → JPG 转换后就地改写文件行（保留 file_id，从而保留标签/星标）。

    为什么不"删旧行 + 让扫描重新插入"：file_id 会变，`file_tag_mappings`
    随之丢失。就地改写 + 重置哈希（交给索引管线重算）语义更干净。

    返回:
        True 表示确实更新了一行。
    """
    updated = session.query(MediaFile).filter(MediaFile.id == file_id).update({
        "path": path,
        "filename": filename,
        "extension": extension,
        "size_bytes": int(size_bytes),
        "md5_hash": None,
        "phash": None,
        "dhash": None,
        "width": None,
        "height": None,
        "face_scan_version": 0,
        "updated_at": datetime.now(),
    }, synchronize_session=False)
    if updated:
        session.query(FaceVector).filter(FaceVector.file_id == file_id).delete(
            synchronize_session=False)
        session.query(VideoFrame).filter(VideoFrame.file_id == file_id).delete(
            synchronize_session=False)
    return bool(updated)


def delete_video_frames_for_file(session: Session, file_id: int) -> int:
    """删除某个文件的全部视频帧哈希（转换/重索引时清理派生数据用）。"""
    return session.query(VideoFrame).filter(VideoFrame.file_id == file_id).delete(
        synchronize_session=False)


def refresh_unit_stats(session: Session, unit_ids: Sequence[int]) -> int:
    """按当前文件行重算单元的 file_count / total_size / content_modified_at。

    与 scan_worker 清理失效文件后的算法一致（内容日期 = 单元内文件的
    max(st_mtime)，文件夹自身时间戳会因移动而失真），但这里额外允许把
    content_modified_at 清成 NULL —— `update_unit_stats(None)` 是"不更新"，
    单元被清空时会留下过期日期。

    返回:
        实际刷新的单元数。
    """
    refreshed = 0
    for uid in list(dict.fromkeys(int(i) for i in unit_ids)):
        files = get_files_by_unit(session, uid)
        total_size = sum(int(f.size_bytes or 0) for f in files)
        latest: Optional[float] = None
        for f in files:
            try:
                m = Path(f.path).stat().st_mtime
            except OSError:
                continue
            latest = m if latest is None else max(latest, m)
        session.query(ResourceUnit).filter(ResourceUnit.id == uid).update({
            "file_count": len(files),
            "total_size": total_size,
            "content_modified_at": (
                datetime.fromtimestamp(latest) if latest is not None else None
            ),
            "updated_at": datetime.now(),
        }, synchronize_session=False)
        refreshed += 1
    return refreshed


# ============================================================
# 人脸向量
# ============================================================

def insert_face_vector(session: Session, file_id: int, vector_data: bytes,
                       face_index: int = 0, bbox: Optional[tuple] = None,
                       source_ms: Optional[int] = None) -> FaceVector:
    """插入一条人脸向量记录。

    参数:
        file_id: 关联的媒体文件 ID。
        vector_data: 128 维 float32 字节数据。
        face_index: 人脸序号（同一文件内递增；视频多帧会跨帧继续编号）。
        bbox: 边界框 (x, y, w, h)。
        source_ms: 视频帧时间位置（毫秒）；图片传 None。
    """
    kwargs = {
        "file_id": file_id,
        "vector_data": vector_data,
        "face_index": face_index,
        "source_ms": source_ms,
    }
    if bbox:
        kwargs.update({"bbox_x": bbox[0], "bbox_y": bbox[1],
                       "bbox_w": bbox[2], "bbox_h": bbox[3]})

    fv = FaceVector(**kwargs)
    session.add(fv)
    session.flush()
    return fv


def get_face_vectors_by_file(session: Session, file_id: int) -> list[FaceVector]:
    """获取指定文件的所有人脸向量（按人脸序号排序，保证结果稳定）。"""
    return (
        session.query(FaceVector)
        .filter(FaceVector.file_id == file_id)
        .order_by(FaceVector.face_index)
        .all()
    )


def get_all_face_vectors(session: Session) -> list[FaceVector]:
    """获取数据库中所有人脸向量（用于全局人脸比对）。"""
    return session.query(FaceVector).all()


def delete_face_vectors_for_file(session: Session, file_id: int) -> int:
    """删除指定文件的全部人脸向量（重扫前清空，避免新旧向量叠加）。

    返回:
        被删除的行数。
    """
    n = (
        session.query(FaceVector)
        .filter(FaceVector.file_id == file_id)
        .delete(synchronize_session=False)
    )
    session.flush()
    return int(n or 0)


def set_face_scan_version(session: Session, file_id: int, version: int) -> None:
    """标记该文件的人脸是按哪版抽帧策略扫描的（见 FACE_SCAN_VERSION）。"""
    session.query(MediaFile).filter(MediaFile.id == file_id).update(
        {MediaFile.face_scan_version: version}, synchronize_session=False)
    session.flush()


def get_videos_needing_face_scan(session: Session, current_version: int,
                                 limit: int = 1000) -> list[MediaFile]:
    """取出人脸抽帧策略过期的视频（补扫任务的输入）。

    只挑视频：图片的人脸检测是全图检测，不受"单帧/多帧"策略影响。
    """
    return (
        session.query(MediaFile)
        .filter(
            MediaFile.media_type == "video",
            MediaFile.face_scan_version < current_version,
        )
        .order_by(MediaFile.id)
        .limit(limit)
        .all()
    )


def count_videos_needing_face_scan(session: Session, current_version: int) -> int:
    """待补扫视频数量（供界面显示预计工作量）。"""
    return int(
        session.query(func.count(MediaFile.id))
        .filter(
            MediaFile.media_type == "video",
            MediaFile.face_scan_version < current_version,
        )
        .scalar() or 0
    )


def _face_match_file_ids_subquery(session: Session):
    """出现在人脸匹配行里的文件 ID（两侧合并去重）。"""
    a = session.query(DedupFileMatch.file_a_id).filter(
        DedupFileMatch.match_type == MatchType.FACE.value)
    b = session.query(DedupFileMatch.file_b_id).filter(
        DedupFileMatch.match_type == MatchType.FACE.value)
    return a.union(b).subquery()


def get_candidate_videos_for_face_rescan(session: Session, current_version: int,
                                         limit: int = 1000) -> list[MediaFile]:
    """取出"上一轮出现过人脸线索"且抽帧策略过期的视频（精查候选）。

    为什么按候选精查而不是每次全库重扫：实测本库 1142 个视频里只有 165 个
    参与过人脸匹配 —— 对这 165 个做多帧精查约 2~3 分钟，全库则是十几分钟。
    候选集之外的视频由显式的"补扫全部"动作处理（scope='all'）。
    """
    sub = _face_match_file_ids_subquery(session)
    return (
        session.query(MediaFile)
        .filter(
            MediaFile.media_type == "video",
            MediaFile.face_scan_version < current_version,
            MediaFile.id.in_(session.query(sub.c[0])),
        )
        .order_by(MediaFile.id)
        .limit(limit)
        .all()
    )


def count_candidate_videos_for_face_rescan(session: Session,
                                           current_version: int) -> int:
    """待精查的候选视频数量（按需精查的工作量）。"""
    sub = _face_match_file_ids_subquery(session)
    return int(
        session.query(func.count(MediaFile.id))
        .filter(
            MediaFile.media_type == "video",
            MediaFile.face_scan_version < current_version,
            MediaFile.id.in_(session.query(sub.c[0])),
        )
        .scalar() or 0
    )


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
                        match_types: str,
                        match_level: str = MatchLevel.DUPLICATE.value,
                        evidence_kind: str = EvidenceKind.FILE.value,
                        computed_version: int = DEDUP_RESULT_VERSION) -> DedupResult:
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
        existing.match_level = match_level
        existing.evidence_kind = evidence_kind
        existing.computed_version = computed_version
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
            match_level=match_level,
            evidence_kind=evidence_kind,
            computed_version=computed_version,
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
                           page: int = 1, per_page: int = 20,
                           level: Optional[str] = None,
                           evidence: Optional[str] = None,
                           unit_a_id: Optional[int] = None
                           ) -> tuple[list[DedupResult], int]:
    """分页查询查重结果（SQL LIMIT/OFFSET + 精确总数）。

    参数:
        unresolved_only: 仅返回未处置的记录。
        page / per_page: 分页。
        level: 仅返回指定命中等级（duplicate / related）；None 表示不过滤。
            实测本库 166 个单元会产生 200+ 对"疑似相关"，不过滤时它们会和
            真正的重复混在一页里 —— 客户端按等级分开取才能各看各的。
        evidence: 仅返回指定证据来源（file / face）；None 表示不过滤。
            "疑似相关"里混着 7 条真实文件重叠与 373 条同演员线索，
            只有分开取，"相关"列表才不是一片 0% 噪音。
        unit_a_id: 仅返回以该单元为左侧（较小 ID）的结果，用于"按来源单元分组"展开。

    返回:
        (当前页结果列表, 满足条件的总条数)。

    说明：旧实现先按 limit=100/200 取全量再内存切片，导致 total 被截断
    （DB 有 261 行时 total 恒 ≤200，超出部分任何页都取不到）。
    """
    query = session.query(DedupResult)
    if unresolved_only:
        query = query.filter(DedupResult.is_resolved == False)  # noqa: E712
    if level:
        query = query.filter(DedupResult.match_level == level)
    if evidence:
        query = query.filter(DedupResult.evidence_kind == evidence)
    if unit_a_id is not None:
        query = query.filter(DedupResult.unit_a_id == unit_a_id)
    total = query.count()
    rows = (
        query.order_by(DedupResult.similarity_score.desc(), DedupResult.id.asc())
        .offset(max(0, (page - 1) * per_page))
        .limit(per_page)
        .all()
    )
    return rows, total


def count_dedup_results_by_level(session: Session) -> dict[str, int]:
    """按命中等级 / 证据来源统计未处置结果数量（供客户端分组显示）。

    返回:
        {"duplicate": N, "related": M, "face_only": K, "total": N+M+K}
        - related：有文件级证据的"疑似相关"
        - face_only：只有人脸线索的"同演员"（match_level 同样是 related，
          因此早期只按 level 分栏的客户端会看到 380 条 0% 噪音）
    """
    counts = {"duplicate": 0, "related": 0, "face_only": 0}
    rows = (
        session.query(
            DedupResult.match_level,
            DedupResult.evidence_kind,
            func.count(DedupResult.id),
        )
        .filter(DedupResult.is_resolved == False)  # noqa: E712
        .group_by(DedupResult.match_level, DedupResult.evidence_kind)
        .all()
    )
    for level, kind, n in rows:
        if (level or MatchLevel.DUPLICATE.value) == MatchLevel.DUPLICATE.value:
            counts["duplicate"] += int(n)
        elif (kind or EvidenceKind.FILE.value) == EvidenceKind.FACE.value:
            counts["face_only"] += int(n)
        else:
            counts["related"] += int(n)
    counts["total"] = counts["duplicate"] + counts["related"] + counts["face_only"]
    # 旧规则结论的数量（口径必须与列表项的 stale 标记完全一致，否则角标与卡片对不上）
    counts["stale"] = int(
        session.query(func.count(DedupResult.id))
        .filter(
            DedupResult.is_resolved == False,  # noqa: E712
            or_(
                DedupResult.computed_version < DEDUP_RESULT_VERSION,
                DedupResult.match_count > func.min(DedupResult.total_files_a,
                                                  DedupResult.total_files_b),
            ),
        )
        .scalar() or 0
    )
    counts["result_version"] = DEDUP_RESULT_VERSION
    return counts


def get_dedup_groups(session: Session, unresolved_only: bool = True,
                     level: Optional[str] = None,
                     evidence: Optional[str] = None,
                     page: int = 1, per_page: int = 50
                     ) -> tuple[list[dict], int]:
    """按"左侧单元"分组统计查重结果（列表页二级分组的父级）。

    分组键就是存储时的 unit_a_id（upsert 已保证小 ID 在前），
    与卡片左侧显示的是同一个单元 —— 用户看到的"左边相同"就能折叠成一组。

    返回:
        (组列表, 组总数)。每项含 anchor_unit_id / pair_count / max_similarity /
        match_types（组内出现过的匹配类型合并串）/ level / evidence_kind。
    """
    base = session.query(DedupResult)
    if unresolved_only:
        base = base.filter(DedupResult.is_resolved == False)  # noqa: E712
    if level:
        base = base.filter(DedupResult.match_level == level)
    if evidence:
        base = base.filter(DedupResult.evidence_kind == evidence)

    grouped = (
        base.with_entities(
            DedupResult.unit_a_id.label("anchor_unit_id"),
            DedupResult.match_level.label("level"),
            DedupResult.evidence_kind.label("evidence_kind"),
            func.count(DedupResult.id).label("pair_count"),
            func.max(DedupResult.similarity_score).label("max_similarity"),
            func.group_concat(DedupResult.match_types, ",").label("match_types"),
            func.min(DedupResult.computed_version).label("min_version"),
        )
        .group_by(DedupResult.unit_a_id, DedupResult.match_level,
                  DedupResult.evidence_kind)
        .all()
    )

    # 同一单元可能同时出现在多个 (level, evidence) 组合里 → 合并成一条，
    # 保留最强的一份（对数多者优先，其次最高分），避免同一个文件夹出现两行。
    merged: dict[int, dict] = {}
    for row in grouped:
        anchor = row.anchor_unit_id
        types = [t for t in (row.match_types or "").split(",") if t]
        item = merged.get(anchor)
        if item is None:
            merged[anchor] = {
                "anchor_unit_id": anchor,
                "pair_count": int(row.pair_count or 0),
                "max_similarity": float(row.max_similarity or 0.0),
                # group_concat 会把同一类型重复拼进来（组内多条各自带 video）→ 去重保序
                "match_types": _dedupe_types(types),
                "level": row.level,
                "evidence_kind": row.evidence_kind,
                "stale": int(row.min_version or 0) < DEDUP_RESULT_VERSION,
            }
            continue
        item["pair_count"] += int(row.pair_count or 0)
        item["max_similarity"] = max(item["max_similarity"],
                                     float(row.max_similarity or 0.0))
        item["stale"] = item["stale"] or (
            int(row.min_version or 0) < DEDUP_RESULT_VERSION)
        known = [t for t in item["match_types"].split(",") if t]
        item["match_types"] = _dedupe_types(known + types)

    groups = sorted(
        merged.values(),
        key=lambda g: (-g["pair_count"], -g["max_similarity"], g["anchor_unit_id"]),
    )
    total = len(groups)
    start = max(0, (page - 1) * per_page)
    return groups[start:start + per_page], total


def _dedupe_types(types: Sequence[str]) -> str:
    """匹配类型去重保序（group_concat 会把同一类型重复拼进来）。"""
    seen: list[str] = []
    for t in types:
        if t and t not in seen:
            seen.append(t)
    return ",".join(seen)


def get_face_hint_counts(session: Session, result_ids: Sequence[int]) -> dict[int, int]:
    """批量统计每条查重结果的人脸线索条数（避免逐条查询的 N+1）。

    人脸线索不能再用 match_count 表达（新语义下它只统计计入判定的文件对），
    因此界面上的"N 处人脸线索"必须来自 dedup_file_matches 的 face 行数。
    """
    if not result_ids:
        return {}
    rows = (
        session.query(DedupFileMatch.dedup_result_id, func.count(DedupFileMatch.id))
        .filter(
            DedupFileMatch.dedup_result_id.in_(list(result_ids)),
            DedupFileMatch.match_type == MatchType.FACE.value,
        )
        .group_by(DedupFileMatch.dedup_result_id)
        .all()
    )
    return {int(rid): int(n) for rid, n in rows}


def prune_stale_dedup_results(session: Session, unit_ids: Sequence[int],
                              keep_pairs: set[tuple[int, int]]) -> int:
    """清理本轮查重范围内"未再命中"的陈旧结果行。

    为什么必须清理：分类规则/阈值一变，旧结果不会自己消失 ——
    库里的 380 条 related 是旧规则产物，重跑后它们可能已不成立，
    但 upsert 只更新"本轮仍命中"的单元对，剩下的会永远留在列表里。

    安全边界（三者缺一不可）：
    1. 只动 unit_a_id 与 unit_b_id **都**在本次参与比对的单元集合内的行；
    2. 只删 is_resolved=False 的行 —— 用户的处置决定永久保留；
    3. keep_pairs 里是本轮真正命中的单元对（小 ID 在前）。

    返回:
        被删除的结果行数（其文件匹配行由外键 CASCADE 一并删除）。
    """
    if not unit_ids:
        return 0
    ids = list(unit_ids)
    rows = (
        session.query(DedupResult)
        .filter(
            DedupResult.is_resolved == False,  # noqa: E712
            DedupResult.unit_a_id.in_(ids),
            DedupResult.unit_b_id.in_(ids),
        )
        .all()
    )
    stale = [dr for dr in rows if _pair_key(dr.unit_a_id, dr.unit_b_id) not in keep_pairs]
    for dr in stale:
        session.delete(dr)
    session.flush()
    return len(stale)


def get_units_by_ids(session: Session, unit_ids: list[int]) -> list[ResourceUnit]:
    """按 ID 批量获取资源单元（避免逐条查询的 N+1）。"""
    if not unit_ids:
        return []
    return session.query(ResourceUnit).filter(ResourceUnit.id.in_(unit_ids)).all()


def mark_dedup_results_stale_for_units(session: Session, unit_ids: Sequence[int],
                                       exclude_result_id: Optional[int] = None) -> int:
    """把涉及指定单元的**未处置**查重结果标记为陈旧（computed_version=0）。

    为什么不直接删行：合并后这些"结果"引用的文件匹配关系已经失真
    （一侧文件被移走/删除），数字不再可信；但删行会丢掉对其它单元对的
    提示（如 B 曾与 C 也报过重复 —— B 的内容并进了 A，这条线索仍值得看）。
    computed_version=0 让 `_is_stale` 命中，客户端显示"数据待重算"，
    重跑查重时由 prune_stale_dedup_results 按现状自然收敛。

    参数:
        exclude_result_id: 本次合并自身的结果行（处置已完成，无需标 stale）。

    返回:
        被标记的结果行数。
    """
    if not unit_ids:
        return 0
    ids = list(unit_ids)
    query = session.query(DedupResult).filter(
        DedupResult.is_resolved == False,  # noqa: E712
        or_(DedupResult.unit_a_id.in_(ids), DedupResult.unit_b_id.in_(ids)),
    )
    if exclude_result_id is not None:
        query = query.filter(DedupResult.id != exclude_result_id)
    rows = query.all()
    for dr in rows:
        dr.computed_version = 0
        dr.updated_at = datetime.now()
    session.flush()
    return len(rows)


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
                      match_type: str, frame_support: int = 1) -> DedupFileMatch:
    """插入一条文件匹配记录。

    参数:
        frame_support: 支持这条匹配的帧数（人脸线索专用，其它类型恒为 1）。
    """
    dfm = DedupFileMatch(
        dedup_result_id=dedup_result_id,
        file_a_id=file_a_id,
        file_b_id=file_b_id,
        similarity_score=similarity_score,
        match_type=match_type,
        frame_support=frame_support,
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


# ============================================================
# 路径改库（盘符/挂载点变化）
# ============================================================
#
# 媒体库路径以**绝对路径**落库（media_library_roots / resource_units /
# media_files 三张表的 path 都有 unique 约束），盘符一变整库就"看不见"。
# 换盘后把这些路径按前缀整体改写，比重扫安全得多（重扫会新建一套根+单元，
# 旧记录变成幽灵，还会把用户的星标/标签/封面全部孤立在旧 id 上）。
#
# 约定：
#   - 比较一律走 LOWER()：Windows 路径大小写不敏感，`D:\Media` 与 `d:\media`
#     是同一个目录，按大小写敏感比较会漏改一半；
#   - 只改前缀能对上的行，不做任何模糊/相似度匹配（改错库比不改更糟）；
#   - 本模块只负责"执行"，冲突检测与 dry-run 编排在 path_remap_service。

_PATH_TABLES = (
    # (展示名, 模型, 路径列名)
    ("library_roots", MediaLibraryRoot, "path"),
    ("units", ResourceUnit, "path"),
    ("files", MediaFile, "path"),
)


def _path_prefix_filter(column, prefix: str):
    """构造"路径以 prefix 开头"的条件（大小写不敏感）。"""
    return func.lower(column).like(prefix.lower() + "%")


def _path_prefix_column(model, column_name: str):
    """按列名取 ORM 类属性（避免在调用处写死属性引用）。"""
    return getattr(model, column_name)


def count_paths_with_prefix(session: Session, prefix: str) -> dict:
    """统计各表有多少行路径以 prefix 开头（dry-run / 预览用）。

    返回:
        {"library_roots": n, "units": n, "unit_covers": n, "files": n,
         "whitelist": n}
    """
    counts = {}
    for name, model, column_name in _PATH_TABLES:
        column = _path_prefix_column(model, column_name)
        counts[name] = int(
            session.query(func.count()).select_from(model)
            .filter(_path_prefix_filter(column, prefix)).scalar() or 0
        )
    # 单元封面也是绝对路径，但不是 unique 列，单独统计
    counts["unit_covers"] = int(
        session.query(func.count()).select_from(ResourceUnit)
        .filter(ResourceUnit.cover_path.isnot(None),
                _path_prefix_filter(ResourceUnit.cover_path, prefix)).scalar() or 0
    )
    # 白名单里的路径型规则（含正则/glob 的规则只做前缀改写，语义可能变化，
    # 因此只在 dry-run 里报数量，由用户决定是否接受）
    counts["whitelist"] = int(
        session.query(func.count()).select_from(Whitelist)
        .filter(_path_prefix_filter(Whitelist.pattern, prefix)).scalar() or 0
    )
    return counts


def find_path_conflicts(session: Session, old_prefix: str,
                        new_prefix: str, limit: int = 20) -> list[dict]:
    """找出"改写后会撞上已有记录"的冲突（apply 前必须为空）。

    冲突定义：某行路径以 new_prefix 开头、但**不**以 old_prefix 开头 ——
    说明目标位置已存在另一份记录，改写会违反 path 的 unique 约束，
    或让新旧两套记录指向同一个目录（后续扫描会互相覆盖）。
    """
    conflicts: list[dict] = []
    for name, model, column_name in _PATH_TABLES:
        column = _path_prefix_column(model, column_name)
        rows = (
            session.query(column)
            .filter(_path_prefix_filter(column, new_prefix),
                    ~_path_prefix_filter(column, old_prefix))
            .limit(limit).all()
        )
        for (path,) in rows:
            conflicts.append({"table": name, "path": path})
    return conflicts


def rebase_path_prefix(session: Session, old_prefix: str, new_prefix: str,
                       *, include_whitelist: bool = False,
                       include_cover: bool = True) -> dict:
    """把以 old_prefix 开头的路径整体改写成 new_prefix（事务由调用方保证）。

    参数:
        session: 数据库会话（调用方负责提交/回滚）。
        old_prefix: 旧前缀，如 `D:\\Media`。
        new_prefix: 新前缀，如 `E:\\Media`。
        include_whitelist: 是否一并改写白名单规则里的路径前缀。
        include_cover: 是否一并改写资源单元封面路径。

    返回:
        各表实际改写行数（与 count_paths_with_prefix 口径一致）。

    说明:
        幂等：old_prefix 已经不存在时全部返回 0，重复执行不会二次改写。
        前缀相同（含仅大小写不同）时视为无需改动，直接返回全 0 ——
        否则 LOWER() 比较会把"同一个路径"当成命中，产生无意义的 UPDATE。
    """
    if old_prefix == new_prefix:
        return {"library_roots": 0, "units": 0, "unit_covers": 0,
                "files": 0, "whitelist": 0}

    # 改写表达式：SUBSTR(path, len(old)+1) 拼到 new 后面，保留原后缀大小写
    def _replacement(column):
        return new_prefix + func.substr(column, len(old_prefix) + 1)

    updated: dict[str, int] = {}
    for name, model, column_name in _PATH_TABLES:
        column = _path_prefix_column(model, column_name)
        updated[name] = int(
            session.query(model)
            .filter(_path_prefix_filter(column, old_prefix))
            .update({column: _replacement(column)}, synchronize_session=False)
        )

    if include_cover:
        updated["unit_covers"] = int(
            session.query(ResourceUnit)
            .filter(ResourceUnit.cover_path.isnot(None),
                    _path_prefix_filter(ResourceUnit.cover_path, old_prefix))
            .update({ResourceUnit.cover_path: _replacement(ResourceUnit.cover_path)},
                    synchronize_session=False)
        )
    else:
        updated["unit_covers"] = 0

    if include_whitelist:
        updated["whitelist"] = int(
            session.query(Whitelist)
            .filter(_path_prefix_filter(Whitelist.pattern, old_prefix))
            .update({Whitelist.pattern: _replacement(Whitelist.pattern)},
                    synchronize_session=False)
        )
    else:
        updated["whitelist"] = 0

    session.flush()
    logger.info(
        "路径改库: %s → %s  " % (old_prefix, new_prefix)
        + ", ".join(f"{k}={v}" for k, v in updated.items())
    )
    return updated

