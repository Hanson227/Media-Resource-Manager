# -*- coding: utf-8 -*-
# -*- coding: utf-8 -*-
"""
数据库迁移与初始化。

负责首次创建所有表结构和后续数据库版本迁移。
"""

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app.db.engine import DatabaseManager
from app.db.models import Base

logger = logging.getLogger(__name__)

# 当前数据库 Schema 版本号
CURRENT_SCHEMA_VERSION = 2


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


def _get_schema_version(engine) -> int:
    """读取当前数据库的 Schema 版本。"""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("PRAGMA user_version;")).scalar()
            return result or 0
    except OperationalError:
        return 0


def _set_schema_version(engine, version: int) -> None:
    """设置数据库 Schema 版本。"""
    with engine.connect() as conn:
        conn.execute(text(f"PRAGMA user_version = {int(version)};"))
        conn.commit()


def migrate_db() -> None:
    """执行数据库迁移（当 Schema 版本变更时）。"""
    engine = DatabaseManager.get_engine()
    current = _get_schema_version(engine)
    logger.info(f"数据库当前版本: v{current}，目标版本: v{CURRENT_SCHEMA_VERSION}")

    if current >= CURRENT_SCHEMA_VERSION:
        return

    # 逐版本迁移
    if current < 1:
        _set_schema_version(engine, 1)
        current = 1

    if current < 2:
        inspector = inspect(engine)
        columns = [c["name"] for c in inspector.get_columns("resource_units")]
        if "cover_path" not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE resource_units ADD COLUMN cover_path VARCHAR(2048);"
                ))
                conn.commit()
                logger.info("迁移 v1→v2: 添加 resource_units.cover_path 列")
        _set_schema_version(engine, 2)
        current = 2

    logger.info(f"数据库迁移完成，当前版本: v{current}")


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
