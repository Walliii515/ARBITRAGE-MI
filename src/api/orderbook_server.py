# coding: utf-8
"""
跨交易所订单簿 FastAPI 桥接服务
合并 Gate 永续与 Binance 现货本地订单簿，通过 WebSocket 推送给前端
支持前端控制 WS 服务的启动/终止及进度查询
"""
import asyncio
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Set

try:
    import orjson
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.calculate_hedge_metrics import calculate_hedge_metrics
from calc.etl_pipeline import start_daily_schedulers, stop_daily_schedulers, ETL_TASKS, _etl_config
from common.config import config
from common.database import db_manager
from common.logger import get_logger, log_print, setup_logging
from common.meta_loader import (
    fetch_asset_market_profile_meta,
    fetch_asset_tier_meta,
    fetch_contract_meta,
    fetch_spot_meta,
)
from common.strategy_accounts import get_binance_credentials

from api.trading_api import router as trading_router
from api.auth import router as auth_router, verify_token_dependency, verify_ws_token
from calc.trading_executor import TradingExecutor, TradingExecutorConfig
from calc.position_tracker import PositionTracker
from calc.orderbook_enricher import EnrichConfig, enrich_trading_fields, enrich_snapshot_fields
from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl
from calc.reverse_position_pnl_calculator import ReversePnlConfig, calculate_reverse_realtime_pnl
from calc.gate_position_risk import attach_gate_position_risk
from calc.gate_cross_risk import (
    GateCrossRiskMonitor,
    build_default_gate_cross_risk_monitor,
    gate_cross_risk_health,
)
from calc.vwap_snapshot_recorder import record_vwap_snapshots
from calc.reconciliation import build_default_reconciler
from calc.gate_risk_event_monitor import build_default_gate_risk_event_monitor
from calc.account_capital import (
    GateCrossRiskNotifier,
    build_default_capital_snapshotter,
    build_default_gate_cross_risk_notifier,
)
from calc.delist_risk_monitor import DelistRiskConfig, DelistRiskMonitor
from calc.reverse_arbitrage import ReverseArbitrageConfig, enrich_reverse_opportunities
from calc.reverse_research_store import (
    ReverseResearchConfig,
    get_reverse_research_page,
    record_reverse_research_snapshot,
)
from calc.reverse_funding_predictor import (
    get_reverse_funding_prediction_page,
    refresh_reverse_funding_predictions,
)
from calc.reverse_signal_monitor import ReverseSignalMonitor, ReverseSignalMonitorConfig
from calc.reverse_trade_store import list_reverse_positions, summarize_reverse_positions
from calc.reverse_position_cost_sync import refresh_reverse_position_costs
from calc.executor_client import ExecutorClient
from calc.service_lifecycle import SERVICE_IDLE, SERVICE_STARTING, SERVICE_RUNNING, SERVICE_STOPPING
from calc.orderbook_data_client import OrderBookDataClient
from calc.server_metrics import get_latest_server_metrics, list_server_metrics, record_server_metrics
from calc.popup_notification_store import upsert_popup_notification
from exchange_apis.get_binance_margin_borrow import BinanceMarginBorrowClient, BinanceMarginBorrowConfig

setup_logging()
logger = get_logger(__name__)

AUTO_RISK_CLOSE_REASON_LABELS = {
    'margin_close': '保证金风控强平',
    'negative_funding_exit': '负资金费率强平',
    'delist_risk_exit': '下架风险强平',
}


def _json_dumps(data) -> str:
    """高性能 JSON 序列化（优先使用 orjson，回退 stdlib json）"""
    if _HAS_ORJSON:
        # orjson 原生支持 float/int/str/None/datetime，Decimal 需转 float
        return orjson.dumps(
            data,
            default=_orjson_default,
            option=orjson.OPT_NON_STR_KEYS,
        ).decode('utf-8')
    else:
        def _default(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            if isinstance(obj, datetime):
                return obj.strftime('%Y-%m-%d %H:%M:%S')
            raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')
        return json.dumps(data, default=_default, ensure_ascii=False)


def _orjson_default(obj):
    """orjson 的 default 回调（仅处理 Decimal）"""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')


def _start_gate_risk_event_monitor():
    """启动 Gate 私有 ADL/强平事件监听；失败只告警，不阻塞主服务。"""
    global _gate_risk_event_monitor
    if config.get_trade_mode() == 'virtual':
        logger.info('virtual 模式跳过 Gate 风险事件监听')
        return
    if not config.get_bool('exchange_risk_monitor.enabled', True):
        logger.info('Gate 风险事件监听已关闭')
        return
    try:
        _gate_risk_event_monitor = build_default_gate_risk_event_monitor()
        _gate_risk_event_monitor.start()
    except Exception as e:
        logger.error(f'Gate 风险事件监听启动失败: {e}', exc_info=True)


def _stop_gate_risk_event_monitor():
    global _gate_risk_event_monitor
    if not _gate_risk_event_monitor:
        return
    try:
        _gate_risk_event_monitor.stop()
    except Exception as e:
        logger.warning(f'Gate 风险事件监听关闭异常: {e}', exc_info=True)
    finally:
        _gate_risk_event_monitor = None


def _gate_risk_event_monitor_status() -> dict:
    if not config.get_bool('exchange_risk_monitor.enabled', True):
        return {'enabled': False, 'connected': False, 'channels': {}}
    if not _gate_risk_event_monitor:
        return {'enabled': True, 'connected': False, 'channels': {}, 'last_error': 'not_started'}
    try:
        return _gate_risk_event_monitor.get_status()
    except Exception as e:
        logger.warning(f'Gate 风险事件监听状态读取失败: {e}', exc_info=True)
        return {'enabled': True, 'connected': False, 'channels': {}, 'last_error': str(e)[:200]}


def _attach_base_asset_status(rows: List[Dict]) -> None:
    """为连接状态补充 mi_base_asset 有效状态，便于前端区分运行时缓存与监控候选。"""
    assets = sorted({
        str(row.get('base_asset') or '').strip().upper()
        for row in rows
        if row.get('base_asset')
    })
    if not assets:
        return

    status_by_asset: Dict[str, str] = {}
    try:
        placeholders = ', '.join(['%s'] * len(assets))
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    UPPER(TRIM(base_asset)) AS base_asset,
                    COALESCE(is_valid, 'Y') AS is_valid
                FROM mi_base_asset
                WHERE UPPER(TRIM(base_asset)) IN ({placeholders})
                """,
                assets,
            )
            for item in cursor.fetchall() or []:
                asset = str(item.get('base_asset') or '').strip().upper()
                if asset:
                    status_by_asset[asset] = str(item.get('is_valid') or 'Y').strip().upper()
    except Exception as e:
        logger.warning(f'连接状态资产有效性读取失败: {e}', exc_info=True)

    for row in rows:
        asset = str(row.get('base_asset') or '').strip().upper()
        is_valid = status_by_asset.get(asset, 'Y') == 'Y'
        row['asset_is_valid'] = is_valid
        row['asset_status'] = 'enabled' if is_valid else 'disabled'

_meta_update_time: str = ''  # 上次缓存刷新时间

# 以下参数从 src/config/config.yaml 加载，环境变量可覆盖
SETTLE = config.get_str('orderbook.settle', 'usdt', env='ORDERBOOK_SETTLE')
BROADCAST_THROTTLE_SEC = config.get_float('orderbook.broadcast_throttle_sec', 1.0)
# 每次开仓金额（USDT），作为新列推送给前端
OPEN_AMOUNT_USDT = config.get_float('trade.open.amount_usdt', 5000.0)
# 资金费率阈值百分位字段名（对应 mi_gate_future_funding_rate_threshold 表的列名，保留给前端展示）
FUNDING_THRESHOLD_PERCENTILE = config.get_str('trade.filter.funding_rate_threshold_percentile', 'percentile_30')
# 资金费率下限(bps)，开仓过滤用
MIN_FUNDING_RATE_BPS = config.get_float('trade.open.min_funding_rate_bps', -6.0)
MIN_FUNDING_SUPPORT_BPS = config.get_float('trade.open.min_funding_support_bps', MIN_FUNDING_RATE_BPS)
FUNDING_SUPPORT_WINDOW_HOURS = config.get_float('trade.open.funding_support_window_hours', 24.0)
FUNDING_SUPPORT_MIN_SAMPLES = config.get_int('trade.open.funding_support_min_samples', 2)
REALTIME_MIN_FUNDING_RATE_BPS = config.get_float(
    'trade.open.realtime_min_funding_rate_bps',
    5.0,
)
# 盘口覆盖阈值
ORDERBOOK_COVERAGE_THRESHOLD = config.get_float('trade.open.orderbook_coverage_threshold', 0.8)
# 风险缓释（bps）
RISK_RELIEF_BPS = config.get_float('trade.open.risk_relief_bps', 10)
# 开仓边际基差阈值（bps）
OPEN_VWAP_BASIS_THRESHOLD_BPS = config.get_float('trade.open.vwap_basis_threshold_bps', -60)
# 24小时成交量过滤阈值（USDT）
MIN_SPOT_VOLUME_24H_USDT = config.get_float('trade.filter.min_spot_volume_24h_usdt', 0)
MIN_FUTURE_VOLUME_24H_USDT = config.get_float('trade.filter.min_future_volume_24h_usdt', 0)
# 费率配置（bps 计算用）
SPOT_OPEN_FEE = config.get_float('trade.fee.spot_open', 0.00075)
SPOT_CLOSE_FEE = config.get_float('trade.fee.spot_close', 0.00075)
FUTURE_OPEN_FEE = config.get_float('trade.fee.future_open', 0.00075)
FUTURE_CLOSE_FEE = config.get_float('trade.fee.future_close', 0.00075)
FUTURE_TAKER_OPEN_FEE = config.get_float('trade.fee.future_taker_open', FUTURE_OPEN_FEE)
FUTURE_TAKER_CLOSE_FEE = config.get_float('trade.fee.future_taker_close', FUTURE_CLOSE_FEE)

# 反向资金费率策略（short spot + long future）独立配置
REVERSE_BORROW_AUTO_ENABLED = config.get_bool('reverse_arbitrage.binance_margin.enabled', True)
REVERSE_BORROW_CACHE_TTL_SEC = config.get_float('reverse_arbitrage.binance_margin.cache_ttl_sec', 60.0)
REVERSE_BORROW_MAX_ASSETS = config.get_int('reverse_arbitrage.binance_margin.max_borrowable_assets_per_refresh', 250)
REVERSE_SIGNAL_ENABLED = config.get_bool('reverse_arbitrage.signal.enabled', True)
REVERSE_SIGNAL_INTERVAL_SEC = config.get_float('reverse_arbitrage.signal.check_interval_sec', 1.0)
REVERSE_RESEARCH_ENABLED = config.get_bool('reverse_arbitrage.research.enabled', True)
REVERSE_RESEARCH_INTERVAL_SEC = config.get_float('reverse_arbitrage.research.snapshot_interval_sec', 60.0)
REVERSE_RESEARCH_MAX_ROWS = config.get_int('reverse_arbitrage.research.max_rows_per_snapshot', 400)

# 富化配置实例（快照推送、开仓检查共用）
_enrich_cfg = EnrichConfig(
    open_amount_usdt=OPEN_AMOUNT_USDT,
    funding_threshold_percentile=FUNDING_THRESHOLD_PERCENTILE,
    risk_relief_bps=RISK_RELIEF_BPS,
    spot_open_fee=SPOT_OPEN_FEE,
    spot_close_fee=SPOT_CLOSE_FEE,
    future_open_fee=FUTURE_OPEN_FEE,
    future_close_fee=FUTURE_CLOSE_FEE,
    close_threshold_col=config.get_str('trade.vwap.close_threshold_percentile', 'close_basis_p20').strip(),
    open_vwap_basis_threshold_bps=OPEN_VWAP_BASIS_THRESHOLD_BPS,
    funding_entry_enabled=config.get_bool('trade.funding_adjusted_entry.enabled', True),
    funding_entry_capture_ratio=config.get_float('trade.funding_adjusted_entry.funding_capture_ratio', 0.5),
    funding_entry_slippage_buffer_bps=config.get_float('trade.funding_adjusted_entry.slippage_buffer_bps', 10.0),
    funding_entry_min_expected_edge_bps=config.get_float('trade.funding_adjusted_entry.min_expected_edge_bps', 0.0),
    funding_entry_strong_funding_24h_bps=config.get_float('trade.funding_adjusted_entry.strong_funding_24h_bps', 50.0),
    funding_entry_discount_ratio=config.get_float('trade.funding_adjusted_entry.discount_ratio', 0.2),
    funding_entry_max_discount_bps=config.get_float('trade.funding_adjusted_entry.max_funding_discount_bps', 10.0),
)

# 盈亏计算配置实例（持仓实时推送用）
_pnl_cfg = PnlConfig(
    open_amount_usdt=OPEN_AMOUNT_USDT,
    spot_open_fee=SPOT_OPEN_FEE,
    spot_close_fee=SPOT_CLOSE_FEE,
    future_open_fee=FUTURE_OPEN_FEE,
    future_close_fee=FUTURE_CLOSE_FEE,
    future_taker_open_fee=FUTURE_TAKER_OPEN_FEE,
    future_taker_close_fee=FUTURE_TAKER_CLOSE_FEE,
    risk_relief_bps=RISK_RELIEF_BPS,
    margin_default_mmr=config.get_float('margin.default_maintenance_rate', 0.005),
)
_reverse_pnl_cfg = ReversePnlConfig(open_amount_usdt=OPEN_AMOUNT_USDT)

_reverse_cfg = ReverseArbitrageConfig(
    open_amount_usdt=OPEN_AMOUNT_USDT,
    spot_open_fee=SPOT_OPEN_FEE,
    spot_close_fee=SPOT_CLOSE_FEE,
    future_open_fee=FUTURE_OPEN_FEE,
    future_close_fee=FUTURE_CLOSE_FEE,
    orderbook_coverage_threshold=ORDERBOOK_COVERAGE_THRESHOLD,
    funding_capture_ratio=config.get_float('reverse_arbitrage.funding_capture_ratio', 0.5),
    funding_carry_enabled=config.get_bool('reverse_arbitrage.funding_carry.enabled', True),
    funding_carry_min_24h_bps=config.get_float('reverse_arbitrage.funding_carry.min_24h_bps', 80.0),
    funding_carry_max_next_funding_min=config.get_float(
        'reverse_arbitrage.funding_carry.max_next_funding_min',
        60.0,
    ),
    funding_carry_min_margin_edge_bps=config.get_float(
        'reverse_arbitrage.funding_carry.min_margin_edge_bps',
        50.0,
    ),
    funding_carry_basis_relax_bps=config.get_float('reverse_arbitrage.funding_carry.basis_relax_bps', 30.0),
)

# 服务生命周期管理器（在 lifespan 中初始化）
svc: Optional[OrderBookDataClient] = None

# 运行时全局状态（广播相关）
event_loop: Optional[asyncio.AbstractEventLoop] = None
broadcast_queue: Optional[asyncio.Queue] = None
ws_clients: Set[WebSocket] = set()
reverse_ws_clients: Set[WebSocket] = set()
event_ws_clients: Set[WebSocket] = set()
last_broadcast_time = 0.0
pending_broadcast = False

# 元数据缓存（启动时加载）
_contract_meta: Dict[str, Dict] = {}
_spot_meta: Dict[str, Dict] = {}
_threshold_meta: Dict[str, float] = {}
_vwap_threshold_meta: Dict[str, float] = {}  # base_asset -> threshold_bps
_close_vwap_threshold_meta: Dict[str, Dict] = {}  # base_asset -> {close_basis_p10..p40}
_reverse_vwap_threshold_meta: Dict[str, Dict] = {}  # base_asset -> {reverse_open_basis_p20, reverse_close_basis_p20}
_funding_rate_p40_meta: Dict[str, float] = {}  # base_asset -> percentile_40费率(止盈用)
_funding_support_meta: Dict[str, Dict] = {}  # base_asset -> 最近已结算 funding 均值
_asset_tier_meta: Dict[str, str] = {}  # base_asset -> strategy_tier
_asset_profile_meta: Dict[str, Dict] = {}  # base_asset -> market_profile metadata
_latest_account_summary: Optional[Dict] = None  # 最新交易所资金快照汇总
_latest_account_summary_ts: float = 0.0
_gate_position_risk_cache: List[Dict] = []
_gate_position_risk_cache_ts: float = 0.0
_gate_cross_risk_monitor: Optional[GateCrossRiskMonitor] = None
_gate_cross_risk_notifier: Optional[GateCrossRiskNotifier] = None
_last_margin_danger_force_refresh_ts: float = 0.0
_delist_risk_report: Dict = {'items': [], 'summary': {'total': 0, 'critical': 0, 'warning': 0}}
_delist_risk_report_ts: float = 0.0

# 开仓/平仓检查执行器单例（避免每次循环重复创建 ExecutorClient）
_trading_executor: Optional['TradingExecutor'] = None
_closing_executor: Optional['ClosingExecutor'] = None
_reverse_signal_monitor: Optional['ReverseSignalMonitor'] = None
_reverse_executor_client: Optional[ExecutorClient] = None
_gate_risk_event_monitor = None
_critical_open_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='critical-open')
_critical_close_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='critical-close')
_reconciliation_trigger_lock = threading.Lock()
_reconciliation_trigger_running: bool = False

# ───── 开仓暂停开关 ─────
_open_paused: bool = True                    # 正向开仓启动默认暂停，需人工恢复；平仓不受影响
_reverse_open_paused: bool = True            # 反向开仓启动默认暂停，需人工恢复；正向不受影响

# ───── 交易链路连通性熔断 ─────
# 仅实盘模式下启用：Binance + Gate 任一不通即禁止交易
_exchange_connectivity_ok: bool = True       # 默认 True（虚拟模式不受影响）
_is_real_executor: bool = False              # 是否接入真实成交引擎
_connectivity_detail: Dict = {}              # 最近一次连通性检查详情
_connectivity_check_interval: int = 30       # 连通性检查间隔（秒）

# 合并+对冲指标缓存（避免多个后台循环重复计算）
_cached_merged_rows: List[Dict] = []
_cached_merged_ts: float = 0.0
_cached_trading_rows: List[Dict] = []
_cached_trading_ts: float = 0.0
TRADING_SCAN_CACHE_SEC = config.get_float('orderbook.trading_scan_cache_sec', 1.0)

# 完整广播 payload 缓存（预序列化 JSON 字符串，避免多客户端重复序列化）
_cached_payload_json: str = ''
_cached_payload_ts: float = 0.0
_cached_reverse_payload_json: str = ''
_cached_reverse_payload_ts: float = 0.0
_reverse_borrow_cache: Dict[str, Dict] = {}
_reverse_borrow_cache_ts: float = 0.0


def _build_binance_margin_borrow_client() -> Optional[BinanceMarginBorrowClient]:
    if not REVERSE_BORROW_AUTO_ENABLED:
        return None

    trade_mode = config.get_trade_mode()
    creds = get_binance_credentials('reverse', mainnet=trade_mode == 'live')
    api_key = creds.api_key
    api_secret = creds.api_secret
    if not api_key or not api_secret:
        return None

    base_url = config.get_str(
        'reverse_arbitrage.binance_margin.base_url',
        'https://api1.binance.com' if trade_mode == 'live' else 'https://testnet.binance.vision',
    ).rstrip('/')
    return BinanceMarginBorrowClient(BinanceMarginBorrowConfig(
        base_url=base_url,
        api_key=api_key,
        api_secret=api_secret,
        timeout_sec=config.get_int('reverse_arbitrage.binance_margin.timeout_sec', 10),
        recv_window_ms=config.get_int('reverse_arbitrage.binance_margin.recv_window_ms', 5000),
    ))


def _get_reverse_borrow_meta_from_config() -> Dict[str, Dict]:
    """读取手工借币数据覆盖表，后续可替换为 Binance Margin API 缓存。"""
    raw = config.get('reverse_arbitrage.borrow_overrides', {}) or {}
    if not isinstance(raw, dict):
        return {}

    result: Dict[str, Dict] = {}
    for asset, item in raw.items():
        if not isinstance(item, dict):
            continue
        base_asset = _normalize_base_asset(asset)
        hourly_rate = item.get('hourly_interest_rate')
        if hourly_rate is None and item.get('hourly_interest_percent') is not None:
            hourly_rate = float(item.get('hourly_interest_percent')) / 100.0
        result[base_asset] = {
            'borrowable': item.get('borrowable', True),
            'hourly_interest_rate': float(hourly_rate) if hourly_rate is not None else None,
            'borrow_limit': float(item['borrow_limit']) if item.get('borrow_limit') is not None else None,
        }
    return result


def _get_reverse_borrow_meta(assets: List[str]) -> tuple[Dict[str, Dict], str]:
    """获取反向策略借币数据，优先自动刷新，失败时叠加手工覆盖。"""
    global _reverse_borrow_cache, _reverse_borrow_cache_ts

    overrides = _get_reverse_borrow_meta_from_config()
    clean_assets = sorted({_normalize_base_asset(asset) for asset in assets if _normalize_base_asset(asset)})
    if not clean_assets:
        return overrides, 'config' if overrides else 'none'

    client = _build_binance_margin_borrow_client()
    now = time.time()
    missing_assets = set(clean_assets) - set(_reverse_borrow_cache)
    if client and (
        not _reverse_borrow_cache
        or missing_assets
        or now - _reverse_borrow_cache_ts >= REVERSE_BORROW_CACHE_TTL_SEC
    ):
        try:
            _reverse_borrow_cache = client.get_cross_margin_borrow_meta(
                clean_assets,
                max_borrowable_assets=REVERSE_BORROW_MAX_ASSETS,
            )
            _reverse_borrow_cache_ts = now
        except Exception as exc:
            logger.warning(f'Binance Margin 借币数据刷新失败: {exc}')

    merged = dict(_reverse_borrow_cache)
    merged.update(overrides)
    if _reverse_borrow_cache:
        return merged, 'binance_margin'
    if overrides:
        return merged, 'config'
    return {}, 'none'


def _get_reverse_realtime_borrow_meta(asset: str) -> Dict[str, Dict]:
    """旁路风控专用：只复核单个标的当前真实可借额度。"""
    global _reverse_borrow_cache, _reverse_borrow_cache_ts

    base_asset = _normalize_base_asset(asset)
    if not base_asset:
        return {}

    client = _build_binance_margin_borrow_client()
    cached = dict(_reverse_borrow_cache.get(base_asset) or {})
    if not client:
        return {base_asset: cached} if cached else {}

    try:
        borrowable = client.get_max_borrowable(base_asset)
        amount = borrowable.get('amount')
        limit = borrowable.get('borrowLimit')
        cached['max_borrowable_amount'] = amount
        cached['account_borrow_limit'] = limit
        cached['borrow_limit'] = amount
        if amount is not None:
            cached['borrowable'] = amount > 0
    except Exception as exc:
        logger.warning(f'反向旁路实时借币额度复核失败 | {base_asset} | {exc}')
        cached['max_borrowable_amount'] = 0.0
        cached['account_borrow_limit'] = cached.get('account_borrow_limit') or cached.get('borrow_limit')
        cached['borrow_limit'] = 0.0
        cached['borrowable'] = False
        cached['borrow_unavailable_reason'] = 'pre_gate_max_borrowable_unavailable'

    if cached:
        _reverse_borrow_cache[base_asset] = cached
        _reverse_borrow_cache_ts = time.time()
        return {base_asset: cached}
    return {}


def _normalize_base_asset(value) -> str:
    """统一元数据索引键，避免 DB 小写/混合大小写导致前端阈值匹配失败。"""
    return str(value or '').strip().upper()


def _get_merged_rows() -> List[Dict]:
    """获取合并+对冲指标数据，带短期缓存避免重复计算

    缓存有效期 = BROADCAST_THROTTLE_SEC（默认 1 秒）。
    同一秒内多个消费者（广播/开仓/平仓/持仓/VWAP快照）共享同一份计算结果。
    """
    global _cached_merged_rows, _cached_merged_ts
    now = time.time()
    if _cached_merged_rows and (now - _cached_merged_ts) < BROADCAST_THROTTLE_SEC:
        return [dict(row) for row in _cached_merged_rows]

    rows = svc.get_merged_rows() if svc else []
    rows = calculate_hedge_metrics(rows, _contract_meta, _spot_meta, OPEN_AMOUNT_USDT)

    _cached_merged_rows = [dict(row) for row in rows]
    _cached_merged_ts = now
    return [dict(row) for row in _cached_merged_rows]



def fetch_threshold_meta() -> tuple:
    """从 mi_gate_future_funding_rate_threshold 表一次性加载：
    1. 前端过滤用阈值（按 contract 索引）
    2. 止盈用 percentile_40（按 base_asset 索引）

    Returns:
        (threshold_meta, funding_rate_p40_meta)
        - threshold_meta: Dict[contract, float] — 前端展示/过滤用
        - funding_rate_p40_meta: Dict[base_asset, float] — 止盈阈值计算用
    """
    sql = f"SELECT contract, {FUNDING_THRESHOLD_PERCENTILE}, percentile_40 FROM mi_gate_future_funding_rate_threshold"
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            threshold_result = {}
            p40_result = {}
            for row in rows:
                contract = row['contract']
                # 前端过滤用（按 contract 索引）
                val = row[FUNDING_THRESHOLD_PERCENTILE]
                threshold_result[contract] = float(val) if val is not None else None
                # 止盈用 percentile_40（按 base_asset 索引）
                p40_val = row['percentile_40']
                if p40_val is not None:
                    base_asset = contract.replace('_USDT', '').replace('_usdt', '')
                    p40_result[base_asset] = float(p40_val)
            logger.info(f'已加载费率阈值元数据 {len(threshold_result)} 条（含p40 {len(p40_result)} 条）')
            return threshold_result, p40_result
    except Exception as e:
        logger.error(f'加载费率阈值元数据失败: {e}')
        return {}, {}


def fetch_funding_support_meta(window_hours: float) -> Dict[str, Dict]:
    """加载最近已结算 funding_rate_24h 均值，供普通开仓门槛和监控页展示使用。"""
    cutoff_ts = int(time.time() - max(float(window_hours or 0), 1.0) * 3600)
    sql = """
        SELECT contract, AVG(funding_rate_24h) AS avg_rate_24h, COUNT(*) AS samples
        FROM mi_gate_future_his_funding_rates
        WHERE funding_rate_24h IS NOT NULL
          AND timestamp >= %s
          AND timestamp <= UNIX_TIMESTAMP(NOW())
        GROUP BY contract
    """
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (cutoff_ts,))
            rows = cursor.fetchall()
            result: Dict[str, Dict] = {}
            for row in rows:
                avg_rate = row.get('avg_rate_24h')
                if avg_rate is None:
                    continue
                contract = str(row.get('contract') or '')
                base_asset = _normalize_base_asset(
                    contract.replace('_USDT', '').replace('_usdt', '')
                )
                if not base_asset:
                    continue
                result[base_asset] = {
                    'funding_rate_24h_avg_bps': float(avg_rate) * 10000.0,
                    'funding_rate_24h_avg_samples': int(row.get('samples') or 0),
                    'funding_rate_24h_avg_window_hours': float(window_hours),
                }
            logger.info(f'已加载已结算资金费率均值 {len(result)} 条（窗口={window_hours:g}h）')
            return result
    except Exception as e:
        logger.error(f'加载已结算资金费率均值失败: {e}', exc_info=True)
        return {}


def fetch_vwap_threshold_meta() -> Dict[str, Dict[str, float]]:
    """从 mi_vwap_basis_threshold 加载最新一天的按标的VWAP开仓基差阈值（p10 + p20）

    返回: base_asset -> {'p10': float, 'p20': float}
    - p10: 直接开仓阈值（降序前10%，更高基差）
    - p20: 回落确认阈值（降序前20%，中高基差）
    """
    sql = """
        SELECT base_asset, open_basis_p10, open_basis_p20
        FROM mi_vwap_basis_threshold
        WHERE calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
          AND (open_basis_p10 IS NOT NULL OR open_basis_p20 IS NOT NULL)
    """
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                entry = {}
                if row.get('open_basis_p10') is not None:
                    entry['p10'] = float(row['open_basis_p10'])
                if row.get('open_basis_p20') is not None:
                    entry['p20'] = float(row['open_basis_p20'])
                if entry:
                    result[_normalize_base_asset(row['base_asset'])] = entry
            logger.info(f'已加载VWAP开仓基差阈值 {len(result)} 条 (p10+p20)')
            return result
    except Exception as e:
        logger.error(f'加载VWAP开仓基差阈值失败: {e}')
        return {}


def fetch_close_vwap_threshold_meta() -> Dict[str, Dict]:
    """从 mi_vwap_basis_threshold 加载配置指定的平仓基差阈值列

    阈值列由 config.yaml 的 trade.vwap.close_threshold_percentile 决定，
    例如 close_basis_p20。前端的 close_vwap_threshold_bps 直接展示这一列。

    返回格式: base_asset -> {open_basis_p20, <close_threshold_col>}
    若加载失败或结果为空，返回空字典 {} （调用方需保留旧缓存）

    注意：不能只按整表 MAX(calc_date) 加载。某些标的最新批次该配置列可能为
    NULL；此时旧日期明明有阈值，前端却会显示为空。这里按 base_asset 取
    最近一条“配置列非空”的记录。
    """
    table_name = 'mi_vwap_basis_threshold'
    close_threshold_col = config.get_str('trade.vwap.close_threshold_percentile', 'close_basis_p20').strip()

    sql = f"""
        SELECT t.base_asset, t.calc_date, t.open_basis_p20, t.{close_threshold_col}
        FROM {table_name} t
        JOIN (
            SELECT base_asset, MAX(calc_date) AS calc_date
            FROM {table_name}
            WHERE {close_threshold_col} IS NOT NULL
            GROUP BY base_asset
        ) latest
          ON latest.base_asset = t.base_asset
         AND latest.calc_date = t.calc_date
    """
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            if not rows:
                logger.warning(f'平仓VWAP基差阈值查询结果为空（{close_threshold_col} 无非空数据）')
                return {}
            result: Dict[str, Dict] = {}
            dates = set()
            for row in rows:
                ba = _normalize_base_asset(row['base_asset'])
                dates.add(str(row.get('calc_date')))
                entry = {
                    'open_basis_p20': float(row['open_basis_p20']) if row.get('open_basis_p20') is not None else None,
                    close_threshold_col: float(row[close_threshold_col]),
                }
                result[ba] = entry
            logger.info(
                f"已加载平仓VWAP基差阈值 {len(result)} 条"
                f"（字段={close_threshold_col}，按标的最近非空记录，日期数={len(dates)}）"
            )
            return result
    except Exception as e:
        logger.error(f'加载平仓VWAP基差阈值失败: {e}', exc_info=True)
        return {}


def fetch_reverse_vwap_threshold_meta() -> Dict[str, Dict]:
    """从 mi_vwap_basis_threshold 加载反向套利 open/close p20 阈值。"""
    sql = """
        SELECT base_asset, reverse_open_basis_p20, reverse_close_basis_p20
        FROM mi_vwap_basis_threshold
        WHERE calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
          AND (reverse_open_basis_p20 IS NOT NULL OR reverse_close_basis_p20 IS NOT NULL)
    """
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            result: Dict[str, Dict] = {}
            for row in rows:
                entry = {}
                if row.get('reverse_open_basis_p20') is not None:
                    entry['reverse_open_basis_p20'] = float(row['reverse_open_basis_p20'])
                if row.get('reverse_close_basis_p20') is not None:
                    entry['reverse_close_basis_p20'] = float(row['reverse_close_basis_p20'])
                if entry:
                    result[_normalize_base_asset(row['base_asset'])] = entry
            logger.info(f'已加载反向VWAP基差阈值 {len(result)} 条 (open/close p20)')
            return result
    except Exception as e:
        logger.error(f'加载反向VWAP基差阈值失败: {e}', exc_info=True)
        return {}


def build_payload() -> dict:
    """构建 WebSocket 推送载荷（Gate + Binance 合并宽表，附带开仓金额）"""
    rows = _get_merged_rows()

    # 重要：enrich_snapshot_fields 会就地修改 rows，但由于 _cached_merged_rows
    # 每秒重建一次，这里的就地修改不影响其他消费者
    enrich_snapshot_fields(
        rows, _contract_meta, _spot_meta, _threshold_meta,
        _vwap_threshold_meta, _enrich_cfg, _meta_update_time,
        _close_vwap_threshold_meta,
        _asset_profile_meta,
        _funding_support_meta,
    )

    return {
        'type': 'snapshot',
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'open_amount_usdt': OPEN_AMOUNT_USDT,
        'funding_threshold_percentile': FUNDING_THRESHOLD_PERCENTILE,
        'min_funding_rate_bps': MIN_FUNDING_RATE_BPS,
        'min_funding_support_bps': MIN_FUNDING_SUPPORT_BPS,
        'funding_support_window_hours': FUNDING_SUPPORT_WINDOW_HOURS,
        'funding_support_min_samples': FUNDING_SUPPORT_MIN_SAMPLES,
        'realtime_min_funding_rate_bps': REALTIME_MIN_FUNDING_RATE_BPS,
        'orderbook_coverage_threshold': ORDERBOOK_COVERAGE_THRESHOLD,
        'risk_relief_bps': RISK_RELIEF_BPS,
        'open_vwap_basis_threshold_bps': OPEN_VWAP_BASIS_THRESHOLD_BPS,
        'min_spot_volume_24h_usdt': MIN_SPOT_VOLUME_24H_USDT,
        'min_future_volume_24h_usdt': MIN_FUTURE_VOLUME_24H_USDT,
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms() if svc else None,
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms() if svc else None,
        'rows': rows,
    }


def _build_reverse_enriched_rows() -> tuple[List[Dict], Dict[str, Dict], str]:
    """构建反向机会富化行，供页面展示和反向信号监控共用。"""
    rows = _get_merged_rows()
    enrich_snapshot_fields(
        rows, _contract_meta, _spot_meta, _threshold_meta,
        _vwap_threshold_meta, _enrich_cfg, _meta_update_time,
        _close_vwap_threshold_meta,
        _asset_profile_meta,
        _funding_support_meta,
    )
    negative_assets = [
        row.get('base_asset')
        for row in rows
        if row.get('funding_rate_24h') is not None and float(row.get('funding_rate_24h') or 0) < 0
    ]
    borrow_meta, borrow_source = _get_reverse_borrow_meta(negative_assets)
    enrich_reverse_opportunities(
        rows,
        _contract_meta,
        _reverse_cfg,
        borrow_meta=borrow_meta,
        reverse_threshold_meta=_reverse_vwap_threshold_meta,
    )
    return rows, borrow_meta, borrow_source


def build_reverse_opportunities_payload() -> dict:
    """构建反向资金费率套利机会载荷（只读，不触发交易）。"""
    rows, borrow_meta, borrow_source = _build_reverse_enriched_rows()
    return {
        'type': 'reverse_opportunities',
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'open_amount_usdt': OPEN_AMOUNT_USDT,
        'orderbook_coverage_threshold': ORDERBOOK_COVERAGE_THRESHOLD,
        'reverse_margin_edge_threshold_bps': 0,
        'reverse_funding_carry': {
            'enabled': _reverse_cfg.funding_carry_enabled,
            'min_24h_bps': _reverse_cfg.funding_carry_min_24h_bps,
            'max_next_funding_min': _reverse_cfg.funding_carry_max_next_funding_min,
            'min_margin_edge_bps': _reverse_cfg.funding_carry_min_margin_edge_bps,
            'basis_relax_bps': _reverse_cfg.funding_carry_basis_relax_bps,
        },
        'borrow_data_available': bool(borrow_meta),
        'borrow_data_source': borrow_source,
        'borrow_cache_age_sec': (
            round(time.time() - _reverse_borrow_cache_ts, 1)
            if _reverse_borrow_cache_ts > 0
            else None
        ),
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms() if svc else None,
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms() if svc else None,
        'rows': rows,
    }


def record_reverse_research_snapshot_once(sample_source: str = 'manual') -> dict:
    """记录一批反向研究快照；仅用于观察分析，不参与开仓判断。"""
    rows, _borrow_meta, borrow_source = _build_reverse_enriched_rows()
    inserted = record_reverse_research_snapshot(
        rows,
        ReverseResearchConfig(
            open_amount_usdt=OPEN_AMOUNT_USDT,
            max_rows_per_snapshot=REVERSE_RESEARCH_MAX_ROWS,
        ),
        sample_source=sample_source,
    )
    return {
        'ok': True,
        'inserted': inserted,
        'borrow_data_source': borrow_source,
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }


def _refresh_delist_risk_report_once() -> Dict:
    """刷新下架风险报告；在后台线程执行，平仓关键路径只读结果。"""
    global _delist_risk_report, _delist_risk_report_ts
    lookahead_days = max(config.get_int('trade.close.delist_risk_lookahead_days', 30), 1)
    timeout_sec = max(config.get_int('trade.close.delist_risk_timeout_sec', 8), 1)
    report = DelistRiskMonitor(
        DelistRiskConfig(lookahead_days=lookahead_days, timeout_sec=timeout_sec)
    ).build_report()
    _delist_risk_report = report
    _delist_risk_report_ts = time.time()
    summary = report.get('summary') or {}
    risk_assets = sorted({
        str(item.get('base_asset') or '').strip().upper()
        for item in (report.get('items') or [])
        if str(item.get('base_asset') or '').strip()
    })
    assets_text = ','.join(risk_assets[:20]) if risk_assets else '-'
    if len(risk_assets) > 20:
        assets_text = f"{assets_text},...(+{len(risk_assets) - 20})"
    logger.info(
        '下架风险缓存刷新完成: total=%s critical=%s warning=%s assets=%s',
        summary.get('total', 0),
        summary.get('critical', 0),
        summary.get('warning', 0),
        assets_text,
    )
    return report


async def _delist_risk_refresh_loop():
    interval = max(config.get_int('trade.close.delist_risk_refresh_interval_sec', 900), 60)
    while True:
        try:
            await asyncio.to_thread(_refresh_delist_risk_report_once)
        except Exception as e:
            logger.warning(f'下架风险缓存刷新失败: {e}', exc_info=True)
        await asyncio.sleep(interval)


def build_payload_json() -> str:
    """构建并预序列化广播载荷 JSON 字符串（缓存复用，避免重复序列化）"""
    global _cached_payload_json, _cached_payload_ts
    now = time.time()
    # 与 merged_rows 缓存同步：同一秒内复用已序列化的 JSON
    if _cached_payload_json and (now - _cached_payload_ts) < BROADCAST_THROTTLE_SEC:
        return _cached_payload_json
    payload = build_payload()
    _cached_payload_json = _json_dumps(payload)
    _cached_payload_ts = now
    return _cached_payload_json


def build_reverse_opportunities_payload_json() -> str:
    """构建并预序列化反向机会广播载荷，和正向快照客户端隔离。"""
    global _cached_reverse_payload_json, _cached_reverse_payload_ts
    now = time.time()
    if _cached_reverse_payload_json and (now - _cached_reverse_payload_ts) < BROADCAST_THROTTLE_SEC:
        return _cached_reverse_payload_json
    payload = build_reverse_opportunities_payload()
    _cached_reverse_payload_json = _json_dumps(payload)
    _cached_reverse_payload_ts = now
    return _cached_reverse_payload_json


def schedule_broadcast():
    """调度盘口快照广播（启动阶段与运行阶段均推送，带节流）"""
    global pending_broadcast, last_broadcast_time

    if not svc or svc.state not in (SERVICE_RUNNING, SERVICE_STARTING):
        return

    now = time.time()
    if now - last_broadcast_time >= BROADCAST_THROTTLE_SEC:
        last_broadcast_time = now
        pending_broadcast = False
        payload = build_payload()
        if event_loop and broadcast_queue:
            asyncio.run_coroutine_threadsafe(broadcast_queue.put(payload), event_loop)
    else:
        if not pending_broadcast:
            pending_broadcast = True
            delay = BROADCAST_THROTTLE_SEC - (now - last_broadcast_time)
            threading_timer = threading.Timer(delay, _delayed_broadcast)
            threading_timer.daemon = True
            threading_timer.start()


def _delayed_broadcast():
    global last_broadcast_time, pending_broadcast
    if not svc or svc.state not in (SERVICE_RUNNING, SERVICE_STARTING):
        return
    pending_broadcast = False
    last_broadcast_time = time.time()
    payload = build_payload()
    if event_loop and broadcast_queue:
        asyncio.run_coroutine_threadsafe(broadcast_queue.put(payload), event_loop)


async def broadcast_worker():
    while True:
        payload = await broadcast_queue.get()
        payload_type = 'snapshot' if isinstance(payload, str) else payload.get('type')
        if payload_type == 'snapshot':
            targets = ws_clients
        elif payload_type == 'reverse_opportunities':
            targets = reverse_ws_clients
        else:
            targets = ws_clients | reverse_ws_clients | event_ws_clients
        # 预序列化一次，所有客户端共享同一份 JSON 字符串
        if isinstance(payload, str):
            text = payload  # 已经是预序列化的 JSON
        else:
            text = _json_dumps(payload)
        dead_clients = []
        for ws in list(targets):
            try:
                await ws.send_text(text)
            except Exception:
                dead_clients.append(ws)
        for ws in dead_clients:
            ws_clients.discard(ws)
            reverse_ws_clients.discard(ws)
            event_ws_clients.discard(ws)


async def _orderbook_broadcast_loop():
    """定时轮询广播盘口快照，替代每条 WS 消息触发的 callback 模式

    优势：
    - 彻底解耦「数据更新」与「广播推送」，消除 callback 风暴
    - 固定频率重建 payload，无论 WS 消息量多少，广播开销恒定
    - 无前端连接时自动跳过计算，降低空负CPU
    - 预序列化 JSON，多客户端共享同一份字符串
    """
    while True:
        await asyncio.sleep(BROADCAST_THROTTLE_SEC)
        try:
            if not svc or svc.state not in (SERVICE_RUNNING, SERVICE_STARTING):
                continue
            # 无前端连接时跳过计算
            if not ws_clients and not reverse_ws_clients:
                continue
            # 直接放入预序列化的 JSON 字符串，broadcast_worker 无需再次序列化
            if ws_clients:
                await broadcast_queue.put(build_payload_json())
            if reverse_ws_clients:
                await broadcast_queue.put(build_reverse_opportunities_payload_json())
        except Exception as e:
            logger.error(f"盘口广播失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global svc, event_loop, broadcast_queue

    event_loop = asyncio.get_running_loop()
    event_loop.set_exception_handler(_asyncio_exception_handler)
    broadcast_queue = asyncio.Queue()
    worker_task = asyncio.create_task(broadcast_worker())

    global _contract_meta, _spot_meta, _threshold_meta, _vwap_threshold_meta
    global _close_vwap_threshold_meta, _reverse_vwap_threshold_meta, _funding_rate_p40_meta
    global _funding_support_meta
    global _asset_tier_meta, _asset_profile_meta, _meta_update_time
    _contract_meta = fetch_contract_meta()
    _spot_meta = fetch_spot_meta()
    _asset_tier_meta = fetch_asset_tier_meta()
    _asset_profile_meta = fetch_asset_market_profile_meta()
    _threshold_meta, _funding_rate_p40_meta = fetch_threshold_meta()
    _funding_support_meta = fetch_funding_support_meta(FUNDING_SUPPORT_WINDOW_HOURS)
    _vwap_threshold_meta = fetch_vwap_threshold_meta()
    _close_vwap_threshold_meta = fetch_close_vwap_threshold_meta()
    _reverse_vwap_threshold_meta = fetch_reverse_vwap_threshold_meta()
    _meta_update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_print(
        f'已加载合约元数据 {len(_contract_meta)} 条，现货元数据 {len(_spot_meta)} 条，'
        f'标的分层 {len(_asset_tier_meta)} 条，行情画像 {len(_asset_profile_meta)} 条，'
        f'阈値元数据 {len(_threshold_meta)} 条，'
        f'VWAP阈値 {len(_vwap_threshold_meta)} 条，平仓阈値 {len(_close_vwap_threshold_meta)} 条，'
        f'反向阈値 {len(_reverse_vwap_threshold_meta)} 条，'
        f'费率p40 {len(_funding_rate_p40_meta)} 条，'
        f'已结算均费 {len(_funding_support_meta)} 条'
    )

    async def _refresh_meta_cache_loop():
        """定时刷新内存缓存（ETL由各任务的IntervalScheduler独立执行）"""
        # 获取所有 interval 任务的最小间隔作为缓存刷新频率
        interval_tasks = [t for t in ETL_TASKS if t.schedule == 'interval' and t.enabled]
        if interval_tasks:
            min_interval = min(t.interval_minutes for t in interval_tasks)
        else:
            min_interval = _etl_config.get('default_interval_minutes', 15)
        
        interval = min_interval * 60  # 转换为秒
        
        while True:
            await asyncio.sleep(interval)
            try:
                logger.info(f'开始定时刷新内存缓存 (间隔: {min_interval} 分钟)...')
                # 刷新内存缓存
                global _contract_meta, _spot_meta, _threshold_meta, _vwap_threshold_meta
                global _close_vwap_threshold_meta, _reverse_vwap_threshold_meta, _funding_rate_p40_meta
                global _funding_support_meta
                global _asset_tier_meta, _asset_profile_meta, _meta_update_time
                _contract_meta = fetch_contract_meta()
                _spot_meta = fetch_spot_meta()
                _asset_tier_meta = fetch_asset_tier_meta()
                _asset_profile_meta = fetch_asset_market_profile_meta()
                _threshold_meta, _funding_rate_p40_meta = fetch_threshold_meta()
                _funding_support_meta = fetch_funding_support_meta(FUNDING_SUPPORT_WINDOW_HOURS)
                _vwap_threshold_meta = fetch_vwap_threshold_meta()
                new_close_meta = fetch_close_vwap_threshold_meta()
                if new_close_meta:
                    _close_vwap_threshold_meta = new_close_meta
                else:
                    logger.warning(f'平仓VWAP基差阈值刷新结果为空，保留旧缓存（{len(_close_vwap_threshold_meta)} 条）')
                new_reverse_meta = fetch_reverse_vwap_threshold_meta()
                if new_reverse_meta:
                    _reverse_vwap_threshold_meta = new_reverse_meta
                else:
                    logger.warning(f'反向VWAP基差阈值刷新结果为空，保留旧缓存（{len(_reverse_vwap_threshold_meta)} 条）')
                _meta_update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logger.info(
                    f'内存缓存刷新完成: 合约 {len(_contract_meta)} 条, 现货 {len(_spot_meta)} 条, '
                    f'标的分层 {len(_asset_tier_meta)} 条, 行情画像 {len(_asset_profile_meta)} 条, '
                    f'阈値 {len(_threshold_meta)} 条, '
                    f'VWAP阈値 {len(_vwap_threshold_meta)} 条, 平仓阈値 {len(_close_vwap_threshold_meta)} 条, '
                    f'反向阈値 {len(_reverse_vwap_threshold_meta)} 条, '
                    f'费率p40 {len(_funding_rate_p40_meta)} 条, '
                    f'已结算均费 {len(_funding_support_meta)} 条'
                )
                # 重置执行器单例，下次循环用新元数据重建
                global _trading_executor, _closing_executor, _reverse_signal_monitor
                _trading_executor = None
                _closing_executor = None
                _reverse_signal_monitor = None
            except Exception as e:
                logger.error(f'内存缓存刷新失败: {e}')

    asyncio.create_task(_refresh_meta_cache_loop())
    asyncio.create_task(_gate_cross_risk_loop())
    asyncio.create_task(_open_position_loop())
    asyncio.create_task(_reverse_signal_loop())
    asyncio.create_task(_close_position_loop())
    asyncio.create_task(_position_funding_loop())
    asyncio.create_task(_account_capital_snapshot_loop())
    asyncio.create_task(_position_realtime_push())
    asyncio.create_task(_reverse_position_realtime_push())
    asyncio.create_task(_reconciliation_loop())
    asyncio.create_task(_vwap_snapshot_loop())
    asyncio.create_task(_reverse_research_snapshot_loop())
    asyncio.create_task(_stale_signal_cleanup_loop())
    asyncio.create_task(_server_metric_snapshot_loop())
    asyncio.create_task(_delist_risk_refresh_loop())
    _start_gate_risk_event_monitor()

    # 启动所有 daily 类型任务的定时调度器（如 VWAP 基差分位阈值每日 00:00 计算）
    start_daily_schedulers()

    svc = OrderBookDataClient()
    asyncio.create_task(_orderbook_broadcast_loop())
    asyncio.create_task(_connectivity_check_loop())

    log_print(f'盘口数据来自独立服务: {svc.base_url}')
    logger.info('开仓默认暂停，需在订单管理页面手动恢复开仓')

    yield

    if svc:
        svc.shutdown()
    _stop_gate_risk_event_monitor()
    stop_daily_schedulers()
    _critical_open_executor.shutdown(wait=False, cancel_futures=True)
    _critical_close_executor.shutdown(wait=False, cancel_futures=True)
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title='Cross-Exchange OrderBook Monitor', lifespan=lifespan)
app.include_router(trading_router)
app.include_router(auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173', '*'],  # 生产环境建议配置具体域名
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/api/health')
async def health():
    """健康检查接口（无需认证，用于系统监控）"""
    status = svc.get_status() if svc else {}
    return {
        'status': 'ok',
        'gate_ws_connected': status.get('gate_ws_connected', False),
        'binance_ws_connected': status.get('binance_ws_connected', False),
        'contracts': status.get('contracts', []),
        'spot_symbols': status.get('spot_symbols', []),
        'client_count': len(ws_clients),
        'event_client_count': len(event_ws_clients),
        'service_state': status.get('state', SERVICE_IDLE),
    }


@app.get('/api/service/status')
async def service_status():
    """获取服务运行状态（无需认证，用于前端健康检查）"""
    return svc.get_status() if svc else {'state': SERVICE_IDLE}


@app.get('/api/service/connections')
async def service_connections():
    """获取逐标的资产 OBU full 快照 / WS 订阅状态（无需认证，用于连接监控）"""
    if not svc:
        return {'items': [], 'state': SERVICE_IDLE}
    items = svc.get_connection_status()
    _attach_base_asset_status(items)
    return {
        'items': items,
        'state': svc.state,
        'gate_ws_connected': svc._gate_ws_connected(),
        'binance_ws_connected': svc._binance_ws_connected(),
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms(),
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms(),
        'exchange_risk_monitor': _gate_risk_event_monitor_status(),
    }


@app.get('/api/service/exchange-risk-monitor')
async def exchange_risk_monitor_status():
    """获取 Gate 私有 ADL/强平事件监听状态（无需认证，用于连接监控）。"""
    return _gate_risk_event_monitor_status()


@app.get('/api/service/diagnostics')
async def service_diagnostics():
    """透传盘口数据服务诊断指标，并附带主服务 HTTP 客户端耗时。"""
    if not svc:
        return {'state': SERVICE_IDLE}
    if hasattr(svc, 'get_diagnostics'):
        return svc.get_diagnostics()
    return {'state': svc.state}


@app.get('/api/service/server-metrics', dependencies=[Depends(verify_token_dependency)])
async def server_metrics(days: int = Query(7, ge=1, le=30)):
    """获取服务器关键指标历史快照。"""
    rows = await asyncio.to_thread(lambda: list_server_metrics(days=days))
    latest = rows[-1] if rows else await asyncio.to_thread(get_latest_server_metrics)
    return {
        'ok': True,
        'days': days,
        'latest': latest,
        'items': rows,
        'sample_interval_sec': config.get_int('server_metrics.interval_sec', 3600),
    }


@app.get('/api/service/exchange-connectivity')
async def exchange_connectivity():
    """获取交易链路连通性状态（无需认证，用于前端监控展示）

    返回：
    - is_real: 是否为实盘模式
    - all_ok: 双边交易所是否均连通
    - detail: 最近一次检查详情
    - trading_allowed: 是否允许交易（虚拟模式始终 True）
    """
    return {
        'is_real': _is_real_executor,
        'all_ok': _exchange_connectivity_ok,
        'trading_allowed': (not _is_real_executor) or _exchange_connectivity_ok,
        'detail': _connectivity_detail,
        'check_interval_sec': _connectivity_check_interval,
    }


@app.post('/api/service/retry-snapshot', dependencies=[Depends(verify_token_dependency)])
async def retry_snapshot(body: dict):
    """手动重试单个标的的 OBU 重订阅 + Binance WS 订阅"""
    base_asset = (body.get('base_asset') or '').strip()
    if not base_asset:
        raise HTTPException(status_code=400, detail='base_asset 不能为空')
    if not svc:
        raise HTTPException(status_code=400, detail='服务未初始化')
    loop = asyncio.get_event_loop()
    ok, message = await loop.run_in_executor(None, svc.retry_contract, base_asset)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {'ok': True, 'message': message}


@app.post('/api/service/retry-all-failed', dependencies=[Depends(verify_token_dependency)])
async def retry_all_failed():
    """一键重连：对所有快照失败或无实时数据的标的批量重试"""
    if not svc:
        raise HTTPException(status_code=400, detail='服务未初始化')
    if svc.state != SERVICE_RUNNING:
        raise HTTPException(status_code=400, detail='服务未运行，无法重试')

    # 找出所有异常标的
    connections = svc.get_connection_status()
    failed_assets = [
        c['base_asset'] for c in connections
        if c['gate_snapshot_status'] == 'failed'
        or (not c['gate_receiving_data'] and c['gate_snapshot_status'] != 'pending')
        or not c['binance_receiving_data']
    ]

    if not failed_assets:
        return {'ok': True, 'message': '所有连接正常，无需重试', 'results': []}

    # 在线程池中执行同步阻塞的重试操作，避免卡住 event loop
    loop = asyncio.get_event_loop()

    def _do_retry_all():
        results = []
        for asset in failed_assets:
            ok, msg = svc.retry_contract(asset)
            results.append({'base_asset': asset, 'ok': ok, 'message': msg})
        return results

    results = await loop.run_in_executor(None, _do_retry_all)

    success_count = sum(1 for r in results if r['ok'])
    return {
        'ok': True,
        'message': f'已重试 {len(failed_assets)} 个标的，成功 {success_count} 个',
        'results': results,
    }


@app.get('/api/orderbook/snapshot', dependencies=[Depends(verify_token_dependency)])
async def orderbook_snapshot():
    return build_payload()


@app.get('/api/reverse-arbitrage/opportunities', dependencies=[Depends(verify_token_dependency)])
async def reverse_arbitrage_opportunities():
    return build_reverse_opportunities_payload()


@app.get('/api/reverse-research/analysis', dependencies=[Depends(verify_token_dependency)])
async def reverse_research_analysis(
    hours: int = Query(24, ge=1, le=168),
    view: str = Query('negative'),
    keyword: str = Query('', max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=5000),
):
    return get_reverse_research_page(
        hours=hours,
        view=view,
        keyword=keyword,
        page=page,
        page_size=page_size,
        open_amount_usdt=OPEN_AMOUNT_USDT,
        funding_capture_ratio=_reverse_cfg.funding_capture_ratio,
        fee_cost_bps=(SPOT_OPEN_FEE + SPOT_CLOSE_FEE + FUTURE_OPEN_FEE + FUTURE_CLOSE_FEE) * 10000.0,
    )


@app.post('/api/reverse-research/collect', dependencies=[Depends(verify_token_dependency)])
async def reverse_research_collect():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: record_reverse_research_snapshot_once('manual'),
    )


@app.get('/api/reverse-funding/predictions', dependencies=[Depends(verify_token_dependency)])
async def reverse_funding_predictions(
    threshold: float = Query(-0.006, ge=-1.0, le=0.0),
    lookback_days: int = Query(30, ge=3, le=90),
    keyword: str = Query('', max_length=64),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=10, le=5000),
    prefer_stored: bool = Query(True),
    follow_score_filter: bool = Query(False),
    min_follow_score: float = Query(50.0, ge=0.0),
    funding_down_filter: bool = Query(False),
    min_funding_drop_bps: float = Query(5.0, ge=0.0),
    borrow_drop_filter: bool = Query(False),
    min_borrow_pressure_score: float = Query(12.0, ge=0.0),
    min_capacity_drawdown_pct: float = Query(2.0, ge=0.0),
    min_capacity_drop_usdt: float = Query(5.0, ge=0.0),
    min_borrow_drop_pct: float = Query(20.0, ge=0.0),
    history_high_negative_filter: bool = Query(False),
    probability_filter: bool = Query(False),
    min_p_next_2: float = Query(0.20, ge=0.0, le=1.0),
    min_p_next_3: float = Query(0.25, ge=0.0, le=1.0),
    confidence_filter: bool = Query(False),
    min_confidence: float = Query(0.50, ge=0.0, le=1.0),
    negative_funding_filter: bool = Query(False),
    borrowable_filter: bool = Query(False),
    capacity_filter: bool = Query(False),
    min_borrow_capacity_usdt: float = Query(100.0, ge=0.0),
    borrow_cost_filter: bool = Query(False),
    max_borrow_cost_ratio: float = Query(1.0, ge=0.0, le=10.0),
):
    return await asyncio.to_thread(
        lambda: get_reverse_funding_prediction_page(
            threshold_rate=threshold,
            lookback_days=lookback_days,
            keyword=keyword,
            page=page,
            page_size=page_size,
            prefer_stored=prefer_stored,
            filter_options={
                'follow_score_enabled': follow_score_filter,
                'min_follow_score': min_follow_score,
                'funding_down_enabled': funding_down_filter,
                'min_funding_drop_bps': min_funding_drop_bps,
                'borrow_drop_enabled': borrow_drop_filter,
                'min_borrow_pressure_score': min_borrow_pressure_score,
                'min_capacity_drawdown_pct': min_capacity_drawdown_pct,
                'min_capacity_drop_usdt': min_capacity_drop_usdt,
                'min_borrow_drop_pct': min_borrow_drop_pct,
                'history_high_negative_enabled': history_high_negative_filter,
                'probability_enabled': probability_filter,
                'min_p_next_2': min_p_next_2,
                'min_p_next_3': min_p_next_3,
                'confidence_enabled': confidence_filter,
                'min_confidence': min_confidence,
                'negative_funding_enabled': negative_funding_filter,
                'borrowable_enabled': borrowable_filter,
                'capacity_enabled': capacity_filter,
                'min_borrow_capacity_usdt': min_borrow_capacity_usdt,
                'borrow_cost_enabled': borrow_cost_filter,
                'max_borrow_cost_ratio': max_borrow_cost_ratio,
            },
        )
    )


@app.post('/api/reverse-funding/predictions/refresh', dependencies=[Depends(verify_token_dependency)])
async def reverse_funding_predictions_refresh(
    threshold: float = Query(-0.006, ge=-1.0, le=0.0),
    lookback_days: int = Query(30, ge=3, le=90),
):
    return await asyncio.to_thread(
        lambda: refresh_reverse_funding_predictions(
            threshold_rate=threshold,
            lookback_days=lookback_days,
        )
    )


@app.post('/api/service/start', dependencies=[Depends(verify_token_dependency)])
async def service_start():
    ok, message = svc.start()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@app.post('/api/service/stop', dependencies=[Depends(verify_token_dependency)])
async def service_stop():
    ok, message = svc.stop()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@app.post('/api/trading/open/pause', dependencies=[Depends(verify_token_dependency)])
async def pause_open():
    """暂停正向开仓（平仓不受影响）"""
    global _open_paused
    _open_paused = True
    logger.info('⏸ 正向开仓已暂停（手动操作）')
    return {'ok': True, 'open_paused': True}


@app.post('/api/trading/open/resume', dependencies=[Depends(verify_token_dependency)])
async def resume_open():
    """恢复正向开仓"""
    global _open_paused
    _open_paused = False
    logger.info('▶ 正向开仓已恢复（手动操作）')
    return {'ok': True, 'open_paused': False}


@app.get('/api/trading/open/status')
async def open_status():
    """查询正向开仓暂停状态（无需认证）"""
    return {
        'open_paused': _open_paused,
        'reverse_open_paused': _reverse_open_paused,
        'min_available_ratio': config.get_float('trade.open.min_available_ratio', 0.10),
        'min_binance_available_ratio': config.get_float(
            'trade.open.min_binance_available_ratio',
            config.get_float('trade.open.min_available_ratio', 0.10),
        ),
        'min_gate_available_ratio': config.get_float(
            'trade.open.min_gate_available_ratio',
            config.get_float('trade.open.min_available_ratio', 0.10),
        ),
        'max_asset_exposure_ratio': config.get_float('trade.open.max_asset_exposure_ratio', 0.10),
    }


@app.post('/api/trading/reverse-open/pause', dependencies=[Depends(verify_token_dependency)])
async def pause_reverse_open():
    """暂停反向开仓/反向信号监控（正向不受影响）"""
    global _reverse_open_paused
    _reverse_open_paused = True
    logger.info('⏸ 反向开仓已暂停（手动操作）')
    return {'ok': True, 'reverse_open_paused': True}


@app.post('/api/trading/reverse-open/resume', dependencies=[Depends(verify_token_dependency)])
async def resume_reverse_open():
    """恢复反向开仓/反向信号监控"""
    global _reverse_open_paused
    _reverse_open_paused = False
    logger.info('▶ 反向开仓已恢复（手动操作）')
    return {'ok': True, 'reverse_open_paused': False}


@app.get('/api/trading/reverse-open/status')
async def reverse_open_status():
    """查询反向开仓暂停状态（无需认证）"""
    return {
        'reverse_open_paused': _reverse_open_paused,
        'max_total_positions': config.get_int('reverse_arbitrage.execution.max_total_positions', 10),
        'max_positions_per_asset': config.get_int('reverse_arbitrage.execution.max_positions_per_asset', 1),
    }


@app.get('/api/trading/reverse-positions/realtime', dependencies=[Depends(verify_token_dependency)])
async def reverse_positions_realtime(
    status: Optional[str] = Query(None),
    order_side: Optional[str] = Query(None),
    exchange_risk: bool = Query(False),
    base_asset: Optional[str] = Query(None),
    days: int = Query(90, ge=1, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=5000),
):
    """反向持仓实时监控视图：持久化持仓 + 当前平仓 VWAP + 浮动/总盈亏。"""
    return _load_reverse_positions_with_realtime_pnl(
        status=status,
        order_side=order_side,
        exchange_risk=exchange_risk,
        base_asset=base_asset,
        days=days,
        page=page,
        page_size=page_size,
    )


def _find_orderbook_row_by_base_asset(base_asset: str) -> Optional[Dict]:
    ba = (base_asset or '').strip().upper()
    if not ba:
        return None
    for row in _get_merged_rows():
        if (row.get('base_asset') or '').strip().upper() == ba:
            return row
    return None


async def _ensure_orderbook_row_for_close(base_asset: str) -> Dict:
    """手动平仓前确保持仓标的有盘口；缺失时强制补订阅后短暂等待。"""
    ba = (base_asset or '').strip().upper()
    orderbook_row = _find_orderbook_row_by_base_asset(ba)
    if orderbook_row:
        return orderbook_row

    if not svc:
        raise HTTPException(status_code=503, detail='服务未初始化，无法执行平仓')

    loop = asyncio.get_running_loop()
    try:
        ok, message = await loop.run_in_executor(None, svc.retry_contract, ba, True)
    except Exception as e:
        logger.warning(f'手动平仓补订阅 {ba} 失败: {e}', exc_info=True)
        raise HTTPException(status_code=503, detail=f'标的 {ba} 无盘口数据，补订阅失败: {e}')

    if not ok:
        raise HTTPException(status_code=503, detail=f'标的 {ba} 无盘口数据，补订阅失败: {message}')

    for _ in range(6):
        await asyncio.sleep(0.5)
        orderbook_row = _find_orderbook_row_by_base_asset(ba)
        if orderbook_row:
            return orderbook_row

    raise HTTPException(status_code=503, detail=f'标的 {ba} 已补订阅但暂未收到完整盘口，无法执行平仓: {message}')


def _get_live_gate_cross_risk_snapshot() -> Optional[Dict]:
    monitor = _gate_cross_risk_monitor
    return monitor.get_snapshot() if monitor is not None else None


def _build_live_gate_cross_risk_payload(
    snapshot: Optional[Dict],
    *,
    now_ts: Optional[float] = None,
) -> Dict:
    risk = dict(snapshot) if isinstance(snapshot, dict) else {
        'enabled': True,
        'status': 'unknown',
        'status_label': '未知',
        'source': 'gate_cross_risk_monitor',
        'error': 'Gate全仓风险采集尚未产生快照',
        'account_fetched_at_ts': None,
        'positions_fetched_at_ts': None,
    }
    risk.update(gate_cross_risk_health(
        risk,
        now_ts=now_ts,
        max_age_sec=config.get_float('account_capital.gate_cross_risk.max_age_sec', 5.0),
    ))
    return risk


def _get_gate_cross_risk_notifier() -> GateCrossRiskNotifier:
    global _gate_cross_risk_notifier
    if _gate_cross_risk_notifier is None:
        _gate_cross_risk_notifier = build_default_gate_cross_risk_notifier()
    return _gate_cross_risk_notifier


def _refresh_gate_cross_risk_once() -> Dict:
    global _gate_cross_risk_monitor
    if _gate_cross_risk_monitor is None:
        _gate_cross_risk_monitor = build_default_gate_cross_risk_monitor()
    snapshot = _gate_cross_risk_monitor.refresh()
    _get_gate_cross_risk_notifier().record(datetime.now(), snapshot)
    return snapshot


def _record_gate_cross_risk_collection_failure(exc: Exception) -> int:
    risk = _build_live_gate_cross_risk_payload({
        'enabled': True,
        'status': 'unknown',
        'status_label': '未知',
        'source': 'gate_cross_risk_loop',
        'error': str(exc)[:300],
        'account_fetched_at_ts': None,
        'positions_fetched_at_ts': None,
    })
    return _get_gate_cross_risk_notifier().record(datetime.now(), risk)


@app.get(
    '/api/trading/capital/gate-cross-risk/live',
    dependencies=[Depends(verify_token_dependency)],
)
async def get_live_gate_cross_risk():
    """Return the second-level Gate cross-risk snapshot and input health."""
    return {'risk': _build_live_gate_cross_risk_payload(_get_live_gate_cross_risk_snapshot())}


def _account_summary_with_live_gate_cross_risk() -> Optional[Dict]:
    risk = _get_live_gate_cross_risk_snapshot()
    if not isinstance(risk, dict):
        return _latest_account_summary

    summary = dict(_latest_account_summary or {})
    gate_summary = dict(summary.get('gate') or {})
    gate_summary['cross_risk'] = risk
    summary['gate'] = gate_summary
    if not summary.get('snapshot_at'):
        summary['snapshot_at'] = risk.get('fetched_at')
    return summary


def _configure_closing_executor(executor):
    if svc:
        executor.set_orderbook_managers(svc.gate_manager, svc.spot_manager)
    if hasattr(executor, 'set_delist_risk_report'):
        executor.set_delist_risk_report(_delist_risk_report)
    if hasattr(executor, 'set_reconciliation_trigger'):
        executor.set_reconciliation_trigger(_trigger_reconciliation_once)
    if hasattr(executor, 'set_gate_cross_risk_provider'):
        executor.set_gate_cross_risk_provider(_get_live_gate_cross_risk_snapshot)
    return executor


def _trigger_reconciliation_once(reason: str, base_asset: str = ''):
    """后台触发一轮对账，缩短风险平仓部分成交后的兜底延迟。"""
    global _reconciliation_trigger_running
    if config.get_trade_mode() == 'virtual':
        return
    if not config.get_bool('reconciliation.enabled', True):
        return
    with _reconciliation_trigger_lock:
        if _reconciliation_trigger_running:
            logger.warning(
                "即时对账已在运行，跳过重复触发 | asset=%s | reason=%s",
                base_asset, reason,
            )
            return
        _reconciliation_trigger_running = True

    def _worker():
        global _reconciliation_trigger_running
        try:
            result = build_default_reconciler().run_once()
            logger.warning(
                "风险平仓后即时对账完成 | asset=%s | reason=%s | result=%s",
                base_asset, reason, result,
            )
        except Exception as e:
            logger.error(
                "风险平仓后即时对账失败 | asset=%s | reason=%s | %s",
                base_asset, reason, e, exc_info=True,
            )
        finally:
            with _reconciliation_trigger_lock:
                _reconciliation_trigger_running = False

    thread = threading.Thread(
        target=_worker,
        name=f"reconciliation-trigger-{str(base_asset or 'risk').lower()}",
        daemon=True,
    )
    thread.start()


@app.post('/api/trading/positions/{position_id}/manual-close', dependencies=[Depends(verify_token_dependency)])
async def manual_close_position(position_id: int):
    """手动一键平仓：跳过条件检查，直接对指定持仓执行平仓"""
    # 1. 查询持仓记录
    def _query_position():
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                "SELECT * FROM mi_trade_position WHERE id = %s AND status = 'holding'",
                (position_id,)
            )
            return cursor.fetchone()

    pos = await asyncio.to_thread(_query_position)
    if not pos:
        raise HTTPException(status_code=404, detail=f'持仓 {position_id} 不存在或已平仓')

    ba = pos.get('base_asset', '')

    # 2. 检查盘口数据可用性
    if not svc or svc.state != SERVICE_RUNNING:
        raise HTTPException(status_code=503, detail='服务未运行，无法执行平仓')

    if not svc._gate_ws_connected() or not svc._binance_ws_connected():
        raise HTTPException(status_code=503, detail='WebSocket 未连接，无法获取实时盘口数据')

    # 3. 获取该标的的最新盘口数据；持仓标的缺订阅时先强制补订阅。
    orderbook_row = await _ensure_orderbook_row_for_close(ba)

    # 4. 复用 ClosingExecutor 执行平仓；放入平仓专用线程，与自动平仓串行。
    def _manual_close_in_critical_thread():
        global _closing_executor
        if _closing_executor is None:
            from calc.closing_executor import ClosingExecutor
            _closing_executor = ClosingExecutor(_contract_meta, _spot_meta, _funding_rate_p40_meta)
        _configure_closing_executor(_closing_executor)
        return _closing_executor.manual_close(pos, orderbook_row)

    result = await asyncio.get_running_loop().run_in_executor(
        _critical_close_executor, _manual_close_in_critical_thread
    )

    # 5. 平仓成功后推送 WebSocket 通知
    if result.get('success') and event_loop and broadcast_queue:
        close_payload = {
            'type': 'close_position_result',
            'results': [result],
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        await broadcast_queue.put(close_payload)
        order_payload = {
            'type': 'order_update',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        await broadcast_queue.put(order_payload)

    # 6. 返回结果
    return {
        'success': result.get('success', False),
        'message': result.get('message', ''),
        'order_uuid': result.get('order_uuid'),
        'base_asset': ba,
    }


@app.post('/api/trading/positions/close-all', dependencies=[Depends(verify_token_dependency)])
async def close_all_positions():
    """一键平仓：对所有 holding 状态的持仓执行平仓（跳过条件检查，直接平仓）"""
    # 1. 检查服务状态
    if not svc or svc.state != SERVICE_RUNNING:
        raise HTTPException(status_code=503, detail='服务未运行，无法执行平仓')

    if not svc._gate_ws_connected() or not svc._binance_ws_connected():
        raise HTTPException(status_code=503, detail='WebSocket 未连接，无法获取实时盘口数据')

    # 2. 获取所有持仓中的仓位
    def _query_holdings():
        with db_manager.get_cursor() as cursor:
            cursor.execute("SELECT * FROM mi_trade_position WHERE status = 'holding'")
            return cursor.fetchall()

    positions = await asyncio.to_thread(_query_holdings)
    if not positions:
        return {'success': True, 'message': '无持仓中的仓位', 'results': [], 'total': 0, 'closed': 0}

    # 3. 获取最新盘口数据
    merged_rows = _get_merged_rows()
    orderbook_map: Dict[str, Dict] = {}
    for row in merged_rows:
        ba = row.get('base_asset', '')
        if ba:
            orderbook_map[ba] = row

    # 5. 逐个执行平仓；全部走平仓专用线程，与自动平仓串行。
    loop = asyncio.get_running_loop()
    results = []
    for pos in positions:
        ba = pos.get('base_asset', '')
        orderbook_row = orderbook_map.get(ba)
        if not orderbook_row:
            results.append({'base_asset': ba, 'success': False, 'message': f'无盘口数据'})
            continue
        try:
            def _manual_close_one(pos=pos, orderbook_row=orderbook_row):
                global _closing_executor
                if _closing_executor is None:
                    from calc.closing_executor import ClosingExecutor
                    _closing_executor = ClosingExecutor(_contract_meta, _spot_meta, _funding_rate_p40_meta)
                _configure_closing_executor(_closing_executor)
                return _closing_executor.manual_close(pos, orderbook_row)

            result = await loop.run_in_executor(_critical_close_executor, _manual_close_one)
            results.append(result)
        except Exception as e:
            results.append({'base_asset': ba, 'success': False, 'message': str(e)})

    # 6. 推送 WebSocket 通知
    success_count = sum(1 for r in results if r.get('success'))
    if success_count > 0 and event_loop and broadcast_queue:
        close_payload = {
            'type': 'close_position_result',
            'results': results,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        await broadcast_queue.put(close_payload)
        order_payload = {
            'type': 'order_update',
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        await broadcast_queue.put(order_payload)

    return {
        'success': True,
        'message': f'已平仓 {success_count}/{len(positions)} 个持仓',
        'results': results,
        'total': len(positions),
        'closed': success_count,
    }


@app.websocket('/ws/orderbook')
async def ws_orderbook(websocket: WebSocket, token: str = Query(None), mode: str = Query('events')):
    # 验证 token
    try:
        verify_ws_token(token)
    except HTTPException as e:
        await websocket.close(code=4001, reason=e.detail)
        return
    
    await websocket.accept()
    reverse_mode = mode == 'reverse'
    snapshot_mode = mode != 'events' and not reverse_mode
    if snapshot_mode:
        ws_clients.add(websocket)
    if reverse_mode:
        reverse_ws_clients.add(websocket)
    event_ws_clients.add(websocket)

    try:
        # 初始连接时发送当前快照（复用缓存的 JSON）
        if snapshot_mode:
            await websocket.send_text(build_payload_json())
        if reverse_mode:
            await websocket.send_text(build_reverse_opportunities_payload_json())
        if svc and svc.state in (SERVICE_STARTING, SERVICE_STOPPING):
            await websocket.send_text(_json_dumps(svc.get_progress_payload()))

        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                # 处理前端 ping 请求，回复 pong（用于延迟测量）
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict) and msg.get('type') == 'ping':
                        await websocket.send_text(json.dumps({'type': 'pong', 'ts': msg.get('ts')}))
                except (json.JSONDecodeError, TypeError):
                    pass
            except asyncio.TimeoutError:
                await websocket.send_json({'type': 'ping'})
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)
        reverse_ws_clients.discard(websocket)
        event_ws_clients.discard(websocket)


def _get_fresh_trading_rows() -> List[Dict]:
    """获取候选扫描用盘口+富化数据。

    候选扫描复用短期合并缓存，避免 0.2s 开仓循环反复跨进程拉取全量宽表。
    真正下单前仍由 TradingExecutor 的单标的旁路校验读取最新盘口并检查 lag。
    """
    global _cached_trading_rows, _cached_trading_ts
    now = time.time()
    if _cached_trading_rows and (now - _cached_trading_ts) < TRADING_SCAN_CACHE_SEC:
        return [dict(row) for row in _cached_trading_rows]

    rows = _get_merged_rows()
    rows = calculate_hedge_metrics(rows, _contract_meta, _spot_meta, OPEN_AMOUNT_USDT)
    enrich_trading_fields(
        rows, _contract_meta, _threshold_meta, _enrich_cfg,
        _asset_profile_meta, _funding_support_meta,
    )
    _cached_trading_rows = [dict(row) for row in rows]
    _cached_trading_ts = now
    return [dict(row) for row in _cached_trading_rows]


def _run_open_position_check_once():
    """开仓关键路径：在专用线程中运行，避免被 API/WS 广播事件循环阻塞。"""
    start = time.monotonic()
    try:
        if not svc or svc.state != SERVICE_RUNNING:
            return

        if _open_paused:
            return

        if _is_real_executor and not _exchange_connectivity_ok:
            return

        if not svc._gate_ws_connected() or not svc._binance_ws_connected():
            return

        if not (svc.gate_manager and svc.spot_manager):
            return

        merged_rows = _get_fresh_trading_rows()
        if not merged_rows:
            return

        global _trading_executor
        if _trading_executor is None:
            _trading_cfg = TradingExecutorConfig(
                executor_url=config.get_executor_url(),
                executor_timeout=config.get_int('trade.executor.timeout_sec', 5),
                coverage_threshold=config.get_float('trade.open.orderbook_coverage_threshold', 0.8),
                basis_threshold_bps=config.get_float('trade.open.vwap_basis_threshold_bps', -60),
                cooldown_sec=config.get_int('trade.open.cooldown_sec', 3600),
                min_funding_rate_bps=config.get_float('trade.open.min_funding_rate_bps', -6.0),
                min_funding_support_bps=config.get_float('trade.open.min_funding_support_bps', 8.0),
                funding_support_min_samples=config.get_int('trade.open.funding_support_min_samples', 2),
                realtime_min_funding_rate_bps=config.get_float(
                    'trade.open.realtime_min_funding_rate_bps', 5.0
                ),
                open_amount_usdt=config.get_float('trade.open.amount_usdt', 5),
                reduced_open_amount_multiplier=config.get_float(
                    'trade.open.reduced_amount_multiplier',
                    config.get_float('trade.market_profile.thin_bursty.open_amount_multiplier', 0.6),
                ),
                min_available_ratio=config.get_float('trade.open.min_available_ratio', 0.10),
                min_binance_available_ratio=config.get_float(
                    'trade.open.min_binance_available_ratio',
                    config.get_float('trade.open.min_available_ratio', 0.10),
                ),
                min_gate_available_ratio=config.get_float(
                    'trade.open.min_gate_available_ratio',
                    config.get_float('trade.open.min_available_ratio', 0.10),
                ),
                max_asset_exposure_ratio=config.get_float('trade.open.max_asset_exposure_ratio', 0.10),
                quality_scale_in_enabled=config.get_bool('trade.open.quality_scale_in.enabled', False),
                quality_scale_in_enhanced_ratio=config.get_float(
                    'trade.open.quality_scale_in.enhanced_ratio', 0.20
                ),
                quality_scale_in_min_funding_24h_bps=config.get_float(
                    'trade.open.quality_scale_in.min_funding_24h_bps', 50.0
                ),
                quality_scale_in_min_basis_improvement_bps=config.get_float(
                    'trade.open.quality_scale_in.min_basis_improvement_bps', 8.0
                ),
                quality_scale_in_basis_improvement_ratio=config.get_float(
                    'trade.open.quality_scale_in.basis_improvement_ratio', 0.25
                ),
                quality_scale_in_max_basis_improvement_bps=config.get_float(
                    'trade.open.quality_scale_in.max_basis_improvement_bps', 20.0
                ),
                quality_scale_in_cooldown_sec=config.get_int(
                    'trade.open.quality_scale_in.cooldown_sec', 300
                ),
                presignal_reject_log_cooldown_sec=config.get_int(
                    'trade.open.presignal_reject_log_cooldown_sec', 300
                ),
                reject_cooldown_sec=config.get_int('trade.open.reject_cooldown_sec', 60),
                max_orderbook_lag_ms=config.get_float('trade.open.max_orderbook_lag_ms', 1000.0),
                fee_spot_open=config.get_float('trade.fee.spot_open', 0.00075),
                fee_spot_close=config.get_float('trade.fee.spot_close', 0.00075),
                fee_future_open=config.get_float('trade.fee.future_open', 0.00075),
                fee_future_close=config.get_float('trade.fee.future_close', 0.00075),
                fee_future_taker_open=config.get_float(
                    'trade.fee.future_taker_open',
                    config.get_float('trade.fee.future_open', 0.00075),
                ),
                fee_future_taker_close=config.get_float(
                    'trade.fee.future_taker_close',
                    config.get_float('trade.fee.future_close', 0.00075),
                ),
                close_threshold_percentile=config.get_str('trade.vwap.close_threshold_percentile', 'close_basis_p20').strip(),
                min_spot_volume_24h_usdt=config.get_float('trade.filter.min_spot_volume_24h_usdt', 0),
                min_future_volume_24h_usdt=config.get_float('trade.filter.min_future_volume_24h_usdt', 0),
                peak_pullback_pct=config.get_float('trade.peak_pullback.pullback_pct', 0.10),
                peak_monitor_timeout_sec=config.get_int('trade.peak_pullback.monitor_timeout_sec', 60),
                peak_timeout_cooldown_sec=config.get_int('trade.peak_pullback.timeout_cooldown_sec', 10),
                sustain_sec=config.get_float('trade.peak_pullback.sustain_sec', 5.0),
                risk_relief_bps=config.get_float('trade.open.risk_relief_bps', 10),
                resiliency_enabled=config.get_bool('trade.resiliency.enabled', True),
                resiliency_window_sec=config.get_float('trade.resiliency.window_sec', 3.0),
                resiliency_min_samples=config.get_int('trade.resiliency.min_samples', 2),
                resiliency_min_recovery_ratio=config.get_float('trade.resiliency.min_recovery_ratio', 0.65),
                resiliency_max_spread_widen_bps=config.get_float('trade.resiliency.max_spread_widen_bps', 8.0),
                resiliency_max_basis_volatility_bps=config.get_float('trade.resiliency.max_basis_volatility_bps', 6.0),
                resiliency_min_hold_sec=config.get_float('trade.resiliency.min_hold_sec', 0.4),
                resiliency_max_wait_sec=config.get_float('trade.resiliency.max_wait_sec', 5.0),
                momentum_enabled=config.get_bool('trade.momentum_open.enabled', False),
                momentum_window_sec=config.get_float('trade.momentum_open.window_sec', 1.2),
                momentum_min_samples=config.get_int('trade.momentum_open.min_samples', 3),
                momentum_min_rise_bps=config.get_float('trade.momentum_open.min_rise_bps', 3.0),
                momentum_min_basis_buffer_bps=config.get_float('trade.momentum_open.min_basis_buffer_bps', 8.0),
                momentum_safety_bps=config.get_float('trade.momentum_open.safety_bps', 8.0),
                momentum_allowed_tiers=config.get('trade.momentum_open.allowed_tiers', ['A']),
                momentum_tier_overrides=config.get('trade.momentum_open.tier_overrides', {}),
                rebound_enabled=config.get_bool('trade.rebound_open.enabled', True),
                rebound_allowed_tiers=config.get('trade.rebound_open.allowed_tiers', ['A', 'B']),
                rebound_min_rise_bps=config.get_float('trade.rebound_open.min_rise_bps', 4.0),
                rebound_min_slope_bps=config.get_float('trade.rebound_open.min_slope_bps', 0.5),
                rebound_min_basis_buffer_bps=config.get_float('trade.rebound_open.min_basis_buffer_bps', 4.0),
                rebound_max_wait_sec=config.get_float('trade.rebound_open.max_wait_sec', 4.0),
                rebound_high_funding_24h_bps=config.get_float(
                    'trade.rebound_open.high_funding_24h_bps', 50.0
                ),
                rebound_high_funding_max_wait_sec=config.get_float(
                    'trade.rebound_open.high_funding_max_wait_sec', 10.0
                ),
                rebound_strong_cushion_bps=config.get_float('trade.rebound_open.strong_cushion_bps', 20.0),
                rebound_strong_cushion_min_hold_sec=config.get_float(
                    'trade.rebound_open.strong_cushion_min_hold_sec', 1.0
                ),
                rebound_strong_cushion_max_wait_sec=config.get_float(
                    'trade.rebound_open.strong_cushion_max_wait_sec', 8.0
                ),
                execution_guard_enabled=config.get_bool('trade.execution_guard.enabled', True),
                execution_guard_min_profit_buffer_bps=config.get_float('trade.execution_guard.min_profit_buffer_bps', 15.0),
                execution_guard_min_p20_buffer_bps=config.get_float('trade.execution_guard.min_p20_buffer_bps', 3.0),
                execution_guard_max_peak_decay_bps=config.get_float('trade.execution_guard.max_peak_decay_bps', 45.0),
                funding_entry_enabled=config.get_bool('trade.funding_adjusted_entry.enabled', True),
                funding_entry_capture_ratio=config.get_float('trade.funding_adjusted_entry.funding_capture_ratio', 0.5),
                funding_entry_slippage_buffer_bps=config.get_float('trade.funding_adjusted_entry.slippage_buffer_bps', 10.0),
                funding_entry_min_expected_edge_bps=config.get_float('trade.funding_adjusted_entry.min_expected_edge_bps', 0.0),
                funding_entry_strong_funding_24h_bps=config.get_float('trade.funding_adjusted_entry.strong_funding_24h_bps', 50.0),
                funding_entry_discount_ratio=config.get_float('trade.funding_adjusted_entry.discount_ratio', 0.2),
                funding_entry_max_discount_bps=config.get_float('trade.funding_adjusted_entry.max_funding_discount_bps', 10.0),
                funding_carry_enabled=config.get_bool('trade.funding_carry_open.enabled', False),
                funding_carry_allowed_tiers=config.get('trade.funding_carry_open.allowed_tiers', ['A', 'B']),
                funding_carry_min_24h_bps=config.get_float('trade.funding_carry_open.min_24h_bps', 30.0),
                funding_carry_basis_relax_bps=config.get_float('trade.funding_carry_open.basis_relax_bps', 15.0),
                funding_carry_max_next_funding_min=config.get_float(
                    'trade.funding_carry_open.max_next_funding_min', 30.0
                ),
                high_basis_enabled=config.get_bool('trade.high_basis_open.enabled', True),
                high_basis_allowed_tiers=config.get('trade.high_basis_open.allowed_tiers', ['A', 'B']),
                high_basis_amount_multiplier=config.get_float(
                    'trade.high_basis_open.amount_multiplier', 0.5
                ),
                high_basis_min_funding_24h_bps=config.get_float(
                    'trade.high_basis_open.min_funding_24h_bps', 3.0
                ),
                high_basis_min_entry_buffer_bps=config.get_float(
                    'trade.high_basis_open.min_entry_buffer_bps', 25.0
                ),
                high_basis_min_net_edge_bps=config.get_float(
                    'trade.high_basis_open.min_net_edge_bps', 20.0
                ),
                high_basis_scale_in_min_basis_improvement_bps=config.get_float(
                    'trade.high_basis_open.scale_in_min_basis_improvement_bps', 20.0
                ),
                thin_bursty_enabled=config.get_bool('trade.market_profile.thin_bursty.enabled', True),
                thin_bursty_max_orderbook_lag_ms=config.get_float(
                    'trade.market_profile.thin_bursty.max_orderbook_lag_ms', 1500.0
                ),
                thin_bursty_max_book_skew_ms=config.get_float(
                    'trade.market_profile.thin_bursty.max_book_skew_ms', 1500.0
                ),
                rebound_timeout_cooldown_enabled=config.get_bool('trade.rebound_timeout_cooldown.enabled', True),
                rebound_timeout_cooldown_sec=config.get_int('trade.rebound_timeout_cooldown.cooldown_sec', 60),
                rebound_timeout_basis_change_reset_bps=config.get_float('trade.rebound_timeout_cooldown.basis_change_reset_bps', 5.0),
                asset_noise_cooldown_enabled=config.get_bool('trade.asset_noise_cooldown.enabled', True),
                asset_noise_lookback_min=config.get_int('trade.asset_noise_cooldown.lookback_min', 60),
                asset_noise_max_signals=config.get_int('trade.asset_noise_cooldown.max_signals', 100),
                asset_noise_min_opened=config.get_int('trade.asset_noise_cooldown.min_opened', 1),
                asset_noise_cooldown_min=config.get_int('trade.asset_noise_cooldown.cooldown_min', 10),
                execution_drift_cooldown_enabled=config.get_bool('trade.execution_drift_cooldown.enabled', True),
                execution_drift_max_bps=config.get_float('trade.execution_drift_cooldown.max_drift_bps', 40.0),
                execution_drift_cooldown_hour=config.get_float('trade.execution_drift_cooldown.cooldown_hour', 0.5),
                future_maker_open_enabled=config.get_bool('trade.execution.future_maker_open.enabled', False),
                future_maker_open_allowed_tiers=config.get('trade.execution.future_maker_open.allowed_tiers', ['A', 'B']),
                future_maker_open_ttl_ms=config.get_int('trade.execution.future_maker_open.ttl_ms', 1000),
                future_maker_open_price_offset_bps=config.get_float(
                    'trade.execution.future_maker_open.price_offset_bps', 0.0
                ),
                future_maker_open_fallback_ioc_enabled=config.get_bool(
                    'trade.execution.future_maker_open.fallback_ioc_enabled', True
                ),
                future_maker_open_fallback_allowed_tiers=config.get(
                    'trade.execution.future_maker_open.fallback_allowed_tiers', ['A', 'B']
                ),
                future_maker_open_fallback_min_buffer_bps=config.get_float(
                    'trade.execution.future_maker_open.fallback_min_buffer_bps', 8.0
                ),
                future_maker_open_fallback_slippage_bps=config.get_float(
                    'trade.execution.future_maker_open.fallback_slippage_bps', 5.0
                ),
                future_maker_open_spot_hedge_protective_ioc_enabled=config.get_bool(
                    'trade.execution.future_maker_open.spot_hedge_protective_ioc_enabled', True
                ),
                capital_required=config.get_trade_mode() != 'virtual',
                capital_max_age_sec=config.get_int('account_capital.max_age_sec', 180),
                gate_cross_risk_max_age_sec=config.get_float(
                    'account_capital.gate_cross_risk.max_age_sec', 5.0
                ),
                binance_margin_required=config.get_bool('account_capital.binance_margin.enabled', False),
                binance_margin_min_open_level=config.get_float(
                    'account_capital.binance_margin.min_open_margin_level',
                    2.5,
                ),
            )
            _trading_executor = TradingExecutor(
                _trading_cfg, _contract_meta, _spot_meta,
                _vwap_threshold_meta, _close_vwap_threshold_meta, _asset_tier_meta,
                _asset_profile_meta, _funding_support_meta
            )
            _trading_executor.set_orderbook_managers(svc.gate_manager, svc.spot_manager)

        if hasattr(_trading_executor, 'set_delist_risk_report'):
            _trading_executor.set_delist_risk_report(_delist_risk_report)
        _trading_executor.update_account_capital_status(
            _account_summary_with_live_gate_cross_risk(),
            _latest_account_summary_ts,
        )
        results = _trading_executor.check_and_open(merged_rows)

        if results and event_loop and broadcast_queue:
            signal_payload = {
                'type': 'signal_update',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            asyncio.run_coroutine_threadsafe(broadcast_queue.put(signal_payload), event_loop)

        if any(r.get('success') for r in results):
            if event_loop and broadcast_queue:
                payload = {
                    'type': 'open_position_result',
                    'results': results,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                asyncio.run_coroutine_threadsafe(broadcast_queue.put(payload), event_loop)
                order_payload = {
                    'type': 'order_update',
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                asyncio.run_coroutine_threadsafe(broadcast_queue.put(order_payload), event_loop)
    except Exception as e:
        logger.error(f"开仓检查失败: {e}", exc_info=True)
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if elapsed_ms > 1000:
            logger.warning(f"开仓关键路径耗时偏高: {elapsed_ms:.0f}ms")


def _get_reverse_signal_monitor() -> ReverseSignalMonitor:
    """获取反向信号监控器；反向策略独立状态机，不复用正向 TradingExecutor。"""
    global _reverse_signal_monitor, _reverse_executor_client
    monitor_cfg = ReverseSignalMonitorConfig(
        open_amount_usdt=OPEN_AMOUNT_USDT,
        monitor_timeout_sec=config.get_float('reverse_arbitrage.signal.monitor_timeout_sec', 60.0),
        valley_rebound_pct=config.get_float('reverse_arbitrage.signal.valley_rebound_pct', 0.05),
        min_rebound_bps=config.get_float('reverse_arbitrage.signal.min_rebound_bps', 2.0),
        min_monitor_sec=config.get_float('reverse_arbitrage.signal.min_monitor_sec', 1.5),
        rebound_sustain_sec=config.get_float('reverse_arbitrage.signal.rebound_sustain_sec', 0.4),
        max_orderbook_lag_ms=config.get_float(
            'reverse_arbitrage.signal.max_orderbook_lag_ms',
            config.get_float('trade.open.max_orderbook_lag_ms', 500.0),
        ),
        execution_enabled=config.get_bool('reverse_arbitrage.execution.enabled', False),
        max_total_positions=config.get_int('reverse_arbitrage.execution.max_total_positions', 10),
        max_positions_per_asset=config.get_int('reverse_arbitrage.execution.max_positions_per_asset', 2),
        monitor_timeout_cooldown_sec=config.get_int(
            'reverse_arbitrage.signal.monitor_timeout_cooldown_sec', 10
        ),
        reject_cooldown_sec=config.get_int('reverse_arbitrage.signal.reject_cooldown_sec', 60),
        asset_noise_lookback_min=config.get_int(
            'reverse_arbitrage.signal.asset_noise_lookback_min', 60
        ),
        asset_noise_max_signals=config.get_int(
            'reverse_arbitrage.signal.asset_noise_max_signals', 100
        ),
        asset_noise_min_opened=config.get_int(
            'reverse_arbitrage.signal.asset_noise_min_opened', 1
        ),
        asset_noise_cooldown_min=config.get_int(
            'reverse_arbitrage.signal.asset_noise_cooldown_min', 10
        ),
    )
    if _reverse_executor_client is None:
        _reverse_executor_client = ExecutorClient(
            config.get_executor_url(),
            timeout=config.get_int('trade.executor.timeout_sec', 5),
        )
    if _reverse_signal_monitor is None:
        _reverse_signal_monitor = ReverseSignalMonitor(
            monitor_cfg,
            _reverse_cfg,
            _contract_meta,
            _spot_meta,
            _reverse_vwap_threshold_meta,
            executor_client=_reverse_executor_client,
            borrow_meta_refresher=_get_reverse_realtime_borrow_meta,
        )
    else:
        _reverse_signal_monitor.cfg = monitor_cfg
        _reverse_signal_monitor.update_meta(_contract_meta, _spot_meta, _reverse_vwap_threshold_meta)
        _reverse_signal_monitor.set_executor_client(_reverse_executor_client)
        _reverse_signal_monitor.set_borrow_meta_refresher(_get_reverse_realtime_borrow_meta)

    if svc and svc.gate_manager and svc.spot_manager:
        _reverse_signal_monitor.set_orderbook_managers(svc.gate_manager, svc.spot_manager)
    return _reverse_signal_monitor


def _run_reverse_signal_check_once():
    """反向开仓信号监控关键路径：只入表/监控，不触碰正向开仓状态。"""
    if not REVERSE_SIGNAL_ENABLED:
        return
    if not svc or svc.state != SERVICE_RUNNING:
        return
    if _reverse_open_paused:
        return
    start = time.monotonic()
    try:
        rows, borrow_meta, _borrow_source = _build_reverse_enriched_rows()
        monitor = _get_reverse_signal_monitor()
        results = monitor.process_rows(rows, borrow_meta=borrow_meta)
        if results and event_loop and broadcast_queue:
            signal_payload = {
                'type': 'reverse_signal_update',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            asyncio.run_coroutine_threadsafe(broadcast_queue.put(signal_payload), event_loop)
    except Exception as e:
        logger.error(f"反向信号监控失败: {e}", exc_info=True)
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if elapsed_ms > 1000:
            logger.warning(f"反向信号监控耗时偏高: {elapsed_ms:.0f}ms")


async def _reverse_research_snapshot_loop():
    """定时记录反向研究快照；观察任务和交易关键路径隔离。"""
    if not REVERSE_RESEARCH_ENABLED:
        logger.info('反向研究快照采集已关闭')
        return

    interval = max(30.0, float(REVERSE_RESEARCH_INTERVAL_SEC or 60.0))
    loop = asyncio.get_running_loop()
    await asyncio.sleep(15)

    while True:
        try:
            if svc and svc.state == SERVICE_RUNNING:
                result = await loop.run_in_executor(
                    None,
                    lambda: record_reverse_research_snapshot_once('loop'),
                )
                inserted = int(result.get('inserted') or 0)
                if inserted > 0:
                    logger.info(f'反向研究快照已记录 {inserted} 条')
        except Exception as e:
            logger.error(f'反向研究快照采集失败: {e}', exc_info=True)
        await asyncio.sleep(interval)


async def _open_position_loop():
    """定时检查开仓条件。实际判断在专用线程中串行执行。"""
    interval = config.get_float('trade.open.check_interval_sec', 5)
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(interval)
        await loop.run_in_executor(_critical_open_executor, _run_open_position_check_once)


async def _reverse_signal_loop():
    """定时检查反向交易信号；独立于正向开仓暂停按钮。"""
    interval = max(REVERSE_SIGNAL_INTERVAL_SEC, 0.2)
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(interval)
        await loop.run_in_executor(_critical_open_executor, _run_reverse_signal_check_once)


async def _position_funding_loop():
    """定时从交易所真实流水同步资金费收益。
    结算完成后通过 WS 推送 funding_history_update 事件，前端按需更新。
    """
    interval = max(config.get_int('trade.position.funding_update_sec', 600), 60)
    loop = asyncio.get_running_loop()

    # 启动后等待服务就绪再执行第一次
    await asyncio.sleep(10)

    while True:
        histories = []
        try:
            tracker = PositionTracker(_contract_meta)
            tracker.update_funding_pnl()
            histories = tracker.get_all_funding_histories()
        except Exception as e:
            logger.error(f"正向资金费更新失败: {e}")

        try:
            await loop.run_in_executor(_critical_open_executor, refresh_reverse_position_costs)
        except Exception as e:
            logger.error(f"反向持仓成本同步失败: {e}")

        # 结算后推送一次性的正向资金费历史更新事件
        if histories and broadcast_queue:
            payload = {
                'type': 'funding_history_update',
                'funding_histories': histories,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            await broadcast_queue.put(payload)

        next_run = datetime.now() + timedelta(seconds=interval)
        logger.info(f"资金费下次检查时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        await asyncio.sleep(interval)


def _publish_close_position_results(results: List[Dict]) -> None:
    if not results:
        return
    _record_auto_risk_close_notifications(results, event_at=datetime.now())
    if not any(result.get('success') for result in results):
        return
    if not event_loop or not broadcast_queue:
        return

    payload = {
        'type': 'close_position_result',
        'results': results,
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    asyncio.run_coroutine_threadsafe(broadcast_queue.put(payload), event_loop)
    order_payload = {
        'type': 'order_update',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    asyncio.run_coroutine_threadsafe(broadcast_queue.put(order_payload), event_loop)


def _run_close_position_check_once():
    """Run emergency account-risk exits before the ordinary orderbook close path."""
    start = time.monotonic()
    try:
        tracker = PositionTracker(_contract_meta)
        positions = tracker.get_holding_positions()
        if not positions:
            return

        global _closing_executor
        if _closing_executor is None:
            from calc.closing_executor import ClosingExecutor
            _closing_executor = ClosingExecutor(_contract_meta, _spot_meta, _funding_rate_p40_meta)
        _configure_closing_executor(_closing_executor)

        # This snapshot is REST-backed and independent of the orderbook service/WS.
        attach_gate_position_risk(positions, _get_gate_position_risk_snapshot())
        global _last_margin_danger_force_refresh_ts
        margin_risk_refresh_summary = _closing_executor.margin_risk_refresh_summary(positions)
        if margin_risk_refresh_summary.get('danger') or margin_risk_refresh_summary.get('missing'):
            now = time.time()
            min_interval = max(
                config.get_float(
                    'account_capital.gate_cross_risk.force_refresh_min_interval_sec', 2.0
                ),
                0.0,
            )
            if now - _last_margin_danger_force_refresh_ts < min_interval:
                logger.debug(
                    f"Gate保证金风险刷新跳过 | interval_guard={min_interval:.2f}s "
                    f"| danger={margin_risk_refresh_summary.get('danger')} "
                    f"| missing={margin_risk_refresh_summary.get('missing')}"
                )
            else:
                logger.warning(
                    f"Gate保证金风险刷新触发 | danger={margin_risk_refresh_summary.get('danger')} "
                    f"| missing={margin_risk_refresh_summary.get('missing')}"
                )
                _last_margin_danger_force_refresh_ts = now
                _invalidate_gate_position_risk_cache('margin_danger_path')
                attach_gate_position_risk(
                    positions,
                    _get_gate_position_risk_snapshot(force_refresh=True),
                )

        emergency_results = _closing_executor.check_and_close_margin_danger(positions, {})
        if emergency_results:
            _publish_close_position_results(emergency_results)
            return

        # Everything below is the ordinary close path and may depend on live books.
        if not svc or svc.state != SERVICE_RUNNING:
            return
        if _is_real_executor and not _exchange_connectivity_ok:
            return
        if not svc._gate_ws_connected() or not svc._binance_ws_connected():
            return
        if not (svc.gate_manager and svc.spot_manager):
            return

        merged_rows = _get_merged_rows()
        if not merged_rows:
            return

        close_vwaps: Dict[str, Dict] = {}
        orderbook_rows_by_asset: Dict[str, Dict] = {}
        for row in merged_rows:
            ba = row.get('base_asset', '')
            if not ba:
                continue
            orderbook_rows_by_asset[ba] = row
            spot_cv = row.get('spot_close_vwap')
            future_cv = row.get('future_close_vwap')
            if spot_cv is not None and future_cv is not None:
                close_vwaps[ba] = {
                    'spot_close_vwap': float(spot_cv),
                    'future_close_vwap': float(future_cv),
                }

        tracker.attach_funding_histories(positions)
        calculate_realtime_pnl(positions, close_vwaps, _contract_meta, _pnl_cfg)
        results = _closing_executor.check_and_close(
            positions, _close_vwap_threshold_meta, orderbook_rows_by_asset
        )
        _publish_close_position_results(results)
    except Exception as e:
        logger.error(f"平仓检查失败: {e}", exc_info=True)
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if elapsed_ms > 1000:
            logger.warning(f"平仓关键路径耗时偏高: {elapsed_ms:.0f}ms")


async def _close_position_loop():
    """定时检查平仓条件。实际判断在专用线程中串行执行。"""
    interval = config.get_float('trade.close.check_interval_sec', 0.5)
    loop = asyncio.get_running_loop()

    while True:
        await asyncio.sleep(interval)
        await loop.run_in_executor(_critical_close_executor, _run_close_position_check_once)


def _build_auto_risk_close_notification(result: Dict, event_at: Optional[datetime] = None) -> Optional[Dict]:
    close_reason = str(result.get('close_reason') or '').strip()
    if close_reason not in AUTO_RISK_CLOSE_REASON_LABELS:
        return None
    base_asset = str(result.get('base_asset') or '-').upper()
    success = bool(result.get('success'))
    status_text = '成功' if success else '失败'
    label = AUTO_RISK_CLOSE_REASON_LABELS[close_reason]
    order_uuid = result.get('order_uuid')
    message_parts = [
        f"{label}{status_text}",
        f"标的={base_asset}",
    ]
    if result.get('message'):
        message_parts.append(f"结果={result.get('message')}")
    if result.get('pre_gate_basis_bps') is not None:
        message_parts.append(f"旁路基差={float(result.get('pre_gate_basis_bps')):.1f}bps")
    if result.get('actual_close_basis_bps') is not None:
        message_parts.append(f"成交基差={float(result.get('actual_close_basis_bps')):.1f}bps")
    if result.get('close_basis_slip_bps') is not None:
        message_parts.append(f"滑点={float(result.get('close_basis_slip_bps')):+.1f}bps")
    if order_uuid:
        message_parts.append(f"order={order_uuid}")
    return {
        'title': f"系统强平{status_text}: {base_asset}",
        'message': ' | '.join(message_parts),
        'type': 'warning' if success else 'error',
        'source': 'auto_risk_close',
        'dedup_key': f"auto_risk_close:{close_reason}:{order_uuid}" if order_uuid else None,
        'event_at': event_at or datetime.now(),
        'payload': result,
    }


def _record_auto_risk_close_notifications(results: List[Dict], event_at: Optional[datetime] = None) -> int:
    recorded = 0
    for result in results or []:
        item = _build_auto_risk_close_notification(result, event_at=event_at)
        if not item:
            continue
        try:
            upsert_popup_notification(**item)
            recorded += 1
        except Exception as exc:
            logger.warning(
                "系统强平铃铛消息写入失败 | asset=%s reason=%s error=%s",
                result.get('base_asset'),
                result.get('close_reason'),
                exc,
                exc_info=True,
            )
    return recorded


def _invalidate_gate_position_risk_cache(reason: str = ''):
    global _gate_position_risk_cache, _gate_position_risk_cache_ts
    had_cache = bool(_gate_position_risk_cache)
    _gate_position_risk_cache = []
    _gate_position_risk_cache_ts = 0.0
    if had_cache:
        suffix = f" | reason={reason}" if reason else ''
        logger.info(f"Gate持仓风险快照缓存已失效{suffix}")


def _get_gate_position_risk_snapshot(force_refresh: bool = False) -> List[Dict]:
    """读取 Gate 实时仓位风险，短缓存用于持仓监控展示。"""
    global _gate_position_risk_cache, _gate_position_risk_cache_ts
    if config.get_trade_mode() == 'virtual':
        return []

    monitor = _gate_cross_risk_monitor
    if monitor is not None:
        try:
            if force_refresh:
                monitor.refresh()
            if monitor.positions_fetched_at_ts > 0:
                _gate_position_risk_cache = monitor.get_positions()
                _gate_position_risk_cache_ts = monitor.positions_fetched_at_ts
                return _gate_position_risk_cache
        except Exception as e:
            logger.warning(f"共享 Gate 持仓风险快照读取失败: {e}", exc_info=True)

    now = time.time()
    ttl_sec = max(10, config.get_int('trade.position.gate_risk_cache_sec', 30))
    if not force_refresh and _gate_position_risk_cache and now - _gate_position_risk_cache_ts < ttl_sec:
        return _gate_position_risk_cache

    try:
        gate_positions = build_default_reconciler().executor.fetch_gate_futures_positions()
        _gate_position_risk_cache = gate_positions
        _gate_position_risk_cache_ts = now
        return gate_positions
    except Exception as e:
        logger.warning(f"Gate持仓风险快照拉取失败: {e}")
        return _gate_position_risk_cache


def _build_position_close_vwaps() -> Dict[str, Dict]:
    close_vwaps: Dict[str, Dict] = {}
    if not svc or not svc.gate_manager or not svc.spot_manager:
        return close_vwaps
    for row in _get_merged_rows():
        ba = row.get('base_asset', '')
        spot_cv = row.get('spot_close_vwap')
        future_cv = row.get('future_close_vwap')
        if ba and spot_cv is not None and future_cv is not None:
            close_vwaps[ba] = {
                'spot_close_vwap': float(spot_cv),
                'future_close_vwap': float(future_cv),
            }
    return close_vwaps


def _build_orderbook_rows_by_asset() -> Dict[str, Dict]:
    if not svc or not svc.gate_manager or not svc.spot_manager:
        return {}
    rows = _get_merged_rows()
    return {
        str(row.get('base_asset') or '').upper(): row
        for row in rows
        if row.get('base_asset')
    }


def _load_reverse_positions_with_realtime_pnl(
    *,
    status: Optional[str] = None,
    order_side: Optional[str] = None,
    exchange_risk: bool = False,
    base_asset: Optional[str] = None,
    days: int = 90,
    page: int = 1,
    page_size: int = 100,
) -> Dict:
    summary = summarize_reverse_positions(
        exchange_risk=exchange_risk,
        base_asset=base_asset,
        days=days,
    )
    result = list_reverse_positions(
        status=status,
        order_side=order_side,
        exchange_risk=exchange_risk,
        base_asset=base_asset,
        days=days,
        page=page,
        page_size=page_size,
    )
    positions = result.rows
    calculate_reverse_realtime_pnl(
        positions,
        _build_orderbook_rows_by_asset(),
        _contract_meta,
        _reverse_pnl_cfg,
    )
    return {
        'positions': positions,
        'pagination': {
            'page': result.page,
            'page_size': result.page_size,
            'total': result.total,
            'total_pages': result.total_pages,
        },
        'summary': summary,
        'open_amount_usdt': OPEN_AMOUNT_USDT,
    }


def _build_strategy_position_pnl_summary() -> Dict:
    positions = PositionTracker(_contract_meta).get_all_positions()
    if not positions:
        return {
            'position_count': 0,
            'closed_count': 0,
            'realized_pnl': 0.0,
            'funding_pnl': 0.0,
            'fee_cost': 0.0,
            'binance_spot_floating_pnl': 0.0,
            'gate_future_floating_pnl': 0.0,
            'floating_pnl': 0.0,
            'total_pnl': 0.0,
            'pnl_rows': 0,
            'missing_realtime_rows': 0,
        }

    calculate_realtime_pnl(positions, _build_position_close_vwaps(), _contract_meta, _pnl_cfg)
    realized_pnl = sum(float(pos.get('realized_pnl') or 0) for pos in positions)
    funding_pnl = sum(float(pos.get('funding_total_pnl') or 0) for pos in positions)
    fee_cost = sum(float(pos.get('fee_cost') or 0) for pos in positions)
    spot_floating = sum(float(pos.get('floating_spot_pnl') or 0) for pos in positions)
    future_floating = sum(float(pos.get('floating_future_pnl') or 0) for pos in positions)
    floating_pnl = sum(float(pos.get('floating_pnl_total') or 0) for pos in positions)
    total_pnl = sum(float(pos.get('total_pnl') or 0) for pos in positions)
    pnl_rows = sum(1 for pos in positions if pos.get('total_pnl') is not None)
    missing_rows = len(positions) - pnl_rows
    return {
        'position_count': len(positions),
        'closed_count': sum(1 for pos in positions if pos.get('status') == 'closed'),
        'realized_pnl': round(realized_pnl, 8),
        'funding_pnl': round(funding_pnl, 8),
        'fee_cost': round(fee_cost, 8),
        'binance_spot_floating_pnl': round(spot_floating, 8),
        'gate_future_floating_pnl': round(future_floating, 8),
        'floating_pnl': round(floating_pnl, 8),
        'strategy_total_pnl': round(total_pnl, 8),
        'pnl_rows': pnl_rows,
        'missing_realtime_rows': missing_rows,
    }


async def _position_realtime_push():
    """定时推送持仓实时数据（含已平仓，使用平仓 VWAP 作为实时价格）
    注意：funding_history 不在此推送（低频数据），仅通过 REST 初始加载 + 结算后事件推送。
    """
    interval = config.get_float('trade.position.push_interval_sec', 5.0)

    while True:
        try:
            await asyncio.sleep(interval)

            if not svc or svc.state != SERVICE_RUNNING:
                continue

            tracker = PositionTracker(_contract_meta)
            positions = tracker.get_all_positions()

            if not positions:
                continue

            # 计算实时盈亏（已平仓持仓用DB存储的价格，不依赖 close_vwaps）
            calculate_realtime_pnl(positions, _build_position_close_vwaps(), _contract_meta, _pnl_cfg)
            attach_gate_position_risk(positions, _get_gate_position_risk_snapshot())
            
            # 资金金额按分钟刷新；Gate 全仓风险使用共享的秒级实时快照。
            account_summary = _account_summary_with_live_gate_cross_risk()
            
            # 推送（不含 funding_history，保持消息精简）
            payload = {
                'type': 'position_update',
                'positions': positions,
                'account_summary': account_summary,
                # 标准开仓金额：前端兑底计算 funding_pnl_bps 使用，避免与后端配置漂移
                'open_amount_usdt': OPEN_AMOUNT_USDT,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            await broadcast_queue.put(payload)

        except Exception as e:
            logger.error(f"持仓实时推送失败: {e}")


async def _reverse_position_realtime_push():
    """定时推送反向持仓实时数据。"""
    interval = config.get_float(
        'reverse_arbitrage.position.push_interval_sec',
        config.get_float('trade.position.push_interval_sec', 5.0),
    )

    while True:
        try:
            await asyncio.sleep(interval)

            if not svc or svc.state != SERVICE_RUNNING:
                continue

            payload = _load_reverse_positions_with_realtime_pnl(
                days=365,
                page=1,
                page_size=5000,
            )
            if not payload.get('positions'):
                continue

            payload.update({
                'type': 'reverse_position_update',
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            await broadcast_queue.put(payload)

        except Exception as e:
            logger.error(f"反向持仓实时推送失败: {e}", exc_info=True)


async def _gate_cross_risk_loop():
    """Refresh the shared Gate cross-margin risk snapshot independently of charts."""
    if config.get_trade_mode() == 'virtual':
        logger.info('virtual 模式跳过 Gate 全仓实时风险采集')
        return

    interval = max(
        0.5,
        config.get_float('account_capital.gate_cross_risk.poll_interval_sec', 1.0),
    )
    while True:
        try:
            snapshot = await asyncio.to_thread(_refresh_gate_cross_risk_once)
            if snapshot.get('error'):
                logger.warning(
                    'Gate 全仓实时风险采集部分失败: %s',
                    snapshot.get('error'),
                )
        except Exception as e:
            logger.error(f'Gate 全仓实时风险采集失败: {e}', exc_info=True)
            await asyncio.to_thread(_record_gate_cross_risk_collection_failure, e)
        await asyncio.sleep(interval)


async def _account_capital_snapshot_loop():
    """分钟级采集交易所真实资金并落库，同时刷新开仓风控缓存。"""
    global _latest_account_summary, _latest_account_summary_ts
    if config.get_trade_mode() == 'virtual':
        logger.info('virtual 模式跳过交易所真实资金采集')
        return
    if not config.get_bool('account_capital.enabled', True):
        logger.info('交易所资金采集已关闭')
        return

    interval = max(30, config.get_int('account_capital.snapshot_interval_sec', 60))
    while True:
        try:
            strategy_pnl = _build_strategy_position_pnl_summary()
            result = await asyncio.to_thread(
                lambda: build_default_capital_snapshotter(
                    gate_cross_risk_provider=_get_live_gate_cross_risk_snapshot,
                ).run_once(strategy_pnl)
            )
            _latest_account_summary = result.get('summary')
            _latest_account_summary_ts = time.time()
            logger.info(f"交易所资金快照完成: snapshot_at={result.get('snapshot_at')}")
        except Exception as e:
            logger.error(f"交易所资金快照失败: {e}", exc_info=True)
        await asyncio.sleep(interval)


async def _server_metric_snapshot_loop():
    """小时级采集 ECS CPU/内存/硬盘等服务器指标。"""
    if not config.get_bool('server_metrics.enabled', True):
        logger.info('服务器指标采集已关闭')
        return

    interval = max(300, config.get_int('server_metrics.interval_sec', 3600))
    retention_days = max(7, config.get_int('server_metrics.retention_days', 14))
    disk_path = config.get_str('server_metrics.disk_path', '/')

    while True:
        try:
            result = await asyncio.to_thread(
                lambda: record_server_metrics(disk_path=disk_path, retention_days=retention_days)
            )
            logger.info(
                '服务器指标快照完成: '
                f"snapshot_at={result.get('snapshot_at')}, "
                f"cpu={result.get('cpu_usage_percent')}, "
                f"memory={result.get('memory_usage_percent')}, "
                f"disk={result.get('disk_usage_percent')}"
            )
        except Exception as e:
            logger.error(f'服务器指标快照失败: {e}', exc_info=True)
        await asyncio.sleep(interval)


async def _reconciliation_loop():
    """定时执行基础持仓对账；只记录差异，不告警、不修复。"""
    if config.get_trade_mode() == 'virtual':
        logger.info('virtual 模式跳过交易所对账循环')
        return
    if not config.get_bool('reconciliation.enabled', True):
        logger.info('交易所对账循环已关闭')
        return

    interval = max(30, config.get_int('reconciliation.interval_sec', 300))
    await asyncio.sleep(5)

    while True:
        try:
            global _reconciliation_trigger_running
            skip_running = False
            with _reconciliation_trigger_lock:
                if _reconciliation_trigger_running:
                    skip_running = True
                else:
                    _reconciliation_trigger_running = True
            if skip_running:
                logger.info('交易所对账循环跳过：已有对账任务正在执行')
            else:
                try:
                    result = await asyncio.to_thread(lambda: build_default_reconciler().run_once())
                    logger.info(f"交易所对账循环完成: {result}")
                finally:
                    with _reconciliation_trigger_lock:
                        _reconciliation_trigger_running = False
        except Exception as e:
            logger.error(f"交易所对账循环失败: {e}", exc_info=True)
        await asyncio.sleep(interval)


async def _vwap_snapshot_loop():
    """定时采样VWAP基差数据落库，用于历史分位统计"""
    interval = config.get_int('trade.vwap.snapshot_interval_sec', 10)

    while True:
        try:
            await asyncio.sleep(interval)

            if not svc or svc.state != SERVICE_RUNNING:
                continue

            if not svc.gate_manager or not svc.spot_manager:
                continue

            merged_rows = _get_merged_rows()

            if not merged_rows:
                continue

            record_vwap_snapshots(merged_rows, OPEN_AMOUNT_USDT)

        except Exception as e:
            logger.error(f"VWAP快照落库失败: {e}")


def _cleanup_stale_monitoring_signals_once() -> int:
    """Resolve old monitoring signals so UI statistics are not polluted by stale rows."""
    if not config.get_bool('trade.signal_cleanup.enabled', True):
        return 0
    stale_sec = max(config.get_int('trade.signal_cleanup.stale_monitoring_sec', 180), 30)
    batch_limit = max(config.get_int('trade.signal_cleanup.batch_limit', 500), 1)
    sql = """
        UPDATE mi_trade_signal
        SET status = 'monitor_timeout',
            resolved_time = NOW(),
            duration_sec = TIMESTAMPDIFF(SECOND, signal_time, NOW()),
            exit_reason = CONCAT('stale monitoring cleanup(>', %s, 's)')
        WHERE status = 'monitoring'
          AND signal_time < DATE_SUB(NOW(), INTERVAL %s SECOND)
        ORDER BY signal_time ASC
        LIMIT %s
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (stale_sec, stale_sec, batch_limit))
        return cursor.rowcount or 0


async def _stale_signal_cleanup_loop():
    """Low-frequency cleanup for orphaned monitoring signals."""
    if not config.get_bool('trade.signal_cleanup.enabled', True):
        logger.info('交易信号stale monitoring清理已关闭')
        return
    interval = max(config.get_int('trade.signal_cleanup.interval_sec', 300), 60)
    await asyncio.sleep(30)
    while True:
        try:
            cleaned = await asyncio.to_thread(_cleanup_stale_monitoring_signals_once)
            if cleaned:
                logger.info(f"清理stale monitoring交易信号: {cleaned}条")
        except Exception as e:
            logger.error(f"清理stale monitoring交易信号失败: {e}", exc_info=True)
        await asyncio.sleep(interval)


async def _connectivity_check_loop():
    """定时检查交易所 API 链路连通性（仅实盘模式生效）

    逻辑：
    1. 启动时等待 10s 让成交引擎服务就绪
    2. 通过 ExecutorClient.check_health() 判断是否为 real 引擎
    3. 如果是 real 引擎，定期调用 check_connectivity() 检查 Binance + Gate
    4. 任一不通则设 _exchange_connectivity_ok = False，阻断开仓/平仓
    5. 恢复后自动解除熔断
    """
    global _exchange_connectivity_ok, _is_real_executor, _connectivity_detail

    from calc.executor_client import ExecutorClient

    executor_url = config.get_executor_url()
    executor_timeout = config.get_int('trade.executor.timeout_sec', 5)
    client = ExecutorClient(executor_url, timeout=executor_timeout)

    # 启动等待：让成交引擎服务先启动
    await asyncio.sleep(10)

    # 探测是否为真实成交引擎
    health = client.check_health()
    engine_type = health.get('engine', 'virtual')
    _is_real_executor = (engine_type == 'real')

    if not _is_real_executor:
        logger.info(f'成交引擎类型: {engine_type}，跳过交易链路连通性检查')
        return  # 虚拟模式不需要检查

    env_name = health.get('env', 'unknown')
    logger.info(f'检测到真实成交引擎 (env={env_name})，启动交易链路连通性定时检查 (间隔 {_connectivity_check_interval}s)')

    # 启动时立即执行一次检查
    result = client.check_connectivity()
    _connectivity_detail = result
    _exchange_connectivity_ok = result.get('all_ok', False)

    if _exchange_connectivity_ok:
        log_print(f'✅ 交易链路连通性检查通过 (env={env_name})')
    else:
        binance_ok = result.get('binance', {}).get('ok', False)
        gate_ok = result.get('gate', {}).get('ok', False)
        log_print(
            f'❌ 交易链路连通性检查失败! '
            f'Binance={"✅" if binance_ok else "❌"} Gate={"✅" if gate_ok else "❌"} '
            f'— 开仓/平仓已熔断'
        )

    # 定时循环检查
    while True:
        await asyncio.sleep(_connectivity_check_interval)
        try:
            result = client.check_connectivity()
            _connectivity_detail = result
            was_ok = _exchange_connectivity_ok
            _exchange_connectivity_ok = result.get('all_ok', False)

            # 状态变更时打印日志
            if was_ok and not _exchange_connectivity_ok:
                binance_ok = result.get('binance', {}).get('ok', False)
                gate_ok = result.get('gate', {}).get('ok', False)
                logger.warning(
                    f'交易链路断开! Binance={"✅" if binance_ok else "❌"} '
                    f'Gate={"✅" if gate_ok else "❌"} — 开仓/平仓已熔断'
                )
                # 通过 WS 通知前端
                if broadcast_queue:
                    await broadcast_queue.put({
                        'type': 'connectivity_alert',
                        'all_ok': False,
                        'binance_ok': binance_ok,
                        'gate_ok': gate_ok,
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    })
            elif not was_ok and _exchange_connectivity_ok:
                logger.info('交易链路已恢复! 开仓/平仓熔断解除')
                if broadcast_queue:
                    await broadcast_queue.put({
                        'type': 'connectivity_alert',
                        'all_ok': True,
                        'binance_ok': True,
                        'gate_ok': True,
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    })
        except Exception as e:
            logger.error(f'交易链路连通性检查异常: {e}')
            _exchange_connectivity_ok = False


# ───── 崩溃日志：全局异常钩子 ─────

def _global_exception_handler(exc_type, exc_value, exc_tb):
    """捕获未处理的同步异常，记录到日志文件"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical('未捕获的异常导致进程即将退出', exc_info=(exc_type, exc_value, exc_tb))

sys.excepthook = _global_exception_handler


def _threading_exception_handler(args):
    """捕获子线程中未处理的异常"""
    logger.critical(
        f'线程 [{args.thread.name}] 未捕获异常: {args.exc_type.__name__}: {args.exc_value}',
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )

threading.excepthook = _threading_exception_handler


def _asyncio_exception_handler(loop, context):
    """捕获 asyncio 事件循环中未处理的异常"""
    exception = context.get('exception')
    message = context.get('message', '未知 asyncio 异常')
    if exception:
        logger.critical(f'asyncio 未处理异常: {message}', exc_info=exception)
    else:
        logger.critical(f'asyncio 未处理异常: {message}')


def main():
    import uvicorn

    host = '0.0.0.0'
    port = 19876

    log_print(f'启动业务订单簿服务 http://{host}:{port}')
    log_print('Binance/Gate WS 由独立 orderbook_data_service 维护')
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    main()
