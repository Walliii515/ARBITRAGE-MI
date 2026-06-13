# coding: utf-8
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.reconciliation import Reconciler, ReconciliationConfig, normalize_asset_set
from calc.exchange_desync_remediator import (
    ExchangeDesyncRemediationConfig,
    ExchangeDesyncRemediator,
)


class TestReconciliationIgnoreAssets(unittest.TestCase):
    def test_normalize_asset_set_accepts_list_and_csv(self):
        self.assertEqual(normalize_asset_set(['bnb', ' USDT ', None, '']), {'BNB', 'USDT'})
        self.assertEqual(normalize_asset_set('bnb, fdusd'), {'BNB', 'FDUSD'})

    def test_compare_binance_ignores_fee_asset(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(ignored_binance_spot_assets={'BNB'}),
        )

        rows = reconciler._compare_binance(
            datetime(2026, 6, 7, 10, 0, 0),
            local={'ALLO': 1.0},
            balances=[
                {'asset': 'BNB', 'total': 0.02, 'free': 0.02, 'locked': 0.0},
                {'asset': 'ALLO', 'total': 1.1, 'free': 1.1, 'locked': 0.0},
            ],
        )

        self.assertEqual([row['base_asset'] for row in rows], ['ALLO'])
        self.assertFalse(rows[0]['is_match'])
        self.assertAlmostEqual(rows[0]['diff_value'], 0.1)

    def test_gate_risk_type_from_values(self):
        self.assertEqual(
            Reconciler._gate_risk_type_from_values(local_value=10, exchange_value=0),
            'missing_gate_position',
        )
        self.assertEqual(
            Reconciler._gate_risk_type_from_values(local_value=10, exchange_value=8),
            'qty_mismatch',
        )
        self.assertEqual(
            Reconciler._gate_risk_type_from_values(local_value=0, exchange_value=5),
            'extra_gate_position',
        )

    def test_detect_gate_extra_risk_keeps_exchange_size(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=False),
        )
        risk = reconciler._detect_gate_extra_risk(
            'EPIC',
            datetime(2026, 6, 13, 10, 0, 0),
            local_contracts=0,
            exchange_contracts=12,
            row={'detail': {'size': '-12', 'mark_price': '0.1234'}},
        )

        self.assertEqual(risk['type'], 'extra_gate_position')
        self.assertEqual(risk['exchange_size'], -12)
        self.assertEqual(risk['future_close_size'], 12)
        self.assertEqual(risk['mark_price'], 0.1234)


class TestExchangeDesyncRemediator(unittest.TestCase):
    def test_extra_gate_short_uses_reduce_only_close_buy(self):
        class FakeExecutor:
            contract_meta = {'EPIC': {'quanto_multiplier': 1}}

            def __init__(self):
                self.order = None

            def place_gate_futures_order(self, order):
                self.order = order
                return {
                    'success': True,
                    'exec_price': 0.12,
                    'exec_qty': 12,
                    'exec_amount': 1.44,
                    'exchange_order_id': 'gate-1',
                }

        fake = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            fake,
            ExchangeDesyncRemediationConfig(enabled=True, close_extra_gate_position=True),
        )

        result = remediator.remediate_gate_extra_position(
            'EPIC',
            12,
            {'contract': 'EPIC_USDT', 'exchange_size': -12, 'mark_price': 0.12},
        )

        self.assertTrue(result['success'])
        self.assertEqual(fake.order['order_side'], 'close')
        self.assertEqual(fake.order['trade_direction'], 'buy')
        self.assertEqual(fake.order['future_contract'], 'EPIC_USDT')
        self.assertEqual(fake.order['target_qty'], 12)

    def test_extra_gate_long_is_not_forward_short_remediated(self):
        class FakeExecutor:
            contract_meta = {'EPIC': {'quanto_multiplier': 1}}

            def place_gate_futures_order(self, order):
                raise AssertionError('should not place order for extra long position')

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True, close_extra_gate_position=True),
        )

        result = remediator.remediate_gate_extra_position(
            'EPIC',
            12,
            {'contract': 'EPIC_USDT', 'exchange_size': 12, 'mark_price': 0.12},
        )

        self.assertFalse(result['attempted'])
        self.assertEqual(result['reason'], 'extra_gate_position_not_confirmed_short')


if __name__ == '__main__':
    unittest.main(verbosity=2)
