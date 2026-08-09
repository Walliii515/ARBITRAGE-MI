# coding: utf-8
"""订单级已平仓持仓收益计算。"""
from typing import Dict, Iterable, Optional

from common.database import db_manager


def _float_or_none(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float:
    parsed = _float_or_none(value)
    return parsed if parsed is not None else 0.0


def _fee_cost(order: Dict) -> float:
    actual = _float_or_none(order.get('fee_amount_usdt'))
    if actual is not None:
        return actual
    amount = _float_or_none(order.get('exec_amount'))
    if amount is None:
        amount = _float_or_none(order.get('target_amount'))
    rate = _float_or_none(order.get('fee_rate'))
    if amount is None or rate is None:
        return 0.0
    return amount * rate


def _sum_exec_amount(orders: Iterable[Dict], order_side: str, market_type: str) -> float:
    return sum(
        _float(order.get('exec_amount'))
        for order in orders
        if str(order.get('order_side') or '').lower() == order_side
        and str(order.get('market_type') or '').lower() == market_type
        and str(order.get('status') or '').lower() == 'executed'
    )


def _sum_exec_qty(orders: Iterable[Dict], order_side: str, market_type: str) -> float:
    return sum(
        abs(_float(order.get('exec_qty')))
        for order in orders
        if str(order.get('order_side') or '').lower() == order_side
        and str(order.get('market_type') or '').lower() == market_type
        and str(order.get('status') or '').lower() == 'executed'
    )


def _basis_bps(spot_price: float, future_price: float) -> Optional[float]:
    if spot_price <= 0 or future_price <= 0:
        return None
    return (future_price - spot_price) / spot_price * 10000


def compute_closed_position_pnl(pos: Dict, orders: Iterable[Dict]) -> Optional[Dict]:
    """
    用真实成交额计算正向套利已平仓收益。

    公式：
      realized = spot_close - spot_open + future_open - future_close
      total    = realized + funding_total_pnl - fee_cost
    """
    executed_orders = [
        order for order in orders
        if str(order.get('status') or '').lower() == 'executed'
    ]
    if not executed_orders:
        return None

    spot_open = _sum_exec_amount(executed_orders, 'open', 'spot')
    spot_close = _sum_exec_amount(executed_orders, 'close', 'spot')
    future_open = _sum_exec_amount(executed_orders, 'open', 'future')
    future_close = _sum_exec_amount(executed_orders, 'close', 'future')

    if spot_open <= 0:
        spot_open = _float(pos.get('spot_open_amount'))
    if spot_close <= 0:
        spot_close = _float(pos.get('spot_close_amount'))
    if future_open <= 0:
        future_open = (
            _float(pos.get('future_open_amount'))
            or _float(pos.get('future_open_qty')) * _float(pos.get('future_open_price'))
        )
    if future_close <= 0:
        future_close = _float(pos.get('future_close_amount'))

    if spot_open <= 0 or spot_close <= 0 or future_open <= 0 or future_close <= 0:
        return None

    open_notional = spot_open
    spot_close_qty = _sum_exec_qty(executed_orders, 'close', 'spot')
    future_close_qty = _sum_exec_qty(executed_orders, 'close', 'future')
    close_spread_bps = None
    if spot_close_qty > 0 and future_close_qty > 0:
        close_spread_bps = _basis_bps(spot_close / spot_close_qty, future_close / future_close_qty)
    realized_spot = spot_close - spot_open
    realized_future = future_open - future_close
    realized_pnl = realized_spot + realized_future
    fee_cost = sum(_fee_cost(order) for order in executed_orders)
    funding_pnl = _float(pos.get('funding_total_pnl'))
    total_pnl = realized_pnl + funding_pnl - fee_cost

    return {
        'open_notional': round(open_notional, 8),
        'realized_spot_pnl': round(realized_spot, 8),
        'realized_future_pnl': round(realized_future, 8),
        'realized_pnl': round(realized_pnl, 8),
        'realized_pnl_bps': round(realized_pnl / open_notional * 10000, 4),
        'funding_pnl': round(funding_pnl, 8),
        'fee_cost': round(fee_cost, 8),
        'fee_bps': round(-fee_cost / open_notional * 10000, 4),
        'total_pnl': round(total_pnl, 8),
        'total_pnl_bps': round(total_pnl / open_notional * 10000, 4),
        'close_spread_bps': round(close_spread_bps, 4) if close_spread_bps is not None else None,
    }


def fetch_executed_position_orders(position_id: int) -> list[Dict]:
    sql = """
        SELECT id, order_side, market_type, status, exec_price, exec_qty, exec_amount, target_amount,
               fee_rate, fee_amount_usdt, funding_rate_24h, executed_at
        FROM mi_trade_order
        WHERE position_id = %s
          AND status = 'executed'
        ORDER BY id ASC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (position_id,))
        return list(cursor.fetchall())


def existing_position_columns() -> set[str]:
    with db_manager.get_cursor() as cursor:
        cursor.execute("SHOW COLUMNS FROM mi_trade_position")
        return {str(row.get('Field') or '') for row in cursor.fetchall()}


def update_closed_position_pnl(cursor, position_id: int, pnl: Dict, columns: set[str]) -> bool:
    candidate_values = {
        'realized_pnl': pnl['realized_pnl'],
        'realized_pnl_bps': pnl['realized_pnl_bps'],
        'total_pnl': pnl['total_pnl'],
        'total_pnl_bps': pnl['total_pnl_bps'],
        'fee_cost': -pnl['fee_cost'],
        'fee_bps': pnl['fee_bps'],
        'realized_pnl_spot': pnl['realized_spot_pnl'],
        'realized_pnl_future': pnl['realized_future_pnl'],
        'realized_pnl_total': pnl['realized_pnl'],
        'realized_spot_pnl': pnl['realized_spot_pnl'],
        'realized_future_pnl': pnl['realized_future_pnl'],
    }
    if pnl.get('close_spread_bps') is not None:
        candidate_values['close_spread_bps'] = pnl['close_spread_bps']
    values = {key: value for key, value in candidate_values.items() if key in columns}
    if not values:
        return False
    assignments = ', '.join(f"{key} = %s" for key in values)
    sql = f"UPDATE mi_trade_position SET {assignments} WHERE id = %s"
    cursor.execute(sql, list(values.values()) + [position_id])
    return True
