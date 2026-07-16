# coding: utf-8
"""Single-source Gate cross-margin risk snapshots for the forward strategy."""

import copy
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from calc.real_executor import GATE_CROSS_MARGIN_LEVERAGE, RealExecutor
from calc.reconciliation import build_exchange_config
from common.config import config
from common.meta_loader import fetch_contract_meta, fetch_spot_meta


@dataclass(frozen=True)
class GateCrossRiskThresholds:
    warning_mmr_pct: float = 500.0
    danger_mmr_pct: float = 300.0
    warning_liq_distance_bps: float = 600.0
    danger_liq_distance_bps: float = 300.0
    min_available_pct: float = 15.0


def build_default_gate_cross_risk_monitor() -> 'GateCrossRiskMonitor':
    executor = RealExecutor(
        build_exchange_config(),
        contract_meta=fetch_contract_meta(),
        spot_meta=fetch_spot_meta(),
        leverage=GATE_CROSS_MARGIN_LEVERAGE,
    )
    return GateCrossRiskMonitor(
        executor,
        load_gate_cross_risk_thresholds(),
        max_age_sec=config.get_float('account_capital.gate_cross_risk.max_age_sec', 5.0),
    )


def load_gate_cross_risk_thresholds() -> GateCrossRiskThresholds:
    return GateCrossRiskThresholds(
        warning_mmr_pct=config.get_float(
            'account_capital.gate_cross_risk.warning_mmr_pct', 500.0
        ),
        danger_mmr_pct=config.get_float(
            'account_capital.gate_cross_risk.danger_mmr_pct', 300.0
        ),
        warning_liq_distance_bps=config.get_float(
            'account_capital.gate_cross_risk.warning_liq_distance_bps', 600.0
        ),
        danger_liq_distance_bps=config.get_float(
            'account_capital.gate_cross_risk.danger_liq_distance_bps', 300.0
        ),
        min_available_pct=config.get_float(
            'account_capital.gate_cross_risk.min_available_pct', 15.0
        ),
    )


class GateCrossRiskMonitor:
    """Poll Gate account and positions and publish one atomic risk snapshot."""

    def __init__(
        self,
        executor: RealExecutor,
        thresholds: Optional[GateCrossRiskThresholds] = None,
        max_age_sec: float = 5.0,
    ):
        self.executor = executor
        self.thresholds = thresholds or GateCrossRiskThresholds()
        self.max_age_sec = max(float(max_age_sec or 0.0), 0.1)
        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._snapshot: Optional[Dict] = None
        self._positions: List[Dict] = []
        self._positions_fetched_at_ts = 0.0

    def refresh(self) -> Dict:
        with self._refresh_lock:
            return self._refresh_locked()

    def _refresh_locked(self) -> Dict:
        started = time.monotonic()
        fetched_at_ts = time.time()

        account_started = time.monotonic()
        try:
            account = self.executor.fetch_gate_futures_account()
        except Exception as exc:
            account_latency_ms = (time.monotonic() - account_started) * 1000.0
            snapshot = {
                'enabled': True,
                'status': 'unknown',
                'status_label': '未知',
                'source': 'gate_account_api',
                'error': f"Gate account: {str(exc)[:300]}",
                'fetched_at': _format_timestamp(fetched_at_ts),
                'fetched_at_ts': fetched_at_ts,
                'account_fetched_at_ts': None,
                'positions_fetched_at_ts': self.positions_fetched_at_ts,
                'account_latency_ms': round(account_latency_ms, 3),
                'positions_latency_ms': None,
                'latency_ms': round((time.monotonic() - started) * 1000.0, 3),
            }
            snapshot.update(gate_cross_risk_health(
                snapshot,
                now_ts=time.time(),
                max_age_sec=self.max_age_sec,
            ))
            self._store_snapshot(snapshot)
            return copy.deepcopy(snapshot)
        account_latency_ms = (time.monotonic() - account_started) * 1000.0

        positions_started = time.monotonic()
        positions_error = None
        try:
            positions = self.executor.fetch_gate_futures_positions()
            positions_fetched_at_ts = time.time()
            with self._state_lock:
                self._positions = copy.deepcopy(positions or [])
                self._positions_fetched_at_ts = positions_fetched_at_ts
        except Exception as exc:
            positions_error = f"Gate positions: {str(exc)[:300]}"
            with self._state_lock:
                positions = copy.deepcopy(self._positions)
                positions_fetched_at_ts = self._positions_fetched_at_ts
        positions_latency_ms = (time.monotonic() - positions_started) * 1000.0

        metrics = gate_account_metrics(account)
        snapshot = build_gate_cross_risk(
            account,
            positions,
            equity=metrics['risk_equity'],
            available=metrics['risk_available'],
            margin_used=metrics['margin_used'],
            thresholds=self.thresholds,
        )
        if positions_error and snapshot.get('status') != 'danger':
            snapshot['observed_status'] = snapshot.get('status')
            snapshot['status'] = 'unknown'
            snapshot['status_label'] = gate_cross_risk_status_label('unknown')
        snapshot.update({
            'source': 'gate_account_api',
            'error': positions_error,
            'fetched_at': _format_timestamp(fetched_at_ts),
            'fetched_at_ts': fetched_at_ts,
            'account_fetched_at_ts': fetched_at_ts,
            'positions_fetched_at_ts': positions_fetched_at_ts or None,
            'account_latency_ms': round(account_latency_ms, 3),
            'positions_latency_ms': round(positions_latency_ms, 3),
            'latency_ms': round((time.monotonic() - started) * 1000.0, 3),
        })
        snapshot.update(gate_cross_risk_health(
            snapshot,
            now_ts=time.time(),
            max_age_sec=self.max_age_sec,
        ))
        self._store_snapshot(snapshot)
        return copy.deepcopy(snapshot)

    def get_snapshot(self) -> Optional[Dict]:
        with self._state_lock:
            return copy.deepcopy(self._snapshot)

    def get_positions(self) -> List[Dict]:
        with self._state_lock:
            return copy.deepcopy(self._positions)

    @property
    def positions_fetched_at_ts(self) -> float:
        with self._state_lock:
            return float(self._positions_fetched_at_ts or 0.0)

    def _store_snapshot(self, snapshot: Dict) -> None:
        with self._state_lock:
            self._snapshot = copy.deepcopy(snapshot)


def gate_account_metrics(account: Dict) -> Dict[str, float]:
    available = _float(account.get('available'))
    total = _float(account.get('total'))
    unrealized = _float(account.get('unrealised_pnl') or account.get('unrealized_pnl'))
    cross_initial_margin = _float(account.get('cross_initial_margin'))
    cross_order_margin = _float(account.get('cross_order_margin'))
    margin_used = cross_initial_margin + cross_order_margin
    equity = total + unrealized if _has_value(account.get('total')) else (
        available + margin_used + unrealized
    )
    cross_balance = _float_or_none(account.get('cross_margin_balance'))
    cross_available = _float_or_none(account.get('cross_available'))
    return {
        'available': available,
        'total': total,
        'unrealized': unrealized,
        'cross_initial_margin': cross_initial_margin,
        'cross_order_margin': cross_order_margin,
        'margin_used': margin_used,
        'equity': equity,
        'risk_equity': cross_balance if cross_balance is not None and cross_balance > 0 else equity,
        'risk_available': cross_available if cross_available is not None else available,
    }


def build_gate_cross_risk(
    account: Dict,
    positions: List[Dict],
    *,
    equity: float,
    available: float,
    margin_used: float,
    thresholds: Optional[GateCrossRiskThresholds] = None,
) -> Dict:
    thresholds = thresholds or GateCrossRiskThresholds()
    active_positions = [pos for pos in positions or [] if abs(_float(pos.get('size'))) > 0]
    total_position_maintenance = 0.0
    nearest_liq = None
    top_risks = []

    for pos in active_positions:
        maintenance = _float(pos.get('maintenance_margin'))
        initial_margin = _float_or_none(pos.get('initial_margin'))
        unrealized = _float(pos.get('unrealised_pnl') or pos.get('unrealized_pnl'))
        liq_distance_bps = gate_liq_distance_bps(pos)

        total_position_maintenance += max(maintenance, 0.0)

        item = {
            'contract': pos.get('contract'),
            'side': 'short' if _float(pos.get('size')) < 0 else 'long',
            'size': _round(_float(pos.get('size')), 8),
            'initial_margin_usdt': _round(initial_margin),
            'unrealized_pnl_usdt': _round(unrealized),
            'maintenance_margin_usdt': _round(maintenance),
            'liq_distance_bps': _round(liq_distance_bps),
            'mark_price': _round(_float_or_none(pos.get('mark_price')), 10),
            'liq_price': _round(_float_or_none(pos.get('liq_price')), 10),
        }
        top_risks.append(item)
        if liq_distance_bps is not None and (
            nearest_liq is None or liq_distance_bps < nearest_liq['liq_distance_bps']
        ):
            nearest_liq = item

    raw_cross_mmr = _float_or_none(account.get('cross_mmr'))
    cross_initial_margin = _float_or_none(account.get('cross_initial_margin'))
    account_mmr_pct = raw_cross_mmr * 100.0 if raw_cross_mmr is not None else None
    direct_maintenance = _float_or_none(account.get('cross_maintenance_margin'))
    total_maintenance = (
        direct_maintenance
        if direct_maintenance is not None and direct_maintenance >= 0
        else total_position_maintenance
    )
    computed_mmr_pct = equity / total_position_maintenance * 100 if total_position_maintenance > 0 else None
    available_ratio_pct = available / equity * 100 if equity > 0 else None
    margin_usage_pct = margin_used / equity * 100 if equity > 0 else None
    has_exposure = bool(active_positions) or total_maintenance > 0

    top_risks.sort(key=lambda item: (
        item['liq_distance_bps'] is None,
        item['liq_distance_bps'] if item['liq_distance_bps'] is not None else 10**9,
        -float(item['maintenance_margin_usdt'] or 0.0),
    ))
    status = gate_cross_risk_status(
        has_exposure=has_exposure,
        account_mmr_pct=account_mmr_pct,
        nearest_liq_distance_bps=nearest_liq.get('liq_distance_bps') if nearest_liq else None,
        available_ratio_pct=available_ratio_pct,
        thresholds=thresholds,
    )
    if has_exposure and account_mmr_pct is None and status != 'danger':
        status = 'unknown'
    difference_pct = None
    if account_mmr_pct is not None and computed_mmr_pct is not None:
        difference_pct = account_mmr_pct - computed_mmr_pct

    return {
        'enabled': True,
        'status': status,
        'status_label': gate_cross_risk_status_label(status),
        'position_count': len(active_positions),
        'account_equity_usdt': _round(equity),
        'available_usdt': _round(available),
        'available_ratio_pct': _round(available_ratio_pct),
        'margin_used_usdt': _round(margin_used),
        'margin_usage_pct': _round(margin_usage_pct),
        'initial_margin_usdt': _round(cross_initial_margin),
        'maintenance_margin_usdt': _round(total_maintenance),
        'account_mmr_pct': _round(account_mmr_pct),
        'account_mmr_source': 'gate_account.cross_mmr' if raw_cross_mmr is not None else 'missing',
        'computed_account_mmr_pct': _round(computed_mmr_pct),
        'account_mmr_difference_pct': _round(difference_pct),
        'nearest_liq_contract': nearest_liq.get('contract') if nearest_liq else None,
        'nearest_liq_distance_bps': nearest_liq.get('liq_distance_bps') if nearest_liq else None,
        'thresholds': {
            'warning_mmr_pct': thresholds.warning_mmr_pct,
            'danger_mmr_pct': thresholds.danger_mmr_pct,
            'warning_liq_distance_bps': thresholds.warning_liq_distance_bps,
            'danger_liq_distance_bps': thresholds.danger_liq_distance_bps,
            'min_available_pct': thresholds.min_available_pct,
        },
        'top_risks': top_risks[:5],
        'raw_account_keys': sorted(account.keys()),
    }


def gate_cross_risk_status(
    *,
    has_exposure: bool,
    account_mmr_pct: Optional[float],
    nearest_liq_distance_bps: Optional[float],
    available_ratio_pct: Optional[float],
    thresholds: GateCrossRiskThresholds,
) -> str:
    if not has_exposure:
        return 'idle'
    if (
        (account_mmr_pct is not None and account_mmr_pct <= thresholds.danger_mmr_pct)
        or (
            nearest_liq_distance_bps is not None
            and nearest_liq_distance_bps <= thresholds.danger_liq_distance_bps
        )
    ):
        return 'danger'
    if (
        (account_mmr_pct is not None and account_mmr_pct <= thresholds.warning_mmr_pct)
        or (
            nearest_liq_distance_bps is not None
            and nearest_liq_distance_bps <= thresholds.warning_liq_distance_bps
        )
        or (
            available_ratio_pct is not None
            and available_ratio_pct <= thresholds.min_available_pct
        )
    ):
        return 'warning'
    return 'safe'


def gate_liq_distance_bps(pos: Dict) -> Optional[float]:
    mark = _float_or_none(pos.get('mark_price'))
    liq = _float_or_none(pos.get('liq_price'))
    size = _float(pos.get('size'))
    if mark is None or liq is None or mark <= 0 or liq <= 0 or size == 0:
        return None
    if size < 0:
        return (liq - mark) / mark * 10000.0
    return (mark - liq) / mark * 10000.0


def gate_cross_risk_status_label(status: str) -> str:
    return {
        'idle': '无持仓',
        'safe': '安全',
        'warning': '预警',
        'danger': '危险',
        'unknown': '未知',
    }.get(status, status or '-')


def gate_cross_risk_health(
    snapshot: Optional[Dict],
    *,
    now_ts: Optional[float] = None,
    max_age_sec: float = 5.0,
) -> Dict:
    """Describe whether the account and position inputs are usable right now."""
    now_ts = float(now_ts if now_ts is not None else time.time())
    max_age_sec = max(float(max_age_sec or 0.0), 0.1)
    risk = snapshot if isinstance(snapshot, dict) else {}
    account_age_sec = _timestamp_age(risk.get('account_fetched_at_ts'), now_ts)
    positions_age_sec = _timestamp_age(risk.get('positions_fetched_at_ts'), now_ts)
    stale = any(
        age is not None and age > max_age_sec
        for age in (account_age_sec, positions_age_sec)
    )

    if not risk or account_age_sec is None:
        health_status = 'unavailable'
    elif stale:
        health_status = 'stale'
    elif (
        positions_age_sec is None
        or risk.get('error')
        or str(risk.get('status') or '').lower() == 'unknown'
    ):
        health_status = 'degraded'
    else:
        health_status = 'healthy'

    return {
        'health_status': health_status,
        'health_label': gate_cross_risk_health_label(health_status),
        'account_age_sec': _round(account_age_sec, 3),
        'positions_age_sec': _round(positions_age_sec, 3),
        'max_age_sec': max_age_sec,
        'stale': stale,
    }


def gate_cross_risk_health_label(status: str) -> str:
    return {
        'healthy': '正常',
        'degraded': '部分异常',
        'stale': '数据陈旧',
        'unavailable': '不可用',
    }.get(status, status or '-')


def _format_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S')


def _timestamp_age(value, now_ts: float) -> Optional[float]:
    timestamp = _float_or_none(value)
    if timestamp is None or timestamp <= 0:
        return None
    return max(now_ts - timestamp, 0.0)


def _has_value(value) -> bool:
    return value is not None and value != ''


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value) -> Optional[float]:
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None
