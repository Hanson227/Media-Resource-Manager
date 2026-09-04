# -*- coding: utf-8 -*-
"""
数据库迁移与初始化。

负责首次创建所有表结构和后续数据库版本迁移。
"""

import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError

from app.db.engine import DatabaseManager
from app.db.models import Base

logger = logging.getLogger(__name__)

# 当前数据库 Schema 版本号
CURRENT_SCHEMA_VERSION = 4


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


def _backfill_content_modified_at(engine) -> None:
    """回填 resource_units.content_modified_at（v3→v4 迁移的存量数据修复）。

    对每个单元，stat 其 media_files 已记录的文件路径，取最新 st_mtime
    作为单元内容的真实修改日期。文件已不可访问（移动/离线/删除）时跳过；
    全部不可访问的单元保持 NULL，显示层回退到 created_at。
    幂等：重复执行结果一致。
    """
    from datetime import datetime

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT resource_unit_id, path FROM media_files;"
        )).all()
        buckets: dict[int, float] = {}
        for uid, path in rows:
            try:
                m = os.stat(path).st_mtime
            except OSError:
                continue  # 文件缺失/离线，跳过
            buckets[uid] = max(buckets.get(uid, 0.0), m)
        for uid, m in buckets.items():
            conn.execute(
                text("UPDATE resource_units SET content_modified_at = :m WHERE id = :u"),
                {"m": datetime.fromtimestamp(m), "u": uid},
            )
        conn.commit()
    logger.info(f"回填 content_modified_at 完成: {len(buckets)} 个单元")


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

    if current < 3:
        inspector = inspect(engine)
        tables = [t for t in inspector.get_table_names()]
        if "file_tags" not in tables:
            with engine.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE file_tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name VARCHAR(64) NOT NULL UNIQUE,
                        color VARCHAR(7),
                        created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                    );
                """))
                conn.execute(text("""
                    CREATE TABLE file_tag_mappings (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
                        tag_id INTEGER NOT NULL REFERENCES file_tags(id) ON DELETE CASCADE,
                        created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                        UNIQUE(file_id, tag_id)
                    );
                """))
                conn.execute(text("CREATE INDEX ix_file_tag_mappings_file ON file_tag_mappings(file_id);"))
                conn.execute(text("CREATE INDEX ix_file_tag_mappings_tag ON file_tag_mappings(tag_id);"))
                conn.commit()
                logger.info("迁移 v2→v3: 创建 file_tags 和 file_tag_mappings 表")
        _set_schema_version(engine, 3)
        current = 3

    if current < 4:
        inspector = inspect(engine)
        columns = [c["name"] for c in inspector.get_columns("resource_units")]
        if "content_modified_at" not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE resource_units ADD COLUMN content_modified_at DATETIME;"
                ))
                conn.commit()
                logger.info("迁移 v3→v4: 添加 resource_units.content_modified_at 列")
        # 存量数据回填（幂等）：以文件真实 mtime 修复单元内容日期
        _backfill_content_modified_at(engine)
        _set_schema_version(engine, 4)
        current = 4

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
