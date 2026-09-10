# coding: utf-8
"""Validation helpers for exchange metadata snapshots and contract sizing."""

import math
from typing import Dict, Iterable, Mapping, Optional

from common.logger import get_logger

logger = get_logger(__name__)

MIN_SNAPSHOT_RETAIN_RATIO = 0.5
MIN_REFERENCE_COUNT = 20


def _positive_float(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _guard_snapshot_count(label: str, incoming_count: int, previous_count: int) -> None:
    if incoming_count <= 0:
        raise ValueError(f'{label}快照为空')
    if (
        previous_count >= MIN_REFERENCE_COUNT
        and incoming_count < previous_count * MIN_SNAPSHOT_RETAIN_RATIO
    ):
        raise ValueError(
            f'{label}快照数量异常下降({incoming_count}<{previous_count}x'
            f'{MIN_SNAPSHOT_RETAIN_RATIO:g})'
        )


def validate_contract_records(records: Iterable[Dict], previous_count: int = 0) -> None:
    rows = list(records)
    _guard_snapshot_count('Gate合约元数据', len(rows), int(previous_count or 0))
    seen = set()
    for row in rows:
        name = str(row.get('name') or '').strip().upper()
        base_asset = str(row.get('base_asset') or name.removesuffix('_USDT')).strip().upper()
        multiplier = _positive_float(row.get('quanto_multiplier'))
        if not name or not base_asset or multiplier is None:
            raise ValueError(
                f'Gate合约元数据字段无效(name={name or "-"},asset={base_asset or "-"},'
                f'multiplier={row.get("quanto_multiplier")})'
            )
        if base_asset in seen:
            raise ValueError(f'Gate合约元数据标的重复({base_asset})')
        seen.add(base_asset)


def validate_spot_records(records: Iterable[Dict], previous_count: int = 0) -> None:
    rows = list(records)
    _guard_snapshot_count('Binance现货元数据', len(rows), int(previous_count or 0))
    seen = set()
    for row in rows:
        symbol = str(row.get('symbol') or '').strip().upper()
        base_asset = str(row.get('base_asset') or '').strip().upper()
        if (
            not symbol
            or not base_asset
            or _positive_float(row.get('step_size')) is None
            or _positive_float(row.get('tick_size')) is None
        ):
            raise ValueError(
                f'Binance现货元数据字段无效(symbol={symbol or "-"},asset={base_asset or "-"},'
                f'step={row.get("step_size")},tick={row.get("tick_size")})'
            )
        if base_asset in seen:
            raise ValueError(f'Binance现货元数据标的重复({base_asset})')
        seen.add(base_asset)


def require_quanto_multiplier(contract_meta: Mapping[str, Dict], base_asset: str) -> float:
    asset = str(base_asset or '').strip().upper()
    if (contract_meta.get(asset) or {}).get('multiplier_conflict'):
        raise ValueError(f'Gate持仓与合约乘数不一致({asset})')
    multiplier = _positive_float((contract_meta.get(asset) or {}).get('quanto_multiplier'))
    if multiplier is None:
        raise ValueError(f'缺少有效Gate合约乘数({asset or "unknown"})')
    return multiplier


def merge_position_multipliers(metadata: Dict[str, Dict], positions: Iterable[Dict]) -> Dict[str, Dict]:
    """Retain sizing for held contracts, without making missing contracts tradable."""
    result = {asset: dict(row) for asset, row in metadata.items()}
    for pos in positions:
        asset = str(pos.get('base_asset') or '').upper()
        saved = _positive_float(pos.get('future_quanto_multiplier'))
        row = result.setdefault(asset, {'position_only': True})
        current = _positive_float(row.get('quanto_multiplier'))
        if saved is None or (current is not None and not math.isclose(current, saved, rel_tol=1e-9, abs_tol=0)):
            row['multiplier_conflict'] = True
            row['quanto_multiplier'] = None
        elif not row.get('multiplier_conflict'):
            row['quanto_multiplier'] = saved
    return result


def validate_position_multiplier(metadata: Mapping[str, Dict], position: Dict) -> float:
    if 'future_quanto_multiplier' not in position:
        return require_quanto_multiplier(metadata, position.get('base_asset'))
    merged = merge_position_multipliers(dict(metadata), [position])
    return require_quanto_multiplier(merged, position.get('base_asset'))


def execution_position_multiplier(metadata: Mapping[str, Dict], asset: str, fill: Dict) -> float:
    """Validate the unit snapshot before storing a newly executed position."""
    multiplier = require_quanto_multiplier(metadata, asset)
    if fill.get('quanto_multiplier') is not None:
        multiplier = validate_position_multiplier(metadata, {
            'base_asset': asset, 'future_quanto_multiplier': fill['quanto_multiplier'],
        })
    quantity = _positive_float(fill.get('exec_qty'))
    contracts = _positive_float(fill.get('exec_contracts'))
    if fill.get('exec_contracts') is not None and (contracts is None or not contracts.is_integer()):
        raise ValueError(f'Gate成交合约张数无效({asset})')
    if quantity is None or (contracts is not None and not math.isclose(quantity, contracts * multiplier, rel_tol=1e-9, abs_tol=1e-8)):
        raise ValueError(f'Gate成交数量与持仓乘数不一致({asset})')
    return multiplier


def retain_healthy_contract_meta(
    candidate: Dict[str, Dict],
    current: Optional[Dict[str, Dict]] = None,
) -> Dict[str, Dict]:
    if current is None:
        current = {}
    try:
        _guard_snapshot_count('Gate合约元数据缓存', len(candidate), len(current))
        for asset in candidate:
            if candidate[asset].get('multiplier_conflict'):
                continue  # Keep the per-asset rejection visible, not an older valid cache.
            require_quanto_multiplier(candidate, asset)
    except ValueError as exc:
        logger.error('拒绝异常Gate合约元数据缓存，保留上一版本 | %s', exc)
        return current
    return candidate


def retain_healthy_spot_meta(
    candidate: Dict[str, Dict],
    current: Optional[Dict[str, Dict]] = None,
) -> Dict[str, Dict]:
    if current is None:
        current = {}
    try:
        _guard_snapshot_count('Binance现货元数据缓存', len(candidate), len(current))
        for asset, meta in candidate.items():
            if _positive_float(meta.get('step_size')) is None:
                raise ValueError(f'Binance现货元数据step_size无效({asset})')
    except ValueError as exc:
        logger.error('拒绝异常Binance现货元数据缓存，保留上一版本 | %s', exc)
        return current
    return candidate
