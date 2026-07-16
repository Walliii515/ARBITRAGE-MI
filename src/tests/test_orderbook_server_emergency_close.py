# coding: utf-8
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api import orderbook_server


class TestOrderbookServerEmergencyClose(unittest.TestCase):
    def test_live_gate_cross_risk_payload_recomputes_staleness(self):
        payload = orderbook_server._build_live_gate_cross_risk_payload(
            {
                'status': 'safe',
                'source': 'gate_account_api',
                'account_fetched_at_ts': 100.0,
                'positions_fetched_at_ts': 100.0,
            },
            now_ts=106.0,
        )

        self.assertEqual(payload['status'], 'safe')
        self.assertEqual(payload['health_status'], 'stale')
        self.assertEqual(payload['account_age_sec'], 6.0)
        self.assertTrue(payload['stale'])

    def test_live_gate_cross_risk_payload_is_unknown_before_first_snapshot(self):
        payload = orderbook_server._build_live_gate_cross_risk_payload(None, now_ts=100.0)

        self.assertEqual(payload['status'], 'unknown')
        self.assertEqual(payload['health_status'], 'unavailable')
        self.assertIn('尚未产生快照', payload['error'])

    def test_second_level_refresh_records_bell_notification(self):
        snapshot = {'status': 'warning', 'account_mmr_pct': 450.0}
        monitor = MagicMock()
        monitor.refresh.return_value = snapshot
        notifier = MagicMock()

        with (
            patch.object(orderbook_server, '_gate_cross_risk_monitor', monitor),
            patch.object(orderbook_server, '_gate_cross_risk_notifier', notifier),
        ):
            result = orderbook_server._refresh_gate_cross_risk_once()

        self.assertIs(result, snapshot)
        monitor.refresh.assert_called_once_with()
        notifier.record.assert_called_once()
        self.assertIs(notifier.record.call_args.args[1], snapshot)

    def test_margin_danger_runs_when_orderbook_service_and_ws_are_unavailable(self):
        position = {
            'id': 7,
            'status': 'holding',
            'base_asset': 'AI',
            'future_contract': 'AI_USDT',
            'future_open_qty': 100.0,
        }
        tracker = MagicMock()
        tracker.get_holding_positions.return_value = [position]
        closing_executor = MagicMock()
        closing_executor.margin_risk_refresh_summary.return_value = {
            'danger': [],
            'missing': [],
        }
        emergency_results = [{
            'position_id': 7,
            'base_asset': 'AI',
            'success': True,
            'close_reason': 'margin_close',
        }]
        closing_executor.check_and_close_margin_danger.return_value = emergency_results

        with (
            patch.object(orderbook_server, 'svc', None),
            patch.object(orderbook_server, '_closing_executor', closing_executor),
            patch.object(orderbook_server, 'PositionTracker', return_value=tracker),
            patch.object(orderbook_server, '_get_gate_position_risk_snapshot', return_value=[]),
            patch.object(orderbook_server, 'attach_gate_position_risk') as attach_risk,
            patch.object(orderbook_server, '_publish_close_position_results') as publish,
        ):
            orderbook_server._run_close_position_check_once()

        attach_risk.assert_called_once_with([position], [])
        closing_executor.check_and_close_margin_danger.assert_called_once_with([position], {})
        publish.assert_called_once_with(emergency_results)
        closing_executor.check_and_close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
