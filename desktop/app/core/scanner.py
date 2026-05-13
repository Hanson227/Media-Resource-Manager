# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
文件夹扫描器 —— 递归遍历媒体库根目录，自动识别资源单元。

资源单元自动判定规则:
    自底向上遍历。若文件夹内直接包含媒体文件，则该文件夹为候选单元。
    当候选单元存在嵌套关系时，保留父级，移除子级——
    子文件夹的媒体文件会递归归入父单元管理。
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, FrozenSet

from app.utils.media_types import is_media_file, get_media_type
from app.utils.file_helpers import normalize_path
from app.core.exceptions import ScanError, AccessDeniedError

logger = logging.getLogger(__name__)


# ============================================================
# 不可变结果类型
# ============================================================

@dataclass(frozen=True)
class DiscoveredFile:
    """扫描发现的单个媒体文件。"""

    path: Path
    """文件绝对路径。"""

    filename: str
    """文件名（含扩展名）。"""

    extension: str
    """小写扩展名。"""

    media_type: str
    """'image' 或 'video'。"""

    size_bytes: int
    """文件字节数。"""

    relative_to_root: Path
    """相对于媒体库根目录的路径（用于显示）。"""


@dataclass(frozen=True)
class ResourceUnit:
    """一个资源单元（包含媒体文件的文件夹）。"""

    path: Path
    """文件夹绝对路径。"""

    name: str
    """文件夹名称。"""

    files: tuple
    """该单元内直接包含的媒体文件（不可变元组）。"""

    is_leaf: bool
    """是否为叶子单元（子文件夹中无媒体文件）。"""

    parent_root: Path
    """所属媒体库根目录。"""

    @property
    def file_count(self) -> int:
        """该单元内媒体文件数量。"""
        return len(self.files)

    @property
    def total_size(self) -> int:
        """该单元内所有文件的总大小（字节）。"""
        return sum(f.size_bytes for f in self.files)


@dataclass(frozen=True)
class ScanResult:
    """一次完整扫描的结果。"""

    root_path: Path
    """扫描的媒体库根目录。"""

    units: tuple
    """所有检测到的资源单元（不可变元组）。"""

    total_files: int
    """总文件数。"""

    total_size_bytes: int
    """总文件大小（字节）。"""

    errors: tuple
    """扫描错误列表（不可变元组）。"""


# ============================================================
# 扫描器
# ============================================================

class MediaScanner:
    """文件夹扫描器，自动识别资源单元。

    使用方式:
        scanner = MediaScanner(extensions, exclude_patterns)
        result = scanner.scan_root(Path("D:/Movies"))
        # result.units 包含所有检测到的资源单元
    """

    def __init__(
        self,
        extensions: FrozenSet[str],
        exclude_patterns: FrozenSet[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """初始化扫描器。

        参数:
            extensions: 有效的媒体文件扩展名集合。
            exclude_patterns: 扫描时排除的文件夹名称集合。
            progress_callback: 进度回调函数 (当前数, 总数)。
        """
        self._extensions = extensions
        self._exclude_patterns = exclude_patterns
        self._progress_callback = progress_callback
        self._cancelled = False

    def cancel(self) -> None:
        """取消当前扫描。"""
        self._cancelled = True

    def scan_root(self, root_path: Path) -> ScanResult:
        """扫描单个媒体库根目录。

        参数:
            root_path: 媒体库根目录的绝对路径。

        返回:
            ScanResult: 不可变的扫描结果。

        异常:
            AccessDeniedError: 无权限访问目录。
        """
        root_path = normalize_path(root_path)
        if not root_path.exists():
            raise ScanError(f"目录不存在: {root_path}")
        if not root_path.is_dir():
            raise ScanError(f"路径不是目录: {root_path}")

        # 第一步：自底向上遍历，找出所有包含媒体文件的文件夹
        try:
            unit_paths = self._find_resource_units(root_path)
        except PermissionError as e:
            raise AccessDeniedError(str(root_path)) from e

        # 第二步：收集每个单元的媒体文件
        errors: list[str] = []
        all_files: list[DiscoveredFile] = []
        total_files = 0
        total_size = 0
        units: list[ResourceUnit] = []
        unit_path_set = set(unit_paths)  # 用于排除同级子单元

        for i, unit_path in enumerate(unit_paths):
            if self._cancelled:
                break

            try:
                files_in_unit = self._collect_files_in_dir(unit_path, root_path, unit_path_set)
                if files_in_unit:
                    unit = ResourceUnit(
                        path=unit_path,
                        name=unit_path.name or unit_path.stem or str(unit_path),
                        files=tuple(files_in_unit),
                        is_leaf=self._is_leaf_unit(unit_path),
                        parent_root=root_path,
                    )
                    units.append(unit)
                    all_files.extend(files_in_unit)
                    total_files += len(files_in_unit)
                    total_size += unit.total_size

            except PermissionError:
                errors.append(f"无权限读取: {unit_path}")

            if self._progress_callback:
                self._progress_callback(i + 1, len(unit_paths))

        result = ScanResult(
            root_path=root_path,
            units=tuple(units),
            total_files=total_files,
            total_size_bytes=total_size,
            errors=tuple(errors),
        )
        logger.info(f"扫描完成: {root_path} → {len(units)} 个资源单元, {total_files} 个文件")
        return result

    def scan_roots(self, root_paths: list[Path]) -> list[ScanResult]:
        """扫描多个媒体库根目录。

        参数:
            root_paths: 根目录路径列表。

        返回:
            每个根目录对应一个 ScanResult 的列表。
        """
        results: list[ScanResult] = []
        for path in root_paths:
            try:
                result = self.scan_root(path)
                results.append(result)
            except (ScanError, AccessDeniedError) as e:
                logger.error(f"扫描失败: {path} - {e}")
                results.append(ScanResult(
                    root_path=path,
                    units=(),
                    total_files=0,
                    total_size_bytes=0,
                    errors=(str(e),),
                ))
        return results

    # ============================================================
    # 内部方法
    # ============================================================

    def _filter_dirnames(self, dirnames: list[str]) -> list[str]:
        """过滤排除列表和隐藏目录中的目录名。"""
        return [
            d for d in dirnames
            if d not in self._exclude_patterns and not d.startswith('.')
        ]

    def _find_resource_units(self, root: Path) -> list[Path]:
        """自底向上找出所有包含媒体文件的文件夹，去掉嵌套的子单元。

        判定逻辑:
            - 若文件夹内直接包含媒体文件 → 标记为候选单元
            - 对于父子候选的嵌套关系：
              1. 父只有 1 个子候选 → 吞并（子文件夹归入父单元）
              2. 父有多个子候选 → 子候选各自独立（父只是松散容器）
            - 文件夹名在排除列表中 → 跳过
        """
        candidates: set[Path] = set()

        for dirpath, dirnames, filenames in os.walk(str(root), topdown=False):
            current = Path(dirpath)
            # 排除隐藏目录和排除列表中的目录
            if current.name in self._exclude_patterns or current.name.startswith('.'):
                continue
            media_files_in_this_dir = [
                f for f in filenames
                if is_media_file(current / f, self._extensions)
            ]

            if media_files_in_this_dir:
                candidates.add(current)

        if not candidates:
            return []

        # ---- 嵌套单子目录提升 ----
        # 如果一个非候选父文件夹内有且仅有一个候选子文件夹，
        # 将父文件夹提升为候选（解决 "父无媒体/子有媒体" 时子文件夹名无意义的问题）
        promoted: set[Path] = set()
        for c in list(candidates):
            if c == root:
                continue  # 根本身不参与提升
            parent = c.parent
            if parent == root or parent in candidates or parent in promoted:
                continue
            # 检查 parent 下有几个直接候选子目录
            direct_child_candidates = [
                x for x in candidates if x != c and x.parent == parent
            ]
            if len(direct_child_candidates) == 0:
                # 唯一候选子 → 提升父
                candidates.remove(c)
                candidates.add(parent)
                promoted.add(parent)

        # ---- 跳过单文件叶子单元 ----
        # 文件夹内只有零散文件（无子目录、少于最少文件数），跳过避免产生无意义单元
        MIN_FILES_FOR_LEAF_UNIT = 2
        for c in list(candidates):
            if c == root or c in promoted:
                continue
            try:
                entries = list(c.iterdir())
                has_subdirs = any(
                    e.is_dir() and not e.name.startswith('.')
                    for e in entries
                )
                if has_subdirs:
                    continue  # 有子目录的不是叶子单元
                file_count = sum(
                    1 for e in entries
                    if e.is_file() and is_media_file(e, self._extensions)
                )
                if file_count < MIN_FILES_FOR_LEAF_UNIT:
                    candidates.remove(c)
            except OSError:
                continue

        # 计算每个候选有多少个子候选
        def count_child_candidates(parent: Path) -> int:
            count = 0
            for c in candidates:
                if c == parent:
                    continue
                try:
                    c.relative_to(parent)
                    count += 1
                except ValueError:
                    continue
            return count

        # 过滤：父候选吞并子候选的条件是父只有1个子候选
        # （父有多个子候选时，子候选各自独立，例如"网友自拍"下有多个"片段X"）
        removed: set[Path] = set()
        for child in sorted(candidates, key=lambda p: str(p)):
            if child in removed:
                continue
            for parent in candidates:
                if parent == child or parent in removed:
                    continue
                try:
                    child.relative_to(parent)
                    # child 是 parent 的子目录
                    if count_child_candidates(parent) <= 1:
                        removed.add(child)
                    break  # 只检查最近的父候选
                except ValueError:
                    continue

        top_level = [p for p in candidates if p not in removed]

        # ---- 跳过仅有零散文件的容器单元 ----
        # 如果候选文件夹里只有少量文件（≤2），而所有子目录都是独立场景单元，
        # 跳过容器（它的零散文件由上级单元收集）
        for c in list(top_level):
            if c == root:
                continue
            try:
                entries = list(c.iterdir())
                direct_media = [
                    e for e in entries
                    if e.is_file() and is_media_file(e, self._extensions)
                ]
                if len(direct_media) > 2:
                    continue  # 文件足够多，保留
                # 检查子目录中是否有独立单元
                child_candidates = [
                    e for e in entries
                    if e.is_dir() and e in candidates
                ]
                if not child_candidates:
                    continue  # 没有子单元 → 不用跳过
                # 所有含媒体的子目录都是独立单元 → 本文件夹只是容器
                top_level.remove(c)
            except OSError:
                continue

        return sorted(top_level, key=lambda p: str(p))

    def _collect_files_in_dir(self, directory: Path, root: Path,
                             unit_paths: set[Path] | None = None) -> list[DiscoveredFile]:
        """递归收集目录及子目录下的所有媒体文件。

        参数:
            directory: 要收集的目录（作为资源单元的顶级目录）。
            root: 媒体库根目录（用于计算相对路径）。
            unit_paths: 同级单元路径集合，遇到这些路径时会跳过。
        """
        discovered: list[DiscoveredFile] = []
        if unit_paths is None:
            unit_paths = set()

        try:
            for dirpath, dirnames, filenames in os.walk(str(directory), topdown=True):
                dirnames[:] = self._filter_dirnames(dirnames)
                # 跳过属于其他独立单元的子目录
                dirnames[:] = [
                    d for d in dirnames
                    if (Path(dirpath) / d) not in unit_paths
                ]
                current = Path(dirpath)
                for fname in filenames:
                    entry = current / fname
                    if is_media_file(entry, self._extensions):
                        try:
                            relative = entry.relative_to(root)
                        except ValueError:
                            continue
                        try:
                            stat = entry.stat()
                            if stat.st_size == 0:
                                continue  # 跳过空文件（无效/残留文件）
                            mt = get_media_type(entry.suffix)
                            media_type = mt.value if mt else "unknown"
                            df = DiscoveredFile(
                                path=entry,
                                filename=entry.name,
                                extension=entry.suffix.lower(),
                                media_type=media_type,
                                size_bytes=stat.st_size,
                                relative_to_root=relative,
                            )
                            discovered.append(df)
                        except OSError:
                            pass
        except PermissionError:
            raise

        return discovered

    def _is_leaf_unit(self, unit_path: Path) -> bool:
        """判断该资源单元是否为叶子单元。

        叶子单元：该单元路径下没有任何子文件夹包含媒体文件。
        （即所有媒体文件都直接位于 unit_path 下，不存在更深层的媒体子目录）
        """
        for dirpath, dirnames, filenames in os.walk(str(unit_path)):
            dirnames[:] = self._filter_dirnames(dirnames)
            current = Path(dirpath)
            if current == unit_path:
                continue  # 跳过单元目录自身，只看子目录
            if any(
                is_media_file(current / f, self._extensions)
                for f in filenames
            ):
                return False
        return True
