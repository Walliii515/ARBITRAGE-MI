# coding: utf-8
"""Capital snapshot write repository.

Wraps existing parameterized SQL for POST /capital/clear-range.
Count, backup CREATE TABLE, and DELETE stay on one connection.
Does not change table schema.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from common.database import DatabaseManager, db_manager as _default_db_manager


class CapitalCommandRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def clear_range(
        self,
        start_at: datetime,
        end_at: datetime,
        backup_table: str,
    ) -> dict[str, Any]:
        with self._db.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) AS row_count,
                        MIN(snapshot_at) AS first_snapshot_at,
                        MAX(snapshot_at) AS last_snapshot_at
                    FROM mi_capital_snapshot
                    WHERE snapshot_at BETWEEN %s AND %s
                    """,
                    (start_at, end_at),
                )
                summary = cursor.fetchone() or {}
                row_count = int(summary.get('row_count') or 0)
                if row_count <= 0:
                    return {
                        'empty': True,
                        'deleted': 0,
                        'backup_table': None,
                        'summary': summary,
                    }
                cursor.execute(
                    f"""
                    CREATE TABLE `{backup_table}` AS
                    SELECT *
                    FROM mi_capital_snapshot
                    WHERE snapshot_at BETWEEN %s AND %s
                    """,
                    (start_at, end_at),
                )
                cursor.execute(
                    """
                    DELETE FROM mi_capital_snapshot
                    WHERE snapshot_at BETWEEN %s AND %s
                    """,
                    (start_at, end_at),
                )
                return {
                    'empty': False,
                    'deleted': cursor.rowcount,
                    'backup_table': backup_table,
                    'summary': summary,
                }
