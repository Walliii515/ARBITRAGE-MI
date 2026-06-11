# coding: utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.service_lifecycle import merge_contracts_with_forced_holdings


class TestServiceLifecycleSubscriptions(unittest.TestCase):
    def test_forced_holdings_are_appended_beyond_selected_contracts(self):
        merged, forced = merge_contracts_with_forced_holdings(
            ['ASR_USDT', 'BANK_USDT'],
            ['PSG_USDT', 'BANK_USDT', 'psg_usdt'],
        )

        self.assertEqual(merged, ['ASR_USDT', 'BANK_USDT', 'PSG_USDT'])
        self.assertEqual(forced, ['PSG_USDT'])


if __name__ == '__main__':
    unittest.main()
