# coding: utf-8
"""Funding-only prediction helpers for reverse pre-borrow observation.

This module deliberately ignores borrow availability. Borrow state is a real-time
gate for pre-borrowing, while this predictor only estimates future high-negative
funding probability from Gate funding history.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common.database import db_manager


DEFAULT_THRESHOLD_RATE = -0.01
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_PAGE_SIZE = 100
MIN_CONDITIONAL_SAMPLES = 20


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _serialize_dt(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def _rate_bucket(rate: Optional[float], threshold: float) -> str:
    if rate is None:
        return 'unknown'
    if rate <= threshold:
        return 'high_negative'
    if rate <= -0.005:
        return 'mid_negative'
    if rate <= -0.003:
        return 'watch_negative'
    if rate <= -0.001:
        return 'light_negative'
    if rate < 0:
        return 'near_zero_negative'
    return 'non_negative'


def _bucket_label(bucket: str) -> str:
    return {
        'high_negative': '已高负',
        'mid_negative': '中高负',
        'watch_negative': '观察负',
        'light_negative': '轻微负',
        'near_zero_negative': '近零负',
        'non_negative': '非负',
        'unknown': '未知',
    }.get(bucket, bucket)


def _hit_within(rates: List[float], start_index: int, horizon: int, threshold: float) -> bool:
    end = min(len(rates), start_index + horizon + 1)
    return any(rate <= threshold for rate in rates[start_index + 1:end])


def _probability(hits: int, total: int) -> Optional[float]:
    if total <= 0:
        return None
    return hits / total


def _blend_probability(base: Optional[float], conditional: Optional[float], conditional_samples: int) -> Optional[float]:
    if conditional is None:
        return base
    if base is None:
        return conditional
    weight = min(1.0, conditional_samples / float(MIN_CONDITIONAL_SAMPLES))
    return conditional * weight + base * (1.0 - weight)


def _round_or_none(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def _load_current_contracts() -> Dict[str, Dict[str, Any]]:
    sql = """
        SELECT
            name AS contract,
            base_asset,
            funding_rate_24h,
            funding_next_apply,
            updated_at
        FROM mi_gate_future_contracts
        WHERE type = 'direct'
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall() or []
    return {str(row.get('contract') or ''): row for row in rows if row.get('contract')}


def _load_funding_history(lookback_days: int) -> List[Dict[str, Any]]:
    sql = """
        SELECT contract, funding_rate_24h, record_time
        FROM mi_gate_future_his_funding_rates
        WHERE record_time >= DATE_SUB(NOW(), INTERVAL %s DAY)
          AND funding_rate_24h IS NOT NULL
        ORDER BY contract ASC, record_time ASC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (lookback_days,))
        return list(cursor.fetchall() or [])


def _group_history(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Tuple[datetime, float]]]:
    grouped: Dict[str, List[Tuple[datetime, float]]] = defaultdict(list)
    for row in rows:
        contract = str(row.get('contract') or '')
        rate = _as_float(row.get('funding_rate_24h'))
        record_time = row.get('record_time')
        if not contract or rate is None or record_time is None:
            continue
        grouped[contract].append((record_time, rate))
    return grouped


def _contract_base_asset(contract: str, current: Optional[Dict[str, Any]]) -> str:
    if current and current.get('base_asset'):
        return str(current['base_asset']).upper()
    return contract.split('_', 1)[0].upper()


def _compute_prediction_row(
    contract: str,
    history: List[Tuple[datetime, float]],
    current: Optional[Dict[str, Any]],
    threshold_rate: float,
) -> Optional[Dict[str, Any]]:
    if len(history) < 2:
        return None

    rates = [rate for _, rate in history]
    times = [record_time for record_time, _ in history]
    current_rate = _as_float(current.get('funding_rate_24h')) if current else None
    if current_rate is None:
        current_rate = rates[-1]
    current_bucket = _rate_bucket(current_rate, threshold_rate)

    high_count = sum(1 for rate in rates if rate <= threshold_rate)
    negative_count = sum(1 for rate in rates if rate < 0)
    min_rate = min(rates)
    max_rate = max(rates)
    avg_rate = sum(rates) / len(rates)
    last_high_time = None
    for record_time, rate in reversed(history):
        if rate <= threshold_rate:
            last_high_time = record_time
            break

    predictions: Dict[str, Any] = {}
    for horizon in (1, 2, 3):
        base_total = max(0, len(rates) - horizon)
        base_hits = sum(
            1
            for idx in range(base_total)
            if _hit_within(rates, idx, horizon, threshold_rate)
        )
        cond_indices = [
            idx
            for idx in range(base_total)
            if _rate_bucket(rates[idx], threshold_rate) == current_bucket
        ]
        cond_hits = sum(
            1
            for idx in cond_indices
            if _hit_within(rates, idx, horizon, threshold_rate)
        )
        base_prob = _probability(base_hits, base_total)
        cond_prob = _probability(cond_hits, len(cond_indices))
        final_prob = _blend_probability(base_prob, cond_prob, len(cond_indices))
        predictions[f'p_next_{horizon}'] = _round_or_none(final_prob, 6)
        predictions[f'base_p_next_{horizon}'] = _round_or_none(base_prob, 6)
        predictions[f'conditional_p_next_{horizon}'] = _round_or_none(cond_prob, 6)

    conditional_samples = sum(1 for rate in rates[:-1] if _rate_bucket(rate, threshold_rate) == current_bucket)
    confidence = min(1.0, len(rates) / 100.0) * min(1.0, max(conditional_samples, 1) / MIN_CONDITIONAL_SAMPLES)

    previous_rate = rates[-2] if len(rates) >= 2 else None
    current_updated_at = current.get('updated_at') if current else None
    return {
        'base_asset': _contract_base_asset(contract, current),
        'contract': contract,
        'current_funding_rate_24h': _round_or_none(current_rate, 10),
        'previous_funding_rate_24h': _round_or_none(previous_rate, 10),
        'funding_rate_change': _round_or_none(
            current_rate - previous_rate if current_rate is not None and previous_rate is not None else None,
            10,
        ),
        'current_bucket': current_bucket,
        'current_bucket_label': _bucket_label(current_bucket),
        'threshold_rate': threshold_rate,
        'sample_count': len(rates),
        'conditional_sample_count': conditional_samples,
        'high_negative_count': high_count,
        'high_negative_frequency': _round_or_none(high_count / len(rates), 6),
        'negative_count': negative_count,
        'negative_frequency': _round_or_none(negative_count / len(rates), 6),
        'min_funding_rate_24h': _round_or_none(min_rate, 10),
        'max_funding_rate_24h': _round_or_none(max_rate, 10),
        'avg_funding_rate_24h': _round_or_none(avg_rate, 10),
        'last_history_time': _serialize_dt(times[-1]),
        'last_high_negative_time': _serialize_dt(last_high_time),
        'funding_next_apply': _serialize_dt(current.get('funding_next_apply')) if current else None,
        'current_updated_at': _serialize_dt(current_updated_at),
        'confidence': _round_or_none(confidence, 6),
        **predictions,
    }


def get_reverse_funding_prediction_page(
    *,
    threshold_rate: float = DEFAULT_THRESHOLD_RATE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    keyword: str = '',
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Dict[str, Any]:
    threshold_rate = float(threshold_rate or DEFAULT_THRESHOLD_RATE)
    # This page is for high-negative funding. If the UI sends +1%, normalize to -1%.
    if threshold_rate > 0:
        threshold_rate = -threshold_rate
    lookback_days = min(max(int(lookback_days or DEFAULT_LOOKBACK_DAYS), 3), 90)
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or DEFAULT_PAGE_SIZE), 10), 5000)
    keyword = str(keyword or '').strip().upper()

    current_by_contract = _load_current_contracts()
    history_by_contract = _group_history(_load_funding_history(lookback_days))

    rows: List[Dict[str, Any]] = []
    for contract, history in history_by_contract.items():
        current = current_by_contract.get(contract)
        row = _compute_prediction_row(contract, history, current, threshold_rate)
        if not row:
            continue
        if keyword and keyword not in row['base_asset'] and keyword not in row['contract']:
            continue
        rows.append(row)

    rows.sort(
        key=lambda item: (
            item.get('p_next_3') or 0.0,
            item.get('p_next_2') or 0.0,
            item.get('p_next_1') or 0.0,
            item.get('high_negative_frequency') or 0.0,
        ),
        reverse=True,
    )

    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    page_rows = rows[start:end]

    latest_history_time = max((row.get('last_history_time') for row in rows if row.get('last_history_time')), default=None)
    current_high_count = sum(1 for row in rows if (row.get('current_funding_rate_24h') or 0) <= threshold_rate)
    summary = {
        'asset_count': total,
        'current_high_negative_count': current_high_count,
        'threshold_rate': threshold_rate,
        'lookback_days': lookback_days,
        'latest_history_time': latest_history_time,
        'avg_p_next_1': _round_or_none(
            sum((row.get('p_next_1') or 0.0) for row in rows) / total if total else None,
            6,
        ),
        'avg_p_next_2': _round_or_none(
            sum((row.get('p_next_2') or 0.0) for row in rows) / total if total else None,
            6,
        ),
        'avg_p_next_3': _round_or_none(
            sum((row.get('p_next_3') or 0.0) for row in rows) / total if total else None,
            6,
        ),
    }
    return {
        'summary': summary,
        'rows': page_rows,
        'pagination': {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size if page_size else 1,
        },
    }
