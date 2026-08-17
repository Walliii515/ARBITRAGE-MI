# coding: utf-8
"""Delist-risk report cache and position-row attach helpers.

GET /delist-risks and order/position enrich share the same cache.
Sync on purpose; callers should wrap with asyncio.to_thread.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from calc.delist_risk_monitor import DelistRiskConfig, DelistRiskMonitor
from common.logger import get_logger

logger = get_logger(__name__)

GetReport = Callable[..., dict[str, Any]]


def get_delist_risk_report_cached(
    lookahead_days: int = 30,
    max_age_sec: int = 900,
    *,
    cache: dict[str, Any],
    lock: threading.Lock,
) -> dict[str, Any]:
    now = time.time()
    cached_report = cache.get('report')
    if (
        cached_report is not None
        and cache.get('lookahead_days') == lookahead_days
        and now - float(cache.get('at') or 0) < max_age_sec
    ):
        return cached_report

    with lock:
        now = time.time()
        cached_report = cache.get('report')
        if (
            cached_report is not None
            and cache.get('lookahead_days') == lookahead_days
            and now - float(cache.get('at') or 0) < max_age_sec
        ):
            return cached_report

        try:
            monitor = DelistRiskMonitor(DelistRiskConfig(lookahead_days=lookahead_days))
            report = monitor.build_report()
            cache.update({
                'at': now,
                'lookahead_days': lookahead_days,
                'report': report,
            })
            return report
        except Exception as exc:
            logger.warning('下架风险报告刷新失败: %s', exc, exc_info=True)
            if cached_report is not None:
                return cached_report
            return {
                'success': False,
                'lookahead_days': lookahead_days,
                'items': [],
                'source_errors': {'internal': str(exc)},
            }


def delist_risk_asset_set(
    report: Optional[dict[str, Any]] = None,
    *,
    get_report: GetReport,
) -> set[str]:
    report = report or get_report()
    return {
        str(item.get('base_asset') or '').strip().upper()
        for item in report.get('items', [])
        if item.get('base_asset')
    }


def format_delist_risk_summary(items: list[dict[str, Any]]) -> str:
    fragments = []
    for item in items:
        exchange = item.get('exchange') or ''
        message = item.get('message') or item.get('status') or item.get('risk_type') or '下架风险'
        due = item.get('delist_at')
        fragments.append(f"{exchange}:{message}{f' {due}' if due else ''}")
    return ' | '.join(fragments)


def attach_delist_risks(
    rows: list[dict[str, Any]],
    *,
    get_report: GetReport,
) -> None:
    if not rows:
        return
    report = get_report()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in report.get('items', []):
        asset = str(item.get('base_asset') or '').strip().upper()
        if not asset:
            continue
        grouped.setdefault(asset, []).append(item)

    for row in rows:
        asset = str(row.get('base_asset') or '').strip().upper()
        items = grouped.get(asset, [])
        levels = {item.get('severity') or item.get('risk_level') for item in items}
        row['delist_risks'] = items
        row['delist_risk_level'] = (
            'critical' if 'critical' in levels
            else 'warning' if items else None
        )
        row['delist_risk_summary'] = format_delist_risk_summary(items) if items else None
        if not items:
            continue

        existing_status = row.get('exchange_risk_status')
        existing_detail = row.get('exchange_risk_detail')
        summary = f"下架风险: {row['delist_risk_summary']}"
        if not existing_status or existing_status == 'normal':
            row['exchange_risk_status'] = 'delist_risk'
            row['exchange_risk_type'] = 'delist_risk'
            row['exchange_risk_detail'] = summary
        elif existing_detail and summary not in str(existing_detail):
            row['exchange_risk_detail'] = f"{existing_detail} | {summary}"
