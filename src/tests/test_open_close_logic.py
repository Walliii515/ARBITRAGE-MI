# coding: utf-8
"""
开仓 / 平仓核心逻辑分支测试

覆盖目标：
  TradingExecutor:
    - _check_update_count_freshness（uc 闸通用）
    - _pass_peak_check（首次/更新峰值/超时/未回落/sustain不足/uc不足/通过）
    - _pre_execution_gate（manager未注入/lag拦截/uc闸拦截/基差衰减/盈利性守卫/覆盖超限/通过）
  ClosingExecutor:
    - _check_update_count_freshness（uc 闸通用）
    - _pass_valley_check（首次/更新谷底/超时通过/谷底>=open异常通过/反弹通过/未达标）
    - _pre_execution_gate（manager未注入/lag拦截/uc闸拦截/收敛逆转/回弹过大/通过）

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
                          close_vwap_threshold_meta=None):
    """构造独立的 TradingExecutor 实例（不依赖 DB / API）"""
    from calc.trading_executor import TradingExecutor, TradingExecutorConfig

    cfg = TradingExecutorConfig(
        sustain_sec=sustain_sec,
        peak_pullback_pct=peak_pullback_pct,
        peak_monitor_timeout_sec=peak_monitor_timeout_sec,
        basis_threshold_bps=basis_threshold_bps,
        coverage_threshold=coverage_threshold,
        max_orderbook_lag_ms=max_orderbook_lag_ms,
    )
    te = TradingExecutor(
        cfg, contract_meta={}, spot_meta={},
        vwap_threshold_meta=vwap_threshold_meta,
        close_vwap_threshold_meta=close_vwap_threshold_meta,
    )
    return te


def make_closing_executor():
    """构造独立的 ClosingExecutor 实例（不依赖 DB；config 用真实 yaml 即可，本测试只关心方法逻辑）"""
    from calc.closing_executor import ClosingExecutor
    return ClosingExecutor(contract_meta={}, spot_meta={}, funding_rate_p40_meta={})


# ══════════════════════════════════════════════════════════════════
# TradingExecutor 测试
# ══════════════════════════════════════════════════════════════════

class TestTradingExecutorUpdateCountFreshness(unittest.TestCase):
    """update_count 闸通用校验（开仓侧）"""

    def setUp(self):
        # sustain_sec=3 → min_update_count = 6
        self.te = make_trading_executor(sustain_sec=3.0)

    def test_threshold_dynamic_calculation(self):
        """阈值 = max(1, int(sustain_sec * 2))"""
        self.assertEqual(self.te.min_update_count, 6)

        te2 = make_trading_executor(sustain_sec=0.0)
        self.assertEqual(te2.min_update_count, 1)  # 兜底 max(1, ...)

        te3 = make_trading_executor(sustain_sec=5.0)
        self.assertEqual(te3.min_update_count, 10)

    def test_state_missing_passes(self):
        """状态缺失（now/start 任一为 None）退化为放行"""
        passed, _ = self.te._check_update_count_freshness(None, 100, 90, 90)
        self.assertTrue(passed)

        passed, _ = self.te._check_update_count_freshness(100, 100, None, 90)
        self.assertTrue(passed)

    def test_increment_insufficient_blocks(self):
        """gate 或 spot 任一侧增量 < 阈值 → 拦截"""
        # gate 增量 = 5 < 6, spot = 10
        passed, reason = self.te._check_update_count_freshness(105, 110, 100, 100)
        self.assertFalse(passed)
        self.assertIn('盘口呆滞', reason)
        self.assertIn('gate增量=5', reason)

    def test_both_increments_pass(self):
        """gate 与 spot 增量都 ≥ 阈值 → 通过"""
        # gate 增量 = 6, spot = 7
        passed, reason = self.te._check_update_count_freshness(106, 107, 100, 100)
        self.assertTrue(passed)
        self.assertEqual(reason, '')


class TestTradingExecutorPeakCheck(unittest.TestCase):
    """峰值回落 + sustain 确认（开仓唯一通道）"""

    def setUp(self):
        self.te = make_trading_executor(sustain_sec=2.0, peak_pullback_pct=0.10,
                                        peak_monitor_timeout_sec=60)
        # min_update_count = 4

        # mock 副作用：实时费率校验恒通过、信号写库返回固定 ID
        self.te._verify_realtime_funding_rate = MagicMock(return_value=True)
        self.te._create_signal = MagicMock(return_value=1001)
        self.te._resolve_signal = MagicMock()

        # 注入 manager，模拟 update_count 起点 = 0
        self.gate_mgr = FakeManager({'BTC_USDT': FakeOrderBook(update_count=0)})
        self.spot_mgr = FakeManager({'BTCUSDT': FakeOrderBook(update_count=0)})
        self.te.set_orderbook_managers(self.gate_mgr, self.spot_mgr)

        self.row = {'contract': 'BTC_USDT', 'symbol': 'BTCUSDT'}

    def test_first_entry_records_peak_returns_false(self):
        """首次进入：记录峰值与 uc 起点，返回 False"""
        ret = self.te._pass_peak_check('BTC', 100.0, self.row)
        self.assertFalse(ret)
        state = self.te._peak_state['BTC']
        self.assertEqual(state['peak_bps'], 100.0)
        self.assertEqual(state['gate_uc_start'], 0)
        self.assertEqual(state['spot_uc_start'], 0)
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

    def test_pullback_sustain_uc_insufficient_returns_false(self):
        """回落 + sustain 都达标，但 uc 增量不足 → 等待（不重置）"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        # 模拟 sustain 已过：手工把 start_time 推早 3s
        self.te._peak_state['BTC']['start_time'] = datetime.now() - timedelta(seconds=3)
        # uc 不变（=0）：增量 = 0 < 4 → 拦截
        ret = self.te._pass_peak_check('BTC', 90.0, self.row)
        self.assertFalse(ret)
        # 状态保留（不重置）
        self.assertIn('BTC', self.te._peak_state)

    def test_full_pass_returns_true(self):
        """回落 + sustain + uc 全部达标 → 通过，trigger=pullback"""
        self.te._pass_peak_check('BTC', 100.0, self.row)
        self.te._peak_state['BTC']['start_time'] = datetime.now() - timedelta(seconds=3)
        # uc 增量 +5 ≥ 4
        self.gate_mgr._books['BTC_USDT'].update_count = 5
        self.spot_mgr._books['BTCUSDT'].update_count = 5

        ret = self.te._pass_peak_check('BTC', 90.0, self.row)
        self.assertTrue(ret)
        self.assertEqual(self.te._peak_state['BTC']['trigger'], 'pullback')

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

    def test_uc_gate_blocks(self):
        """update_count 闸：起点已记录但增量不足 → 拦截"""
        # peak_state 已记录起点 100/100；uc_now = 102/102，增量 = 2 < 4
        self.te._peak_state['BTC'] = {
            'peak_bps': 50.0,
            'start_time': datetime.now() - timedelta(seconds=3),
            'trigger': None,
            'signal_id': 1,
            'gate_uc_start': 100,
            'spot_uc_start': 100,
        }
        self._setup_books(gate_uc=102, spot_uc=102)

        passed, _, _, reason = self.te._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT')
        self.assertFalse(passed)
        self.assertIn('盘口呆滞', reason)

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
# ClosingExecutor 测试
# ══════════════════════════════════════════════════════════════════

class TestClosingExecutorUpdateCountFreshness(unittest.TestCase):
    """update_count 闸通用校验（平仓侧）"""

    def setUp(self):
        self.ce = make_closing_executor()

    def test_threshold_dynamic_with_sustain(self):
        """阈值 = max(1, int(sustain_sec * 2))，sustain 取自 config"""
        self.assertEqual(self.ce.min_update_count, max(1, int(self.ce.sustain_sec * 2)))
        self.assertGreaterEqual(self.ce.min_update_count, 1)

    def test_state_missing_passes(self):
        passed, _ = self.ce._check_update_count_freshness(None, 100, 90, 90)
        self.assertTrue(passed)
        passed, _ = self.ce._check_update_count_freshness(100, 100, 90, None)
        self.assertTrue(passed)

    def test_increment_insufficient_blocks(self):
        thr = self.ce.min_update_count
        # spot 增量 = thr - 1 → 拦截
        passed, reason = self.ce._check_update_count_freshness(
            100 + thr, 100 + thr - 1, 100, 100
        )
        self.assertFalse(passed)
        self.assertIn('盘口呆滞', reason)

    def test_both_increments_pass(self):
        thr = self.ce.min_update_count
        passed, reason = self.ce._check_update_count_freshness(
            100 + thr, 100 + thr, 100, 100
        )
        self.assertTrue(passed)
        self.assertEqual(reason, '')


class TestClosingExecutorValleyCheck(unittest.TestCase):
    """谷底反弹确认（止盈唯一确认通道）"""

    def setUp(self):
        self.ce = make_closing_executor()
        # 注入 manager
        self.gate_mgr = FakeManager({'BTC_USDT': FakeOrderBook(update_count=0)})
        self.spot_mgr = FakeManager({'BTCUSDT': FakeOrderBook(update_count=0)})
        self.ce.set_orderbook_managers(self.gate_mgr, self.spot_mgr)

        self.pos = {
            'base_asset': 'BTC',
            'future_contract': 'BTC_USDT',
            'spot_symbol': 'BTCUSDT',
            'open_spread_bps': 100.0,
        }

    def test_first_entry_records_valley_returns_false(self):
        """首次进入：记录谷底 + uc 起点，返回 False"""
        ret = self.ce._pass_valley_check('BTC', 50.0, self.pos)
        self.assertFalse(ret)
        state = self.ce._valley_state['BTC']
        self.assertEqual(state['valley_bps'], 50.0)
        self.assertEqual(state['open_spread_bps'], 100.0)
        self.assertEqual(state['gate_uc_start'], 0)
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

    def test_uc_gate_blocks(self):
        """有 valley_state + 增量不足 → 拦截"""
        thr = self.ce.min_update_count
        self.ce._valley_state['BTC'] = {
            'valley_bps': 30.0,
            'start_time': datetime.now() - timedelta(seconds=3),
            'open_spread_bps': 100.0,
            'trigger': 'rebound',
            'gate_uc_start': 100,
            'spot_uc_start': 100,
        }
        # 增量 = thr - 1 → 拦截
        self._setup_books(gate_uc=100 + thr - 1, spot_uc=100 + thr - 1)
        passed, _, _, reason = self.ce._pre_execution_gate('BTC', 'BTC_USDT', 'BTCUSDT', self.pos)
        self.assertFalse(passed)
        self.assertIn('盘口呆滞', reason)

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
            'gate_uc_start': 0, 'spot_uc_start': 0,
        }
        detail = self.ce._build_take_profit_detail(self.pos, 35.0)
        self.assertIn('鲜度(gate=', detail)
        self.assertIn('谷底反弹', detail)
        # 消费一次后再调用 → 应回退到 NA
        detail2 = self.ce._build_take_profit_detail(self.pos, 35.0)
        self.assertIn('鲜度(NA)', detail2)


# ══════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    unittest.main(verbosity=2)
