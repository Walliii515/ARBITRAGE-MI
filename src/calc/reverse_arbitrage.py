# coding: utf-8
"""
反向资金费率套利候选计算。

反向策略方向：
- 现货保证金卖出（short spot）
- 永续合约买入（long future）

本模块只负责只读机会富化，不触碰正向开仓/平仓状态机。
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from calc.orderbook_enricher import LEVEL, calc_full_fee_bps, calc_vwap, calc_vwap_basis_bps


@dataclass
class ReverseArbitrageConfig:
    """反向机会展示与过滤配置。"""
    open_amount_usdt: float
    spot_open_fee: float
    spot_close_fee: float
    future_open_fee: float
    future_close_fee: float
    orderbook_coverage_threshold: float
    funding_capture_ratio: float = 0.5
    funding_carry_enabled: bool = False
    funding_carry_min_24h_bps: float = 80.0
    funding_carry_max_next_funding_min: float = 60.0
    funding_carry_min_margin_edge_bps: float = 50.0
    funding_carry_basis_relax_bps: float = 30.0


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _minutes_until(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        target = value
    else:
        text = str(value).strip()
        if not text:
            return None
        numeric = _as_float(text)
        if numeric is not None:
            if numeric > 10_000_000_000:
                numeric = numeric / 1000.0
            try:
                target = datetime.fromtimestamp(numeric)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            normalized = text.replace('T', ' ').replace('Z', '')
            if '+' in normalized:
                normalized = normalized.split('+', 1)[0].strip()
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
                try:
                    target = datetime.strptime(normalized[:19], fmt)
                    break
                except ValueError:
                    target = None
            if target is None:
                try:
                    target = datetime.fromisoformat(text)
                except ValueError:
                    return None
    if target.tzinfo is not None:
        target = target.replace(tzinfo=None)
    return (target - datetime.now()).total_seconds() / 60.0


def _side_total_value(row: Dict, market: str, side: str, qty_multiplier: float = 1.0) -> float:
    total = 0.0
    for i in range(1, LEVEL + 1):
        price = _as_float(row.get(f'{market}_price_{side}_{i}'))
        volume = _as_float(row.get(f'{market}_volume_{side}_{i}'))
        if price is None or volume is None:
            continue
        total += price * volume * qty_multiplier
    return total


def _side_prices_and_volumes(row: Dict, market: str, side: str) -> tuple[list, list]:
    prices = [row.get(f'{market}_price_{side}_{i}') for i in range(1, LEVEL + 1)]
    volumes = [row.get(f'{market}_volume_{side}_{i}') for i in range(1, LEVEL + 1)]
    return prices, volumes


def _reverse_status(row: Dict, cfg: ReverseArbitrageConfig) -> str:
    if row.get('reverse_open_data_missing'):
        return 'missing_open_data'
    if not row.get('reverse_funding_pass'):
        return 'funding_too_low'
    if row.get('reverse_borrow_data_missing'):
        return 'missing_borrow_data'
    if row.get('reverse_borrowable') is False:
        return 'borrow_unavailable'
    if not row.get('reverse_borrow_pass'):
        return 'borrow_capacity_low'
    if not row.get('reverse_coverage_pass'):
        return 'depth_too_thin'

    margin_edge = row.get('reverse_margin_edge_bps')
    if margin_edge is None:
        return 'missing_margin_edge'
    if not row.get('reverse_margin_edge_pass'):
        return 'margin_edge_too_low'
    return 'candidate'


def enrich_reverse_opportunities(
    rows: List[Dict],
    contract_meta: Dict[str, Dict],
    cfg: ReverseArbitrageConfig,
    borrow_meta: Optional[Dict[str, Dict]] = None,
    reverse_threshold_meta: Optional[Dict[str, Dict]] = None,
) -> None:
    """为反向机会页面富化行数据（就地修改 rows）。"""
    borrow_meta = borrow_meta or {}
    reverse_threshold_meta = reverse_threshold_meta or {}
    fee_cost_bps = -calc_full_fee_bps(
        cfg.spot_open_fee,
        cfg.spot_close_fee,
        cfg.future_open_fee,
        cfg.future_close_fee,
    )

    for row in rows:
        base_asset = str(row.get('base_asset') or '').upper()
        c_meta = contract_meta.get(base_asset, {})
        b_meta = borrow_meta.get(base_asset, {})
        threshold_meta = reverse_threshold_meta.get(base_asset, {})
        quanto_multiplier = float(c_meta.get('quanto_multiplier', 1.0) or 1.0)

        spot_qty = _as_float(row.get('spot_qty'))
        future_qty = _as_float(row.get('future_qty'))
        hedge_qty = spot_qty if spot_qty is not None else future_qty

        open_data_missing = hedge_qty is None
        if hedge_qty is None:
            row.update({
                'reverse_spot_open_vwap': None,
                'reverse_future_open_vwap': None,
                'reverse_basis_bps': None,
                'reverse_spot_close_vwap': None,
                'reverse_future_close_vwap': None,
                'reverse_close_basis_bps': None,
                'reverse_open_coverage': None,
                'reverse_spot_open_coverage': None,
                'reverse_future_open_coverage': None,
            })
        else:
            spot_bid_prices, spot_bid_volumes = _side_prices_and_volumes(row, 'spot', 'bid')
            future_ask_prices, future_ask_volumes = _side_prices_and_volumes(row, 'future', 'ask')
            spot_open_vwap = calc_vwap(spot_bid_prices, spot_bid_volumes, hedge_qty, 1.0)
            future_open_vwap = calc_vwap(future_ask_prices, future_ask_volumes, hedge_qty, quanto_multiplier)
            reverse_basis_bps = calc_vwap_basis_bps(spot_open_vwap, future_open_vwap)

            spot_ask_prices, spot_ask_volumes = _side_prices_and_volumes(row, 'spot', 'ask')
            future_bid_prices, future_bid_volumes = _side_prices_and_volumes(row, 'future', 'bid')
            spot_close_vwap = calc_vwap(spot_ask_prices, spot_ask_volumes, hedge_qty, 1.0)
            future_close_vwap = calc_vwap(future_bid_prices, future_bid_volumes, hedge_qty, quanto_multiplier)
            reverse_close_basis_bps = calc_vwap_basis_bps(spot_close_vwap, future_close_vwap)

            trade_value = cfg.open_amount_usdt
            spot_bid_total = _side_total_value(row, 'spot', 'bid', 1.0)
            future_ask_total = _side_total_value(row, 'future', 'ask', quanto_multiplier)
            spot_coverage = trade_value / spot_bid_total if spot_bid_total > 0 else None
            future_coverage = trade_value / future_ask_total if future_ask_total > 0 else None
            coverages = [x for x in (spot_coverage, future_coverage) if x is not None]

            row.update({
                'reverse_spot_open_vwap': spot_open_vwap,
                'reverse_future_open_vwap': future_open_vwap,
                'reverse_basis_bps': reverse_basis_bps,
                'reverse_spot_close_vwap': spot_close_vwap,
                'reverse_future_close_vwap': future_close_vwap,
                'reverse_close_basis_bps': reverse_close_basis_bps,
                'reverse_open_coverage': max(coverages) if coverages else None,
                'reverse_spot_open_coverage': spot_coverage,
                'reverse_future_open_coverage': future_coverage,
            })
            open_data_missing = (
                spot_open_vwap is None
                or future_open_vwap is None
                or reverse_basis_bps is None
            )

        funding_rate_24h = _as_float(c_meta.get('funding_rate_24h'))
        gross_funding_bps = abs(funding_rate_24h) * 10000.0 if funding_rate_24h is not None and funding_rate_24h < 0 else 0.0
        expected_funding_bps = gross_funding_bps * cfg.funding_capture_ratio
        funding_pass = gross_funding_bps > 0

        borrow_hourly_rate = _as_float(b_meta.get('hourly_interest_rate'))
        borrow_limit = _as_float(b_meta.get('borrow_limit'))
        max_borrowable_amount = _as_float(b_meta.get('max_borrowable_amount'))
        borrowable = b_meta.get('borrowable')
        borrow_data_missing = not bool(b_meta) or borrow_hourly_rate is None or borrow_limit is None
        borrow_24h_bps = borrow_hourly_rate * 24.0 * 10000.0 if borrow_hourly_rate is not None else None

        spot_price = _as_float(row.get('spot_price_bid_1')) or _as_float(row.get('spot_price_ask_1'))
        borrow_capacity_usdt = (
            borrow_limit * spot_price
            if borrow_limit is not None and spot_price is not None
            else None
        )
        depth_capacity_candidates = [
            _side_total_value(row, 'spot', 'bid', 1.0),
            _side_total_value(row, 'future', 'ask', quanto_multiplier),
        ]
        depth_capacity_usdt = min([x for x in depth_capacity_candidates if x > 0], default=None)
        capacity_candidates = [
            x for x in (borrow_capacity_usdt, depth_capacity_usdt, cfg.open_amount_usdt) if x is not None
        ]
        reverse_capacity_usdt = min(capacity_candidates) if capacity_candidates else None
        borrow_pass = (
            not borrow_data_missing
            and borrowable is True
            and borrow_24h_bps is not None
            and borrow_capacity_usdt is not None
            and borrow_capacity_usdt >= cfg.open_amount_usdt
        )
        coverage = row.get('reverse_open_coverage')
        coverage_pass = coverage is not None and float(coverage) <= cfg.orderbook_coverage_threshold

        basis_bps = row.get('reverse_basis_bps')
        reverse_open_p20 = _as_float(threshold_meta.get('reverse_open_basis_p20'))
        reverse_close_p20 = _as_float(threshold_meta.get('reverse_close_basis_p20'))
        reverse_p20_edge_bps = (
            min(reverse_open_p20, reverse_close_p20)
            if reverse_open_p20 is not None and reverse_close_p20 is not None
            else None
        )
        margin_edge_bps = None
        if borrow_24h_bps is not None and basis_bps is not None and reverse_p20_edge_bps is not None:
            margin_edge_bps = (
                expected_funding_bps
                + reverse_p20_edge_bps
                - float(basis_bps)
                - borrow_24h_bps
                - fee_cost_bps
            )
        margin_edge_pass = margin_edge_bps is not None and margin_edge_bps >= 0
        next_funding_min = _minutes_until(row.get('funding_next_apply') or c_meta.get('funding_next_apply'))
        funding_carry_basis_ceiling_bps = (
            reverse_p20_edge_bps + cfg.funding_carry_basis_relax_bps
            if reverse_p20_edge_bps is not None
            else None
        )
        funding_carry_pass = (
            bool(cfg.funding_carry_enabled)
            and not open_data_missing
            and funding_pass
            and not borrow_data_missing
            and borrow_pass
            and coverage_pass
            and gross_funding_bps >= cfg.funding_carry_min_24h_bps
            and next_funding_min is not None
            and 0 <= next_funding_min <= cfg.funding_carry_max_next_funding_min
            and margin_edge_bps is not None
            and margin_edge_bps >= cfg.funding_carry_min_margin_edge_bps
            and basis_bps is not None
            and funding_carry_basis_ceiling_bps is not None
            and float(basis_bps) <= funding_carry_basis_ceiling_bps
        )

        row.update({
            'reverse_strategy': 'short_spot_long_future',
            'reverse_open_data_missing': open_data_missing,
            'reverse_gross_funding_bps': round(gross_funding_bps, 4),
            'reverse_expected_funding_bps': round(expected_funding_bps, 4),
            'reverse_funding_capture_ratio': cfg.funding_capture_ratio,
            'reverse_funding_pass': funding_pass,
            'reverse_borrow_hourly_rate': borrow_hourly_rate,
            'reverse_borrow_24h_bps': round(borrow_24h_bps, 4) if borrow_24h_bps is not None else None,
            'reverse_borrow_limit': borrow_limit,
            'reverse_max_borrowable_amount': max_borrowable_amount,
            'reverse_borrowable': borrowable,
            'reverse_borrow_data_missing': borrow_data_missing,
            'reverse_borrow_pass': borrow_pass,
            'reverse_borrow_capacity_usdt': round(borrow_capacity_usdt, 2) if borrow_capacity_usdt is not None else None,
            'reverse_depth_capacity_usdt': round(depth_capacity_usdt, 2) if depth_capacity_usdt is not None else None,
            'reverse_capacity_usdt': round(reverse_capacity_usdt, 2) if reverse_capacity_usdt is not None else None,
            'reverse_coverage_pass': coverage_pass,
            'reverse_fee_bps': round(fee_cost_bps, 4),
            'reverse_p20_edge_bps': round(reverse_p20_edge_bps, 4) if reverse_p20_edge_bps is not None else None,
            'reverse_margin_edge_bps': round(margin_edge_bps, 4) if margin_edge_bps is not None else None,
            'reverse_margin_edge_pass': margin_edge_pass,
            'reverse_open_basis_p20': reverse_open_p20,
            'reverse_close_basis_p20': reverse_close_p20,
            'reverse_funding_carry_pass': funding_carry_pass,
            'reverse_funding_carry_next_min': round(next_funding_min, 4) if next_funding_min is not None else None,
            'reverse_funding_carry_basis_ceiling_bps': (
                round(funding_carry_basis_ceiling_bps, 4)
                if funding_carry_basis_ceiling_bps is not None
                else None
            ),
            'reverse_funding_carry_min_24h_bps': cfg.funding_carry_min_24h_bps,
            'reverse_funding_carry_min_margin_edge_bps': cfg.funding_carry_min_margin_edge_bps,
            'reverse_funding_carry_basis_relax_bps': cfg.funding_carry_basis_relax_bps,
        })
        row['reverse_status'] = _reverse_status(row, cfg)
