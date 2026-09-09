# -*- coding: utf-8 -*-
"""
查重引擎 —— 资源单元级别的两两比对与文件级匹配。

核心算法：
1. 对每对资源单元，执行多策略匹配（人脸 → MD5 → 视频帧 → pHash → dHash）
2. 一对一贪婪匹配（每个文件 A/B 最多各匹配一次，保证杰卡德指数 ≤ 1.0）
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
from app.utils.hash_helpers import hamming_distance

logger = logging.getLogger(__name__)

# ---- 视频帧级匹配参数 ----
# 每条视频参与比对的帧数上限（按索引均匀采样）。同内容不同码率/分辨率的
# 视频抽帧时间戳一致，采样后仍然对齐；极端时长差异属已知限制。
VIDEO_FRAME_MAX_SAMPLES = 32
# 匹配帧数占“较短视频帧数”的比例下限
VIDEO_FRAME_MATCH_RATIO = 0.5
# 最少匹配帧数（短视频自动放宽到 min(该值, 两视频帧数)）
VIDEO_FRAME_MIN_MATCHES = 2


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
        file_count_ratio_limit: float = 0.05,
    ) -> None:
        """初始化查重引擎。

        参数:
            jaccard_threshold: 杰卡德指数阈值 [0.0, 1.0]。
            phash_hamming_threshold: pHash 汉明距离阈值。
            dhash_hamming_threshold: dHash 汉明距离阈值。
            face_distance_threshold: 人脸欧氏距离阈值。
            face_enabled: 是否启用人脸比对。
            registry: 哈希算法注册器。
            file_count_ratio_limit: 单元文件数比例下限（默认 0.05 = 1/20，
                即两单元文件数相差 20 倍以上时杰卡德指数不可能达标，直接跳过）。
        """
        self._jaccard_threshold = jaccard_threshold
        self._phash_threshold = phash_hamming_threshold
        self._dhash_threshold = dhash_hamming_threshold
        self._face_threshold = face_distance_threshold
        self._face_enabled = face_enabled
        self._registry = registry or create_default_registry()
        self._file_count_ratio_limit = file_count_ratio_limit
        self._cancelled = False

    def cancel(self) -> None:
        """取消当前查重任务。"""
        self._cancelled = True

    def _reset_cancel(self) -> None:
        """重置取消状态，允许实例复用。"""
        self._cancelled = False

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

        # ---- 早期过滤：文件数比例差异过大则跳过 ----
        n_a, n_b = len(files_a), len(files_b)
        ratio = n_a / n_b if n_b > n_a else n_b / n_a
        if ratio < self._file_count_ratio_limit:  # 默认 20 倍以上差异
            logger.debug(
                f"跳过: [{unit_a_name}] vs [{unit_b_name}] 文件数差异过大 ({n_a} vs {n_b})"
            )
            return None

        matches: list[FileMatch] = []
        # 一对一贪婪匹配：每个 A 文件、每个 B 文件最多各被匹配一次。
        # 若只约束 B 侧，同一 A 文件可被 md5 与 phash 各匹配一次，
        # 杰卡德指数会超过 1.0（假阳性）；反之同一内容的多个副本
        # 若只保留一条匹配，则会漏报（假阴性）。
        matched_a_indices: set[int] = set()
        matched_b_indices: set[int] = set()

        # 人脸匹配优先（最可靠），然后 MD5 精确，最后 pHash/dHash 备用
        if self._face_enabled:
            matches.extend(self._match_by_face(
                files_a, files_b, matched_a_indices, matched_b_indices))

        for match_type, match_func in [
            ("md5", self._match_by_md5),
            ("video", self._match_by_video_frames),
            ("phash", self._match_by_perceptual_hash),
            ("dhash", self._match_by_dhash),
        ]:
            if self._cancelled:
                return None

            matches.extend(match_func(
                files_a, files_b, matched_a_indices, matched_b_indices))

        # 计算杰卡德指数
        n_matches = len(matches)
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
        skip_pairs: Optional[set] = None,
    ) -> DedupSession:
        """对多个资源单元执行全量两两比对。

        参数:
            unit_files_map: {unit_id: [file_dict, ...]} 映射。
            unit_names: {unit_id: unit_name} 映射（可选）。
            progress_callback: 进度回调 (已完成对数, 总对数)。
            skip_pairs: 需要跳过的单元对集合（元素为 (小id, 大id)），
                用于已处置/白名单对的重跑跳过。

        返回:
            DedupSession: 包含所有比对结果和重复结果。
        """
        start_time = time.time()
        unit_ids = list(unit_files_map.keys())
        n_units = len(unit_ids)
        skip = skip_pairs or set()
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
                if (uid_a, uid_b) in skip or (uid_b, uid_a) in skip:
                    completed_pairs += 1  # 跳过的对计入进度
                    continue
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
                      matched_a: set[int], matched_b: set[int]) -> list[FileMatch]:
        """MD5 精确匹配（一对一贪婪配对，支持同一内容存在多个副本）。"""
        matches: list[FileMatch] = []
        # md5 → 尚未匹配的 B 文件索引列表（同一内容可在 B 中重复出现）
        b_md5_map: dict[str, list[int]] = {}
        for idx_b, fb in enumerate(files_b):
            if idx_b in matched_b:
                continue
            md5_b = fb.get("md5_hash")
            if md5_b:
                b_md5_map.setdefault(md5_b, []).append(idx_b)

        for idx_a, fa in enumerate(files_a):
            if idx_a in matched_a:
                continue
            md5_a = fa.get("md5_hash")
            if not md5_a:
                continue
            candidates = b_md5_map.get(md5_a)
            if not candidates:
                continue
            idx_b = candidates.pop(0)
            fb = files_b[idx_b]
            matches.append(FileMatch(
                file_a_id=fa["id"],
                file_b_id=fb["id"],
                file_a_path=fa.get("path", ""),
                file_b_path=fb.get("path", ""),
                match_type=MatchType.MD5.value,
                score=1.0,
            ))
            matched_a.add(idx_a)
            matched_b.add(idx_b)
        return matches

    @staticmethod
    def _slice_keys(hash_hex: str, slices: int) -> list[str]:
        """把哈希位串切成 slices 个连续片段，返回每段的位串键。

        鸽巢原理：两个哈希最多有 t 位不同时，切成 t+1 段后至少有一段完全相同。
        因此“每段精确匹配”建索引可在零漏配前提下剪枝，替代原先只查 ±1 邻桶的
        做法（汉明距离 ≤5 不蕴含 8bit 前缀整数差 ≤1：0x00 vs 0x1f 距离 5、
        整数差 31，会被漏掉；实测近重复漏配率 23%）。
        """
        if not hash_hex:
            return []
        bits = len(hash_hex) * 4
        value = int(hash_hex, 16)
        bit_str = bin(value)[2:].zfill(bits)
        width = (bits + slices - 1) // slices
        return [bit_str[k * width:(k + 1) * width] for k in range(slices)]

    def _build_slice_index(self, files: list[dict], exclude: set[int],
                           slices: int, field: str) -> list[dict[str, list[tuple[int, dict, str]]]]:
        """按切片键建立 slices 张倒排表：index[k][key] = [(idx, file, hash)]。"""
        index: list[dict[str, list[tuple[int, dict, str]]]] = [dict() for _ in range(slices)]
        for idx, f in enumerate(files):
            if idx in exclude:
                continue
            h = f.get(field)
            if not h:
                continue
            for k, key in enumerate(self._slice_keys(h, slices)):
                index[k].setdefault(key, []).append((idx, f, h))
        return index

    def _slice_candidates(self, hash_hex: str, index, slices: int) -> list[tuple[int, dict, str]]:
        """取与 hash_hex 在任一切片段完全相同的候选（按索引去重保序）。"""
        seen: set[int] = set()
        out: list[tuple[int, dict, str]] = []
        for k, key in enumerate(self._slice_keys(hash_hex, slices)):
            for cand in index[k].get(key, ()):
                if cand[0] not in seen:
                    seen.add(cand[0])
                    out.append(cand)
        return out

    def _match_by_hamming(self, files_a: list[dict], files_b: list[dict],
                          matched_a: set[int], matched_b: set[int],
                          field: str, threshold: int, match_type: str) -> list[FileMatch]:
        """感知哈希匹配（汉明距离，鸽巢切片索引，一对一配对）。

        将 files_b 按 (threshold+1) 段切片键建倒排表，每个 files_a 只在
        “至少有一段完全相同”的候选中搜索 —— 距离 ≤ threshold 的匹配必然
        出现在候选中（鸽巢原理），零漏配且远小于 O(|A|*|B|)。
        """
        algo = self._registry.get(field)
        if not algo:
            return []

        bits = len(files_a[0].get(field) or "") * 4 or 64
        slices = max(1, min(threshold + 1, bits))
        index = self._build_slice_index(files_b, matched_b, slices, field)

        matches: list[FileMatch] = []
        for idx_a, fa in enumerate(files_a):
            if idx_a in matched_a:
                continue
            hash_a = fa.get(field)
            if not hash_a:
                continue
            bits_a = len(hash_a) * 4

            best_match = None
            best_dist = threshold + 1
            for idx_b, fb, hash_b in self._slice_candidates(hash_a, index, slices):
                if idx_b in matched_b:
                    continue
                try:
                    dist = algo.distance(hash_a, hash_b)
                except Exception:
                    continue
                if dist < best_dist:
                    best_dist = dist
                    best_match = (idx_b, fb)

            if best_match and best_dist <= threshold:
                idx_b, fb = best_match
                score = 1.0 - (best_dist / max(1, bits_a))
                matches.append(FileMatch(
                    file_a_id=fa["id"],
                    file_b_id=fb["id"],
                    file_a_path=fa.get("path", ""),
                    file_b_path=fb.get("path", ""),
                    match_type=match_type,
                    score=round(max(0.0, score), 4),
                ))
                matched_a.add(idx_a)
                matched_b.add(idx_b)
        return matches

    def _match_by_perceptual_hash(self, files_a: list[dict], files_b: list[dict],
                                   matched_a: set[int], matched_b: set[int]) -> list[FileMatch]:
        """pHash 感知哈希匹配（汉明距离 + 鸽巢切片索引）。"""
        return self._match_by_hamming(
            files_a, files_b, matched_a, matched_b,
            field="phash", threshold=self._phash_threshold,
            match_type=MatchType.PHASH.value,
        )

    def _match_by_dhash(self, files_a: list[dict], files_b: list[dict],
                        matched_a: set[int], matched_b: set[int]) -> list[FileMatch]:
        """dHash 差异哈希匹配（汉明距离 + 鸽巢切片索引）。"""
        return self._match_by_hamming(
            files_a, files_b, matched_a, matched_b,
            field="dhash", threshold=self._dhash_threshold,
            match_type=MatchType.DHASH.value,
        )

    def _match_by_video_frames(self, files_a: list[dict], files_b: list[dict],
                                matched_a: set[int], matched_b: set[int]) -> list[FileMatch]:
        """视频帧级匹配 —— 关键帧 pHash 集合相似即视为同一视频。

        视频没有整片 phash/dhash（恒为空串），只有 video_frames 里的抽帧哈希。
        此前这些帧哈希算完即弃，导致重编码/换分辨率的近似视频永远查不出来。
        这里对帧哈希做一对一贪心配对：匹配帧数 ≥ 较短视频帧数的一半即判定为
        同一视频。帧距离沿用 pHash 汉明阈值。
        """
        algo = self._registry.get("phash")
        if not algo:
            return []

        matches: list[FileMatch] = []
        for idx_a, fa in enumerate(files_a):
            if idx_a in matched_a:
                continue
            frames_a = self._sample_frames(fa.get("video_frames"))
            if not frames_a:
                continue

            best = None          # (idx_b, fb, matched_count, frames_b_len, ratio)
            best_ratio = 0.0
            for idx_b, fb in enumerate(files_b):
                if idx_b in matched_b:
                    continue
                frames_b = self._sample_frames(fb.get("video_frames"))
                if not frames_b:
                    continue
                ratio, count = self._frame_set_similarity(frames_a, frames_b, algo)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = (idx_b, fb, count, len(frames_b))

            if best is None:
                continue
            idx_b, fb, count, frames_b_len = best
            required = min(VIDEO_FRAME_MIN_MATCHES, len(frames_a), frames_b_len)
            if count >= required and best_ratio >= VIDEO_FRAME_MATCH_RATIO:
                matches.append(FileMatch(
                    file_a_id=fa["id"],
                    file_b_id=fb["id"],
                    file_a_path=fa.get("path", ""),
                    file_b_path=fb.get("path", ""),
                    # 帧比对本质是 pHash 比对，沿用已有 match_type（避免改库约束）
                    match_type=MatchType.PHASH.value,
                    score=round(min(1.0, best_ratio), 4),
                ))
                matched_a.add(idx_a)
                matched_b.add(idx_b)
        return matches

    @staticmethod
    def _sample_frames(frames) -> list[str]:
        """把帧哈希列表均匀采样到 ≤ VIDEO_FRAME_MAX_SAMPLES 个。

        同内容不同码率/分辨率的视频抽帧时间戳一致（间隔由配置决定），
        按索引等距采样后仍然对齐。
        """
        if not frames:
            return []
        frames = list(frames)
        if len(frames) <= VIDEO_FRAME_MAX_SAMPLES:
            return frames
        step = len(frames) / VIDEO_FRAME_MAX_SAMPLES
        return [frames[int(i * step)] for i in range(VIDEO_FRAME_MAX_SAMPLES)]

    def _frame_set_similarity(self, frames_a: list[str], frames_b: list[str],
                              algo) -> tuple[float, int]:
        """两两帧哈希贪心配对，返回 (匹配比例, 匹配帧数)。"""
        used: set[int] = set()
        matched = 0
        for hash_a in frames_a:
            best_j = None
            best_dist = self._phash_threshold + 1
            for j, hash_b in enumerate(frames_b):
                if j in used:
                    continue
                try:
                    dist = algo.distance(hash_a, hash_b)
                except Exception:
                    continue
                if dist < best_dist:
                    best_dist = dist
                    best_j = j
            if best_j is not None:
                used.add(best_j)
                matched += 1
        denom = max(1, min(len(frames_a), len(frames_b)))
        return matched / denom, matched

    def _match_by_face(self, files_a: list[dict], files_b: list[dict],
                       matched_a: set[int], matched_b: set[int]) -> list[FileMatch]:
        """人脸特征向量匹配 —— 128 维欧氏距离比对。

        对文件 A 中的每张人脸，在文件 B 中查找最近的人脸。
        距离 ≤ 阈值视为同一人。文件级一对一（贪婪策略）。
        """
        if not self._face_enabled:
            return []

        matches: list[FileMatch] = []
        # 记录已被匹配的 B 人脸 (file_id, face_index)，避免同一张脸被重复消费
        matched_faces_b: set[tuple[int, int]] = set()

        for idx_a, fa in enumerate(files_a):
            if idx_a in matched_a:
                continue
            vectors_a = fa.get("face_vectors")
            if not vectors_a:
                continue

            best_overall: Optional[FileMatch] = None
            best_b_idx: Optional[int] = None
            best_overall_dist = self._face_threshold + 1.0
            # 追踪最佳匹配对应的 B 人脸 key，最终匹配确认后才标记为已消费
            best_face_keys: set[tuple[int, int]] = set()

            for idx_b, fb in enumerate(files_b):
                if idx_b in matched_b:
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
                            best_b_idx = idx_b
                            best_face_keys = {face_key}

            if (best_overall is not None and best_b_idx is not None
                    and best_overall_dist <= self._face_threshold):
                matches.append(best_overall)
                matched_faces_b.update(best_face_keys)
                matched_a.add(idx_a)
                matched_b.add(best_b_idx)

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
    def compute_hamming_distance_from_hex(hex_a: str, hex_b: str) -> int:
        """从两个十六进制哈希字符串计算汉明距离（统一实现见 utils.hash_helpers）。"""
        return hamming_distance(hex_a, hex_b)
