# -*- coding: utf-8 -*-
"""
查重引擎 —— 资源单元级别的两两比对与文件级匹配。

核心算法：
1. 对每对资源单元，执行多策略匹配（MD5/pHash/dHash/人脸）
2. 使用贪婪最佳匹配分配（每个文件 A 最多匹配一个文件 B
3. 杰卡德指数 = |匹配对| / (|A| + |B| - |匹配对|)
4. 杰卡德指数 ≥ 阈值 → 视为单元级重复
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

import numpy as np

from app.registry.hash_registry import HashAlgorithmRegistry, create_default_registry
from app.core.hash_engine import FileHashes
from app.utils.constants import MatchType

logger = logging.getLogger(__name__)


# ============================================================
# 不可变结果类型
# ============================================================

@dataclass(frozen=True)
class FileMatch:
    """一对匹配文件的详细信息。"""

    file_a_id: int
    """文件 A 的数据库 ID。"""

    file_b_id: int
    """文件 B 的数据库 ID。"""

    file_a_path: str
    file_b_path: str
    """两个文件的路径。"""

    match_type: str
    """匹配类型：'md5'/'phash'/'dhash'/'face'。"""

    score: float
    """归一化相似度分数 [0.0, 1.0]（1 = 完全相同）。"""


@dataclass(frozen=True)
class UnitComparisonResult:
    """两个资源单元的查重比对结果。"""

    unit_a_id: int
    unit_b_id: int

    unit_a_name: str = ""
    unit_b_name: str = ""

    jaccard_similarity: float = 0.0
    """杰卡德相似度 [0.0, 1.0]。"""

    file_matches: tuple = ()
    """匹配到的文件对列表（不可变元组）。"""

    match_counts: dict = field(default_factory=dict)
    """各类匹配数量统计 {'md5': N, 'phash': M, ...}。"""

    total_files_a: int = 0
    total_files_b: int = 0
    """两个单元各自的文件总数。"""

    @property
    def match_types_str(self) -> str:
        """逗号分隔的匹配类型，如 'md5,phash'。"""
        return ",".join(k for k, v in self.match_counts.items() if v > 0)


@dataclass(frozen=True)
class DedupSession:
    """一次完整查重会话的结果。"""

    comparisons: tuple
    """所有两两比对结果。"""

    duplicates_found: tuple
    """达到阈值的重复结果。"""

    total_units_compared: int
    """参与比对的资源单元数。"""

    elapsed_seconds: float
    """耗时（秒）。"""


# ============================================================
# 查重引擎
# ============================================================

class DedupEngine:
    """查重引擎。

    负责对资源单元间执行多策略文件匹配和杰卡德相似度计算。
    """

    def __init__(
        self,
        jaccard_threshold: float = 0.80,
        phash_hamming_threshold: int = 5,
        dhash_hamming_threshold: int = 5,
        face_distance_threshold: float = 0.6,
        face_enabled: bool = True,
        registry: Optional[HashAlgorithmRegistry] = None,
    ) -> None:
        """初始化查重引擎。

        参数:
            jaccard_threshold: 杰卡德指数阈值 [0.0, 1.0]。
            phash_hamming_threshold: pHash 汉明距离阈值。
            dhash_hamming_threshold: dHash 汉明距离阈值。
            face_distance_threshold: 人脸欧氏距离阈值。
            face_enabled: 是否启用人脸比对。
            registry: 哈希算法注册器。
        """
        self._jaccard_threshold = jaccard_threshold
        self._phash_threshold = phash_hamming_threshold
        self._dhash_threshold = dhash_hamming_threshold
        self._face_threshold = face_distance_threshold
        self._face_enabled = face_enabled
        self._registry = registry or create_default_registry()
        self._cancelled = False

    def cancel(self) -> None:
        """取消当前查重任务。"""
        self._cancelled = True

    def compare_units(
        self,
        files_a: list[dict],
        files_b: list[dict],
        unit_a_id: int,
        unit_b_id: int,
        unit_a_name: str = "",
        unit_b_name: str = "",
    ) -> Optional[UnitComparisonResult]:
        """比较两个资源单元，执行完整的多策略匹配。

        参数:
            files_a: 单元 A 的文件记录列表（dict 含 id, path, md5_hash, phash, dhash 等）。
            files_b: 单元 B 的文件记录列表。
            unit_a_id: 单元 A 的数据库 ID。
            unit_b_id: 单元 B 的数据库 ID。
            unit_a_name: 单元 A 的显示名称。
            unit_b_name: 单元 B 的显示名称。

        返回:
            UnitComparisonResult 如果杰卡德指数 ≥ 阈值，否则 None。
        """
        if not files_a or not files_b:
            return None

        matches: list[FileMatch] = []
        matched_b_indices: set[int] = set()  # 已被匹配的 B 文件索引

        # 人脸匹配优先（最可靠），然后 MD5 精确，最后 pHash/dHash 备用
        if self._face_enabled:
            face_matches = self._match_by_face(files_a, files_b, matched_b_indices)
            for m in face_matches:
                b_idx = self._find_file_index(files_b, m.file_b_id)
                if b_idx not in matched_b_indices:
                    matches.append(m)
                    matched_b_indices.add(b_idx)

        for match_type, match_func in [
            ("md5", self._match_by_md5),
            ("phash", self._match_by_perceptual_hash),
            ("dhash", self._match_by_dhash),
        ]:
            if self._cancelled:
                return None

            new_matches = match_func(files_a, files_b, matched_b_indices)
            for m in new_matches:
                b_idx = self._find_file_index(files_b, m.file_b_id)
                if b_idx not in matched_b_indices:
                    matches.append(m)
                    matched_b_indices.add(b_idx)

        # 计算杰卡德指数
        n_matches = len(matches)
        n_a = len(files_a)
        n_b = len(files_b)
        jaccard = n_matches / (n_a + n_b - n_matches) if (n_a + n_b - n_matches) > 0 else 0.0

        # 统计各类匹配数量
        match_counts: dict[str, int] = {}
        for m in matches:
            match_counts[m.match_type] = match_counts.get(m.match_type, 0) + 1

        result = UnitComparisonResult(
            unit_a_id=unit_a_id,
            unit_b_id=unit_b_id,
            unit_a_name=unit_a_name,
            unit_b_name=unit_b_name,
            jaccard_similarity=round(jaccard, 4),
            file_matches=tuple(matches),
            match_counts=match_counts,
            total_files_a=n_a,
            total_files_b=n_b,
        )

        if jaccard >= self._jaccard_threshold:
            logger.info(
                f"查重命中: [{unit_a_name}] vs [{unit_b_name}] "
                f"杰卡德={jaccard:.3f} 匹配={n_matches}"
            )
            return result

        logger.debug(
            f"未达阈值: [{unit_a_name}] vs [{unit_b_name}] "
            f"杰卡德={jaccard:.3f} 阈值={self._jaccard_threshold}"
        )
        return None

    def run_dedup(
        self,
        unit_files_map: dict[int, list[dict]],
        unit_names: Optional[dict[int, str]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> DedupSession:
        """对多个资源单元执行全量两两比对。

        参数:
            unit_files_map: {unit_id: [file_dict, ...]} 映射。
            unit_names: {unit_id: unit_name} 映射（可选）。
            progress_callback: 进度回调 (已完成对数, 总对数)。

        返回:
            DedupSession: 包含所有比对结果和重复结果。
        """
        start_time = time.time()
        unit_ids = list(unit_files_map.keys())
        n_units = len(unit_ids)
        comparisons: list[UnitComparisonResult] = []
        duplicates: list[UnitComparisonResult] = []

        # 计算总比对对数
        total_pairs = n_units * (n_units - 1) // 2
        completed_pairs = 0

        for i in range(n_units):
            for j in range(i + 1, n_units):
                if self._cancelled:
                    break

                uid_a = unit_ids[i]
                uid_b = unit_ids[j]
                name_a = unit_names.get(uid_a, "") if unit_names else ""
                name_b = unit_names.get(uid_b, "") if unit_names else ""

                try:
                    result = self.compare_units(
                        files_a=unit_files_map[uid_a],
                        files_b=unit_files_map[uid_b],
                        unit_a_id=uid_a,
                        unit_b_id=uid_b,
                        unit_a_name=name_a,
                        unit_b_name=name_b,
                    )
                    if result is not None:
                        comparisons.append(result)
                        if result.jaccard_similarity >= self._jaccard_threshold:
                            duplicates.append(result)
                except Exception as e:
                    logger.error(f"比对失败: 单元{uid_a} vs 单元{uid_b} - {e}")

                completed_pairs += 1
                if progress_callback:
                    progress_callback(completed_pairs, total_pairs)

            if self._cancelled:
                break

        elapsed = time.time() - start_time
        session = DedupSession(
            comparisons=tuple(comparisons),
            duplicates_found=tuple(duplicates),
            total_units_compared=n_units,
            elapsed_seconds=round(elapsed, 2),
        )
        logger.info(
            f"查重完成: {n_units} 个单元，{completed_pairs} 对，"
            f"发现 {len(duplicates)} 组重复，耗时 {elapsed:.1f}s"
        )
        return session

    # ============================================================
    # 匹配策略（内部）
    # ============================================================

    def _match_by_md5(self, files_a: list[dict], files_b: list[dict],
                      exclude_b: set[int]) -> list[FileMatch]:
        """MD5 精确匹配。"""
        matches: list[FileMatch] = []
        # 建立 B 的 MD5 索引
        b_md5_map: dict[str, int] = {}
        for idx_b, fb in enumerate(files_b):
            if idx_b not in exclude_b and fb.get("md5_hash"):
                b_md5_map[fb["md5_hash"]] = idx_b

        for fa in files_a:
            md5_a = fa.get("md5_hash")
            if md5_a and md5_a in b_md5_map:
                idx_b = b_md5_map[md5_a]
                fb = files_b[idx_b]
                matches.append(FileMatch(
                    file_a_id=fa["id"],
                    file_b_id=fb["id"],
                    file_a_path=fa.get("path", ""),
                    file_b_path=fb.get("path", ""),
                    match_type=MatchType.MD5.value,
                    score=1.0,
                ))
        return matches

    def _match_by_perceptual_hash(self, files_a: list[dict], files_b: list[dict],
                                   exclude_b: set[int]) -> list[FileMatch]:
        """pHash 感知哈希匹配（汉明距离）。"""
        phash_algo = self._registry.get("phash")
        if not phash_algo:
            return []

        matches: list[FileMatch] = []
        for fa in files_a:
            ph_a = fa.get("phash")
            if not ph_a:
                continue

            best_match = None
            best_dist = self._phash_threshold + 1

            for idx_b, fb in enumerate(files_b):
                if idx_b in exclude_b:
                    continue
                ph_b = fb.get("phash")
                if not ph_b:
                    continue

                try:
                    dist = phash_algo.distance(ph_a, ph_b)
                    if dist < best_dist:
                        best_dist = dist
                        best_match = (idx_b, fb, dist)
                except Exception:
                    continue

            if best_match and best_dist <= self._phash_threshold:
                idx_b, fb, dist = best_match
                score = 1.0 - (dist / 64.0)  # 归一化到 [0, 1]
                matches.append(FileMatch(
                    file_a_id=fa["id"],
                    file_b_id=fb["id"],
                    file_a_path=fa.get("path", ""),
                    file_b_path=fb.get("path", ""),
                    match_type=MatchType.PHASH.value,
                    score=round(max(0.0, score), 4),
                ))
        return matches

    def _match_by_dhash(self, files_a: list[dict], files_b: list[dict],
                        exclude_b: set[int]) -> list[FileMatch]:
        """dHash 差异哈希匹配（汉明距离）。"""
        dhash_algo = self._registry.get("dhash")
        if not dhash_algo:
            return []

        matches: list[FileMatch] = []
        for fa in files_a:
            dh_a = fa.get("dhash")
            if not dh_a:
                continue

            best_match = None
            best_dist = self._dhash_threshold + 1

            for idx_b, fb in enumerate(files_b):
                if idx_b in exclude_b:
                    continue
                dh_b = fb.get("dhash")
                if not dh_b:
                    continue

                try:
                    dist = dhash_algo.distance(dh_a, dh_b)
                    if dist < best_dist:
                        best_dist = dist
                        best_match = (idx_b, fb, dist)
                except Exception:
                    continue

            if best_match and best_dist <= self._dhash_threshold:
                idx_b, fb, dist = best_match
                score = 1.0 - (dist / 64.0)
                matches.append(FileMatch(
                    file_a_id=fa["id"],
                    file_b_id=fb["id"],
                    file_a_path=fa.get("path", ""),
                    file_b_path=fb.get("path", ""),
                    match_type=MatchType.DHASH.value,
                    score=round(max(0.0, score), 4),
                ))
        return matches

    def _match_by_face(self, files_a: list[dict], files_b: list[dict],
                       exclude_b: set[int]) -> list[FileMatch]:
        """人脸特征向量匹配 —— 128 维欧氏距离比对。

        对文件 A 中的每张人脸，在文件 B 中查找最近的人脸。
        距离 ≤ 阈值视为同一人。每个 B 文件只被匹配一次（贪婪策略）。
        """
        if not self._face_enabled:
            return []

        matches: list[FileMatch] = []
        # 记录已被匹配的 B 文件以及其中的人脸索引
        matched_faces_b: set[tuple[int, int]] = set()

        for fa in files_a:
            vectors_a = fa.get("face_vectors")
            if not vectors_a:
                continue

            best_overall: Optional[FileMatch] = None
            best_overall_dist = self._face_threshold + 1.0
            # 追踪最佳匹配对应的 B 人脸 key，最终匹配确认后才标记为已消费
            best_face_keys: set[tuple[int, int]] = set()

            for idx_b, fb in enumerate(files_b):
                if idx_b in exclude_b:
                    continue
                vectors_b = fb.get("face_vectors")
                if not vectors_b:
                    continue

                # 对 A 中的每张人脸找 B 中的最佳匹配
                for fi_a, vec_a in enumerate(vectors_a):
                    for fi_b, vec_b in enumerate(vectors_b):
                        face_key = (fb["id"], fi_b)
                        if face_key in matched_faces_b:
                            continue

                        dist = self._euclidean_distance(vec_a, vec_b)
                        if dist < best_overall_dist:
                            best_overall_dist = dist
                            score = max(0.0, 1.0 - dist / 2.0)
                            best_overall = FileMatch(
                                file_a_id=fa["id"],
                                file_b_id=fb["id"],
                                file_a_path=fa.get("path", ""),
                                file_b_path=fb.get("path", ""),
                                match_type=MatchType.FACE.value,
                                score=round(score, 4),
                            )
                            best_face_keys = {face_key}

            if best_overall is not None and best_overall_dist <= self._face_threshold:
                matches.append(best_overall)
                matched_faces_b.update(best_face_keys)

        return matches

    @staticmethod
    def _euclidean_distance(vec_a: tuple, vec_b: tuple) -> float:
        """计算两个特征向量的欧氏距离。"""
        import math
        if len(vec_a) != len(vec_b):
            return float('inf')
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(vec_a, vec_b)))

    # ============================================================
    # 辅助方法
    # ============================================================

    @staticmethod
    def _find_file_index(files: list[dict], file_id: int) -> int:
        """根据文件 ID 在列表中查找索引。"""
        for i, f in enumerate(files):
            if f.get("id") == file_id:
                return i
        return -1

    @staticmethod
    def compute_hamming_distance_from_hex(hex_a: str, hex_b: str) -> int:
        """从两个十六进制哈希字符串计算汉明距离。"""
        try:
            return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")
        except Exception:
            return 999
