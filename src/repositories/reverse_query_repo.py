# coding: utf-8
"""Reverse signal query repository.

Wraps existing parameterized SQL for GET /reverse-signals.
Does not change table schema or result columns.
"""
from __future__ import annotations

from typing import Any, Optional

from common.database import DatabaseManager, db_manager as _default_db_manager

Row = dict[str, Any]


def build_reverse_signal_filters(
    *,
    status: Optional[str],
    base_asset: Optional[str],
    days: int,
    prefix: str = '',
) -> tuple[str, list[Any]]:
    conditions = [
        f'{prefix}signal_time >= DATE_SUB(NOW(), INTERVAL %s DAY)',
        f'{prefix}signal_basis_bps IS NOT NULL',
    ]
    params: list[Any] = [days]
    if status:
        conditions.append(f'{prefix}status = %s')
        params.append(status)
    if base_asset:
        conditions.append(f'{prefix}base_asset LIKE %s')
        params.append(f'%{base_asset}%')
    return ' AND '.join(conditions), params


class ReverseQueryRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def count_reverse_signals(self, where_sql: str, params: list[Any]) -> int:
        count_sql = f'SELECT COUNT(*) AS total FROM mi_reverse_trade_signal WHERE {where_sql}'
        with self._db.get_cursor() as cursor:
            cursor.execute(count_sql, params)
            total_row = cursor.fetchone()
            return int(total_row['total'] if total_row else 0)

    def list_reverse_signals(
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
        FROM mi_reverse_trade_signal s
        LEFT JOIN mi_base_asset b
          ON UPPER(TRIM(b.base_asset)) = UPPER(TRIM(s.base_asset))
        WHERE {where_sql}
        ORDER BY s.signal_time DESC
        LIMIT %s OFFSET %s
    """
        query_params = list(params)
        query_params.extend([page_size, offset])
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, query_params)
            return list(cursor.fetchall() or [])

    def fetch_reverse_signal_summary(self, where_sql: str, params: list[Any]) -> Optional[Row]:
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
        with self._db.get_cursor() as cursor:
            cursor.execute(summary_sql, params)
            return cursor.fetchone()
