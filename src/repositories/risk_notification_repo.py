# coding: utf-8
"""Risk-bell query repository.

Wraps existing parameterized SQL for exchange-risk events and
reconciliation mismatch candidates. Three independent get_cursor
calls, same as the original route. Does not change table schema.
"""
from __future__ import annotations

from typing import Any, Optional

from common.database import DatabaseManager, db_manager as _default_db_manager

Row = dict[str, Any]


class RiskNotificationRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def list_exchange_risk_events(self, cutoff: Any, limit: int) -> list[Row]:
        sql = """
        SELECT
            id, event_key, exchange, market_type, risk_type, base_asset, contract,
            event_at, side, size, fill_price, mark_price, liq_price, pnl,
            status, remediation_action, created_at, updated_at
        FROM mi_exchange_risk_event
        WHERE event_key NOT LIKE 'recon:%%'
          AND (created_at >= %s OR updated_at >= %s OR event_at >= %s)
        ORDER BY GREATEST(created_at, updated_at) DESC, event_at DESC
        LIMIT %s
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, [cutoff, cutoff, cutoff, limit])
            return list(cursor.fetchall() or [])

    def get_latest_recon_snapshot_at(self) -> Any:
        with self._db.get_cursor() as cursor:
            cursor.execute(
                "SELECT MAX(snapshot_at) AS latest_snapshot_at FROM mi_recon_snapshot"
            )
            latest_recon_row = cursor.fetchone()
        return latest_recon_row.get('latest_snapshot_at') if latest_recon_row else None

    def list_recon_mismatch_candidates(
        self,
        cutoff: Any,
        ignore_sql: str,
        ignore_params: list[Any],
        candidate_limit: int,
    ) -> list[Row]:
        sql = f"""
        SELECT
            r.id, r.snapshot_at, r.exchange, r.base_asset, r.dimension,
            r.local_value, r.exchange_value, r.diff_value, r.diff_ratio, r.detail,
            (
                SELECT prev.is_match
                FROM mi_recon_snapshot prev
                WHERE prev.exchange = r.exchange
                  AND prev.base_asset = r.base_asset
                  AND prev.dimension = r.dimension
                  AND prev.snapshot_at < r.snapshot_at
                ORDER BY prev.snapshot_at DESC
                LIMIT 1
            ) AS previous_is_match
        FROM mi_recon_snapshot r
        WHERE r.snapshot_at >= %s
          AND r.is_match = 0
          {ignore_sql}
        ORDER BY r.snapshot_at DESC, r.exchange ASC, r.base_asset ASC
        LIMIT %s
    """
        with self._db.get_cursor() as cursor:
            cursor.execute(sql, [cutoff, *ignore_params, candidate_limit])
            return list(cursor.fetchall() or [])
