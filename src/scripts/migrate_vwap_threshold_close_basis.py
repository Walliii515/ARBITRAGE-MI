#!/usr/bin/env python3
# coding: utf-8
"""
迁移脚本：mi_vwap_basis_threshold 表结构变更

变更内容：
1. 移除 open_basis_p50 ~ open_basis_p90（不再需要）
2. 移除 threshold_bps、threshold_percentile（应用层根据配置动态选列，无需存储）
3. 将 sample_count 重命名为 open_sample_count
4. 新增平仓统计字段：
   - close_sample_count, close_basis_max, close_basis_min, close_basis_mean, close_basis_std
   - close_basis_p10, close_basis_p20, close_basis_p30, close_basis_p40

用法：
    python src/scripts/migrate_vwap_threshold_close_basis.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from common.database import db_manager
from common.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


def migrate():
    """执行表结构迁移"""
    ddl_statements = [
        # 1. 移除不再需要的字段（不用 IF EXISTS，列不存在时会报 1091 并被跨过）
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN open_basis_p50",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN open_basis_p60",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN open_basis_p70",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN open_basis_p80",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN open_basis_p90",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN threshold_bps",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN threshold_percentile",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN open_threshold_bps",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN open_threshold_percentile",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN close_threshold_bps",
        "ALTER TABLE mi_vwap_basis_threshold DROP COLUMN close_threshold_percentile",

        # 2. 重命名 sample_count → open_sample_count
        """ALTER TABLE mi_vwap_basis_threshold 
           CHANGE COLUMN sample_count open_sample_count INT DEFAULT NULL""",

        # 3. 新增平仓统计字段
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_sample_count INT DEFAULT NULL AFTER open_basis_p40""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_max DECIMAL(10,4) DEFAULT NULL AFTER close_sample_count""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_min DECIMAL(10,4) DEFAULT NULL AFTER close_basis_max""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_mean DECIMAL(10,4) DEFAULT NULL AFTER close_basis_min""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_std DECIMAL(10,4) DEFAULT NULL AFTER close_basis_mean""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_p10 DECIMAL(10,4) DEFAULT NULL AFTER close_basis_std""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_p20 DECIMAL(10,4) DEFAULT NULL AFTER close_basis_p10""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_p30 DECIMAL(10,4) DEFAULT NULL AFTER close_basis_p20""",
        """ALTER TABLE mi_vwap_basis_threshold 
           ADD COLUMN close_basis_p40 DECIMAL(10,4) DEFAULT NULL AFTER close_basis_p30""",
    ]

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        for i, sql in enumerate(ddl_statements, 1):
            try:
                cursor.execute(sql)
                conn.commit()
                logger.info(f"[{i}/{len(ddl_statements)}] OK: {sql.strip()[:80]}...")
            except Exception as e:
                logger.warning(f"[{i}/{len(ddl_statements)}] SKIP: {e}")
                conn.rollback()

    logger.info("✓ mi_vwap_basis_threshold 表结构迁移完成")


if __name__ == '__main__':
    try:
        migrate()
    except Exception as e:
        logger.error(f"迁移失败: {e}", exc_info=True)
        sys.exit(1)
