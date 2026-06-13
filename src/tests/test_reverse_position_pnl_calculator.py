# coding: utf-8
import pytest

from calc.reverse_position_pnl_calculator import (
    ReversePnlConfig,
    calculate_reverse_realtime_pnl,
)


def test_reverse_floating_and_total_bps_follow_usdt_amount():
    position = {
        'id': 1,
        'base_asset': 'ABC',
        'status': 'holding',
        'open_amount_usdt': 10.0,
        'spot_open_qty': 0.1,
        'spot_open_price': 100.0,
        'future_open_qty': 0.1,
        'future_open_price': 99.0,
        'reverse_open_basis_bps': -100.0,
        'funding_pnl_usdt': 0.05,
        'funding_pnl_bps': 50.0,
        'borrow_interest_usdt': 0.01,
        'borrow_interest_bps': 10.0,
        'fee_total_usdt': 0.02,
        'fee_total_bps': -20.0,
    }
    orderbook = {
        'ABC': {
            'spot_price_ask_1': 98.0,
            'spot_volume_ask_1': 1.0,
            'future_price_bid_1': 99.5,
            'future_volume_bid_1': 1.0,
        }
    }

    calculate_reverse_realtime_pnl(
        [position],
        orderbook,
        {'ABC': {'quanto_multiplier': 1.0}},
        ReversePnlConfig(open_amount_usdt=10.0),
    )

    assert position['floating_pnl_total'] == pytest.approx(0.25)
    assert position['floating_pnl_bps'] == pytest.approx(250.0)
    assert position['total_pnl'] == pytest.approx(0.27)
    assert position['total_pnl_bps'] == pytest.approx(270.0)
