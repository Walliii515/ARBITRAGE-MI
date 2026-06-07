# coding: utf-8
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.reconciliation import Reconciler, ReconciliationConfig, normalize_asset_set


class TestReconciliationIgnoreAssets(unittest.TestCase):
    def test_normalize_asset_set_accepts_list_and_csv(self):
        self.assertEqual(normalize_asset_set(['bnb', ' USDT ', None, '']), {'BNB', 'USDT'})
        self.assertEqual(normalize_asset_set('bnb, fdusd'), {'BNB', 'FDUSD'})

    def test_compare_binance_ignores_fee_asset(self):
        reconciler = Reconciler(
            executor=object(),
            cfg=ReconciliationConfig(ignored_binance_spot_assets={'BNB'}),
        )

        rows = reconciler._compare_binance(
            datetime(2026, 6, 7, 10, 0, 0),
            local={'ALLO': 1.0},
            balances=[
                {'asset': 'BNB', 'total': 0.02, 'free': 0.02, 'locked': 0.0},
                {'asset': 'ALLO', 'total': 1.1, 'free': 1.1, 'locked': 0.0},
            ],
        )

        self.assertEqual([row['base_asset'] for row in rows], ['ALLO'])
        self.assertFalse(rows[0]['is_match'])
        self.assertAlmostEqual(rows[0]['diff_value'], 0.1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
