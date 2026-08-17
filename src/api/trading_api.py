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
import math
import threading
import time
from decimal import Decimal
from datetime import datetime, date, timedelta
from typing import Optional, Any, Callable, List, Dict
from fastapi import APIRouter, Query, Depends, HTTPException
from pydantic import BaseModel

from common.database import db_manager
from common.config import config
from common.errors import ValidationAppError
from common.logger import get_logger
from api.auth import verify_token_dependency, verify_user_password
from common.meta_loader import fetch_contract_meta
from calc.reconciliation import build_default_reconciler, get_ignored_binance_spot_assets
from calc.account_capital import build_default_capital_snapshotter
from calc.delist_risk_monitor import DelistRiskConfig, DelistRiskMonitor
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
from calc.reverse_account_monitor import build_reverse_reconciliation_rows, get_reverse_capital_snapshot
from calc.reverse_trade_store import list_reverse_positions
from repositories.trading_query_repo import build_forward_signal_filters
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
from services.reverse_query_service import ReverseQueryService
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
    values = [str(part if part is not None else '').strip() for part in parts]
    return f"{prefix}:{':'.join(values)}"[:220]


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
    now = time.time()
    cached_report = _delist_risk_cache.get('report')
    if (
        cached_report is not None
        and _delist_risk_cache.get('lookahead_days') == lookahead_days
        and now - float(_delist_risk_cache.get('at') or 0) < max_age_sec
    ):
        return cached_report

    with _delist_risk_lock:
        now = time.time()
        cached_report = _delist_risk_cache.get('report')
        if (
            cached_report is not None
            and _delist_risk_cache.get('lookahead_days') == lookahead_days
            and now - float(_delist_risk_cache.get('at') or 0) < max_age_sec
        ):
            return cached_report

        try:
            monitor = DelistRiskMonitor(DelistRiskConfig(lookahead_days=lookahead_days))
            report = monitor.build_report()
            _delist_risk_cache.update({
                'at': now,
                'lookahead_days': lookahead_days,
                'report': report,
            })
            return report
        except Exception as exc:
            logger.warning(f'下架风险报告刷新失败: {exc}', exc_info=True)
            if cached_report is not None:
                return cached_report
            return {
                'success': False,
                'lookahead_days': lookahead_days,
                'items': [],
                'source_errors': {'internal': str(exc)},
            }


def _delist_risk_asset_set(report: Optional[Dict[str, Any]] = None) -> set[str]:
    report = report or _get_delist_risk_report_cached()
    return {
        str(item.get('base_asset') or '').strip().upper()
        for item in report.get('items', [])
        if item.get('base_asset')
    }


def _format_delist_risk_summary(items: List[Dict[str, Any]]) -> str:
    fragments = []
    for item in items:
        exchange = item.get('exchange') or ''
        message = item.get('message') or item.get('status') or item.get('risk_type') or '下架风险'
        due = item.get('delist_at')
        fragments.append(f"{exchange}:{message}{f' {due}' if due else ''}")
    return ' | '.join(fragments)


def _attach_delist_risks(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    report = _get_delist_risk_report_cached()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in report.get('items', []):
        asset = str(item.get('base_asset') or '').strip().upper()
        if not asset:
            continue
        grouped.setdefault(asset, []).append(item)

    for row in rows:
        asset = str(row.get('base_asset') or '').strip().upper()
        items = grouped.get(asset, [])
        levels = {item.get('severity') or item.get('risk_level') for item in items}
        row['delist_risks'] = items
        row['delist_risk_level'] = (
            'critical' if 'critical' in levels
            else 'warning' if items else None
        )
        row['delist_risk_summary'] = _format_delist_risk_summary(items) if items else None
        if not items:
            continue

        existing_status = row.get('exchange_risk_status')
        existing_detail = row.get('exchange_risk_detail')
        summary = f"下架风险: {row['delist_risk_summary']}"
        if not existing_status or existing_status == 'normal':
            row['exchange_risk_status'] = 'delist_risk'
            row['exchange_risk_type'] = 'delist_risk'
            row['exchange_risk_detail'] = summary
        elif existing_detail and summary not in str(existing_detail):
            row['exchange_risk_detail'] = f"{existing_detail} | {summary}"


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
    )


def _capital_query_service() -> CapitalQueryService:
    return CapitalQueryService(
        db_manager,
        serialize_row=_serialize_row,
        serialize_rows=_serialize_rows,
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
    ignored = sorted(get_ignored_binance_spot_assets())
    if not ignored:
        return '', []
    placeholders = ','.join(['%s'] * len(ignored))
    prefix = f"{table_alias}." if table_alias else ''
    return f" AND NOT ({prefix}exchange = 'binance' AND {prefix}base_asset IN ({placeholders}))", ignored


def _reconciliation_latest_sql(ignore_sql: str) -> str:
    return """
        SELECT
            s.*,
            c.quanto_multiplier
        FROM mi_recon_snapshot s
        LEFT JOIN mi_gate_future_contracts c
          ON UPPER(TRIM(c.base_asset)) COLLATE utf8mb4_unicode_ci
           = UPPER(TRIM(s.base_asset)) COLLATE utf8mb4_unicode_ci
        WHERE s.snapshot_at = (SELECT MAX(snapshot_at) FROM mi_recon_snapshot)
        {ignore_sql}
        ORDER BY s.exchange ASC, s.base_asset ASC
    """.format(ignore_sql=ignore_sql)


def _db_bool(value: Any) -> bool:
    if value is None:
        return False
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _should_emit_reconciliation_notification(row: Dict[str, Any], latest_snapshot_at: Any) -> bool:
    """Suppress one-off historical reconciliation mismatches caused by in-flight trades."""
    if latest_snapshot_at is not None and row.get('snapshot_at') == latest_snapshot_at:
        return True
    previous_is_match = row.get('previous_is_match')
    if previous_is_match is None:
        return False
    return not _db_bool(previous_is_match)


def _format_reconciliation_notification(row: Dict[str, Any], dedup_key: str) -> Dict[str, Any]:
    base_asset = row.get('base_asset') or '-'
    exchange = row.get('exchange') or '-'
    exchange_label = str(exchange).capitalize() if exchange != '-' else '-'
    dimension = row.get('dimension') or '-'
    detail = row.get('detail') if isinstance(row.get('detail'), dict) else {}
    is_error = base_asset == '__ERROR__' or dimension == 'error'

    if is_error:
        error_msg = detail.get('error_msg') or '未返回持仓数据'
        title = f"持仓对账拉取失败: {exchange_label}"
        message = f"{exchange_label} 对账接口错误: {error_msg}"
        status = 'error'
    else:
        title = f"持仓对账不一致: {base_asset}"
        message = (
            f"{exchange} {dimension} "
            f"local={row.get('local_value') if row.get('local_value') is not None else '-'} "
            f"exchange={row.get('exchange_value') if row.get('exchange_value') is not None else '-'} "
            f"diff={row.get('diff_value') if row.get('diff_value') is not None else '-'}"
        )
        status = 'mismatch'

    return {
        'dedup_key': dedup_key,
        'source': 'reconciliation',
        'severity': 'warning',
        'title': title,
        'message': message,
        'event_at': row.get('snapshot_at'),
        'base_asset': base_asset,
        'risk_type': dimension,
        'status': status,
        'detail': row,
    }


def _append_unique_notification(
    items: List[Dict[str, Any]],
    seen_keys: set[str],
    item: Dict[str, Any],
) -> None:
    dedup_key = str(item.get('dedup_key') or '')
    if dedup_key:
        if dedup_key in seen_keys:
            return
        seen_keys.add(dedup_key)
    items.append(item)


@router.get('/reconciliation/latest')
async def get_reconciliation_latest():
    """返回最近一轮对账快照。"""
    ignore_sql, ignore_params = _reconciliation_ignore_clause('s')
    sql = _reconciliation_latest_sql(ignore_sql)
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, ignore_params)
        rows = cursor.fetchall()
    return {'rows': _serialize_rows(rows)}


@router.get('/reconciliation/history')
async def get_reconciliation_history(
    days: int = Query(1, ge=1, le=30, description="最近N天"),
    mismatches_only: bool = Query(False, description="是否仅显示差异/错误"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(500, ge=1, le=5000, description="每页条数"),
):
    """查询对账历史快照。"""
    where_clauses = ["snapshot_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
    params: List[Any] = [days]
    if mismatches_only:
        where_clauses.append("is_match = 0")
    ignore_sql, ignore_params = _reconciliation_ignore_clause()
    where_sql = " AND ".join(where_clauses)

    with db_manager.get_cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS total FROM mi_recon_snapshot WHERE {where_sql}{ignore_sql}",
            [*params, *ignore_params],
        )
        total_row = cursor.fetchone()
        total = int(total_row['total']) if total_row else 0

    offset = (page - 1) * page_size
    sql = f"""
        SELECT *
        FROM mi_recon_snapshot
        WHERE {where_sql}{ignore_sql}
        ORDER BY snapshot_at DESC, exchange ASC, base_asset ASC
        LIMIT %s OFFSET %s
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, [*params, *ignore_params, page_size, offset])
        rows = cursor.fetchall()
    return {
        'rows': _serialize_rows(rows),
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if total else 0,
        },
    }


def _build_recent_risk_notification_items(hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
    cutoff = datetime.now() - timedelta(hours=hours)
    items: List[Dict[str, Any]] = []
    seen_notification_keys: set[str] = set()

    exchange_sql = """
        SELECT
            id, event_key, exchange, market_type, risk_type, base_asset, contract,
            event_at, side, size, fill_price, mark_price, liq_price, pnl,
            status, remediation_action, created_at, updated_at
        FROM mi_exchange_risk_event
        WHERE event_key NOT LIKE 'recon:%%'
          AND (created_at >= %s OR updated_at >= %s OR event_at >= %s)
        ORDER BY GREATEST(created_at, updated_at) DESC, event_at DESC
        LIMIT %s
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(exchange_sql, [cutoff, cutoff, cutoff, limit])
        exchange_rows = cursor.fetchall()

    for row in exchange_rows:
        row = _serialize_row(row)
        _append_unique_notification(items, seen_notification_keys, {
            'dedup_key': _risk_notification_key('exchange_risk', row.get('event_key')),
            'source': 'exchange_risk',
            'severity': 'error',
            'title': f"交易所风险: {row.get('base_asset') or '-'}",
            'message': (
                f"{row.get('exchange') or '-'} {row.get('risk_type') or 'unknown'} "
                f"status={row.get('status') or '-'} "
                f"size={row.get('size') if row.get('size') is not None else '-'} "
                f"price={row.get('fill_price') if row.get('fill_price') is not None else '-'}"
            ),
            'event_at': row.get('event_at'),
            'base_asset': row.get('base_asset'),
            'risk_type': row.get('risk_type'),
            'status': row.get('status'),
            'detail': row,
        })

    with db_manager.get_cursor() as cursor:
        cursor.execute("SELECT MAX(snapshot_at) AS latest_snapshot_at FROM mi_recon_snapshot")
        latest_recon_row = cursor.fetchone()
    latest_snapshot_at = latest_recon_row.get('latest_snapshot_at') if latest_recon_row else None

    ignore_sql, ignore_params = _reconciliation_ignore_clause('r')
    candidate_limit = min(max(limit * 5, 100), 1000)
    recon_sql = f"""
        SELECT
            r.id, r.snapshot_at, r.exchange, r.base_asset, r.dimension,
            r.local_value, r.exchange_value, r.diff_value, r.diff_ratio, r.detail,
            (
                SELECT prev.is_match
                FROM mi_recon_snapshot prev
                WHERE prev.exchange = r.exchange
                  AND prev.base_asset = r.base_asset
                  AND prev.dimension = r.dimension
                  AND prev.snapshot_at < r.snapshot_at
                ORDER BY prev.snapshot_at DESC
                LIMIT 1
            ) AS previous_is_match
        FROM mi_recon_snapshot r
        WHERE r.snapshot_at >= %s
          AND r.is_match = 0
          {ignore_sql}
        ORDER BY r.snapshot_at DESC, r.exchange ASC, r.base_asset ASC
        LIMIT %s
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(recon_sql, [cutoff, *ignore_params, candidate_limit])
        recon_rows = cursor.fetchall()

    for row in recon_rows:
        if not _should_emit_reconciliation_notification(row, latest_snapshot_at):
            continue
        row.pop('previous_is_match', None)
        row = _serialize_row(row)
        base_asset = row.get('base_asset') or '-'
        exchange = row.get('exchange') or '-'
        dimension = row.get('dimension') or '-'
        dedup_key = _risk_notification_key(
            'reconciliation',
            exchange,
            base_asset,
            dimension,
            row.get('local_value'),
            row.get('exchange_value'),
        )
        _append_unique_notification(
            items,
            seen_notification_keys,
            _format_reconciliation_notification(row, dedup_key),
        )

    items.sort(key=lambda item: str(item.get('event_at') or ''), reverse=True)
    return items[:limit]


def _sync_recent_popup_notifications() -> Dict[str, int]:
    """Materialize current backend risk/listing signals into persistent bell history."""
    listing_synced = 0
    risk_synced = 0

    listing_rows = list_listing_events(
        action_status='pending',
        candidate_status='matched',
        actionable_only=True,
        limit=20,
    )
    if listing_rows:
        rows = _serialize_rows(listing_rows)
        fingerprint = '|'.join(sorted(
            f"{row.get('base_asset')}:{row.get('gate_contract') or ''}:"
            f"{row.get('binance_symbol') or ''}:{row.get('last_seen_at') or ''}"
            for row in rows
        ))
        preview = '\n'.join(
            f"{row.get('base_asset')} Gate:{row.get('gate_contract') or '-'} "
            f"Binance:{row.get('binance_symbol') or '-'} "
            f"24h={float(row.get('gate_volume_24h_settle') or 0):.0f}/"
            f"{float(row.get('binance_quote_volume') or 0):.0f}"
            for row in rows[:8]
        )
        upsert_popup_notification(
            title=f"交易对上新候选 {len(rows)} 个",
            message=preview,
            type='warning',
            source='listing_events',
            dedup_key=_risk_notification_key('listing_events', fingerprint),
            event_at=max((row.get('last_seen_at') for row in rows if row.get('last_seen_at')), default=None),
            payload={'items': rows},
        )
        listing_synced = 1

    risk_items = _build_recent_risk_notification_items(hours=24, limit=50)
    risk_synced = upsert_popup_notifications(
        [
            {
                'title': item.get('title') or '交易风险通知',
                'message': item.get('message') or '',
                'type': 'error' if item.get('severity') == 'error' else 'warning',
                'source': item.get('source') or 'risk',
                'dedup_key': item.get('dedup_key'),
                'event_at': item.get('event_at'),
                'payload': item,
            }
            for item in risk_items
        ],
    )
    return {'listing_events': listing_synced, 'risk': risk_synced}


@router.get('/risk-notifications/recent')
async def get_recent_risk_notifications(
    hours: int = Query(24, ge=1, le=168, description="回看最近N小时风险事件"),
    limit: int = Query(50, ge=1, le=200, description="最多返回事件数"),
):
    """返回需要进入前端铃铛的近期风险事件。"""
    items = _build_recent_risk_notification_items(hours=hours, limit=limit)
    return {
        'items': items,
        'summary': {
            'total': len(items),
            'exchange_risk': sum(1 for item in items if item.get('source') == 'exchange_risk'),
            'reconciliation': sum(1 for item in items if item.get('source') == 'reconciliation'),
        },
        'lookback_hours': hours,
    }


@router.get('/notifications')
async def get_popup_notifications(
    read_status: str = Query('unread', description="读取状态：unread/read/all"),
    source: Optional[str] = Query(None, description="消息来源过滤"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sync_recent: bool = Query(True, description="先同步近期后端风险/上新事件"),
):
    """查询持久化铃铛消息。"""
    synced = _sync_recent_popup_notifications() if sync_recent else {}
    result = list_popup_notifications(
        read_status=read_status,
        source=source,
        page=page,
        page_size=page_size,
    )
    return {
        'items': _serialize_rows(result['items']),
        'pagination': result['pagination'],
        'unread_count': result['unread_count'],
        'synced': synced,
    }


@router.get('/notifications/unread-count')
async def get_popup_notification_unread_count():
    """返回铃铛未读数量。"""
    return {'unread_count': count_unread_popup_notifications()}


@router.post('/notifications', dependencies=[Depends(verify_token_dependency)])
async def create_popup_notification(payload: PopupNotificationCreateRequest):
    """写入一条持久化铃铛消息。"""
    row = upsert_popup_notification(
        title=payload.title,
        message=payload.message,
        type=payload.type or 'info',
        source=payload.source,
        dedup_key=payload.dedup_key,
        event_at=payload.event_at,
        payload=payload.payload,
    )
    return {'success': True, 'item': _serialize_row(row)}


@router.post('/notifications/mark-read', dependencies=[Depends(verify_token_dependency)])
async def mark_popup_notification_read(payload: PopupNotificationMarkReadRequest):
    """将指定消息或全部未读消息标记为已读。"""
    affected = mark_popup_notifications_read(ids=payload.ids)
    return {'success': True, 'affected': affected, 'unread_count': count_unread_popup_notifications()}


@router.post('/notifications/{notification_id}/read', dependencies=[Depends(verify_token_dependency)])
async def mark_one_popup_notification_read(notification_id: int):
    """将单条铃铛消息标记为已读。"""
    affected = mark_popup_notifications_read(ids=[notification_id])
    return {'success': True, 'affected': affected, 'unread_count': count_unread_popup_notifications()}


@router.post('/reconciliation/run')
async def run_reconciliation_now():
    """手动触发一次对账。virtual 模式下跳过。"""
    global _recon_running
    if config.get_trade_mode() == 'virtual':
        return {'success': False, 'message': 'virtual 模式不执行交易所对账'}
    if not config.get_bool('reconciliation.enabled', True):
        return {'success': False, 'message': '对账功能已关闭'}

    with _recon_lock:
        if _recon_running:
            return {'success': False, 'message': '对账任务正在执行中'}
        _recon_running = True

    try:
        result = await asyncio.to_thread(
            lambda: build_default_reconciler().run_with_fast_confirmation()
        )
        return {'success': True, 'message': '对账完成', **result}
    except Exception as e:
        logger.error(f'手动对账失败: {e}', exc_info=True)
        return {'success': False, 'message': f'对账失败: {e}'}
    finally:
        with _recon_lock:
            _recon_running = False


def _format_dust_cleanup_message(cleanup: Dict[str, Any]) -> str:
    if cleanup.get('message'):
        return str(cleanup['message'])

    if cleanup.get('success'):
        return '小额残余清理完成'

    reason = str(cleanup.get('reason') or 'unknown')
    if reason == 'binance_dust_conversion_cooldown':
        remaining = cleanup.get('cooldown_remaining_sec')
        if remaining is not None:
            seconds = max(0, int(math.ceil(float(remaining))))
            return f'小额残余清理失败: Binance 小额兑换冷却中，剩余 {seconds} 秒'
        return '小额残余清理失败: Binance 小额兑换冷却中'

    skipped = cleanup.get('skipped')
    if reason == 'no_safe_dust_found' and isinstance(skipped, list) and skipped:
        summary = '；'.join(
            f"{item.get('base_asset') or '-'}={item.get('reason') or 'unknown'}"
            for item in skipped[:5]
            if isinstance(item, dict)
        )
        if summary:
            return f'未发现可安全兑换的小额残余，已跳过: {summary}'

    return f'小额残余清理失败: {reason}'


@router.post(
    '/reconciliation/dust/cleanup',
    dependencies=[Depends(verify_token_dependency)],
)
async def cleanup_reconciliation_dust():
    """Clean one fully explained post-close dust hedge, then refresh reconciliation."""
    global _recon_running
    if config.get_trade_mode() == 'virtual':
        return {'success': False, 'message': 'virtual 模式不执行小额兑换'}
    if not config.get_bool('reconciliation.enabled', True):
        return {'success': False, 'message': '对账功能已关闭'}

    with _recon_lock:
        if _recon_running:
            return {'success': False, 'message': '对账任务正在执行中'}
        _recon_running = True

    try:
        def _cleanup_and_reconcile():
            reconciler = build_default_reconciler()
            cleanup = reconciler.cleanup_post_close_dust()
            reconciliation = reconciler.run_once()
            return cleanup, reconciliation

        cleanup, reconciliation = await asyncio.to_thread(_cleanup_and_reconcile)
        message = _format_dust_cleanup_message(cleanup)
        return {
            **cleanup,
            'message': message,
            'reconciliation': reconciliation,
        }
    except Exception as e:
        logger.error(f'小额残余清理失败: {e}', exc_info=True)
        return {'success': False, 'message': f'小额残余清理失败: {e}'}
    finally:
        with _recon_lock:
            _recon_running = False


# ─── 真实资金快照 ────────────────────────────────────────────────────────────

def _parse_capital_range_datetime(value: str, field_name: str) -> datetime:
    text = (value or '').strip()
    if not text:
        raise HTTPException(status_code=400, detail=f'{field_name} 不能为空')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise HTTPException(status_code=400, detail=f'{field_name} 格式必须为 YYYY-MM-DD HH:mm:ss')


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
    global _capital_running
    if config.get_trade_mode() == 'virtual':
        return {'success': False, 'message': 'virtual 模式不采集交易所真实资金'}
    if not config.get_bool('account_capital.enabled', True):
        return {'success': False, 'message': '真实资金采集已关闭'}

    with _capital_lock:
        if _capital_running:
            return {'success': False, 'message': '资金采集正在执行中'}
        _capital_running = True

    try:
        provider = _capital_strategy_pnl_provider
        if provider is None:
            return {
                'success': False,
                'message': '实时策略盈亏尚未就绪，本次未写入资金快照',
            }
        strategy_pnl = await asyncio.to_thread(provider)
        if not isinstance(strategy_pnl, dict):
            raise RuntimeError('实时策略盈亏返回格式无效')
        result = await asyncio.to_thread(
            lambda: build_default_capital_snapshotter().run_once(strategy_pnl)
        )
        return {'success': True, 'message': '资金采集完成', **result}
    except Exception as e:
        logger.error(f'手动资金采集失败: {e}', exc_info=True)
        return {'success': False, 'message': f'资金采集失败: {e}'}
    finally:
        with _capital_lock:
            _capital_running = False


@router.get('/capital/fund-transfer', dependencies=[Depends(verify_token_dependency)])
async def get_fund_transfer_tasks(limit: int = Query(30, ge=1, le=200)):
    """Return the active transfer and durable transfer history."""
    try:
        service = get_fund_transfer_service()
        active = service.store.get_active()
        history = service.store.list(limit=limit)
        return {
            'active': _serialize_row(active) if active else None,
            'history': _serialize_rows(history),
            'open_locked': service.open_locked,
        }
    except Exception as exc:
        logger.error('读取资金划转任务失败: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f'读取资金划转任务失败: {exc}')


@router.get(
    '/capital/fund-transfer/limits',
    dependencies=[Depends(verify_token_dependency)],
)
async def get_fund_transfer_limits():
    """Return current live minimum and maximum transferable amounts."""
    try:
        result = await asyncio.to_thread(
            get_fund_transfer_service().limits
        )
        result.pop('_network_info', None)
        return {'success': True, 'limits': _serialize_row(result)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error('读取资金划转额度失败: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f'读取资金划转额度失败: {exc}')


@router.get(
    '/capital/fund-transfer/preflight',
    dependencies=[Depends(verify_token_dependency)],
)
async def preflight_fund_transfer(amount: Decimal = Query(..., gt=0)):
    """Read live balances, fee, network and fixed destination without moving money."""
    try:
        result = await asyncio.to_thread(
            lambda: get_fund_transfer_service().preview(amount)
        )
        return {'success': True, 'preview': _serialize_row(result)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error('资金划转预检失败: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f'资金划转预检失败: {exc}')


@router.post('/capital/fund-transfer', status_code=201)
async def create_fund_transfer(
    req: FundTransferCreateRequest,
    user: Dict[str, Any] = Depends(verify_token_dependency),
):
    """Create one real transfer after current-password re-authentication."""
    if config.get_trade_mode() == 'virtual':
        raise HTTPException(status_code=409, detail='virtual 模式不执行真实资金划转')
    if not verify_user_password(user_id=user.get('user_id'), password=req.password):
        raise HTTPException(status_code=403, detail='当前登录密码校验失败')
    try:
        service = get_fund_transfer_service()
        task = await asyncio.to_thread(
            lambda: service.create_task(
                amount=req.amount,
                user_id=str(user.get('user_id') or 'default'),
                username=str(user.get('username') or ''),
            )
        )
        return {'success': True, 'task': _serialize_row(task)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error('创建资金划转任务失败: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f'创建资金划转任务失败: {exc}')


@router.post('/capital/fund-transfer/{task_id}/retry')
async def retry_fund_transfer(
    task_id: int,
    req: FundTransferRetryRequest,
    user: Dict[str, Any] = Depends(verify_token_dependency),
):
    """Recheck an ambiguous task or retry only its location-safe recovery step."""
    if not verify_user_password(user_id=user.get('user_id'), password=req.password):
        raise HTTPException(status_code=403, detail='当前登录密码校验失败')
    try:
        task = await asyncio.to_thread(
            lambda: get_fund_transfer_service().request_retry(task_id)
        )
        return {'success': True, 'task': _serialize_row(task)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.error('资金划转恢复失败: task=%s error=%s', task_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f'资金划转恢复失败: {exc}')


@router.post('/capital/clear-range', dependencies=[Depends(verify_token_dependency)])
async def clear_capital_snapshot_range(req: CapitalClearRangeRequest):
    """按时间段清理资金快照数据，清理前自动备份命中行。"""
    start_at = _parse_capital_range_datetime(req.start_at, 'start_at')
    end_at = _parse_capital_range_datetime(req.end_at, 'end_at')
    if start_at > end_at:
        raise HTTPException(status_code=400, detail='开始时间不能晚于结束时间')

    backup_table = f"mi_capital_snapshot_backup_clear_range_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    try:
        with db_manager.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS row_count,
                        MIN(snapshot_at) AS first_snapshot_at,
                        MAX(snapshot_at) AS last_snapshot_at
                    FROM mi_capital_snapshot
                    WHERE snapshot_at BETWEEN %s AND %s
                    """,
                    (start_at, end_at),
                )
                summary = cursor.fetchone() or {}
                row_count = int(summary.get('row_count') or 0)
                if row_count <= 0:
                    return {
                        'success': True,
                        'deleted': 0,
                        'backup_table': None,
                        'message': '指定时间段没有资金监控数据',
                    }

                cursor.execute(
                    f"""
                    CREATE TABLE `{backup_table}` AS
                    SELECT *
                    FROM mi_capital_snapshot
                    WHERE snapshot_at BETWEEN %s AND %s
                    """,
                    (start_at, end_at),
                )
                cursor.execute(
                    """
                    DELETE FROM mi_capital_snapshot
                    WHERE snapshot_at BETWEEN %s AND %s
                    """,
                    (start_at, end_at),
                )
                deleted = cursor.rowcount

        logger.warning(
            '资金监控数据已按时间段清理: start=%s end=%s deleted=%s backup=%s',
            start_at.strftime('%Y-%m-%d %H:%M:%S'),
            end_at.strftime('%Y-%m-%d %H:%M:%S'),
            deleted,
            backup_table,
        )
        return {
            'success': True,
            'deleted': deleted,
            'backup_table': backup_table,
            'first_snapshot_at': _serialize_row({'value': summary.get('first_snapshot_at')})['value'],
            'last_snapshot_at': _serialize_row({'value': summary.get('last_snapshot_at')})['value'],
            'message': f'已清理 {deleted} 条资金监控数据',
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error('资金监控数据按时间段清理失败: %s', exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f'清理失败: {exc}')


@router.post('/capital/binance-bnb/buy', dependencies=[Depends(verify_token_dependency)])
async def buy_forward_binance_bnb(req: BinanceBnbBuyRequest):
    """FORWARD Binance Spot 使用 USDT 市价买入 BNB 手续费余额。"""
    if config.get_trade_mode() == 'virtual':
        return {'success': False, 'message': 'virtual 模式不执行 Binance 真实买入'}
    try:
        result = await asyncio.to_thread(lambda: build_default_forward_bnb_fee_buyer().buy_with_usdt(req.amount_usdt))
    except ValueError as exc:
        return {'success': False, 'message': str(exc)}
    except Exception as exc:
        logger.error('FORWARD Binance BNB 手续费余额买入失败: %s', exc, exc_info=True)
        return {'success': False, 'message': f'买入失败: {exc}'}

    payload = {
        'success': result.success,
        'message': result.message,
        'amount_usdt': result.amount_usdt,
        'result': result.result,
    }
    if result.success:
        try:
            snapshot = await asyncio.to_thread(lambda: build_default_capital_snapshotter().run_once())
            payload['capital_snapshot'] = snapshot
        except Exception as exc:
            logger.warning('FORWARD Binance BNB 买入后资金快照刷新失败: %s', exc, exc_info=True)
            payload['snapshot_error'] = str(exc)
    return payload


# ─── VWAP 基差阈值 ────────────────────────────────────────────────────────────

@router.get('/threshold/latest-date')
async def get_threshold_latest_date():
    """获取 BTC 的最新数据写入时间（用于页面展示，避免全表扫描）"""
    sql = """
        SELECT updated_at
        FROM mi_vwap_basis_threshold
        WHERE base_asset = 'BTC'
          AND updated_at IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
        if row and row.get('updated_at'):
            d = row['updated_at']
            return {"latest_date": d.strftime('%Y-%m-%d %H:%M') if hasattr(d, 'strftime') else str(d)}
        return {"latest_date": None}


@router.get('/threshold/dates')
async def get_threshold_dates():
    """获取 mi_vwap_basis_threshold 表中所有可用日期（降序）"""
    sql = "SELECT DISTINCT calc_date FROM mi_vwap_basis_threshold ORDER BY calc_date DESC LIMIT 365"
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [row['calc_date'].strftime('%Y-%m-%d') if hasattr(row['calc_date'], 'strftime') else str(row['calc_date']) for row in rows]


@router.get('/threshold/assets')
async def get_threshold_assets():
    """获取 mi_vwap_basis_threshold 表中所有标的（按 open_basis_p20 降序）"""
    sql = """
        SELECT base_asset
        FROM mi_vwap_basis_threshold
        WHERE calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
        ORDER BY open_basis_p20 DESC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [row['base_asset'] for row in rows]


@router.get('/threshold/data')
async def get_threshold_data(
    calc_date: Optional[str] = Query(None, description="计算日期，格式 YYYY-MM-DD，默认最新日期"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
):
    """查询 mi_vwap_basis_threshold 表数据（后端分页）"""
    where_sql = " FROM mi_vwap_basis_threshold WHERE 1=1"
    params: list = []

    if calc_date:
        where_sql += " AND calc_date = %s"
        params.append(calc_date)
    else:
        where_sql += " AND calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)"

    if base_asset:
        where_sql += " AND base_asset = %s"
        params.append(base_asset)

    count_sql = "SELECT COUNT(*) AS total" + where_sql
    with db_manager.get_cursor() as cursor:
        cursor.execute(count_sql, params)
        total_row = cursor.fetchone()
        total = int(total_row['total']) if total_row and total_row.get('total') is not None else 0

    offset = (page - 1) * page_size
    sql = """
        SELECT
            id, base_asset, calc_date,
            open_basis_max, open_basis_min,
            open_basis_p1, open_basis_p2, open_basis_p3, open_basis_p5, open_basis_p10, open_basis_p20,
            close_basis_max, close_basis_min,
            close_basis_p1, close_basis_p2, close_basis_p3, close_basis_p5, close_basis_p10, close_basis_p20,
            updated_at
    """ + where_sql + " ORDER BY open_basis_p20 DESC, base_asset ASC LIMIT %s OFFSET %s"
    data_params = [*params, page_size, offset]

    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, data_params)
        rows = cursor.fetchall()
        return {
            'rows': _serialize_rows(rows),
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size,
            },
        }


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
    global _threshold_calc_running

    def on_progress(progress: Dict[str, Any]):
        processed = int(progress.get('processed') or 0)
        total = int(progress.get('total') or 0)
        _set_threshold_calc_status(
            processed=processed,
            total=total,
            current_asset=progress.get('current_asset'),
            success_count=progress.get('success_count', 0),
            skip_count=progress.get('skip_count', 0),
            fail_count=progress.get('fail_count', 0),
            message=f'{processed}/{total}' if total else '准备中',
        )

    try:
        from calc.calculate_vwap_basis_threshold import run_analysis
        run_analysis(lookback_days, progress_callback=on_progress)
        status = _get_threshold_calc_status()
        total = int(status.get('total') or 0)
        _set_threshold_calc_status(
            running=False,
            processed=total,
            message=f'{total}/{total} 计算完成' if total else '计算完成',
            finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            error=None,
        )
    except Exception as e:
        logger.error(f"手动执行阈值计算失败: {e}", exc_info=True)
        _set_threshold_calc_status(
            running=False,
            message=f'计算失败: {e}',
            finished_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            error=str(e),
        )
    finally:
        with _threshold_calc_lock:
            _threshold_calc_running = False


@router.post('/threshold/calculate')
async def trigger_threshold_calculate():
    """手动触发 VWAP 基差分位阈值计算（后台执行）"""
    global _threshold_calc_running
    if not config.get_bool('trade.vwap.update_threshold_enabled', True):
        return {
            "success": False,
            "message": "VWAP基差分位阈值更新已关闭，仅保留 mi_vwap_basis_snapshot 快照",
        }

    with _threshold_calc_lock:
        if _threshold_calc_running:
            return {"success": False, "message": "计算任务正在执行中", "status": dict(_threshold_calc_status)}

        lookback_days = config.get_int('trade.vwap.threshold_lookback_days', 7)
        _threshold_calc_running = True
        _threshold_calc_status.update({
            'running': True,
            'processed': 0,
            'total': 0,
            'current_asset': None,
            'success_count': 0,
            'skip_count': 0,
            'fail_count': 0,
            'message': '准备中',
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'finished_at': None,
            'error': None,
        })

    thread = threading.Thread(
        target=_run_threshold_calculate_job,
        args=(lookback_days,),
        name='vwap-threshold-calculate',
        daemon=True,
    )
    thread.start()
    return {"success": True, "message": f"计算已启动（回溯 {lookback_days} 天）", "status": _get_threshold_calc_status()}


@router.get('/threshold/calculate/status')
async def get_threshold_calculate_status():
    """获取手动 VWAP 阈值计算进度"""
    return _get_threshold_calc_status()


@router.get('/delist-risks')
async def get_delist_risks(
    lookahead_days: int = Query(30, ge=1, le=180, description="下架计划预警窗口（天）"),
):
    """检查当前监控/持仓标的的交易所下架风险。"""
    return _get_delist_risk_report_cached(lookahead_days=lookahead_days)


@router.get('/listing-events')
async def get_listing_events(
    action_status: Optional[str] = Query(None, description="处理状态过滤：pending/acknowledged/ignored/disabled/added_to_monitor/all"),
    candidate_status: Optional[str] = Query(None, description="候选状态过滤：matched/gate_only/binance_only/added_to_monitor/all"),
    monitor_status: Optional[str] = Query(None, description="监控状态过滤：not_added/added/all"),
    actionable_only: bool = Query(False, description="仅展示可提醒候选"),
    limit: int = Query(200, ge=1, le=1000),
):
    """查询交易对上新事件。"""
    rows = list_listing_events(
        action_status=action_status,
        candidate_status=candidate_status,
        monitor_status=monitor_status,
        actionable_only=actionable_only,
        limit=limit,
    )
    return {
        'items': _serialize_rows(rows),
        'summary': _serialize_row(listing_event_summary()),
    }


@router.get('/listing-events/summary')
async def get_listing_events_summary():
    """交易对上新事件摘要，用于固定时间弹窗提醒。"""
    items = list_listing_events(
        action_status='pending',
        candidate_status='matched',
        actionable_only=True,
        limit=20,
    )
    return {
        'summary': _serialize_row(listing_event_summary()),
        'items': _serialize_rows(items),
    }


@router.post('/listing-events/refresh', dependencies=[Depends(verify_token_dependency)])
async def refresh_listing_events_api():
    """手动刷新交易对上新事件。"""
    return refresh_listing_events()


@router.post('/listing-events/{base_asset}/ack', dependencies=[Depends(verify_token_dependency)])
async def ack_listing_event(base_asset: str, payload: ListingEventActionRequest | None = None):
    """确认上新事件；保留在页面，但不再弹窗。"""
    asset = (base_asset or '').strip().upper()
    affected = mark_listing_events([asset], 'acknowledged', (payload.reason if payload else None) or 'acknowledged')
    return {'success': True, 'base_asset': asset, 'affected': affected}


@router.post('/listing-events/{base_asset}/ignore', dependencies=[Depends(verify_token_dependency)])
async def ignore_listing_event(base_asset: str, payload: ListingEventActionRequest | None = None):
    """忽略上新事件；不修改 mi_base_asset。"""
    asset = (base_asset or '').strip().upper()
    affected = mark_listing_events([asset], 'ignored', (payload.reason if payload else None) or 'ignored')
    return {'success': True, 'base_asset': asset, 'affected': affected}


@router.post('/listing-events/{base_asset}/add-to-monitor', dependencies=[Depends(verify_token_dependency)])
async def add_listing_event_to_monitor(base_asset: str):
    """将上新候选加入 mi_base_asset，后续按普通标的进入监控候选。"""
    try:
        result = add_listing_asset_to_monitor(base_asset)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    subscription = await asyncio.to_thread(_subscribe_listing_asset_orderbook, result.get('base_asset') or base_asset)
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
async def disable_listing_event_asset(base_asset: str, payload: ListingEventActionRequest | None = None):
    """将上新候选写入/更新为失效标的，后续不再弹窗。"""
    try:
        return disable_listing_asset(
            base_asset,
            (payload.reason if payload else None) or 'listing_event_disabled',
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post('/base-assets/{base_asset}/disable', dependencies=[Depends(verify_token_dependency)])
async def disable_base_asset(base_asset: str, payload: DisableBaseAssetRequest | None = None):
    """将币种标记为失效，后续常规订阅/监控候选不再包含该币种。"""
    asset = (base_asset or '').strip().upper()
    if not asset or not asset.replace('_', '').replace('-', '').isalnum():
        raise HTTPException(status_code=400, detail='无效标的资产')

    reason = (payload.reason or '').strip() if payload else ''
    with db_manager.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS holding_count
                FROM mi_trade_position
                WHERE status = 'holding'
                  AND UPPER(TRIM(base_asset)) = %s
                """,
                [asset],
            )
            holding_count = int((cursor.fetchone() or {}).get('holding_count') or 0)
            cursor.execute(
                """
                UPDATE mi_base_asset
                SET is_valid = 'N'
                WHERE UPPER(TRIM(base_asset)) = %s
                """,
                [asset],
            )
            affected = cursor.rowcount

    if affected <= 0:
        raise HTTPException(status_code=404, detail=f'{asset} 不存在于 mi_base_asset')

    logger.warning(
        '标的资产已设为失效: asset=%s holding_count=%s reason=%s',
        asset,
        holding_count,
        reason or '-',
    )
    return {
        'success': True,
        'base_asset': asset,
        'affected': affected,
        'holding_count': holding_count,
        'requires_service_reload': True,
        'message': (
            f'{asset} 已设为失效；当前仍有持仓，系统会保留必要持仓风险监控直到平仓，'
            '常规新订阅/新监控候选将在重启订单簿服务后排除。'
            if holding_count > 0
            else f'{asset} 已设为失效；重启订单簿服务后不再进入常规订阅/监控。'
        ),
    }


# ─── AG Grid 列配置管理 ───────────────────────────────────────────────────────

@router.get('/column-config/{page_key}')
async def get_column_config(page_key: str):
    """获取指定页面的AG Grid列配置"""
    # TODO: 从认证信息中获取 user_id，暂时使用 'default'
    user_id = 'default'
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            "SELECT col_id, sort_order, is_visible, width, pinned, sort, filter_model "
            "FROM ag_grid_column_config "
            "WHERE user_id = %s AND page_key = %s "
            "ORDER BY sort_order ASC",
            (user_id, page_key)
        )
        rows = cursor.fetchall()
    
    # 转换为 AG Grid ColumnState 格式
    # 注意：AG Grid 的 applyColumnState 依赖数组顺序来确定列顺序
    # order 字段不是必须的，关键是数组的排列顺序
    column_state = []
    for row in rows:
        state = {
            'colId': row['col_id'],
            'hide': not bool(row['is_visible']),
        }
        if row['width'] is not None:
            state['width'] = row['width']
        if row['pinned']:
            state['pinned'] = row['pinned']
        if row['sort']:
            state['sort'] = row['sort']
        if row['filter_model']:
            state['filterModel'] = json.loads(row['filter_model']) if isinstance(row['filter_model'], str) else row['filter_model']
        column_state.append(state)
    
    return {'columnState': column_state}


@router.post('/column-config/{page_key}')
async def save_column_config(page_key: str, payload: Dict[str, Any]):
    """保存指定页面的AG Grid列配置"""
    # TODO: 从认证信息中获取 user_id，暂时使用 'default'
    user_id = 'default'
    column_state = payload.get('columnState', [])
    
    if not column_state or not isinstance(column_state, list):
        return {'success': False, 'message': 'columnState 必须是非空数组'}
    
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        try:
            # 批量 upsert（列顺序由数组索引决定，而非 item 中的 order 字段）
            for idx, item in enumerate(column_state):
                if 'colId' not in item:
                    continue
                
                filter_model_json = None
                if item.get('filterModel'):
                    filter_model_json = json.dumps(item['filterModel'])
                
                cursor.execute(
                    "INSERT INTO ag_grid_column_config "
                    "(user_id, page_key, col_id, sort_order, is_visible, width, pinned, sort, filter_model) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "sort_order = VALUES(sort_order), "
                    "is_visible = VALUES(is_visible), "
                    "width = VALUES(width), "
                    "pinned = VALUES(pinned), "
                    "sort = VALUES(sort), "
                    "filter_model = VALUES(filter_model)",
                    (
                        user_id,
                        page_key,
                        item['colId'],
                        idx,
                        not item.get('hide', False),
                        item.get('width'),
                        item.get('pinned'),
                        item.get('sort'),
                        filter_model_json
                    )
                )
            return {'success': True, 'message': '列配置保存成功'}
        except Exception as e:
            logger.error(f"保存列配置失败: {e}", exc_info=True)
            return {'success': False, 'message': f'保存失败: {str(e)}'}
        finally:
            cursor.close()


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
    return get_reverse_capital_snapshot()


@router.get('/reverse-reconciliation')
async def get_reverse_reconciliation(
    days: int = Query(365, ge=1, le=365, description="最近N天持仓"),
    mismatches_only: bool = Query(False, description="仅返回差异行"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
):
    """反向套利持仓对账（独立反向持仓表 + reverse 子账户）。"""
    result = list_reverse_positions(
        status='holding',
        days=days,
        page=1,
        page_size=5000,
    )
    payload = build_reverse_reconciliation_rows(_serialize_rows(result.rows))
    rows = payload.get('rows') or []
    if mismatches_only:
        rows = [row for row in rows if not row.get('is_match')]
    total = len(rows)
    offset = (page - 1) * page_size
    payload['rows'] = rows[offset:offset + page_size]
    payload['pagination'] = {
        'page': page,
        'page_size': page_size,
        'total': total,
        'total_pages': (total + page_size - 1) // page_size if total else 0,
    }
    return payload
