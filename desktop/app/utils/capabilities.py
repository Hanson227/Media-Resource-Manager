# -*- coding: utf-8 -*-
"""运行能力探测 —— 一套代码，功能差异只由"包里到底有什么"决定。

设计原则（重要，别退回旧做法）：
    功能是否可用**不能**由 `profile`（lite / full）这类运行档位决定。那样同一份
    代码里就有两套功能语义：轻量档不注册查重路由 → HTTP 契约分叉、前端要按档位
    分支、测试要跑两遍、文档要写两遍。正确做法是探测"这个包里真的有什么"
    （Pillow / cv2 / pillow-heif / rawpy / 人脸模型文件 / PySide6）：能力缺席时
    对应功能自动降级，而**打包脚本**（`build_web.spec` / `build.spec`）通过编译
    开关决定放不放这些重资产。

    于是便携版与桌面档共用同一条代码路径：桌面档 = 便携版 + PySide6 + 人脸模型。

约定：本模块只做**探测**，不 import 重库（`find_spec` 不加载模块），
因此可以放心在请求路径上调用。
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

from app.utils import paths as app_paths

logger = logging.getLogger(__name__)

__all__ = [
    "Capabilities", "detect", "reset_cache", "resolve_face_model_dir",
    "FACE_DETECT_MODEL", "FACE_RECOGNIZE_MODEL",
]

# OpenCV Zoo ONNX：YuNet 检测 + SFace 特征（与 hash_engine 共用，勿改名）
FACE_DETECT_MODEL = "face_detection_yunet_2023mar.onnx"
FACE_RECOGNIZE_MODEL = "face_recognition_sface_2021dec.onnx"


@dataclass(frozen=True)
class Capabilities:
    """当前进程真实具备的能力快照（frozen，便于日志与断言）。"""

    image: bool
    """Pillow 可用：图片缩略图 / pHash / dHash —— 文件级查重的前提。"""
    cv2: bool
    """OpenCV 可用：视频缩略图、视频抽帧查重、人脸检测。"""
    pillow_heif: bool
    """HEIC/HEIF 解码可用。"""
    rawpy: bool
    """RAW（CR2/NEF/ARW…）解码可用。"""
    face_models: bool
    """人脸模型文件（YuNet + SFace）在包里。"""
    qt: bool
    """PySide6 可用（本机具备桌面界面条件）。"""
    face_model_dir: str = ""
    """实际使用的人脸模型目录（打包后是 `_internal/models`）。"""

    # ---- 派生能力：功能开关一律读这些，不要各自拼条件 ----

    @property
    def dedup(self) -> bool:
        """文件级查重（MD5/pHash/dHash）是否可用。"""
        return self.image

    @property
    def dedup_video(self) -> bool:
        """视频帧查重是否可用（需要 OpenCV 抽帧）。"""
        return self.cv2

    @property
    def face(self) -> bool:
        """人脸线索是否可用（需要模型 + OpenCV 推理）。"""
        return self.cv2 and self.face_models

    @property
    def video_thumbnail(self) -> bool:
        return self.cv2

    def as_dict(self) -> dict:
        """给 `/api/features` 用的扁平字典（键名是前端契约，改动要同步 API.md）。"""
        return {
            "image": self.image,
            "cv2": self.cv2,
            "heic": self.pillow_heif,
            "raw": self.rawpy,
            "face_models": self.face_models,
            "qt": self.qt,
        }

    def notes(self) -> List[str]:
        """缺什么能力、缺了以后哪些功能不能用（前端/日志直接展示）。"""
        notes: List[str] = []
        if not self.image:
            notes.append("缺少 Pillow：图片缩略图与查重不可用")
        if not self.cv2:
            notes.append("未打包 OpenCV：视频缩略图与视频帧查重不可用"
                         "（打包时设 MEDIA_BUILD_NO_CV2=1 即为此模式）")
        if not self.pillow_heif:
            notes.append("未安装 pillow-heif：HEIC/HEIF 无法解码（HEIC 照片仍会入库）")
        if not self.rawpy:
            notes.append("未安装 rawpy：RAW 无缩略图（文件仍会入库）")
        if not self.face_models:
            notes.append("未打包人脸模型：人脸线索不可用"
                         "（需要时打包设 MEDIA_BUILD_WITH_FACE=1）")
        if not self.qt:
            notes.append("未打包 PySide6：本机没有桌面界面（Web 界面功能相同）")
        return notes


def _has_module(name: str) -> bool:
    """模块是否存在于当前环境/包内（只查不导入）。

    单独提成函数是为了测试能打桩（模拟"包里没有 cv2"）。
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@lru_cache(maxsize=1)
def _modules() -> Tuple[bool, bool, bool, bool, bool]:
    """模块级能力（与配置无关，因此可缓存）：image / cv2 / heif / rawpy / qt。"""
    return (
        _has_module("PIL"),
        _has_module("cv2"),
        _has_module("pillow_heif"),
        _has_module("rawpy"),
        _has_module("PySide6"),
    )


def reset_cache() -> None:
    """清掉能力缓存（测试打桩后必须调用）。"""
    _modules.cache_clear()


def resolve_face_model_dir(configured: Optional[object] = None) -> Optional[Path]:
    """找出同时含两个模型文件的目录；都不含则返回 None。

    为什么要多候选：`config.face_model_dir` 默认是相对路径 `models`，而打包后
    （PyInstaller onedir）模型在 `_internal/models`，CWD 在应用目录 —— 直接按相对
    路径找必然失败，人脸会在打包版里**静默失效**。候选顺序：
        配置值 → `paths.resource_dir()/models`（冻结态 = `_internal`）→ 应用目录 → CWD
    """
    candidates: List[Path] = []
    if configured:
        candidates.append(Path(str(configured)))
    candidates.append(app_paths.resource_dir() / "models")
    candidates.append(app_paths.app_dir() / "models")
    candidates.append(Path("models"))
    for candidate in candidates:
        try:
            if all((candidate / name).is_file() for name in
                   (FACE_DETECT_MODEL, FACE_RECOGNIZE_MODEL)):
                return candidate
        except OSError:  # 权限/网络盘等：换下一个候选
            continue
    return None


def detect(configured_model_dir: Optional[object] = None) -> Capabilities:
    """探测当前能力快照（模块级结果有缓存，模型目录每次实查）。"""
    image, cv2_on, heif_on, rawpy_on, qt_on = _modules()
    model_dir = resolve_face_model_dir(configured_model_dir)
    return Capabilities(
        image=image,
        cv2=cv2_on,
        pillow_heif=heif_on,
        rawpy=rawpy_on,
        face_models=model_dir is not None,
        qt=qt_on,
        face_model_dir=str(model_dir) if model_dir else "",
    )
