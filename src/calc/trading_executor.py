"""
交易执行器模块
- TradingExecutor: 开仓判断 + 订单生成 + 持久化
- 成交引擎通过 ExecutorClient (HTTP) 调用独立的执行器服务（虚拟/实盘），实现虚实分离
"""
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Set

from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)
from calc.orderbook_enricher import calc_vwap_basis_bps, calc_full_fee_bps, calc_open_fee_bps
from calc.executor_client import ExecutorClient
from calc.orderbook_resiliency import (
    BookSideSpec,
    OrderBookResiliencyMonitor,
    ResiliencyConfig,
)
from calc.execution_audit import format_execution_audit
from calc.order_fee_resolver import build_order_execution_fields
from exchange_apis.get_gate_future_contracts import get_single_contract_funding_info

REBOUND_STRONG_TRIGGER = 'rebound_strong'


@dataclass
class TradingExecutorConfig:
    """交易执行器配置（由 api/ 层从 config.yaml 加载后注入）"""

    # ─── 成交引擎 ───
    executor_url: str = 'http://localhost:8081'
    executor_timeout: int = 5

    # ─── 开仓策略 ───
    coverage_threshold: float = 0.8
    basis_threshold_bps: float = -60
    cooldown_sec: int = 3600
    min_funding_rate_bps: float = -6.0
    open_amount_usdt: float = 5.0
    max_total_positions: int = 45
    max_positions_per_asset: int = 1
    reject_cooldown_sec: int = 60
    # 旁路风控新鲜度硬约束：本地接收最近一次 WS 盘口与当前下单时刻的允许最大延迟（毫秒）
    # 阐释：update_time(交易所时间戳) 不可靠，只以本地 last_update_time 判定“现在距上次收到行情多久”
    max_orderbook_lag_ms: float = 200.0

    # ─── 手续费率 ───
    fee_spot_open: float = 0.00075
    fee_spot_close: float = 0.00075
    fee_future_open: float = 0.00075
    fee_future_close: float = 0.00075
    fee_future_taker_open: float = 0.00075
    fee_future_taker_close: float = 0.00075

    # ─── VWAP基差阈值 ───
    close_threshold_percentile: str = 'close_basis_p20'

    # ─── 成交量过滤 ───
    min_spot_volume_24h_usdt: float = 0
    min_future_volume_24h_usdt: float = 0

    # ─── 峰值回落 + sustain 确认（开仓回落通道） ───
    peak_pullback_pct: float = 0.10
    peak_monitor_timeout_sec: int = 60
    peak_timeout_cooldown_sec: int = 10
    sustain_sec: float = 5.0                # 峰值监控最低持续秒数（过滤脉冲信号）

    # ─── 保证金风控 ───
    margin_warning_pct: float = 8.0

    # ─── 风险缓释 ───
    risk_relief_bps: float = 10.0

    # ─── 盘口恢复确认 ───
    resiliency_enabled: bool = True
    resiliency_window_sec: float = 3.0
    resiliency_min_samples: int = 5
    resiliency_min_recovery_ratio: float = 0.65
    resiliency_max_spread_widen_bps: float = 8.0
    resiliency_max_basis_volatility_bps: float = 6.0
    resiliency_min_hold_sec: float = 0.8
    resiliency_max_wait_sec: float = 3.0

    # ─── 动量开仓通道 ───
    momentum_enabled: bool = False
    momentum_window_sec: float = 1.2
    momentum_min_samples: int = 3
    momentum_min_rise_bps: float = 3.0
    momentum_min_basis_buffer_bps: float = 8.0
    momentum_safety_bps: float = 8.0
    momentum_allowed_tiers: List[str] = field(default_factory=lambda: ['A'])
    momentum_tier_overrides: Dict[str, Dict] = field(default_factory=dict)

    # ─── 回调后二次突破开仓 ───
    rebound_enabled: bool = True
    rebound_allowed_tiers: List[str] = field(default_factory=lambda: ['A', 'B'])
    rebound_min_rise_bps: float = 4.0
    rebound_min_slope_bps: float = 0.5
    rebound_min_basis_buffer_bps: float = 4.0
    rebound_max_wait_sec: float = 4.0
    rebound_strong_cushion_bps: float = 20.0
    rebound_strong_cushion_min_hold_sec: float = 1.0
    rebound_strong_cushion_max_wait_sec: float = 8.0

    # ─── 下单前执行质量保护 ───
    execution_guard_enabled: bool = True
    execution_guard_min_profit_buffer_bps: float = 15.0
    execution_guard_min_p20_buffer_bps: float = 3.0
    execution_guard_max_peak_decay_bps: float = 45.0

    # ─── funding-adjusted 统一入场门槛 ───
    funding_entry_enabled: bool = True
    funding_entry_capture_ratio: float = 0.5
    funding_entry_slippage_buffer_bps: float = 10.0
    funding_entry_min_expected_edge_bps: float = 0.0
    funding_entry_strong_funding_24h_bps: float = 50.0
    funding_entry_discount_ratio: float = 0.2
    funding_entry_max_discount_bps: float = 10.0

    # ─── 高资金费 carry 开仓通道 ───
    funding_carry_enabled: bool = False
    funding_carry_allowed_tiers: List[str] = field(default_factory=lambda: ['A', 'B'])
    funding_carry_min_24h_bps: float = 30.0
    funding_carry_basis_relax_bps: float = 15.0
    funding_carry_max_next_funding_min: float = 60.0
    funding_carry_amount_usdt: float = 0.0

    # ─── 信号降噪 / 执行质量冷却 ───
    rebound_timeout_cooldown_enabled: bool = True
    rebound_timeout_cooldown_sec: int = 60
    rebound_timeout_basis_change_reset_bps: float = 5.0
    asset_noise_cooldown_enabled: bool = True
    asset_noise_lookback_min: int = 60
    asset_noise_max_signals: int = 100
    asset_noise_min_opened: int = 1
    asset_noise_cooldown_min: int = 10
    execution_drift_cooldown_enabled: bool = True
    execution_drift_max_bps: float = 40.0
    execution_drift_cooldown_hour: float = 0.5

    # ─── 执行策略：Gate future maker + Binance spot taker ───
    future_maker_open_enabled: bool = False
    future_maker_open_allowed_tiers: List[str] = field(default_factory=lambda: ['A', 'B'])
    future_maker_open_ttl_ms: int = 1000
    future_maker_open_price_offset_bps: float = 0.0
    future_maker_open_fallback_ioc_enabled: bool = True
    future_maker_open_fallback_allowed_tiers: List[str] = field(default_factory=lambda: ['A', 'B'])
    future_maker_open_fallback_min_buffer_bps: float = 8.0
    future_maker_open_fallback_slippage_bps: float = 5.0
    future_maker_open_spot_hedge_protective_ioc_enabled: bool = True

    # ─── 交易所真实资金风控 ───
    capital_required: bool = False
    capital_max_age_sec: int = 180
    capital_gate_leverage: float = 2.0


class TradingExecutor:
    """交易执行器(开仓判断 + 订单生成 + 持久化，通过 ExecutorClient 调用成交引擎服务)"""
    
    def __init__(self, cfg: TradingExecutorConfig, contract_meta: Dict, spot_meta: Dict,
                 vwap_threshold_meta: Optional[Dict[str, float]] = None,
                 close_vwap_threshold_meta: Optional[Dict[str, Dict]] = None,
                 asset_tier_meta: Optional[Dict[str, str]] = None):
        """
        Args:
            cfg: 配置 dataclass（由 api/ 层构造后注入）
            contract_meta: base_asset -> {quanto_multiplier, order_size_min, price_decimal, size_decimal, ...}
            spot_meta: base_asset -> {step_size, min_qty, ...}
            vwap_threshold_meta: base_asset -> threshold_bps (按标的VWAP基差阈值)
            close_vwap_threshold_meta: base_asset -> {close_basis_p10..p40} (平仓基差阈值；旧模式用于盈利性守卫)
            asset_tier_meta: base_asset -> strategy_tier ('A'/'B'/'C')
        """
        self.contract_meta = contract_meta
        self.spot_meta = spot_meta
        self.vwap_threshold_meta = vwap_threshold_meta or {}
        self.close_vwap_threshold_meta = close_vwap_threshold_meta or {}
        self.asset_tier_meta = {
            str(k).strip().upper(): str(v).strip().upper()
            for k, v in (asset_tier_meta or {}).items()
            if k
        }
        
        # 通过 HTTP 客户端调用独立的成交引擎服务（虚拟/实盘），实现虚实分离
        self.executor_client = ExecutorClient(cfg.executor_url, timeout=cfg.executor_timeout)
        
        # 从 dataclass 读取策略参数
        self.coverage_threshold = cfg.coverage_threshold
        self.basis_threshold_bps = cfg.basis_threshold_bps
        self.cooldown_sec = cfg.cooldown_sec
        self.min_funding_rate_bps = cfg.min_funding_rate_bps

        # 手续费率（用于 entry_floor / 旧盈利性守卫计算）
        self.fee_cost_bps = -calc_full_fee_bps(
            cfg.fee_spot_open, cfg.fee_spot_close,
            cfg.fee_future_open, cfg.fee_future_close
        )
        # 单纯开仓费率（用于持久化计算边际基差）
        self._fee_spot_open = cfg.fee_spot_open
        self._fee_spot_close = cfg.fee_spot_close
        self._fee_future_open = cfg.fee_future_open
        self._fee_future_close = cfg.fee_future_close
        self._fee_future_taker_open = cfg.fee_future_taker_open
        self._fee_future_taker_close = cfg.fee_future_taker_close
        self._risk_relief_bps = cfg.risk_relief_bps

        # 平仓基差分位字段名（旧盈利性守卫仍使用）
        self.close_threshold_col = cfg.close_threshold_percentile

        # 24小时成交量过滤阈值（USDT）
        self.min_spot_volume = cfg.min_spot_volume_24h_usdt
        self.min_future_volume = cfg.min_future_volume_24h_usdt

        # 峰值回落 + sustain 开仓策略（单通道）
        self.peak_pullback_pct = cfg.peak_pullback_pct
        self.peak_monitor_timeout_sec = cfg.peak_monitor_timeout_sec
        self.peak_timeout_cooldown_sec = int(cfg.peak_timeout_cooldown_sec)
        self.sustain_sec = cfg.sustain_sec
        self._peak_state: Dict[str, Dict] = {}  # base_asset -> {peak_bps, start_time, trigger, signal_id}
        self._peak_timeout_cooldown: Dict[str, datetime] = {}
        # 临时槽位：实时费率校验通过时记录的实时费率(bps)，供 _build_open_reason 取用
        self._last_realtime_rate_bps: Dict[str, float] = {}
        self._last_realtime_funding_info: Dict[str, Dict] = {}

        # 保证金风控：保证金/维持保证金低于此值时禁止开仓
        self.margin_warning_pct = cfg.margin_warning_pct
        # 持仓 Gate 保证金/维持保证金缓存: base_asset -> min(gate_maintenance_margin_rate)
        self._holding_margin_rate: Dict[str, float] = {}
        self._holding_count: Dict[str, int] = {}  # base_asset -> 持仓中仓位数量
        self._holding_total_count: int = 0
        self.max_total_positions = max(int(cfg.max_total_positions or 0), 0)
        self.max_positions_per_asset = cfg.max_positions_per_asset

        # 开仓拒单冷却：被交易所拒单后暂停该标的开仓，避免重复提交注定失败的订单
        self.reject_cooldown_sec = cfg.reject_cooldown_sec
        self._reject_cooldown_until: Dict[str, datetime] = {}  # base_asset -> 冷却截止时间

        # 开仓冷却缓存：base_asset -> 上次成功开仓时间（DB 真理源 + 内存维护）
        # 仅本类 _save_orders 成功路径会写入，无外部入口，故可纯内存维护，避免每标的查 SQL
        self._last_open_time: Dict[str, datetime] = {}
        self._cooldown_loaded: bool = False  # 启动后首轮从 DB 一次性 load

        # 开仓金额（用于 min_notional 前置校验）
        self.open_amount_usdt = cfg.open_amount_usdt

        # 旁路风控新鲜度硬约束：以本地 last_update_time 为准，超过阈值判为“行情滞后”拒开
        self._max_orderbook_lag_ms = float(cfg.max_orderbook_lag_ms)
        # 临时槽位：旁路风控通过时记录的 (gate_lag_ms, spot_lag_ms)，供 _build_open_reason 拼到开仓原因
        self._last_orderbook_lag_ms: Dict[str, tuple] = {}
        # 临时槽位：盘口恢复确认通过时记录 metrics，供 _build_open_reason 拼到开仓原因
        self._last_resiliency_metrics: Dict[str, Dict] = {}
        # OrderBookManager 引用（由外部注入）
        self._gate_manager = None
        self._spot_manager = None

        self._open_resiliency = OrderBookResiliencyMonitor(
            ResiliencyConfig(
                enabled=cfg.resiliency_enabled,
                window_sec=cfg.resiliency_window_sec,
                min_samples=cfg.resiliency_min_samples,
                min_recovery_ratio=cfg.resiliency_min_recovery_ratio,
                max_spread_widen_bps=cfg.resiliency_max_spread_widen_bps,
                max_basis_volatility_bps=cfg.resiliency_max_basis_volatility_bps,
                min_hold_sec=cfg.resiliency_min_hold_sec,
                max_wait_sec=cfg.resiliency_max_wait_sec,
                allow_timeout_pass=False,
            ),
            [
                BookSideSpec('spot', 'ask', 1.0, 'spot_ask'),
                BookSideSpec('future', 'bid', 1.0, 'future_bid', '_future_qty_multiplier'),
            ],
            ['spot_open_coverage', 'future_open_coverage'],
            'open',
        )

        # 动量通道：适合高流动性标的在“超阈值 + 上升期 + 盘口好”时直接进入旁路。
        self.momentum_enabled = cfg.momentum_enabled
        self.momentum_window_sec = cfg.momentum_window_sec
        self.momentum_min_samples = cfg.momentum_min_samples
        self.momentum_min_rise_bps = cfg.momentum_min_rise_bps
        self.momentum_min_basis_buffer_bps = cfg.momentum_min_basis_buffer_bps
        self.momentum_safety_bps = cfg.momentum_safety_bps
        self.momentum_allowed_tiers: Set[str] = {
            str(t).strip().upper()
            for t in (cfg.momentum_allowed_tiers or [])
            if str(t).strip().upper() in ('A', 'B', 'C')
        }
        self.momentum_tier_overrides = {
            str(tier).strip().upper(): params
            for tier, params in (cfg.momentum_tier_overrides or {}).items()
            if isinstance(params, dict)
        }
        self._momentum_samples: Dict[str, deque] = {}

        self.rebound_enabled = cfg.rebound_enabled
        self.rebound_allowed_tiers: Set[str] = {
            str(t).strip().upper()
            for t in (cfg.rebound_allowed_tiers or [])
            if str(t).strip().upper() in ('A', 'B', 'C')
        }
        self.rebound_min_rise_bps = float(cfg.rebound_min_rise_bps)
        self.rebound_min_slope_bps = float(cfg.rebound_min_slope_bps)
        self.rebound_min_basis_buffer_bps = float(cfg.rebound_min_basis_buffer_bps)
        self.rebound_max_wait_sec = float(cfg.rebound_max_wait_sec)
        self.rebound_strong_cushion_bps = float(cfg.rebound_strong_cushion_bps)
        self.rebound_strong_cushion_min_hold_sec = float(cfg.rebound_strong_cushion_min_hold_sec)
        self.rebound_strong_cushion_max_wait_sec = float(cfg.rebound_strong_cushion_max_wait_sec)

        self.execution_guard_enabled = cfg.execution_guard_enabled
        self.execution_guard_min_profit_buffer_bps = float(cfg.execution_guard_min_profit_buffer_bps)
        self.execution_guard_min_p20_buffer_bps = float(cfg.execution_guard_min_p20_buffer_bps)
        self.execution_guard_max_peak_decay_bps = float(cfg.execution_guard_max_peak_decay_bps)

        self.funding_entry_enabled = bool(cfg.funding_entry_enabled)
        self.funding_entry_capture_ratio = float(cfg.funding_entry_capture_ratio)
        self.funding_entry_slippage_buffer_bps = float(cfg.funding_entry_slippage_buffer_bps)
        self.funding_entry_min_expected_edge_bps = float(cfg.funding_entry_min_expected_edge_bps)
        self.funding_entry_strong_funding_24h_bps = float(cfg.funding_entry_strong_funding_24h_bps)
        self.funding_entry_discount_ratio = float(cfg.funding_entry_discount_ratio)
        self.funding_entry_max_discount_bps = float(cfg.funding_entry_max_discount_bps)

        self.funding_carry_enabled = bool(cfg.funding_carry_enabled)
        self.funding_carry_allowed_tiers: Set[str] = {
            str(t).strip().upper()
            for t in (cfg.funding_carry_allowed_tiers or [])
            if str(t).strip().upper() in ('A', 'B', 'C')
        }
        self.funding_carry_min_24h_bps = float(cfg.funding_carry_min_24h_bps)
        self.funding_carry_basis_relax_bps = float(cfg.funding_carry_basis_relax_bps)
        self.funding_carry_max_next_funding_min = float(cfg.funding_carry_max_next_funding_min)
        self.funding_carry_amount_usdt = float(cfg.funding_carry_amount_usdt or 0.0)

        self.rebound_timeout_cooldown_enabled = bool(cfg.rebound_timeout_cooldown_enabled)
        self.rebound_timeout_cooldown_sec = int(cfg.rebound_timeout_cooldown_sec)
        self.rebound_timeout_basis_change_reset_bps = float(cfg.rebound_timeout_basis_change_reset_bps)
        self._rebound_timeout_cooldown: Dict[str, Dict] = {}

        self.asset_noise_cooldown_enabled = bool(cfg.asset_noise_cooldown_enabled)
        self.asset_noise_lookback_sec = int(cfg.asset_noise_lookback_min) * 60
        self.asset_noise_max_signals = int(cfg.asset_noise_max_signals)
        self.asset_noise_min_opened = int(cfg.asset_noise_min_opened)
        self.asset_noise_cooldown_sec = int(cfg.asset_noise_cooldown_min) * 60
        self._asset_noise_events: Dict[str, deque] = {}
        self._asset_noise_cooldown_until: Dict[str, datetime] = {}

        self.execution_drift_cooldown_enabled = bool(cfg.execution_drift_cooldown_enabled)
        self.execution_drift_max_bps = float(cfg.execution_drift_max_bps)
        self.execution_drift_cooldown_sec = int(float(cfg.execution_drift_cooldown_hour) * 3600)
        self._execution_drift_cooldown_until: Dict[str, datetime] = {}

        self.future_maker_open_enabled = bool(cfg.future_maker_open_enabled)
        self.future_maker_open_allowed_tiers: Set[str] = {
            str(t).strip().upper()
            for t in (cfg.future_maker_open_allowed_tiers or [])
            if str(t).strip().upper() in ('A', 'B', 'C')
        }
        self.future_maker_open_ttl_ms = max(int(cfg.future_maker_open_ttl_ms or 0), 0)
        self.future_maker_open_price_offset_bps = float(cfg.future_maker_open_price_offset_bps or 0)
        self.future_maker_open_fallback_ioc_enabled = bool(
            cfg.future_maker_open_fallback_ioc_enabled
        )
        self.future_maker_open_fallback_allowed_tiers: Set[str] = {
            str(t).strip().upper()
            for t in (cfg.future_maker_open_fallback_allowed_tiers or [])
            if str(t).strip().upper() in ('A', 'B', 'C')
        }
        self.future_maker_open_fallback_min_buffer_bps = float(
            cfg.future_maker_open_fallback_min_buffer_bps or 0
        )
        self.future_maker_open_fallback_slippage_bps = float(
            cfg.future_maker_open_fallback_slippage_bps or 0
        )
        self.future_maker_open_spot_hedge_protective_ioc_enabled = bool(
            cfg.future_maker_open_spot_hedge_protective_ioc_enabled
        )

        self.capital_required = cfg.capital_required
        self.capital_max_age_sec = cfg.capital_max_age_sec
        self.capital_gate_leverage = max(float(cfg.capital_gate_leverage or 1.0), 1.0)
        self._account_summary: Optional[Dict] = None
        self._account_summary_ts: float = 0.0
    
    def set_orderbook_managers(self, gate_manager, spot_manager):
        """
        注入 OrderBookManager 引用，供最终风控旁路直接读取单标的盘口。

        Args:
            gate_manager: Gate 期货 OrderBookManager 实例
            spot_manager: Binance 现货 OrderBookManager 实例
        """
        self._gate_manager = gate_manager
        self._spot_manager = spot_manager
        logger.info('OrderBookManager 已注入 TradingExecutor（最终风控旁路就绪）')

    def update_account_capital_status(self, account_summary: Optional[Dict], snapshot_ts: float):
        """更新交易所真实资金缓存，供开仓热路径只读。"""
        self._account_summary = account_summary
        self._account_summary_ts = float(snapshot_ts or 0)

    def update_holding_margin_status(self, positions: List[Dict]):
        """
        更新持仓的 Gate 保证金/维持保证金缓存（由 _margin_status_loop 每 5s 调用一次）

        注意：本方法不再维护 self._holding_count。
        持仓数量计数由 _refresh_holding_count_from_db() 在每轮 check_and_open 开始时
        从 DB 实时刷新，避免 5s 刷新窗口被高频开仓循环（0.5s）穿透。

        Args:
            positions: 已由 calculate_realtime_pnl 和 attach_gate_position_risk 富化的持仓列表
        """
        self._holding_margin_rate.clear()
        for pos in positions:
            if pos.get('status') != 'holding':
                continue
            ba = pos.get('base_asset', '')
            if not ba:
                continue
            margin_rate = pos.get('gate_maintenance_margin_rate')
            if margin_rate is not None:
                margin_rate = float(margin_rate)
                # 同一标的多个仓位时，取最小保证金/维持保证金比例（最危险的）
                if ba not in self._holding_margin_rate or margin_rate < self._holding_margin_rate[ba]:
                    self._holding_margin_rate[ba] = margin_rate

    def _refresh_holding_count_from_db(self):
        """
        从 DB 实时刷新各标的持仓中仓位数量（每轮 check_and_open 开始时调用）。

        设计目的：
        - 消除 _margin_status_loop 5s 刷新与 _open_position_loop 0.5s 检查之间的时间窗口；
        - 消除服务启动后首个 5s 内 _holding_count 为空的窗口（仅靠 cooldown_sec 兜底易失效）；
        - 与 DB 单一真理源一致，避免内存计数与 DB 偏离（如 margin_loop 异常时 _holding_count 残留）。

        热路径开销：单次 SELECT + GROUP BY，在 holding 仓位有限（<<100）时延迟 < 5ms，可接受。
        """
        try:
            sql = """
                SELECT base_asset, COUNT(*) AS cnt
                FROM mi_trade_position
                WHERE status = 'holding'
                GROUP BY base_asset
            """
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
            self._holding_count = {r['base_asset']: int(r['cnt']) for r in rows if r.get('base_asset')}
            self._holding_total_count = sum(self._holding_count.values())
        except Exception as e:
            # 查询失败时保留旧计数（保守策略：宁可拦截过多也不绕过上限）
            logger.error(f"刷新 _holding_count 失败，沿用旧计数: {e}")

    def check_and_open(self, orderbook_rows: List[Dict]) -> List[Dict]:
        """
        检查所有合约并执行开仓
        
        Args:
            orderbook_rows: 合并后的订单簿行(已计算对冲指标)
        
        Returns:
            开仓结果列表
        """
        results = []

        # 每轮开始时从 DB 实时刷新持仓数量计数（DB 作为单一真理源），
        # 避免依赖 5s 间隔的 margin_loop 导致计数滞后被高频检查穿透。
        self._refresh_holding_count_from_db()
        exchange_risk_blocked_assets = self._load_exchange_risk_blocked_assets()

        # 启动后首次进入：一次性从 DB 加载所有标的的最近一次成功开仓时间，
        # 之后冷却检查全走内存（无外部插入订单的前提下，单一写入路径在 check_and_open 自身）。
        self._load_open_cooldown_from_db()

        for row in orderbook_rows:
            try:
                base_asset = row.get('base_asset', '')

                if str(base_asset or '').upper() in exchange_risk_blocked_assets:
                    reason = '交易所仓位风险(desynced)暂停开仓'
                    self._resolve_signal(base_asset, 'conditions_lost', reason)
                    self._peak_state.pop(base_asset, None)
                    self._open_resiliency.clear(base_asset)
                    continue
                
                # 0. 数据完整性检查：缺少有效盘口数据时跳过
                if row.get('spot_qty') is None or row.get('open_vwap_basis_bps') is None:
                    # 数据不完整时清除峰值状态，避免数据恢复后误触发超时开仓
                    self._resolve_signal(base_asset, 'conditions_lost', '数据不完整(盘口中断)')
                    self._peak_state.pop(base_asset, None)
                    self._open_resiliency.clear(base_asset)
                    continue

                open_vwap_basis = float(row.get('open_vwap_basis_bps'))
                self._annotate_entry_snapshot(row, open_vwap_basis)
                self._annotate_funding_carry_candidate(row, open_vwap_basis)

                # 这些冷却只拦截“新一轮信号”，不打断已经进入状态机的信号。
                if base_asset not in self._peak_state:
                    if not self._pass_peak_timeout_cooldown(base_asset):
                        continue
                    if not self._pass_asset_noise_cooldown(base_asset):
                        continue
                    if not self._pass_rebound_timeout_cooldown(base_asset, open_vwap_basis):
                        continue
                    if not self._pass_execution_drift_cooldown(base_asset):
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
                    self._open_resiliency.clear(base_asset)
                    continue
                                
                # 2. 冷却检查
                if not self._pass_cooldown_check(base_asset):
                    continue
                                
                # 2.5 拒单冷却检查（被交易所拒单后暂停该标开仓）
                if not self._pass_reject_cooldown(base_asset):
                    continue
                                
                # 3. 峰值回落 + sustain 确认（单通道）
                self._annotate_resiliency_row(row)
                self._record_momentum_sample(base_asset, open_vwap_basis)
                funding_carry_ready = self._pass_funding_carry_check(base_asset, open_vwap_basis, row)
                if row.pop('_funding_carry_realtime_rejected', False):
                    continue
                momentum_ready = (
                    False if funding_carry_ready
                    else self._pass_momentum_check(base_asset, open_vwap_basis, row)
                )

                if funding_carry_ready:
                    if not self._pass_open_resiliency_check(base_asset, row, open_vwap_basis):
                        continue
                elif not momentum_ready:
                    self._open_resiliency.observe_shock(base_asset, row)
                    if not self._pass_peak_check(base_asset, open_vwap_basis, row):
                        if base_asset not in self._peak_state:
                            self._open_resiliency.clear(base_asset)
                        continue

                    # 3.2 盘口恢复确认：pullback 通过后进入 RESILIENCY_WAIT，
                    # 后续循环持续采样恢复质量，不再反复要求 pullback 条件成立。
                    if not self._pass_open_resiliency_check(base_asset, row, open_vwap_basis):
                        continue

                    # 3.3 回调通道不在盘口恢复后立刻开仓，而是等待基差二次上行突破。
                    # A 级若没命中 momentum，也必须走 rebound；B 级只走 rebound。
                    if not self._pass_rebound_check(base_asset, open_vwap_basis, row):
                        continue
                                
                # 3.5 最终风控旁路：单标的最短链路重新校验（拦截信号过期场景）
                contract = row.get('contract', '')
                symbol = row.get('symbol', '')
                gate_passed, gate_row, gate_basis, gate_reason = self._pre_execution_gate(
                    base_asset, contract, symbol
                )
                if not gate_passed:
                    # 最终风控拦截，信号标记为 gate_rejected
                    peak_state = self._peak_state.get(base_asset, {})
                    self._resolve_signal(
                        base_asset, 'gate_rejected', gate_reason,
                        exit_basis_bps=gate_basis,
                        trigger_type=peak_state.get('trigger')
                    )
                    self._maybe_start_peak_timeout_cooldown(base_asset, open_vwap_basis, gate_reason)
                    self._peak_state.pop(base_asset, None)
                    self._open_resiliency.clear(base_asset)
                    logger.info(
                        f"最终风控旁路拦截 | {base_asset} | "
                        f"gate_basis={gate_basis}bps | 原因: {gate_reason}"
                    )
                    continue

                # 使用旁路返回的最新数据（单标的最短链路）
                if gate_row is not None:
                    row = gate_row
                    if row.get('funding_rate_24h') is None:
                        row['funding_rate_24h'] = self.contract_meta.get(
                            base_asset, {}
                        ).get('funding_rate_24h')
                if gate_basis is not None:
                    open_vwap_basis = gate_basis
                peak_state = self._peak_state.get(base_asset, {})
                if peak_state.get('trigger') == 'funding_carry':
                    self._apply_entry_snapshot_to_row(row, peak_state.get('entry_snapshot') or {})
                else:
                    self._annotate_entry_snapshot(row, open_vwap_basis)
                signal_basis = peak_state.get('signal_basis_bps')
                row['signal_basis_bps'] = signal_basis
                row['pre_gate_basis_bps'] = open_vwap_basis

                guard_passed, guard_reason = self._pass_execution_guard(
                    base_asset, open_vwap_basis, peak_state
                )
                if not guard_passed:
                    self._resolve_signal(
                        base_asset, 'gate_rejected', guard_reason,
                        exit_basis_bps=open_vwap_basis,
                        trigger_type=peak_state.get('trigger'),
                        signal_basis_bps=signal_basis,
                        pre_gate_basis_bps=open_vwap_basis,
                    )
                    self._maybe_start_peak_timeout_cooldown(base_asset, open_vwap_basis, guard_reason)
                    self._peak_state.pop(base_asset, None)
                    self._open_resiliency.clear(base_asset)
                    logger.info(
                        f"执行质量保护拦截 | {base_asset} | "
                        f"pre_gate_basis={open_vwap_basis:.2f}bps | 原因: {guard_reason}"
                    )
                    continue
                
                # 4. 构建开仓原因（用于复盘）
                open_reason = self._build_open_reason(row, base_asset, open_vwap_basis)
                
                # 5. 生成订单组
                order_group = self._create_order_group(row)
                order_group['open_reason'] = open_reason
                
                # 6. 调用成交引擎服务（虚拟/实盘）
                exec_result = self.executor_client.execute(order_group, row)
                
                # 7. 持久化订单
                self._save_orders(order_group, exec_result)
                self._maybe_start_execution_drift_cooldown(base_asset, order_group)
                
                # 8. 更新信号状态
                peak_state = self._peak_state.get(base_asset, {})
                trigger_type = peak_state.get('trigger')
                if exec_result['success']:
                    self._resolve_signal(
                        base_asset, 'opened', order_group.get('open_reason'),
                        exit_basis_bps=open_vwap_basis,
                        trigger_type=trigger_type,
                        order_uuid=order_group['order_uuid'],
                        signal_basis_bps=order_group.get('signal_basis_bps'),
                        pre_gate_basis_bps=order_group.get('pre_gate_basis_bps'),
                        actual_basis_bps=order_group.get('actual_basis_bps'),
                    )
                else:
                    self._resolve_signal(
                        base_asset, 'rejected', exec_result.get('message'),
                        exit_basis_bps=open_vwap_basis,
                        trigger_type=trigger_type,
                        signal_basis_bps=order_group.get('signal_basis_bps'),
                        pre_gate_basis_bps=order_group.get('pre_gate_basis_bps'),
                    )
                    self._maybe_start_peak_timeout_cooldown(
                        base_asset, open_vwap_basis, exec_result.get('message')
                    )
                    # 被交易所拒单后启动冷却，避免重复提交失败订单
                    self._reject_cooldown_until[base_asset] = datetime.now() + timedelta(seconds=self.reject_cooldown_sec)
                    logger.info(
                        f"开仓拒单冷却启动 | {base_asset} | "
                        f"冷却{self.reject_cooldown_sec}s | 原因: {exec_result.get('message', '')[:80]}"
                    )
                
                # 开仓后清除峰值状态（pullback / timeout 等通道共用）
                self._peak_state.pop(base_asset, None)
                self._open_resiliency.clear(base_asset)
                
                results.append({
                    'base_asset': base_asset,
                    'success': exec_result['success'],
                    'order_uuid': order_group['order_uuid'],
                    'message': exec_result.get('message')
                })
                
                if exec_result['success']:
                    # 立即递增持仓计数 + 写入冷却时间，避免下一轮（0.5s后）穿透上限/冷却检查
                    self._holding_count[base_asset] = self._holding_count.get(base_asset, 0) + 1
                    self._holding_total_count += 1
                    self._last_open_time[base_asset] = datetime.now()
                    logger.info(
                        f"开仓成功 | {base_asset} | "
                        f"spot_vwap={exec_result['spot_order']['exec_price']} | "
                        f"future_vwap={exec_result['future_order']['exec_price']} | "
                        f"holding_count={self._holding_count[base_asset]}/{self.max_positions_per_asset} | "
                        f"total_holding={self._holding_total_count}/{self.max_total_positions or '∞'}"
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

    def _load_exchange_risk_blocked_assets(self) -> Set[str]:
        """仍处于交易所断腿风险的资产禁止新增开仓。"""
        sql = """
            SELECT DISTINCT UPPER(base_asset) AS base_asset
            FROM mi_trade_position
            WHERE status = 'holding'
              AND exchange_risk_status = 'desynced'
        """
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql)
                return {
                    str(row.get('base_asset') or '').upper()
                    for row in cursor.fetchall()
                    if row.get('base_asset')
                }
        except Exception as e:
            # 兼容尚未执行迁移的环境；迁移部署后会自动生效。
            logger.warning(f"读取交易所风险资产失败，跳过开仓风险资产过滤: {e}")
            return set()

    def _annotate_resiliency_row(self, row: Dict) -> None:
        """Attach per-asset contract multiplier for shared depth calculations."""
        base_asset = row.get('base_asset', '')
        row['_future_qty_multiplier'] = self._get_quanto_multiplier(base_asset)

    def _record_momentum_sample(self, base_asset: str, basis_bps: float) -> None:
        now = datetime.now()
        samples = self._momentum_samples.setdefault(base_asset, deque())
        samples.append((now, basis_bps))
        cutoff = now - timedelta(seconds=self.momentum_window_sec)
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def _asset_tier(self, base_asset: str) -> str:
        return self.asset_tier_meta.get((base_asset or '').strip().upper(), 'C')

    def _funding_24h_bps(self, base_asset: str, row: Optional[Dict] = None) -> float:
        row = row or {}
        funding_rate = row.get('funding_rate_24h')
        if funding_rate is None and base_asset in self.contract_meta:
            funding_rate = self.contract_meta[base_asset].get('funding_rate_24h')
        if funding_rate is None:
            return 0.0
        return float(funding_rate) * 10000.0

    def _parse_datetime(self, value) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                continue
        return None

    def _time_to_next_funding_min(self, base_asset: str, row: Optional[Dict] = None) -> Optional[float]:
        row = row or {}
        next_at = row.get('funding_next_apply')
        if next_at is None and base_asset in self.contract_meta:
            next_at = self.contract_meta[base_asset].get('funding_next_apply')
        parsed = self._parse_datetime(next_at)
        if not parsed:
            return None
        return (parsed - datetime.now()).total_seconds() / 60.0

    def _open_close_p20_floor(self, base_asset: str) -> Optional[float]:
        values = []
        threshold_data = self.vwap_threshold_meta.get(base_asset, {})
        if threshold_data.get('p20') is not None:
            values.append(float(threshold_data['p20']))
        close_data = self.close_vwap_threshold_meta.get(base_asset, {})
        close_p20 = close_data.get(self.close_threshold_col)
        if close_p20 is None:
            close_p20 = close_data.get('close_basis_p20')
        if close_p20 is not None:
            values.append(float(close_p20))
        return max(values) if values else None

    def _funding_carry_amount(self) -> float:
        return self.funding_carry_amount_usdt if self.funding_carry_amount_usdt > 0 else self.open_amount_usdt

    def _active_open_amount_usdt(self, row: Optional[Dict] = None) -> float:
        if row and row.get('open_amount_usdt') is not None:
            return float(row.get('open_amount_usdt'))
        return self.open_amount_usdt

    def _funding_carry_snapshot(
        self, base_asset: str, basis_bps: float, row: Optional[Dict] = None
    ) -> Optional[Dict]:
        if not self.funding_carry_enabled:
            return None
        tier = self._asset_tier(base_asset)
        if tier not in self.funding_carry_allowed_tiers:
            return None

        funding_24h_bps = self._funding_24h_bps(base_asset, row)
        if funding_24h_bps < self.funding_carry_min_24h_bps:
            return None

        next_min = self._time_to_next_funding_min(base_asset, row)
        if next_min is None or next_min < 0 or next_min > self.funding_carry_max_next_funding_min:
            return None

        p20_floor = self._open_close_p20_floor(base_asset)
        if p20_floor is None:
            return None

        entry_floor = p20_floor - self.funding_carry_basis_relax_bps
        if float(basis_bps) < entry_floor:
            return None

        expected_funding_bps = funding_24h_bps * self.funding_entry_capture_ratio
        expected_edge_bps = (
            float(basis_bps)
            + expected_funding_bps
            - self.fee_cost_bps
            - self.funding_entry_slippage_buffer_bps
        )
        open_p20 = self.vwap_threshold_meta.get(base_asset, {}).get('p20', self.basis_threshold_bps)
        return {
            'p20_bps': round(float(open_p20), 4),
            'entry_floor_bps': round(entry_floor, 4),
            'funding_24h_bps': round(funding_24h_bps, 4),
            'expected_funding_bps': round(expected_funding_bps, 4),
            'carry_floor_bps': round(entry_floor, 4),
            'timing_floor_bps': round(entry_floor, 4),
            'funding_discount_bps': round(self.funding_carry_basis_relax_bps, 4),
            'expected_edge_bps': round(expected_edge_bps, 4),
            'funding_carry': True,
            'funding_carry_p20_floor_bps': round(p20_floor, 4),
            'funding_carry_next_min': round(next_min, 4),
            'funding_carry_tier': tier,
            'funding_carry_amount_usdt': round(self._funding_carry_amount(), 4),
        }

    def _apply_entry_snapshot_to_row(self, row: Dict, snapshot: Dict) -> None:
        for key, value in snapshot.items():
            row[f'_entry_{key}'] = value

    def _annotate_funding_carry_candidate(self, row: Dict, basis_bps: float) -> Optional[Dict]:
        base_asset = row.get('base_asset', '')
        snapshot = self._funding_carry_snapshot(base_asset, basis_bps, row)
        if not snapshot:
            row.pop('_funding_carry_candidate', None)
            return None
        row['_funding_carry_candidate'] = True
        row['open_amount_usdt'] = self._funding_carry_amount()
        self._apply_entry_snapshot_to_row(row, snapshot)
        return snapshot

    def _clear_funding_carry_candidate(self, row: Dict, basis_bps: float) -> None:
        row.pop('_funding_carry_candidate', None)
        row.pop('open_amount_usdt', None)
        self._annotate_entry_snapshot(row, basis_bps)

    def _entry_snapshot(self, base_asset: str, basis_bps: float, row: Optional[Dict] = None) -> Dict:
        """
        统一开仓门槛。

        p20 只是历史位置参考；真正的 entry_floor 同时考虑 funding、手续费、
        滑点缓冲和一个有上限的 funding 折扣，避免高 funding 标的被放到太差位置。
        """
        threshold_data = self.vwap_threshold_meta.get(base_asset, {})
        p20 = float(threshold_data.get('p20', self.basis_threshold_bps))
        funding_24h_bps = self._funding_24h_bps(base_asset, row)

        if not self.funding_entry_enabled:
            entry_floor = p20
            expected_funding_bps = 0.0
            carry_floor = p20
            timing_floor = p20
            discount_bps = 0.0
        else:
            expected_funding_bps = funding_24h_bps * self.funding_entry_capture_ratio
            carry_floor = (
                self.funding_entry_min_expected_edge_bps
                + self.fee_cost_bps
                + self.funding_entry_slippage_buffer_bps
                - expected_funding_bps
            )
            discount_bps = min(
                self.funding_entry_max_discount_bps,
                max(0.0, funding_24h_bps) * self.funding_entry_discount_ratio,
            )
            timing_floor = p20 - discount_bps
            entry_floor = max(carry_floor, timing_floor)

            # funding 不够厚时，不允许因为历史 p20 很低而提前做 carry。
            if funding_24h_bps < self.funding_entry_strong_funding_24h_bps:
                entry_floor = max(entry_floor, p20)

        expected_edge_bps = (
            basis_bps
            + expected_funding_bps
            - self.fee_cost_bps
            - self.funding_entry_slippage_buffer_bps
        )
        return {
            'p20_bps': round(p20, 4),
            'entry_floor_bps': round(entry_floor, 4),
            'funding_24h_bps': round(funding_24h_bps, 4),
            'expected_funding_bps': round(expected_funding_bps, 4),
            'carry_floor_bps': round(carry_floor, 4),
            'timing_floor_bps': round(timing_floor, 4),
            'funding_discount_bps': round(discount_bps, 4),
            'expected_edge_bps': round(expected_edge_bps, 4),
        }

    def _annotate_entry_snapshot(self, row: Dict, basis_bps: Optional[float] = None) -> Dict:
        base_asset = row.get('base_asset', '')
        if basis_bps is None:
            basis_bps = float(row.get('open_vwap_basis_bps') or 0)
        snapshot = self._entry_snapshot(base_asset, float(basis_bps), row)
        self._apply_entry_snapshot_to_row(row, snapshot)
        return snapshot

    def _state_entry_snapshot(self, base_asset: str, row: Optional[Dict] = None,
                              basis_bps: Optional[float] = None) -> Dict:
        state = self._peak_state.get(base_asset) or {}
        snapshot = state.get('entry_snapshot')
        if snapshot:
            return snapshot
        if row and '_entry_entry_floor_bps' in row:
            return {
                'p20_bps': row.get('_entry_p20_bps'),
                'entry_floor_bps': row.get('_entry_entry_floor_bps'),
                'funding_24h_bps': row.get('_entry_funding_24h_bps'),
                'expected_funding_bps': row.get('_entry_expected_funding_bps'),
                'carry_floor_bps': row.get('_entry_carry_floor_bps'),
                'timing_floor_bps': row.get('_entry_timing_floor_bps'),
                'funding_discount_bps': row.get('_entry_funding_discount_bps'),
                'expected_edge_bps': row.get('_entry_expected_edge_bps'),
                'funding_carry': row.get('_entry_funding_carry'),
                'funding_carry_p20_floor_bps': row.get('_entry_funding_carry_p20_floor_bps'),
                'funding_carry_next_min': row.get('_entry_funding_carry_next_min'),
                'funding_carry_tier': row.get('_entry_funding_carry_tier'),
                'funding_carry_amount_usdt': row.get('_entry_funding_carry_amount_usdt'),
            }
        if basis_bps is None:
            basis_bps = float((row or {}).get('open_vwap_basis_bps') or 0)
        return self._entry_snapshot(base_asset, float(basis_bps), row)

    def _entry_floor_bps(self, base_asset: str, row: Optional[Dict] = None,
                         basis_bps: Optional[float] = None) -> float:
        return float(self._state_entry_snapshot(base_asset, row, basis_bps).get('entry_floor_bps'))

    def _format_entry_snapshot(self, snapshot: Dict) -> str:
        return (
            f"entry_floor={float(snapshot.get('entry_floor_bps', 0)):.1f},"
            f"p20={float(snapshot.get('p20_bps', 0)):.1f},"
            f"funding24h={float(snapshot.get('funding_24h_bps', 0)):.1f},"
            f"edge={float(snapshot.get('expected_edge_bps', 0)):.1f}"
        )

    def _pass_rebound_timeout_cooldown(self, base_asset: str, basis_bps: float) -> bool:
        if not self.rebound_timeout_cooldown_enabled:
            return True
        cooldown = self._rebound_timeout_cooldown.get(base_asset)
        if not cooldown:
            return True
        now = datetime.now()
        if now >= cooldown.get('until', now):
            self._rebound_timeout_cooldown.pop(base_asset, None)
            return True
        anchor_basis = float(cooldown.get('basis_bps', basis_bps))
        if abs(float(basis_bps) - anchor_basis) >= self.rebound_timeout_basis_change_reset_bps:
            self._rebound_timeout_cooldown.pop(base_asset, None)
            logger.info(
                f"回弹超时冷却解除 | {base_asset} | "
                f"basis变化{abs(float(basis_bps) - anchor_basis):.1f}bps"
            )
            return True
        return False

    def _pass_peak_timeout_cooldown(self, base_asset: str) -> bool:
        cooldown_until = self._peak_timeout_cooldown.get(base_asset)
        if cooldown_until is None:
            return True
        if datetime.now() >= cooldown_until:
            self._peak_timeout_cooldown.pop(base_asset, None)
            logger.info(f"峰值超时直开冷却解除 | {base_asset}")
            return True
        return False

    def _maybe_start_peak_timeout_cooldown(
        self,
        base_asset: str,
        basis_bps: Optional[float],
        reason: Optional[str] = None,
    ) -> None:
        state = self._peak_state.get(base_asset) or {}
        if state.get('trigger') != 'timeout':
            return
        if self.peak_timeout_cooldown_sec <= 0:
            return
        until = datetime.now() + timedelta(seconds=self.peak_timeout_cooldown_sec)
        self._peak_timeout_cooldown[base_asset] = until
        basis_text = 'NA' if basis_bps is None else f'{float(basis_bps):.1f}bps'
        logger.info(
            f"峰值超时直开冷却启动 | {base_asset} | "
            f"cooldown={self.peak_timeout_cooldown_sec}s | basis={basis_text} | "
            f"reason={(reason or '')[:80]}"
        )

    def _start_rebound_timeout_cooldown(self, base_asset: str, basis_bps: float) -> None:
        if not self.rebound_timeout_cooldown_enabled:
            return
        until = datetime.now() + timedelta(seconds=self.rebound_timeout_cooldown_sec)
        self._rebound_timeout_cooldown[base_asset] = {
            'until': until,
            'basis_bps': float(basis_bps),
        }
        logger.info(
            f"回弹超时冷却启动 | {base_asset} | "
            f"cooldown={self.rebound_timeout_cooldown_sec}s | basis={float(basis_bps):.1f}bps"
        )

    def _pass_asset_noise_cooldown(self, base_asset: str) -> bool:
        if not self.asset_noise_cooldown_enabled:
            return True
        until = self._asset_noise_cooldown_until.get(base_asset)
        if not until:
            return True
        if datetime.now() >= until:
            self._asset_noise_cooldown_until.pop(base_asset, None)
            logger.info(f"信号降噪冷却解除 | {base_asset}")
            return True
        return False

    def _record_signal_noise_event(self, base_asset: str, status: str) -> None:
        if not self.asset_noise_cooldown_enabled or not base_asset:
            return
        now = datetime.now()
        events = self._asset_noise_events.setdefault(base_asset, deque())
        events.append((now, status))
        cutoff = now - timedelta(seconds=self.asset_noise_lookback_sec)
        while events and events[0][0] < cutoff:
            events.popleft()

        total = len(events)
        opened = sum(1 for _, s in events if s == 'opened')
        if total >= self.asset_noise_max_signals and opened < self.asset_noise_min_opened:
            until = now + timedelta(seconds=self.asset_noise_cooldown_sec)
            prev_until = self._asset_noise_cooldown_until.get(base_asset)
            if prev_until is None or until > prev_until:
                self._asset_noise_cooldown_until[base_asset] = until
                logger.info(
                    f"信号降噪冷却启动 | {base_asset} | "
                    f"近{self.asset_noise_lookback_sec // 60}min信号{total}个/"
                    f"开仓{opened}个 | 冷却{self.asset_noise_cooldown_sec // 60}min"
                )

    def _pass_execution_drift_cooldown(self, base_asset: str) -> bool:
        if not self.execution_drift_cooldown_enabled:
            return True
        until = self._execution_drift_cooldown_until.get(base_asset)
        if not until:
            return True
        if datetime.now() >= until:
            self._execution_drift_cooldown_until.pop(base_asset, None)
            logger.info(f"执行漂移冷却解除 | {base_asset}")
            return True
        return False

    def _maybe_start_execution_drift_cooldown(self, base_asset: str, order_group: Dict) -> None:
        if not self.execution_drift_cooldown_enabled:
            return
        pre_gate = order_group.get('pre_gate_basis_bps')
        actual = order_group.get('actual_basis_bps')
        if pre_gate is None or actual is None:
            return
        drift_bps = float(pre_gate) - float(actual)
        if drift_bps <= self.execution_drift_max_bps:
            return
        until = datetime.now() + timedelta(seconds=self.execution_drift_cooldown_sec)
        self._execution_drift_cooldown_until[base_asset] = until
        logger.warning(
            f"执行漂移冷却启动 | {base_asset} | "
            f"pre_gate={float(pre_gate):.1f}bps actual={float(actual):.1f}bps "
            f"drift={drift_bps:.1f}>{self.execution_drift_max_bps:.1f}bps | "
            f"cooldown={self.execution_drift_cooldown_sec // 3600:.1f}h"
        )

    def _momentum_params_for_tier(self, tier: str) -> Dict[str, float]:
        override = self.momentum_tier_overrides.get((tier or '').strip().upper(), {})
        return {
            'window_sec': float(override.get('window_sec', self.momentum_window_sec)),
            'min_samples': int(override.get('min_samples', self.momentum_min_samples)),
            'min_rise_bps': float(override.get('min_rise_bps', self.momentum_min_rise_bps)),
            'min_basis_buffer_bps': float(
                override.get('min_basis_buffer_bps', self.momentum_min_basis_buffer_bps)
            ),
            'safety_bps': float(override.get('safety_bps', self.momentum_safety_bps)),
        }

    def _pass_funding_carry_check(self, base_asset: str, current_basis_bps: float, row: Dict) -> bool:
        """高资金费 carry 通道：不等触顶回调，临近结算且 basis 只差一小段时直接进入恢复确认。"""
        state = self._peak_state.get(base_asset)
        if state and state.get('trigger') == 'funding_carry':
            return True
        if state:
            return False

        snapshot = self._funding_carry_snapshot(base_asset, current_basis_bps, row)
        if not snapshot:
            return False

        contract = row.get('contract', '')
        if not self._verify_realtime_funding_rate(base_asset, contract):
            self._clear_funding_carry_candidate(row, current_basis_bps)
            if not self._pass_risk_check(row):
                row['_funding_carry_realtime_rejected'] = True
            return False
        if self.executor_client.channel == 'Live' and base_asset not in self._last_realtime_funding_info:
            self._clear_funding_carry_candidate(row, current_basis_bps)
            if not self._pass_risk_check(row):
                row['_funding_carry_realtime_rejected'] = True
            logger.info(f"Funding Carry 实时费率不可用，放弃本轮 carry | {base_asset}")
            return False
        self._apply_realtime_funding_info(base_asset, row)

        snapshot = self._funding_carry_snapshot(base_asset, current_basis_bps, row)
        if not snapshot:
            self._clear_funding_carry_candidate(row, current_basis_bps)
            if not self._pass_risk_check(row):
                row['_funding_carry_realtime_rejected'] = True
            return False
        row['_funding_carry_candidate'] = True
        row['open_amount_usdt'] = self._funding_carry_amount()
        self._apply_entry_snapshot_to_row(row, snapshot)

        signal_id = self._create_signal(base_asset, current_basis_bps)
        now = datetime.now()
        self._peak_state[base_asset] = {
            'peak_bps': current_basis_bps,
            'start_time': now,
            'trigger': 'funding_carry',
            'signal_id': signal_id,
            'signal_basis_bps': current_basis_bps,
            'strategy_tier': snapshot.get('funding_carry_tier'),
            'entry_snapshot': snapshot,
            'resiliency_active': True,
            'resiliency_start_time': now,
        }
        self._open_resiliency.observe_shock(base_asset, row, now)
        logger.info(
            f"Funding Carry 开仓候选 | {base_asset} | "
            f"tier={snapshot.get('funding_carry_tier')} | "
            f"basis={current_basis_bps:.2f}bps≥floor={float(snapshot['entry_floor_bps']):.2f} | "
            f"p20_floor={float(snapshot['funding_carry_p20_floor_bps']):.2f} | "
            f"funding24h={float(snapshot['funding_24h_bps']):.2f}bps | "
            f"next_in={float(snapshot['funding_carry_next_min']):.1f}min | "
            f"amount={float(snapshot['funding_carry_amount_usdt']):.2f}USDT"
        )
        return True

    def _pass_momentum_check(self, base_asset: str, current_basis_bps: float, row: Dict) -> bool:
        """
        动量开仓通道：当基差已经明显超过阈值，并且短窗口内继续上行，
        且盘口覆盖和 entry_floor 仍然健康时，直接进入最终旁路，不等待回调。
        """
        if not self.momentum_enabled:
            return False
        tier = self._asset_tier(base_asset)
        if tier not in self.momentum_allowed_tiers:
            return False
        if base_asset in self._peak_state:
            return False

        samples = self._momentum_samples.get(base_asset)
        params = self._momentum_params_for_tier(tier)
        now = datetime.now()
        cutoff = now - timedelta(seconds=params['window_sec'])
        recent_samples = [(t, b) for t, b in (samples or []) if t >= cutoff]
        if len(recent_samples) < params['min_samples']:
            return False

        contract = row.get('contract', '')
        if not self._verify_realtime_funding_rate(base_asset, contract):
            return False
        self._apply_realtime_funding_info(base_asset, row)

        entry_snapshot = self._state_entry_snapshot(base_asset, row, current_basis_bps)
        entry_floor = float(entry_snapshot.get('entry_floor_bps'))
        min_entry_basis = entry_floor + params['min_basis_buffer_bps']
        if current_basis_bps < min_entry_basis:
            return False

        first_basis = float(recent_samples[0][1])
        rise_bps = current_basis_bps - first_basis
        if rise_bps < params['min_rise_bps']:
            return False

        recent_values = [float(v) for _, v in recent_samples]
        if current_basis_bps < max(recent_values) - 0.5:
            return False

        open_coverage = row.get('open_coverage')
        if open_coverage is not None and float(open_coverage) > self.coverage_threshold:
            return False

        if not self.funding_entry_enabled:
            close_data = self.close_vwap_threshold_meta.get(base_asset)
            if not close_data:
                return False
            close_thr = close_data.get(self.close_threshold_col)
            if close_thr is None:
                return False
            min_profit_basis = float(close_thr) + self.fee_cost_bps + params['safety_bps']
            if current_basis_bps <= min_profit_basis:
                return False
        else:
            min_profit_basis = min_entry_basis

        now = datetime.now()
        signal_id = self._create_signal(base_asset, current_basis_bps)
        self._peak_state[base_asset] = {
            'peak_bps': current_basis_bps,
            'start_time': now,
            'trigger': 'momentum',
            'signal_id': signal_id,
            'signal_basis_bps': current_basis_bps,
            'momentum_rise_bps': rise_bps,
            'strategy_tier': tier,
            'momentum_window_sec': params['window_sec'],
            'momentum_min_basis_buffer_bps': params['min_basis_buffer_bps'],
            'entry_snapshot': entry_snapshot,
        }
        logger.info(
            f"动量开仓确认 | {base_asset} | "
            f"tier={tier} | "
            f"basis={current_basis_bps:.2f}bps≥entry_floor+buffer={min_entry_basis:.2f} | "
            f"rise={rise_bps:.2f}bps/{params['window_sec']:.1f}s | "
            f"coverage={open_coverage} | guard>{min_profit_basis:.2f}"
        )
        return True

    def _pass_open_resiliency_check(self, base_asset: str, row: Dict, open_vwap_basis: float) -> bool:
        min_basis = self._entry_floor_bps(base_asset, row, open_vwap_basis)
        result = self._open_resiliency.check(
            base_asset,
            row,
            basis_bps=open_vwap_basis,
            coverage_threshold=self.coverage_threshold,
            min_basis_bps=float(min_basis) if min_basis is not None else None,
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
            self._last_resiliency_metrics[base_asset] = dict(m)
            logger.info(f"开仓盘口恢复通过 | {base_asset} | {metric_text}")
            return True
        if result.terminal:
            state = self._peak_state.get(base_asset) or {}
            trigger_type = state.get('trigger') or 'pullback'
            self._resolve_signal(
                base_asset,
                'gate_rejected',
                f'resiliency:{result.reason}|{metric_text}',
                exit_basis_bps=open_vwap_basis,
                trigger_type=trigger_type,
            )
            self._maybe_start_peak_timeout_cooldown(
                base_asset, open_vwap_basis, f'resiliency:{result.reason}'
            )
            self._peak_state.pop(base_asset, None)
            self._open_resiliency.clear(base_asset)
            logger.info(f"开仓盘口恢复终止 | {base_asset} | reason={result.reason} | {metric_text}")
            return False
        logger.info(f"开仓盘口恢复等待 | {base_asset} | reason={result.reason} | {metric_text}")
        return False

    def _pass_rebound_check(self, base_asset: str, current_basis_bps: float, row: Dict) -> bool:
        """
        回调 + 盘口恢复后，等待基差再次向上突破再入场。

        这只改变 pullback 通道；momentum 通道已经代表“上升期直接入场”，不经过这里。
        """
        if not self.rebound_enabled:
            return True

        tier = self._asset_tier(base_asset)
        state = self._peak_state.get(base_asset)
        if not state or state.get('trigger') != 'pullback':
            return True

        if tier not in self.rebound_allowed_tiers:
            self._resolve_signal(
                base_asset,
                'gate_rejected',
                f'分层不允许pullback直开(tier={tier}, allowed_rebound={sorted(self.rebound_allowed_tiers)})',
                exit_basis_bps=current_basis_bps,
                trigger_type='pullback',
            )
            self._peak_state.pop(base_asset, None)
            self._open_resiliency.clear(base_asset)
            logger.info(
                f"回调通道分层拦截 | {base_asset} | tier={tier} | "
                f"allowed_rebound={sorted(self.rebound_allowed_tiers)}"
            )
            return False

        now = datetime.now()
        entry_snapshot = self._state_entry_snapshot(base_asset, row, current_basis_bps)
        entry_floor = float(entry_snapshot.get('entry_floor_bps'))
        min_basis = entry_floor + self.rebound_min_basis_buffer_bps
        strong_basis = entry_floor + self.rebound_strong_cushion_bps

        if current_basis_bps < entry_floor:
            self._resolve_signal(
                base_asset,
                'conditions_lost',
                f'回弹等待中基差跌回入场门槛下({current_basis_bps:.1f}<entry_floor={entry_floor:.1f}bps)',
                exit_basis_bps=current_basis_bps,
                trigger_type='pullback',
            )
            self._peak_state.pop(base_asset, None)
            self._open_resiliency.clear(base_asset)
            return False

        if not state.get('rebound_active'):
            state['rebound_active'] = True
            state['rebound_start_time'] = now
            state['rebound_floor_bps'] = current_basis_bps
            state['rebound_last_basis_bps'] = current_basis_bps
            if current_basis_bps >= strong_basis:
                state['rebound_strong_cushion_start_time'] = now
            logger.info(
                f"回调后再突破等待开始 | {base_asset} | tier={tier} | "
                f"floor={current_basis_bps:.2f}bps | "
                f"需回升{self.rebound_min_rise_bps:.1f}bps且≥entry_floor+buffer={min_basis:.2f}bps | "
                f"强安全垫≥entry_floor+{self.rebound_strong_cushion_bps:.1f}bps后持有"
                f"{self.rebound_strong_cushion_min_hold_sec:.1f}s"
            )
            return False

        floor_bps = float(state.get('rebound_floor_bps', current_basis_bps))
        last_basis = float(state.get('rebound_last_basis_bps', current_basis_bps))
        if current_basis_bps < floor_bps:
            floor_bps = current_basis_bps
            state['rebound_floor_bps'] = floor_bps

        waited_sec = (now - state.get('rebound_start_time', now)).total_seconds()
        rise_from_floor = current_basis_bps - floor_bps
        slope_bps = current_basis_bps - last_basis
        strong_cushion_bps = current_basis_bps - entry_floor
        strong_active = current_basis_bps >= strong_basis
        strong_hold_sec = 0.0
        if strong_active:
            strong_start = state.get('rebound_strong_cushion_start_time')
            if not strong_start:
                strong_start = now
                state['rebound_strong_cushion_start_time'] = strong_start
            strong_hold_sec = (now - strong_start).total_seconds()
        else:
            state.pop('rebound_strong_cushion_start_time', None)

        if (
            strong_active
            and strong_hold_sec >= self.rebound_strong_cushion_min_hold_sec
            and current_basis_bps >= min_basis
        ):
            state['trigger'] = REBOUND_STRONG_TRIGGER
            state['rebound_rise_bps'] = rise_from_floor
            state['rebound_floor_bps'] = floor_bps
            state['rebound_strong_cushion_bps'] = strong_cushion_bps
            state['rebound_strong_hold_sec'] = strong_hold_sec
            logger.info(
                f"回调后强安全垫确认 | {base_asset} | tier={tier} | "
                f"floor={floor_bps:.2f}bps -> current={current_basis_bps:.2f}bps | "
                f"cushion={strong_cushion_bps:.2f}/{self.rebound_strong_cushion_bps:.1f}bps | "
                f"hold={strong_hold_sec:.1f}/{self.rebound_strong_cushion_min_hold_sec:.1f}s | "
                f"rise={rise_from_floor:.2f}bps | slope={slope_bps:.2f}bps"
            )
            return True

        timeout_sec = (
            self.rebound_strong_cushion_max_wait_sec
            if strong_active
            else self.rebound_max_wait_sec
        )
        if waited_sec > timeout_sec:
            self._resolve_signal(
                base_asset,
                'monitor_timeout',
                (
                    f'回弹等待{waited_sec:.1f}s未确认('
                    f'floor={floor_bps:.1f},current={current_basis_bps:.1f},'
                    f'rise={rise_from_floor:.1f}/{self.rebound_min_rise_bps:.1f}bps,'
                    f'slope={slope_bps:.1f}/{self.rebound_min_slope_bps:.1f}bps,'
                    f'min_basis={min_basis:.1f},'
                    f'entry_floor={entry_floor:.1f}+buffer={self.rebound_min_basis_buffer_bps:.1f},'
                    f'strong_cushion={strong_cushion_bps:.1f}/{self.rebound_strong_cushion_bps:.1f}bps,'
                    f'strong_hold={strong_hold_sec:.1f}/{self.rebound_strong_cushion_min_hold_sec:.1f}s,'
                    f'timeout={waited_sec:.1f}/{timeout_sec:.1f}s)'
                ),
                exit_basis_bps=current_basis_bps,
                trigger_type='rebound_timeout',
            )
            self._peak_state.pop(base_asset, None)
            self._open_resiliency.clear(base_asset)
            self._start_rebound_timeout_cooldown(base_asset, current_basis_bps)
            logger.info(
                f"回调后再突破超时放弃 | {base_asset} | "
                f"floor={floor_bps:.2f} | current={current_basis_bps:.2f} | "
                f"rise={rise_from_floor:.2f}/{self.rebound_min_rise_bps:.1f}bps | "
                f"slope={slope_bps:.2f}/{self.rebound_min_slope_bps:.1f}bps | "
                f"min_basis={min_basis:.2f} | "
                f"strong_cushion={strong_cushion_bps:.2f}/{self.rebound_strong_cushion_bps:.1f}bps | "
                f"strong_hold={strong_hold_sec:.1f}/{self.rebound_strong_cushion_min_hold_sec:.1f}s | "
                f"waited={waited_sec:.1f}/{timeout_sec:.1f}s"
            )
            return False

        state['rebound_last_basis_bps'] = current_basis_bps

        if (
            rise_from_floor >= self.rebound_min_rise_bps
            and slope_bps >= self.rebound_min_slope_bps
            and current_basis_bps >= min_basis
        ):
            state['trigger'] = 'rebound'
            state['rebound_rise_bps'] = rise_from_floor
            state['rebound_floor_bps'] = floor_bps
            logger.info(
                f"回调后再突破确认 | {base_asset} | tier={tier} | "
                f"floor={floor_bps:.2f}bps -> current={current_basis_bps:.2f}bps | "
                f"rise={rise_from_floor:.2f}bps | slope={slope_bps:.2f}bps"
            )
            return True

        logger.info(
            f"回调后再突破等待 | {base_asset} | "
            f"floor={floor_bps:.2f} | current={current_basis_bps:.2f} | "
            f"rise={rise_from_floor:.2f}/{self.rebound_min_rise_bps:.1f}bps | "
            f"slope={slope_bps:.2f}/{self.rebound_min_slope_bps:.1f}bps | "
            f"min_basis={min_basis:.2f} | "
            f"strong_cushion={strong_cushion_bps:.2f}/{self.rebound_strong_cushion_bps:.1f}bps | "
            f"strong_hold={strong_hold_sec:.1f}/{self.rebound_strong_cushion_min_hold_sec:.1f}s"
        )
        return False

    def _pass_execution_guard(self, base_asset: str, pre_gate_basis_bps: float, peak_state: Dict) -> tuple:
        """下单前对 pre-gate 基差加安全垫，过滤回调通道里已经衰减太多的机会。"""
        if not self.execution_guard_enabled:
            return True, ''

        if self.funding_entry_enabled:
            entry_snapshot = (peak_state or {}).get('entry_snapshot') or self._entry_snapshot(
                base_asset, pre_gate_basis_bps
            )
            entry_floor = float(entry_snapshot.get('entry_floor_bps'))
            if (peak_state or {}).get('trigger') == 'funding_carry':
                min_entry_basis = entry_floor
            else:
                min_entry_basis = entry_floor + self.execution_guard_min_p20_buffer_bps
            if pre_gate_basis_bps < min_entry_basis:
                return False, (
                    f'执行保护(pregate={pre_gate_basis_bps:.1f}<'
                    f'entry_floor+buffer={min_entry_basis:.1f})'
                )
        else:
            threshold_data = self.vwap_threshold_meta.get(base_asset, {})
            p20 = threshold_data.get('p20', self.basis_threshold_bps)
            if p20 is not None:
                min_p20_basis = float(p20) + self.execution_guard_min_p20_buffer_bps
                if pre_gate_basis_bps < min_p20_basis:
                    return False, (
                        f'执行保护(pregate={pre_gate_basis_bps:.1f}<='
                        f'p20+buffer={min_p20_basis:.1f})'
                    )

            close_data = self.close_vwap_threshold_meta.get(base_asset)
            if close_data:
                close_thr = close_data.get(self.close_threshold_col)
                if close_thr is not None:
                    min_profit_basis = (
                        float(close_thr)
                        + self.fee_cost_bps
                        + self.execution_guard_min_profit_buffer_bps
                    )
                    if pre_gate_basis_bps <= min_profit_basis:
                        return False, (
                            f'执行保护(pregate={pre_gate_basis_bps:.1f}<='
                            f'close+fee+buffer={min_profit_basis:.1f})'
                        )

        peak_bps = peak_state.get('peak_bps') if peak_state else None
        if peak_bps is not None:
            decay_bps = float(peak_bps) - pre_gate_basis_bps
            if decay_bps > self.execution_guard_max_peak_decay_bps:
                return False, (
                    f'执行保护(峰值衰减{decay_bps:.1f}bps>'
                    f'{self.execution_guard_max_peak_decay_bps:.1f}bps)'
                )

        return True, ''
    
    def _pass_risk_check(self, row: Dict) -> bool:
        """
        风控规则检查:
        0. 保证金风控: 该标的现有持仓 保证金/维持保证金 < warning_pct 时禁止开仓
        1. 资金费率 >= 下限(min_funding_rate_bps)
        2. 开仓盘口覆盖 <= 阈值
        3. 开仓基差 >= funding-adjusted entry_floor
        4. 旧模式下保留盈利性守卫；新模式下 entry_floor 已合并 funding、手续费和滑点缓冲
        """
        base_asset = row.get('base_asset', '')

        # 全局持仓数上限检查：保留可用资金 buffer 给 future 追保/极端行情。
        if self.max_total_positions > 0 and self._holding_total_count >= self.max_total_positions:
            return False

        # 同标的持仓数上限检查：防止同一波收敛行情中连续开仓
        if self._holding_count.get(base_asset, 0) >= self.max_positions_per_asset:
            return False

        # 保证金风控检查：该标的现有持仓保证金/维持保证金比例过低时禁止加仓
        if base_asset in self._holding_margin_rate:
            if self._holding_margin_rate[base_asset] < self.margin_warning_pct:
                return False

        active_amount = self._active_open_amount_usdt(row)
        if not self._pass_account_capital_check(active_amount):
            return False

        # 最小名义价值检查：开仓金额低于交易所最低要求时直接过滤
        if base_asset in self.spot_meta:
            min_notional = self.spot_meta[base_asset].get('min_notional')
            if min_notional is not None and active_amount < min_notional:
                return False

        # 资金费率下限检查：24h费率(bps) >= min_funding_rate_bps
        funding_rate = row.get('funding_rate_24h')
        if funding_rate is not None:
            if float(funding_rate) * 10000 < self.min_funding_rate_bps:
                return False
        
        # 盘口覆盖检查
        open_coverage = row.get('open_coverage')
        if open_coverage is not None:
            if float(open_coverage) > self.coverage_threshold:
                return False
        
        # 统一入场门槛：p20 是历史位置参考，entry_floor 同时考虑 funding、手续费和滑点缓冲。
        open_vwap_basis = row.get('open_vwap_basis_bps')
        if open_vwap_basis is not None:
            if row.get('_funding_carry_candidate'):
                entry_snapshot = self._state_entry_snapshot(base_asset, row, float(open_vwap_basis))
                entry_floor = float(entry_snapshot.get('entry_floor_bps'))
            else:
                entry_floor = self._entry_floor_bps(base_asset, row, float(open_vwap_basis))
            if float(open_vwap_basis) < entry_floor:
                return False
        
        # 兼容旧模式：未启用 funding-adjusted entry 时仍使用传统盈利性守卫。
        if open_vwap_basis is not None and not self.funding_entry_enabled:
            if base_asset not in self.close_vwap_threshold_meta:
                return False
            close_data = self.close_vwap_threshold_meta[base_asset]
            close_threshold = close_data.get(self.close_threshold_col)
            if close_threshold is None:
                return False
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

    def _pass_account_capital_check(self, amount_usdt: Optional[float] = None) -> bool:
        """真实资金检查：开仓前确认 Binance/Gate 可用资金足够且快照新鲜。"""
        if not self.capital_required:
            return True
        if not self._account_summary or not self._account_summary_ts:
            return False
        if time.time() - self._account_summary_ts > self.capital_max_age_sec:
            return False

        binance_available = float((self._account_summary.get('binance') or {}).get('available') or 0)
        gate_available = float((self._account_summary.get('gate') or {}).get('available') or 0)
        amount = float(amount_usdt if amount_usdt is not None else self.open_amount_usdt)
        spot_required = amount * (1 + self._fee_spot_open)
        gate_required = (
            amount / self.capital_gate_leverage
            + amount * self._fee_future_open
        )
        return binance_available >= spot_required and gate_available >= gate_required

    def _apply_realtime_funding_info(self, base_asset: str, row: Optional[Dict]) -> None:
        """
        将已通过实时校验的 funding 信息写入本轮 row 与共享合约元数据。

        row['_cached_funding_rate_24h'] 仅用于开仓原因诊断；决策字段 funding_rate_24h
        会被实时值覆盖，保证 entry_floor/carry/订单记录和持仓展示口径一致。
        """
        if row is None:
            return
        info = self._last_realtime_funding_info.get(base_asset)
        if not info:
            return

        if '_cached_funding_rate_24h' not in row:
            cached = row.get('funding_rate_24h')
            if cached is None:
                cached = self.contract_meta.get(base_asset, {}).get('funding_rate_24h')
            row['_cached_funding_rate_24h'] = cached

        for key in (
            'funding_rate',
            'funding_rate_24h',
            'funding_interval',
            'funding_next_apply',
            'funding_last_apply',
        ):
            row[key] = info.get(key)
        row['_realtime_funding_24h_bps'] = float(info.get('funding_rate_24h') or 0) * 10000.0

        meta = self.contract_meta.setdefault(base_asset, {})
        meta.update({
            'funding_rate': info.get('funding_rate'),
            'funding_rate_24h': info.get('funding_rate_24h'),
            'funding_interval': info.get('funding_interval'),
            'funding_next_apply': info.get('funding_next_apply'),
            'funding_last_apply': info.get('funding_last_apply'),
        })
    
    def _verify_realtime_funding_rate(self, base_asset: str, contract: str) -> bool:
        """
        开仓前实时校验资金费率：从 Gate API 获取最新费率，确认仍达到下限。
        若 API 调用失败（网络问题等），回退为放行（不阻塞开仓）。
        """
        try:
            self._last_realtime_funding_info.pop(base_asset, None)
            info = get_single_contract_funding_info(contract)
            if not info or info.get('funding_rate_24h') is None:
                # API 调用失败，回退为放行（不因网络问题阻止开仓）
                logger.debug(f"实时费率校验跳过(获取失败) | {base_asset}")
                return True
            
            # 校验费率 >= 下限(min_funding_rate_bps)
            rate_bps = float(info['funding_rate_24h']) * 10000
            if rate_bps < self.min_funding_rate_bps:
                logger.info(
                    f"实时费率校验拦截 | {base_asset} | "
                    f"实时费率={rate_bps:.2f}bps < 下限{self.min_funding_rate_bps:.1f}bps"
                )
                return False

            # 校验通过：记录实时费率，供 _build_open_reason 拼接到开仓原因
            self._last_realtime_rate_bps[base_asset] = rate_bps
            self._last_realtime_funding_info[base_asset] = info
            return True
        except Exception as e:
            # 任何异常均回退为放行
            logger.debug(f"实时费率校验异常(回退放行) | {base_asset}: {e}")
            return True

    def _pre_execution_gate(self, base_asset: str, contract: str, symbol: str) -> tuple:
        """
        最终风控旁路：下单前用单标的最短链路重新校验开仓条件。
    
        设计目的：
        - 拦截“信号过期”场景（峰值监控期间基差已衰减到不值得开仓的水平）
        - 检测盘口数据陈旧性（低流动性标的WS更新稀疏）
        - 确保下单用的数据 = 校验用的数据（同一份最短链路读取）
    
        最短链路：单标的盘口读取 → 新鲜度硬约束(lag_ms) → VWAP基差计算 → 统一入场门槛+覆盖率校验
        """
        # 未注入 manager 时退化为放行（兼容测试场景）
        if not self._gate_manager or not self._spot_manager:
            return True, None, None, ''

        try:
            # ── 1. 单标的盘口读取（最短链路，不遍历其他标的）──
            gate_ob = self._gate_manager.get_orderbook(contract)
            spot_ob = self._spot_manager.get_orderbook(symbol)

            if not gate_ob or not spot_ob:
                return False, None, None, f'盘口不可用(gate={gate_ob is not None}, spot={spot_ob is not None})'

            gate_row = gate_ob.to_dict_row()
            spot_row = spot_ob.to_dict_row()

            # ── 2. 盘口新鲜度硬约束：以本地 last_update_time 为准计算 lag_ms（破除同源缺陷）──
            # 原因：update_time(交易所推送时间戳) 在快照重建后会立刻“看起来很新”，
            # 但内容可能就是异常价。只有 last_update_time（本地接收时刻）能客观反映“现在距上次收到行情多久”。
            now_ts = time.time()
            gate_local_ts = float(getattr(gate_ob, 'last_update_time', 0) or 0)
            spot_local_ts = float(getattr(spot_ob, 'last_update_time', 0) or 0)
            gate_lag_ms = (now_ts - gate_local_ts) * 1000.0 if gate_local_ts > 0 else float('inf')
            spot_lag_ms = (now_ts - spot_local_ts) * 1000.0 if spot_local_ts > 0 else float('inf')

            if gate_lag_ms > self._max_orderbook_lag_ms or spot_lag_ms > self._max_orderbook_lag_ms:
                logger.info(
                    f"开仓旁路-行情滞后拦截 | {base_asset} | "
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
                    f"开仓旁路-Gate本地簿未就绪拦截 | {base_asset} | "
                    f"update_count={getattr(gate_ob, 'update_count', None)} | "
                    f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                )
                return False, None, None, 'Gate本地簿未接上连续WS增量'

            book_skew_ms = abs(gate_local_ts - spot_local_ts) * 1000.0
            if book_skew_ms > self._max_orderbook_lag_ms:
                logger.info(
                    f"开仓旁路-跨所盘口不同步拦截 | {base_asset} | "
                    f"skew={book_skew_ms:.0f}ms > max={self._max_orderbook_lag_ms:.0f}ms | "
                    f"gate_local={gate_local_ts:.3f} | spot_local={spot_local_ts:.3f}"
                )
                return False, None, None, (
                    f'跨所盘口不同步(skew={book_skew_ms:.0f}ms, max={self._max_orderbook_lag_ms:.0f}ms)'
                )

            # ── 3. 合并 + 计算对冲指标（单元素列表，开销极小）──
            from calc.merge_cross_exchange_orderbook import merge_orderbook_records
            from calc.calculate_hedge_metrics import calculate_hedge_metrics

            merged = merge_orderbook_records([gate_row], [spot_row])
            if not merged:
                return False, None, None, '盘口合并失败'

            state = self._peak_state.get(base_asset)
            target_amount_usdt = (
                self._funding_carry_amount()
                if state and state.get('trigger') == 'funding_carry'
                else self.open_amount_usdt
            )
            merged = calculate_hedge_metrics(
                merged, self.contract_meta, self.spot_meta, target_amount_usdt
            )
            row = merged[0]
            row['base_asset'] = base_asset
            row['open_amount_usdt'] = target_amount_usdt
            c_meta = self.contract_meta.get(base_asset, {})
            row['funding_rate'] = c_meta.get('funding_rate')
            row['funding_rate_24h'] = c_meta.get('funding_rate_24h')
            row['funding_interval'] = c_meta.get('funding_interval')
            row['funding_next_apply'] = c_meta.get('funding_next_apply')
            row['funding_last_apply'] = c_meta.get('funding_last_apply')

            # ── 4. 计算VWAP基差 ──
            gate_basis_bps = calc_vwap_basis_bps(
                row.get('spot_open_vwap'), row.get('future_open_vwap')
            )
            if gate_basis_bps is None:
                return False, None, None, 'VWAP基差计算失败(盘口深度不足)'
            gate_basis_bps = round(gate_basis_bps, 2)

            # ── 4.5 实时资金费刷新：实盘最终旁路用下单前最新 funding 重算门槛 ──
            if self.executor_client.channel == 'Live':
                if not self._verify_realtime_funding_rate(base_asset, contract):
                    return False, row, gate_basis_bps, '实时资金费率低于下限'
                self._apply_realtime_funding_info(base_asset, row)

            # ── 5. 统一入场门槛校验 ──
            if state and state.get('trigger') == 'funding_carry':
                entry_snapshot = self._funding_carry_snapshot(base_asset, gate_basis_bps, row)
                if not entry_snapshot:
                    logger.info(
                        f"开仓旁路-FundingCarry复核失败 | {base_asset} | "
                        f"gate_basis={gate_basis_bps:.2f}bps | lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                    )
                    return False, row, gate_basis_bps, 'FundingCarry旁路复核失败(实时条件不满足)'
                self._apply_entry_snapshot_to_row(row, entry_snapshot)
            else:
                entry_snapshot = self._entry_snapshot(base_asset, gate_basis_bps, row)
                self._annotate_entry_snapshot(row, gate_basis_bps)
            if state is not None:
                state['entry_snapshot'] = entry_snapshot
            entry_floor = float(entry_snapshot.get('entry_floor_bps'))
            if gate_basis_bps < entry_floor:
                logger.info(
                    f"开仓旁路-入场门槛拦截 | {base_asset} | "
                    f"gate_basis={gate_basis_bps:.2f}bps < entry_floor={entry_floor:.2f}bps | "
                    f"{self._format_entry_snapshot(entry_snapshot)} | "
                    f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                )
                return False, row, gate_basis_bps, (
                    f'基差衰减({gate_basis_bps:.1f}bps < entry_floor={entry_floor:.1f}|'
                    f'{self._format_entry_snapshot(entry_snapshot)})'
                )

            # ── 6. 兼容旧模式：盈利性守卫 ──
            if not self.funding_entry_enabled:
                if base_asset not in self.close_vwap_threshold_meta:
                    logger.info(
                        f"开仓旁路-盈利性守卫拦截(无平仓阈值) | {base_asset} | "
                        f"close_vwap_threshold_meta 未包含该标的 | "
                        f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                    )
                    return False, row, gate_basis_bps, '盈利性守卫拦截(无平仓阈值,拒绝开仓)'
                close_data = self.close_vwap_threshold_meta[base_asset]
                close_threshold = close_data.get(self.close_threshold_col)
                if close_threshold is None:
                    logger.info(
                        f"开仓旁路-盈利性守卫拦截(阈值为NULL) | {base_asset} | "
                        f"{self.close_threshold_col}=None | "
                        f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                    )
                    return False, row, gate_basis_bps, f'盈利性守卫拦截({self.close_threshold_col}为NULL,拒绝开仓)'
                if gate_basis_bps <= float(close_threshold) + self.fee_cost_bps:
                    logger.info(
                        f"开仓旁路-盈利性守卫拦截 | {base_asset} | "
                        f"gate_basis={gate_basis_bps:.2f}bps <= "
                        f"close_thr={float(close_threshold):.2f}+fee={self.fee_cost_bps:.0f} | "
                        f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms)"
                    )
                    return False, row, gate_basis_bps, (
                        f'盈利性守卫拦截(basis={gate_basis_bps:.1f} <= '
                        f'close_thr={float(close_threshold):.1f}+fee={self.fee_cost_bps:.0f})'
                    )

            # ── 7. 盘口覆盖校验 ──
            open_coverage = row.get('open_coverage')
            if open_coverage is not None and float(open_coverage) > self.coverage_threshold:
                logger.info(
                    f"开仓旁路-覆盖超限拦截 | {base_asset} | "
                    f"coverage={float(open_coverage):.3f} > {self.coverage_threshold} | "
                    f"gate_basis={gate_basis_bps:.2f}bps"
                )
                return False, row, gate_basis_bps, (
                    f'盘口覆盖超限({float(open_coverage):.2f} > {self.coverage_threshold})'
                )

            # ── 全部通过 ──
            # 记录本次旁路风控读取到的 lag，供 _build_open_reason 拼接到开仓原因
            self._last_orderbook_lag_ms[base_asset] = (gate_lag_ms, spot_lag_ms)
            logger.info(
                f"开仓旁路通过 | {base_asset} | "
                f"gate_basis={gate_basis_bps:.2f}bps | coverage={open_coverage} | "
                f"lag(gate={gate_lag_ms:.0f}ms,spot={spot_lag_ms:.0f}ms,"
                f"skew={book_skew_ms:.0f}ms,max={self._max_orderbook_lag_ms:.0f}ms)"
            )
            return True, row, gate_basis_bps, ''

        except Exception as e:
            # 异常时退化为放行（不因旁路故障阻塞正常开仓）
            logger.warning(f"最终风控旁路异常(退化放行) | {base_asset}: {e}")
            return True, None, None, ''

    def _pass_peak_check(self, base_asset: str, current_basis_bps: float, row: Dict = None) -> bool:
        """
        峰值回落 + sustain 开仓确认（单通道）:
        - 首次超阈值: 实时校验资金费率 + 记录峰值、开始时间, 返回 False(等待)
        - 后续更高: 更新峰值, 返回 False(继续等待)
        - 监控超时(elapsed ≥ monitor_timeout_sec): 基差长期不回落，设置 trigger='timeout'，
          进入盘口恢复与最终旁路风控，避免错过持续高基差机会。
        - 从峰值回落 ≥ pullback_pct，且 elapsed ≥ sustain_sec:
          设置 trigger='pullback' + resiliency_active=True，进入盘口恢复等待。
        - resiliency_active 后续轮次：直接返回 True，让恢复检查持续采样；
          不再反复要求当前 basis 仍满足 pullback 形态。
        """
        now = datetime.now()
        state = self._peak_state.get(base_asset)
        row = row or {}
        contract = row.get('contract', '')
        symbol = row.get('symbol', '')

        if state is None:
            # 首次进入监控：实时费率校验 + 记录峰值 + 创建信号
            if not self._verify_realtime_funding_rate(base_asset, contract):
                return False
            self._apply_realtime_funding_info(base_asset, row)

            entry_snapshot = self._state_entry_snapshot(base_asset, row, current_basis_bps)
            signal_id = self._create_signal(base_asset, current_basis_bps)
            self._peak_state[base_asset] = {
                'peak_bps': current_basis_bps,
                'start_time': now,
                'trigger': None,
                'signal_id': signal_id,
                'signal_basis_bps': current_basis_bps,
                'entry_snapshot': entry_snapshot,
            }
            logger.info(
                f"峰值监控开始 | {base_asset} | "
                f"basis={current_basis_bps:.2f}bps | "
                f"{self._format_entry_snapshot(entry_snapshot)} | "
                f"需持续{self.sustain_sec}s且回落{self.peak_pullback_pct*100:.0f}% | "
                f"start_time={now.strftime('%H:%M:%S.%f')[:-3]}"
            )
            return False

        if state.get('resiliency_active'):
            return True

        # 更新峰值
        if current_basis_bps > state['peak_bps']:
            state['peak_bps'] = current_basis_bps

        # 监控超时：基差长期不回落，直接进入盘口恢复与最终旁路风控。
        elapsed_sec = (now - state['start_time']).total_seconds()
        if elapsed_sec >= self.peak_monitor_timeout_sec:
            peak_bps = state['peak_bps']
            state['trigger'] = 'timeout'
            state['resiliency_active'] = True
            state['resiliency_start_time'] = now
            state['timeout_elapsed_sec'] = elapsed_sec
            logger.info(
                f"峰值监控超时直开候选 | {base_asset} | "
                f"peak={peak_bps:.2f} | current={current_basis_bps:.2f} | "
                f"elapsed={elapsed_sec:.0f}s | 进入盘口恢复与最终旁路风控"
            )
            return True

        # 检查回落阈值：当前基差 ≤ 峰值 × (1 - pullback_pct)
        pullback_threshold = state['peak_bps'] * (1 - self.peak_pullback_pct)
        if current_basis_bps > pullback_threshold:
            return False  # 尚未回落到位

        # 回落到位，其次检查持续时间
        if elapsed_sec < self.sustain_sec:
            return False  # 持续不足，继续等待
        
        # 持续时间 + 回落均达标，确认开仓
        state['trigger'] = 'pullback'
        state['resiliency_active'] = True
        state['resiliency_start_time'] = now
        logger.info(
            f"峰值回落确认，进入盘口恢复等待 | {base_asset} | "
            f"peak={state['peak_bps']:.2f}bps | current={current_basis_bps:.2f}bps | "
            f"sustained={elapsed_sec:.1f}s≥{self.sustain_sec}s | pullback={self.peak_pullback_pct*100:.0f}% | "
            f"start={state['start_time'].strftime('%H:%M:%S.%f')[:-3]}→"
            f"now={now.strftime('%H:%M:%S.%f')[:-3]}"
        )
        return True

    # ──────────────────────────────────────────────────────────────────
    # 信号日志记录
    # ──────────────────────────────────────────────────────────────────

    def _create_signal(self, base_asset: str, entry_basis_bps: float) -> Optional[int]:
        """创建信号记录（进入峰值监控时）"""
        try:
            sql = """
                INSERT INTO mi_trade_signal (
                    base_asset, signal_time, status,
                    entry_basis_bps, signal_basis_bps, peak_basis_bps
                ) VALUES (
                    %(base_asset)s, %(signal_time)s, 'monitoring',
                    %(entry_basis_bps)s, %(signal_basis_bps)s, %(peak_basis_bps)s
                )
            """
            basis = round(entry_basis_bps, 2)
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, {
                    'base_asset': base_asset,
                    'signal_time': datetime.now(),
                    'entry_basis_bps': basis,
                    'signal_basis_bps': basis,
                    'peak_basis_bps': basis,
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
        signal_basis_bps: Optional[float] = None,
        pre_gate_basis_bps: Optional[float] = None,
        actual_basis_bps: Optional[float] = None,
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
        signal_basis = signal_basis_bps
        if signal_basis is None:
            signal_basis = state.get('signal_basis_bps')
        trigger_type = self._normalize_signal_trigger_type(trigger_type)

        try:
            sql = """
                UPDATE mi_trade_signal SET
                    status = %(status)s,
                    resolved_time = %(resolved_time)s,
                    signal_basis_bps = %(signal_basis_bps)s,
                    peak_basis_bps = %(peak_basis_bps)s,
                    pre_gate_basis_bps = %(pre_gate_basis_bps)s,
                    actual_basis_bps = %(actual_basis_bps)s,
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
                    'signal_basis_bps': round(signal_basis, 2) if signal_basis is not None else None,
                    'peak_basis_bps': round(peak_bps, 2) if peak_bps is not None else None,
                    'pre_gate_basis_bps': round(pre_gate_basis_bps, 2) if pre_gate_basis_bps is not None else None,
                    'actual_basis_bps': round(actual_basis_bps, 2) if actual_basis_bps is not None else None,
                    'exit_basis_bps': round(exit_basis_bps, 2) if exit_basis_bps is not None else None,
                    'exit_reason': exit_reason if exit_reason else None,
                    'duration_sec': duration_sec,
                    'trigger_type': trigger_type,
                    'order_uuid': order_uuid,
                    'id': signal_id,
                })
        except Exception as e:
            logger.error(f"信号记录更新失败 {base_asset}: {e}")
        self._record_signal_noise_event(base_asset, status)

    @staticmethod
    def _normalize_signal_trigger_type(trigger_type: Optional[str]) -> Optional[str]:
        """Keep persisted trigger_type within the DB varchar(20) contract."""
        if not trigger_type:
            return None
        aliases = {
            'rebound_strong_cushion': REBOUND_STRONG_TRIGGER,
        }
        normalized = aliases.get(str(trigger_type), str(trigger_type))
        return normalized[:20]

    def _get_risk_fail_reason(self, row: Dict) -> str:
        """识别风控失败的具体原因（用于信号日志）"""
        base_asset = row.get('base_asset', '')

        if self.max_total_positions > 0 and self._holding_total_count >= self.max_total_positions:
            return f"总持仓数上限({self._holding_total_count}/{self.max_total_positions})"

        if self._holding_count.get(base_asset, 0) >= self.max_positions_per_asset:
            return (
                f"同标的持仓数上限"
                f"({self._holding_count.get(base_asset, 0)}/{self.max_positions_per_asset})"
            )

        # 保证金风控检查
        if base_asset in self._holding_margin_rate:
            margin_rate = self._holding_margin_rate[base_asset]
            if margin_rate < self.margin_warning_pct:
                return (
                    f"保证金风控(保证金/维持保证金"
                    f"{margin_rate:.1f}%<{self.margin_warning_pct:.1f}%)"
                )

        if self.capital_required:
            if not self._account_summary or not self._account_summary_ts:
                return '资金风控(无交易所资金快照)'
            age = time.time() - self._account_summary_ts
            if age > self.capital_max_age_sec:
                return f"资金风控(资金快照过期{age:.0f}s>{self.capital_max_age_sec}s)"
            active_amount = self._active_open_amount_usdt(row)
            binance_available = float((self._account_summary.get('binance') or {}).get('available') or 0)
            gate_available = float((self._account_summary.get('gate') or {}).get('available') or 0)
            spot_required = active_amount * (1 + self._fee_spot_open)
            gate_required = (
                active_amount / self.capital_gate_leverage
                + active_amount * self._fee_future_open
            )
            if binance_available < spot_required:
                return f"资金风控(Binance可用{binance_available:.2f}<需{spot_required:.2f}USDT)"
            if gate_available < gate_required:
                return f"资金风控(Gate可用{gate_available:.2f}<需{gate_required:.2f}USDT)"

        # 最小名义价值检查
        active_amount = self._active_open_amount_usdt(row)
        if base_asset in self.spot_meta:
            min_notional = self.spot_meta[base_asset].get('min_notional')
            if min_notional is not None and active_amount < min_notional:
                return f"开仓金额低于最小名义值({active_amount}<{min_notional}USDT)"
        # 资金费率下限检查
        funding_rate = row.get('funding_rate_24h')
        if funding_rate is not None:
            rate_bps = float(funding_rate) * 10000
            if rate_bps < self.min_funding_rate_bps:
                return f"资金费率不达标({rate_bps:.2f}bps<{self.min_funding_rate_bps:.1f}bps)"

        # 盘口覆盖检查
        open_coverage = row.get('open_coverage')
        if open_coverage is not None and float(open_coverage) > self.coverage_threshold:
            return f"盘口覆盖超限({float(open_coverage):.2f}>{self.coverage_threshold})"

        # 统一入场门槛不达标
        open_vwap_basis = row.get('open_vwap_basis_bps')
        if open_vwap_basis is not None:
            entry_snapshot = self._state_entry_snapshot(base_asset, row, float(open_vwap_basis))
            entry_floor = float(entry_snapshot.get('entry_floor_bps'))
            if float(open_vwap_basis) < entry_floor:
                if row.get('_funding_carry_candidate'):
                    return (
                        f"FundingCarry基差不足({float(open_vwap_basis):.1f}<"
                        f"floor={entry_floor:.1f}|p20_floor="
                        f"{float(entry_snapshot.get('funding_carry_p20_floor_bps', 0)):.1f}"
                        f"-{self.funding_carry_basis_relax_bps:.1f})"
                    )
                return (
                    f"基差跌回入场门槛下({float(open_vwap_basis):.1f}<"
                    f"entry_floor={entry_floor:.1f}bps|{self._format_entry_snapshot(entry_snapshot)})"
                )

        # 兼容旧模式：盈利性守卫
        if (
            open_vwap_basis is not None
            and not self.funding_entry_enabled
            and base_asset in self.close_vwap_threshold_meta
        ):
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
        格式: "基差{bps}(entry_floor={floor},p20={p20})|carry(...)|费率|峰值|门槛|量"
        """
        parts = []

        # 1. VWAP基差 vs funding-adjusted entry floor
        entry_snapshot = dict(self._state_entry_snapshot(base_asset, row, open_vwap_basis))
        current_snapshot = self._entry_snapshot(base_asset, open_vwap_basis, row)
        entry_snapshot['expected_edge_bps'] = current_snapshot.get('expected_edge_bps')
        p20 = float(entry_snapshot.get('p20_bps', self.basis_threshold_bps))
        entry_floor = float(entry_snapshot.get('entry_floor_bps', p20))
        parts.append(f"基差{open_vwap_basis:.1f}bps(entry_floor={entry_floor:.1f},p20={p20:.1f})")
        parts.append(
            f"carry(funding24h={float(entry_snapshot.get('funding_24h_bps', 0)):.1f}bps,"
            f"预期={float(entry_snapshot.get('expected_funding_bps', 0)):.1f}bps,"
            f"edge={float(entry_snapshot.get('expected_edge_bps', 0)):.1f}bps)"
        )

        # 2. 24h资金费率（决策口径：实时值优先）
        funding_rate = row.get('funding_rate_24h')
        if funding_rate is not None:
            rate_pct = float(funding_rate) * 100
            parts.append(f"实时24h费率{rate_pct:.4f}%")

        cached_rate = row.get('_cached_funding_rate_24h')
        if cached_rate is not None and funding_rate is not None:
            cached_bps = float(cached_rate) * 10000
            realtime_bps = float(funding_rate) * 10000
            if abs(cached_bps - realtime_bps) >= 0.05:
                parts.append(
                    f"缓存24h费率{float(cached_rate) * 100:.4f}%"
                    f"(偏移{realtime_bps - cached_bps:+.1f}bps)"
                )

        # 3. 实时费率校验结果
        rt_rate = self._last_realtime_rate_bps.pop(base_asset, None)
        if rt_rate is not None:
            parts.append(
                f"实时24h校验✓({rt_rate:.2f}bps≥{self.min_funding_rate_bps:.1f})"
            )

        # 4. 峰值回落 / 超时信息
        peak_state = self._peak_state.get(base_asset)
        if peak_state:
            peak_bps = peak_state.get('peak_bps', 0)
            trigger = peak_state.get('trigger', 'unknown')
            elapsed = (datetime.now() - peak_state['start_time']).total_seconds()
            if trigger == 'pullback':
                parts.append(
                    f"峰值回落(峰{peak_bps:.1f},持续{elapsed:.1f}s≥{self.sustain_sec}s,"
                    f"回落{self.peak_pullback_pct*100:.0f}%)"
                )
            elif trigger in ('rebound', REBOUND_STRONG_TRIGGER, 'rebound_strong_cushion'):
                floor_bps = float(peak_state.get('rebound_floor_bps', 0) or 0)
                rise_bps = float(peak_state.get('rebound_rise_bps', 0) or 0)
                if trigger in (REBOUND_STRONG_TRIGGER, 'rebound_strong_cushion'):
                    cushion_bps = float(peak_state.get('rebound_strong_cushion_bps', 0) or 0)
                    hold_sec = float(peak_state.get('rebound_strong_hold_sec', 0) or 0)
                    parts.append(
                        f"回调强垫(峰{peak_bps:.1f},低{floor_bps:.1f},"
                        f"垫{cushion_bps:.1f}bps,hold={hold_sec:.1f}s)"
                    )
                else:
                    parts.append(
                        f"回调再突破(峰{peak_bps:.1f},低{floor_bps:.1f},"
                        f"回升{rise_bps:.1f}bps)"
                    )
            elif trigger == 'momentum':
                rise_bps = float(peak_state.get('momentum_rise_bps', 0) or 0)
                tier = peak_state.get('strategy_tier', self._asset_tier(base_asset))
                window_sec = float(peak_state.get('momentum_window_sec', self.momentum_window_sec) or 0)
                buffer_bps = float(
                    peak_state.get('momentum_min_basis_buffer_bps', self.momentum_min_basis_buffer_bps) or 0
                )
                parts.append(
                    f"动量开仓(tier={tier},升{rise_bps:.1f}bps/{window_sec:.1f}s,"
                    f"buffer={buffer_bps:.1f}bps)"
                )
            elif trigger == 'funding_carry':
                tier = peak_state.get('strategy_tier', self._asset_tier(base_asset))
                parts.append(
                    f"FundingCarry(tier={tier},"
                    f"p20_floor={float(entry_snapshot.get('funding_carry_p20_floor_bps', 0)):.1f},"
                    f"放宽{self.funding_carry_basis_relax_bps:.1f}bps,"
                    f"距结算{float(entry_snapshot.get('funding_carry_next_min', 0)):.1f}min,"
                    f"金额{float(entry_snapshot.get('funding_carry_amount_usdt', self.open_amount_usdt)):.1f}U)"
                )
            elif trigger == 'timeout':
                timeout_elapsed = float(peak_state.get('timeout_elapsed_sec', elapsed) or 0)
                parts.append(
                    f"峰值超时直开(峰{peak_bps:.1f},持续{timeout_elapsed:.1f}s≥"
                    f"{self.peak_monitor_timeout_sec}s)"
                )
            else:
                parts.append(f"峰值{peak_bps:.1f}")

        # 5. 入场门槛 / 旧盈利性守卫
        if self.funding_entry_enabled:
            parts.append(
                f"门槛({open_vwap_basis:.1f}>={entry_floor:.1f}|"
                f"carry_floor={float(entry_snapshot.get('carry_floor_bps', 0)):.1f},"
                f"timing_floor={float(entry_snapshot.get('timing_floor_bps', 0)):.1f})"
            )
        elif base_asset in self.close_vwap_threshold_meta:
            close_data = self.close_vwap_threshold_meta[base_asset]
            close_thr = close_data.get(self.close_threshold_col)
            if close_thr is not None:
                parts.append(f"守卫({open_vwap_basis:.1f}>{float(close_thr):.1f}+{self.fee_cost_bps:.0f}费)")
            else:
                parts.append(f"守卫(无{self.close_threshold_col}阈值,已拒单)")
        else:
            parts.append("守卫(无平仓阈值,已拒单)")

        # 5.5 行情新鲜度（旁路风控读取到的 lag）─ 反映“下单时刻距上次收到行情多久”
        lag = self._last_orderbook_lag_ms.pop(base_asset, None)
        if lag is not None:
            gate_lag_ms, spot_lag_ms = lag
            def _fmt(ms):
                if ms is None or ms == float('inf'):
                    return 'NA'
                return f'{ms:.0f}ms'
            parts.append(f"鲜度(gate={_fmt(gate_lag_ms)},spot={_fmt(spot_lag_ms)})")
        else:
            parts.append("鲜度(NA)")

        # 5.6 盘口恢复确认（resiliency）通过时的关键指标，便于复盘成功开仓质量
        resiliency_metrics = self._last_resiliency_metrics.pop(base_asset, None)
        if resiliency_metrics:
            recovery = float(resiliency_metrics.get('recovery_ratio', 0) or 0)
            drop = float(resiliency_metrics.get('shock_drop_ratio', 0) or 0)
            basis_vol = float(resiliency_metrics.get('basis_volatility_bps', 0) or 0)
            spread_widen = float(resiliency_metrics.get('max_spread_widen_bps', 0) or 0)
            coverage = resiliency_metrics.get('coverage')
            hold_sec = float(resiliency_metrics.get('hold_sec', 0) or 0)
            cov_text = 'NA' if coverage is None else f'{float(coverage):.2f}'
            parts.append(
                f"恢复(recovery={recovery:.2f},drop={drop:.2f},"
                f"vol={basis_vol:.1f}bps,spread={spread_widen:.1f}bps,"
                f"cov={cov_text},hold={hold_sec:.1f}s)"
            )

        # 6. 24h成交量（现货/期货）
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

    def _load_open_cooldown_from_db(self):
        """
        启动后首次进入开仓循环时，从 DB 一次性加载所有标的的最近一次成功开仓时间，
        之后所有冷却判定走内存（_pass_cooldown_check）。

        前提：本服务是 mi_trade_order(open) 的唯一写入入口（无手动开仓 / 外部脚本）。
        失败时不置 loaded flag，下轮自动重试。
        """
        if self._cooldown_loaded:
            return
        try:
            sql = """
                SELECT base_asset, MAX(created_at) AS last_open_time
                FROM mi_trade_order
                WHERE market_type = 'spot'
                  AND order_side = 'open'
                  AND status = 'executed'
                  AND channel = %s
                GROUP BY base_asset
            """
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, (self.executor_client.channel,))
                rows = cursor.fetchall()
            for r in rows:
                ba = r.get('base_asset')
                t = r.get('last_open_time')
                if ba and t:
                    self._last_open_time[ba] = t
            self._cooldown_loaded = True
            logger.info(f"开仓冷却缓存初始化 | {len(self._last_open_time)}个标的")
        except Exception as e:
            logger.error(f"开仓冷却缓存加载失败(下轮重试): {e}")

    def _pass_cooldown_check(self, base_asset: str) -> bool:
        """检查同标的开仓冷却（纯内存，由 _last_open_time 维护）"""
        last_time = self._last_open_time.get(base_asset)
        if last_time is None:
            return True  # 无开仓记录
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
        self._apply_future_maker_open(row, future_order)
        
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
            'signal_basis_bps': row.get('signal_basis_bps'),
            'pre_gate_basis_bps': row.get('pre_gate_basis_bps'),
            'actual_basis_bps': row.get('actual_basis_bps'),
            'risk_relief_bps': row.get('risk_relief_bps'),
            'open_marginal_basis_bps': row.get('open_marginal_basis_bps'),
            'funding_rate_24h': row.get('funding_rate_24h')
        }

    def _apply_future_maker_open(self, row: Dict, future_order: Dict) -> None:
        """给实盘开仓期货腿附加 maker 执行参数；缺少卖一价时保持原 taker 逻辑。"""
        if not self.future_maker_open_enabled:
            return
        if self.executor_client.channel != 'Live':
            return

        base_asset = str(row.get('base_asset') or '').upper()
        tier = self._asset_tier(base_asset)
        if tier not in self.future_maker_open_allowed_tiers:
            return

        maker_price = row.get('future_price_ask_1') or row.get('future_ask_price_1') or row.get('future_ask_1')
        if maker_price is None:
            return

        maker_price = float(maker_price)
        if maker_price <= 0:
            return
        if self.future_maker_open_price_offset_bps:
            maker_price *= 1 + self.future_maker_open_price_offset_bps / 10000.0

        maker_params = {
            'execution_style': 'maker',
            'maker_ttl_ms': self.future_maker_open_ttl_ms,
            'maker_price': maker_price,
            'maker_price_source': 'future_ask1',
            'maker_strategy_tier': tier,
            'maker_taker_reference_price': row.get('future_open_vwap'),
            'maker_spot_reference_price': row.get('spot_open_vwap'),
        }
        if self.future_maker_open_fallback_ioc_enabled and tier in self.future_maker_open_fallback_allowed_tiers:
            fallback_price = self._future_open_protective_price(
                row, self.future_maker_open_fallback_slippage_bps
            )
            fallback_min_basis = self._fallback_min_open_basis(row, base_asset)
            current_basis = row.get('pre_gate_basis_bps') or row.get('open_vwap_basis_bps')
            fallback_allowed = (
                fallback_price is not None
                and current_basis is not None
                and float(current_basis) >= fallback_min_basis
            )
            maker_params.update({
                'maker_fallback_ioc_enabled': fallback_allowed,
                'maker_fallback_protective_price': fallback_price,
                'maker_fallback_min_basis_bps': round(fallback_min_basis, 2),
                'maker_fallback_current_basis_bps': round(float(current_basis), 2)
                if current_basis is not None else None,
                'maker_fallback_slippage_bps': self.future_maker_open_fallback_slippage_bps,
            })
        else:
            fallback_min_basis = self._fallback_min_open_basis(row, base_asset)
        if self.future_maker_open_spot_hedge_protective_ioc_enabled:
            maker_params.update({
                'maker_spot_hedge_protective_ioc_enabled': True,
                'maker_spot_hedge_min_basis_bps': round(float(fallback_min_basis), 2),
            })
        future_order.update(maker_params)

    def _future_open_protective_price(self, row: Dict, slippage_bps: float) -> Optional[float]:
        """开仓 future 卖空 fallback IOC 的最低可接受成交价。"""
        price = row.get('future_open_vwap') or row.get('future_bid_price_1') or row.get('future_bid_1')
        if price is None:
            return None
        price = float(price)
        if price <= 0:
            return None
        return round(price * (1 - max(float(slippage_bps), 0.0) / 10000.0), 10)

    def _fallback_min_open_basis(self, row: Dict, base_asset: str) -> float:
        """maker 未成交后允许转保护 IOC 的最低 pre-gate 基差。"""
        basis_bps = float(row.get('pre_gate_basis_bps') or row.get('open_vwap_basis_bps') or 0.0)
        state = self._peak_state.get(base_asset) or {}
        entry_snapshot = (
            state.get('entry_snapshot')
            or row.get('_entry_snapshot')
            or (
                self._state_entry_snapshot(base_asset, row, basis_bps)
                if row.get('_entry_entry_floor_bps') is not None
                else None
            )
            or self._entry_snapshot(base_asset, basis_bps, row)
        )
        entry_floor = float(entry_snapshot.get('entry_floor_bps') or 0.0)
        buffer_bps = max(
            self.execution_guard_min_p20_buffer_bps,
            self.future_maker_open_fallback_min_buffer_bps,
        )
        return entry_floor + buffer_bps
    
    def _format_basis_audit(self, order_group: Dict) -> str:
        def _fmt(value):
            return 'NA' if value is None else f'{float(value):.1f}bps'

        return (
            f"基差对比(signal={_fmt(order_group.get('signal_basis_bps'))},"
            f"pre_gate={_fmt(order_group.get('pre_gate_basis_bps'))},"
            f"actual={_fmt(order_group.get('actual_basis_bps'))})"
        )

    def _attach_actual_basis_audit(self, order_group: Dict, exec_result: Dict) -> None:
        if not exec_result['success']:
            return

        spot_exec_price = float(exec_result['spot_order']['exec_price'])
        future_exec_price = float(exec_result['future_order']['exec_price'])
        actual_basis_bps = calc_vwap_basis_bps(spot_exec_price, future_exec_price)
        if actual_basis_bps is not None:
            actual_basis_bps = round(actual_basis_bps, 2)
        else:
            actual_basis_bps = order_group.get('pre_gate_basis_bps') or order_group.get('open_vwap_basis_bps')

        order_group['actual_basis_bps'] = actual_basis_bps
        order_group['open_vwap_basis_bps'] = actual_basis_bps

        open_fee_bps = calc_open_fee_bps(
            self._fee_spot_open,
            self._actual_future_open_fee(order_group.get('base_asset'), exec_result)
        )
        if actual_basis_bps is not None:
            order_group['open_marginal_basis_bps'] = round(
                actual_basis_bps + open_fee_bps + self._risk_relief_bps, 2
            )

        reason = order_group.get('open_reason') or ''
        audit_text = self._format_basis_audit(order_group)
        order_group['open_reason'] = f"{reason}|{audit_text}" if reason else audit_text
        execution_audit = format_execution_audit(exec_result)
        if execution_audit:
            order_group['open_reason'] = f"{order_group['open_reason']}|{execution_audit}"

    def _actual_future_open_fee(self, base_asset: Optional[str], exec_result: Dict) -> float:
        maker = (exec_result.get('execution_stats') or {}).get('future_maker') or {}
        if maker.get('fallback_filled'):
            return self._fee_future_taker_open
        if maker.get('filled'):
            return self._fee_future_open
        return self._fee_future_open

    def _save_orders(self, order_group: Dict, exec_result: Dict):
        """持久化订单到数据库"""
        self._attach_actual_basis_audit(order_group, exec_result)

        # 开仓成功时，先创建持仓记录，获取 position_id
        position_id = None
        if exec_result['success'] and order_group['spot_order']['order_side'] == 'open':
            position_id = self._create_position(order_group, exec_result)
        
        sql = """
            INSERT INTO mi_trade_order (
                order_uuid, position_id, base_asset, spot_symbol, future_contract, order_side, market_type,
                trade_direction, leverage, status, channel, reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_vwap_basis_bps, signal_basis_bps, pre_gate_basis_bps, actual_basis_bps,
                risk_relief_bps, open_marginal_basis_bps, funding_rate_24h,
                liquidity_role, fee_rate, fee_amount, fee_amount_usdt, fee_asset, exchange_order_id, executed_at
            ) VALUES (
                %(order_uuid)s, %(position_id)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                %(order_side)s, %(market_type)s, %(trade_direction)s, %(leverage)s, %(status)s, %(channel)s,
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                %(open_coverage)s, %(open_vwap_basis_bps)s, %(signal_basis_bps)s, %(pre_gate_basis_bps)s, %(actual_basis_bps)s,
                %(risk_relief_bps)s, %(open_marginal_basis_bps)s, %(funding_rate_24h)s,
                %(liquidity_role)s, %(fee_rate)s, %(fee_amount)s, %(fee_amount_usdt)s, %(fee_asset)s, %(exchange_order_id)s, %(executed_at)s
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
            order['signal_basis_bps'] = order_group.get('signal_basis_bps')
            order['pre_gate_basis_bps'] = order_group.get('pre_gate_basis_bps')
            order['actual_basis_bps'] = order_group.get('actual_basis_bps')
            order['risk_relief_bps'] = order_group.get('risk_relief_bps')
            order['open_marginal_basis_bps'] = order_group.get('open_marginal_basis_bps')
            order['funding_rate_24h'] = order_group.get('funding_rate_24h')
            
            # 注入渠道和持仓关联
            order['channel'] = self.executor_client.channel
            order['position_id'] = position_id
            order['leverage'] = self._order_leg_leverage(market_key)
            
            # 更新成交信息
            if exec_result['success']:
                exec_data = exec_result[market_key]
                order['status'] = 'executed'
                order['exec_price'] = exec_data['exec_price']
                order['exec_qty'] = exec_data['exec_qty']
                order['exec_amount'] = exec_data['exec_amount']
                order['coverage_ratio'] = exec_data.get('coverage_ratio')
                order.update(build_order_execution_fields(
                    market_key,
                    order,
                    exec_data,
                    exec_result,
                    spot_open_fee=self._fee_spot_open,
                    spot_close_fee=self._fee_spot_close,
                    future_open_fee=self._fee_future_open,
                    future_close_fee=self._fee_future_close,
                    future_taker_open_fee=self._fee_future_taker_open,
                    future_taker_close_fee=self._fee_future_taker_close,
                ))
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
                order['liquidity_role'] = None
                order['fee_rate'] = None
                order['fee_amount'] = None
                order['fee_amount_usdt'] = None
                order['fee_asset'] = None
                order['exchange_order_id'] = None
                order['executed_at'] = None
            
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, order)

    def _order_leg_leverage(self, market_key: str) -> float:
        if market_key == 'future_order':
            return self.capital_gate_leverage
        return 1.0
    
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
                open_spread_bps, signal_basis_bps, pre_gate_basis_bps, actual_basis_bps, open_reason,
                funding_rate_sum_bps, funding_payments_count, funding_total_pnl
            ) VALUES (
                %(order_uuid)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                'holding', %(opened_at)s,
                %(spot_open_qty)s, %(spot_open_price)s, %(spot_open_amount)s,
                %(future_open_qty)s, %(future_open_price)s, %(future_open_contracts)s,
                %(open_spread_bps)s, %(signal_basis_bps)s, %(pre_gate_basis_bps)s, %(actual_basis_bps)s, %(open_reason)s,
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
            'signal_basis_bps': order_group.get('signal_basis_bps'),
            'pre_gate_basis_bps': order_group.get('pre_gate_basis_bps'),
            'actual_basis_bps': order_group.get('actual_basis_bps'),
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
