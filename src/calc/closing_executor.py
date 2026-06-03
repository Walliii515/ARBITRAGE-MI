# coding: utf-8
"""
平仓执行器模块
- ClosingExecutor: 平仓条件检查 + 平仓订单生成 + 持久化
- 成交引擎通过 ExecutorClient (HTTP) 调用独立的执行器服务（虚拟/实盘），实现虚实分离

平仓触发条件（按优先级）：
  0. 保证金爆仓风控（距爆仓距离 < 阈值）
  1. 资金费次数 >= max_funding_payments
  2. 止盈: 总盈亏bps(基差收敛 + 已收资金费) >= 24h资金费率bps * multiplier + 手续费bps
     (止盈下单前有最终风控旁路，确认仍满足止盈)
  3. 累计资金费超阈值: 费率为负时，累计支出bps >= max(open_p20 - close_p20, 15) × 0.5
"""
import time
import uuid
from datetime import datetime
from typing import List, Dict

from common.database import db_manager
from common.config import config
from common.logger import get_logger
from calc.executor_client import ExecutorClient
from calc.orderbook_enricher import calc_vwap_basis_bps, calc_full_fee_bps

logger = get_logger(__name__)


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

        self.take_profit_multiplier = config.get_float('trade.close.take_profit_days_multiplier', 6.0)
        self.max_funding_payments = config.get_int('trade.close.max_funding_payments', 30)

        # 手续费率（用于止盈阈值计算）
        self.fee_spot_open = config.get_float('trade.fee.spot_open', 0.00075)
        self.fee_spot_close = config.get_float('trade.fee.spot_close', 0.00075)
        self.fee_future_open = config.get_float('trade.fee.future_open', 0.00075)
        self.fee_future_close = config.get_float('trade.fee.future_close', 0.00075)
        # 全部手续费 BPS（正数，用于止盈阈值累加）
        self.fee_full_bps = -calc_full_fee_bps(
            self.fee_spot_open, self.fee_spot_close,
            self.fee_future_open, self.fee_future_close
        )

        # 谷底反弹止盈策略
        self.valley_rebound_pct = config.get_float('trade.valley_rebound.rebound_pct', 0.10)
        self.valley_monitor_timeout_sec = config.get_int('trade.valley_rebound.monitor_timeout_sec', 60)
        self._valley_state: Dict[str, Dict] = {}  # base_asset -> {valley_bps, start_time, open_spread_bps}

        # 平仓失败冷却机制
        self.close_cooldown_sec = config.get_int('trade.close.cooldown_sec', 60)
        self._close_cooldown: Dict[str, datetime] = {}  # base_asset -> 上次失败时间

        # 累计资金费支出阈值配置
        self.funding_cost_tolerance_ratio = config.get_float('trade.close.funding_cost_tolerance_ratio', 0.5)
        self.funding_cost_min_threshold_bps = config.get_float('trade.close.funding_cost_min_threshold_bps', 15.0)

        # 保证金风控配置
        self.margin_close_threshold_pct = config.get_float('margin.close_threshold_pct', 5.0)

        # 最终风控旁路：旁路风控新鲜度硬约束（以本地 last_update_time 为准计算 lag_ms，超过阈值拒平）
        self._max_orderbook_lag_ms = config.get_float('trade.close.max_orderbook_lag_ms', 200.0)
        # 盘口健康度门禁：update_count 闸阈值 ─ 动态 = sustain_sec × 2（与开仓侧阈值一致）
        # 唯一计算点：_check_update_count_freshness / _pass_valley_check / 日志描述均复用该成员
        sustain_sec = config.get_float('trade.peak_pullback.sustain_sec', 3.0)
        self.sustain_sec = sustain_sec
        self.min_update_count = max(1, int(sustain_sec * 2))
        # 临时槽位：旁路风控读取到的 (gate_lag_ms, spot_lag_ms)，供平仓原因拼接
        self._last_orderbook_lag_ms: Dict[str, tuple] = {}
        # OrderBookManager 引用（由外部注入）
        self._gate_manager = None
        self._spot_manager = None

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

    def _get_orderbook_update_counts(self, contract: str, symbol: str) -> tuple:
        """从 OrderBookManager 读取 gate / spot 盘口的 update_count。任一缺失时返回 None。"""
        gate_count = None
        spot_count = None
        try:
            if self._gate_manager is not None:
                gob = self._gate_manager.get_orderbook(contract)
                if gob is not None:
                    gate_count = int(getattr(gob, 'update_count', 0) or 0)
            if self._spot_manager is not None:
                sob = self._spot_manager.get_orderbook(symbol)
                if sob is not None:
                    spot_count = int(getattr(sob, 'update_count', 0) or 0)
        except Exception as e:
            logger.debug(f'读取 update_count 异常(忽略): {e}')
        return gate_count, spot_count

    def _check_update_count_freshness(
        self,
        gate_uc_now,
        spot_uc_now,
        gate_uc_start,
        spot_uc_start,
    ) -> tuple:
        """“更新次数闸”校验：gate 与 spot 任一侧增量 < self.min_update_count 即拒。依赖状态缺失退化为放行。"""
        threshold = self.min_update_count
        if threshold <= 0:
            return True, ''
        if gate_uc_now is None or spot_uc_now is None:
            return True, ''
        if gate_uc_start is None or spot_uc_start is None:
            return True, ''
        gate_delta = gate_uc_now - gate_uc_start
        spot_delta = spot_uc_now - spot_uc_start
        if gate_delta < threshold or spot_delta < threshold:
            return False, (
                f'盘口呆滞(gate增量={gate_delta}, spot增量={spot_delta}, '
                f'需≥{threshold}=sustain{self.sustain_sec:.0f}s×2)'
            )
        return True, ''

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

        for pos in positions:
            if pos.get('status') != 'holding':
                continue

            ba = pos.get('base_asset', '')
            current_spread_bps = pos.get('current_spread_bps')

            if current_spread_bps is None:
                continue  # 无盘口数据，跳过

            # ── 冷却期检查：平仓失败后 N 秒内不重试 ──
            cooldown_until = self._close_cooldown.get(ba)
            if cooldown_until and (datetime.now() - cooldown_until).total_seconds() < self.close_cooldown_sec:
                continue

            # ── 按优先级检查平仓条件 ──
            close_reason = None
            close_reason_detail = None

            # 优先级 0：保证金爆仓风控（最高优先级，距爆仓距离低于阈值时立即平仓）
            if self._check_margin_liquidation(pos):
                liq_distance = pos.get('liq_distance_pct')
                liq_price = pos.get('liq_price')
                close_reason = 'margin_close'
                close_reason_detail = (
                    f"保证金风控|距爆仓{liq_distance:.2f}%"
                    f"(<{self.margin_close_threshold_pct}%)"
                    f"|爆仓价{liq_price:.4f}"
                    f"|当前{pos.get('current_future_price', 0):.4f}"
                )
                # 紧急平仓，清除谷底状态
                self._valley_state.pop(ba, None)
            elif self._check_funding_count(pos):
                close_reason = 'funding_count'
                count = int(pos.get('funding_payments_count') or 0)
                close_reason_detail = (
                    f"资金费次数|{count}次/{self.max_funding_payments}次"
                    f"(~{count // 3}天)"
                )
                # 强制平仓，清除谷底状态
                self._valley_state.pop(ba, None)
            elif self._check_take_profit(pos, current_spread_bps):
                # 止盈条件满足，进入谷底反弹确认
                if self._pass_valley_check(ba, current_spread_bps, pos):
                    close_reason = 'take_profit'
                    close_reason_detail = self._build_take_profit_detail(pos, current_spread_bps)
                # else: 谷底监控中，不平仓
            else:
                # 止盈不再满足，清除谷底监控状态
                self._valley_state.pop(ba, None)
                threshold_data = close_vwap_threshold_meta.get(ba, {})
                if self._check_funding_cost_exceeded(pos, threshold_data):
                    close_reason = 'funding_cost_exceeded'
                    close_reason_detail = self._build_funding_cost_detail(pos, threshold_data)

            if not close_reason:
                continue

            # ── 最终风控旁路：仅对止盈平仓生效，确认下单前仍满足止盈 ──
            if close_reason == 'take_profit':
                contract = pos.get('future_contract', '')
                symbol = pos.get('spot_symbol') or f"{ba}USDT"
                gate_passed, gate_row, gate_basis, gate_reason = self._pre_execution_gate(
                    ba, contract, symbol, pos
                )
                if not gate_passed:
                    logger.info(
                        f"平仓最终风控旁路拦截 | {ba} | reason={close_reason} | "
                        f"gate_basis={gate_basis}bps | 原因: {gate_reason}"
                    )
                    # 旁路拦截后清除谷底状态，下一轮重新判断
                    self._valley_state.pop(ba, None)
                    continue
                # 使用旁路返回的最新盘口行（确保下单数据 = 校验数据）
                if gate_row is not None:
                    orderbook_rows_by_asset[ba] = gate_row
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

            try:
                result = self._execute_close(pos, close_reason, close_reason_detail, orderbook_row)
                results.append(result)
                if result.get('success'):
                    # 平仓成功，清除谷底监控状态和冷却记录
                    self._valley_state.pop(ba, None)
                    self._close_cooldown.pop(ba, None)
                    logger.info(
                        f"平仓成功 | {ba} | reason={close_reason} | "
                        f"spread_bps={current_spread_bps:.2f}"
                    )
                else:
                    # 平仓失败，进入冷却期
                    self._close_cooldown[ba] = datetime.now()
                    # 超时触发的谷底状态也需清除，避免下次继续超时重试
                    self._valley_state.pop(ba, None)
                    logger.warning(
                        f"平仓失败 | {ba} | reason={close_reason} | "
                        f"msg={result.get('message')} | "
                        f"冷却{self.close_cooldown_sec}s"
                    )
            except Exception as e:
                logger.error(f"平仓执行异常 {ba}: {e}", exc_info=True)
                results.append({'base_asset': ba, 'success': False, 'message': str(e)})

        return results

    # ──────────────────────────────────────────────────────────────────
    # 条件检查
    # ──────────────────────────────────────────────────────────────────

    def _check_margin_liquidation(self, pos: Dict) -> bool:
        """
        保证金爆仓风控（优先级 0）：
        当仓位的距爆仓距离低于平仓阈值时触发两端同时平仓。
        距爆仓距离 <= 0 表示已“模拟爆仓”，同样触发平仓。
        """
        liq_distance = pos.get('liq_distance_pct')
        if liq_distance is None:
            return False
        return liq_distance < self.margin_close_threshold_pct

    def _check_funding_count(self, pos: Dict) -> bool:
        """检查资金费次数是否达到上限"""
        count = int(pos.get('funding_payments_count') or 0)
        return count >= self.max_funding_payments

    def _check_take_profit(self, pos: Dict, current_spread_bps: float) -> bool:
        """
        止盈条件：
            总盈亏bps(基差收敛 + 已收资金费) >= percentile_40费率(bps) * multiplier + 手续费(bps)

        使用历史 percentile_40 资金费率（约为中位数正费率）作为基准，
        避免当前费率为负时止盈失效。
        """
        ba = pos.get('base_asset', '')

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

    def _check_funding_cost_exceeded(self, pos: Dict, threshold_data: Dict) -> bool:
        """
        累计资金费支出超阈值：
        - 前置：当前费率为负
        - 阈值 = max(open_basis_p20 - close_basis_p20, min_threshold_bps) * tolerance_ratio
        - 当 abs(funding_pnl_bps) >= 阈值时触发
        """
        funding_rate_24h = pos.get('funding_rate_24h')
        if funding_rate_24h is None or float(funding_rate_24h) >= 0:
            return False

        funding_pnl_bps = float(pos.get('funding_pnl_bps') or 0)
        if funding_pnl_bps >= 0:
            return False  # 净收入，无需平仓

        open_p20 = threshold_data.get('open_basis_p20')
        close_p20 = threshold_data.get('close_basis_p20')
        if open_p20 is None or close_p20 is None:
            return False

        convergence_space = float(open_p20) - float(close_p20)
        effective_space = max(convergence_space, self.funding_cost_min_threshold_bps)
        threshold = effective_space * self.funding_cost_tolerance_ratio

        return abs(funding_pnl_bps) >= threshold

    def _pre_execution_gate(self, base_asset: str, contract: str, symbol: str, pos: Dict) -> tuple:
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

            # ── 2.5 更新次数闸：拦截“盘口冻住 / 快照刚重建”呆滞场景 ──
            valley_state = self._valley_state.get(base_asset)
            if valley_state:
                gate_uc_now = int(getattr(gate_ob, 'update_count', 0) or 0)
                spot_uc_now = int(getattr(spot_ob, 'update_count', 0) or 0)
                uc_passed, uc_reason = self._check_update_count_freshness(
                    gate_uc_now, spot_uc_now,
                    valley_state.get('gate_uc_start'), valley_state.get('spot_uc_start'),
                )
                if not uc_passed:
                    logger.info(
                        f"平仓旁路-update_count闸拦截 | {base_asset} | "
                        f"uc_now(gate={gate_uc_now},spot={spot_uc_now}) | "
                        f"uc_start(gate={valley_state.get('gate_uc_start')},spot={valley_state.get('spot_uc_start')}) | "
                        f"sustained={(datetime.now() - valley_state['start_time']).total_seconds():.1f}s | "
                        f"原因:{uc_reason}"
                    )
                    return False, None, None, uc_reason

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

            # ── 全部通过 ──
            # 记录本次旁路风控读取到的 lag，供 _build_take_profit_detail 拼接到平仓原因
            self._last_orderbook_lag_ms[base_asset] = (gate_lag_ms, spot_lag_ms)
            logger.info(
                f"平仓旁路通过 | {base_asset} | "
                f"gate_basis={gate_basis_bps:.2f}bps | open_spread={open_spread_bps:.2f}bps | "
                f"trigger_basis={pos.get('current_spread_bps')}bps | "
                f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms,max={self._max_orderbook_lag_ms:.0f}ms)"
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
        state = self._valley_state.get(base_asset)

        contract = pos.get('future_contract', '')
        symbol = pos.get('spot_symbol') or f"{base_asset}USDT"
        uc_threshold = self.min_update_count

        if state is None:
            # 首次进入监控，记录谷底、开始时间 及 update_count 起点
            open_spread_bps = float(pos.get('open_spread_bps') or 0)
            gate_uc_start, spot_uc_start = self._get_orderbook_update_counts(contract, symbol)
            self._valley_state[base_asset] = {
                'valley_bps': current_spread_bps,
                'start_time': now,
                'open_spread_bps': open_spread_bps,
                'trigger': None,
                'gate_uc_start': gate_uc_start,
                'spot_uc_start': spot_uc_start,
            }
            logger.info(
                f"止盈谷底监控开始 | {base_asset} | "
                f"spread={current_spread_bps:.2f}bps | open_spread={open_spread_bps:.2f}bps | "
                f"start_time={now.strftime('%H:%M:%S.%f')[:-3]} | "
                f"uc_threshold≥{uc_threshold}(=sustain{self.sustain_sec:.0f}s×2) | "
                f"uc_start(gate={gate_uc_start}, spot={spot_uc_start})"
            )
            return False

        # 更新谷底
        if current_spread_bps < state['valley_bps']:
            state['valley_bps'] = current_spread_bps

        # 检查超时
        elapsed_sec = (now - state['start_time']).total_seconds()
        if elapsed_sec >= self.valley_monitor_timeout_sec:
            # 所有“通过”路径必须打日志：超时直接放行，记录完整时间戳与 uc 增量供复盘
            gate_uc_now, spot_uc_now = self._get_orderbook_update_counts(contract, symbol)
            gate_delta = (gate_uc_now or 0) - (state.get('gate_uc_start') or 0)
            spot_delta = (spot_uc_now or 0) - (state.get('spot_uc_start') or 0)
            state['trigger'] = 'timeout'
            logger.info(
                f"止盈谷底监控超时，直接平仓 | {base_asset} | "
                f"valley={state['valley_bps']:.2f}bps | current={current_spread_bps:.2f}bps | "
                f"elapsed={elapsed_sec:.1f}s≥{self.valley_monitor_timeout_sec}s | "
                f"start={state['start_time'].strftime('%H:%M:%S.%f')[:-3]}→"
                f"now={now.strftime('%H:%M:%S.%f')[:-3]} | "
                f"uc增量(gate={gate_delta}, spot={spot_delta})"
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
                f"valley={state['valley_bps']:.2f} | open_spread={open_spread_bps:.2f} | "
                f"current={current_spread_bps:.2f}"
            )
            state['trigger'] = 'rebound'
            return True

        if current_spread_bps >= rebound_threshold:
            # 所有“通过”路径必须打日志：反弹确认放行，含 uc 增量与时间戳
            gate_uc_now, spot_uc_now = self._get_orderbook_update_counts(contract, symbol)
            gate_delta = (gate_uc_now or 0) - (state.get('gate_uc_start') or 0)
            spot_delta = (spot_uc_now or 0) - (state.get('spot_uc_start') or 0)
            state['trigger'] = 'rebound'
            logger.info(
                f"止盈谷底反弹确认，执行平仓 | {base_asset} | "
                f"valley={state['valley_bps']:.2f}bps | current={current_spread_bps:.2f}bps | "
                f"rebound_thr={rebound_threshold:.2f}bps | "
                f"open_spread={open_spread_bps:.2f}bps | rebound_pct={self.valley_rebound_pct*100:.0f}% | "
                f"sustained={elapsed_sec:.1f}s | "
                f"start={state['start_time'].strftime('%H:%M:%S.%f')[:-3]}→"
                f"now={now.strftime('%H:%M:%S.%f')[:-3]} | "
                f"uc增量(gate={gate_delta}, spot={spot_delta})≥{uc_threshold}"
            )
            return True

        return False

    def _build_take_profit_detail(self, pos: Dict, current_spread_bps: float) -> str:
        """构建止盈平仓的详细原因字符串（含谷底确认信息 + 行情新鲜度）"""
        ba = pos.get('base_asset', '')
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
        valley_state = self._valley_state.get(ba)
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

    def _build_funding_cost_detail(self, pos: Dict, threshold_data: Dict) -> str:
        """构建累计资金费超阈值平仓的详细原因"""
        funding_pnl_bps = float(pos.get('funding_pnl_bps') or 0)
        open_p20 = float(threshold_data.get('open_basis_p20') or 0)
        close_p20 = float(threshold_data.get('close_basis_p20') or 0)
        convergence_space = open_p20 - close_p20
        effective_space = max(convergence_space, self.funding_cost_min_threshold_bps)
        threshold = effective_space * self.funding_cost_tolerance_ratio

        return (
            f"资金费超限|累计{funding_pnl_bps:.1f}bps"
            f"|阈值-{threshold:.1f}bps"
            f"(收敛空间{effective_space:.1f}×{self.funding_cost_tolerance_ratio})"
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
        liq_distance = pos.get('liq_distance_pct')
        if liq_distance is not None:
            parts.append(f"距爆仓{float(liq_distance):.2f}%")
        close_reason_detail = '|'.join(parts)
        return self._execute_close(pos, close_reason, close_reason_detail, orderbook_row)

    # ──────────────────────────────────────────────────────────────────
    # 订单构建与执行
    # ──────────────────────────────────────────────────────────────────

    def _execute_close(self, pos: Dict, close_reason: str, close_reason_detail: str, orderbook_row: Dict) -> Dict:
        """构建平仓订单组 → 调用成交引擎 → 持久化"""
        ba = pos.get('base_asset', '')
        order_group = self._build_close_order_group(pos)
        exec_result = self.executor_client.execute(order_group, orderbook_row)
        self._save_close(pos, order_group, exec_result, close_reason, close_reason_detail)
        return {
            'base_asset': ba,
            'success': exec_result['success'],
            'order_uuid': order_group['order_uuid'],
            'close_reason': close_reason,
            'message': exec_result.get('message'),
        }

    def _build_close_order_group(self, pos: Dict) -> Dict:
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

        return {
            'order_uuid': order_uuid,
            'base_asset': ba,
            'spot_symbol': spot_symbol,
            'future_contract': future_contract,
            'spot_order': spot_order,
            'future_order': future_order,
        }

    # ──────────────────────────────────────────────────────────────────
    # 持久化
    # ──────────────────────────────────────────────────────────────────

    def _save_close(
        self, pos: Dict, order_group: Dict, exec_result: Dict,
        close_reason: str, close_reason_detail: str
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

        # ── 插入平仓订单 ──
        insert_sql = """
            INSERT INTO mi_trade_order (
                order_uuid, position_id, base_asset, spot_symbol, future_contract,
                order_side, market_type, trade_direction, status, channel,
                reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_vwap_basis_bps, risk_relief_bps,
                open_marginal_basis_bps, funding_rate_24h, executed_at
            ) VALUES (
                %(order_uuid)s, %(position_id)s, %(base_asset)s, %(spot_symbol)s,
                %(future_contract)s, %(order_side)s, %(market_type)s,
                %(trade_direction)s, %(status)s, %(channel)s,
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                %(open_coverage)s, %(open_vwap_basis_bps)s, %(risk_relief_bps)s,
                %(open_marginal_basis_bps)s, %(funding_rate_24h)s, %(executed_at)s
            )
        """

        for market_key in ['spot_order', 'future_order']:
            order = order_group[market_key].copy()
            order['position_id'] = position_id
            order['channel'] = self.executor_client.channel
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
