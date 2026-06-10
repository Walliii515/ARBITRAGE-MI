# coding: utf-8
"""
反向资金费率套利候选计算。

反向策略方向：
- 现货保证金卖出（short spot）
- 永续合约买入（long future）

本模块只负责只读机会富化，不触碰正向开仓/平仓状态机。
"""
from dataclasses import dataclass
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
    min_net_edge_bps: float = 20.0
    max_basis_exposure_bps: float = 50.0
    slippage_buffer_bps: float = 10.0
    funding_capture_ratio: float = 0.5
    strong_funding_24h_bps: float = 50.0
    funding_discount_ratio: float = 0.2
    max_funding_discount_bps: float = 10.0


def _as_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    net_edge = row.get('reverse_net_edge_bps')
    basis = row.get('reverse_basis_bps')
    entry_ceiling = row.get('reverse_entry_ceiling_bps')

    if net_edge is None or entry_ceiling is None or basis is None:
        return 'missing_edge'
    if net_edge < cfg.min_net_edge_bps:
        return 'edge_too_low'
    if basis is not None and basis > cfg.max_basis_exposure_bps:
        return 'basis_too_wide'
    if basis > entry_ceiling:
        return 'basis_above_entry'
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
    full_fee_bps = -calc_full_fee_bps(
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
        borrowable = b_meta.get('borrowable')
        borrow_data_missing = not bool(b_meta)
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
            and borrowable is not False
            and borrow_24h_bps is not None
            and (borrow_capacity_usdt is None or borrow_capacity_usdt >= cfg.open_amount_usdt)
        )
        coverage = row.get('reverse_open_coverage')
        coverage_pass = coverage is not None and float(coverage) <= cfg.orderbook_coverage_threshold

        basis_bps = row.get('reverse_basis_bps')
        reverse_open_p20 = threshold_meta.get('reverse_open_basis_p20')
        funding_discount_bps = min(
            cfg.max_funding_discount_bps,
            max(0.0, gross_funding_bps) * cfg.funding_discount_ratio,
        )
        carry_ceiling_bps = None
        timing_ceiling_bps = None
        entry_ceiling_bps = None
        if borrow_24h_bps is not None:
            carry_ceiling_bps = (
                expected_funding_bps
                - borrow_24h_bps
                - full_fee_bps
                - cfg.slippage_buffer_bps
                - cfg.min_net_edge_bps
            )
        if reverse_open_p20 is not None:
            timing_ceiling_bps = float(reverse_open_p20) + funding_discount_bps
        ceiling_candidates = [
            x for x in (carry_ceiling_bps, timing_ceiling_bps, cfg.max_basis_exposure_bps) if x is not None
        ]
        if ceiling_candidates:
            entry_ceiling_bps = min(ceiling_candidates)
            if gross_funding_bps < cfg.strong_funding_24h_bps and reverse_open_p20 is not None:
                entry_ceiling_bps = min(entry_ceiling_bps, float(reverse_open_p20))

        net_edge_bps = None
        if borrow_24h_bps is not None and basis_bps is not None:
            net_edge_bps = (
                expected_funding_bps
                - float(basis_bps)
                - borrow_24h_bps
                - full_fee_bps
                - cfg.slippage_buffer_bps
            )
        edge_pass = net_edge_bps is not None and net_edge_bps >= cfg.min_net_edge_bps
        basis_pass = (
            basis_bps is not None
            and entry_ceiling_bps is not None
            and float(basis_bps) <= entry_ceiling_bps
            and float(basis_bps) <= cfg.max_basis_exposure_bps
            and edge_pass
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
            'reverse_borrowable': borrowable,
            'reverse_borrow_data_missing': borrow_data_missing,
            'reverse_borrow_pass': borrow_pass,
            'reverse_borrow_capacity_usdt': round(borrow_capacity_usdt, 2) if borrow_capacity_usdt is not None else None,
            'reverse_depth_capacity_usdt': round(depth_capacity_usdt, 2) if depth_capacity_usdt is not None else None,
            'reverse_capacity_usdt': round(reverse_capacity_usdt, 2) if reverse_capacity_usdt is not None else None,
            'reverse_coverage_pass': coverage_pass,
            'reverse_fee_bps': round(full_fee_bps, 4),
            'reverse_slippage_buffer_bps': cfg.slippage_buffer_bps,
            'reverse_carry_ceiling_bps': round(carry_ceiling_bps, 4) if carry_ceiling_bps is not None else None,
            'reverse_timing_ceiling_bps': round(timing_ceiling_bps, 4) if timing_ceiling_bps is not None else None,
            'reverse_funding_discount_bps': round(funding_discount_bps, 4),
            'reverse_entry_ceiling_bps': round(entry_ceiling_bps, 4) if entry_ceiling_bps is not None else None,
            'reverse_net_edge_bps': round(net_edge_bps, 4) if net_edge_bps is not None else None,
            'reverse_basis_pass': basis_pass,
            'reverse_open_basis_p20': reverse_open_p20,
            'reverse_close_basis_p20': threshold_meta.get('reverse_close_basis_p20'),
        })
        row['reverse_status'] = _reverse_status(row, cfg)
