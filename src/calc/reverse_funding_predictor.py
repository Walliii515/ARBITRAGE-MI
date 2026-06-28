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

from common.config import config
from common.database import db_manager
from common.logger import get_logger


DEFAULT_THRESHOLD_RATE = -0.006
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_PAGE_SIZE = 100
MIN_CONDITIONAL_SAMPLES = 20
MODEL_VERSION = 'funding_follow_v1'
DEFAULT_MIN_P_NEXT_2 = 0.20
DEFAULT_MIN_P_NEXT_3 = 0.25
DEFAULT_MIN_CONFIDENCE = 0.50
DEFAULT_BORROW_COST_RATIO = 1.0
DEFAULT_MIN_FUNDING_DROP_BPS = 5.0
DEFAULT_MIN_BORROW_DROP_PCT = 20.0
DEFAULT_MIN_FOLLOW_SCORE = 50.0

logger = get_logger(__name__)


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _normalize_threshold_rate(threshold_rate: float) -> float:
    threshold = float(threshold_rate or DEFAULT_THRESHOLD_RATE)
    # This page is for high-negative funding. If the UI sends +1%, normalize to -1%.
    if threshold > 0:
        threshold = -threshold
    return max(-1.0, min(0.0, threshold))


def _normalize_lookback_days(lookback_days: int) -> int:
    return min(max(int(lookback_days or DEFAULT_LOOKBACK_DAYS), 3), 90)


def _like_keyword(keyword: str) -> str:
    return f"%{str(keyword or '').strip().upper()}%"


def _allowed_strategy_tiers() -> List[str]:
    raw = config.get('orderbook.strategy_tiers', ['A', 'B'])
    if isinstance(raw, str):
        tiers = [part.strip().upper() for part in raw.split(',')]
    elif isinstance(raw, (list, tuple, set)):
        tiers = [str(part).strip().upper() for part in raw]
    else:
        tiers = ['A', 'B']
    tiers = [tier for tier in tiers if tier in ('A', 'B', 'C')]
    return tiers or ['A', 'B']


def _load_prediction_universe() -> Dict[str, str]:
    """Load the same asset universe used by orderbook subscriptions."""
    allowed_tiers = _allowed_strategy_tiers()
    tier_placeholders = ', '.join(['%s'] * len(allowed_tiers))
    max_contracts = config.get_int('orderbook.max_contracts', 999)
    settle = str(config.get('orderbook.settle', 'usdt') or 'usdt').upper()
    min_spot_volume = config.get_float('trade.filter.min_spot_volume_24h_usdt', 0)
    min_future_volume = config.get_float('trade.filter.min_future_volume_24h_usdt', 0)
    sql = """
        SELECT
            UPPER(TRIM(b.base_asset)) AS base_asset,
            COALESCE(b.strategy_tier, 'C') AS strategy_tier
        FROM mi_base_asset b
        INNER JOIN mi_gate_future_contracts g
            ON g.base_asset = UPPER(TRIM(b.base_asset))
           AND g.name = CONCAT(UPPER(TRIM(b.base_asset)), %s)
        INNER JOIN mi_binance_spot_info s
            ON s.base_asset = UPPER(TRIM(b.base_asset))
           AND s.symbol = CONCAT(UPPER(TRIM(b.base_asset)), %s)
        WHERE b.is_valid = 'Y'
          AND COALESCE(b.strategy_tier, 'C') IN ({tier_placeholders})
          AND g.status = 'trading'
          AND s.status = 'TRADING'
          AND s.is_spot_trading_allowed = 1
          AND UPPER(TRIM(b.base_asset)) REGEXP '^[A-Z0-9]+$'
          AND COALESCE(g.volume_24h_settle, 0) >= %s
          AND COALESCE(s.quote_volume, 0) >= %s
        ORDER BY g.funding_rate_24h DESC, g.volume_24h_settle DESC, s.quote_volume DESC
        LIMIT %s
    """.format(tier_placeholders=tier_placeholders)
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            sql,
            (
                f'_{settle}',
                settle,
                *allowed_tiers,
                min_future_volume,
                min_spot_volume,
                max_contracts,
            ),
        )
        rows = cursor.fetchall() or []
    return {
        str(row.get('base_asset') or '').strip().upper(): str(row.get('strategy_tier') or 'C').strip().upper()
        for row in rows
        if row.get('base_asset')
    }


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


def _load_latest_borrow_meta() -> Dict[str, Dict[str, Any]]:
    sql = """
        SELECT s.base_asset, s.snapshot_time, s.borrowable, s.borrow_capacity_usdt,
               s.borrow_hourly_rate, s.borrow_24h_bps, s.max_borrowable_amount
        FROM mi_reverse_research_snapshot s
        INNER JOIN (
            SELECT base_asset, MAX(snapshot_time) AS max_time
            FROM mi_reverse_research_snapshot
            GROUP BY base_asset
        ) x
          ON x.base_asset = s.base_asset
         AND x.max_time = s.snapshot_time
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall() or []
    return {
        str(row.get('base_asset') or '').strip().upper(): row
        for row in rows
        if row.get('base_asset')
    }


def _load_borrow_history(hours: int = 24) -> Dict[str, List[Dict[str, Any]]]:
    sql = """
        SELECT base_asset, snapshot_time, borrowable, borrow_capacity_usdt,
               borrow_hourly_rate, borrow_24h_bps, max_borrowable_amount
        FROM mi_reverse_research_snapshot
        WHERE snapshot_time >= DATE_SUB(NOW(), INTERVAL %s HOUR)
        ORDER BY base_asset ASC, snapshot_time ASC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (hours,))
        rows = cursor.fetchall() or []
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        base_asset = str(row.get('base_asset') or '').strip().upper()
        if base_asset:
            grouped[base_asset].append(row)
    return grouped


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


def _expected_funding_bps(current_funding_rate_24h: Optional[float]) -> float:
    rate = _as_float(current_funding_rate_24h, 0.0) or 0.0
    if rate >= 0:
        return 0.0
    capture_ratio = config.get_float('reverse_arbitrage.funding_capture_ratio', 0.5)
    return abs(rate) * 10000.0 * capture_ratio


def _value_at_or_before(
    series: List[Tuple[datetime, Optional[float]]],
    current_time: datetime,
    hours: int,
) -> Optional[float]:
    if not series:
        return None
    target = current_time.timestamp() - hours * 3600
    fallback = series[0][1]
    chosen = None
    for item_time, value in series:
        if value is None:
            continue
        if item_time.timestamp() <= target:
            chosen = value
        else:
            break
    return chosen if chosen is not None else fallback


def _rate_delta_bps(history: List[Tuple[datetime, float]], current_rate: Optional[float], hours: int) -> Optional[float]:
    if not history or current_rate is None:
        return None
    current_time = history[-1][0]
    ref_rate = _value_at_or_before([(item_time, rate) for item_time, rate in history], current_time, hours)
    if ref_rate is None:
        return None
    return (current_rate - ref_rate) * 10000.0


def _capacity_drop_pct(history: List[Dict[str, Any]], hours: int) -> Optional[float]:
    points = [
        (row.get('snapshot_time'), _as_float(row.get('borrow_capacity_usdt')))
        for row in history
        if row.get('snapshot_time') is not None
    ]
    points = [(item_time, value) for item_time, value in points if isinstance(item_time, datetime)]
    if not points:
        return None
    current_time, current_value = points[-1]
    current_value = current_value or 0.0
    ref_value = _value_at_or_before(points, current_time, hours)
    if ref_value is None or ref_value <= 0:
        return None
    return max(0.0, (ref_value - current_value) / ref_value * 100.0)


def _score_follow_signal(row: Dict[str, Any]) -> Tuple[float, str]:
    current_rate = _as_float(row.get('current_funding_rate_24h'), 0.0) or 0.0
    funding_drop_1h = max(0.0, -(_as_float(row.get('funding_change_1h_bps'), 0.0) or 0.0))
    funding_drop_4h = max(0.0, -(_as_float(row.get('funding_change_4h_bps'), 0.0) or 0.0))
    funding_drop_12h = max(0.0, -(_as_float(row.get('funding_change_12h_bps'), 0.0) or 0.0))
    borrow_drop_1h = _as_float(row.get('borrow_capacity_drop_1h_pct'), 0.0) or 0.0
    borrow_drop_4h = _as_float(row.get('borrow_capacity_drop_4h_pct'), 0.0) or 0.0
    borrow_drop_12h = _as_float(row.get('borrow_capacity_drop_12h_pct'), 0.0) or 0.0
    max_borrow_drop = max(borrow_drop_1h, borrow_drop_4h, borrow_drop_12h)
    history_freq = _as_float(row.get('high_negative_frequency'), 0.0) or 0.0
    history_count = _as_float(row.get('high_negative_count'), 0.0) or 0.0
    borrowable = int(row.get('borrowable') or 0) == 1
    capacity = _as_float(row.get('borrow_capacity_usdt'), 0.0) or 0.0
    borrow_cost = _as_float(row.get('borrow_24h_bps'), 0.0) or 0.0

    funding_trend_score = min(35.0, funding_drop_1h * 1.2 + funding_drop_4h * 0.9 + funding_drop_12h * 0.45)
    borrow_drop_score = min(30.0, max_borrow_drop * 0.30)
    current_negative_score = min(15.0, max(0.0, -current_rate * 10000.0) * 0.4)
    history_score = min(15.0, history_freq * 80.0 + min(history_count, 10.0) * 0.7)
    borrow_state_score = (8.0 if borrowable else 0.0) + min(5.0, capacity / 100.0)
    cost_penalty = min(18.0, borrow_cost * 0.25)
    score = max(0.0, funding_trend_score + borrow_drop_score + current_negative_score + history_score + borrow_state_score - cost_penalty)

    parts = []
    if funding_drop_4h > 0 or funding_drop_12h > 0:
        parts.append(f"资金费下行(4h={funding_drop_4h:.1f}bps,12h={funding_drop_12h:.1f}bps)")
    if max_borrow_drop > 0:
        parts.append(f"额度下降(max={max_borrow_drop:.1f}%)")
    if borrowable:
        parts.append(f"当前可借({capacity:.0f}U)")
    if history_count > 0:
        parts.append(f"历史高负{int(history_count)}次")
    if borrow_cost > 0:
        parts.append(f"借币成本{borrow_cost:.1f}bps")
    return round(score, 2), '|'.join(parts) or '暂无明显跟随信号'


def _attach_borrow_metrics(row: Dict[str, Any], borrow: Optional[Dict[str, Any]]) -> None:
    borrow = borrow or {}
    expected_bps = _expected_funding_bps(_as_float(row.get('current_funding_rate_24h')))
    row['expected_funding_bps'] = _round_or_none(expected_bps, 4)
    row['borrowable'] = borrow.get('borrowable')
    row['borrow_capacity_usdt'] = _round_or_none(_as_float(borrow.get('borrow_capacity_usdt')), 4)
    row['borrow_hourly_rate'] = _round_or_none(_as_float(borrow.get('borrow_hourly_rate')), 10)
    row['borrow_24h_bps'] = _round_or_none(_as_float(borrow.get('borrow_24h_bps')), 4)
    row['max_borrowable_amount'] = _round_or_none(_as_float(borrow.get('max_borrowable_amount')), 8)
    row['borrow_snapshot_time'] = _serialize_dt(borrow.get('snapshot_time'))


def _attach_follow_metrics(row: Dict[str, Any], history: List[Tuple[datetime, float]], borrow_history: List[Dict[str, Any]]) -> None:
    current_rate = _as_float(row.get('current_funding_rate_24h'))
    row['funding_change_1h_bps'] = _round_or_none(_rate_delta_bps(history, current_rate, 1), 4)
    row['funding_change_4h_bps'] = _round_or_none(_rate_delta_bps(history, current_rate, 4), 4)
    row['funding_change_12h_bps'] = _round_or_none(_rate_delta_bps(history, current_rate, 12), 4)
    row['borrow_capacity_drop_1h_pct'] = _round_or_none(_capacity_drop_pct(borrow_history, 1), 4)
    row['borrow_capacity_drop_4h_pct'] = _round_or_none(_capacity_drop_pct(borrow_history, 4), 4)
    row['borrow_capacity_drop_12h_pct'] = _round_or_none(_capacity_drop_pct(borrow_history, 12), 4)
    row['borrow_capacity_drop_max_pct'] = _round_or_none(max(
        _as_float(row.get('borrow_capacity_drop_1h_pct'), 0.0) or 0.0,
        _as_float(row.get('borrow_capacity_drop_4h_pct'), 0.0) or 0.0,
        _as_float(row.get('borrow_capacity_drop_12h_pct'), 0.0) or 0.0,
    ), 4)
    score, reason = _score_follow_signal(row)
    row['follow_score'] = score
    row['follow_reason'] = reason


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
        'model_version': MODEL_VERSION,
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


def ensure_reverse_funding_prediction_table() -> None:
    sql = """
        CREATE TABLE IF NOT EXISTS mi_reverse_funding_prediction (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            generated_at DATETIME NOT NULL,
            model_version VARCHAR(64) NOT NULL,
            threshold_rate DECIMAL(18,10) NOT NULL,
            lookback_days INT NOT NULL,
            base_asset VARCHAR(32) NOT NULL,
            contract VARCHAR(64) NOT NULL,
            strategy_tier VARCHAR(8) DEFAULT NULL,
            expected_funding_bps DECIMAL(12,4) DEFAULT NULL,
            borrowable TINYINT DEFAULT NULL,
            borrow_capacity_usdt DECIMAL(20,4) DEFAULT NULL,
            borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
            borrow_24h_bps DECIMAL(12,4) DEFAULT NULL,
            max_borrowable_amount DECIMAL(28,8) DEFAULT NULL,
            borrow_snapshot_time DATETIME DEFAULT NULL,
            follow_score DECIMAL(12,4) DEFAULT NULL,
            follow_reason VARCHAR(512) DEFAULT NULL,
            funding_change_1h_bps DECIMAL(12,4) DEFAULT NULL,
            funding_change_4h_bps DECIMAL(12,4) DEFAULT NULL,
            funding_change_12h_bps DECIMAL(12,4) DEFAULT NULL,
            borrow_capacity_drop_1h_pct DECIMAL(12,4) DEFAULT NULL,
            borrow_capacity_drop_4h_pct DECIMAL(12,4) DEFAULT NULL,
            borrow_capacity_drop_12h_pct DECIMAL(12,4) DEFAULT NULL,
            borrow_capacity_drop_max_pct DECIMAL(12,4) DEFAULT NULL,
            current_funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
            previous_funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
            funding_rate_change DECIMAL(18,10) DEFAULT NULL,
            current_bucket VARCHAR(32) DEFAULT NULL,
            current_bucket_label VARCHAR(32) DEFAULT NULL,
            sample_count INT DEFAULT NULL,
            conditional_sample_count INT DEFAULT NULL,
            high_negative_count INT DEFAULT NULL,
            high_negative_frequency DECIMAL(12,6) DEFAULT NULL,
            negative_count INT DEFAULT NULL,
            negative_frequency DECIMAL(12,6) DEFAULT NULL,
            min_funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
            max_funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
            avg_funding_rate_24h DECIMAL(18,10) DEFAULT NULL,
            p_next_1 DECIMAL(12,6) DEFAULT NULL,
            p_next_2 DECIMAL(12,6) DEFAULT NULL,
            p_next_3 DECIMAL(12,6) DEFAULT NULL,
            base_p_next_1 DECIMAL(12,6) DEFAULT NULL,
            base_p_next_2 DECIMAL(12,6) DEFAULT NULL,
            base_p_next_3 DECIMAL(12,6) DEFAULT NULL,
            conditional_p_next_1 DECIMAL(12,6) DEFAULT NULL,
            conditional_p_next_2 DECIMAL(12,6) DEFAULT NULL,
            conditional_p_next_3 DECIMAL(12,6) DEFAULT NULL,
            confidence DECIMAL(12,6) DEFAULT NULL,
            last_history_time DATETIME DEFAULT NULL,
            last_high_negative_time DATETIME DEFAULT NULL,
            funding_next_apply DATETIME DEFAULT NULL,
            current_updated_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            KEY idx_generated (generated_at),
            KEY idx_model_latest (threshold_rate, lookback_days, generated_at),
            KEY idx_asset_generated (base_asset, generated_at),
            KEY idx_contract_generated (contract, generated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        _ensure_prediction_columns(cursor)


def _ensure_prediction_columns(cursor) -> None:
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'mi_reverse_funding_prediction'
        """
    )
    existing = {str(row.get('COLUMN_NAME') or '') for row in (cursor.fetchall() or [])}
    alters = {
        'strategy_tier': "ADD COLUMN strategy_tier VARCHAR(8) DEFAULT NULL AFTER contract",
        'expected_funding_bps': "ADD COLUMN expected_funding_bps DECIMAL(12,4) DEFAULT NULL AFTER strategy_tier",
        'borrowable': "ADD COLUMN borrowable TINYINT DEFAULT NULL AFTER expected_funding_bps",
        'borrow_capacity_usdt': "ADD COLUMN borrow_capacity_usdt DECIMAL(20,4) DEFAULT NULL AFTER borrowable",
        'borrow_hourly_rate': "ADD COLUMN borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL AFTER borrow_capacity_usdt",
        'borrow_24h_bps': "ADD COLUMN borrow_24h_bps DECIMAL(12,4) DEFAULT NULL AFTER borrow_hourly_rate",
        'max_borrowable_amount': "ADD COLUMN max_borrowable_amount DECIMAL(28,8) DEFAULT NULL AFTER borrow_24h_bps",
        'borrow_snapshot_time': "ADD COLUMN borrow_snapshot_time DATETIME DEFAULT NULL AFTER max_borrowable_amount",
        'follow_score': "ADD COLUMN follow_score DECIMAL(12,4) DEFAULT NULL AFTER borrow_snapshot_time",
        'follow_reason': "ADD COLUMN follow_reason VARCHAR(512) DEFAULT NULL AFTER follow_score",
        'funding_change_1h_bps': "ADD COLUMN funding_change_1h_bps DECIMAL(12,4) DEFAULT NULL AFTER follow_reason",
        'funding_change_4h_bps': "ADD COLUMN funding_change_4h_bps DECIMAL(12,4) DEFAULT NULL AFTER funding_change_1h_bps",
        'funding_change_12h_bps': "ADD COLUMN funding_change_12h_bps DECIMAL(12,4) DEFAULT NULL AFTER funding_change_4h_bps",
        'borrow_capacity_drop_1h_pct': "ADD COLUMN borrow_capacity_drop_1h_pct DECIMAL(12,4) DEFAULT NULL AFTER funding_change_12h_bps",
        'borrow_capacity_drop_4h_pct': "ADD COLUMN borrow_capacity_drop_4h_pct DECIMAL(12,4) DEFAULT NULL AFTER borrow_capacity_drop_1h_pct",
        'borrow_capacity_drop_12h_pct': "ADD COLUMN borrow_capacity_drop_12h_pct DECIMAL(12,4) DEFAULT NULL AFTER borrow_capacity_drop_4h_pct",
        'borrow_capacity_drop_max_pct': "ADD COLUMN borrow_capacity_drop_max_pct DECIMAL(12,4) DEFAULT NULL AFTER borrow_capacity_drop_12h_pct",
    }
    for column, ddl in alters.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE mi_reverse_funding_prediction {ddl}")


def _compute_prediction_rows(
    *,
    threshold_rate: float = DEFAULT_THRESHOLD_RATE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    keyword: str = '',
) -> List[Dict[str, Any]]:
    threshold_rate = _normalize_threshold_rate(threshold_rate)
    lookback_days = _normalize_lookback_days(lookback_days)
    keyword = str(keyword or '').strip().upper()

    current_by_contract = _load_current_contracts()
    eligible_assets = _load_prediction_universe()
    borrow_by_asset = _load_latest_borrow_meta()
    borrow_history_by_asset = _load_borrow_history(24)
    history_by_contract = _group_history(_load_funding_history(lookback_days))

    rows: List[Dict[str, Any]] = []
    for contract, history in history_by_contract.items():
        current = current_by_contract.get(contract)
        base_asset = _contract_base_asset(contract, current)
        strategy_tier = eligible_assets.get(base_asset)
        if not strategy_tier:
            continue
        row = _compute_prediction_row(contract, history, current, threshold_rate)
        if not row:
            continue
        row['strategy_tier'] = strategy_tier
        _attach_borrow_metrics(row, borrow_by_asset.get(base_asset))
        _attach_follow_metrics(row, history, borrow_history_by_asset.get(base_asset, []))
        if keyword and keyword not in row['base_asset'] and keyword not in row['contract']:
            continue
        rows.append(row)

    rows.sort(
        key=lambda item: (
            item.get('follow_score') or 0.0,
            item.get('borrow_capacity_drop_max_pct') or 0.0,
            max(0.0, -(item.get('funding_change_4h_bps') or 0.0)),
            item.get('p_next_3') or 0.0,
            item.get('p_next_2') or 0.0,
            item.get('p_next_1') or 0.0,
            item.get('high_negative_frequency') or 0.0,
        ),
        reverse=True,
    )
    return rows


def _build_summary(
    rows: List[Dict[str, Any]],
    *,
    threshold_rate: float,
    lookback_days: int,
    source: str,
    generated_at: Optional[Any] = None,
) -> Dict[str, Any]:
    total = len(rows)
    latest_history_time = max((row.get('last_history_time') for row in rows if row.get('last_history_time')), default=None)
    current_high_count = sum(1 for row in rows if (row.get('current_funding_rate_24h') or 0) <= threshold_rate)
    follow_candidates = sum(1 for row in rows if (_as_float(row.get('follow_score'), 0.0) or 0.0) >= DEFAULT_MIN_FOLLOW_SCORE)
    borrow_drop_count = sum(1 for row in rows if (_as_float(row.get('borrow_capacity_drop_max_pct'), 0.0) or 0.0) >= DEFAULT_MIN_BORROW_DROP_PCT)
    funding_down_count = sum(1 for row in rows if min(
        _as_float(row.get('funding_change_1h_bps'), 0.0) or 0.0,
        _as_float(row.get('funding_change_4h_bps'), 0.0) or 0.0,
        _as_float(row.get('funding_change_12h_bps'), 0.0) or 0.0,
    ) <= -DEFAULT_MIN_FUNDING_DROP_BPS)
    return {
        'asset_count': total,
        'current_high_negative_count': current_high_count,
        'follow_candidate_count': follow_candidates,
        'borrow_drop_count': borrow_drop_count,
        'funding_down_count': funding_down_count,
        'threshold_rate': threshold_rate,
        'lookback_days': lookback_days,
        'latest_history_time': latest_history_time,
        'generated_at': _serialize_dt(generated_at),
        'model_version': MODEL_VERSION,
        'source': source,
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
        'avg_follow_score': _round_or_none(
            sum((_as_float(row.get('follow_score'), 0.0) or 0.0) for row in rows) / total if total else None,
            4,
        ),
    }


def _filter_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _normalize_filter_options(options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = options or {}
    return {
        'follow_score_enabled': _filter_bool(raw.get('follow_score_enabled')),
        'funding_down_enabled': _filter_bool(raw.get('funding_down_enabled')),
        'borrow_drop_enabled': _filter_bool(raw.get('borrow_drop_enabled')),
        'history_high_negative_enabled': _filter_bool(raw.get('history_high_negative_enabled')),
        'probability_enabled': _filter_bool(raw.get('probability_enabled')),
        'confidence_enabled': _filter_bool(raw.get('confidence_enabled')),
        'negative_funding_enabled': _filter_bool(raw.get('negative_funding_enabled')),
        'borrowable_enabled': _filter_bool(raw.get('borrowable_enabled')),
        'capacity_enabled': _filter_bool(raw.get('capacity_enabled')),
        'borrow_cost_enabled': _filter_bool(raw.get('borrow_cost_enabled')),
        'min_p_next_2': max(0.0, min(1.0, float(raw.get('min_p_next_2') or DEFAULT_MIN_P_NEXT_2))),
        'min_p_next_3': max(0.0, min(1.0, float(raw.get('min_p_next_3') or DEFAULT_MIN_P_NEXT_3))),
        'min_confidence': max(0.0, min(1.0, float(raw.get('min_confidence') or DEFAULT_MIN_CONFIDENCE))),
        'min_follow_score': max(0.0, float(raw.get('min_follow_score') or DEFAULT_MIN_FOLLOW_SCORE)),
        'min_funding_drop_bps': max(0.0, float(raw.get('min_funding_drop_bps') or DEFAULT_MIN_FUNDING_DROP_BPS)),
        'min_borrow_drop_pct': max(0.0, float(raw.get('min_borrow_drop_pct') or DEFAULT_MIN_BORROW_DROP_PCT)),
        'min_borrow_capacity_usdt': max(0.0, float(
            raw.get('min_borrow_capacity_usdt')
            if raw.get('min_borrow_capacity_usdt') is not None
            else config.get_float('trade.open.amount_usdt', 10.0)
        )),
        'max_borrow_cost_ratio': max(0.0, float(raw.get('max_borrow_cost_ratio') or DEFAULT_BORROW_COST_RATIO)),
    }


def _annotate_filter_pass(row: Dict[str, Any], opts: Dict[str, Any]) -> Dict[str, Any]:
    p2 = _as_float(row.get('p_next_2'), 0.0) or 0.0
    p3 = _as_float(row.get('p_next_3'), 0.0) or 0.0
    confidence = _as_float(row.get('confidence'), 0.0) or 0.0
    current_rate = _as_float(row.get('current_funding_rate_24h'), 0.0) or 0.0
    borrowable = int(row.get('borrowable') or 0) == 1
    capacity = _as_float(row.get('borrow_capacity_usdt'), 0.0) or 0.0
    borrow_cost = _as_float(row.get('borrow_24h_bps'))
    expected = _as_float(row.get('expected_funding_bps'), 0.0) or 0.0
    follow_score = _as_float(row.get('follow_score'), 0.0) or 0.0
    min_funding_change = min(
        _as_float(row.get('funding_change_1h_bps'), 0.0) or 0.0,
        _as_float(row.get('funding_change_4h_bps'), 0.0) or 0.0,
        _as_float(row.get('funding_change_12h_bps'), 0.0) or 0.0,
    )
    max_borrow_drop = _as_float(row.get('borrow_capacity_drop_max_pct'), 0.0) or 0.0
    high_negative_count = _as_float(row.get('high_negative_count'), 0.0) or 0.0

    row['follow_score_filter_pass'] = follow_score >= opts['min_follow_score']
    row['funding_down_filter_pass'] = min_funding_change <= -opts['min_funding_drop_bps']
    row['borrow_drop_filter_pass'] = max_borrow_drop >= opts['min_borrow_drop_pct']
    row['history_high_negative_filter_pass'] = high_negative_count > 0
    row['probability_filter_pass'] = p2 >= opts['min_p_next_2'] or p3 >= opts['min_p_next_3']
    row['confidence_filter_pass'] = confidence >= opts['min_confidence']
    row['negative_funding_filter_pass'] = current_rate < 0
    row['borrowable_filter_pass'] = borrowable
    row['capacity_filter_pass'] = capacity >= opts['min_borrow_capacity_usdt']
    row['borrow_cost_filter_pass'] = (
        borrow_cost is not None
        and expected > 0
        and borrow_cost <= expected * opts['max_borrow_cost_ratio']
    )
    enabled_checks = [
        'follow_score_filter_pass' if opts['follow_score_enabled'] else None,
        'funding_down_filter_pass' if opts['funding_down_enabled'] else None,
        'borrow_drop_filter_pass' if opts['borrow_drop_enabled'] else None,
        'history_high_negative_filter_pass' if opts['history_high_negative_enabled'] else None,
        'probability_filter_pass' if opts['probability_enabled'] else None,
        'confidence_filter_pass' if opts['confidence_enabled'] else None,
        'negative_funding_filter_pass' if opts['negative_funding_enabled'] else None,
        'borrowable_filter_pass' if opts['borrowable_enabled'] else None,
        'capacity_filter_pass' if opts['capacity_enabled'] else None,
        'borrow_cost_filter_pass' if opts['borrow_cost_enabled'] else None,
    ]
    checks = [key for key in enabled_checks if key]
    row['preborrow_filter_pass'] = all(bool(row.get(key)) for key in checks) if checks else True
    row['preborrow_filter_reason'] = (
        f"跟随分>={opts['min_follow_score']:.0f}={'Y' if row['follow_score_filter_pass'] else 'N'};"
        f"资金费下行>={opts['min_funding_drop_bps']:.1f}bps={'Y' if row['funding_down_filter_pass'] else 'N'};"
        f"额度下降>={opts['min_borrow_drop_pct']:.0f}%={'Y' if row['borrow_drop_filter_pass'] else 'N'};"
        f"历史高负={'Y' if row['history_high_negative_filter_pass'] else 'N'};"
        f"概率(p2>={opts['min_p_next_2']:.0%}或p3>={opts['min_p_next_3']:.0%})="
        f"{'Y' if row['probability_filter_pass'] else 'N'};"
        f"置信度>={opts['min_confidence']:.0%}={'Y' if row['confidence_filter_pass'] else 'N'};"
        f"负费率={'Y' if row['negative_funding_filter_pass'] else 'N'};"
        f"可借={'Y' if row['borrowable_filter_pass'] else 'N'};"
        f"额度>={opts['min_borrow_capacity_usdt']:.0f}U={'Y' if row['capacity_filter_pass'] else 'N'};"
        f"成本<=预期*{opts['max_borrow_cost_ratio']:.2f}={'Y' if row['borrow_cost_filter_pass'] else 'N'}"
    )
    return row


def _apply_prediction_filters(rows: List[Dict[str, Any]], options: Optional[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    opts = _normalize_filter_options(options)
    current = [_annotate_filter_pass(dict(row), opts) for row in rows]
    steps = [{'key': 'all', 'label': '全部观察池', 'enabled': True, 'count': len(current)}]
    filters = [
        ('follow_score_enabled', 'follow_score_filter_pass', '跟随分'),
        ('funding_down_enabled', 'funding_down_filter_pass', '资金费下行'),
        ('borrow_drop_enabled', 'borrow_drop_filter_pass', '额度下降'),
        ('history_high_negative_enabled', 'history_high_negative_filter_pass', '历史高负'),
        ('probability_enabled', 'probability_filter_pass', '概率候选'),
        ('confidence_enabled', 'confidence_filter_pass', '置信度'),
        ('negative_funding_enabled', 'negative_funding_filter_pass', '当前负费率'),
        ('borrowable_enabled', 'borrowable_filter_pass', '当前可借'),
        ('capacity_enabled', 'capacity_filter_pass', '额度足够'),
        ('borrow_cost_enabled', 'borrow_cost_filter_pass', '借币成本'),
    ]
    for enabled_key, pass_key, label in filters:
        if opts[enabled_key]:
            current = [row for row in current if bool(row.get(pass_key))]
        steps.append({
            'key': enabled_key,
            'label': label,
            'enabled': bool(opts[enabled_key]),
            'count': len(current),
        })
    return current, steps, opts


def predict_high_negative_funding(
    *,
    base_asset: Optional[str] = None,
    threshold_rate: float = DEFAULT_THRESHOLD_RATE,
    horizons: Tuple[int, ...] = (1, 2, 3),
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    as_of: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Reusable prediction entrypoint for reverse pre-borrow research.

    ``horizons`` and ``as_of`` are accepted to keep the call shape stable for
    future model upgrades. The current bucket model supports 1/2/3 period
    horizons and always reads the latest stored market state.
    """
    del as_of
    supported = {1, 2, 3}
    unsupported = set(horizons) - supported
    if unsupported:
        raise ValueError(f'unsupported horizons: {sorted(unsupported)}')
    keyword = str(base_asset or '').strip().upper()
    rows = _compute_prediction_rows(
        threshold_rate=threshold_rate,
        lookback_days=lookback_days,
        keyword=keyword,
    )
    if base_asset:
        wanted = str(base_asset).strip().upper()
        rows = [row for row in rows if row.get('base_asset') == wanted]
    return rows


def refresh_reverse_funding_predictions(
    *,
    threshold_rate: float = DEFAULT_THRESHOLD_RATE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Dict[str, Any]:
    threshold_rate = _normalize_threshold_rate(threshold_rate)
    lookback_days = _normalize_lookback_days(lookback_days)
    ensure_reverse_funding_prediction_table()
    rows = _compute_prediction_rows(
        threshold_rate=threshold_rate,
        lookback_days=lookback_days,
    )
    generated_at = datetime.now()
    if not rows:
        return {
            'success': True,
            'inserted': 0,
            'summary': _build_summary(
                [],
                threshold_rate=threshold_rate,
                lookback_days=lookback_days,
                source='stored',
                generated_at=generated_at,
            ),
        }

    columns = [
        'generated_at', 'model_version', 'threshold_rate', 'lookback_days',
        'base_asset', 'contract', 'strategy_tier', 'expected_funding_bps',
        'borrowable', 'borrow_capacity_usdt', 'borrow_hourly_rate',
        'borrow_24h_bps', 'max_borrowable_amount', 'borrow_snapshot_time',
        'follow_score', 'follow_reason', 'funding_change_1h_bps',
        'funding_change_4h_bps', 'funding_change_12h_bps',
        'borrow_capacity_drop_1h_pct', 'borrow_capacity_drop_4h_pct',
        'borrow_capacity_drop_12h_pct', 'borrow_capacity_drop_max_pct',
        'current_funding_rate_24h',
        'previous_funding_rate_24h', 'funding_rate_change', 'current_bucket',
        'current_bucket_label', 'sample_count', 'conditional_sample_count',
        'high_negative_count', 'high_negative_frequency', 'negative_count',
        'negative_frequency', 'min_funding_rate_24h', 'max_funding_rate_24h',
        'avg_funding_rate_24h', 'p_next_1', 'p_next_2', 'p_next_3',
        'base_p_next_1', 'base_p_next_2', 'base_p_next_3',
        'conditional_p_next_1', 'conditional_p_next_2', 'conditional_p_next_3',
        'confidence', 'last_history_time', 'last_high_negative_time',
        'funding_next_apply', 'current_updated_at',
    ]
    sql = f"""
        INSERT INTO mi_reverse_funding_prediction ({', '.join(columns)})
        VALUES ({', '.join(['%s'] * len(columns))})
    """
    params = []
    for row in rows:
        params.append(tuple(
            generated_at if col == 'generated_at'
            else MODEL_VERSION if col == 'model_version'
            else threshold_rate if col == 'threshold_rate'
            else lookback_days if col == 'lookback_days'
            else row.get(col)
            for col in columns
        ))

    with db_manager.get_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(sql, params)
        conn.commit()

    logger.info(
        '反向Funding预测已刷新: generated_at=%s threshold=%s lookback=%s rows=%s',
        generated_at.strftime('%Y-%m-%d %H:%M:%S'),
        threshold_rate,
        lookback_days,
        len(rows),
    )
    return {
        'success': True,
        'inserted': len(rows),
        'summary': _build_summary(
            rows,
            threshold_rate=threshold_rate,
            lookback_days=lookback_days,
            source='stored',
            generated_at=generated_at,
        ),
    }


def _latest_generated_at(threshold_rate: float, lookback_days: int) -> Optional[Any]:
    ensure_reverse_funding_prediction_table()
    sql = """
        SELECT MAX(generated_at) AS generated_at
        FROM mi_reverse_funding_prediction
        WHERE threshold_rate = %s
          AND lookback_days = %s
          AND model_version = %s
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (threshold_rate, lookback_days, MODEL_VERSION))
        row = cursor.fetchone() or {}
    return row.get('generated_at')


def _load_stored_prediction_page(
    *,
    threshold_rate: float,
    lookback_days: int,
    keyword: str = '',
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    filter_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    generated_at = _latest_generated_at(threshold_rate, lookback_days)
    if generated_at is None:
        return {'rows': [], 'pagination': {'page': page, 'page_size': page_size, 'total': 0, 'total_pages': 1}}

    where = """
        generated_at = %s
        AND threshold_rate = %s
        AND lookback_days = %s
        AND model_version = %s
    """
    params: List[Any] = [generated_at, threshold_rate, lookback_days, MODEL_VERSION]
    if keyword:
        where += " AND (base_asset LIKE %s OR contract LIKE %s)"
        like = _like_keyword(keyword)
        params.extend([like, like])

    data_sql = f"""
        SELECT *
        FROM mi_reverse_funding_prediction
        WHERE {where}
        ORDER BY follow_score DESC, borrow_capacity_drop_max_pct DESC, p_next_3 DESC, p_next_2 DESC, base_asset ASC
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(data_sql, params)
        rows = [_serialize_prediction_row(row) for row in (cursor.fetchall() or [])]

    filtered_rows, filter_steps, normalized_filters = _apply_prediction_filters(rows, filter_options)
    total = len(filtered_rows)
    start = (page - 1) * page_size
    end = start + page_size
    page_rows = filtered_rows[start:end]
    summary = _build_summary(
        filtered_rows,
        threshold_rate=threshold_rate,
        lookback_days=lookback_days,
        source='stored',
        generated_at=generated_at,
    )
    summary['filter_steps'] = filter_steps
    summary['filter_options'] = normalized_filters
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


def _serialize_prediction_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key in {'id'}:
            continue
        if isinstance(value, datetime):
            out[key] = _serialize_dt(value)
        elif hasattr(value, '__float__') and not isinstance(value, (str, bytes)):
            try:
                out[key] = float(value)
            except Exception:
                out[key] = value
        else:
            out[key] = value
    return out


def get_reverse_funding_prediction_page(
    *,
    threshold_rate: float = DEFAULT_THRESHOLD_RATE,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    keyword: str = '',
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    prefer_stored: bool = True,
    filter_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    threshold_rate = _normalize_threshold_rate(threshold_rate)
    lookback_days = _normalize_lookback_days(lookback_days)
    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or DEFAULT_PAGE_SIZE), 10), 5000)
    keyword = str(keyword or '').strip().upper()

    if prefer_stored:
        try:
            stored = _load_stored_prediction_page(
                threshold_rate=threshold_rate,
                lookback_days=lookback_days,
                keyword=keyword,
                page=page,
                page_size=page_size,
                filter_options=filter_options,
            )
            if stored.get('summary', {}).get('source') == 'stored':
                return stored
        except Exception as e:
            logger.warning(f'读取反向Funding预测落库结果失败，改用即时计算: {e}', exc_info=True)

    rows = _compute_prediction_rows(
        threshold_rate=threshold_rate,
        lookback_days=lookback_days,
        keyword=keyword,
    )
    rows, filter_steps, normalized_filters = _apply_prediction_filters(rows, filter_options)
    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    page_rows = rows[start:end]
    summary = _build_summary(
        rows,
        threshold_rate=threshold_rate,
        lookback_days=lookback_days,
        source='live',
    )
    summary['filter_steps'] = filter_steps
    summary['filter_options'] = normalized_filters
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
