# coding: utf-8
"""
平仓执行器模块
- ClosingExecutor: 平仓条件检查 + 平仓订单生成 + 持久化
- 成交引擎通过 ExecutorClient (HTTP) 调用独立的执行器服务（虚拟/实盘），实现虚实分离

平仓触发条件（按优先级）：
  0. 保证金爆仓风控（保证金/维持保证金 < 阈值）
  1. 当前负24h资金费率临近结算/极端恶化，或历史实际负资金费成本超阈值
  2. 资金费次数 >= max_funding_payments
  3. 固定净收益止盈（下单前有最终风控旁路复核）
"""
import time
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Set

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

logger = get_logger(__name__)


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
            'trade.close.negative_funding_exit_current_window_min', 30.0
        )
        self.negative_funding_exit_extreme_24h_bps = config.get_float(
            'trade.close.negative_funding_exit_extreme_24h_bps', 45.0
        )
        self.negative_funding_exit_paid_bps = config.get_float(
            'trade.close.negative_funding_exit_paid_bps', 7.0
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
        self.margin_leverage = max(config.get_float('margin.leverage', 2.0), 1.0)
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
        self._margin_topup_attempt_cooldown: Dict[int, datetime] = {}

        # 保证金风控配置
        self.margin_close_threshold_pct = config.get_float('margin.close_threshold_pct', 5.0)
        self.margin_topup_pct = config.get_float('margin.topup_pct', 12.0)
        self.margin_topup_enabled = config.get_bool('margin.topup.enabled', True)
        self.margin_topup_target_rate_pct = config.get_float(
            'margin.topup.target_maintenance_margin_rate_pct',
            3000.0,
        )
        self.margin_topup_max_count = config.get_int('margin.topup.max_topup_per_position', 3)
        self.margin_topup_max_ratio = config.get_float('margin.topup.max_topup_ratio', 0.0)
        self.margin_topup_min_gate_available = config.get_float('margin.topup.min_gate_available', 50.0)
        self.margin_topup_cooldown_sec = config.get_int('margin.topup.cooldown_sec', 300)
        self.margin_topup_min_amount = config.get_float('margin.topup.min_amount_usdt', 0.1)
        self.margin_topup_snapshot_max_age_sec = config.get_int('margin.topup.snapshot_max_age_sec', 180)
        self.margin_topup_balance_tolerance_pct = config.get_float(
            'margin.topup.hedge_balance_tolerance_pct', 5.0
        )

        # 最终风控旁路：旁路风控新鲜度硬约束（以本地 last_update_time 为准计算 lag_ms，超过阈值拒平）
        self._max_orderbook_lag_ms = config.get_float('trade.close.max_orderbook_lag_ms', 200.0)
        # 临时槽位：旁路风控读取到的 (gate_lag_ms, spot_lag_ms)，供平仓原因拼接
        self._last_orderbook_lag_ms: Dict[str, tuple] = {}
        # OrderBookManager 引用（由外部注入）
        self._gate_manager = None
        self._spot_manager = None

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

    def set_orderbook_managers(self, gate_manager, spot_manager):
        """
        注入 OrderBookManager 引用，供平仓最终风控旁路直接读取单标的盘口。
    
        Args:
            gate_manager: Gate 期货 OrderBookManager 实例
            spot_manager: Binance 现货 OrderBookManager 实例
        """
        self._gate_manager = gate_manager
        self._spot_manager = spot_manager
        logger.info('OrderBookManager 已注入 ClosingExecutor（平仓最终风控旁路就绪）')

    # ──────────────────────────────────────────────────────────────────
    # 公共入口
    # ──────────────────────────────────────────────────────────────────

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
        topup_contracts_this_run: Set[str] = set()

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

            topup_result = self._check_and_topup_margin(pos, topup_contracts_this_run)
            if topup_result:
                results.append(topup_result)
                if topup_result.get('success'):
                    continue

            # 优先级 0：保证金爆仓风控（最高优先级，保证金/维持保证金低于阈值时立即平仓）
            if self._check_margin_liquidation(pos):
                margin_rate = self._maintenance_margin_rate(pos)
                close_reason = 'margin_close'
                close_reason_detail = (
                    f"保证金风控|保证金/维持保证金{margin_rate:.2f}%"
                    f"(<{self.margin_close_threshold_pct}%)"
                )
                if pos.get('gate_position_margin') is not None:
                    close_reason_detail += f"|仓位保证金{float(pos.get('gate_position_margin') or 0):.4f}"
                if pos.get('gate_maintenance_margin') is not None:
                    close_reason_detail += f"|维持保证金{float(pos.get('gate_maintenance_margin') or 0):.4f}"
                # 紧急平仓，清除谷底状态
                self._clear_position_close_state(ba, pos)
            elif self._check_negative_funding_exit(pos):
                close_reason = 'negative_funding_exit'
                close_reason_detail = self._build_negative_funding_exit_detail(pos)
                self._clear_position_close_state(ba, pos)
            elif self._check_funding_count(pos):
                close_reason = 'funding_count'
                count = int(pos.get('funding_payments_count') or 0)
                close_reason_detail = (
                    f"资金费次数|{count}次/{self.max_funding_payments}次"
                    f"(~{count // 3}天)"
                )
                # 强制平仓，清除谷底状态
                self._clear_position_close_state(ba, pos)
            elif self._check_take_profit(pos, current_spread_bps):
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

            # ── 最终风控旁路：止盈复核盈利性；风险平仓复核新鲜度/同步/深度 ──
            guarded_reasons = {'take_profit', 'negative_funding_exit', 'funding_count'}
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
                logger.warning(f"平仓条件触发但无盘口数据: {ba} | reason={close_reason}")
                continue
            if self.protective_ioc_enabled and (
                not self.future_maker_close_enabled or close_reason in {'margin_close', 'manual'}
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

    def _valley_key(self, base_asset: str, pos: Optional[Dict] = None):
        """止盈谷底状态按持仓隔离；无 position_id 时兼容旧测试/降级为 base_asset。"""
        if pos:
            position_id = pos.get('id') or pos.get('position_id')
            if position_id:
                return int(position_id)
        return base_asset

    def _clear_position_close_state(self, base_asset: str, pos: Optional[Dict] = None) -> None:
        self._valley_state.pop(self._valley_key(base_asset, pos), None)
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

    def _should_hold_for_positive_funding(self, pos: Dict, close_basis_bps: float) -> bool:
        if not self.positive_funding_hold_enabled:
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
        """负 funding 风险触发：已付成本立即；当前负费率临近结算或极端恶化才触发。"""
        if not self.negative_funding_exit_enabled:
            return False
        current_threshold = abs(float(self.negative_funding_exit_current_24h_bps or 0.0))
        current_window_min = abs(float(self.negative_funding_exit_current_window_min or 0.0))
        extreme_threshold = abs(float(self.negative_funding_exit_extreme_24h_bps or 0.0))
        paid_threshold = abs(float(self.negative_funding_exit_paid_bps or 0.0))
        current_neg = self._negative_current_24h_bps(pos)
        paid_neg = self._negative_paid_bps(pos)
        if paid_threshold > 0 and paid_neg >= paid_threshold:
            return True
        if extreme_threshold > 0 and current_neg >= extreme_threshold:
            return True
        next_min = self._time_to_next_funding_min(pos)
        near_next_funding = next_min is not None and 0 <= next_min <= current_window_min
        return current_threshold > 0 and current_neg >= current_threshold and near_next_funding

    def _check_margin_liquidation(self, pos: Dict) -> bool:
        """
        保证金爆仓风控（优先级 0）：
        当 Gate 保证金/维持保证金低于平仓阈值时触发两端同时平仓。
        """
        margin_rate = self._maintenance_margin_rate(pos)
        if margin_rate is None:
            return False
        return margin_rate < self.margin_close_threshold_pct

    def _maintenance_margin_rate(self, pos: Dict) -> Optional[float]:
        """Gate 聚合仓位口径：仓位保证金 / 维持保证金 * 100，越高越安全。"""
        value = pos.get('gate_maintenance_margin_rate')
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
        margin = _float_or_none(pos.get('gate_position_margin'))
        maintenance = _float_or_none(pos.get('gate_maintenance_margin'))
        if margin is None or maintenance is None or maintenance <= 0:
            return None
        return margin / maintenance * 100

    def _check_and_topup_margin(
        self,
        pos: Dict,
        topup_contracts_this_run: Optional[Set[str]] = None,
    ) -> Optional[Dict]:
        """在紧急平仓前尝试自动追加 Gate 逐仓保证金。"""
        if not self.margin_topup_enabled:
            return None

        margin_rate = self._maintenance_margin_rate(pos)
        if margin_rate is None:
            return None
        if margin_rate >= self.margin_topup_pct:
            return None

        ba = pos.get('base_asset', '')
        position_id = int(pos.get('id') or 0)
        contract = pos.get('future_contract') or f"{ba}_USDT"
        if topup_contracts_this_run is not None and contract in topup_contracts_this_run:
            return None
        if self._in_margin_topup_cooldown(position_id):
            return None

        hedge_balanced = self._is_hedge_balanced(pos)
        if not hedge_balanced:
            message = '现货/期货数量不平衡，跳过自动追保'
            self._insert_margin_topup_log(pos, 0, 0, 0, margin_rate, None, None, False, False, message)
            self._set_margin_topup_cooldown(position_id)
            logger.warning(f"自动追保跳过 | {ba} | position_id={position_id} | {message}")
            return None

        topup_count = int(pos.get('margin_topup_count') or 0)
        if topup_count >= self.margin_topup_max_count:
            return None

        last_at = pos.get('margin_topup_last_at')
        if last_at and isinstance(last_at, str):
            try:
                last_at = datetime.strptime(last_at, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                last_at = None
        if last_at and (datetime.now() - last_at).total_seconds() < self.margin_topup_cooldown_sec:
            return None

        calc = self._calculate_margin_topup_amount(pos)
        if not calc:
            return None
        topup_amount = calc['topup_amount']
        if topup_amount < self.margin_topup_min_amount:
            return None

        initial_margin = calc['initial_margin']
        topup_total = float(pos.get('margin_topup_total') or 0)
        max_total = initial_margin * self.margin_topup_max_ratio
        if max_total > 0 and topup_total + topup_amount > max_total:
            topup_amount = max(0.0, max_total - topup_total)
            if topup_amount < self.margin_topup_min_amount:
                return None

        gate_available = self._get_latest_gate_available()
        if gate_available is None:
            message = '无有效Gate资金快照，跳过自动追保'
            self._insert_margin_topup_log(
                pos, topup_amount, calc['target_margin'], calc['margin_before'],
                margin_rate, calc['margin_rate_after'], None, True, False, message
            )
            self._set_margin_topup_cooldown(position_id)
            logger.warning(f"自动追保跳过 | {ba} | position_id={position_id} | {message}")
            if topup_contracts_this_run is not None:
                topup_contracts_this_run.add(contract)
            return None
        if gate_available - topup_amount < self.margin_topup_min_gate_available:
            message = (
                f"Gate可用余额不足: available={gate_available:.4f}, "
                f"topup={topup_amount:.4f}, reserve={self.margin_topup_min_gate_available:.4f}"
            )
            self._insert_margin_topup_log(
                pos, topup_amount, calc['target_margin'], calc['margin_before'],
                margin_rate, calc['margin_rate_after'], gate_available, True, False, message
            )
            self._set_margin_topup_cooldown(position_id)
            logger.warning(f"自动追保跳过 | {ba} | position_id={position_id} | {message}")
            if topup_contracts_this_run is not None:
                topup_contracts_this_run.add(contract)
            return None

        exec_result = self.executor_client.topup_margin(contract, topup_amount, dual_side='short')
        success = bool(exec_result.get('success'))
        message = exec_result.get('message') or ('追保成功' if success else '追保失败')
        self._insert_margin_topup_log(
            pos, topup_amount, calc['target_margin'], calc['margin_before'],
            margin_rate, calc['margin_rate_after'], gate_available, True, success, message
        )
        if topup_contracts_this_run is not None:
            topup_contracts_this_run.add(contract)
        if not success:
            self._set_margin_topup_cooldown(position_id)
            logger.warning(
                f"自动追保失败 | {ba} | position_id={position_id} | "
                f"amount={topup_amount:.6f} | {message}"
            )
            return {'base_asset': ba, 'success': False, 'action': 'margin_topup', 'message': message}

        self._mark_margin_topup_success(position_id, topup_amount)
        logger.info(
            f"自动追保成功 | {ba} | position_id={position_id} | amount={topup_amount:.6f} | "
            f"margin={calc['margin_before']:.6f}->{calc['margin_before'] + topup_amount:.6f} | "
            f"target={calc['target_margin']:.6f} | "
            f"maintenance_rate={margin_rate:.2f}%->{calc['margin_rate_after']:.2f}%"
        )
        return {
            'base_asset': ba,
            'success': True,
            'action': 'margin_topup',
            'topup_amount': round(topup_amount, 6),
            'message': message,
        }

    def _in_margin_topup_cooldown(self, position_id: int) -> bool:
        if not position_id:
            return False
        last_attempt = self._margin_topup_attempt_cooldown.get(position_id)
        if not last_attempt:
            return False
        return (datetime.now() - last_attempt).total_seconds() < self.margin_topup_cooldown_sec

    def _set_margin_topup_cooldown(self, position_id: int):
        if position_id:
            self._margin_topup_attempt_cooldown[position_id] = datetime.now()

    def _calculate_margin_topup_amount(self, pos: Dict) -> Optional[Dict]:
        """按 Gate 聚合仓位口径，计算补到目标 保证金/维持保证金 比例所需金额。"""
        margin_before = _float_or_none(pos.get('gate_position_margin'))
        maintenance_margin = _float_or_none(pos.get('gate_maintenance_margin'))
        if margin_before is None or maintenance_margin is None or maintenance_margin <= 0:
            return None

        target_margin = maintenance_margin * max(self.margin_topup_target_rate_pct, 0) / 100
        topup_amount = max(0.0, target_margin - margin_before)
        margin_rate_after = (margin_before + topup_amount) / maintenance_margin * 100
        return {
            'initial_margin': margin_before,
            'margin_before': margin_before,
            'target_margin': target_margin,
            'topup_amount': topup_amount,
            'margin_rate_after': margin_rate_after,
        }

    def _is_hedge_balanced(self, pos: Dict) -> bool:
        """追保前确认现货腿和期货腿仍大致平衡；追保不改变对冲数量。"""
        spot_qty = float(pos.get('spot_open_qty') or 0)
        future_qty = float(pos.get('future_open_qty') or 0)
        if spot_qty <= 0 or future_qty <= 0:
            return False
        tolerance = max(self.margin_topup_balance_tolerance_pct, 0) / 100
        return abs(spot_qty - future_qty) / max(spot_qty, future_qty) <= tolerance

    def _get_maintenance_rate(self, base_asset) -> float:
        meta = self.contract_meta.get(str(base_asset or '').upper(), {})
        return float(meta.get('maintenance_rate') or config.get_float('margin.default_maintenance_rate', 0.005))

    def _get_latest_gate_available(self) -> Optional[float]:
        sql = """
            SELECT available_usdt, snapshot_at
            FROM mi_capital_snapshot
            WHERE exchange = 'gate'
            ORDER BY snapshot_at DESC
            LIMIT 1
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
        if not row:
            return None
        snapshot_at = row.get('snapshot_at')
        if snapshot_at and isinstance(snapshot_at, str):
            try:
                snapshot_at = datetime.strptime(snapshot_at, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                snapshot_at = None
        if snapshot_at and (datetime.now() - snapshot_at).total_seconds() > self.margin_topup_snapshot_max_age_sec:
            return None
        return float(row.get('available_usdt') or 0)

    def _mark_margin_topup_success(self, position_id: int, amount: float):
        sql = """
            UPDATE mi_trade_position SET
                margin_topup_count = margin_topup_count + 1,
                margin_topup_total = margin_topup_total + %s,
                margin_topup_last_at = NOW()
            WHERE id = %s AND status = 'holding'
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (amount, position_id))

    def _insert_margin_topup_log(
        self, pos: Dict, amount: float, target_margin: float, margin_before: float,
        liq_before: Optional[float], liq_after: Optional[float], gate_available: Optional[float],
        hedge_balanced: bool, success: bool, message: str
    ):
        sql = """
            INSERT INTO mi_margin_topup_log (
                base_asset, position_id, contract, topup_amount, target_margin_usdt,
                margin_before_usdt, liq_distance_before, liq_distance_after,
                gate_available_before, hedge_balanced, success, error_msg
            ) VALUES (
                %(base_asset)s, %(position_id)s, %(contract)s, %(topup_amount)s, %(target_margin)s,
                %(margin_before)s, %(liq_before)s, %(liq_after)s,
                %(gate_available)s, %(hedge_balanced)s, %(success)s, %(error_msg)s
            )
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'base_asset': pos.get('base_asset'),
                'position_id': pos.get('id'),
                'contract': pos.get('future_contract') or f"{pos.get('base_asset')}_USDT",
                'topup_amount': round(float(amount or 0), 6),
                'target_margin': round(float(target_margin or 0), 6),
                'margin_before': round(float(margin_before or 0), 6),
                'liq_before': liq_before,
                'liq_after': round(float(liq_after), 2) if liq_after is not None else None,
                'gate_available': gate_available,
                'hedge_balanced': 1 if hedge_balanced else 0,
                'success': 1 if success else 0,
                'error_msg': (message or '')[:200],
            })

    def _check_funding_count(self, pos: Dict) -> bool:
        """检查资金费次数是否达到上限"""
        count = int(pos.get('funding_payments_count') or 0)
        return count >= self.max_funding_payments

    def _check_take_profit(self, pos: Dict, current_spread_bps: float) -> bool:
        """
        止盈条件：
            fixed_net_bps:
                基差收敛 + 已收资金费 - 全部手续费 >= fixed_take_profit_bps
            legacy_p40:
                总盈亏bps(基差收敛 + 已收资金费) >= percentile_40费率(bps) * multiplier + 手续费(bps)
        """
        ba = pos.get('base_asset', '')
        if self.take_profit_mode == 'fixed_net_bps':
            comps = self._profit_components(pos, current_spread_bps)
            if comps['net_profit_bps'] < self.fixed_take_profit_bps:
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
                comps = self._profit_components(pos, gate_basis_bps)
                if comps['net_profit_bps'] < self.fixed_take_profit_bps:
                    logger.info(
                        f"平仓旁路-固定净止盈不足拦截 | {base_asset} | "
                        f"net={comps['net_profit_bps']:.2f}bps < {self.fixed_take_profit_bps:.2f}bps | "
                        f"gate_basis={gate_basis_bps:.2f}bps"
                    )
                    return False, row, gate_basis_bps, (
                        f'固定净止盈不足(净{comps["net_profit_bps"]:.1f}bps < '
                        f'{self.fixed_take_profit_bps:.1f}bps)'
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
            comps = self._profit_components(pos, current_spread_bps)
            detail = (
                f"固定净止盈|净{comps['net_profit_bps']:.1f}bps"
                f"(收敛{comps['spread_profit_bps']:.1f}+资金费{comps['funding_earned_bps']:.1f}"
                f"-{comps['fee_full_bps']:.0f}费)"
                f">={self.fixed_take_profit_bps:.1f}bps"
                f"|{self._funding_context_text(pos)}"
            )
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
        current_neg = self._negative_current_24h_bps(pos)
        paid_neg = self._negative_paid_bps(pos)
        current_threshold = abs(float(self.negative_funding_exit_current_24h_bps or 0.0))
        current_window_min = abs(float(self.negative_funding_exit_current_window_min or 0.0))
        extreme_threshold = abs(float(self.negative_funding_exit_extreme_24h_bps or 0.0))
        paid_threshold = abs(float(self.negative_funding_exit_paid_bps or 0.0))
        next_bps = self._next_funding_bps(pos)
        next_min = self._time_to_next_funding_min(pos)
        next_text = 'NA' if next_bps is None else f'{next_bps:+.1f}bps'
        next_min_text = 'NA' if next_min is None else f'{next_min:.1f}min'
        return (
            f"负资金费风险|当前24h负费{current_neg:.1f}bps"
            f"|已付负费{paid_neg:.1f}bps"
            f"|阈值(当前{current_threshold:.1f}@{current_window_min:.0f}min,"
            f"极端{extreme_threshold:.1f},已付{paid_threshold:.1f})"
            f"|next={next_text}|距结算{next_min_text}"
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
        future_protective_price = None
        if self.protective_ioc_enabled:
            future_protective_price = self._future_close_protective_price(
                orderbook_row,
                self.protective_ioc_risk_slippage_bps,
            )
            if future_protective_price is not None:
                close_reason_detail = (
                    f"{close_reason_detail}|保护IOC(future_buy≤{future_protective_price})"
                )
        return self._execute_close(
            pos,
            close_reason,
            close_reason_detail,
            orderbook_row,
            future_protective_price=future_protective_price,
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
        self._save_close(
            pos,
            order_group,
            exec_result,
            close_reason,
            close_reason_detail,
            pre_gate_basis_bps=pre_gate_basis_bps,
        )
        return {
            'base_asset': ba,
            'success': exec_result['success'],
            'order_uuid': order_group['order_uuid'],
            'close_reason': close_reason,
            'message': exec_result.get('message'),
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
        if future_protective_price is not None:
            future_order['protective_price'] = future_protective_price
        if close_reason not in {'margin_close', 'manual'}:
            self._apply_future_maker_close(pos, orderbook_row or {}, future_order, close_reason)

        return {
            'order_uuid': order_uuid,
            'base_asset': ba,
            'spot_symbol': spot_symbol,
            'future_contract': future_contract,
            'spot_order': spot_order,
            'future_order': future_order,
        }

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

        fallback_price = None
        if (
            self.future_maker_close_fallback_ioc_enabled
            and tier in self.future_maker_close_fallback_allowed_tiers
        ):
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
            'maker_fallback_ioc_enabled': fallback_price is not None,
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
    ):
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

            if exec_result.get('success'):
                # 成功时写入平仓触发原因，供前端复盘查看
                order['reject_reason'] = close_reason_detail
                exec_data = exec_result[market_key] or {}
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
                    'close_reason':         close_reason_detail,
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

    def _position_future_leverage(self, pos: Dict) -> float:
        leverage = _float_or_none(pos.get('future_open_leverage'))
        if (leverage is None or leverage <= 0) and pos.get('id') is not None:
            leverage = self._load_position_future_open_leverage(int(pos.get('id')))
        if leverage is None or leverage <= 0:
            leverage = self.margin_leverage
        return max(float(leverage or 1.0), 1.0)

    def _load_position_future_open_leverage(self, position_id: int) -> Optional[float]:
        sql = """
            SELECT leverage
            FROM mi_trade_order
            WHERE position_id = %s
              AND order_side = 'open'
              AND market_type = 'future'
              AND status = 'executed'
            ORDER BY id ASC
            LIMIT 1
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (position_id,))
            row = cursor.fetchone()
        return _float_or_none(row.get('leverage')) if row else None

    def _order_leg_leverage(self, market_key: str, pos: Dict) -> float:
        if market_key == 'future_order':
            return self._position_future_leverage(pos)
        return 1.0
