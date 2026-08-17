# coding: utf-8
"""AG Grid column-config repository.

Wraps existing parameterized SQL for GET/POST /column-config/{page_key}.
Write path keeps one get_connection and a manually closed cursor.
Exceptions are swallowed inside the connection so the original
commit-on-caught-error behavior is unchanged.
Does not change table schema.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from common.database import DatabaseManager, db_manager as _default_db_manager

Row = dict[str, Any]
UpsertParams = Sequence[Any]

_LIST_SQL = (
    "SELECT col_id, sort_order, is_visible, width, pinned, sort, filter_model "
    "FROM ag_grid_column_config "
    "WHERE user_id = %s AND page_key = %s "
    "ORDER BY sort_order ASC"
)
_UPSERT_SQL = (
    "INSERT INTO ag_grid_column_config "
    "(user_id, page_key, col_id, sort_order, is_visible, width, pinned, sort, filter_model) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON DUPLICATE KEY UPDATE "
    "sort_order = VALUES(sort_order), "
    "is_visible = VALUES(is_visible), "
    "width = VALUES(width), "
    "pinned = VALUES(pinned), "
    "sort = VALUES(sort), "
    "filter_model = VALUES(filter_model)"
)


class ColumnConfigRepo:
    def __init__(self, db_manager: Optional[DatabaseManager] = None) -> None:
        self._db = db_manager or _default_db_manager

    def list_column_state(self, user_id: str, page_key: str) -> list[Row]:
        with self._db.get_cursor() as cursor:
            cursor.execute(_LIST_SQL, (user_id, page_key))
            return list(cursor.fetchall() or [])

    def upsert_column_states(self, rows: list[UpsertParams]) -> Optional[BaseException]:
        with self._db.get_connection() as conn:
            cursor = conn.cursor()
            try:
                for params in rows:
                    cursor.execute(_UPSERT_SQL, params)
                return None
            except Exception as exc:
                return exc
            finally:
                cursor.close()
