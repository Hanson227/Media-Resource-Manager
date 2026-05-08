# -*- coding: utf-8 -*-
"""
SMB 共享服务 —— 通过 subprocess 调用 Windows net share 命令管理文件夹共享。

提供界面层调用的简化接口，处理：
- 创建共享
- 删除共享
- 查询当前共享状态
"""

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.exceptions import SMBShareError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShareInfo:
    """SMB 共享信息。"""

    share_name: str
    """共享名称。"""

    folder_path: str
    """共享的文件夹路径。"""

    description: str = ""
    """共享描述。"""

    max_users: int = 20
    """最大并发用户数。"""


class SMBService:
    """Windows SMB 共享服务封装。

    底层通过 subprocess 调用 net share 命令。
    需要管理员权限才能创建/删除共享。

    用法:
        shares = SMBService.list_shares()
        success = SMBService.create_share(ShareInfo(...))
    """

    @staticmethod
    def list_shares() -> list[ShareInfo]:
        """列出当前系统的所有 SMB 共享。

        返回:
            ShareInfo 列表。

        异常:
            SMBShareError: 执行命令失败。
        """
        try:
            result = subprocess.run(
                ["net", "share"],
                capture_output=True,
                text=True,
                encoding="gbk",
                timeout=10,
            )
            if result.returncode != 0:
                raise SMBShareError(f"net share 命令执行失败: {result.stderr.strip()}")

            shares: list[ShareInfo] = []
            for line in result.stdout.split("\n"):
                line = line.strip()
                if not line or line.startswith("共享名") or line.startswith("命令"):
                    continue
                # 格式: "ShareName    C:\Path    描述"
                parts = line.split(None, 2)
                if len(parts) >= 2:
                    share_name = parts[0]
                    folder_path = parts[1]
                    description = parts[2] if len(parts) > 2 else ""
                    # 过滤掉系统默认共享（如 C$、ADMIN$）
                    if share_name.endswith("$") and len(share_name) <= 3:
                        continue
                    shares.append(ShareInfo(
                        share_name=share_name,
                        folder_path=folder_path,
                        description=description,
                    ))

            return shares

        except subprocess.TimeoutExpired:
            raise SMBShareError("net share 命令超时")
        except Exception as e:
            raise SMBShareError(f"列出共享失败: {e}")

    @staticmethod
    def create_share(
        share_name: str,
        folder_path: Path,
        description: str = "",
        max_users: int = 20,
    ) -> bool:
        """创建一个新的 SMB 共享。

        参数:
            share_name: 共享名称（不含反斜杠）。
            folder_path: 要共享的文件夹路径。
            description: 共享描述。
            max_users: 最大并发用户数。

        返回:
            True 如果创建成功。

        异常:
            SMBShareError: 创建失败。
        """
        if not folder_path.is_dir():
            raise SMBShareError(f"文件夹不存在: {folder_path}")

        # 检查是否已共享
        existing = SMBService.get_share_by_name(share_name)
        if existing:
            raise SMBShareError(f"共享名 '{share_name}' 已存在，指向: {existing.folder_path}")

        try:
            cmd = [
                "net", "share", share_name,
                str(folder_path),
                f"/grant:everyone,read",
                f"/remark:{description}",
                f"/users:{max_users}",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="gbk",
                timeout=15,
            )

            if result.returncode != 0:
                raise SMBShareError(f"创建共享失败: {result.stderr.strip()}")

            logger.info(f"SMB 共享创建成功: {share_name} → {folder_path}")
            return True

        except subprocess.TimeoutExpired:
            raise SMBShareError("net share 命令超时")
        except SMBShareError:
            raise
        except Exception as e:
            raise SMBShareError(f"创建共享异常: {e}")

    @staticmethod
    def delete_share(share_name: str) -> bool:
        """删除一个 SMB 共享。

        参数:
            share_name: 要删除的共享名称。

        返回:
            True 如果删除成功。

        异常:
            SMBShareError: 删除失败。
        """
        try:
            result = subprocess.run(
                ["net", "share", share_name, "/delete"],
                capture_output=True,
                text=True,
                encoding="gbk",
                timeout=10,
            )

            if result.returncode != 0:
                raise SMBShareError(f"删除共享失败: {result.stderr.strip()}")

            logger.info(f"SMB 共享已删除: {share_name}")
            return True

        except subprocess.TimeoutExpired:
            raise SMBShareError("net share 命令超时")
        except SMBShareError:
            raise
        except Exception as e:
            raise SMBShareError(f"删除共享异常: {e}")

    @staticmethod
    def get_share_by_name(share_name: str) -> Optional[ShareInfo]:
        """按名称查找 SMB 共享。

        参数:
            share_name: 共享名称。

        返回:
            ShareInfo 如果存在，否则 None。
        """
        try:
            shares = SMBService.list_shares()
            for s in shares:
                if s.share_name.lower() == share_name.lower():
                    return s
        except SMBShareError:
            pass
        return None

    @staticmethod
    def share_exists(share_name: str) -> bool:
        """检查指定名称的共享是否已存在。"""
        return SMBService.get_share_by_name(share_name) is not None

    @staticmethod
    def get_share_instructions() -> str:
        """返回手动共享的操作说明（当自动命令不可用时）。"""
        return (
            "无法自动设置 SMB 共享，请手动操作：\n"
            "1. 在文件资源管理器中右键目标文件夹\n"
            "2. 选择「属性」→「共享」选项卡\n"
            "3. 点击「高级共享」\n"
            "4. 勾选「共享此文件夹」\n"
            "5. 设置共享名称和权限\n"
            "6. 点击「确定」完成设置"
        )
