# coding: utf-8
"""
修复脚本：清理因迁移未执行导致的重复平仓订单，并回填持仓平仓状态

问题原因：
    closing_executor 插入平仓订单成功，但 UPDATE mi_trade_position 失败（缺字段），
    导致持仓保持 holding，下个5秒循环重复触发平仓，产生大量重复订单。

修复步骤：
    1. 删除重复平仓订单（每个 position_id 仅保留最早一组 spot+future）
    2. 对仍为 holding 但已有成功平仓订单的持仓，回填 closed 状态

用法：
    python src/scripts/fix_duplicate_close_orders.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from common.database import db_manager
from common.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


def fix():
    logger.info("===== 开始修复重复平仓订单 =====")

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()

        # ── Step 1: 找到每个 position+market_type 应保留的最早订单 id ──
        logger.info("Step 1: 标记每个 position 需保留的最早一组订单...")
        cursor.execute("""
            SELECT position_id, market_type, MIN(id) as keep_id
            FROM mi_trade_order
            WHERE order_side = 'close'
            GROUP BY position_id, market_type
        """)
        keep_rows = cursor.fetchall()
        keep_ids = [r['keep_id'] for r in keep_rows]
        logger.info(f"  应保留订单数: {len(keep_ids)}")

        if not keep_ids:
            logger.info("无平仓订单需要清理")
            return

        # ── Step 2: 删除重复订单（不在保留列表中的） ──
        logger.info("Step 2: 删除重复平仓订单...")
        # keep_ids 列表较小（~640条），直接用 NOT IN
        placeholders = ','.join(['%s'] * len(keep_ids))
        cursor.execute(f"""
            DELETE FROM mi_trade_order
            WHERE order_side = 'close' AND id NOT IN ({placeholders})
        """, keep_ids)
        deleted = cursor.rowcount
        logger.info(f"  已删除重复订单: {deleted} 条")

        # ── Step 3: 回填持仓平仓状态 ──
        logger.info("Step 3: 回填仍为 holding 但有成功平仓订单的持仓...")
        cursor.execute("""
            SELECT DISTINCT p.id as position_id
            FROM mi_trade_position p
            JOIN mi_trade_order o ON o.position_id = p.id
            WHERE p.status = 'holding'
              AND o.order_side = 'close'
              AND o.status = 'executed'
        """)
        positions_to_fix = [r['position_id'] for r in cursor.fetchall()]
        logger.info(f"  需回填的持仓数: {len(positions_to_fix)}")

        fixed_count = 0
        for pos_id in positions_to_fix:
            # 获取该持仓保留的平仓订单数据
            cursor.execute("""
                SELECT exec_price, exec_amount, executed_at, reject_reason, market_type
                FROM mi_trade_order
                WHERE position_id = %s AND order_side = 'close' AND status = 'executed'
                ORDER BY id ASC
                LIMIT 2
            """, (pos_id,))
            orders = cursor.fetchall()

            spot_price = None
            future_price = None
            spot_amount = None
            future_amount = None
            close_reason_display = None
            closed_at = None

            for o in orders:
                if o['market_type'] == 'spot':
                    spot_price = o['exec_price']
                    spot_amount = o['exec_amount']
                    close_reason_display = o['reject_reason']
                    closed_at = o['executed_at']
                elif o['market_type'] == 'future':
                    future_price = o['exec_price']
                    future_amount = o['exec_amount']

            # 从 reject_reason 中文反查 close_reason 枚举
            reason_map = {
                '资金费次数平仓': 'funding_count',
                '止盈平仓': 'take_profit',
                '资金费率为负平仓': 'negative_funding',
            }
            close_reason = reason_map.get(close_reason_display, close_reason_display)

            # 计算平仓基差 bps
            close_spread_bps = None
            if spot_price and future_price:
                s, f = float(spot_price), float(future_price)
                if s != 0:
                    close_spread_bps = round((f - s) / s * 10000, 2)

            cursor.execute("""
                UPDATE mi_trade_position SET
                    status = 'closed',
                    closed_at = %s,
                    close_reason = %s,
                    spot_close_price = %s,
                    future_close_price = %s,
                    spot_close_amount = %s,
                    future_close_amount = %s,
                    close_spread_bps = %s
                WHERE id = %s AND status = 'holding'
            """, (
                closed_at, close_reason,
                spot_price, future_price,
                spot_amount, future_amount,
                close_spread_bps, pos_id
            ))
            fixed_count += 1

        logger.info(f"  已回填持仓状态: {fixed_count} 条")

        conn.commit()

    # 最终统计
    with db_manager.get_cursor() as cursor:
        cursor.execute("SELECT status, COUNT(*) as cnt FROM mi_trade_position GROUP BY status")
        rows = cursor.fetchall()
        logger.info("===== 修复完成 =====")
        logger.info("修复后持仓状态分布:")
        for r in rows:
            logger.info(f"  {r['status']}: {r['cnt']}")

        cursor.execute("SELECT COUNT(*) as cnt FROM mi_trade_order WHERE order_side='close'")
        logger.info(f"修复后平仓订单总数: {cursor.fetchone()['cnt']}")


if __name__ == '__main__':
    fix()
