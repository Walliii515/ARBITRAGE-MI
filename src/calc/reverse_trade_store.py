# coding: utf-8
"""反向套利持仓/订单只读数据边界。

本模块只管理反向策略自己的表，不读写正向 mi_trade_position /
mi_trade_order，避免两套策略的持仓语义互相污染。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from calc.orderbook_enricher import calc_vwap_basis_bps
from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


REVERSE_POSITION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_reverse_trade_position (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_uuid VARCHAR(64) NOT NULL,
    signal_id BIGINT DEFAULT NULL,
    base_asset VARCHAR(32) NOT NULL,
    spot_symbol VARCHAR(64) NOT NULL,
    future_contract VARCHAR(64) NOT NULL,
    status ENUM('holding','closing','closed','risk','desynced') NOT NULL DEFAULT 'holding',
    opened_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME DEFAULT NULL,
    close_reason TEXT DEFAULT NULL,
    open_amount_usdt DECIMAL(24,8) DEFAULT NULL,
    close_amount_usdt DECIMAL(24,8) DEFAULT NULL,
    borrow_asset VARCHAR(32) DEFAULT NULL,
    borrow_qty DECIMAL(30,12) DEFAULT NULL,
    borrow_repaid_qty DECIMAL(30,12) DEFAULT NULL,
    borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
    borrow_interest_usdt DECIMAL(24,8) NOT NULL DEFAULT 0,
    borrow_interest_bps DECIMAL(12,4) NOT NULL DEFAULT 0,
    spot_open_qty DECIMAL(30,12) DEFAULT NULL,
    spot_open_price DECIMAL(24,12) DEFAULT NULL,
    spot_open_amount DECIMAL(24,8) DEFAULT NULL,
    spot_close_qty DECIMAL(30,12) DEFAULT NULL,
    spot_close_price DECIMAL(24,12) DEFAULT NULL,
    spot_close_amount DECIMAL(24,8) DEFAULT NULL,
    future_open_qty DECIMAL(30,12) DEFAULT NULL,
    future_open_price DECIMAL(24,12) DEFAULT NULL,
    future_open_amount DECIMAL(24,8) DEFAULT NULL,
    future_close_qty DECIMAL(30,12) DEFAULT NULL,
    future_close_price DECIMAL(24,12) DEFAULT NULL,
    future_close_amount DECIMAL(24,8) DEFAULT NULL,
    reverse_open_basis_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_close_basis_bps DECIMAL(12,4) DEFAULT NULL,
    signal_basis_bps DECIMAL(12,4) DEFAULT NULL,
    pre_gate_basis_bps DECIMAL(12,4) DEFAULT NULL,
    actual_basis_bps DECIMAL(12,4) DEFAULT NULL,
    execution_drift_bps DECIMAL(12,4) DEFAULT NULL,
    funding_pnl_usdt DECIMAL(24,8) NOT NULL DEFAULT 0,
    funding_pnl_bps DECIMAL(12,4) NOT NULL DEFAULT 0,
    fee_total_usdt DECIMAL(24,8) NOT NULL DEFAULT 0,
    fee_total_bps DECIMAL(12,4) NOT NULL DEFAULT 0,
    realized_pnl_usdt DECIMAL(24,8) DEFAULT NULL,
    realized_pnl_bps DECIMAL(12,4) DEFAULT NULL,
    exchange_risk_status ENUM('normal','desynced','resolved') NOT NULL DEFAULT 'normal',
    exchange_risk_type VARCHAR(64) DEFAULT NULL,
    exchange_risk_at DATETIME DEFAULT NULL,
    exchange_risk_detail TEXT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_reverse_position_order_uuid (order_uuid),
    INDEX idx_reverse_position_status_time (status, opened_at),
    INDEX idx_reverse_position_asset_status (base_asset, status),
    INDEX idx_reverse_position_signal (signal_id),
    INDEX idx_reverse_position_risk (exchange_risk_status, exchange_risk_type, exchange_risk_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='反向套利持仓'
"""


REVERSE_ORDER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_reverse_trade_order (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_uuid VARCHAR(64) NOT NULL,
    position_id BIGINT DEFAULT NULL,
    signal_id BIGINT DEFAULT NULL,
    base_asset VARCHAR(32) NOT NULL,
    spot_symbol VARCHAR(64) DEFAULT NULL,
    future_contract VARCHAR(64) DEFAULT NULL,
    order_side ENUM('open','close','repay','unwind') NOT NULL,
    market_type ENUM('margin_spot','future','margin_repay') NOT NULL,
    trade_direction ENUM('buy','sell','borrow','repay') NOT NULL,
    status ENUM('pending','filled','partial','failed','cancelled','skipped') NOT NULL DEFAULT 'pending',
    target_qty DECIMAL(30,12) DEFAULT NULL,
    target_amount DECIMAL(24,8) DEFAULT NULL,
    exec_price DECIMAL(24,12) DEFAULT NULL,
    exec_qty DECIMAL(30,12) DEFAULT NULL,
    exec_amount DECIMAL(24,8) DEFAULT NULL,
    exchange_order_id VARCHAR(128) DEFAULT NULL,
    client_order_id VARCHAR(128) DEFAULT NULL,
    liquidity_role VARCHAR(16) DEFAULT NULL,
    fee_rate DECIMAL(18,10) DEFAULT NULL,
    fee_amount DECIMAL(30,12) DEFAULT NULL,
    fee_asset VARCHAR(32) DEFAULT NULL,
    fee_amount_usdt DECIMAL(24,8) DEFAULT NULL,
    reduce_only TINYINT(1) DEFAULT NULL,
    protective_price DECIMAL(24,12) DEFAULT NULL,
    execution_style VARCHAR(32) DEFAULT NULL,
    reject_reason TEXT DEFAULT NULL,
    raw_response JSON DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_reverse_order_uuid (order_uuid),
    INDEX idx_reverse_order_position (position_id),
    INDEX idx_reverse_order_signal (signal_id),
    INDEX idx_reverse_order_asset_time (base_asset, created_at),
    INDEX idx_reverse_order_status_time (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='反向套利订单'
"""


ALLOWED_POSITION_STATUSES = {'holding', 'closing', 'closed', 'risk', 'desynced'}
ALLOWED_ORDER_SIDES = {'open', 'close', 'repay', 'unwind'}
ALLOWED_ORDER_STATUSES = {'pending', 'filled', 'partial', 'failed', 'cancelled', 'skipped'}
ALLOWED_ORDER_MARKETS = {'margin_spot', 'future', 'margin_repay'}

_tables_ready = False


@dataclass(frozen=True)
class PageResult:
    rows: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        return (self.total + self.page_size - 1) // self.page_size


def ensure_reverse_trade_tables() -> None:
    """Create reverse trade tables if missing.

    Runtime creation mirrors the existing reverse signal table behavior and keeps
    first deployment safe even when migrations are applied manually later.
    """
    global _tables_ready
    if _tables_ready:
        return
    with db_manager.get_cursor() as cursor:
        cursor.execute(REVERSE_POSITION_TABLE_SQL)
        cursor.execute(REVERSE_ORDER_TABLE_SQL)
    _tables_ready = True


def _page_bounds(page: int, page_size: int) -> Tuple[int, int]:
    page = max(int(page or 1), 1)
    page_size = max(min(int(page_size or 100), 5000), 1)
    return page, page_size


def _append_like_filter(where: List[str], params: List[Any], column: str, value: Optional[str]) -> None:
    if value:
        where.append(f"{column} LIKE %s")
        params.append(f"%{value.strip().upper()}%")


def _append_exact_filter(
    where: List[str],
    params: List[Any],
    column: str,
    value: Optional[str],
    allowed: set[str],
) -> None:
    if value:
        normalized = value.strip().lower()
        if normalized in allowed:
            where.append(f"{column} = %s")
            params.append(normalized)


def list_reverse_positions(
    *,
    status: Optional[str] = None,
    order_side: Optional[str] = None,
    exchange_risk: bool = False,
    base_asset: Optional[str] = None,
    days: int = 30,
    page: int = 1,
    page_size: int = 100,
) -> PageResult:
    """Return reverse positions with backend pagination."""
    ensure_reverse_trade_tables()
    page, page_size = _page_bounds(page, page_size)
    days = max(min(int(days or 30), 365), 1)
    where = ["opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
    params: List[Any] = [days]
    _append_exact_filter(where, params, 'status', status, ALLOWED_POSITION_STATUSES)
    if order_side == 'open':
        where.append("status IN ('holding','closing','risk','desynced')")
    elif order_side == 'close':
        where.append("status = 'closed'")
    if exchange_risk:
        where.append("exchange_risk_status IS NOT NULL AND exchange_risk_status <> 'normal'")
    _append_like_filter(where, params, 'UPPER(base_asset)', base_asset)
    where_sql = " AND ".join(where)

    with db_manager.get_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS total FROM mi_reverse_trade_position WHERE {where_sql}", params)
        total_row = cursor.fetchone() or {}
        total = int(total_row.get('total') or 0)

        query_params = list(params)
        query_params.extend([page_size, (page - 1) * page_size])
        cursor.execute(
            f"""
            SELECT p.*,
                   (SELECT COUNT(*) FROM mi_reverse_trade_order o WHERE o.position_id = p.id) AS order_count
            FROM mi_reverse_trade_position p
            WHERE {where_sql}
            ORDER BY p.opened_at DESC, p.id DESC
            LIMIT %s OFFSET %s
            """,
            query_params,
        )
        rows = cursor.fetchall()
    return PageResult(rows=list(rows or []), total=total, page=page, page_size=page_size)


def summarize_reverse_positions(
    *,
    exchange_risk: bool = False,
    base_asset: Optional[str] = None,
    days: int = 30,
) -> Dict[str, int]:
    """Return reverse position summary for the current page filters.

    Keep this reverse-owned: it only reads mi_reverse_trade_position.
    """
    ensure_reverse_trade_tables()
    days = max(min(int(days or 30), 365), 1)
    where = ["opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
    params: List[Any] = [days]
    if exchange_risk:
        where.append("exchange_risk_status IS NOT NULL AND exchange_risk_status <> 'normal'")
    _append_like_filter(where, params, 'UPPER(base_asset)', base_asset)
    where_sql = " AND ".join(where)

    with db_manager.get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status IN ('holding','closing','risk','desynced') THEN 1 ELSE 0 END) AS open_count,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS close_count,
                SUM(CASE WHEN exchange_risk_status IS NOT NULL AND exchange_risk_status <> 'normal' THEN 1 ELSE 0 END) AS exchange_risk_count
            FROM mi_reverse_trade_position
            WHERE {where_sql}
            """,
            params,
        )
        row = cursor.fetchone() or {}
    return {
        'total': int(row.get('total') or 0),
        'open': int(row.get('open_count') or 0),
        'close': int(row.get('close_count') or 0),
        'exchange_risk': int(row.get('exchange_risk_count') or 0),
    }


def list_reverse_position_orders(position_id: int) -> List[Dict[str, Any]]:
    """Return all reverse order legs for a reverse position."""
    ensure_reverse_trade_tables()
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM mi_reverse_trade_order
            WHERE position_id = %s
            ORDER BY id ASC
            """,
            [int(position_id)],
        )
        rows = cursor.fetchall()
    return list(rows or [])


def list_reverse_orders(
    *,
    position_id: Optional[int] = None,
    order_uuid: Optional[str] = None,
    order_side: Optional[str] = None,
    status: Optional[str] = None,
    market_type: Optional[str] = None,
    base_asset: Optional[str] = None,
    days: int = 30,
    page: int = 1,
    page_size: int = 100,
) -> PageResult:
    """Return reverse orders with backend pagination."""
    ensure_reverse_trade_tables()
    page, page_size = _page_bounds(page, page_size)
    days = max(min(int(days or 30), 365), 1)
    where = ["created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"]
    params: List[Any] = [days]
    if position_id is not None:
        where.append("position_id = %s")
        params.append(int(position_id))
    if order_uuid:
        where.append("order_uuid = %s")
        params.append(order_uuid.strip())
    _append_exact_filter(where, params, 'order_side', order_side, ALLOWED_ORDER_SIDES)
    _append_exact_filter(where, params, 'status', status, ALLOWED_ORDER_STATUSES)
    _append_exact_filter(where, params, 'market_type', market_type, ALLOWED_ORDER_MARKETS)
    _append_like_filter(where, params, 'UPPER(base_asset)', base_asset)
    where_sql = " AND ".join(where)

    with db_manager.get_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS total FROM mi_reverse_trade_order WHERE {where_sql}", params)
        total_row = cursor.fetchone() or {}
        total = int(total_row.get('total') or 0)

        query_params = list(params)
        query_params.extend([page_size, (page - 1) * page_size])
        cursor.execute(
            f"""
            SELECT *
            FROM mi_reverse_trade_order
            WHERE {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            query_params,
        )
        rows = cursor.fetchall()
    return PageResult(rows=list(rows or []), total=total, page=page, page_size=page_size)


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def record_reverse_open_execution(
    *,
    signal_id: Optional[int],
    order_group: Dict[str, Any],
    orderbook_row: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist reverse open execution result into reverse-owned tables."""
    ensure_reverse_trade_tables()
    spot_order = dict(order_group.get('spot_order') or {})
    future_order = dict(order_group.get('future_order') or {})
    base_asset = str(spot_order.get('base_asset') or future_order.get('base_asset') or '').upper()
    order_uuid = str(order_group.get('order_uuid') or '')
    spot_result = dict(result.get('spot_order') or {})
    future_result = dict(result.get('future_order') or {})
    borrow_result = dict(result.get('borrow_order') or {})
    repay_result = dict(result.get('repay_order') or {})
    unwind_result = dict(result.get('unwind_order') or {})

    spot_success = bool(spot_result.get('success'))
    future_success = bool(future_result.get('success'))
    success = bool(result.get('success')) and spot_success and future_success
    borrow_left_open = bool(borrow_result.get('success')) and not bool(repay_result.get('success'))
    has_real_leg = spot_success or future_success or borrow_left_open
    position_id = None

    actual_basis = calc_vwap_basis_bps(
        spot_result.get('exec_price'),
        future_result.get('exec_price'),
    )
    pre_gate_basis = order_group.get('pre_gate_basis_bps')
    execution_drift = (
        actual_basis - _as_float(pre_gate_basis)
        if actual_basis is not None and pre_gate_basis is not None
        else None
    )
    fee_total_usdt = sum(
        _as_float(x.get('fee_amount_usdt'))
        for x in (spot_result, future_result)
        if x.get('fee_amount_usdt') is not None
    )
    open_amount = spot_result.get('exec_amount') or future_result.get('exec_amount') or order_group.get('target_amount')

    with db_manager.get_cursor() as cursor:
        if has_real_leg:
            position_status = 'holding' if success else 'desynced'
            cursor.execute(
                """
                INSERT INTO mi_reverse_trade_position (
                    order_uuid, signal_id, base_asset, spot_symbol, future_contract, status,
                    open_amount_usdt, borrow_asset, borrow_qty, borrow_hourly_rate,
                    spot_open_qty, spot_open_price, spot_open_amount,
                    future_open_qty, future_open_price, future_open_amount,
                    reverse_open_basis_bps, signal_basis_bps, pre_gate_basis_bps,
                    actual_basis_bps, execution_drift_bps, fee_total_usdt,
                    exchange_risk_status, exchange_risk_type, exchange_risk_detail
                ) VALUES (
                    %(order_uuid)s, %(signal_id)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s, %(status)s,
                    %(open_amount)s, %(borrow_asset)s, %(borrow_qty)s, %(borrow_hourly_rate)s,
                    %(spot_qty)s, %(spot_price)s, %(spot_amount)s,
                    %(future_qty)s, %(future_price)s, %(future_amount)s,
                    %(reverse_open_basis)s, %(signal_basis)s, %(pre_gate_basis)s,
                    %(actual_basis)s, %(execution_drift)s, %(fee_total_usdt)s,
                    %(risk_status)s, %(risk_type)s, %(risk_detail)s
                )
                """,
                {
                    'order_uuid': order_uuid,
                    'signal_id': signal_id,
                    'base_asset': base_asset,
                    'spot_symbol': spot_order.get('spot_symbol') or f'{base_asset}USDT',
                    'future_contract': future_order.get('future_contract') or f'{base_asset}_USDT',
                    'status': position_status,
                    'open_amount': open_amount,
                    'borrow_asset': base_asset,
                    'borrow_qty': borrow_result.get('amount'),
                    'borrow_hourly_rate': order_group.get('borrow_hourly_rate'),
                    'spot_qty': spot_result.get('exec_qty'),
                    'spot_price': spot_result.get('exec_price'),
                    'spot_amount': spot_result.get('exec_amount'),
                    'future_qty': future_result.get('exec_qty'),
                    'future_price': future_result.get('exec_price'),
                    'future_amount': future_result.get('exec_amount'),
                    'reverse_open_basis': orderbook_row.get('reverse_basis_bps'),
                    'signal_basis': order_group.get('signal_basis_bps'),
                    'pre_gate_basis': pre_gate_basis,
                    'actual_basis': actual_basis,
                    'execution_drift': execution_drift,
                    'fee_total_usdt': fee_total_usdt,
                    'risk_status': 'normal' if success else 'desynced',
                    'risk_type': None if success else 'reverse_open_partial_or_failed',
                    'risk_detail': None if success else str(result.get('message') or '')[:1000],
                },
            )
            position_id = cursor.lastrowid

        order_rows = [
            ('open', 'margin_spot', 'borrow', borrow_result, {'target_qty': borrow_result.get('amount')}),
            ('open', 'margin_spot', 'sell', spot_result, spot_order),
            ('open', 'future', 'buy', future_result, future_order),
        ]
        if repay_result:
            order_rows.append(('repay', 'margin_repay', 'repay', repay_result, {'target_qty': repay_result.get('amount')}))
        if unwind_result:
            market = 'future' if future_success and not spot_success else 'margin_spot'
            direction = 'sell' if market == 'future' else 'buy'
            order_rows.append(('unwind', market, direction, unwind_result, {}))

        for order_side, market_type, trade_direction, exec_row, request_order in order_rows:
            if not exec_row:
                continue
            status = 'filled' if exec_row.get('success') else 'failed'
            cursor.execute(
                """
                INSERT INTO mi_reverse_trade_order (
                    order_uuid, position_id, signal_id, base_asset, spot_symbol, future_contract,
                    order_side, market_type, trade_direction, status,
                    target_qty, target_amount, exec_price, exec_qty, exec_amount,
                    exchange_order_id, client_order_id, fee_amount, fee_asset, fee_amount_usdt,
                    reduce_only, protective_price, execution_style, reject_reason, raw_response
                ) VALUES (
                    %(order_uuid)s, %(position_id)s, %(signal_id)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                    %(order_side)s, %(market_type)s, %(trade_direction)s, %(status)s,
                    %(target_qty)s, %(target_amount)s, %(exec_price)s, %(exec_qty)s, %(exec_amount)s,
                    %(exchange_order_id)s, %(client_order_id)s, %(fee_amount)s, %(fee_asset)s, %(fee_amount_usdt)s,
                    %(reduce_only)s, %(protective_price)s, %(execution_style)s, %(reject_reason)s, %(raw_response)s
                )
                """,
                {
                    'order_uuid': order_uuid,
                    'position_id': position_id,
                    'signal_id': signal_id,
                    'base_asset': base_asset,
                    'spot_symbol': spot_order.get('spot_symbol') or f'{base_asset}USDT',
                    'future_contract': future_order.get('future_contract') or f'{base_asset}_USDT',
                    'order_side': order_side,
                    'market_type': market_type,
                    'trade_direction': trade_direction,
                    'status': status,
                    'target_qty': request_order.get('target_qty'),
                    'target_amount': request_order.get('target_amount'),
                    'exec_price': exec_row.get('exec_price'),
                    'exec_qty': exec_row.get('exec_qty') or exec_row.get('amount'),
                    'exec_amount': exec_row.get('exec_amount'),
                    'exchange_order_id': exec_row.get('exchange_order_id'),
                    'client_order_id': request_order.get('client_order_id'),
                    'fee_amount': exec_row.get('fee_amount'),
                    'fee_asset': exec_row.get('fee_asset'),
                    'fee_amount_usdt': exec_row.get('fee_amount_usdt'),
                    'reduce_only': 1 if request_order.get('reduce_only') else 0,
                    'protective_price': request_order.get('protective_price'),
                    'execution_style': request_order.get('execution_style'),
                    'reject_reason': exec_row.get('reason') or (None if exec_row.get('success') else result.get('message')),
                    'raw_response': _json_dumps(exec_row.get('raw') or exec_row),
                },
            )

    return {
        'position_id': position_id,
        'actual_basis_bps': actual_basis,
        'execution_drift_bps': execution_drift,
        'fee_total_usdt': fee_total_usdt,
    }
