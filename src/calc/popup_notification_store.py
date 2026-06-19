# coding: utf-8
"""Persistent popup notification storage."""
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from common.database import db_manager


CREATE_POPUP_NOTIFICATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS mi_popup_notification (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL DEFAULT 'default',
    dedup_key VARCHAR(220) NULL,
    source VARCHAR(64) NULL,
    type ENUM('warning','error','success','info') NOT NULL DEFAULT 'info',
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    payload JSON NULL,
    event_at DATETIME NULL,
    read_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uniq_popup_user_dedup (user_id, dedup_key),
    INDEX idx_popup_user_read_created (user_id, read_at, created_at),
    INDEX idx_popup_user_source_created (user_id, source, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

VALID_TYPES = {'warning', 'error', 'success', 'info'}
VALID_READ_STATUS = {'unread', 'read', 'all'}


def ensure_popup_notification_table() -> None:
    with db_manager.get_cursor() as cursor:
        cursor.execute(CREATE_POPUP_NOTIFICATION_TABLE_SQL)


def _normalize_type(value: Optional[str]) -> str:
    value = (value or 'info').strip().lower()
    return value if value in VALID_TYPES else 'info'


def _parse_event_at(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace('Z', '+00:00')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _payload_json(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, default=str)


def upsert_popup_notification(
    *,
    title: str,
    message: str,
    type: str = 'info',
    source: Optional[str] = None,
    dedup_key: Optional[str] = None,
    event_at: Any = None,
    payload: Any = None,
    user_id: str = 'default',
) -> Dict[str, Any]:
    """Insert a notification, deduplicating by (user_id, dedup_key) when supplied."""
    ensure_popup_notification_table()
    title = str(title or '').strip()[:255]
    message = str(message or '').strip()
    if not title or not message:
        raise ValueError('title 和 message 不能为空')
    user_id = str(user_id or 'default')[:64]
    dedup_key = str(dedup_key).strip()[:220] if dedup_key else None
    source = str(source).strip()[:64] if source else None
    event_dt = _parse_event_at(event_at)
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO mi_popup_notification
                (user_id, dedup_key, source, type, title, message, payload, event_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                dedup_key,
                source,
                _normalize_type(type),
                title,
                message,
                _payload_json(payload),
                event_dt,
            ),
        )
        notification_id = cursor.lastrowid
        if notification_id:
            cursor.execute("SELECT * FROM mi_popup_notification WHERE id = %s", (notification_id,))
        else:
            cursor.execute(
                "SELECT * FROM mi_popup_notification WHERE user_id = %s AND dedup_key = %s",
                (user_id, dedup_key),
            )
        return cursor.fetchone() or {}


def upsert_popup_notifications(items: Iterable[Dict[str, Any]], *, user_id: str = 'default') -> int:
    count = 0
    for item in items:
        try:
            upsert_popup_notification(
                title=item.get('title') or '',
                message=item.get('message') or '',
                type=item.get('type') or item.get('severity') or 'info',
                source=item.get('source'),
                dedup_key=item.get('dedup_key'),
                event_at=item.get('event_at'),
                payload=item.get('payload') if 'payload' in item else item.get('detail'),
                user_id=user_id,
            )
            count += 1
        except ValueError:
            continue
    return count


def count_unread_popup_notifications(*, user_id: str = 'default') -> int:
    ensure_popup_notification_table()
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM mi_popup_notification WHERE user_id = %s AND read_at IS NULL",
            (user_id,),
        )
        row = cursor.fetchone() or {}
        return int(row.get('cnt') or 0)


def list_popup_notifications(
    *,
    read_status: str = 'unread',
    source: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    user_id: str = 'default',
) -> Dict[str, Any]:
    ensure_popup_notification_table()
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 50), 200))
    read_status = read_status if read_status in VALID_READ_STATUS else 'unread'
    conditions = ['user_id = %s']
    params: List[Any] = [user_id]
    if read_status == 'unread':
        conditions.append('read_at IS NULL')
    elif read_status == 'read':
        conditions.append('read_at IS NOT NULL')
    if source:
        conditions.append('source = %s')
        params.append(source)
    where_sql = ' AND '.join(conditions)
    offset = (page - 1) * page_size
    with db_manager.get_cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM mi_popup_notification WHERE {where_sql}", params)
        total = int((cursor.fetchone() or {}).get('cnt') or 0)
        cursor.execute(
            f"""
            SELECT id, user_id, dedup_key, source, type, title, message, payload,
                   event_at, read_at, created_at, updated_at
            FROM mi_popup_notification
            WHERE {where_sql}
            ORDER BY COALESCE(event_at, created_at) DESC, id DESC
            LIMIT %s OFFSET %s
            """,
            [*params, page_size, offset],
        )
        rows = cursor.fetchall() or []
    return {
        'items': rows,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if total else 0,
        },
        'unread_count': count_unread_popup_notifications(user_id=user_id),
    }


def mark_popup_notifications_read(
    *,
    ids: Optional[List[int]] = None,
    user_id: str = 'default',
) -> int:
    ensure_popup_notification_table()
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        if ids:
            clean_ids = [int(item) for item in ids if str(item).strip().isdigit()]
            if not clean_ids:
                return 0
            placeholders = ','.join(['%s'] * len(clean_ids))
            cursor.execute(
                f"""
                UPDATE mi_popup_notification
                SET read_at = COALESCE(read_at, NOW())
                WHERE user_id = %s AND id IN ({placeholders})
                """,
                [user_id, *clean_ids],
            )
        else:
            cursor.execute(
                """
                UPDATE mi_popup_notification
                SET read_at = COALESCE(read_at, NOW())
                WHERE user_id = %s AND read_at IS NULL
                """,
                (user_id,),
            )
        return cursor.rowcount
