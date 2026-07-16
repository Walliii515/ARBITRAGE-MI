# coding: utf-8
"""Attach exchange-native Gate contract fields to local forward positions.

Forward positions always use Gate cross margin. ``initial_margin`` is allocated
across local rows only for display; it is never treated as independent collateral
and no contract/local-position MMR is derived here. Account risk comes exclusively
from the shared Gate cross-risk snapshot.
"""
from datetime import datetime
from typing import Dict, Iterable, List


def attach_gate_position_risk(positions: List[Dict], gate_positions: Iterable[Dict]) -> None:
    """将 Gate 实时仓位风险字段注入本地持仓行。"""
    gate_by_contract = _build_gate_position_map(gate_positions)
    holdings_by_contract = _group_holding_positions_by_contract(positions, gate_by_contract)
    contract_weights = {
        contract: sum(_position_weight(pos) for pos in rows)
        for contract, rows in holdings_by_contract.items()
    }
    contract_open_notional = {
        contract: sum(_position_open_notional(pos) for pos in rows)
        for contract, rows in holdings_by_contract.items()
    }
    for pos in positions:
        if pos.get('status') != 'holding':
            _clear_gate_risk_fields(pos)
            continue

        contract = _position_contract(pos)
        gate_pos = gate_by_contract.get(contract)
        if not gate_pos:
            _clear_gate_risk_fields(pos)
            continue

        initial_margin = _float_or_none(gate_pos.get('initial_margin'))
        unrealised_pnl = _float(gate_pos.get('unrealised_pnl'))
        maintenance_margin = _float(gate_pos.get('maintenance_margin'))
        share = _position_share(pos, holdings_by_contract.get(contract), contract_weights.get(contract, 0))

        pos['gate_mark_price'] = _float_or_none(gate_pos.get('mark_price'))
        pos['gate_liq_price'] = _float_or_none(gate_pos.get('liq_price'))
        pos['gate_contract_initial_margin'] = initial_margin
        pos['gate_contract_unrealised_pnl'] = unrealised_pnl
        pos['gate_contract_maintenance_margin'] = maintenance_margin
        pos['gate_contract_local_position_count'] = len(holdings_by_contract.get(contract) or [])
        pos['gate_contract_open_notional'] = contract_open_notional.get(contract, 0.0)
        pos['gate_initial_margin'] = initial_margin * share if initial_margin is not None else None
        pos['gate_unrealised_pnl'] = unrealised_pnl * share
        pos['gate_maintenance_margin'] = maintenance_margin * share
        gate_size = _float_or_none(gate_pos.get('size'))
        pos['gate_position_size'] = gate_size * share if gate_size is not None else None
        pos['gate_risk_updated_at'] = _format_ts(gate_pos.get('update_time'))


def _build_gate_position_map(gate_positions: Iterable[Dict]) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    for item in gate_positions or []:
        contract = str(item.get('contract') or '').upper()
        if not contract:
            continue
        size = _float(item.get('size'))
        if size == 0:
            continue
        # 本系统的 Gate leg 是空头；若同一合约双向持仓同时存在，优先展示空头风险。
        if contract not in result or size < 0:
            result[contract] = item
    return result


def _group_holding_positions_by_contract(
    positions: List[Dict],
    gate_by_contract: Dict[str, Dict],
) -> Dict[str, List[Dict]]:
    result: Dict[str, List[Dict]] = {}
    for pos in positions:
        if pos.get('status') != 'holding':
            continue
        contract = _position_contract(pos)
        if not contract or contract not in gate_by_contract:
            continue
        result.setdefault(contract, []).append(pos)
    return result


def _position_contract(pos: Dict) -> str:
    contract = str(pos.get('future_contract') or '').strip().upper()
    if contract:
        return contract
    base_asset = str(pos.get('base_asset') or '').strip().upper()
    return f"{base_asset}_USDT" if base_asset else ''


def _position_share(pos: Dict, contract_positions: List[Dict], total_weight: float) -> float:
    if not contract_positions:
        return 1.0
    if total_weight <= 0:
        return 1.0 / len(contract_positions)
    return _position_weight(pos) / total_weight


def _position_weight(pos: Dict) -> float:
    contracts = abs(_float(pos.get('future_open_contracts')))
    if contracts > 0:
        return contracts
    qty = abs(_float(pos.get('future_open_qty')))
    return qty if qty > 0 else 1.0


def _position_open_notional(pos: Dict) -> float:
    spot_amount = _float(pos.get('spot_open_amount'))
    if spot_amount > 0:
        return spot_amount
    future_qty = abs(_float(pos.get('future_open_qty')))
    future_price = _float(pos.get('future_open_price'))
    return future_qty * future_price if future_qty > 0 and future_price > 0 else 0.0


def _clear_gate_risk_fields(pos: Dict) -> None:
    pos['gate_mark_price'] = None
    pos['gate_liq_price'] = None
    pos['gate_contract_initial_margin'] = None
    pos['gate_contract_unrealised_pnl'] = None
    pos['gate_contract_maintenance_margin'] = None
    pos['gate_contract_local_position_count'] = None
    pos['gate_contract_open_notional'] = None
    pos['gate_initial_margin'] = None
    pos['gate_unrealised_pnl'] = None
    pos['gate_maintenance_margin'] = None
    pos['gate_position_size'] = None
    pos['gate_risk_updated_at'] = None


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _float_or_none(value):
    if value in (None, ''):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_ts(value):
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
