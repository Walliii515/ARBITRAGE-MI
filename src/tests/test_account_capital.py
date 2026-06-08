# coding: utf-8
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.account_capital import AccountCapitalSnapshotter, AccountCapitalConfig


class FakeCapitalExecutor:
    def __init__(self, gate_account=None):
        self.gate_account = gate_account or {}

    def fetch_gate_futures_account(self):
        return dict(self.gate_account)


class TestAccountCapitalSnapshotter(unittest.TestCase):
    def test_gate_equity_includes_unrealized_pnl(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor({
                'available': '100',
                'total': '120',
                'unrealised_pnl': '7.5',
                'position_margin': '20',
                'order_margin': '0',
            }),
            AccountCapitalConfig(),
        )
        pnl = {
            'gate_realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'gate_fee_cost': 0.0,
            'window': {},
            'gate_account_book_types': {},
        }

        row = snapshotter._build_gate_row(datetime(2026, 6, 9, 12, 0, 0), pnl)

        self.assertEqual(row['equity_usdt'], 127.5)
        self.assertEqual(row['unrealized_pnl_usdt'], 7.5)
        self.assertEqual(row['detail']['raw_total_usdt'], 120.0)
        self.assertEqual(row['detail']['equity_formula'], 'gate_total_plus_unrealized_pnl')

    def test_total_equity_uses_corrected_gate_equity(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        binance = {
            'equity_usdt': 500.0,
            'available_usdt': 480.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 20.0,
        }
        gate = {
            'equity_usdt': 127.5,
            'available_usdt': 100.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 20.0,
            'margin_used_usdt': 20.0,
            'unrealized_pnl_usdt': 7.5,
        }
        pnl = {
            'realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'fee_cost': 0.0,
            'total_pnl': 0.0,
            'window': {},
        }

        row = snapshotter._build_total_row(datetime(2026, 6, 9, 12, 0, 0), binance, gate, pnl)

        self.assertEqual(row['equity_usdt'], 627.5)
        self.assertEqual(row['unrealized_pnl_usdt'], 7.5)
        self.assertEqual(row['detail']['equity_formula'], 'binance_equity_plus_gate_equity')


if __name__ == '__main__':
    unittest.main()
