# coding: utf-8
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.forward_margin_account import ForwardMarginAccountOperator


class FakeMarginExecutor:
    def __init__(self):
        self.calls = []

    def borrow_binance_cross_margin_asset(self, asset, amount):
        self.calls.append(('borrow', asset, amount))
        return {'success': True, 'asset': asset, 'amount': amount, 'exchange_order_id': 'b1'}

    def repay_binance_cross_margin_asset(self, asset, amount):
        self.calls.append(('repay', asset, amount))
        return {'success': True, 'asset': asset, 'amount': amount, 'exchange_order_id': 'r1'}

    def transfer_binance_cross_margin_asset(self, asset, amount, direction):
        self.calls.append(('transfer', asset, amount, direction))
        return {'success': True, 'asset': asset, 'amount': amount, 'direction': direction, 'exchange_order_id': 't1'}


class TestForwardMarginAccountOperator(unittest.TestCase):
    def test_borrow_repay_and_transfer_usdt(self):
        executor = FakeMarginExecutor()
        operator = ForwardMarginAccountOperator(executor)

        borrow = operator.borrow_usdt(50)
        repay = operator.repay_usdt(10)
        transfer = operator.transfer_usdt(20, 'margin_to_spot')

        self.assertTrue(borrow.success)
        self.assertTrue(repay.success)
        self.assertTrue(transfer.success)
        self.assertEqual(executor.calls, [
            ('borrow', 'USDT', 50.0),
            ('repay', 'USDT', 10.0),
            ('transfer', 'USDT', 20.0, 'margin_to_spot'),
        ])

    def test_rejects_invalid_amount(self):
        operator = ForwardMarginAccountOperator(FakeMarginExecutor())

        with self.assertRaises(ValueError):
            operator.borrow_usdt(0)

    def test_rejects_unknown_transfer_direction(self):
        operator = ForwardMarginAccountOperator(FakeMarginExecutor())

        with self.assertRaises(ValueError):
            operator.transfer_usdt(1, 'wallet_to_mars')  # type: ignore[arg-type]


if __name__ == '__main__':
    unittest.main()
