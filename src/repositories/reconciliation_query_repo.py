# coding: utf-8
"""Forward reconciliation snapshot query repository.

Wraps existing parameterized SQL for latest / history. Does not change
table schema or result columns.
"""
from __future__ import annotations

from typing import Any, Optional

from common.database import DatabaseManager, db_manager as _default_db_manager

Row = dict[str, Any]


def reconciliation_ignore_clause(
    ignored: list[str],
    table_alias: str = '',
) -> tuple[str, list[Any]]:
    if not ignored:
        return '', []
    placeholders = ','.join(['%s'] * len(ignored))
    prefix = f'{table_alias}.' if table_alias else ''
    return (
        f" AND NOT ({prefix}exchange = 'binance' AND {prefix}base_asset IN ({placeholders}))",
        list(ignored),
    )


def reconciliation_latest_sql(ignore_sql: str) -> str:
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


class ReconciliationQueryRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def list_latest(self, ignore_sql: str, ignore_params: list[Any]) -> list[Row]:
        sql = reconciliation_latest_sql(ignore_sql)
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, ignore_params)
            return list(cursor.fetchall() or [])

    def count_history(self, where_sql: str, ignore_sql: str, params: list[Any]) -> int:
        sql = f'SELECT COUNT(*) AS total FROM mi_recon_snapshot WHERE {where_sql}{ignore_sql}'
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, params)
            total_row = cursor.fetchone()
            return int(total_row['total']) if total_row else 0

    def list_history(
        self,
        where_sql: str,
        ignore_sql: str,
        params: list[Any],
        page_size: int,
        offset: int,
    ) -> list[Row]:
        sql = f"""
        SELECT *
        FROM mi_recon_snapshot
        WHERE {where_sql}{ignore_sql}
        ORDER BY snapshot_at DESC, exchange ASC, base_asset ASC
        LIMIT %s OFFSET %s
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, [*params, page_size, offset])
            return list(cursor.fetchall() or [])
