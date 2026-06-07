# coding: utf-8
"""订单执行角色与手续费字段解析。"""
from typing import Dict, Optional


def _float_or_none(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def infer_liquidity_role(market_key: str, order: Dict, exec_result: Dict) -> str:
    """返回订单实际流动性角色：maker / taker / unknown。"""
    if market_key == 'spot_order':
        return 'taker'

    maker = (exec_result.get('execution_stats') or {}).get('future_maker') or {}
    if maker.get('fallback_filled'):
        return 'taker'
    if maker.get('attempted') and maker.get('filled'):
        return 'maker'
    if order.get('execution_style') == 'maker':
        return 'unknown'
    return 'taker'


def resolve_fee_rate(
    order: Dict,
    role: str,
    *,
    spot_open_fee: float,
    spot_close_fee: float,
    future_open_fee: float,
    future_close_fee: float,
    future_taker_open_fee: float,
    future_taker_close_fee: float,
) -> Optional[float]:
    """根据订单腿、开平仓方向和实际角色返回账户费率。"""
    order_side = order.get('order_side')
    market_type = order.get('market_type')
    if market_type == 'spot':
        return float(spot_open_fee if order_side == 'open' else spot_close_fee)
    if market_type != 'future':
        return None

    if role == 'maker':
        return float(future_open_fee if order_side == 'open' else future_close_fee)
    if role == 'taker':
        return float(future_taker_open_fee if order_side == 'open' else future_taker_close_fee)
    return None


def build_order_execution_fields(
    market_key: str,
    order: Dict,
    exec_data: Optional[Dict],
    exec_result: Dict,
    *,
    spot_open_fee: float,
    spot_close_fee: float,
    future_open_fee: float,
    future_close_fee: float,
    future_taker_open_fee: float,
    future_taker_close_fee: float,
) -> Dict:
    """构造 mi_trade_order 的执行事实字段。"""
    role = infer_liquidity_role(market_key, order, exec_result)
    fee_rate = resolve_fee_rate(
        order,
        role,
        spot_open_fee=spot_open_fee,
        spot_close_fee=spot_close_fee,
        future_open_fee=future_open_fee,
        future_close_fee=future_close_fee,
        future_taker_open_fee=future_taker_open_fee,
        future_taker_close_fee=future_taker_close_fee,
    )
    exec_data = exec_data or {}
    return {
        'liquidity_role': role,
        'fee_rate': fee_rate,
        'fee_amount': _float_or_none(exec_data.get('fee_amount')),
        'fee_amount_usdt': _float_or_none(exec_data.get('fee_amount_usdt')),
        'fee_asset': exec_data.get('fee_asset'),
        'exchange_order_id': exec_data.get('exchange_order_id'),
    }
