# coding: utf-8
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.holding_volatility_monitor import (
    HoldingVolatilityMonitor,
    PendingVolatilityAlert,
    VolatilityAlertConfig,
    VolatilitySnapshot,
    build_volatility_snapshots,
    evaluate_transition,
    refresh_holding_volatility_alerts,
)
from calc.etl_pipeline import _run_update_gate_future_contracts


def snapshot(amplitude=50.0, position=0.8):
    return VolatilitySnapshot(
        base_asset='TUT',
        amplitude_pct=amplitude,
        range_position=position,
        last_price=1.4,
        high_24h=1.5,
        low_24h=1.0,
    )


class TestVolatilityTransitions(unittest.TestCase):
    def setUp(self):
        self.cfg = VolatilityAlertConfig()

    def test_exact_trigger_boundaries_alert(self):
        self.assertEqual(
            evaluate_transition(active=False, snapshot=snapshot(), is_holding=True, cfg=self.cfg),
            'trigger',
        )

    def test_both_trigger_conditions_are_required(self):
        self.assertEqual(
            evaluate_transition(active=False, snapshot=snapshot(49.99, 0.9), is_holding=True, cfg=self.cfg),
            'hold',
        )
        self.assertEqual(
            evaluate_transition(active=False, snapshot=snapshot(80.0, 0.799), is_holding=True, cfg=self.cfg),
            'hold',
        )

    def test_hysteresis_band_stays_active(self):
        self.assertEqual(
            evaluate_transition(active=True, snapshot=snapshot(40.0, 0.6), is_holding=True, cfg=self.cfg),
            'hold',
        )
        self.assertEqual(
            evaluate_transition(active=True, snapshot=snapshot(45.0, 0.7), is_holding=True, cfg=self.cfg),
            'hold',
        )

    def test_either_recovery_condition_ends_episode(self):
        self.assertEqual(
            evaluate_transition(active=True, snapshot=snapshot(39.99, 0.9), is_holding=True, cfg=self.cfg),
            'recover',
        )
        self.assertEqual(
            evaluate_transition(active=True, snapshot=snapshot(80.0, 0.599), is_holding=True, cfg=self.cfg),
            'recover',
        )

    def test_missing_market_data_does_not_fake_recovery(self):
        self.assertEqual(
            evaluate_transition(active=True, snapshot=None, is_holding=True, cfg=self.cfg),
            'hold',
        )

    def test_position_no_longer_held_recovers(self):
        self.assertEqual(
            evaluate_transition(active=True, snapshot=snapshot(), is_holding=False, cfg=self.cfg),
            'recover',
        )

    def test_invalid_hysteresis_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            VolatilityAlertConfig(recover_amplitude_pct=50.0).validate()
        with self.assertRaises(ValueError):
            VolatilityAlertConfig(recover_range_position=0.8).validate()


class FakeStore:
    def __init__(self, pending=None):
        self.pending = pending or []
        self.ensure_calls = 0
        self.apply_calls = 0
        self.sent = []

    def ensure_table(self):
        self.ensure_calls += 1

    def fetch_holding_assets(self):
        return {'TUT'}

    def apply_transitions(self, holdings, snapshots, cfg):
        self.apply_calls += 1
        self.holdings = holdings
        self.snapshots = snapshots
        return self.pending

    def mark_notification_sent(self, base_asset, episode_id):
        self.sent.append((base_asset, episode_id))


class TestHoldingVolatilityMonitor(unittest.TestCase):
    def setUp(self):
        self.contracts = [{
            'base_asset': 'tut',
            'range_24h_pct': 55.0,
            'range_position_24h': 0.9,
            'last_price': 1.45,
            'high_24h': 1.5,
            'low_24h': 1.0,
        }]

    def test_snapshot_builder_skips_incomplete_or_non_finite_rows(self):
        rows = self.contracts + [
            {'base_asset': 'BAD', 'range_24h_pct': None},
            {
                'base_asset': 'NAN', 'range_24h_pct': 'nan', 'range_position_24h': 1,
                'last_price': 1, 'high_24h': 1, 'low_24h': 1,
            },
        ]
        built = build_volatility_snapshots(rows)
        self.assertEqual(set(built), {'TUT'})

    def test_sends_one_episode_notification_and_marks_it_sent(self):
        event = PendingVolatilityAlert(build_volatility_snapshots(self.contracts)['TUT'], 3)
        store = FakeStore([event])
        notifier = MagicMock(return_value={'id': 1})
        monitor = HoldingVolatilityMonitor(store=store, notifier=notifier)

        sent = monitor.refresh(self.contracts)

        self.assertEqual(sent, 1)
        self.assertEqual(store.sent, [('TUT', 3)])
        self.assertEqual(notifier.call_args.kwargs['dedup_key'], 'holding-volatility:TUT:3')
        self.assertEqual(notifier.call_args.kwargs['source'], 'holding_volatility')

    def test_failed_notification_is_not_marked_sent(self):
        event = PendingVolatilityAlert(build_volatility_snapshots(self.contracts)['TUT'], 4)
        store = FakeStore([event])
        monitor = HoldingVolatilityMonitor(
            store=store,
            notifier=MagicMock(side_effect=RuntimeError('db unavailable')),
        )

        self.assertEqual(monitor.refresh(self.contracts), 0)
        self.assertEqual(store.sent, [])

    def test_disabled_monitor_does_not_touch_store(self):
        store = FakeStore()
        monitor = HoldingVolatilityMonitor(
            cfg=VolatilityAlertConfig(enabled=False),
            store=store,
        )

        self.assertEqual(monitor.refresh(self.contracts), 0)
        self.assertEqual(store.ensure_calls, 0)

    def test_public_wrapper_does_not_break_etl_on_monitor_failure(self):
        with patch(
            'calc.holding_volatility_monitor.HoldingVolatilityMonitor.refresh',
            side_effect=RuntimeError('state table unavailable'),
        ):
            self.assertEqual(refresh_holding_volatility_alerts(self.contracts), 0)

    def test_gate_etl_runs_monitor_only_after_successful_refresh(self):
        with patch(
            'calc.update_gate_future_contracts.update_gate_future_contracts',
            return_value=self.contracts,
        ), patch(
            'calc.holding_volatility_monitor.refresh_holding_volatility_alerts',
        ) as refresh_alerts:
            _run_update_gate_future_contracts()
        refresh_alerts.assert_called_once_with(self.contracts)

    def test_gate_etl_skips_monitor_for_empty_refresh(self):
        with patch(
            'calc.update_gate_future_contracts.update_gate_future_contracts',
            return_value=None,
        ), patch(
            'calc.holding_volatility_monitor.refresh_holding_volatility_alerts',
        ) as refresh_alerts:
            _run_update_gate_future_contracts()
        refresh_alerts.assert_not_called()


if __name__ == '__main__':
    unittest.main()
