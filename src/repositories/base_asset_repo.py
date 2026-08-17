# coding: utf-8
"""Base-asset write repository.

Wraps existing parameterized SQL for POST /base-assets/{asset}/disable.
Holding count and is_valid update stay on one connection.
Does not change table schema.
"""
from __future__ import annotations

from typing import Optional

from common.database import DatabaseManager, db_manager as _default_db_manager


class BaseAssetRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def disable_asset(self, asset: str) -> tuple[int, int]:
        with self._db.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                SELECT COUNT(*) AS holding_count
                FROM mi_trade_position
                WHERE status = 'holding'
                  AND UPPER(TRIM(base_asset)) = %s
                """,
                    [asset],
                )
                holding_count = int((cursor.fetchone() or {}).get('holding_count') or 0)
                cursor.execute(
                    """
                UPDATE mi_base_asset
                SET is_valid = 'N'
                WHERE UPPER(TRIM(base_asset)) = %s
                """,
                    [asset],
                )
                affected = cursor.rowcount
        return holding_count, affected
