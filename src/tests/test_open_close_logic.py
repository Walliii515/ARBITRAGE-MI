# coding: utf-8
"""
开仓 / 平仓核心逻辑分支测试

覆盖目标：
  TradingExecutor:
    - _pass_peak_check（首次/更新峰值/超时直开候选/未回落/sustain不足/通过）
    - _pass_open_resiliency_check（盘口恢复等待/通过/超时拒绝）
    - _pre_execution_gate（manager未注入/lag拦截/基差衰减/盈利性守卫/覆盖超限/通过）
  ClosingExecutor:
    - _pass_valley_check（首次/更新谷底/超时通过/谷底>=open异常通过/反弹通过/未达标）
    - _pass_close_resiliency_check（止盈平仓盘口恢复等待/超时放行）
    - _pre_execution_gate（manager未注入/lag拦截/收敛逆转/回弹过大/通过）

设计原则：
  1) 直接构造对象、注入 fake manager，避免起服务
  2) DB / 外部 API 全部 mock，杜绝副作用
  3) 时间用 datetime.now() + 偏移构造，sustain/超时通过修改 state['start_time'] 模拟
  4) lag 通过 last_update_time = time.time() - 偏移秒 模拟

运行：
    cd src && python3 -m pytest tests/test_open_close_logic.py -v
或：
    cd src && python3 -m unittest tests.test_open_close_logic -v
"""
import os
import sys
import time
import json
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ──────────────────────────────────────────────────────────────────
# Fake OrderBook / Manager（仅暴露 _pre_execution_gate / _get_orderbook_update_counts 用到的属性）
# ──────────────────────────────────────────────────────────────────

class FakeOrderBook:
    """模拟 LocalOrderBook，仅承载本测试关心的属性。"""

    def __init__(self, last_update_time=None, update_count=0, row=None):
        # last_update_time = None 表示从未收到（lag = inf）
        self.last_update_time = (
            time.time() if last_update_time is None else last_update_time
        )
        self.update_count = update_count
        self._row = row or {}

    def to_dict_row(self):
        return dict(self._row)


class FakeManager:
    """模拟 OrderBookManager，按 key 取盘口。"""

    def __init__(self, books=None):
        self._books = books or {}

    def get_orderbook(self, key):
        return self._books.get(key)


# ──────────────────────────────────────────────────────────────────
# 公共 fixture：构造可独立测试的 TradingExecutor / ClosingExecutor
# ──────────────────────────────────────────────────────────────────

def fresh_safe_gate_summary(available: float, net_value: float) -> dict:
    return {
        'available': available,
        'net_value': net_value,
        'cross_risk': {
            'status': 'safe',
            'account_mmr_pct': 1200.0,
            'account_fetched_at_ts': time.time(),
        },
    }


def make_trading_executor(sustain_sec=2.0, peak_pullback_pct=0.10,
                          peak_monitor_timeout_sec=60,
                          basis_threshold_bps=-60,
                          coverage_threshold=0.8,
                          min_funding_rate_bps=-6.0,
                          min_funding_support_bps=None,
                          funding_support_min_samples=2,
                          realtime_min_funding_rate_bps=None,
                          max_orderbook_lag_ms=200.0,
                          vwap_threshold_meta=None,
                          close_vwap_threshold_meta=None,
                          asset_tier_meta=None,
                          momentum_enabled=False,
                          momentum_allowed_tiers=None,
                          momentum_tier_overrides=None,
                          rebound_enabled=True,
                          rebound_allowed_tiers=None,
                          funding_entry_enabled=True,
                          funding_carry_enabled=False,
                          min_available_ratio=0.10,
                          min_binance_available_ratio=None,
                          min_gate_available_ratio=None,
                          max_asset_exposure_ratio=0.15,
                          quality_scale_in_enabled=False,
                          quality_scale_in_enhanced_ratio=0.25,
                          quality_scale_in_min_funding_24h_bps=50.0,
                          quality_scale_in_min_basis_improvement_bps=8.0,
                          quality_scale_in_basis_improvement_ratio=0.25,
                          quality_scale_in_max_basis_improvement_bps=20.0,
                          quality_scale_in_cooldown_sec=300,
                          high_basis_enabled=False,
                          high_basis_amount_multiplier=0.5,
                          high_basis_min_funding_24h_bps=3.0,
                          high_basis_min_entry_buffer_bps=25.0,
                          high_basis_min_net_edge_bps=20.0,
                          high_basis_scale_in_min_basis_improvement_bps=20.0,
                          presignal_reject_log_cooldown_sec=300,
                          contract_meta=None,
                          spot_meta=None,
                          asset_profile_meta=None):
    """构造独立的 TradingExecutor 实例（不依赖 DB / API）"""
    from calc.trading_executor import TradingExecutor, TradingExecutorConfig

    cfg = TradingExecutorConfig(
        sustain_sec=sustain_sec,
        peak_pullback_pct=peak_pullback_pct,
        peak_monitor_timeout_sec=peak_monitor_timeout_sec,
        basis_threshold_bps=basis_threshold_bps,
        coverage_threshold=coverage_threshold,
        min_funding_rate_bps=min_funding_rate_bps,
        min_funding_support_bps=min_funding_support_bps,
        funding_support_min_samples=funding_support_min_samples,
        realtime_min_funding_rate_bps=realtime_min_funding_rate_bps,
        max_orderbook_lag_ms=max_orderbook_lag_ms,
        reduced_open_amount_multiplier=0.6,
        momentum_enabled=momentum_enabled,
        momentum_allowed_tiers=momentum_allowed_tiers or ['A'],
        momentum_tier_overrides=momentum_tier_overrides or {},
        rebound_enabled=rebound_enabled,
        rebound_allowed_tiers=rebound_allowed_tiers or ['A', 'B'],
        funding_entry_enabled=funding_entry_enabled,
        funding_carry_enabled=funding_carry_enabled,
        min_available_ratio=min_available_ratio,
        min_binance_available_ratio=min_binance_available_ratio,
        min_gate_available_ratio=min_gate_available_ratio,
        max_asset_exposure_ratio=max_asset_exposure_ratio,
        quality_scale_in_enabled=quality_scale_in_enabled,
        quality_scale_in_enhanced_ratio=quality_scale_in_enhanced_ratio,
        quality_scale_in_min_funding_24h_bps=quality_scale_in_min_funding_24h_bps,
        quality_scale_in_min_basis_improvement_bps=quality_scale_in_min_basis_improvement_bps,
        quality_scale_in_basis_improvement_ratio=quality_scale_in_basis_improvement_ratio,
        quality_scale_in_max_basis_improvement_bps=quality_scale_in_max_basis_improvement_bps,
        quality_scale_in_cooldown_sec=quality_scale_in_cooldown_sec,
        high_basis_enabled=high_basis_enabled,
        high_basis_amount_multiplier=high_basis_amount_multiplier,
        high_basis_min_funding_24h_bps=high_basis_min_funding_24h_bps,
        high_basis_min_entry_buffer_bps=high_basis_min_entry_buffer_bps,
        high_basis_min_net_edge_bps=high_basis_min_net_edge_bps,
        high_basis_scale_in_min_basis_improvement_bps=(
            high_basis_scale_in_min_basis_improvement_bps
        ),
        presignal_reject_log_cooldown_sec=presignal_reject_log_cooldown_sec,
        funding_carry_allowed_tiers=['A', 'B'],
        funding_carry_min_24h_bps=30.0,
        funding_carry_basis_relax_bps=15.0,
        funding_carry_max_next_funding_min=30.0,
        thin_bursty_max_orderbook_lag_ms=1500.0,
        thin_bursty_max_book_skew_ms=1500.0,
    )
    te = TradingExecutor(
        cfg, contract_meta=contract_meta or {}, spot_meta=spot_meta or {},
        vwap_threshold_meta=vwap_threshold_meta,
        close_vwap_threshold_meta=close_vwap_threshold_meta,
        asset_tier_meta=asset_tier_meta,
        asset_profile_meta=asset_profile_meta,
    )
    return te


def make_closing_executor():
    """构造独立的 ClosingExecutor 实例（不依赖 DB；config 用真实 yaml 即可，本测试只关心方法逻辑）"""
    from calc.closing_executor import ClosingExecutor
    return ClosingExecutor(contract_meta={}, spot_meta={}, funding_rate_p40_meta={})


def make_gate_cross_risk(
    *,
    status='safe',
    account_mmr_pct=1000.0,
    age_sec=0.0,
    positions_age_sec=None,
    **overrides,
):
    now = time.time()
    if positions_age_sec is None:
        positions_age_sec = age_sec
    risk = {
        'status': status,
        'account_mmr_pct': account_mmr_pct,
        'account_fetched_at_ts': now - age_sec,
        'positions_fetched_at_ts': now - positions_age_sec,
    }
    risk.update(overrides)
    return risk


class TestAssetReductionOwnership(unittest.TestCase):
    def test_closing_client_timeout_covers_internal_retry_window(self):
        ce = make_closing_executor()
        self.assertEqual(ce.executor_client.timeout, 120)

    def test_closing_skips_asset_owned_by_reconciliation_thread(self):
        from calc.asset_reduction_guard import asset_reduction_guard

        ce = make_closing_executor()
        ce.executor_client.execute = MagicMock()
        claimed = threading.Event()
        release = threading.Event()

        def hold_reconciliation_claim():
            with asset_reduction_guard.claim('TUT', 'reconciliation-test') as acquired:
                self.assertTrue(acquired)
                claimed.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_reconciliation_claim)
        holder.start()
        self.assertTrue(claimed.wait(timeout=1))
        try:
            result = ce._execute_close(
                {'id': 318, 'base_asset': 'TUT'},
                'margin_close',
                'MMR风险退出',
                {},
            )
        finally:
            release.set()
            holder.join(timeout=2)

        self.assertFalse(result['success'])
        self.assertIn('已有减仓执行', result['message'])
        ce.executor_client.execute.assert_not_called()

    def test_guard_allows_different_assets_concurrently(self):
        from calc.asset_reduction_guard import asset_reduction_guard

        claimed = threading.Event()
        release = threading.Event()

        def hold_tut():
            with asset_reduction_guard.claim('TUT', 'tut-close') as acquired:
                self.assertTrue(acquired)
                claimed.set()
                release.wait(timeout=2)

        holder = threading.Thread(target=hold_tut)
        holder.start()
        self.assertTrue(claimed.wait(timeout=1))
        try:
            with asset_reduction_guard.claim('BEL', 'bel-close') as acquired:
                self.assertTrue(acquired)
                self.assertEqual(asset_reduction_guard.owner('BEL'), 'bel-close')
        finally:
            release.set()
            holder.join(timeout=2)

    def test_guard_is_reentrant_and_releases_after_outer_scope(self):
        from calc.asset_reduction_guard import asset_reduction_guard

        with asset_reduction_guard.claim('TUT', 'outer') as outer:
            self.assertTrue(outer)
            with asset_reduction_guard.claim('TUT', 'inner') as inner:
                self.assertTrue(inner)
                self.assertEqual(asset_reduction_guard.owner('TUT'), 'outer')
            self.assertEqual(asset_reduction_guard.owner('TUT'), 'outer')
        self.assertIsNone(asset_reduction_guard.owner('TUT'))

    def test_guard_releases_when_execution_raises(self):
        from calc.asset_reduction_guard import asset_reduction_guard

        with self.assertRaisesRegex(RuntimeError, 'exchange down'):
            with asset_reduction_guard.claim('TUT', 'failing-close') as acquired:
                self.assertTrue(acquired)
                raise RuntimeError('exchange down')
        self.assertIsNone(asset_reduction_guard.owner('TUT'))

    def test_closing_executor_releases_guard_when_executor_client_raises(self):
        from calc.asset_reduction_guard import asset_reduction_guard

        ce = make_closing_executor()
        ce.executor_client.execute = MagicMock(side_effect=RuntimeError('executor unavailable'))
        pos = {
            'id': 318,
            'base_asset': 'TUT',
            'spot_open_qty': 100.0,
            'spot_open_price': 0.14,
            'future_open_qty': 100.0,
            'future_open_price': 0.141,
            'future_open_contracts': 100.0,
            'future_contract': 'TUT_USDT',
        }

        with self.assertRaisesRegex(RuntimeError, 'executor unavailable'):
            ce._execute_close(pos, 'margin_close', 'MMR风险退出', {})

        self.assertIsNone(asset_reduction_guard.owner('TUT'))


# ══════════════════════════════════════════════════════════════════
# TradingExecutor 测试
# ══════════════════════════════════════════════════════════════════

class TestGateLocalOrderBookSequencing(unittest.TestCase):
    """Gate 本地订单簿使用 OBU full 快照 + 连续增量维护。"""

    def _full(self, book_id=100):
        return {
            'full': True,
            'u': book_id,
            't': 1,
            'a': [{'p': '101', 's': '1'}],
            'b': [{'p': '99', 's': '1'}],
        }

    def test_obu_full_snapshot_marks_book_ready(self):
        from calc.create_gate_futures_local_orderbook import LocalOrderBook

        ob = LocalOrderBook('BTC_USDT', 'BTC')
        ok = ob.apply_obu(self._full(100))

        self.assertTrue(ok)
        self.assertTrue(ob.is_ready())
        self.assertEqual(ob.id, 100)
        self.assertEqual(ob.update_count, 1)

    def test_continuous_obu_update_is_applied(self):
        from calc.create_gate_futures_local_orderbook import LocalOrderBook

        ob = LocalOrderBook('BTC_USDT', 'BTC')
        ob.apply_obu(self._full(100))

        ok = ob.apply_obu({
            'full': False,
            'U': 101,
            'u': 101,
            't': 2,
            'a': [{'p': '101', 's': '0'}],
            'b': [{'p': '100', 's': '2'}],
        })

        self.assertTrue(ok)
        self.assertTrue(ob.is_ready())
        self.assertEqual(ob.id, 101)
        self.assertEqual(ob.update_count, 2)

    def test_obu_update_with_gap_marks_book_not_ready(self):
        from calc.create_gate_futures_local_orderbook import LocalOrderBook

        ob = LocalOrderBook('BTC_USDT', 'BTC')
        ob.apply_obu(self._full(100))

        ok = ob.apply_obu({
            'full': False,
            'U': 103,
            'u': 103,
            't': 2,
            'a': [],
            'b': [{'p': '100', 's': '2'}],
        })

        self.assertFalse(ok)
        self.assertFalse(ob.is_ready())
        self.assertEqual(ob.id, 100)
        self.assertEqual(ob.update_count, 0)


class TestGateOrderBookManagerObu(unittest.TestCase):
    """Gate manager 缺口时不再 REST 重载，只触发 OBU 重订阅。"""

    def test_gap_schedules_resubscribe(self):
        from calc.create_gate_futures_local_orderbook import OrderBookManager

        manager = OrderBookManager(settle='usdt')
        manager.prepare_contracts(['BTC_USDT'])
        manager._schedule_resubscribe = MagicMock()

        manager._handle_ws_update('BTC_USDT', {
            'full': True,
            'u': 100,
            't': 1,
            'a': [{'p': '101', 's': '1'}],
            'b': [{'p': '99', 's': '1'}],
        })
        manager._handle_ws_update('BTC_USDT', {
            'full': False,
            'U': 103,
            'u': 103,
            't': 2,
            'a': [],
            'b': [],
        })

        self.assertFalse(manager.get_orderbook('BTC_USDT').is_ready())
        manager._schedule_resubscribe.assert_called_once_with('BTC_USDT')


class TestFutureMakerOpenConfig(unittest.TestCase):
    """开仓订单生成：实盘 A/B 档可带 future maker 执行参数。"""

    def test_live_ab_asset_adds_future_maker_order_params(self):
        te = make_trading_executor(
            asset_tier_meta={'ASR': 'B'},
            contract_meta={'ASR': {'quanto_multiplier': 1}},
            spot_meta={'ASR': {'step_size': 1}},
        )
        te.executor_client.channel = 'Live'
        te.future_maker_open_enabled = True
        te.future_maker_open_allowed_tiers = {'A', 'B'}
        te.future_maker_open_ttl_ms = 800

        group = te._create_order_group({
            'base_asset': 'ASR',
            'contract': 'ASR_USDT',
            'spot_qty': 10,
            'open_amount_usdt': 10,
            'future_price_ask_1': 1.0123,
            'future_open_vwap': 1.01,
            'spot_open_vwap': 1.0,
        })

        future_order = group['future_order']
        self.assertEqual(future_order.get('execution_style'), 'maker')
        self.assertEqual(future_order.get('maker_ttl_ms'), 800)
        self.assertEqual(future_order.get('maker_price'), 1.0123)
        self.assertEqual(future_order.get('maker_strategy_tier'), 'B')
        self.assertEqual(future_order.get('maker_taker_reference_price'), 1.01)
        self.assertTrue(future_order.get('maker_spot_hedge_protective_ioc_enabled'))
        self.assertIsNotNone(future_order.get('maker_spot_hedge_min_basis_bps'))

    def test_mock_channel_keeps_plain_taker_order(self):
        te = make_trading_executor(asset_tier_meta={'ASR': 'B'})
        te.executor_client.channel = 'Mock'
        te.future_maker_open_enabled = True
        te.future_maker_open_allowed_tiers = {'A', 'B'}

        group = te._create_order_group({
            'base_asset': 'ASR',
            'contract': 'ASR_USDT',
            'spot_qty': 10,
            'future_price_ask_1': 1.0123,
        })

        self.assertNotIn('execution_style', group['future_order'])

    def test_create_position_persists_open_funding_snapshot(self):
        te = make_trading_executor(contract_meta={'ASR': {'quanto_multiplier': 1}})
        group = te._create_order_group({
            'base_asset': 'ASR',
            'contract': 'ASR_USDT',
            'spot_qty': 10,
            'open_amount_usdt': 10,
            'funding_rate_24h': 0.00123,
        })
        exec_result = {
            'spot_order': {'exec_price': 1.0, 'exec_qty': 10, 'exec_amount': 10},
            'future_order': {'exec_price': 1.01, 'exec_qty': 10, 'exec_amount': 10.1},
        }
        cursor = MagicMock()
        cursor.lastrowid = 42
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('calc.trading_executor.db_manager.get_cursor', return_value=context):
            position_id = te._create_position(group, exec_result)

        self.assertEqual(position_id, 42)
        sql, params = cursor.execute.call_args.args
        self.assertIn('open_funding_rate_24h', sql)
        self.assertEqual(params['open_funding_rate_24h'], 0.00123)


class TestRealExecutorGateParsing(unittest.TestCase):
    """Gate 成交解析：部分成交按 size-left 计算。"""

    def test_partial_fill_uses_size_minus_left(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        parsed = executor._parse_gate_response(
            {
                'id': '123',
                'status': 'finished',
                'size': '-10',
                'left': '-4',
                'fill_price': '1.25',
            },
            quanto_multiplier=2,
            allow_partial=True,
        )

        self.assertTrue(parsed['success'])
        self.assertEqual(parsed['exec_qty'], 12)
        self.assertEqual(parsed['exec_amount'], 15.0)

    def test_binance_dust_conversion_returns_auditable_execution_value(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._binance_signed_post = MagicMock(return_value={
            'transferResult': [{
                'fromAsset': 'BICO',
                'amount': '0.2',
                'transferedAmount': '0.00000955',
                'serviceChargeAmount': '0.00000019',
                'tranId': 12345,
            }],
        })
        executor._get_binance_usdt_price = MagicMock(return_value=800.0)

        result = executor.convert_binance_spot_dust_to_bnb('bico')

        self.assertTrue(result['success'])
        self.assertEqual(result['source_qty'], 0.2)
        self.assertEqual(result['transaction_id'], '12345')
        self.assertAlmostEqual(result['exec_amount_usdt'], 0.00764)
        self.assertAlmostEqual(result['exec_price_usdt'], 0.0382)
        self.assertAlmostEqual(result['service_charge_usdt'], 0.000152)
        self.assertAlmostEqual(result['gross_exec_amount_usdt'], 0.007792)
        self.assertAlmostEqual(result['gross_exec_price_usdt'], 0.03896)
        executor._binance_signed_post.assert_called_once_with(
            '/sapi/v1/asset/dust-convert/convert',
            {'asset': ['BICO'], 'accountType': 'SPOT', 'targetAsset': 'BNB'},
        )

    def test_binance_dust_conversion_batch_returns_asset_results(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._binance_signed_post = MagicMock(return_value={
            'transferResult': [
                {
                    'fromAsset': 'BICO',
                    'amount': '0.2',
                    'transferedAmount': '0.00000955',
                    'serviceChargeAmount': '0.00000019',
                    'tranId': 12345,
                },
                {
                    'fromAsset': 'FRAX',
                    'amount': '0.18',
                    'transferedAmount': '0.000006',
                    'serviceChargeAmount': '0.00000012',
                    'tranId': 12346,
                },
            ],
        })
        executor._get_binance_usdt_price = MagicMock(return_value=800.0)

        result = executor.convert_binance_spot_dust_to_bnb_batch(['frax', 'bico'])

        self.assertTrue(result['success'])
        self.assertEqual(result['converted_assets'], ['BICO', 'FRAX'])
        self.assertEqual(result['results']['BICO']['transaction_id'], '12345')
        self.assertEqual(result['results']['FRAX']['source_qty'], 0.18)
        executor._binance_signed_post.assert_called_once_with(
            '/sapi/v1/asset/dust-convert/convert',
            {'asset': ['BICO', 'FRAX'], 'accountType': 'SPOT', 'targetAsset': 'BNB'},
        )

    def test_gate_ioc_zero_fill_is_no_fill_not_data_error(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        parsed = executor._parse_gate_response(
            {
                'id': '123',
                'status': 'finished',
                'size': '-10',
                'left': '-10',
                'fill_price': '0',
                'finish_as': 'ioc',
            },
            quanto_multiplier=1,
        )

        self.assertFalse(parsed['success'])
        self.assertIn('IOC未成交', parsed['reason'])
        self.assertNotIn('成交数据异常', parsed['reason'])

    def test_maker_cancel_not_found_reconciles_full_fill(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._cancel_gate_futures_order = MagicMock(return_value=None)
        executor._reconcile_gate_maker_terminal_state = MagicMock(return_value=(
            {
                'id': 'maker-26',
                'status': 'finished',
                'size': '-26',
                'left': '0',
                'fill_price': '0.018692',
            },
            True,
        ))

        result = executor._wait_gate_maker_fill(
            order={
                'base_asset': 'TUT',
                'future_contract': 'TUT_USDT',
                'trade_direction': 'sell',
                'order_side': 'open',
                'maker_ttl_ms': 0,
            },
            initial_order={
                'id': 'maker-26',
                'status': 'open',
                'size': '-26',
                'left': '-26',
                'fill_price': '0',
            },
            quanto_multiplier=1,
            requested_contracts=26,
            maker_price=0.018692,
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['exec_qty'], 26)
        maker = result['execution_stats']['future_maker']
        self.assertEqual(maker['filled_contracts'], 26)
        self.assertTrue(maker['terminal_confirmed'])
        self.assertFalse(maker['fill_state_uncertain'])

    def test_maker_terminal_recheck_reconstructs_matching_trades(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._get_gate_futures_order = MagicMock(return_value=None)
        executor.fetch_gate_futures_my_trades = MagicMock(return_value=[
            {'order_id': 'maker-26', 'size': '-13', 'price': '0.018692'},
            {'order_id': 'other', 'size': '-99', 'price': '0.02'},
            {'order_id': 'maker-26', 'size': '-13', 'price': '0.018694'},
        ])

        reconciled, terminal = executor._reconcile_gate_maker_terminal_state(
            contract='TUT_USDT',
            order_id='maker-26',
            initial_order={
                'id': 'maker-26',
                'status': 'open',
                'size': '-26',
                'left': '-26',
            },
            requested_contracts=26,
        )

        self.assertTrue(terminal)
        self.assertEqual(reconciled['status'], 'finished')
        self.assertEqual(reconciled['left'], 0)
        self.assertAlmostEqual(float(reconciled['fill_price']), 0.018693)

    def test_maker_unknown_terminal_state_blocks_duplicate_fallback(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': False,
            'reason': 'future maker未成交(fill=0%,终态未确认)',
            'execution_stats': {
                'future_maker': {
                    'attempted': True,
                    'filled': False,
                    'fill_state_uncertain': True,
                }
            },
        })
        executor._place_binance_spot_order = MagicMock()

        result = executor.execute({
            'spot_order': {
                'base_asset': 'TUT',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 2600,
                'target_amount': 50,
            },
            'future_order': {
                'base_asset': 'TUT',
                'future_contract': 'TUT_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_fallback_ioc_enabled': True,
                'maker_fallback_protective_price': 0.01869,
                'target_qty': 2600,
                'target_amount': 50,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertEqual(executor._place_gate_futures_order.call_count, 1)
        executor._place_binance_spot_order.assert_not_called()
        self.assertIn('禁止fallback', result['message'])

    def test_maker_partial_fill_fallback_only_submits_remaining_contracts(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(side_effect=[
            {
                'success': True,
                'exec_price': 0.018692,
                'exec_qty': 1000.0,
                'exec_amount': 18.692,
                'coverage_ratio': 0,
                'exchange_order_id': 'maker-10',
                'execution_stats': {
                    'future_maker': {
                        'attempted': True,
                        'filled': True,
                        'fill_ratio': 10 / 26,
                        'requested_contracts': 26,
                        'filled_contracts': 10,
                        'terminal_confirmed': True,
                        'fill_state_uncertain': False,
                    }
                },
            },
            {
                'success': True,
                'exec_price': 0.018700,
                'exec_qty': 1600.0,
                'exec_amount': 29.92,
                'coverage_ratio': 0,
                'exchange_order_id': 'fallback-16',
            },
        ])
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.0185,
            'exec_qty': 2600.0,
            'exec_amount': 48.1,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'spot_order': {
                'base_asset': 'TUT',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 2600,
                'target_amount': 48.1,
            },
            'future_order': {
                'base_asset': 'TUT',
                'future_contract': 'TUT_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_fallback_ioc_enabled': True,
                'maker_fallback_protective_price': 0.01869,
                'target_qty': 2600,
                'target_amount': 48.612,
            },
        }, {})

        self.assertTrue(result['success'])
        self.assertEqual(executor._place_gate_futures_order.call_count, 2)
        fallback_order = executor._place_gate_futures_order.call_args_list[1].args[0]
        self.assertEqual(fallback_order['target_qty'], 1600)
        self.assertAlmostEqual(fallback_order['target_amount'], 48.612 * 16 / 26)
        hedge_order = executor._place_binance_spot_order.call_args.args[0]
        self.assertEqual(hedge_order['target_qty'], 2600)
        self.assertEqual(result['future_order']['exec_qty'], 2600)
        self.assertEqual(
            result['execution_stats']['future_maker']['fallback_remaining_contracts'],
            16,
        )

    def test_gate_open_sets_leverage_when_contract_has_no_position(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(gate_base_url='https://gate.test'), leverage=10)
        executor.fetch_gate_futures_positions = MagicMock(return_value=[])
        executor._gate_sign = MagicMock(return_value={})
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '{}'
        executor._session = MagicMock()
        executor._session.post.return_value = resp

        ok, reason = executor._ensure_leverage('AI_USDT')

        self.assertTrue(ok, reason)
        called_url = executor._session.post.call_args.args[0]
        self.assertIn('/positions/AI_USDT/leverage', called_url)
        self.assertIn('leverage=10', called_url)

    def test_gate_open_skips_leverage_change_when_existing_position_matches(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(gate_base_url='https://gate.test'), leverage=10)
        executor.fetch_gate_futures_positions = MagicMock(return_value=[
            {'contract': 'AI_USDT', 'size': -10, 'leverage': '10'},
        ])
        executor._session = MagicMock()

        ok, reason = executor._ensure_leverage('AI_USDT')

        self.assertTrue(ok, reason)
        executor._session.post.assert_not_called()

    def test_forward_execute_rejects_before_spot_when_existing_leverage_differs(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(gate_base_url='https://gate.test'), leverage=10)
        executor.fetch_gate_futures_positions = MagicMock(return_value=[
            {'contract': 'AI_USDT', 'size': -10, 'leverage': '5'},
        ])
        executor._place_binance_spot_order = MagicMock()
        executor._place_gate_futures_order = MagicMock()

        result = executor.execute({
            'spot_order': {'base_asset': 'AI', 'trade_direction': 'buy'},
            'future_order': {
                'base_asset': 'AI',
                'future_contract': 'AI_USDT',
                'order_side': 'open',
                'trade_direction': 'sell',
                'target_qty': 10,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertIn('Gate已有仓位，禁止修改保证金模式', result['message'])
        executor._place_binance_spot_order.assert_not_called()
        executor._place_gate_futures_order.assert_not_called()

    def test_take_profit_protective_ioc_hedges_gate_partial_fill_by_position_ratio(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        calls = []
        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})

        def gate_order(order):
            calls.append('future')
            self.assertEqual(order['protective_price'], 0.119)
            return {
                'success': True,
                'exec_price': 0.12092,
                'exec_qty': 400.0,
                'exec_amount': 48.368,
                'coverage_ratio': 0,
            }

        def spot_order(order):
            calls.append('spot')
            self.assertEqual(order['target_qty'], 396.0)
            self.assertEqual(order['target_amount'], 48.368)
            self.assertEqual(order['quantity_mode'], 'base')
            return {
                'success': True,
                'exec_price': 0.1196,
                'exec_qty': 396.0,
                'exec_amount': 47.3616,
                'coverage_ratio': 0,
            }

        executor._place_gate_futures_order = MagicMock(side_effect=gate_order)
        executor._place_binance_spot_order = MagicMock(side_effect=spot_order)

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'execution_reason': 'take_profit',
            'spot_order': {
                'base_asset': 'BEL',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 488.07,
            },
            'future_order': {
                'base_asset': 'BEL',
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 493.0,
                'protective_price': 0.119,
            },
        }, {})

        self.assertTrue(result['success'])
        self.assertEqual(calls, ['future', 'spot'])

    def test_take_profit_protective_ioc_zero_fill_does_not_touch_spot(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': False,
            'reason': 'IOC未成交(fill=0, finish_as=ioc)',
        })
        executor._place_binance_spot_order = MagicMock()

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'execution_reason': 'take_profit',
            'spot_order': {
                'base_asset': 'EPIC',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 297.0,
            },
            'future_order': {
                'base_asset': 'EPIC',
                'future_contract': 'EPIC_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 297.0,
                'protective_price': 0.69195,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertIn('未执行现货', result['message'])
        executor._place_binance_spot_order.assert_not_called()

    def test_take_profit_partial_fill_allows_one_spot_step_truncation(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(),
            contract_meta={},
            spot_meta={'BEL': {'step_size': 0.1}},
        )
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.12092,
            'exec_qty': 400.0,
            'exec_amount': 48.368,
            'coverage_ratio': 0,
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.1196,
            'exec_qty': 395.9,
            'exec_amount': 47.34964,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'execution_reason': 'take_profit',
            'spot_order': {
                'base_asset': 'BEL',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 488.0,
            },
            'future_order': {
                'base_asset': 'BEL',
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 493.0,
                'protective_price': 0.119,
            },
        }, {})

        self.assertTrue(result['success'])
        requested_spot_qty = (
            executor._place_binance_spot_order.call_args.args[0]['target_qty']
        )
        self.assertAlmostEqual(requested_spot_qty, 488.0 * 400.0 / 493.0)

    def test_margin_close_strips_maker_and_protective_fields_before_gate(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.12092,
            'exec_qty': 493.0,
            'exec_amount': 59.61,
            'coverage_ratio': 0,
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.1196,
            'exec_qty': 493.0,
            'exec_amount': 58.96,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'execution_reason': 'margin_close',
            'spot_order': {
                'base_asset': 'BEL',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 493.0,
            },
            'future_order': {
                'base_asset': 'BEL',
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 493.0,
                'execution_style': 'maker',
                'protective_price': 0.119,
            },
        }, {})

        self.assertTrue(result['success'])
        gate_order = executor._place_gate_futures_order.call_args.args[0]
        self.assertNotIn('execution_style', gate_order)
        self.assertNotIn('protective_price', gate_order)

    def test_negative_funding_opportunistic_close_keeps_protective_price(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.12092,
            'exec_qty': 493.0,
            'exec_amount': 59.61,
            'coverage_ratio': 0,
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.1196,
            'exec_qty': 493.0,
            'exec_amount': 58.96,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'execution_reason': 'negative_funding_exit',
            'allow_protective_close': True,
            'spot_order': {
                'base_asset': 'BEL',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 493.0,
            },
            'future_order': {
                'base_asset': 'BEL',
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 493.0,
                'protective_price': 0.121,
            },
        }, {})

        self.assertTrue(result['success'])
        gate_order = executor._place_gate_futures_order.call_args.args[0]
        self.assertEqual(gate_order['protective_price'], 0.121)

    def test_gate_take_profit_protective_ioc_uses_nonzero_limit_and_reduce_only(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        class FakeResponse:
            status_code = 200
            text = '{}'

            @staticmethod
            def json():
                return {
                    'id': 'gate-close-1',
                    'status': 'finished',
                    'size': '297',
                    'left': '0',
                    'fill_price': '0.6919',
                    'finish_as': 'filled',
                }

        executor = RealExecutor(
            ExchangeConfig(gate_base_url='https://gate.test'),
            contract_meta={'EPIC': {'quanto_multiplier': 1.0}},
            spot_meta={},
        )
        executor._gate_sign = MagicMock(return_value={})
        executor._session = MagicMock()
        executor._session.post.return_value = FakeResponse()

        result = executor._place_gate_futures_order({
            'order_uuid': 'abcdef123456',
            'base_asset': 'EPIC',
            'future_contract': 'EPIC_USDT',
            'order_side': 'close',
            'trade_direction': 'buy',
            'target_qty': 297.0,
            'protective_price': 0.69195,
        })

        self.assertTrue(result['success'])
        body = json.loads(executor._session.post.call_args.kwargs['data'])
        self.assertNotEqual(body['price'], '0')
        self.assertEqual(body['tif'], 'ioc')
        self.assertTrue(body['reduce_only'])

    def test_gate_risk_close_uses_market_ioc_and_reduce_only(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        class FakeResponse:
            status_code = 200
            text = '{}'

            @staticmethod
            def json():
                return {
                    'id': 'gate-risk-close-1',
                    'status': 'finished',
                    'size': '297',
                    'left': '0',
                    'fill_price': '0.6949',
                    'finish_as': 'filled',
                }

        executor = RealExecutor(
            ExchangeConfig(gate_base_url='https://gate.test'),
            contract_meta={'EPIC': {'quanto_multiplier': 1.0}},
            spot_meta={},
        )
        executor._gate_sign = MagicMock(return_value={})
        executor._session = MagicMock()
        executor._session.post.return_value = FakeResponse()

        result = executor._place_gate_futures_order({
            'order_uuid': 'abcdef123456',
            'base_asset': 'EPIC',
            'future_contract': 'EPIC_USDT',
            'order_side': 'close',
            'trade_direction': 'buy',
            'target_qty': 297.0,
        })

        self.assertTrue(result['success'])
        body = json.loads(executor._session.post.call_args.kwargs['data'])
        self.assertEqual(body['price'], '0')
        self.assertEqual(body['tif'], 'ioc')
        self.assertTrue(body['reduce_only'])

    def test_margin_close_skips_spot_when_gate_market_close_fails(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': False,
            'reason': 'Gate 请求超时',
        })
        executor._place_binance_spot_order = MagicMock()

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'spot_order': {
                'base_asset': 'BEL',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 493.0,
            },
            'future_order': {
                'base_asset': 'BEL',
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 493.0,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertIn('未执行现货', result['message'])
        executor._place_binance_spot_order.assert_not_called()

    def test_margin_close_returns_future_fill_when_spot_fails(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.12092,
            'exec_qty': 493.0,
            'exec_amount': 59.61,
            'coverage_ratio': 0,
            'exchange_order_id': 'gate-1',
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': False,
            'reason': 'Binance 请求超时',
        })

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'spot_order': {
                'base_asset': 'BEL',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 493.0,
            },
            'future_order': {
                'base_asset': 'BEL',
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 493.0,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertIsNotNone(result['future_order'])
        self.assertEqual(result['future_order']['exchange_order_id'], 'gate-1')
        self.assertIn('期货已成交', result['message'])

    def test_margin_close_marks_spot_partial_fill_as_manual_risk(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=1),
            contract_meta={},
            spot_meta={},
        )
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.12092,
            'exec_qty': 493.0,
            'exec_amount': 59.61,
            'coverage_ratio': 0,
            'exchange_order_id': 'gate-1',
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.1196,
            'exec_qty': 300.0,
            'exec_amount': 35.88,
            'coverage_ratio': 0,
            'exchange_order_id': 'spot-partial',
        })

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'spot_order': {
                'base_asset': 'BEL',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 493.0,
            },
            'future_order': {
                'base_asset': 'BEL',
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 493.0,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertEqual(result['future_order']['exchange_order_id'], 'gate-1')
        self.assertEqual(result['spot_order']['exchange_order_id'], 'spot-partial')
        self.assertIn('现货部分成交', result['message'])

    def test_binance_close_price_range_rejection_retries_remaining_in_slices(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=4,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1, 'min_notional': 5}},
        )
        submitted = []

        def place(order):
            submitted.append(dict(order))
            if len(submitted) == 1:
                return {
                    'success': False,
                    'reason': 'order expired',
                    'order_status': 'EXPIRED',
                    'expiry_reason': 'EXECUTION_RULE_PRICE_RANGE_EXCEEDED',
                    'exec_qty': 0,
                    'exec_amount': 0,
                }
            qty = float(order['target_qty'])
            return {
                'success': True,
                'exec_price': 0.14,
                'exec_qty': qty,
                'exec_amount': qty * 0.14,
                'exchange_order_id': f'binance-{len(submitted)}',
                'fee_amount_usdt': 0,
            }

        executor._place_binance_spot_order = MagicMock(side_effect=place)
        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'tut-close-1234',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 9000,
            'target_amount': 1260,
        })

        self.assertTrue(result['success'])
        self.assertEqual(result['exec_qty'], 9000)
        self.assertEqual(result['retry_attempts'], 4)
        self.assertEqual([row['target_qty'] for row in submitted], [9000, 3000, 3000, 3000])
        self.assertTrue(all(row['trade_direction'] == 'sell' for row in submitted))
        self.assertEqual(len({row['client_order_id'] for row in submitted}), 4)

    def test_binance_close_timeout_queries_original_before_any_retry(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=4),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': False,
            'reason': 'Binance 请求超时',
            'request_ambiguous': True,
            'client_order_id': 'arb_tut_cs1',
        })
        executor._query_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.14,
            'exec_qty': 9000,
            'exec_amount': 1260,
            'exchange_order_id': 'binance-original',
            'fee_amount_usdt': 0,
        })

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'tut-close-1234',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 9000,
            'target_amount': 1260,
        })

        self.assertTrue(result['success'])
        executor._place_binance_spot_order.assert_called_once()
        executor._query_binance_spot_order.assert_called_once_with(
            symbol='TUTUSDT',
            client_order_id='arb_tut_cs1',
        )

    def test_binance_close_unknown_timeout_does_not_blindly_resubmit(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=4),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': False,
            'reason': 'Binance 请求超时',
            'request_ambiguous': True,
            'client_order_id': 'arb_tut_cs1',
        })
        executor._query_binance_spot_order = MagicMock(return_value=None)

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'tut-close-1234',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 9000,
            'target_amount': 1260,
        })

        self.assertFalse(result['success'])
        self.assertIn('原订单状态未确认', result['reason'])
        executor._place_binance_spot_order.assert_called_once()

    def test_future_then_spot_retry_never_reopens_or_adds_either_leg(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=4,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.14,
            'exec_qty': 9000,
            'exec_amount': 1260,
            'exchange_order_id': 'gate-close',
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': False,
            'reason': 'order expired',
            'order_status': 'EXPIRED',
            'expiry_reason': 'EXECUTION_RULE_PRICE_RANGE_EXCEEDED',
            'exec_qty': 0,
            'exec_amount': 0,
        })

        result = executor.execute({
            'execution_sequence': 'future_then_spot',
            'execution_reason': 'margin_close',
            'spot_order': {
                'order_uuid': 'tut-close-1234',
                'base_asset': 'TUT',
                'order_side': 'close',
                'trade_direction': 'sell',
                'target_qty': 9000,
            },
            'future_order': {
                'base_asset': 'TUT',
                'future_contract': 'TUT_USDT',
                'order_side': 'close',
                'trade_direction': 'buy',
                'target_qty': 9000,
            },
        }, {})

        self.assertFalse(result['success'])
        executor._place_gate_futures_order.assert_called_once()
        self.assertEqual(executor._place_binance_spot_order.call_count, 4)
        for call in executor._place_binance_spot_order.call_args_list:
            submitted = call.args[0]
            self.assertEqual(submitted['order_side'], 'close')
            self.assertEqual(submitted['trade_direction'], 'sell')

    def test_binance_expired_response_preserves_price_range_reason(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        result = executor._parse_binance_response({
            'symbol': 'TUTUSDT',
            'status': 'EXPIRED',
            'type': 'MARKET',
            'executedQty': '0',
            'cummulativeQuoteQty': '0',
            'orderId': 123,
            'clientOrderId': 'arb_tut_cs1',
            'expiryReason': 'EXECUTION_RULE_PRICE_RANGE_EXCEEDED',
        })

        self.assertFalse(result['success'])
        self.assertEqual(result['order_status'], 'EXPIRED')
        self.assertEqual(result['expiry_reason'], 'EXECUTION_RULE_PRICE_RANGE_EXCEEDED')
        self.assertIn('EXECUTION_RULE_PRICE_RANGE_EXCEEDED', result['reason'])

    def test_binance_close_non_retryable_rejection_stops_immediately(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=4),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': False,
            'reason': 'HTTP 400: insufficient balance',
            'exec_qty': 0,
            'exec_amount': 0,
        })

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'non-retryable',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertFalse(result['success'])
        self.assertEqual(result['retry_attempts'], 1)
        executor._place_binance_spot_order.assert_called_once()

    def test_binance_close_percent_price_rejection_is_retryable(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=2,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(side_effect=[
            {'success': False, 'reason': 'Filter failure: PERCENT_PRICE', 'exec_qty': 0},
            {
                'success': True,
                'exec_qty': 100,
                'exec_amount': 14,
                'exchange_order_id': 'retry-filled',
                'fee_amount_usdt': 0,
            },
        ])

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'percent-price',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertTrue(result['success'])
        self.assertEqual(result['retry_attempts'], 2)

    def test_binance_close_multiple_partial_fills_aggregate_actual_execution(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=3,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        submitted = []
        responses = [
            {'success': True, 'exec_qty': 40, 'exec_amount': 4, 'exchange_order_id': 'a', 'fee_amount_usdt': 0.1, 'fee_asset': 'USDT'},
            {'success': True, 'exec_qty': 30, 'exec_amount': 6, 'exchange_order_id': 'b', 'fee_amount_usdt': 0.2, 'fee_asset': 'USDT'},
            {'success': True, 'exec_qty': 30, 'exec_amount': 9, 'exchange_order_id': 'c', 'fee_amount_usdt': 0.3, 'fee_asset': 'USDT'},
        ]

        def place(order):
            submitted.append(order['target_qty'])
            return responses[len(submitted) - 1]

        executor._place_binance_spot_order = MagicMock(side_effect=place)
        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'partials',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertTrue(result['success'])
        self.assertEqual(submitted, [100, 30, 30])
        self.assertEqual(result['exec_qty'], 100)
        self.assertAlmostEqual(result['exec_amount'], 19)
        self.assertAlmostEqual(result['exec_price'], 0.19)
        self.assertAlmostEqual(result['fee_amount_usdt'], 0.6)
        self.assertEqual(result['exchange_order_ids'], ['a', 'b', 'c'])

    def test_binance_close_exactly_one_step_remaining_is_not_complete(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=1),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_qty': 9,
            'exec_amount': 1.26,
            'exchange_order_id': 'partial',
            'fee_amount_usdt': 0,
        })

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'one-step-left',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 10,
        })

        self.assertFalse(result['success'])
        self.assertEqual(result['remaining_qty'], 1)

    def test_binance_close_sub_step_remaining_is_dust_complete(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=1),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_qty': 9.5,
            'exec_amount': 1.33,
            'exchange_order_id': 'dust',
            'fee_amount_usdt': 0,
        })

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'dust-left',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 10,
        })

        self.assertTrue(result['success'])
        self.assertEqual(result['remaining_qty'], 0.5)

    def test_binance_close_retry_chunk_never_falls_below_step(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=4,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        submitted = []

        def place(order):
            submitted.append(order['target_qty'])
            if len(submitted) == 1:
                return {'success': False, 'order_status': 'EXPIRED', 'exec_qty': 0}
            return {'success': True, 'exec_qty': 2, 'exec_amount': 0.28, 'fee_amount_usdt': 0}

        executor._place_binance_spot_order = MagicMock(side_effect=place)
        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'small-remainder',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 2,
        })

        self.assertTrue(result['success'])
        self.assertEqual(submitted, [2, 2])

    def test_binance_close_retry_chunk_respects_min_notional(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=4),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1, 'min_notional': 5}},
        )
        chunk = executor._binance_close_retry_chunk_qty(
            {'base_asset': 'TUT', 'target_qty': 100, 'target_amount': 10},
            remaining_qty=100,
            slots_left=3,
            is_first=False,
        )
        self.assertEqual(chunk, 100)

    def test_binance_close_ambiguous_query_partial_retries_only_remaining(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=2,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        submitted = []

        def place(order):
            submitted.append(order['target_qty'])
            if len(submitted) == 1:
                return {
                    'success': False,
                    'reason': 'timeout',
                    'request_ambiguous': True,
                    'client_order_id': 'original',
                }
            return {'success': True, 'exec_qty': 60, 'exec_amount': 8.4, 'fee_amount_usdt': 0}

        executor._place_binance_spot_order = MagicMock(side_effect=place)
        executor._query_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_qty': 40,
            'exec_amount': 5.6,
            'exchange_order_id': 'queried-partial',
            'fee_amount_usdt': 0,
        })
        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'query-partial',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertTrue(result['success'])
        self.assertEqual(submitted, [100, 60])

    def test_binance_close_ambiguous_query_open_order_does_not_resubmit(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=4),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': False,
            'reason': 'timeout',
            'request_ambiguous': True,
            'client_order_id': 'original',
        })
        executor._query_binance_spot_order = MagicMock(return_value={
            'success': False,
            'order_status': 'NEW',
            'reason': '订单仍在交易所处理中',
            'exec_qty': 0,
        })

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'query-new',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertFalse(result['success'])
        executor._place_binance_spot_order.assert_called_once()

    def test_binance_close_caps_anomalous_overreported_fill(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(binance_close_retry_attempts=1),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_qty': 150,
            'exec_amount': 30,
            'exchange_order_id': 'bad-response',
            'fee_amount_usdt': 0,
        })

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'cap-fill',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertTrue(result['success'])
        self.assertEqual(result['exec_qty'], 100)
        self.assertEqual(result['exec_amount'], 20)

    def test_binance_close_missing_fee_in_one_fill_marks_fee_incomplete(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=2,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(side_effect=[
            {'success': True, 'exec_qty': 50, 'exec_amount': 7, 'fee_amount_usdt': 0.01},
            {'success': True, 'exec_qty': 50, 'exec_amount': 7},
        ])

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'fees',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertTrue(result['success'])
        self.assertIsNone(result['fee_amount_usdt'])

    def test_binance_retry_wrapper_does_not_apply_to_buy_or_open_orders(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        delegated = {'success': True, 'exec_qty': 1}
        executor._place_binance_spot_order = MagicMock(return_value=delegated)

        for order in (
            {'order_side': 'open', 'trade_direction': 'buy', 'target_qty': 1},
            {'order_side': 'close', 'trade_direction': 'buy', 'target_qty': 1},
        ):
            with self.subTest(order=order):
                self.assertIs(executor._place_binance_spot_close_with_retry(order), delegated)
        self.assertEqual(executor._place_binance_spot_order.call_count, 2)

    def test_binance_close_retry_attempts_are_hard_capped_at_four(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(
                binance_close_retry_attempts=99,
                binance_close_retry_base_delay_sec=0,
                binance_close_retry_max_delay_sec=0,
            ),
            contract_meta={},
            spot_meta={'TUT': {'step_size': 1}},
        )
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': False,
            'order_status': 'EXPIRED',
            'exec_qty': 0,
        })

        result = executor._place_binance_spot_close_with_retry({
            'order_uuid': 'hard-cap',
            'base_asset': 'TUT',
            'order_side': 'close',
            'trade_direction': 'sell',
            'target_qty': 100,
        })

        self.assertFalse(result['success'])
        self.assertEqual(result['retry_attempts'], 4)
        self.assertEqual(executor._place_binance_spot_order.call_count, 4)

    def test_binance_sell_base_fee_does_not_reduce_hedged_execution_qty(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        data = {
            'symbol': 'TUTUSDT',
            'side': 'SELL',
            'status': 'FILLED',
            'executedQty': '10',
            'cummulativeQuoteQty': '1.4',
            'orderId': 1,
            'fills': [{
                'price': '0.14',
                'qty': '10',
                'commission': '0.01',
                'commissionAsset': 'TUT',
            }],
        }

        sell = executor._parse_binance_response(data, trade_direction='sell')
        buy = executor._parse_binance_response(
            {**data, 'side': 'BUY'},
            trade_direction='buy',
        )

        self.assertEqual(sell['exec_qty'], 10.0)
        self.assertEqual(buy['exec_qty'], 9.99)

    def test_binance_spot_protective_ioc_uses_limit_ioc_params(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        class FakeResponse:
            status_code = 200
            text = ''

            @staticmethod
            def json():
                return {
                    'symbol': 'ASRUSDT',
                    'status': 'FILLED',
                    'executedQty': '3',
                    'cummulativeQuoteQty': '5.97',
                    'orderId': 123,
                    'fills': [{'price': '1.99', 'qty': '3', 'commission': '0', 'commissionAsset': 'BNB'}],
                }

        executor = RealExecutor(
            ExchangeConfig(binance_api_secret='secret'),
            contract_meta={},
            spot_meta={'ASR': {'step_size': 0.001, 'tick_size': 0.0001}},
        )
        executor._session = MagicMock()
        executor._session.post.return_value = FakeResponse()

        result = executor._place_binance_spot_order({
            'order_uuid': 'abcdef123456',
            'base_asset': 'ASR',
            'trade_direction': 'buy',
            'quantity_mode': 'base',
            'target_qty': 3,
            'protective_price': 1.99999,
        })

        self.assertTrue(result['success'])
        params = executor._session.post.call_args.kwargs['params']
        self.assertEqual(params['type'], 'LIMIT')
        self.assertEqual(params['timeInForce'], 'IOC')
        self.assertEqual(params['newOrderRespType'], 'FULL')
        self.assertEqual(params['price'], '1.9999')
        self.assertEqual(params['quantity'], '3.0')

    def test_future_maker_execute_hedges_filled_qty_only(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(
            ExchangeConfig(),
            contract_meta={},
            spot_meta={'ASR': {'step_size': 0.001, 'tick_size': 0.0001}},
        )
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 2.0,
            'exec_qty': 3.0,
            'exec_amount': 6.0,
            'coverage_ratio': 0,
            'execution_stats': {
                'future_maker': {
                    'attempted': True,
                    'filled': True,
                    'fill_ratio': 0.3,
                    'wait_ms': 120,
                    'ttl_ms': 800,
                }
            },
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 1.99,
            'exec_qty': 3.0,
            'exec_amount': 5.97,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'abc',
                'base_asset': 'ASR',
                'order_side': 'open',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 10,
                'target_amount': 10,
            },
            'future_order': {
                'order_uuid': 'abc',
                'base_asset': 'ASR',
                'future_contract': 'ASR_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_spot_hedge_protective_ioc_enabled': True,
                'maker_spot_hedge_min_basis_bps': 20.0,
                'target_qty': 10,
                'target_amount': 10,
            },
        }, {})

        self.assertTrue(result['success'])
        hedge_order = executor._place_binance_spot_order.call_args.args[0]
        self.assertEqual(hedge_order['target_qty'], 3.0)
        self.assertEqual(hedge_order['target_amount'], 6.0)
        self.assertEqual(hedge_order['quantity_mode'], 'base')
        self.assertEqual(hedge_order['order_type'], 'LIMIT_IOC')
        self.assertAlmostEqual(hedge_order['protective_price'], 2.0 / 1.002)
        self.assertEqual(
            result['execution_stats']['future_maker']['spot_exec_price'],
            1.99,
        )
        self.assertTrue(result['execution_stats']['future_maker']['spot_protective_ioc'])

    def test_future_maker_fallback_ioc_then_spot(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(side_effect=[
            {
                'success': False,
                'reason': 'future maker未成交(fill=0%)',
                'execution_stats': {
                    'future_maker': {
                        'attempted': True,
                        'filled': False,
                        'fill_ratio': 0,
                        'wait_ms': 800,
                        'ttl_ms': 800,
                    }
                },
            },
            {
                'success': True,
                'exec_price': 2.01,
                'exec_qty': 10.0,
                'exec_amount': 20.1,
                'coverage_ratio': 0,
            },
        ])
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 2.0,
            'exec_qty': 10.0,
            'exec_amount': 20.0,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'abc',
                'base_asset': 'BANK',
                'order_side': 'open',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 10,
                'target_amount': 10,
            },
            'future_order': {
                'order_uuid': 'abc',
                'base_asset': 'BANK',
                'future_contract': 'BANK_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_fallback_ioc_enabled': True,
                'maker_fallback_protective_price': 2.005,
                'target_qty': 10,
                'target_amount': 10,
            },
        }, {})

        self.assertTrue(result['success'])
        self.assertEqual(executor._place_gate_futures_order.call_count, 2)
        fallback_order = executor._place_gate_futures_order.call_args_list[1].args[0]
        self.assertNotIn('execution_style', fallback_order)
        self.assertEqual(fallback_order['protective_price'], 2.005)
        self.assertEqual(result['execution_stats']['future_maker']['fallback_attempted'], True)
        self.assertEqual(result['execution_stats']['future_maker']['fallback_filled'], True)
        executor._place_binance_spot_order.assert_called_once()

    def test_future_maker_close_fallback_uses_gate_market_semantics(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(side_effect=[
            {
                'success': False,
                'reason': 'future maker未成交(fill=0%)',
                'execution_stats': {
                    'future_maker': {
                        'attempted': True,
                        'filled': False,
                        'fill_ratio': 0,
                        'wait_ms': 1000,
                        'ttl_ms': 1000,
                    }
                },
            },
            {
                'success': True,
                'exec_price': 0.01983,
                'exec_qty': 1429.0,
                'exec_amount': 28.33,
                'coverage_ratio': 0,
            },
        ])
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.0197,
            'exec_qty': 1429.0,
            'exec_amount': 28.14,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'ai-close',
                'base_asset': 'AI',
                'order_side': 'close',
                'market_type': 'spot',
                'trade_direction': 'sell',
                'target_qty': 1429.0,
                'target_amount': 28.14,
            },
            'future_order': {
                'order_uuid': 'ai-close',
                'base_asset': 'AI',
                'future_contract': 'AI_USDT',
                'order_side': 'close',
                'market_type': 'future',
                'trade_direction': 'buy',
                'execution_style': 'maker',
                'maker_fallback_ioc_enabled': True,
                'maker_fallback_protective_price': 0.01983013,
                'target_qty': 1429.0,
                'target_amount': 28.33,
            },
        }, {})

        self.assertTrue(result['success'])
        fallback_order = executor._place_gate_futures_order.call_args_list[1].args[0]
        self.assertNotIn('execution_style', fallback_order)
        self.assertNotIn('protective_price', fallback_order)
        self.assertNotIn('maker_fallback_protective_price', fallback_order)
        self.assertTrue(result['execution_stats']['future_maker']['fallback_market'])
        executor._place_binance_spot_order.assert_called_once()

    def test_future_maker_spot_ioc_failure_retries_market_hedge(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.486,
            'exec_qty': 21.0,
            'exec_amount': 10.21,
            'coverage_ratio': 0,
            'execution_stats': {
                'future_maker': {
                    'attempted': True,
                    'filled': True,
                    'fill_ratio': 1,
                    'wait_ms': 300,
                    'ttl_ms': 1000,
                }
            },
        })
        executor._place_binance_spot_order = MagicMock(side_effect=[
            {'success': False, 'reason': '保护IOC未成交(fill=0,status=EXPIRED)'},
            {
                'success': True,
                'exec_price': 0.485,
                'exec_qty': 21.0,
                'exec_amount': 10.18,
                'coverage_ratio': 0,
            },
        ])

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'abc',
                'base_asset': 'EPIC',
                'order_side': 'open',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 21,
                'target_amount': 10,
            },
            'future_order': {
                'order_uuid': 'abc',
                'base_asset': 'EPIC',
                'future_contract': 'EPIC_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_spot_hedge_protective_ioc_enabled': True,
                'maker_spot_hedge_min_basis_bps': 20.0,
                'target_qty': 21,
                'target_amount': 10,
            },
        }, {})

        self.assertTrue(result['success'])
        self.assertEqual(executor._place_binance_spot_order.call_count, 2)
        retry_order = executor._place_binance_spot_order.call_args_list[1].args[0]
        self.assertNotIn('protective_price', retry_order)
        self.assertEqual(retry_order['quantity_mode'], 'base')
        maker = result['execution_stats']['future_maker']
        self.assertTrue(maker['spot_retry_market_attempted'])
        self.assertTrue(maker['spot_retry_market_filled'])
        self.assertEqual(maker['spot_retry_market_price'], 0.485)
        self.assertFalse(maker.get('future_unwind_attempted', False))

    def test_future_maker_spot_partial_ioc_retries_shortfall_and_merges(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 0.486,
            'exec_qty': 21.0,
            'exec_amount': 10.21,
            'coverage_ratio': 0,
            'execution_stats': {
                'future_maker': {
                    'attempted': True,
                    'filled': True,
                    'fill_ratio': 1,
                    'wait_ms': 300,
                    'ttl_ms': 1000,
                }
            },
        })
        executor._place_binance_spot_order = MagicMock(side_effect=[
            {
                'success': True,
                'exec_price': 0.485,
                'exec_qty': 10.0,
                'exec_amount': 4.85,
                'coverage_ratio': 0,
                'exchange_order_id': 'spot-a',
            },
            {
                'success': True,
                'exec_price': 0.487,
                'exec_qty': 11.0,
                'exec_amount': 5.357,
                'coverage_ratio': 0,
                'exchange_order_id': 'spot-b',
            },
        ])

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'abc',
                'base_asset': 'EPIC',
                'order_side': 'open',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 21,
                'target_amount': 10,
            },
            'future_order': {
                'order_uuid': 'abc',
                'base_asset': 'EPIC',
                'future_contract': 'EPIC_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_spot_hedge_protective_ioc_enabled': True,
                'maker_spot_hedge_min_basis_bps': 20.0,
                'target_qty': 21,
                'target_amount': 10,
            },
        }, {})

        self.assertTrue(result['success'])
        self.assertEqual(executor._place_binance_spot_order.call_count, 2)
        retry_order = executor._place_binance_spot_order.call_args_list[1].args[0]
        self.assertEqual(retry_order['target_qty'], 11.0)
        self.assertAlmostEqual(result['spot_order']['exec_qty'], 21.0)
        self.assertEqual(result['spot_order']['exchange_order_id'], 'spot-a,spot-b')
        maker = result['execution_stats']['future_maker']
        self.assertEqual(maker['spot_partial_qty'], 10.0)
        self.assertEqual(maker['spot_shortfall_qty'], 11.0)
        self.assertTrue(maker['spot_retry_market_filled'])

    def test_future_maker_spot_retry_failure_unwinds_future_leg(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(side_effect=[
            {
                'success': True,
                'exec_price': 0.486,
                'exec_qty': 21.0,
                'exec_amount': 10.21,
                'coverage_ratio': 0,
                'execution_stats': {
                    'future_maker': {
                        'attempted': True,
                        'filled': True,
                        'fill_ratio': 1,
                        'wait_ms': 300,
                        'ttl_ms': 1000,
                    }
                },
            },
            {
                'success': True,
                'exec_price': 0.484,
                'exec_qty': 21.0,
                'exec_amount': 10.16,
                'coverage_ratio': 0,
            },
        ])
        executor._place_binance_spot_order = MagicMock(side_effect=[
            {'success': False, 'reason': '保护IOC未成交(fill=0,status=EXPIRED)'},
            {'success': False, 'reason': 'Binance 余额不足'},
        ])

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'abc',
                'base_asset': 'EPIC',
                'order_side': 'open',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 21,
                'target_amount': 10,
            },
            'future_order': {
                'order_uuid': 'abc',
                'base_asset': 'EPIC',
                'future_contract': 'EPIC_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_spot_hedge_protective_ioc_enabled': True,
                'maker_spot_hedge_min_basis_bps': 20.0,
                'target_qty': 21,
                'target_amount': 10,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertIn('future已自动撤腿', result['message'])
        self.assertEqual(executor._place_gate_futures_order.call_count, 2)
        unwind_order = executor._place_gate_futures_order.call_args_list[1].args[0]
        self.assertEqual(unwind_order['trade_direction'], 'buy')
        self.assertEqual(unwind_order['order_side'], 'close')
        self.assertEqual(unwind_order['target_qty'], 21.0)
        self.assertNotIn('execution_style', unwind_order)
        maker = result['execution_stats']['future_maker']
        self.assertTrue(maker['future_unwind_attempted'])
        self.assertTrue(maker['future_unwind_filled'])

    def test_future_maker_fallback_ioc_zero_fill_is_normal_no_fill_reason(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': False,
            'reason': 'IOC未成交(fill=0, finish_as=ioc)',
        })
        result = executor._try_future_maker_fallback_ioc(
            {
                'maker_fallback_ioc_enabled': True,
                'maker_fallback_protective_price': 2.005,
            },
            {'success': False, 'reason': 'future maker未成交(fill=0%)'},
            {'future_maker': {}},
        )

        self.assertFalse(result['success'])
        self.assertIn('fallback_ioc未成交', result['reason'])
        self.assertNotIn('成交数据异常', result['reason'])

    def test_future_maker_no_fallback_keeps_rejected(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': False,
            'reason': 'future maker未成交(fill=0%)',
            'execution_stats': {'future_maker': {'attempted': True, 'filled': False}},
        })
        executor._place_binance_spot_order = MagicMock()

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'abc',
                'base_asset': 'BANK',
                'order_side': 'open',
                'market_type': 'spot',
                'trade_direction': 'buy',
                'target_qty': 10,
                'target_amount': 10,
            },
            'future_order': {
                'order_uuid': 'abc',
                'base_asset': 'BANK',
                'future_contract': 'BANK_USDT',
                'order_side': 'open',
                'market_type': 'future',
                'trade_direction': 'sell',
                'execution_style': 'maker',
                'maker_fallback_ioc_enabled': False,
                'target_qty': 10,
                'target_amount': 10,
            },
        }, {})

        self.assertFalse(result['success'])
        self.assertEqual(executor._place_gate_futures_order.call_count, 1)
        executor._place_binance_spot_order.assert_not_called()

    def test_future_maker_close_hedges_spot_by_position_ratio(self):
        from calc.real_executor import RealExecutor, ExchangeConfig

        executor = RealExecutor(ExchangeConfig(), contract_meta={}, spot_meta={})
        executor._place_gate_futures_order = MagicMock(return_value={
            'success': True,
            'exec_price': 2.0,
            'exec_qty': 5.0,
            'exec_amount': 10.0,
            'coverage_ratio': 0,
            'execution_stats': {
                'future_maker': {
                    'attempted': True,
                    'filled': True,
                    'fill_ratio': 0.5,
                    'wait_ms': 100,
                    'ttl_ms': 800,
                }
            },
        })
        executor._place_binance_spot_order = MagicMock(return_value={
            'success': True,
            'exec_price': 1.99,
            'exec_qty': 4.95,
            'exec_amount': 9.85,
            'coverage_ratio': 0,
        })

        result = executor.execute({
            'spot_order': {
                'order_uuid': 'abc',
                'base_asset': 'ASR',
                'order_side': 'close',
                'market_type': 'spot',
                'trade_direction': 'sell',
                'target_qty': 9.9,
                'target_amount': 10,
            },
            'future_order': {
                'order_uuid': 'abc',
                'base_asset': 'ASR',
                'future_contract': 'ASR_USDT',
                'order_side': 'close',
                'market_type': 'future',
                'trade_direction': 'buy',
                'execution_style': 'maker',
                'target_qty': 10,
                'target_amount': 10,
            },
        }, {})

        self.assertTrue(result['success'])
        hedge_order = executor._place_binance_spot_order.call_args.args[0]
        self.assertAlmostEqual(hedge_order['target_qty'], 4.95)
        self.assertEqual(hedge_order['quantity_mode'], 'base')


class TestTradingExecutorPeakCheck(unittest.TestCase):
    """峰值回落 + sustain 确认（开仓回落通道）"""

    def setUp(self):
        self.te = make_trading_executor(sustain_sec=2.0, peak_pullback_pct=0.10,
                                        peak_monitor_timeout_sec=60)

        # mock 副作用：实时费率校验恒通过、信号写库返回固定 ID
        self.te._verify_realtime_funding_rate = MagicMock(return_value=True)
        self.te._create_signal = MagicMock(return_value=1001)
        self.te._resolve_signal = MagicMock()

        self.row = {'contract': 'BTC_USDT', 'symbol': 'BTCUSDT'}

    def test_first_entry_records_peak_returns_false(self):
        """首次进入：记录峰值，返回 False"""
        ret = self.te._pass_peak_check('BTC', 100.0, self.row)
        self.assertFalse(ret)
        state = self.te._peak_state['BTC']
        self.assertEqual(state['peak_bps'], 100.0)
        self.te._verify_realtime_funding_rate.assert_called_once()
        self.te._create_signal.assert_called_once()

    def test_higher_basis_updates_peak(self):
        """后续更高 basis：更新峰值，仍返回 False"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        ret = self.te._pass_peak_check('BTC', 120.0, self.row)
        self.assertFalse(ret)
        self.assertEqual(self.te._peak_state['BTC']['peak_bps'], 120.0)

    def test_not_pulled_back_returns_false(self):
        """峰值后小幅回落但未达 pullback_pct → 等待"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        # 回落 5% < 10% 阈值
        ret = self.te._pass_peak_check('BTC', 95.0, self.row)
        self.assertFalse(ret)
        # 状态未清，继续监控
        self.assertIn('BTC', self.te._peak_state)

    def test_pullback_but_sustain_insufficient(self):
        """回落到位但持续时间不够 → 等待"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        # 回落 10%（达到 pullback 阈值），但 elapsed = 0s < 2s
        ret = self.te._pass_peak_check('BTC', 90.0, self.row)
        self.assertFalse(ret)
        self.assertIn('BTC', self.te._peak_state)

    def test_full_pass_returns_true(self):
        """回落 + sustain 达标 → 进入 resiliency_active，trigger=pullback"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        self.te._peak_state['BTC']['start_time'] = datetime.now() - timedelta(seconds=3)

        ret = self.te._pass_peak_check('BTC', 90.0, self.row)
        self.assertTrue(ret)
        self.assertEqual(self.te._peak_state['BTC']['trigger'], 'pullback')
        self.assertTrue(self.te._peak_state['BTC']['resiliency_active'])

    def test_resiliency_active_keeps_sampling_after_basis_rebounds(self):
        """进入盘口恢复等待后，不再反复要求 pullback 条件成立"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        self.te._peak_state['BTC']['start_time'] = datetime.now() - timedelta(seconds=3)
        self.assertTrue(self.te._pass_peak_check('BTC', 90.0, self.row))

        # basis 反弹到 pullback 阈值上方，仍应继续交给 resiliency 采样。
        ret = self.te._pass_peak_check('BTC', 99.0, self.row)
        self.assertTrue(ret)
        self.assertTrue(self.te._peak_state['BTC']['resiliency_active'])

    def test_monitor_timeout_passes_with_timeout_trigger(self):
        """监控超时（elapsed ≥ 60s）→ 进入盘口恢复/旁路风控，trigger=timeout"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        # 推早 65s 模拟超时
        self.te._peak_state['BTC']['start_time'] = datetime.now() - timedelta(seconds=65)

        ret = self.te._pass_peak_check('BTC', 95.0, self.row)
        self.assertTrue(ret)
        self.assertIn('BTC', self.te._peak_state)
        self.assertEqual(self.te._peak_state['BTC']['trigger'], 'timeout')
        self.assertTrue(self.te._peak_state['BTC']['resiliency_active'])
        self.te._resolve_signal.assert_not_called()


class TestTradingExecutorPreExecutionGate(unittest.TestCase):
    """开仓最终风控旁路 6 个分支"""

    def setUp(self):
        self.te = make_trading_executor(
            sustain_sec=2.0,
            basis_threshold_bps=-60,
            coverage_threshold=0.8,
            max_orderbook_lag_ms=200.0,
            vwap_threshold_meta={'BTC': {'p20': -50}},
            close_vwap_threshold_meta={'BTC': {'close_basis_p20': -100}},
            funding_entry_enabled=False,
        )

    def _patch_gate_chain(self, vwap_basis_bps, open_coverage):
        """patch _pre_execution_gate 内部函数依赖，让其返回可控值"""
        merge_mock = MagicMock(return_value=[{'_': 'merged'}])
        hedge_mock = MagicMock(return_value=[{
            'spot_open_vwap': 100.0,
            'future_open_vwap': 100.0,
            'open_coverage': open_coverage,
        }])
        vwap_mock = MagicMock(return_value=vwap_basis_bps)
        return (
            patch('calc.merge_cross_exchange_orderbook.merge_orderbook_records', merge_mock),
            patch('calc.calculate_hedge_metrics.calculate_hedge_metrics', hedge_mock),
            patch('calc.trading_executor.calc_vwap_basis_bps', vwap_mock),
        )

    def _setup_books(self, gate_lag_sec=0.05, spot_lag_sec=0.05,
                     gate_uc=10, spot_uc=10):
        """构造 manager，注入 BTC 标的盘口"""
        now = time.time()
        gate_book = FakeOrderBook(
            last_update_time=now - gate_lag_sec,
            update_count=gate_uc,
            row={'contract': 'BTC_USDT'},
        )
        spot_book = FakeOrderBook(
            last_update_time=now - spot_lag_sec,
            update_count=spot_uc,
            row={'symbol': 'BTCUSDT'},
        )
        self.te.set_orderbook_managers(
            FakeManager({'BTC_USDT': gate_book}),
            FakeManager({'BTCUSDT': spot_book}),
        )

    def test_manager_not_injected_passes(self):
        """未注入 manager → 退化放行"""
        passed, row, basis, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertTrue(passed)
        self.assertEqual(reason, '')

    def test_orderbook_missing_blocks(self):
        """盘口不可用 → 拦截"""
        self.te.set_orderbook_managers(FakeManager({}), FakeManager({}))
        passed, _, _, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertFalse(passed)
        self.assertIn('盘口不可用', reason)

    def test_lag_exceeds_blocks(self):
        """lag > 200ms → 拦截"""
        self._setup_books(gate_lag_sec=0.5)  # 500ms > 200ms
        passed, _, _, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertFalse(passed)
        self.assertIn('行情滞后', reason)

    def test_basis_decay_blocks(self):
        """VWAP 基差 < p20 → 基差衰减拦截"""
        self._setup_books()
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(
            vwap_basis_bps=-80, open_coverage=0.5,  # -80 < p20(-50)
        )
        with m_merge, m_hedge, m_vwap:
            passed, _, basis, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertFalse(passed)
        self.assertIn('基差衰减', reason)
        self.assertEqual(basis, -80)

    def test_profitability_guard_blocks(self):
        """盈利性守卫：basis ≤ close_thr + fee_cost → 拦截
        构造前提：basis 必须先通过基差衰减（> p20），才能走到盈利性守卫这一关。
        因此把 p20 调到极低值，避开基差衰减拦截。
        """
        self.te.vwap_threshold_meta = {'BTC': {'p20': -500}}  # 不限制基差衰减
        self._setup_books()
        # close_thr=-100, fee_cost_bps=+30（4 × 0.00075 = 0.003 → 30bps）
        # 守卫阈值 = -100 + 30 = -70；basis=-71 ≤ -70 → 触发
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(
            vwap_basis_bps=-71, open_coverage=0.5,
        )
        with m_merge, m_hedge, m_vwap:
            passed, _, _, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertFalse(passed)
        self.assertIn('盈利性守卫', reason)

    def test_coverage_excess_blocks(self):
        """覆盖率 > 阈值 → 拦截"""
        self._setup_books()
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(
            vwap_basis_bps=0, open_coverage=0.95,  # > 0.8
        )
        with m_merge, m_hedge, m_vwap:
            passed, _, _, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertFalse(passed)
        self.assertIn('盘口覆盖超限', reason)

    def test_full_pass_writes_lag_cache(self):
        """全部通过：返回 True + 写入 _last_orderbook_lag_ms"""
        self._setup_books(gate_lag_sec=0.05, spot_lag_sec=0.06)
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(
            vwap_basis_bps=0, open_coverage=0.5,
        )
        with m_merge, m_hedge, m_vwap:
            passed, row, basis, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertTrue(passed)
        self.assertEqual(reason, '')
        self.assertEqual(basis, 0)
        # bug 修复验证：lag 缓存必须写入
        self.assertIn('BTC', self.te._last_orderbook_lag_ms)
        gate_lag, spot_lag = self.te._last_orderbook_lag_ms['BTC']
        self.assertAlmostEqual(gate_lag, 50, delta=30)
        self.assertAlmostEqual(spot_lag, 60, delta=30)

    def test_thin_bursty_profile_uses_wider_pre_gate_lag(self):
        """thin_bursty 可放宽最终旁路 lag，但仍走其它旁路风控。"""
        self.te.asset_profile_meta = {'BTC': {'market_profile': 'thin_bursty'}}
        self._setup_books(gate_lag_sec=1.0, spot_lag_sec=1.0)
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(
            vwap_basis_bps=0, open_coverage=0.5,
        )
        with m_merge, m_hedge, m_vwap:
            passed, _, basis, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertTrue(passed)
        self.assertEqual(reason, '')
        self.assertEqual(basis, 0)

    def test_thin_bursty_profile_scales_open_amount(self):
        """thin_bursty 单笔金额按配置缩小。"""
        self.te.asset_profile_meta = {'BTC': {'market_profile': 'thin_bursty'}}
        self.te.open_amount_usdt = 10.0
        self.assertEqual(self.te._active_open_amount_usdt({'base_asset': 'BTC'}), 6.0)

    def test_funding_carry_and_thin_bursty_use_one_reduced_amount(self):
        """funding carry 与 thin_bursty 共用降档金额，不重复乘倍率。"""
        self.te.open_amount_usdt = 10.0
        self.assertEqual(self.te._funding_carry_amount('ETH'), 6.0)

        self.te.asset_profile_meta = {'BTC': {'market_profile': 'thin_bursty'}}
        self.assertEqual(self.te._active_open_amount_usdt({'base_asset': 'BTC'}), 6.0)
        self.assertEqual(self.te._funding_carry_amount('BTC'), 6.0)

    def test_live_pre_gate_uses_realtime_funding_snapshot(self):
        """实盘旁路用下单前实时 funding 覆盖缓存 funding，并重算 entry snapshot。"""
        self.te.funding_entry_enabled = True
        self.te.executor_client.channel = 'Live'
        self.te.contract_meta = {
            'BTC': {
                'funding_rate': 0.002,
                'funding_rate_24h': 0.008,
                'funding_interval': 21600,
            }
        }
        self.te.vwap_threshold_meta = {'BTC': {'p20': 10.0}}
        self.te._peak_state['BTC'] = {
            'peak_bps': 60.0,
            'start_time': datetime.now(),
            'signal_id': 1,
            'signal_basis_bps': 60.0,
        }
        self._setup_books(gate_lag_sec=0.05, spot_lag_sec=0.05)
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(
            vwap_basis_bps=50, open_coverage=0.5,
        )
        realtime_info = {
            'funding_rate': 0.00075,
            'funding_rate_24h': 0.003,
            'funding_interval': 21600,
            'funding_next_apply': '2026-06-07 20:00:00',
            'funding_last_apply': '2026-06-07 14:00:00',
        }
        with m_merge, m_hedge, m_vwap, patch(
            'calc.trading_executor.get_single_contract_funding_info',
            return_value=realtime_info,
        ):
            passed, row, basis, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')

        self.assertTrue(passed)
        self.assertEqual(reason, '')
        self.assertEqual(basis, 50)
        self.assertEqual(row['funding_rate_24h'], 0.003)
        self.assertEqual(row['_cached_funding_rate_24h'], 0.008)
        self.assertEqual(self.te.contract_meta['BTC']['funding_rate_24h'], 0.003)
        self.assertAlmostEqual(
            self.te._peak_state['BTC']['entry_snapshot']['funding_24h_bps'],
            30.0,
        )


# ══════════════════════════════════════════════════════════════════
# Funding-adjusted entry 测试
# ══════════════════════════════════════════════════════════════════

class TestTradingExecutorFundingAdjustedEntry(unittest.TestCase):
    """Funding 是核心收益，但 entry_floor 仍要保护入场位置和执行质量。"""

    def _row(self, base_asset, basis, funding_rate_24h, coverage=0.5):
        return {
            'base_asset': base_asset,
            'contract': f'{base_asset}_USDT',
            'symbol': f'{base_asset}USDT',
            'spot_qty': 1.0,
            'open_vwap_basis_bps': basis,
            'open_coverage': coverage,
            'funding_rate_24h': funding_rate_24h,
            'funding_next_apply': (datetime.now() + timedelta(minutes=20)).strftime('%Y-%m-%d %H:%M:%S'),
        }

    def test_high_funding_gets_capped_discount_not_unlimited_entry(self):
        te = make_trading_executor(
            vwap_threshold_meta={'ALLO': {'p20': 26.9}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )

        snapshot = te._entry_snapshot('ALLO', 20.0, self._row('ALLO', 20.0, 0.008646))

        self.assertAlmostEqual(snapshot['funding_24h_bps'], 86.46, places=2)
        self.assertAlmostEqual(snapshot['entry_floor_bps'], 16.9, places=1)
        self.assertFalse(te._pass_risk_check(self._row('ALLO', 10.0, 0.008646)))
        self.assertTrue(te._pass_risk_check(self._row('ALLO', 20.0, 0.008646)))

    def test_funding_support_stable_channel_requires_avg_and_current(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        row = self._row('ALLO', 50.0, 0.0008)
        row.update({
            'funding_rate_24h_avg_bps': 8.5,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertTrue(te._pass_risk_check(row))

    def test_persisted_funding_snapshots_do_not_change_open_risk_decision(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        row = self._row('ALLO', 50.0, 0.0008)
        row.update({
            'funding_rate_24h_avg_bps': 8.5,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        baseline = te._pass_risk_check(dict(row))
        row.update({
            'open_funding_rate_24h': -9.99,
            'close_funding_rate_24h': -9.99,
        })

        self.assertTrue(baseline)
        self.assertEqual(te._pass_risk_check(row), baseline)

    def test_funding_support_high_realtime_channel_bypasses_weak_history(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        row = self._row('ALLO', 50.0, 0.0025)
        row.update({
            'funding_rate_24h_avg_bps': 7.5,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertTrue(te._pass_risk_check(row))

    def test_funding_support_rejects_weak_history_without_high_realtime(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        row = self._row('ALLO', 50.0, 0.0012)
        row.update({
            'funding_rate_24h_avg_bps': 7.5,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('资金费率通道不达标', te._get_risk_fail_reason(row))

    def test_high_basis_channel_allows_weak_funding_when_convergence_edge_is_large(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            high_basis_enabled=True,
            high_basis_min_funding_24h_bps=3.0,
            high_basis_min_entry_buffer_bps=25.0,
            high_basis_min_net_edge_bps=20.0,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
            asset_tier_meta={'ALLO': 'A'},
        )
        row = self._row('ALLO', 70.0, 0.0003)
        row.update({
            'funding_rate_24h_avg_bps': 2.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertTrue(te._pass_risk_check(row))
        self.assertTrue(row.get('_high_basis_channel'))
        self.assertEqual(row.get('open_amount_usdt'), te.open_amount_usdt * 0.5)
        self.assertEqual(row.get('_entry_high_basis_amount_usdt'), te.open_amount_usdt * 0.5)
        self.assertIn('高基差通道', row.get('_open_channel_reason', ''))

    def test_delist_risk_report_blocks_new_open_even_when_signal_is_good(self):
        te = make_trading_executor(
            min_funding_rate_bps=10.0,
            min_funding_support_bps=8.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'NFP': {'p20': 0.0}},
            close_vwap_threshold_meta={'NFP': {'close_basis_p20': -100}},
        )
        te.set_delist_risk_report({
            'items': [{
                'base_asset': 'NFP',
                'exchange': 'binance',
                'market_type': 'spot',
                'risk_type': 'delist_schedule',
                'risk_level': 'critical',
                'status': 'scheduled',
                'delist_at': (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S'),
                'days_left': 5,
                'message': 'Binance现货已进入下架计划',
            }],
        })
        row = self._row('NFP', 80.0, 0.0030)
        row.update({
            'funding_rate_24h_avg_bps': 12.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('下架风险禁止开仓', reason)
        self.assertIn('binance/spot:scheduled', reason)

    def test_gate_scheduled_delist_blocks_new_open_before_reduce_only(self):
        te = make_trading_executor()
        te.set_delist_risk_report({
            'items': [{
                'base_asset': 'VIC',
                'exchange': 'gate',
                'market_type': 'future',
                'risk_type': 'delist_schedule',
                'risk_level': 'critical',
                'status': 'scheduled',
                'contract_status': 'trading',
                'reduce_only_at': (datetime.now() + timedelta(hours=45)).strftime('%Y-%m-%d %H:%M:%S'),
                'delist_at': (datetime.now() + timedelta(hours=46)).strftime('%Y-%m-%d %H:%M:%S'),
                'days_left': 1.92,
                'message': 'Gate合约已计划下架，当前状态=trading',
            }],
        })

        allowed, reason = te._check_delist_open_block('VIC')

        self.assertFalse(allowed)
        self.assertIn('gate/future:scheduled', reason)
        self.assertIn('reduce_only_at=', reason)
        self.assertIn('Gate合约已计划下架', reason)

    def test_check_and_open_skips_delist_risk_before_executor_call(self):
        te = make_trading_executor()
        te.set_delist_risk_report({
            'items': [{
                'base_asset': 'NFP',
                'exchange': 'binance',
                'market_type': 'spot',
                'risk_type': 'delist_schedule',
                'risk_level': 'critical',
                'status': 'scheduled',
                'delist_at': (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
                'days_left': 1,
                'message': 'Binance现货已进入下架计划',
            }],
        })
        te.executor_client.execute = MagicMock()
        te._resolve_signal = MagicMock()
        te._record_presignal_rejection = MagicMock()
        row = self._row('NFP', 80.0, 0.0030)

        with (
            patch.object(te, '_refresh_holding_exposure_from_db'),
            patch.object(te, '_load_exchange_risk_blocked_assets', return_value=set()),
            patch.object(te, '_load_open_cooldown_from_db'),
        ):
            results = te.check_and_open([row])

        self.assertEqual(results, [])
        te.executor_client.execute.assert_not_called()
        reason = te._resolve_signal.call_args.args[2]
        self.assertIn('下架风险禁止开仓', reason)
        te._record_presignal_rejection.assert_called_once()

    def test_check_and_open_skips_desynced_asset_before_executor_call(self):
        te = make_trading_executor()
        te.executor_client.execute = MagicMock()
        te._resolve_signal = MagicMock()
        row = self._row('BEL', 80.0, 0.0030)
        te._peak_state['BEL'] = {'peak_bps': 90.0}
        te._open_resiliency.observe_shock('BEL', row)

        with (
            patch.object(te, '_refresh_holding_exposure_from_db'),
            patch.object(te, '_load_exchange_risk_blocked_assets', return_value={'BEL'}),
            patch.object(te, '_load_open_cooldown_from_db'),
        ):
            results = te.check_and_open([row])

        self.assertEqual(results, [])
        te.executor_client.execute.assert_not_called()
        te._resolve_signal.assert_called_once_with(
            'BEL',
            'conditions_lost',
            '交易所仓位风险(desynced)暂停开仓',
        )
        self.assertNotIn('BEL', te._peak_state)
        self.assertNotIn('BEL', te._open_resiliency._state)

    def test_check_and_open_skips_asset_under_negative_funding_watch(self):
        te = make_trading_executor()
        te.set_negative_funding_watch_assets({'HEI'})
        te.executor_client.execute = MagicMock()
        te._resolve_signal = MagicMock()
        te._record_presignal_rejection = MagicMock()
        row = self._row('HEI', 80.0, 0.0030)
        te._peak_state['HEI'] = {'peak_bps': 90.0}
        te._open_resiliency.observe_shock('HEI', row)

        with (
            patch.object(te, '_refresh_holding_exposure_from_db'),
            patch.object(te, '_load_exchange_risk_blocked_assets', return_value=set()),
            patch.object(te, '_load_open_cooldown_from_db'),
        ):
            results = te.check_and_open([row])

        self.assertEqual(results, [])
        te.executor_client.execute.assert_not_called()
        te._resolve_signal.assert_called_once_with(
            'HEI',
            'conditions_lost',
            '负资金费风险监控中，暂停同币开仓',
        )
        te._record_presignal_rejection.assert_called_once()
        self.assertNotIn('HEI', te._peak_state)
        self.assertNotIn('HEI', te._open_resiliency._state)

    def test_open_reason_starts_with_funding_channel_label(self):
        te = make_trading_executor(
            min_funding_rate_bps=10.0,
            min_funding_support_bps=8.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
        )
        row = self._row('ALLO', 70.0, 0.0010)
        row.update({
            'funding_rate_24h_avg_bps': 8.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertTrue(te._pass_risk_check(row))
        reason = te._build_open_reason(row, 'ALLO', 70.0)

        self.assertTrue(reason.startswith('开仓通道(funding)|'))

    def test_open_reason_starts_with_high_basis_channel_label(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            funding_support_min_samples=2,
            high_basis_enabled=True,
            high_basis_min_funding_24h_bps=3.0,
            high_basis_min_entry_buffer_bps=25.0,
            high_basis_min_net_edge_bps=20.0,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
            asset_tier_meta={'ALLO': 'A'},
        )
        row = self._row('ALLO', 70.0, 0.0003)
        row.update({
            'funding_rate_24h_avg_bps': 2.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertTrue(te._pass_risk_check(row))
        reason = te._build_open_reason(row, 'ALLO', 70.0)

        self.assertTrue(reason.startswith('开仓通道(高基差)|'))

    def test_high_basis_channel_rejects_when_net_convergence_edge_is_small(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            high_basis_enabled=True,
            high_basis_min_funding_24h_bps=3.0,
            high_basis_min_entry_buffer_bps=25.0,
            high_basis_min_net_edge_bps=20.0,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
            asset_tier_meta={'ALLO': 'A'},
        )
        row = self._row('ALLO', 55.0, 0.0003)
        row.update({
            'funding_rate_24h_avg_bps': 2.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('资金费率通道不达标', reason)
        self.assertIn('高基差通道不达标', reason)

    def test_high_basis_realtime_funding_uses_its_own_floor(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            high_basis_enabled=True,
            high_basis_min_funding_24h_bps=3.0,
        )

        with patch('calc.trading_executor.get_single_contract_funding_info', return_value={
            'funding_rate_24h': 0.0003,
        }):
            self.assertTrue(te._verify_realtime_funding_rate(
                'ALLO', 'ALLO_USDT', min_floor_bps=te.high_basis_min_funding_24h_bps
            ))

    def test_funding_support_stable_channel_keeps_realtime_floor(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        row = self._row('ALLO', 50.0, 0.0003)
        row.update({
            'funding_rate_24h_avg_bps': 8.5,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('稳定资金费通道实时不达标', te._get_risk_fail_reason(row))

    def test_funding_support_samples_need_high_realtime_channel(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        row = self._row('ALLO', 50.0, 0.0018)
        row.update({
            'funding_rate_24h_avg_bps': 8.5,
            'funding_rate_24h_avg_samples': 1,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('资金费率样本不足', te._get_risk_fail_reason(row))

    def test_realtime_funding_floor_uses_stable_channel_when_history_strong(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
        )
        te.funding_support_meta = {
            'ALLO': {
                'funding_rate_24h_avg_bps': 8.5,
                'funding_rate_24h_avg_samples': 3,
            }
        }

        with patch('calc.trading_executor.get_single_contract_funding_info', return_value={
            'funding_rate_24h': 0.0005,
        }):
            self.assertTrue(te._verify_realtime_funding_rate('ALLO', 'ALLO_USDT'))

    def test_realtime_funding_floor_uses_high_channel_when_history_not_available(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            funding_support_min_samples=2,
        )
        te.funding_support_meta = {
            'ALLO': {
                'funding_rate_24h_avg_bps': 8.5,
                'funding_rate_24h_avg_samples': 1,
            }
        }

        with patch('calc.trading_executor.get_single_contract_funding_info', return_value={
            'funding_rate_24h': 0.0018,
        }):
            self.assertFalse(te._verify_realtime_funding_rate('ALLO', 'ALLO_USDT'))

    def test_total_available_ratio_blocks_new_open(self):
        te = make_trading_executor(
            min_available_ratio=0.10,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 25.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(14.0, 100.0),
        }
        te._account_summary_ts = time.time()

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 10.0

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('Gate下单后可用', te._get_risk_fail_reason(row))

    def test_exchange_available_ratio_uses_split_thresholds(self):
        te = make_trading_executor(
            min_binance_available_ratio=0.15,
            min_gate_available_ratio=0.15,
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 251.0, 'net_value': 1000.0},
            'gate': fresh_safe_gate_summary(249.0, 1000.0),
        }
        te._account_summary_ts = time.time()

        ok, reason = te._check_account_capital(100.0)

        self.assertFalse(ok)
        self.assertIn('Gate下单后可用', reason)
        self.assertIn('总资金15%', reason)

    def test_binance_available_ratio_blocks_below_fifteen_percent_reserve(self):
        te = make_trading_executor(
            min_binance_available_ratio=0.15,
            min_gate_available_ratio=0.15,
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 249.0, 'net_value': 1000.0},
            'gate': fresh_safe_gate_summary(270.0, 1000.0),
        }
        te._account_summary_ts = time.time()

        ok, reason = te._check_account_capital(100.0)

        self.assertFalse(ok)
        self.assertIn('Binance下单后可用', reason)
        self.assertIn('总资金15%', reason)

    def test_binance_available_ratio_allows_above_fifteen_percent_reserve(self):
        te = make_trading_executor(
            min_binance_available_ratio=0.15,
            min_gate_available_ratio=0.15,
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 251.0, 'net_value': 1000.0},
            'gate': fresh_safe_gate_summary(270.0, 1000.0),
        }
        te._account_summary_ts = time.time()

        ok, reason = te._check_account_capital(100.0)

        self.assertTrue(ok, reason)

    def test_gate_cross_risk_warning_blocks_new_open(self):
        te = make_trading_executor(
            min_binance_available_ratio=0.08,
            min_gate_available_ratio=0.15,
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 500.0, 'net_value': 1000.0},
            'gate': {
                'available': 500.0,
                'net_value': 1000.0,
                'cross_risk': {
                    'status': 'warning',
                    'status_label': '预警',
                    'account_mmr_pct': 420.12,
                    'nearest_liq_distance_bps': 780.5,
                    'available_ratio_pct': 50.0,
                    'account_fetched_at_ts': time.time(),
                },
            },
        }
        te._account_summary_ts = time.time()

        ok, reason = te._check_account_capital(100.0)

        self.assertFalse(ok)
        self.assertIn('Gate全仓风险预警', reason)
        self.assertIn('MMR=420.1%', reason)

    def test_gate_cross_risk_safe_allows_new_open(self):
        te = make_trading_executor(
            min_binance_available_ratio=0.08,
            min_gate_available_ratio=0.15,
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 500.0, 'net_value': 1000.0},
            'gate': {
                'available': 500.0,
                'net_value': 1000.0,
                'cross_risk': {
                    'status': 'safe',
                    'account_mmr_pct': 1200.0,
                    'available_ratio_pct': 50.0,
                    'account_fetched_at_ts': time.time(),
                },
            },
        }
        te._account_summary_ts = time.time()

        ok, reason = te._check_account_capital(100.0)

        self.assertTrue(ok, reason)

    def test_gate_cross_risk_warning_from_detail_blocks_new_open(self):
        te = make_trading_executor(
            min_binance_available_ratio=0.08,
            min_gate_available_ratio=0.15,
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 500.0, 'net_value': 1000.0},
            'gate': {
                'available': 500.0,
                'net_value': 1000.0,
                'detail': {
                    'gate_cross_risk': {
                        'status': 'warning',
                        'account_mmr_pct': 499.9,
                        'account_fetched_at_ts': time.time(),
                    },
                },
            },
        }
        te._account_summary_ts = time.time()

        ok, reason = te._check_account_capital(100.0)

        self.assertFalse(ok)
        self.assertIn('Gate全仓风险预警', reason)

    def test_cross_margin_capital_check_reserves_full_order_notional(self):
        te = make_trading_executor(
            min_binance_available_ratio=0,
            min_gate_available_ratio=0,
        )
        te.capital_required = True
        te._fee_future_open = 0
        te._account_summary = {
            'binance': {'available': 200.0, 'net_value': 1000.0},
            'gate': fresh_safe_gate_summary(50.0, 1000.0),
        }
        te._account_summary_ts = time.time()

        ok, reason = te._check_account_capital(100.0)

        self.assertFalse(ok)
        self.assertIn('Gate可用50.00<需100.00USDT', reason)

    def test_asset_exposure_ratio_blocks_new_open(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 5.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 10.0

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('Binance spot', te._get_risk_fail_reason(row))

    def test_asset_exposure_uses_spot_leg_for_concentration(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 1000.0, 'net_value': 5000.0},
            'gate': {
                'available': 4000.0,
                'net_value': 4200.0,
                'cross_risk': {
                    'status': 'safe',
                    'account_mmr_pct': 3000.0,
                    'account_fetched_at_ts': time.time(),
                },
            },
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 275.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 160.0

        self.assertTrue(te._pass_risk_check(row))
        self.assertNotIn('_quality_scale_in_used', row)

    def test_asset_exposure_still_blocks_spot_concentration(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=False,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 1000.0, 'net_value': 4000.0},
            'gate': {
                'available': 4000.0,
                'net_value': 4200.0,
                'cross_risk': {
                    'status': 'safe',
                    'account_mmr_pct': 3000.0,
                    'account_fetched_at_ts': time.time(),
                },
            },
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 275.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 160.0

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('Binance spot 435.00', te._get_risk_fail_reason(row))

    def test_asset_exposure_uses_normal_limit_when_quality_scale_in_disabled(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=False,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 20.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertFalse(te._pass_risk_check(row))
        self.assertNotIn('_quality_scale_in_used', row)
        self.assertIn('Binance spot', te._get_risk_fail_reason(row))
        self.assertNotIn('优质加仓', te._get_risk_fail_reason(row))

    def test_default_concentration_limits_are_15_and_25_percent(self):
        te = make_trading_executor()

        self.assertEqual(te.max_asset_exposure_ratio, 0.15)
        self.assertEqual(te.quality_scale_in_enhanced_ratio, 0.25)

    def test_normal_concentration_allows_exactly_15_percent(self):
        te = make_trading_executor(
            quality_scale_in_enabled=False,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 13.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertTrue(te._pass_risk_check(row))
        self.assertNotIn('_quality_scale_in_used', row)

    def test_normal_concentration_blocks_above_15_percent_without_quality(self):
        te = make_trading_executor(
            quality_scale_in_enabled=False,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 14.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('总资金15%=15.00USDT', te._get_risk_fail_reason(row))

    def test_quality_scale_in_allows_exposure_to_enhanced_25_percent_limit(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.15,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.25,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': {
                'available': 80.0,
                'net_value': 100.0,
                'cross_risk': {
                    'status': 'safe',
                    'account_mmr_pct': 800.0,
                    'account_fetched_at_ts': time.time(),
                },
            },
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 19.0
        te._holding_weighted_basis_by_asset['ALLO'] = 20.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertTrue(te._pass_risk_check(row))
        self.assertTrue(row.get('_quality_scale_in_used'))
        self.assertIn('15%->25%', row.get('_quality_scale_in_reason', ''))

    def test_quality_scale_in_rejects_above_25_percent_limit(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.15,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.25,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 24.0
        te._holding_weighted_basis_by_asset['ALLO'] = 20.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('增强25%=25.00', reason)
        self.assertIn('优质加仓拒绝', reason)

    def test_high_basis_quality_scale_in_allows_weak_funding_to_25_percent_limit(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            max_asset_exposure_ratio=0.15,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.25,
            high_basis_enabled=True,
            high_basis_scale_in_min_basis_improvement_bps=20.0,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
            asset_tier_meta={'ALLO': 'A'},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 19.0
        te._holding_weighted_basis_by_asset['ALLO'] = 40.0

        row = self._row('ALLO', 70.0, 0.0006)
        row.update({
            'funding_rate_24h_avg_bps': 2.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertTrue(te._pass_risk_check(row))
        self.assertEqual(row.get('_open_channel'), 'high_basis')
        self.assertTrue(row.get('_quality_scale_in_used'))
        reason = row.get('_quality_scale_in_reason', '')
        self.assertIn('高基差优质加仓额度', reason)
        self.assertIn('15%->25%', reason)
        self.assertIn('basis_improve=30.0/20.0bps', reason)

    def test_high_basis_quality_scale_in_is_checked_after_stable_funding_admission(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.20,
            high_basis_enabled=True,
            high_basis_scale_in_min_basis_improvement_bps=20.0,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
            asset_tier_meta={'ALLO': 'A'},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 40.0

        row = self._row('ALLO', 70.0, 0.0009)
        row.update({
            'funding_rate_24h_avg_bps': 9.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertTrue(te._pass_risk_check(row))
        self.assertEqual(row.get('_open_channel'), 'funding')
        self.assertTrue(row.get('_quality_scale_in_used'))
        self.assertIn('高基差优质加仓额度', row.get('_quality_scale_in_reason', ''))

    def test_quality_scale_in_reports_funding_and_high_basis_rejections(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            high_basis_enabled=True,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
            asset_tier_meta={'ALLO': 'A'},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 40.0

        row = self._row('ALLO', 38.0, 0.0009)
        row.update({
            'funding_rate_24h_avg_bps': 9.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('funding24h 9.0<50.0bps', reason)
        self.assertIn('高基差加仓不达标', reason)
        self.assertIn('基差垫', reason)

    def test_high_basis_quality_scale_in_requires_stricter_basis_improvement(self):
        te = make_trading_executor(
            min_funding_rate_bps=25.0,
            min_funding_support_bps=8.0,
            realtime_min_funding_rate_bps=5.0,
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.20,
            high_basis_enabled=True,
            high_basis_scale_in_min_basis_improvement_bps=20.0,
            vwap_threshold_meta={'ALLO': {'p20': 10.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': 0.0}},
            asset_tier_meta={'ALLO': 'A'},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 55.0

        row = self._row('ALLO', 70.0, 0.0006)
        row.update({
            'funding_rate_24h_avg_bps': 2.0,
            'funding_rate_24h_avg_samples': 3,
            'funding_rate_24h_avg_window_hours': 24,
        })

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('优质加仓拒绝', reason)
        self.assertIn('基差改善15.0<20.0bps', reason)

    def test_quality_scale_in_rejects_when_basis_not_enough_better(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.20,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 43.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('优质加仓拒绝', reason)
        self.assertIn('基差改善7.0<10.8bps', reason)

    def test_quality_scale_in_caps_relative_improvement_requirement(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.20,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 100.0

        row = self._row('ALLO', 120.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertTrue(te._pass_risk_check(row))
        self.assertIn('basis_improve=20.0/20.0bps', row.get('_quality_scale_in_reason', ''))

    def test_quality_scale_in_rejects_without_existing_weighted_basis(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.20,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(80.0, 100.0),
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('优质加仓拒绝', reason)
        self.assertIn('缺少已有仓位均价', reason)

    def test_quality_scale_in_is_blocked_when_gate_risk_is_missing(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.20,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': {'available': 80.0, 'net_value': 100.0},
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 20.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('Gate全仓风险未知', te._get_risk_fail_reason(row))
        self.assertNotIn('_quality_scale_in_used', row)

    def test_gate_danger_blocks_before_quality_scale_in(self):
        te = make_trading_executor(
            max_asset_exposure_ratio=0.10,
            quality_scale_in_enabled=True,
            quality_scale_in_enhanced_ratio=0.20,
            vwap_threshold_meta={'ALLO': {'p20': 0.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
        )
        te.capital_required = True
        te._account_summary = {
            'binance': {'available': 80.0, 'net_value': 100.0},
            'gate': {
                'available': 80.0,
                'net_value': 100.0,
                'cross_risk': {
                    'status': 'danger',
                    'status_label': '危险',
                    'account_mmr_pct': 249.4,
                    'account_fetched_at_ts': time.time(),
                },
            },
        }
        te._account_summary_ts = time.time()
        te._holding_spot_amount_by_asset['ALLO'] = 9.0
        te._holding_weighted_basis_by_asset['ALLO'] = 20.0

        row = self._row('ALLO', 50.0, 0.008646)
        row['open_amount_usdt'] = 2.0

        self.assertFalse(te._pass_risk_check(row))
        reason = te._get_risk_fail_reason(row)
        self.assertIn('Gate全仓风险危险', reason)
        self.assertIn('MMR=249.4%', reason)
        self.assertNotIn('优质加仓拒绝', reason)

    def test_presignal_rejection_is_rate_limited_by_asset_and_reason_category(self):
        te = make_trading_executor(presignal_reject_log_cooldown_sec=300)
        cursor = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor

        with patch('calc.trading_executor.db_manager.get_cursor', return_value=ctx):
            te._record_presignal_rejection('BEL', '单标的资金占用(Binance spot 345.07>总资金10%=330.92USDT)', 36.1)
            te._record_presignal_rejection('BEL', '单标的资金占用(Binance spot 345.08>总资金10%=330.92USDT)', 36.2)

        self.assertEqual(cursor.execute.call_count, 1)
        params = cursor.execute.call_args.args[1]
        self.assertEqual(params['base_asset'], 'BEL')
        self.assertEqual(params['trigger_type'], 'pre_risk')
        self.assertIn('预信号风控拒绝|单标的资金占用', params['exit_reason'])
        self.assertEqual(params['exit_basis_bps'], 36.1)

    def test_presignal_rejection_skips_plain_market_condition_failures(self):
        te = make_trading_executor(presignal_reject_log_cooldown_sec=300)
        cursor = MagicMock()
        ctx = MagicMock()
        ctx.__enter__.return_value = cursor

        with patch('calc.trading_executor.db_manager.get_cursor', return_value=ctx):
            te._record_presignal_rejection('BEL', '基差跌回入场门槛下(3.0<entry_floor=8.0bps)', 3.0)

        cursor.execute.assert_not_called()

    def test_low_funding_negative_p20_requires_positive_carry_floor(self):
        te = make_trading_executor(
            vwap_threshold_meta={'NFP': {'p20': -36.6}},
            close_vwap_threshold_meta={'NFP': {'close_basis_p20': -100}},
        )

        row = self._row('NFP', -12.0, 0.0003)
        snapshot = te._entry_snapshot('NFP', -12.0, row)

        self.assertAlmostEqual(snapshot['entry_floor_bps'], 38.5, places=1)
        self.assertFalse(te._pass_risk_check(row))
        self.assertIn('entry_floor=38.5', te._get_risk_fail_reason(row))

    def test_rebound_uses_entry_floor_instead_of_raw_p20(self):
        te = make_trading_executor(
            vwap_threshold_meta={'ALLO': {'p20': 30.0}},
            close_vwap_threshold_meta={'ALLO': {'close_basis_p20': -100}},
            asset_tier_meta={'ALLO': 'B'},
        )
        te.rebound_min_rise_bps = 4.0
        te.rebound_min_slope_bps = 0.5
        te.rebound_min_basis_buffer_bps = 2.0
        te._resolve_signal = MagicMock()
        row = self._row('ALLO', 25.0, 0.01)
        te._peak_state['ALLO'] = {
            'peak_bps': 60.0,
            'start_time': datetime.now(),
            'trigger': 'pullback',
            'signal_id': 1001,
            'signal_basis_bps': 60.0,
            'resiliency_active': True,
            'entry_snapshot': te._entry_snapshot('ALLO', 25.0, row),
        }

        self.assertFalse(te._pass_rebound_check('ALLO', 25.0, row))
        self.assertTrue(te._pass_rebound_check('ALLO', 29.0, self._row('ALLO', 29.0, 0.01)))
        self.assertEqual(te._peak_state['ALLO']['trigger'], 'rebound')
        te._resolve_signal.assert_not_called()

    def test_high_funding_extends_rebound_wait_window(self):
        te = make_trading_executor(
            vwap_threshold_meta={'CGPT': {'p20': 20.0}},
            close_vwap_threshold_meta={'CGPT': {'close_basis_p20': -100}},
            asset_tier_meta={'CGPT': 'B'},
        )
        te.rebound_min_rise_bps = 4.0
        te.rebound_min_slope_bps = 0.5
        te.rebound_min_basis_buffer_bps = 2.0
        te.rebound_max_wait_sec = 5.0
        te.rebound_high_funding_24h_bps = 50.0
        te.rebound_high_funding_max_wait_sec = 10.0
        te._resolve_signal = MagicMock()
        row = self._row('CGPT', 20.0, 0.006)
        te._peak_state['CGPT'] = {
            'peak_bps': 40.0,
            'start_time': datetime.now(),
            'trigger': 'pullback',
            'signal_id': 1001,
            'signal_basis_bps': 40.0,
            'resiliency_active': True,
            'entry_snapshot': te._entry_snapshot('CGPT', 20.0, row),
        }

        self.assertFalse(te._pass_rebound_check('CGPT', 20.0, row))
        te._peak_state['CGPT']['rebound_start_time'] = datetime.now() - timedelta(seconds=6)
        self.assertFalse(te._pass_rebound_check('CGPT', 21.0, row))
        te._resolve_signal.assert_not_called()

        te._peak_state['CGPT']['rebound_start_time'] = datetime.now() - timedelta(seconds=11)
        self.assertFalse(te._pass_rebound_check('CGPT', 21.0, row))
        reason = te._resolve_signal.call_args.args[2]
        self.assertIn('timeout=11.0/10.0s', reason)

    def test_funding_carry_allows_near_p20_before_standard_entry_floor(self):
        te = make_trading_executor(
            funding_carry_enabled=True,
            vwap_threshold_meta={'BANANA': {'p20': -18.0}},
            close_vwap_threshold_meta={'BANANA': {'close_basis_p20': -10.0}},
            asset_tier_meta={'BANANA': 'B'},
        )
        row = self._row('BANANA', -20.0, 0.0030)
        te._annotate_entry_snapshot(row, -20.0)
        snapshot = te._annotate_funding_carry_candidate(row, -20.0)

        self.assertIsNotNone(snapshot)
        self.assertAlmostEqual(snapshot['entry_floor_bps'], -25.0)
        self.assertTrue(te._pass_risk_check(row))

    def test_funding_carry_uses_30_min_next_funding_window(self):
        te = make_trading_executor(
            funding_carry_enabled=True,
            vwap_threshold_meta={'BANANA': {'p20': -18.0}},
            close_vwap_threshold_meta={'BANANA': {'close_basis_p20': -10.0}},
            asset_tier_meta={'BANANA': 'B'},
        )
        row = self._row('BANANA', -20.0, 0.0030)
        row['funding_next_apply'] = (datetime.now() + timedelta(minutes=29)).strftime('%Y-%m-%d %H:%M:%S')

        self.assertIsNotNone(te._annotate_funding_carry_candidate(row, -20.0))

        late_row = self._row('BANANA', -20.0, 0.0030)
        late_row['funding_next_apply'] = (datetime.now() + timedelta(minutes=31)).strftime('%Y-%m-%d %H:%M:%S')

        self.assertIsNone(te._annotate_funding_carry_candidate(late_row, -20.0))

    def test_funding_carry_creates_dedicated_signal_state(self):
        te = make_trading_executor(
            funding_carry_enabled=True,
            vwap_threshold_meta={'BANANA': {'p20': -18.0}},
            close_vwap_threshold_meta={'BANANA': {'close_basis_p20': -10.0}},
            asset_tier_meta={'BANANA': 'B'},
        )
        te._create_signal = MagicMock(return_value=123)
        te._verify_realtime_funding_rate = MagicMock(return_value=True)
        row = self._row('BANANA', -20.0, 0.0030)

        self.assertTrue(te._pass_funding_carry_check('BANANA', -20.0, row))
        self.assertEqual(te._peak_state['BANANA']['trigger'], 'funding_carry')
        self.assertEqual(te._peak_state['BANANA']['signal_id'], 123)

    def test_funding_carry_realtime_failure_clears_relaxed_entry_floor(self):
        te = make_trading_executor(
            funding_carry_enabled=True,
            vwap_threshold_meta={'BANANA': {'p20': -18.0}},
            close_vwap_threshold_meta={'BANANA': {'close_basis_p20': -10.0}},
            asset_tier_meta={'BANANA': 'B'},
        )
        row = self._row('BANANA', -20.0, 0.0030)
        te._annotate_entry_snapshot(row, -20.0)
        self.assertIsNotNone(te._annotate_funding_carry_candidate(row, -20.0))

        def fake_verify(base_asset, contract):
            te._last_realtime_funding_info[base_asset] = {
                'funding_rate': 0.000333,
                'funding_rate_24h': 0.0010,
                'funding_interval': 28800,
                'funding_next_apply': row['funding_next_apply'],
                'funding_last_apply': None,
            }
            return True

        te._verify_realtime_funding_rate = fake_verify

        self.assertFalse(te._pass_funding_carry_check('BANANA', -20.0, row))
        self.assertTrue(row.get('_funding_carry_realtime_rejected'))
        self.assertNotIn('_funding_carry_candidate', row)
        self.assertGreater(float(row['_entry_entry_floor_bps']), -20.0)
        self.assertNotIn('BANANA', te._peak_state)

    def test_funding_carry_fallback_uses_carry_entry_floor(self):
        te = make_trading_executor(
            funding_carry_enabled=True,
            vwap_threshold_meta={'BANANA': {'p20': -18.0}},
            close_vwap_threshold_meta={'BANANA': {'close_basis_p20': -10.0}},
            asset_tier_meta={'BANANA': 'B'},
        )
        row = self._row('BANANA', -20.0, 0.0030)
        snapshot = te._funding_carry_snapshot('BANANA', -20.0, row)
        te._peak_state['BANANA'] = {'entry_snapshot': snapshot, 'trigger': 'funding_carry'}

        self.assertAlmostEqual(te._fallback_min_open_basis(row, 'BANANA'), -17.0)

    def test_account_capital_check_uses_channel_amount(self):
        te = make_trading_executor()
        te.capital_required = True
        te.open_amount_usdt = 100.0
        te._account_summary = {
            'binance': {'available': 25.0, 'net_value': 100.0},
            'gate': fresh_safe_gate_summary(25.0, 100.0),
        }
        te._account_summary_ts = time.time()

        self.assertTrue(te._pass_account_capital_check(10.0))
        self.assertFalse(te._pass_account_capital_check(100.0))

    def test_rebound_timeout_cooldown_resets_when_basis_moves(self):
        te = make_trading_executor()
        te._start_rebound_timeout_cooldown('BTC', 50.0)

        self.assertFalse(te._pass_rebound_timeout_cooldown('BTC', 51.0))
        self.assertTrue(te._pass_rebound_timeout_cooldown('BTC', 55.0))

    def test_peak_timeout_cooldown_only_starts_for_timeout_trigger(self):
        te = make_trading_executor()
        te._peak_state['AI'] = {'trigger': 'timeout'}
        te._maybe_start_peak_timeout_cooldown('AI', 96.0, '行情滞后')

        self.assertFalse(te._pass_peak_timeout_cooldown('AI'))
        te._peak_timeout_cooldown['AI'] = datetime.now() - timedelta(seconds=1)
        self.assertTrue(te._pass_peak_timeout_cooldown('AI'))

        te._peak_state['HEI'] = {'trigger': 'pullback'}
        te._maybe_start_peak_timeout_cooldown('HEI', 50.0, '行情滞后')
        self.assertTrue(te._pass_peak_timeout_cooldown('HEI'))

    def test_execution_drift_cooldown_starts_after_bad_fill(self):
        te = make_trading_executor()
        te._maybe_start_execution_drift_cooldown(
            'OPN',
            {'pre_gate_basis_bps': 47.9, 'actual_basis_bps': -12.0},
        )

        self.assertFalse(te._pass_execution_drift_cooldown('OPN'))
        te._execution_drift_cooldown_until['OPN'] = datetime.now() - timedelta(seconds=1)
        self.assertTrue(te._pass_execution_drift_cooldown('OPN'))

    def test_open_marginal_basis_uses_fallback_taker_fee(self):
        te = make_trading_executor(
            contract_meta={'BANK': {'taker_fee_rate': 0.00075, 'maker_fee_rate': -0.0001}},
            asset_tier_meta={'BANK': 'B'},
        )
        te._fee_spot_open = 0.00075
        te._fee_future_open = 0.0002
        te._fee_future_taker_open = 0.0005
        te._risk_relief_bps = 10
        order_group = {
            'base_asset': 'BANK',
            'open_reason': 'x',
            'pre_gate_basis_bps': 22.2,
            'open_vwap_basis_bps': 22.2,
        }
        exec_result = {
            'success': True,
            'spot_order': {'exec_price': 0.033, 'exec_qty': 1, 'exec_amount': 0.033},
            'future_order': {'exec_price': 0.033066, 'exec_qty': 1, 'exec_amount': 0.033066},
            'execution_stats': {
                'future_maker': {
                    'attempted': True,
                    'filled': False,
                    'fallback_filled': True,
                }
            },
        }

        te._attach_actual_basis_audit(order_group, exec_result)

        self.assertAlmostEqual(order_group['actual_basis_bps'], 20.0)
        self.assertAlmostEqual(order_group['open_marginal_basis_bps'], 17.5)

    def test_open_marginal_basis_uses_contract_maker_fee_when_maker_fills(self):
        te = make_trading_executor(
            contract_meta={'BANK': {'taker_fee_rate': 0.00075, 'maker_fee_rate': -0.0001}},
            asset_tier_meta={'BANK': 'B'},
        )
        te._fee_spot_open = 0.00075
        te._fee_future_open = 0.0002
        te._fee_future_taker_open = 0.0005
        te._risk_relief_bps = 10
        order_group = {
            'base_asset': 'BANK',
            'open_reason': 'x',
            'pre_gate_basis_bps': 22.2,
            'open_vwap_basis_bps': 22.2,
        }
        exec_result = {
            'success': True,
            'spot_order': {'exec_price': 0.033, 'exec_qty': 1, 'exec_amount': 0.033},
            'future_order': {'exec_price': 0.033066, 'exec_qty': 1, 'exec_amount': 0.033066},
            'execution_stats': {
                'future_maker': {
                    'attempted': True,
                    'filled': True,
                    'fallback_filled': False,
                }
            },
        }

        te._attach_actual_basis_audit(order_group, exec_result)

        self.assertAlmostEqual(order_group['actual_basis_bps'], 20.0)
        self.assertAlmostEqual(order_group['open_marginal_basis_bps'], 20.5)


# ══════════════════════════════════════════════════════════════════
# Shared resiliency monitor 测试
# ══════════════════════════════════════════════════════════════════

class TestOrderBookResiliencyMonitor(unittest.TestCase):
    """共享盘口恢复状态机。"""

    def _row(self, future_bid_qty=10, future_ask_qty=10, spot_ask_qty=10,
             spot_bid_qty=10, basis=50.0, open_cov=0.4, close_cov=0.4):
        row = {
            'open_vwap_basis_bps': basis,
            'close_vwap_basis_bps': basis,
            'spot_open_coverage': open_cov,
            'future_open_coverage': open_cov,
            'spot_close_coverage': close_cov,
            'future_close_coverage': close_cov,
            '_future_qty_multiplier': 1.0,
        }
        for i in range(1, 21):
            row[f'spot_price_bid_{i}'] = 99.0
            row[f'spot_volume_bid_{i}'] = spot_bid_qty
            row[f'spot_price_ask_{i}'] = 100.0
            row[f'spot_volume_ask_{i}'] = spot_ask_qty
            row[f'future_price_bid_{i}'] = 101.0
            row[f'future_volume_bid_{i}'] = future_bid_qty
            row[f'future_price_ask_{i}'] = 102.0
            row[f'future_volume_ask_{i}'] = future_ask_qty
        return row

    def test_open_resiliency_waits_until_depth_recovers(self):
        from calc.orderbook_resiliency import (
            BookSideSpec, OrderBookResiliencyMonitor, ResiliencyConfig
        )

        monitor = OrderBookResiliencyMonitor(
            ResiliencyConfig(min_samples=2, min_hold_sec=0, max_wait_sec=5),
            [
                BookSideSpec('spot', 'ask', 1.0, 'spot_ask'),
                BookSideSpec('future', 'bid', 1.0, 'future_bid', '_future_qty_multiplier'),
            ],
            ['spot_open_coverage', 'future_open_coverage'],
            'open',
        )

        monitor.observe_shock('BTC', self._row(future_bid_qty=10))
        monitor.observe_shock('BTC', self._row(future_bid_qty=3))
        waiting = monitor.check('BTC', self._row(future_bid_qty=4), 50, 0.6, min_basis_bps=20)
        monitor.check('BTC', self._row(future_bid_qty=8), 51, 0.6, min_basis_bps=20)
        passed = monitor.check('BTC', self._row(future_bid_qty=8), 51, 0.6, min_basis_bps=20)

        self.assertFalse(waiting.passed)
        self.assertTrue(waiting.waiting)
        self.assertTrue(passed.passed)
        self.assertGreaterEqual(passed.metrics['recovery_ratio'], 0.65)

    def test_open_resiliency_timeout_is_terminal(self):
        from calc.orderbook_resiliency import (
            BookSideSpec, OrderBookResiliencyMonitor, ResiliencyConfig
        )

        monitor = OrderBookResiliencyMonitor(
            ResiliencyConfig(min_samples=1, min_recovery_ratio=0.9, max_wait_sec=0.01),
            [BookSideSpec('future', 'bid', 1.0, 'future_bid', '_future_qty_multiplier')],
            ['future_open_coverage'],
            'open',
        )
        monitor.observe_shock('BTC', self._row(future_bid_qty=10))
        monitor.observe_shock('BTC', self._row(future_bid_qty=2))
        monitor.check('BTC', self._row(future_bid_qty=3), 50, 0.6, min_basis_bps=20)
        time.sleep(0.02)
        result = monitor.check('BTC', self._row(future_bid_qty=3), 50, 0.6, min_basis_bps=20)

        self.assertFalse(result.passed)
        self.assertTrue(result.terminal)
        self.assertIn('timeout', result.reason)

    def test_close_resiliency_timeout_can_pass_to_gate(self):
        from calc.orderbook_resiliency import (
            BookSideSpec, OrderBookResiliencyMonitor, ResiliencyConfig
        )

        monitor = OrderBookResiliencyMonitor(
            ResiliencyConfig(
                min_samples=1, min_recovery_ratio=0.9, max_wait_sec=0.01,
                allow_timeout_pass=True,
            ),
            [BookSideSpec('future', 'ask', 1.0, 'future_ask', '_future_qty_multiplier')],
            ['future_close_coverage'],
            'close',
        )
        monitor.observe_shock('BTC', self._row(future_ask_qty=10))
        monitor.observe_shock('BTC', self._row(future_ask_qty=2))
        monitor.check('BTC', self._row(future_ask_qty=3), 20, 0.6, max_basis_bps=100)
        time.sleep(0.02)
        result = monitor.check('BTC', self._row(future_ask_qty=3), 20, 0.6, max_basis_bps=100)

        self.assertTrue(result.passed)
        self.assertIn('timeout_pass', result.reason)


class TestTradingExecutorOpenFlowResiliency(unittest.TestCase):
    """开仓主流程里的 RESILIENCY_WAIT 持续采样。"""

    def _row(self, basis):
        row = {
            'base_asset': 'BTC',
            'contract': 'BTC_USDT',
            'symbol': 'BTCUSDT',
            'spot_qty': 1.0,
            'open_vwap_basis_bps': basis,
            'spot_open_coverage': 0.1,
            'future_open_coverage': 0.1,
            'open_coverage': 0.1,
            'funding_rate_24h': 0.001,
        }
        for i in range(1, 21):
            row[f'spot_price_bid_{i}'] = 99.0
            row[f'spot_volume_bid_{i}'] = 10.0
            row[f'spot_price_ask_{i}'] = 100.0
            row[f'spot_volume_ask_{i}'] = 10.0
            row[f'future_price_bid_{i}'] = 101.0
            row[f'future_volume_bid_{i}'] = 10.0
            row[f'future_price_ask_{i}'] = 102.0
            row[f'future_volume_ask_{i}'] = 10.0
        return row

    def test_pullback_enters_resiliency_wait_and_keeps_sampling(self):
        te = make_trading_executor(
            sustain_sec=0.0,
            peak_pullback_pct=0.10,
            basis_threshold_bps=20,
            coverage_threshold=0.8,
            vwap_threshold_meta={'BTC': {'p20': 20}},
            close_vwap_threshold_meta={'BTC': {'close_basis_p20': -100}},
        )
        te._refresh_holding_exposure_from_db = MagicMock()
        te._load_open_cooldown_from_db = MagicMock()
        te._verify_realtime_funding_rate = MagicMock(return_value=True)
        te._create_signal = MagicMock(return_value=1001)
        te._resolve_signal = MagicMock()
        te._pass_open_resiliency_check = MagicMock(return_value=False)

        te.check_and_open([self._row(100.0)])
        self.assertFalse(te._pass_open_resiliency_check.called)

        te.check_and_open([self._row(90.0)])
        self.assertEqual(te._pass_open_resiliency_check.call_count, 1)
        self.assertTrue(te._peak_state['BTC']['resiliency_active'])

        # basis 反弹到 pullback 阈值上方；仍应继续跑 resiliency，而不是回到 peak 等待。
        te.check_and_open([self._row(99.0)])
        self.assertEqual(te._pass_open_resiliency_check.call_count, 2)


class TestTradingExecutorTierMomentum(unittest.TestCase):
    """A 级允许 momentum；A 未命中 momentum 和 B 级都必须走 rebound。"""

    def _row(self, base_asset, basis):
        return {
            'base_asset': base_asset,
            'contract': f'{base_asset}_USDT',
            'symbol': f'{base_asset}USDT',
            'open_vwap_basis_bps': basis,
            'open_coverage': 0.1,
            'funding_rate_24h': 0.001,
        }

    def _executor(self, tier):
        base_asset = 'BTC'
        te = make_trading_executor(
            basis_threshold_bps=20,
            coverage_threshold=0.8,
            vwap_threshold_meta={base_asset: {'p20': 20}},
            close_vwap_threshold_meta={base_asset: {'close_basis_p20': -100}},
            asset_tier_meta={base_asset: tier},
            momentum_enabled=True,
            momentum_allowed_tiers=['A'],
            momentum_tier_overrides={
                'A': {
                    'window_sec': 1.0,
                    'min_samples': 3,
                    'min_rise_bps': 3,
                    'min_basis_buffer_bps': 6,
                    'safety_bps': 6,
                }
            },
        )
        te._verify_realtime_funding_rate = MagicMock(return_value=True)
        te._create_signal = MagicMock(return_value=1001)
        return te

    def test_a_tier_can_enter_momentum_channel(self):
        te = self._executor('A')
        for basis in [30.0, 36.0, 42.0]:
            te._record_momentum_sample('BTC', basis)

        self.assertTrue(te._pass_momentum_check('BTC', 42.0, self._row('BTC', 42.0)))
        self.assertEqual(te._peak_state['BTC']['trigger'], 'momentum')
        self.assertEqual(te._peak_state['BTC']['strategy_tier'], 'A')

    def test_b_tier_does_not_enter_momentum_channel(self):
        te = self._executor('B')
        for basis in [30.0, 36.0, 42.0]:
            te._record_momentum_sample('BTC', basis)

        self.assertFalse(te._pass_momentum_check('BTC', 42.0, self._row('BTC', 42.0)))
        self.assertNotIn('BTC', te._peak_state)

    def _assert_waits_for_rebound_after_pullback_resiliency(self, tier):
        te = make_trading_executor(
            basis_threshold_bps=20,
            vwap_threshold_meta={'BTC': {'p20': 20}},
            close_vwap_threshold_meta={'BTC': {'close_basis_p20': -100}},
            asset_tier_meta={'BTC': tier},
            rebound_enabled=True,
            rebound_allowed_tiers=['A', 'B'],
        )
        te.rebound_min_rise_bps = 4.0
        te.rebound_min_slope_bps = 0.5
        te.rebound_min_basis_buffer_bps = 2.0
        te._resolve_signal = MagicMock()
        te._peak_state['BTC'] = {
            'peak_bps': 50.0,
            'start_time': datetime.now(),
            'trigger': 'pullback',
            'signal_id': 1001,
            'signal_basis_bps': 50.0,
            'resiliency_active': True,
        }

        self.assertFalse(te._pass_rebound_check('BTC', 42.0, self._row('BTC', 42.0)))
        self.assertFalse(te._pass_rebound_check('BTC', 44.0, self._row('BTC', 44.0)))
        self.assertTrue(te._pass_rebound_check('BTC', 46.5, self._row('BTC', 46.5)))
        self.assertEqual(te._peak_state['BTC']['trigger'], 'rebound')
        self.assertAlmostEqual(te._peak_state['BTC']['rebound_rise_bps'], 4.5)

    def test_a_tier_waits_for_rebound_when_momentum_not_used(self):
        self._assert_waits_for_rebound_after_pullback_resiliency('A')

    def test_b_tier_waits_for_rebound_after_pullback_resiliency(self):
        self._assert_waits_for_rebound_after_pullback_resiliency('B')

    def test_rebound_strong_cushion_passes_after_hold_without_extra_rise(self):
        te = make_trading_executor(
            basis_threshold_bps=20,
            vwap_threshold_meta={'BTC': {'p20': 20}},
            close_vwap_threshold_meta={'BTC': {'close_basis_p20': -100}},
            asset_tier_meta={'BTC': 'B'},
            rebound_enabled=True,
            rebound_allowed_tiers=['A', 'B'],
        )
        te.rebound_min_rise_bps = 4.0
        te.rebound_min_slope_bps = 0.5
        te.rebound_min_basis_buffer_bps = 2.0
        te.rebound_strong_cushion_bps = 20.0
        te.rebound_strong_cushion_min_hold_sec = 1.0
        te._resolve_signal = MagicMock()
        te._peak_state['BTC'] = {
            'peak_bps': 70.0,
            'start_time': datetime.now(),
            'trigger': 'pullback',
            'signal_id': 1001,
            'signal_basis_bps': 70.0,
            'resiliency_active': True,
        }

        self.assertFalse(te._pass_rebound_check('BTC', 56.0, self._row('BTC', 56.0)))
        te._peak_state['BTC']['rebound_strong_cushion_start_time'] = (
            datetime.now() - timedelta(seconds=2)
        )

        self.assertTrue(te._pass_rebound_check('BTC', 56.0, self._row('BTC', 56.0)))
        self.assertEqual(te._peak_state['BTC']['trigger'], 'rebound_strong')
        self.assertAlmostEqual(te._peak_state['BTC']['rebound_rise_bps'], 0.0)
        self.assertGreaterEqual(te._peak_state['BTC']['rebound_strong_cushion_bps'], 20.0)
        self.assertLessEqual(len(te._peak_state['BTC']['trigger']), 20)
        te._resolve_signal.assert_not_called()

    def test_rebound_timeout_reason_includes_threshold_breakdown(self):
        te = make_trading_executor(
            basis_threshold_bps=20,
            vwap_threshold_meta={'BTC': {'p20': 20}},
            close_vwap_threshold_meta={'BTC': {'close_basis_p20': -100}},
            asset_tier_meta={'BTC': 'B'},
            rebound_enabled=True,
            rebound_allowed_tiers=['A', 'B'],
        )
        te.rebound_min_rise_bps = 4.0
        te.rebound_min_slope_bps = 0.5
        te.rebound_min_basis_buffer_bps = 2.0
        te.rebound_max_wait_sec = 1.0
        te._resolve_signal = MagicMock()
        te._peak_state['BTC'] = {
            'peak_bps': 50.0,
            'start_time': datetime.now(),
            'trigger': 'pullback',
            'signal_id': 1001,
            'signal_basis_bps': 50.0,
            'resiliency_active': True,
        }

        self.assertFalse(te._pass_rebound_check('BTC', 42.0, self._row('BTC', 42.0)))
        te._peak_state['BTC']['rebound_start_time'] = datetime.now() - timedelta(seconds=2)

        self.assertFalse(te._pass_rebound_check('BTC', 45.0, self._row('BTC', 45.0)))
        reason = te._resolve_signal.call_args.args[2]
        self.assertIn('floor=42.0,current=45.0', reason)
        self.assertIn('rise=3.0/4.0bps', reason)
        self.assertIn('slope=3.0/0.5bps', reason)
        self.assertIn('min_basis=', reason)
        self.assertIn('entry_floor=', reason)
        self.assertIn('+buffer=2.0', reason)
        self.assertIn('strong_cushion=', reason)
        self.assertIn('strong_hold=', reason)
        self.assertIn('timeout=2.0/1.0s', reason)

    def test_pullback_direct_open_is_blocked_for_non_rebound_tier(self):
        te = make_trading_executor(
            basis_threshold_bps=20,
            vwap_threshold_meta={'BTC': {'p20': 20}},
            close_vwap_threshold_meta={'BTC': {'close_basis_p20': -100}},
            asset_tier_meta={'BTC': 'C'},
            rebound_enabled=True,
            rebound_allowed_tiers=['A', 'B'],
        )
        te._resolve_signal = MagicMock()
        te._peak_state['BTC'] = {
            'peak_bps': 50.0,
            'start_time': datetime.now(),
            'trigger': 'pullback',
            'signal_id': 1001,
            'signal_basis_bps': 50.0,
            'resiliency_active': True,
        }

        self.assertFalse(te._pass_rebound_check('BTC', 42.0, self._row('BTC', 42.0)))
        te._resolve_signal.assert_called_once()
        self.assertNotIn('BTC', te._peak_state)


# ══════════════════════════════════════════════════════════════════
# ClosingExecutor 测试
# ══════════════════════════════════════════════════════════════════

class TestClosingExecutorValleyCheck(unittest.TestCase):
    """谷底反弹确认（止盈唯一确认通道）"""

    def setUp(self):
        self.ce = make_closing_executor()

        self.pos = {
            'base_asset': 'BTC',
            'future_contract': 'BTC_USDT',
            'spot_symbol': 'BTCUSDT',
            'open_spread_bps': 100.0,
        }

    def test_first_entry_records_valley_returns_false(self):
        """首次进入：记录谷底，返回 False"""
        ret = self.ce._pass_valley_check('BTC', 50.0, self.pos)
        self.assertFalse(ret)
        state = self.ce._valley_state['BTC']
        self.assertEqual(state['valley_bps'], 50.0)
        self.assertEqual(state['open_spread_bps'], 100.0)
        self.assertIsNone(state['trigger'])

    def test_lower_spread_updates_valley(self):
        """spread 继续下降 → 更新谷底"""
        self.ce._pass_valley_check('BTC', 50.0, self.pos)
        ret = self.ce._pass_valley_check('BTC', 30.0, self.pos)
        self.assertFalse(ret)
        self.assertEqual(self.ce._valley_state['BTC']['valley_bps'], 30.0)

    def test_rebound_pct_not_reached_returns_false(self):
        """谷底回升但未达 rebound_pct → 等待"""
        self.ce._pass_valley_check('BTC', 50.0, self.pos)
        # convergence_range = 100 - 50 = 50, rebound_thr = 50 + 50*0.10 = 55
        ret = self.ce._pass_valley_check('BTC', 53.0, self.pos)
        self.assertFalse(ret)

    def test_rebound_pass_sets_trigger(self):
        """谷底反弹 ≥ rebound_pct → 通过，trigger=rebound"""
        self.ce._pass_valley_check('BTC', 50.0, self.pos)
        # current=58 > rebound_thr=55
        ret = self.ce._pass_valley_check('BTC', 58.0, self.pos)
        self.assertTrue(ret)
        self.assertEqual(self.ce._valley_state['BTC']['trigger'], 'rebound')

    def test_timeout_passes_with_trigger_timeout(self):
        """监控超时 → 直接平仓，trigger=timeout"""
        self.ce._pass_valley_check('BTC', 50.0, self.pos)
        # 推早 monitor_timeout_sec + 5 秒
        timeout_sec = self.ce.valley_monitor_timeout_sec
        self.ce._valley_state['BTC']['start_time'] = (
            datetime.now() - timedelta(seconds=timeout_sec + 5)
        )
        ret = self.ce._pass_valley_check('BTC', 51.0, self.pos)
        self.assertTrue(ret)
        self.assertEqual(self.ce._valley_state['BTC']['trigger'], 'timeout')

    def test_valley_above_open_spread_anomaly_passes(self):
        """异常：谷底 ≥ 开仓基差（convergence_range ≤ 0）→ 直接平仓"""
        # 首次记录谷底 = 110，open_spread_bps = 100 → range = -10
        self.ce._pass_valley_check('BTC', 110.0, self.pos)
        ret = self.ce._pass_valley_check('BTC', 110.0, self.pos)
        self.assertTrue(ret)
        self.assertEqual(self.ce._valley_state['BTC']['trigger'], 'rebound')

    def test_same_asset_positions_keep_independent_valleys(self):
        """同一标的多笔持仓按 position_id 隔离谷底状态。"""
        pos1 = dict(self.pos, id=101, open_spread_bps=100.0)
        pos2 = dict(self.pos, id=102, open_spread_bps=80.0)

        self.ce._pass_valley_check('BTC', 50.0, pos1)
        self.ce._pass_valley_check('BTC', 30.0, pos2)

        self.assertEqual(self.ce._valley_state[101]['valley_bps'], 50.0)
        self.assertEqual(self.ce._valley_state[102]['valley_bps'], 30.0)


class TestClosingExecutorPreExecutionGate(unittest.TestCase):
    """平仓最终风控旁路 6 个分支"""

    def setUp(self):
        self.ce = make_closing_executor()
        self.ce.fixed_take_profit_bps = 30.0
        self.pos = {
            'base_asset': 'BTC',
            'future_contract': 'BTC_USDT',
            'spot_symbol': 'BTCUSDT',
            'open_spread_bps': 100.0,
            'current_spread_bps': 30.0,
        }

    def _setup_books(self, gate_lag_sec=0.05, spot_lag_sec=0.05,
                     gate_uc=10, spot_uc=10):
        now = time.time()
        gate_book = FakeOrderBook(
            last_update_time=now - gate_lag_sec,
            update_count=gate_uc,
            row={'contract': 'BTC_USDT'},
        )
        spot_book = FakeOrderBook(
            last_update_time=now - spot_lag_sec,
            update_count=spot_uc,
            row={
                'symbol': 'BTCUSDT',
                'spot_price_ask_1': 2.0,
                'spot_price_bid_1': 1.99,
            },
        )
        self.ce.set_orderbook_managers(
            FakeManager({'BTC_USDT': gate_book}),
            FakeManager({'BTCUSDT': spot_book}),
        )

    def _patch_gate_chain(self, vwap_basis_bps):
        merge_mock = MagicMock(return_value=[{'_': 'merged'}])
        hedge_mock = MagicMock(return_value=[{
            'spot_close_vwap': 100.0,
            'future_close_vwap': 100.0,
        }])
        vwap_mock = MagicMock(return_value=vwap_basis_bps)
        return (
            patch('calc.merge_cross_exchange_orderbook.merge_orderbook_records', merge_mock),
            patch('calc.calculate_hedge_metrics.calculate_hedge_metrics', hedge_mock),
            patch('calc.closing_executor.calc_vwap_basis_bps', vwap_mock),
        )

    def test_manager_not_injected_passes(self):
        passed, _, _, reason = self.ce._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT', self.pos)
        self.assertTrue(passed)
        self.assertEqual(reason, '')

    def test_orderbook_missing_blocks(self):
        self.ce.set_orderbook_managers(FakeManager({}), FakeManager({}))
        passed, _, _, reason = self.ce._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT', self.pos)
        self.assertFalse(passed)
        self.assertIn('盘口不可用', reason)

    def test_lag_exceeds_blocks(self):
        self._setup_books(spot_lag_sec=0.5)  # 500ms
        passed, _, _, reason = self.ce._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT', self.pos)
        self.assertFalse(passed)
        self.assertIn('行情滞后', reason)

    def test_uc_gate_removed_no_longer_blocks(self):
        """已移除 update_count 闸：有 valley_state 但不再校验 uc 增量，仅依赖 lag_ms"""
        self.ce._valley_state['BTC'] = {
            'valley_bps': 30.0,
            'start_time': datetime.now() - timedelta(seconds=3),
            'open_spread_bps': 100.0,
            'trigger': 'rebound',
        }
        # lag 小于阈值，不会被 uc 闸拦截
        self._setup_books(gate_uc=101, spot_uc=101)
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=35)
        with m_merge, m_hedge, m_vwap:
            passed, _, _, reason = self.ce._pre_execution_gate(
                'BTC', 'BTC_USDT', 'BTCUSDT', self.pos
            )
        self.assertTrue(passed)

    def test_convergence_reversed_blocks(self):
        """收敛逆转：gate_basis ≥ open_spread → 拦截"""
        self._setup_books()
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=105)  # >= 100
        with m_merge, m_hedge, m_vwap:
            passed, _, basis, reason = self.ce._pre_execution_gate(
                'BTC', 'BTC_USDT', 'BTCUSDT', self.pos
            )
        self.assertFalse(passed)
        self.assertIn('收敛逆转', reason)
        self.assertEqual(basis, 105)

    def test_excess_rebound_blocks(self):
        """基差回弹比 > 50% → 拦截
        original_convergence = 100 - 30 = 70
        若 gate_basis = 70, reversion = (70-30)/70 = 57% > 50% → 拦
        """
        self._setup_books()
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=70)
        with m_merge, m_hedge, m_vwap:
            passed, _, _, reason = self.ce._pre_execution_gate(
                'BTC', 'BTC_USDT', 'BTCUSDT', self.pos
            )
        self.assertFalse(passed)
        self.assertIn('基差回弹过大', reason)

    def test_fixed_net_take_profit_shortfall_blocks(self):
        """固定净止盈复核不足 → 旁路拦截。"""
        self.ce.fixed_take_profit_bps = 50.0
        self._setup_books()
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=35)
        with m_merge, m_hedge, m_vwap:
            passed, _, basis, reason = self.ce._pre_execution_gate(
                'BTC', 'BTC_USDT', 'BTCUSDT', self.pos
            )
        self.assertFalse(passed)
        self.assertEqual(basis, 35)
        self.assertIn('动态净止盈不足', reason)

    def test_risk_gate_does_not_require_profit(self):
        """风险平仓旁路只查执行质量，不用收敛盈利性挡住退出。"""
        self._setup_books()
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=105)
        with m_merge, m_hedge, m_vwap:
            passed, _, basis, reason = self.ce._pre_execution_gate(
                'BTC', 'BTC_USDT', 'BTCUSDT', self.pos, require_profit=False
            )
        self.assertTrue(passed)
        self.assertEqual(basis, 105)
        self.assertEqual(reason, '')

    def test_delist_risk_exit_does_not_require_profit(self):
        """临近下架退出复用风险平仓旁路，不被盈利性复核挡住。"""
        self._setup_books()
        self.ce.set_delist_risk_report({
            'items': [{
                'base_asset': 'BTC',
                'exchange': 'binance',
                'market_type': 'spot',
                'risk_type': 'delist_schedule',
                'status': 'scheduled',
                'risk_level': 'critical',
                'delist_at': (datetime.now() + timedelta(hours=36)).strftime('%Y-%m-%d %H:%M:%S'),
                'days_left': 1,
                'message': 'Binance现货已进入下架计划',
            }],
        })
        pos = dict(self.pos)
        pos.update({'status': 'holding'})
        execute_mock = MagicMock(return_value={
            'base_asset': 'BTC',
            'success': True,
            'order_uuid': 'delist-close',
            'close_reason': 'delist_risk_exit',
            'message': None,
        })
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=105)
        with (
            patch.object(self.ce, '_check_take_profit', return_value=True) as take_profit_mock,
            patch.object(self.ce, '_execute_close', execute_mock),
            m_merge, m_hedge, m_vwap,
        ):
            results = self.ce.check_and_close([pos], {}, {'BTC': {'old': 'row'}})

        self.assertEqual(len(results), 1)
        self.assertEqual(execute_mock.call_args.args[1], 'delist_risk_exit')
        self.assertIn('下架风险退出', execute_mock.call_args.args[2])
        self.assertIn('旁路✓', execute_mock.call_args.args[2])
        take_profit_mock.assert_not_called()

    def test_full_pass_writes_lag_cache(self):
        """全部通过 → 返回 True + 写入 _last_orderbook_lag_ms（bug 修复验证）"""
        self._setup_books(gate_lag_sec=0.05, spot_lag_sec=0.06)
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=35)  # < open=100
        with m_merge, m_hedge, m_vwap:
            passed, row, basis, reason = self.ce._pre_execution_gate(
                'BTC', 'BTC_USDT', 'BTCUSDT', self.pos
            )
        self.assertTrue(passed)
        self.assertEqual(reason, '')
        self.assertEqual(basis, 35)
        # bug 修复验证：通过路径必须写入 lag 缓存（之前的 bug 是漏写）
        self.assertIn('BTC', self.ce._last_orderbook_lag_ms)
        gate_lag, spot_lag = self.ce._last_orderbook_lag_ms['BTC']
        self.assertAlmostEqual(gate_lag, 50, delta=30)
        self.assertAlmostEqual(spot_lag, 60, delta=30)

    def test_pre_execution_vwap_uses_actual_position_quantity(self):
        """旁路按真实待平数量计算深度，不再固定使用普通开仓金额。"""
        self._setup_books()
        pos = dict(self.pos)
        pos.update({
            'spot_open_qty': 297.0,
            'future_open_qty': 297.0,
        })
        merge_mock = MagicMock(return_value=[{'_': 'merged'}])
        hedge_mock = MagicMock(return_value=[{
            'spot_close_vwap': 1.99,
            'future_close_vwap': 1.98,
        }])
        with (
            patch(
                'calc.merge_cross_exchange_orderbook.merge_orderbook_records',
                merge_mock,
            ),
            patch(
                'calc.calculate_hedge_metrics.calculate_hedge_metrics',
                hedge_mock,
            ),
            patch('calc.closing_executor.calc_vwap_basis_bps', return_value=35.0),
        ):
            passed, row, _, _ = self.ce._pre_execution_gate(
                'BTC', 'BTC_USDT', 'BTCUSDT', pos
            )

        self.assertTrue(passed)
        self.assertAlmostEqual(hedge_mock.call_args.args[3], 594.0)
        self.assertAlmostEqual(row['close_target_amount_usdt'], 594.0)

    def test_full_pass_then_build_take_profit_detail_consumes_lag(self):
        """端到端验证：通过路径写 lag → _build_take_profit_detail 弹出并拼接到原因"""
        self._setup_books()
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=35)
        with m_merge, m_hedge, m_vwap:
            self.ce._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT', self.pos)
        # valley_state 也注入，让鲜度+触发都拼上
        self.ce._valley_state['BTC'] = {
            'valley_bps': 30.0,
            'start_time': datetime.now(),
            'open_spread_bps': 100.0,
            'trigger': 'rebound',
        }
        detail = self.ce._build_take_profit_detail(self.pos, 35.0)
        self.assertIn('鲜度(gate=', detail)
        self.assertIn('谷底反弹', detail)
        # 消费一次后再调用 → 应回退到 NA
        detail2 = self.ce._build_take_profit_detail(self.pos, 35.0)
        self.assertIn('鲜度(NA)', detail2)

    def test_check_and_close_builds_take_profit_detail_after_gate(self):
        """真实平仓主流程：旁路先写 lag，止盈详情再消费，避免鲜度显示 NA。"""
        self._setup_books(gate_lag_sec=0.04, spot_lag_sec=0.05)
        pos = dict(self.pos)
        pos.update({
            'status': 'holding',
            'current_spread_bps': 35.0,
            'funding_pnl_bps': 0.0,
        })
        self.ce._valley_state['BTC'] = {
            'valley_bps': 30.0,
            'start_time': datetime.now(),
            'open_spread_bps': 100.0,
            'trigger': 'rebound',
        }

        execute_mock = MagicMock(return_value={
            'base_asset': 'BTC',
            'success': True,
            'order_uuid': 'test-order',
            'close_reason': 'take_profit',
            'message': None,
        })
        m_merge, m_hedge, m_vwap = self._patch_gate_chain(vwap_basis_bps=36)
        with (
            patch.object(self.ce, '_check_funding_count', return_value=False),
            patch.object(self.ce, '_check_take_profit', return_value=True),
            patch.object(self.ce, '_pass_valley_check', return_value=True),
            patch.object(self.ce, '_pass_close_resiliency_check', return_value=True),
            patch.object(self.ce, '_execute_close', execute_mock),
            m_merge, m_hedge, m_vwap,
        ):
            results = self.ce.check_and_close([pos], {}, {'BTC': {'old': 'row'}})

        self.assertEqual(len(results), 1)
        detail = execute_mock.call_args.args[2]
        self.assertIn('鲜度(gate=', detail)
        self.assertNotIn('鲜度(NA)', detail)
        self.assertIn('旁路✓', detail)
        self.assertIn('保护IOC', detail)
        self.assertAlmostEqual(
            execute_mock.call_args.kwargs['future_protective_price'],
            100.05,
        )

    def test_take_profit_batch_guard_stops_same_asset_after_bad_probe(self):
        """同标的多笔止盈时，首笔成交质量差则本轮不继续批量平仓。"""
        positions = [
            {
                'id': 1,
                'status': 'holding',
                'base_asset': 'BTC',
                'future_contract': 'BTC_USDT',
                'spot_symbol': 'BTCUSDT',
                'open_spread_bps': 100.0,
                'current_spread_bps': 35.0,
            },
            {
                'id': 2,
                'status': 'holding',
                'base_asset': 'BTC',
                'future_contract': 'BTC_USDT',
                'spot_symbol': 'BTCUSDT',
                'open_spread_bps': 90.0,
                'current_spread_bps': 30.0,
            },
        ]
        execute_mock = MagicMock(return_value={
            'base_asset': 'BTC',
            'success': True,
            'order_uuid': 'probe-order',
            'close_reason': 'take_profit',
            'message': None,
            'close_basis_slip_bps': 12.0,
        })
        gate_mock = MagicMock(return_value=(
            True,
            {'fresh': 'row', 'future_close_vwap': 100.0},
            10.0,
            '',
        ))

        with (
            patch.object(self.ce, '_check_negative_funding_exit', return_value=False),
            patch.object(self.ce, '_check_funding_count', return_value=False),
            patch.object(self.ce, '_check_take_profit', return_value=True),
            patch.object(self.ce, '_pass_valley_check', return_value=True),
            patch.object(self.ce, '_pass_close_resiliency_check', return_value=True),
            patch.object(self.ce, '_pre_execution_gate', gate_mock),
            patch.object(self.ce, '_build_take_profit_detail', return_value='动态止盈'),
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close(positions, {}, {'BTC': {'old': 'row'}})
            results_again = self.ce.check_and_close(positions, {}, {'BTC': {'old': 'row'}})

        self.assertEqual(len(results), 1)
        execute_mock.assert_called_once()
        gate_mock.assert_called_once()
        self.assertEqual(results_again, [])
        execute_mock.assert_called_once()
        gate_mock.assert_called_once()

    def test_take_profit_batch_guard_continues_same_asset_after_good_probe(self):
        """首笔成交质量好时，本轮可继续处理同标的其它止盈仓位。"""
        positions = [
            {
                'id': 1,
                'status': 'holding',
                'base_asset': 'BTC',
                'future_contract': 'BTC_USDT',
                'spot_symbol': 'BTCUSDT',
                'open_spread_bps': 100.0,
                'current_spread_bps': 35.0,
            },
            {
                'id': 2,
                'status': 'holding',
                'base_asset': 'BTC',
                'future_contract': 'BTC_USDT',
                'spot_symbol': 'BTCUSDT',
                'open_spread_bps': 90.0,
                'current_spread_bps': 30.0,
            },
        ]
        execute_mock = MagicMock(side_effect=[
            {
                'base_asset': 'BTC',
                'success': True,
                'order_uuid': 'probe-order',
                'close_reason': 'take_profit',
                'message': None,
                'close_basis_slip_bps': 3.0,
            },
            {
                'base_asset': 'BTC',
                'success': True,
                'order_uuid': 'batch-order',
                'close_reason': 'take_profit',
                'message': None,
                'close_basis_slip_bps': 2.0,
            },
        ])
        gate_mock = MagicMock(return_value=(
            True,
            {'fresh': 'row', 'future_close_vwap': 100.0},
            10.0,
            '',
        ))

        with (
            patch.object(self.ce, '_check_negative_funding_exit', return_value=False),
            patch.object(self.ce, '_check_funding_count', return_value=False),
            patch.object(self.ce, '_check_take_profit', return_value=True),
            patch.object(self.ce, '_pass_valley_check', return_value=True),
            patch.object(self.ce, '_pass_close_resiliency_check', return_value=True),
            patch.object(self.ce, '_pre_execution_gate', gate_mock),
            patch.object(self.ce, '_build_take_profit_detail', return_value='动态止盈'),
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close(positions, {}, {'BTC': {'old': 'row'}})

        self.assertEqual(len(results), 2)
        self.assertEqual(execute_mock.call_count, 2)
        self.assertEqual(gate_mock.call_count, 2)


class TestClosingExecutorFundingAwareClose(unittest.TestCase):
    """固定净止盈 + funding-aware 平仓触发。"""

    def setUp(self):
        self.ce = make_closing_executor()
        self.ce.fixed_take_profit_bps = 50.0
        self.ce.contract_meta = {'BTC': {'funding_interval': 28800}}
        self.pos = {
            'base_asset': 'BTC',
            'open_spread_bps': 120.0,
            'current_spread_bps': 40.0,
            'funding_pnl_bps': 0.0,
            'funding_rate_24h': 0.0,
            'funding_next_apply': (datetime.now() + timedelta(minutes=120)).strftime('%Y-%m-%d %H:%M:%S'),
        }

    def _bel_close_position(self, qty=410.0):
        return {
            'id': 247,
            'base_asset': 'BEL',
            'spot_symbol': 'BELUSDT',
            'future_contract': 'BEL_USDT',
            'spot_open_qty': qty,
            'spot_open_price': 0.12,
            'spot_open_amount': qty * 0.12,
            'future_open_qty': qty,
            'future_open_price': 0.121,
            'future_open_contracts': qty,
            'future_open_leverage': 10,
            'current_spread_bps': -25.0,
            'funding_rate_24h': 0.0,
        }

    def _bel_close_order_group(self, qty=410.0):
        return {
            'order_uuid': 'bel-close',
            'spot_order': {
                'order_uuid': 'bel-close',
                'base_asset': 'BEL',
                'spot_symbol': 'BELUSDT',
                'future_contract': None,
                'order_side': 'close',
                'market_type': 'spot',
                'trade_direction': 'sell',
                'target_qty': qty,
                'target_amount': 50.0,
            },
            'future_order': {
                'order_uuid': 'bel-close',
                'base_asset': 'BEL',
                'spot_symbol': None,
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'market_type': 'future',
                'trade_direction': 'buy',
                'target_qty': qty,
                'target_amount': 50.0,
            },
        }

    def _close_exec_result(self, spot_qty, future_qty):
        return {
            'success': True,
            'spot_order': {
                'exec_price': 0.1774,
                'exec_qty': spot_qty,
                'exec_amount': 0.1774 * spot_qty,
                'coverage_ratio': 0,
            },
            'future_order': {
                'exec_price': 0.17715,
                'exec_qty': future_qty,
                'exec_amount': 0.17715 * future_qty,
                'coverage_ratio': 0,
            },
            'execution_stats': {},
        }

    def _record_save_close(self, pos, order_group, exec_result, close_reason='take_profit', detail='动态止盈'):
        class FakeCursor:
            rowcount = 1

            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, exc_type, exc, tb):
                return False

        cursor = FakeCursor()
        triggered = []
        self.ce.set_reconciliation_trigger(lambda reason, asset: triggered.append((reason, asset)))
        with patch('calc.closing_executor.db_manager.get_cursor', return_value=FakeCtx(cursor)):
            self.ce._save_close(pos, order_group, exec_result, close_reason, detail)
        return cursor, triggered

    def test_desynced_position_is_not_submitted_to_close_executor_again(self):
        pos = {
            **self.pos,
            'id': 379,
            'status': 'holding',
            'exchange_risk_status': 'desynced',
        }
        execute = MagicMock()

        with patch.object(self.ce, '_execute_close', execute):
            result = self.ce.check_and_close([pos], {}, {'BTC': {'base_asset': 'BTC'}})

        self.assertEqual(result, [])
        execute.assert_not_called()

    def test_future_only_take_profit_marks_desync_and_triggers_reconciliation(self):
        class FakeCursor:
            rowcount = 1

            def __init__(self):
                self.params = None

            def execute(self, sql, params=None):
                self.params = params

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, exc_type, exc, tb):
                return False

        cursor = FakeCursor()
        triggered = []
        self.ce.set_reconciliation_trigger(lambda reason, asset: triggered.append((reason, asset)))

        with patch('calc.closing_executor.db_manager.get_cursor', return_value=FakeCtx(cursor)):
            marked = self.ce._mark_future_only_close_desync(
                {
                    'id': 222,
                    'base_asset': 'BEL',
                    'future_open_qty': 493.0,
                },
                {
                    'success': False,
                    'message': '现货拒单(期货已成交,需人工处理): Binance 请求超时',
                    'future_order': {
                        'exec_price': 0.12092,
                        'exec_qty': 493.0,
                        'exchange_order_id': 'gate-1',
                    },
                    'spot_order': None,
                },
                'take_profit',
                '动态止盈',
            )

        self.assertTrue(marked)
        self.assertEqual(cursor.params['risk_type'], 'missing_gate_position')
        self.assertEqual(cursor.params['future_open_qty'], 0.0)
        self.assertEqual(cursor.params['future_open_contracts'], 0.0)
        self.assertIn('Gate期货已成交但Binance现货失败', cursor.params['detail'])
        self.assertIn('close_reason=take_profit', cursor.params['detail'])
        self.ce._trigger_reconciliation('close_future_only_desync', 'BEL')
        self.assertEqual(triggered, [('close_future_only_desync', 'BEL')])

    def test_future_only_partial_retry_decrements_local_gate_remainder(self):
        cursor = MagicMock()
        cursor.rowcount = 1
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False

        with patch('calc.closing_executor.db_manager.get_cursor', return_value=context):
            marked = self.ce._mark_future_only_close_desync(
                {
                    'id': 432,
                    'base_asset': 'BICO',
                    'future_open_qty': 0.2,
                    'future_open_contracts': 2,
                },
                {
                    'success': False,
                    'message': 'Binance 现货低于最小名义金额',
                    'future_order': {
                        'exec_price': 0.0566,
                        'exec_qty': 0.1,
                        'exchange_order_id': 'gate-bico-dust',
                    },
                    'spot_order': None,
                },
                'negative_funding_exit',
                '负资金费风险',
            )

        self.assertTrue(marked)
        params = cursor.execute.call_args.args[1]
        self.assertEqual(params['risk_type'], 'qty_mismatch')
        self.assertAlmostEqual(params['future_open_qty'], 0.1)
        self.assertEqual(params['future_open_contracts'], 1.0)

    def test_take_profit_partial_fill_keeps_remaining_position(self):
        class FakeCursor:
            rowcount = 1

            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, exc_type, exc, tb):
                return False

        cursor = FakeCursor()
        triggered = []
        self.ce.set_reconciliation_trigger(lambda reason, asset: triggered.append((reason, asset)))
        pos = {
            'id': 247,
            'base_asset': 'BEL',
            'spot_symbol': 'BELUSDT',
            'future_contract': 'BEL_USDT',
            'spot_open_qty': 421.0,
            'spot_open_price': 0.12,
            'spot_open_amount': 50.52,
            'future_open_qty': 421.0,
            'future_open_price': 0.121,
            'future_open_amount': 50.941,
            'future_open_contracts': 421,
            'future_open_leverage': 10,
            'current_spread_bps': -25.0,
            'funding_rate_24h': 0.0,
        }
        order_group = {
            'order_uuid': 'partial-close',
            'spot_order': {
                'order_uuid': 'partial-close',
                'base_asset': 'BEL',
                'spot_symbol': 'BELUSDT',
                'future_contract': None,
                'order_side': 'close',
                'market_type': 'spot',
                'trade_direction': 'sell',
                'target_qty': 421.0,
                'target_amount': 50.0,
            },
            'future_order': {
                'order_uuid': 'partial-close',
                'base_asset': 'BEL',
                'spot_symbol': None,
                'future_contract': 'BEL_USDT',
                'order_side': 'close',
                'market_type': 'future',
                'trade_direction': 'buy',
                'target_qty': 421.0,
                'target_amount': 50.0,
            },
        }
        exec_result = {
            'success': True,
            'spot_order': {
                'exec_price': 0.1774,
                'exec_qty': 369.0,
                'exec_amount': 65.4606,
                'coverage_ratio': 0,
            },
            'future_order': {
                'exec_price': 0.17715,
                'exec_qty': 369.0,
                'exec_amount': 65.3700,
                'coverage_ratio': 0,
            },
            'execution_stats': {},
        }

        with patch('calc.closing_executor.db_manager.get_cursor', return_value=FakeCtx(cursor)):
            self.ce._save_close(pos, order_group, exec_result, 'take_profit', '动态止盈')

        update_calls = [params for sql, params in cursor.calls if 'spot_open_qty = %(spot_open_qty)s' in sql]
        self.assertEqual(len(update_calls), 1)
        update_sql = next(sql for sql, _ in cursor.calls if 'spot_open_qty = %(spot_open_qty)s' in sql)
        self.assertNotIn('future_open_amount', update_sql)
        self.assertEqual(update_calls[0]['spot_open_qty'], 52.0)
        self.assertEqual(update_calls[0]['future_open_qty'], 52.0)
        self.assertEqual(update_calls[0]['future_open_contracts'], 52.0)
        self.assertIn('部分平仓保留剩余', update_calls[0]['close_reason'])
        self.assertFalse(any("status            = 'closed'" in sql for sql, _ in cursor.calls))
        self.assertFalse(any('close_funding_rate_24h' in sql for sql, _ in cursor.calls))
        self.assertEqual(triggered, [('close_partial_fill', 'BEL')])

    def test_take_profit_partial_ioc_keeps_unequal_legs_at_same_hedge_ratio(self):
        """历史两腿数量略有差异时，按相同成交比例保留仍属于平衡持仓。"""
        pos = self._bel_close_position(qty=493.0)
        pos.update({
            'spot_open_qty': 488.07,
            'spot_open_amount': 488.07 * 0.12,
        })
        order_group = self._bel_close_order_group(qty=493.0)
        order_group['spot_order']['target_qty'] = 488.07

        cursor, triggered = self._record_save_close(
            pos,
            order_group,
            self._close_exec_result(396.0, 400.0),
        )

        update_calls = [
            params for sql, params in cursor.calls
            if 'spot_open_qty = %(spot_open_qty)s' in sql
        ]
        self.assertEqual(len(update_calls), 1)
        self.assertAlmostEqual(update_calls[0]['spot_open_qty'], 92.07)
        self.assertAlmostEqual(update_calls[0]['future_open_qty'], 93.0)
        self.assertEqual(update_calls[0]['future_open_contracts'], 93.0)
        self.assertFalse(any(
            "exchange_risk_status = 'desynced'" in sql for sql, _ in cursor.calls
        ))
        self.assertEqual(triggered, [('close_partial_fill', 'BEL')])

    def test_take_profit_partial_ioc_accepts_spot_step_size_rounding(self):
        """同比例现货数量不足一个 step 的截断不应把平衡余仓标记为断腿。"""
        self.ce.spot_meta = {'BEL': {'step_size': 0.1}}
        pos = self._bel_close_position(qty=493.0)
        pos.update({
            'spot_open_qty': 488.0,
            'spot_open_amount': 488.0 * 0.12,
        })
        order_group = self._bel_close_order_group(qty=493.0)
        order_group['spot_order']['target_qty'] = 488.0

        cursor, triggered = self._record_save_close(
            pos,
            order_group,
            self._close_exec_result(395.9, 400.0),
        )

        update_calls = [
            params for sql, params in cursor.calls
            if 'spot_open_qty = %(spot_open_qty)s' in sql
        ]
        self.assertEqual(len(update_calls), 1)
        self.assertAlmostEqual(update_calls[0]['spot_open_qty'], 92.1)
        self.assertAlmostEqual(update_calls[0]['future_open_qty'], 93.0)
        self.assertFalse(any(
            "exchange_risk_status = 'desynced'" in sql for sql, _ in cursor.calls
        ))
        self.assertEqual(triggered, [('close_partial_fill', 'BEL')])

    def test_full_future_fill_with_spot_dust_marks_desync(self):
        """期货已归零时，即使现货余量小于 step，也不能保留为普通双腿持仓。"""
        self.ce.spot_meta = {'BEL': {'step_size': 1.0}}
        pos = self._bel_close_position(qty=410.0)
        cursor, triggered = self._record_save_close(
            pos,
            self._bel_close_order_group(qty=410.0),
            self._close_exec_result(409.5, 410.0),
            close_reason='margin_close',
            detail='保证金危险路径',
        )

        risk_calls = [
            (sql, params) for sql, params in cursor.calls
            if "exchange_risk_status = 'desynced'" in sql
        ]
        self.assertEqual(len(risk_calls), 1)
        self.assertIn('spot=409.5/410|future=410/410', risk_calls[0][1]['detail'])
        self.assertEqual(risk_calls[0][1]['spot_open_qty'], 0.5)
        self.assertEqual(risk_calls[0][1]['future_open_qty'], 0.0)
        self.assertEqual(pos['spot_open_qty'], 0.5)
        self.assertEqual(pos['future_open_qty'], 0.0)
        self.assertEqual(risk_calls[0][1]['risk_type'], 'missing_gate_position')
        self.assertEqual(triggered, [('close_partial_desync', 'BEL')])

    def test_partial_close_exactly_one_spot_step_imbalance_marks_desync(self):
        self.ce.spot_meta = {'BEL': {'step_size': 1.0}}
        pos = self._bel_close_position(qty=410.0)
        cursor, triggered = self._record_save_close(
            pos,
            self._bel_close_order_group(qty=410.0),
            self._close_exec_result(299.0, 300.0),
        )

        risk_calls = [
            (sql, params) for sql, params in cursor.calls
            if "exchange_risk_status = 'desynced'" in sql
        ]
        self.assertEqual(len(risk_calls), 1)
        self.assertEqual(risk_calls[0][1]['spot_open_qty'], 111.0)
        self.assertEqual(risk_calls[0][1]['future_open_qty'], 110.0)
        self.assertEqual(risk_calls[0][1]['risk_type'], 'qty_mismatch')
        self.assertEqual(triggered, [('close_partial_desync', 'BEL')])

    def test_save_close_never_persists_null_target_amount(self):
        pos = self._bel_close_position(qty=0.5)
        pos['future_open_qty'] = 0.0
        pos['future_open_contracts'] = 0.0
        order_group = self._bel_close_order_group(qty=0.5)
        order_group['future_order']['target_qty'] = 0.0
        order_group['future_order']['target_amount'] = None
        exec_result = {
            'success': False,
            'message': 'Gate 平仓数量无效',
            'spot_order': None,
            'future_order': None,
            'execution_stats': {},
        }

        cursor, _ = self._record_save_close(
            pos,
            order_group,
            exec_result,
            close_reason='margin_close',
            detail='保证金危险路径',
        )

        insert_params = [
            params for sql, params in cursor.calls if 'INSERT INTO mi_trade_order' in sql
        ]
        self.assertEqual(len(insert_params), 2)
        self.assertTrue(all(params['target_amount'] is not None for params in insert_params))
        self.assertEqual(insert_params[1]['target_amount'], 0.0)

    def test_incomplete_spot_only_holding_is_marked_missing_gate(self):
        cursor = MagicMock()
        cursor.rowcount = 1
        context = MagicMock()
        context.__enter__.return_value = cursor
        context.__exit__.return_value = False
        pos = {
            'id': 387,
            'status': 'holding',
            'base_asset': 'TUT',
            'spot_open_qty': 0.535,
            'future_open_qty': 0.0,
            'exchange_risk_status': 'normal',
        }

        with patch('calc.closing_executor.db_manager.get_cursor', return_value=context):
            marked = self.ce._mark_incomplete_holding_desync(pos)

        self.assertTrue(marked)
        params = cursor.execute.call_args.args[1]
        self.assertEqual(params['position_id'], 387)
        self.assertEqual(params['risk_type'], 'missing_gate_position')
        self.assertIn('spot_qty=0.535|future_qty=0', params['detail'])
        self.assertEqual(pos['exchange_risk_status'], 'desynced')
        self.assertEqual(pos['exchange_risk_type'], 'missing_gate_position')

    def test_take_profit_full_fill_marks_position_closed(self):
        pos = self._bel_close_position(qty=410.0)
        pos['funding_rate_24h'] = -0.00356
        cursor, triggered = self._record_save_close(
            pos,
            self._bel_close_order_group(qty=410.0),
            self._close_exec_result(410.0, 410.0),
        )

        closed_updates = [
            (sql, params) for sql, params in cursor.calls
            if "status            = 'closed'" in sql
        ]
        self.assertEqual(len(closed_updates), 1)
        self.assertIn('close_funding_rate_24h', closed_updates[0][0])
        self.assertEqual(closed_updates[0][1]['close_funding_rate_24h'], -0.00356)
        self.assertFalse(any('spot_open_qty = %(spot_open_qty)s' in sql for sql, _ in cursor.calls))
        self.assertFalse(any('exchange_risk_status = ' in sql for sql, _ in cursor.calls))
        self.assertEqual(triggered, [])

    def test_take_profit_unbalanced_partial_fill_marks_desync_without_closing(self):
        pos = self._bel_close_position(qty=410.0)
        cursor, triggered = self._record_save_close(
            pos,
            self._bel_close_order_group(qty=410.0),
            self._close_exec_result(235.0, 410.0),
        )

        risk_calls = [
            (sql, params) for sql, params in cursor.calls
            if "exchange_risk_status = 'desynced'" in sql
        ]
        self.assertEqual(len(risk_calls), 1)
        self.assertIn('普通平仓部分成交且两腿不一致', risk_calls[0][1]['detail'])
        self.assertIn('spot=235/410|future=410/410', risk_calls[0][1]['detail'])
        self.assertFalse(any("status            = 'closed'" in sql for sql, _ in cursor.calls))
        self.assertEqual(risk_calls[0][1]['spot_open_qty'], 175.0)
        self.assertEqual(risk_calls[0][1]['future_open_qty'], 0.0)
        self.assertEqual(risk_calls[0][1]['future_open_contracts'], 0.0)
        self.assertEqual(pos['spot_open_qty'], 175.0)
        self.assertEqual(pos['future_open_qty'], 0.0)
        self.assertEqual(pos['exchange_risk_status'], 'desynced')
        self.assertEqual(pos['exchange_risk_type'], 'missing_gate_position')
        self.assertEqual(triggered, [('close_partial_desync', 'BEL')])

    def test_failed_overall_close_with_two_leg_fills_persists_actual_remainders(self):
        pos = self._bel_close_position(qty=410.0)
        exec_result = self._close_exec_result(235.0, 410.0)
        exec_result['success'] = False
        exec_result['message'] = 'Binance平仓重试未完成(filled=235,target=410)'

        cursor, triggered = self._record_save_close(
            pos,
            self._bel_close_order_group(qty=410.0),
            exec_result,
            close_reason='margin_close',
            detail='保证金危险路径',
        )

        risk_calls = [
            (sql, params) for sql, params in cursor.calls
            if "exchange_risk_status = 'desynced'" in sql
        ]
        self.assertEqual(len(risk_calls), 1)
        params = risk_calls[0][1]
        self.assertEqual(params['spot_open_qty'], 175.0)
        self.assertEqual(params['spot_open_amount'], 21.0)
        self.assertEqual(params['future_open_qty'], 0.0)
        self.assertEqual(params['future_open_contracts'], 0.0)
        self.assertEqual(triggered, [('close_partial_desync', 'BEL')])
        self.assertFalse(any("status            = 'closed'" in sql for sql, _ in cursor.calls))
        self.assertFalse(any('close_funding_rate_24h' in sql for sql, _ in cursor.calls))

    def test_failed_close_with_no_fills_does_not_create_false_desync(self):
        exec_result = {
            'success': False,
            'message': 'Gate rejected',
            'spot_order': None,
            'future_order': None,
            'execution_stats': {},
        }
        cursor, triggered = self._record_save_close(
            self._bel_close_position(qty=410.0),
            self._bel_close_order_group(qty=410.0),
            exec_result,
        )

        self.assertFalse(any(
            "exchange_risk_status = 'desynced'" in sql for sql, _ in cursor.calls
        ))
        self.assertFalse(any('close_funding_rate_24h' in sql for sql, _ in cursor.calls))
        self.assertEqual(triggered, [])

    def test_take_profit_low_notional_residual_skips_execution(self):
        class FakeCursor:
            rowcount = 1

            def __init__(self):
                self.calls = []

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        class FakeCtx:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, exc_type, exc, tb):
                return False

        cursor = FakeCursor()
        self.ce.spot_meta = {'AI': {'min_notional': 5.0}}
        pos = {
            'id': 293,
            'status': 'holding',
            'base_asset': 'AI',
            'spot_symbol': 'AIUSDT',
            'future_contract': 'AI_USDT',
            'spot_open_qty': 11.0,
            'spot_open_price': 0.021,
            'spot_open_amount': 0.231,
            'future_open_qty': 11.0,
            'future_open_price': 0.0212,
            'future_open_contracts': 11.0,
            'current_spread_bps': 55.0,
            'funding_rate_24h': 0.001,
        }
        row = {
            'spot_close_vwap': 0.0197,
            'future_close_vwap': 0.01983,
            'close_vwap_basis_bps': 66.0,
        }
        execute_mock = MagicMock(return_value={'success': True})

        with (
            patch.object(self.ce, '_check_negative_funding_exit', return_value=False),
            patch.object(self.ce, '_check_funding_count', return_value=False),
            patch.object(self.ce, '_check_take_profit', return_value=True),
            patch.object(self.ce, '_pass_valley_check', return_value=True),
            patch.object(self.ce, '_pass_close_resiliency_check', return_value=True),
            patch.object(self.ce, '_pre_execution_gate', return_value=(True, row, 66.0, '')),
            patch.object(self.ce, '_build_take_profit_detail', return_value='动态止盈'),
            patch.object(self.ce, '_execute_close', execute_mock),
            patch('calc.closing_executor.db_manager.get_cursor', return_value=FakeCtx(cursor)),
        ):
            results = self.ce.check_and_close([pos], {}, {'AI': row})

        execute_mock.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['success'])
        self.assertIn('低名义残仓跳过平仓', results[0]['message'])
        update_calls = [params for sql, params in cursor.calls if 'UPDATE mi_trade_position' in sql]
        self.assertEqual(len(update_calls), 1)
        self.assertIn('notional=0.2167<min_notional=5USDT', update_calls[0]['reason'])

    def test_fixed_net_take_profit_uses_fee_adjusted_profit(self):
        self.assertTrue(self.ce._check_take_profit(self.pos, 40.0))
        self.assertFalse(self.ce._check_take_profit(self.pos, 75.0))

    def test_high_basis_position_uses_dedicated_take_profit_threshold(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.ce.high_basis_close_take_profit_bps = 30.0
        self.pos.update({
            'open_reason': '基差105.0bps|高基差通道(entry垫=35.0bps,净空间=21.0bps)',
            'open_spread_bps': 100.0,
            'funding_pnl_bps': 0.0,
            'funding_rate_24h': 0.0,
        })

        eval_ = self.ce._take_profit_eval(self.pos, 40.0, {'close_basis_p20': 30.0})

        self.assertEqual(eval_.threshold_bps, 30.0)
        self.assertTrue(self.ce._check_take_profit(self.pos, 40.0, {'close_basis_p20': 30.0}))

    def test_high_basis_position_does_not_hold_for_positive_funding_by_default(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.ce.high_basis_close_take_profit_bps = 30.0
        self.ce.high_basis_close_positive_funding_hold_enabled = False
        self.pos.update({
            'open_reason': '高基差通道(entry垫=35.0bps,净空间=21.0bps)',
            'open_spread_bps': 100.0,
            'funding_pnl_bps': 0.0,
            'funding_rate_24h': 0.003,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=20)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })

        self.assertTrue(self.ce._check_take_profit(self.pos, 40.0, {'close_basis_p20': 30.0}))

    def test_positive_funding_near_settlement_holds_take_profit(self):
        self.pos['funding_rate_24h'] = 0.003  # 约每8小时 +10bps
        self.pos['funding_next_apply'] = (
            datetime.now() + timedelta(minutes=20)
        ).strftime('%Y-%m-%d %H:%M:%S')
        self.assertFalse(self.ce._check_take_profit(self.pos, 40.0))

    def test_negative_funding_exit_waits_until_near_settlement_for_current_24h_rate(self):
        self.pos.update({
            'open_spread_bps': 60.0,
            'current_spread_bps': 60.0,
            'funding_rate_24h': -0.0021,  # 24h -21bps
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=120)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        self.assertFalse(self.ce._check_negative_funding_exit(self.pos))

    def test_negative_funding_exit_triggers_on_current_24h_rate_near_settlement(self):
        self.pos.update({
            'open_spread_bps': 60.0,
            'current_spread_bps': 60.0,
            'funding_rate_24h': -0.0024,  # 24h -24bps, next约-8bps
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=4)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        self.assertTrue(self.ce._check_negative_funding_exit(self.pos))

    def test_negative_funding_exit_watches_extreme_current_24h_rate_far_from_settlement(self):
        self.ce.contract_meta['BTC'] = {'funding_interval': 14400}
        self.pos.update({
            'open_spread_bps': 60.0,
            'current_spread_bps': 64.0,
            'funding_rate_24h': -0.0045,  # 24h -45bps
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=180)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        extreme_pos = dict(self.pos, status='holding')
        self.ce._observe_negative_funding_extremes([extreme_pos])
        self.assertEqual(self.ce._negative_funding_state(self.pos), 'none')
        self.ce._observe_negative_funding_extremes([extreme_pos])
        self.assertFalse(self.ce._check_negative_funding_exit(self.pos))
        self.assertEqual(self.ce._negative_funding_state(self.pos), 'watch')

    def test_negative_funding_extreme_confirmation_counts_asset_once_per_cycle(self):
        positions = [
            dict(self.pos, id=1, status='holding', funding_rate_24h=-0.0045),
            dict(self.pos, id=2, status='holding', funding_rate_24h=-0.0045),
        ]

        self.ce._observe_negative_funding_extremes(positions)

        self.assertEqual(self.ce._negative_funding_extreme_counts['BTC'], 1)
        self.assertEqual(self.ce.negative_funding_watch_assets(positions), set())

    def test_negative_funding_watch_starts_thirty_minutes_before_negative_settlement(self):
        self.pos.update({
            'funding_rate_24h': -0.0024,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=29)
            ).strftime('%Y-%m-%d %H:%M:%S'),
            'current_spread_bps': 130.0,
        })

        self.assertEqual(self.ce._negative_funding_state(self.pos), 'watch')
        self.assertFalse(self.ce._check_negative_funding_exit(self.pos))

    def test_negative_funding_exit_watches_paid_funding_cost(self):
        self.pos.update({
            'funding_rate_24h': 0.001,
            'funding_rate_sum_bps': 7.0,
        })
        self.assertFalse(self.ce._check_negative_funding_exit(self.pos))
        self.assertEqual(self.ce._negative_funding_state(self.pos), 'watch')

    def test_negative_funding_exit_ignores_positive_and_small_negative(self):
        self.pos.update({
            'funding_rate_24h': -0.0020,  # 24h -20bps
            'funding_rate_sum_bps': 6.9,
        })
        self.assertFalse(self.ce._check_negative_funding_exit(self.pos))

    def test_negative_funding_opportunistic_exit_requires_min_net_profit_in_window(self):
        self.pos.update({
            'open_spread_bps': 100.0,
            'current_spread_bps': 50.0,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 7.0,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=60)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })

        self.assertEqual(self.ce._projected_net_bps(self.pos, 50.0), 20.0)
        self.assertEqual(
            self.ce._negative_funding_exit_mode(self.pos, 50.0),
            'opportunistic',
        )
        self.assertTrue(self.ce._check_negative_funding_exit(self.pos, 50.0))

    def test_negative_funding_opportunistic_exit_remains_watch_below_min_net_profit(self):
        self.pos.update({
            'open_spread_bps': 100.0,
            'current_spread_bps': 50.1,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 7.0,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=60)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })

        self.assertAlmostEqual(self.ce._projected_net_bps(self.pos, 50.1), 19.9)
        self.assertIsNone(self.ce._negative_funding_exit_mode(self.pos, 50.1))
        self.assertEqual(self.ce._negative_funding_state(self.pos, 50.1), 'watch')

    def test_negative_funding_opportunistic_exit_waits_for_60_minute_window(self):
        self.pos.update({
            'open_spread_bps': 100.0,
            'current_spread_bps': 40.0,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 7.0,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=61)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })

        self.assertAlmostEqual(self.ce._projected_net_bps(self.pos, 40.0), 30.0)
        self.assertIsNone(self.ce._negative_funding_exit_mode(self.pos, 40.0))
        self.assertEqual(self.ce._negative_funding_state(self.pos, 40.0), 'watch')

    def test_hei_far_from_settlement_does_not_force_exit_on_transient_basis_loss(self):
        self.ce.contract_meta['HEI'] = {'funding_interval': 14400}
        pos = dict(self.pos, base_asset='HEI')
        pos.update({
            'open_spread_bps': 60.0,
            'current_spread_bps': 94.3,
            'funding_pnl_bps': 14.6,
            'funding_rate_24h': -0.00356,
            'funding_rate_sum_bps': 0.6,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=115)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })

        self.assertAlmostEqual(self.ce._projected_exit_loss_bps(pos, 94.3), 41.7)
        self.assertEqual(self.ce._negative_funding_state(pos, 94.3), 'none')
        self.assertFalse(self.ce._check_negative_funding_exit(pos, 94.3))

    def test_negative_funding_budget_force_uses_paid_plus_next_cost(self):
        self.pos.update({
            'funding_rate_24h': -0.0021,
            'funding_rate_sum_bps': 18.0,
        })

        self.assertTrue(self.ce._negative_funding_budget_force(self.pos))
        self.assertEqual(self.ce._negative_funding_exit_mode(self.pos), 'funding_budget')

    def test_negative_funding_budget_does_not_force_when_next_rate_recovers(self):
        self.pos.update({
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 8.0,
            'current_spread_bps': 170.0,
        })

        self.assertFalse(self.ce._negative_funding_budget_force(self.pos))
        self.assertIsNone(self.ce._negative_funding_exit_mode(self.pos, 170.0))

    def test_negative_funding_master_switch_disables_budget_force(self):
        self.ce.negative_funding_exit_enabled = False
        self.pos.update({
            'funding_rate_24h': -0.0030,
            'funding_rate_sum_bps': 20.0,
        })

        self.assertFalse(self.ce._negative_funding_budget_force(self.pos))
        self.assertIsNone(self.ce._negative_funding_exit_mode(self.pos))

    def test_negative_funding_opportunistic_exit_stops_for_recovered_positive_rate(self):
        self.pos.update({
            'open_spread_bps': 60.0,
            'current_spread_bps': 90.0,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': 0.001,
            'funding_rate_sum_bps': 7.0,
        })

        self.assertEqual(self.ce._projected_exit_loss_bps(self.pos, 90.0), 60.0)
        self.assertIsNone(self.ce._negative_funding_exit_mode(self.pos, 90.0))
        self.assertEqual(self.ce._negative_funding_state(self.pos, 90.0), 'watch')

    def test_negative_funding_opportunistic_exit_uses_net_funding_pnl(self):
        self.pos.update({
            'open_spread_bps': 60.0,
            'current_spread_bps': 75.0,
            'funding_pnl_bps': 10.0,
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 7.0,
        })

        self.assertEqual(self.ce._projected_exit_loss_bps(self.pos, 75.0), 27.0)
        self.assertIsNone(self.ce._negative_funding_exit_mode(self.pos, 75.0))

    def test_negative_funding_projected_loss_uses_position_fee_estimate(self):
        self.pos.update({
            'spot_open_amount': 100.0,
            'fee_cost': 0.1,
            'spot_open_qty': 100.0,
            'future_open_qty': 100.0,
            'current_spot_price': 1.0,
            'current_future_price': 1.0,
        })

        comps = self.ce._profit_components(self.pos, 40.0)

        self.assertAlmostEqual(
            comps['fee_full_bps'],
            self.ce._estimated_full_fee_bps(self.pos),
        )
        self.assertNotEqual(comps['fee_full_bps'], self.ce.fee_full_bps)

    def test_negative_funding_exit_detail_exposes_budget_and_loss_composition(self):
        self.pos.update({
            'open_spread_bps': 60.0,
            'current_spread_bps': 75.0,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': -0.0021,
            'funding_rate_sum_bps': 7.0,
        })

        detail = self.ce._build_negative_funding_exit_detail(
            self.pos,
            75.0,
            'opportunistic',
        )

        self.assertIn('mode=opportunistic', detail)
        self.assertIn('负费预算14.0/25.0bps', detail)
        self.assertIn('预计净收益-45.0<20.0bps', detail)
        self.assertIn('组成(价差-15.0+净资金费-8.0-22.0费)', detail)

    def test_negative_funding_opportunistic_recheck_cancels_after_vwap_worsens(self):
        pos = dict(self.pos)
        pos.update({
            'status': 'holding',
            'future_contract': 'BTC_USDT',
            'spot_symbol': 'BTCUSDT',
            'open_spread_bps': 100.0,
            'current_spread_bps': 50.0,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 7.0,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=30)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        execute_mock = MagicMock()

        with (
            patch.object(self.ce, '_check_delist_risk_exit', return_value=False),
            patch.object(
                self.ce,
                '_pre_execution_gate',
                return_value=(True, {'fresh': 'row'}, 50.1, ''),
            ),
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close([pos], {}, {'BTC': {'old': 'row'}})

        self.assertEqual(results, [])
        execute_mock.assert_not_called()

    def test_negative_funding_opportunistic_recheck_executes_with_protective_ioc(self):
        pos = dict(self.pos)
        pos.update({
            'status': 'holding',
            'future_contract': 'BTC_USDT',
            'spot_symbol': 'BTCUSDT',
            'open_spread_bps': 100.0,
            'current_spread_bps': 50.0,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 7.0,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=30)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        execute_mock = MagicMock(return_value={
            'base_asset': 'BTC',
            'success': True,
            'order_uuid': 'risk-close',
            'close_reason': 'negative_funding_exit',
            'message': None,
        })

        with (
            patch.object(self.ce, '_check_delist_risk_exit', return_value=False),
            patch.object(
                self.ce,
                '_pre_execution_gate',
                return_value=(True, {'future_close_vwap': 1.0}, 49.0, ''),
            ),
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close([pos], {}, {'BTC': {'old': 'row'}})

        self.assertEqual(len(results), 1)
        detail = execute_mock.call_args.args[2]
        self.assertIn('mode=opportunistic', detail)
        self.assertIn('预计净收益21.0>=20.0bps', detail)
        self.assertIn('旁路✓(gate=49.0bps', detail)
        self.assertIsNotNone(execute_mock.call_args.kwargs['future_protective_price'])

    def test_negative_funding_opportunistic_exit_requires_fresh_vwap(self):
        pos = dict(self.pos)
        pos.update({
            'status': 'holding',
            'future_contract': 'BTC_USDT',
            'spot_symbol': 'BTCUSDT',
            'open_spread_bps': 100.0,
            'current_spread_bps': 50.0,
            'funding_pnl_bps': -8.0,
            'funding_rate_24h': -0.0018,
            'funding_rate_sum_bps': 7.0,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=30)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        execute_mock = MagicMock()

        with (
            patch.object(self.ce, '_check_delist_risk_exit', return_value=False),
            patch.object(
                self.ce,
                '_pre_execution_gate',
                return_value=(True, None, None, ''),
            ),
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close([pos], {}, {'BTC': {'old': 'row'}})

        self.assertEqual(results, [])
        execute_mock.assert_not_called()

    def test_negative_funding_opportunistic_exit_uses_first_fill_quality_guard(self):
        positions = []
        for position_id in (1, 2):
            positions.append(dict(
                self.pos,
                id=position_id,
                status='holding',
                future_contract='BTC_USDT',
                spot_symbol='BTCUSDT',
                open_spread_bps=100.0,
                current_spread_bps=50.0,
                funding_pnl_bps=-8.0,
                funding_rate_24h=-0.0018,
                funding_rate_sum_bps=7.0,
                funding_next_apply=(
                    datetime.now() + timedelta(minutes=30)
                ).strftime('%Y-%m-%d %H:%M:%S'),
            ))
        execute_mock = MagicMock(return_value={
            'base_asset': 'BTC',
            'success': True,
            'order_uuid': 'first-close',
            'close_reason': 'negative_funding_exit',
            'close_basis_slip_bps': 10.0,
            'message': None,
        })

        with (
            patch.object(self.ce, '_check_delist_risk_exit', return_value=False),
            patch.object(
                self.ce,
                '_pre_execution_gate',
                return_value=(True, {'future_close_vwap': 1.0}, 49.0, ''),
            ) as gate_mock,
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close(positions, {}, {'BTC': {'old': 'row'}})

        self.assertEqual(len(results), 1)
        self.assertEqual(execute_mock.call_count, 1)
        self.assertEqual(gate_mock.call_count, 1)
        self.assertTrue(self.ce._is_close_quality_guard_blocked(
            'BTC',
            'negative_funding_opportunistic',
        ))

    def test_negative_funding_budget_force_ignores_opportunistic_quality_guard(self):
        pos = dict(
            self.pos,
            status='holding',
            future_contract='BTC_USDT',
            spot_symbol='BTCUSDT',
            open_spread_bps=60.0,
            current_spread_bps=90.0,
            funding_rate_24h=-0.0021,
            funding_rate_sum_bps=18.0,
        )
        self.ce._close_quality_guard_cooldown[
            ('BTC', 'negative_funding_opportunistic')
        ] = datetime.now() + timedelta(seconds=60)
        execute_mock = MagicMock(return_value={
            'base_asset': 'BTC',
            'success': True,
            'order_uuid': 'budget-close',
            'close_reason': 'negative_funding_exit',
            'message': None,
        })

        with (
            patch.object(self.ce, '_check_delist_risk_exit', return_value=False),
            patch.object(
                self.ce,
                '_pre_execution_gate',
                return_value=(True, {'fresh': 'row'}, 90.0, ''),
            ),
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close([pos], {}, {'BTC': {'old': 'row'}})

        self.assertEqual(len(results), 1)
        self.assertIn('mode=funding_budget', execute_mock.call_args.args[2])
        self.assertIsNone(execute_mock.call_args.kwargs['future_protective_price'])

    def test_negative_funding_settlement_force_does_not_require_fresh_vwap(self):
        pos = dict(self.pos)
        pos.update({
            'status': 'holding',
            'future_contract': 'BTC_USDT',
            'spot_symbol': 'BTCUSDT',
            'open_spread_bps': 60.0,
            'current_spread_bps': 60.0,
            'funding_pnl_bps': 0.0,
            'funding_rate_24h': -0.0024,
            'funding_next_apply': (
                datetime.now() + timedelta(minutes=4)
            ).strftime('%Y-%m-%d %H:%M:%S'),
        })
        execute_mock = MagicMock(return_value={
            'base_asset': 'BTC',
            'success': True,
            'order_uuid': 'settlement-close',
            'close_reason': 'negative_funding_exit',
            'message': None,
        })

        with (
            patch.object(self.ce, '_check_delist_risk_exit', return_value=False),
            patch.object(
                self.ce,
                '_pre_execution_gate',
                return_value=(True, None, None, ''),
            ),
            patch.object(self.ce, '_execute_close', execute_mock),
        ):
            results = self.ce.check_and_close([pos], {}, {})

        self.assertEqual(len(results), 1)
        self.assertIn('mode=settlement', execute_mock.call_args.args[2])
        self.assertIsNone(execute_mock.call_args.kwargs['future_protective_price'])

    def test_delist_risk_exit_triggers_inside_two_day_window(self):
        self.ce.set_delist_risk_report({
            'items': [{
                'base_asset': 'BTC',
                'exchange': 'binance',
                'market_type': 'spot',
                'risk_type': 'delist_schedule',
                'status': 'scheduled',
                'risk_level': 'critical',
                'delist_at': (datetime.now() + timedelta(hours=47)).strftime('%Y-%m-%d %H:%M:%S'),
                'days_left': 1,
                'message': 'Binance现货已进入下架计划',
            }],
        })
        self.assertTrue(self.ce._check_delist_risk_exit(self.pos))
        self.assertIn('下架风险退出', self.ce._build_delist_risk_exit_detail(self.pos))

    def test_gate_scheduled_delist_triggers_exit_before_reduce_only(self):
        reduce_only_at = datetime.now() + timedelta(hours=45)
        delist_at = datetime.now() + timedelta(hours=46)
        self.ce.set_delist_risk_report({
            'items': [{
                'base_asset': 'BTC',
                'exchange': 'gate',
                'market_type': 'future',
                'risk_type': 'delist_schedule',
                'status': 'scheduled',
                'risk_level': 'critical',
                'reduce_only_at': reduce_only_at.strftime('%Y-%m-%d %H:%M:%S'),
                'delist_at': delist_at.strftime('%Y-%m-%d %H:%M:%S'),
                'days_left': 1.92,
                'message': 'Gate合约已计划下架，当前状态=trading',
            }],
        })

        self.assertTrue(self.ce._check_delist_risk_exit(self.pos))
        detail = self.ce._build_delist_risk_exit_detail(self.pos)
        self.assertIn('gate/future:scheduled', detail)
        self.assertIn(f'reduce_only_at={reduce_only_at:%Y-%m-%d %H:%M:%S}', detail)

    def test_delist_risk_exit_ignores_schedule_outside_window(self):
        self.ce.set_delist_risk_report({
            'items': [{
                'base_asset': 'BTC',
                'exchange': 'binance',
                'market_type': 'spot',
                'risk_type': 'delist_schedule',
                'status': 'scheduled',
                'risk_level': 'warning',
                'delist_at': (datetime.now() + timedelta(days=4)).strftime('%Y-%m-%d %H:%M:%S'),
                'days_left': 4,
                'message': 'Binance现货已进入下架计划',
            }],
        })
        self.assertFalse(self.ce._check_delist_risk_exit(self.pos))

    def test_delist_risk_exit_triggers_gate_in_delisting_without_date(self):
        self.ce.set_delist_risk_report({
            'items': [{
                'base_asset': 'BTC',
                'exchange': 'gate',
                'market_type': 'future',
                'risk_type': 'contract_status',
                'status': 'delisting',
                'risk_level': 'critical',
                'delist_at': None,
                'days_left': None,
                'message': 'Gate合约状态=delisting，已进入下架流程',
            }],
        })
        self.assertTrue(self.ce._check_delist_risk_exit(self.pos))

    def test_take_profit_close_order_group_uses_gate_first(self):
        group = self.ce._build_close_order_group({
            'base_asset': 'BTC',
            'spot_open_qty': 1.0,
            'future_open_qty': 1.0,
            'future_contract': 'BTC_USDT',
        }, close_reason='take_profit')

        self.assertNotIn('protective_price', group['spot_order'])
        self.assertNotIn('protective_price', group['future_order'])
        self.assertNotIn('execution_style', group['future_order'])
        self.assertEqual(group['execution_sequence'], 'future_then_spot')
        self.assertEqual(group['execution_reason'], 'take_profit')

    def test_negative_funding_opportunistic_order_group_keeps_protective_price(self):
        group = self.ce._build_close_order_group({
            'base_asset': 'BTC',
            'spot_open_qty': 1.0,
            'future_open_qty': 1.0,
            'future_contract': 'BTC_USDT',
        }, close_reason='negative_funding_exit', future_protective_price=1.01)

        self.assertEqual(group['future_order']['protective_price'], 1.01)
        self.assertTrue(group['allow_protective_close'])

    def test_close_order_target_amount_uses_target_qty_and_close_vwap(self):
        pos = {
            'base_asset': 'TUT',
            'spot_open_qty': 2100.0,
            'spot_open_price': 0.01855,
            'future_open_qty': 2100.0,
            'future_open_price': 0.018725,
            'future_contract': 'TUT_USDT',
        }
        close_row = {
            'spot_close_vwap': 0.02387,
            'future_close_vwap': 0.023782,
        }

        with patch('calc.closing_executor.config.get_float', return_value=160.0):
            group = self.ce._build_close_order_group(
                pos,
                close_reason='take_profit',
                orderbook_row=close_row,
            )

        self.assertAlmostEqual(group['spot_order']['target_amount'], 50.127)
        self.assertAlmostEqual(group['future_order']['target_amount'], 49.9422)
        self.assertEqual(group['spot_order']['target_qty'], 2100.0)
        self.assertEqual(group['future_order']['target_qty'], 2100.0)

    def test_close_order_target_amount_falls_back_to_position_open_price(self):
        group = self.ce._build_close_order_group({
            'base_asset': 'TUT',
            'spot_open_qty': 2100.0,
            'spot_open_price': 0.01855,
            'future_open_qty': 2100.0,
            'future_open_price': 0.018725,
            'future_contract': 'TUT_USDT',
        }, close_reason='margin_close', orderbook_row={'base_asset': 'TUT'})

        self.assertAlmostEqual(group['spot_order']['target_amount'], 38.955)
        self.assertAlmostEqual(group['future_order']['target_amount'], 39.3225)

    def test_take_profit_threshold_includes_gate_taker_close_fee(self):
        self.assertEqual(self.ce.fee_full_bps, 22.0)

    def test_take_profit_fee_estimate_scales_with_current_close_notional(self):
        pos = {
            'spot_open_amount': 160.08,
            'spot_open_qty': 297.0,
            'future_open_qty': 297.0,
            'fee_cost': -0.2003329,
            'current_spot_price': 0.69443993,
            'current_future_price': 0.6949,
        }

        self.assertAlmostEqual(
            self.ce._estimated_full_fee_bps(pos),
            28.62,
            places=2,
        )

    def test_dynamic_take_profit_uses_asset_funding_history_when_current_drops(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.pos.update({
            'base_asset': 'BANK',
            'open_spread_bps': 180.0,
            'funding_rate_24h': 0.0003,  # current 3bps
            'asset_funding_history': [
                {'rate_24h': v / 10000, 'time': f'06-13 {i:02d}:00'}
                for i, v in enumerate([10, 28, 29, 30, 31, 38, 47])
            ],
            'market_profile': 'normal',
        })
        eval_ = self.ce._take_profit_eval(
            self.pos,
            70.0,
            {'close_basis_p20': 42.0},
        )
        self.assertEqual(eval_.confidence, 'high')
        self.assertGreater(eval_.funding_potential_bps, 25.0)
        self.assertEqual(eval_.threshold_bps, 150.0)
        self.assertFalse(self.ce._check_take_profit(
            self.pos,
            70.0,
            {'close_basis_p20': 42.0},
        ))

    def test_dynamic_take_profit_lowers_threshold_when_history_is_weak(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.pos.update({
            'base_asset': 'EPIC',
            'open_spread_bps': 150.0,
            'funding_rate_24h': 0.0016,  # current 16bps, but history weak
            'asset_funding_history': [
                {'rate_24h': v / 10000, 'time': f'06-13 {i:02d}:00'}
                for i, v in enumerate([3, 3, 3, 4, 6, 6, 31])
            ],
            'market_profile': 'normal',
        })
        eval_ = self.ce._take_profit_eval(
            self.pos,
            70.0,
            {'close_basis_p20': 45.0},
        )
        self.assertEqual(eval_.confidence, 'high')
        self.assertLessEqual(eval_.funding_potential_bps, 6.0)
        self.assertEqual(eval_.threshold_bps, 80.0)
        self.assertFalse(self.ce._check_take_profit(
            self.pos,
            70.0,
            {'close_basis_p20': 45.0},
        ))

    def test_dynamic_take_profit_aging_caps_high_threshold_after_six_days(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.pos.update({
            'base_asset': 'BANK',
            'open_spread_bps': 270.0,
            'funding_rate_24h': 0.0019,
            'opened_at': datetime.now() - timedelta(days=6, minutes=10),
            'asset_funding_history': [
                {'rate_24h': v / 10000, 'time': f'06-13 {i:02d}:00'}
                for i, v in enumerate([15, 16, 17, 18, 18, 19, 19])
            ],
            'market_profile': 'normal',
        })
        eval_ = self.ce._take_profit_eval(
            self.pos,
            140.0,
            {'close_basis_p20': 50.0},
        )
        self.assertEqual(eval_.pre_aging_threshold_bps, 150.0)
        self.assertEqual(eval_.aging_stage, 'aging')
        self.assertEqual(eval_.aging_trigger, 'age')
        self.assertEqual(eval_.threshold_bps, 100.0)
        self.assertTrue(self.ce._check_take_profit(
            self.pos,
            140.0,
            {'close_basis_p20': 50.0},
        ))

    def test_dynamic_take_profit_hard_aging_caps_threshold_after_ten_days(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.pos.update({
            'base_asset': 'BANK',
            'open_spread_bps': 240.0,
            'funding_rate_24h': 0.0019,
            'opened_at': datetime.now() - timedelta(days=10, minutes=10),
            'asset_funding_history': [
                {'rate_24h': v / 10000, 'time': f'06-13 {i:02d}:00'}
                for i, v in enumerate([15, 16, 17, 18, 18, 19, 19])
            ],
            'market_profile': 'normal',
        })
        eval_ = self.ce._take_profit_eval(
            self.pos,
            140.0,
            {'close_basis_p20': 50.0},
        )
        self.assertEqual(eval_.pre_aging_threshold_bps, 150.0)
        self.assertEqual(eval_.aging_stage, 'hard')
        self.assertEqual(eval_.aging_trigger, 'age')
        self.assertEqual(eval_.threshold_bps, 80.0)
        self.assertTrue(self.ce._check_take_profit(
            self.pos,
            137.0,
            {'close_basis_p20': 50.0},
        ))

    def test_dynamic_take_profit_funding_count_enters_aging_without_age(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.pos.update({
            'base_asset': 'BANK',
            'open_spread_bps': 270.0,
            'funding_rate_24h': 0.0019,
            'funding_payments_count': 32,
            'asset_funding_history': [
                {'rate_24h': v / 10000, 'time': f'06-13 {i:02d}:00'}
                for i, v in enumerate([15, 16, 17, 18, 18, 19, 19])
            ],
            'market_profile': 'normal',
        })
        eval_ = self.ce._take_profit_eval(
            self.pos,
            140.0,
            {'close_basis_p20': 50.0},
        )
        self.assertEqual(eval_.aging_stage, 'aging')
        self.assertEqual(eval_.aging_trigger, 'funding_count')
        self.assertEqual(eval_.threshold_bps, 100.0)

    def test_dynamic_take_profit_good_funding_skips_aging_discount(self):
        self.ce.fixed_take_profit_bps = 200.0
        self.pos.update({
            'base_asset': 'BANK',
            'open_spread_bps': 240.0,
            'funding_rate_24h': 0.0080,
            'opened_at': datetime.now() - timedelta(days=10, minutes=10),
            'funding_payments_count': 50,
            'asset_funding_history': [
                {'rate_24h': v / 10000, 'time': f'06-13 {i:02d}:00'}
                for i, v in enumerate([60, 65, 70, 75, 80, 85, 90])
            ],
            'market_profile': 'normal',
        })
        eval_ = self.ce._take_profit_eval(
            self.pos,
            100.0,
            {'close_basis_p20': 50.0},
        )
        self.assertEqual(eval_.pre_aging_threshold_bps, 200.0)
        self.assertIsNone(eval_.aging_stage)
        self.assertTrue(eval_.aging_blocked_by_funding)
        self.assertEqual(eval_.threshold_bps, 200.0)

    def test_take_profit_close_adds_protective_ioc_without_maker(self):
        group = self.ce._build_close_order_group({
            'base_asset': 'BTC',
            'spot_open_qty': 0.99,
            'future_open_qty': 1.0,
            'future_contract': 'BTC_USDT',
        }, close_reason='take_profit', future_protective_price=100.05)

        future_order = group['future_order']
        self.assertEqual(future_order['protective_price'], 100.05)
        self.assertNotIn('execution_style', future_order)
        self.assertFalse(any(str(key).startswith('maker_') for key in future_order))
        self.assertEqual(group['execution_sequence'], 'future_then_spot')

    def test_margin_close_uses_gate_market_first_without_protective_ioc(self):
        group = self.ce._build_close_order_group({
            'base_asset': 'BTC',
            'spot_open_qty': 1.0,
            'future_open_qty': 1.0,
            'future_contract': 'BTC_USDT',
        }, close_reason='margin_close')

        future_order = group['future_order']
        self.assertNotIn('execution_style', future_order)
        self.assertNotIn('protective_price', future_order)
        self.assertEqual(group['execution_sequence'], 'future_then_spot')

    def test_risk_close_reasons_use_gate_market_first_without_maker_or_protective_ioc(self):
        for reason in ('delist_risk_exit', 'negative_funding_exit'):
            with self.subTest(reason=reason):
                group = self.ce._build_close_order_group({
                    'base_asset': 'BTC',
                    'spot_open_qty': 1.0,
                    'future_open_qty': 1.0,
                    'future_contract': 'BTC_USDT',
                }, close_reason=reason)

                future_order = group['future_order']
                self.assertNotIn('execution_style', future_order)
                self.assertNotIn('protective_price', future_order)
                self.assertEqual(group['execution_sequence'], 'future_then_spot')
                self.assertEqual(group['execution_reason'], reason)

    def test_manual_close_uses_gate_market_without_protective_ioc(self):
        group = self.ce._build_close_order_group({
            'base_asset': 'BTC',
            'spot_open_qty': 1.0,
            'future_open_qty': 1.0,
            'future_contract': 'BTC_USDT',
        }, close_reason='manual')

        future_order = group['future_order']
        self.assertNotIn('execution_style', future_order)
        self.assertNotIn('protective_price', future_order)
        self.assertEqual(group['execution_sequence'], 'future_then_spot')


class TestGatePositionRiskEnrichment(unittest.TestCase):
    """Gate 持仓只展示交易所原生全仓字段，不推导单仓 MMR。"""

    def test_initial_margin_and_unrealised_pnl_use_exchange_snapshot(self):
        from calc.gate_position_risk import attach_gate_position_risk

        positions = [{
            'status': 'holding',
            'future_contract': 'HMSTR_USDT',
        }]
        gate_positions = [{
            'contract': 'HMSTR_USDT',
            'size': '-2054',
            'margin': '99',
            'initial_margin': '13.11702499',
            'unrealised_pnl': '-5.42',
            'maintenance_margin': '1.15885139',
            'mark_price': '0.0002719',
            'liq_price': '0.000302',
        }]

        attach_gate_position_risk(positions, gate_positions)

        self.assertAlmostEqual(positions[0]['gate_initial_margin'], 13.11702499)
        self.assertAlmostEqual(positions[0]['gate_unrealised_pnl'], -5.42)
        self.assertAlmostEqual(positions[0]['gate_maintenance_margin'], 1.15885139)

    def test_position_without_future_contract_uses_base_asset_contract(self):
        from calc.gate_position_risk import attach_gate_position_risk

        positions = [{
            'status': 'holding',
            'base_asset': 'BEL',
            'future_open_contracts': 20,
        }]
        gate_positions = [{
            'contract': 'BEL_USDT',
            'size': '-20',
            'margin': '10',
            'initial_margin': '8',
            'unrealised_pnl': '1',
            'maintenance_margin': '0.5',
        }]

        attach_gate_position_risk(positions, gate_positions)

        self.assertAlmostEqual(positions[0]['gate_initial_margin'], 8.0)

    def test_contract_amounts_are_allocated_across_local_positions(self):
        from calc.gate_position_risk import attach_gate_position_risk

        positions = [
            {
                'status': 'holding',
                'future_contract': 'TUT_USDT',
                'future_open_contracts': 10,
                'spot_open_amount': 10.0,
            },
            {
                'status': 'holding',
                'future_contract': 'TUT_USDT',
                'future_open_contracts': 30,
                'spot_open_amount': 30.0,
            },
        ]
        gate_positions = [{
            'contract': 'TUT_USDT',
            'size': '-40',
            'margin': '14.4',
            'initial_margin': '14.4',
            'unrealised_pnl': '-1.2',
            'maintenance_margin': '2.0',
        }]

        attach_gate_position_risk(positions, gate_positions)

        self.assertAlmostEqual(positions[0]['gate_initial_margin'], 3.6)
        self.assertAlmostEqual(positions[1]['gate_initial_margin'], 10.8)
        self.assertAlmostEqual(sum(p['gate_initial_margin'] for p in positions), 14.4)
        self.assertAlmostEqual(sum(p['gate_unrealised_pnl'] for p in positions), -1.2)
        self.assertAlmostEqual(sum(p['gate_maintenance_margin'] for p in positions), 2.0)
        self.assertAlmostEqual(positions[0]['gate_contract_initial_margin'], 14.4)
        self.assertAlmostEqual(positions[1]['gate_contract_initial_margin'], 14.4)
        self.assertEqual(positions[0]['gate_contract_local_position_count'], 2)
        self.assertAlmostEqual(positions[0]['gate_contract_open_notional'], 40.0)



class TestMarginDangerPath(unittest.TestCase):
    """Gate 全仓保证金危险路径。"""

    def _risk_position(self, **overrides):
        pos = {
            'id': 11,
            'base_asset': 'TUT',
            'future_contract': 'TUT_USDT',
            'spot_symbol': 'TUTUSDT',
            'status': 'holding',
            'spot_open_qty': 10.0,
            'future_open_qty': 10.0,
            'future_open_contracts': 10,
            'spot_open_amount': 10.0,
            'future_open_price': 1.0,
            'open_spread_bps': 100.0,
            'current_spread_bps': 100.0,
            'gate_contract_maintenance_margin': 1.0,
            'gate_liq_price': 1.05,
            'gate_mark_price': 1.03,
        }
        pos.update(overrides)
        return pos

    def test_realtime_pnl_does_not_emit_position_margin_or_liq_estimates(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'holding',
            'base_asset': 'BTC',
            'spot_open_price': 100.0,
            'spot_open_qty': 1.0,
            'future_open_price': 100.0,
            'future_open_qty': 1.0,
            'open_spread_bps': 0.0,
            'funding_total_pnl': 0,
        }]
        cfg = PnlConfig(
            open_amount_usdt=100.0,
            spot_open_fee=0,
            spot_close_fee=0,
            future_open_fee=0,
            future_close_fee=0,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(
            positions,
            {'BTC': {'spot_close_vwap': 130.0, 'future_close_vwap': 130.0}},
            {'BTC': {'maintenance_rate': 0.005}},
            cfg,
        )

        self.assertEqual(positions[0]['future_open_notional'], 100.0)
        self.assertEqual(positions[0]['estimated_maintenance_margin'], 0.5)
        self.assertNotIn('margin_initial', positions[0])
        self.assertNotIn('current_margin', positions[0])
        self.assertNotIn('liq_price', positions[0])
        self.assertNotIn('liq_distance_pct', positions[0])

    def test_danger_path_closes_market_directly(self):
        ce = make_closing_executor()
        ce.margin_danger_liq_distance_bps = 300.0
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=1000.0),
        }
        ce._close_cooldown['TUT'] = datetime.now()
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })

        results = ce.check_and_close_margin_danger(
            [self._risk_position(current_spread_bps=None)],
            {'TUT': {'base_asset': 'TUT'}},
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['close_reason'], 'margin_close')
        ce._execute_close.assert_called_once()
        args = ce._execute_close.call_args.args
        self.assertEqual(args[1], 'margin_close')
        self.assertIn('保证金危险路径', args[2])
        self.assertIn('全仓风险触发', args[2])

    def test_danger_margin_close_uses_minimal_row_when_orderbook_missing(self):
        ce = make_closing_executor()
        ce.margin_danger_liq_distance_bps = 300.0
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=1000.0),
        }
        ce._execute_close = MagicMock(return_value={
            'base_asset': 'TUT',
            'success': True,
            'close_reason': 'margin_close',
        })

        results = ce.check_and_close_margin_danger(
            [self._risk_position(current_spread_bps=None)],
            {},
        )

        self.assertEqual(results[-1]['close_reason'], 'margin_close')
        args = ce._execute_close.call_args.args
        self.assertEqual(args[3], {'base_asset': 'TUT'})

    def test_danger_path_marks_incomplete_holding_and_never_submits_close(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='safe', account_mmr_pct=800.0),
        }
        pos = self._risk_position(
            spot_open_qty=0.7,
            future_open_qty=0.0,
            future_open_contracts=0.0,
        )
        ce._mark_incomplete_holding_desync = MagicMock(return_value=True)
        ce._execute_close = MagicMock()
        triggered = []
        ce.set_reconciliation_trigger(lambda reason, asset: triggered.append((reason, asset)))

        results = ce.check_and_close_margin_danger([pos], {})

        self.assertEqual(results, [])
        ce._mark_incomplete_holding_desync.assert_called_once_with(pos)
        ce._execute_close.assert_not_called()
        self.assertEqual(triggered, [('incomplete_holding_desync', 'TUT')])

    def test_needs_fresh_margin_risk_detects_mmr_and_liq_distance(self):
        ce = make_closing_executor()
        ce.margin_danger_mmr_pct = 300.0
        ce.margin_danger_liq_distance_bps = 300.0
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=450.0),
        }

        safe = self._risk_position(
            gate_liq_price=1.20,
            gate_mark_price=1.00,
        )
        near_liq = self._risk_position(
            gate_liq_price=1.05,
            gate_mark_price=1.03,
        )
        missing_risk = self._risk_position(
            gate_liq_price=None,
        )

        self.assertFalse(ce.needs_fresh_margin_risk([safe]))
        self.assertTrue(ce.needs_fresh_margin_risk([near_liq]))
        self.assertTrue(ce.needs_fresh_margin_risk([missing_risk]))
        summary = ce.margin_risk_refresh_summary([safe, near_liq, missing_risk])
        self.assertEqual(summary, {'danger': ['TUT'], 'missing': ['TUT']})

        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=250.0),
        }
        self.assertTrue(ce.needs_fresh_margin_risk([safe]))

    def test_missing_gate_margin_risk_refresh_can_be_disabled(self):
        ce = make_closing_executor()
        ce.margin_danger_missing_risk_force_refresh = False

        missing_risk = self._risk_position(
            gate_liq_price=None,
        )

        self.assertFalse(ce.needs_fresh_margin_risk([missing_risk]))

    def test_liq_distance_uses_current_price_as_bps_denominator(self):
        ce = make_closing_executor()
        ce.margin_danger_mmr_pct = 300.0
        ce.margin_danger_liq_distance_bps = 192.0
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='danger', account_mmr_pct=450.0),
        }
        pos = self._risk_position(
            gate_liq_price=1.05,
            gate_mark_price=1.03,
        )

        state = ce._margin_danger_state(pos)

        self.assertAlmostEqual(state['liq_distance_bps'], 194.174757, places=5)
        self.assertFalse(state['active'])

    def test_cross_margin_zero_position_margin_does_not_trigger_mmr_close(self):
        ce = make_closing_executor()
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(status='idle', account_mmr_pct=0.0),
        }
        pos = self._risk_position(
            gate_contract_maintenance_margin=2.4506,
            gate_maintenance_margin=2.4506,
            gate_liq_price=9.90629,
            gate_mark_price=0.13827722,
        )

        state = ce._margin_danger_state(pos)

        self.assertEqual(state['margin_rate'], 0.0)
        self.assertIsNone(state['liq_distance_bps'])
        self.assertFalse(state['active'])

    def test_cross_margin_uses_account_mmr_snapshot_for_danger_path(self):
        ce = make_closing_executor()
        ce.margin_danger_mmr_pct = 300.0
        ce._gate_cross_risk_cache = {
            'ts': time.time(),
            'risk': make_gate_cross_risk(
                status='danger',
                account_mmr_pct=250.0,
                account_equity_usdt=4434.8311,
                maintenance_margin_usdt=1773.9324,
            ),
        }
        pos = self._risk_position(
            gate_contract_maintenance_margin=2.4506,
            gate_liq_price=9.90629,
            gate_mark_price=0.13827722,
        )

        state = ce._margin_danger_state(pos)
        detail = ce._build_margin_close_detail(pos, '保证金危险路径', state)

        self.assertTrue(state['active'])
        self.assertEqual(state['margin_rate'], 250.0)
        self.assertIn('MMR250.00%<=300.0%', ';'.join(state['reasons']))
        self.assertIn('全仓权益4434.8311', detail)
        self.assertIn('全仓维持保证金1773.9324', detail)

    def test_close_order_preserves_cross_margin_future_leverage_zero(self):
        ce = make_closing_executor()
        pos = self._risk_position(future_open_leverage=0.0)

        self.assertEqual(ce._order_leg_leverage('future_order', pos), 0.0)


class TestPositionPnlFees(unittest.TestCase):

    def test_holding_fee_uses_future_taker_when_open_fallback_fills(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'holding',
            'base_asset': 'BANK',
            'spot_open_price': 100.0,
            'spot_open_qty': 1.0,
            'future_open_price': 100.0,
            'future_open_qty': 1.0,
            'open_spread_bps': 0.0,
            'funding_total_pnl': 0,
            'future_open_fee_rate': 0.0005,
        }]
        cfg = PnlConfig(
            open_amount_usdt=100.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            future_taker_open_fee=0.0005,
            future_taker_close_fee=0.0005,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(
            positions,
            {'BANK': {'spot_close_vwap': 100.0, 'future_close_vwap': 100.0}},
            {'BANK': {'maker_fee_rate': -0.0001, 'taker_fee_rate': 0.00075}},
            cfg,
        )

        self.assertAlmostEqual(positions[0]['fee_bps'], -12.5)
        self.assertAlmostEqual(positions[0]['fee_cost'], -0.125)
        self.assertEqual(positions[0]['fee_source'], 'estimated')

    def test_holding_fee_uses_future_maker_when_maker_fills(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'holding',
            'base_asset': 'BANK',
            'spot_open_price': 100.0,
            'spot_open_qty': 1.0,
            'future_open_price': 100.0,
            'future_open_qty': 1.0,
            'open_spread_bps': 0.0,
            'funding_total_pnl': 0,
            'future_open_fee_rate': 0.0002,
        }]
        cfg = PnlConfig(
            open_amount_usdt=100.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            future_taker_open_fee=0.0005,
            future_taker_close_fee=0.0005,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(
            positions,
            {'BANK': {'spot_close_vwap': 100.0, 'future_close_vwap': 100.0}},
            {'BANK': {'maker_fee_rate': -0.0001, 'taker_fee_rate': 0.00075}},
            cfg,
        )

        self.assertAlmostEqual(positions[0]['fee_bps'], -9.5)
        self.assertAlmostEqual(positions[0]['fee_cost'], -0.095)
        self.assertEqual(positions[0]['fee_source'], 'estimated')

    def test_holding_fee_prefers_actual_spot_and_future_usdt_amounts(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'holding',
            'base_asset': 'BANK',
            'spot_open_price': 100.0,
            'spot_open_qty': 1.0,
            'future_open_price': 100.0,
            'future_open_qty': 1.0,
            'open_spread_bps': 0.0,
            'funding_total_pnl': 0,
            'spot_open_fee_amount_usdt': 0.0074,
            'future_open_fee_amount_usdt': 0.0049,
            'future_open_fee_rate': 0.0005,
        }]
        cfg = PnlConfig(
            open_amount_usdt=10.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            future_taker_open_fee=0.0005,
            future_taker_close_fee=0.0005,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(
            positions,
            {'BANK': {'spot_close_vwap': 100.0, 'future_close_vwap': 100.0}},
            {'BANK': {}},
            cfg,
        )

        self.assertAlmostEqual(positions[0]['fee_bps'], -12.3)
        self.assertAlmostEqual(positions[0]['fee_cost'], -0.0123)
        self.assertEqual(positions[0]['fee_source'], 'actual')

    def test_holding_fee_bps_uses_position_open_notional_when_config_changes(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'holding',
            'base_asset': 'BANK',
            'spot_open_amount': 10.0,
            'spot_open_price': 100.0,
            'spot_open_qty': 0.1,
            'future_open_price': 100.0,
            'future_open_qty': 0.1,
            'open_spread_bps': 0.0,
            'funding_total_pnl': 0,
            'spot_open_fee_amount_usdt': 0.0075,
            'future_open_fee_estimated_usdt': 0.005,
            'future_open_fee_rate': 0.0005,
        }]
        cfg = PnlConfig(
            open_amount_usdt=20.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            future_taker_open_fee=0.0005,
            future_taker_close_fee=0.0005,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(
            positions,
            {'BANK': {'spot_close_vwap': 100.0, 'future_close_vwap': 100.0}},
            {'BANK': {}},
            cfg,
        )

        self.assertAlmostEqual(positions[0]['fee_bps'], -12.5)
        self.assertAlmostEqual(positions[0]['fee_cost'], -0.0125)
        self.assertEqual(positions[0]['fee_source'], 'mixed_estimated')

    def test_closed_realized_pnl_uses_position_open_notional_when_config_changes(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'closed',
            'base_asset': 'BANK',
            'spot_open_amount': 10.0,
            'spot_open_price': 100.0,
            'spot_open_qty': 0.1,
            'future_open_price': 100.0,
            'future_open_qty': 0.1,
            'spot_close_price': 99.0,
            'future_close_price': 98.0,
            'close_spread_bps': -100.0,
            'open_spread_bps': 100.0,
            'funding_total_pnl': 0.01,
            'spot_open_fee_amount_usdt': 0.0075,
            'future_open_fee_estimated_usdt': 0.005,
            'spot_close_fee_estimated_usdt': 0.0075,
            'future_close_fee_estimated_usdt': 0.005,
            'future_open_fee_rate': 0.0005,
            'future_close_fee_rate': 0.0005,
        }]
        cfg = PnlConfig(
            open_amount_usdt=50.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            future_taker_open_fee=0.0005,
            future_taker_close_fee=0.0005,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(positions, {}, {'BANK': {}}, cfg)

        self.assertAlmostEqual(positions[0]['realized_pnl_bps'], 200.0)
        self.assertAlmostEqual(positions[0]['realized_pnl'], 0.2)
        self.assertAlmostEqual(positions[0]['funding_pnl_bps'], 10.0)
        self.assertAlmostEqual(positions[0]['fee_bps'], -25.0)
        self.assertAlmostEqual(positions[0]['total_pnl'], 0.185)

    def test_closed_position_pnl_uses_order_level_amounts_and_fees(self):
        from calc.closed_position_pnl import compute_closed_position_pnl

        pos = {'id': 1, 'base_asset': 'BANK', 'funding_total_pnl': 0.03}
        orders = [
            {'order_side': 'open', 'market_type': 'spot', 'status': 'executed', 'exec_qty': 1.0, 'exec_amount': 10.00, 'fee_amount_usdt': 0.01},
            {'order_side': 'open', 'market_type': 'future', 'status': 'executed', 'exec_qty': 1.0, 'exec_amount': 10.05, 'fee_rate': 0.0005},
            {'order_side': 'close', 'market_type': 'spot', 'status': 'executed', 'exec_qty': 1.0, 'exec_amount': 10.08, 'fee_rate': 0.00075},
            {'order_side': 'close', 'market_type': 'future', 'status': 'executed', 'exec_qty': 1.0, 'exec_amount': 9.95, 'fee_amount_usdt': 0.005},
        ]

        pnl = compute_closed_position_pnl(pos, orders)

        self.assertIsNotNone(pnl)
        self.assertAlmostEqual(pnl['realized_pnl'], 0.18)
        self.assertAlmostEqual(pnl['fee_cost'], 0.027585, places=6)
        self.assertAlmostEqual(pnl['total_pnl'], 0.182415, places=6)
        self.assertAlmostEqual(pnl['close_spread_bps'], -128.9683, places=4)

    def test_closed_realtime_pnl_prefers_stored_order_level_values(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'closed',
            'base_asset': 'BANK',
            'spot_open_amount': 10.0,
            'spot_close_price': 99.0,
            'future_close_price': 98.0,
            'close_spread_bps': -100.0,
            'open_spread_bps': 100.0,
            'funding_total_pnl': 0.01,
            'realized_pnl': 0.1234,
            'realized_pnl_bps': 123.4,
            'total_pnl': 0.101,
            'total_pnl_bps': 101.0,
        }]
        cfg = PnlConfig(
            open_amount_usdt=50.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            future_taker_open_fee=0.0005,
            future_taker_close_fee=0.0005,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(positions, {}, {'BANK': {}}, cfg)

        self.assertAlmostEqual(positions[0]['realized_pnl'], 0.1234)
        self.assertAlmostEqual(positions[0]['realized_pnl_bps'], 123.4)
        self.assertAlmostEqual(positions[0]['total_pnl'], 0.101)
        self.assertAlmostEqual(positions[0]['total_pnl_bps'], 101.0)

    def test_holding_fee_estimates_missing_leg_from_order_exec_amount(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'holding',
            'base_asset': 'HMSTR',
            'spot_open_price': 100.0,
            'spot_open_qty': 1.0,
            'future_open_price': 100.0,
            'future_open_qty': 1.0,
            'open_spread_bps': 0.0,
            'funding_total_pnl': 0,
            'spot_open_fee_amount_usdt': 0.0074,
            'future_open_fee_estimated_usdt': 0.005015,
            'future_open_fee_estimated_count': 1,
            'future_open_fee_rate': 0.0005,
        }]
        cfg = PnlConfig(
            open_amount_usdt=10.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            future_taker_open_fee=0.0005,
            future_taker_close_fee=0.0005,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(
            positions,
            {'HMSTR': {'spot_close_vwap': 100.0, 'future_close_vwap': 100.0}},
            {'HMSTR': {}},
            cfg,
        )

        self.assertAlmostEqual(positions[0]['fee_bps'], -12.41)
        self.assertAlmostEqual(positions[0]['fee_cost'], -0.0124)
        self.assertEqual(positions[0]['fee_source'], 'mixed_estimated')

    def test_holding_fee_present_without_close_vwap(self):
        from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl

        positions = [{
            'status': 'holding',
            'base_asset': 'HMSTR',
            'open_spread_bps': 0.0,
            'funding_total_pnl': 0.001,
            'future_open_fee_rate': 0.0005,
        }]
        cfg = PnlConfig(
            open_amount_usdt=10.0,
            spot_open_fee=0.00075,
            spot_close_fee=0.00075,
            future_open_fee=0.0002,
            future_close_fee=0.0002,
            risk_relief_bps=0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(positions, {}, {'HMSTR': {}}, cfg)

        self.assertAlmostEqual(positions[0]['fee_bps'], -12.5)
        self.assertAlmostEqual(positions[0]['fee_cost'], -0.0125)
        self.assertAlmostEqual(positions[0]['funding_pnl_bps'], 1.0)
        self.assertIsNone(positions[0]['total_pnl'])


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    unittest.main(verbosity=2)
