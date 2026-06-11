# coding: utf-8
"""
Gate 合约仓位风险字段富化。

Gate 的强平预警邮件使用合约聚合仓位口径；本地 mi_trade_position
可能把同一合约拆成多条持仓，因此这里按 Gate contract 将同一风险值
贴到所有本地持仓行。

Gate 页面展示的 MMR 口径是 仓位权益 / 维持保证金，其中仓位权益等于
逐仓保证金加未实现盈亏。只用 margin / maintenance_margin 会在浮亏时
高估安全度，导致系统晚于交易所预警。
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
    for pos in positions:
        if pos.get('status') != 'holding':
            _clear_gate_risk_fields(pos)
            continue

        contract = str(pos.get('future_contract') or '').upper()
        gate_pos = gate_by_contract.get(contract)
        if not gate_pos:
            _clear_gate_risk_fields(pos)
            continue

        margin = _float(gate_pos.get('margin'))
        unrealised_pnl = _float(gate_pos.get('unrealised_pnl'))
        margin_equity = margin + unrealised_pnl
        maintenance_margin = _float(gate_pos.get('maintenance_margin'))
        rate = margin_equity / maintenance_margin * 100 if maintenance_margin > 0 else None
        share = _position_share(pos, holdings_by_contract.get(contract), contract_weights.get(contract, 0))

        pos['gate_mark_price'] = _float_or_none(gate_pos.get('mark_price'))
        pos['gate_liq_price'] = _float_or_none(gate_pos.get('liq_price'))
        pos['gate_contract_position_margin'] = margin
        pos['gate_contract_position_margin_equity'] = margin_equity
        pos['gate_contract_unrealised_pnl'] = unrealised_pnl
        pos['gate_contract_maintenance_margin'] = maintenance_margin
        pos['gate_position_margin'] = margin * share
        pos['gate_position_margin_equity'] = margin_equity * share
        pos['gate_unrealised_pnl'] = unrealised_pnl * share
        pos['gate_maintenance_margin'] = maintenance_margin * share
        pos['gate_maintenance_margin_rate'] = round(rate, 2) if rate is not None else None
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
        contract = str(pos.get('future_contract') or '').upper()
        if not contract or contract not in gate_by_contract:
            continue
        result.setdefault(contract, []).append(pos)
    return result


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


def _clear_gate_risk_fields(pos: Dict) -> None:
    pos['gate_mark_price'] = None
    pos['gate_liq_price'] = None
    pos['gate_contract_position_margin'] = None
    pos['gate_contract_position_margin_equity'] = None
    pos['gate_contract_unrealised_pnl'] = None
    pos['gate_contract_maintenance_margin'] = None
    pos['gate_position_margin'] = None
    pos['gate_position_margin_equity'] = None
    pos['gate_unrealised_pnl'] = None
    pos['gate_maintenance_margin'] = None
    pos['gate_maintenance_margin_rate'] = None
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
