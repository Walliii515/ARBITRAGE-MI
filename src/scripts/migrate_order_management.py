#!/usr/bin/env python3
# coding: utf-8
"""
订单管理功能重构 - 数据库迁移脚本
1. 订单表：status 枚举扩展，新增 channel 和 position_id 字段
2. 持仓表：新增 close_order_uuid 字段
3. 数据迁移：simulated -> executed，回填 position_id
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


def run_migration():
    """执行数据库迁移"""
    logger.info("开始执行数据库迁移...")
    
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        
        # 1. 先修改 ENUM 包含新值（包含新旧值）
        logger.info("Step 1: 扩展 status ENUM 包含 executed...")
        try:
            cursor.execute("""
                ALTER TABLE mi_trade_order 
                MODIFY COLUMN status ENUM('pending', 'simulated', 'executed', 'rejected', 'failed') 
                NOT NULL DEFAULT 'pending' 
                COMMENT '订单状态: pending=待执行, simulated=已模拟(旧), executed=已成交, rejected=已拒单, failed=失败'
            """)
            logger.info("✓ status 字段枚举扩展成功（包含 simulated + executed）")
        except Exception as e:
            logger.error(f"✗ status 字段枚举扩展失败: {e}")
            raise
        
        # 2. 数据迁移：simulated -> executed
        logger.info("Step 2: 数据迁移 simulated -> executed...")
        try:
            cursor.execute("""
                UPDATE mi_trade_order 
                SET status = 'executed' 
                WHERE status = 'simulated'
            """)
            updated_rows = cursor.rowcount
            logger.info(f"✓ 已迁移 {updated_rows} 条 simulated -> executed")
        except Exception as e:
            logger.error(f"✗ 数据迁移失败: {e}")
            raise
        
        # 3. 移除旧枚举值（最终状态）
        logger.info("Step 3: 移除旧枚举值 simulated...")
        try:
            cursor.execute("""
                ALTER TABLE mi_trade_order 
                MODIFY COLUMN status ENUM('pending', 'executed', 'rejected', 'failed') 
                NOT NULL DEFAULT 'pending' 
                COMMENT '订单状态: pending=待执行, executed=已成交, rejected=已拒单, failed=失败'
            """)
            logger.info("✓ status 字段最终枚举修改成功")
        except Exception as e:
            logger.error(f"✗ status 字段最终修改失败: {e}")
            raise
        
        # 4. 新增 channel 字段
        logger.info("Step 4: 新增 channel 字段...")
        try:
            # 先检查字段是否已存在
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'mi_trade_order' 
                AND COLUMN_NAME = 'channel'
            """)
            if cursor.fetchone()['cnt'] == 0:
                cursor.execute("""
                    ALTER TABLE mi_trade_order 
                    ADD COLUMN channel ENUM('Mock', 'SimTrade', 'Live') 
                    NOT NULL DEFAULT 'Mock' 
                    COMMENT '渠道: Mock=模拟成交, SimTrade=模拟盘, Live=实盘'
                    AFTER status
                """)
                logger.info("✓ channel 字段新增成功")
            else:
                logger.info("✓ channel 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ channel 字段新增失败: {e}")
            raise
        
        # 5. 新增 position_id 字段
        logger.info("Step 5: 新增 position_id 字段...")
        try:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'mi_trade_order' 
                AND COLUMN_NAME = 'position_id'
            """)
            if cursor.fetchone()['cnt'] == 0:
                cursor.execute("""
                    ALTER TABLE mi_trade_order 
                    ADD COLUMN position_id BIGINT NULL 
                    COMMENT '关联持仓ID，4笔订单共享同一个 position_id'
                    AFTER order_uuid
                """)
                logger.info("✓ position_id 字段新增成功")
            else:
                logger.info("✓ position_id 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ position_id 字段新增失败: {e}")
            raise
        
        # 6. 添加索引
        logger.info("Step 6: 添加索引...")
        try:
            # position_id 索引
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.STATISTICS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'mi_trade_order' 
                AND INDEX_NAME = 'idx_position_id'
            """)
            if cursor.fetchone()['cnt'] == 0:
                cursor.execute("""
                    ALTER TABLE mi_trade_order 
                    ADD INDEX idx_position_id (position_id)
                """)
                logger.info("✓ idx_position_id 索引添加成功")
            else:
                logger.info("✓ idx_position_id 索引已存在，跳过")
            
            # channel 索引
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.STATISTICS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'mi_trade_order' 
                AND INDEX_NAME = 'idx_channel'
            """)
            if cursor.fetchone()['cnt'] == 0:
                cursor.execute("""
                    ALTER TABLE mi_trade_order 
                    ADD INDEX idx_channel (channel)
                """)
                logger.info("✓ idx_channel 索引添加成功")
            else:
                logger.info("✓ idx_channel 索引已存在，跳过")
        except Exception as e:
            logger.error(f"✗ 索引添加失败: {e}")
            raise
        
        # 7. 持仓表新增 close_order_uuid 字段
        logger.info("Step 7: 持仓表新增 close_order_uuid 字段...")
        try:
            cursor.execute("""
                SELECT COUNT(*) as cnt FROM information_schema.COLUMNS 
                WHERE TABLE_SCHEMA = DATABASE() 
                AND TABLE_NAME = 'mi_trade_position' 
                AND COLUMN_NAME = 'close_order_uuid'
            """)
            if cursor.fetchone()['cnt'] == 0:
                cursor.execute("""
                    ALTER TABLE mi_trade_position 
                    ADD COLUMN close_order_uuid VARCHAR(36) NULL 
                    COMMENT '平仓订单组UUID'
                    AFTER order_uuid
                """)
                logger.info("✓ close_order_uuid 字段新增成功")
            else:
                logger.info("✓ close_order_uuid 字段已存在，跳过")
        except Exception as e:
            logger.error(f"✗ close_order_uuid 字段新增失败: {e}")
            raise
        
        # 8. 回填 position_id
        logger.info("Step 8: 回填 position_id...")
        try:
            # 通过 order_uuid 关联持仓表（开仓订单）
            cursor.execute("""
                UPDATE mi_trade_order o
                JOIN mi_trade_position p ON o.order_uuid = p.order_uuid
                SET o.position_id = p.id
                WHERE o.position_id IS NULL
            """)
            updated_open = cursor.rowcount
            logger.info(f"✓ 开仓订单回填 {updated_open} 条")
            
            # 通过 close_order_uuid 关联持仓表（平仓订单）
            cursor.execute("""
                UPDATE mi_trade_order o
                JOIN mi_trade_position p ON o.order_uuid = p.close_order_uuid
                SET o.position_id = p.id
                WHERE o.position_id IS NULL
            """)
            updated_close = cursor.rowcount
            logger.info(f"✓ 平仓订单回填 {updated_close} 条")
        except Exception as e:
            logger.error(f"✗ position_id 回填失败: {e}")
            raise
        
        conn.commit()
        logger.info("=" * 60)
        logger.info("数据库迁移完成！")
        logger.info("=" * 60)


if __name__ == '__main__':
    try:
        run_migration()
    except Exception as e:
        logger.error(f"迁移失败: {e}")
        sys.exit(1)
