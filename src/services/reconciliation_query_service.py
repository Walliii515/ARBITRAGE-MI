# coding: utf-8
"""Reconciliation query service.

Assembles forward and reverse reconciliation API JSON. Sync on purpose;
callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from typing import Any, Callable

from calc.reverse_account_monitor import build_reverse_reconciliation_rows
from calc.reverse_trade_store import list_reverse_positions
from common.database import DatabaseManager
from repositories.reconciliation_query_repo import ReconciliationQueryRepo

Row = dict[str, Any]
SerializeRows = Callable[[list[Row]], list[Row]]
IgnoreClause = Callable[..., tuple[str, list[Any]]]


class ReconciliationQueryService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        serialize_rows: SerializeRows,
        ignore_clause: IgnoreClause,
    ) -> None:
        self._repo = ReconciliationQueryRepo(db_manager)
        self._serialize_rows = serialize_rows
        self._ignore_clause = ignore_clause

    def latest(self) -> dict[str, Any]:
        ignore_sql, ignore_params = self._ignore_clause('s')
        rows = self._repo.list_latest(ignore_sql, ignore_params)
        return {'rows': self._serialize_rows(rows)}

    def history(
        self,
        *,
        days: int,
        mismatches_only: bool,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        where_clauses = ['snapshot_at >= DATE_SUB(NOW(), INTERVAL %s DAY)']
        params: list[Any] = [days]
        if mismatches_only:
            where_clauses.append('is_match = 0')
        ignore_sql, ignore_params = self._ignore_clause()
        where_sql = ' AND '.join(where_clauses)
        query_params = [*params, *ignore_params]
        total = self._repo.count_history(where_sql, ignore_sql, query_params)
        offset = (page - 1) * page_size
        rows = self._repo.list_history(
            where_sql, ignore_sql, query_params, page_size, offset
        )
        return {
            'rows': self._serialize_rows(rows),
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': (total + page_size - 1) // page_size if total else 0,
            },
        }

    def reverse_history(
        self,
        *,
        days: int,
        mismatches_only: bool,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        result = list_reverse_positions(
            status='holding',
            days=days,
            page=1,
            page_size=5000,
        )
        payload = build_reverse_reconciliation_rows(self._serialize_rows(result.rows))
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
