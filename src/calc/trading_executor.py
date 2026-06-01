"""
交易执行器模块
- TradingExecutor: 开仓判断 + 订单生成 + 持久化
- 成交引擎通过 ExecutorClient (HTTP) 调用独立的执行器服务（虚拟/实盘），实现虚实分离
"""
import uuid
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from common.database import db_manager
from common.config import config
from common.logger import get_logger

logger = get_logger(__name__)
from calc.orderbook_enricher import calc_vwap_basis_bps, calc_full_fee_bps, calc_open_fee_bps
from calc.executor_client import ExecutorClient
from exchange_apis.get_gate_future_contracts import get_single_contract_funding_rate


class TradingExecutor:
    """交易执行器(开仓判断 + 订单生成 + 持久化，通过 ExecutorClient 调用成交引擎服务)"""
    
    def __init__(self, contract_meta: Dict, spot_meta: Dict, threshold_meta: Dict,
                 vwap_threshold_meta: Optional[Dict[str, float]] = None,
                 close_vwap_threshold_meta: Optional[Dict[str, Dict]] = None):
        """
        Args:
            contract_meta: base_asset -> {quanto_multiplier, order_size_min, price_decimal, size_decimal, ...}
            spot_meta: base_asset -> {step_size, min_qty, ...}
            threshold_meta: contract -> percentile threshold value
            vwap_threshold_meta: base_asset -> threshold_bps (按标的VWAP基差阈值)
            close_vwap_threshold_meta: base_asset -> {close_basis_p10..p40} (平仓基差阈值，用于盈利性守卫)
        """
        self.contract_meta = contract_meta
        self.spot_meta = spot_meta
        self.threshold_meta = threshold_meta
        self.vwap_threshold_meta = vwap_threshold_meta or {}
        self.close_vwap_threshold_meta = close_vwap_threshold_meta or {}
        
        # 通过 HTTP 客户端调用独立的成交引擎服务（虚拟/实盘），实现虚实分离
        executor_url = config.get_executor_url()
        executor_timeout = config.get_int('trade.executor.timeout_sec', 5)
        self.executor_client = ExecutorClient(executor_url, timeout=executor_timeout)
        
        # 从配置读取阈值
        self.coverage_threshold = config.get_float('trade.open.orderbook_coverage_threshold', 0.8)
        self.basis_threshold_bps = config.get_float('trade.open.vwap_basis_threshold_bps', -60)
        self.cooldown_sec = config.get_int('trade.open.cooldown_sec', 3600)
        self.funding_threshold_key = config.get('trade.filter.funding_rate_threshold_percentile', 'percentile_30')

        # 手续费率（用于盈利性守卫计算）
        self.fee_cost_bps = -calc_full_fee_bps(
            config.get_float('trade.fee.spot_open', 0.00075),
            config.get_float('trade.fee.spot_close', 0.00075),
            config.get_float('trade.fee.future_open', 0.00075),
            config.get_float('trade.fee.future_close', 0.00075)
        )

        # 盈利性守卫使用的平仓基差分位字段名
        self.close_threshold_col = config.get_str('trade.vwap.close_threshold_percentile', 'close_basis_p20')

        # 24小时成交量过滤阈值（USDT）
        self.min_spot_volume = config.get_float('trade.filter.min_spot_volume_24h_usdt', 0)
        self.min_future_volume = config.get_float('trade.filter.min_future_volume_24h_usdt', 0)

        # 峰值回落开仓策略
        self.peak_pullback_pct = config.get_float('trade.peak_pullback.pullback_pct', 0.10)
        self.peak_monitor_timeout_sec = config.get_int('trade.peak_pullback.monitor_timeout_sec', 60)
        self.peak_timeout_cooldown_sec = config.get_int('trade.peak_pullback.timeout_cooldown_sec', 300)
        self._peak_state: Dict[str, Dict] = {}  # base_asset -> {peak_bps, start_time, signal_id}
        self._timeout_cooldown_until: Dict[str, datetime] = {}  # base_asset -> 超时冷却截止时间

        # 保证金风控：距爆仓距离低于此值时禁止开仓
        self.margin_warning_pct = config.get_float('margin.warning_pct', 8.0)
        # 持仓距爆仓距离缓存: base_asset -> liq_distance_pct
        self._holding_liq_distance: Dict[str, float] = {}
        self._holding_count: Dict[str, int] = {}  # base_asset -> 持仓中仓位数量
        self.max_positions_per_asset = config.get_int('trade.open.max_positions_per_asset', 1)

        # 开仓拒单冷却：被交易所拒单后暂停该标的开仓，避免重复提交注定失败的订单
        self.reject_cooldown_sec = config.get_int('trade.open.reject_cooldown_sec', 300)
        self._reject_cooldown_until: Dict[str, datetime] = {}  # base_asset -> 冷却截止时间

        # 开仓金额（用于 min_notional 前置校验）
        self.open_amount_usdt = config.get_float('trade.open.amount_usdt', 5)
    
    def update_holding_margin_status(self, positions: List[Dict]):
        """
        更新持仓的保证金状态缓存（由调用方在每个开仓检查周期前调用）

        Args:
            positions: 已由 calculate_realtime_pnl 富化的持仓列表（含 liq_distance_pct）
        """
        self._holding_liq_distance.clear()
        self._holding_count.clear()
        for pos in positions:
            if pos.get('status') != 'holding':
                continue
            ba = pos.get('base_asset', '')
            if not ba:
                continue
            # 统计同标的持仓数量
            self._holding_count[ba] = self._holding_count.get(ba, 0) + 1
            liq_dist = pos.get('liq_distance_pct')
            if liq_dist is not None:
                # 同一标的多个仓位时，取最小距爆仓距离（最危险的）
                if ba not in self._holding_liq_distance or liq_dist < self._holding_liq_distance[ba]:
                    self._holding_liq_distance[ba] = liq_dist

    def check_and_open(self, orderbook_rows: List[Dict], refresh_fn=None) -> List[Dict]:
        """
        检查所有合约并执行开仓
        
        Args:
            orderbook_rows: 合并后的订单簿行(已计算对冲指标)
            refresh_fn: 可选回调，peak确认后调用获取最新盘口数据（减少决策到执行间的数据陈旧）
        
        Returns:
            开仓结果列表
        """
        results = []
        # 记录本轮风控通过的标的，用于清理已不满足条件的峰值状态
        risk_passed_assets = set()
        
        for row in orderbook_rows:
            try:
                base_asset = row.get('base_asset', '')
                
                # 0. 数据完整性检查：缺少有效盘口数据时跳过
                if row.get('spot_qty') is None or row.get('open_vwap_basis_bps') is None:
                    # 数据不完整时清除峰值状态，避免数据恢复后误触发超时开仓
                    self._resolve_signal(base_asset, 'conditions_lost', '数据不完整(盘口中断)')
                    self._peak_state.pop(base_asset, None)
                    continue
                
                # 1. 风控检查
                if not self._pass_risk_check(row):
                    # 风控不通过，清除该标的峰值监控状态（基差已跌回阈值下）
                    exit_reason = self._get_risk_fail_reason(row)
                    current_basis = row.get('open_vwap_basis_bps')
                    self._resolve_signal(
                        base_asset, 'conditions_lost', exit_reason,
                        exit_basis_bps=float(current_basis) if current_basis is not None else None
                    )
                    self._peak_state.pop(base_asset, None)
                    continue
                
                risk_passed_assets.add(base_asset)
                
                # 2. 冷却检查
                if not self._pass_cooldown_check(base_asset):
                    continue
                
                # 2.5 超时开仓冷却检查（防止连续超时重复开仓）
                if not self._pass_timeout_cooldown(base_asset):
                    continue
                
                # 2.6 拒单冷却检查（被交易所拒单后暂停该标的开仓）
                if not self._pass_reject_cooldown(base_asset):
                    continue
                
                # 3. 峰值回落确认
                open_vwap_basis = float(row.get('open_vwap_basis_bps'))
                if not self._pass_peak_check(base_asset, open_vwap_basis, row):
                    continue
                
                # 3.5 peak确认后，刷新最新盘口数据（消除决策到执行间的数据陈旧）
                if refresh_fn:
                    try:
                        fresh_rows = refresh_fn()
                        fresh_row = next((r for r in fresh_rows if r.get('base_asset') == base_asset), None)
                        if fresh_row and fresh_row.get('open_vwap_basis_bps') is not None:
                            row = fresh_row
                            open_vwap_basis = float(fresh_row['open_vwap_basis_bps'])
                    except Exception as e:
                        logger.debug(f"刷新盘口失败(使用原数据): {e}")
                
                # 4. 构建开仓原因（用于复盘）
                open_reason = self._build_open_reason(row, base_asset, open_vwap_basis)
                
                # 5. 生成订单组
                order_group = self._create_order_group(row)
                order_group['open_reason'] = open_reason
                
                # 6. 调用成交引擎服务（虚拟/实盘）
                exec_result = self.executor_client.execute(order_group, row)
                
                # 7. 持久化订单
                self._save_orders(order_group, exec_result)
                
                # 8. 更新信号状态
                peak_state = self._peak_state.get(base_asset, {})
                trigger_type = peak_state.get('trigger')
                if exec_result['success']:
                    self._resolve_signal(
                        base_asset, 'opened', None,
                        exit_basis_bps=open_vwap_basis,
                        trigger_type=trigger_type,
                        order_uuid=order_group['order_uuid']
                    )
                else:
                    self._resolve_signal(
                        base_asset, 'rejected', exec_result.get('message'),
                        exit_basis_bps=open_vwap_basis,
                        trigger_type=trigger_type
                    )
                    # 被交易所拒单后启动冷却，避免重复提交失败订单
                    self._reject_cooldown_until[base_asset] = datetime.now() + timedelta(seconds=self.reject_cooldown_sec)
                    logger.info(
                        f"开仓拒单冷却启动 | {base_asset} | "
                        f"冷却{self.reject_cooldown_sec}s | 原因: {exec_result.get('message', '')[:80]}"
                    )
                
                # 开仓后清除峰值状态
                self._peak_state.pop(base_asset, None)
                
                # 超时触发的开仓，设置较长冷却期
                if trigger_type == 'timeout':
                    self._timeout_cooldown_until[base_asset] = datetime.now() + timedelta(seconds=self.peak_timeout_cooldown_sec)
                    logger.info(
                        f"超时开仓冷却启动 | {base_asset} | "
                        f"冷却{self.peak_timeout_cooldown_sec}s"
                    )
                
                results.append({
                    'base_asset': base_asset,
                    'success': exec_result['success'],
                    'order_uuid': order_group['order_uuid'],
                    'message': exec_result.get('message')
                })
                
                if exec_result['success']:
                    logger.info(
                        f"开仓成功 | {base_asset} | "
                        f"spot_vwap={exec_result['spot_order']['exec_price']} | "
                        f"future_vwap={exec_result['future_order']['exec_price']}"
                    )
                else:
                    logger.warning(f"开仓拒单 | {base_asset} | reason={exec_result['message']}")
                    
            except Exception as e:
                logger.error(f"开仓失败 {row.get('base_asset', 'unknown')}: {e}")
                results.append({
                    'base_asset': row.get('base_asset', 'unknown'),
                    'success': False,
                    'message': str(e)
                })
        
        return results
    
    def _pass_risk_check(self, row: Dict) -> bool:
        """
        风控规则检查:
        0. 保证金风控: 该标的现有持仓距爆仓距离 < warning_pct 时禁止开仓
        1. 资金费率 >= 阈值
        2. 开仓盘口覆盖 <= 阈值
        3. 开仓边际基差 >= 阈值
        4. 盈利性守卫: 开仓基差 > 平仓基差阈值 + 手续费(确保价差有收敛空间)
        """
        base_asset = row.get('base_asset', '')

        # 同标的持仓数上限检查：防止同一波收敛行情中连续开仓
        if self._holding_count.get(base_asset, 0) >= self.max_positions_per_asset:
            return False

        # 保证金风控检查：该标的现有持仓已接近爆仓时禁止加仓
        if base_asset in self._holding_liq_distance:
            if self._holding_liq_distance[base_asset] < self.margin_warning_pct:
                return False

        # 最小名义价值检查：开仓金额低于交易所最低要求时直接过滤
        if base_asset in self.spot_meta:
            min_notional = self.spot_meta[base_asset].get('min_notional')
            if min_notional is not None and self.open_amount_usdt < min_notional:
                return False

        # 资金费率检查
        funding_rate = row.get('funding_rate_24h')
        contract = row.get('contract', '')
        threshold = self.threshold_meta.get(contract)
        if funding_rate is not None and threshold is not None:
            if float(funding_rate) < float(threshold):
                return False
        
        # 盘口覆盖检查
        open_coverage = row.get('open_coverage')
        if open_coverage is not None:
            if float(open_coverage) > self.coverage_threshold:
                return False
        
        # 边际基差检查（支持按标的VWAP基差阈值）
        # 统一口径：均与纯基差（open_vwap_basis_bps）对比，不含手续费和风险缓释
        base_asset = row.get('base_asset', '')
        open_vwap_basis = row.get('open_vwap_basis_bps')
        if open_vwap_basis is not None:
            if base_asset in self.vwap_threshold_meta:
                # 按标的阈值：基差必须 >= 阈值才允许开仓
                if float(open_vwap_basis) < self.vwap_threshold_meta[base_asset]:
                    return False
            else:
                # 全局回退阈值
                if float(open_vwap_basis) < self.basis_threshold_bps:
                    return False
        
        # 盈利性守卫: 开仓基差 > 平仓基差阈值 + 手续费成本
        # 确保即使按历史分位平仓，利润仍能覆盖手续费（过滤结构性亏损标的）
        if open_vwap_basis is not None and base_asset in self.close_vwap_threshold_meta:
            close_data = self.close_vwap_threshold_meta[base_asset]
            close_threshold = close_data.get(self.close_threshold_col)
            if close_threshold is not None:
                if float(open_vwap_basis) <= float(close_threshold) + self.fee_cost_bps:
                    return False
        
        # 24小时成交量检查（期货）
        if self.min_future_volume > 0 and base_asset in self.contract_meta:
            volume_24h_settle = self.contract_meta[base_asset].get('volume_24h_settle')
            if volume_24h_settle is not None and volume_24h_settle < self.min_future_volume:
                return False
        
        # 24小时成交量检查（现货）
        if self.min_spot_volume > 0 and base_asset in self.spot_meta:
            quote_volume = self.spot_meta[base_asset].get('quote_volume')
            if quote_volume is not None and quote_volume < self.min_spot_volume:
                return False
        
        return True
    
    def _verify_realtime_funding_rate(self, base_asset: str, contract: str) -> bool:
        """
        开仓前实时校验资金费率：从 Gate API 获取最新费率，确认仍为正且达标。
        若 API 调用失败（网络问题等），回退为放行（不阻塞开仓）。
        """
        try:
            realtime_rate_24h = get_single_contract_funding_rate(contract)
            if realtime_rate_24h is None:
                # API 调用失败，回退为放行（不因网络问题阻止开仓）
                logger.debug(f"实时费率校验跳过(获取失败) | {base_asset}")
                return True
            
            # 校验费率 >= 0（基本条件：费率为正）
            if realtime_rate_24h < 0:
                logger.info(
                    f"实时费率校验拦截 | {base_asset} | "
                    f"实时费率={realtime_rate_24h*100:.4f}%(已翻负)"
                )
                return False
            
            # 校验费率是否达到阈值
            threshold = self.threshold_meta.get(contract)
            if threshold is not None and realtime_rate_24h < float(threshold):
                logger.info(
                    f"实时费率校验拦截 | {base_asset} | "
                    f"实时费率={realtime_rate_24h*100:.4f}% < 阈值{float(threshold)*100:.4f}%"
                )
                return False
            
            return True
        except Exception as e:
            # 任何异常均回退为放行
            logger.debug(f"实时费率校验异常(回退放行) | {base_asset}: {e}")
            return True

    def _pass_peak_check(self, base_asset: str, current_basis_bps: float, row: Dict = None) -> bool:
        """
        峰值回落确认逻辑:
        - 首次超阈值: 实时校验资金费率 + 记录峰值和开始时间, 返回 False(等待)
        - 后续更高: 更新峰值, 返回 False(继续等待)
        - 从峰值回落 X%: 返回 True(确认开仓)
        - 超时: 返回 True(直接开仓)
        """
        now = datetime.now()
        state = self._peak_state.get(base_asset)
        
        if state is None:
            # 首次进入监控前，实时校验资金费率（仅在此时调用一次API，不影响后续开仓速度）
            if row:
                contract = row.get('contract', '')
                if not self._verify_realtime_funding_rate(base_asset, contract):
                    return False
            
            # 实时费率确认OK，开始峰值监控
            signal_id = self._create_signal(base_asset, current_basis_bps)
            self._peak_state[base_asset] = {
                'peak_bps': current_basis_bps,
                'start_time': now,
                'trigger': None,  # 用于记录触发方式
                'signal_id': signal_id,
            }
            logger.info(
                f"峰值监控开始 | {base_asset} | "
                f"basis={current_basis_bps:.2f}bps"
            )
            return False
        
        # 更新峰值
        if current_basis_bps > state['peak_bps']:
            state['peak_bps'] = current_basis_bps
        
        # 检查超时
        elapsed_sec = (now - state['start_time']).total_seconds()
        if elapsed_sec >= self.peak_monitor_timeout_sec:
            state['trigger'] = 'timeout'
            logger.info(
                f"峰值监控超时，直接开仓 | {base_asset} | "
                f"peak={state['peak_bps']:.2f} | current={current_basis_bps:.2f} | "
                f"elapsed={elapsed_sec:.0f}s"
            )
            return True
        
        # 检查回落确认: 当前基差 <= 峰值 * (1 - pullback_pct)
        pullback_threshold = state['peak_bps'] * (1 - self.peak_pullback_pct)
        if current_basis_bps <= pullback_threshold:
            state['trigger'] = 'pullback'
            logger.info(
                f"峰值回落确认，执行开仓 | {base_asset} | "
                f"peak={state['peak_bps']:.2f} | current={current_basis_bps:.2f} | "
                f"pullback={self.peak_pullback_pct*100:.0f}%"
            )
            return True
        
        return False

    # ──────────────────────────────────────────────────────────────────
    # 信号日志记录
    # ──────────────────────────────────────────────────────────────────

    def _create_signal(self, base_asset: str, entry_basis_bps: float) -> Optional[int]:
        """创建信号记录（进入峰值监控时）"""
        try:
            sql = """
                INSERT INTO mi_trade_signal (base_asset, signal_time, status, entry_basis_bps, peak_basis_bps)
                VALUES (%(base_asset)s, %(signal_time)s, 'monitoring', %(entry_basis_bps)s, %(peak_basis_bps)s)
            """
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, {
                    'base_asset': base_asset,
                    'signal_time': datetime.now(),
                    'entry_basis_bps': round(entry_basis_bps, 2),
                    'peak_basis_bps': round(entry_basis_bps, 2),
                })
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"信号记录创建失败 {base_asset}: {e}")
            return None

    def _resolve_signal(
        self, base_asset: str, status: str, exit_reason: Optional[str],
        exit_basis_bps: Optional[float] = None,
        trigger_type: Optional[str] = None,
        order_uuid: Optional[str] = None,
    ):
        """结束信号记录（状态转为终态）"""
        state = self._peak_state.get(base_asset)
        if not state:
            return  # 无峰值状态，说明没有活跃信号

        signal_id = state.get('signal_id')
        if not signal_id:
            return

        now = datetime.now()
        duration_sec = int((now - state['start_time']).total_seconds())
        peak_bps = state.get('peak_bps')

        try:
            sql = """
                UPDATE mi_trade_signal SET
                    status = %(status)s,
                    resolved_time = %(resolved_time)s,
                    peak_basis_bps = %(peak_basis_bps)s,
                    exit_basis_bps = %(exit_basis_bps)s,
                    exit_reason = %(exit_reason)s,
                    duration_sec = %(duration_sec)s,
                    trigger_type = %(trigger_type)s,
                    order_uuid = %(order_uuid)s
                WHERE id = %(id)s
            """
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, {
                    'status': status,
                    'resolved_time': now,
                    'peak_basis_bps': round(peak_bps, 2) if peak_bps is not None else None,
                    'exit_basis_bps': round(exit_basis_bps, 2) if exit_basis_bps is not None else None,
                    'exit_reason': exit_reason[:200] if exit_reason else None,
                    'duration_sec': duration_sec,
                    'trigger_type': trigger_type,
                    'order_uuid': order_uuid,
                    'id': signal_id,
                })
        except Exception as e:
            logger.error(f"信号记录更新失败 {base_asset}: {e}")

    def _get_risk_fail_reason(self, row: Dict) -> str:
        """识别风控失败的具体原因（用于信号日志）"""
        base_asset = row.get('base_asset', '')

        # 保证金风控检查
        if base_asset in self._holding_liq_distance:
            liq_dist = self._holding_liq_distance[base_asset]
            if liq_dist < self.margin_warning_pct:
                return f"保证金风控(距爆仓{liq_dist:.1f}%<{self.margin_warning_pct:.1f}%)"

        # 最小名义价值检查
        if base_asset in self.spot_meta:
            min_notional = self.spot_meta[base_asset].get('min_notional')
            if min_notional is not None and self.open_amount_usdt < min_notional:
                return f"开仓金额低于最小名义值({self.open_amount_usdt}<{min_notional}USDT)"
        # 资金费率检查
        funding_rate = row.get('funding_rate_24h')
        contract = row.get('contract', '')
        threshold = self.threshold_meta.get(contract)
        if funding_rate is not None and threshold is not None:
            if float(funding_rate) < float(threshold):
                return f"资金费率不达标({float(funding_rate)*100:.4f}%<{float(threshold)*100:.4f}%)"

        # 盘口覆盖检查
        open_coverage = row.get('open_coverage')
        if open_coverage is not None and float(open_coverage) > self.coverage_threshold:
            return f"盘口覆盖超限({float(open_coverage):.2f}>{self.coverage_threshold})"

        # 基差不达标
        open_vwap_basis = row.get('open_vwap_basis_bps')
        if open_vwap_basis is not None:
            thr = self.vwap_threshold_meta.get(base_asset, self.basis_threshold_bps)
            if float(open_vwap_basis) < thr:
                return f"基差跌回阈值下({float(open_vwap_basis):.1f}<{thr:.1f}bps)"

        # 盈利性守卫
        if open_vwap_basis is not None and base_asset in self.close_vwap_threshold_meta:
            close_data = self.close_vwap_threshold_meta[base_asset]
            close_thr = close_data.get(self.close_threshold_col)
            if close_thr is not None:
                if float(open_vwap_basis) <= float(close_thr) + self.fee_cost_bps:
                    return f"盈利性守卫({float(open_vwap_basis):.1f}<={float(close_thr):.1f}+{self.fee_cost_bps:.0f})"

        # 成交量不足
        if self.min_future_volume > 0 and base_asset in self.contract_meta:
            vol = self.contract_meta[base_asset].get('volume_24h_settle')
            if vol is not None and vol < self.min_future_volume:
                return f"期货成交量不足({vol:.0f}<{self.min_future_volume:.0f})"

        if self.min_spot_volume > 0 and base_asset in self.spot_meta:
            vol = self.spot_meta[base_asset].get('quote_volume')
            if vol is not None and vol < self.min_spot_volume:
                return f"现货成交量不足({vol:.0f}<{self.min_spot_volume:.0f})"

        return '风控条件变化'

    def _build_open_reason(self, row: Dict, base_asset: str, open_vwap_basis: float) -> str:
        """
        构建开仓原因字符串，记录关键决策参数，便于复盘。
        格式: "基差{bps}(阈值{thr})|费率{rate}|峰值{info}"
        """
        parts = []

        # 1. VWAP基差 vs 阈值
        threshold_bps = self.vwap_threshold_meta.get(base_asset, self.basis_threshold_bps)
        parts.append(f"基差{open_vwap_basis:.1f}bps(阈值{threshold_bps:.1f})")

        # 2. 24h资金费率
        funding_rate = row.get('funding_rate_24h')
        if funding_rate is not None:
            rate_pct = float(funding_rate) * 100
            parts.append(f"费率{rate_pct:.4f}%")

        # 3. 峰值回落信息
        peak_state = self._peak_state.get(base_asset)
        if peak_state:
            peak_bps = peak_state.get('peak_bps', 0)
            trigger = peak_state.get('trigger', 'unknown')
            if trigger == 'pullback':
                parts.append(f"峰值回落(峰{peak_bps:.1f}→回落{self.peak_pullback_pct*100:.0f}%)")
            elif trigger == 'timeout':
                elapsed = (datetime.now() - peak_state['start_time']).total_seconds()
                parts.append(f"峰值超时(峰{peak_bps:.1f},{elapsed:.0f}s)")
            else:
                parts.append(f"峰值{peak_bps:.1f}")

        # 4. 盈利性守卫
        if base_asset in self.close_vwap_threshold_meta:
            close_data = self.close_vwap_threshold_meta[base_asset]
            close_thr = close_data.get(self.close_threshold_col)
            if close_thr is not None:
                parts.append(f"守卫({open_vwap_basis:.1f}>{float(close_thr):.1f}+{self.fee_cost_bps:.0f}费)")

        # 5. 24h成交量（现货/期货）
        vol_parts = []
        if base_asset in self.spot_meta:
            qv = self.spot_meta[base_asset].get('quote_volume')
            if qv is not None:
                vol_parts.append(f"现货{qv/10000:.0f}w")
        if base_asset in self.contract_meta:
            fv = self.contract_meta[base_asset].get('volume_24h_settle')
            if fv is not None:
                vol_parts.append(f"期货{fv/10000:.0f}w")
        if vol_parts:
            parts.append(f"量({'|'.join(vol_parts)})")

        return '|'.join(parts)

    def _pass_timeout_cooldown(self, base_asset: str) -> bool:
        """检查超时开仓冷却期（防止连续超时重复开仓）"""
        cooldown_until = self._timeout_cooldown_until.get(base_asset)
        if cooldown_until is None:
            return True  # 无超时冷却
        if datetime.now() >= cooldown_until:
            # 冷却已过期，清除
            self._timeout_cooldown_until.pop(base_asset, None)
            return True
        return False

    def _pass_reject_cooldown(self, base_asset: str) -> bool:
        """检查开仓拒单冷却期（被交易所拒单后暂停该标的开仓）"""
        cooldown_until = self._reject_cooldown_until.get(base_asset)
        if cooldown_until is None:
            return True  # 无拒单冷却
        if datetime.now() >= cooldown_until:
            # 冷却已过期，清除
            self._reject_cooldown_until.pop(base_asset, None)
            return True
        return False

    def _pass_cooldown_check(self, base_asset: str) -> bool:
        """检查冷却期(从订单表查询最近一次成功开仓)"""
        sql = """
            SELECT MAX(created_at) as last_open_time 
            FROM mi_trade_order 
            WHERE base_asset = %s 
              AND market_type = 'spot' 
              AND order_side = 'open' 
              AND status = 'executed'
              AND channel = 'Mock'
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (base_asset,))
            row = cursor.fetchone()
            
            if not row or not row['last_open_time']:
                return True  # 无开仓记录
            
            last_time = row['last_open_time']
            elapsed = (datetime.now() - last_time).total_seconds()
            return elapsed >= self.cooldown_sec
    
    def _create_order_group(self, row: Dict) -> Dict:
        """生成订单组(现货+期货)"""
        order_uuid = str(uuid.uuid4())
        base_asset = row['base_asset']
        contract = row['contract']
        
        target_qty = row['spot_qty']  # 已对齐的对冲数量
        target_amount = row.get('open_amount_usdt', self.open_amount_usdt)
        
        # 获取精度配置
        quanto_multiplier = self._get_quanto_multiplier(base_asset)
        
        # 现货订单
        spot_order = {
            'order_uuid': order_uuid,
            'base_asset': base_asset,
            'spot_symbol': f"{base_asset}USDT",
            'future_contract': None,
            'order_side': 'open',
            'market_type': 'spot',
            'trade_direction': 'buy',
            'status': 'pending',
            'target_qty': target_qty,
            'target_amount': target_amount,
        }
        
        # 期货订单
        future_order = {
            'order_uuid': order_uuid,
            'base_asset': base_asset,
            'spot_symbol': None,
            'future_contract': contract,
            'order_side': 'open',
            'market_type': 'future',
            'trade_direction': 'sell',
            'status': 'pending',
            'target_qty': target_qty,
            'target_amount': target_amount,
        }
        
        return {
            'order_uuid': order_uuid,
            'base_asset': base_asset,
            'spot_symbol': f"{base_asset}USDT",
            'future_contract': contract,
            'spot_order': spot_order,
            'future_order': future_order,
            'open_coverage': row.get('open_coverage'),
            'spot_open_coverage': row.get('spot_open_coverage'),
            'future_open_coverage': row.get('future_open_coverage'),
            'open_vwap_basis_bps': row.get('open_vwap_basis_bps'),
            'risk_relief_bps': row.get('risk_relief_bps'),
            'open_marginal_basis_bps': row.get('open_marginal_basis_bps'),
            'funding_rate_24h': row.get('funding_rate_24h')
        }
    
    def _save_orders(self, order_group: Dict, exec_result: Dict):
        """持久化订单到数据库"""
        # 开仓成功时，先创建持仓记录，获取 position_id
        position_id = None
        if exec_result['success'] and order_group['spot_order']['order_side'] == 'open':
            position_id = self._create_position(order_group, exec_result)
        
        # --- 统一为实际成交口径：用格式化后的 exec_price 重新计算 VWAP 基差 ---
        if exec_result['success']:
            spot_exec_price = float(exec_result['spot_order']['exec_price'])
            future_exec_price = float(exec_result['future_order']['exec_price'])
            actual_basis_bps = calc_vwap_basis_bps(spot_exec_price, future_exec_price)
            if actual_basis_bps is not None:
                actual_basis_bps = round(actual_basis_bps, 2)
            else:
                actual_basis_bps = order_group.get('open_vwap_basis_bps')
            order_group['open_vwap_basis_bps'] = actual_basis_bps
            # 重算开仓边际基差
            open_fee_bps = calc_open_fee_bps(
                config.get_float('trade.fee.spot_open', 0.00075),
                config.get_float('trade.fee.future_open', 0.00075)
            )
            risk_relief_bps = config.get_float('trade.open.risk_relief_bps', 10)
            if actual_basis_bps is not None:
                order_group['open_marginal_basis_bps'] = round(actual_basis_bps + open_fee_bps + risk_relief_bps, 2)
        
        sql = """
            INSERT INTO mi_trade_order (
                order_uuid, position_id, base_asset, spot_symbol, future_contract, order_side, market_type,
                trade_direction, status, channel, reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_vwap_basis_bps, risk_relief_bps, open_marginal_basis_bps, funding_rate_24h, executed_at
            ) VALUES (
                %(order_uuid)s, %(position_id)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                %(order_side)s, %(market_type)s, %(trade_direction)s, %(status)s, %(channel)s,
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                %(open_coverage)s, %(open_vwap_basis_bps)s, %(risk_relief_bps)s, %(open_marginal_basis_bps)s, %(funding_rate_24h)s, %(executed_at)s
            )
        """
        
        for market_key in ['spot_order', 'future_order']:
            order = order_group[market_key].copy()
            
            # 注入风控指标
            # 盘口覆盖：现货使用 spot_open_coverage，期货使用 future_open_coverage
            if market_key == 'spot_order':
                order['open_coverage'] = order_group.get('spot_open_coverage')
            else:
                order['open_coverage'] = order_group.get('future_open_coverage')
            order['open_vwap_basis_bps'] = order_group.get('open_vwap_basis_bps')
            order['risk_relief_bps'] = order_group.get('risk_relief_bps')
            order['open_marginal_basis_bps'] = order_group.get('open_marginal_basis_bps')
            order['funding_rate_24h'] = order_group.get('funding_rate_24h')
            
            # 注入渠道和持仓关联
            order['channel'] = 'Mock'
            order['position_id'] = position_id
            
            # 更新成交信息
            if exec_result['success']:
                exec_data = exec_result[market_key]
                order['status'] = 'executed'
                order['exec_price'] = exec_data['exec_price']
                order['exec_qty'] = exec_data['exec_qty']
                order['exec_amount'] = exec_data['exec_amount']
                order['coverage_ratio'] = exec_data.get('coverage_ratio')
                # 开仓成功时，将开仓原因写入reject_reason供前端复盘查看
                order['reject_reason'] = order_group.get('open_reason')
                order['executed_at'] = datetime.now()
            else:
                order['status'] = 'rejected'
                order['reject_reason'] = exec_result.get('message')
                order['exec_price'] = None
                order['exec_qty'] = None
                order['exec_amount'] = None
                order['coverage_ratio'] = None
                order['executed_at'] = None
            
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, order)
    
    def _create_position(self, order_group: Dict, exec_result: Dict) -> int:
        """创建持仓记录，返回 position_id"""
        spot_order = order_group['spot_order']
        future_order = order_group['future_order']
        spot_exec = exec_result['spot_order']
        future_exec = exec_result['future_order']
        
        # 计算开仓价差 bps
        spot_price = float(spot_exec['exec_price'])
        future_price = float(future_exec['exec_price'])
        open_spread_bps = calc_vwap_basis_bps(spot_price, future_price) or 0
        
        sql = """
            INSERT INTO mi_trade_position (
                order_uuid, base_asset, spot_symbol, future_contract,
                status, opened_at,
                spot_open_qty, spot_open_price, spot_open_amount,
                future_open_qty, future_open_price, future_open_contracts,
                open_spread_bps, open_reason,
                funding_rate_sum_bps, funding_payments_count, funding_total_pnl
            ) VALUES (
                %(order_uuid)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                'holding', %(opened_at)s,
                %(spot_open_qty)s, %(spot_open_price)s, %(spot_open_amount)s,
                %(future_open_qty)s, %(future_open_price)s, %(future_open_contracts)s,
                %(open_spread_bps)s, %(open_reason)s,
                0, 0, 0
            )
        """
        
        # 计算期货张数
        quanto_multiplier = self._get_quanto_multiplier(order_group['base_asset'])
        future_contracts = int(float(future_exec['exec_qty']) / quanto_multiplier)
        
        params = {
            'order_uuid': order_group['order_uuid'],
            'base_asset': order_group['base_asset'],
            'spot_symbol': f"{order_group['base_asset']}USDT",
            'future_contract': order_group['future_contract'],
            'opened_at': datetime.now(),
            'spot_open_qty': spot_exec['exec_qty'],
            'spot_open_price': spot_exec['exec_price'],
            'spot_open_amount': spot_exec['exec_amount'],
            'future_open_qty': future_exec['exec_qty'],
            'future_open_price': future_exec['exec_price'],
            'future_open_contracts': future_contracts,
            'open_spread_bps': open_spread_bps,
            'open_reason': order_group.get('open_reason'),
        }
        
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.lastrowid
    
    def _get_spot_qty_precision(self, base_asset: str) -> int:
        if base_asset in self.spot_meta:
            step_size = self.spot_meta[base_asset].get('step_size', 0.00001)
            step_str = str(step_size)
            if '.' in step_str:
                return len(step_str.split('.')[-1].rstrip('0')) or 0
        return 5
    
    def _get_quanto_multiplier(self, base_asset: str) -> float:
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0
