# coding: utf-8
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tests.test_open_close_logic import make_trading_executor


class TestTradingExecutorGateCrossMarginRisk(unittest.TestCase):
    @staticmethod
    def _executor_with_risk(risk):
        te = make_trading_executor()
        te.capital_required = True
        te.gate_cross_risk_max_age_sec = 5.0
        te._account_summary = {
            'binance': {'available': 1000.0, 'net_value': 1000.0},
            'gate': {
                'available': 1000.0,
                'net_value': 1000.0,
                'cross_risk': risk,
            },
        }
        te._account_summary_ts = time.time()
        return te

    def test_fresh_safe_risk_allows_open(self):
        te = self._executor_with_risk({
            'enabled': True,
            'status': 'safe',
            'account_mmr_pct': 3349.5,
            'account_fetched_at_ts': time.time(),
        })

        ok, reason = te._check_account_capital(10.0)

        self.assertTrue(ok, reason)

    def test_warning_risk_blocks_open(self):
        te = self._executor_with_risk({
            'enabled': True,
            'status': 'warning',
            'account_mmr_pct': 420.0,
            'account_fetched_at_ts': time.time(),
        })

        ok, reason = te._check_account_capital(10.0)

        self.assertFalse(ok)
        self.assertIn('Gate全仓风险预警', reason)

    def test_missing_risk_snapshot_blocks_open(self):
        te = self._executor_with_risk(None)

        ok, reason = te._check_account_capital(10.0)

        self.assertFalse(ok)
        self.assertIn('Gate全仓风险未知', reason)
        self.assertIn('无实时风险快照', reason)

    def test_error_snapshot_blocks_open(self):
        te = self._executor_with_risk({
            'enabled': True,
            'status': 'unknown',
            'error': 'Gate positions: timeout',
            'account_fetched_at_ts': time.time(),
        })

        ok, reason = te._check_account_capital(10.0)

        self.assertFalse(ok)
        self.assertIn('Gate positions: timeout', reason)

    def test_stale_snapshot_blocks_open(self):
        te = self._executor_with_risk({
            'enabled': True,
            'status': 'safe',
            'account_mmr_pct': 1200.0,
            'account_fetched_at_ts': time.time() - 10.0,
        })

        ok, reason = te._check_account_capital(10.0)

        self.assertFalse(ok)
        self.assertIn('快照过期', reason)

if __name__ == '__main__':
    unittest.main()
