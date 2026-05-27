# coding: utf-8
import pymysql
import os
from dotenv import load_dotenv
from contextlib import contextmanager

# 自动加载 .env 文件（从当前目录向上查找）
load_dotenv()


class DatabaseManager:
    """数据库连接管理器"""
    
    def __init__(self):
        self.config = {
            'host': os.getenv('MYSQL_HOST', 'localhost'),
            'port': int(os.getenv('MYSQL_PORT', 3306)),
            'database': os.getenv('MYSQL_DATABASE', 'crypto_arbitrage'),
            'user': os.getenv('MYSQL_USER', 'arb_app'),
            'password': os.getenv('MYSQL_PASSWORD', ''),
            'charset': 'utf8mb4',
            'cursorclass': pymysql.cursors.DictCursor
        }
    
    @contextmanager
    def get_connection(self):
        """获取数据库连接的上下文管理器"""
        connection = None
        try:
            connection = pymysql.connect(**self.config)
            yield connection
            connection.commit()
        except Exception as e:
            if connection:
                connection.rollback()
            raise e
        finally:
            if connection:
                connection.close()
    
    @contextmanager
    def get_cursor(self):
        """获取游标的上下文管理器"""
        with self.get_connection() as connection:
            cursor = connection.cursor()
            try:
                yield cursor
            finally:
                cursor.close()


# 创建全局数据库管理器实例
db_manager = DatabaseManager()
