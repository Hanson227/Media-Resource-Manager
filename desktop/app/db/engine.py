# -*- coding: utf-8 -*-
"""
数据库引擎与会话管理。

使用单例模式管理 SQLAlchemy 引擎和会话工厂，
提供线程安全的会话上下文管理器。
"""

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from sqlalchemy import create_engine, Engine, event, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


class DatabaseManager:
    """数据库管理器单例。

    负责 SQLAlchemy 引擎创建、会话工厂管理和连接生命周期。
    """

    _engine: Optional[Engine] = None
    _session_factory: Optional[sessionmaker] = None
    _db_path: Optional[Path] = None

    @classmethod
    def initialize(cls, db_path: Path, echo: bool = False) -> None:
        """初始化数据库引擎和会话工厂。

        首次调用时创建引擎，后续调用不会重复创建（除非路径不同）。
        自动启用 SQLite WAL 模式以提升并发读取性能。

        参数:
            db_path: SQLite 数据库文件路径。
            echo: 是否打印 SQL 语句（调试用）。
        """
        if cls._engine is not None and cls._db_path == db_path:
            return

        # 确保数据库目录存在
        db_path.parent.mkdir(parents=True, exist_ok=True)

        db_url = f"sqlite:///{db_path.resolve()}"
        cls._engine = create_engine(
            db_url,
            echo=echo,
            connect_args={
                "check_same_thread": False,
                # 写入冲突时等待最多 5 秒而非立即失败
                "timeout": 5,
            },
        )

        # 启用 WAL 模式以提升并发性能
        @event.listens_for(cls._engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        cls._session_factory = sessionmaker(
            bind=cls._engine,
            expire_on_commit=False,  # 提交后不过期属性，防止 ORM 对象在 session 外访问时崩溃
        )
        cls._db_path = db_path

        # 启动时自动检查完整性
        integrity = cls.check_integrity()
        if integrity is not None and integrity != "ok":
            logger.warning(f"数据库文件可能损坏: {integrity}")
            if cls.try_repair():
                logger.info("数据库已自动修复")
            else:
                logger.error("数据库自动修复失败，建议恢复备份")

    @classmethod
    @contextmanager
    def session(cls) -> Generator[Session, None, None]:
        """创建数据库会话的上下文管理器。

        用法:
            with DatabaseManager.session() as session:
                units = get_all_active_units(session)

        退出时自动提交或回滚。
        """
        if cls._session_factory is None:
            raise RuntimeError("数据库未初始化，请先调用 DatabaseManager.initialize()")

        session = cls._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @classmethod
    def get_engine(cls) -> Engine:
        """获取 SQLAlchemy 引擎实例。"""
        if cls._engine is None:
            raise RuntimeError("数据库未初始化，请先调用 DatabaseManager.initialize()")
        return cls._engine

    @classmethod
    def dispose(cls) -> None:
        """关闭数据库引擎，释放所有连接。"""
        if cls._engine is not None:
            cls._engine.dispose()
            cls._engine = None
            cls._session_factory = None
            cls._db_path = None

    @classmethod
    def check_integrity(cls) -> Optional[str]:
        """执行 SQLite 完整性检查。

        返回:
            损坏描述字符串（损坏时），或 None（完好时）。
        """
        if cls._engine is None:
            return "数据库未初始化"
        try:
            with cls._engine.connect() as conn:
                result = conn.execute(text("PRAGMA integrity_check;")).scalar()
                if result != "ok":
                    logger.error(f"数据库完整性检查失败: {result}")
                    return str(result)
                return None
        except Exception as e:
            logger.error(f"完整性检查异常: {e}")
            return str(e)

    @classmethod
    def try_repair(cls) -> bool:
        """尝试修复损坏的数据库（VACUUM + 从 WAL 恢复）。

        返回:
            修复是否成功。
        """
        if cls._engine is None:
            return False
        try:
            with cls._engine.connect() as conn:
                conn.execute(text("PRAGMA integrity_check;"))
                # VACUUM 重建数据库文件，可清除 WAL 不一致
                conn.execute(text("VACUUM;"))
                # 再次检查
                result = conn.execute(text("PRAGMA integrity_check;")).scalar()
                if result == "ok":
                    logger.info("数据库修复成功")
                    return True
                logger.error(f"VACUUM 修复后仍有问题: {result}")
                return False
        except Exception as e:
            logger.error(f"数据库修复失败: {e}")
            return False

    @classmethod
    @property
    def is_initialized(cls) -> bool:
        """检查数据库是否已初始化。"""
        return cls._engine is not None
