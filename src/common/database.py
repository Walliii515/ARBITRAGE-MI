# coding: utf-8
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pymysql
from dotenv import load_dotenv
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

# 自动加载 .env 文件（从当前目录向上查找）
load_dotenv()


class DatabaseManager:
    """数据库连接管理器"""

    def __init__(self) -> None:
        self.config: dict[str, Any] = {
            'host': os.getenv('MYSQL_HOST', 'localhost'),
            'port': int(os.getenv('MYSQL_PORT', 3306)),
            'database': os.getenv('MYSQL_DATABASE', 'crypto_arbitrage'),
            'user': os.getenv('MYSQL_USER', 'arb_app'),
            'password': os.getenv('MYSQL_PASSWORD', ''),
            'charset': 'utf8mb4',
            'cursorclass': pymysql.cursors.DictCursor
        }

    @contextmanager
    def get_connection(self) -> Iterator[Connection]:
        """获取数据库连接的上下文管理器"""
        connection: Connection | None = None
        try:
            connection = pymysql.connect(**self.config)
            yield connection
            connection.commit()
        except Exception:
            if connection:
                connection.rollback()
            raise
        finally:
            if connection:
                connection.close()

    @contextmanager
    def get_cursor(self) -> Iterator[DictCursor]:
        """获取游标的上下文管理器"""
        with self.get_connection() as connection:
            cursor = connection.cursor()
            try:
                yield cursor
            finally:
                cursor.close()


# 创建全局数据库管理器实例
db_manager = DatabaseManager()
