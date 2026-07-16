# coding: utf-8
"""
平仓执行器模块
- ClosingExecutor: 平仓条件检查 + 平仓订单生成 + 持久化
- 成交引擎通过 ExecutorClient (HTTP) 调用独立的执行器服务（虚拟/实盘），实现虚实分离

平仓触发条件（按优先级）：
  0. Gate 全仓保证金风险平仓
  1. 下架风险临近窗口退出
  2. 当前负24h资金费率临近结算且下一期仍明显为负
  3. 动态净收益止盈（含老仓退出；下单前有最终风控旁路复核）
"""
import time
import uuid
import json
import threading
from datetime import datetime, timedelta
from typing import Callable, List, Dict, Optional

from common.database import db_manager
from common.config import config
from common.logger import get_logger
from calc.executor_client import ExecutorClient
from calc.orderbook_enricher import calc_vwap_basis_bps, calc_full_fee_bps
from calc.orderbook_resiliency import (
    BookSideSpec,
    OrderBookResiliencyMonitor,
    ResiliencyConfig,
)
from calc.execution_audit import format_execution_audit
from calc.order_fee_resolver import build_order_execution_fields
from calc.dynamic_take_profit import (
    DynamicTakeProfitConfig,
    evaluate_dynamic_take_profit,
    format_dynamic_take_profit,
)
from calc.real_executor import GATE_CROSS_MARGIN_LEVERAGE

logger = get_logger(__name__)


FAST_RISK_CLOSE_REASONS = {'margin_close', 'delist_risk_exit', 'negative_funding_exit'}
CLOSE_QTY_TOLERANCE = 1e-8


def _float_or_none(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ClosingExecutor:
    """平仓执行器（条件判断 + 订单生成 + 持久化，通过 ExecutorClient 调用成交引擎服务）"""

    def __init__(self, contract_meta: Dict, spot_meta: Dict, funding_rate_p40_meta: Dict = None):
        """
        Args:
            contract_meta: base_asset -> {quanto_multiplier, ...}
            spot_meta:     base_asset -> {step_size, min_qty, ...}
            funding_rate_p40_meta: base_asset -> percentile_40资金费率（用于止盈阈值计算）
        """
        self.contract_meta = contract_meta
        self.spot_meta = spot_meta
        self.funding_rate_p40_meta = funding_rate_p40_meta or {}

        executor_url = config.get_executor_url()
        executor_timeout = config.get_int('trade.executor.timeout_sec', 5)
        self.executor_client = ExecutorClient(executor_url, timeout=executor_timeout)

        self.take_profit_mode = config.get_str('trade.close.take_profit_mode', 'fixed_net_bps')
        self.fixed_take_profit_bps = config.get_float('trade.close.fixed_take_profit_bps', 50.0)
        self.take_profit_multiplier = config.get_float('trade.close.take_profit_days_multiplier', 6.0)
        self.close_threshold_col = config.get_str(
            'trade.vwap.close_threshold_percentile', 'close_basis_p20'
        ).strip()
        self.dynamic_take_profit_cfg = self._load_dynamic_take_profit_config()
        self.high_basis_close_take_profit_bps = config.get_float(
            'trade.high_basis_open.close_take_profit_bps', 30.0
        )
        self.high_basis_close_positive_funding_hold_enabled = config.get_bool(
            'trade.high_basis_open.close_positive_funding_hold_enabled', False
        )
        self.max_funding_payments = config.get_int('trade.close.max_funding_payments', 30)
        self.positive_funding_hold_enabled = config.get_bool(
            'trade.close.positive_funding_hold_enabled', True
        )
        self.positive_funding_hold_window_min = config.get_float(
            'trade.close.positive_funding_hold_window_min', 60.0
        )
        self.positive_funding_hold_min_bps = config.get_float(
            'trade.close.positive_funding_hold_min_bps', 5.0
        )
        self.negative_funding_exit_enabled = config.get_bool(
            'trade.close.negative_funding_exit_enabled', True
        )
        self.negative_funding_exit_current_24h_bps = config.get_float(
            'trade.close.negative_funding_exit_current_24h_bps', 21.0
        )
        self.negative_funding_exit_current_window_min = config.get_float(
            'trade.close.negative_funding_exit_current_window_min', 5.0
        )
        self.negative_funding_exit_extreme_24h_bps = config.get_float(
            'trade.close.negative_funding_exit_extreme_24h_bps', 45.0
        )
        self.negative_funding_exit_next_bps = config.get_float(
            'trade.close.negative_funding_exit_next_bps', 7.0
        )
        self.negative_funding_exit_paid_bps = config.get_float(
            'trade.close.negative_funding_exit_paid_bps', 7.0
        )
        self.delist_risk_exit_enabled = config.get_bool(
            'trade.close.delist_risk_exit_enabled', True
        )
        self.delist_risk_exit_days = max(
            config.get_float('trade.close.delist_risk_exit_days', 2.0),
            0.0,
        )
        self.protective_ioc_enabled = config.get_bool('trade.close.protective_ioc_enabled', True)
        self.protective_ioc_take_profit_slippage_bps = config.get_float(
            'trade.close.protective_ioc_take_profit_slippage_bps', 5.0
        )
        self.protective_ioc_risk_slippage_bps = config.get_float(
            'trade.close.protective_ioc_risk_slippage_bps', 12.0
        )
        self.future_maker_close_enabled = config.get_bool(
            'trade.execution.future_maker_close.enabled', False
        )
        self.future_maker_close_allowed_tiers = {
            str(t).strip().upper()
            for t in config.get('trade.execution.future_maker_close.allowed_tiers', ['A', 'B'])
            if str(t).strip().upper() in ('A', 'B', 'C')
        }
        self.future_maker_close_ttl_ms = max(
            config.get_int('trade.execution.future_maker_close.ttl_ms', 1000), 0
        )
        self.future_maker_close_price_offset_bps = config.get_float(
            'trade.execution.future_maker_close.price_offset_bps', 0.0
        )
        self.future_maker_close_fallback_ioc_enabled = config.get_bool(
            'trade.execution.future_maker_close.fallback_ioc_enabled', True
        )
        self.future_maker_close_fallback_allowed_tiers = {
            str(t).strip().upper()
            for t in config.get('trade.execution.future_maker_close.fallback_allowed_tiers', ['A', 'B'])
            if str(t).strip().upper() in ('A', 'B', 'C')
        }

        # 手续费率（用于止盈阈值计算）
        self.fee_spot_open = config.get_float('trade.fee.spot_open', 0.00075)
        self.fee_spot_close = config.get_float('trade.fee.spot_close', 0.00075)
        self.fee_future_open = config.get_float('trade.fee.future_open', 0.00075)
        self.fee_future_close = config.get_float('trade.fee.future_close', 0.00075)
        self.fee_future_taker_open = config.get_float('trade.fee.future_taker_open', self.fee_future_open)
        self.fee_future_taker_close = config.get_float('trade.fee.future_taker_close', self.fee_future_close)
        # 全部手续费 BPS（正数，用于止盈阈值累加）
        self.fee_full_bps = -calc_full_fee_bps(
            self.fee_spot_open, self.fee_spot_close,
            self.fee_future_open, self.fee_future_close
        )

        # 谷底反弹止盈策略
        self.valley_rebound_pct = config.get_float('trade.valley_rebound.rebound_pct', 0.10)
        self.valley_monitor_timeout_sec = config.get_int('trade.valley_rebound.monitor_timeout_sec', 60)
        self._valley_state: Dict[object, Dict] = {}  # position_id -> {valley_bps, start_time, open_spread_bps}

        # 平仓失败冷却机制
        self.close_cooldown_sec = config.get_int('trade.close.cooldown_sec', 60)
        self._close_cooldown: Dict[str, datetime] = {}  # base_asset -> 上次失败时间
        self.close_quality_guard_enabled = config.get_bool(
            'trade.close.close_quality_guard.enabled',
            config.get_bool('trade.close.take_profit_batch_guard.enabled', True),
        )
        self.close_quality_guard_max_close_basis_slip_bps = max(
            config.get_float(
                'trade.close.close_quality_guard.max_close_basis_slip_bps',
                config.get_float('trade.close.take_profit_batch_guard.max_close_basis_slip_bps', 8.0),
            ),
            0.0,
        )
        self.close_quality_guard_cooldown_sec = max(
            config.get_int('trade.close.close_quality_guard.cooldown_sec', 60),
            0,
        )
        self._close_quality_guard_cooldown: Dict[tuple, datetime] = {}

        # Gate 全仓保证金风控配置
        self.margin_danger_path_enabled = True
        self.margin_danger_mmr_pct = config.get_float(
            'account_capital.gate_cross_risk.danger_mmr_pct',
            300.0,
        )
        self.margin_danger_liq_distance_bps = max(
            config.get_float('account_capital.gate_cross_risk.danger_liq_distance_bps', 300.0),
            0.0,
        )
        self.margin_danger_missing_risk_force_refresh = True
        self.gate_cross_risk_max_age_sec = max(
            config.get_float('account_capital.gate_cross_risk.max_age_sec', 5.0),
            0.0,
        )
        self._gate_cross_risk_cache: Dict[str, object] = {'ts': 0.0, 'risk': None}
        self._margin_close_inflight = set()
        self._margin_close_inflight_lock = threading.Lock()

        # 最终风控旁路：旁路风控新鲜度硬约束（以本地 last_update_time 为准计算 lag_ms，超过阈值拒平）
        self._max_orderbook_lag_ms = config.get_float('trade.close.max_orderbook_lag_ms', 200.0)
        # 临时槽位：旁路风控读取到的 (gate_lag_ms, spot_lag_ms)，供平仓原因拼接
        self._last_orderbook_lag_ms: Dict[str, tuple] = {}
        self._last_take_profit_eval: Dict[object, object] = {}
        self._active_close_vwap_threshold_meta: Dict[str, Dict] = {}
        self._delist_risk_by_asset: Dict[str, List[Dict]] = {}
        # OrderBookManager 引用（由外部注入）
        self._gate_manager = None
        self._spot_manager = None
        self._reconciliation_trigger: Optional[Callable[[str, str], None]] = None
        self._gate_cross_risk_provider: Optional[Callable[[], Optional[Dict]]] = None

        self._close_resiliency_max_basis_rebound_bps = config.get_float(
            'trade.close_resiliency.max_basis_rebound_bps', 8.0
        )
        self._close_resiliency_coverage_threshold = config.get_float(
            'trade.close_resiliency.coverage_threshold',
            config.get_float('trade.open.orderbook_coverage_threshold', 0.8),
        )
        self._close_resiliency = OrderBookResiliencyMonitor(
            ResiliencyConfig(
                enabled=config.get_bool('trade.close_resiliency.enabled', True),
                window_sec=config.get_float('trade.close_resiliency.window_sec', 1.5),
                min_samples=config.get_int('trade.close_resiliency.min_samples', 3),
                min_recovery_ratio=config.get_float('trade.close_resiliency.min_recovery_ratio', 0.55),
                max_spread_widen_bps=config.get_float('trade.close_resiliency.max_spread_widen_bps', 10.0),
                max_basis_volatility_bps=config.get_float('trade.close_resiliency.max_basis_volatility_bps', 8.0),
                min_hold_sec=config.get_float('trade.close_resiliency.min_hold_sec', 0.3),
                max_wait_sec=config.get_float('trade.close_resiliency.max_wait_sec', 1.5),
                allow_timeout_pass=True,
            ),
            [
                BookSideSpec('spot', 'bid', 1.0, 'spot_bid'),
                BookSideSpec('future', 'ask', 1.0, 'future_ask', '_future_qty_multiplier'),
            ],
            ['spot_close_coverage', 'future_close_coverage'],
            'close',
        )

    def _load_dynamic_take_profit_config(self) -> DynamicTakeProfitConfig:
        cfg = config.get('trade.close.dynamic_take_profit', {}) or {}
        raw_tiers = cfg.get('tiers') or DynamicTakeProfitConfig().tiers
        return DynamicTakeProfitConfig(
            enabled=bool(cfg.get('enabled', True)),
            recent_settlements=int(cfg.get('recent_settlements', 3)),
            high_confidence_min_samples=int(cfg.get('high_confidence_min_samples', 6)),
            medium_confidence_min_samples=int(cfg.get('medium_confidence_min_samples', 3)),
            high_recent_weight=float(cfg.get('high_recent_weight', 0.6)),
            high_p50_weight=float(cfg.get('high_p50_weight', 0.4)),
            medium_current_weight=float(cfg.get('medium_current_weight', 0.5)),
            medium_recent_weight=float(cfg.get('medium_recent_weight', 0.3)),
            medium_p50_weight=float(cfg.get('medium_p50_weight', 0.2)),
            basis_discount_tier_a=float(cfg.get('basis_discount_tier_a', 0.45)),
            basis_discount_normal=float(cfg.get('basis_discount_normal', 0.35)),
            basis_discount_thin_bursty=float(cfg.get('basis_discount_thin_bursty', 0.25)),
            basis_score_cap_bps=float(cfg.get('basis_score_cap_bps', 30.0)),
            low_confidence_min_take_profit_bps=float(
                cfg.get('low_confidence_min_take_profit_bps', 110.0)
            ),
            medium_confidence_min_take_profit_bps=float(
                cfg.get('medium_confidence_min_take_profit_bps', 80.0)
            ),
            aging_enabled=bool(cfg.get('aging_enabled', True)),
            aging_start_days=float(cfg.get('aging_start_days', 6.0)),
            aging_start_funding_count=int(cfg.get('aging_start_funding_count', 32)),
            aging_max_threshold_bps=float(cfg.get('aging_max_threshold_bps', 100.0)),
            aging_min_net_profit_bps=float(cfg.get('aging_min_net_profit_bps', 80.0)),
            aging_hard_days=float(cfg.get('aging_hard_days', 10.0)),
            aging_hard_funding_count=int(cfg.get('aging_hard_funding_count', 50)),
            aging_hard_max_threshold_bps=float(
                cfg.get('aging_hard_max_threshold_bps', 80.0)
            ),
            aging_hard_min_net_profit_bps=float(
                cfg.get('aging_hard_min_net_profit_bps', 80.0)
            ),
            aging_hold_funding_bps=float(
                cfg.get('aging_hold_funding_bps', 20.0)
            ),
            tiers=[
                {
                    'hold_value_min_bps': float(row.get('hold_value_min_bps', 0.0)),
                    'take_profit_bps': float(row.get('take_profit_bps', 80.0)),
                }
                for row in raw_tiers
                if isinstance(row, dict)
            ],
        )

    def set_orderbook_managers(self, gate_manager, spot_manager):
        """
        注入 OrderBookManager 引用，供平仓最终风控旁路直接读取单标的盘口。
    
        Args:
            gate_manager: Gate 期货 OrderBookManager 实例
            spot_manager: Binance 现货 OrderBookManager 实例
        """
        changed = self._gate_manager is not gate_manager or self._spot_manager is not spot_manager
        self._gate_manager = gate_manager
        self._spot_manager = spot_manager
        if changed:
            logger.info('OrderBookManager 已注入 ClosingExecutor（平仓最终风控旁路就绪）')

    def set_reconciliation_trigger(self, callback: Callable[[str, str], None]):
        """注入后台对账触发器，用于断腿风险出现后尽快走现有兜底链路。"""
        self._reconciliation_trigger = callback

    def set_gate_cross_risk_provider(
        self,
        callback: Optional[Callable[[], Optional[Dict]]],
    ):
        """Inject the shared real-time Gate cross-risk snapshot provider."""
        self._gate_cross_risk_provider = callback

    def set_delist_risk_report(self, report: Optional[Dict]):
        """注入异步刷新得到的下架风险报告；平仓关键路径只读内存。"""
        grouped: Dict[str, List[Dict]] = {}
        for item in (report or {}).get('items', []) or []:
            asset = str(item.get('base_asset') or '').strip().upper()
            if not asset:
                continue
            grouped.setdefault(asset, []).append(item)
        self._delist_risk_by_asset = grouped

    # ──────────────────────────────────────────────────────────────────
    # 公共入口
    # ──────────────────────────────────────────────────────────────────

    def check_and_close_margin_danger(
        self,
        positions: List[Dict],
        orderbook_rows_by_asset: Optional[Dict[str, Dict]] = None,
    ) -> List[Dict]:
        """Execute the Gate cross-margin emergency path without market-data gates.

        Only a fresh shared Gate risk snapshot may trigger this path. Account MMR
        danger applies to every forward holding; liquidation-distance danger only
        applies to the affected contract. Failed attempts are retried on the next
        risk loop and never enter the ordinary close cooldown.
        """
        if not self.margin_danger_path_enabled:
            return []

        cross_risk = self._latest_gate_cross_risk()
        if not self._is_gate_cross_risk_fresh(cross_risk):
            return []

        rows = orderbook_rows_by_asset or {}
        results = []
        for pos in positions or []:
            if pos.get('status') != 'holding':
                continue

            danger = self._margin_danger_state(pos, cross_risk=cross_risk)
            if not danger.get('active'):
                continue

            base_asset = str(pos.get('base_asset') or '').upper()
            inflight_key = self._margin_close_key(pos)
            if not self._claim_margin_close(inflight_key):
                logger.warning(
                    "保证金危险平仓已有执行中的请求，跳过重复提交 | %s | position_id=%s",
                    base_asset,
                    pos.get('id'),
                )
                continue

            orderbook_row = rows.get(base_asset) or {'base_asset': base_asset}
            detail = self._build_margin_close_detail(
                pos,
                '保证金危险路径',
                danger,
                cross_risk=cross_risk,
            )
            self._clear_position_close_state(base_asset, pos)
            try:
                result = self._execute_close(
                    pos,
                    'margin_close',
                    detail,
                    orderbook_row,
                    pre_gate_basis_bps=None,
                    future_protective_price=None,
                )
                result.setdefault('position_id', pos.get('id'))
                results.append(result)
                if result.get('success'):
                    logger.critical(
                        "Gate全仓危险平仓成功 | %s | position_id=%s | %s",
                        base_asset,
                        pos.get('id'),
                        ';'.join(danger.get('reasons') or []),
                    )
                else:
                    logger.critical(
                        "Gate全仓危险平仓失败，将在下一轮立即重试 | %s | position_id=%s | msg=%s",
                        base_asset,
                        pos.get('id'),
                        result.get('message'),
                    )
            except Exception as exc:
                logger.critical(
                    "Gate全仓危险平仓异常，将在下一轮立即重试 | %s | position_id=%s | %s",
                    base_asset,
                    pos.get('id'),
                    exc,
                    exc_info=True,
                )
                results.append({
                    'position_id': pos.get('id'),
                    'base_asset': base_asset,
                    'success': False,
                    'close_reason': 'margin_close',
                    'message': str(exc),
                })
            finally:
                self._release_margin_close(inflight_key)

        return results

    def check_and_close(
        self,
        positions: List[Dict],
        close_vwap_threshold_meta: Dict[str, Dict],
        orderbook_rows_by_asset: Dict[str, Dict],
    ) -> List[Dict]:
        """
        检查所有持仓并执行平仓

        Args:
            positions: 已由 calculate_realtime_pnl 富化的持仓列表
                       （含 current_spread_bps / funding_rate_24h / funding_next_apply）
            close_vwap_threshold_meta: base_asset ->
                {close_basis_p10, close_basis_p20, close_basis_p30, close_basis_p40}
            orderbook_rows_by_asset: base_asset -> merged orderbook row（传给成交引擎）

        Returns:
            平仓执行结果列表 [{base_asset, success, close_reason, order_uuid, message}, ...]
        """
        results = []
        self._active_close_vwap_threshold_meta = close_vwap_threshold_meta or {}

        for pos in positions:
            if pos.get('status') != 'holding':
                continue

            ba = pos.get('base_asset', '')
            current_spread_bps = pos.get('current_spread_bps')

            if current_spread_bps is None:
                continue  # 无盘口数据，跳过
            valley_key = self._valley_key(ba, pos)

            # ── 冷却期检查：平仓失败后 N 秒内不重试 ──
            cooldown_until = self._close_cooldown.get(ba)
            if cooldown_until and (datetime.now() - cooldown_until).total_seconds() < self.close_cooldown_sec:
                continue

            # ── 按优先级检查平仓条件 ──
            close_reason = None
            close_reason_detail = None
            pre_gate_basis_bps = None
            future_protective_price = None

            if self._check_delist_risk_exit(pos):
                close_reason = 'delist_risk_exit'
                close_reason_detail = self._build_delist_risk_exit_detail(pos)
                self._clear_position_close_state(ba, pos)
            elif self._check_negative_funding_exit(pos):
                close_reason = 'negative_funding_exit'
                close_reason_detail = self._build_negative_funding_exit_detail(pos)
                self._clear_position_close_state(ba, pos)
            elif self._check_take_profit(
                pos,
                current_spread_bps,
                close_vwap_threshold_meta.get(ba, {}),
            ):
                # 止盈条件满足，进入谷底反弹确认
                orderbook_row = orderbook_rows_by_asset.get(ba)
                if orderbook_row is not None:
                    self._annotate_resiliency_row(orderbook_row, ba)
                    close_basis_for_resiliency = orderbook_row.get('close_vwap_basis_bps')
                    if close_basis_for_resiliency is None:
                        close_basis_for_resiliency = current_spread_bps
                    self._close_resiliency.observe_shock(ba, orderbook_row)
                if self._pass_valley_check(ba, current_spread_bps, pos):
                    if orderbook_row is not None and not self._pass_close_resiliency_check(
                        ba, orderbook_row, float(close_basis_for_resiliency), pos
                    ):
                        continue
                    close_reason = 'take_profit'
                # else: 谷底监控中，不平仓
            else:
                # 止盈不再满足，清除谷底监控状态
                self._clear_position_close_state(ba, pos)

            if not close_reason:
                continue
            ba_key = str(ba or '').upper()
            if self._is_close_quality_guard_blocked(ba_key, close_reason):
                logger.info(f"平仓质量保护跳过 | {ba} | reason={close_reason} | 等待冷却后重新观察")
                continue

            # ── 最终风控旁路：止盈复核盈利性；风险平仓复核新鲜度/同步/深度 ──
            guarded_reasons = {'take_profit', 'negative_funding_exit', 'delist_risk_exit'}
            if close_reason in guarded_reasons:
                contract = pos.get('future_contract', '')
                symbol = pos.get('spot_symbol') or f"{ba}USDT"
                gate_passed, gate_row, gate_basis, gate_reason = self._pre_execution_gate(
                    ba, contract, symbol, pos, require_profit=(close_reason == 'take_profit')
                )
                if not gate_passed:
                    logger.info(
                        f"平仓最终风控旁路拦截 | {ba} | reason={close_reason} | "
                        f"gate_basis={gate_basis}bps | 原因: {gate_reason}"
                    )
                    # 旁路拦截后清除谷底状态，下一轮重新判断
                    self._valley_state.pop(valley_key, None)
                    continue
                pre_gate_basis_bps = gate_basis
                # 使用旁路返回的最新盘口行（确保下单数据 = 校验数据）
                if gate_row is not None:
                    orderbook_rows_by_asset[ba] = gate_row
                if close_reason == 'take_profit':
                    # 旁路通过后再构建详情，才能把本次旁路写入的 lag 拼入“鲜度”字段
                    close_reason_detail = self._build_take_profit_detail(pos, current_spread_bps)
                else:
                    close_reason_detail = self._append_lag_detail(ba, close_reason_detail)
                # 补充旁路判定信息到原因详情（供复盘）
                if gate_basis is not None:
                    drift_bps = gate_basis - current_spread_bps
                    close_reason_detail = (
                        f"{close_reason_detail}"
                        f"|旁路✓(gate={gate_basis:.1f}bps,偏移{drift_bps:+.1f})"
                    )

            # ── 执行平仓 ──
            orderbook_row = orderbook_rows_by_asset.get(ba)
            if not orderbook_row:
                if close_reason in FAST_RISK_CLOSE_REASONS:
                    orderbook_row = {'base_asset': ba}
                    orderbook_rows_by_asset[ba] = orderbook_row
                    logger.warning(
                        f"风险平仓无完整盘口，使用市价兜底最小盘口行: {ba} | reason={close_reason}"
                    )
                else:
                    logger.warning(f"平仓条件触发但无盘口数据: {ba} | reason={close_reason}")
                    continue
            if close_reason == 'take_profit':
                notional_ok, notional_reason = self._check_spot_close_min_notional(pos, orderbook_row)
                if not notional_ok:
                    self._mark_low_notional_residual(pos, notional_reason)
                    self._clear_position_close_state(ba, pos)
                    self._close_cooldown[ba] = datetime.now()
                    logger.warning(
                        "低名义残仓跳过普通止盈平仓 | %s | position_id=%s | %s",
                        ba, pos.get('id'), notional_reason,
                    )
                    results.append({
                        'base_asset': ba,
                        'success': False,
                        'close_reason': close_reason,
                        'message': notional_reason,
                    })
                    continue
            if (
                close_reason not in FAST_RISK_CLOSE_REASONS
                and close_reason != 'manual'
                and self.protective_ioc_enabled
                and not self.future_maker_close_enabled
            ):
                slippage_bps = (
                    self.protective_ioc_take_profit_slippage_bps
                    if close_reason == 'take_profit'
                    else self.protective_ioc_risk_slippage_bps
                )
                future_protective_price = self._future_close_protective_price(orderbook_row, slippage_bps)
                if future_protective_price is not None:
                    close_reason_detail = (
                        f"{close_reason_detail}|保护IOC(future_buy≤{future_protective_price})"
                    )

            try:
                result = self._execute_close(
                    pos,
                    close_reason,
                    close_reason_detail,
                    orderbook_row,
                    pre_gate_basis_bps=pre_gate_basis_bps,
                    future_protective_price=future_protective_price,
                )
                results.append(result)
                if result.get('success'):
                    # 平仓成功，清除谷底监控状态和冷却记录
                    self._clear_position_close_state(ba, pos)
                    self._close_cooldown.pop(ba, None)
                    logger.info(
                        f"平仓成功 | {ba} | reason={close_reason} | "
                        f"spread_bps={current_spread_bps:.2f}"
                    )
                    if close_reason == 'take_profit':
                        self._update_close_quality_guard(
                            ba_key,
                            close_reason,
                            result,
                        )
                else:
                    # 平仓失败，进入冷却期
                    self._close_cooldown[ba] = datetime.now()
                    # 超时触发的谷底状态也需清除，避免下次继续超时重试
                    self._clear_position_close_state(ba, pos)
                    logger.warning(
                        f"平仓失败 | {ba} | reason={close_reason} | "
                        f"msg={result.get('message')} | "
                        f"冷却{self.close_cooldown_sec}s"
                    )
            except Exception as e:
                logger.error(f"平仓执行异常 {ba}: {e}", exc_info=True)
                results.append({'base_asset': ba, 'success': False, 'message': str(e)})

        return results

    def _close_quality_guard_key(self, base_asset: str, close_reason: str) -> tuple:
        return (str(base_asset or '').upper(), str(close_reason or '').strip())

    def _is_close_quality_guard_blocked(self, base_asset: str, close_reason: str) -> bool:
        if close_reason != 'take_profit':
            return False
        if not self.close_quality_guard_enabled:
            return False
        key = self._close_quality_guard_key(base_asset, close_reason)
        cooldown_until = self._close_quality_guard_cooldown.get(key)
        if cooldown_until is None:
            return False
        if datetime.now() >= cooldown_until:
            self._close_quality_guard_cooldown.pop(key, None)
            return False
        return True

    def _update_close_quality_guard(
        self,
        base_asset: str,
        close_reason: str,
        result: Dict,
    ) -> None:
        if not self.close_quality_guard_enabled:
            return
        if close_reason != 'take_profit':
            return
        slip_bps = result.get('close_basis_slip_bps')
        if slip_bps is None:
            return
        slip_bps = float(slip_bps)
        threshold = self.close_quality_guard_max_close_basis_slip_bps
        if slip_bps > threshold:
            key = self._close_quality_guard_key(base_asset, close_reason)
            if self.close_quality_guard_cooldown_sec > 0:
                self._close_quality_guard_cooldown[key] = (
                    datetime.now() + timedelta(seconds=self.close_quality_guard_cooldown_sec)
                )
            logger.warning(
                f"平仓质量保护触发 | {base_asset} | reason={close_reason} | "
                f"close_basis_slip={slip_bps:.1f}bps>{threshold:.1f}bps | "
                f"暂停同标的后续平仓{self.close_quality_guard_cooldown_sec}s"
            )
            return
        logger.info(
            f"平仓质量保护通过 | {base_asset} | reason={close_reason} | "
            f"close_basis_slip={slip_bps:.1f}bps<={threshold:.1f}bps"
        )

    def _check_spot_close_min_notional(self, pos: Dict, orderbook_row: Dict) -> tuple[bool, str]:
        """普通止盈前确认 Binance spot 腿不低于最小名义金额。"""
        base_asset = str(pos.get('base_asset') or '').upper()
        meta = self.spot_meta.get(base_asset) or {}
        min_notional = _float_or_none(meta.get('min_notional'))
        if min_notional is None or min_notional <= 0:
            return True, ''

        qty = _float_or_none(pos.get('spot_open_qty')) or 0.0
        if qty <= CLOSE_QTY_TOLERANCE:
            return True, ''

        price = self._spot_close_reference_price(pos, orderbook_row)
        if price is None or price <= 0:
            return True, ''

        notional = qty * price
        if notional + 1e-9 >= min_notional:
            return True, ''
        return False, (
            f"低名义残仓跳过平仓|qty={qty:g}|price={price:g}|"
            f"notional={notional:.4f}<min_notional={min_notional:g}USDT|避免单腿成交"
        )

    @staticmethod
    def _spot_close_reference_price(pos: Dict, orderbook_row: Dict) -> Optional[float]:
        for key in (
            'spot_close_vwap',
            'spot_price_bid_1',
            'spot_bid_price_1',
            'spot_bid_1',
            'spot_close_price',
        ):
            price = _float_or_none(orderbook_row.get(key))
            if price is not None and price > 0:
                return price
        price = _float_or_none(pos.get('spot_open_price'))
        return price if price is not None and price > 0 else None

    def _mark_low_notional_residual(self, pos: Dict, reason: str) -> None:
        position_id = pos.get('id')
        if not position_id:
            return
        sql = """
            UPDATE mi_trade_position
            SET close_reason = CASE
                WHEN close_reason IS NULL OR close_reason = '' THEN %(reason)s
                WHEN close_reason NOT LIKE %(reason_like)s THEN CONCAT(close_reason, '|', %(reason)s)
                ELSE close_reason
            END
            WHERE id = %(position_id)s
              AND status = 'holding'
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'reason': reason,
                'reason_like': '%低名义残仓跳过平仓%',
                'position_id': position_id,
            })

    def _valley_key(self, base_asset: str, pos: Optional[Dict] = None):
        """止盈谷底状态按持仓隔离；无 position_id 时兼容旧测试/降级为 base_asset。"""
        if pos:
            position_id = pos.get('id') or pos.get('position_id')
            if position_id:
                return int(position_id)
        return base_asset

    def _clear_position_close_state(self, base_asset: str, pos: Optional[Dict] = None) -> None:
        key = self._valley_key(base_asset, pos)
        self._valley_state.pop(key, None)
        self._last_take_profit_eval.pop(key, None)
        self._close_resiliency.clear(base_asset)

    def _annotate_resiliency_row(self, row: Dict, base_asset: str) -> None:
        row['_future_qty_multiplier'] = self._get_quanto_multiplier(base_asset)

    def _pass_close_resiliency_check(
        self, base_asset: str, row: Dict, close_basis_bps: float, pos: Dict
    ) -> bool:
        open_spread_bps = float(pos.get('open_spread_bps') or 0)
        result = self._close_resiliency.check(
            base_asset,
            row,
            basis_bps=close_basis_bps,
            coverage_threshold=self._close_resiliency_coverage_threshold,
            max_basis_bps=open_spread_bps,
            max_basis_rebound_bps=self._close_resiliency_max_basis_rebound_bps,
        )
        m = result.metrics
        metric_text = (
            f"recovery={m.get('recovery_ratio', 0):.2f} | "
            f"drop={m.get('shock_drop_ratio', 0):.2f} | "
            f"basis_vol={m.get('basis_volatility_bps', 0):.1f}bps | "
            f"spread_widen={m.get('max_spread_widen_bps', 0):.1f}bps | "
            f"coverage={m.get('coverage')}"
        )
        if result.passed:
            logger.info(f"平仓盘口恢复通过 | {base_asset} | reason={result.reason} | {metric_text}")
            return True
        if result.terminal:
            self._clear_position_close_state(base_asset, pos)
            logger.info(f"平仓盘口恢复终止 | {base_asset} | reason={result.reason} | {metric_text}")
            return False
        logger.info(f"平仓盘口恢复等待 | {base_asset} | reason={result.reason} | {metric_text}")
        return False

    # ──────────────────────────────────────────────────────────────────
    # 条件检查
    # ──────────────────────────────────────────────────────────────────

    def _parse_datetime(self, value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        normalized = text.replace('Z', '').replace('+00:00', '')
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(text.replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError:
            return None

    def _funding_periods_per_day(self, base_asset: str) -> float:
        meta = self.contract_meta.get(base_asset, {})
        interval_sec = float(meta.get('funding_interval') or meta.get('funding_interval_sec') or 28800)
        if interval_sec <= 0:
            interval_sec = 28800
        return max(1.0, 86400.0 / interval_sec)

    def _next_funding_bps(self, pos: Dict) -> Optional[float]:
        funding_rate_24h = pos.get('funding_rate_24h')
        if funding_rate_24h is None:
            return None
        base_asset = pos.get('base_asset', '')
        return float(funding_rate_24h) * 10000.0 / self._funding_periods_per_day(base_asset)

    def _time_to_next_funding_min(self, pos: Dict) -> Optional[float]:
        next_at = self._parse_datetime(pos.get('funding_next_apply') or pos.get('next_funding_time'))
        if next_at is None:
            return None
        return (next_at - datetime.now()).total_seconds() / 60.0

    def _profit_components(self, pos: Dict, close_basis_bps: float) -> Dict[str, float]:
        open_spread_bps = float(pos.get('open_spread_bps') or 0)
        funding_earned_bps = float(pos.get('funding_pnl_bps') or 0)
        spread_profit_bps = open_spread_bps - float(close_basis_bps)
        gross_profit_bps = spread_profit_bps + funding_earned_bps
        net_profit_bps = gross_profit_bps - self.fee_full_bps
        return {
            'open_spread_bps': open_spread_bps,
            'close_basis_bps': float(close_basis_bps),
            'spread_profit_bps': spread_profit_bps,
            'funding_earned_bps': funding_earned_bps,
            'gross_profit_bps': gross_profit_bps,
            'fee_full_bps': self.fee_full_bps,
            'net_profit_bps': net_profit_bps,
        }

    def _funding_context_text(self, pos: Dict) -> str:
        next_bps = self._next_funding_bps(pos)
        next_min = self._time_to_next_funding_min(pos)
        if next_bps is None or next_min is None:
            return 'nextFunding(NA)'
        return f'nextFunding({next_bps:+.1f}bps,{next_min:.0f}min)'

    @staticmethod
    def _is_high_basis_position(pos: Dict) -> bool:
        return '高基差通道' in str(pos.get('open_reason') or '')

    def _effective_take_profit_bps(self, pos: Dict) -> float:
        if self._is_high_basis_position(pos):
            return max(float(self.high_basis_close_take_profit_bps or 0.0), 0.0)
        return float(self.fixed_take_profit_bps)

    def _should_hold_for_positive_funding(self, pos: Dict, close_basis_bps: float) -> bool:
        if not self.positive_funding_hold_enabled:
            return False
        if self._is_high_basis_position(pos) and not self.high_basis_close_positive_funding_hold_enabled:
            return False
        next_bps = self._next_funding_bps(pos)
        next_min = self._time_to_next_funding_min(pos)
        if next_bps is None or next_min is None:
            return False
        if next_min < 0 or next_min > self.positive_funding_hold_window_min:
            return False
        if next_bps < self.positive_funding_hold_min_bps:
            return False
        comps = self._profit_components(pos, close_basis_bps)
        logger.info(
            f"止盈暂缓等待正资金费 | {pos.get('base_asset')} | "
            f"net={comps['net_profit_bps']:.1f}bps | next={next_bps:+.1f}bps | "
            f"next_in={next_min:.1f}min"
        )
        return True

    def _negative_current_24h_bps(self, pos: Dict) -> float:
        funding_rate_24h = pos.get('funding_rate_24h')
        if funding_rate_24h is None:
            return 0.0
        rate_bps = float(funding_rate_24h) * 10000.0
        return abs(rate_bps) if rate_bps < 0 else 0.0

    def _negative_paid_bps(self, pos: Dict) -> float:
        return max(0.0, float(pos.get('funding_rate_sum_bps') or 0.0))

    def _check_negative_funding_exit(self, pos: Dict) -> bool:
        """负 funding 强制退出：临近结算且下一期仍明显为负才触发。"""
        return self._negative_funding_state(pos) == 'force_exit'

    def _delist_exit_risks(self, pos: Dict) -> List[Dict]:
        if not self.delist_risk_exit_enabled:
            return []
        asset = str(pos.get('base_asset') or '').strip().upper()
        if not asset:
            return []
        return [
            item for item in self._delist_risk_by_asset.get(asset, [])
            if self._is_delist_exit_risk(item)
        ]

    def _check_delist_risk_exit(self, pos: Dict) -> bool:
        """下架风险退出：临近下架窗口时不等待止盈/正资金费继续持有。"""
        return bool(self._delist_exit_risks(pos))

    def _is_delist_exit_risk(self, item: Dict) -> bool:
        status = str(item.get('status') or '').strip().lower()
        risk_key = str(item.get('risk_key') or '').strip().lower()
        message = str(item.get('message') or '')
        if status in {'delisting', 'delisted'} or 'in_delisting' in risk_key or '已进入下架流程' in message:
            return True

        delist_at = self._parse_delist_at(item.get('delist_at'))
        if delist_at is not None:
            return delist_at <= datetime.now() + timedelta(days=self.delist_risk_exit_days)

        try:
            days_left = item.get('days_left')
            if days_left is not None:
                return float(days_left) <= self.delist_risk_exit_days
        except (TypeError, ValueError):
            return False
        return False

    @staticmethod
    def _parse_delist_at(value) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        text = str(value).strip()
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            return None

    def _negative_funding_state(self, pos: Dict) -> str:
        """返回负 funding 状态：none / watch / force_exit。"""
        if not self.negative_funding_exit_enabled:
            return 'none'
        current_threshold = abs(float(self.negative_funding_exit_current_24h_bps or 0.0))
        current_window_min = abs(float(self.negative_funding_exit_current_window_min or 0.0))
        extreme_threshold = abs(float(self.negative_funding_exit_extreme_24h_bps or 0.0))
        next_threshold = abs(float(self.negative_funding_exit_next_bps or 0.0))
        paid_threshold = abs(float(self.negative_funding_exit_paid_bps or 0.0))
        current_neg = self._negative_current_24h_bps(pos)
        paid_neg = self._negative_paid_bps(pos)
        next_min = self._time_to_next_funding_min(pos)
        next_bps = self._next_funding_bps(pos)
        near_next_funding = next_min is not None and 0 <= next_min <= current_window_min
        next_still_negative = (
            next_bps is not None
            and (next_threshold <= 0 or next_bps <= -next_threshold)
        )
        if (
            current_threshold > 0
            and current_neg >= current_threshold
            and near_next_funding
            and next_still_negative
        ):
            return 'force_exit'

        if paid_threshold > 0 and paid_neg >= paid_threshold:
            return 'watch'
        if extreme_threshold > 0 and current_neg >= extreme_threshold:
            return 'watch'
        if current_threshold > 0 and current_neg >= current_threshold:
            return 'watch'
        return 'none'

    @staticmethod
    def _margin_close_key(pos: Dict) -> tuple:
        position_id = pos.get('id')
        if position_id is not None:
            return ('position', str(position_id))
        return (
            'contract',
            str(pos.get('future_contract') or ''),
            str(pos.get('base_asset') or ''),
        )

    def _claim_margin_close(self, key: tuple) -> bool:
        with self._margin_close_inflight_lock:
            if key in self._margin_close_inflight:
                return False
            self._margin_close_inflight.add(key)
            return True

    def _release_margin_close(self, key: tuple) -> None:
        with self._margin_close_inflight_lock:
            self._margin_close_inflight.discard(key)

    def _risk_timestamp_is_fresh(self, risk: Optional[Dict], field: str) -> bool:
        if not isinstance(risk, dict):
            return False
        if self.gate_cross_risk_max_age_sec <= 0:
            return True
        fetched_at_ts = _float_or_none(risk.get(field))
        if fetched_at_ts is None or fetched_at_ts <= 0:
            return False
        age_sec = time.time() - fetched_at_ts
        return -5.0 <= age_sec <= self.gate_cross_risk_max_age_sec

    def _is_gate_cross_risk_fresh(self, risk: Optional[Dict]) -> bool:
        return self._risk_timestamp_is_fresh(risk, 'account_fetched_at_ts')

    def _is_gate_position_risk_fresh(self, risk: Optional[Dict]) -> bool:
        return self._risk_timestamp_is_fresh(risk, 'positions_fetched_at_ts')

    def _maintenance_margin_rate(
        self,
        pos: Dict,
        cross_risk: Optional[Dict] = None,
    ) -> Optional[float]:
        """Gate 正向仓位固定全仓，读取新鲜的官方账户 cross_mmr。"""
        return self._latest_gate_cross_account_mmr(cross_risk)

    def _latest_gate_cross_account_mmr(
        self,
        cross_risk: Optional[Dict] = None,
    ) -> Optional[float]:
        risk = cross_risk if cross_risk is not None else self._latest_gate_cross_risk()
        if not self._is_gate_cross_risk_fresh(risk):
            return None
        return _float_or_none(risk.get('account_mmr_pct'))

    def _latest_gate_cross_risk(self) -> Optional[Dict]:
        now = time.time()
        if self._gate_cross_risk_provider is not None:
            try:
                risk = self._gate_cross_risk_provider()
                if isinstance(risk, dict):
                    self._gate_cross_risk_cache = {'ts': now, 'risk': risk}
                    return risk
            except Exception as exc:
                logger.warning("读取实时 Gate 全仓风险失败: %s", exc, exc_info=True)

        if now - float(self._gate_cross_risk_cache.get('ts') or 0.0) <= 5.0:
            risk = self._gate_cross_risk_cache.get('risk')
            return risk if isinstance(risk, dict) else None

        risk = None
        try:
            sql = """
                SELECT detail
                FROM mi_capital_snapshot
                WHERE exchange = 'gate'
                ORDER BY snapshot_at DESC
                LIMIT 1
            """
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
            detail = row.get('detail') if row else None
            if isinstance(detail, str) and detail:
                parsed = json.loads(detail)
            elif isinstance(detail, dict):
                parsed = detail
            else:
                parsed = {}
            candidate = parsed.get('gate_cross_risk') if isinstance(parsed, dict) else None
            if isinstance(candidate, dict):
                risk = candidate
        except Exception as exc:
            logger.warning("读取 Gate 全仓风险快照失败: %s", exc, exc_info=True)
        self._gate_cross_risk_cache = {'ts': now, 'risk': risk}
        return risk

    def margin_risk_refresh_summary(self, positions: List[Dict]) -> Dict:
        """返回需要绕过 Gate 风险快照缓存的原因摘要。"""
        summary = {'danger': [], 'missing': []}
        if not self.margin_danger_path_enabled:
            return summary
        cross_risk = self._latest_gate_cross_risk()
        for pos in positions or []:
            if pos.get('status') != 'holding':
                continue
            asset = pos.get('base_asset') or pos.get('future_contract') or ''
            if self._margin_danger_state(pos, cross_risk=cross_risk).get('active'):
                summary['danger'].append(asset)
            if self._missing_gate_margin_risk(pos, cross_risk=cross_risk):
                summary['missing'].append(asset)
        return summary

    def needs_fresh_margin_risk(self, positions: List[Dict]) -> bool:
        """是否需要绕过 Gate 风险快照缓存，立刻刷新 MMR/强平价。"""
        summary = self.margin_risk_refresh_summary(positions)
        return bool(summary.get('danger') or summary.get('missing'))

    def _missing_gate_margin_risk(
        self,
        pos: Dict,
        cross_risk: Optional[Dict] = None,
    ) -> bool:
        """持仓仍有 Gate 腿，但缺少 MMR/强平价字段时需要刷新 Gate 风险。"""
        if not self.margin_danger_missing_risk_force_refresh:
            return False
        future_qty = abs(_float_or_none(pos.get('future_open_qty')) or 0.0)
        future_contract = str(pos.get('future_contract') or '').strip()
        if future_qty <= 0 or not future_contract:
            return False
        risk = cross_risk if cross_risk is not None else self._latest_gate_cross_risk()
        risk_status = str((risk or {}).get('status') or '').strip().lower()
        return (
            risk_status in {'', 'idle', 'unknown'}
            or self._latest_gate_cross_account_mmr(risk) is None
            or not self._is_gate_position_risk_fresh(risk)
            or pos.get('gate_liq_price') is None
        )

    def _margin_danger_state(
        self,
        pos: Dict,
        cross_risk: Optional[Dict] = None,
    ) -> Dict:
        if not self.margin_danger_path_enabled:
            return {'active': False, 'reasons': []}

        risk = cross_risk if cross_risk is not None else self._latest_gate_cross_risk()
        risk_status = str((risk or {}).get('status') or '').strip().lower()
        reasons = []
        margin_rate = self._maintenance_margin_rate(pos, risk)
        if (
            risk_status == 'danger'
            and margin_rate is not None
            and margin_rate <= self.margin_danger_mmr_pct
        ):
            reasons.append(f"MMR{margin_rate:.2f}%<={self.margin_danger_mmr_pct:.1f}%")

        liq_price = _float_or_none(pos.get('gate_liq_price'))
        ref_price = self._margin_danger_reference_price(pos)
        liq_distance_bps = None
        at_liq = False
        if (
            risk_status == 'danger'
            and self._is_gate_position_risk_fresh(risk)
            and liq_price is not None
            and liq_price > 0
            and ref_price is not None
            and ref_price > 0
        ):
            # 正向策略 Gate 腿为空头，价格上涨接近/穿过强平价时最危险。
            liq_distance_bps = (liq_price - ref_price) / ref_price * 10000.0
            at_liq = liq_distance_bps <= 0
            if liq_distance_bps <= self.margin_danger_liq_distance_bps:
                reasons.append(
                    f"距强平价{liq_distance_bps:.1f}bps<="
                    f"{self.margin_danger_liq_distance_bps:.1f}bps"
                )

        return {
            'active': bool(reasons),
            'reasons': reasons,
            'margin_rate': margin_rate,
            'ref_price': ref_price,
            'liq_price': liq_price,
            'liq_distance_bps': liq_distance_bps,
            'at_liq': at_liq,
            'risk_status': risk_status or 'unknown',
        }

    @staticmethod
    def _margin_danger_reference_price(pos: Dict) -> Optional[float]:
        for key in (
            'gate_mark_price',
            'current_future_price',
            'future_close_vwap',
            'future_open_price',
        ):
            value = _float_or_none(pos.get(key))
            if value is not None and value > 0:
                return value
        return None

    def _build_margin_close_detail(
        self,
        pos: Dict,
        prefix: str,
        danger: Optional[Dict] = None,
        cross_risk: Optional[Dict] = None,
    ) -> str:
        parts = [prefix]
        margin_rate = self._maintenance_margin_rate(pos, cross_risk)
        if margin_rate is not None and '保证金/维持保证金' not in prefix:
            parts.append(f"保证金/维持保证金{margin_rate:.2f}%")
        cross_risk = cross_risk or self._latest_gate_cross_risk() or {}
        account_equity = _float_or_none(cross_risk.get('account_equity_usdt'))
        total_maintenance = _float_or_none(cross_risk.get('maintenance_margin_usdt'))
        if account_equity is not None:
            parts.append(f"全仓权益{account_equity:.4f}")
        if total_maintenance is not None:
            parts.append(f"全仓维持保证金{total_maintenance:.4f}")

        danger = danger or {}
        if danger.get('liq_price') is not None:
            parts.append(f"强平价{float(danger['liq_price']):.8g}")
        if danger.get('ref_price') is not None:
            parts.append(f"参考价{float(danger['ref_price']):.8g}")
        if danger.get('liq_distance_bps') is not None:
            parts.append(f"距强平{float(danger['liq_distance_bps']):.1f}bps")
        if danger.get('reasons'):
            parts.append(f"危险判定({';'.join(danger['reasons'])})")

        if danger.get('active'):
            parts.append("全仓风险触发")
        parts.append("全量市价平仓")
        return '|'.join(parts)

    def _check_funding_count(self, pos: Dict) -> bool:
        """兼容旧测试/外部调用：资金费次数不再作为强制平仓条件。"""
        return False

    def _check_take_profit(
        self,
        pos: Dict,
        current_spread_bps: float,
        close_threshold_meta: Optional[Dict] = None,
    ) -> bool:
        """
        止盈条件：
            fixed_net_bps:
                基差收敛 + 已收资金费 - 全部手续费 >= 动态净止盈阈值
            legacy_p40:
                总盈亏bps(基差收敛 + 已收资金费) >= percentile_40费率(bps) * multiplier + 手续费(bps)
        """
        ba = pos.get('base_asset', '')
        if self.take_profit_mode == 'fixed_net_bps':
            eval_ = self._take_profit_eval(pos, current_spread_bps, close_threshold_meta)
            self._last_take_profit_eval[self._valley_key(ba, pos)] = eval_
            if eval_.net_profit_bps < eval_.threshold_bps:
                return False
            if self._should_hold_for_positive_funding(pos, current_spread_bps):
                return False
            return True

        # 从历史分位获取基准费率
        funding_rate_p40 = self.funding_rate_p40_meta.get(ba)
        if funding_rate_p40 is None or funding_rate_p40 <= 0:
            return False  # 无历史数据或历史中位数也为负，不触发止盈

        open_spread_bps = float(pos.get('open_spread_bps') or 0)
        # 基差收敛利润 = 开仓基差 - 当前基差（收敛则为正）
        spread_profit_bps = open_spread_bps - current_spread_bps
        # 已实现资金费收益(bps)
        funding_earned_bps = float(pos.get('funding_pnl_bps') or 0)
        # 总盈亏 = 基差收敛 + 已收资金费
        total_pnl_bps = spread_profit_bps + funding_earned_bps

        # percentile_40 是24小时费率原始值（如 0.0003），转为 bps
        funding_rate_bps = funding_rate_p40 * 10000
        # 止盈阈值 = N天资金费等价收益 + 全部开平仓手续费(bps)
        fee_cost_bps = self.fee_full_bps
        threshold = funding_rate_bps * self.take_profit_multiplier + fee_cost_bps
        return total_pnl_bps >= threshold

    def _take_profit_eval(
        self,
        pos: Dict,
        current_spread_bps: float,
        close_threshold_meta: Optional[Dict] = None,
    ):
        ba = pos.get('base_asset', '')
        threshold_meta = close_threshold_meta
        if threshold_meta is None:
            threshold_meta = self._active_close_vwap_threshold_meta.get(ba, {})
        return evaluate_dynamic_take_profit(
            position=pos,
            close_basis_bps=float(current_spread_bps),
            close_threshold_meta=threshold_meta,
            close_threshold_col=self.close_threshold_col,
            fixed_take_profit_bps=self._effective_take_profit_bps(pos),
            fee_full_bps=self.fee_full_bps,
            cfg=self.dynamic_take_profit_cfg,
        )

    def _pre_execution_gate(
        self,
        base_asset: str,
        contract: str,
        symbol: str,
        pos: Dict,
        require_profit: bool = True,
    ) -> tuple:
        """
        平仓最终风控旁路：下单前用单标的最短链路重新校验平仓条件。

        设计目的：
        - 拦截“信号过期”场景（谷底确认后基差已回弹至不利平仓的水平）
        - 检测盘口数据陈旧性（低流动性标的WS更新稀疏）
        - 确保下单用的数据 = 校验用的数据

        校验逻辑：重新计算平仓VWAP基差，确认平仓仍然有利可图：
        - 新基差未大幅回弹超过开仓基差（即仍有收敛空间）

        Args:
            base_asset: 标的资产 (e.g. 'BTC')
            contract: Gate合约名 (e.g. 'BTC_USDT')
            symbol: Binance交易对 (e.g. 'BTCUSDT')
            pos: 当前持仓记录（含 open_spread_bps）
            require_profit: True=止盈旁路，复核收敛/固定净收益；False=风险平仓旁路，只查执行质量

        Returns:
            (passed, fresh_row, gate_basis_bps, reject_reason)
            - passed: 是否通过最终风控
            - fresh_row: 通过时返回合并后的最新盘口行（供后续下单使用）
            - gate_basis_bps: 最终校验时的平仓VWAP基差
            - reject_reason: 未通过时的拒绝原因
        """
        # 未注入 manager 时退化为放行（兼容测试场景）
        if not self._gate_manager or not self._spot_manager:
            return True, None, None, ''

        try:
            # ── 1. 单标的盘口读取（最短链路）──
            gate_ob = self._gate_manager.get_orderbook(contract)
            spot_ob = self._spot_manager.get_orderbook(symbol)

            if not gate_ob or not spot_ob:
                return False, None, None, f'盘口不可用(gate={gate_ob is not None}, spot={spot_ob is not None})'

            gate_row = gate_ob.to_dict_row()
            spot_row = spot_ob.to_dict_row()

            # ── 2. 盘口新鲜度硬约束：以本地 last_update_time 为准计算 lag_ms（破除同源缺陷）──
            # 原因：update_time(交易所推送时间戳) 在快照重建后会立刻“看起来很新”但内容可能是异常价。
            # 只有 last_update_time（本地接收时刻）能客观反映“现在距上次收到 WS 增量多久”。
            now_ts = time.time()
            gate_local_ts = float(getattr(gate_ob, 'last_update_time', 0) or 0)
            spot_local_ts = float(getattr(spot_ob, 'last_update_time', 0) or 0)
            gate_lag_ms = (now_ts - gate_local_ts) * 1000.0 if gate_local_ts > 0 else float('inf')
            spot_lag_ms = (now_ts - spot_local_ts) * 1000.0 if spot_local_ts > 0 else float('inf')

            if gate_lag_ms > self._max_orderbook_lag_ms or spot_lag_ms > self._max_orderbook_lag_ms:
                logger.info(
                    f"平仓旁路-行情滞后拦截 | {base_asset} | "
                    f"now={now_ts:.3f} | gate_local={gate_local_ts:.3f}(lag={gate_lag_ms:.0f}ms) | "
                    f"spot_local={spot_local_ts:.3f}(lag={spot_lag_ms:.0f}ms) | "
                    f"max={self._max_orderbook_lag_ms:.0f}ms"
                )
                return False, None, None, (
                    f'行情滞后(gate_lag={gate_lag_ms:.0f}ms, spot_lag={spot_lag_ms:.0f}ms, '
                    f'max={self._max_orderbook_lag_ms:.0f}ms)'
                )

            gate_ready = getattr(gate_ob, 'is_ready', lambda: True)()
            if not gate_ready:
                logger.info(
                    f"平仓旁路-Gate本地簿未就绪拦截 | {base_asset} | "
                    f"update_count={getattr(gate_ob, 'update_count', None)} | "
                    f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                )
                return False, None, None, 'Gate本地簿未接上连续WS增量'

            book_skew_ms = abs(gate_local_ts - spot_local_ts) * 1000.0
            if book_skew_ms > self._max_orderbook_lag_ms:
                logger.info(
                    f"平仓旁路-跨所盘口不同步拦截 | {base_asset} | "
                    f"skew={book_skew_ms:.0f}ms > max={self._max_orderbook_lag_ms:.0f}ms | "
                    f"gate_local={gate_local_ts:.3f} | spot_local={spot_local_ts:.3f}"
                )
                return False, None, None, (
                    f'跨所盘口不同步(skew={book_skew_ms:.0f}ms, max={self._max_orderbook_lag_ms:.0f}ms)'
                )

            # ── 3. 合并 + 计算对冲指标（单元素列表，开销极小）──
            from calc.merge_cross_exchange_orderbook import merge_orderbook_records
            from calc.calculate_hedge_metrics import calculate_hedge_metrics

            open_amount_usdt = config.get_float('trade.open.amount_usdt', 5)
            merged = merge_orderbook_records([gate_row], [spot_row])
            if not merged:
                return False, None, None, '盘口合并失败'

            merged = calculate_hedge_metrics(
                merged, self.contract_meta, self.spot_meta, open_amount_usdt
            )
            row = merged[0]

            # ── 4. 计算平仓VWAP基差 ──
            gate_basis_bps = calc_vwap_basis_bps(
                row.get('spot_close_vwap'), row.get('future_close_vwap')
            )
            if gate_basis_bps is None:
                return False, None, None, '平仓VWAP基差计算失败(盘口深度不足)'
            gate_basis_bps = round(gate_basis_bps, 2)

            coverages = [
                row.get('close_coverage'),
                row.get('spot_close_coverage'),
                row.get('future_close_coverage'),
            ]
            valid_coverages = [float(c) for c in coverages if c is not None]
            coverage = max(valid_coverages) if valid_coverages else None
            if coverage is not None and coverage > self._close_resiliency_coverage_threshold:
                logger.info(
                    f"平仓旁路-盘口覆盖过高拦截 | {base_asset} | "
                    f"coverage={coverage:.3f} > max={self._close_resiliency_coverage_threshold:.3f}"
                )
                return False, row, gate_basis_bps, (
                    f'盘口覆盖过高(coverage={coverage:.3f} > '
                    f'{self._close_resiliency_coverage_threshold:.3f})'
                )

            if not require_profit:
                self._last_orderbook_lag_ms[base_asset] = (gate_lag_ms, spot_lag_ms)
                logger.info(
                    f"风险平仓旁路通过 | {base_asset} | "
                    f"gate_basis={gate_basis_bps:.2f}bps | "
                    f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms,"
                    f"skew={book_skew_ms:.0f}ms,max={self._max_orderbook_lag_ms:.0f}ms)"
                )
                return True, row, gate_basis_bps, ''

            # ── 5. 盈利性校验：确认平仓仍有利可图 ──
            # 平仓基差应 < 开仓基差（即价差已收敛）
            # 如果新基差 >= 开仓基差，说明收敛已完全逆转，平仓会亏损
            open_spread_bps = float(pos.get('open_spread_bps') or 0)
            if gate_basis_bps >= open_spread_bps:
                logger.info(
                    f"平仓旁路-收敛逆转拦截 | {base_asset} | "
                    f"gate_basis={gate_basis_bps:.2f}bps >= open_spread={open_spread_bps:.2f}bps | "
                    f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                )
                return False, row, gate_basis_bps, (
                    f'收敛逆转(平仓基差{gate_basis_bps:.1f}bps >= '
                    f'开仓基差{open_spread_bps:.1f}bps, 无收敛利润)'
                )

            # ── 6. 基差回弹幅度检查：与触发时相比不应回弹过大 ──
            # 如果新基差比开仓基差的收敛空间回弹超过 50%，说明收敛已大幅逆转
            convergence_total = open_spread_bps - gate_basis_bps
            original_convergence = open_spread_bps - float(pos.get('current_spread_bps') or gate_basis_bps)
            if original_convergence > 0 and convergence_total > 0:
                reversion_ratio = (gate_basis_bps - float(pos.get('current_spread_bps') or gate_basis_bps)) / original_convergence
                if reversion_ratio > 0.5:
                    logger.info(
                        f"平仓旁路-基差回弹过大拦截 | {base_asset} | "
                        f"reversion_ratio={reversion_ratio:.2%} | "
                        f"trigger_basis={pos.get('current_spread_bps')}bps→gate_basis={gate_basis_bps:.2f}bps"
                    )
                    return False, row, gate_basis_bps, (
                        f'基差回弹过大(回弹{reversion_ratio:.0%}, '
                        f'触发时{pos.get("current_spread_bps")}bps→当前{gate_basis_bps}bps)'
                    )

            if self.take_profit_mode == 'fixed_net_bps':
                eval_ = self._take_profit_eval(pos, gate_basis_bps)
                self._last_take_profit_eval[self._valley_key(base_asset, pos)] = eval_
                if eval_.net_profit_bps < eval_.threshold_bps:
                    logger.info(
                        f"平仓旁路-动态净止盈不足拦截 | {base_asset} | "
                        f"net={eval_.net_profit_bps:.2f}bps < {eval_.threshold_bps:.2f}bps | "
                        f"hold={eval_.hold_value_bps:.2f}bps | "
                        f"gate_basis={gate_basis_bps:.2f}bps"
                    )
                    return False, row, gate_basis_bps, (
                        f'动态净止盈不足(净{eval_.net_profit_bps:.1f}bps < '
                        f'{eval_.threshold_bps:.1f}bps, hold={eval_.hold_value_bps:.1f})'
                    )
                if self._should_hold_for_positive_funding(pos, gate_basis_bps):
                    return False, row, gate_basis_bps, (
                        f'下一期正资金费暂缓({self._funding_context_text(pos)})'
                    )

            # ── 全部通过 ──
            # 记录本次旁路风控读取到的 lag，供 _build_take_profit_detail 拼接到平仓原因
            self._last_orderbook_lag_ms[base_asset] = (gate_lag_ms, spot_lag_ms)
            logger.info(
                f"平仓旁路通过 | {base_asset} | "
                f"gate_basis={gate_basis_bps:.2f}bps | open_spread={open_spread_bps:.2f}bps | "
                f"trigger_basis={pos.get('current_spread_bps')}bps | "
                f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms,"
                f"skew={book_skew_ms:.0f}ms,max={self._max_orderbook_lag_ms:.0f}ms)"
            )
            return True, row, gate_basis_bps, ''

        except Exception as e:
            # 异常时退化为放行（不因旁路故障阻塞正常平仓）
            logger.warning(f'平仓最终风控旁路异常(退化放行) | {base_asset}: {e}')
            return True, None, None, ''

    def _pass_valley_check(self, base_asset: str, current_spread_bps: float, pos: Dict) -> bool:
        """
        谷底反弹确认逻辑（止盈时等待基差见底再平仓，捕捉更多收敛利润）:
        - 止盈首次满足: 记录当前 spread 为谷底, 返回 False(等待)
        - spread 继续下降: 更新谷底, 返回 False(继续等待)
        - spread 从谷底反弹 X%: 返回 True(确认平仓)
        - 超时: 返回 True(直接平仓)
        """
        now = datetime.now()
        state_key = self._valley_key(base_asset, pos)
        state = self._valley_state.get(state_key)
    
        if state is None:
            # 首次进入监控，记录谷底、开始时间
            open_spread_bps = float(pos.get('open_spread_bps') or 0)
            self._valley_state[state_key] = {
                'valley_bps': current_spread_bps,
                'start_time': now,
                'open_spread_bps': open_spread_bps,
                'trigger': None,
            }
            logger.info(
                f"止盈谷底监控开始 | {base_asset} | "
                f"position_id={state_key} | "
                f"spread={current_spread_bps:.2f}bps | open_spread={open_spread_bps:.2f}bps | "
                f"start_time={now.strftime('%H:%M:%S.%f')[:-3]}"
            )
            return False
    
        # 更新谷底
        if current_spread_bps < state['valley_bps']:
            state['valley_bps'] = current_spread_bps
    
        # 检查超时
        elapsed_sec = (now - state['start_time']).total_seconds()
        if elapsed_sec >= self.valley_monitor_timeout_sec:
            state['trigger'] = 'timeout'
            logger.info(
                f"止盈谷底监控超时，直接平仓 | {base_asset} | "
                f"position_id={state_key} | "
                f"valley={state['valley_bps']:.2f}bps | current={current_spread_bps:.2f}bps | "
                f"elapsed={elapsed_sec:.1f}s≥{self.valley_monitor_timeout_sec}s | "
                f"start={state['start_time'].strftime('%H:%M:%S.%f')[:-3]}→"
                f"now={now.strftime('%H:%M:%S.%f')[:-3]}"
            )
            return True
    
        # 检查反弹确认: 基差从谷底回升超过 X%
        # 使用开仓基差与谷底的差值作为参考范围，避免小值/零值问题
        # rebound_threshold = valley + (open_spread - valley) * rebound_pct
        open_spread_bps = state['open_spread_bps']
        convergence_range = open_spread_bps - state['valley_bps']
        if convergence_range > 0:
            rebound_threshold = state['valley_bps'] + convergence_range * self.valley_rebound_pct
        else:
            # 异常: 谷底高于开仓基差，直接平仓（罕见场景，仍打详细日志）
            logger.info(
                f"止盈谷底异常(谷底>=开仓基差)，直接平仓 | {base_asset} | "
                f"position_id={state_key} | "
                f"valley={state['valley_bps']:.2f} | open_spread={open_spread_bps:.2f} | "
                f"current={current_spread_bps:.2f}"
            )
            state['trigger'] = 'rebound'
            return True
    
        if current_spread_bps >= rebound_threshold:
            state['trigger'] = 'rebound'
            logger.info(
                f"止盈谷底反弹确认，执行平仓 | {base_asset} | "
                f"position_id={state_key} | "
                f"valley={state['valley_bps']:.2f}bps | current={current_spread_bps:.2f}bps | "
                f"rebound_thr={rebound_threshold:.2f}bps | "
                f"open_spread={open_spread_bps:.2f}bps | rebound_pct={self.valley_rebound_pct*100:.0f}% | "
                f"sustained={elapsed_sec:.1f}s | "
                f"start={state['start_time'].strftime('%H:%M:%S.%f')[:-3]}→"
                f"now={now.strftime('%H:%M:%S.%f')[:-3]}"
            )
            return True
    
        return False

    def _build_take_profit_detail(self, pos: Dict, current_spread_bps: float) -> str:
        """构建止盈平仓的详细原因字符串（含谷底确认信息 + 行情新鲜度）"""
        ba = pos.get('base_asset', '')
        if self.take_profit_mode == 'fixed_net_bps':
            eval_key = self._valley_key(ba, pos)
            eval_ = self._last_take_profit_eval.get(eval_key)
            if eval_ is None:
                eval_ = self._take_profit_eval(pos, current_spread_bps)
            if self.dynamic_take_profit_cfg.enabled:
                detail = f"{format_dynamic_take_profit(eval_)}|{self._funding_context_text(pos)}"
            else:
                comps = self._profit_components(pos, current_spread_bps)
                detail = (
                    f"固定净止盈|净{comps['net_profit_bps']:.1f}bps"
                    f"(收敛{comps['spread_profit_bps']:.1f}+资金费{comps['funding_earned_bps']:.1f}"
                    f"-{comps['fee_full_bps']:.0f}费)"
                    f">={self.fixed_take_profit_bps:.1f}bps"
                    f"|{self._funding_context_text(pos)}"
                )
            if self._is_high_basis_position(pos):
                detail = f"高基差仓|{detail}"
        else:
            open_spread_bps = float(pos.get('open_spread_bps') or 0)
            spread_profit_bps = open_spread_bps - current_spread_bps
            funding_earned_bps = float(pos.get('funding_pnl_bps') or 0)
            total_pnl_bps = spread_profit_bps + funding_earned_bps
            funding_rate_p40 = self.funding_rate_p40_meta.get(ba, 0)
            funding_rate_bps = funding_rate_p40 * 10000
            fee_cost_bps = self.fee_full_bps
            threshold = funding_rate_bps * self.take_profit_multiplier + fee_cost_bps

            detail = (
                f"止盈|总盈亏{total_pnl_bps:.1f}bps"
                f"(收敛{spread_profit_bps:.1f}+资金费{funding_earned_bps:.1f})"
                f">={threshold:.1f}bps"
                f"(p40={funding_rate_bps:.1f}×{self.take_profit_multiplier:.0f}+{fee_cost_bps:.0f}费)"
            )

        if self._negative_funding_state(pos) == 'watch':
            detail += f"|负费监控({self._negative_funding_context(pos)})"

        # 附加谷底反弹信息
        valley_state = self._valley_state.get(self._valley_key(ba, pos))
        if valley_state:
            valley_bps = valley_state.get('valley_bps', 0)
            trigger = valley_state.get('trigger', 'unknown')
            if trigger == 'rebound':
                detail += f"|谷底反弹(谷{valley_bps:.1f}→当前{current_spread_bps:.1f})"
            elif trigger == 'timeout':
                elapsed = (datetime.now() - valley_state['start_time']).total_seconds()
                detail += f"|谷底超时(谷{valley_bps:.1f},{elapsed:.0f}s)"

        # 行情新鲜度（旁路风控读取到的 lag）─ 反映“下单时刻距上次收到 WS 增量多久”
        lag = self._last_orderbook_lag_ms.pop(ba, None)
        if lag is not None:
            gate_lag_ms, spot_lag_ms = lag
            def _fmt(ms):
                if ms is None or ms == float('inf'):
                    return 'NA'
                return f'{ms:.0f}ms'
            detail += f"|鲜度(gate={_fmt(gate_lag_ms)},spot={_fmt(spot_lag_ms)})"
        else:
            detail += "|鲜度(NA)"

        return detail

    def _build_negative_funding_exit_detail(self, pos: Dict) -> str:
        """构建负资金费风险平仓原因。"""
        state = self._negative_funding_state(pos)
        current_neg = self._negative_current_24h_bps(pos)
        paid_neg = self._negative_paid_bps(pos)
        current_threshold = abs(float(self.negative_funding_exit_current_24h_bps or 0.0))
        current_window_min = abs(float(self.negative_funding_exit_current_window_min or 0.0))
        extreme_threshold = abs(float(self.negative_funding_exit_extreme_24h_bps or 0.0))
        next_threshold = abs(float(self.negative_funding_exit_next_bps or 0.0))
        paid_threshold = abs(float(self.negative_funding_exit_paid_bps or 0.0))
        next_bps = self._next_funding_bps(pos)
        next_min = self._time_to_next_funding_min(pos)
        next_text = 'NA' if next_bps is None else f'{next_bps:+.1f}bps'
        next_min_text = 'NA' if next_min is None else f'{next_min:.1f}min'
        return (
            f"负资金费风险|state={state}|当前24h负费{current_neg:.1f}bps"
            f"|已付负费{paid_neg:.1f}bps"
            f"|阈值(当前{current_threshold:.1f}@{current_window_min:.0f}min,"
            f"next≤-{next_threshold:.1f},极端监控{extreme_threshold:.1f},"
            f"已付监控{paid_threshold:.1f})"
            f"|next={next_text}|距结算{next_min_text}"
        )

    def _build_delist_risk_exit_detail(self, pos: Dict) -> str:
        """构建下架风险平仓原因。"""
        risks = self._delist_exit_risks(pos)
        if not risks:
            return f"下架风险退出|阈值{self.delist_risk_exit_days:.1f}天|风险详情缺失"
        fragments = []
        for item in risks[:3]:
            exchange = item.get('exchange') or 'unknown'
            market_type = item.get('market_type') or ''
            status = item.get('status') or item.get('risk_type') or ''
            delist_at = item.get('delist_at') or 'NA'
            days_left = item.get('days_left')
            message = item.get('message') or ''
            days_text = 'NA' if days_left is None else str(days_left)
            fragments.append(
                f"{exchange}/{market_type}:{status}|delist_at={delist_at}|days_left={days_text}|{message}"
            )
        return (
            f"下架风险退出|阈值{self.delist_risk_exit_days:.1f}天|"
            + ' || '.join(fragments)
        )

    def _negative_funding_context(self, pos: Dict) -> str:
        current_neg = self._negative_current_24h_bps(pos)
        paid_neg = self._negative_paid_bps(pos)
        next_bps = self._next_funding_bps(pos)
        next_min = self._time_to_next_funding_min(pos)
        next_text = 'NA' if next_bps is None else f'{next_bps:+.1f}bps'
        next_min_text = 'NA' if next_min is None else f'{next_min:.0f}min'
        return (
            f"当前24h负费{current_neg:.1f}bps,"
            f"已付负费{paid_neg:.1f}bps,"
            f"next={next_text},距结算{next_min_text}"
        )

    def _append_lag_detail(self, base_asset: str, detail: str) -> str:
        lag = self._last_orderbook_lag_ms.pop(base_asset, None)
        if lag is None:
            return f"{detail}|鲜度(NA)"
        gate_lag_ms, spot_lag_ms = lag

        def _fmt(ms):
            if ms is None or ms == float('inf'):
                return 'NA'
            return f'{ms:.0f}ms'

        return f"{detail}|鲜度(gate={_fmt(gate_lag_ms)},spot={_fmt(spot_lag_ms)})"

    def _future_close_protective_price(self, row: Dict, slippage_bps: float) -> Optional[float]:
        """期货空头平仓是 buy，用旁路 close VWAP 加保护垫作为 IOC 最高成交价。"""
        price = row.get('future_close_vwap') or row.get('future_ask_price_1') or row.get('future_ask_1')
        if price is None:
            return None
        price = float(price)
        if price <= 0:
            return None
        return round(price * (1 + max(float(slippage_bps), 0.0) / 10000.0), 10)

    def _append_close_basis_compare(
        self,
        detail: str,
        trigger_basis_bps,
        pre_gate_basis_bps,
        actual_basis_bps,
    ) -> str:
        def _fmt(value):
            if value is None:
                return 'NA'
            return f'{float(value):.1f}bps'

        return (
            f"{detail}|平仓基差对比("
            f"trigger={_fmt(trigger_basis_bps)},"
            f"pre_gate={_fmt(pre_gate_basis_bps)},"
            f"actual={_fmt(actual_basis_bps)})"
        )

    @staticmethod
    def _close_basis_slip_bps(pre_gate_basis_bps, actual_basis_bps) -> Optional[float]:
        if pre_gate_basis_bps is None or actual_basis_bps is None:
            return None
        return float(actual_basis_bps) - float(pre_gate_basis_bps)

    # ──────────────────────────────────────────────────────────────────
    # 手动平仓（外部调用入口）
    # ──────────────────────────────────────────────────────────────────

    def manual_close(self, pos: Dict, orderbook_row: Dict) -> Dict:
        """手动一键平仓（跳过条件检查，直接执行）

        Args:
            pos: 持仓记录（需包含 id, base_asset, spot_symbol, future_contract,
                 spot_open_qty, future_open_qty 等字段）
            orderbook_row: 该标的的合并订单簿行数据（传给成交引擎）

        Returns:
            {base_asset, success, order_uuid, close_reason, message}
        """
        close_reason = 'manual'
        # 构建详情：携带当前基差/开仓基差/资金费收益等关键判定数据，便于复盘
        ba = pos.get('base_asset', '')
        open_spread = pos.get('open_spread_bps')
        current_spread = pos.get('current_spread_bps')
        funding_pnl_bps = pos.get('funding_pnl_bps')
        parts = ['手动一键平仓']
        if open_spread is not None and current_spread is not None:
            convergence = float(open_spread) - float(current_spread)
            parts.append(
                f"基差 {float(open_spread):.1f}→{float(current_spread):.1f}bps"
                f"(收敛{convergence:+.1f})"
            )
        if funding_pnl_bps is not None:
            parts.append(f"已收资金费{float(funding_pnl_bps):+.1f}bps")
        margin_rate = self._maintenance_margin_rate(pos)
        if margin_rate is not None:
            parts.append(f"保证金/维持保证金{float(margin_rate):.2f}%")
        close_reason_detail = '|'.join(parts)
        return self._execute_close(
            pos,
            close_reason,
            close_reason_detail,
            orderbook_row,
            future_protective_price=None,
        )

    # ──────────────────────────────────────────────────────────────────
    # 订单构建与执行
    # ──────────────────────────────────────────────────────────────────

    def _execute_close(
        self,
        pos: Dict,
        close_reason: str,
        close_reason_detail: str,
        orderbook_row: Dict,
        pre_gate_basis_bps: Optional[float] = None,
        future_protective_price: Optional[float] = None,
    ) -> Dict:
        """构建平仓订单组 → 调用成交引擎 → 持久化"""
        ba = pos.get('base_asset', '')
        order_group = self._build_close_order_group(
            pos,
            future_protective_price=future_protective_price,
            orderbook_row=orderbook_row,
            close_reason=close_reason,
        )
        future_order = order_group.get('future_order') or {}
        if future_order.get('execution_style') == 'maker':
            close_reason_detail = (
                f"{close_reason_detail}|future maker("
                f"future_buy@{future_order.get('maker_price')},"
                f"ttl={future_order.get('maker_ttl_ms')}ms)"
            )
        exec_result = self.executor_client.execute(order_group, orderbook_row)
        actual_close_basis_bps = self._save_close(
            pos,
            order_group,
            exec_result,
            close_reason,
            close_reason_detail,
            pre_gate_basis_bps=pre_gate_basis_bps,
        )
        close_basis_slip_bps = self._close_basis_slip_bps(
            pre_gate_basis_bps,
            actual_close_basis_bps,
        )
        return {
            'base_asset': ba,
            'success': exec_result['success'],
            'order_uuid': order_group['order_uuid'],
            'close_reason': close_reason,
            'message': exec_result.get('message'),
            'pre_gate_basis_bps': pre_gate_basis_bps,
            'actual_close_basis_bps': actual_close_basis_bps,
            'close_basis_slip_bps': close_basis_slip_bps,
        }

    def _build_close_order_group(
        self,
        pos: Dict,
        future_protective_price: Optional[float] = None,
        orderbook_row: Optional[Dict] = None,
        close_reason: Optional[str] = None,
    ) -> Dict:
        """
        生成平仓订单组：
          现货 sell（bid 侧）+ 期货 buy（ask 侧），方向与开仓相反
        """
        order_uuid = str(uuid.uuid4())
        ba = pos.get('base_asset', '')
        spot_symbol = pos.get('spot_symbol') or f"{ba}USDT"
        future_contract = pos.get('future_contract', '')
        target_amount = config.get_float('trade.open.amount_usdt', 500)

        spot_order = {
            'order_uuid': order_uuid,
            'base_asset': ba,
            'spot_symbol': spot_symbol,
            'future_contract': None,
            'order_side': 'close',
            'market_type': 'spot',
            'trade_direction': 'sell',
            'status': 'pending',
            'target_qty': float(pos.get('spot_open_qty') or 0),
            'target_amount': target_amount,
        }

        future_order = {
            'order_uuid': order_uuid,
            'base_asset': ba,
            'spot_symbol': None,
            'future_contract': future_contract,
            'order_side': 'close',
            'market_type': 'future',
            'trade_direction': 'buy',
            'status': 'pending',
            'target_qty': float(pos.get('future_open_qty') or 0),
            'target_amount': target_amount,
        }
        if future_protective_price is not None and close_reason != 'manual':
            future_order['protective_price'] = future_protective_price
        if close_reason not in FAST_RISK_CLOSE_REASONS and close_reason != 'manual':
            self._apply_future_maker_close(pos, orderbook_row or {}, future_order, close_reason)

        order_group = {
            'order_uuid': order_uuid,
            'base_asset': ba,
            'spot_symbol': spot_symbol,
            'future_contract': future_contract,
            'spot_order': spot_order,
            'future_order': future_order,
        }
        if close_reason in FAST_RISK_CLOSE_REASONS:
            future_order.pop('protective_price', None)
            order_group['execution_sequence'] = 'future_then_spot'
            order_group['execution_reason'] = close_reason
        return order_group

    def _apply_future_maker_close(
        self,
        pos: Dict,
        row: Dict,
        future_order: Dict,
        close_reason: Optional[str] = None,
    ) -> None:
        """给实盘平仓期货腿附加 maker 执行参数；空头买回挂在 future bid1。"""
        if not self.future_maker_close_enabled:
            return
        if self.executor_client.channel != 'Live':
            return

        base_asset = str(pos.get('base_asset') or future_order.get('base_asset') or '').upper()
        tier = str(pos.get('strategy_tier') or '').strip().upper()
        if not tier:
            # close path 没有注入 asset_tier_meta，未知时按 A/B 池处理，避免已有持仓不能平。
            tier = 'B'
        if tier not in self.future_maker_close_allowed_tiers:
            return

        maker_price = row.get('future_price_bid_1') or row.get('future_bid_price_1') or row.get('future_bid_1')
        if maker_price is None:
            return

        maker_price = float(maker_price)
        if maker_price <= 0:
            return
        if self.future_maker_close_price_offset_bps:
            maker_price *= 1 - self.future_maker_close_price_offset_bps / 10000.0

        fallback_allowed = (
            self.future_maker_close_fallback_ioc_enabled
            and tier in self.future_maker_close_fallback_allowed_tiers
        )
        fallback_price = None
        if fallback_allowed:
            slippage_bps = (
                self.protective_ioc_take_profit_slippage_bps
                if close_reason == 'take_profit'
                else self.protective_ioc_risk_slippage_bps
            )
            fallback_price = self._future_close_protective_price(row, slippage_bps)

        future_order.pop('protective_price', None)
        future_order.update({
            'execution_style': 'maker',
            'maker_ttl_ms': self.future_maker_close_ttl_ms,
            'maker_price': maker_price,
            'maker_price_source': 'future_bid1',
            'maker_strategy_tier': tier,
            'maker_taker_reference_price': row.get('future_close_vwap'),
            'maker_spot_reference_price': row.get('spot_close_vwap'),
            'maker_fallback_ioc_enabled': fallback_allowed,
            'maker_fallback_protective_price': fallback_price,
            'maker_fallback_slippage_bps': slippage_bps
            if fallback_price is not None else None,
        })

    def _get_quanto_multiplier(self, base_asset: str) -> float:
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0

    # ──────────────────────────────────────────────────────────────────
    # 持久化
    # ──────────────────────────────────────────────────────────────────

    def _save_close(
        self, pos: Dict, order_group: Dict, exec_result: Dict,
        close_reason: str, close_reason_detail: str,
        pre_gate_basis_bps: Optional[float] = None,
    ) -> Optional[float]:
        """插入 2 笔平仓订单到 mi_trade_order，成功时更新 mi_trade_position 状态"""
        position_id = pos.get('id')

        # 计算平仓基差（仅成交成功时）
        spot_close_price = future_close_price = None
        spot_close_amount = future_close_amount = close_spread_bps = None

        if exec_result.get('success'):
            spot_exec = exec_result.get('spot_order') or {}
            future_exec = exec_result.get('future_order') or {}
            spot_close_price = spot_exec.get('exec_price')
            future_close_price = future_exec.get('exec_price')
            spot_close_amount = spot_exec.get('exec_amount')
            future_close_amount = future_exec.get('exec_amount')
            if spot_close_price and future_close_price:
                basis = calc_vwap_basis_bps(float(spot_close_price), float(future_close_price))
                close_spread_bps = round(basis, 2) if basis is not None else None
            close_reason_detail = self._append_close_basis_compare(
                close_reason_detail,
                pos.get('current_spread_bps'),
                pre_gate_basis_bps,
                close_spread_bps,
            )

        execution_audit = format_execution_audit(exec_result)
        if execution_audit:
            close_reason_detail = f"{close_reason_detail}|{execution_audit}"
        position_close_reason = self._position_close_reason(close_reason_detail)

        # ── 插入平仓订单 ──
        insert_sql = """
            INSERT INTO mi_trade_order (
                order_uuid, position_id, base_asset, spot_symbol, future_contract,
                order_side, market_type, trade_direction, leverage, status, channel,
                reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_vwap_basis_bps, risk_relief_bps,
                open_marginal_basis_bps, funding_rate_24h,
                liquidity_role, fee_rate, fee_amount, fee_amount_usdt, fee_asset, exchange_order_id, executed_at
            ) VALUES (
                %(order_uuid)s, %(position_id)s, %(base_asset)s, %(spot_symbol)s,
                %(future_contract)s, %(order_side)s, %(market_type)s,
                %(trade_direction)s, %(leverage)s, %(status)s, %(channel)s,
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                %(open_coverage)s, %(open_vwap_basis_bps)s, %(risk_relief_bps)s,
                %(open_marginal_basis_bps)s, %(funding_rate_24h)s,
                %(liquidity_role)s, %(fee_rate)s, %(fee_amount)s, %(fee_amount_usdt)s, %(fee_asset)s, %(exchange_order_id)s, %(executed_at)s
            )
        """

        for market_key in ['spot_order', 'future_order']:
            order = order_group[market_key].copy()
            order['position_id'] = position_id
            order['channel'] = self.executor_client.channel
            order['leverage'] = self._order_leg_leverage(market_key, pos)
            # 平仓订单无开仓风控指标，置 None
            order['open_coverage'] = None
            order['open_vwap_basis_bps'] = None
            order['risk_relief_bps'] = None
            order['open_marginal_basis_bps'] = None
            order['funding_rate_24h'] = pos.get('funding_rate_24h')

            exec_data = exec_result.get(market_key) or {}
            leg_executed = bool(
                exec_data
                and exec_data.get('exec_price') is not None
                and exec_data.get('exec_qty') is not None
            )
            if exec_result.get('success') or leg_executed:
                # 成功时写入平仓触发原因，供前端复盘查看
                if exec_result.get('success'):
                    order['reject_reason'] = close_reason_detail
                else:
                    reject_msg = exec_result.get('message', '')
                    order['reject_reason'] = f"{close_reason_detail} | 部分成交: {reject_msg}"
                order['status'] = 'executed'
                order['exec_price'] = exec_data.get('exec_price')
                order['exec_qty'] = exec_data.get('exec_qty')
                order['exec_amount'] = exec_data.get('exec_amount')
                order['coverage_ratio'] = exec_data.get('coverage_ratio')
                order.update(build_order_execution_fields(
                    market_key,
                    order,
                    exec_data,
                    exec_result,
                    spot_open_fee=self.fee_spot_open,
                    spot_close_fee=self.fee_spot_close,
                    future_open_fee=self.fee_future_open,
                    future_close_fee=self.fee_future_close,
                    future_taker_open_fee=self.fee_future_taker_open,
                    future_taker_close_fee=self.fee_future_taker_close,
                ))
                order['executed_at'] = datetime.now()
            else:
                # 失败时写入拒单原因：触发原因 + 执行器拒绝消息
                reject_msg = exec_result.get('message', '')
                order['reject_reason'] = f"{close_reason_detail} | 拒单: {reject_msg}"
                order['status'] = 'rejected'
                order['exec_price'] = None
                order['exec_qty'] = None
                order['exec_amount'] = None
                order['coverage_ratio'] = None
                order['liquidity_role'] = None
                order['fee_rate'] = None
                order['fee_amount'] = None
                order['fee_amount_usdt'] = None
                order['fee_asset'] = None
                order['exchange_order_id'] = None
                order['executed_at'] = None

            with db_manager.get_cursor() as cursor:
                cursor.execute(insert_sql, order)

        if self._mark_partial_risk_close_desync(pos, exec_result, close_reason, close_reason_detail):
            self._trigger_reconciliation('risk_close_partial_desync', str(pos.get('base_asset') or ''))

        partial_state = self._close_partial_fill_state(pos, order_group, exec_result)
        if partial_state.get('partial'):
            if partial_state.get('balanced'):
                self._keep_partial_close_remainder(pos, partial_state, position_close_reason)
                self._trigger_reconciliation('close_partial_fill', str(pos.get('base_asset') or ''))
            else:
                self._mark_unbalanced_partial_close_desync(pos, partial_state, close_reason_detail)
                self._trigger_reconciliation('close_partial_desync', str(pos.get('base_asset') or ''))
            return close_spread_bps

        # ── 更新持仓状态为 closed（仅成交成功时）──
        if exec_result.get('success'):
            update_sql = """
                UPDATE mi_trade_position SET
                    status            = 'closed',
                    closed_at         = %(closed_at)s,
                    close_reason      = %(close_reason)s,
                    spot_close_price  = %(spot_close_price)s,
                    future_close_price= %(future_close_price)s,
                    spot_close_amount = %(spot_close_amount)s,
                    future_close_amount = %(future_close_amount)s,
                    close_spread_bps  = %(close_spread_bps)s
                WHERE id = %(position_id)s
            """
            with db_manager.get_cursor() as cursor:
                cursor.execute(update_sql, {
                    'closed_at':            datetime.now(),
                    'close_reason':         position_close_reason,
                    'spot_close_price':     spot_close_price,
                    'future_close_price':   future_close_price,
                    'spot_close_amount':    spot_close_amount,
                    'future_close_amount':  future_close_amount,
                    'close_spread_bps':     close_spread_bps,
                    'position_id':          position_id,
                })
            logger.info(
                f"持仓状态更新为 closed | position_id={position_id} | "
                f"close_spread_bps={close_spread_bps}"
            )
        return close_spread_bps

    def _close_partial_fill_state(self, pos: Dict, order_group: Dict, exec_result: Dict) -> Dict:
        if not exec_result.get('success'):
            return {'partial': False}

        spot_target = _float_or_none((order_group.get('spot_order') or {}).get('target_qty')) or 0.0
        future_target = _float_or_none((order_group.get('future_order') or {}).get('target_qty')) or 0.0
        spot_exec = _float_or_none((exec_result.get('spot_order') or {}).get('exec_qty')) or 0.0
        future_exec = _float_or_none((exec_result.get('future_order') or {}).get('exec_qty')) or 0.0
        if spot_target <= CLOSE_QTY_TOLERANCE or future_target <= CLOSE_QTY_TOLERANCE:
            return {'partial': False}

        spot_shortfall = max(0.0, spot_target - spot_exec)
        future_shortfall = max(0.0, future_target - future_exec)
        partial = spot_shortfall > CLOSE_QTY_TOLERANCE or future_shortfall > CLOSE_QTY_TOLERANCE
        if not partial:
            return {'partial': False}

        remaining_contracts = self._remaining_future_contracts(pos, future_target, future_shortfall)
        balanced = (
            spot_exec > CLOSE_QTY_TOLERANCE
            and future_exec > CLOSE_QTY_TOLERANCE
            and abs(spot_shortfall - future_shortfall) <= max(CLOSE_QTY_TOLERANCE, min(spot_target, future_target) * 1e-8)
        )
        return {
            'partial': True,
            'balanced': balanced,
            'spot_target': spot_target,
            'future_target': future_target,
            'spot_exec': spot_exec,
            'future_exec': future_exec,
            'spot_remaining': spot_shortfall,
            'future_remaining': future_shortfall,
            'future_contracts_remaining': remaining_contracts,
        }

    def _remaining_future_contracts(self, pos: Dict, future_target: float, future_remaining: float) -> float:
        open_contracts = abs(_float_or_none(pos.get('future_open_contracts')) or future_target)
        if future_target <= CLOSE_QTY_TOLERANCE:
            return max(0.0, future_remaining)
        contracts = open_contracts * max(0.0, future_remaining) / future_target
        rounded = round(contracts)
        return float(rounded) if abs(contracts - rounded) <= 1e-8 else contracts

    def _keep_partial_close_remainder(self, pos: Dict, state: Dict, close_reason: str) -> None:
        spot_remaining = max(0.0, float(state.get('spot_remaining') or 0))
        future_remaining = max(0.0, float(state.get('future_remaining') or 0))
        future_contracts_remaining = max(0.0, float(state.get('future_contracts_remaining') or 0))
        spot_open_price = _float_or_none(pos.get('spot_open_price')) or 0.0
        future_open_price = _float_or_none(pos.get('future_open_price')) or 0.0
        partial_note = (
            f"部分平仓保留剩余|spot={state.get('spot_exec'):g}/{state.get('spot_target'):g}|"
            f"future={state.get('future_exec'):g}/{state.get('future_target'):g}|"
            f"remaining={spot_remaining:g}"
        )
        update_sql = """
            UPDATE mi_trade_position SET
                status = 'holding',
                spot_open_qty = %(spot_open_qty)s,
                spot_open_amount = %(spot_open_amount)s,
                future_open_qty = %(future_open_qty)s,
                future_open_contracts = %(future_open_contracts)s,
                close_reason = %(close_reason)s
            WHERE id = %(position_id)s
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(update_sql, {
                'spot_open_qty': spot_remaining,
                'spot_open_amount': spot_remaining * spot_open_price,
                'future_open_qty': future_remaining,
                'future_open_contracts': future_contracts_remaining,
                'close_reason': f"{close_reason}|{partial_note}",
                'position_id': pos.get('id'),
            })
        logger.warning(
            "平仓部分成交，保留剩余持仓 | position_id=%s | %s",
            pos.get('id'), partial_note,
        )

    def _mark_unbalanced_partial_close_desync(self, pos: Dict, state: Dict, close_reason_detail: str) -> bool:
        detail = (
            f"普通平仓部分成交且两腿不一致|asset={pos.get('base_asset')}|"
            f"spot={state.get('spot_exec'):g}/{state.get('spot_target'):g}|"
            f"future={state.get('future_exec'):g}/{state.get('future_target'):g}"
        )
        sql = """
            UPDATE mi_trade_position
            SET exchange_risk_status = 'desynced',
                exchange_risk_type = 'qty_mismatch',
                exchange_risk_at = %(risk_at)s,
                exchange_risk_detail = %(detail)s,
                close_reason = CASE
                    WHEN close_reason IS NULL OR close_reason = '' THEN %(reason)s
                    WHEN close_reason NOT LIKE %(reason_like)s THEN CONCAT(close_reason, '|', %(reason)s)
                    ELSE close_reason
                END
            WHERE id = %(position_id)s
              AND status = 'holding'
        """
        reason = f"交易所仓位风险:qty_mismatch|{detail}|{close_reason_detail}"
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'risk_at': datetime.now(),
                'detail': detail,
                'reason': reason,
                'reason_like': '%普通平仓部分成交且两腿不一致%',
                'position_id': pos.get('id'),
            })
            updated = int(cursor.rowcount or 0)
        if updated:
            logger.warning("普通平仓部分成交断腿标记 | position_id=%s | %s", pos.get('id'), detail)
        return bool(updated)

    def _mark_partial_risk_close_desync(
        self,
        pos: Dict,
        exec_result: Dict,
        close_reason: str,
        close_reason_detail: str,
    ) -> bool:
        """风险平仓 Gate 腿已成交但 spot 未成交时，标记断腿并交给对账兜底。"""
        if close_reason not in FAST_RISK_CLOSE_REASONS or exec_result.get('success'):
            return False
        future_exec = exec_result.get('future_order') or {}
        spot_exec = exec_result.get('spot_order') or {}
        if not future_exec or spot_exec:
            return False
        future_price = _float_or_none(future_exec.get('exec_price'))
        future_qty = _float_or_none(future_exec.get('exec_qty'))
        if future_price is None or future_qty is None or future_qty <= 0:
            return False

        base_asset = str(pos.get('base_asset') or '').upper()
        position_id = pos.get('id')
        open_qty = abs(float(pos.get('future_open_qty') or 0))
        risk_type = 'missing_gate_position'
        if open_qty > 0 and abs(future_qty) + 1e-9 < open_qty:
            risk_type = 'qty_mismatch'
        message = str(exec_result.get('message') or '')
        exchange_order_id = future_exec.get('exchange_order_id')
        detail = (
            f"系统风险平仓Gate期货已成交但Binance现货失败|asset={base_asset}|"
            f"future_price={future_price}|future_qty={future_qty}|"
            f"future_order_id={exchange_order_id or ''}|spot_reason={message[:180]}"
        )
        reason = f"交易所仓位风险:{risk_type}|{detail}"
        sql = """
            UPDATE mi_trade_position
            SET exchange_risk_status = 'desynced',
                exchange_risk_type = %(risk_type)s,
                exchange_risk_at = %(risk_at)s,
                exchange_risk_detail = %(detail)s,
                close_reason = CASE
                    WHEN close_reason IS NULL OR close_reason = '' THEN %(reason)s
                    WHEN close_reason NOT LIKE %(reason_like)s THEN CONCAT(close_reason, '|', %(reason)s)
                    ELSE close_reason
                END
            WHERE id = %(position_id)s
              AND status = 'holding'
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'risk_type': risk_type,
                'risk_at': datetime.now(),
                'detail': detail,
                'reason': reason,
                'reason_like': '%系统风险平仓Gate期货已成交但Binance现货失败%',
                'position_id': position_id,
            })
            updated = int(getattr(cursor, 'rowcount', 0) or 0)

        logger.critical(
            "风险平仓出现待处置断腿 | %s | position_id=%s | risk_type=%s | "
            "future_price=%s | future_qty=%s | spot_reason=%s",
            base_asset, position_id, risk_type, future_price, future_qty, message,
        )
        return updated > 0

    def _trigger_reconciliation(self, reason: str, base_asset: str):
        callback = self._reconciliation_trigger
        if not callback:
            return
        try:
            callback(reason, base_asset)
        except Exception as e:
            logger.warning(
                "风险平仓后触发即时对账失败 | %s | reason=%s | %s",
                base_asset, reason, e, exc_info=True,
            )

    @staticmethod
    def _position_close_reason(close_reason_detail: str) -> str:
        """Position 表只保存可展示摘要；完整执行审计仍保留在 mi_trade_order.reject_reason。"""
        max_len = 60000
        text = str(close_reason_detail or '')
        if len(text) <= max_len:
            return text
        suffix = '|...(完整执行审计见mi_trade_order.reject_reason)'
        return text[: max_len - len(suffix)] + suffix

    def _order_leg_leverage(self, market_key: str, pos: Dict) -> float:
        if market_key == 'future_order':
            return GATE_CROSS_MARGIN_LEVERAGE
        return 1.0
