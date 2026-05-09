# -*- coding: utf-8 -*-
"""
清理服务 —— 安全删除和移动文件的统一入口。

所有文件删除和移动操作均通过此服务，确保：
- 删除操作默认使用回收站（send2trash），而非永久删除
- 移动操作记录日志以便回滚
- 同步更新数据库中的文件记录
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from app.db.engine import DatabaseManager
from app.db.queries import delete_media_file

logger = logging.getLogger(__name__)


class CleanupService:
    """文件清理服务。

    用法:
        CleanupService.move_to_backup(file_path, backup_dir)
        CleanupService.delete_safe(file_id, file_path)
    """

    @staticmethod
    def move_to_backup(
        file_path: Path,
        backup_dir: Path,
        file_id: Optional[int] = None,
    ) -> bool:
        """将文件移动到备份目录。

        参数:
            file_path: 要移动的文件路径。
            backup_dir: 备份目标目录。
            file_id: 数据库文件 ID（可选，提供则同步删除 DB 记录）。

        返回:
            True 如果移动成功。
        """
        try:
            if not file_path.is_file():
                logger.warning(f"文件不存在，跳过: {file_path}")
                return False

            # 清理缩略图缓存
            CleanupService._remove_thumbnail(file_id)

            # 确保备份目录存在
            backup_dir.mkdir(parents=True, exist_ok=True)

            # 若有冲突，添加时间戳后缀
            dest = backup_dir / file_path.name
            if dest.exists():
                stem = file_path.stem
                suffix = file_path.suffix
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = backup_dir / f"{stem}_{timestamp}{suffix}"

            shutil.move(str(file_path), str(dest))
            logger.info(f"已移动: {file_path} → {dest}")

            # 同步删除数据库记录
            if file_id is not None:
                try:
                    with DatabaseManager.session() as session:
                        delete_media_file(session, file_id)
                except Exception as e:
                    logger.error(f"删除数据库记录失败 (file_id={file_id}): {e}")

            return True

        except Exception as e:
            logger.error(f"移动文件失败: {file_path} - {e}")
            return False

    @staticmethod
    def delete_safe(
        file_path: Path,
        file_id: Optional[int] = None,
    ) -> bool:
        """安全删除文件（放入回收站）。

        参数:
            file_path: 要删除的文件路径。
            file_id: 数据库文件 ID。

        返回:
            True 如果操作成功。
        """
        try:
            if not file_path.exists():
                logger.warning(f"文件不存在，跳过: {file_path}")
                return False

            # 清理缩略图缓存
            CleanupService._remove_thumbnail(file_id)

            # 尝试使用 send2trash（放入回收站）
            try:
                import send2trash
                send2trash.send2trash(str(file_path))
                logger.info(f"已放入回收站: {file_path}")
            except ImportError:
                # 回退：直接永久删除
                logger.warning("send2trash 不可用，将执行永久删除")
                file_path.unlink()
                logger.info(f"已永久删除: {file_path}")

            # 同步删除数据库记录
            if file_id is not None:
                try:
                    with DatabaseManager.session() as session:
                        delete_media_file(session, file_id)
                except Exception as e:
                    logger.error(f"删除数据库记录失败 (file_id={file_id}): {e}")

            return True

        except Exception as e:
            logger.error(f"删除文件失败: {file_path} - {e}")
            return False

    @staticmethod
    def delete_permanently(
        file_path: Path,
        file_id: Optional[int] = None,
    ) -> bool:
        """永久删除文件（不经过回收站）。

        参数:
            file_path: 文件路径。
            file_id: 数据库文件 ID。

        返回:
            True 如果删除成功。
        """
        try:
            if not file_path.exists():
                return False

            # 清理缩略图缓存
            CleanupService._remove_thumbnail(file_id)

            file_path.unlink()
            logger.info(f"已永久删除: {file_path}")

            if file_id is not None:
                try:
                    with DatabaseManager.session() as session:
                        delete_media_file(session, file_id)
                except Exception as e:
                    logger.error(f"删除数据库记录失败 (file_id={file_id}): {e}")

            return True

        except Exception as e:
            logger.error(f"永久删除失败: {file_path} - {e}")
            return False

    @staticmethod
    def _remove_thumbnail(file_id: int, cache_dir=None) -> None:
        """删除文件对应的缩略图缓存文件。"""
        if cache_dir is None:
            cache_dir = Path("data/.thumbnails")
        if file_id is None:
            return
        thumb = cache_dir / f"{file_id}_thumb.jpg"
        if thumb.exists():
            try:
                thumb.unlink()
                logger.debug(f"已清理缩略图缓存: {thumb}")
            except Exception as e:
                logger.warning(f"缩略图清理失败 {thumb}: {e}")

    @staticmethod
    def remove_unit_thumbnails(unit_id: int) -> int:
        """删除指定资源单元下所有文件的缩略图缓存。

        返回:
            删除的文件数。
        """
        from app.db import queries as q
        count = 0
        try:
            with DatabaseManager.session() as session:
                files = q.get_files_by_unit(session, unit_id)
                for f in files:
                    CleanupService._remove_thumbnail(f.id)
                    count += 1
            logger.info(f"已清理单元 {unit_id} 共 {count} 个缩略图")
        except Exception as e:
            logger.error(f"清理单元缩略图失败 (unit_id={unit_id}): {e}")
        return count

    @staticmethod
    def remove_root_thumbnails(root_id: int) -> int:
        """删除指定媒体库根目录下所有文件的缩略图缓存。

        返回:
            删除的文件数。
        """
        from app.db import queries as q
        count = 0
        try:
            with DatabaseManager.session() as session:
                units = q.get_units_by_root(session, root_id)
                for unit in units:
                    count += CleanupService.remove_unit_thumbnails(unit.id)
            logger.info(f"已清理根目录 {root_id} 共 {count} 个缩略图")
        except Exception as e:
            logger.error(f"清理根目录缩略图失败 (root_id={root_id}): {e}")
        return count

    @staticmethod
    def purge_orphaned_thumbnails(cache_dir: Path = None) -> int:
        """清理孤儿缩略图：缓存存在但 DB 中无对应文件的。

        返回:
            删除的孤儿文件数。
        """
        if cache_dir is None:
            cache_dir = Path("data/.thumbnails")
        if not cache_dir.is_dir():
            return 0

        from app.db.engine import DatabaseManager
        from app.db.models import MediaFile
        from sqlalchemy import select

        count = 0
        try:
            with DatabaseManager.session() as session:
                cached_ids = set()
                for f in cache_dir.iterdir():
                    if f.suffix == ".jpg" and f.stem.isdigit():
                        cached_ids.add(int(f.stem))

                if not cached_ids:
                    return 0

                # 查出所有缓存ID中哪些确实有对应的文件记录
                existing = {
                    row[0] for row in session.execute(
                        select(MediaFile.id).where(MediaFile.id.in_(cached_ids))
                    ).all()
                }
                orphaned = cached_ids - existing
                for fid in orphaned:
                    (cache_dir / f"{fid}_thumb.jpg").unlink(missing_ok=True)
                    count += 1

            if count > 0:
                logger.info(f"已清理 {count} 个孤儿缩略图")
            return count
        except Exception as e:
            logger.error(f"清理孤儿缩略图失败: {e}")
            return 0

    @staticmethod
    def invalidate_thumbnail(file_id: int) -> None:
        """使缩略图缓存失效：删除后触发下次请求时重新生成。"""
        CleanupService._remove_thumbnail(file_id)
        logger.debug(f"缩略图缓存已失效 (file_id={file_id})")

    @staticmethod
    def delete_empty_dirs(directory: Path) -> int:
        """递归删除空文件夹。

        参数:
            directory: 起始目录。

        返回:
            删除的空文件夹数量。
        """
        count = 0
        try:
            for dirpath, dirnames, filenames in sorted(
                directory.walk(), key=lambda x: -len(str(x[0]))
            ):
                current = Path(dirpath)
                if current == directory:
                    continue
                # 跳过缩略图缓存目录
                items = list(current.iterdir())
                has_only_thumbnails = all(
                    i.name == ".thumbnails" or
                    (i.name.startswith(".") and i.is_dir())
                    for i in items
                )
                if not items or has_only_thumbnails:
                    try:
                        if has_only_thumbnails:
                            for item in items:
                                shutil.rmtree(item, ignore_errors=True)
                        current.rmdir()
                        count += 1
                        logger.debug(f"已删除空目录: {current}")
                    except OSError:
                        pass
        except Exception as e:
            logger.error(f"清理空目录失败: {directory} - {e}")

        return count

    @staticmethod
    def batch_operation(
        file_actions: list[dict],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> dict[str, int]:
        """执行批量文件操作。

        参数:
            file_actions: 操作列表，每项为:
                {'action': 'delete'/'move', 'path': Path, 'file_id': int,
                 'backup_dir': Path (可选)}
            progress_callback: 进度回调 (已完成, 总数)。

        返回:
            {'success': N, 'failed': M} 操作结果统计。
        """
        success = 0
        failed = 0
        total = len(file_actions)

        for i, action in enumerate(file_actions):
            try:
                action_type = action.get("action", "delete")
                file_path = action.get("path")
                file_id = action.get("file_id")

                if not file_path or not isinstance(file_path, Path):
                    failed += 1
                    continue

                if action_type == "move":
                    backup_dir = action.get("backup_dir")
                    if not backup_dir:
                        failed += 1
                        continue
                    if CleanupService.move_to_backup(file_path, backup_dir, file_id):
                        success += 1
                    else:
                        failed += 1
                else:
                    if CleanupService.delete_safe(file_path, file_id):
                        success += 1
                    else:
                        failed += 1

            except Exception as e:
                logger.error(f"批量操作失败: {action} - {e}")
                failed += 1

            if progress_callback:
                progress_callback(i + 1, total)

        logger.info(f"批量操作完成: 成功 {success}, 失败 {failed}")
        return {"success": success, "failed": failed}
