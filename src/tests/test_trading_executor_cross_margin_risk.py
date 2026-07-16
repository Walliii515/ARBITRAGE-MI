# coding: utf-8
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tests.test_open_close_logic import make_trading_executor


class TestTradingExecutorGateCrossMarginRisk(unittest.TestCase):
    def test_cross_margin_open_guard_uses_account_mmr_not_contract_pseudo_mmr(self):
        te = make_trading_executor(capital_gate_leverage=0.0)
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
        te._holding_margin_rate['AI'] = 0.6

        self.assertAlmostEqual(te._effective_holding_margin_rate('AI'), 3349.5)
        self.assertNotIn(
            '保证金风控',
            te._get_risk_fail_reason({'base_asset': 'AI'}),
        )

    def test_cross_margin_open_guard_still_blocks_low_account_mmr(self):
        te = make_trading_executor(capital_gate_leverage=0.0)
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
        te._holding_margin_rate['AI'] = 800.0

        self.assertEqual(
            te._get_risk_fail_reason({'base_asset': 'AI'}),
            '保证金风控(Gate全仓MMR250.0%<300.0%)',
        )

    def test_isolated_margin_open_guard_keeps_contract_mmr(self):
        te = make_trading_executor(capital_gate_leverage=10.0)
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
        te._holding_margin_rate['AI'] = 250.0

        self.assertEqual(
            te._get_risk_fail_reason({'base_asset': 'AI'}),
            '保证金风控(保证金/维持保证金250.0%<300.0%)',
        )


if __name__ == '__main__':
    unittest.main()
