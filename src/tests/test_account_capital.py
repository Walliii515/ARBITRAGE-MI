# coding: utf-8
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.account_capital import (
    AccountCapitalConfig,
    AccountCapitalSnapshotter,
    BinanceBnbBalanceNotifier,
    GateCrossRiskNotifier,
    build_default_capital_snapshotter,
    rebuild_capital_daily_summaries,
)


class FakeCapitalExecutor:
    def __init__(
        self,
        gate_account=None,
        gate_positions=None,
        binance_balances=None,
        binance_prices=None,
        margin_account=None,
    ):
        self.gate_account = gate_account or {}
        self.gate_positions = gate_positions or []
        self.binance_balances = binance_balances or []
        self.binance_prices = binance_prices or {}
        self.margin_account = margin_account

    def fetch_gate_futures_account(self):
        return dict(self.gate_account)

    def fetch_gate_futures_positions(self):
        return [dict(pos) for pos in self.gate_positions]

    def fetch_binance_account_balances(self):
        return list(self.binance_balances)

    def fetch_binance_ticker_prices(self, assets):
        return dict(self.binance_prices)

    def fetch_binance_cross_margin_account(self):
        if self.margin_account is None:
            raise RuntimeError('margin unavailable')
        return dict(self.margin_account)


class TestAccountCapitalSnapshotter(unittest.TestCase):
    def test_insert_rows_updates_daily_return_summary_with_total_row(self):
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value = cursor
        context = MagicMock()
        context.__enter__.return_value = connection
        context.__exit__.return_value = False
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(),
            AccountCapitalConfig(),
        )
        rows = [
            {
                'snapshot_at': datetime(2026, 7, 25, 12, 0, 0),
                'exchange': exchange,
                'equity_usdt': 1000 if exchange == 'total' else 500,
                'available_usdt': 100,
                'locked_usdt': 0,
                'position_value_usdt': 900,
                'margin_used_usdt': 0,
                'unrealized_pnl_usdt': 5 if exchange == 'total' else 0,
                'realized_pnl_usdt': 0,
                'funding_pnl_usdt': 0,
                'fee_cost_usdt': 0,
                'total_pnl_usdt': 20 if exchange == 'total' else 0,
                'detail': {'source': 'exchange_api'},
            }
            for exchange in ('binance', 'gate', 'total')
        ]

        with patch('calc.account_capital.db_manager.get_connection', return_value=context):
            snapshotter._insert_rows(rows)

        cursor.executemany.assert_called_once()
        daily_sql, daily_params = cursor.execute.call_args.args
        self.assertIn('mi_capital_daily_summary', daily_sql)
        self.assertEqual(daily_params['gross_pnl_usdt'], 25)
        self.assertEqual(daily_params['equity_usdt'], 1000)

    def test_rebuild_daily_summaries_uses_retained_total_snapshots(self):
        cursor = MagicMock()
        cursor.rowcount = 12
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('calc.account_capital.db_manager.get_cursor', return_value=context):
            affected = rebuild_capital_daily_summaries(400)

        self.assertEqual(affected, 12)
        sql, params = cursor.execute.call_args.args
        self.assertIn("exchange = 'total'", sql)
        self.assertIn('ON DUPLICATE KEY UPDATE', sql)
        self.assertEqual(params, (400,))

    def test_default_snapshotter_uses_cross_margin(self):
        with patch('calc.account_capital.fetch_contract_meta', return_value={}), \
                patch('calc.account_capital.fetch_spot_meta', return_value={}), \
                patch('calc.account_capital.build_exchange_config', return_value=object()), \
                patch('calc.account_capital.RealExecutor') as executor_cls:
            build_default_capital_snapshotter()

        self.assertEqual(executor_cls.call_args.kwargs['leverage'], 0.0)

    def test_gate_row_reuses_shared_cross_risk_snapshot(self):
        class NoPositionReadExecutor(FakeCapitalExecutor):
            def fetch_gate_futures_positions(self):
                raise AssertionError('capital snapshot must not refetch Gate positions')

        shared_risk = {
            'enabled': True,
            'status': 'safe',
            'account_mmr_pct': 1200.0,
            'source': 'gate_account_api',
        }
        snapshotter = AccountCapitalSnapshotter(
            NoPositionReadExecutor({
                'available': '100',
                'total': '120',
                'unrealised_pnl': '0',
            }),
            AccountCapitalConfig(),
            gate_cross_risk_provider=lambda: shared_risk,
        )
        pnl = {
            'gate_realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'gate_fee_cost': 0.0,
            'window': {},
        }

        row = snapshotter._build_gate_row(datetime(2026, 7, 16, 22, 0, 0), pnl)

        self.assertIs(row['detail']['gate_cross_risk'], shared_risk)

    def test_gate_equity_includes_unrealized_pnl(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor({
                'available': '100',
                'total': '120',
                'unrealised_pnl': '7.5',
                'cross_initial_margin': '20',
                'cross_order_margin': '0',
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
        self.assertEqual(row['detail']['gate_cross_risk']['status'], 'idle')

    def test_gate_cross_initial_margin_counts_as_used_margin(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor({
                'available': '4431.333487254757',
                'total': '4434.801087909632',
                'unrealised_pnl': '-0.24766',
                'cross_initial_margin': '3.066220654875',
                'cross_order_margin': '0',
            }),
            AccountCapitalConfig(),
        )
        pnl = {
            'gate_realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'gate_fee_cost': 0.0,
            'window': {},
        }

        row = snapshotter._build_gate_row(datetime(2026, 7, 6, 18, 4, 17), pnl)

        self.assertAlmostEqual(row['margin_used_usdt'], 3.066220654875)
        self.assertAlmostEqual(row['position_value_usdt'], 3.066220654875)
        self.assertEqual(row['locked_usdt'], 0.0)
        self.assertAlmostEqual(
            row['detail']['margin_used_components']['cross_initial_margin'],
            3.066220654875,
        )

    def test_gate_row_includes_cross_margin_risk_detail(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(
                gate_account={
                    'available': '60',
                    'total': '100',
                    'unrealised_pnl': '-10',
                    'cross_initial_margin': '40',
                    'cross_order_margin': '0',
                    'cross_mmr': '5.29411765',
                },
                gate_positions=[
                    {
                        'contract': 'AI_USDT',
                        'size': '-100',
                        'initial_margin': '35',
                        'unrealised_pnl': '-5',
                        'maintenance_margin': '15',
                        'mark_price': '0.0300',
                        'liq_price': '0.0306',
                    },
                    {
                        'contract': 'HMSTR_USDT',
                        'size': '-1000',
                        'initial_margin': '10',
                        'unrealised_pnl': '1',
                        'maintenance_margin': '2',
                        'mark_price': '0.0010',
                        'liq_price': '0.0014',
                    },
                ],
            ),
            AccountCapitalConfig(
                gate_cross_warning_mmr_pct=500,
                gate_cross_danger_mmr_pct=300,
                gate_cross_warning_liq_distance_bps=600,
                gate_cross_danger_liq_distance_bps=300,
                gate_cross_min_available_pct=15,
            ),
        )
        pnl = {
            'gate_realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'gate_fee_cost': 0.0,
            'window': {},
        }

        row = snapshotter._build_gate_row(datetime(2026, 6, 9, 12, 0, 0), pnl)
        risk = row['detail']['gate_cross_risk']

        self.assertEqual(risk['status'], 'danger')
        self.assertEqual(risk['position_count'], 2)
        self.assertAlmostEqual(risk['account_mmr_pct'], 529.411765, places=5)
        self.assertEqual(risk['initial_margin_usdt'], 40.0)
        self.assertEqual(risk['nearest_liq_contract'], 'AI_USDT')
        self.assertAlmostEqual(risk['nearest_liq_distance_bps'], 200.0, places=5)
        self.assertEqual(risk['top_risks'][0]['contract'], 'AI_USDT')
        self.assertEqual(risk['top_risks'][0]['initial_margin_usdt'], 35.0)

    def test_gate_cross_risk_does_not_publish_contract_mmr(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(
                gate_account={
                    'available': '4053.58',
                    'total': '4215.30',
                    'unrealised_pnl': '1.2',
                    'cross_initial_margin': '158.74',
                    'cross_order_margin': '0',
                    'cross_mmr': '1081.15384615',
                },
                gate_positions=[{
                    'contract': 'AI_USDT',
                    'size': '-100',
                    'initial_margin': '158.74',
                    'unrealised_pnl': '0.21',
                    'maintenance_margin': '3.9',
                    'mark_price': '0.138',
                    'liq_price': '9.9',
                }],
            ),
            AccountCapitalConfig(),
        )
        pnl = {
            'gate_realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'gate_fee_cost': 0.0,
            'window': {},
        }

        row = snapshotter._build_gate_row(datetime(2026, 7, 16, 19, 8, 9), pnl)
        risk = row['detail']['gate_cross_risk']

        self.assertAlmostEqual(risk['account_mmr_pct'], 108115.384615, places=5)
        self.assertNotIn('worst_contract', risk)
        self.assertNotIn('worst_contract_mmr_pct', risk)
        self.assertNotIn('position_equity_usdt', risk)
        self.assertNotIn('mmr_pct', risk['top_risks'][0])
        self.assertEqual(risk['top_risks'][0]['initial_margin_usdt'], 158.74)

    def test_gate_cross_risk_notification_uses_status_cooldown_bucket(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(),
            AccountCapitalConfig(
                gate_cross_warning_notify_cooldown_sec=3600,
                gate_cross_danger_notify_cooldown_sec=300,
            ),
        )
        risk = {
            'status': 'warning',
            'status_label': '预警',
            'position_count': 3,
            'account_mmr_pct': 420.12,
            'available_ratio_pct': 13.2,
            'margin_usage_pct': 55.5,
            'initial_margin_usdt': 24.68,
            'maintenance_margin_usdt': 12.34,
            'nearest_liq_contract': 'AI_USDT',
            'nearest_liq_distance_bps': 580.0,
            'thresholds': {
                'warning_mmr_pct': 500,
                'warning_liq_distance_bps': 600,
                'danger_mmr_pct': 300,
                'danger_liq_distance_bps': 300,
            },
        }

        item = snapshotter._build_gate_cross_risk_notification(
            datetime(2026, 7, 6, 11, 59, 59),
            risk,
        )

        self.assertEqual(item['title'], 'Gate 全仓风险预警')
        self.assertEqual(item['type'], 'warning')
        self.assertEqual(item['source'], 'gate_cross_risk')
        self.assertEqual(item['dedup_key'], 'gate_cross_risk:warning:20260706110000')
        self.assertIn('全仓MMR=420.12%', item['message'])
        self.assertIn('最近强平距离=580.00bps', item['message'])
        self.assertIn('初始保证金=24.68 USDT', item['message'])
        self.assertNotIn('最弱合约MMR', item['message'])
        self.assertIn('阈值=MMR≤500.00%,强平距离≤600.00bps', item['message'])

    def test_gate_cross_risk_danger_notification_uses_shorter_bucket(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(),
            AccountCapitalConfig(gate_cross_danger_notify_cooldown_sec=300),
        )
        risk = {
            'status': 'danger',
            'account_mmr_pct': 260,
            'nearest_liq_distance_bps': 120,
            'thresholds': {
                'danger_mmr_pct': 300,
                'danger_liq_distance_bps': 300,
            },
        }

        item = snapshotter._build_gate_cross_risk_notification(
            datetime(2026, 7, 6, 11, 59, 59),
            risk,
        )

        self.assertEqual(item['title'], 'Gate 全仓风险告急')
        self.assertEqual(item['type'], 'error')
        self.assertEqual(item['dedup_key'], 'gate_cross_risk:danger:20260706115500')

    def test_gate_cross_risk_safe_notification_is_silent(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())

        item = snapshotter._build_gate_cross_risk_notification(
            datetime(2026, 7, 6, 11, 59, 59),
            {'status': 'safe'},
        )

        self.assertIsNone(item)

    def test_gate_cross_risk_unknown_notification_includes_collection_health(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(),
            AccountCapitalConfig(gate_cross_unknown_notify_cooldown_sec=300),
        )

        item = snapshotter._build_gate_cross_risk_notification(
            datetime(2026, 7, 16, 11, 59, 59),
            {
                'status': 'unknown',
                'health_label': '部分异常',
                'account_age_sec': 0.2,
                'positions_age_sec': 6.4,
                'latency_ms': 120.5,
                'source': 'gate_account_api',
                'error': 'Gate positions: timeout',
            },
        )

        self.assertEqual(item['title'], 'Gate 全仓风险数据异常')
        self.assertEqual(item['type'], 'error')
        self.assertEqual(item['dedup_key'], 'gate_cross_risk:unknown:20260716115500')
        self.assertIn('数据健康=部分异常', item['message'])
        self.assertIn('账户数据年龄=0.20s', item['message'])
        self.assertIn('持仓数据年龄=6.40s', item['message'])
        self.assertIn('错误=Gate positions: timeout', item['message'])

    def test_gate_cross_risk_notifier_skips_same_second_level_bucket(self):
        notifier = GateCrossRiskNotifier(AccountCapitalConfig(
            gate_cross_warning_notify_cooldown_sec=3600,
        ))
        risk = {
            'status': 'warning',
            'thresholds': {
                'warning_mmr_pct': 500,
                'warning_liq_distance_bps': 600,
            },
        }

        with patch('calc.account_capital.upsert_popup_notification') as upsert:
            first = notifier.record(datetime(2026, 7, 16, 11, 1, 0), risk)
            duplicate = notifier.record(datetime(2026, 7, 16, 11, 2, 0), risk)

        self.assertEqual(first, 1)
        self.assertEqual(duplicate, 0)
        upsert.assert_called_once()

    def test_binance_bnb_notification_warns_when_fee_asset_value_is_low(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(),
            AccountCapitalConfig(binance_bnb_min_available_usdt=1.0),
        )
        binance = {
            'detail': {
                'bnb_fee_asset': {
                    'free': 0.001,
                    'price_usdt': 500,
                    'free_value_usdt': 0.5,
                },
            },
        }

        item = snapshotter._build_binance_bnb_notification(
            datetime(2026, 8, 7, 12, 0, 0),
            binance,
        )

        self.assertEqual(item['title'], 'Binance BNB 可用不足')
        self.assertEqual(item['type'], 'warning')
        self.assertEqual(item['source'], 'binance_bnb_balance')
        self.assertEqual(item['dedup_key'], 'binance_bnb_balance:low:20260807120000')
        self.assertIn('BNB可用价值=0.50 USDT', item['message'])
        self.assertIn('阈值=1.00 USDT', item['message'])
        self.assertEqual(item['payload']['free_bnb'], 0.001)
        self.assertEqual(item['payload']['free_value_usdt'], 0.5)

    def test_binance_bnb_notification_is_silent_when_balance_is_enough(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(),
            AccountCapitalConfig(binance_bnb_min_available_usdt=1.0),
        )
        binance = {
            'detail': {
                'bnb_fee_asset': {
                    'free': 0.01,
                    'price_usdt': 500,
                    'free_value_usdt': 5.0,
                },
            },
        }

        item = snapshotter._build_binance_bnb_notification(
            datetime(2026, 8, 7, 12, 0, 0),
            binance,
        )

        self.assertIsNone(item)

    def test_binance_bnb_notifier_skips_same_cooldown_bucket(self):
        notifier = BinanceBnbBalanceNotifier(AccountCapitalConfig(
            binance_bnb_notify_cooldown_sec=3600,
        ))
        binance = {
            'detail': {
                'bnb_fee_asset': {
                    'free': 0.001,
                    'price_usdt': 500,
                    'free_value_usdt': 0.5,
                },
            },
        }

        with patch('calc.account_capital.upsert_popup_notification') as upsert:
            first = notifier.record(datetime(2026, 8, 7, 12, 1, 0), binance)
            duplicate = notifier.record(datetime(2026, 8, 7, 12, 2, 0), binance)

        self.assertEqual(first, 1)
        self.assertEqual(duplicate, 0)
        upsert.assert_called_once()

    def test_total_equity_uses_corrected_gate_equity(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        binance = {
            'equity_usdt': 500.0,
            'available_usdt': 480.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 20.0,
            'unrealized_pnl_usdt': 0.0,
            'realized_pnl_usdt': 0.0,
            'funding_pnl_usdt': 0.0,
            'fee_cost_usdt': 0.0,
        }
        gate = {
            'equity_usdt': 127.5,
            'available_usdt': 100.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 20.0,
            'margin_used_usdt': 20.0,
            'unrealized_pnl_usdt': 7.5,
            'realized_pnl_usdt': 0.0,
            'funding_pnl_usdt': 0.0,
            'fee_cost_usdt': 0.0,
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
                    {'asset': 'BNB', 'free': '0.25', 'locked': '0.01', 'total': '0.26'},
                ],
                binance_prices={'BTC': 50000, 'BNB': 600},
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

        self.assertEqual(row['equity_usdt'], 5258.0)
        self.assertEqual(row['realized_pnl_usdt'], 12.5)
        self.assertEqual(row['fee_cost_usdt'], -0.8)
        self.assertEqual(row['total_pnl_usdt'], 11.7)
        self.assertEqual(row['detail']['binance_spot_realized']['closed_count'], 2)
        self.assertEqual(row['detail']['bnb_fee_asset']['free'], 0.25)
        self.assertEqual(row['detail']['bnb_fee_asset']['free_value_usdt'], 150.0)

    def test_binance_row_includes_cross_margin_risk_detail(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(
                binance_balances=[{'asset': 'USDT', 'free': '100', 'locked': '0', 'total': '100'}],
                margin_account={
                    'borrowEnabled': True,
                    'tradeEnabled': True,
                    'marginLevel': '2.7',
                    'totalAssetOfBtc': '0.12',
                    'totalLiabilityOfBtc': '0.04',
                    'totalNetAssetOfBtc': '0.08',
                    'userAssets': [
                        {
                            'asset': 'USDT',
                            'free': '5',
                            'locked': '0',
                            'borrowed': '1000',
                            'interest': '1.23',
                            'netAsset': '-996.23',
                        }
                    ],
                },
            ),
            AccountCapitalConfig(
                binance_margin_warning_level=3.0,
                binance_margin_min_open_level=2.5,
            ),
        )
        pnl = {
            'binance_realized_pnl': 0.0,
            'binance_fee_cost': 0.0,
            'window': {},
            'binance_spot_realized': {},
        }

        row = snapshotter._build_binance_row(datetime(2026, 6, 13, 12, 0, 0), pnl)
        margin = row['detail']['binance_cross_margin']

        self.assertEqual(margin['status'], 'warning')
        self.assertTrue(margin['open_allowed'])
        self.assertEqual(margin['marginLevel'], 2.7)
        self.assertEqual(margin['USDT']['borrowed'], 1000.0)
        self.assertEqual(margin['USDT']['interest'], 1.23)

    def test_total_pnl_includes_binance_spot_realized_pnl(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        binance = {
            'equity_usdt': 500.0,
            'available_usdt': 480.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 20.0,
            'unrealized_pnl_usdt': 0.0,
            'realized_pnl_usdt': 15.0,
            'funding_pnl_usdt': 0.0,
            'fee_cost_usdt': -0.8,
        }
        gate = {
            'equity_usdt': 120.0,
            'available_usdt': 80.0,
            'locked_usdt': 0.0,
            'position_value_usdt': 40.0,
            'margin_used_usdt': 40.0,
            'unrealized_pnl_usdt': -3.0,
            'realized_pnl_usdt': -35.0,
            'funding_pnl_usdt': 4.0,
            'fee_cost_usdt': -1.2,
        }
        pnl = {
            'realized_pnl': 999.0,
            'funding_pnl': 999.0,
            'fee_cost': 999.0,
            'total_pnl': 999.0,
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

    def test_strategy_summary_falls_back_to_exchange_prices_for_binance_floating_pnl(self):
        snapshotter = AccountCapitalSnapshotter(
            FakeCapitalExecutor(
                binance_prices={'BICO': 0.12, 'AI': 0.03},
                gate_positions=[],
            ),
            AccountCapitalConfig(),
        )
        snapshotter._load_strategy_positions = lambda start, end: [
            {
                'id': 1,
                'status': 'holding',
                'base_asset': 'BICO',
                'spot_open_qty': 1000,
                'spot_open_price': 0.10,
                'spot_open_amount': 100,
                'funding_total_pnl': 0,
            },
            {
                'id': 2,
                'status': 'holding',
                'base_asset': 'AI',
                'spot_open_qty': 500,
                'spot_open_price': 0.02,
                'spot_open_amount': 10,
                'funding_total_pnl': 0,
            },
        ]
        snapshotter._load_strategy_order_fee_summary = lambda position_ids: {
            'binance_fee_cost': 0.0,
            'gate_fee_cost': 0.0,
            'fee_cost': 0.0,
        }
        snapshotter._load_strategy_executed_close_pnl = lambda position_ids, positions: {}

        summary = snapshotter._load_strategy_pnl_summary(
            datetime(2026, 8, 9, 0, 0, 0),
            datetime(2026, 8, 9, 1, 0, 0),
        )

        self.assertAlmostEqual(summary['binance_spot_floating_pnl'], 25.0)
        self.assertAlmostEqual(summary['floating_pnl'], 25.0)

    def test_strategy_summary_includes_matched_partial_close_pnl(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        snapshotter._load_strategy_positions = lambda start, end: [{
            'id': 123,
            'status': 'holding',
            'base_asset': 'AI',
            'spot_open_qty': 6.0,
            'spot_open_price': 10.0,
            'spot_open_amount': 60.0,
            'funding_total_pnl': 0.0,
        }]
        snapshotter._load_strategy_order_fee_summary = lambda position_ids: {
            'binance_fee_cost': -0.03,
            'gate_fee_cost': -0.02,
            'fee_cost': -0.05,
        }
        snapshotter._load_strategy_executed_close_pnl = lambda position_ids, positions: {
            123: {
                'open_notional': 40.0,
                'spot_close_amount': 42.0,
                'realized_spot_pnl': 2.0,
                'realized_pnl': 3.2,
            },
        }
        snapshotter._load_strategy_floating_pnl_summary = lambda positions: {
            'binance_spot_floating_pnl': 0.0,
            'gate_future_floating_pnl': 0.0,
            'floating_pnl': 0.0,
            'floating_pnl_source': 'test',
        }

        summary = snapshotter._load_strategy_pnl_summary(
            datetime(2026, 8, 13, 0, 0, 0),
            datetime(2026, 8, 13, 1, 0, 0),
        )

        self.assertAlmostEqual(summary['realized_pnl'], 3.2)
        self.assertAlmostEqual(summary['gate_realized_pnl'], 1.2)
        self.assertEqual(summary['closed_count'], 0)
        self.assertEqual(summary['binance_spot_realized']['partial_close_count'], 1)
        self.assertAlmostEqual(summary['binance_spot_realized']['realized_pnl'], 2.0)

    def test_strategy_summary_preserves_closed_history_without_order_ledger(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        snapshotter._load_strategy_positions = lambda start, end: [{
            'id': 124,
            'status': 'closed',
            'base_asset': 'BANK',
            'spot_open_amount': 100.0,
            'spot_close_amount': 104.0,
            'future_open_qty': 10.0,
            'future_open_price': 10.2,
            'future_close_amount': 100.0,
            'realized_pnl': 6.0,
            'funding_total_pnl': 0.0,
        }]
        snapshotter._load_strategy_order_fee_summary = lambda position_ids: {
            'binance_fee_cost': 0.0,
            'gate_fee_cost': 0.0,
            'fee_cost': 0.0,
        }
        load_partial = MagicMock(return_value={
            124: {
                'open_notional': 50.0,
                'spot_close_amount': 99.0,
                'realized_spot_pnl': 49.0,
                'realized_pnl': 88.0,
            },
        })
        snapshotter._load_strategy_executed_close_pnl = load_partial
        snapshotter._load_strategy_floating_pnl_summary = lambda positions: {
            'binance_spot_floating_pnl': 0.0,
            'gate_future_floating_pnl': 0.0,
            'floating_pnl': 0.0,
            'floating_pnl_source': 'test',
        }

        summary = snapshotter._load_strategy_pnl_summary(
            datetime(2026, 8, 13, 0, 0, 0),
            datetime(2026, 8, 13, 1, 0, 0),
        )

        self.assertAlmostEqual(summary['realized_pnl'], 6.0)
        self.assertAlmostEqual(summary['binance_spot_realized']['realized_pnl'], 4.0)
        self.assertEqual(summary['closed_count'], 1)
        self.assertEqual(summary['binance_spot_realized']['partial_close_count'], 0)
        load_partial.assert_called_once_with([], [])

    def test_incomplete_realtime_floating_does_not_overwrite_exchange_fallback(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        snapshotter._load_strategy_pnl_summary = lambda start, end: {
            'realized_pnl': 0.0,
            'gate_realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'binance_fee_cost': 0.0,
            'gate_fee_cost': 0.0,
            'fee_cost': 0.0,
            'binance_spot_floating_pnl': 12.34,
            'gate_future_floating_pnl': -2.0,
            'floating_pnl': 10.34,
            'binance_spot_realized': {
                'closed_count': 0,
                'open_amount': 0.0,
                'close_amount': 0.0,
                'realized_pnl': 0.0,
            },
            'gate_strategy_realized': {
                'realized_pnl': 0.0,
                'derived_from': 'strategy_realized_pnl - binance_spot_realized_pnl',
            },
        }

        summary = snapshotter._load_exchange_pnl_summary(
            datetime(2026, 8, 9, 1, 0, 0),
            {
                'position_count': 5,
                'pnl_rows': 0,
                'missing_realtime_rows': 5,
                'binance_spot_floating_pnl': 0.0,
                'gate_future_floating_pnl': 0.0,
                'floating_pnl': 0.0,
            },
        )

        self.assertEqual(summary['binance_spot_floating_pnl'], 12.34)
        self.assertEqual(summary['floating_pnl'], 10.34)

    def test_complete_realtime_floating_overwrites_exchange_fallback(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        snapshotter._load_strategy_pnl_summary = lambda start, end: {
            'realized_pnl': 0.0,
            'gate_realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'binance_fee_cost': 0.0,
            'gate_fee_cost': 0.0,
            'fee_cost': 0.0,
            'binance_spot_floating_pnl': 12.34,
            'gate_future_floating_pnl': -2.0,
            'floating_pnl': 10.34,
            'binance_spot_realized': {
                'closed_count': 0,
                'open_amount': 0.0,
                'close_amount': 0.0,
                'realized_pnl': 0.0,
            },
            'gate_strategy_realized': {
                'realized_pnl': 0.0,
                'derived_from': 'strategy_realized_pnl - binance_spot_realized_pnl',
            },
        }

        summary = snapshotter._load_exchange_pnl_summary(
            datetime(2026, 8, 9, 1, 0, 0),
            {
                'position_count': 5,
                'pnl_rows': 5,
                'missing_realtime_rows': 0,
                'binance_spot_floating_pnl': 1.5,
                'gate_future_floating_pnl': -0.5,
                'floating_pnl': 1.0,
            },
        )

        self.assertEqual(summary['binance_spot_floating_pnl'], 1.5)
        self.assertEqual(summary['floating_pnl'], 1.0)

    def test_position_strategy_realized_pnl_uses_basis_and_actual_open_amount(self):
        snapshotter = AccountCapitalSnapshotter(FakeCapitalExecutor(), AccountCapitalConfig())
        pnl = snapshotter._position_strategy_realized_pnl({
            'open_spread_bps': 30.0,
            'close_spread_bps': 45.0,
            'spot_open_amount': 10.0,
            'spot_close_amount': 12.0,
            'future_open_qty': 100.0,
            'future_open_price': 0.1,
            'future_close_amount': 12.2,
        })

        self.assertAlmostEqual(pnl, -0.015)


if __name__ == '__main__':
    unittest.main()
