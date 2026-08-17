# coding: utf-8
"""Popup-notification API facade.

Keeps calc.popup_notification_store and listing-event sync behavior.
Sync on purpose; callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

Row = dict[str, Any]
SerializeRow = Callable[[Row], Row]
SerializeRows = Callable[[list[Row]], list[Row]]
ListListingEvents = Callable[..., list[Row]]
UpsertOne = Callable[..., Row]
UpsertMany = Callable[..., int]
ListNotifications = Callable[..., dict[str, Any]]
CountUnread = Callable[..., int]
MarkRead = Callable[..., int]
ListRecentRiskItems = Callable[..., list[dict[str, Any]]]
NotificationKey = Callable[..., str]


class PopupNotificationApiService:
    def __init__(
        self,
        *,
        serialize_row: SerializeRow,
        serialize_rows: SerializeRows,
        list_listing_events: ListListingEvents,
        upsert_one: UpsertOne,
        upsert_many: UpsertMany,
        list_notifications: ListNotifications,
        count_unread: CountUnread,
        mark_read: MarkRead,
        list_recent_risk_items: ListRecentRiskItems,
        notification_key: NotificationKey,
    ) -> None:
        self._serialize_row = serialize_row
        self._serialize_rows = serialize_rows
        self._list_listing_events = list_listing_events
        self._upsert_one = upsert_one
        self._upsert_many = upsert_many
        self._list_notifications = list_notifications
        self._count_unread = count_unread
        self._mark_read = mark_read
        self._list_recent_risk_items = list_recent_risk_items
        self._notification_key = notification_key

    def sync_recent(self) -> dict[str, int]:
        listing_synced = 0
        listing_rows = self._list_listing_events(
            action_status='pending',
            candidate_status='matched',
            actionable_only=True,
            limit=20,
        )
        if listing_rows:
            rows = self._serialize_rows(listing_rows)
            fingerprint = '|'.join(sorted(
                f"{row.get('base_asset')}:{row.get('gate_contract') or ''}:"
                f"{row.get('binance_symbol') or ''}:{row.get('last_seen_at') or ''}"
                for row in rows
            ))
            preview = '\n'.join(
                f"{row.get('base_asset')} Gate:{row.get('gate_contract') or '-'} "
                f"Binance:{row.get('binance_symbol') or '-'} "
                f"24h={float(row.get('gate_volume_24h_settle') or 0):.0f}/"
                f"{float(row.get('binance_quote_volume') or 0):.0f}"
                for row in rows[:8]
            )
            self._upsert_one(
                title=f"交易对上新候选 {len(rows)} 个",
                message=preview,
                type='warning',
                source='listing_events',
                dedup_key=self._notification_key('listing_events', fingerprint),
                event_at=max(
                    (row.get('last_seen_at') for row in rows if row.get('last_seen_at')),
                    default=None,
                ),
                payload={'items': rows},
            )
            listing_synced = 1

        risk_items = self._list_recent_risk_items(hours=24, limit=50)
        risk_synced = self._upsert_many(
            [
                {
                    'title': item.get('title') or '交易风险通知',
                    'message': item.get('message') or '',
                    'type': 'error' if item.get('severity') == 'error' else 'warning',
                    'source': item.get('source') or 'risk',
                    'dedup_key': item.get('dedup_key'),
                    'event_at': item.get('event_at'),
                    'payload': item,
                }
                for item in risk_items
            ],
        )
        return {'listing_events': listing_synced, 'risk': risk_synced}

    def list_items(
        self,
        *,
        read_status: str,
        source: Optional[str],
        page: int,
        page_size: int,
        sync_recent: bool,
    ) -> dict[str, Any]:
        synced = self.sync_recent() if sync_recent else {}
        result = self._list_notifications(
            read_status=read_status,
            source=source,
            page=page,
            page_size=page_size,
        )
        return {
            'items': self._serialize_rows(result['items']),
            'pagination': result['pagination'],
            'unread_count': result['unread_count'],
            'synced': synced,
        }

    def unread_count(self) -> dict[str, int]:
        return {'unread_count': self._count_unread()}

    def create(
        self,
        *,
        title: str,
        message: str,
        type: Optional[str],
        source: Optional[str],
        dedup_key: Optional[str],
        event_at: Any,
        payload: Any,
    ) -> dict[str, Any]:
        row = self._upsert_one(
            title=title,
            message=message,
            type=type or 'info',
            source=source,
            dedup_key=dedup_key,
            event_at=event_at,
            payload=payload,
        )
        return {'success': True, 'item': self._serialize_row(row)}

    def mark_read(self, ids: Optional[list[int]] = None) -> dict[str, Any]:
        affected = self._mark_read(ids=ids)
        return {
            'success': True,
            'affected': affected,
            'unread_count': self._count_unread(),
        }
