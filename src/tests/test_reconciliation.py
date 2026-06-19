# coding: utf-8
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock

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

    def test_unconfirmed_gate_qty_mismatch_does_not_mark_position_risk(self):
        class TrackingReconciler(Reconciler):
            def __init__(self):
                super().__init__(executor=object(), cfg=ReconciliationConfig(auto_remediate_enabled=False))
                self.mark_calls = []

            def _detect_gate_desync_risk(self, base_asset, snapshot_at, local_contracts, exchange_contracts):
                return {
                    'status': 'desynced',
                    'type': 'qty_mismatch',
                    'event_at': snapshot_at,
                    'detail': f'{base_asset}:{local_contracts}:{exchange_contracts}',
                }

            def _is_gate_risk_confirmed(self, base_asset, risk_type, snapshot_at):
                return False

            def _mark_positions_exchange_risk(self, base_asset, risk):
                self.mark_calls.append((base_asset, risk))
                return 1

        reconciler = TrackingReconciler()
        row = {
            'exchange': 'gate',
            'dimension': 'position',
            'base_asset': 'HEI',
            'local_value': 241,
            'exchange_value': 120,
        }

        risks = reconciler._mark_gate_desync_risks(datetime(2026, 6, 16, 0, 8, 50), [row])

        self.assertEqual(len(risks), 1)
        self.assertFalse(risks[0]['confirmed'])
        self.assertEqual(reconciler.mark_calls, [])
        self.assertFalse(row['detail']['exchange_risk']['confirmed'])

    def test_confirmed_gate_qty_mismatch_marks_position_risk(self):
        class TrackingReconciler(Reconciler):
            def __init__(self):
                super().__init__(executor=object(), cfg=ReconciliationConfig(auto_remediate_enabled=False))
                self.mark_calls = []

            def _detect_gate_desync_risk(self, base_asset, snapshot_at, local_contracts, exchange_contracts):
                return {
                    'status': 'desynced',
                    'type': 'qty_mismatch',
                    'event_at': snapshot_at,
                    'detail': f'{base_asset}:{local_contracts}:{exchange_contracts}',
                }

            def _is_gate_risk_confirmed(self, base_asset, risk_type, snapshot_at):
                return True

            def _mark_positions_exchange_risk(self, base_asset, risk):
                self.mark_calls.append((base_asset, risk))
                return 1

        reconciler = TrackingReconciler()
        row = {
            'exchange': 'gate',
            'dimension': 'position',
            'base_asset': 'HEI',
            'local_value': 241,
            'exchange_value': 120,
        }

        risks = reconciler._mark_gate_desync_risks(datetime(2026, 6, 16, 0, 8, 50), [row])

        self.assertEqual(len(risks), 1)
        self.assertTrue(risks[0]['confirmed'])
        self.assertEqual(len(reconciler.mark_calls), 1)
        self.assertEqual(reconciler.mark_calls[0][0], 'HEI')
        self.assertTrue(reconciler.mark_calls[0][1]['confirmed'])


class TestExchangeDesyncRemediator(unittest.TestCase):
    def test_gate_adl_reuses_prior_spot_fill_when_available_spot_is_zero(self):
        class FakeExecutor:
            contract_meta = {'BEL': {'quanto_multiplier': 1}}

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_positions_to_remediate = MagicMock(return_value=[{
            'id': 222,
            'base_asset': 'BEL',
            'spot_open_qty': 493.0,
            'future_open_qty': 493.0,
            'future_open_price': 0.10217,
            'future_open_contracts': 493,
            'spot_symbol': 'BELUSDT',
            'future_contract': 'BEL_USDT',
            'close_reason': (
                '交易所仓位风险:liquidation|Gate强平|contract=BEL_USDT|'
                'size=6568|price=0.12092|order_id=27866022859296966'
            ),
        }])
        remediator._load_binance_available_qty = MagicMock(return_value=0.0)
        remediator._load_prior_spot_fill = MagicMock(return_value={
            'id': 1571,
            'order_uuid': 'close-risk-order',
            'exec_price': 0.1196,
            'exec_qty': 493.0,
            'exec_amount': 58.9628,
            'created_at': datetime(2026, 6, 19, 15, 48, 34),
        })
        remediator._mark_prior_spot_order_executed = MagicMock()
        remediator._insert_synthetic_future_adl_order = MagicMock()
        remediator._close_position = MagicMock()

        result = remediator.remediate_gate_short_desync(
            'BEL',
            493.0,
            {
                'type': 'missing_gate_position',
                'detail': 'Gate实仓不匹配|contract=BEL_USDT|local=493|exchange=0|missing=493',
            },
            require_desynced=False,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['success_count'], 1)
        self.assertTrue(result['results'][0]['reused_prior_spot_fill'])
        remediator._mark_prior_spot_order_executed.assert_called_once()
        remediator._insert_synthetic_future_adl_order.assert_called_once()
        synthetic_risk = remediator._insert_synthetic_future_adl_order.call_args.args[2]
        self.assertEqual(synthetic_risk['future_close_price'], 0.12092)
        self.assertEqual(synthetic_risk['future_exchange_order_id'], '27866022859296966')
        remediator._close_position.assert_called_once()

    def test_gate_adl_does_not_reuse_partial_prior_spot_fill(self):
        class FakeExecutor:
            contract_meta = {'BEL': {'quanto_multiplier': 1}}

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_positions_to_remediate = MagicMock(return_value=[{
            'id': 222,
            'base_asset': 'BEL',
            'spot_open_qty': 493.0,
            'future_open_qty': 493.0,
            'future_open_price': 0.10217,
            'future_open_contracts': 493,
            'spot_symbol': 'BELUSDT',
            'future_contract': 'BEL_USDT',
        }])
        remediator._load_binance_available_qty = MagicMock(return_value=0.0)
        remediator._load_prior_spot_fill = MagicMock(return_value={
            'id': 1571,
            'order_uuid': 'close-risk-order',
            'exec_price': 0.1196,
            'exec_qty': 100.0,
            'exec_amount': 11.96,
            'created_at': datetime(2026, 6, 19, 15, 48, 34),
        })
        remediator._mark_prior_spot_order_executed = MagicMock()
        remediator._insert_synthetic_future_adl_order = MagicMock()
        remediator._close_position = MagicMock()

        result = remediator.remediate_gate_short_desync(
            'BEL',
            493.0,
            {
                'type': 'liquidation',
                'detail': 'Gate强平|contract=BEL_USDT',
                'future_close_price': 0.12092,
            },
            require_desynced=False,
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['failure_count'], 1)
        self.assertEqual(result['results'][0]['reason'], 'prior_spot_fill_partial')
        remediator._mark_prior_spot_order_executed.assert_not_called()
        remediator._insert_synthetic_future_adl_order.assert_not_called()
        remediator._close_position.assert_not_called()

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
