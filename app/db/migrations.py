# -*- coding: utf-8 -*-
"""
数据库迁移与初始化。

负责首次创建所有表结构和后续数据库版本迁移。
"""

import logging
from pathlib import Path
from typing import Optional

from app.db.engine import DatabaseManager
from app.db.models import Base

logger = logging.getLogger(__name__)

# 当前数据库 Schema 版本号
CURRENT_SCHEMA_VERSION = 1


def init_db(db_path: Optional[Path] = None) -> None:
    """初始化数据库：创建引擎并建立所有表。

    如果数据库文件已存在且表已建好，则跳过（不重复创建）。

    参数:
        db_path: 数据库文件路径，默认使用 config.py 中的值。

    异常:
        RuntimeError: 数据库初始化失败。
    """
    if db_path is None:
        from config import AppConfig
        config = AppConfig()
        db_path = config.db_path

    logger.info(f"初始化数据库: {db_path}")
    DatabaseManager.initialize(db_path)

    # 创建所有表（如果不存在）
    engine = DatabaseManager.get_engine()
    Base.metadata.create_all(engine)
    logger.info("数据库表创建/验证完成")


def migrate_db() -> None:
    """执行数据库迁移（当 Schema 版本变更时）。

    当前为初始版本，无需迁移。未来版本在此实现增量更新逻辑。
    """
    engine = DatabaseManager.get_engine()
    # TODO: 读取当前数据库版本号，与 CURRENT_SCHEMA_VERSION 比对
    # 若需迁移，按版本号依次执行 ALTER TABLE 等操作
    logger.info(f"数据库 Schema 版本: v{CURRENT_SCHEMA_VERSION}，无需迁移")


def drop_all_tables() -> None:
    """删除所有表（仅限开发调试使用，生产环境勿调用）。"""
    engine = DatabaseManager.get_engine()
    Base.metadata.drop_all(engine)
    logger.warning("所有数据库表已被删除！")


def reset_db(db_path: Optional[Path] = None) -> None:
    """重置数据库：删除所有数据并重建表结构。

    参数:
        db_path: 数据库文件路径，默认使用配置中的值。
    """
    if db_path is None:
        from config import AppConfig
        config = AppConfig()
        db_path = config.db_path

    logger.info(f"正在重置数据库: {db_path}")
    DatabaseManager.dispose()

    # 删除数据库文件（如果存在）
    if db_path.exists():
        try:
            db_path.unlink()
            logger.info(f"已删除数据库文件: {db_path}")
        except PermissionError as e:
            logger.error(f"无法删除数据库文件: {e}")
            raise

    # 重新初始化（重建引擎和表结构）
    init_db(db_path)
    logger.info("数据库重置完成")
