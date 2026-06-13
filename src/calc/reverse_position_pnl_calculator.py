# coding: utf-8
"""反向套利持仓实时盈亏计算。

反向策略独立于正向 `position_pnl_calculator`：
- 持仓方向是 short spot + long future
- 平仓盘口是 spot ask + future bid
- 盈亏口径包含浮动价差、资金费、借币利息和手续费
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from calc.orderbook_enricher import calc_vwap, calc_vwap_basis_bps


@dataclass
class ReversePnlConfig:
    open_amount_usdt: float = 10.0


def _as_float(value, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                return datetime.strptime(value[:19], fmt)
            except ValueError:
                continue
    return None


def _side_prices_and_volumes(row: Dict, prefix: str, side: str) -> tuple[list, list]:
    prices = [row.get(f'{prefix}_price_{side}_{i}') for i in range(1, 21)]
    volumes = [row.get(f'{prefix}_volume_{side}_{i}') for i in range(1, 21)]
    return prices, volumes


def _position_qty(pos: Dict) -> Optional[float]:
    for key in ('spot_open_qty', 'future_open_qty', 'borrow_qty'):
        value = _as_float(pos.get(key))
        if value and value > 0:
            return value
    return None


def _notional(pos: Dict, cfg: ReversePnlConfig) -> float:
    for key in ('open_amount_usdt', 'spot_open_amount', 'future_open_amount'):
        value = _as_float(pos.get(key))
        if value and value > 0:
            return value
    return max(float(cfg.open_amount_usdt or 0), 1.0)


def _fee_cost(pos: Dict) -> float:
    return _as_float(pos.get('fee_total_usdt'), 0.0) or 0.0


def _fee_bps(pos: Dict) -> float:
    value = _as_float(pos.get('fee_total_bps'), 0.0) or 0.0
    # 反向持仓统一口径：手续费是成本，bps 进入总盈亏时应为负值。
    return -abs(value) if value else 0.0


def _funding_pnl(pos: Dict) -> float:
    return _as_float(pos.get('funding_pnl_usdt'), 0.0) or 0.0


def _funding_bps(pos: Dict) -> float:
    return _as_float(pos.get('funding_pnl_bps'), 0.0) or 0.0


def _estimate_borrow_interest(pos: Dict, current_spot: Optional[float]) -> tuple[float, float]:
    stored_interest = _as_float(pos.get('borrow_interest_usdt'), 0.0) or 0.0
    stored_bps = _as_float(pos.get('borrow_interest_bps'), 0.0) or 0.0
    hourly_rate = _as_float(pos.get('borrow_hourly_rate'))
    borrow_qty = _as_float(pos.get('borrow_qty'))
    opened_at = _parse_dt(pos.get('opened_at'))
    if not hourly_rate or not borrow_qty or not opened_at or pos.get('status') == 'closed':
        return stored_interest, stored_bps

    price = current_spot or _as_float(pos.get('spot_open_price')) or 0.0
    hours = max((datetime.now() - opened_at).total_seconds() / 3600.0, 0.0)
    estimated = max(stored_interest, borrow_qty * price * hourly_rate * hours)
    notional = _as_float(pos.get('open_amount_usdt')) or (borrow_qty * price)
    estimated_bps = estimated / notional * 10000 if notional > 0 else stored_bps
    return round(estimated, 8), round(max(stored_bps, estimated_bps), 4)


def _reverse_close_vwap(pos: Dict, orderbook_row: Optional[Dict], contract_meta: Dict[str, Dict]) -> tuple[Optional[float], Optional[float]]:
    if not orderbook_row:
        return None, None
    qty = _position_qty(pos)
    if not qty:
        return None, None
    base_asset = str(pos.get('base_asset') or '').upper()
    quanto_multiplier = _as_float((contract_meta.get(base_asset) or {}).get('quanto_multiplier'), 1.0) or 1.0
    spot_ask_prices, spot_ask_volumes = _side_prices_and_volumes(orderbook_row, 'spot', 'ask')
    future_bid_prices, future_bid_volumes = _side_prices_and_volumes(orderbook_row, 'future', 'bid')
    spot_close = calc_vwap(spot_ask_prices, spot_ask_volumes, qty, 1.0)
    future_close = calc_vwap(future_bid_prices, future_bid_volumes, qty, quanto_multiplier)
    return spot_close, future_close


def _inject_totals(pos: Dict, floating_bps: Optional[float], floating_usdt: Optional[float], cfg: ReversePnlConfig) -> None:
    borrow_interest, borrow_bps = _estimate_borrow_interest(pos, pos.get('current_spot_price'))
    pos['borrow_interest_realtime_usdt'] = borrow_interest
    pos['borrow_interest_realtime_bps'] = borrow_bps
    fee_cost = -round(_fee_cost(pos), 8)
    pos['fee_cost'] = fee_cost
    pos['fee_total_bps'] = round(_fee_bps(pos), 4)
    pos['funding_total_pnl'] = round(_funding_pnl(pos), 8)

    if pos.get('status') == 'closed':
        realized_bps = _as_float(pos.get('realized_pnl_bps'), 0.0) or 0.0
        realized_usdt = _as_float(pos.get('realized_pnl_usdt'), 0.0) or 0.0
    else:
        realized_bps = 0.0
        realized_usdt = 0.0
    pos['realized_pnl'] = round(realized_usdt, 8)
    pos['realized_pnl_display_bps'] = round(realized_bps, 4)

    if floating_bps is None or floating_usdt is None:
        pos['total_pnl_bps'] = None
        pos['total_pnl'] = None
        return

    total_bps = floating_bps + realized_bps + _funding_bps(pos) - borrow_bps + _fee_bps(pos)
    total_usdt = floating_usdt + realized_usdt + _funding_pnl(pos) - borrow_interest + fee_cost
    pos['total_pnl_bps'] = round(total_bps, 4)
    pos['total_pnl'] = round(total_usdt, 8)


def calculate_reverse_realtime_pnl(
    positions: List[Dict],
    orderbook_rows_by_asset: Dict[str, Dict],
    contract_meta: Dict[str, Dict],
    cfg: ReversePnlConfig,
) -> List[Dict]:
    """就地注入反向持仓实时平仓 VWAP、浮动盈亏和总盈亏字段。"""
    for pos in positions:
        base_asset = str(pos.get('base_asset') or '').upper()
        status = pos.get('status')

        if status == 'closed':
            current_spot = _as_float(pos.get('spot_close_price'))
            current_future = _as_float(pos.get('future_close_price'))
            current_basis = _as_float(pos.get('reverse_close_basis_bps'))
            pos['current_spot_price'] = current_spot
            pos['current_future_price'] = current_future
            pos['current_spread_bps'] = current_basis
            pos['floating_spot_pnl'] = 0.0
            pos['floating_future_pnl'] = 0.0
            pos['floating_pnl_total'] = 0.0
            pos['floating_pnl_bps'] = 0.0
            if pos.get('realized_pnl_usdt') is None and current_basis is not None:
                open_basis = _as_float(pos.get('reverse_open_basis_bps'), 0.0) or 0.0
                pos['realized_pnl_bps'] = round(current_basis - open_basis, 4)
                pos['realized_pnl_usdt'] = round((current_basis - open_basis) / 10000 * _notional(pos, cfg), 8)
            _inject_totals(pos, 0.0, 0.0, cfg)
            continue

        spot_close, future_close = _reverse_close_vwap(
            pos,
            orderbook_rows_by_asset.get(base_asset),
            contract_meta,
        )
        current_basis = calc_vwap_basis_bps(spot_close, future_close)
        pos['current_spot_price'] = spot_close
        pos['current_future_price'] = future_close
        pos['current_spread_bps'] = round(current_basis, 4) if current_basis is not None else None

        if spot_close is None or future_close is None or current_basis is None:
            pos['floating_spot_pnl'] = None
            pos['floating_future_pnl'] = None
            pos['floating_pnl_total'] = None
            pos['floating_pnl_bps'] = None
            _inject_totals(pos, None, None, cfg)
            continue

        spot_open = _as_float(pos.get('spot_open_price'))
        future_open = _as_float(pos.get('future_open_price'))
        spot_qty = _as_float(pos.get('spot_open_qty')) or _position_qty(pos)
        future_qty = _as_float(pos.get('future_open_qty')) or _position_qty(pos)
        if not spot_open or not future_open or not spot_qty or not future_qty:
            pos['floating_spot_pnl'] = None
            pos['floating_future_pnl'] = None
            pos['floating_pnl_total'] = None
            pos['floating_pnl_bps'] = None
            _inject_totals(pos, None, None, cfg)
            continue

        floating_spot = (spot_open - spot_close) * spot_qty
        floating_future = (future_close - future_open) * future_qty
        floating_total = floating_spot + floating_future
        open_basis = _as_float(pos.get('reverse_open_basis_bps'))
        floating_bps = current_basis - open_basis if open_basis is not None else floating_total / _notional(pos, cfg) * 10000

        pos['floating_spot_pnl'] = round(floating_spot, 8)
        pos['floating_future_pnl'] = round(floating_future, 8)
        pos['floating_pnl_total'] = round(floating_total, 8)
        pos['floating_pnl_bps'] = round(floating_bps, 4)
        _inject_totals(pos, pos['floating_pnl_bps'], pos['floating_pnl_total'], cfg)

    return positions
