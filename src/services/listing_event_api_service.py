# coding: utf-8
"""Listing-event API facade.

Keeps calc.listing_event_monitor behavior and maps ValueError to AppError
with the existing public detail strings.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from common.errors import AppError

Row = dict[str, Any]
SerializeRow = Callable[[Row], Row]
SerializeRows = Callable[[list[Row]], list[Row]]
ListEvents = Callable[..., list[Row]]
EventSummary = Callable[[], Row]
RefreshEvents = Callable[[], dict[str, Any]]
MarkEvents = Callable[..., int]
AddToMonitor = Callable[[str], dict[str, Any]]
DisableAsset = Callable[..., dict[str, Any]]


class ListingEventApiService:
    def __init__(
        self,
        *,
        serialize_row: SerializeRow,
        serialize_rows: SerializeRows,
        list_events: ListEvents,
        event_summary: EventSummary,
        refresh_events: RefreshEvents,
        mark_events: MarkEvents,
        add_to_monitor: AddToMonitor,
        disable_asset: DisableAsset,
    ) -> None:
        self._serialize_row = serialize_row
        self._serialize_rows = serialize_rows
        self._list_events = list_events
        self._event_summary = event_summary
        self._refresh_events = refresh_events
        self._mark_events = mark_events
        self._add_to_monitor = add_to_monitor
        self._disable_asset = disable_asset

    def list_events(
        self,
        *,
        action_status: Optional[str],
        candidate_status: Optional[str],
        monitor_status: Optional[str],
        actionable_only: bool,
        limit: int,
    ) -> dict[str, Any]:
        rows = self._list_events(
            action_status=action_status,
            candidate_status=candidate_status,
            monitor_status=monitor_status,
            actionable_only=actionable_only,
            limit=limit,
        )
        return {
            'items': self._serialize_rows(rows),
            'summary': self._serialize_row(self._event_summary()),
        }

    def summary(self) -> dict[str, Any]:
        items = self._list_events(
            action_status='pending',
            candidate_status='matched',
            actionable_only=True,
            limit=20,
        )
        return {
            'summary': self._serialize_row(self._event_summary()),
            'items': self._serialize_rows(items),
        }

    def refresh(self) -> dict[str, Any]:
        return self._refresh_events()

    def ack(self, base_asset: str, reason: Optional[str] = None) -> dict[str, Any]:
        asset = (base_asset or '').strip().upper()
        affected = self._mark_events(
            [asset], 'acknowledged', reason or 'acknowledged'
        )
        return {'success': True, 'base_asset': asset, 'affected': affected}

    def ignore(self, base_asset: str, reason: Optional[str] = None) -> dict[str, Any]:
        asset = (base_asset or '').strip().upper()
        affected = self._mark_events([asset], 'ignored', reason or 'ignored')
        return {'success': True, 'base_asset': asset, 'affected': affected}

    def add_to_monitor(self, base_asset: str) -> dict[str, Any]:
        try:
            return self._add_to_monitor(base_asset)
        except ValueError as exc:
            raise AppError(str(exc), status_code=400) from exc

    def disable(self, base_asset: str, reason: Optional[str] = None) -> dict[str, Any]:
        try:
            return self._disable_asset(
                base_asset,
                reason or 'listing_event_disabled',
            )
        except ValueError as exc:
            raise AppError(str(exc), status_code=400) from exc
