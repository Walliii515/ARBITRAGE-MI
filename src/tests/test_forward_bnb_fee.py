# coding: utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.forward_bnb_fee import ForwardBnbFeeBuyer


class FakeBnbExecutor:
    def __init__(self, usdt_free=100):
        self.usdt_free = usdt_free
        self.orders = []

    def fetch_binance_account_balances(self):
        return [{'asset': 'USDT', 'free': self.usdt_free, 'locked': 0, 'total': self.usdt_free}]

    def place_binance_spot_order(self, order):
        self.orders.append(dict(order))
        return {
            'success': True,
            'exec_price': 600,
            'exec_qty': 0.05,
            'exchange_order_id': 'bnb1',
        }


class TestForwardBnbFeeBuyer(unittest.TestCase):
    def test_buy_with_usdt_places_bnb_market_buy(self):
        executor = FakeBnbExecutor(usdt_free=100)
        result = ForwardBnbFeeBuyer(executor).buy_with_usdt(30)

        self.assertTrue(result.success)
        self.assertEqual(result.amount_usdt, 30.0)
        self.assertEqual(len(executor.orders), 1)
        order = executor.orders[0]
        self.assertEqual(order['base_asset'], 'BNB')
        self.assertEqual(order['trade_direction'], 'buy')
        self.assertEqual(order['target_amount'], 30.0)
        self.assertTrue(order['order_uuid'].startswith('bnb_fee_'))

    def test_rejects_when_usdt_is_insufficient(self):
        executor = FakeBnbExecutor(usdt_free=4)
        result = ForwardBnbFeeBuyer(executor).buy_with_usdt(10)

        self.assertFalse(result.success)
        self.assertEqual(executor.orders, [])
        self.assertEqual(result.result['reason'], 'insufficient_usdt')

    def test_rejects_too_small_amount(self):
        with self.assertRaises(ValueError):
            ForwardBnbFeeBuyer(FakeBnbExecutor()).buy_with_usdt(1)


if __name__ == '__main__':
    unittest.main()
