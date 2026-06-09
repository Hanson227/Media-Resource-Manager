# -*- coding: utf-8 -*-
"""
哈希算法注册器 —— 工厂 + 注册模式实现。

提供可插拔的哈希算法框架，通过注册机制支持
MD5、感知哈希（pHash）、差异哈希（dHash）等算法。
所有算法使用纯 numpy/Pillow 实现，无需 scipy/imagehash 依赖。
"""

import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
from PIL import Image

from app.registry.base import Registry

logger = logging.getLogger(__name__)

# 文件读取分块大小
_CHUNK_SIZE = 64 * 1024  # 64KB


# ============================================================
# 抽象基类
# ============================================================

class HashAlgorithm(ABC):
    """哈希算法抽象基类。

    每种哈希算法应实现：
    - compute(): 对文件计算哈希值
    - distance(): 比较两个哈希值的差异距离

    属性:
        name: 算法唯一名称（如 'md5', 'phash'）
        label: 算法显示名称（中文）
    """

    name: str = ""
    label: str = ""

    @abstractmethod
    def compute(self, file_path: Path) -> str:
        """计算文件哈希值。

        参数:
            file_path: 要计算哈希的文件路径。

        返回:
            哈希值的十六进制字符串表示。
        """
        ...

    @abstractmethod
    def distance(self, hash_a: str, hash_b: str) -> int:
        """计算两个哈希值之间的差异距离。

        参数:
            hash_a: 第一个哈希值。
            hash_b: 第二个哈希值。

        返回:
            距离值（非负整数），值越小越相似。
        """
        ...

    def is_match(self, hash_a: str, hash_b: str, threshold: int) -> bool:
        """判断两个哈希值是否匹配。

        参数:
            hash_a: 第一个哈希值。
            hash_b: 第二个哈希值。
            threshold: 距离阈值，≤ 此值视为匹配。

        返回:
            True 如果距离 ≤ 阈值。
        """
        return self.distance(hash_a, hash_b) <= threshold


# ============================================================
# 哈希算法注册器
# ============================================================

class HashAlgorithmRegistry(Registry[str, HashAlgorithm]):
    """哈希算法注册器，以算法名称为键。

    用法:
        >>> reg = HashAlgorithmRegistry()
        >>> reg.register("md5", MD5Hash())
        >>> algo = reg.get("md5")
        >>> algo.compute(Path("file.jpg"))
    """

    pass


# ============================================================
# 纯 numpy 工具函数
# ============================================================

# Pre-compute DCT matrices up to size 64 (cached)
_DCT_MATRICES: dict[int, np.ndarray] = {}


def _get_dct_matrix(N: int) -> np.ndarray:
    """返回 N×N DCT-II 变换矩阵（缓存复用）。"""
    if N not in _DCT_MATRICES:
        k = np.arange(N, dtype=np.float64)
        n = np.arange(N, dtype=np.float64)
        _DCT_MATRICES[N] = np.cos(np.pi * k[:, None] * (2 * n + 1) / (2 * N))
    return _DCT_MATRICES[N]


def _bits_to_hex(bits: np.ndarray) -> str:
    """将布尔数组转为十六进制字符串（兼容 imagehash 格式）。"""
    # bits: flat 1-D bool array, length = hash_size * hash_size
    # 转为整数再 hex
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    return hex(val)[2:].zfill(len(bits) // 4)


def _hamming_distance(hex_a: str, hex_b: str) -> int:
    """计算两个十六进制哈希字符串的汉明距离。"""
    try:
        return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")
    except Exception:
        return 999


# ============================================================
# 具体算法实现
# ============================================================

class MD5Hash(HashAlgorithm):
    """MD5 哈希算法 —— 用于精确匹配。

    分块读取文件以支持大文件，避免一次性加载到内存。
    """

    name = "md5"
    label = "MD5"

    def compute(self, file_path: Path) -> str:
        """计算文件的 MD5 哈希值。"""
        md5 = hashlib.md5()
        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    md5.update(chunk)
        except Exception as e:
            logger.error(f"MD5 计算失败: {file_path} - {e}")
            raise
        return md5.hexdigest()

    def distance(self, hash_a: str, hash_b: str) -> int:
        """MD5 距离：相同返回 0，不同返回 1。"""
        return 0 if hash_a == hash_b else 1


class PHash(HashAlgorithm):
    """感知哈希（pHash）—— 基于频率域（DCT）的图像相似度算法。

    纯 numpy 实现，无需 scipy。
    """

    name = "phash"
    label = "感知哈希"

    def __init__(self, hash_size: int = 8) -> None:
        """初始化。

        参数:
            hash_size: 哈希尺寸，值越大越精确但计算越慢。
        """
        self._hash_size = hash_size
        self._highfreq_factor = 4  # 高分辨率因子

    def compute(self, file_path: Path) -> str:
        """计算图片的感知哈希值。

        DCT-based pHash:
        1. 转灰度 + 缩放到 hash_size*4 × hash_size*4
        2. 计算 2D DCT-II
        3. 取左上角 hash_size×hash_size 低频系数
        4. 与中位数比较 → 二进制哈希
        """
        try:
            img = Image.open(file_path).convert("L")
            size = self._hash_size * self._highfreq_factor
            img = img.resize((size, size), Image.Resampling.LANCZOS)
            pixels = np.array(img, dtype=np.float64)

            # 2D DCT-II via separable transform
            T = _get_dct_matrix(size)
            dct = T @ pixels @ T.T

            # 取左上角低频
            dct_low = dct[:self._hash_size, :self._hash_size]

            # 与中位数比较
            median = np.median(dct_low)
            bits = (dct_low > median).flatten()

            return _bits_to_hex(bits)
        except Exception as e:
            logger.error(f"pHash 计算失败: {file_path} - {e}")
            raise

    def compute_from_image(self, img: Image.Image) -> str:
        """从 PIL Image 对象直接计算 pHash（用于视频帧）。"""
        img = img.convert("L")
        size = self._hash_size * self._highfreq_factor
        img = img.resize((size, size), Image.Resampling.LANCZOS)
        pixels = np.array(img, dtype=np.float64)
        T = _get_dct_matrix(size)
        dct = T @ pixels @ T.T
        dct_low = dct[:self._hash_size, :self._hash_size]
        median = np.median(dct_low)
        bits = (dct_low > median).flatten()
        return _bits_to_hex(bits)

    def distance(self, hash_a: str, hash_b: str) -> int:
        """计算汉明距离。"""
        return _hamming_distance(hash_a, hash_b)


class DHash(HashAlgorithm):
    """差异哈希（dHash）—— 基于像素水平梯度。

    纯 numpy 实现，无需 imagehash。
    """

    name = "dhash"
    label = "差异哈希"

    def __init__(self, hash_size: int = 8) -> None:
        """初始化。

        参数:
            hash_size: 哈希尺寸。
        """
        self._hash_size = hash_size

    def compute(self, file_path: Path) -> str:
        """计算图片的差异哈希值。

        1. 转灰度 + 缩放到 (hash_size+1) × hash_size
        2. 逐行比较相邻像素：左边 > 右边 → 1, 否则 → 0
        3. 得到 hash_size×hash_size 位
        """
        try:
            img = Image.open(file_path).convert("L")
            img = img.resize((self._hash_size + 1, self._hash_size),
                             Image.Resampling.LANCZOS)
            pixels = np.array(img, dtype=np.float64)

            # 水平梯度：比较相邻列
            diff = pixels[:, 1:] > pixels[:, :-1]
            bits = diff.flatten()

            return _bits_to_hex(bits)
        except Exception as e:
            logger.error(f"dHash 计算失败: {file_path} - {e}")
            raise

    def distance(self, hash_a: str, hash_b: str) -> int:
        """计算汉明距离。"""
        return _hamming_distance(hash_a, hash_b)


# ============================================================
# 默认注册器工厂
# ============================================================

def create_default_registry(phash_size: int = 8, dhash_size: int = 8) -> HashAlgorithmRegistry:
    """创建预设的哈希算法注册器。

    参数:
        phash_size: pHash 的哈希尺寸。
        dhash_size: dHash 的哈希尺寸。

    返回:
        已注册 MD5、pHash、dHash 的注册器。
    """
    registry = HashAlgorithmRegistry()
    registry.register("md5", MD5Hash())
    registry.register("phash", PHash(hash_size=phash_size))
    registry.register("dhash", DHash(hash_size=dhash_size))
    return registry
