# coding: utf-8
"""
Reverse arbitrage borrow research snapshots and lightweight analysis.

Only borrow-side observations are persisted here. Funding and VWAP/basis
context is joined from the existing market data tables at query time so we do
not duplicate high-frequency samples already collected elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

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


def _serialize_dt(value) -> Optional[str]:
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value) if value else None


def _minutes_until(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        target = value
    else:
        text = str(value).strip()
        if not text:
            return None
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
            try:
                target = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                target = None
        if target is None:
            return None
    return (target - datetime.now()).total_seconds() / 60.0


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


def ensure_reverse_research_tables() -> None:
    sql = """
        CREATE TABLE IF NOT EXISTS mi_reverse_research_snapshot (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            snapshot_time DATETIME NOT NULL,
            base_asset VARCHAR(32) NOT NULL,
            contract VARCHAR(64) DEFAULT NULL,
            symbol VARCHAR(64) DEFAULT NULL,
            sample_source VARCHAR(32) NOT NULL DEFAULT 'loop',
            borrowable TINYINT DEFAULT NULL,
            max_borrowable_amount DECIMAL(28,12) DEFAULT NULL,
            account_borrow_limit DECIMAL(28,12) DEFAULT NULL,
            borrow_capacity_usdt DECIMAL(20,4) DEFAULT NULL,
            borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
            borrow_24h_bps DECIMAL(12,4) DEFAULT NULL,
            borrow_unavailable_reason VARCHAR(128) DEFAULT NULL,
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
        'borrowable': _as_bool_int(row.get('reverse_borrowable')),
        'max_borrowable_amount': _as_float(
            row.get('reverse_max_borrowable_amount'),
            _as_float(row.get('reverse_borrow_limit')),
        ),
        'account_borrow_limit': _as_float(row.get('reverse_account_borrow_limit')),
        'borrow_capacity_usdt': _as_float(row.get('reverse_borrow_capacity_usdt')),
        'borrow_hourly_rate': _as_float(row.get('reverse_borrow_hourly_rate')),
        'borrow_24h_bps': _as_float(row.get('reverse_borrow_24h_bps')),
        'borrow_unavailable_reason': row.get('borrow_unavailable_reason')
        or row.get('reverse_borrow_unavailable_reason'),
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
            borrowable, max_borrowable_amount, account_borrow_limit,
            borrow_capacity_usdt, borrow_hourly_rate, borrow_24h_bps,
            borrow_unavailable_reason, reverse_status
        ) VALUES (
            %(snapshot_time)s, %(base_asset)s, %(contract)s, %(symbol)s, %(sample_source)s,
            %(borrowable)s, %(max_borrowable_amount)s, %(account_borrow_limit)s,
            %(borrow_capacity_usdt)s, %(borrow_hourly_rate)s, %(borrow_24h_bps)s,
            %(borrow_unavailable_reason)s, %(reverse_status)s
        )
    """
    params = [_snapshot_params(row, snapshot_time, sample_source) for row in selected]
    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, params)
        conn.commit()
    return len(params)


def _latest_rows(hours: int) -> List[Dict[str, Any]]:
    ensure_reverse_research_tables()
    vwap_hours = max(1, min(int(hours or 24), 2))
    sql = """
        SELECT s.*,
               fut.funding_rate_24h,
               fut.funding_next_apply AS next_funding_time,
               fut.volume_24h_settle AS future_volume_24h_settle,
               spot.quote_volume AS spot_quote_volume_24h,
               v.close_vwap_basis_bps AS reverse_open_basis_bps,
               v.open_vwap_basis_bps AS reverse_close_basis_bps,
               v.close_coverage AS reverse_open_coverage,
               v.snapshot_time AS vwap_snapshot_time,
               th.reverse_open_basis_p20,
               th.reverse_close_basis_p20
        FROM mi_reverse_research_snapshot s
        INNER JOIN (
            SELECT base_asset, MAX(snapshot_time) AS max_time
            FROM mi_reverse_research_snapshot
            WHERE snapshot_time >= DATE_SUB(NOW(), INTERVAL %s HOUR)
            GROUP BY base_asset
        ) x
          ON x.base_asset = s.base_asset
         AND x.max_time = s.snapshot_time
        LEFT JOIN mi_gate_future_contracts fut
          ON fut.base_asset = s.base_asset
         AND fut.type = 'direct'
        LEFT JOIN mi_binance_spot_info spot
          ON spot.base_asset = s.base_asset
        LEFT JOIN (
            SELECT v1.base_asset, v1.snapshot_time, v1.open_vwap_basis_bps,
                   v1.close_vwap_basis_bps, v1.open_coverage, v1.close_coverage
            FROM mi_vwap_basis_snapshot v1
            INNER JOIN (
                SELECT base_asset, MAX(snapshot_time) AS max_time
                FROM mi_vwap_basis_snapshot
                WHERE snapshot_time >= DATE_SUB(NOW(), INTERVAL %s HOUR)
                GROUP BY base_asset
            ) vx
              ON vx.base_asset = v1.base_asset
             AND vx.max_time = v1.snapshot_time
        ) v
          ON v.base_asset = s.base_asset
        LEFT JOIN mi_vwap_basis_threshold th
          ON th.base_asset = s.base_asset
         AND th.calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
        ORDER BY fut.funding_rate_24h ASC, s.base_asset ASC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (hours, vwap_hours))
        return list(cursor.fetchall() or [])


def _history_rows(hours: int) -> List[Dict[str, Any]]:
    sql = """
        SELECT base_asset, snapshot_time, max_borrowable_amount, borrow_capacity_usdt,
               borrowable, reverse_status
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


def _attach_market_metrics(
    latest: List[Dict[str, Any]],
    funding_capture_ratio: float,
    fee_cost_bps: float,
) -> None:
    for row in latest:
        funding_rate = _as_float(row.get('funding_rate_24h'))
        gross = abs(funding_rate) * 10000.0 if funding_rate is not None and funding_rate < 0 else 0.0
        expected = gross * funding_capture_ratio
        borrow_24h = _as_float(row.get('borrow_24h_bps'))
        reverse_open = _as_float(row.get('reverse_open_basis_bps'))
        open_p20 = _as_float(row.get('reverse_open_basis_p20'))
        close_p20 = _as_float(row.get('reverse_close_basis_p20'))
        edge_p20 = min(open_p20, close_p20) if open_p20 is not None and close_p20 is not None else None

        margin_edge = None
        if borrow_24h is not None and reverse_open is not None and edge_p20 is not None:
            margin_edge = expected + edge_p20 - reverse_open - borrow_24h - fee_cost_bps

        row['gross_funding_bps'] = round(gross, 4)
        row['expected_funding_bps'] = round(expected, 4)
        row['next_funding_min'] = _minutes_until(row.get('next_funding_time'))
        row['reverse_p20_edge_bps'] = round(edge_p20, 4) if edge_p20 is not None else None
        row['reverse_margin_edge_bps'] = round(margin_edge, 4) if margin_edge is not None else None


def _summary(latest: List[Dict[str, Any]], open_amount_usdt: float) -> Dict[str, Any]:
    total = len(latest)
    borrowable = sum(
        1 for r in latest
        if r.get('borrowable') == 1 and (_as_float(r.get('borrow_capacity_usdt'), 0) or 0) >= open_amount_usdt
    )
    zero = sum(1 for r in latest if (_as_float(r.get('max_borrowable_amount'), 0) or 0) <= 0)
    negative = sum(1 for r in latest if (_as_float(r.get('funding_rate_24h'), 0) or 0) < 0)
    candidates = sum(1 for r in latest if r.get('reverse_status') == 'candidate')
    drains = sum(
        1 for r in latest
        if (_as_float(r.get('borrow_change_15m_pct')) is not None
            and (_as_float(r.get('borrow_change_15m_pct')) or 0) <= -50)
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
    funding_capture_ratio: float = 0.5,
    fee_cost_bps: float = 0.0,
) -> Dict[str, Any]:
    hours = min(max(int(hours or 24), 1), 168)
    limit = min(max(int(limit or 100), 1), 500)
    latest = _latest_rows(hours)
    history = _history_rows(max(1, min(hours, 2)))
    _attach_drain(latest, history)
    _attach_market_metrics(latest, funding_capture_ratio, fee_cost_bps)

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
