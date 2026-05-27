# coding: utf-8
"""
迁移脚本：mi_trade_position 表新增开仓原因字段、扩展平仓原因字段

变更内容：
1. 新增 open_reason VARCHAR(500) - 开仓原因及关键参数（便于复盘）
2. 扩展 close_reason VARCHAR(50) → VARCHAR(500) - 支持存储平仓详细原因及计算过程

用法：
    python src/scripts/migrate_add_trade_reason.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from common.database import db_manager
from common.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


def _col_exists(cursor, table: str, column: str) -> bool:
    """检查表中的字段是否已存在"""
    cursor.execute("""
        SELECT COUNT(*) as cnt FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
    """, (table, column))
    return cursor.fetchone()['cnt'] > 0


def migrate():
    """执行数据库迁移"""
    logger.info("开始执行交易原因字段迁移...")

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()

        # ── Step 1: 新增 open_reason ──
        logger.info("Step 1: 新增 open_reason 字段...")
        try:
            if not _col_exists(cursor, 'mi_trade_position', 'open_reason'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN open_reason VARCHAR(500) NULL
                    COMMENT '开仓原因及关键参数(便于复盘)'
                    AFTER open_spread_bps
                """)
                logger.info("✓ open_reason 字段新增成功")
            else:
                logger.info("✓ open_reason 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ open_reason 字段新增失败: {e}")
            raise

        # ── Step 2: 扩展 close_reason 为 VARCHAR(500) ──
        logger.info("Step 2: 扩展 close_reason 为 VARCHAR(500)...")
        try:
            cursor.execute("""
                SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'mi_trade_position'
                  AND COLUMN_NAME = 'close_reason'
            """)
            row = cursor.fetchone()
            if row and row['CHARACTER_MAXIMUM_LENGTH'] and int(row['CHARACTER_MAXIMUM_LENGTH']) < 500:
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    MODIFY COLUMN close_reason VARCHAR(500) NULL
                    COMMENT '平仓原因及关键参数(便于复盘)'
                """)
                logger.info("✓ close_reason 扩展为 VARCHAR(500) 成功")
            else:
                logger.info("✓ close_reason 长度已足够，跳过")
        except Exception as e:
            logger.error(f"✗ close_reason 扩展失败: {e}")
            raise

        conn.commit()
        logger.info("✅ 迁移完成！")
        logger.info("   - open_reason: 开仓原因及参数")
        logger.info("   - close_reason: 平仓原因及参数(已扩展)")


if __name__ == '__main__':
    migrate()
