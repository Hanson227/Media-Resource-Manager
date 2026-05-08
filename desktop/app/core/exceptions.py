# -*- coding: utf-8 -*-
"""
自定义异常类。

所有业务逻辑相关的异常集中定义在此，便于统一捕获和处理。
"""


class MediaManagerError(Exception):
    """应用基础异常，所有自定义异常继承自此类。"""
    pass


class ScanError(MediaManagerError):
    """扫描过程中的错误。"""
    pass


class AccessDeniedError(ScanError):
    """无权限访问文件或目录时抛出。"""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"无权访问: {path}")


class MediaFileError(MediaManagerError):
    """媒体文件相关的错误。"""
    pass


class UnsupportedFormatError(MediaFileError):
    """文件格式不支持时抛出。"""

    def __init__(self, path: str, reason: str = "") -> None:
        self.path = path
        msg = f"不支持的格式: {path}"
        if reason:
            msg += f"（{reason}）"
        super().__init__(msg)


class HashComputationError(MediaManagerError):
    """哈希计算失败时抛出。"""

    def __init__(self, path: str, algorithm: str, reason: str = "") -> None:
        self.path = path
        self.algorithm = algorithm
        msg = f"哈希计算失败 [{algorithm}]: {path}"
        if reason:
            msg += f" - {reason}"
        super().__init__(msg)


class FaceDetectionError(MediaManagerError):
    """人脸检测失败时抛出。"""
    pass


class ThumbnailGenerationError(MediaManagerError):
    """缩略图生成失败时抛出。"""

    def __init__(self, path: str, reason: str = "") -> None:
        self.path = path
        msg = f"缩略图生成失败: {path}"
        if reason:
            msg += f" - {reason}"
        super().__init__(msg)


class DatabaseError(MediaManagerError):
    """数据库操作错误。"""
    pass


class ConfigurationError(MediaManagerError):
    """配置错误。"""
    pass


class SMBShareError(MediaManagerError):
    """SMB 共享操作错误。"""
    pass


class APIError(MediaManagerError):
    """API 服务相关错误。"""
    pass


class WatcherError(MediaManagerError):
    """文件监控相关错误。"""
    pass
