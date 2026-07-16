# coding: utf-8
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tests.test_open_close_logic import make_trading_executor


class TestTradingExecutorGateCrossMarginRisk(unittest.TestCase):
    def test_open_guard_uses_account_mmr(self):
        te = make_trading_executor()
        te.margin_warning_pct = 300.0
        te._account_summary = {
            'gate': {
                'cross_risk': {
                    'enabled': True,
                    'status': 'safe',
                    'account_mmr_pct': 3349.5,
                },
            },
        }
        te._account_summary_ts = time.time()

        self.assertAlmostEqual(te._gate_cross_account_mmr_pct(), 3349.5)
        self.assertNotIn(
            '保证金风控',
            te._get_risk_fail_reason({'base_asset': 'AI'}),
        )

    def test_open_guard_blocks_low_account_mmr(self):
        te = make_trading_executor()
        te.margin_warning_pct = 300.0
        te._account_summary = {
            'gate': {
                'cross_risk': {
                    'enabled': True,
                    'status': 'danger',
                    'account_mmr_pct': 250.0,
                },
            },
        }
        te._account_summary_ts = time.time()

        self.assertEqual(
            te._get_risk_fail_reason({'base_asset': 'AI'}),
            '保证金风控(Gate全仓MMR250.0%<300.0%)',
        )

if __name__ == '__main__':
    unittest.main()
