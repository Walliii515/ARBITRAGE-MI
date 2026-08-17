# coding: utf-8
"""Reverse trading query service.

Assembles reverse signal / position / order rows into the existing API JSON.
Sync on purpose; callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from calc.reverse_account_monitor import get_reverse_capital_snapshot
from calc.reverse_trade_store import (
    list_reverse_orders,
    list_reverse_position_orders,
    list_reverse_positions,
    summarize_reverse_positions,
)
from common.database import DatabaseManager
from repositories.reverse_query_repo import ReverseQueryRepo, build_reverse_signal_filters

Row = dict[str, Any]
SerializeRow = Callable[[Row], Row]
SerializeRows = Callable[[list[Row]], list[Row]]
GetCapitalSnapshot = Callable[[], dict[str, Any]]


class ReverseQueryService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        serialize_row: SerializeRow,
        serialize_rows: SerializeRows,
        get_capital_snapshot: GetCapitalSnapshot = get_reverse_capital_snapshot,
    ) -> None:
        self._repo = ReverseQueryRepo(db_manager)
        self._serialize_row = serialize_row
        self._serialize_rows = serialize_rows
        self._get_capital_snapshot = get_capital_snapshot

    def list_signals(
        self,
        *,
        status: Optional[str],
        base_asset: Optional[str],
        days: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        where_sql, where_params = build_reverse_signal_filters(
            status=status,
            base_asset=base_asset,
            days=days,
        )
        aliased_where_sql, aliased_where_params = build_reverse_signal_filters(
            status=status,
            base_asset=base_asset,
            days=days,
            prefix='s.',
        )
        total = self._repo.count_reverse_signals(where_sql, where_params)
        offset = (page - 1) * page_size
        rows = self._repo.list_reverse_signals(
            aliased_where_sql, aliased_where_params, page_size, offset
        )
        summary_row = self._repo.fetch_reverse_signal_summary(where_sql, where_params)
        summary_data = self._serialize_row(summary_row) if summary_row else {}
        total_count = int(summary_data.get('total') or 0)
        opened_count = int(summary_data.get('opened') or 0)
        signal_rows = self._serialize_rows(rows)
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

    def list_positions(
        self,
        *,
        status: Optional[str],
        order_side: Optional[str],
        exchange_risk: bool,
        base_asset: Optional[str],
        days: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
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
            'positions': self._serialize_rows(result.rows),
            'pagination': {
                'page': result.page,
                'page_size': result.page_size,
                'total': result.total,
                'total_pages': result.total_pages,
            },
            'summary': summary,
        }

    def list_position_orders(self, position_id: int) -> dict[str, Any]:
        rows = list_reverse_position_orders(position_id)
        return {'orders': self._serialize_rows(rows)}

    def list_orders(
        self,
        *,
        position_id: Optional[int],
        order_uuid: Optional[str],
        order_side: Optional[str],
        status: Optional[str],
        market_type: Optional[str],
        base_asset: Optional[str],
        days: int,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
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
            'orders': self._serialize_rows(result.rows),
            'pagination': {
                'page': result.page,
                'page_size': result.page_size,
                'total': result.total,
                'total_pages': result.total_pages,
            },
        }

    def capital(self) -> dict[str, Any]:
        return self._get_capital_snapshot()
