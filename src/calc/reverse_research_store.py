# coding: utf-8
"""
Reverse arbitrage research snapshots and simple analysis.

This module is intentionally read/observation oriented. It does not participate
in forward or reverse execution decisions; it only persists and summarizes
features that are useful for later borrow/funding analysis.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ReverseResearchConfig:
    open_amount_usdt: float
    max_rows_per_snapshot: int = 400


def _as_float(value, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool_int(value) -> Optional[int]:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                return datetime.strptime(value[:19], fmt)
            except ValueError:
                continue
    return None


def ensure_reverse_research_tables() -> None:
    sql = """
        CREATE TABLE IF NOT EXISTS mi_reverse_research_snapshot (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            snapshot_time DATETIME NOT NULL,
            base_asset VARCHAR(32) NOT NULL,
            contract VARCHAR(64) DEFAULT NULL,
            symbol VARCHAR(64) DEFAULT NULL,
            sample_source VARCHAR(32) NOT NULL DEFAULT 'loop',
            funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
            gross_funding_bps DECIMAL(12,4) DEFAULT NULL,
            expected_funding_bps DECIMAL(12,4) DEFAULT NULL,
            next_funding_time DATETIME DEFAULT NULL,
            next_funding_min DECIMAL(12,4) DEFAULT NULL,
            borrowable TINYINT DEFAULT NULL,
            max_borrowable_amount DECIMAL(28,12) DEFAULT NULL,
            borrow_capacity_usdt DECIMAL(20,4) DEFAULT NULL,
            borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
            borrow_24h_bps DECIMAL(12,4) DEFAULT NULL,
            borrow_unavailable_reason VARCHAR(128) DEFAULT NULL,
            reverse_open_basis_bps DECIMAL(12,4) DEFAULT NULL,
            reverse_close_basis_bps DECIMAL(12,4) DEFAULT NULL,
            reverse_margin_edge_bps DECIMAL(12,4) DEFAULT NULL,
            reverse_open_coverage DECIMAL(10,6) DEFAULT NULL,
            spot_spread_bps DECIMAL(12,4) DEFAULT NULL,
            future_spread_bps DECIMAL(12,4) DEFAULT NULL,
            spot_top_bid_usdt DECIMAL(20,4) DEFAULT NULL,
            future_top_ask_usdt DECIMAL(20,4) DEFAULT NULL,
            spot_quote_volume_24h DECIMAL(24,4) DEFAULT NULL,
            future_volume_24h_settle DECIMAL(24,4) DEFAULT NULL,
            reverse_status VARCHAR(64) DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_snapshot_time (snapshot_time),
            KEY idx_asset_time (base_asset, snapshot_time),
            KEY idx_status_time (reverse_status, snapshot_time)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)


def _snapshot_params(row: Dict[str, Any], snapshot_time: datetime, sample_source: str) -> Dict[str, Any]:
    return {
        'snapshot_time': snapshot_time,
        'base_asset': str(row.get('base_asset') or '').upper(),
        'contract': row.get('contract'),
        'symbol': row.get('symbol'),
        'sample_source': sample_source,
        'funding_rate_24h': _as_float(row.get('funding_rate_24h')),
        'gross_funding_bps': _as_float(row.get('reverse_gross_funding_bps')),
        'expected_funding_bps': _as_float(row.get('reverse_expected_funding_bps')),
        'next_funding_time': _parse_dt(row.get('funding_next_apply')),
        'next_funding_min': _as_float(row.get('reverse_funding_carry_next_min')),
        'borrowable': _as_bool_int(row.get('reverse_borrowable')),
        'max_borrowable_amount': _as_float(
            row.get('reverse_max_borrowable_amount'),
            _as_float(row.get('reverse_borrow_limit')),
        ),
        'borrow_capacity_usdt': _as_float(row.get('reverse_borrow_capacity_usdt')),
        'borrow_hourly_rate': _as_float(row.get('reverse_borrow_hourly_rate')),
        'borrow_24h_bps': _as_float(row.get('reverse_borrow_24h_bps')),
        'borrow_unavailable_reason': row.get('borrow_unavailable_reason')
        or row.get('reverse_borrow_unavailable_reason'),
        'reverse_open_basis_bps': _as_float(row.get('reverse_basis_bps')),
        'reverse_close_basis_bps': _as_float(row.get('reverse_close_basis_bps')),
        'reverse_margin_edge_bps': _as_float(row.get('reverse_margin_edge_bps')),
        'reverse_open_coverage': _as_float(row.get('reverse_open_coverage')),
        'spot_spread_bps': _as_float(row.get('spot_spread_bps')),
        'future_spread_bps': _as_float(row.get('future_spread_bps')),
        'spot_top_bid_usdt': _as_float(row.get('spot_top_bid_usdt')),
        'future_top_ask_usdt': _as_float(row.get('future_top_ask_usdt')),
        'spot_quote_volume_24h': _as_float(row.get('quote_volume')),
        'future_volume_24h_settle': _as_float(row.get('volume_24h_settle')),
        'reverse_status': row.get('reverse_status'),
    }


def _should_record(row: Dict[str, Any]) -> bool:
    funding_rate = _as_float(row.get('funding_rate_24h'), 0.0) or 0.0
    if funding_rate < 0:
        return True
    if row.get('reverse_status') in {'candidate', 'borrow_unavailable', 'borrow_capacity_low'}:
        return True
    return False


def record_reverse_research_snapshot(
    rows: Iterable[Dict[str, Any]],
    cfg: ReverseResearchConfig,
    sample_source: str = 'loop',
) -> int:
    ensure_reverse_research_tables()
    snapshot_time = datetime.now()
    selected = [row for row in rows if _should_record(row)]
    selected.sort(
        key=lambda r: (
            _as_float(r.get('funding_rate_24h'), 0.0) or 0.0,
            -(_as_float(r.get('reverse_margin_edge_bps'), -999999.0) or -999999.0),
        )
    )
    if cfg.max_rows_per_snapshot > 0:
        selected = selected[:cfg.max_rows_per_snapshot]
    if not selected:
        return 0

    sql = """
        INSERT INTO mi_reverse_research_snapshot (
            snapshot_time, base_asset, contract, symbol, sample_source,
            funding_rate_24h, gross_funding_bps, expected_funding_bps,
            next_funding_time, next_funding_min,
            borrowable, max_borrowable_amount, borrow_capacity_usdt,
            borrow_hourly_rate, borrow_24h_bps, borrow_unavailable_reason,
            reverse_open_basis_bps, reverse_close_basis_bps, reverse_margin_edge_bps,
            reverse_open_coverage, spot_spread_bps, future_spread_bps,
            spot_top_bid_usdt, future_top_ask_usdt,
            spot_quote_volume_24h, future_volume_24h_settle, reverse_status
        ) VALUES (
            %(snapshot_time)s, %(base_asset)s, %(contract)s, %(symbol)s, %(sample_source)s,
            %(funding_rate_24h)s, %(gross_funding_bps)s, %(expected_funding_bps)s,
            %(next_funding_time)s, %(next_funding_min)s,
            %(borrowable)s, %(max_borrowable_amount)s, %(borrow_capacity_usdt)s,
            %(borrow_hourly_rate)s, %(borrow_24h_bps)s, %(borrow_unavailable_reason)s,
            %(reverse_open_basis_bps)s, %(reverse_close_basis_bps)s, %(reverse_margin_edge_bps)s,
            %(reverse_open_coverage)s, %(spot_spread_bps)s, %(future_spread_bps)s,
            %(spot_top_bid_usdt)s, %(future_top_ask_usdt)s,
            %(spot_quote_volume_24h)s, %(future_volume_24h_settle)s, %(reverse_status)s
        )
    """
    params = [_snapshot_params(row, snapshot_time, sample_source) for row in selected]
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, params)
        conn.commit()
    return len(params)


def _serialize_dt(value) -> Optional[str]:
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value) if value else None


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in row.items():
        if hasattr(value, 'strftime'):
            out[key] = _serialize_dt(value)
        elif hasattr(value, '__float__') and not isinstance(value, (str, bytes)):
            try:
                out[key] = float(value)
            except Exception:
                out[key] = value
        else:
            out[key] = value
    return out


def _latest_rows(hours: int) -> List[Dict[str, Any]]:
    ensure_reverse_research_tables()
    sql = """
        SELECT s.*
        FROM mi_reverse_research_snapshot s
        INNER JOIN (
            SELECT base_asset, MAX(snapshot_time) AS max_time
            FROM mi_reverse_research_snapshot
            WHERE snapshot_time >= DATE_SUB(NOW(), INTERVAL %s HOUR)
            GROUP BY base_asset
        ) x
          ON x.base_asset = s.base_asset
         AND x.max_time = s.snapshot_time
        ORDER BY s.funding_rate_24h ASC, s.base_asset ASC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (hours,))
        return list(cursor.fetchall() or [])


def _history_rows(hours: int) -> List[Dict[str, Any]]:
    sql = """
        SELECT base_asset, snapshot_time, max_borrowable_amount, borrow_capacity_usdt,
               funding_rate_24h, reverse_open_basis_bps, reverse_margin_edge_bps,
               reverse_status
        FROM mi_reverse_research_snapshot
        WHERE snapshot_time >= DATE_SUB(NOW(), INTERVAL %s HOUR)
        ORDER BY base_asset ASC, snapshot_time ASC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (hours,))
        return list(cursor.fetchall() or [])


def _pct_change(new_value: Optional[float], old_value: Optional[float]) -> Optional[float]:
    if new_value is None or old_value is None or old_value <= 0:
        return None
    return (new_value - old_value) / old_value * 100.0


def _attach_drain(latest: List[Dict[str, Any]], history: List[Dict[str, Any]]) -> None:
    by_asset: Dict[str, List[Dict[str, Any]]] = {}
    for row in history:
        by_asset.setdefault(str(row.get('base_asset') or ''), []).append(row)

    for row in latest:
        asset = str(row.get('base_asset') or '')
        samples = by_asset.get(asset, [])
        latest_time = row.get('snapshot_time')
        if not isinstance(latest_time, datetime):
            continue
        current = _as_float(row.get('max_borrowable_amount'))
        for minutes in (5, 15):
            cutoff = latest_time - timedelta(minutes=minutes)
            prior = None
            for sample in samples:
                sample_time = sample.get('snapshot_time')
                if isinstance(sample_time, datetime) and sample_time <= cutoff:
                    prior = sample
                elif isinstance(sample_time, datetime) and sample_time > cutoff:
                    break
            old_value = _as_float(prior.get('max_borrowable_amount')) if prior else None
            row[f'borrow_change_{minutes}m_pct'] = _pct_change(current, old_value)


def _summary(latest: List[Dict[str, Any]], open_amount_usdt: float) -> Dict[str, Any]:
    total = len(latest)
    borrowable = sum(1 for r in latest if r.get('borrowable') == 1 and (_as_float(r.get('borrow_capacity_usdt'), 0) or 0) >= open_amount_usdt)
    zero = sum(1 for r in latest if (_as_float(r.get('max_borrowable_amount'), 0) or 0) <= 0)
    negative = sum(1 for r in latest if (_as_float(r.get('funding_rate_24h'), 0) or 0) < 0)
    candidates = sum(1 for r in latest if r.get('reverse_status') == 'candidate')
    drains = sum(
        1
        for r in latest
        if (_as_float(r.get('borrow_change_15m_pct')) is not None and (_as_float(r.get('borrow_change_15m_pct')) or 0) <= -50)
    )
    latest_time = max((r.get('snapshot_time') for r in latest if isinstance(r.get('snapshot_time'), datetime)), default=None)
    return {
        'asset_count': total,
        'borrowable_count': borrowable,
        'zero_borrow_count': zero,
        'negative_funding_count': negative,
        'candidate_count': candidates,
        'drain_15m_count': drains,
        'latest_snapshot_time': _serialize_dt(latest_time),
    }


def get_reverse_research_analysis(
    *,
    hours: int = 24,
    limit: int = 100,
    open_amount_usdt: float = 10.0,
) -> Dict[str, Any]:
    hours = min(max(int(hours or 24), 1), 168)
    limit = min(max(int(limit or 100), 1), 500)
    latest = _latest_rows(hours)
    history = _history_rows(max(1, min(hours, 2)))
    _attach_drain(latest, history)

    top_negative = sorted(
        latest,
        key=lambda r: _as_float(r.get('funding_rate_24h'), 999.0) or 999.0,
    )[:limit]
    top_drain = sorted(
        [r for r in latest if _as_float(r.get('borrow_change_15m_pct')) is not None],
        key=lambda r: _as_float(r.get('borrow_change_15m_pct'), 999.0) or 999.0,
    )[:limit]
    top_candidates = [
        r for r in sorted(
            latest,
            key=lambda r: _as_float(r.get('reverse_margin_edge_bps'), -999999.0) or -999999.0,
            reverse=True,
        )
        if r.get('reverse_status') in {'candidate', 'borrow_unavailable', 'borrow_capacity_low'}
    ][:limit]

    return {
        'hours': hours,
        'summary': _summary(latest, open_amount_usdt),
        'top_negative_funding': [_serialize_row(r) for r in top_negative],
        'top_borrow_drain': [_serialize_row(r) for r in top_drain],
        'top_candidates': [_serialize_row(r) for r in top_candidates],
    }
