# coding: utf-8
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.calculate_hedge_metrics import _truncate_to_tick, calculate_hedge_metrics
from calc.orderbook_enricher import calc_vwap_basis_bps
from calc.virtual_executor import VirtualExecutor
from common.tools import (
    format_binance_order_params,
    format_price_precision,
    format_qty_precision,
    truncate_to_precision,
)


def _w_orderbook():
    return {
        'spot_price_ask_1': 0.01160, 'spot_volume_ask_1': 50000,
        'spot_price_ask_2': 0.01161, 'spot_volume_ask_2': 50000,
        'spot_price_ask_3': 0.01162, 'spot_volume_ask_3': 50000,
        'spot_price_ask_4': 0.01170, 'spot_volume_ask_4': 50000,
        'spot_price_ask_5': 0.01180, 'spot_volume_ask_5': 50000,
        'spot_price_bid_1': 0.01155, 'spot_volume_bid_1': 50000,
        'spot_price_bid_2': 0.01154, 'spot_volume_bid_2': 50000,
        'spot_price_bid_3': 0.01153, 'spot_volume_bid_3': 50000,
        'spot_price_bid_4': 0.01152, 'spot_volume_bid_4': 50000,
        'spot_price_bid_5': 0.01151, 'spot_volume_bid_5': 50000,
        'future_price_bid_1': 0.01170, 'future_volume_bid_1': 500,
        'future_price_bid_2': 0.01169, 'future_volume_bid_2': 500,
        'future_price_bid_3': 0.01168, 'future_volume_bid_3': 500,
        'future_price_bid_4': 0.01167, 'future_volume_bid_4': 500,
        'future_price_bid_5': 0.01166, 'future_volume_bid_5': 500,
        'future_price_ask_1': 0.01175, 'future_volume_ask_1': 500,
        'future_price_ask_2': 0.01176, 'future_volume_ask_2': 500,
        'future_price_ask_3': 0.01177, 'future_volume_ask_3': 500,
        'future_price_ask_4': 0.01178, 'future_volume_ask_4': 500,
        'future_price_ask_5': 0.01179, 'future_volume_ask_5': 500,
    }


def test_truncate_to_precision_uses_floor_not_round():
    assert truncate_to_precision(0.01169, 4) == 0.0116
    assert truncate_to_precision(0.01161, 4) == 0.0116
    assert truncate_to_precision(47.678, 2) == 47.67
    assert truncate_to_precision(47.999, 2) == 47.99
    assert truncate_to_precision(1.9, 0) == 1.0
    assert truncate_to_precision(None, 4) is None
    assert truncate_to_precision(0.01165, 4) == 0.0116
    assert truncate_to_precision(0.01175, 4) == 0.0117
    assert truncate_to_precision(99.999, 2) == 99.99
    assert truncate_to_precision(0.123456789, 8) == 0.12345678
    assert format_price_precision(0.01169, 4) == 0.0116
    assert format_qty_precision(123.4567, 2) == 123.45


@pytest.mark.parametrize(
    ('value', 'precision'),
    [
        (0.01169, 4),
        (0.01175, 4),
        (47.678, 2),
        (None, 4),
        (99.999, 2),
        (0.123456789, 8),
    ],
)
def test_truncate_to_tick_matches_common_precision_helper(value, precision):
    assert _truncate_to_tick(value, precision) == truncate_to_precision(value, precision)


def test_calc_vwap_basis_bps_handles_sign_and_empty_values():
    assert calc_vwap_basis_bps(0.0116, 0.0117) == pytest.approx(
        (0.0117 - 0.0116) / 0.0116 * 10000,
        abs=0.01,
    )
    assert calc_vwap_basis_bps(0.0117, 0.0116) == pytest.approx(
        (0.0116 - 0.0117) / 0.0117 * 10000,
        abs=0.01,
    )
    assert calc_vwap_basis_bps(None, 0.0116) is None
    assert calc_vwap_basis_bps(0.0116, None) is None
    assert calc_vwap_basis_bps(0, 0.0116) is None
    assert calc_vwap_basis_bps(100.0, 100.0) == pytest.approx(0.0)


def test_virtual_executor_vwap_keeps_forward_basis_positive_for_low_price_asset():
    executor = VirtualExecutor(
        {'W': {'quanto_multiplier': 100, 'order_price_round': '0.0001', 'size_decimal': 0}},
        {'W': {'tick_size': '0.0001', 'step_size': 1.0, 'min_qty': 1.0}},
    )

    result = executor.execute(
        {
            'spot_order': {'base_asset': 'W', 'trade_direction': 'buy', 'target_qty': 43000},
            'future_order': {'base_asset': 'W', 'trade_direction': 'sell', 'target_qty': 43000},
        },
        _w_orderbook(),
    )

    assert result['success'] is True
    basis_bps = calc_vwap_basis_bps(
        result['spot_order']['exec_price'],
        result['future_order']['exec_price'],
    )
    assert basis_bps > 0


def test_enricher_and_virtual_executor_use_consistent_open_vwap_path():
    row = {
        'base_asset': 'W',
        'contract': 'W_USDT',
        'spot_ready': True,
        **_w_orderbook(),
    }
    enriched = calculate_hedge_metrics(
        [row],
        {'W': {'quanto_multiplier': 100, 'order_size_min': 1, 'order_price_round': 0.0001}},
        {'W': {'step_size': 1.0, 'tick_size': 0.0001, 'min_qty': 1.0}},
        500,
    )

    executor = VirtualExecutor(
        {'W': {'quanto_multiplier': 100, 'order_price_round': '0.0001', 'size_decimal': 0}},
        {'W': {'tick_size': '0.0001', 'step_size': 1.0, 'min_qty': 1.0}},
    )
    result = executor.execute(
        {
            'spot_order': {
                'base_asset': 'W',
                'trade_direction': 'buy',
                'target_qty': enriched[0]['spot_qty'],
            },
            'future_order': {
                'base_asset': 'W',
                'trade_direction': 'sell',
                'target_qty': enriched[0]['spot_qty'],
            },
        },
        row,
    )

    assert result['success'] is True
    assert enriched[0]['spot_open_vwap'] == result['spot_order']['exec_price']
    assert enriched[0]['future_open_vwap'] == result['future_order']['exec_price']
    assert calc_vwap_basis_bps(
        enriched[0]['spot_open_vwap'],
        enriched[0]['future_open_vwap'],
    ) == pytest.approx(
        calc_vwap_basis_bps(
            result['spot_order']['exec_price'],
            result['future_order']['exec_price'],
        ),
        abs=0.01,
    )


def test_basis_calculation_uses_raw_vwap_precision_without_flooring():
    spot_vwap = 0.011655
    future_vwap = 0.011645

    assert calc_vwap_basis_bps(spot_vwap, future_vwap) == pytest.approx(
        (future_vwap - spot_vwap) / spot_vwap * 10000,
        abs=1e-6,
    )


def test_binance_order_quantity_params_are_floored():
    assert format_binance_order_params('W', 43103.999, 0, 'test-uuid-1234')['quantity'] == '43103.0'
    assert format_binance_order_params('BTC', 0.001567, 5, 'test-uuid-1234')['quantity'] == '0.00156'
