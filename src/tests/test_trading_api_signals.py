import unittest
from datetime import datetime

from api.trading_api import (
    _append_unique_notification,
    _build_forward_signal_filters,
    _reconciliation_latest_sql,
    _should_emit_reconciliation_notification,
)


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


class ReconciliationLatestQueryTests(unittest.TestCase):
    def test_latest_query_attaches_gate_quanto_multiplier(self):
        sql = _reconciliation_latest_sql(" AND NOT (s.exchange = 'binance' AND s.base_asset IN (%s))")

        self.assertIn('c.quanto_multiplier', sql)
        self.assertIn('mi_gate_future_contracts c', sql)
        self.assertIn('UPPER(TRIM(c.base_asset)) COLLATE utf8mb4_unicode_ci', sql)
        self.assertIn('UPPER(TRIM(s.base_asset)) COLLATE utf8mb4_unicode_ci', sql)
        self.assertIn('s.snapshot_at = (SELECT MAX(snapshot_at) FROM mi_recon_snapshot)', sql)


if __name__ == '__main__':
    unittest.main()
