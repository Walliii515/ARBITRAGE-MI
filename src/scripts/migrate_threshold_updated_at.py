#!/usr/bin/env python3
"""
数据库迁移脚本：mi_vwap_basis_threshold 表添加 updated_at 字段

新增字段：
- updated_at DATETIME NULL COMMENT '数据写入/更新时间'

用途：记录每行阈值数据最后一次被写入的时间，供前端展示"最新计算日期"。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.common.database import db_manager


def migrate():
    """执行迁移"""
    print("🚀 开始数据库迁移：mi_vwap_basis_threshold 添加 updated_at 字段")

    with db_manager.get_cursor() as cursor:
        # 1. 检查字段是否已存在
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'mi_vwap_basis_threshold'
              AND COLUMN_NAME = 'updated_at'
        """)
        existing = cursor.fetchone()

        if not existing:
            cursor.execute("""
                ALTER TABLE mi_vwap_basis_threshold
                ADD COLUMN updated_at DATETIME NULL COMMENT '数据写入/更新时间'
                AFTER close_basis_p40
            """)
            print("✅ 添加字段: updated_at")
        else:
            print("⏭️  字段已存在: updated_at")

    print("\n📊 迁移完成！")
    print("   - updated_at: 每次 UPSERT 时自动写入 NOW()")


if __name__ == '__main__':
    migrate()
