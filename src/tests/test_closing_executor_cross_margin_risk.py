# coding: utf-8
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tests.test_open_close_logic import make_closing_executor


def _risk_position(**overrides):
    pos = {
        'id': 11,
        'base_asset': 'TUT',
        'future_contract': 'TUT_USDT',
        'spot_symbol': 'TUTUSDT',
        'status': 'holding',
        'spot_open_qty': 10.0,
        'future_open_qty': 10.0,
        'future_open_contracts': 10,
        'spot_open_amount': 10.0,
        'future_open_price': 1.0,
        'open_spread_bps': 100.0,
        'current_spread_bps': 100.0,
        'gate_maintenance_margin_rate': None,
        'gate_contract_position_margin': 0.0,
        'gate_contract_position_margin_equity': None,
        'gate_contract_maintenance_margin': 1.0,
        'gate_liq_price': 1.05,
        'gate_mark_price': 1.03,
    }
    pos.update(overrides)
    return pos


class TestClosingExecutorGateCrossRisk(unittest.TestCase):
    def test_cross_margin_missing_refresh_uses_account_mmr_not_contract_mmr(self):
        ce = make_closing_executor()
        ce.forward_gate_leverage = 0.0
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': {
                'account_mmr_pct': 3349.5,
            },
        }

        self.assertFalse(ce.needs_fresh_margin_risk([
            _risk_position(gate_liq_price=1.20, gate_mark_price=1.00),
        ]))

    def test_cross_margin_missing_refresh_requires_account_mmr_and_liq_price(self):
        ce = make_closing_executor()
        ce.forward_gate_leverage = 0.0
        ce._gate_cross_risk_cache = {'ts': time.time(), 'risk': None}

        self.assertTrue(ce.needs_fresh_margin_risk([_risk_position()]))

        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': {'account_mmr_pct': 3349.5},
        }
        self.assertTrue(ce.needs_fresh_margin_risk([
            _risk_position(gate_liq_price=None),
        ]))


if __name__ == '__main__':
    unittest.main()
