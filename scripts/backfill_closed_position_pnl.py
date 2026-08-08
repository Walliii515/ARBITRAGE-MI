#!/usr/bin/env python3
# coding: utf-8
"""回填正向套利已平仓持仓的订单级真实收益。"""
import argparse
import os
import sys
from typing import Dict, List


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC = os.path.join(ROOT, 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from common.database import db_manager  # noqa: E402
from calc.closed_position_pnl import (  # noqa: E402
    compute_closed_position_pnl,
    existing_position_columns,
    update_closed_position_pnl,
)


def _load_closed_positions(position_id: int | None = None, limit: int | None = None) -> List[Dict]:
    sql = """
        SELECT *
        FROM mi_trade_position
        WHERE status = 'closed'
    """
    params: list = []
    if position_id is not None:
        sql += " AND id = %s"
        params.append(position_id)
    sql += " ORDER BY id ASC"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


def _load_orders(position_ids: List[int]) -> Dict[int, List[Dict]]:
    if not position_ids:
        return {}
    placeholders = ','.join(['%s'] * len(position_ids))
    sql = f"""
        SELECT position_id, order_side, market_type, status, exec_amount, target_amount,
               fee_rate, fee_amount_usdt
        FROM mi_trade_order
        WHERE position_id IN ({placeholders})
          AND status = 'executed'
        ORDER BY position_id, id ASC
    """
    grouped: Dict[int, List[Dict]] = {pid: [] for pid in position_ids}
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, position_ids)
        for row in cursor.fetchall():
            pid = int(row['position_id'])
            grouped.setdefault(pid, []).append(row)
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='实际写库；不传则只预览')
    parser.add_argument('--position-id', type=int, help='只回填指定持仓 ID')
    parser.add_argument('--limit', type=int, help='最多处理多少条已平仓持仓')
    args = parser.parse_args()

    positions = _load_closed_positions(args.position_id, args.limit)
    orders_by_position = _load_orders([int(pos['id']) for pos in positions])
    columns = existing_position_columns()
    computed = []
    skipped = []

    for pos in positions:
        position_id = int(pos['id'])
        pnl = compute_closed_position_pnl(pos, orders_by_position.get(position_id, []))
        if not pnl:
            skipped.append(position_id)
            continue
        computed.append((position_id, pos.get('base_asset'), pnl))

    total_pnl = sum(item[2]['total_pnl'] for item in computed)
    realized_pnl = sum(item[2]['realized_pnl'] for item in computed)
    fees = sum(item[2]['fee_cost'] for item in computed)
    print(
        f"closed_positions={len(positions)} computed={len(computed)} skipped={len(skipped)} "
        f"realized_pnl={realized_pnl:.8f} fee_cost={fees:.8f} total_pnl={total_pnl:.8f}"
    )
    if skipped:
        preview = ','.join(str(pid) for pid in skipped[:20])
        suffix = '...' if len(skipped) > 20 else ''
        print(f"skipped_position_ids={preview}{suffix}")

    for position_id, base_asset, pnl in computed[:20]:
        print(
            f"preview id={position_id} asset={base_asset} "
            f"realized={pnl['realized_pnl']:.8f} funding={pnl['funding_pnl']:.8f} "
            f"fee={pnl['fee_cost']:.8f} total={pnl['total_pnl']:.8f}"
        )

    if not args.apply:
        print("dry_run=true; pass --apply to update mi_trade_position")
        return 0

    updated = 0
    with db_manager.get_cursor() as cursor:
        for position_id, _, pnl in computed:
            if update_closed_position_pnl(cursor, position_id, pnl, columns):
                updated += 1
    print(f"updated={updated}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
