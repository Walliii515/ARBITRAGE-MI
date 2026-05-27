# coding: utf-8
"""
VWAP 快照采样模块

定时采样 VWAP 基差数据并批量落库，用于后续历史分位统计。
"""
from datetime import datetime
from typing import Dict, List

from calc.orderbook_enricher import calc_vwap_basis_bps
from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


def record_vwap_snapshots(merged_rows: List[Dict], open_amount_usdt: float) -> int:
    """
    采样 VWAP 基差数据并批量落库

    Args:
        merged_rows: 已完成 calculate_hedge_metrics 的合并行
        open_amount_usdt: 开仓金额（随行记录）

    Returns:
        实际插入行数
    """
    snapshot_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    insert_rows = []

    for row in merged_rows:
        base_asset = row.get('base_asset')
        spot_open_vwap = row.get('spot_open_vwap')
        future_open_vwap = row.get('future_open_vwap')

        if not base_asset or spot_open_vwap is None or future_open_vwap is None:
            continue

        # 开仓 VWAP 基差 (bps)
        open_basis_bps = calc_vwap_basis_bps(spot_open_vwap, future_open_vwap)
        if open_basis_bps is not None:
            open_basis_bps = round(open_basis_bps, 4)

        # 平仓 VWAP 基差 (bps)
        spot_close_vwap = row.get('spot_close_vwap')
        future_close_vwap = row.get('future_close_vwap')
        close_basis_bps = calc_vwap_basis_bps(spot_close_vwap, future_close_vwap)
        if close_basis_bps is not None:
            close_basis_bps = round(close_basis_bps, 4)

        insert_rows.append((
            snapshot_time,
            base_asset,
            open_amount_usdt,
            spot_open_vwap,
            future_open_vwap,
            spot_close_vwap,
            future_close_vwap,
            open_basis_bps,
            close_basis_bps,
            row.get('open_coverage'),
            row.get('close_coverage'),
        ))

    if not insert_rows:
        return 0

    sql = """
        INSERT INTO mi_vwap_basis_snapshot (
            snapshot_time, base_asset, open_amount_usdt,
            spot_open_vwap, future_open_vwap, spot_close_vwap, future_close_vwap,
            open_vwap_basis_bps, close_vwap_basis_bps,
            open_coverage, close_coverage
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, insert_rows)
        conn.commit()

    return len(insert_rows)
