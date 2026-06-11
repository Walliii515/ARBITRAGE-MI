# coding: utf-8
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.account_capital import AccountCapitalSnapshotter, AccountCapitalConfig


class FakeCapitalExecutor:
    def __init__(self, gate_account=None, binance_balances=None, binance_prices=None):
        self.gate_account = gate_account or {}
        self.binance_balances = binance_balances or []
        self.binance_prices = binance_prices or {}

    def fetch_gate_futures_account(self):
        return dict(self.gate_account)

    def fetch_binance_account_balances(self):
        return list(self.binance_balances)

    def fetch_binance_ticker_prices(self, assets):
        return dict(self.binance_prices)


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

    def test_binance_row_includes_local_spot_realized_pnl(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(
                binance_balances=[
                    {'asset': 'USDT', 'free': '100', 'locked': '2', 'total': '102'},
                    {'asset': 'BTC', 'free': '0.1', 'locked': '0', 'total': '0.1'},
                ],
                binance_prices={'BTC': 50000},
            ),
            AccountCapitalConfig(),
        )
        pnl = {
            'binance_realized_pnl': 12.5,
            'binance_fee_cost': -0.8,
            'window': {},
            'binance_spot_realized': {
                'closed_count': 2,
                'open_amount': 100.0,
                'close_amount': 112.5,
                'realized_pnl': 12.5,
            },
        }

        row = snapshotter._build_binance_row(datetime(2026, 6, 9, 12, 0, 0), pnl)

        self.assertEqual(row['equity_usdt'], 5102.0)
        self.assertEqual(row['realized_pnl_usdt'], 12.5)
        self.assertEqual(row['fee_cost_usdt'], -0.8)
        self.assertEqual(row['total_pnl_usdt'], 11.7)
        self.assertEqual(row['detail']['binance_spot_realized']['closed_count'], 2)

    def test_total_pnl_includes_binance_spot_realized_pnl(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        binance = {
            'equity_usdt': 500.0,
            'available_usdt': 480.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 20.0,
        }
        gate = {
            'equity_usdt': 120.0,
            'available_usdt': 80.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 40.0,
            'margin_used_usdt': 40.0,
            'unrealized_pnl_usdt': -3.0,
        }
        pnl = {
            'realized_pnl': -20.0,  # gate -35 + binance +15
            'funding_pnl': 4.0,
            'fee_cost': -2.0,
            'total_pnl': -18.0,
            'window': {},
        }

        row = snapshotter._build_total_row(datetime(2026, 6, 9, 12, 0, 0), binance, gate, pnl)

        self.assertEqual(row['realized_pnl_usdt'], -20.0)
        self.assertEqual(row['funding_pnl_usdt'], 4.0)
        self.assertEqual(row['fee_cost_usdt'], -2.0)
        self.assertEqual(row['total_pnl_usdt'], -18.0)

    def test_exchange_pnl_summary_uses_local_strategy_pnl(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        snapshotter._load_strategy_pnl_summary = lambda start, end: {
            'realized_pnl': -20.0,
            'gate_realized_pnl': -35.0,
            'funding_pnl': 4.0,
            'binance_fee_cost': -0.8,
            'gate_fee_cost': -1.2,
            'fee_cost': -2.0,
            'binance_spot_realized': {
                'closed_count': 3,
                'open_amount': 300.0,
                'close_amount': 315.0,
                'realized_pnl': 15.0,
            },
            'gate_strategy_realized': {
                'realized_pnl': -35.0,
                'derived_from': 'strategy_realized_pnl - binance_spot_realized_pnl',
            },
        }

        summary = snapshotter._load_exchange_pnl_summary(datetime(2026, 6, 9, 12, 0, 0))

        self.assertEqual(summary['gate_realized_pnl'], -35.0)
        self.assertEqual(summary['binance_realized_pnl'], 15.0)
        self.assertEqual(summary['realized_pnl'], -20.0)
        self.assertEqual(summary['funding_pnl'], 4.0)
        self.assertEqual(summary['fee_cost'], -2.0)
        self.assertEqual(summary['total_pnl'], -18.0)


if __name__ == '__main__':
    unittest.main()
