# coding: utf-8
"""
Order book resiliency checks shared by open and take-profit close flows.

The monitor answers a different question from static VWAP/coverage checks:
after a basis shock, has the relevant book side recovered enough to trade?
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Dict, List, Optional


LEVEL = 20


@dataclass(frozen=True)
class ResiliencyConfig:
    enabled: bool = True
    window_sec: float = 3.0
    min_samples: int = 5
    min_recovery_ratio: float = 0.65
    max_spread_widen_bps: float = 8.0
    max_basis_volatility_bps: float = 6.0
    min_hold_sec: float = 0.8
    max_wait_sec: float = 3.0
    allow_timeout_pass: bool = False


@dataclass(frozen=True)
class BookSideSpec:
    prefix: str
    side: str
    qty_multiplier: float = 1.0
    label: str = ''
    qty_multiplier_key: str = ''


@dataclass
class ResiliencyResult:
    passed: bool
    waiting: bool
    terminal: bool
    reason: str
    metrics: Dict[str, float]


def side_depth_usdt(row: Dict, spec: BookSideSpec, level: int = LEVEL) -> float:
    qty_multiplier = spec.qty_multiplier
    if spec.qty_multiplier_key:
        qty_multiplier = float(row.get(spec.qty_multiplier_key) or qty_multiplier)
    total = 0.0
    for i in range(1, level + 1):
        price = row.get(f'{spec.prefix}_price_{spec.side}_{i}')
        volume = row.get(f'{spec.prefix}_volume_{spec.side}_{i}')
        if price is None or volume is None:
            continue
        total += float(price) * float(volume) * qty_multiplier
    return total


def book_spread_bps(row: Dict, prefix: str) -> Optional[float]:
    bid = row.get(f'{prefix}_price_bid_1')
    ask = row.get(f'{prefix}_price_ask_1')
    if bid is None or ask is None:
        return None
    bid = float(bid)
    ask = float(ask)
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10000


class OrderBookResiliencyMonitor:
    """Stateful resiliency checker for one trading side profile."""

    def __init__(
        self,
        cfg: ResiliencyConfig,
        side_specs: List[BookSideSpec],
        coverage_keys: List[str],
        label: str,
    ):
        self.cfg = cfg
        self.side_specs = side_specs
        self.coverage_keys = coverage_keys
        self.label = label
        self._state: Dict[str, Dict] = {}

    def clear(self, asset: str) -> None:
        self._state.pop(asset, None)

    def observe_shock(self, asset: str, row: Dict, now: Optional[datetime] = None) -> None:
        if not self.cfg.enabled:
            return
        now = now or datetime.now()
        state = self._state.get(asset)
        if state is None:
            depths = self._side_depths(row)
            state = {
                'start_time': now,
                'baseline_depths': depths,
                'shock_low_depths': dict(depths),
                'samples': deque(),
                'passed_since': None,
            }
            self._state[asset] = state
            return

        depths = self._side_depths(row)
        for key, depth in depths.items():
            old = state['shock_low_depths'].get(key, depth)
            state['shock_low_depths'][key] = min(old, depth)

    def check(
        self,
        asset: str,
        row: Dict,
        basis_bps: Optional[float],
        coverage_threshold: float,
        min_basis_bps: Optional[float] = None,
        max_basis_bps: Optional[float] = None,
        max_basis_rebound_bps: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> ResiliencyResult:
        if not self.cfg.enabled:
            return ResiliencyResult(True, False, False, 'disabled', {})

        now = now or datetime.now()
        if asset not in self._state:
            self.observe_shock(asset, row, now)
        state = self._state[asset]
        if state.get('wait_start_time') is None:
            state['wait_start_time'] = now

        samples: Deque[Dict] = state['samples']
        sample = self._sample(row, basis_bps)
        sample['time'] = now
        samples.append(sample)
        while samples and (now - samples[0]['time']).total_seconds() > self.cfg.window_sec:
            samples.popleft()

        metrics = self._metrics(state, sample, samples)
        elapsed = (now - state['wait_start_time']).total_seconds()
        metrics['elapsed_sec'] = elapsed

        basis = sample.get('basis_bps')
        if min_basis_bps is not None and basis is not None and basis < min_basis_bps:
            return ResiliencyResult(False, False, True, f'basis_below_min({basis:.1f}<{min_basis_bps:.1f})', metrics)
        if max_basis_bps is not None and basis is not None and basis >= max_basis_bps:
            return ResiliencyResult(False, False, True, f'basis_above_max({basis:.1f}>={max_basis_bps:.1f})', metrics)

        if len(samples) < self.cfg.min_samples:
            return self._wait_or_timeout(state, now, metrics, f'samples({len(samples)}<{self.cfg.min_samples})')

        if metrics['recovery_ratio'] < self.cfg.min_recovery_ratio:
            return self._wait_or_timeout(
                state, now, metrics,
                f'recovery({metrics["recovery_ratio"]:.2f}<{self.cfg.min_recovery_ratio:.2f})'
            )

        if metrics['max_spread_widen_bps'] > self.cfg.max_spread_widen_bps:
            return self._wait_or_timeout(
                state, now, metrics,
                f'spread_widen({metrics["max_spread_widen_bps"]:.1f}>{self.cfg.max_spread_widen_bps:.1f})'
            )

        if metrics['basis_volatility_bps'] > self.cfg.max_basis_volatility_bps:
            return self._wait_or_timeout(
                state, now, metrics,
                f'basis_vol({metrics["basis_volatility_bps"]:.1f}>{self.cfg.max_basis_volatility_bps:.1f})'
            )

        if max_basis_rebound_bps is not None:
            first_basis = samples[0].get('basis_bps')
            if first_basis is not None and basis is not None:
                rebound = basis - first_basis
                metrics['basis_rebound_bps'] = rebound
                if rebound > max_basis_rebound_bps:
                    return ResiliencyResult(
                        False, False, True,
                        f'basis_rebound({rebound:.1f}>{max_basis_rebound_bps:.1f})',
                        metrics,
                    )

        coverage = sample.get('coverage')
        if coverage is None or coverage > coverage_threshold:
            cov_text = 'NA' if coverage is None else f'{coverage:.2f}'
            return self._wait_or_timeout(state, now, metrics, f'coverage({cov_text}>{coverage_threshold:.2f})')

        if state.get('passed_since') is None:
            state['passed_since'] = now
            return ResiliencyResult(False, True, False, 'hold_started', metrics)

        hold_sec = (now - state['passed_since']).total_seconds()
        metrics['hold_sec'] = hold_sec
        if hold_sec < self.cfg.min_hold_sec:
            return ResiliencyResult(False, True, False, f'hold({hold_sec:.1f}<{self.cfg.min_hold_sec:.1f})', metrics)

        return ResiliencyResult(True, False, False, 'passed', metrics)

    def _wait_or_timeout(self, state: Dict, now: datetime, metrics: Dict[str, float], reason: str) -> ResiliencyResult:
        state['passed_since'] = None
        elapsed = (now - state.get('wait_start_time', state['start_time'])).total_seconds()
        if self.cfg.max_wait_sec > 0 and elapsed >= self.cfg.max_wait_sec:
            if self.cfg.allow_timeout_pass:
                return ResiliencyResult(True, False, False, f'timeout_pass({reason})', metrics)
            return ResiliencyResult(False, False, True, f'timeout({reason})', metrics)
        return ResiliencyResult(False, True, False, reason, metrics)

    def _side_depths(self, row: Dict) -> Dict[str, float]:
        depths = {}
        for spec in self.side_specs:
            label = spec.label or f'{spec.prefix}_{spec.side}'
            depths[label] = side_depth_usdt(row, spec)
        return depths

    def _sample(self, row: Dict, basis_bps: Optional[float]) -> Dict:
        depths = self._side_depths(row)
        spreads = {
            'spot_spread_bps': book_spread_bps(row, 'spot'),
            'future_spread_bps': book_spread_bps(row, 'future'),
        }
        coverages = [row.get(key) for key in self.coverage_keys]
        valid_coverages = [float(x) for x in coverages if x is not None]
        coverage = max(valid_coverages) if len(valid_coverages) == len(self.coverage_keys) else None
        return {
            'basis_bps': float(basis_bps) if basis_bps is not None else None,
            'depths': depths,
            'coverage': coverage,
            **spreads,
        }

    def _metrics(self, state: Dict, sample: Dict, samples: Deque[Dict]) -> Dict[str, float]:
        baseline = state['baseline_depths']
        shock_low = state['shock_low_depths']
        current_depths = sample['depths']

        recovery_ratios = []
        drop_ratios = []
        for key, base_depth in baseline.items():
            if base_depth <= 0:
                continue
            recovery_ratios.append(current_depths.get(key, 0.0) / base_depth)
            drop_ratios.append(max(0.0, (base_depth - shock_low.get(key, base_depth)) / base_depth))

        basis_values = [s['basis_bps'] for s in samples if s.get('basis_bps') is not None]
        spread_widens = []
        first = samples[0] if samples else sample
        for key in ('spot_spread_bps', 'future_spread_bps'):
            start = first.get(key)
            current = sample.get(key)
            if start is not None and current is not None:
                spread_widens.append(max(0.0, current - start))

        return {
            'recovery_ratio': min(recovery_ratios) if recovery_ratios else 1.0,
            'shock_drop_ratio': max(drop_ratios) if drop_ratios else 0.0,
            'basis_volatility_bps': (max(basis_values) - min(basis_values)) if basis_values else 0.0,
            'max_spread_widen_bps': max(spread_widens) if spread_widens else 0.0,
            'coverage': sample.get('coverage'),
        }
