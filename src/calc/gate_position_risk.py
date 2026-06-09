# coding: utf-8
"""
Gate 合约仓位风险字段富化。

Gate 的强平预警邮件使用合约聚合仓位口径；本地 mi_trade_position
可能把同一合约拆成多条持仓，因此这里按 Gate contract 将同一风险值
贴到所有本地持仓行。
"""
from datetime import datetime
from typing import Dict, Iterable, List


def attach_gate_position_risk(positions: List[Dict], gate_positions: Iterable[Dict]) -> None:
    """将 Gate 实时仓位风险字段注入本地持仓行。"""
    gate_by_contract = _build_gate_position_map(gate_positions)
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
        maintenance_margin = _float(gate_pos.get('maintenance_margin'))
        rate = margin / maintenance_margin * 100 if maintenance_margin > 0 else None

        pos['gate_mark_price'] = _float_or_none(gate_pos.get('mark_price'))
        pos['gate_liq_price'] = _float_or_none(gate_pos.get('liq_price'))
        pos['gate_position_margin'] = margin
        pos['gate_maintenance_margin'] = maintenance_margin
        pos['gate_maintenance_margin_rate'] = round(rate, 2) if rate is not None else None
        pos['gate_position_size'] = _float_or_none(gate_pos.get('size'))
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


def _clear_gate_risk_fields(pos: Dict) -> None:
    pos['gate_mark_price'] = None
    pos['gate_liq_price'] = None
    pos['gate_position_margin'] = None
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
