# coding: utf-8
"""
Dynamic take-profit threshold for forward carry positions.

The calculator is intentionally pure: it receives one position, current close
basis, and threshold metadata, then returns a decision snapshot. Trading
execution and orderbook access stay in ClosingExecutor.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional


@dataclass
class DynamicTakeProfitConfig:
    enabled: bool = True
    recent_settlements: int = 3
    high_confidence_min_samples: int = 6
    medium_confidence_min_samples: int = 3
    high_recent_weight: float = 0.6
    high_p50_weight: float = 0.4
    medium_current_weight: float = 0.5
    medium_recent_weight: float = 0.3
    medium_p50_weight: float = 0.2
    basis_discount_tier_a: float = 0.45
    basis_discount_normal: float = 0.35
    basis_discount_thin_bursty: float = 0.25
    basis_score_cap_bps: float = 30.0
    low_confidence_min_take_profit_bps: float = 110.0
    medium_confidence_min_take_profit_bps: float = 80.0
    aging_enabled: bool = True
    aging_start_days: float = 6.0
    aging_start_funding_count: int = 32
    aging_max_threshold_bps: float = 100.0
    aging_min_net_profit_bps: float = 80.0
    aging_hard_days: float = 10.0
    aging_hard_funding_count: int = 50
    aging_hard_max_threshold_bps: float = 80.0
    aging_hard_min_net_profit_bps: float = 80.0
    aging_hold_funding_bps: float = 20.0
    tiers: List[Dict[str, float]] = field(default_factory=lambda: [
        {'hold_value_min_bps': 60.0, 'take_profit_bps': 200.0},
        {'hold_value_min_bps': 40.0, 'take_profit_bps': 150.0},
        {'hold_value_min_bps': 25.0, 'take_profit_bps': 110.0},
        {'hold_value_min_bps': 15.0, 'take_profit_bps': 80.0},
        {'hold_value_min_bps': 5.0, 'take_profit_bps': 80.0},
        {'hold_value_min_bps': 0.0, 'take_profit_bps': 80.0},
    ])


@dataclass
class DynamicTakeProfitEvaluation:
    enabled: bool
    confidence: str
    net_profit_bps: float
    threshold_bps: float
    fixed_take_profit_bps: float
    hold_value_bps: float
    funding_potential_bps: float
    funding_current_24h_bps: float
    funding_hist_samples: int
    funding_recent_avg_bps: Optional[float]
    funding_p50_bps: Optional[float]
    funding_p70_bps: Optional[float]
    funding_support_bps: Optional[float]
    basis_remaining_bps: float
    basis_discount: float
    basis_score_bps: float
    close_basis_bps: float
    close_threshold_bps: Optional[float]
    spread_profit_bps: float
    funding_earned_bps: float
    fee_full_bps: float
    age_days: Optional[float]
    funding_count: int
    pre_aging_threshold_bps: float
    aging_stage: Optional[str]
    aging_trigger: Optional[str]
    aging_cap_bps: Optional[float]
    aging_min_profit_bps: Optional[float]
    aging_blocked_by_funding: bool
    aging_hold_funding_bps: Optional[float]

    @property
    def passed(self) -> bool:
        return self.net_profit_bps >= self.threshold_bps


def evaluate_dynamic_take_profit(
    *,
    position: Dict,
    close_basis_bps: float,
    close_threshold_meta: Optional[Dict],
    close_threshold_col: str,
    fixed_take_profit_bps: float,
    fee_full_bps: float,
    cfg: DynamicTakeProfitConfig,
) -> DynamicTakeProfitEvaluation:
    open_basis = _as_float(position.get('open_spread_bps'), 0.0)
    funding_earned = _as_float(position.get('funding_pnl_bps'), 0.0)
    spread_profit = _as_float(
        position.get('economic_spread_pnl_bps'),
        open_basis - float(close_basis_bps),
    )
    net_profit = spread_profit + funding_earned - float(fee_full_bps or 0.0)

    close_threshold = _close_threshold_bps(close_threshold_meta, close_threshold_col)
    funding = _funding_potential_bps(position, cfg)
    basis_discount = _basis_discount(position, cfg)

    if close_threshold is None:
        basis_remaining = 0.0
    else:
        basis_remaining = max(0.0, float(close_basis_bps) - close_threshold)
    basis_score = min(
        max(0.0, cfg.basis_score_cap_bps),
        max(0.0, basis_remaining * basis_discount),
    )
    hold_value = funding['potential_bps'] + basis_score

    dynamic_threshold = _threshold_from_hold_value(hold_value, cfg)
    if funding['confidence'] == 'low':
        dynamic_threshold = max(dynamic_threshold, cfg.low_confidence_min_take_profit_bps)
    elif funding['confidence'] == 'medium':
        dynamic_threshold = max(dynamic_threshold, cfg.medium_confidence_min_take_profit_bps)
    threshold = min(float(fixed_take_profit_bps), dynamic_threshold)
    pre_aging_threshold = threshold
    age_days = _position_age_days(position)
    funding_count = _funding_count(position)
    aging_stage = None
    aging_trigger = None
    aging_cap = None
    aging_min_profit = None
    aging_blocked_by_funding = False
    aging_hold_funding = None

    if cfg.enabled and cfg.aging_enabled:
        aging_stage, aging_trigger = _aging_stage(position, age_days, funding_count, cfg)

        if aging_stage:
            funding_strength = max(
                float(funding['potential_bps'] or 0.0),
                float(funding['current_24h_bps'] or 0.0),
            )
            hold_threshold = max(float(cfg.aging_hold_funding_bps or 0.0), 0.0)
            if hold_threshold > 0 and funding_strength >= hold_threshold:
                aging_blocked_by_funding = True
                aging_hold_funding = hold_threshold
                aging_stage = None
                aging_trigger = None
            else:
                if aging_stage == 'hard':
                    aging_cap = float(cfg.aging_hard_max_threshold_bps)
                    aging_min_profit = float(cfg.aging_hard_min_net_profit_bps)
                else:
                    aging_cap = float(cfg.aging_max_threshold_bps)
                    aging_min_profit = float(cfg.aging_min_net_profit_bps)
                threshold = max(min(threshold, aging_cap), max(0.0, aging_min_profit))

    if not cfg.enabled:
        threshold = float(fixed_take_profit_bps)
        pre_aging_threshold = threshold
        aging_stage = None
        aging_trigger = None
        aging_cap = None
        aging_min_profit = None
        aging_blocked_by_funding = False
        aging_hold_funding = None

    return DynamicTakeProfitEvaluation(
        enabled=bool(cfg.enabled),
        confidence=funding['confidence'],
        net_profit_bps=net_profit,
        threshold_bps=threshold,
        fixed_take_profit_bps=float(fixed_take_profit_bps),
        hold_value_bps=hold_value,
        funding_potential_bps=funding['potential_bps'],
        funding_current_24h_bps=funding['current_24h_bps'],
        funding_hist_samples=funding['hist_samples'],
        funding_recent_avg_bps=funding['recent_avg_bps'],
        funding_p50_bps=funding['p50_bps'],
        funding_p70_bps=funding['p70_bps'],
        funding_support_bps=funding['support_bps'],
        basis_remaining_bps=basis_remaining,
        basis_discount=basis_discount,
        basis_score_bps=basis_score,
        close_basis_bps=float(close_basis_bps),
        close_threshold_bps=close_threshold,
        spread_profit_bps=spread_profit,
        funding_earned_bps=funding_earned,
        fee_full_bps=float(fee_full_bps or 0.0),
        age_days=age_days,
        funding_count=funding_count,
        pre_aging_threshold_bps=pre_aging_threshold,
        aging_stage=aging_stage,
        aging_trigger=aging_trigger,
        aging_cap_bps=aging_cap,
        aging_min_profit_bps=aging_min_profit,
        aging_blocked_by_funding=aging_blocked_by_funding,
        aging_hold_funding_bps=aging_hold_funding,
    )


def format_dynamic_take_profit(eval_: DynamicTakeProfitEvaluation) -> str:
    def fmt(value: Optional[float], suffix: str = 'bps') -> str:
        if value is None:
            return 'NA'
        return f'{value:.1f}{suffix}'

    aging = ''
    if eval_.aging_stage:
        age_text = 'NA' if eval_.age_days is None else f'{eval_.age_days:.1f}d'
        aging = (
            f"|aging({eval_.aging_stage},trigger={eval_.aging_trigger},"
            f"age={age_text},count={eval_.funding_count},"
            f"raw={eval_.pre_aging_threshold_bps:.1f},"
            f"cap={eval_.aging_cap_bps:.1f},min={eval_.aging_min_profit_bps:.1f})"
        )
    elif eval_.aging_blocked_by_funding:
        aging = (
            f"|aging_hold(funding={max(eval_.funding_potential_bps, eval_.funding_current_24h_bps):.1f}"
            f">={eval_.aging_hold_funding_bps:.1f},count={eval_.funding_count})"
        )

    return (
        f"动态止盈|净{eval_.net_profit_bps:.1f}bps"
        f">={eval_.threshold_bps:.1f}bps"
        f"{aging}"
        f"|hold={eval_.hold_value_bps:.1f}bps"
        f"(funding={eval_.funding_potential_bps:.1f},"
        f"basis={eval_.basis_remaining_bps:.1f}×{eval_.basis_discount:.2f}"
        f"={eval_.basis_score_bps:.1f})"
        f"|funding_potential(cur={eval_.funding_current_24h_bps:.1f},"
        f"avg{eval_.funding_hist_samples}={fmt(eval_.funding_recent_avg_bps)},"
        f"p50={fmt(eval_.funding_p50_bps)},p70={fmt(eval_.funding_p70_bps)},"
        f"conf={eval_.confidence})"
        f"|basis(close={eval_.close_basis_bps:.1f},p20={fmt(eval_.close_threshold_bps)})"
        f"|组成(收敛{eval_.spread_profit_bps:.1f}+资金费{eval_.funding_earned_bps:.1f}"
        f"-{eval_.fee_full_bps:.0f}费)"
    )


def _funding_potential_bps(position: Dict, cfg: DynamicTakeProfitConfig) -> Dict[str, object]:
    current = _as_float(position.get('funding_rate_24h'), 0.0) * 10000.0
    history = _funding_history_bps(position)
    n = len(history)
    recent_n = max(1, int(cfg.recent_settlements or 1))

    if n >= cfg.high_confidence_min_samples:
        recent = history[-recent_n:]
        recent_avg = sum(recent) / len(recent)
        p50 = _percentile(history, 0.5)
        p70 = _percentile(history, 0.7)
        support = cfg.high_recent_weight * recent_avg + cfg.high_p50_weight * p50
        potential = min(p70, max(current, support))
        confidence = 'high'
    elif n >= cfg.medium_confidence_min_samples:
        recent = history[-min(recent_n, n):]
        recent_avg = sum(recent) / len(recent)
        p50 = _percentile(history, 0.5)
        p70 = _percentile(history, 0.7)
        support = (
            cfg.medium_current_weight * current
            + cfg.medium_recent_weight * recent_avg
            + cfg.medium_p50_weight * p50
        )
        potential = support
        confidence = 'medium'
    else:
        recent_avg = None
        p50 = None
        p70 = None
        support = None
        potential = current
        confidence = 'low'

    return {
        'potential_bps': max(0.0, float(potential or 0.0)),
        'confidence': confidence,
        'hist_samples': n,
        'current_24h_bps': current,
        'recent_avg_bps': recent_avg,
        'p50_bps': p50,
        'p70_bps': p70,
        'support_bps': support,
    }


def _funding_history_bps(position: Dict) -> List[float]:
    raw_history = (
        position.get('asset_funding_history')
        or position.get('funding_history')
        or []
    )
    values = []
    seen = set()
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        raw = item.get('rate_24h')
        if raw is None:
            raw = item.get('funding_rate_24h')
        if raw is None:
            continue
        try:
            bps = float(raw) * 10000.0
        except (TypeError, ValueError):
            continue
        key = item.get('settled_at') or item.get('time') or item.get('seq') or len(values)
        dedupe_key = (str(key), round(bps, 8))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        values.append(bps)
    return values


def _close_threshold_bps(meta: Optional[Dict], close_threshold_col: str) -> Optional[float]:
    if not meta:
        return None
    candidates = [
        close_threshold_col,
        'close_basis_p20',
        'close_vwap_threshold_bps',
        'p20',
    ]
    for key in candidates:
        if key and meta.get(key) is not None:
            return _as_float(meta.get(key), None)
    return None


def _basis_discount(position: Dict, cfg: DynamicTakeProfitConfig) -> float:
    tier = str(position.get('strategy_tier') or '').strip().upper()
    if tier == 'A':
        return cfg.basis_discount_tier_a
    profile = str(position.get('market_profile') or 'normal').strip().lower()
    if profile == 'thin_bursty':
        return cfg.basis_discount_thin_bursty
    return cfg.basis_discount_normal


def _threshold_from_hold_value(hold_value_bps: float, cfg: DynamicTakeProfitConfig) -> float:
    tiers = sorted(
        cfg.tiers or [],
        key=lambda row: float(row.get('hold_value_min_bps', 0.0)),
        reverse=True,
    )
    for row in tiers:
        if hold_value_bps >= float(row.get('hold_value_min_bps', 0.0)):
            return float(row.get('take_profit_bps', 80.0))
    return 80.0


def _funding_count(position: Dict) -> int:
    try:
        return max(0, int(position.get('funding_payments_count') or 0))
    except (TypeError, ValueError):
        return 0


def _aging_stage(
    position: Dict,
    age_days: Optional[float],
    funding_count: int,
    cfg: DynamicTakeProfitConfig,
) -> tuple:
    hard_reasons = []
    if age_days is not None and age_days >= max(float(cfg.aging_hard_days or 0.0), 0.0):
        hard_reasons.append('age')
    if int(cfg.aging_hard_funding_count or 0) > 0 and funding_count >= int(cfg.aging_hard_funding_count):
        hard_reasons.append('funding_count')
    if hard_reasons:
        return 'hard', '+'.join(hard_reasons)

    aging_reasons = []
    if age_days is not None and age_days >= max(float(cfg.aging_start_days or 0.0), 0.0):
        aging_reasons.append('age')
    if int(cfg.aging_start_funding_count or 0) > 0 and funding_count >= int(cfg.aging_start_funding_count):
        aging_reasons.append('funding_count')
    if aging_reasons:
        return 'aging', '+'.join(aging_reasons)
    return None, None


def _position_age_days(position: Dict) -> Optional[float]:
    opened_at = (
        position.get('opened_at')
        or position.get('open_time')
        or position.get('created_at')
    )
    opened_dt = _as_datetime(opened_at)
    if opened_dt is None:
        return None
    now = datetime.now(tz=opened_dt.tzinfo) if opened_dt.tzinfo else datetime.now()
    age_sec = (now - opened_dt).total_seconds()
    return max(0.0, age_sec / 86400.0)


def _as_datetime(value) -> Optional[datetime]:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Millisecond timestamps are common in frontend/API snapshots.
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = f'{text[:-1]}+00:00'
        for fmt in (
            None,
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M:%S.%f',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M:%S.%f',
        ):
            try:
                if fmt is None:
                    return datetime.fromisoformat(text)
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def _percentile(values: Iterable[float], q: float) -> float:
    data = sorted(float(v) for v in values)
    if not data:
        return 0.0
    idx = int((len(data) - 1) * max(0.0, min(1.0, q)))
    return data[idx]


def _as_float(value, default=0.0):
    if value is None or value == '':
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
