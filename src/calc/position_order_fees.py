# coding: utf-8
"""按持仓汇总订单级成交与手续费字段。"""
from typing import Dict, List

from common.database import db_manager


def fetch_position_order_fee_summary(position_ids: List[int]) -> Dict[int, Dict]:
    if not position_ids:
        return {}

    placeholders = ','.join(['%s'] * len(position_ids))
    sql = f"""
        SELECT
            position_id,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'spot'
                    THEN COALESCE(exec_amount, target_amount, 0)
                ELSE 0
            END) AS original_spot_open_amount,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'spot'
                    THEN ABS(COALESCE(exec_qty, 0))
                ELSE 0
            END) AS original_spot_open_qty,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'future'
                    THEN COALESCE(exec_amount, target_amount, 0)
                ELSE 0
            END) AS original_future_open_amount,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'future'
                    THEN ABS(COALESCE(exec_qty, 0))
                ELSE 0
            END) AS original_future_open_qty,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'spot'
                    THEN COALESCE(exec_amount, 0)
                ELSE 0
            END) AS executed_spot_close_amount,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'spot'
                    THEN ABS(COALESCE(exec_qty, 0))
                ELSE 0
            END) AS executed_spot_close_qty,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'future'
                    THEN COALESCE(exec_amount, 0)
                ELSE 0
            END) AS executed_future_close_amount,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'future'
                    THEN ABS(COALESCE(exec_qty, 0))
                ELSE 0
            END) AS executed_future_close_qty,
            MAX(CASE WHEN order_side = 'open' AND market_type = 'future' THEN fee_rate END)
                AS future_open_fee_rate,
            MAX(CASE WHEN order_side = 'close' AND market_type = 'future' THEN fee_rate END)
                AS future_close_fee_rate,
            MAX(CASE WHEN order_side = 'open' AND market_type = 'future' THEN liquidity_role END)
                AS future_open_liquidity_role,
            MAX(CASE WHEN order_side = 'close' AND market_type = 'future' THEN liquidity_role END)
                AS future_close_liquidity_role,
            MAX(CASE WHEN order_side = 'open' AND market_type = 'spot' THEN leverage END)
                AS spot_open_leverage,
            MAX(CASE WHEN order_side = 'open' AND market_type = 'future' THEN leverage END)
                AS future_open_leverage,
            MAX(CASE WHEN order_side = 'close' AND market_type = 'spot' THEN leverage END)
                AS spot_close_leverage,
            MAX(CASE WHEN order_side = 'close' AND market_type = 'future' THEN leverage END)
                AS future_close_leverage,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'spot' THEN fee_amount_usdt
                ELSE NULL
            END) AS spot_open_fee_amount_usdt,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'spot' AND fee_amount_usdt IS NULL
                    THEN COALESCE(exec_amount, target_amount, 0) * COALESCE(fee_rate, 0)
                ELSE NULL
            END) AS spot_open_fee_estimated_usdt,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'spot' AND fee_amount_usdt IS NULL
                    THEN 1
                ELSE 0
            END) AS spot_open_fee_estimated_count,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'future' THEN fee_amount_usdt
                ELSE NULL
            END) AS future_open_fee_amount_usdt,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'future' AND fee_amount_usdt IS NULL
                    THEN COALESCE(exec_amount, target_amount, 0) * COALESCE(fee_rate, 0)
                ELSE NULL
            END) AS future_open_fee_estimated_usdt,
            SUM(CASE
                WHEN order_side = 'open' AND market_type = 'future' AND fee_amount_usdt IS NULL
                    THEN 1
                ELSE 0
            END) AS future_open_fee_estimated_count,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'spot' THEN fee_amount_usdt
                ELSE NULL
            END) AS spot_close_fee_amount_usdt,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'spot' AND fee_amount_usdt IS NULL
                    THEN COALESCE(exec_amount, target_amount, 0) * COALESCE(fee_rate, 0)
                ELSE NULL
            END) AS spot_close_fee_estimated_usdt,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'spot' AND fee_amount_usdt IS NULL
                    THEN 1
                ELSE 0
            END) AS spot_close_fee_estimated_count,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'future' THEN fee_amount_usdt
                ELSE NULL
            END) AS future_close_fee_amount_usdt,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'future' AND fee_amount_usdt IS NULL
                    THEN COALESCE(exec_amount, target_amount, 0) * COALESCE(fee_rate, 0)
                ELSE NULL
            END) AS future_close_fee_estimated_usdt,
            SUM(CASE
                WHEN order_side = 'close' AND market_type = 'future' AND fee_amount_usdt IS NULL
                    THEN 1
                ELSE 0
            END) AS future_close_fee_estimated_count
        FROM mi_trade_order
        WHERE position_id IN ({placeholders})
          AND status = 'executed'
        GROUP BY position_id
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, position_ids)
        rows = cursor.fetchall()
    return {int(row['position_id']): row for row in rows if row.get('position_id') is not None}


def attach_position_order_fee_summary(positions: List[Dict]) -> List[Dict]:
    position_ids = [int(p['id']) for p in positions if p.get('id') is not None]
    summaries = fetch_position_order_fee_summary(position_ids)
    for pos in positions:
        summary = summaries.get(int(pos['id'])) if pos.get('id') is not None else None
        if summary:
            pos.update(summary)
    return positions
