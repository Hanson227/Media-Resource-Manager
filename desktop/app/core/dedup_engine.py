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
import math
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Optional, Sequence

import numpy as np

from app.registry.hash_registry import HashAlgorithmRegistry, create_default_registry
from app.core.hash_engine import FileHashes
from app.utils.constants import FACE_SIMILARITY_THRESHOLD, MatchLevel, MatchType
from app.utils.hash_helpers import hamming_distance

logger = logging.getLogger(__name__)

# ---- 视频帧级匹配参数 ----
# 每条视频参与比对的帧数上限（按索引均匀采样）。同内容不同码率/分辨率的
# 视频抽帧时间戳一致，采样后仍然对齐；极端时长差异属已知限制。
VIDEO_FRAME_MAX_SAMPLES = 32
# 匹配帧数占“较短视频帧数”的比例下限。
# 取 0.75：真正的重编码/裁剪同源视频几乎逐帧对应，比例接近 1.0；
# 而 0.5 会把“仅共享一半抽帧”的不同视频判为同一视频（假阳性 → 误导用户删文件）。
VIDEO_FRAME_MATCH_RATIO = 0.75
# 最少匹配帧数：证据量低于此值不足以判定同一视频。
VIDEO_FRAME_MIN_MATCHES = 3
# 抽帧数下限：短于该帧数的视频不参与帧级判定。否则“1 帧恰好相同”即可
# 判为同一视频（分母 max(1, min(...)) = 1 → 比例恒为 1.0）。
# 这类短视频的精确重复仍由 MD5 兜住，不会漏报完全相同的文件。
VIDEO_FRAME_MIN_FRAMES = 3


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

    face_hints: tuple = ()
    """人脸相似提示（不计入杰卡德）。

    人脸向量回答的是“是不是同一个人”，不是“是不是同一个文件”。
    把它计入文件级匹配会让同一演员的不同作品被判为重复单元，
    因此只作为辅助线索单独返回，供界面提示，不参与重复判定。
    """

    level: str = MatchLevel.DUPLICATE.value
    """命中等级：duplicate（建议处置）/ related（仅提醒，见 MatchLevel）。"""

    @property
    def evidence_count(self) -> int:
        """证据总数 = 计数匹配 + 人脸线索。用于排序与"是否值得提醒"。"""
        return len(self.file_matches) + len(self.face_hints)

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
    """达到"重复"阈值的单元对（建议处置）。"""

    total_units_compared: int
    """参与比对的资源单元数。"""

    elapsed_seconds: float
    """耗时（秒）。"""

    related_found: tuple = ()
    """疑似相关但未达重复阈值的单元对（仅提醒，不建议处置）。

    包括同演员、同场景、部分文件重叠等情形 —— 用户希望知情但不打算删除。
    """


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
        face_similarity_threshold: float = FACE_SIMILARITY_THRESHOLD,
        face_enabled: bool = True,
        registry: Optional[HashAlgorithmRegistry] = None,
        file_count_ratio_limit: float = 0.05,
        related_min_matches: int = 2,
    ) -> None:
        """初始化查重引擎。

        参数:
            jaccard_threshold: 杰卡德指数阈值 [0.0, 1.0]。
            phash_hamming_threshold: pHash 汉明距离阈值。
            dhash_hamming_threshold: dHash 汉明距离阈值。
            face_similarity_threshold: 人脸余弦相似度阈值，≥ 此值视为同一人物。
            face_enabled: 是否启用人脸比对。
            registry: 哈希算法注册器。
            file_count_ratio_limit: 单元文件数比例下限（默认 0.05 = 1/20，
                即两单元文件数相差 20 倍以上时杰卡德指数不可能达标，直接跳过）。
            related_min_matches: 未达重复阈值时，"疑似相关"所需的最少证据数
                （计数匹配 + 人脸线索）。
        """
        self._jaccard_threshold = jaccard_threshold
        self._phash_threshold = phash_hamming_threshold
        self._dhash_threshold = dhash_hamming_threshold
        self._face_threshold = face_similarity_threshold
        self._face_enabled = face_enabled
        self._registry = registry or create_default_registry()
        self._file_count_ratio_limit = file_count_ratio_limit
        self._related_min_matches = max(1, related_min_matches)
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

        # 精确身份优先：MD5 → 视频帧 → pHash → dHash。
        # 顺序有意义：matched_* 跨策略共享，先跑的策略会“消费”文件对。
        # 若把模糊策略排到 MD5 之前，字节级一致的铁证会被记为模糊匹配，
        # 甚至抢占到错误的对手方，导致真正的重复对被漏配。
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

        # 人脸只作为辅助提示：用独立的 matched 集合计算，既不消费文件对，
        # 也不计入杰卡德 —— 见 UnitComparisonResult.face_hints 的说明。
        face_hints: tuple = ()
        if self._face_enabled:
            face_hints = tuple(self._match_by_face(
                files_a, files_b, set(), set()))

        # 计算杰卡德指数
        n_matches = len(matches)
        jaccard = n_matches / (n_a + n_b - n_matches) if (n_a + n_b - n_matches) > 0 else 0.0

        # 统计各类匹配数量
        match_counts: dict[str, int] = {}
        for m in matches:
            match_counts[m.match_type] = match_counts.get(m.match_type, 0) + 1

        result_dup = UnitComparisonResult(
            unit_a_id=unit_a_id,
            unit_b_id=unit_b_id,
            unit_a_name=unit_a_name,
            unit_b_name=unit_b_name,
            jaccard_similarity=round(jaccard, 4),
            file_matches=tuple(matches),
            face_hints=face_hints,
            match_counts=match_counts,
            total_files_a=n_a,
            total_files_b=n_b,
        )

        if jaccard >= self._jaccard_threshold:
            logger.info(
                f"查重命中: [{unit_a_name}] vs [{unit_b_name}] "
                f"杰卡德={jaccard:.3f} 匹配={n_matches}"
            )
            return result_dup

        # 未达"重复"阈值，但证据足够 → 记为一档"疑似相关"（仅提醒）。
        # 典型来源：同演员的不同片子（人脸线索）、同场景不同剪辑（帧匹配）。
        if result_dup.evidence_count >= self._related_min_matches:
            logger.info(
                f"疑似相关: [{unit_a_name}] vs [{unit_b_name}] "
                f"杰卡德={jaccard:.3f} 证据={result_dup.evidence_count}"
                f"（匹配{n_matches} + 人脸{len(face_hints)}）"
            )
            return replace(result_dup, level=MatchLevel.RELATED.value)

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
        related: list[UnitComparisonResult] = []

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
                        if result.level == MatchLevel.DUPLICATE.value:
                            duplicates.append(result)
                        elif result.level == MatchLevel.RELATED.value:
                            related.append(result)
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
            related_found=tuple(related),
            total_units_compared=n_units,
            elapsed_seconds=round(elapsed, 2),
        )
        logger.info(
            f"查重完成: {n_units} 个单元，{completed_pairs} 对，"
            f"发现 {len(duplicates)} 组重复、{len(related)} 对疑似相关，"
            f"耗时 {elapsed:.1f}s"
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
        """视频帧级匹配 —— 关键帧 pHash 序列相似才视为同一视频。

        视频没有整片 phash/dhash（恒为空串），只有 video_frames 里的抽帧哈希。
        这里对帧哈希做一对一贪心配对，判定同一视频需同时满足三个条件：

        1. 匹配帧数 ≥ VIDEO_FRAME_MIN_MATCHES；
        2. 匹配比例 ≥ VIDEO_FRAME_MATCH_RATIO（分母为较短视频的帧数）；
        3. 匹配对在时间轴上同序（最长递增子序列 ≥ 比例的帧数）。

        条件 1、3 是后加的：只有比例一条时，“1 帧恰好命中”和
        “帧集合相同但顺序被打乱的剪辑”都会被判为同一视频。
        帧距离沿用 pHash 汉明阈值。

        性能：B 侧所有帧按鸽巢切片建一次倒排索引，每个 A 侧帧只与
        “至少有一段完全相同”的候选帧算精确距离。距离 ≤ 阈值必然出现在
        候选中（鸽巢原理），零漏配；非候选帧本就永远不可能入选贪心配对，
        因此结果与逐帧暴力比对完全一致，但复杂度从 O(|A|·|B|·F²) 降为
        接近线性（见 test_dedup_video_frame_index_equivalence 的差分验证）。
        """
        algo = self._registry.get("phash")
        if not algo:
            return []

        slices = self._phash_threshold + 1
        frames_b_cache: dict[int, list[str]] = {}
        frame_index = self._build_frame_index(
            files_b, matched_b, slices, frames_b_cache)

        matches: list[FileMatch] = []
        for idx_a, fa in enumerate(files_a):
            if idx_a in matched_a:
                continue
            frames_a = self._sample_frames(fa.get("video_frames"))
            # 抽帧太少时证据不足，直接放弃判定（精确重复交给 MD5）
            if len(frames_a) < VIDEO_FRAME_MIN_FRAMES:
                continue

            # 该 A 视频在每个候选 B 视频上可能命中的帧位置
            candidate_positions: dict[int, set[int]] = {}
            for hash_a in frames_a:
                for idx_b, pos in self._frame_candidates(hash_a, frame_index, slices):
                    if idx_b in matched_b:
                        continue
                    candidate_positions.setdefault(idx_b, set()).add(pos)

            best = None          # (idx_b, fb, count, frames_b_len, ratio, pairs)
            best_ratio = 0.0
            # 按 idx_b 升序迭代：候选是发现序（取决于切片命中顺序），
            # 而暴力路径按 files_b 顺序迭代；比例相同时两者必须选中同一个
            # B 视频，否则索引化会改变匹配结果。
            for idx_b in sorted(candidate_positions):
                if idx_b in matched_b:
                    continue  # 同一次 A 循环内可能已被标记
                full_frames = frames_b_cache[idx_b]
                ordered = sorted(candidate_positions[idx_b])
                # 只保留候选帧参与贪心配对（非候选帧距离必然超阈值，永不入选）
                subset = [full_frames[j] for j in ordered]
                ratio, count, sub_pairs = self._frame_set_similarity(
                    frames_a, subset, algo, denom_frames_b=len(full_frames))
                if ratio > best_ratio:
                    # 把子集索引还原成 B 侧真实帧位置，供时序判定使用
                    pairs = [(i_a, ordered[j_b]) for i_a, j_b in sub_pairs]
                    best_ratio = ratio
                    best = (idx_b, files_b[idx_b], count, len(full_frames), pairs)

            if best is None:
                continue
            idx_b, fb, count, frames_b_len, pairs = best
            required = min(VIDEO_FRAME_MIN_MATCHES, len(frames_a), frames_b_len)
            # 时序一致性：同源视频的抽帧在时间轴上应同序，逆序对多说明是
            # 拼剪/混排。允许少量错位（静态画面会有近似帧抢配）。
            monotone = self._longest_increasing([pb for _, pb in pairs])
            monotone_required = max(
                VIDEO_FRAME_MIN_MATCHES,
                math.ceil(VIDEO_FRAME_MATCH_RATIO * count),
            )
            if (count >= required
                    and best_ratio >= VIDEO_FRAME_MATCH_RATIO
                    and monotone >= monotone_required):
                matches.append(FileMatch(
                    file_a_id=fa["id"],
                    file_b_id=fb["id"],
                    file_a_path=fa.get("path", ""),
                    file_b_path=fb.get("path", ""),
                    # 独立记为 video：帧级匹配证据最弱，混记为 phash 会让
                    # 界面无法区分"整图感知哈希命中"与"视频帧命中"。
                    match_type=MatchType.VIDEO.value,
                    score=round(min(1.0, best_ratio), 4),
                ))
                matched_a.add(idx_a)
                matched_b.add(idx_b)
        return matches

    def _build_frame_index(self, files_b: list[dict], exclude: set[int],
                           slices: int,
                           cache: dict[int, list[str]]) -> list[dict]:
        """按切片键建立帧级倒排索引：index[k][key] = [(idx_b, 帧位置), ...]。

        同时把每个 B 视频采样后的帧列表写入 cache（避免重复采样）。
        抽帧数不足 VIDEO_FRAME_MIN_FRAMES 的视频不参与帧级判定，不入索引。
        """
        index: list[dict] = [dict() for _ in range(slices)]
        for idx_b, fb in enumerate(files_b):
            if idx_b in exclude:
                continue
            frames = self._sample_frames(fb.get("video_frames"))
            if len(frames) < VIDEO_FRAME_MIN_FRAMES:
                continue
            cache[idx_b] = frames
            for pos, h in enumerate(frames):
                if not h:
                    continue
                for k, key in enumerate(self._slice_keys(h, slices)):
                    index[k].setdefault(key, []).append((idx_b, pos))
        return index

    @staticmethod
    def _frame_candidates(hash_hex: str, index: list[dict],
                          slices: int) -> list[tuple[int, int]]:
        """返回与 hash_hex 至少共享一个完全相同切片的帧 (idx_b, 帧位置)。

        鸽巢原理保证：距离 ≤ 阈值(切片数-1) 的帧对必然共享至少一段，
        因此候选集合是零漏配的超集。
        """
        seen: set[tuple[int, int]] = set()
        out: list[tuple[int, int]] = []
        for k, key in enumerate(DedupEngine._slice_keys(hash_hex, slices)):
            for cand in index[k].get(key, ()):
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
        return out

    @staticmethod
    def _longest_increasing(seq: Sequence[int]) -> int:
        """最长严格递增子序列长度（耐心排序，O(n log n)）。

        用于帧级匹配的时序一致性判定：把匹配对按 A 侧帧序排列后，
        B 侧索引序列的 LIS 就是“能按时间轴对齐的匹配帧数”。
        """
        import bisect
        tails: list[int] = []
        for x in seq:
            i = bisect.bisect_left(tails, x)
            if i == len(tails):
                tails.append(x)
            else:
                tails[i] = x
        return len(tails)

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
                              algo,
                              denom_frames_b: Optional[int] = None
                              ) -> tuple[float, int, list[tuple[int, int]]]:
        """两两帧哈希贪心配对。

        参数:
            frames_a: A 侧帧哈希。
            frames_b: B 侧参与配对的帧哈希（索引化路径下为候选子集）。
            algo: 哈希算法。
            denom_frames_b: 计算比例时分母使用的 B 侧帧数。索引化路径传入
                B 视频的完整帧数（frames_b 只是候选子集），暴力路径留空。

        返回:
            (匹配比例, 匹配帧数, 匹配对索引列表)。匹配对按 A 侧帧序排列，
            索引是传入 frames_b 的下标，供调用方做时序一致性判定。
        """
        used: set[int] = set()
        pairs: list[tuple[int, int]] = []
        for i_a, hash_a in enumerate(frames_a):
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
                pairs.append((i_a, best_j))
        matched = len(pairs)
        denom_b = len(frames_b) if denom_frames_b is None else denom_frames_b
        denom = max(1, min(len(frames_a), denom_b))
        return matched / denom, matched, pairs

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
            best_overall_sim = self._face_threshold - 1.0
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

                        sim = self._cosine_similarity(vec_a, vec_b)
                        if sim > best_overall_sim:
                            best_overall_sim = sim
                            best_overall = FileMatch(
                                file_a_id=fa["id"],
                                file_b_id=fb["id"],
                                file_a_path=fa.get("path", ""),
                                file_b_path=fb.get("path", ""),
                                match_type=MatchType.FACE.value,
                                score=round(max(0.0, min(1.0, sim)), 4),
                            )
                            best_b_idx = idx_b
                            best_face_keys = {face_key}

            if (best_overall is not None and best_b_idx is not None
                    and best_overall_sim >= self._face_threshold):
                matches.append(best_overall)
                matched_faces_b.update(best_face_keys)
                matched_a.add(idx_a)
                matched_b.add(best_b_idx)

        return matches

    @staticmethod
    def _cosine_similarity(vec_a: tuple, vec_b: tuple) -> float:
        """计算两个人脸特征向量的余弦相似度（SFace 官方度量）。

        旧实现是 OpenFace nn4 + 欧氏距离；换成 SFace 后必须改用余弦相似度，
        阈值也随之变为 FACE_SIMILARITY_THRESHOLD(0.363)。维度不一致返回 -1。
        """
        import math
        if not vec_a or len(vec_a) != len(vec_b):
            return -1.0
        dot = norm_a = norm_b = 0.0
        for a, b in zip(vec_a, vec_b):
            dot += a * b
            norm_a += a * a
            norm_b += b * b
        if norm_a <= 0.0 or norm_b <= 0.0:
            return -1.0
        return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))

    # ============================================================
    # 辅助方法
    # ============================================================

    @staticmethod
    def compute_hamming_distance_from_hex(hex_a: str, hex_b: str) -> int:
        """从两个十六进制哈希字符串计算汉明距离（统一实现见 utils.hash_helpers）。"""
        return hamming_distance(hex_a, hex_b)
