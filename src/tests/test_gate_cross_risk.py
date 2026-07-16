# coding: utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.gate_cross_risk import (
    GateCrossRiskMonitor,
    GateCrossRiskThresholds,
    build_gate_cross_risk,
    gate_account_metrics,
    gate_cross_risk_health,
)


class FakeRiskExecutor:
    def __init__(self, account, positions):
        self.account = account
        self.positions = positions
        self.account_error = None
        self.positions_error = None

    def fetch_gate_futures_account(self):
        if self.account_error:
            raise RuntimeError(self.account_error)
        return dict(self.account)

    def fetch_gate_futures_positions(self):
        if self.positions_error:
            raise RuntimeError(self.positions_error)
        return [dict(item) for item in self.positions]


class TestGateCrossRisk(unittest.TestCase):
    def test_gate_cross_mmr_is_authoritative_over_local_formula(self):
        account = {
            'cross_mmr': '3.2',
            'cross_margin_balance': '100',
            'cross_maintenance_margin': '31.25',
            'cross_available': '80',
            'cross_initial_margin': '10',
            'cross_order_margin': '0',
        }
        positions = [{
            'contract': 'AI_USDT',
            'size': '-10',
            'initial_margin': '10',
            'maintenance_margin': '1',
            'mark_price': '1',
            'liq_price': '1.2',
        }]

        risk = build_gate_cross_risk(
            account,
            positions,
            equity=100,
            available=80,
            margin_used=10,
            thresholds=GateCrossRiskThresholds(),
        )

        self.assertEqual(risk['account_mmr_pct'], 320.0)
        self.assertEqual(risk['computed_account_mmr_pct'], 10000.0)
        self.assertEqual(risk['account_mmr_source'], 'gate_account.cross_mmr')
        self.assertEqual(risk['initial_margin_usdt'], 10.0)
        self.assertEqual(risk['maintenance_margin_usdt'], 31.25)
        self.assertEqual(risk['status'], 'warning')
        self.assertNotIn('mmr_pct', risk['top_risks'][0])
        self.assertNotIn('worst_contract', risk)

    def test_account_metrics_prefer_cross_risk_fields(self):
        metrics = gate_account_metrics({
            'available': '70',
            'total': '100',
            'unrealised_pnl': '-5',
            'cross_initial_margin': '20',
            'cross_margin_balance': '96',
            'cross_available': '74',
        })

        self.assertEqual(metrics['equity'], 95.0)
        self.assertEqual(metrics['risk_equity'], 96.0)
        self.assertEqual(metrics['risk_available'], 74.0)
        self.assertEqual(metrics['margin_used'], 20.0)
        self.assertEqual(metrics['cross_initial_margin'], 20.0)

    def test_account_margin_usage_ignores_non_cross_margin_fields(self):
        metrics = gate_account_metrics({
            'total': '100',
            'available': '95',
            'position_margin': '90',
            'isolated_position_margin': '80',
            'position_initial_margin': '70',
            'order_margin': '60',
            'cross_initial_margin': '3',
            'cross_order_margin': '2',
        })

        self.assertEqual(metrics['margin_used'], 5.0)
        self.assertEqual(metrics['cross_initial_margin'], 3.0)
        self.assertEqual(metrics['cross_order_margin'], 2.0)

    def test_monitor_publishes_account_and_position_timestamps(self):
        executor = FakeRiskExecutor(
            {
                'cross_mmr': '8',
                'cross_margin_balance': '100',
                'cross_maintenance_margin': '12.5',
                'cross_available': '75',
            },
            [{
                'contract': 'AI_USDT',
                'size': '-10',
                'margin': '0',
                'maintenance_margin': '2',
                'mark_price': '1',
                'liq_price': '1.2',
            }],
        )
        monitor = GateCrossRiskMonitor(executor)

        snapshot = monitor.refresh()

        self.assertEqual(snapshot['account_mmr_pct'], 800.0)
        self.assertEqual(snapshot['source'], 'gate_account_api')
        self.assertIsNotNone(snapshot['account_fetched_at_ts'])
        self.assertIsNotNone(snapshot['positions_fetched_at_ts'])
        self.assertEqual(snapshot['health_status'], 'healthy')
        self.assertEqual(snapshot['health_label'], '正常')
        self.assertEqual(monitor.get_positions()[0]['contract'], 'AI_USDT')

    def test_position_failure_keeps_account_mmr_and_last_positions(self):
        executor = FakeRiskExecutor(
            {
                'cross_mmr': '8',
                'cross_margin_balance': '100',
                'cross_maintenance_margin': '12.5',
                'cross_available': '75',
            },
            [{
                'contract': 'AI_USDT',
                'size': '-10',
                'margin': '0',
                'maintenance_margin': '2',
                'mark_price': '1',
                'liq_price': '1.2',
            }],
        )
        monitor = GateCrossRiskMonitor(executor)
        first = monitor.refresh()
        executor.account['cross_mmr'] = '4.8'
        executor.positions_error = 'timeout'

        second = monitor.refresh()

        self.assertEqual(second['account_mmr_pct'], 480.0)
        self.assertEqual(second['status'], 'unknown')
        self.assertEqual(second['observed_status'], 'warning')
        self.assertIn('Gate positions: timeout', second['error'])
        self.assertEqual(second['positions_fetched_at_ts'], first['positions_fetched_at_ts'])
        self.assertEqual(monitor.get_positions()[0]['contract'], 'AI_USDT')

    def test_account_failure_publishes_unknown_snapshot(self):
        executor = FakeRiskExecutor({}, [])
        executor.account_error = 'timeout'
        monitor = GateCrossRiskMonitor(executor)

        snapshot = monitor.refresh()

        self.assertEqual(snapshot['status'], 'unknown')
        self.assertIn('Gate account: timeout', snapshot['error'])
        self.assertIsNone(snapshot['account_fetched_at_ts'])
        self.assertEqual(snapshot['health_status'], 'unavailable')

    def test_health_reports_degraded_for_fresh_partial_failure(self):
        health = gate_cross_risk_health(
            {
                'status': 'unknown',
                'error': 'Gate positions: timeout',
                'account_fetched_at_ts': 100.0,
                'positions_fetched_at_ts': 99.0,
            },
            now_ts=100.0,
            max_age_sec=5.0,
        )

        self.assertEqual(health['health_status'], 'degraded')
        self.assertEqual(health['account_age_sec'], 0.0)
        self.assertEqual(health['positions_age_sec'], 1.0)
        self.assertFalse(health['stale'])

    def test_health_reports_stale_when_either_input_exceeds_max_age(self):
        health = gate_cross_risk_health(
            {
                'status': 'safe',
                'account_fetched_at_ts': 100.0,
                'positions_fetched_at_ts': 99.0,
            },
            now_ts=106.0,
            max_age_sec=5.0,
        )

        self.assertEqual(health['health_status'], 'stale')
        self.assertEqual(health['health_label'], '数据陈旧')
        self.assertTrue(health['stale'])

    def test_health_reports_unavailable_without_account_timestamp(self):
        health = gate_cross_risk_health(None, now_ts=100.0, max_age_sec=5.0)

        self.assertEqual(health['health_status'], 'unavailable')
        self.assertIsNone(health['account_age_sec'])

    def test_missing_account_mmr_is_unknown_when_position_is_not_dangerous(self):
        risk = build_gate_cross_risk(
            {'cross_margin_balance': '100', 'cross_available': '80'},
            [{
                'contract': 'AI_USDT',
                'size': '-10',
                'margin': '0',
                'maintenance_margin': '2',
                'mark_price': '1',
                'liq_price': '1.2',
            }],
            equity=100,
            available=80,
            margin_used=10,
        )

        self.assertEqual(risk['status'], 'unknown')
        self.assertIsNone(risk['account_mmr_pct'])

    def test_liquidation_danger_wins_when_account_mmr_is_missing(self):
        risk = build_gate_cross_risk(
            {'cross_margin_balance': '100', 'cross_available': '80'},
            [{
                'contract': 'AI_USDT',
                'size': '-10',
                'margin': '0',
                'maintenance_margin': '2',
                'mark_price': '1',
                'liq_price': '1.02',
            }],
            equity=100,
            available=80,
            margin_used=10,
        )

        self.assertEqual(risk['status'], 'danger')
        self.assertAlmostEqual(risk['nearest_liq_distance_bps'], 200.0)


if __name__ == '__main__':
    unittest.main()
