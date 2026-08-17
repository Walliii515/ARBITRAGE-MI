# coding: utf-8
"""VWAP basis threshold query repository.

Wraps existing parameterized SQL for latest-date / dates / assets / data.
Does not change table schema or result columns.
"""
from __future__ import annotations

from typing import Any, Optional

from common.database import DatabaseManager, db_manager as _default_db_manager

Row = dict[str, Any]


def build_threshold_data_filters(
    calc_date: Optional[str],
    base_asset: Optional[str],
) -> tuple[str, list[Any]]:
    where_sql = ' FROM mi_vwap_basis_threshold WHERE 1=1'
    params: list[Any] = []
    if calc_date:
        where_sql += ' AND calc_date = %s'
        params.append(calc_date)
    else:
        where_sql += ' AND calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)'
    if base_asset:
        where_sql += ' AND base_asset = %s'
        params.append(base_asset)
    return where_sql, params


class ThresholdQueryRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def get_btc_latest_updated_at(self) -> Any:
        sql = """
        SELECT updated_at
        FROM mi_vwap_basis_threshold
        WHERE base_asset = 'BTC'
          AND updated_at IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
        if row and row.get('updated_at'):
            return row['updated_at']
        return None

    def list_calc_dates(self) -> list[Row]:
        sql = 'SELECT DISTINCT calc_date FROM mi_vwap_basis_threshold ORDER BY calc_date DESC LIMIT 365'
        with self._db.get_cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall() or [])

    def list_assets_for_latest_date(self) -> list[Row]:
        sql = """
        SELECT base_asset
        FROM mi_vwap_basis_threshold
        WHERE calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
        ORDER BY open_basis_p20 DESC
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall() or [])

    def count_threshold_data(self, where_sql: str, params: list[Any]) -> int:
        sql = 'SELECT COUNT(*) AS total' + where_sql
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, params)
            total_row = cursor.fetchone()
            return int(total_row['total']) if total_row and total_row.get('total') is not None else 0

    def list_threshold_data(
        self,
        where_sql: str,
        params: list[Any],
        page_size: int,
        offset: int,
    ) -> list[Row]:
        sql = """
        SELECT
            id, base_asset, calc_date,
            open_basis_max, open_basis_min,
            open_basis_p1, open_basis_p2, open_basis_p3, open_basis_p5, open_basis_p10, open_basis_p20,
            close_basis_max, close_basis_min,
            close_basis_p1, close_basis_p2, close_basis_p3, close_basis_p5, close_basis_p10, close_basis_p20,
            updated_at
    """ + where_sql + ' ORDER BY open_basis_p20 DESC, base_asset ASC LIMIT %s OFFSET %s'
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, [*params, page_size, offset])
            return list(cursor.fetchall() or [])
