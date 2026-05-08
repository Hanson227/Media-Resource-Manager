# -*- coding: utf-8 -*-
"""
哈希辅助函数 —— 哈希值距离计算和十六进制转换工具。

提供纯函数工具，不依赖数据库或文件系统。
"""


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """计算两个十六进制哈希字符串之间的汉明距离。

    参数:
        hex_a: 第一个哈希值（十六进制字符串）。
        hex_b: 第二个哈希值（十六进制字符串）。

    返回:
        汉明距离（不同比特位的数量）。
    """
    try:
        return bin(int(hex_a, 16) ^ int(hex_b, 16)).count("1")
    except (ValueError, TypeError):
        return -1


def hamming_similarity(hex_a: str, hex_b: str, max_bits: int = 64) -> float:
    """汉明距离归一化为相似度分数 [0.0, 1.0]。

    参数:
        hex_a: 第一个哈希值。
        hex_b: 第二个哈希值。
        max_bits: 最大可能比特位数。

    返回:
        相似度（1.0 = 完全相同，0.0 = 完全不同）。
    """
    dist = hamming_distance(hex_a, hex_b)
    if dist < 0:
        return 0.0
    return max(0.0, 1.0 - (dist / max_bits))


def is_hex_match(hex_a: str, hex_b: str) -> bool:
    """判断两个十六进制哈希是否完全匹配。"""
    if not hex_a or not hex_b:
        return False
    return hex_a.lower() == hex_b.lower()


def md5_to_display(md5_hex: str) -> str:
    """将 MD5 十六进制字符串格式化为显示用短格式。"""
    if len(md5_hex) >= 16:
        return f"{md5_hex[:8]}...{md5_hex[-8:]}"
    return md5_hex


def normalize_hash_string(hash_str: str) -> str:
    """标准化哈希字符串：去除空格、转小写。"""
    return hash_str.strip().lower()
