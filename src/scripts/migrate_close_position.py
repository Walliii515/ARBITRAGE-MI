# coding: utf-8
"""
迁移脚本：mi_trade_position 表新增平仓相关字段

变更内容：
1. 扩展 status ENUM 新增 'closed' 值
2. 新增平仓时间字段：closed_at
3. 新增平仓原因字段：close_reason
4. 新增平仓成交价格字段：spot_close_price, future_close_price
5. 新增平仓成交金额字段：spot_close_amount, future_close_amount
6. 新增平仓基差快照字段：close_spread_bps

用法：
    python src/scripts/migrate_close_position.py
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
    logger.info("开始执行平仓字段迁移...")

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()

        # ── Step 1: 扩展 status ENUM 新增 'closed' ──
        logger.info("Step 1: 扩展 status ENUM 新增 'closed'...")
        try:
            cursor.execute("""
                SELECT COLUMN_TYPE FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'mi_trade_position'
                  AND COLUMN_NAME = 'status'
            """)
            row = cursor.fetchone()
            col_type = row['COLUMN_TYPE'] if row else ''
            if 'closed' not in col_type:
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    MODIFY COLUMN status ENUM('holding','closed') NOT NULL DEFAULT 'holding'
                    COMMENT '持仓状态: holding=持仓中, closed=已平仓'
                """)
                logger.info("✓ status ENUM 扩展成功（新增 closed）")
            else:
                logger.info("✓ status ENUM 已包含 'closed'，跳过")
        except Exception as e:
            logger.error(f"✗ status ENUM 扩展失败: {e}")
            raise

        # ── Step 2: 新增 closed_at ──
        logger.info("Step 2: 新增 closed_at 字段...")
        try:
            if not _col_exists(cursor, 'mi_trade_position', 'closed_at'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN closed_at DATETIME NULL
                    COMMENT '平仓时间'
                    AFTER opened_at
                """)
                logger.info("✓ closed_at 字段新增成功")
            else:
                logger.info("✓ closed_at 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ closed_at 字段新增失败: {e}")
            raise

        # ── Step 3: 新增 close_reason ──
        logger.info("Step 3: 新增 close_reason 字段...")
        try:
            if not _col_exists(cursor, 'mi_trade_position', 'close_reason'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN close_reason VARCHAR(50) NULL
                    COMMENT '平仓原因: negative_funding=资金费率为负, take_profit=止盈, funding_count=资金费次数'
                    AFTER closed_at
                """)
                logger.info("✓ close_reason 字段新增成功")
            else:
                logger.info("✓ close_reason 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ close_reason 字段新增失败: {e}")
            raise

        # ── Step 4: 新增平仓成交价格字段 ──
        logger.info("Step 4: 新增平仓成交价格字段（spot_close_price / future_close_price）...")
        try:
            if not _col_exists(cursor, 'mi_trade_position', 'spot_close_price'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN spot_close_price DECIMAL(20,8) NULL
                    COMMENT '现货平仓成交VWAP'
                    AFTER close_reason
                """)
                logger.info("✓ spot_close_price 字段新增成功")
            else:
                logger.info("✓ spot_close_price 字段已存在，跳过")

            if not _col_exists(cursor, 'mi_trade_position', 'future_close_price'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN future_close_price DECIMAL(20,8) NULL
                    COMMENT '期货平仓成交VWAP'
                    AFTER spot_close_price
                """)
                logger.info("✓ future_close_price 字段新增成功")
            else:
                logger.info("✓ future_close_price 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ 平仓成交价格字段新增失败: {e}")
            raise

        # ── Step 5: 新增平仓成交金额字段 ──
        logger.info("Step 5: 新增平仓成交金额字段（spot_close_amount / future_close_amount）...")
        try:
            if not _col_exists(cursor, 'mi_trade_position', 'spot_close_amount'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN spot_close_amount DECIMAL(20,4) NULL
                    COMMENT '现货平仓成交金额(USDT)'
                    AFTER future_close_price
                """)
                logger.info("✓ spot_close_amount 字段新增成功")
            else:
                logger.info("✓ spot_close_amount 字段已存在，跳过")

            if not _col_exists(cursor, 'mi_trade_position', 'future_close_amount'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN future_close_amount DECIMAL(20,4) NULL
                    COMMENT '期货平仓成交金额(USDT)'
                    AFTER spot_close_amount
                """)
                logger.info("✓ future_close_amount 字段新增成功")
            else:
                logger.info("✓ future_close_amount 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ 平仓成交金额字段新增失败: {e}")
            raise

        # ── Step 6: 新增 close_spread_bps ──
        logger.info("Step 6: 新增 close_spread_bps 字段...")
        try:
            if not _col_exists(cursor, 'mi_trade_position', 'close_spread_bps'):
                cursor.execute("""
                    ALTER TABLE mi_trade_position
                    ADD COLUMN close_spread_bps DECIMAL(10,2) NULL
                    COMMENT '平仓时实际VWAP基差(bps)'
                    AFTER future_close_amount
                """)
                logger.info("✓ close_spread_bps 字段新增成功")
            else:
                logger.info("✓ close_spread_bps 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ close_spread_bps 字段新增失败: {e}")
            raise

        conn.commit()
        logger.info("迁移完成！所有平仓相关字段已就绪。")
        logger.info("字段汇总：")
        logger.info("  mi_trade_position.status       - 新增枚举值 'closed'")
        logger.info("  mi_trade_position.closed_at    - 平仓时间")
        logger.info("  mi_trade_position.close_reason - 平仓原因（negative_funding/take_profit/funding_count）")
        logger.info("  mi_trade_position.spot_close_price   - 现货平仓VWAP")
        logger.info("  mi_trade_position.future_close_price - 期货平仓VWAP")
        logger.info("  mi_trade_position.spot_close_amount  - 现货平仓成交金额")
        logger.info("  mi_trade_position.future_close_amount - 期货平仓成交金额")
        logger.info("  mi_trade_position.close_spread_bps  - 平仓时VWAP基差(bps)")


if __name__ == '__main__':
    migrate()
