#!/usr/bin/env python3
"""
数据库迁移脚本：订单表添加开仓VWAP基差和风险缓释字段

新增字段：
- open_vwap_basis_bps DECIMAL(10,2) NULL COMMENT '开仓VWAP基差(bps)'
- risk_relief_bps DECIMAL(10,2) NULL COMMENT '风险缓释(bps)'

计算公式：
open_marginal_basis_bps = open_vwap_basis_bps + open_fee_bps + risk_relief_bps
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.common.database import db_manager


def migrate():
    """执行迁移"""
    print("🚀 开始数据库迁移：添加 open_vwap_basis_bps 和 risk_relief_bps 字段")
    
    with db_manager.get_cursor() as cursor:
        # 1. 检查字段是否已存在
        cursor.execute("""
            SELECT COLUMN_NAME 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_SCHEMA = DATABASE() 
              AND TABLE_NAME = 'mi_trade_order' 
              AND COLUMN_NAME IN ('open_vwap_basis_bps', 'risk_relief_bps')
        """)
        existing = {row['COLUMN_NAME'] for row in cursor.fetchall()}
        
        # 2. 添加 open_vwap_basis_bps
        if 'open_vwap_basis_bps' not in existing:
            cursor.execute("""
                ALTER TABLE mi_trade_order 
                ADD COLUMN open_vwap_basis_bps DECIMAL(10,2) NULL COMMENT '开仓VWAP基差(bps)'
                AFTER open_coverage
            """)
            print("✅ 添加字段: open_vwap_basis_bps")
        else:
            print("⏭️  字段已存在: open_vwap_basis_bps")
        
        # 3. 添加 risk_relief_bps
        if 'risk_relief_bps' not in existing:
            cursor.execute("""
                ALTER TABLE mi_trade_order 
                ADD COLUMN risk_relief_bps DECIMAL(10,2) NULL COMMENT '风险缓释(bps)'
                AFTER open_vwap_basis_bps
            """)
            print("✅ 添加字段: risk_relief_bps")
        else:
            print("⏭️  字段已存在: risk_relief_bps")
    
    print("\n📊 迁移完成！")
    print("   - open_vwap_basis_bps: 开仓VWAP基差(bps)")
    print("   - risk_relief_bps: 风险缓释(bps)")
    print("\n💡 计算公式: open_marginal_basis_bps = open_vwap_basis_bps + open_fee_bps + risk_relief_bps")


if __name__ == '__main__':
    migrate()
