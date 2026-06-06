# coding: utf-8
"""
开仓 / 平仓核心逻辑分支测试

覆盖目标：
  TradingExecutor:
    - _pass_peak_check（首次/更新峰值/超时/未回落/sustain不足/通过）
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

def make_trading_executor(sustain_sec=2.0, peak_pullback_pct=0.10,
                          peak_monitor_timeout_sec=60,
                          basis_threshold_bps=-60,
                          coverage_threshold=0.8,
                          max_orderbook_lag_ms=200.0,
                          vwap_threshold_meta=None,
                          close_vwap_threshold_meta=None,
                          asset_tier_meta=None,
                          momentum_enabled=False,
                          momentum_allowed_tiers=None,
                          momentum_tier_overrides=None,
                          rebound_enabled=True,
                          rebound_allowed_tiers=None):
    """构造独立的 TradingExecutor 实例（不依赖 DB / API）"""
    from calc.trading_executor import TradingExecutor, TradingExecutorConfig

    cfg = TradingExecutorConfig(
        sustain_sec=sustain_sec,
        peak_pullback_pct=peak_pullback_pct,
        peak_monitor_timeout_sec=peak_monitor_timeout_sec,
        basis_threshold_bps=basis_threshold_bps,
        coverage_threshold=coverage_threshold,
        max_orderbook_lag_ms=max_orderbook_lag_ms,
        momentum_enabled=momentum_enabled,
        momentum_allowed_tiers=momentum_allowed_tiers or ['A'],
        momentum_tier_overrides=momentum_tier_overrides or {},
        rebound_enabled=rebound_enabled,
        rebound_allowed_tiers=rebound_allowed_tiers or ['B'],
    )
    te = TradingExecutor(
        cfg, contract_meta={}, spot_meta={},
        vwap_threshold_meta=vwap_threshold_meta,
        close_vwap_threshold_meta=close_vwap_threshold_meta,
        asset_tier_meta=asset_tier_meta,
    )
    return te


def make_closing_executor():
    """构造独立的 ClosingExecutor 实例（不依赖 DB；config 用真实 yaml 即可，本测试只关心方法逻辑）"""
    from calc.closing_executor import ClosingExecutor
    return ClosingExecutor(contract_meta={}, spot_meta={}, funding_rate_p40_meta={})


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

    def test_monitor_timeout_resolves_and_cools(self):
        """监控超时（elapsed ≥ 60s）→ 不开单 + 进入 timeout_cooldown，状态清除"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        # 推早 65s 模拟超时
        self.te._peak_state['BTC']['start_time'] = datetime.now() - timedelta(seconds=65)

        ret = self.te._pass_peak_check('BTC', 95.0, self.row)
        self.assertFalse(ret)
        self.assertNotIn('BTC', self.te._peak_state)
        self.assertIn('BTC', self.te._timeout_cooldown_until)
        self.te._resolve_signal.assert_called_once()
        # resolve 调用参数中 status='monitor_timeout'
        args, kwargs = self.te._resolve_signal.call_args
        self.assertEqual(args[1], 'monitor_timeout')


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
        te._refresh_holding_count_from_db = MagicMock()
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
    """A 级允许 momentum，B 级只走回落+恢复。"""

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
        for basis in [30.0, 32.0, 34.0]:
            te._record_momentum_sample('BTC', basis)

        self.assertTrue(te._pass_momentum_check('BTC', 34.0, self._row('BTC', 34.0)))
        self.assertEqual(te._peak_state['BTC']['trigger'], 'momentum')
        self.assertEqual(te._peak_state['BTC']['strategy_tier'], 'A')

    def test_b_tier_does_not_enter_momentum_channel(self):
        te = self._executor('B')
        for basis in [30.0, 32.0, 34.0]:
            te._record_momentum_sample('BTC', basis)

        self.assertFalse(te._pass_momentum_check('BTC', 34.0, self._row('BTC', 34.0)))
        self.assertNotIn('BTC', te._peak_state)

    def test_b_tier_waits_for_rebound_after_pullback_resiliency(self):
        te = make_trading_executor(
            basis_threshold_bps=20,
            vwap_threshold_meta={'BTC': {'p20': 20}},
            close_vwap_threshold_meta={'BTC': {'close_basis_p20': -100}},
            asset_tier_meta={'BTC': 'B'},
            rebound_enabled=True,
            rebound_allowed_tiers=['B'],
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


class TestClosingExecutorPreExecutionGate(unittest.TestCase):
    """平仓最终风控旁路 6 个分支"""

    def setUp(self):
        self.ce = make_closing_executor()
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
            row={'symbol': 'BTCUSDT'},
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
            patch.object(self.ce, '_check_margin_liquidation', return_value=False),
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


class TestMarginTopupCalculation(unittest.TestCase):
    """自动追保核心公式。"""

    def test_topup_amount_targets_half_current_future_notional(self):
        ce = make_closing_executor()
        pos = {
            'id': 1,
            'base_asset': 'BTC',
            'future_open_qty': 1.0,
            'future_open_price': 100.0,
            'current_future_price': 130.0,
            'margin_topup_total': 0.0,
        }

        calc = ce._calculate_margin_topup_amount(pos)

        self.assertIsNotNone(calc)
        self.assertAlmostEqual(calc['initial_margin'], 50.0)
        self.assertAlmostEqual(calc['target_margin'], 65.0)
        self.assertAlmostEqual(calc['topup_amount'], 15.0)

    def test_liq_price_includes_margin_topup_total(self):
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
            'margin_topup_total': 10.0,
        }]
        cfg = PnlConfig(
            open_amount_usdt=100.0,
            spot_open_fee=0,
            spot_close_fee=0,
            future_open_fee=0,
            future_close_fee=0,
            risk_relief_bps=0,
            margin_leverage=2.0,
            margin_default_mmr=0.005,
        )

        calculate_realtime_pnl(
            positions,
            {'BTC': {'spot_close_vwap': 130.0, 'future_close_vwap': 130.0}},
            {'BTC': {'maintenance_rate': 0.005}},
            cfg,
        )

        self.assertAlmostEqual(positions[0]['margin_initial'], 50.0)
        self.assertAlmostEqual(positions[0]['current_margin'], 60.0)
        self.assertAlmostEqual(positions[0]['liq_price'], 159.5)


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    unittest.main(verbosity=2)
