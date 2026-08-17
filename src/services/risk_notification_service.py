# coding: utf-8
"""Risk-bell query service.

Assembles GET /risk-notifications/recent JSON. Sync on purpose;
callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from common.database import DatabaseManager
from repositories.risk_notification_repo import RiskNotificationRepo

Row = dict[str, Any]
SerializeRow = Callable[[Row], Row]
IgnoreClause = Callable[..., tuple[str, list[Any]]]


def risk_notification_key(prefix: str, *parts: Any) -> str:
    values = [str(part if part is not None else '').strip() for part in parts]
    return f"{prefix}:{':'.join(values)}"[:220]


def db_bool(value: Any) -> bool:
    if value is None:
        return False
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def should_emit_reconciliation_notification(row: dict[str, Any], latest_snapshot_at: Any) -> bool:
    """Suppress one-off historical reconciliation mismatches caused by in-flight trades."""
    if latest_snapshot_at is not None and row.get('snapshot_at') == latest_snapshot_at:
        return True
    previous_is_match = row.get('previous_is_match')
    if previous_is_match is None:
        return False
    return not db_bool(previous_is_match)


def format_reconciliation_notification(row: dict[str, Any], dedup_key: str) -> dict[str, Any]:
    base_asset = row.get('base_asset') or '-'
    exchange = row.get('exchange') or '-'
    exchange_label = str(exchange).capitalize() if exchange != '-' else '-'
    dimension = row.get('dimension') or '-'
    detail = row.get('detail') if isinstance(row.get('detail'), dict) else {}
    is_error = base_asset == '__ERROR__' or dimension == 'error'

    if is_error:
        error_msg = detail.get('error_msg') or '未返回持仓数据'
        title = f"持仓对账拉取失败: {exchange_label}"
        message = f"{exchange_label} 对账接口错误: {error_msg}"
        status = 'error'
    else:
        title = f"持仓对账不一致: {base_asset}"
        message = (
            f"{exchange} {dimension} "
            f"local={row.get('local_value') if row.get('local_value') is not None else '-'} "
            f"exchange={row.get('exchange_value') if row.get('exchange_value') is not None else '-'} "
            f"diff={row.get('diff_value') if row.get('diff_value') is not None else '-'}"
        )
        status = 'mismatch'

    return {
        'dedup_key': dedup_key,
        'source': 'reconciliation',
        'severity': 'warning',
        'title': title,
        'message': message,
        'event_at': row.get('snapshot_at'),
        'base_asset': base_asset,
        'risk_type': dimension,
        'status': status,
        'detail': row,
    }


def append_unique_notification(
    items: list[dict[str, Any]],
    seen_keys: set[str],
    item: dict[str, Any],
) -> None:
    dedup_key = str(item.get('dedup_key') or '')
    if dedup_key:
        if dedup_key in seen_keys:
            return
        seen_keys.add(dedup_key)
    items.append(item)


class RiskNotificationService:
    def __init__(
        self,
        db_manager: DatabaseManager,
        *,
        serialize_row: SerializeRow,
        ignore_clause: IgnoreClause,
    ) -> None:
        self._repo = RiskNotificationRepo(db_manager)
        self._serialize_row = serialize_row
        self._ignore_clause = ignore_clause

    def list_recent_items(self, *, hours: int, limit: int) -> list[dict[str, Any]]:
        cutoff = datetime.now() - timedelta(hours=hours)
        items: list[dict[str, Any]] = []
        seen_notification_keys: set[str] = set()

        exchange_rows = self._repo.list_exchange_risk_events(cutoff, limit)
        for row in exchange_rows:
            row = self._serialize_row(row)
            append_unique_notification(items, seen_notification_keys, {
                'dedup_key': risk_notification_key('exchange_risk', row.get('event_key')),
                'source': 'exchange_risk',
                'severity': 'error',
                'title': f"交易所风险: {row.get('base_asset') or '-'}",
                'message': (
                    f"{row.get('exchange') or '-'} {row.get('risk_type') or 'unknown'} "
                    f"status={row.get('status') or '-'} "
                    f"size={row.get('size') if row.get('size') is not None else '-'} "
                    f"price={row.get('fill_price') if row.get('fill_price') is not None else '-'}"
                ),
                'event_at': row.get('event_at'),
                'base_asset': row.get('base_asset'),
                'risk_type': row.get('risk_type'),
                'status': row.get('status'),
                'detail': row,
            })

        latest_snapshot_at = self._repo.get_latest_recon_snapshot_at()
        ignore_sql, ignore_params = self._ignore_clause('r')
        candidate_limit = min(max(limit * 5, 100), 1000)
        recon_rows = self._repo.list_recon_mismatch_candidates(
            cutoff, ignore_sql, ignore_params, candidate_limit
        )
        for row in recon_rows:
            if not should_emit_reconciliation_notification(row, latest_snapshot_at):
                continue
            row.pop('previous_is_match', None)
            row = self._serialize_row(row)
            base_asset = row.get('base_asset') or '-'
            exchange = row.get('exchange') or '-'
            dimension = row.get('dimension') or '-'
            dedup_key = risk_notification_key(
                'reconciliation',
                exchange,
                base_asset,
                dimension,
                row.get('local_value'),
                row.get('exchange_value'),
            )
            append_unique_notification(
                items,
                seen_notification_keys,
                format_reconciliation_notification(row, dedup_key),
            )

        items.sort(key=lambda item: str(item.get('event_at') or ''), reverse=True)
        return items[:limit]

    def recent(self, *, hours: int, limit: int) -> dict[str, Any]:
        items = self.list_recent_items(hours=hours, limit=limit)
        return {
            'items': items,
            'summary': {
                'total': len(items),
                'exchange_risk': sum(1 for item in items if item.get('source') == 'exchange_risk'),
                'reconciliation': sum(1 for item in items if item.get('source') == 'reconciliation'),
            },
            'lookback_hours': hours,
        }
