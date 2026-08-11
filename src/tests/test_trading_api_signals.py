import asyncio
import json
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from api import trading_api
from api.trading_api import (
    _append_unique_notification,
    _aggregate_capital_latest_account_rows,
    _build_gate_cross_minimum_summary,
    _calculate_capital_annualized_return,
    _capital_history_interval,
    _capital_history_select_columns,
    _build_forward_signal_filters,
    _filter_capital_transfer_transient_rows,
    _format_reconciliation_notification,
    _format_dust_cleanup_message,
    _reconciliation_latest_sql,
    _should_emit_reconciliation_notification,
    get_capital_annualized_return,
    get_capital_history,
    get_gate_cross_risk_summary,
    register_capital_strategy_pnl_provider,
    run_capital_snapshot_now,
    cleanup_reconciliation_dust,
)
from fastapi import HTTPException


class ForwardSignalFilterTests(unittest.TestCase):
    def test_today_time_range_uses_calendar_day_filter(self):
        where_sql, params = _build_forward_signal_filters(
            status=None,
            exit_reason=None,
            base_asset=None,
            time_range='today',
            days=90,
        )

        self.assertIn('signal_time >= CURDATE()', where_sql)
        self.assertIn('signal_time < DATE_ADD(CURDATE(), INTERVAL 1 DAY)', where_sql)
        self.assertEqual(params, [])

    def test_non_today_time_range_uses_backend_recent_days_filter(self):
        where_sql, params = _build_forward_signal_filters(
            status='opened',
            exit_reason='旁路',
            base_asset='AI',
            time_range='7',
            days=7,
            prefix='s.',
        )

        self.assertIn('s.signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)', where_sql)
        self.assertIn('s.status = %s', where_sql)
        self.assertIn('s.exit_reason LIKE %s', where_sql)
        self.assertIn('s.base_asset LIKE %s', where_sql)
        self.assertEqual(params, [7, 'opened', '%旁路%', '%AI%'])


class ManualCapitalSnapshotTests(unittest.TestCase):
    def tearDown(self):
        register_capital_strategy_pnl_provider(None)
        trading_api._capital_running = False

    def test_manual_snapshot_uses_registered_realtime_strategy_pnl(self):
        strategy_pnl = {
            'binance_spot_floating_pnl': 123.45,
            'gate_future_floating_pnl': -120.0,
            'floating_pnl': 3.45,
            'position_count': 2,
        }
        captured = {}

        class FakeSnapshotter:
            def run_once(self, value):
                captured['strategy_pnl'] = value
                return {'success': True, 'snapshot_at': '2026-07-25 12:00:00'}

        register_capital_strategy_pnl_provider(lambda: strategy_pnl)
        with patch(
            'api.trading_api.build_default_capital_snapshotter',
            return_value=FakeSnapshotter(),
        ):
            result = asyncio.run(run_capital_snapshot_now())

        self.assertTrue(result['success'])
        self.assertIs(captured['strategy_pnl'], strategy_pnl)

    def test_manual_snapshot_without_realtime_provider_does_not_write(self):
        register_capital_strategy_pnl_provider(None)

        with patch('api.trading_api.build_default_capital_snapshotter') as builder:
            result = asyncio.run(run_capital_snapshot_now())

        self.assertFalse(result['success'])
        self.assertIn('本次未写入', result['message'])
        builder.assert_not_called()


class ManualDustCleanupTests(unittest.TestCase):
    def tearDown(self):
        trading_api._recon_running = False

    def test_cleanup_runs_exchange_refresh_then_reconciliation(self):
        reconciler = MagicMock()
        reconciler.cleanup_post_close_dust.return_value = {
            'success': True,
            'attempted': True,
            'message': 'BICO 小额残余已清理',
        }
        reconciler.run_once.return_value = {
            'success': True,
            'mismatch_count': 0,
        }
        with patch('api.trading_api.config.get_trade_mode', return_value='real'), \
                patch('api.trading_api.config.get_bool', return_value=True), \
                patch('api.trading_api.build_default_reconciler', return_value=reconciler):
            result = asyncio.run(cleanup_reconciliation_dust())

        self.assertTrue(result['success'])
        self.assertEqual(result['message'], 'BICO 小额残余已清理')
        self.assertEqual(result['reconciliation']['mismatch_count'], 0)
        reconciler.cleanup_post_close_dust.assert_called_once_with()
        reconciler.run_once.assert_called_once_with()
        self.assertFalse(trading_api._recon_running)

    def test_cleanup_cooldown_message_includes_remaining_seconds(self):
        message = _format_dust_cleanup_message({
            'success': False,
            'attempted': False,
            'reason': 'binance_dust_conversion_cooldown',
            'cooldown_remaining_sec': 110.1,
        })

        self.assertEqual(
            message,
            '小额残余清理失败: Binance 小额兑换冷却中，剩余 111 秒',
        )


class CapitalLatestAccountAggregationTests(unittest.TestCase):
    def test_total_account_breakdown_sums_exchange_account_values(self):
        rows = [
            {
                'exchange': 'binance',
                'equity_usdt': Decimal('10854.8283015280'),
                'available_usdt': Decimal('1852.6505021100'),
                'locked_usdt': Decimal('0'),
                'position_value_usdt': Decimal('9002.1777994180'),
                'margin_used_usdt': Decimal('0'),
                'unrealized_pnl_usdt': Decimal('54.0580'),
                'account_balance_usdt': Decimal('10800.7703015280'),
                'account_unrealized_pnl_usdt': Decimal('54.0580'),
            },
            {
                'exchange': 'gate',
                'equity_usdt': Decimal('4224.7719069583'),
                'available_usdt': Decimal('3788.1067827441'),
                'locked_usdt': Decimal('0'),
                'position_value_usdt': Decimal('432.4346142142'),
                'margin_used_usdt': Decimal('432.4346142142'),
                'unrealized_pnl_usdt': Decimal('-73.4507'),
                'account_balance_usdt': Decimal('4283.342997206459'),
                'account_unrealized_pnl_usdt': Decimal('-58.57109024817'),
            },
            {
                'exchange': 'total',
                'equity_usdt': Decimal('15079.6002084863'),
                'available_usdt': Decimal('5640.7572848541'),
                'locked_usdt': Decimal('0'),
                'position_value_usdt': Decimal('9434.6124136321'),
                'margin_used_usdt': Decimal('432.4346142142'),
                'unrealized_pnl_usdt': Decimal('-19.3930'),
                'account_balance_usdt': Decimal('15098.9932084863'),
                'account_unrealized_pnl_usdt': Decimal('-19.3930'),
            },
        ]

        result = _aggregate_capital_latest_account_rows(rows)
        by_exchange = {row['exchange']: row for row in result}
        total = by_exchange['total']

        self.assertAlmostEqual(total['equity_usdt'], 15079.6002084863)
        self.assertAlmostEqual(total['available_usdt'], 5640.7572848541)
        self.assertAlmostEqual(total['position_value_usdt'], 9434.6124136321)
        self.assertAlmostEqual(total['account_balance_usdt'], 15084.113298734459)
        self.assertAlmostEqual(total['account_unrealized_pnl_usdt'], -4.51309024817)
        self.assertAlmostEqual(
            total['account_balance_usdt'] + total['account_unrealized_pnl_usdt'],
            total['equity_usdt'],
        )
        self.assertAlmostEqual(total['unrealized_pnl_usdt'], -19.3930)


class ReconciliationNotificationTests(unittest.TestCase):
    def test_latest_mismatch_is_emitted_without_confirmation(self):
        snapshot_at = datetime(2026, 6, 19, 11, 52, 9)
        row = {'snapshot_at': snapshot_at, 'previous_is_match': 1}

        self.assertTrue(_should_emit_reconciliation_notification(row, snapshot_at))

    def test_historical_one_off_mismatch_is_suppressed(self):
        row = {
            'snapshot_at': datetime(2026, 6, 19, 11, 52, 9),
            'previous_is_match': 1,
        }
        latest_snapshot_at = datetime(2026, 6, 19, 11, 53, 9)

        self.assertFalse(_should_emit_reconciliation_notification(row, latest_snapshot_at))

    def test_historical_consecutive_mismatch_is_emitted(self):
        row = {
            'snapshot_at': datetime(2026, 6, 19, 11, 52, 9),
            'previous_is_match': 0,
        }
        latest_snapshot_at = datetime(2026, 6, 19, 11, 53, 9)

        self.assertTrue(_should_emit_reconciliation_notification(row, latest_snapshot_at))

    def test_append_unique_notification_keeps_first_dedup_key(self):
        items = []
        seen = set()

        _append_unique_notification(items, seen, {'dedup_key': 'reconciliation:ASR', 'event_at': '11:52'})
        _append_unique_notification(items, seen, {'dedup_key': 'reconciliation:ASR', 'event_at': '11:51'})

        self.assertEqual(items, [{'dedup_key': 'reconciliation:ASR', 'event_at': '11:52'}])

    def test_reconciliation_error_notification_uses_fetch_failure_copy(self):
        row = {
            'snapshot_at': datetime(2026, 7, 16, 18, 20, 39),
            'exchange': 'gate',
            'base_asset': '__ERROR__',
            'dimension': 'error',
            'local_value': None,
            'exchange_value': None,
            'diff_value': None,
            'detail': {'error_msg': 'Read timed out. (read timeout=10)'},
        }

        item = _format_reconciliation_notification(row, 'reconciliation:gate:__ERROR__')

        self.assertEqual(item['title'], '持仓对账拉取失败: Gate')
        self.assertEqual(item['message'], 'Gate 对账接口错误: Read timed out. (read timeout=10)')
        self.assertEqual(item['status'], 'error')

    def test_reconciliation_position_mismatch_notification_copy_is_unchanged(self):
        row = {
            'snapshot_at': datetime(2026, 7, 16, 18, 20, 39),
            'exchange': 'binance',
            'base_asset': 'BEL',
            'dimension': 'position',
            'local_value': 100,
            'exchange_value': 95,
            'diff_value': -5,
            'detail': {},
        }

        item = _format_reconciliation_notification(row, 'reconciliation:binance:BEL')

        self.assertEqual(item['title'], '持仓对账不一致: BEL')
        self.assertEqual(item['message'], 'binance position local=100 exchange=95 diff=-5')
        self.assertEqual(item['status'], 'mismatch')


class ReconciliationLatestQueryTests(unittest.TestCase):
    def test_latest_query_attaches_gate_quanto_multiplier(self):
        sql = _reconciliation_latest_sql(" AND NOT (s.exchange = 'binance' AND s.base_asset IN (%s))")

        self.assertIn('c.quanto_multiplier', sql)
        self.assertIn('mi_gate_future_contracts c', sql)
        self.assertIn('UPPER(TRIM(c.base_asset)) COLLATE utf8mb4_unicode_ci', sql)
        self.assertIn('UPPER(TRIM(s.base_asset)) COLLATE utf8mb4_unicode_ci', sql)
        self.assertIn('s.snapshot_at = (SELECT MAX(snapshot_at) FROM mi_recon_snapshot)', sql)


class CapitalHistoryFilterTests(unittest.TestCase):
    def test_transfer_intermediate_total_equity_drop_is_hidden_after_recovery(self):
        rows = [
            {'snapshot_at': '2026-07-04 11:09:50', 'exchange': 'total', 'equity_usdt': 11504.23},
            {'snapshot_at': '2026-07-04 11:19:05', 'exchange': 'total', 'equity_usdt': 10306.14},
            {'snapshot_at': '2026-07-04 11:24:11', 'exchange': 'total', 'equity_usdt': 11506.38},
        ]

        filtered = _filter_capital_transfer_transient_rows(rows)

        self.assertEqual([row['snapshot_at'] for row in filtered], [
            '2026-07-04 11:09:50',
            '2026-07-04 11:24:11',
        ])

    def test_sustained_total_equity_change_is_kept(self):
        rows = [
            {'snapshot_at': '2026-07-04 11:09:50', 'exchange': 'total', 'equity_usdt': 11504.23},
            {'snapshot_at': '2026-07-04 11:19:05', 'exchange': 'total', 'equity_usdt': 10306.14},
            {'snapshot_at': '2026-07-04 11:24:11', 'exchange': 'total', 'equity_usdt': 10310.38},
        ]

        filtered = _filter_capital_transfer_transient_rows(rows)

        self.assertEqual(filtered, rows)


class CapitalHistoryQueryTests(unittest.TestCase):
    def test_interval_is_selected_from_requested_window(self):
        self.assertEqual(_capital_history_interval(6, 7), ('1m', 60))
        self.assertEqual(_capital_history_interval(None, 1), ('1m', 60))
        self.assertEqual(_capital_history_interval(None, 7), ('10m', 600))
        self.assertEqual(_capital_history_interval(None, 30), ('1h', 3600))
        self.assertEqual(_capital_history_interval(None, 90), ('1h', 3600))

    def test_metric_columns_only_include_fields_used_by_chart(self):
        equity_columns = _capital_history_select_columns('equity_usdt')
        self.assertIn('s.equity_usdt', equity_columns)
        self.assertNotIn('unrealized_pnl_usdt', equity_columns)
        self.assertNotIn('gate_cross_risk', equity_columns)

        realized_columns = _capital_history_select_columns('realized_breakdown')
        self.assertIn('s.realized_pnl_usdt', realized_columns)
        self.assertIn('s.funding_pnl_usdt', realized_columns)
        self.assertIn('s.total_pnl_usdt', realized_columns)
        self.assertNotIn('bnb_fee_asset', realized_columns)

    def test_unknown_metric_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            _capital_history_select_columns('everything')

        self.assertEqual(raised.exception.status_code, 400)

    def test_history_endpoint_uses_auto_interval_and_narrow_query(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{
            'snapshot_at': datetime(2026, 7, 25, 12, 0, 0),
            'exchange': 'total',
            'equity_usdt': Decimal('15000.25'),
        }]
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('api.trading_api.db_manager.get_cursor', return_value=context):
            result = asyncio.run(get_capital_history(
                days=7,
                hours=None,
                exchange='total',
                metric='equity_usdt',
            ))

        self.assertEqual(result['interval'], '10m')
        self.assertEqual(result['metric'], 'equity_usdt')
        self.assertEqual(result['rows'][0]['equity_usdt'], 15000.25)
        sql, params = cursor.execute.call_args.args
        self.assertIn('FORCE INDEX (idx_exchange_snapshot)', sql)
        self.assertNotIn('bnb_fee_asset', sql)
        self.assertNotIn('account_latency_ms', sql)
        self.assertEqual(params, [600, 600, 7, 'total'])

    def test_daily_return_endpoint_uses_calendar_day_aggregation(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{
            'snapshot_at': '2026-08-05 00:00:00',
            'exchange': 'total',
            'equity_usdt': Decimal('15000.00'),
            'daily_realized_pnl_usdt': Decimal('1.23'),
            'daily_return_pct': Decimal('0.0082'),
        }]
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('api.trading_api.db_manager.get_cursor', return_value=context):
            result = asyncio.run(get_capital_history(
                days=30,
                hours=None,
                exchange='total',
                metric='daily_return',
            ))

        self.assertEqual(result['interval'], '1d')
        self.assertEqual(result['metric'], 'daily_return')
        self.assertEqual(result['rows'][0]['daily_realized_pnl_usdt'], 1.23)
        sql, params = cursor.execute.call_args.args
        self.assertIn('DATE(snapshot_at) AS summary_date', sql)
        self.assertIn('MIN(id) AS first_id', sql)
        self.assertIn('MAX(id) AS last_id', sql)
        self.assertNotIn('FROM_UNIXTIME(FLOOR', sql)
        self.assertEqual(params, [30, 'total'])

    def test_gate_risk_metric_forces_gate_exchange(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('api.trading_api.db_manager.get_cursor', return_value=context):
            result = asyncio.run(get_capital_history(
                days=30,
                hours=None,
                exchange='total',
                metric='gate_cross_risk',
            ))

        self.assertEqual(result['interval'], '1h')
        sql, params = cursor.execute.call_args.args
        self.assertIn('gate_cross_mmr_pct', sql)
        self.assertNotIn('gate_cross_nearest_liq_distance_bps', sql)
        self.assertEqual(params, [3600, 3600, 30, 'gate'])


class CapitalAnnualizedReturnTests(unittest.TestCase):
    @staticmethod
    def _daily_rows(
        count: int,
        daily_pnl: Decimal = Decimal('10'),
        daily_realized_pnl: Decimal = Decimal('5'),
    ):
        return [
            {
                'summary_date': date(2026, 7, day + 1),
                'equity_sum_usdt': Decimal('100000'),
                'sample_count': 100,
                'first_gross_pnl_usdt': Decimal('100') + daily_pnl * day,
                'last_gross_pnl_usdt': Decimal('100') + daily_pnl * (day + 1),
                'first_realized_pnl_usdt': Decimal('50') + daily_realized_pnl * day,
                'last_realized_pnl_usdt': Decimal('50') + daily_realized_pnl * (day + 1),
            }
            for day in range(count)
        ]

    def test_compounds_daily_return_and_annualizes_complete_period(self):
        result = _calculate_capital_annualized_return(self._daily_rows(7), 7)

        expected_period = ((1.01 ** 7) - 1) * 100
        expected_annualized = ((1.01 ** 365) - 1) * 100
        expected_realized_period = ((1.005 ** 7) - 1) * 100
        expected_realized_annualized = ((1.005 ** 365) - 1) * 100
        self.assertTrue(result['sufficient_data'])
        self.assertEqual(result['available_days'], 7)
        self.assertAlmostEqual(result['period_return_pct'], expected_period)
        self.assertAlmostEqual(result['annualized_return_pct'], expected_annualized)
        self.assertAlmostEqual(result['period_pnl_usdt'], 70)
        self.assertTrue(result['realized_sufficient_data'])
        self.assertEqual(result['realized_available_days'], 7)
        self.assertAlmostEqual(result['realized_period_return_pct'], expected_realized_period)
        self.assertAlmostEqual(result['realized_annualized_return_pct'], expected_realized_annualized)
        self.assertAlmostEqual(result['realized_period_pnl_usdt'], 35)
        self.assertAlmostEqual(result['average_equity_usdt'], 1000)
        self.assertEqual(result['window_end_policy'], 'previous_calendar_day')

    def test_incomplete_period_reports_coverage_without_annualizing(self):
        result = _calculate_capital_annualized_return(self._daily_rows(15), 30)

        self.assertFalse(result['sufficient_data'])
        self.assertEqual(result['available_days'], 15)
        self.assertIsNone(result['annualized_return_pct'])
        self.assertIsNotNone(result['period_return_pct'])
        self.assertFalse(result['realized_sufficient_data'])
        self.assertEqual(result['realized_available_days'], 15)
        self.assertIsNone(result['realized_annualized_return_pct'])
        self.assertIsNotNone(result['realized_period_return_pct'])

    def test_realized_metric_is_optional_for_legacy_rows(self):
        rows = self._daily_rows(7)
        for row in rows:
            row.pop('first_realized_pnl_usdt')
            row.pop('last_realized_pnl_usdt')

        result = _calculate_capital_annualized_return(rows, 7)

        self.assertTrue(result['sufficient_data'])
        self.assertIsNotNone(result['annualized_return_pct'])
        self.assertFalse(result['realized_data_available'])
        self.assertFalse(result['realized_sufficient_data'])
        self.assertIsNone(result['realized_annualized_return_pct'])

    def test_endpoint_rejects_unknown_period(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(get_capital_annualized_return(days=14))

        self.assertEqual(raised.exception.status_code, 400)

    def test_endpoint_accepts_short_annualized_periods(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = self._daily_rows(3)
        cursor.fetchone.return_value = None
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('api.trading_api.db_manager.get_cursor', return_value=context):
            one_day = asyncio.run(get_capital_annualized_return(days=1))
            three_day = asyncio.run(get_capital_annualized_return(days=3))

        self.assertEqual(one_day['period_days'], 1)
        self.assertEqual(three_day['period_days'], 3)

    def test_endpoint_loads_requested_daily_window(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = self._daily_rows(7)
        cursor.fetchone.return_value = {
            'first_snapshot_at': datetime(2026, 7, 8, 0, 0, 1),
            'last_snapshot_at': datetime(2026, 7, 8, 12, 0, 0),
            'first_equity_usdt': Decimal('1000'),
            'first_total_pnl_usdt': Decimal('10'),
            'last_total_pnl_usdt': Decimal('12.5'),
        }
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('api.trading_api.db_manager.get_cursor', return_value=context):
            result = asyncio.run(get_capital_annualized_return(days=7))

        self.assertTrue(result['sufficient_data'])
        self.assertAlmostEqual(result['today_realized_pnl_usdt'], 2.5)
        self.assertAlmostEqual(result['today_return_pct'], 0.25)
        sql, params = cursor.execute.call_args_list[0].args
        self.assertIn('mi_capital_daily_summary', sql)
        self.assertIn('mi_capital_snapshot', sql)
        self.assertIn('d.summary_date < CURDATE()', sql)
        today_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn('snapshot_at >= CURDATE()', today_sql)
        self.assertEqual(params, (7,))


class GateCrossRiskSummaryTests(unittest.TestCase):
    def test_legacy_snapshot_rebuilds_primary_risk_from_pressure(self):
        summary = _build_gate_cross_minimum_summary({
            'snapshot_at': datetime(2026, 7, 24, 9, 26, 54),
            'gate_cross_mmr_pct': '554.68',
            'detail': json.dumps({
                'gate_cross_risk': {
                    'top_risks': [
                        {
                            'contract': 'BANK_USDT',
                            'maintenance_margin_usdt': 297.18,
                            'unrealized_pnl_usdt': -1418.75,
                            'liq_distance_bps': 6235.74,
                        },
                        {
                            'contract': 'AI_USDT',
                            'maintenance_margin_usdt': 30,
                            'unrealized_pnl_usdt': -20,
                            'liq_distance_bps': 500,
                        },
                    ],
                },
            }),
        })

        self.assertEqual(summary['account_mmr_pct'], 554.68)
        self.assertEqual(summary['snapshot_at'], '2026-07-24 09:26:54')
        self.assertEqual(summary['primary_risk_contract'], 'BANK_USDT')
        self.assertEqual(summary['primary_risk_asset'], 'BANK')
        self.assertAlmostEqual(summary['primary_risk_pressure_usdt'], 1715.93)
        self.assertEqual(summary['attribution'], 'legacy_top_risks')

    def test_new_snapshot_prefers_explicit_primary_risk(self):
        summary = _build_gate_cross_minimum_summary({
            'snapshot_at': '2026-07-24 10:00:00',
            'gate_cross_mmr_pct': 480,
            'detail': {
                'gate_cross_risk': {
                    'primary_risk': {
                        'contract': 'AI_USDT',
                        'risk_pressure_usdt': 200,
                        'maintenance_margin_usdt': 80,
                        'unrealized_pnl_usdt': -120,
                    },
                    'top_risks': [{
                        'contract': 'BANK_USDT',
                        'maintenance_margin_usdt': 500,
                        'unrealized_pnl_usdt': -500,
                    }],
                },
            },
        })

        self.assertEqual(summary['primary_risk_asset'], 'AI')
        self.assertEqual(summary['maintenance_margin_usdt'], 80.0)
        self.assertEqual(summary['unrealized_pnl_usdt'], -120.0)
        self.assertEqual(summary['attribution'], 'full_snapshot')

    def test_summary_endpoint_returns_empty_minimum_when_no_valid_snapshot(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch(
            'api.trading_api.db_manager.get_cursor',
            return_value=context,
        ):
            result = asyncio.run(get_gate_cross_risk_summary(days=7))

        self.assertEqual(result, {'period_days': 7, 'minimum': None})
        sql, params = cursor.execute.call_args.args
        self.assertIn("health_status", sql)
        self.assertIn("position_count", sql)
        self.assertEqual(params, [7])


if __name__ == '__main__':
    unittest.main()
