# coding: utf-8
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from api import orderbook_server


class TestOrderbookServerEmergencyClose(unittest.TestCase):
    def test_profit_release_requires_insufficient_auto_funding_and_no_active_task(self):
        coordinator = MagicMock()
        coordinator.is_profit_release_allowed.return_value = True

        with (
            patch.object(orderbook_server, '_auto_fund_transfer_coordinator', coordinator),
            patch.object(orderbook_server, '_open_paused', False),
            patch.object(orderbook_server.config, 'get_bool', return_value=True),
            patch.object(orderbook_server, 'fund_transfer_open_locked', return_value=False),
        ):
            self.assertTrue(orderbook_server._auto_fund_profit_release_allowed())

        with (
            patch.object(orderbook_server, '_auto_fund_transfer_coordinator', coordinator),
            patch.object(orderbook_server, '_open_paused', False),
            patch.object(orderbook_server.config, 'get_bool', return_value=True),
            patch.object(orderbook_server, 'fund_transfer_open_locked', return_value=True),
        ):
            self.assertFalse(orderbook_server._auto_fund_profit_release_allowed())

    def test_paused_auto_funding_revokes_stale_profit_release_permission(self):
        coordinator = MagicMock()
        with (
            patch.object(orderbook_server, '_auto_fund_transfer_coordinator', coordinator),
            patch.object(orderbook_server, '_open_paused', True),
            patch.object(orderbook_server.config, 'get_bool', return_value=True),
        ):
            result = orderbook_server._evaluate_auto_fund_transfer({
                'health_status': 'healthy',
            })

        self.assertEqual(result['action'], 'disabled_with_forward_open')
        coordinator.suspend_profit_release.assert_called_once_with()
        coordinator.evaluate.assert_not_called()

    def test_successful_350_release_wakes_auto_transfer_before_broadcast(self):
        coordinator = MagicMock()
        results = [{
            'position_id': 7,
            'base_asset': 'AI',
            'success': True,
            'close_reason': 'margin_close',
            'margin_risk_stage': 'profit_release_350',
        }]
        with (
            patch.object(orderbook_server, '_auto_fund_transfer_coordinator', coordinator),
            patch.object(orderbook_server, '_record_auto_risk_close_notifications'),
            patch.object(orderbook_server, 'event_loop', None),
            patch.object(orderbook_server, 'broadcast_queue', None),
        ):
            orderbook_server._publish_close_position_results(results)

        coordinator.notify_binance_funds_released.assert_called_once_with()

    def test_notification_store_failure_cannot_prevent_350_transfer_wakeup(self):
        coordinator = MagicMock()
        results = [{
            'success': True,
            'margin_risk_stage': 'profit_release_350',
        }]
        with (
            patch.object(orderbook_server, '_auto_fund_transfer_coordinator', coordinator),
            patch.object(
                orderbook_server,
                '_record_auto_risk_close_notifications',
                side_effect=RuntimeError('notification db unavailable'),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, 'notification db unavailable'):
                orderbook_server._publish_close_position_results(results)

        coordinator.notify_binance_funds_released.assert_called_once_with()

    def test_failed_or_non_350_close_does_not_wake_auto_transfer(self):
        coordinator = MagicMock()
        cases = [
            [{
                'success': False,
                'margin_risk_stage': 'profit_release_350',
            }],
            [{
                'success': True,
                'margin_risk_stage': 'controlled_300',
            }],
        ]
        with (
            patch.object(orderbook_server, '_auto_fund_transfer_coordinator', coordinator),
            patch.object(orderbook_server, '_record_auto_risk_close_notifications'),
            patch.object(orderbook_server, 'event_loop', None),
            patch.object(orderbook_server, 'broadcast_queue', None),
        ):
            for results in cases:
                orderbook_server._publish_close_position_results(results)

        coordinator.notify_binance_funds_released.assert_not_called()

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

    def test_live_gate_cross_risk_payload_keeps_close_priority_contract(self):
        payload = orderbook_server._build_live_gate_cross_risk_payload(
            {
                'status': 'warning',
                'priority_close_contract': 'BANK_USDT',
                'priority_close_reason': 'maintenance_margin',
                'account_fetched_at_ts': 100.0,
                'positions_fetched_at_ts': 100.0,
            },
            now_ts=101.0,
        )

        self.assertEqual(payload['priority_close_contract'], 'BANK_USDT')
        self.assertEqual(payload['priority_close_reason'], 'maintenance_margin')
        self.assertEqual(payload['health_status'], 'healthy')

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
