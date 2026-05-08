# -*- coding: utf-8 -*-
"""
哈希算法注册器 —— 工厂 + 注册模式实现。

提供可插拔的哈希算法框架，通过注册机制支持
MD5、感知哈希（pHash）、差异哈希（dHash）等算法。
"""

import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path

import imagehash
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
# 具体算法实现
# ============================================================

class MD5Hash(HashAlgorithm):
    """MD5 哈希算法 —— 用于精确匹配。

    分块读取文件以支持大文件，避免一次性加载到内存。
    """

    name = "md5"
    label = "MD5"

    def compute(self, file_path: Path) -> str:
        """计算文件的 MD5 哈希值。

        参数:
            file_path: 文件路径。

        返回:
            32 位十六进制 MD5 字符串。
        """
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
    """感知哈希（pHash）—— 基于频率域的图像相似度算法。

    使用 imagehash 库实现，对图片的缩放、旋转、亮度调整具有鲁棒性。
    """

    name = "phash"
    label = "感知哈希"

    def __init__(self, hash_size: int = 8) -> None:
        """初始化。

        参数:
            hash_size: 哈希尺寸，值越大越精确但计算越慢。
        """
        self._hash_size = hash_size

    def compute(self, file_path: Path) -> str:
        """计算图片的感知哈希值。

        参数:
            file_path: 图片文件路径。

        返回:
            pHash 十六进制字符串。
        """
        try:
            img = Image.open(file_path)
            img = img.convert("L")  # 转为灰度图
            phash = imagehash.phash(img, hash_size=self._hash_size)
            return str(phash)
        except Exception as e:
            logger.error(f"pHash 计算失败: {file_path} - {e}")
            raise

    def distance(self, hash_a: str, hash_b: str) -> int:
        """计算汉明距离。

        将十六进制字符串转为 imagehash 对象后比较。
        """
        try:
            a = imagehash.hex_to_hash(hash_a)
            b = imagehash.hex_to_hash(hash_b)
            return a - b  # imagehash 重载了减号为汉明距离
        except Exception:
            # 回退：计算字符串差异
            return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


class DHash(HashAlgorithm):
    """差异哈希（dHash）—— 基于像素梯度方向的图像相似度算法。

    计算相邻像素之间的差异，对亮度变化不敏感。
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

        参数:
            file_path: 图片文件路径。

        返回:
            dHash 十六进制字符串。
        """
        try:
            img = Image.open(file_path)
            img = img.convert("L")
            dhash = imagehash.dhash(img, hash_size=self._hash_size)
            return str(dhash)
        except Exception as e:
            logger.error(f"dHash 计算失败: {file_path} - {e}")
            raise

    def distance(self, hash_a: str, hash_b: str) -> int:
        """计算汉明距离。"""
        try:
            a = imagehash.hex_to_hash(hash_a)
            b = imagehash.hex_to_hash(hash_b)
            return a - b
        except Exception:
            return bin(int(hash_a, 16) ^ int(hash_b, 16)).count("1")


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
