# -*- coding: utf-8 -*-
"""
HEIC 转 JPG 转换器 —— 文件头检测 + Pillow/pillow-heif 转换。

安全设计：
- 转换成功后才删除源文件（失败不删）
- 同名冲突提示覆盖/跳过/重命名
- 通过文件头识别真实格式（不依赖扩展名）
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# HEIC/HEIF 文件头特征（前 12 字节的 ftyp box）
_HEIC_FTYP_SIGNATURES = {
    b"heic", b"heix", b"hevc", b"heim",
    b"heis", b"mif1", b"msf1",
}


def is_heic_file(path: Path) -> bool:
    """通过文件头判断是否为 HEIC/HEIF 格式（不只是看扩展名）。"""
    if not path.is_file():
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(12)
        # ftyp box: 4 bytes size + 4 bytes "ftyp" + 4 bytes major_brand
        if len(header) < 12:
            return False
        # 偏移 4-7 应为 b"ftyp"
        if header[4:8] != b"ftyp":
            return False
        # 偏移 8-11 为 major brand
        brand = header[8:12]
        return brand in _HEIC_FTYP_SIGNATURES
    except Exception as e:
        logger.debug(f"文件头读取失败 {path}: {e}")
        return False


@dataclass
class ConversionResult:
    """单个文件的转换结果。"""

    source: Path
    """源 HEIC 文件路径。"""

    target: Optional[Path] = None
    """转换后的 JPG 文件路径（成功时）。"""

    success: bool = False
    """是否转换成功。"""

    error: str = ""
    """错误信息（失败时）。"""

    skipped: bool = False
    """是否因用户选择跳过。"""


@dataclass
class ConvertSummary:
    """一次批量转换的汇总。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    results: list[ConversionResult] = field(default_factory=list)


def convert_single(source: Path, target_dir: Optional[Path] = None,
                   overwrite: bool = False) -> ConversionResult:
    """转换单个 HEIC 文件为 JPG。

    参数:
        source: 源 HEIC 文件路径。
        target_dir: 目标目录（默认与源文件同目录）。
        overwrite: 同名文件存在时是否覆盖。

    返回:
        ConversionResult。
    """
    if not source.is_file():
        return ConversionResult(source=source, success=False, error="文件不存在")

    # 文件头校验
    if not is_heic_file(source):
        return ConversionResult(source=source, success=False, error="不是 HEIC 格式")

    target_dir = target_dir or source.parent
    target = target_dir / f"{source.stem}.jpg"

    # 冲突处理
    if target.exists() and not overwrite:
        return ConversionResult(source=source, success=False, error="目标文件已存在", skipped=True)

    try:
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()

        with Image.open(source) as img:
            img = img.convert("RGB")
            img.save(target, "JPEG", quality=95)
        logger.info(f"HEIC 转换成功: {source} → {target}")

        # 验证生成的文件
        if not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            return ConversionResult(source=source, success=False, error="转换生成的文件无效")

        # 删除源文件（转换成功后才删）
        try:
            source.unlink()
            logger.info(f"已删除源文件: {source}")
        except Exception as e:
            logger.warning(f"源文件删除失败 {source}: {e}")

        return ConversionResult(source=source, target=target, success=True)

    except Exception as e:
        logger.error(f"HEIC 转换失败 {source}: {e}")
        return ConversionResult(source=source, success=False, error=str(e))


def convert_batch(sources: list[Path], overwrite: bool = False,
                  progress_callback=None) -> ConvertSummary:
    """批量转换 HEIC 文件。

    参数:
        sources: 源文件路径列表。
        overwrite: 是否覆盖已存在的目标文件。
        progress_callback: 进度回调 (completed, total, current_result)。

    返回:
        ConvertSummary。
    """
    summary = ConvertSummary(total=len(sources))

    for i, src in enumerate(sources):
        result = convert_single(src, overwrite=overwrite)
        summary.results.append(result)
        if result.success:
            summary.success += 1
        elif result.skipped:
            summary.skipped += 1
        else:
            summary.failed += 1

        if progress_callback:
            progress_callback(i + 1, len(sources), result)

    return summary
