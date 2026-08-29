# coding: utf-8
import os
import sys
import threading
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
    def test_gate_post_processing_failure_does_not_skip_combined_exposure_check(self):
        executor = MagicMock()
        executor.fetch_binance_spot_balances.return_value = []
        executor.fetch_gate_futures_positions.return_value = []
        reconciler = Reconciler(
            executor=executor,
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler._load_local_spot_positions = MagicMock(return_value={})
        reconciler._load_local_gate_positions = MagicMock(return_value={})
        reconciler._compare_binance = MagicMock(return_value=[])
        reconciler._compare_gate = MagicMock(return_value=[])
        reconciler._mark_gate_desync_risks = MagicMock(side_effect=RuntimeError('db unavailable'))
        reconciler._auto_cleanup_completed_asset_dust = MagicMock(
            side_effect=RuntimeError('dust cleanup unavailable')
        )
        reconciler._build_combined_exposure_rows = MagicMock(return_value=[])
        reconciler._mark_combined_exposure_risks = MagicMock(return_value=[])
        reconciler._auto_remediate_combined_exposure_risks = MagicMock(return_value=[])
        reconciler._insert_rows = MagicMock()
        reconciler.cleanup_old_snapshots = MagicMock()

        result = reconciler.run_once()

        self.assertTrue(result['success'])
        reconciler._build_combined_exposure_rows.assert_called_once()
        reconciler._mark_combined_exposure_risks.assert_called_once_with(
            unittest.mock.ANY,
            [],
        )
        reconciler._auto_remediate_combined_exposure_risks.assert_called_once()

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

    def test_completed_asset_dust_is_sent_to_shared_cleanup_with_exchange_snapshots(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.cleanup_post_close_dust = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'cleanup_post_close_dust_batch',
        })
        binance_balances = [{'asset': 'BICO', 'total': 0.2, 'free': 0.2}]
        gate_positions = [{'base_asset': 'BICO', 'size': -2, 'mark_price': 0.05}]

        results = reconciler._auto_cleanup_completed_asset_dust(
            binance_balances,
            gate_positions,
        )

        self.assertEqual(len(results), 1)
        reconciler.remediator.cleanup_post_close_dust.assert_called_once_with(
            binance_balances,
            gate_positions,
        )

    def test_completed_asset_dust_noop_is_not_counted_as_remediation(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.cleanup_post_close_dust = MagicMock(return_value={
            'attempted': False,
            'success': True,
            'action': 'no_safe_dust_found',
        })

        results = reconciler._auto_cleanup_completed_asset_dust([], [])

        self.assertEqual(results, [])
        reconciler.remediator.cleanup_post_close_dust.assert_called_once_with([], [])

    def test_dust_settlement_assets_are_reserved_from_same_run_remediation(self):
        results = [{
            'base_assets': ['BICO'],
            'settled_pending_positions': [
                {'base_asset': 'BICO', 'position_id': 501},
                {'base_asset': 'FRAX', 'position_id': 502},
            ],
        }]

        self.assertEqual(
            Reconciler._dust_cleanup_owned_assets(results),
            {'BICO', 'FRAX'},
        )

    def test_local_spot_exposure_query_keeps_closed_pending_dust(self):
        reconciler = Reconciler(executor=object())
        cursor = MagicMock()
        cursor.fetchall.return_value = [{'base_asset': 'BICO', 'local_qty': 9358.8}]
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('calc.reconciliation.db_manager.get_cursor', return_value=context):
            result = reconciler._load_local_spot_positions()

        sql, params = cursor.execute.call_args.args
        self.assertIn("p.status = 'closed'", sql)
        self.assertIn('p.exchange_risk_type = %s', sql)
        self.assertEqual(params, ('post_close_spot_dust_pending',))
        self.assertEqual(result, {'BICO': 9358.8})

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

    def test_combined_exposure_row_compares_binance_and_gate_actuals(self):
        executor = MagicMock()
        executor.contract_meta = {'ABC': {'quanto_multiplier': 0.1}}
        reconciler = Reconciler(
            executor=executor,
            cfg=ReconciliationConfig(auto_remediate_enabled=False),
        )

        rows = reconciler._build_combined_exposure_rows(
            snapshot_at=datetime(2026, 8, 9, 12, 0, 0),
            local_spot={'ABC': 10.0},
            local_gate={'ABC': 100.0},
            binance_balances=[{'asset': 'ABC', 'total': 10.0}],
            gate_positions=[{'base_asset': 'ABC', 'size': '-90'}],
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['exchange'], 'combined')
        self.assertEqual(row['dimension'], 'exposure')
        self.assertFalse(row['is_match'])
        self.assertEqual(row['detail']['risk_type'], 'binance_spot_excess')
        self.assertAlmostEqual(row['detail']['gate_qty'], 9.0)
        self.assertAlmostEqual(row['detail']['exchange_diff'], 1.0)

    def test_combined_exposure_missing_multiplier_fails_closed(self):
        executor = MagicMock()
        executor.contract_meta = {}
        reconciler = Reconciler(
            executor=executor,
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )

        rows = reconciler._build_combined_exposure_rows(
            snapshot_at=datetime(2026, 8, 10, 5, 28, 58),
            local_spot={'TUT': 19300.0},
            local_gate={'TUT': 193.0},
            binance_balances=[{'asset': 'TUT', 'total': 19300.0}],
            gate_positions=[{'base_asset': 'TUT', 'size': '-193'}],
        )

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['is_match'])
        self.assertIsNone(rows[0]['detail']['risk_type'])
        self.assertEqual(rows[0]['detail']['status'], 'metadata_unavailable')
        self.assertEqual(
            rows[0]['detail']['recommended_action'],
            'wait_for_contract_metadata',
        )
        self.assertEqual(
            reconciler._mark_combined_exposure_risks(rows[0]['snapshot_at'], rows),
            [],
        )

    def test_gate_local_mismatch_does_not_trade_when_exchange_legs_are_balanced(self):
        executor = MagicMock()
        executor.contract_meta = {'TUT': {'quanto_multiplier': 100.0}}
        reconciler = Reconciler(
            executor=executor,
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_binance_spot_desync = MagicMock()
        reconciler.remediator.remediate_binance_spot_only_exposure = MagicMock()
        reconciler.remediator.remediate_gate_extra_position = MagicMock()
        reconciler._record_reconciliation_risk_event = MagicMock()

        results = reconciler._auto_remediate_gate_risks(
            datetime(2026, 8, 10, 5, 31, 5),
            [{
                'base_asset': 'TUT',
                'confirmed': True,
                'risk': {'type': 'qty_mismatch', 'contract': 'TUT_USDT'},
                'local_contracts': 193.0,
                'exchange_contracts': 2.0,
                'missing_contracts': 191.0,
            }],
            [{
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'TUT',
                'local_value': 19300.0,
                'exchange_value': 200.0,
            }],
        )

        self.assertEqual(
            results[0]['reason'],
            'exchange_legs_balanced_local_ledger_stale',
        )
        reconciler.remediator.remediate_binance_spot_desync.assert_not_called()
        reconciler.remediator.remediate_binance_spot_only_exposure.assert_not_called()
        reconciler.remediator.remediate_gate_extra_position.assert_not_called()

    def test_gate_local_mismatch_sells_only_actual_binance_excess(self):
        executor = MagicMock()
        executor.contract_meta = {'TUT': {'quanto_multiplier': 100.0}}
        reconciler = Reconciler(
            executor=executor,
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_binance_spot_desync = MagicMock(return_value={
            'attempted': True,
            'success': True,
        })
        reconciler._record_reconciliation_risk_event = MagicMock()

        reconciler._auto_remediate_gate_risks(
            datetime(2026, 8, 10, 5, 29, 1),
            [{
                'base_asset': 'TUT',
                'confirmed': True,
                'risk': {'type': 'qty_mismatch', 'contract': 'TUT_USDT'},
                'local_contracts': 193.0,
                'exchange_contracts': 2.0,
                'missing_contracts': 191.0,
            }],
            [{
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'TUT',
                'local_value': 19300.0,
                'exchange_value': 19300.0,
            }],
        )

        kwargs = reconciler.remediator.remediate_binance_spot_desync.call_args.kwargs
        self.assertEqual(kwargs['base_asset'], 'TUT')
        self.assertEqual(kwargs['local_qty'], 200.0)
        self.assertEqual(kwargs['exchange_qty'], 19300.0)

    def test_gate_local_mismatch_closes_only_actual_gate_excess(self):
        executor = MagicMock()
        executor.contract_meta = {'TUT': {'quanto_multiplier': 100.0}}
        reconciler = Reconciler(
            executor=executor,
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_gate_extra_position = MagicMock(return_value={
            'attempted': True,
            'success': True,
        })
        reconciler._record_reconciliation_risk_event = MagicMock()

        reconciler._auto_remediate_gate_risks(
            datetime(2026, 8, 10, 6, 23, 59),
            [{
                'base_asset': 'TUT',
                'confirmed': True,
                'risk': {'type': 'qty_mismatch', 'contract': 'TUT_USDT'},
                'local_contracts': 193.0,
                'exchange_contracts': 2.0,
                'missing_contracts': 191.0,
            }],
            [{
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'TUT',
                'local_value': 19300.0,
                'exchange_value': 0.0,
            }],
        )

        kwargs = reconciler.remediator.remediate_gate_extra_position.call_args.kwargs
        self.assertEqual(kwargs['base_asset'], 'TUT')
        self.assertEqual(kwargs['extra_contracts'], 2.0)
        self.assertEqual(kwargs['risk']['exchange_size'], -2.0)

    def test_combined_binance_excess_uses_spot_only_reduction_when_gate_is_flat(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_binance_spot_only_exposure = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'sell_spot_only_binance_exposure',
        })
        reconciler._record_reconciliation_risk_event = MagicMock()

        results = reconciler._auto_remediate_combined_exposure_risks(
            datetime(2026, 8, 9, 12, 0, 0),
            [{
                'base_asset': 'AI',
                'risk_type': 'binance_spot_excess',
                'confirmed': True,
                'risk': {'type': 'binance_spot_excess', 'contract': 'AI_USDT'},
                'binance_qty': 11.0,
                'gate_qty': 0.0,
                'gate_contracts': 0.0,
                'quanto_multiplier': 1.0,
            }],
        )

        self.assertTrue(results[0]['success'])
        reconciler.remediator.remediate_binance_spot_only_exposure.assert_called_once_with(
            base_asset='AI',
            spot_qty=11.0,
            risk={'type': 'binance_spot_excess', 'contract': 'AI_USDT'},
        )

    def test_combined_gate_excess_uses_reduce_only_gate_close(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_gate_extra_position = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'close_extra_gate_future',
        })
        reconciler._record_reconciliation_risk_event = MagicMock()

        results = reconciler._auto_remediate_combined_exposure_risks(
            datetime(2026, 8, 9, 12, 0, 0),
            [{
                'base_asset': 'BICO',
                'risk_type': 'gate_short_excess',
                'confirmed': True,
                'risk': {'type': 'gate_short_excess', 'contract': 'BICO_USDT'},
                'binance_qty': 8.0,
                'gate_qty': 10.0,
                'gate_contracts': 100.0,
                'quanto_multiplier': 0.1,
            }],
        )

        self.assertTrue(results[0]['success'])
        reconciler.remediator.remediate_gate_extra_position.assert_called_once()
        kwargs = reconciler.remediator.remediate_gate_extra_position.call_args.kwargs
        self.assertEqual(kwargs['base_asset'], 'BICO')
        self.assertAlmostEqual(kwargs['extra_contracts'], 20.0)

    def test_combined_remediation_skips_asset_owned_by_gate_path_this_run(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_binance_spot_desync = MagicMock()
        reconciler._record_reconciliation_risk_event = MagicMock()

        results = reconciler._auto_remediate_combined_exposure_risks(
            datetime(2026, 8, 9, 12, 0, 0),
            [{
                'base_asset': 'BEL',
                'risk_type': 'binance_spot_excess',
                'confirmed': True,
                'risk': {'type': 'binance_spot_excess', 'contract': 'BEL_USDT'},
                'binance_qty': 100.0,
                'gate_qty': 90.0,
                'gate_contracts': 90.0,
                'quanto_multiplier': 1.0,
            }],
            skip_assets={'BEL'},
        )

        self.assertEqual(results[0]['reason'], 'gate_remediation_owns_asset_this_run')
        reconciler.remediator.remediate_binance_spot_desync.assert_not_called()

    def test_same_run_skip_is_per_asset_and_does_not_block_other_assets(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_binance_spot_desync = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'sell_extra_binance_spot',
        })
        reconciler._record_reconciliation_risk_event = MagicMock()
        risks = [
            {
                'base_asset': asset,
                'risk_type': 'binance_spot_excess',
                'confirmed': True,
                'risk': {'type': 'binance_spot_excess', 'contract': f'{asset}_USDT'},
                'binance_qty': 100.0,
                'gate_qty': 90.0,
                'gate_contracts': 90.0,
                'quanto_multiplier': 1.0,
            }
            for asset in ('BEL', 'TUT')
        ]

        results = reconciler._auto_remediate_combined_exposure_risks(
            datetime(2026, 8, 9, 12, 0, 0),
            risks,
            skip_assets={'BEL'},
        )

        self.assertEqual(results[0]['reason'], 'gate_remediation_owns_asset_this_run')
        self.assertTrue(results[1]['success'])
        reconciler.remediator.remediate_binance_spot_desync.assert_called_once()
        self.assertEqual(
            reconciler.remediator.remediate_binance_spot_desync.call_args.kwargs['base_asset'],
            'TUT',
        )

    def test_gate_risk_does_not_own_asset_until_exchange_order_is_submitted(self):
        owned = Reconciler._gate_remediation_owned_assets(
            [
                {'base_asset': 'BEL', 'confirmed': True},
                {'base_asset': 'TUT', 'confirmed': False},
                {'base_asset': 'AI', 'confirmed': False},
                {'base_asset': 'BICO', 'confirmed': True},
            ],
            [
                {'attempted': False, 'reason': 'no_matching_holding_positions'},
                {'attempted': False, 'reason': 'waiting_for_reconciliation_confirmation'},
                {
                    'attempted': True,
                    'success': False,
                    'reason': 'spot_available_qty_insufficient',
                },
                {
                    'attempted': True,
                    'success': False,
                    'exchange_order_submitted': True,
                    'future_result': {'success': False, 'reason': 'exchange_rejected'},
                },
            ],
        )

        self.assertEqual(owned, {'BICO'})

    def test_gate_noop_does_not_block_combined_gate_short_recovery(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_gate_extra_position = MagicMock(return_value={
            'attempted': True,
            'success': True,
            'action': 'close_extra_gate_future',
            'future_result': {'success': True, 'exec_contracts': 2.0},
        })
        reconciler._record_reconciliation_risk_event = MagicMock()
        gate_risks = [{'base_asset': 'TUT', 'confirmed': True}]
        gate_results = [{
            'attempted': False,
            'reason': 'exchange_legs_balanced_local_ledger_stale',
        }]

        results = reconciler._auto_remediate_combined_exposure_risks(
            datetime(2026, 8, 10, 9, 30, 0),
            [{
                'base_asset': 'TUT',
                'risk_type': 'gate_short_excess',
                'confirmed': True,
                'risk': {'type': 'gate_short_excess', 'contract': 'TUT_USDT'},
                'binance_qty': 0.0,
                'gate_qty': 200.0,
                'gate_contracts': 2.0,
                'quanto_multiplier': 100.0,
            }],
            skip_assets=Reconciler._gate_remediation_owned_assets(
                gate_risks,
                gate_results,
            ),
        )

        self.assertTrue(results[0]['success'])
        reconciler.remediator.remediate_gate_extra_position.assert_called_once()
        self.assertEqual(
            reconciler.remediator.remediate_gate_extra_position.call_args.kwargs['extra_contracts'],
            2.0,
        )

    def test_rejected_gate_order_blocks_same_snapshot_combined_retry(self):
        owned = Reconciler._gate_remediation_owned_assets(
            [{'base_asset': 'TUT', 'confirmed': True}],
            [{
                'attempted': True,
                'success': False,
                'exchange_order_submitted': True,
                'future_result': {'success': False, 'reason': 'exchange_rejected'},
            }],
        )

        self.assertEqual(owned, {'TUT'})

    def test_nested_spot_execution_blocks_same_snapshot_combined_retry(self):
        owned = Reconciler._gate_remediation_owned_assets(
            [{'base_asset': 'BEL', 'confirmed': True}],
            [{
                'attempted': True,
                'success': False,
                'results': [{
                    'attempted': True,
                    'exchange_order_submitted': True,
                    'success': False,
                    'reason': 'spot_close_partial_after_retry',
                }],
            }],
        )

        self.assertEqual(owned, {'BEL'})

    def test_gate_remediation_exception_isolated_and_reserves_only_failed_asset(self):
        executor = MagicMock()
        executor.contract_meta = {
            'TUT': {'quanto_multiplier': 100.0},
            'BEL': {'quanto_multiplier': 1.0},
        }
        reconciler = Reconciler(
            executor=executor,
            cfg=ReconciliationConfig(auto_remediate_enabled=True),
        )
        reconciler.remediator.remediate_gate_extra_position = MagicMock(side_effect=[
            RuntimeError('timeout_after_submit'),
            {
                'attempted': True,
                'exchange_order_submitted': True,
                'success': True,
                'future_result': {'success': True, 'exec_contracts': 2.0},
            },
        ])
        reconciler._record_reconciliation_risk_event = MagicMock()
        risks = [
            {
                'base_asset': 'TUT',
                'confirmed': True,
                'risk': {'type': 'qty_mismatch', 'contract': 'TUT_USDT'},
                'local_contracts': 10.0,
                'exchange_contracts': 2.0,
            },
            {
                'base_asset': 'BEL',
                'confirmed': True,
                'risk': {'type': 'qty_mismatch', 'contract': 'BEL_USDT'},
                'local_contracts': 10.0,
                'exchange_contracts': 2.0,
            },
        ]
        binance_rows = [
            {
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'TUT',
                'exchange_value': 0.0,
            },
            {
                'exchange': 'binance',
                'dimension': 'position',
                'base_asset': 'BEL',
                'exchange_value': 0.0,
            },
        ]

        results = reconciler._auto_remediate_gate_risks(
            datetime(2026, 8, 10, 10, 0, 0),
            risks,
            binance_rows,
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]['exchange_order_state_unknown'])
        self.assertTrue(results[0]['retry_needed'])
        self.assertTrue(results[1]['success'])
        self.assertEqual(
            Reconciler._gate_remediation_owned_assets(risks, results),
            {'TUT', 'BEL'},
        )
        self.assertEqual(reconciler.remediator.remediate_gate_extra_position.call_count, 2)

    def test_missing_binance_spot_is_not_bought_by_reduce_only_remediation(self):
        executor = MagicMock()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True, remediate_binance_spot_position=True),
        )

        result = remediator.remediate_binance_spot_desync(
            'BEL',
            local_qty=100.0,
            exchange_qty=90.0,
            risk={'type': 'extra_gate_position', 'contract': 'BEL_USDT'},
        )

        self.assertFalse(result['attempted'])
        self.assertEqual(result['reason'], 'reduce_only_policy_does_not_buy_missing_spot')
        executor.place_binance_spot_order.assert_not_called()

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
    def test_remediation_skips_asset_owned_by_close_thread(self):
        from calc.asset_reduction_guard import asset_reduction_guard

        executor = MagicMock()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        claimed = threading.Event()
        release = threading.Event()

        def hold_close_claim():
            with asset_reduction_guard.claim('TUT', 'closing-test') as acquired:
                self.assertTrue(acquired)
                claimed.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_close_claim)
        holder.start()
        self.assertTrue(claimed.wait(timeout=1))
        try:
            result = remediator.remediate_binance_spot_desync(
                'TUT',
                local_qty=9000.0,
                exchange_qty=18000.0,
                risk={'type': 'binance_spot_excess'},
            )
        finally:
            release.set()
            holder.join(timeout=2)

        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'asset_reduction_inflight')
        executor.place_binance_spot_order.assert_not_called()

    def test_remediation_guard_releases_after_exception(self):
        from calc.asset_reduction_guard import asset_reduction_guard

        remediator = ExchangeDesyncRemediator(
            MagicMock(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(side_effect=RuntimeError('api down'))

        with self.assertRaisesRegex(RuntimeError, 'api down'):
            remediator.remediate_binance_spot_desync(
                'TUT',
                local_qty=0.0,
                exchange_qty=10.0,
                risk={'type': 'binance_spot_excess'},
            )

        self.assertIsNone(asset_reduction_guard.owner('TUT'))

    def test_spot_only_fallback_can_reenter_guard_without_self_blocking(self):
        class FakeExecutor:
            contract_meta = {'TUT': {'quanto_multiplier': 1}}
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_close_with_retry(self, order):
                return {
                    'success': True,
                    'exec_price': 0.14,
                    'exec_qty': order['target_qty'],
                    'exec_amount': order['target_qty'] * 0.14,
                    'exchange_order_id': 'spot-close',
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_spot_only_positions_to_remediate = MagicMock(return_value=[])
        remediator._load_binance_available_qty = MagicMock(return_value=10.0)
        remediator._insert_spot_order = MagicMock()

        result = remediator.remediate_binance_spot_only_exposure(
            'TUT',
            spot_qty=10.0,
            risk={'type': 'binance_spot_excess'},
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'sell_extra_binance_spot')
        remediator._insert_spot_order.assert_called_once()

    def test_gate_risk_event_legacy_limit_does_not_leave_later_positions_exposed(self):
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
            ExchangeDesyncRemediationConfig(enabled=True, max_positions_per_run=20),
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
                    'funding_rate_24h': 0.0017,
                },
                {'exec_price': 0.025, 'exec_amount': 73.175},
                {'type': 'missing_gate_position'},
                '交易所断腿自动处置|missing_gate_position',
                datetime(2026, 6, 30, 11, 2, 20),
            )

        self.assertIsNone(cursor.params['future_close_price'])
        self.assertIsNone(cursor.params['future_close_amount'])
        self.assertIsNone(cursor.params['close_spread_bps'])
        self.assertEqual(cursor.params['close_funding_rate_24h'], 0.0017)

    def test_close_position_prefers_risk_funding_snapshot_over_position_value(self):
        class FakeExecutor:
            contract_meta = {'AI': {'quanto_multiplier': 1}}

        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )

        with (
            patch('calc.exchange_desync_remediator.db_manager.get_cursor', return_value=context),
            patch.object(remediator, '_compute_closed_position_pnl', return_value=None),
        ):
            remediator._close_position(
                {
                    'id': 438,
                    'base_asset': 'AI',
                    'future_open_qty': 10.0,
                    'funding_rate_24h': 0.0017,
                },
                {'exec_price': 0.025, 'exec_amount': 0.25},
                {'type': 'missing_gate_position', 'funding_rate_24h': -0.0021},
                '交易所断腿自动处置|missing_gate_position',
                datetime(2026, 6, 30, 11, 2, 20),
            )

        sql, params = cursor.execute.call_args.args
        self.assertIn('close_funding_rate_24h', sql)
        self.assertEqual(params['close_funding_rate_24h'], -0.0021)

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
            'close_reason': '普通平仓|部分平仓保留剩余',
            '_spot_remaining_qty': 0.09,
            '_future_remaining_qty': 0.0,
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
        self.assertEqual(fake.order['target_contracts'], 12)

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

    def test_manual_dust_cleanup_reconstructs_bico_from_order_ledger(self):
        class FakeExecutor:
            contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
            spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock(return_value={
                    'success': True,
                    'asset': 'BICO',
                    'source_qty': 0.91513,
                    'bnb_qty': 0.00007,
                    'transaction_id': 'dust-bico',
                    'gross_exec_price_usdt': 0.0565,
                })

        positions = [
            {
                'id': 432,
                'base_asset': 'BICO',
                'spot_symbol': 'BICOUSDT',
                'future_contract': 'BICO_USDT',
                'spot_open_qty': 0.4,
                'spot_open_price': 0.056,
                'future_open_qty': 0.3,
                'close_reason': '负资金费风险|部分平仓保留剩余',
                '_spot_remaining_qty': 0.4,
                '_future_remaining_qty': 0.3,
            },
            {
                'id': 433,
                'base_asset': 'BICO',
                'spot_symbol': 'BICOUSDT',
                'future_contract': 'BICO_USDT',
                'spot_open_qty': 0.51513,
                'spot_open_price': 0.056,
                'future_open_qty': 0.3,
                'close_reason': '负资金费风险|部分平仓保留剩余',
                '_spot_remaining_qty': 0.51513,
                '_future_remaining_qty': 0.3,
            },
        ]
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)
        remediator._load_holding_positions_with_execution_remainders = MagicMock(return_value=positions)
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.0566)
        remediator.remediate_gate_extra_position = MagicMock(return_value={
            'success': True,
            'future_result': {
                'success': True,
                'exec_qty': 0.6,
                'exec_price': 0.0566,
                'exchange_order_id': 'gate-dust-close',
            },
        })
        remediator._record_allocated_dust_orders = MagicMock()
        remediator._zero_local_future_dust = MagicMock()
        remediator._close_positions_after_dust_conversion = MagicMock()

        result = remediator.cleanup_post_close_dust(
            [{'asset': 'BICO', 'total': 0.91513, 'free': 0.91513}],
            [{'base_asset': 'BICO', 'size': -6, 'mark_price': 0.0566}],
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['base_asset'], 'BICO')
        self.assertEqual(result['positions'], 2)
        self.assertEqual(result['gate_contracts_closed'], 6.0)
        remediator.remediate_gate_extra_position.assert_called_once()
        self.assertEqual(
            remediator.remediate_gate_extra_position.call_args.kwargs['extra_contracts'],
            6.0,
        )
        executor.convert_binance_spot_dust_to_bnb.assert_called_once_with('BICO')
        remediator._zero_local_future_dust.assert_called_once()
        remediator._close_positions_after_dust_conversion.assert_called_once()

    def test_aggregate_low_notional_hedge_is_cleaned_without_residual_marker(self):
        class FakeExecutor:
            contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
            spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock(return_value={
                    'success': True,
                    'asset': 'BICO',
                    'source_qty': 0.5,
                    'bnb_qty': 0.00004,
                    'transaction_id': 'aggregate-dust-bico',
                    'gross_exec_price_usdt': 1.0,
                })

        positions = [
            {
                'id': 701,
                'base_asset': 'BICO',
                'spot_open_qty': 0.2,
                'spot_open_price': 1.0,
                'future_open_qty': 0.2,
                'close_reason': '开仓通道(funding)',
                '_spot_remaining_qty': 0.2,
                '_future_remaining_qty': 0.2,
            },
            {
                'id': 702,
                'base_asset': 'BICO',
                'spot_open_qty': 0.3,
                'spot_open_price': 1.0,
                'future_open_qty': 0.3,
                'close_reason': '开仓通道(funding)',
                '_spot_remaining_qty': 0.3,
                '_future_remaining_qty': 0.3,
            },
        ]
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)
        remediator._load_holding_positions_with_execution_remainders = MagicMock(
            return_value=positions
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=1.0)
        remediator.remediate_gate_extra_position = MagicMock(return_value={
            'success': True,
            'future_result': {
                'success': True,
                'exec_qty': 0.5,
                'exec_price': 1.0,
                'exchange_order_id': 'gate-aggregate-dust-close',
            },
        })
        remediator._record_allocated_dust_orders = MagicMock()
        remediator._zero_local_future_dust = MagicMock()
        remediator._close_positions_after_dust_conversion = MagicMock()

        result = remediator.cleanup_post_close_dust(
            [{'asset': 'BICO', 'total': 0.5, 'free': 0.5}],
            [{'base_asset': 'BICO', 'size': -5, 'mark_price': 1.0}],
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['base_asset'], 'BICO')
        self.assertEqual(result['positions'], 2)
        self.assertEqual(result['gate_contracts_closed'], 5.0)
        self.assertEqual(
            remediator.remediate_gate_extra_position.call_args.kwargs['extra_contracts'],
            5.0,
        )
        executor.convert_binance_spot_dust_to_bnb.assert_called_once_with('BICO')
        remediator._close_positions_after_dust_conversion.assert_called_once_with(
            positions,
            unittest.mock.ANY,
            unittest.mock.ANY,
        )

    def test_aggregate_low_notional_hedge_requires_exact_exchange_balance(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}
        executor.convert_binance_spot_dust_to_bnb = MagicMock()
        position = {
            'id': 703,
            'base_asset': 'BICO',
            'spot_open_qty': 0.5,
            'spot_open_price': 1.0,
            'close_reason': '开仓通道(funding)',
            '_spot_remaining_qty': 0.5,
            '_future_remaining_qty': 0.5,
        }
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)
        remediator._load_holding_positions_with_execution_remainders = MagicMock(
            return_value=[position]
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=1.0)

        result = remediator.cleanup_post_close_dust(
            [{'asset': 'BICO', 'total': 0.6, 'free': 0.6}],
            [{'base_asset': 'BICO', 'size': -5, 'mark_price': 1.0}],
        )

        self.assertTrue(result['success'])
        self.assertFalse(result['attempted'])
        self.assertEqual(
            result['skipped'],
            [{'base_asset': 'BICO', 'reason': 'exchange_spot_not_explained_by_orders'}],
        )
        executor.convert_binance_spot_dust_to_bnb.assert_not_called()

    def test_low_notional_skip_marker_is_eligible_for_completed_asset_cleanup(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.04)
        position = {
            'id': 501,
            'base_asset': 'BICO',
            'spot_open_qty': 0.1,
            'spot_open_price': 0.04,
            'future_open_qty': 0.1,
            'close_reason': '负资金费风险|低名义残仓跳过平仓|notional=0.0040',
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.1,
        }

        prepared = remediator._prepare_dust_cleanup_candidate(
            'BICO',
            [position],
            {'asset': 'BICO', 'total': 0.1, 'free': 0.1},
            {'base_asset': 'BICO', 'size': -1, 'mark_price': 0.04},
        )

        self.assertTrue(prepared['eligible'])
        self.assertEqual(prepared['spot_qty'], 0.1)
        self.assertEqual(prepared['gate_contracts'], 1.0)

    def test_dust_cleanup_rejects_local_spot_not_explained_by_order_ledger(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.04)
        position = {
            'id': 501,
            'status': 'holding',
            'base_asset': 'BICO',
            'spot_open_qty': 0.2,
            'future_open_qty': 0.1,
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.1,
        }

        prepared = remediator._prepare_dust_cleanup_candidate(
            'BICO',
            [position],
            {'asset': 'BICO', 'total': 0.1, 'free': 0.1},
            {'base_asset': 'BICO', 'size': -1, 'mark_price': 0.04},
        )

        self.assertFalse(prepared['eligible'])
        self.assertEqual(prepared['reason'], 'local_spot_not_explained_by_orders')

    def test_dust_cleanup_rejects_local_gate_not_explained_by_order_ledger(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.04)
        position = {
            'id': 501,
            'status': 'holding',
            'base_asset': 'BICO',
            'spot_open_qty': 0.1,
            'future_open_qty': 0.2,
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.1,
        }

        prepared = remediator._prepare_dust_cleanup_candidate(
            'BICO',
            [position],
            {'asset': 'BICO', 'total': 0.1, 'free': 0.1},
            {'base_asset': 'BICO', 'size': -1, 'mark_price': 0.04},
        )

        self.assertFalse(prepared['eligible'])
        self.assertEqual(prepared['reason'], 'local_gate_not_explained_by_orders')

    def test_pending_dust_uses_order_remainder_for_local_quantity_check(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.04)
        position = {
            'id': 501,
            'status': 'closed',
            'exchange_risk_type': 'post_close_spot_dust_pending',
            'base_asset': 'BICO',
            'spot_open_qty': 10.0,
            'future_open_qty': 10.0,
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.0,
        }

        prepared = remediator._prepare_dust_cleanup_candidate(
            'BICO',
            [position],
            {'asset': 'BICO', 'total': 0.1, 'free': 0.1},
            {'base_asset': 'BICO', 'size': 0, 'mark_price': 0.04},
        )

        self.assertTrue(prepared['eligible'])
        self.assertEqual(prepared['spot_qty'], 0.1)
        self.assertEqual(prepared['gate_contracts'], 0.0)

    def test_dust_cleanup_rejects_future_remainder_without_quanto_multiplier(self):
        executor = MagicMock()
        executor.contract_meta = {}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.04)
        position = {
            'id': 501,
            'status': 'holding',
            'base_asset': 'BICO',
            'spot_open_qty': 0.1,
            'future_open_qty': 0.1,
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.1,
        }

        prepared = remediator._prepare_dust_cleanup_candidate(
            'BICO',
            [position],
            {'asset': 'BICO', 'total': 0.1, 'free': 0.1},
            {'base_asset': 'BICO', 'size': 0, 'mark_price': 0.04},
        )

        self.assertFalse(prepared['eligible'])
        self.assertEqual(prepared['reason'], 'missing_quanto_multiplier')

    def test_spot_only_dust_is_settled_when_whole_asset_exposure_matches(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.04)
        remediator._mark_spot_dust_pending = MagicMock(return_value=True)
        positions = [
            {
                'id': 484,
                'status': 'holding',
                'base_asset': 'BICO',
                'spot_open_qty': 10.0,
                'close_reason': None,
                '_spot_remaining_qty': 10.0,
                '_future_remaining_qty': 10.0,
            },
            {
                'id': 501,
                'status': 'holding',
                'base_asset': 'BICO',
                'spot_open_qty': 0.1,
                'close_reason': '低名义残仓跳过平仓',
                '_spot_remaining_qty': 0.1,
                '_future_remaining_qty': 0.0,
            },
        ]

        settled = remediator._settle_spot_only_dust_positions(
            positions,
            {'BICO': {'asset': 'BICO', 'total': 10.1, 'free': 10.1}},
            {'BICO': {'base_asset': 'BICO', 'size': -100}},
        )

        self.assertEqual(settled, [{
            'position_id': 501,
            'base_asset': 'BICO',
            'spot_qty': 0.1,
            'spot_notional': 0.004,
        }])
        remediator._mark_spot_dust_pending.assert_called_once_with(
            positions[1],
            0.1,
            0.04,
        )

    def test_spot_only_dust_is_not_settled_when_exchange_exposure_differs(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.04)
        remediator._mark_spot_dust_pending = MagicMock(return_value=True)
        positions = [{
            'id': 501,
            'status': 'holding',
            'base_asset': 'BICO',
            'spot_open_qty': 0.1,
            'close_reason': '低名义残仓跳过平仓',
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.0,
        }]

        settled = remediator._settle_spot_only_dust_positions(
            positions,
            {'BICO': {'asset': 'BICO', 'total': 0.2, 'free': 0.2}},
            {},
        )

        self.assertEqual(settled, [])
        remediator._mark_spot_dust_pending.assert_not_called()

    def test_pending_spot_dust_closes_history_and_books_conservative_pnl(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        position = {
            'id': 501,
            'base_asset': 'BICO',
            'funding_total_pnl': 0.5,
        }
        orders = [
            {'order_side': 'open', 'market_type': 'spot', 'status': 'executed', 'exec_qty': 10, 'exec_amount': 10},
            {'order_side': 'open', 'market_type': 'future', 'status': 'executed', 'exec_qty': 10, 'exec_amount': 10.2},
            {'order_side': 'close', 'market_type': 'spot', 'status': 'executed', 'exec_qty': 9.9, 'exec_amount': 10.1, 'executed_at': datetime(2026, 8, 10, 0, 11, 39)},
            {'order_side': 'close', 'market_type': 'future', 'status': 'executed', 'exec_qty': 10, 'exec_amount': 10, 'executed_at': datetime(2026, 8, 10, 0, 11, 39)},
        ]
        cursor = MagicMock()
        cursor.rowcount = 1
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.fetch_executed_position_orders',
            return_value=orders,
        ), patch.object(
            remediator,
            '_position_columns',
            return_value={'realized_pnl', 'total_pnl'},
        ), patch(
            'calc.exchange_desync_remediator.update_closed_position_pnl',
        ) as update_pnl, patch(
            'calc.exchange_desync_remediator.db_manager.get_cursor',
            return_value=context,
        ):
            updated = remediator._mark_spot_dust_pending(position, 0.1, 0.04)

        self.assertTrue(updated)
        sql, params = cursor.execute.call_args.args
        self.assertIn("status = 'closed'", sql)
        self.assertIn("exchange_risk_status = 'normal'", sql)
        self.assertEqual(params['pending_type'], 'post_close_spot_dust_pending')
        self.assertEqual(params['closed_at'], datetime(2026, 8, 10, 0, 11, 39))
        pnl = update_pnl.call_args.args[2]
        self.assertAlmostEqual(pnl['realized_pnl'], 0.3)
        self.assertAlmostEqual(pnl['total_pnl'], 0.8)

    def test_pending_dust_waits_for_active_position_before_conversion(self):
        executor = MagicMock()
        executor.spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.1}}
        executor.convert_binance_spot_dust_to_bnb_batch = MagicMock()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        active = {
            'id': 484,
            'status': 'holding',
            'base_asset': 'BICO',
            'close_reason': None,
            '_spot_remaining_qty': 10.0,
            '_future_remaining_qty': 10.0,
        }
        pending = {
            'id': 501,
            'status': 'closed',
            'base_asset': 'BICO',
            'exchange_risk_type': 'post_close_spot_dust_pending',
            'close_reason': '低名义残仓跳过平仓',
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.0,
        }
        remediator._load_holding_positions_with_execution_remainders = MagicMock(
            side_effect=[[active, {**pending, 'status': 'holding'}], [active, pending]],
        )
        remediator._settle_spot_only_dust_positions = MagicMock(return_value=[{
            'position_id': 501,
            'base_asset': 'BICO',
            'spot_qty': 0.1,
            'spot_notional': 0.004,
        }])
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)

        result = remediator.cleanup_post_close_dust(
            [{'asset': 'BICO', 'total': 10.1, 'free': 10.1}],
            [{'base_asset': 'BICO', 'size': -100, 'mark_price': 0.04}],
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'settle_spot_only_dust_pending')
        executor.convert_binance_spot_dust_to_bnb_batch.assert_not_called()

    def test_manual_dust_cleanup_batches_multiple_spot_dust_assets(self):
        class FakeExecutor:
            contract_meta = {}
            spot_meta = {
                'BICO': {'min_notional': 5.0, 'step_size': 0.00001},
                'FRAX': {'min_notional': 5.0, 'step_size': 0.01},
            }

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb_batch = MagicMock(return_value={
                    'success': True,
                    'results': {
                        'BICO': {
                            'success': True,
                            'asset': 'BICO',
                            'source_qty': 0.2,
                            'bnb_qty': 0.00002,
                            'transaction_id': 'dust-bico',
                            'gross_exec_price_usdt': 0.05,
                        },
                        'FRAX': {
                            'success': True,
                            'asset': 'FRAX',
                            'source_qty': 0.18,
                            'bnb_qty': 0.00001,
                            'transaction_id': 'dust-frax',
                            'gross_exec_price_usdt': 0.31,
                        },
                    },
                })

        positions = [
            {
                'id': 501,
                'base_asset': 'BICO',
                'spot_open_qty': 0.2,
                'spot_open_price': 0.05,
                'close_reason': '普通平仓|部分平仓保留剩余',
                '_spot_remaining_qty': 0.2,
                '_future_remaining_qty': 0.0,
            },
            {
                'id': 502,
                'base_asset': 'FRAX',
                'spot_open_qty': 0.18,
                'spot_open_price': 0.31,
                'close_reason': '普通平仓|部分平仓保留剩余',
                '_spot_remaining_qty': 0.18,
                '_future_remaining_qty': 0.0,
            },
        ]
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)
        remediator._load_holding_positions_with_execution_remainders = MagicMock(return_value=positions)
        remediator._estimate_binance_spot_price = MagicMock(side_effect=lambda asset, _risk: {
            'BICO': 0.05,
            'FRAX': 0.31,
        }[asset])
        remediator._close_positions_after_dust_conversion = MagicMock()

        result = remediator.cleanup_post_close_dust(
            [
                {'asset': 'BICO', 'total': 0.2, 'free': 0.2},
                {'asset': 'FRAX', 'total': 0.18, 'free': 0.18},
            ],
            [],
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['asset_count'], 2)
        self.assertEqual(result['positions'], 2)
        executor.convert_binance_spot_dust_to_bnb_batch.assert_called_once_with(['BICO', 'FRAX'])
        self.assertEqual(remediator._close_positions_after_dust_conversion.call_count, 2)

    def test_manual_dust_cleanup_rejects_unexplained_gate_position(self):
        class FakeExecutor:
            contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
            spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.00001}}

            def __init__(self):
                self.convert_binance_spot_dust_to_bnb = MagicMock()

        position = {
            'id': 432,
            'base_asset': 'BICO',
            'spot_open_qty': 0.91513,
            'spot_open_price': 0.056,
            'future_open_qty': 0.6,
            'close_reason': '部分平仓保留剩余',
            '_spot_remaining_qty': 0.91513,
            '_future_remaining_qty': 0.6,
        }
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=0.0)
        remediator._load_holding_positions_with_execution_remainders = MagicMock(
            return_value=[position]
        )
        remediator._estimate_binance_spot_price = MagicMock(return_value=0.0566)

        result = remediator.cleanup_post_close_dust(
            [{'asset': 'BICO', 'total': 0.91513, 'free': 0.91513}],
            [{'base_asset': 'BICO', 'size': -8, 'mark_price': 0.0566}],
        )

        self.assertTrue(result['success'])
        self.assertFalse(result['attempted'])
        self.assertEqual(
            result['skipped'],
            [{'base_asset': 'BICO', 'reason': 'gate_position_not_explained_by_orders'}],
        )
        executor.convert_binance_spot_dust_to_bnb.assert_not_called()

    def test_dust_close_marks_history_closed_and_recalculates_pnl(self):
        executor = MagicMock()
        executor.contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        position = {
            'id': 432,
            'base_asset': 'BICO',
            'funding_total_pnl': 0.5,
            '_spot_remaining_qty': 0.1,
            '_future_remaining_qty': 0.0,
        }
        orders = [
            {'order_side': 'open', 'market_type': 'spot', 'status': 'executed', 'exec_qty': 1, 'exec_amount': 10},
            {'order_side': 'open', 'market_type': 'future', 'status': 'executed', 'exec_qty': 1, 'exec_amount': 10.1},
            {
                'order_side': 'close', 'market_type': 'spot', 'status': 'executed',
                'exec_qty': 1, 'exec_amount': 10.2, 'funding_rate_24h': 0.0011,
            },
            {
                'order_side': 'close', 'market_type': 'future', 'status': 'executed',
                'exec_qty': 1, 'exec_amount': 10, 'funding_rate_24h': 0.0013,
            },
        ]
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch.object(remediator, '_record_allocated_dust_orders'), \
                patch.object(remediator, '_position_columns', return_value={'total_pnl'}), \
                patch('calc.exchange_desync_remediator.fetch_executed_position_orders', return_value=orders), \
                patch('calc.exchange_desync_remediator.update_closed_position_pnl') as update_pnl, \
                patch('calc.exchange_desync_remediator.db_manager.get_cursor', return_value=context):
            remediator._close_positions_after_dust_conversion(
                [position],
                {
                    'asset': 'BICO',
                    'source_qty': 0.1,
                    'bnb_qty': 0.00001,
                    'transaction_id': 'dust-bico',
                    'gross_exec_price_usdt': 0.0565,
                },
                {'type': 'post_close_dust'},
            )

        status_sql, status_params = cursor.execute.call_args_list[0].args
        self.assertIn("status = 'closed'", status_sql)
        self.assertIn("THEN 'resolved'", status_sql)
        self.assertIn("status = 'closed' AND exchange_risk_type", status_sql)
        self.assertEqual(status_params['pending_type'], 'post_close_spot_dust_pending')
        self.assertEqual(status_params['spot_open_qty'], 1.0)
        self.assertEqual(status_params['future_open_qty'], 1.0)
        self.assertEqual(status_params['future_open_contracts'], 10.0)
        self.assertEqual(status_params['spot_open_amount'], 10.0)
        self.assertEqual(status_params['close_funding_rate_24h'], 0.0013)
        self.assertNotIn('future_open_amount', status_sql)
        pnl = update_pnl.call_args.args[2]
        self.assertAlmostEqual(pnl['realized_pnl'], 0.3)
        self.assertAlmostEqual(pnl['total_pnl'], 0.8)

    def test_manual_cleanup_recovers_completed_exchange_actions_before_cooldown(self):
        executor = MagicMock()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_holding_positions_with_execution_remainders = MagicMock(return_value=[{
            'id': 432,
            'base_asset': 'BICO',
            'spot_open_qty': 0.91513,
            'close_reason': '负资金费风险|部分平仓保留剩余',
            '_spot_remaining_qty': 0.0,
            '_future_remaining_qty': 0.0,
            'dust_cleanup_order_count': 1,
        }])
        remediator._dust_conversion_cooldown_remaining_sec = MagicMock(return_value=3500.0)
        remediator._finalize_dust_positions = MagicMock()

        result = remediator.cleanup_post_close_dust([], [])

        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'finalize_completed_dust_cleanup')
        self.assertEqual(result['base_asset'], 'BICO')
        remediator._finalize_dust_positions.assert_called_once()
        remediator._dust_conversion_cooldown_remaining_sec.assert_not_called()

    def test_gate_missing_leg_partial_spot_retry_keeps_only_real_remainder(self):
        class FakeExecutor:
            contract_meta = {'TUT': {'quanto_multiplier': 1}}
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_close_with_retry(self, order):
                return {
                    'success': False,
                    'exec_price': 0.14,
                    'exec_qty': 6000.0,
                    'exec_amount': 840.0,
                    'exchange_order_id': 'spot-partial',
                    'retry_attempts': 4,
                    'reason': 'Binance平仓重试未完成',
                }

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_prior_future_fill = MagicMock(side_effect=lambda _pos, risk: risk)
        remediator._insert_spot_order = MagicMock()
        remediator._append_risk_detail = MagicMock()
        remediator._close_position = MagicMock()
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        position = {
            'id': 901,
            'base_asset': 'TUT',
            'spot_open_qty': 9000.0,
            'spot_open_price': 0.13,
            'spot_symbol': 'TUTUSDT',
            'future_contract': 'TUT_USDT',
        }

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_cursor',
            return_value=context,
        ):
            result = remediator._sell_spot_and_close_position(
                position,
                9000.0,
                {'type': 'missing_gate_position'},
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['spot_exec_qty'], 6000.0)
        self.assertEqual(result['spot_remaining_qty'], 3000.0)
        self.assertEqual(position['spot_open_qty'], 3000.0)
        _, update_params = cursor.execute.call_args.args
        self.assertEqual(update_params['spot_open_qty'], 3000.0)
        self.assertEqual(update_params['spot_open_amount'], 390.0)
        remediator._insert_spot_order.assert_called_once()
        remediator._close_position.assert_not_called()

    def test_gate_missing_leg_exactly_one_spot_step_remaining_is_not_closed(self):
        class FakeExecutor:
            contract_meta = {'TUT': {'quanto_multiplier': 1}}
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_price': 0.14,
                    'exec_qty': order['target_qty'] - 1,
                    'exec_amount': (order['target_qty'] - 1) * 0.14,
                    'exchange_order_id': 'spot-one-step-left',
                }

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_prior_future_fill = MagicMock(side_effect=lambda _pos, risk: risk)
        remediator._insert_spot_order = MagicMock()
        remediator._append_risk_detail = MagicMock()
        remediator._close_position = MagicMock()
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        position = {
            'id': 902,
            'base_asset': 'TUT',
            'spot_open_qty': 10.0,
            'spot_open_price': 0.13,
            'spot_symbol': 'TUTUSDT',
            'future_contract': 'TUT_USDT',
        }

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_cursor',
            return_value=context,
        ):
            result = remediator._sell_spot_and_close_position(
                position,
                10.0,
                {'type': 'missing_gate_position'},
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['spot_remaining_qty'], 1.0)
        self.assertEqual(position['spot_open_qty'], 1.0)
        remediator._close_position.assert_not_called()

    def test_successful_reduction_of_part_of_local_row_does_not_close_whole_position(self):
        class FakeExecutor:
            contract_meta = {'TUT': {'quanto_multiplier': 1}}
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_price': 0.14,
                    'exec_qty': order['target_qty'],
                    'exec_amount': order['target_qty'] * 0.14,
                    'exchange_order_id': 'spot-partial-row',
                }

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_prior_future_fill = MagicMock(side_effect=lambda _pos, risk: risk)
        remediator._insert_spot_order = MagicMock()
        remediator._append_risk_detail = MagicMock()
        remediator._close_position = MagicMock()
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        position = {
            'id': 903,
            'base_asset': 'TUT',
            'spot_open_qty': 10.0,
            'spot_open_price': 0.13,
            'spot_symbol': 'TUTUSDT',
            'future_contract': 'TUT_USDT',
        }

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_cursor',
            return_value=context,
        ):
            result = remediator._sell_spot_and_close_position(
                position,
                4.0,
                {'type': 'binance_spot_excess'},
            )

        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'position_partially_reduced_waiting_fresh_snapshot')
        self.assertEqual(result['spot_exec_qty'], 4.0)
        self.assertEqual(result['spot_remaining_qty'], 6.0)
        self.assertEqual(position['spot_open_qty'], 6.0)
        remediator._close_position.assert_not_called()

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

    def test_binance_spot_excess_uses_one_order_and_allocates_fill_to_rows(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def __init__(self):
                self.orders = []

            def place_binance_spot_order(self, order):
                self.orders.append(order)
                return {
                    'success': True,
                    'exec_price': 0.14,
                    'exec_qty': order['target_qty'],
                    'exec_amount': order['target_qty'] * 0.14,
                    'fee_amount': 1.2,
                    'fee_amount_usdt': None,
                    'exchange_order_id': 'spot-aggregate',
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        positions = [
            {
                'id': 1,
                'base_asset': 'TUT',
                'spot_open_qty': 10.0,
                'future_open_qty': 4.0,
                '_spot_excess_qty': 6.0,
            },
            {
                'id': 2,
                'base_asset': 'TUT',
                'spot_open_qty': 8.0,
                'future_open_qty': 2.0,
                '_spot_excess_qty': 6.0,
            },
        ]
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=18.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(
            return_value=positions,
        )
        remediator._insert_spot_order = MagicMock()
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='balanced')
        remediator._resolve_balanced_spot_excess_positions = MagicMock(return_value=2)

        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False
        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT',
                local_qty=6.0,
                exchange_qty=18.0,
                risk={'type': 'binance_spot_excess'},
            )

        self.assertTrue(result['success'])
        self.assertEqual(len(executor.orders), 1)
        self.assertEqual(result['allocated_position_count'], 2)
        self.assertEqual(result['allocated_qty'], 12.0)
        self.assertEqual(remediator._insert_spot_order.call_count, 2)
        first_order, first_result = remediator._insert_spot_order.call_args_list[0].args[:2]
        second_order, second_result = remediator._insert_spot_order.call_args_list[1].args[:2]
        self.assertEqual((first_order['position_id'], second_order['position_id']), (1, 2))
        self.assertEqual((first_result['exec_qty'], second_result['exec_qty']), (6.0, 6.0))
        self.assertEqual((first_result['fee_amount'], second_result['fee_amount']), (0.6, 0.6))
        self.assertIsNone(first_result['fee_amount_usdt'])
        self.assertIs(
            remediator._insert_spot_order.call_args_list[0].kwargs['cursor'],
            connection.cursor.return_value,
        )
        remediator._resolve_balanced_spot_excess_positions.assert_called_once_with('TUT')

    def test_partial_aggregate_spot_fill_allocates_only_actual_fill_and_retries(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': False,
                    'exec_price': 0.14,
                    'exec_qty': 6.0,
                    'exec_amount': 0.84,
                    'reason': 'price_filter_retry_exhausted',
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        positions = [
            {'id': 1, 'base_asset': 'TUT', '_spot_excess_qty': 6.0},
            {'id': 2, 'base_asset': 'TUT', '_spot_excess_qty': 6.0},
        ]
        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=18.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(
            return_value=positions,
        )
        remediator._insert_spot_order = MagicMock()
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='balanced')
        remediator._resolve_balanced_spot_excess_positions = MagicMock()

        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False
        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT',
                local_qty=6.0,
                exchange_qty=18.0,
                risk={'type': 'binance_spot_excess'},
            )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(result['allocated_position_count'], 1)
        self.assertEqual(result['allocated_qty'], 6.0)
        self.assertEqual(result['remaining_qty'], 6.0)
        remediator._apply_allocated_spot_reduction.assert_called_once()
        remediator._resolve_balanced_spot_excess_positions.assert_not_called()

    def test_allocation_transaction_failure_keeps_desync_and_audits_exchange_fill(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_price': 0.14,
                    'exec_qty': order['target_qty'],
                    'exec_amount': order['target_qty'] * 0.14,
                    'exchange_order_id': 'spot-aggregate',
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        positions = [
            {'id': 1, 'base_asset': 'TUT', '_spot_excess_qty': 6.0},
            {'id': 2, 'base_asset': 'TUT', '_spot_excess_qty': 6.0},
        ]
        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=18.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(
            return_value=positions,
        )
        remediator._insert_spot_order = MagicMock(side_effect=[
            None,
            RuntimeError('db write failed'),
            None,
        ])
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='balanced')
        remediator._resolve_balanced_spot_excess_positions = MagicMock()
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT',
                local_qty=6.0,
                exchange_qty=18.0,
                risk={'type': 'binance_spot_excess'},
            )

        self.assertFalse(result['success'])
        self.assertTrue(result['exchange_success'])
        self.assertTrue(result['retry_needed'])
        self.assertIn('local_spot_allocation_failed', result['reason'])
        self.assertEqual(remediator._insert_spot_order.call_count, 3)
        fallback_order = remediator._insert_spot_order.call_args_list[-1].args[0]
        self.assertIsNone(fallback_order['position_id'])
        remediator._resolve_balanced_spot_excess_positions.assert_not_called()

    def test_post_allocation_risk_cleanup_failure_does_not_duplicate_fill_order(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_price': 0.14,
                    'exec_qty': order['target_qty'],
                    'exec_amount': order['target_qty'] * 0.14,
                    'exchange_order_id': 'spot-aggregate',
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=12.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(return_value=[{
            'id': 1,
            'base_asset': 'TUT',
            '_spot_excess_qty': 6.0,
        }])
        remediator._insert_spot_order = MagicMock()
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='balanced')
        remediator._resolve_balanced_spot_excess_positions = MagicMock(
            side_effect=RuntimeError('cleanup failed'),
        )
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT',
                local_qty=6.0,
                exchange_qty=12.0,
                risk={'type': 'binance_spot_excess'},
            )

        self.assertFalse(result['success'])
        self.assertTrue(result['exchange_success'])
        self.assertTrue(result['retry_needed'])
        self.assertIn('local_spot_risk_cleanup_failed', result['reason'])
        self.assertEqual(remediator._insert_spot_order.call_count, 1)

    def test_allocated_spot_fill_closes_row_when_both_legs_are_zero(self):
        remediator = ExchangeDesyncRemediator(
            MagicMock(spot_meta={'TUT': {'step_size': 1}}),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._risk_with_prior_future_fill = MagicMock(return_value={
            'type': 'binance_spot_excess',
            'future_close_price': 0.15,
        })
        remediator._close_position = MagicMock()
        pos = {
            'id': 1,
            'base_asset': 'TUT',
            'spot_open_qty': 6.0,
            'spot_open_price': 0.13,
            'future_open_qty': 0.0,
        }

        state = remediator._apply_allocated_spot_reduction(
            pos,
            6.0,
            {'exec_qty': 6.0, 'exec_price': 0.14},
            {'type': 'binance_spot_excess'},
            '对账兜底',
            datetime(2026, 8, 9, 10, 0, 0),
        )

        self.assertEqual(state, 'closed')
        self.assertEqual(pos['spot_open_qty'], 0.0)
        self.assertEqual(pos['exchange_risk_status'], 'resolved')
        remediator._close_position.assert_called_once()

    def test_allocated_spot_fill_keeps_balanced_remainder_holding_and_resolved(self):
        executor = MagicMock()
        executor.spot_meta = {'TUT': {'step_size': 1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        pos = {
            'id': 1,
            'base_asset': 'TUT',
            'spot_open_qty': 10.0,
            'spot_open_price': 0.13,
            'future_open_qty': 6.0,
        }

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_cursor',
            return_value=context,
        ):
            state = remediator._apply_allocated_spot_reduction(
                pos,
                4.0,
                {'exec_qty': 4.0, 'exec_price': 0.14},
                {'type': 'binance_spot_excess'},
                '对账兜底',
                datetime(2026, 8, 9, 10, 0, 0),
            )

        self.assertEqual(state, 'balanced')
        self.assertEqual(pos['spot_open_qty'], 6.0)
        self.assertEqual(pos['spot_open_amount'], 0.78)
        self.assertEqual(pos['exchange_risk_status'], 'resolved')
        params = cursor.execute.call_args.args[1]
        self.assertEqual(params['risk_status'], 'resolved')
        self.assertIsNone(params['risk_type'])

    def test_exactly_one_spot_step_difference_remains_desynced(self):
        executor = MagicMock()
        executor.spot_meta = {'TUT': {'step_size': 1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        cursor = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        pos = {
            'id': 1,
            'base_asset': 'TUT',
            'spot_open_qty': 10.0,
            'spot_open_price': 0.13,
            'future_open_qty': 5.0,
        }

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_cursor',
            return_value=context,
        ):
            state = remediator._apply_allocated_spot_reduction(
                pos,
                4.0,
                {'exec_qty': 4.0, 'exec_price': 0.14},
                {'type': 'binance_spot_excess'},
                '对账兜底',
                datetime(2026, 8, 9, 10, 0, 0),
            )

        self.assertEqual(state, 'desynced')
        self.assertEqual(pos['spot_open_qty'], 6.0)
        self.assertEqual(pos['future_open_qty'], 5.0)
        self.assertEqual(pos['exchange_risk_status'], 'desynced')

    def test_successful_aggregate_reduction_clears_balanced_sibling_marks(self):
        executor = MagicMock()
        executor.spot_meta = {'TUT': {'step_size': 1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        cursor = MagicMock()
        cursor.rowcount = 3
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_cursor',
            return_value=context,
        ):
            updated = remediator._resolve_balanced_spot_excess_positions('tut')

        self.assertEqual(updated, 3)
        sql, params = cursor.execute.call_args.args
        self.assertIn("exchange_risk_type = 'binance_spot_excess'", sql)
        self.assertIn('ABS(', sql)
        self.assertEqual(params['base_asset'], 'TUT')
        self.assertLess(params['tolerance'], 1.0)

    def test_sub_step_binance_spot_difference_is_ignored(self):
        executor = MagicMock()
        executor.spot_meta = {'TUT': {'step_size': 1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock()

        result = remediator.remediate_binance_spot_desync(
            'TUT',
            local_qty=10.0,
            exchange_qty=10.5,
            risk={'type': 'binance_spot_excess'},
        )

        self.assertFalse(result['attempted'])
        self.assertEqual(result['reason'], 'binance_spot_diff<=tolerance')
        remediator._load_binance_available_qty.assert_not_called()

    def test_exactly_one_spot_step_difference_is_reduced(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def __init__(self):
                self.order = None

            def place_binance_spot_order(self, order):
                self.order = order
                return {
                    'success': True,
                    'exec_price': 0.14,
                    'exec_qty': order['target_qty'],
                    'exec_amount': order['target_qty'] * 0.14,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=11.0)
        remediator._insert_spot_order = MagicMock()

        result = remediator.remediate_binance_spot_desync(
            'TUT',
            local_qty=10.0,
            exchange_qty=11.0,
            risk={'type': 'extra_gate_position'},
        )

        self.assertTrue(result['success'])
        self.assertEqual(executor.order['target_qty'], 1.0)

    def test_executor_success_with_zero_fill_is_incomplete_and_retried(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, _order):
                return {'success': True, 'exec_qty': 0.0, 'exec_price': 0.14}

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._insert_spot_order = MagicMock()

        result = remediator.remediate_binance_spot_desync(
            'TUT', 10.0, 20.0, {'type': 'extra_gate_position'},
        )

        self.assertFalse(result['success'])
        self.assertFalse(result['exchange_success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(result['remaining_qty'], 10.0)
        self.assertIn('spot_reduction_incomplete', result['reason'])

    def test_executor_success_with_partial_fill_allocates_only_fill_and_retries(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, _order):
                return {
                    'success': True,
                    'exec_qty': 4.0,
                    'exec_price': 0.14,
                    'exec_amount': 0.56,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(return_value=[{
            'id': 1,
            'base_asset': 'TUT',
            '_spot_excess_qty': 10.0,
        }])
        remediator._insert_spot_order = MagicMock()
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='desynced')
        remediator._resolve_balanced_spot_excess_positions = MagicMock()
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT', 10.0, 20.0, {'type': 'binance_spot_excess'},
            )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(result['allocated_qty'], 4.0)
        self.assertEqual(result['remaining_qty'], 6.0)
        allocated_result = remediator._insert_spot_order.call_args.args[1]
        self.assertEqual(allocated_result['exec_qty'], 4.0)
        remediator._resolve_balanced_spot_excess_positions.assert_not_called()

    def test_overreported_spot_fill_is_clamped_and_forces_fresh_reconciliation(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, _order):
                return {
                    'success': True,
                    'exec_qty': 12.0,
                    'exec_price': 0.14,
                    'exec_amount': 1.68,
                    'fee_amount': 0.12,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._insert_spot_order = MagicMock()

        result = remediator.remediate_binance_spot_desync(
            'TUT', 10.0, 20.0, {'type': 'extra_gate_position'},
        )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(result['spot_result']['exec_qty'], 10.0)
        self.assertAlmostEqual(result['spot_result']['exec_amount'], 1.4)
        self.assertAlmostEqual(result['spot_result']['fee_amount'], 0.1)
        self.assertIn('spot_fill_exceeds_target', result['reason'])

    def test_missing_exec_amount_falls_back_to_price_times_quantity(self):
        remediator = ExchangeDesyncRemediator(
            MagicMock(spot_meta={'TUT': {'step_size': 1}}),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._insert_spot_order = MagicMock()
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='balanced')
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator._allocate_spot_reduction_to_positions(
                order={'base_asset': 'TUT', 'target_qty': 6.0},
                result={'success': True, 'exec_qty': 6.0, 'exec_price': 0.14},
                positions=[{'id': 1, '_spot_excess_qty': 6.0}],
                risk={'type': 'binance_spot_excess'},
                reason='对账兜底',
                now=datetime(2026, 8, 9, 10, 0, 0),
            )

        self.assertEqual(result['allocated_qty'], 6.0)
        allocated_order, allocated_result = remediator._insert_spot_order.call_args.args[:2]
        self.assertAlmostEqual(allocated_order['target_amount'], 0.84)
        self.assertAlmostEqual(allocated_result['exec_amount'], 0.84)

    def test_sub_step_unallocated_fill_gets_asset_level_audit_record(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, _order):
                return {
                    'success': True,
                    'exec_qty': 10.0,
                    'exec_price': 0.14,
                    'fee_amount': 0.1,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(return_value=[{
            'id': 1,
            'base_asset': 'TUT',
            '_spot_excess_qty': 9.5,
        }])
        remediator._insert_spot_order = MagicMock()
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='balanced')
        remediator._resolve_balanced_spot_excess_positions = MagicMock(return_value=1)
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT', 10.0, 20.0, {'type': 'binance_spot_excess'},
            )

        self.assertTrue(result['success'])
        self.assertEqual(result['allocated_qty'], 9.5)
        self.assertEqual(remediator._insert_spot_order.call_count, 2)
        audit_order, audit_result, audit_reason = (
            remediator._insert_spot_order.call_args_list[-1].args[:3]
        )
        self.assertIsNone(audit_order['position_id'])
        self.assertEqual(audit_result['exec_qty'], 0.5)
        self.assertAlmostEqual(audit_result['exec_amount'], 0.07)
        self.assertAlmostEqual(audit_result['fee_amount'], 0.005)
        self.assertIn('本地未分配成交=0.5', audit_reason)

    def test_closed_position_pnl_refresh_failure_does_not_undo_allocation(self):
        remediator = ExchangeDesyncRemediator(
            MagicMock(spot_meta={'TUT': {'step_size': 1}}),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._insert_spot_order = MagicMock()
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='closed')
        remediator._refresh_closed_position_pnl = MagicMock(
            side_effect=RuntimeError('pnl refresh failed'),
        )
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False
        position = {'id': 1, 'base_asset': 'TUT', '_spot_excess_qty': 6.0}

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator._allocate_spot_reduction_to_positions(
                order={'base_asset': 'TUT', 'target_qty': 6.0},
                result={
                    'success': True,
                    'exec_qty': 6.0,
                    'exec_price': 0.14,
                    'exec_amount': 0.84,
                },
                positions=[position],
                risk={'type': 'binance_spot_excess'},
                reason='对账兜底',
                now=datetime(2026, 8, 9, 10, 0, 0),
            )

        self.assertEqual(result['allocated_qty'], 6.0)
        remediator._refresh_closed_position_pnl.assert_called_once_with(position)

    def test_unallocated_fill_audit_failure_returns_fast_retry(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, _order):
                return {
                    'success': True,
                    'exec_qty': 10.0,
                    'exec_price': 0.14,
                    'exec_amount': 1.4,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(return_value=[{
            'id': 1,
            'base_asset': 'TUT',
            '_spot_excess_qty': 9.5,
        }])
        remediator._insert_spot_order = MagicMock(side_effect=[None, RuntimeError('audit down')])
        remediator._apply_allocated_spot_reduction = MagicMock(return_value='balanced')
        remediator._resolve_balanced_spot_excess_positions = MagicMock()
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT', 10.0, 20.0, {'type': 'binance_spot_excess'},
            )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertIn('unallocated_spot_audit_failed', result['reason'])
        remediator._resolve_balanced_spot_excess_positions.assert_not_called()

    def test_direct_spot_fill_persistence_failure_returns_fast_retry(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_qty': order['target_qty'],
                    'exec_price': 0.14,
                    'exec_amount': order['target_qty'] * 0.14,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._insert_spot_order = MagicMock(side_effect=RuntimeError('db down'))

        result = remediator.remediate_binance_spot_desync(
            'TUT', 10.0, 20.0, {'type': 'extra_gate_position'},
        )

        self.assertFalse(result['success'])
        self.assertTrue(result['exchange_success'])
        self.assertTrue(result['retry_needed'])
        self.assertIn('spot_order_persistence_failed', result['reason'])

    def test_allocation_and_fallback_audit_failure_do_not_escape_reconciler(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_qty': order['target_qty'],
                    'exec_price': 0.14,
                    'exec_amount': order['target_qty'] * 0.14,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(return_value=[{
            'id': 1,
            'base_asset': 'TUT',
            '_spot_excess_qty': 10.0,
        }])
        remediator._insert_spot_order = MagicMock(side_effect=[
            RuntimeError('allocation write failed'),
            RuntimeError('fallback audit failed'),
        ])
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator.remediate_binance_spot_desync(
                'TUT', 10.0, 20.0, {'type': 'binance_spot_excess'},
            )

        self.assertFalse(result['success'])
        self.assertTrue(result['exchange_success'])
        self.assertTrue(result['retry_needed'])
        self.assertIn('aggregate_spot_audit_failed', result['reason'])

    def test_external_spot_surplus_clears_broad_balanced_risk_marks(self):
        class FakeExecutor:
            spot_meta = {'TUT': {'step_size': 1}}

            def place_binance_spot_order(self, order):
                return {
                    'success': True,
                    'exec_qty': order['target_qty'],
                    'exec_price': 0.14,
                    'exec_amount': order['target_qty'] * 0.14,
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.14

        remediator = ExchangeDesyncRemediator(
            FakeExecutor(),
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=20.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(return_value=[])
        remediator._insert_spot_order = MagicMock()
        remediator._resolve_balanced_spot_excess_positions = MagicMock(return_value=3)

        result = remediator.remediate_binance_spot_desync(
            'TUT', 10.0, 20.0, {'type': 'binance_spot_excess'},
        )

        self.assertTrue(result['success'])
        remediator._resolve_balanced_spot_excess_positions.assert_called_once_with('TUT')

    @staticmethod
    def _build_low_notional_paired_trim_case(
        *,
        gate_result=None,
        spot_result=None,
        available_qty=14037.0,
        position=None,
    ):
        calls = []

        class FakeExecutor:
            contract_meta = {'AI': {'quanto_multiplier': 1.0}}
            spot_meta = {'AI': {'min_notional': 5.0, 'step_size': 1.0}}

            def place_gate_futures_order(self, order):
                calls.append('gate')
                if gate_result is not None:
                    return gate_result
                return {
                    'success': True,
                    'exec_qty': order['target_qty'],
                    'exec_price': order['protective_price'],
                    'exec_amount': order['target_qty'] * order['protective_price'],
                    'exchange_order_id': 'gate-fok-1',
                }

            def place_binance_spot_close_with_retry(self, order):
                calls.append('binance')
                if spot_result is not None:
                    return spot_result
                return {
                    'success': True,
                    'exec_qty': order['target_qty'],
                    'exec_price': 0.0184,
                    'exec_amount': order['target_qty'] * 0.0184,
                    'exchange_order_id': 'binance-sell-1',
                }

            def _get_binance_usdt_price(self, _asset):
                return 0.0184

        explained = position or {
            'id': 801,
            'base_asset': 'AI',
            'spot_symbol': 'AIUSDT',
            'future_contract': 'AI_USDT',
            'spot_open_qty': 1127.0,
            'spot_open_price': 0.0180,
            'future_open_qty': 1000.0,
            'future_open_price': 0.0182,
            'future_open_contracts': 1000.0,
            'exchange_risk_status': 'desynced',
            'exchange_risk_type': 'binance_spot_excess',
            '_spot_excess_qty': 127.0,
        }
        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(
                enabled=True,
                low_notional_buffer_ratio=1.2,
                low_notional_fok_slippage_bps=100.0,
                low_notional_retry_cooldown_sec=300.0,
            ),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=available_qty)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(
            return_value=[explained],
        )
        remediator._persist_low_notional_paired_trim = MagicMock(return_value={
            'position_count': 1,
            'closed_position_count': 0,
            'spot_qty': 327.0,
            'future_qty': 200.0,
        })
        return remediator, executor, explained, calls

    def test_low_notional_binance_excess_uses_gate_fok_then_combined_spot_sale(self):
        remediator, executor, _position, calls = self._build_low_notional_paired_trim_case()

        result = remediator.remediate_binance_spot_desync(
            'AI',
            local_qty=13910.0,
            exchange_qty=14037.0,
            risk={
                'type': 'binance_spot_excess',
                'contract': 'AI_USDT',
                'mark_price': 0.0185,
            },
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['action'], 'paired_trim_low_notional_binance_excess')
        self.assertEqual(calls, ['gate', 'binance'])
        self.assertEqual(result['paired_qty'], 200.0)
        self.assertEqual(result['spot_exec_qty'], 327.0)
        persisted = remediator._persist_low_notional_paired_trim.call_args.kwargs
        self.assertEqual(persisted['excess_qty'], 127.0)
        self.assertEqual(persisted['paired_contracts'], 200)
        self.assertEqual(persisted['future_order']['time_in_force'], 'fok')
        self.assertEqual(persisted['future_order']['target_contracts'], 200)
        self.assertAlmostEqual(persisted['future_order']['protective_price'], 0.018685)
        self.assertEqual(persisted['spot_order']['target_qty'], 327.0)

    def test_low_notional_paired_trim_honors_non_unit_contract_multiplier(self):
        class FakeExecutor:
            contract_meta = {'BICO': {'quanto_multiplier': 0.1}}
            spot_meta = {'BICO': {'min_notional': 5.0, 'step_size': 0.1}}

            def __init__(self):
                self.gate_order = None
                self.spot_order = None

            def _get_binance_usdt_price(self, _asset):
                return 0.04

            def place_gate_futures_order(self, order):
                self.gate_order = order
                return {
                    'success': True,
                    'exec_qty': order['target_qty'],
                    'exec_price': order['protective_price'],
                    'exec_amount': order['target_qty'] * order['protective_price'],
                }

            def place_binance_spot_close_with_retry(self, order):
                self.spot_order = order
                return {
                    'success': True,
                    'exec_qty': order['target_qty'],
                    'exec_price': 0.04,
                    'exec_amount': order['target_qty'] * 0.04,
                }

        executor = FakeExecutor()
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=210.0)
        remediator._load_spot_excess_positions_to_remediate = MagicMock(return_value=[{
            'id': 802,
            'base_asset': 'BICO',
            'spot_open_qty': 210.0,
            'future_open_qty': 200.0,
            'future_open_contracts': 2000.0,
            '_spot_excess_qty': 10.0,
        }])
        remediator._persist_low_notional_paired_trim = MagicMock(return_value={})

        result = remediator.remediate_binance_spot_desync(
            'BICO', 200.0, 210.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.041},
        )

        self.assertTrue(result['success'])
        self.assertEqual(executor.gate_order['target_contracts'], 1400)
        self.assertAlmostEqual(executor.gate_order['target_qty'], 140.0)
        self.assertAlmostEqual(executor.spot_order['target_qty'], 150.0)
        persisted = remediator._persist_low_notional_paired_trim.call_args.kwargs
        self.assertEqual(persisted['paired_contracts'], 1400)
        self.assertEqual(persisted['multiplier'], 0.1)

    def test_low_notional_gate_fok_failure_does_not_touch_binance_and_cools_down(self):
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case(
            gate_result={'success': False, 'reason': 'FOK未成交'},
        )
        risk = {'type': 'binance_spot_excess', 'mark_price': 0.0185}

        first = remediator.remediate_binance_spot_desync('AI', 13910.0, 14037.0, risk)
        second = remediator.remediate_binance_spot_desync('AI', 13910.0, 14037.0, risk)

        self.assertFalse(first['success'])
        self.assertTrue(first['attempted'])
        self.assertFalse(first['retry_needed'])
        self.assertEqual(calls, ['gate'])
        self.assertFalse(second['attempted'])
        self.assertEqual(second['reason'], 'low_notional_retry_cooldown')

    def test_low_notional_gate_unexpected_partial_fill_never_sells_spot(self):
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case(
            gate_result={
                'success': True,
                'exec_qty': 199.0,
                'exec_price': 0.018685,
                'exec_amount': 3.718315,
            },
        )

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(calls, ['gate'])
        self.assertIn('fok_qty_mismatch', result['reason'])

    def test_low_notional_gate_fill_check_does_not_use_large_spot_step_as_tolerance(self):
        remediator, executor, _position, calls = self._build_low_notional_paired_trim_case(
            gate_result={
                'success': True,
                'exec_qty': 272.0,
                'exec_price': 0.018685,
                'exec_amount': 5.083,
            },
        )
        executor.spot_meta['AI']['step_size'] = 100.0

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(calls, ['gate'])
        self.assertIn('272!=273', result['reason'])

    def test_low_notional_spot_failure_after_gate_fill_requests_fresh_reconciliation(self):
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case(
            spot_result={'success': False, 'exec_qty': 0.0, 'reason': 'Binance rejected'},
        )

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(calls, ['gate', 'binance'])
        self.assertEqual(result['reason'], 'Binance rejected')

    def test_low_notional_unexplained_excess_never_places_either_leg(self):
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case()
        remediator._load_spot_excess_positions_to_remediate.return_value = []

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertFalse(result['attempted'])
        self.assertEqual(result['reason'], 'low_notional_excess_not_fully_explained')
        self.assertEqual(calls, [])

    def test_low_notional_non_spot_excess_risk_never_places_either_leg(self):
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case()

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'extra_gate_position', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'low_notional_excess_not_pairable')
        self.assertEqual(calls, [])

    def test_low_notional_insufficient_matched_gate_position_never_places_order(self):
        position = {
            'id': 801,
            'base_asset': 'AI',
            'spot_open_qty': 227.0,
            'spot_open_price': 0.0180,
            'future_open_qty': 100.0,
            'future_open_contracts': 100.0,
            '_spot_excess_qty': 127.0,
        }
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case(
            position=position,
        )

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertEqual(
            result['reason'],
            'insufficient_matched_gate_position_for_low_notional_repair',
        )
        self.assertEqual(calls, [])

    def test_low_notional_multiplier_mismatch_never_places_either_leg(self):
        position = {
            'id': 801,
            'base_asset': 'AI',
            'spot_open_qty': 1127.0,
            'future_open_qty': 1000.0,
            'future_open_contracts': 999.0,
            '_spot_excess_qty': 127.0,
        }
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case(
            position=position,
        )

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'position_contract_multiplier_mismatch')
        self.assertEqual(calls, [])

    def test_low_notional_partially_selected_position_excess_never_places_either_leg(self):
        position = {
            'id': 801,
            'base_asset': 'AI',
            'spot_open_qty': 1200.0,
            'future_open_qty': 1000.0,
            'future_open_contracts': 1000.0,
            '_spot_excess_qty': 127.0,
        }
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case(
            position=position,
        )

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'position_spot_excess_not_fully_selected')
        self.assertEqual(calls, [])

    def test_low_notional_insufficient_binance_balance_never_places_gate_order(self):
        remediator, _executor, _position, calls = self._build_low_notional_paired_trim_case(
            available_qty=300.0,
        )

        result = remediator.remediate_binance_spot_desync(
            'AI', 13910.0, 14037.0,
            {'type': 'binance_spot_excess', 'mark_price': 0.0185},
        )

        self.assertFalse(result['success'])
        self.assertEqual(
            result['reason'],
            'spot_available_qty_insufficient_for_low_notional_repair',
        )
        self.assertEqual(calls, [])

    def test_low_notional_paired_trim_persistence_reduces_both_local_legs_equally(self):
        remediator, _executor, position, _calls = self._build_low_notional_paired_trim_case()
        remediator._persist_low_notional_paired_trim = (
            ExchangeDesyncRemediator._persist_low_notional_paired_trim.__get__(remediator)
        )
        cursor = MagicMock()
        cursor.rowcount = 1
        connection = MagicMock()
        connection.cursor.return_value = cursor
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False

        with patch(
            'calc.exchange_desync_remediator.db_manager.get_connection',
            return_value=context,
        ):
            result = remediator._persist_low_notional_paired_trim(
                positions=[position],
                excess_qty=127.0,
                paired_contracts=200,
                multiplier=1.0,
                future_order={
                    'order_uuid': 'repair-1',
                    'base_asset': 'AI',
                    'market_type': 'future',
                    'future_contract': 'AI_USDT',
                },
                future_result={
                    'exec_qty': 200.0,
                    'exec_price': 0.018685,
                    'exec_amount': 3.737,
                },
                spot_order={
                    'order_uuid': 'repair-1',
                    'base_asset': 'AI',
                    'market_type': 'spot',
                    'spot_symbol': 'AIUSDT',
                },
                spot_result={
                    'exec_qty': 327.0,
                    'exec_price': 0.0184,
                    'exec_amount': 6.0168,
                },
                reason='对账兜底小额净敞口合并减仓',
                now=datetime(2026, 8, 29, 12, 0, 0),
            )

        self.assertEqual(result['spot_qty'], 327.0)
        self.assertEqual(result['future_qty'], 200.0)
        self.assertEqual(position['spot_open_qty'], 800.0)
        self.assertEqual(position['future_open_qty'], 800.0)
        self.assertEqual(position['future_open_contracts'], 800.0)
        self.assertEqual(position['exchange_risk_status'], 'resolved')
        sql_calls = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertEqual(sum('INSERT INTO mi_trade_order' in sql for sql in sql_calls), 2)
        self.assertEqual(sum('UPDATE mi_trade_position' in sql for sql in sql_calls), 1)

    def test_insufficient_available_spot_requests_fast_retry_without_order(self):
        executor = MagicMock()
        executor.spot_meta = {'TUT': {'step_size': 1}}
        remediator = ExchangeDesyncRemediator(
            executor,
            ExchangeDesyncRemediationConfig(enabled=True),
        )
        remediator._load_binance_available_qty = MagicMock(return_value=9.0)

        result = remediator.remediate_binance_spot_desync(
            'TUT', 10.0, 20.0, {'type': 'binance_spot_excess'},
        )

        self.assertFalse(result['success'])
        self.assertTrue(result['retry_needed'])
        self.assertEqual(result['reason'], 'spot_available_qty_insufficient')
        executor.place_binance_spot_order.assert_not_called()

    def test_fast_confirmation_detects_nested_available_qty_shortage(self):
        self.assertTrue(Reconciler._remediation_needs_fast_retry({
            'results': [{
                'paired_binance_spot_result': {
                    'attempted': True,
                    'success': False,
                    'reason': 'spot_available_qty_insufficient',
                },
            }],
        }))

    def test_fast_confirmation_retries_reduction_lock_collision(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_fast_confirm_delay_sec=3.0),
        )
        first = {
            'success': True,
            'snapshot_at': '2026-08-09 10:00:00',
            'confirmation_pending_count': 0,
            'remediation_retry_pending_count': 1,
        }
        second = {
            'success': True,
            'snapshot_at': '2026-08-09 10:00:03',
            'confirmation_pending_count': 0,
            'remediation_retry_pending_count': 0,
            'remediation_success_count': 1,
        }

        with (
            patch.object(reconciler, 'run_once', side_effect=[first, second]) as run_once,
            patch('calc.reconciliation.time.sleep') as sleep,
        ):
            result = reconciler.run_with_fast_confirmation()

        self.assertEqual(run_once.call_count, 2)
        sleep.assert_called_once_with(3.0)
        self.assertEqual(result['fast_confirmation_rounds'], 1)
        self.assertEqual(result['remediation_success_count'], 1)

    def test_fast_confirmation_detects_nested_asset_reduction_inflight(self):
        self.assertTrue(Reconciler._remediation_needs_fast_retry({
            'results': [{
                'paired_binance_spot_result': {
                    'attempted': False,
                    'reason': 'asset_reduction_inflight',
                },
            }],
        }))

    def test_fast_confirmation_is_bounded_to_two_follow_up_rounds(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(auto_remediate_fast_confirm_delay_sec=1.0),
        )
        pending = {
            'success': True,
            'snapshot_at': '2026-08-09 10:00:00',
            'confirmation_pending_count': 0,
            'remediation_retry_pending_count': 1,
        }

        with (
            patch.object(reconciler, 'run_once', side_effect=[
                dict(pending), dict(pending), dict(pending),
            ]) as run_once,
            patch('calc.reconciliation.time.sleep') as sleep,
        ):
            result = reconciler.run_with_fast_confirmation()

        self.assertEqual(run_once.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result['fast_confirmation_rounds'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
