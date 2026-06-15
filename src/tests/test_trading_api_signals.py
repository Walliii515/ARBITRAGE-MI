import unittest

from api.trading_api import _build_forward_signal_filters


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


if __name__ == '__main__':
    unittest.main()
