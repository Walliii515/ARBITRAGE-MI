# coding: utf-8
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.trading_executor import TradingExecutor, TradingExecutorConfig


class TestForwardBinanceMarginGuard(unittest.TestCase):
    def _executor(self, margin_level=3.0):
        executor = TradingExecutor(
            TradingExecutorConfig(
                capital_required=True,
                open_amount_usdt=100.0,
                min_available_ratio=0.0,
                max_asset_exposure_ratio=0.0,
                binance_margin_required=True,
                binance_margin_min_open_level=2.5,
            ),
            contract_meta={},
            spot_meta={},
        )
        executor.update_account_capital_status(
            {
                'binance': {
                    'available': 1000.0,
                    'net_value': 2000.0,
                    'margin': {
                        'enabled': True,
                        'marginLevel': margin_level,
                    },
                },
                'gate': {
                    'available': 1000.0,
                    'net_value': 2000.0,
                    'cross_risk': {
                        'status': 'safe',
                        'account_mmr_pct': 1200.0,
                        'account_fetched_at_ts': time.time(),
                    },
                },
            },
            9999999999,
        )
        return executor

    def test_blocks_open_when_forward_binance_margin_level_is_low(self):
        ok, reason = self._executor(margin_level=2.4)._check_account_capital(100.0)

        self.assertFalse(ok)
        self.assertIn('Binance Margin Level', reason)

    def test_allows_open_when_forward_binance_margin_level_is_healthy(self):
        ok, reason = self._executor(margin_level=3.1)._check_account_capital(100.0)

        self.assertTrue(ok)
        self.assertEqual(reason, '')


if __name__ == '__main__':
    unittest.main()
