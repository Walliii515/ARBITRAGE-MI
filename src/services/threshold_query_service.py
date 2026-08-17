# coding: utf-8
"""VWAP basis threshold query service.

Assembles GET /threshold/latest-date, /dates, /assets, /data JSON.
Sync on purpose; callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from common.database import DatabaseManager
from repositories.threshold_query_repo import (
    ThresholdQueryRepo,
    build_threshold_data_filters,
)

Row = dict[str, Any]
SerializeRows = Callable[[list[Row]], list[Row]]


class ThresholdQueryService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        serialize_rows: SerializeRows,
    ) -> None:
        self._repo = ThresholdQueryRepo(db_manager)
        self._serialize_rows = serialize_rows

    def latest_date(self) -> dict[str, Any]:
        updated_at = self._repo.get_btc_latest_updated_at()
        if updated_at:
            return {
                'latest_date': (
                    updated_at.strftime('%Y-%m-%d %H:%M')
                    if hasattr(updated_at, 'strftime')
                    else str(updated_at)
                ),
            }
        return {'latest_date': None}

    def dates(self) -> list[str]:
        rows = self._repo.list_calc_dates()
        return [
            row['calc_date'].strftime('%Y-%m-%d')
            if hasattr(row['calc_date'], 'strftime')
            else str(row['calc_date'])
            for row in rows
        ]

    def assets(self) -> list[Any]:
        rows = self._repo.list_assets_for_latest_date()
        return [row['base_asset'] for row in rows]

    def data(
        self,
        *,
        calc_date: Optional[str],
        base_asset: Optional[str],
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        where_sql, params = build_threshold_data_filters(calc_date, base_asset)
        total = self._repo.count_threshold_data(where_sql, params)
        offset = (page - 1) * page_size
        rows = self._repo.list_threshold_data(where_sql, params, page_size, offset)
        return {
            'rows': self._serialize_rows(rows),
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size,
            },
        }
