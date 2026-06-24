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
import time
from decimal import Decimal
from datetime import datetime, date, timedelta
from typing import Optional, Any, List, Dict
from fastapi import APIRouter, Query, Depends, HTTPException
from pydantic import BaseModel

from common.database import db_manager
from common.config import config
from common.logger import get_logger
from api.auth import verify_token_dependency
from common.meta_loader import fetch_contract_meta
from calc.reconciliation import build_default_reconciler, get_ignored_binance_spot_assets
from calc.account_capital import build_default_capital_snapshotter
from calc.delist_risk_monitor import DelistRiskConfig, DelistRiskMonitor
from calc.forward_bnb_fee import build_default_forward_bnb_fee_buyer
from calc.gate_position_risk import attach_gate_position_risk
from calc.listing_event_monitor import (
    add_listing_asset_to_monitor,
    disable_listing_asset,
    listing_event_summary,
    list_listing_events,
    mark_listing_events,
    refresh_listing_events,
)
from calc.orderbook_data_client import OrderBookDataClient
from calc.position_order_fees import attach_position_order_fee_summary
from calc.popup_notification_store import (
    count_unread_popup_notifications,
    list_popup_notifications,
    mark_popup_notifications_read,
    upsert_popup_notification,
    upsert_popup_notifications,
)
from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl
from calc.reverse_account_monitor import build_reverse_reconciliation_rows, get_reverse_capital_snapshot
from calc.reverse_trade_store import (
    list_reverse_orders,
    list_reverse_position_orders,
    list_reverse_positions,
    summarize_reverse_positions,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/trading", tags=["trading"])

_ALLOWED_CLOSE_THRESHOLD_COLS = {
    'close_basis_p1',
    'close_basis_p2',
    'close_basis_p3',
    'close_basis_p5',
    'close_basis_p10',
    'close_basis_p20',
}

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
        margin_leverage=config.get_float('margin.leverage', 2.0),
        margin_default_mmr=config.get_float('margin.default_maintenance_rate', 0.005),
    )


def _build_signal_time_filter(time_range: Optional[str], days: int, prefix: str = "") -> tuple[str, List]:
    column = f"{prefix}signal_time"
    range_key = (time_range or "today").strip().lower()
    if range_key in {"today", "date_today"}:
        return f"{column} >= CURDATE() AND {column} < DATE_ADD(CURDATE(), INTERVAL 1 DAY)", []
    return f"{column} >= DATE_SUB(NOW(), INTERVAL %s DAY)", [days]


def _build_forward_signal_filters(
    *,
    status: Optional[str],
    exit_reason: Optional[str],
    base_asset: Optional[str],
    time_range: Optional[str],
    days: int,
    prefix: str = "",
) -> tuple[str, List]:
    field_prefix = prefix
    conditions: List[str] = []
    params: List = []
    time_sql, time_params = _build_signal_time_filter(time_range, days, field_prefix)
    conditions.append(time_sql)
    params.extend(time_params)

    if status:
        conditions.append(f"{field_prefix}status = %s")
        params.append(status)
    if exit_reason:
        conditions.append(f"{field_prefix}exit_reason LIKE %s")
        params.append(f"%{exit_reason}%")
    if base_asset:
        conditions.append(f"{field_prefix}base_asset LIKE %s")
        params.append(f"%{base_asset}%")

    return " AND ".join(conditions), params


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


@router.get('/orders')
async def get_orders(
    status: Optional[str] = Query(None, description="持仓状态(holding/closed)"),
    channel: Optional[str] = Query(None, description="渠道过滤"),
    order_side: Optional[str] = Query(None, description="方向过滤(open=持仓中/close=已平仓)"),
    exchange_risk: bool = Query(False, description="仅展示交易所风险持仓"),
    position_id: Optional[int] = Query(None, description="持仓ID过滤"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(90, ge=1, le=90, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页持仓数"),
):
    """查询持仓列表（直接查 mi_trade_position，按持仓分页）"""
    close_threshold_col = config.get_str(
        'trade.vwap.close_threshold_percentile',
        'close_basis_p20',
    ).strip()
    if close_threshold_col not in _ALLOWED_CLOSE_THRESHOLD_COLS:
        logger.warning(f'无效平仓VWAP阈值字段 {close_threshold_col}，回退 close_basis_p20')
        close_threshold_col = 'close_basis_p20'

    # ─── 构建 WHERE 条件 ───
    base_where_clauses = ["p.opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
    base_params: list = [days]

    legacy_status_clause = None
    if status in ('holding', 'closed'):
        legacy_status_clause = status
    elif status in ('pending', 'rejected', 'failed'):
        # 这些状态不适用于持仓级别，返回空。保留兼容旧前端/旧书签。
        return {
            'orders': [],
            'pagination': {'page': page, 'page_size': page_size, 'total': 0, 'total_pages': 0},
            'summary': {'total': 0, 'open': 0, 'close': 0, 'exchange_risk': 0},
        }

    if base_asset:
        base_where_clauses.append("p.base_asset = %s")
        base_params.append(base_asset)

    if position_id is not None:
        base_where_clauses.append("p.id = %s")
        base_params.append(position_id)

    if channel:
        base_where_clauses.append("EXISTS (SELECT 1 FROM mi_trade_order o WHERE o.position_id = p.id AND o.channel = %s)")
        base_params.append(channel)

    delist_risk_assets: list[str] = []
    if exchange_risk:
        delist_risk_assets = sorted(_delist_risk_asset_set())
        if delist_risk_assets:
            placeholders = ','.join(['%s'] * len(delist_risk_assets))
            base_where_clauses.append(
                "(p.exchange_risk_status IS NOT NULL AND p.exchange_risk_status <> 'normal' "
                f"OR UPPER(TRIM(p.base_asset)) IN ({placeholders}))"
            )
            base_params.extend(delist_risk_assets)
        else:
            base_where_clauses.append("p.exchange_risk_status IS NOT NULL AND p.exchange_risk_status <> 'normal'")

    where_clauses = list(base_where_clauses)
    params = list(base_params)

    # 方向过滤 → 映射为持仓状态
    if order_side == 'open':
        where_clauses.append("p.status = 'holding'")
    elif order_side == 'close':
        where_clauses.append("p.status = 'closed'")
    elif legacy_status_clause:
        where_clauses.append("p.status = %s")
        params.append(legacy_status_clause)

    where_sql = " AND ".join(where_clauses)
    summary_where_sql = " AND ".join(base_where_clauses)

    if delist_risk_assets:
        risk_placeholders = ','.join(['%s'] * len(delist_risk_assets))
        summary_risk_expr = (
            "p.exchange_risk_status IS NOT NULL AND p.exchange_risk_status <> 'normal' "
            f"OR UPPER(TRIM(p.base_asset)) IN ({risk_placeholders})"
        )
        # SELECT 中的 risk_expr 占位符会先于 WHERE 被绑定。
        summary_params = list(delist_risk_assets) + list(base_params)
    else:
        summary_risk_expr = "p.exchange_risk_status IS NOT NULL AND p.exchange_risk_status <> 'normal'"
        summary_params = list(base_params)

    summary_sql = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN p.status = 'holding' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN p.status = 'closed' THEN 1 ELSE 0 END) AS close_count,
            SUM(CASE WHEN {summary_risk_expr} THEN 1 ELSE 0 END) AS exchange_risk_count
        FROM mi_trade_position p
        WHERE {summary_where_sql}
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(summary_sql, summary_params)
        summary_row = cursor.fetchone() or {}
        summary = {
            'total': int(summary_row.get('total') or 0),
            'open': int(summary_row.get('open_count') or 0),
            'close': int(summary_row.get('close_count') or 0),
            'exchange_risk': int(summary_row.get('exchange_risk_count') or 0),
        }

    # ─── 统计持仓总数 ───
    count_sql = f"SELECT COUNT(*) AS total FROM mi_trade_position p WHERE {where_sql}"
    with db_manager.get_cursor() as cursor:
        cursor.execute(count_sql, params)
        total_row = cursor.fetchone()
        total = total_row['total'] if total_row else 0

    # ─── 分页查询持仓 ───
    offset = (page - 1) * page_size
    query_params = list(params) + [page_size, offset]
    sql = f"""
        SELECT p.*,
               COALESCE(b.market_profile, 'normal') AS market_profile,
               t.open_basis_p20 AS open_vwap_threshold_bps,
               t.{close_threshold_col} AS close_vwap_threshold_bps,
               (SELECT o.channel FROM mi_trade_order o WHERE o.position_id = p.id LIMIT 1) AS channel,
               (SELECT COUNT(*) FROM mi_trade_order o WHERE o.position_id = p.id) AS order_count
        FROM mi_trade_position p
        LEFT JOIN mi_base_asset b
          ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(p.base_asset))
        LEFT JOIN (
            SELECT v.*
            FROM mi_vwap_basis_threshold v
            INNER JOIN (
                SELECT base_asset, MAX(calc_date) AS calc_date
                FROM mi_vwap_basis_threshold
                GROUP BY base_asset
            ) latest
                ON latest.base_asset = v.base_asset
               AND latest.calc_date = v.calc_date
        ) t ON t.base_asset = p.base_asset
        WHERE {where_sql}
        ORDER BY p.id DESC
        LIMIT %s OFFSET %s
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, query_params)
        rows = cursor.fetchall()
    _attach_delist_risks(rows)

    return {
        'orders': _serialize_rows(rows),
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if total else 0,
        },
        'summary': summary,
    }


@router.get('/positions/{position_id}/orders')
async def get_position_orders(position_id: int):
    """获取指定持仓的全部订单明细（弹窗用）"""
    sql = "SELECT * FROM mi_trade_order WHERE position_id = %s ORDER BY id ASC"
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, [position_id])
        rows = cursor.fetchall()
        cursor.execute(
            """
                SELECT *
                FROM mi_margin_topup_log
                WHERE position_id = %s
                ORDER BY id ASC
            """,
            [position_id],
        )
        topup_rows = cursor.fetchall()
    return {'orders': _serialize_rows(rows), 'topup_logs': _serialize_rows(topup_rows)}


@router.get('/orders/grouped')
async def get_orders_grouped():
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
    sql = """
        SELECT * FROM mi_trade_order 
        WHERE position_id IS NOT NULL
        ORDER BY position_id, order_side, market_type, created_at DESC
        LIMIT 2000
    """
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    
    # 按 position_id 分组
    groups = {}
    for row in rows:
        pid = row['position_id']
        if pid not in groups:
            groups[pid] = {
                'position_id': pid,
                'base_asset': row['base_asset'],
                'orders': [],
                'summary': {}
            }
        groups[pid]['orders'].append(_serialize_row(row))
    
    # 计算汇总信息
    result = []
    for pid, group in groups.items():
        orders = group['orders']
        total_exec_amount = sum(float(o['exec_amount'] or 0) for o in orders)
        total_target_amount = sum(float(o['target_amount'] or 0) for o in orders)
        
        group['summary'] = {
            'total_exec_amount': total_exec_amount,
            'total_target_amount': total_target_amount,
            'order_count': len(orders),
        }
        result.append(group)
    
    return result


@router.get('/positions')
async def get_positions(
    status: Optional[str] = Query(None, description="持仓状态过滤"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(90, ge=1, le=365, description="最近N天（开仓时间）"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
):
    """查询持仓列表（含资金费结算历史，支持分页）"""
    try:
        summary_sql = """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'holding' THEN 1 ELSE 0 END) as holding_count,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_count
            FROM mi_trade_position
            WHERE opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """
        summary_params = [days]
        if base_asset:
            summary_sql += " AND base_asset = %s"
            summary_params.append(base_asset)

        with db_manager.get_cursor() as cursor:
            cursor.execute(summary_sql, summary_params)
            summary_row = cursor.fetchone() or {}
            summary = {
                'total': int(summary_row.get('total') or 0),
                'holding': int(summary_row.get('holding_count') or 0),
                'closed': int(summary_row.get('closed_count') or 0),
            }

        # 查询总数
        count_sql = "SELECT COUNT(*) as total FROM mi_trade_position WHERE opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        count_params = [days]
        
        if status:
            count_sql += " AND status = %s"
            count_params.append(status)
        if base_asset:
            count_sql += " AND base_asset = %s"
            count_params.append(base_asset)
        
        with db_manager.get_cursor() as cursor:
            cursor.execute(count_sql, count_params)
            total_row = cursor.fetchone()
            total = total_row['total'] if total_row else 0
        
        # 查询分页数据
        offset = (page - 1) * page_size
        sql = """
            SELECT p.*, COALESCE(b.market_profile, 'normal') AS market_profile
            FROM mi_trade_position p
            LEFT JOIN mi_base_asset b
              ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(p.base_asset))
            WHERE p.opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """
        params = [days]
        
        if status:
            sql += " AND p.status = %s"
            params.append(status)
        if base_asset:
            sql += " AND p.base_asset = %s"
            params.append(base_asset)
        
        sql += " ORDER BY p.opened_at DESC LIMIT %s OFFSET %s"
        params.extend([page_size, offset])
        
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        
        if not rows:
            return {
                'positions': [],
                'pagination': {
                    'page': page,
                    'page_size': page_size,
                    'total': 0,
                    'total_pages': 0,
                },
                'summary': summary,
            }
        
        # 获取资金费结算历史（仅查询当前返回的持仓 ID）
        position_ids = [r['id'] for r in rows]
        placeholders = ','.join(['%s'] * len(position_ids))
        history_sql = f"""
            SELECT position_id, payment_seq, funding_rate, funding_rate_24h,
                   funding_pnl, future_notional, settled_at
            FROM mi_trade_funding_fee_history
            WHERE position_id IN ({placeholders})
            ORDER BY position_id, payment_seq
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(history_sql, position_ids)
            history_rows = cursor.fetchall()
        
        # 按 position_id 分组
        histories: dict = {}
        for h in history_rows:
            pid = h['position_id']
            if pid not in histories:
                histories[pid] = []
            histories[pid].append({
                'seq': h['payment_seq'],
                'rate': float(h['funding_rate']) if h.get('funding_rate') is not None else None,
                'rate_24h': float(h['funding_rate_24h']) if h.get('funding_rate_24h') else None,
                'pnl': float(h['funding_pnl']) if h.get('funding_pnl') is not None else 0,
                'notional': float(h['future_notional']) if h.get('future_notional') else None,
                'time': h['settled_at'].strftime('%m-%d %H:%M') if h.get('settled_at') else None,
            })
        
        # 注入到每个持仓记录
        attach_position_order_fee_summary(rows)
        if any(row.get('status') == 'holding' for row in rows):
            try:
                gate_positions = build_default_reconciler().executor.fetch_gate_futures_positions()
                attach_gate_position_risk(rows, gate_positions)
            except Exception as e:
                logger.warning(f'Gate维持保证金率拉取失败: {e}')
        contract_meta = fetch_contract_meta()
        calculate_realtime_pnl(rows, {}, contract_meta, _position_pnl_config())
        _attach_delist_risks(rows)
        serialized = _serialize_rows(rows)
        _inject_current_funding_fields(serialized, contract_meta)
        for row in serialized:
            row['funding_history'] = histories.get(row.get('id'), [])
        
        return {
            'positions': serialized,
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size,
            },
            'summary': summary,
            # 标准开仓金额，前端用于兑底计算 funding_pnl_bps、避免硬编码与后端配置漂移
            'open_amount_usdt': config.get_float('trade.open.amount_usdt', 10.0),
        }
    except Exception as e:
        logger.error(f'查询持仓失败: {e}', exc_info=True)
        raise


@router.get('/positions/summary')
async def get_positions_summary():
    """持仓汇总统计"""
    sql = """
        SELECT 
            COUNT(*) as total_positions,
            SUM(CASE WHEN status = 'holding' THEN 1 ELSE 0 END) as holding_count,
            SUM(CASE WHEN status = 'holding' THEN spot_open_amount ELSE 0 END) as total_holding_amount,
            SUM(funding_total_pnl) as total_funding_pnl,
            SUM(total_pnl) as total_pnl
        FROM mi_trade_position
    """
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
        return _serialize_row(row) if row else {}


# ─── 基础对账 ────────────────────────────────────────────────────────────────

_recon_running = False
_recon_lock = threading.Lock()
_capital_running = False
_capital_lock = threading.Lock()


class BinanceBnbBuyRequest(BaseModel):
    amount_usdt: float


_CAPITAL_HISTORY_INTERVALS = {
    '1m': 60,
    '10m': 600,
    '1h': 3600,
}


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
          ON UPPER(TRIM(c.base_asset)) = UPPER(TRIM(s.base_asset))
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
        _append_unique_notification(items, seen_notification_keys, {
            'dedup_key': dedup_key,
            'source': 'reconciliation',
            'severity': 'warning',
            'title': f"持仓对账不一致: {base_asset}",
            'message': (
                f"{exchange} {dimension} "
                f"local={row.get('local_value') if row.get('local_value') is not None else '-'} "
                f"exchange={row.get('exchange_value') if row.get('exchange_value') is not None else '-'} "
                f"diff={row.get('diff_value') if row.get('diff_value') is not None else '-'}"
            ),
            'event_at': row.get('snapshot_at'),
            'base_asset': base_asset,
            'risk_type': dimension,
            'status': 'mismatch',
            'detail': row,
        })

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
        result = await asyncio.to_thread(lambda: build_default_reconciler().run_once())
        return {'success': True, 'message': '对账完成', **result}
    except Exception as e:
        logger.error(f'手动对账失败: {e}', exc_info=True)
        return {'success': False, 'message': f'对账失败: {e}'}
    finally:
        with _recon_lock:
            _recon_running = False


# ─── 真实资金快照 ────────────────────────────────────────────────────────────

@router.get('/capital/latest')
async def get_capital_latest():
    """返回最新资金快照汇总。"""
    sql = """
        SELECT
            id,
            snapshot_at,
            exchange,
            equity_usdt,
            available_usdt,
            locked_usdt,
            position_value_usdt,
            margin_used_usdt,
            unrealized_pnl_usdt,
            realized_pnl_usdt,
            funding_pnl_usdt,
            fee_cost_usdt,
            total_pnl_usdt,
            COALESCE(total_pnl_usdt, 0) + COALESCE(unrealized_pnl_usdt, 0) AS gross_total_pnl_usdt,
            CAST(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.bnb_fee_asset.free')) AS DECIMAL(28,12)) AS bnb_available,
            CAST(JSON_UNQUOTE(JSON_EXTRACT(detail, '$.bnb_fee_asset.free_value_usdt')) AS DECIMAL(28,12)) AS bnb_available_usdt
        FROM mi_capital_snapshot
        WHERE JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'
          AND snapshot_at = (
              SELECT MAX(snapshot_at)
              FROM mi_capital_snapshot
              WHERE JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'
          )
        ORDER BY FIELD(exchange, 'binance', 'gate', 'total')
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    return {'rows': _serialize_rows(rows)}


@router.get('/capital/history')
async def get_capital_history(
    days: int = Query(7, ge=1, le=90, description="最近N天"),
    hours: Optional[int] = Query(None, ge=1, le=24, description="最近N小时，优先于days"),
    exchange: Optional[str] = Query(None, description="交易所过滤(binance/gate/total)"),
    interval: str = Query('10m', description="采样间隔(1m/10m/1h)"),
):
    """返回资金历史曲线数据。"""
    bucket_sec = _CAPITAL_HISTORY_INTERVALS.get(interval, _CAPITAL_HISTORY_INTERVALS['10m'])
    if hours is not None:
        window_clause = "snapshot_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)"
        window_value = hours
    else:
        window_clause = "snapshot_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        window_value = days
    where = [
        window_clause,
        "JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'",
    ]
    params: List[Any] = [window_value]
    if exchange in ('binance', 'gate', 'total'):
        where.append("exchange = %s")
        params.append(exchange)
    where_sql = " AND ".join(where)

    sql = f"""
        SELECT
            s.id,
            s.snapshot_at,
            s.exchange,
            s.equity_usdt,
            s.available_usdt,
            s.locked_usdt,
            s.position_value_usdt,
            s.margin_used_usdt,
            s.unrealized_pnl_usdt,
            s.realized_pnl_usdt,
            s.funding_pnl_usdt,
            s.fee_cost_usdt,
            s.total_pnl_usdt,
            COALESCE(s.total_pnl_usdt, 0) + COALESCE(s.unrealized_pnl_usdt, 0) AS gross_total_pnl_usdt,
            CAST(JSON_UNQUOTE(JSON_EXTRACT(s.detail, '$.bnb_fee_asset.free')) AS DECIMAL(28,12)) AS bnb_available,
            CAST(JSON_UNQUOTE(JSON_EXTRACT(s.detail, '$.bnb_fee_asset.free_value_usdt')) AS DECIMAL(28,12)) AS bnb_available_usdt
        FROM mi_capital_snapshot s
        INNER JOIN (
            SELECT
                exchange,
                FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(snapshot_at) / %s) * %s) AS bucket_at,
                MAX(snapshot_at) AS snapshot_at
            FROM mi_capital_snapshot
            WHERE {where_sql}
            GROUP BY exchange, bucket_at
        ) latest
          ON latest.exchange = s.exchange
         AND latest.snapshot_at = s.snapshot_at
        WHERE JSON_UNQUOTE(JSON_EXTRACT(s.detail, '$.source')) = 'exchange_api'
        ORDER BY s.snapshot_at ASC, FIELD(s.exchange, 'binance', 'gate', 'total')
        LIMIT 10000
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, [bucket_sec, bucket_sec, *params])
        rows = cursor.fetchall()
    return {
        'rows': _serialize_rows(rows),
        'interval': interval if interval in _CAPITAL_HISTORY_INTERVALS else '10m',
        'window': {'hours': hours} if hours is not None else {'days': days},
    }


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
        result = await asyncio.to_thread(lambda: build_default_capital_snapshotter().run_once())
        return {'success': True, 'message': '资金采集完成', **result}
    except Exception as e:
        logger.error(f'手动资金采集失败: {e}', exc_info=True)
        return {'success': False, 'message': f'资金采集失败: {e}'}
    finally:
        with _capital_lock:
            _capital_running = False


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
    actionable_only: bool = Query(False, description="仅展示可提醒候选"),
    limit: int = Query(200, ge=1, le=1000),
):
    """查询交易对上新事件。"""
    rows = list_listing_events(
        action_status=action_status,
        candidate_status=candidate_status,
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
):
    """查询历史交易信号（支持分页）"""
    where_sql, where_params = _build_forward_signal_filters(
        status=status,
        exit_reason=exit_reason,
        base_asset=base_asset,
        time_range=time_range,
        days=days,
    )
    aliased_where_sql, aliased_where_params = _build_forward_signal_filters(
        status=status,
        exit_reason=exit_reason,
        base_asset=base_asset,
        time_range=time_range,
        days=days,
        prefix="s.",
    )

    # 查询总数
    count_sql = f"SELECT COUNT(*) as total FROM mi_trade_signal WHERE {where_sql}"

    with db_manager.get_cursor() as cursor:
        cursor.execute(count_sql, where_params)
        total_row = cursor.fetchone()
        total = total_row['total'] if total_row else 0

    # 查询分页数据
    offset = (page - 1) * page_size
    sql = f"""
        SELECT
            s.*,
            COALESCE(b.strategy_tier, 'C') AS strategy_tier,
            COALESCE(b.market_profile, 'normal') AS market_profile
        FROM mi_trade_signal s
        LEFT JOIN mi_base_asset b
          ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(s.base_asset))
        WHERE {aliased_where_sql}
    """
    params = list(aliased_where_params)
    sql += " ORDER BY s.signal_time DESC LIMIT %s OFFSET %s"
    params.extend([page_size, offset])

    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    data = _serialize_rows(rows)

    # 计算汇总统计（基于全量数据，需要单独查询）
    summary_sql = f"""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN status = 'opened' THEN 1 ELSE 0 END) as opened,
            SUM(CASE WHEN status IN ('rejected', 'gate_rejected') THEN 1 ELSE 0 END) as rejected,
            SUM(CASE WHEN status = 'conditions_lost' THEN 1 ELSE 0 END) as conditions_lost,
            SUM(CASE WHEN status = 'monitoring' THEN 1 ELSE 0 END) as monitoring,
            MAX(signal_time) as latest_signal_time
        FROM mi_trade_signal 
        WHERE {where_sql}
    """

    with db_manager.get_cursor() as cursor:
        cursor.execute(summary_sql, where_params)
        summary_row = cursor.fetchone()

    summary_data = _serialize_row(summary_row) if summary_row else {}
    total_count = summary_data.get('total', 0)
    opened_count = summary_data.get('opened', 0)
    rejected_count = summary_data.get('rejected', 0)
    conditions_lost_count = summary_data.get('conditions_lost', 0)
    monitoring_count = summary_data.get('monitoring', 0)
    latest_signal_time = summary_data.get('latest_signal_time')

    return {
        'signals': data,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total_count,
            'total_pages': (total_count + page_size - 1) // page_size,
        },
        'summary': {
            'total': total_count,
            'opened': opened_count,
            'rejected': rejected_count,
            'conditions_lost': conditions_lost_count,
            'monitoring': monitoring_count,
            'conversion_rate': round(opened_count / total_count * 100, 1) if total_count > 0 else 0,
            'latest_signal_time': latest_signal_time,
        }
    }


@router.get('/reverse-signals')
async def get_reverse_signals(
    status: Optional[str] = Query(None, description="状态过滤: monitoring/opened/conditions_lost/rejected/gate_rejected/monitor_timeout"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(3, ge=1, le=30, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
):
    """查询反向套利交易信号（后端分页）。"""
    where = [
        "signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)",
        "signal_basis_bps IS NOT NULL",
    ]
    aliased_where = [
        "s.signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)",
        "s.signal_basis_bps IS NOT NULL",
    ]
    params: List = [days]
    if status:
        where.append("status = %s")
        aliased_where.append("s.status = %s")
        params.append(status)
    if base_asset:
        where.append("base_asset LIKE %s")
        aliased_where.append("s.base_asset LIKE %s")
        params.append(f"%{base_asset}%")
    where_sql = " AND ".join(where)
    aliased_where_sql = " AND ".join(aliased_where)

    count_sql = f"SELECT COUNT(*) AS total FROM mi_reverse_trade_signal WHERE {where_sql}"
    with db_manager.get_cursor() as cursor:
        cursor.execute(count_sql, params)
        total_row = cursor.fetchone()
        total = int(total_row['total'] if total_row else 0)

    offset = (page - 1) * page_size
    sql = f"""
        SELECT
            s.*,
            COALESCE(b.strategy_tier, 'C') AS strategy_tier,
            COALESCE(b.market_profile, 'normal') AS market_profile
        FROM mi_reverse_trade_signal s
        LEFT JOIN mi_base_asset b
          ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(s.base_asset))
        WHERE {aliased_where_sql}
        ORDER BY s.signal_time DESC
        LIMIT %s OFFSET %s
    """
    page_params = list(params)
    page_params.extend([page_size, offset])
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, page_params)
        rows = cursor.fetchall()

    summary_sql = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'monitoring' THEN 1 ELSE 0 END) AS monitoring,
            SUM(CASE WHEN status = 'opened' THEN 1 ELSE 0 END) AS opened,
            SUM(CASE WHEN status = 'conditions_lost' THEN 1 ELSE 0 END) AS conditions_lost,
            SUM(CASE WHEN status IN ('rejected', 'gate_rejected') THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN status = 'monitor_timeout' THEN 1 ELSE 0 END) AS monitor_timeout,
            MAX(signal_time) AS latest_signal_time
        FROM mi_reverse_trade_signal
        WHERE {where_sql}
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(summary_sql, params)
        summary_row = cursor.fetchone()

    summary_data = _serialize_row(summary_row) if summary_row else {}
    total_count = int(summary_data.get('total') or 0)
    opened_count = int(summary_data.get('opened') or 0)
    signal_rows = _serialize_rows(rows)
    for row in signal_rows:
        row.pop('funding_rate_2h', None)

    return {
        'signals': signal_rows,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size,
        },
        'summary': {
            'total': total_count,
            'monitoring': int(summary_data.get('monitoring') or 0),
            'opened': opened_count,
            'conditions_lost': int(summary_data.get('conditions_lost') or 0),
            'rejected': int(summary_data.get('rejected') or 0),
            'monitor_timeout': int(summary_data.get('monitor_timeout') or 0),
            'conversion_rate': round(opened_count / total_count * 100, 1) if total_count > 0 else 0,
            'latest_signal_time': summary_data.get('latest_signal_time'),
        },
    }


@router.get('/reverse-positions')
async def get_reverse_positions(
    status: Optional[str] = Query(None, description="状态过滤: holding/closing/closed/risk/desynced"),
    order_side: Optional[str] = Query(None, description="方向过滤(open=持仓中/close=已平仓)"),
    exchange_risk: bool = Query(False, description="仅展示交易所风险持仓"),
    base_asset: Optional[str] = Query(None, description="标的资产过滤"),
    days: int = Query(30, ge=1, le=365, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
):
    """查询反向套利持仓（独立于正向 mi_trade_position）。"""
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
    return {
        'positions': _serialize_rows(result.rows),
        'pagination': {
            'page': result.page,
            'page_size': result.page_size,
            'total': result.total,
            'total_pages': result.total_pages,
        },
        'summary': summary,
    }


@router.get('/reverse-positions/{position_id}/orders')
async def get_reverse_position_orders(position_id: int):
    """获取指定反向持仓的全部订单明细（弹窗用）。"""
    rows = list_reverse_position_orders(position_id)
    return {'orders': _serialize_rows(rows), 'topup_logs': []}


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
):
    """查询反向套利订单（独立于正向 mi_trade_order）。"""
    result = list_reverse_orders(
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
    return {
        'orders': _serialize_rows(result.rows),
        'pagination': {
            'page': result.page,
            'page_size': result.page_size,
            'total': result.total,
            'total_pages': result.total_pages,
        },
    }


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
