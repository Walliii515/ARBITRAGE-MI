# coding: utf-8
"""反向套利持仓/订单只读数据边界。

本模块只管理反向策略自己的表，不读写正向 mi_trade_position /
mi_trade_order，避免两套策略的持仓语义互相污染。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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
            SELECT *
            FROM mi_reverse_trade_position
            WHERE {where_sql}
            ORDER BY opened_at DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            query_params,
        )
        rows = cursor.fetchall()
    return PageResult(rows=list(rows or []), total=total, page=page, page_size=page_size)


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
