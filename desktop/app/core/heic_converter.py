# -*- coding: utf-8 -*-
"""
HEIC 转 JPG 转换器 —— 文件头检测 + Pillow/pillow-heif 转换。

安全设计：
- 转换成功后才删除源文件（失败不删；`delete_source=False` 时完全由调用方处置）
- 同名冲突有明确策略：skip（默认，不覆盖不留垃圾）/ rename / overwrite
- 通过文件头识别真实格式（不依赖扩展名）
- EXIF 方向烘焙进像素（iPhone HEIC 几乎必然带方向标记，不处理就是歪的）

职责边界：本模块只做"解码 → 编码 → 校验"，不碰数据库、不碰回收站策略
（源文件删除/入库/重索引由 `app.services.heic_service` 编排）。
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.utils.image_helpers import open_image_rgb_oriented

logger = logging.getLogger(__name__)

# HEIC/HEIF 文件头特征（前 12 字节的 ftyp box）
_HEIC_FTYP_SIGNATURES = {
    b"heic", b"heix", b"hevc", b"heim",
    b"heis", b"mif1", b"msf1",
}

HEIC_EXTENSIONS = frozenset({".heic", ".heif"})
"""参与 HEIC 处理流程的扩展名（文件头判定才是真凭据，扩展名只用于初筛）。"""

DEFAULT_JPEG_QUALITY = 95
"""转换输出 JPEG 默认质量。"""

CONFLICT_SKIP = "skip"
CONFLICT_RENAME = "rename"
CONFLICT_OVERWRITE = "overwrite"
CONFLICT_MODES = (CONFLICT_SKIP, CONFLICT_RENAME, CONFLICT_OVERWRITE)
"""同名冲突策略：跳过（默认，两边都留）/ 自动改名 / 覆盖。

默认 `skip` 而不是 `rename`：已存在的同名 JPG 极可能就是这个 HEIC 的历史转换
产物，改名会凭空多出一张重复图（本应用本身就是查重工具，制造重复很讽刺）；
覆盖则可能毁掉用户手上有价值的那张 JPG。两者都不可接受，故默认"跳过并如实上报"。
"""

CONFLICT_PATTERN = "^(" + "|".join(CONFLICT_MODES) + ")$"
"""API 校验用的正则（由 CONFLICT_MODES 拼出，避免两处枚举漂移）。"""

_RENAME_MAX = 99
"""自动改名的序号上限（stem (1).jpg … stem (99).jpg）。"""


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
    cancelled: bool = False
    """是否被取消（取消后未处理的文件不计入任何分项）。"""

    results: list[ConversionResult] = field(default_factory=list)


def resolve_target(source: Path, target_dir: Optional[Path] = None,
                   on_conflict: str = CONFLICT_SKIP,
                   is_taken: Optional[Callable[[Path], bool]] = None
                   ) -> Optional[Path]:
    """按冲突策略为源文件挑选输出路径。

    参数:
        source: 源 HEIC 文件。
        target_dir: 目标目录（默认与源文件同目录）。
        on_conflict: skip / rename / overwrite。
        is_taken: 额外的"已被占用"判定（服务层用于把数据库里已有的路径也算冲突，
            否则改出来的路径会撞 `media_files.path` 唯一约束）。

    返回:
        目标路径；`skip` 策略下冲突时返回 None。
    """
    target_dir = target_dir or source.parent
    base = target_dir / f"{source.stem}.jpg"

    def taken(p: Path) -> bool:
        if p.exists():
            return True
        return bool(is_taken and is_taken(p))

    if on_conflict == CONFLICT_OVERWRITE:
        return base
    if not taken(base):
        return base
    if on_conflict == CONFLICT_RENAME:
        for i in range(1, _RENAME_MAX + 1):
            candidate = target_dir / f"{source.stem} ({i}).jpg"
            if not taken(candidate):
                return candidate
    return None


def convert_single(source: Path, target_dir: Optional[Path] = None,
                   overwrite: bool = False, *,
                   quality: int = DEFAULT_JPEG_QUALITY,
                   on_conflict: Optional[str] = None,
                   delete_source: bool = True,
                   is_taken: Optional[Callable[[Path], bool]] = None
                   ) -> ConversionResult:
    """转换单个 HEIC 文件为 JPG。

    参数:
        source: 源 HEIC 文件路径。
        target_dir: 目标目录（默认与源文件同目录）。
        overwrite: 兼容旧参数的简写；`on_conflict=None` 时 True→overwrite、False→skip。
        quality: JPEG 质量（1-100）。
        on_conflict: skip / rename / overwrite（显式给出时优先于 overwrite）。
        delete_source: 成功后是否删除源文件。服务层传 False，由它按
            "回收站优先，不可用则保留" 的策略处置。
        is_taken: 额外的占用判定（见 resolve_target）。

    返回:
        ConversionResult。
    """
    if not source.is_file():
        return ConversionResult(source=source, success=False, error="文件不存在")

    # 文件头校验
    if not is_heic_file(source):
        return ConversionResult(source=source, success=False, error="不是 HEIC 格式")

    mode = on_conflict or (CONFLICT_OVERWRITE if overwrite else CONFLICT_SKIP)
    if mode not in CONFLICT_MODES:
        mode = CONFLICT_SKIP

    target = resolve_target(source, target_dir, mode, is_taken)
    if target is None:
        return ConversionResult(source=source, success=False, error="目标文件已存在",
                                skipped=True)

    quality = max(1, min(100, int(quality)))

    try:
        from PIL import Image

        # 解码 + EXIF 方向烘焙（HEIC/HEIF 依赖 pillow-heif，注册在 image_helpers）
        rgb_img = open_image_rgb_oriented(source)
        if rgb_img is None:
            return ConversionResult(
                source=source, success=False,
                error="无法解码（缺少 pillow-heif 或文件损坏）")

        exif_bytes = None
        try:
            exif = rgb_img.getexif()
            if exif:
                exif_bytes = exif.tobytes()   # exif_transpose 已摘掉方向标记
        except Exception as e:
            logger.debug(f"读取 EXIF 失败 {source}: {e}")

        try:
            save_kwargs = {"quality": quality}
            if exif_bytes:
                save_kwargs["exif"] = exif_bytes
            rgb_img.save(target, "JPEG", **save_kwargs)
        finally:
            rgb_img.close()
        logger.info(f"HEIC 转换成功: {source} → {target}")

        # 验证生成的文件
        if not target.is_file() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            return ConversionResult(source=source, success=False, error="转换生成的文件无效")

        # 进一步验证文件完整性
        try:
            with Image.open(target) as verify_img:
                verify_img.verify()
        except Exception:
            target.unlink(missing_ok=True)
            return ConversionResult(source=source, success=False, error="转换生成的文件已损坏")

        # 删除源文件（转换成功后才删）
        if delete_source:
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
                  progress_callback=None, *,
                  quality: int = DEFAULT_JPEG_QUALITY,
                  on_conflict: Optional[str] = None,
                  delete_source: bool = True,
                  cancelled: Optional[Callable[[], bool]] = None,
                  is_taken: Optional[Callable[[Path], bool]] = None) -> ConvertSummary:
    """批量转换 HEIC 文件。

    参数:
        sources: 源文件路径列表。
        overwrite: 兼容旧参数（见 convert_single）。
        progress_callback: 进度回调 (completed, total, current_result)。
        quality: JPEG 质量。
        on_conflict: 冲突策略（见 convert_single）。
        delete_source: 成功后是否删除源文件。
        cancelled: 取消探测；返回 True 时**停止处理剩余文件**。
            旧实现只在循环结束后检查一次中断标志，"取消"其实不生效。
        is_taken: 额外的占用判定（见 resolve_target）。

    返回:
        ConvertSummary（被取消时 `cancelled=True`）。
    """
    summary = ConvertSummary(total=len(sources))
    total = len(sources)

    for i, src in enumerate(sources):
        if cancelled and cancelled():
            summary.cancelled = True
            logger.info(f"HEIC 批量转换已取消: 已处理 {i}/{total}")
            break

        result = convert_single(
            src, overwrite=overwrite, quality=quality, on_conflict=on_conflict,
            delete_source=delete_source, is_taken=is_taken,
        )
        summary.results.append(result)
        if result.success:
            summary.success += 1
        elif result.skipped:
            summary.skipped += 1
        else:
            summary.failed += 1

        if progress_callback:
            progress_callback(i + 1, total, result)

    return summary
