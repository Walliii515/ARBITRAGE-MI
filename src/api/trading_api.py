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
from typing import Optional, Any, List, Dict
from fastapi import APIRouter, Query

from common.database import db_manager
from common.config import config
from common.logger import get_logger
from common.meta_loader import fetch_contract_meta
from calc.reconciliation import build_default_reconciler, get_ignored_binance_spot_assets
from calc.account_capital import build_default_capital_snapshotter
from calc.gate_position_risk import attach_gate_position_risk
from calc.position_order_fees import attach_position_order_fee_summary
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
        elif key in ('detail', 'binance_cross_margin') and isinstance(value, str):
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

    if exchange_risk:
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

    summary_sql = f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN p.status = 'holding' THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN p.status = 'closed' THEN 1 ELSE 0 END) AS close_count,
            SUM(CASE WHEN p.exchange_risk_status IS NOT NULL AND p.exchange_risk_status <> 'normal' THEN 1 ELSE 0 END) AS exchange_risk_count
        FROM mi_trade_position p
        WHERE {summary_where_sql}
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(summary_sql, base_params)
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


_CAPITAL_HISTORY_INTERVALS = {
    '1m': 60,
    '10m': 600,
    '1h': 3600,
}


def _reconciliation_ignore_clause() -> tuple[str, List[Any]]:
    ignored = sorted(get_ignored_binance_spot_assets())
    if not ignored:
        return '', []
    placeholders = ','.join(['%s'] * len(ignored))
    return f" AND NOT (exchange = 'binance' AND base_asset IN ({placeholders}))", ignored


@router.get('/reconciliation/latest')
async def get_reconciliation_latest():
    """返回最近一轮对账快照。"""
    ignore_sql, ignore_params = _reconciliation_ignore_clause()
    sql = """
        SELECT *
        FROM mi_recon_snapshot
        WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM mi_recon_snapshot)
        {ignore_sql}
        ORDER BY exchange ASC, base_asset ASC
    """.format(ignore_sql=ignore_sql)
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
    exchange: Optional[str] = Query(None, description="交易所过滤(binance/gate/total)"),
    interval: str = Query('10m', description="采样间隔(1m/10m/1h)"),
):
    """返回资金历史曲线数据。"""
    bucket_sec = _CAPITAL_HISTORY_INTERVALS.get(interval, _CAPITAL_HISTORY_INTERVALS['10m'])
    where = [
        "snapshot_at >= DATE_SUB(NOW(), INTERVAL %s DAY)",
        "JSON_UNQUOTE(JSON_EXTRACT(detail, '$.source')) = 'exchange_api'",
    ]
    params: List[Any] = [days]
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
    return {'rows': _serialize_rows(rows), 'interval': interval if interval in _CAPITAL_HISTORY_INTERVALS else '10m'}


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
    days: int = Query(90, ge=1, le=90, description="最近N天"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(100, ge=1, le=5000, description="每页条数"),
):
    """查询历史交易信号（支持分页）"""
    # 查询总数
    count_sql = "SELECT COUNT(*) as total FROM mi_trade_signal WHERE signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)"
    count_params: List = [days]

    if status:
        count_sql += " AND status = %s"
        count_params.append(status)
    if exit_reason:
        count_sql += " AND exit_reason LIKE %s"
        count_params.append(f"%{exit_reason}%")
    if base_asset:
        count_sql += " AND base_asset LIKE %s"
        count_params.append(f"%{base_asset}%")

    with db_manager.get_cursor() as cursor:
        cursor.execute(count_sql, count_params)
        total_row = cursor.fetchone()
        total = total_row['total'] if total_row else 0

    # 查询分页数据
    offset = (page - 1) * page_size
    sql = """
        SELECT
            s.*,
            COALESCE(b.strategy_tier, 'C') AS strategy_tier,
            COALESCE(b.market_profile, 'normal') AS market_profile
        FROM mi_trade_signal s
        LEFT JOIN mi_base_asset b
          ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(s.base_asset))
        WHERE s.signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
    """
    params: List = [days]

    if status:
        sql += " AND s.status = %s"
        params.append(status)
    if exit_reason:
        sql += " AND s.exit_reason LIKE %s"
        params.append(f"%{exit_reason}%")
    if base_asset:
        sql += " AND s.base_asset LIKE %s"
        params.append(f"%{base_asset}%")

    sql += " ORDER BY s.signal_time DESC LIMIT %s OFFSET %s"
    params.extend([page_size, offset])

    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    data = _serialize_rows(rows)

    # 计算汇总统计（基于全量数据，需要单独查询）
    summary_sql = """
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN status = 'opened' THEN 1 ELSE 0 END) as opened,
            SUM(CASE WHEN status IN ('rejected', 'gate_rejected') THEN 1 ELSE 0 END) as rejected,
            SUM(CASE WHEN status = 'conditions_lost' THEN 1 ELSE 0 END) as conditions_lost,
            SUM(CASE WHEN status = 'monitoring' THEN 1 ELSE 0 END) as monitoring,
            MAX(signal_time) as latest_signal_time
        FROM mi_trade_signal 
        WHERE signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
    """
    summary_params: List = [days]

    if status:
        summary_sql += " AND status = %s"
        summary_params.append(status)
    if exit_reason:
        summary_sql += " AND exit_reason LIKE %s"
        summary_params.append(f"%{exit_reason}%")
    if base_asset:
        summary_sql += " AND base_asset LIKE %s"
        summary_params.append(f"%{base_asset}%")

    with db_manager.get_cursor() as cursor:
        cursor.execute(summary_sql, summary_params)
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
