# coding: utf-8
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.reconciliation import (
    Reconciler,
    ReconciliationConfig,
    build_default_reconciler,
    normalize_asset_set,
)
from calc.real_executor import GATE_CROSS_MARGIN_LEVERAGE
from calc.exchange_desync_remediator import (
    ExchangeDesyncRemediationConfig,
    ExchangeDesyncRemediator,
)


class TestReconciliationIgnoreAssets(unittest.TestCase):
    def test_default_reconciler_uses_cross_margin(self):
        with patch('calc.reconciliation.fetch_contract_meta', return_value={}), \
                patch('calc.reconciliation.fetch_spot_meta', return_value={}), \
                patch('calc.reconciliation.build_exchange_config', return_value=object()), \
                patch('calc.reconciliation.RealExecutor') as executor_cls:
            build_default_reconciler()

        self.assertEqual(
            executor_cls.call_args.kwargs['leverage'],
            GATE_CROSS_MARGIN_LEVERAGE,
        )

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

    def test_matched_post_close_spot_dust_is_sent_to_shared_remediator(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_post_close_spot_dust = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'convert_binance_dust_to_bnb',
        })

        results = reconciler._auto_remediate_post_close_spot_dust(
            binance_rows=[{
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'FRAX',
                'local_value': 0.18,
                'exchange_value': 0.18,
                'is_match': True,
            }],
            gate_rows=[{
                'exchange': 'gate',
                'dimension': 'position',
                'base_asset': 'FRAX',
                'local_value': 0.0,
                'exchange_value': 0.0,
                'is_match': True,
            }],
        )

        self.assertEqual(len(results), 1)
        reconciler.remediator.remediate_post_close_spot_dust.assert_called_once_with(
            base_asset='FRAX',
            local_spot_qty=0.18,
            exchange_spot_qty=0.18,
        )

    def test_post_close_spot_dust_is_not_touched_while_gate_position_remains(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_post_close_spot_dust = MagicMock()

        results = reconciler._auto_remediate_post_close_spot_dust(
            binance_rows=[{
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'FRAX',
                'local_value': 0.18,
                'exchange_value': 0.18,
                'is_match': True,
            }],
            gate_rows=[{
                'exchange': 'gate',
                'dimension': 'position',
                'base_asset': 'FRAX',
                'local_value': 1.0,
                'exchange_value': 1.0,
                'is_match': True,
            }],
        )

        self.assertEqual(results, [])
        reconciler.remediator.remediate_post_close_spot_dust.assert_not_called()

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

    def test_fast_confirmation_rechecks_pending_mismatch_after_configured_delay(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_fast_confirm_delay_sec=3.0),
        )
        first = {
            'success': True,
            'snapshot_at': '2026-08-04 10:00:00',
            'confirmation_pending_count': 1,
        }
        second = {
            'success': True,
            'snapshot_at': '2026-08-04 10:00:03',
            'confirmation_pending_count': 0,
            'remediation_count': 1,
            'remediation_success_count': 1,
        }

        with (
            patch.object(reconciler, 'run_once', side_effect=[first, second]) as run_once,
            patch('calc.reconciliation.time.sleep') as sleep,
        ):
            result = reconciler.run_with_fast_confirmation()

        self.assertEqual(run_once.call_count, 2)
        sleep.assert_called_once_with(3.0)
        self.assertTrue(result['fast_confirmation'])
        self.assertEqual(result['initial_snapshot_at'], first['snapshot_at'])
        self.assertEqual(result['remediation_success_count'], 1)

    def test_fast_confirmation_does_not_wait_when_first_snapshot_is_clean(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_fast_confirm_delay_sec=3.0),
        )
        clean = {
            'success': True,
            'snapshot_at': '2026-08-04 10:00:00',
            'confirmation_pending_count': 0,
        }

        with (
            patch.object(reconciler, 'run_once', return_value=clean) as run_once,
            patch('calc.reconciliation.time.sleep') as sleep,
        ):
            result = reconciler.run_with_fast_confirmation()

        self.assertIs(result, clean)
        run_once.assert_called_once_with()
        sleep.assert_not_called()

    def test_fast_confirmation_cancels_remediation_when_mismatch_disappears(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_fast_confirm_delay_sec=3.0),
        )
        first = {
            'success': True,
            'snapshot_at': '2026-08-04 10:00:00',
            'confirmation_pending_count': 1,
        }
        clean_second = {
            'success': True,
            'snapshot_at': '2026-08-04 10:00:03',
            'confirmation_pending_count': 0,
            'mismatch_count': 0,
            'remediation_count': 0,
            'remediation_success_count': 0,
        }

        with (
            patch.object(reconciler, 'run_once', side_effect=[first, clean_second]),
            patch('calc.reconciliation.time.sleep'),
        ):
            result = reconciler.run_with_fast_confirmation()

        self.assertEqual(result['mismatch_count'], 0)
        self.assertEqual(result['remediation_count'], 0)
        self.assertTrue(result['fast_confirmation'])

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

    def test_gate_extra_success_pairs_binance_extra_spot_remediation(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_gate_extra_position = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'close_extra_gate_future',
        })
        reconciler.remediator.remediate_binance_spot_desync = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'sell_extra_binance_spot',
        })
        reconciler._record_reconciliation_risk_event = MagicMock()

        result = reconciler._auto_remediate_gate_risks(
            datetime(2026, 6, 23, 16, 38, 33),
            [{
                'base_asset': 'BEL',
                'confirmed': True,
                'risk': {
                    'type': 'extra_gate_position',
                    'contract': 'BEL_USDT',
                    'exchange_size': -2770,
                    'mark_price': 0.17781,
                },
                'local_contracts': 2718.0,
                'exchange_contracts': 2770.0,
                'extra_contracts': 52.0,
            }],
            [{
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'BEL',
                'local_value': 2718.0,
                'exchange_value': 2770.0,
            }],
        )

        self.assertTrue(result[0]['success'])
        self.assertEqual(result[0]['paired_binance_spot_result']['action'], 'sell_extra_binance_spot')
        reconciler.remediator.remediate_binance_spot_desync.assert_called_once()
        kwargs = reconciler.remediator.remediate_binance_spot_desync.call_args.kwargs
        self.assertEqual(kwargs['base_asset'], 'BEL')
        self.assertEqual(kwargs['local_qty'], 2718.0)
        self.assertEqual(kwargs['exchange_qty'], 2770.0)

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

    def test_self_reported_gate_desync_confirms_without_prior_snapshot(self):
        class FakeCursor:
            def execute(self, sql, params=None):
                self.params = params

            def fetchone(self):
                return {'id': 222}

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, exc_type, exc, tb):
                return False

        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_confirm_runs=2),
        )
        cursor = FakeCursor()

        with patch('calc.reconciliation.db_manager.get_cursor', return_value=FakeCtx(cursor)):
            confirmed = reconciler._is_gate_risk_confirmed(
                'BEL',
                'missing_gate_position',
                datetime(2026, 6, 19, 18, 0, 0),
            )

        self.assertTrue(confirmed)
        self.assertEqual(cursor.params[0], 'BEL')
        self.assertEqual(cursor.params[1], 'missing_gate_position')

    def test_cleanup_old_snapshots_keeps_mismatches(self):
        class FakeCursor:
            def __init__(self):
                self.sql = ''
                self.params = None

            def execute(self, sql, params=None):
                self.sql = sql
                self.params = params

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, exc_type, exc, tb):
                return False

        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(retention_days=3),
        )
        cursor = FakeCursor()

        with patch('calc.reconciliation.db_manager.get_cursor', return_value=FakeCtx(cursor)):
            reconciler.cleanup_old_snapshots()

        self.assertIn('snapshot_at < %s', cursor.sql)
        self.assertIn('is_match = 1', cursor.sql)
        self.assertEqual(len(cursor.params), 1)


class TestExchangeDesyncRemediator(unittest.TestCase):
    def test_gate_risk_event_zero_limit_processes_all_matching_positions(self):
        class FakeExecutor:
            contract_meta = {'AI': {'quanto_multiplier': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_price': 0.025,
                    'exec_qty': order['target_qty'],
                    'exec_amount': order['target_qty'] * 0.025,
                    'coverage_ratio': 0,
                }

        positions = [
            {
                'id': idx,
                'base_asset': 'AI',
                'spot_open_qty': 1.0,
                'spot_open_price': 0.021,
                'future_open_qty': 1.0,
                'future_open_price': 0.0212,
                'future_open_contracts': 1,
                'spot_symbol': 'AIUSDT',
                'future_contract': 'AI_USDT',
            }
            for idx in range(1, 26)
        ]
        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True, max_positions_per_run=0),
        )
        remediator._risk_with_recent_liquidation = MagicMock(side_effect=lambda _asset, risk: risk)
        remediator._load_positions_to_remediate = MagicMock(return_value=positions)
        remediator._load_binance_available_qty = MagicMock(return_value=25.0)
        remediator._load_prior_future_fill = MagicMock(return_value=None)
        remediator._insert_spot_order = MagicMock()
        remediator._insert_synthetic_future_adl_order = MagicMock()
        remediator._close_position = MagicMock()

        result = remediator.remediate_gate_short_desync(
            'AI',
            25.0,
            {
                'type': 'liquidation',
                'detail': 'Gate强平|contract=AI_USDT|size=25|price=0.02708',
                'future_close_price': 0.02708,
            },
            require_desynced=False,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['positions'], 25)
        self.assertEqual(result['matching_positions'], 25)
        self.assertEqual(result['success_count'], 25)
        self.assertEqual(remediator._close_position.call_count, 25)

    def test_missing_gate_position_reuses_recent_liquidation_price(self):
        class FakeExecutor:
            contract_meta = {'AI': {'quanto_multiplier': 1}}

        class FakeCursor:
            def __init__(self):
                self.params = None

            def execute(self, _sql, params):
                self.params = params

            def fetchone(self):
                return {
                    'risk_type': 'liquidation',
                    'event_at': datetime(2026, 6, 30, 11, 0, 59),
                    'exchange_order_id': 'liq-1',
                    'exchange_trade_id': 'trade-1',
                    'fill_price': 0.02708,
                    'size': 77087,
                }

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, *args):
                return False

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        cursor = FakeCursor()
        with patch('calc.exchange_desync_remediator.db_manager.get_cursor', return_value=FakeCtx(cursor)):
            risk = remediator._risk_with_recent_liquidation('AI', {
                'type': 'missing_gate_position',
                'event_at': datetime(2026, 6, 30, 11, 2, 19),
                'detail': 'Gate实仓不匹配|contract=AI_USDT|local=13376|exchange=0',
            })

        self.assertEqual(risk['future_close_price'], 0.02708)
        self.assertEqual(risk['future_exchange_order_id'], 'liq-1')
        self.assertIn('复用最近Gate', risk['detail'])
        self.assertEqual(cursor.params[0], 'AI')

    def test_close_position_does_not_fallback_to_open_price_without_future_fill(self):
        class FakeExecutor:
            contract_meta = {'AI': {'quanto_multiplier': 1}}

        class FakeCursor:
            def __init__(self):
                self.params = None

            def execute(self, _sql, params):
                self.params = params

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, *args):
                return False

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        cursor = FakeCursor()
        with (
            patch('calc.exchange_desync_remediator.db_manager.get_cursor', return_value=FakeCtx(cursor)),
            patch.object(remediator, '_compute_closed_position_pnl', return_value=None),
        ):
            remediator._close_position(
                {
                    'id': 438,
                    'base_asset': 'AI',
                    'future_open_qty': 2927.0,
                    'future_open_price': 0.0212,
                },
                {'exec_price': 0.025, 'exec_amount': 73.175},
                {'type': 'missing_gate_position'},
                '交易所断腿自动处置|missing_gate_position',
                datetime(2026, 6, 30, 11, 2, 20),
            )

        self.assertIsNone(cursor.params['future_close_price'])
        self.assertIsNone(cursor.params['future_close_amount'])
        self.assertIsNone(cursor.params['close_spread_bps'])

    def test_close_position_updates_order_level_pnl_when_available(self):
        class FakeExecutor:
            contract_meta = {'AI': {'quanto_multiplier': 1}}

        class FakeCursor:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, *args):
                return False

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        cursor = FakeCursor()
        pnl = {
            'realized_pnl': 0.12,
            'realized_pnl_bps': 12.0,
            'total_pnl': 0.10,
            'total_pnl_bps': 10.0,
            'fee_cost': 0.02,
            'fee_bps': -2.0,
            'realized_spot_pnl': 0.2,
            'realized_future_pnl': -0.08,
            'funding_pnl': 0.0,
            'close_spread_bps': 8.5,
        }
        with (
            patch('calc.exchange_desync_remediator.db_manager.get_cursor', return_value=FakeCtx(cursor)),
            patch.object(remediator, '_compute_closed_position_pnl', return_value=pnl),
            patch.object(remediator, '_position_columns', return_value={
                'realized_pnl', 'realized_pnl_bps', 'total_pnl', 'total_pnl_bps',
                'fee_cost', 'fee_bps', 'close_spread_bps',
            }),
        ):
            remediator._close_position(
                {'id': 438, 'base_asset': 'AI', 'future_open_qty': 10.0},
                {'exec_price': 0.025, 'exec_amount': 0.25},
                {'type': 'missing_gate_position', 'future_close_price': 0.026},
                '交易所断腿自动处置|missing_gate_position',
                datetime(2026, 6, 30, 11, 2, 20),
            )

        self.assertEqual(len(cursor.calls), 2)
        self.assertIn('total_pnl', cursor.calls[1][0])
        self.assertIn(0.10, cursor.calls[1][1])

    def test_gate_adl_reuses_prior_spot_fill_when_available_spot_is_zero(self):
        class FakeExecutor:
            contract_meta = {'BEL': {'quanto_multiplier': 1}}

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_recent_liquidation = MagicMock(side_effect=lambda _asset, risk: risk)
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

    def test_gate_desync_reuses_prior_future_fill_after_spot_retry(self):
        class FakeExecutor:
            contract_meta = {'BEL': {'quanto_multiplier': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_price': 0.1196,
                    'exec_qty': order['target_qty'],
                    'exec_amount': 58.9628,
                    'coverage_ratio': 0,
                }

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_recent_liquidation = MagicMock(side_effect=lambda _asset, risk: risk)
        pos = {
            'id': 222,
            'base_asset': 'BEL',
            'spot_open_qty': 493.0,
            'spot_open_price': 0.119,
            'future_open_qty': 493.0,
            'future_open_price': 0.10217,
            'future_open_contracts': 493,
            'spot_symbol': 'BELUSDT',
            'future_contract': 'BEL_USDT',
        }
        remediator._load_positions_to_remediate = MagicMock(return_value=[pos])
        remediator._load_binance_available_qty = MagicMock(return_value=493.0)
        remediator._load_prior_future_fill = MagicMock(return_value={
            'exec_price': 0.12092,
            'exec_qty': 493.0,
            'exec_amount': 59.61356,
            'exchange_order_id': 'gate-1',
            'liquidity_role': 'taker',
        })
        remediator._insert_spot_order = MagicMock()
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
        remediator._insert_synthetic_future_adl_order.assert_not_called()
        close_risk = remediator._close_position.call_args.args[2]
        self.assertEqual(close_risk['future_close_price'], 0.12092)
        self.assertEqual(close_risk['future_exchange_order_id'], 'gate-1')
        self.assertTrue(close_risk['reused_prior_future_fill'])

    def test_gate_desync_low_notional_residual_does_not_submit_spot_order(self):
        class FakeExecutor:
            contract_meta = {'AI': {'quanto_multiplier': 1}}
            spot_meta = {'AI': {'min_notional': 5.0}}

            def __init__(self):
                self.place_binance_spot_order = MagicMock()

        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_recent_liquidation = MagicMock(side_effect=lambda _asset, risk: risk)
        remediator._load_positions_to_remediate = MagicMock(return_value=[{
            'id': 293,
            'base_asset': 'AI',
            'spot_open_qty': 11.0,
            'spot_open_price': 0.021,
            'future_open_qty': 11.0,
            'future_open_price': 0.0212,
            'future_open_contracts': 11,
            'spot_symbol': 'AIUSDT',
            'future_contract': 'AI_USDT',
        }])
        remediator._load_binance_available_qty = MagicMock(return_value=11.0)
        remediator._append_risk_detail = MagicMock()

        result = remediator.remediate_gate_short_desync(
            'AI',
            11.0,
            {
                'type': 'qty_mismatch',
                'detail': 'Gate实仓不匹配|contract=AI_USDT|local=41809|exchange=41798|missing=11',
            },
            require_desynced=False,
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['failure_count'], 1)
        self.assertIn('spot_notional_below_min', result['results'][0]['reason'])
        executor.place_binance_spot_order.assert_not_called()
        remediator._append_risk_detail.assert_called_once()

    def test_missing_gate_full_asset_dust_converts_to_bnb_and_resolves_positions(self):
        class FakeExecutor:
            contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
            spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.1}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock(return_value={
                    'success': True,
                    'asset': 'BICO',
                    'source_qty': 0.2,
                    'bnb_qty': 0.00000955,
                    'transaction_id': 'dust-1',
                    'exec_price_usdt': 0.03,
                })

        positions = [{
            'id': position_id,
            'base_asset': 'BICO',
            'spot_open_qty': 0.1,
            'spot_open_price': 0.024,
            'future_open_qty': 0.1,
            'future_open_contracts': 1,
        } for position_id in (365, 379)]
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_recent_liquidation = MagicMock(side_effect=lambda _asset, risk: risk)
        remediator._load_positions_to_remediate = MagicMock(return_value=positions)
        remediator._load_binance_available_qty = MagicMock(return_value=0.2)
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.03)
        remediator._close_positions_after_dust_conversion = MagicMock()

        result = remediator.remediate_gate_short_desync(
            'BICO',
            2.0,
            {
                'type': 'missing_gate_position',
                'local_contracts': 2.0,
                'exchange_contracts': 0.0,
            },
            require_desynced=True,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'convert_binance_dust_to_bnb')
        self.assertEqual(result['success_count'], 2)
        executor.convert_binance_spot_dust_to_bnb.assert_called_once_with('BICO')
        remediator._close_positions_after_dust_conversion.assert_called_once()

    def test_post_close_full_asset_dust_converts_to_bnb_and_closes_normal_positions(self):
        class FakeExecutor:
            spot_meta = {'FRAX': {'min_notional': 5.0, 'step_size': 0.01}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock(return_value={
                    'success': True,
                    'asset': 'FRAX',
                    'source_qty': 0.18,
                    'bnb_qty': 0.000001,
                    'transaction_id': 'dust-frax',
                    'exec_price_usdt': 0.31,
                })

        positions = [{
            'id': position_id,
            'base_asset': 'FRAX',
            'spot_open_qty': 0.09,
            'spot_open_price': 0.312,
            'future_open_qty': 0.0,
            'future_open_contracts': 0,
            'exchange_risk_status': 'normal',
        } for position_id in (406, 407)]
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_post_close_spot_dust_positions = MagicMock(return_value=positions)
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)
        remediator._load_binance_available_qty = MagicMock(return_value=0.18)
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.31)
        remediator._close_positions_after_dust_conversion = MagicMock()

        result = remediator.remediate_post_close_spot_dust(
            'FRAX',
            local_spot_qty=0.18,
            exchange_spot_qty=0.18,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['success_count'], 2)
        executor.convert_binance_spot_dust_to_bnb.assert_called_once_with('FRAX')
        remediator._close_positions_after_dust_conversion.assert_called_once()

    def test_post_close_dust_does_not_convert_unrelated_exchange_spot(self):
        class FakeExecutor:
            spot_meta = {'FRAX': {'min_notional': 5.0, 'step_size': 0.01}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock()

        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_post_close_spot_dust_positions = MagicMock(return_value=[{
            'id': 407,
            'base_asset': 'FRAX',
            'spot_open_qty': 0.09,
            'spot_open_price': 0.312,
            'future_open_qty': 0.0,
            'future_open_contracts': 0,
        }])
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)
        remediator._load_binance_available_qty = MagicMock(return_value=0.18)
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.31)

        result = remediator.remediate_post_close_spot_dust(
            'FRAX',
            local_spot_qty=0.09,
            exchange_spot_qty=0.18,
        )

        self.assertFalse(result['attempted'])
        executor.convert_binance_spot_dust_to_bnb.assert_not_called()

    def test_post_close_dust_respects_binance_hourly_conversion_cooldown(self):
        class FakeExecutor:
            spot_meta = {'HEI': {'min_notional': 5.0, 'step_size': 0.0001}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock()

        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=3590.0)
        remediator._load_post_close_spot_dust_positions = MagicMock()

        result = remediator.remediate_post_close_spot_dust(
            'HEI',
            local_spot_qty=0.1568,
            exchange_spot_qty=0.1568,
        )

        self.assertFalse(result['attempted'])
        self.assertEqual(result['reason'], 'binance_dust_conversion_cooldown')
        remediator._load_post_close_spot_dust_positions.assert_not_called()
        executor.convert_binance_spot_dust_to_bnb.assert_not_called()

    def test_dust_conversion_does_not_touch_asset_with_unrelated_spot_balance(self):
        class FakeExecutor:
            contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
            spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.1}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock()
                self.place_binance_spot_order = MagicMock()

        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_recent_liquidation = MagicMock(side_effect=lambda _asset, risk: risk)
        remediator._load_positions_to_remediate = MagicMock(return_value=[{
            'id': 379,
            'base_asset': 'BICO',
            'spot_open_qty': 0.1,
            'spot_open_price': 0.024,
            'future_open_qty': 0.1,
            'future_open_contracts': 1,
        }])
        remediator._load_binance_available_qty = MagicMock(return_value=0.2)
        remediator._append_risk_detail = MagicMock()

        result = remediator.remediate_gate_short_desync(
            'BICO',
            1.0,
            {
                'type': 'missing_gate_position',
                'local_contracts': 1.0,
                'exchange_contracts': 0.0,
            },
            require_desynced=True,
        )

        self.assertFalse(result['success'])
        executor.convert_binance_spot_dust_to_bnb.assert_not_called()
        executor.place_binance_spot_order.assert_not_called()

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

    def test_binance_extra_spot_remediation_sells_surplus(self):
        class FakeExecutor:
            contract_meta = {'BEL': {'quanto_multiplier': 1}}

            def __init__(self):
                self.order = None

            def place_binance_spot_order(self, order):
                self.order = order
                return {
                    'success': True,
                    'exec_price': 0.1738,
                    'exec_qty': order['target_qty'],
                    'exec_amount': 9.0376,
                    'coverage_ratio': 0,
                    'exchange_order_id': 'spot-1',
                }

            def _get_binance_usdt_price(self, asset):
                return 0.1738

        fake = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            fake,
            ExchangeDesyncRemediationConfig(enabled=True, remediate_binance_spot_position=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=2024.0)
        remediator._insert_spot_order = MagicMock()

        result = remediator.remediate_binance_spot_desync(
            'BEL',
            local_qty=1972.0,
            exchange_qty=2024.0,
            risk={'type': 'extra_gate_position', 'contract': 'BEL_USDT'},
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'sell_extra_binance_spot')
        self.assertEqual(fake.order['trade_direction'], 'sell')
        self.assertEqual(fake.order['target_qty'], 52.0)
        remediator._insert_spot_order.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
