# coding: utf-8
"""
交易API路由模块
- 订单查询
- 持仓查询
- 持仓汇总统计
- VWAP基差阈值查询与手动执行
- AG Grid列配置管理
"""
import asyncio
import json
import threading
from decimal import Decimal
from datetime import datetime, date
from typing import Optional, Any, Callable, List, Dict
from fastapi import APIRouter, Query, Depends
from pydantic import BaseModel

from common.database import db_manager
from common.config import config
from common.errors import AppError, ValidationAppError
from common.logger import get_logger
from api.auth import verify_token_dependency, verify_user_password
from common.meta_loader import fetch_contract_meta
from calc.reconciliation import build_default_reconciler, get_ignored_binance_spot_assets
from calc.account_capital import build_default_capital_snapshotter
from calc.forward_bnb_fee import build_default_forward_bnb_fee_buyer
from calc.fund_transfer_service import get_fund_transfer_service
from calc.listing_event_monitor import (
    add_listing_asset_to_monitor,
    disable_listing_asset,
    listing_event_summary,
    list_listing_events,
    mark_listing_events,
    refresh_listing_events,
)
from calc.orderbook_data_client import OrderBookDataClient
from calc.popup_notification_store import (
    count_unread_popup_notifications,
    list_popup_notifications,
    mark_popup_notifications_read,
    upsert_popup_notification,
    upsert_popup_notifications,
)
from calc.position_pnl_calculator import PnlConfig
from calc.reverse_account_monitor import get_reverse_capital_snapshot
from repositories.reconciliation_query_repo import (
    reconciliation_ignore_clause,
    reconciliation_latest_sql,
)
from repositories.trading_query_repo import build_forward_signal_filters
from services.base_asset_service import BaseAssetService
from services.capital_command_service import (
    CapitalCommandService,
    parse_capital_range_datetime,
)
from services.capital_query_service import (
    CAPITAL_ANNUALIZED_PERIODS,
    CapitalQueryService,
    aggregate_capital_latest_account_rows,
    build_gate_cross_minimum_summary,
    calculate_capital_annualized_return,
    capital_history_interval,
    capital_history_select_columns,
    filter_capital_transfer_transient_rows,
)
from services.column_config_service import ColumnConfigService
from services.delist_risk_service import (
    attach_delist_risks,
    delist_risk_asset_set,
    format_delist_risk_summary,
    get_delist_risk_report_cached,
)
from services.fund_transfer_api_service import FundTransferApiService
from services.listing_event_api_service import ListingEventApiService
from services.popup_notification_api_service import PopupNotificationApiService
from services.reconciliation_command_service import (
    ReconciliationCommandService,
    format_dust_cleanup_message,
)
from services.reconciliation_query_service import ReconciliationQueryService
from services.reverse_query_service import ReverseQueryService
from services.risk_notification_service import (
    RiskNotificationService,
    append_unique_notification,
    db_bool,
    format_reconciliation_notification,
    risk_notification_key,
    should_emit_reconciliation_notification,
)
from services.threshold_calc_service import ThresholdCalcService
from services.threshold_query_service import ThresholdQueryService
from services.trading_query_service import TradingQueryService

logger = get_logger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading"])

_delist_risk_cache: dict[str, Any] = {
    'at': 0.0,
    'lookahead_days': None,
    'report': None,
}
_delist_risk_lock = threading.Lock()


class DisableBaseAssetRequest(BaseModel):
    reason: Optional[str] = None


class ListingEventActionRequest(BaseModel):
    reason: Optional[str] = None


class PopupNotificationCreateRequest(BaseModel):
    title: str
    message: str
    type: Optional[str] = 'info'
    source: Optional[str] = None
    dedup_key: Optional[str] = None
    event_at: Optional[Any] = None
    payload: Optional[Dict[str, Any]] = None


class PopupNotificationMarkReadRequest(BaseModel):
    ids: Optional[List[int]] = None


class CapitalClearRangeRequest(BaseModel):
    start_at: str
    end_at: str


class FundTransferCreateRequest(BaseModel):
    amount: Decimal
    password: str


class FundTransferRetryRequest(BaseModel):
    password: str


def _subscribe_listing_asset_orderbook(base_asset: str) -> Dict[str, Any]:
    asset = (base_asset or '').strip().upper()
    if not asset:
        return {'ok': False, 'message': 'base_asset 为空'}
    try:
        client = OrderBookDataClient(timeout=8.0)
        ok, message = client.retry_contract(asset)
        return {'ok': bool(ok), 'message': message or ''}
    except Exception as exc:
        logger.warning('动态订阅上新标的失败: asset=%s error=%s', asset, exc, exc_info=True)
        return {'ok': False, 'message': str(exc)[:200]}


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """将数据库行中的 Decimal/datetime 转换为 JSON 可序列化类型"""
    result = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            result[key] = float(value)
        elif isinstance(value, datetime):
            result[key] = value.strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(value, date):
            result[key] = value.strftime('%Y-%m-%d')
        elif key in ('detail', 'binance_cross_margin', 'source_payload', 'payload') and isinstance(value, str):
            try:
                result[key] = json.loads(value)
            except Exception:
                result[key] = value
        else:
            result[key] = value
    return result


def _serialize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量序列化数据库行"""
    return [_serialize_row(row) for row in rows]


def _aggregate_capital_latest_account_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the total account card on the same exchange-account basis as its components."""
    return aggregate_capital_latest_account_rows(rows, _serialize_rows)


def _risk_notification_key(prefix: str, *parts: Any) -> str:
    return risk_notification_key(prefix, *parts)


def _format_meta_dt(value) -> str | None:
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value) if value else None


def _position_pnl_config() -> PnlConfig:
    return PnlConfig(
        open_amount_usdt=config.get_float('trade.open.amount_usdt', 10.0),
        spot_open_fee=config.get_float('trade.fee.spot_open', 0.00075),
        spot_close_fee=config.get_float('trade.fee.spot_close', 0.00075),
        future_open_fee=config.get_float('trade.fee.future_open', 0.00075),
        future_close_fee=config.get_float('trade.fee.future_close', 0.00075),
        future_taker_open_fee=config.get_float(
            'trade.fee.future_taker_open',
            config.get_float('trade.fee.future_open', 0.00075),
        ),
        future_taker_close_fee=config.get_float(
            'trade.fee.future_taker_close',
            config.get_float('trade.fee.future_close', 0.00075),
        ),
        risk_relief_bps=config.get_float('trade.open.risk_relief_bps', 10),
        margin_default_mmr=config.get_float('margin.default_maintenance_rate', 0.005),
    )


def _build_forward_signal_filters(
    *,
    status: Optional[str],
    exit_reason: Optional[str],
    base_asset: Optional[str],
    time_range: Optional[str],
    days: int,
    prefix: str = "",
) -> tuple[str, List]:
    # 兼容旧测试导入路径；实现已下沉到 TradingQueryRepo。
    return build_forward_signal_filters(
        status=status,
        exit_reason=exit_reason,
        base_asset=base_asset,
        time_range=time_range,
        days=days,
        prefix=prefix,
    )


def _inject_current_funding_fields(rows: List[Dict[str, Any]], contract_meta: Optional[Dict[str, Dict]] = None) -> None:
    """持仓列表展示实时合约 funding 元数据，不使用开仓时快照。"""
    contract_meta = contract_meta if contract_meta is not None else fetch_contract_meta()
    for row in rows:
        base_asset = row.get('base_asset')
        meta = contract_meta.get(base_asset or '', {})
        if not meta:
            continue
        interval_sec = meta.get('funding_interval')
        row['funding_rate'] = meta.get('funding_rate')
        row['funding_rate_24h'] = meta.get('funding_rate_24h')
        row['funding_interval'] = interval_sec
        row['funding_interval_hours'] = (
            round(float(interval_sec) / 3600, 4) if interval_sec else None
        )
        row['funding_last_apply'] = _format_meta_dt(meta.get('funding_last_apply'))
        row['funding_next_apply'] = _format_meta_dt(meta.get('funding_next_apply'))


def _get_delist_risk_report_cached(lookahead_days: int = 30, max_age_sec: int = 900) -> Dict[str, Any]:
    """下架风险报告供多个页面复用，避免持仓页面刷新时频繁请求交易所公告接口。"""
    return get_delist_risk_report_cached(
        lookahead_days,
        max_age_sec,
        cache=_delist_risk_cache,
        lock=_delist_risk_lock,
    )


def _delist_risk_asset_set(report: Optional[Dict[str, Any]] = None) -> set[str]:
    return delist_risk_asset_set(report, get_report=_get_delist_risk_report_cached)


def _format_delist_risk_summary(items: List[Dict[str, Any]]) -> str:
    return format_delist_risk_summary(items)


def _attach_delist_risks(rows: List[Dict[str, Any]]) -> None:
    attach_delist_risks(rows, get_report=_get_delist_risk_report_cached)


def _trading_query_service() -> TradingQueryService:
    return TradingQueryService(
        db_manager,
        serialize_row=_serialize_row,
        serialize_rows=_serialize_rows,
        attach_delist_risks=_attach_delist_risks,
        delist_risk_asset_set=_delist_risk_asset_set,
        inject_current_funding_fields=_inject_current_funding_fields,
        position_pnl_config=_position_pnl_config,
    )


def _reverse_query_service() -> ReverseQueryService:
    return ReverseQueryService(
        db_manager,
        serialize_row=_serialize_row,
        serialize_rows=_serialize_rows,
        get_capital_snapshot=get_reverse_capital_snapshot,
    )


def _capital_query_service() -> CapitalQueryService:
    return CapitalQueryService(
        db_manager,
        serialize_row=_serialize_row,
        serialize_rows=_serialize_rows,
    )


def _reconciliation_query_service() -> ReconciliationQueryService:
    return ReconciliationQueryService(
        db_manager,
        serialize_rows=_serialize_rows,
        ignore_clause=_reconciliation_ignore_clause,
    )


def _fund_transfer_api_service() -> FundTransferApiService:
    return FundTransferApiService(
        get_fund_transfer_service,
        serialize_row=_serialize_row,
        serialize_rows=_serialize_rows,
    )


def _risk_notification_service() -> RiskNotificationService:
    return RiskNotificationService(
        db_manager,
        serialize_row=_serialize_row,
        ignore_clause=_reconciliation_ignore_clause,
    )


def _threshold_query_service() -> ThresholdQueryService:
    return ThresholdQueryService(
        db_manager,
        serialize_rows=_serialize_rows,
    )


def _popup_notification_api_service() -> PopupNotificationApiService:
    return PopupNotificationApiService(
        serialize_row=_serialize_row,
        serialize_rows=_serialize_rows,
        list_listing_events=list_listing_events,
        upsert_one=upsert_popup_notification,
        upsert_many=upsert_popup_notifications,
        list_notifications=list_popup_notifications,
        count_unread=count_unread_popup_notifications,
        mark_read=mark_popup_notifications_read,
        list_recent_risk_items=_build_recent_risk_notification_items,
        notification_key=_risk_notification_key,
    )


def _listing_event_api_service() -> ListingEventApiService:
    return ListingEventApiService(
        serialize_row=_serialize_row,
        serialize_rows=_serialize_rows,
        list_events=list_listing_events,
        event_summary=listing_event_summary,
        refresh_events=refresh_listing_events,
        mark_events=mark_listing_events,
        add_to_monitor=add_listing_asset_to_monitor,
        disable_asset=disable_listing_asset,
    )


def _column_config_service() -> ColumnConfigService:
    return ColumnConfigService(db_manager)


def _base_asset_service() -> BaseAssetService:
    return BaseAssetService(db_manager)


def _set_recon_running(value: bool) -> None:
    global _recon_running
    _recon_running = value


def _set_capital_running(value: bool) -> None:
    global _capital_running
    _capital_running = value


def _set_threshold_calc_running(value: bool) -> None:
    global _threshold_calc_running
    _threshold_calc_running = value


def _run_threshold_analysis(lookback_days: int, progress_callback: Callable[..., Any]) -> Any:
    from calc.calculate_vwap_basis_threshold import run_analysis
    return run_analysis(lookback_days, progress_callback=progress_callback)


def _reconciliation_command_service() -> ReconciliationCommandService:
    return ReconciliationCommandService(
        get_trade_mode=config.get_trade_mode,
        get_bool=config.get_bool,
        build_reconciler=build_default_reconciler,
        lock=_recon_lock,
        is_running=lambda: _recon_running,
        set_running=_set_recon_running,
    )


def _capital_command_service() -> CapitalCommandService:
    return CapitalCommandService(
        db_manager,
        get_trade_mode=config.get_trade_mode,
        get_bool=config.get_bool,
        build_snapshotter=build_default_capital_snapshotter,
        build_bnb_buyer=build_default_forward_bnb_fee_buyer,
        get_pnl_provider=lambda: _capital_strategy_pnl_provider,
        lock=_capital_lock,
        is_running=lambda: _capital_running,
        set_running=_set_capital_running,
        serialize_row=_serialize_row,
    )


def _threshold_calc_service() -> ThresholdCalcService:
    return ThresholdCalcService(
        get_bool=config.get_bool,
        get_int=config.get_int,
        lock=_threshold_calc_lock,
        status=_threshold_calc_status,
        is_running=lambda: _threshold_calc_running,
        set_running=_set_threshold_calc_running,
        set_status=_set_threshold_calc_status,
        get_status=_get_threshold_calc_status,
        run_analysis=_run_threshold_analysis,
    )


@router.get('/orders')
async def get_orders(
    view: str = Query('open', pattern='^(open|close)$', description="订单视图(open/close)"),
    channel: Optional[str] = Query(None, description="渠道过滤"),
    exchange_risk: bool = Query(False, description="仅展示交易所风险持仓"),
    position_id: Optional[int] = Query(None, description="持仓ID过滤"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(90, ge=1, le=90, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页持仓数"),
) -> Dict[str, Any]:
    """按当前持仓状态查询开仓或平仓视图，过滤、排序与分页全部在后端完成。"""
    normalized_view = str(view or '').strip().lower()
    if normalized_view not in {'open', 'close'}:
        raise ValidationAppError('view 必须为 open 或 close')
    return await asyncio.to_thread(
        _trading_query_service().list_order_view,
        view=normalized_view,
        channel=channel,
        exchange_risk=exchange_risk,
        position_id=position_id,
        base_asset=base_asset,
        days=days,
        page=page,
        page_size=page_size,
    )


@router.get('/positions/{position_id}/orders')
async def get_position_orders(position_id: int) -> Dict[str, Any]:
    """获取指定持仓的全部订单明细（弹窗用）"""
    return await asyncio.to_thread(
        _trading_query_service().list_position_orders,
        position_id,
    )


@router.get('/orders/grouped')
async def get_orders_grouped() -> List[Dict[str, Any]]:
    """
    返回分组后的订单列表
    结构：[
        {
            position_id: 123,
            base_asset: 'BTC',
            orders: [现货开仓, 期货开仓, 现货平仓, 期货平仓],
            summary: { 汇总信息 }
        }
    ]
    """
    return await asyncio.to_thread(_trading_query_service().list_grouped_orders)


@router.get('/positions')
async def get_positions(
    status: Optional[str] = Query(None, description="持仓状态过滤"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(90, ge=1, le=365, description="最近N天（开仓时间）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """查询持仓列表（含资金费结算历史，支持分页）"""
    return await asyncio.to_thread(
        _trading_query_service().list_positions,
        status=status,
        base_asset=base_asset,
        days=days,
        page=page,
        page_size=page_size,
    )


@router.get('/positions/summary')
async def get_positions_summary() -> Dict[str, Any]:
    """持仓汇总统计"""
    return await asyncio.to_thread(_trading_query_service().positions_summary)


# ─── 基础对账 ────────────────────────────────────────────────────────────────

_recon_running = False
_recon_lock = threading.Lock()
_capital_running = False
_capital_lock = threading.Lock()
_capital_strategy_pnl_provider: Optional[Callable[[], Dict[str, Any]]] = None


def register_capital_strategy_pnl_provider(
    provider: Optional[Callable[[], Dict[str, Any]]],
) -> None:
    """Register the live strategy PnL source used by manual capital snapshots."""
    global _capital_strategy_pnl_provider
    _capital_strategy_pnl_provider = provider


class BinanceBnbBuyRequest(BaseModel):
    amount_usdt: float


def _capital_history_interval(hours: Optional[int], days: int) -> tuple[str, int]:
    return capital_history_interval(hours, days)


def _capital_history_select_columns(metric: str) -> str:
    return capital_history_select_columns(metric)


def _calculate_capital_annualized_return(
    rows: List[Dict[str, Any]],
    period_days: int,
) -> Dict[str, Any]:
    return calculate_capital_annualized_return(rows, period_days)


def _filter_capital_transfer_transient_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return filter_capital_transfer_transient_rows(rows)


def _build_gate_cross_minimum_summary(row: Optional[Dict[str, Any]]) -> Optional[Dict]:
    return build_gate_cross_minimum_summary(row, serialize_row=_serialize_row)


def _reconciliation_ignore_clause(table_alias: str = '') -> tuple[str, List[Any]]:
    return reconciliation_ignore_clause(
        sorted(get_ignored_binance_spot_assets()),
        table_alias,
    )


def _reconciliation_latest_sql(ignore_sql: str) -> str:
    return reconciliation_latest_sql(ignore_sql)


def _db_bool(value: Any) -> bool:
    return db_bool(value)


def _should_emit_reconciliation_notification(row: Dict[str, Any], latest_snapshot_at: Any) -> bool:
    """Suppress one-off historical reconciliation mismatches caused by in-flight trades."""
    return should_emit_reconciliation_notification(row, latest_snapshot_at)


def _format_reconciliation_notification(row: Dict[str, Any], dedup_key: str) -> Dict[str, Any]:
    return format_reconciliation_notification(row, dedup_key)


def _append_unique_notification(
    items: List[Dict[str, Any]],
    seen_keys: set[str],
    item: Dict[str, Any],
) -> None:
    append_unique_notification(items, seen_keys, item)


@router.get('/reconciliation/latest')
async def get_reconciliation_latest() -> Dict[str, Any]:
    """返回最近一轮对账快照。"""
    return await asyncio.to_thread(_reconciliation_query_service().latest)


@router.get('/reconciliation/history')
async def get_reconciliation_history(
    days: int = Query(1, ge=1, le=30, description="最近N天"),
    mismatches_only: bool = Query(False, description="是否仅显示差异/错误"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(500, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """查询对账历史快照。"""
    return await asyncio.to_thread(
        _reconciliation_query_service().history,
        days=days,
        mismatches_only=mismatches_only,
        page=page,
        page_size=page_size,
    )


def _build_recent_risk_notification_items(hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
    return _risk_notification_service().list_recent_items(hours=hours, limit=limit)


def _sync_recent_popup_notifications() -> Dict[str, int]:
    """Materialize current backend risk/listing signals into persistent bell history."""
    return _popup_notification_api_service().sync_recent()


@router.get('/risk-notifications/recent')
async def get_recent_risk_notifications(
    hours: int = Query(24, ge=1, le=168, description="回看最近N小时风险事件"),
    limit: int = Query(50, ge=1, le=200, description="最多返回事件数"),
) -> Dict[str, Any]:
    """返回需要进入前端铃铛的近期风险事件。"""
    return await asyncio.to_thread(
        _risk_notification_service().recent,
        hours=hours,
        limit=limit,
    )


@router.get('/notifications')
async def get_popup_notifications(
    read_status: str = Query('unread', description="读取状态：unread/read/all"),
    source: Optional[str] = Query(None, description="消息来源过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sync_recent: bool = Query(True, description="先同步近期后端风险/上新事件"),
) -> Dict[str, Any]:
    """查询持久化铃铛消息。"""
    return await asyncio.to_thread(
        _popup_notification_api_service().list_items,
        read_status=read_status,
        source=source,
        page=page,
        page_size=page_size,
        sync_recent=sync_recent,
    )


@router.get('/notifications/unread-count')
async def get_popup_notification_unread_count() -> Dict[str, int]:
    """返回铃铛未读数量。"""
    return await asyncio.to_thread(_popup_notification_api_service().unread_count)


@router.post('/notifications', dependencies=[Depends(verify_token_dependency)])
async def create_popup_notification(payload: PopupNotificationCreateRequest) -> Dict[str, Any]:
    """写入一条持久化铃铛消息。"""
    return await asyncio.to_thread(
        _popup_notification_api_service().create,
        title=payload.title,
        message=payload.message,
        type=payload.type,
        source=payload.source,
        dedup_key=payload.dedup_key,
        event_at=payload.event_at,
        payload=payload.payload,
    )


@router.post('/notifications/mark-read', dependencies=[Depends(verify_token_dependency)])
async def mark_popup_notification_read(payload: PopupNotificationMarkReadRequest) -> Dict[str, Any]:
    """将指定消息或全部未读消息标记为已读。"""
    return await asyncio.to_thread(_popup_notification_api_service().mark_read, payload.ids)


@router.post('/notifications/{notification_id}/read', dependencies=[Depends(verify_token_dependency)])
async def mark_one_popup_notification_read(notification_id: int) -> Dict[str, Any]:
    """将单条铃铛消息标记为已读。"""
    return await asyncio.to_thread(_popup_notification_api_service().mark_read, [notification_id])


@router.post('/reconciliation/run')
async def run_reconciliation_now():
    """手动触发一次对账。virtual 模式下跳过。"""
    return await asyncio.to_thread(_reconciliation_command_service().run_now)


def _format_dust_cleanup_message(cleanup: Dict[str, Any]) -> str:
    return format_dust_cleanup_message(cleanup)


@router.post(
    '/reconciliation/dust/cleanup',
    dependencies=[Depends(verify_token_dependency)],
)
async def cleanup_reconciliation_dust():
    """Clean one fully explained post-close dust hedge, then refresh reconciliation."""
    return await asyncio.to_thread(_reconciliation_command_service().cleanup_dust)


# ─── 真实资金快照 ────────────────────────────────────────────────────────────

def _parse_capital_range_datetime(value: str, field_name: str) -> datetime:
    return parse_capital_range_datetime(value, field_name)


@router.get('/capital/gate-cross-risk/summary')
async def get_gate_cross_risk_summary(
    days: int = Query(7, ge=1, le=90, description="最近N天"),
) -> Dict[str, Any]:
    """Return the lowest valid Gate cross MMR and its main risk contributor."""
    return await asyncio.to_thread(
        _capital_query_service().gate_cross_risk_summary,
        days,
    )


@router.get('/capital/latest')
async def get_capital_latest() -> Dict[str, Any]:
    """返回最新资金快照汇总。"""
    return await asyncio.to_thread(_capital_query_service().latest)


@router.get('/capital/annualized-return')
async def get_capital_annualized_return(
    days: int = Query(7, description="统计周期(1/3/7/30/90/180/365天)"),
) -> Dict[str, Any]:
    """Return compounded annualized strategy return from daily capital summaries."""
    if days not in CAPITAL_ANNUALIZED_PERIODS:
        raise ValidationAppError('不支持的年化收益统计周期')
    return await asyncio.to_thread(
        _capital_query_service().annualized_return,
        days,
    )


@router.get('/capital/history')
async def get_capital_history(
    days: int = Query(7, ge=1, le=90, description="最近N天"),
    hours: Optional[int] = Query(None, ge=1, le=24, description="最近N小时，优先于days"),
    exchange: Optional[str] = Query(None, description="交易所过滤(binance/gate/total)"),
    metric: str = Query('equity_usdt', description="资金趋势指标"),
) -> Dict[str, Any]:
    """按时间范围自动采样，并只返回当前资金趋势需要的字段。"""
    return await asyncio.to_thread(
        _capital_query_service().history,
        days=days,
        hours=hours,
        exchange=exchange,
        metric=metric,
    )


@router.post('/capital/run')
async def run_capital_snapshot_now():
    """手动采集一次真实资金快照。"""
    return await asyncio.to_thread(_capital_command_service().run_snapshot)


@router.get('/capital/fund-transfer', dependencies=[Depends(verify_token_dependency)])
async def get_fund_transfer_tasks(limit: int = Query(30, ge=1, le=200)) -> Dict[str, Any]:
    """Return the active transfer and durable transfer history."""
    return await asyncio.to_thread(_fund_transfer_api_service().list_tasks, limit)


@router.get(
    '/capital/fund-transfer/limits',
    dependencies=[Depends(verify_token_dependency)],
)
async def get_fund_transfer_limits() -> Dict[str, Any]:
    """Return current live minimum and maximum transferable amounts."""
    return await asyncio.to_thread(_fund_transfer_api_service().limits)


@router.get(
    '/capital/fund-transfer/preflight',
    dependencies=[Depends(verify_token_dependency)],
)
async def preflight_fund_transfer(amount: Decimal = Query(..., gt=0)) -> Dict[str, Any]:
    """Read live balances, fee, network and fixed destination without moving money."""
    return await asyncio.to_thread(_fund_transfer_api_service().preview, amount)


@router.post('/capital/fund-transfer', status_code=201)
async def create_fund_transfer(
    req: FundTransferCreateRequest,
    user: Dict[str, Any] = Depends(verify_token_dependency),
) -> Dict[str, Any]:
    """Create one real transfer after current-password re-authentication."""
    if config.get_trade_mode() == 'virtual':
        raise AppError('virtual 模式不执行真实资金划转', status_code=409)
    if not verify_user_password(user_id=user.get('user_id'), password=req.password):
        raise AppError('当前登录密码校验失败', status_code=403)
    return await asyncio.to_thread(
        _fund_transfer_api_service().create_task,
        amount=req.amount,
        user_id=str(user.get('user_id') or 'default'),
        username=str(user.get('username') or ''),
    )


@router.post('/capital/fund-transfer/{task_id}/retry')
async def retry_fund_transfer(
    task_id: int,
    req: FundTransferRetryRequest,
    user: Dict[str, Any] = Depends(verify_token_dependency),
) -> Dict[str, Any]:
    """Recheck an ambiguous task or retry only its location-safe recovery step."""
    if not verify_user_password(user_id=user.get('user_id'), password=req.password):
        raise AppError('当前登录密码校验失败', status_code=403)
    return await asyncio.to_thread(
        _fund_transfer_api_service().retry_task,
        task_id,
    )


@router.post('/capital/clear-range', dependencies=[Depends(verify_token_dependency)])
async def clear_capital_snapshot_range(req: CapitalClearRangeRequest):
    """按时间段清理资金快照数据，清理前自动备份命中行。"""
    return await asyncio.to_thread(
        _capital_command_service().clear_range,
        req.start_at,
        req.end_at,
    )


@router.post('/capital/binance-bnb/buy', dependencies=[Depends(verify_token_dependency)])
async def buy_forward_binance_bnb(req: BinanceBnbBuyRequest):
    """FORWARD Binance Spot 使用 USDT 市价买入 BNB 手续费余额。"""
    return await asyncio.to_thread(_capital_command_service().buy_bnb, req.amount_usdt)


# ─── VWAP 基差阈值 ────────────────────────────────────────────────────────────

@router.get('/threshold/latest-date')
async def get_threshold_latest_date() -> Dict[str, Any]:
    """获取 BTC 的最新数据写入时间（用于页面展示，避免全表扫描）"""
    return await asyncio.to_thread(_threshold_query_service().latest_date)


@router.get('/threshold/dates')
async def get_threshold_dates() -> List[str]:
    """获取 mi_vwap_basis_threshold 表中所有可用日期（降序）"""
    return await asyncio.to_thread(_threshold_query_service().dates)


@router.get('/threshold/assets')
async def get_threshold_assets() -> List[Any]:
    """获取 mi_vwap_basis_threshold 表中所有标的（按 open_basis_p20 降序）"""
    return await asyncio.to_thread(_threshold_query_service().assets)


@router.get('/threshold/data')
async def get_threshold_data(
    calc_date: Optional[str] = Query(None, description="计算日期，格式 YYYY-MM-DD，默认最新日期"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """查询 mi_vwap_basis_threshold 表数据（后端分页）"""
    return await asyncio.to_thread(
        _threshold_query_service().data,
        calc_date=calc_date,
        base_asset=base_asset,
        page=page,
        page_size=page_size,
    )


# 手动执行状态（避免并发重复触发）
_threshold_calc_running = False
_threshold_calc_lock = threading.Lock()
_threshold_calc_status: Dict[str, Any] = {
    'running': False,
    'processed': 0,
    'total': 0,
    'current_asset': None,
    'success_count': 0,
    'skip_count': 0,
    'fail_count': 0,
    'message': '未开始',
    'started_at': None,
    'finished_at': None,
    'error': None,
}


def _set_threshold_calc_status(**kwargs):
    with _threshold_calc_lock:
        _threshold_calc_status.update(kwargs)


def _get_threshold_calc_status() -> Dict[str, Any]:
    with _threshold_calc_lock:
        return dict(_threshold_calc_status)


def _run_threshold_calculate_job(lookback_days: int):
    _threshold_calc_service().run_job(lookback_days)


@router.post('/threshold/calculate')
async def trigger_threshold_calculate():
    """手动触发 VWAP 基差分位阈值计算（后台执行）"""
    return _threshold_calc_service().trigger()


@router.get('/threshold/calculate/status')
async def get_threshold_calculate_status():
    """获取手动 VWAP 阈值计算进度"""
    return _threshold_calc_service().status()


@router.get('/delist-risks')
async def get_delist_risks(
    lookahead_days: int = Query(30, ge=1, le=180, description="下架计划预警窗口（天）"),
) -> Dict[str, Any]:
    """检查当前监控/持仓标的的交易所下架风险。"""
    return await asyncio.to_thread(_get_delist_risk_report_cached, lookahead_days)


@router.get('/listing-events')
async def get_listing_events(
    action_status: Optional[str] = Query(None, description="处理状态过滤：pending/acknowledged/ignored/disabled/added_to_monitor/all"),
    candidate_status: Optional[str] = Query(None, description="候选状态过滤：matched/gate_only/binance_only/added_to_monitor/all"),
    monitor_status: Optional[str] = Query(None, description="监控状态过滤：not_added/added/all"),
    actionable_only: bool = Query(False, description="仅展示可提醒候选"),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    """查询交易对上新事件。"""
    return await asyncio.to_thread(
        _listing_event_api_service().list_events,
        action_status=action_status,
        candidate_status=candidate_status,
        monitor_status=monitor_status,
        actionable_only=actionable_only,
        limit=limit,
    )


@router.get('/listing-events/summary')
async def get_listing_events_summary() -> Dict[str, Any]:
    """交易对上新事件摘要，用于固定时间弹窗提醒。"""
    return await asyncio.to_thread(_listing_event_api_service().summary)


@router.post('/listing-events/refresh', dependencies=[Depends(verify_token_dependency)])
async def refresh_listing_events_api() -> Dict[str, Any]:
    """手动刷新交易对上新事件。"""
    return await asyncio.to_thread(_listing_event_api_service().refresh)


@router.post('/listing-events/{base_asset}/ack', dependencies=[Depends(verify_token_dependency)])
async def ack_listing_event(base_asset: str, payload: ListingEventActionRequest | None = None) -> Dict[str, Any]:
    """确认上新事件；保留在页面，但不再弹窗。"""
    return await asyncio.to_thread(
        _listing_event_api_service().ack,
        base_asset,
        payload.reason if payload else None,
    )


@router.post('/listing-events/{base_asset}/ignore', dependencies=[Depends(verify_token_dependency)])
async def ignore_listing_event(base_asset: str, payload: ListingEventActionRequest | None = None) -> Dict[str, Any]:
    """忽略上新事件；不修改 mi_base_asset。"""
    return await asyncio.to_thread(
        _listing_event_api_service().ignore,
        base_asset,
        payload.reason if payload else None,
    )


@router.post('/listing-events/{base_asset}/add-to-monitor', dependencies=[Depends(verify_token_dependency)])
async def add_listing_event_to_monitor(base_asset: str) -> Dict[str, Any]:
    """将上新候选加入 mi_base_asset，后续按普通标的进入监控候选。"""
    result = await asyncio.to_thread(_listing_event_api_service().add_to_monitor, base_asset)
    subscription = await asyncio.to_thread(
        _subscribe_listing_asset_orderbook,
        result.get('base_asset') or base_asset,
    )
    result['dynamic_subscription'] = subscription
    result['requires_service_reload'] = not bool(subscription.get('ok'))
    if subscription.get('ok'):
        result['message'] = f"{result.get('message') or result.get('base_asset')}，已动态加入实时订阅"
    else:
        result['message'] = (
            f"{result.get('message') or result.get('base_asset')}，但动态订阅失败："
            f"{subscription.get('message') or '未知错误'}"
        )
    return result


@router.post('/listing-events/{base_asset}/disable', dependencies=[Depends(verify_token_dependency)])
async def disable_listing_event_asset(base_asset: str, payload: ListingEventActionRequest | None = None) -> Dict[str, Any]:
    """将上新候选写入/更新为失效标的，后续不再弹窗。"""
    return await asyncio.to_thread(
        _listing_event_api_service().disable,
        base_asset,
        payload.reason if payload else None,
    )


@router.post('/base-assets/{base_asset}/disable', dependencies=[Depends(verify_token_dependency)])
async def disable_base_asset(base_asset: str, payload: DisableBaseAssetRequest | None = None) -> Dict[str, Any]:
    """将币种标记为失效，后续常规订阅/监控候选不再包含该币种。"""
    return await asyncio.to_thread(
        _base_asset_service().disable,
        base_asset,
        (payload.reason or '').strip() if payload else '',
    )


# ─── AG Grid 列配置管理 ───────────────────────────────────────────────────────

@router.get('/column-config/{page_key}')
async def get_column_config(page_key: str) -> Dict[str, Any]:
    """获取指定页面的AG Grid列配置"""
    return await asyncio.to_thread(_column_config_service().get, page_key)


@router.post('/column-config/{page_key}')
async def save_column_config(page_key: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """保存指定页面的AG Grid列配置"""
    return await asyncio.to_thread(_column_config_service().save, page_key, payload)


# ──────────────────────────────────────────────────────────────────
# 交易信号日志
# ──────────────────────────────────────────────────────────────────

@router.get('/signals')
async def get_signals(
    status: Optional[str] = Query(None, description="状态过滤: monitoring/opened/conditions_lost/rejected/gate_rejected"),
    exit_reason: Optional[str] = Query(None, description="结束原因模糊过滤"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    time_range: Optional[str] = Query("today", description="时间范围: today 或最近N天"),
    days: int = Query(1, ge=1, le=90, description="最近N天（time_range非today时生效）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """查询历史交易信号（支持分页）"""
    return await asyncio.to_thread(
        _trading_query_service().list_signals,
        status=status,
        exit_reason=exit_reason,
        base_asset=base_asset,
        time_range=time_range,
        days=days,
        page=page,
        page_size=page_size,
    )


@router.get('/reverse-signals')
async def get_reverse_signals(
    status: Optional[str] = Query(None, description="状态过滤: monitoring/opened/conditions_lost/rejected/gate_rejected/monitor_timeout"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(3, ge=1, le=30, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """查询反向套利交易信号（后端分页）。"""
    return await asyncio.to_thread(
        _reverse_query_service().list_signals,
        status=status,
        base_asset=base_asset,
        days=days,
        page=page,
        page_size=page_size,
    )


@router.get('/reverse-positions')
async def get_reverse_positions(
    status: Optional[str] = Query(None, description="状态过滤: holding/closing/closed/risk/desynced"),
    order_side: Optional[str] = Query(None, description="方向过滤(open=持仓中/close=已平仓)"),
    exchange_risk: bool = Query(False, description="仅展示交易所风险持仓"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(30, ge=1, le=365, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """查询反向套利持仓（独立于正向 mi_trade_position）。"""
    return await asyncio.to_thread(
        _reverse_query_service().list_positions,
        status=status,
        order_side=order_side,
        exchange_risk=exchange_risk,
        base_asset=base_asset,
        days=days,
        page=page,
        page_size=page_size,
    )


@router.get('/reverse-positions/{position_id}/orders')
async def get_reverse_position_orders(position_id: int) -> Dict[str, Any]:
    """获取指定反向持仓的全部订单明细（弹窗用）。"""
    return await asyncio.to_thread(
        _reverse_query_service().list_position_orders,
        position_id,
    )


@router.get('/reverse-orders')
async def get_reverse_orders(
    position_id: Optional[int] = Query(None, description="反向持仓ID过滤"),
    order_uuid: Optional[str] = Query(None, description="订单组UUID过滤"),
    order_side: Optional[str] = Query(None, description="方向过滤: open/close/repay/unwind"),
    status: Optional[str] = Query(None, description="状态过滤"),
    market_type: Optional[str] = Query(None, description="市场过滤: margin_spot/future/margin_repay"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(30, ge=1, le=365, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """查询反向套利订单（独立于正向 mi_trade_order）。"""
    return await asyncio.to_thread(
        _reverse_query_service().list_orders,
        position_id=position_id,
        order_uuid=order_uuid,
        order_side=order_side,
        status=status,
        market_type=market_type,
        base_asset=base_asset,
        days=days,
        page=page,
        page_size=page_size,
    )


@router.get('/reverse-capital')
async def get_reverse_capital():
    """读取反向套利资金快照（reverse 子账户，只读）。"""
    return await asyncio.to_thread(_reverse_query_service().capital)


@router.get('/reverse-reconciliation')
async def get_reverse_reconciliation(
    days: int = Query(365, ge=1, le=365, description="最近N天持仓"),
    mismatches_only: bool = Query(False, description="仅返回差异行"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
) -> Dict[str, Any]:
    """反向套利持仓对账（独立反向持仓表 + reverse 子账户）。"""
    return await asyncio.to_thread(
        _reconciliation_query_service().reverse_history,
        days=days,
        mismatches_only=mismatches_only,
        page=page,
        page_size=page_size,
    )
