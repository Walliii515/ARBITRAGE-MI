# coding: utf-8
"""Forward trading query repository.

Wraps existing parameterized SQL for orders / positions / signals.
Does not change table schema or result columns.
"""
from __future__ import annotations

from typing import Any, Optional

from common.database import DatabaseManager, db_manager as _default_db_manager

Row = dict[str, Any]

ALLOWED_CLOSE_THRESHOLD_COLS = {
    'close_basis_p1',
    'close_basis_p2',
    'close_basis_p3',
    'close_basis_p5',
    'close_basis_p10',
    'close_basis_p20',
}


def build_signal_time_filter(
    time_range: Optional[str],
    days: int,
    prefix: str = '',
) -> tuple[str, list[Any]]:
    column = f'{prefix}signal_time'
    range_key = (time_range or 'today').strip().lower()
    if range_key in {'today', 'date_today'}:
        return f'{column} >= CURDATE() AND {column} < DATE_ADD(CURDATE(), INTERVAL 1 DAY)', []
    return f'{column} >= DATE_SUB(NOW(), INTERVAL %s DAY)', [days]


def build_forward_signal_filters(
    *,
    status: Optional[str],
    exit_reason: Optional[str],
    base_asset: Optional[str],
    time_range: Optional[str],
    days: int,
    prefix: str = '',
) -> tuple[str, list[Any]]:
    field_prefix = prefix
    conditions: list[str] = []
    params: list[Any] = []
    time_sql, time_params = build_signal_time_filter(time_range, days, field_prefix)
    conditions.append(time_sql)
    params.extend(time_params)

    if status:
        conditions.append(f'{field_prefix}status = %s')
        params.append(status)
    if exit_reason:
        conditions.append(f'{field_prefix}exit_reason LIKE %s')
        params.append(f'%{exit_reason}%')
    if base_asset:
        conditions.append(f'{field_prefix}base_asset LIKE %s')
        params.append(f'%{base_asset}%')

    return ' AND '.join(conditions), params


def build_order_view_where(
    *,
    normalized_view: str,
    days: int,
    base_asset: Optional[str],
    position_id: Optional[int],
    channel: Optional[str],
    delist_risk_assets: Optional[list[str]] = None,
    exchange_risk: bool = False,
) -> tuple[str, list[Any]]:
    status_value = 'holding' if normalized_view == 'open' else 'closed'
    time_column = 'p.opened_at' if normalized_view == 'open' else 'p.closed_at'
    base_where_clauses = [
        'p.status = %s',
        f'{time_column} >= DATE_SUB(NOW(), INTERVAL %s DAY)',
    ]
    base_params: list[Any] = [status_value, days]

    if base_asset:
        base_where_clauses.append('p.base_asset = %s')
        base_params.append(base_asset)

    if position_id is not None:
        base_where_clauses.append('p.id = %s')
        base_params.append(position_id)

    if channel:
        base_where_clauses.append(
            'EXISTS (SELECT 1 FROM mi_trade_order o WHERE o.position_id = p.id AND o.channel = %s)'
        )
        base_params.append(channel)

    if exchange_risk:
        assets = list(delist_risk_assets or [])
        if assets:
            placeholders = ','.join(['%s'] * len(assets))
            base_where_clauses.append(
                "(p.exchange_risk_status = 'desynced' "
                f'OR UPPER(TRIM(p.base_asset)) IN ({placeholders}))'
            )
            base_params.extend(assets)
        else:
            base_where_clauses.append("p.exchange_risk_status = 'desynced'")

    return ' AND '.join(base_where_clauses), base_params


class TradingQueryRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def count_positions_by_where(self, where_sql: str, params: list[Any]) -> int:
        count_sql = f'SELECT COUNT(*) AS total FROM mi_trade_position p WHERE {where_sql}'
        with self._db.get_cursor() as cursor:
            cursor.execute(count_sql, params)
            total_row = cursor.fetchone()
            return total_row['total'] if total_row else 0

    def list_order_view_positions(
        self,
        *,
        where_sql: str,
        params: list[Any],
        time_column: str,
        close_threshold_col: str,
        page_size: int,
        offset: int,
    ) -> list[Row]:
        query_params = list(params) + [page_size, offset]
        sql = f"""
        SELECT p.*,
               COALESCE(b.market_profile, 'normal') AS market_profile,
               t.open_basis_p20 AS open_vwap_threshold_bps,
               t.{close_threshold_col} AS close_vwap_threshold_bps,
               (SELECT o.channel FROM mi_trade_order o WHERE o.position_id = p.id LIMIT 1) AS channel,
               (
                   SELECT o.leverage
                   FROM mi_trade_order o
                   WHERE o.position_id = p.id
                     AND o.order_side = 'open'
                     AND o.market_type = 'future'
                   ORDER BY o.id ASC
                   LIMIT 1
               ) AS gate_leverage,
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
        ORDER BY {time_column} DESC, p.id DESC
        LIMIT %s OFFSET %s
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, query_params)
            return list(cursor.fetchall() or [])

    def fetch_order_tab_summary(self) -> Row:
        tab_summary_sql = """
        SELECT
            (SELECT COUNT(*)
             FROM mi_trade_position
             WHERE status = 'holding') AS current_open,
            (SELECT COUNT(*)
             FROM mi_trade_position
             WHERE status = 'closed'
               AND closed_at >= CURDATE()
               AND closed_at < DATE_ADD(CURDATE(), INTERVAL 1 DAY)) AS today_closed
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(tab_summary_sql)
            return cursor.fetchone() or {}

    def list_orders_by_position_id(self, position_id: int) -> list[Row]:
        sql = 'SELECT * FROM mi_trade_order WHERE position_id = %s ORDER BY id ASC'
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, [position_id])
            return list(cursor.fetchall() or [])

    def list_grouped_orders(self) -> list[Row]:
        sql = """
        SELECT * FROM mi_trade_order 
        WHERE position_id IS NOT NULL
        ORDER BY position_id, order_side, market_type, created_at DESC
        LIMIT 2000
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall() or [])

    def fetch_positions_status_summary(
        self,
        days: int,
        base_asset: Optional[str],
    ) -> Row:
        summary_sql = """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'holding' THEN 1 ELSE 0 END) as holding_count,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_count
            FROM mi_trade_position
            WHERE opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """
        summary_params: list[Any] = [days]
        if base_asset:
            summary_sql += ' AND base_asset = %s'
            summary_params.append(base_asset)
        with self._db.get_cursor() as cursor:
            cursor.execute(summary_sql, summary_params)
            return cursor.fetchone() or {}

    def count_positions(
        self,
        days: int,
        status: Optional[str],
        base_asset: Optional[str],
    ) -> int:
        count_sql = 'SELECT COUNT(*) as total FROM mi_trade_position WHERE opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)'
        count_params: list[Any] = [days]
        if status:
            count_sql += ' AND status = %s'
            count_params.append(status)
        if base_asset:
            count_sql += ' AND base_asset = %s'
            count_params.append(base_asset)
        with self._db.get_cursor() as cursor:
            cursor.execute(count_sql, count_params)
            total_row = cursor.fetchone()
            return total_row['total'] if total_row else 0

    def list_positions(
        self,
        days: int,
        status: Optional[str],
        base_asset: Optional[str],
        page_size: int,
        offset: int,
    ) -> list[Row]:
        sql = """
            SELECT p.*, COALESCE(b.market_profile, 'normal') AS market_profile
            FROM mi_trade_position p
            LEFT JOIN mi_base_asset b
              ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(p.base_asset))
            WHERE p.opened_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """
        params: list[Any] = [days]
        if status:
            sql += ' AND p.status = %s'
            params.append(status)
        if base_asset:
            sql += ' AND p.base_asset = %s'
            params.append(base_asset)
        sql += ' ORDER BY p.opened_at DESC LIMIT %s OFFSET %s'
        params.extend([page_size, offset])
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall() or [])

    def list_funding_fee_history(self, position_ids: list[Any]) -> list[Row]:
        if not position_ids:
            return []
        placeholders = ','.join(['%s'] * len(position_ids))
        history_sql = f"""
            SELECT position_id, payment_seq, funding_rate, funding_rate_24h,
                   funding_pnl, future_notional, settled_at
            FROM mi_trade_funding_fee_history
            WHERE position_id IN ({placeholders})
            ORDER BY position_id, payment_seq
        """
        with self._db.get_cursor() as cursor:
            cursor.execute(history_sql, position_ids)
            return list(cursor.fetchall() or [])

    def fetch_positions_aggregate_summary(self) -> Optional[Row]:
        sql = """
        SELECT 
            COUNT(*) as total_positions,
            SUM(CASE WHEN status = 'holding' THEN 1 ELSE 0 END) as holding_count,
            SUM(CASE WHEN status = 'holding' THEN spot_open_amount ELSE 0 END) as total_holding_amount,
            SUM(funding_total_pnl) as total_funding_pnl,
            SUM(total_pnl) as total_pnl
        FROM mi_trade_position
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchone()

    def list_forward_signals(
        self,
        where_sql: str,
        params: list[Any],
        page_size: int,
        offset: int,
    ) -> list[Row]:
        sql = f"""
        SELECT
            s.*,
            COALESCE(b.strategy_tier, 'C') AS strategy_tier,
            COALESCE(b.market_profile, 'normal') AS market_profile
        FROM mi_trade_signal s
        LEFT JOIN mi_base_asset b
          ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(s.base_asset))
        WHERE {where_sql}
    """
        query_params = list(params)
        sql += ' ORDER BY s.signal_time DESC LIMIT %s OFFSET %s'
        query_params.extend([page_size, offset])
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, query_params)
            return list(cursor.fetchall() or [])

    def fetch_forward_signal_summary(self, where_sql: str, params: list[Any]) -> Optional[Row]:
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
        with self._db.get_cursor() as cursor:
            cursor.execute(summary_sql, params)
            return cursor.fetchone()
