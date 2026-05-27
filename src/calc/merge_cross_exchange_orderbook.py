# coding: utf-8
"""
跨交易所订单簿拼接
按 base_asset 将 Gate 永续与 Binance 现货本地订单簿合并为宽表
"""
from typing import Any, Dict, List

LEVEL = 5

_SPOT_NULL_FIELDS = {}
for _side in ('bid', 'ask'):
    for _i in range(1, LEVEL + 1):
        _SPOT_NULL_FIELDS[f'spot_price_{_side}_{_i}'] = None
        _SPOT_NULL_FIELDS[f'spot_volume_{_side}_{_i}'] = None


def gate_contract_to_spot_symbol(contract: str) -> str:
    """Gate 合约名转 Binance 现货 symbol，如 BTC_USDT -> BTCUSDT"""
    return contract.replace('_', '').upper()


def _base_asset_from_contract(contract: str) -> str:
    if '_' in contract:
        return contract.split('_')[0]
    return contract


def contracts_to_spot_items(contracts: List[str]) -> List[dict]:
    """
    从 Gate 合约列表生成 Binance 现货订阅项（按 base_asset 去重）

    Returns:
        [{'symbol': 'BTCUSDT', 'base_asset': 'BTC'}, ...]
    """
    seen: set = set()
    items: List[dict] = []
    for contract in contracts:
        if not contract or not contract.strip():
            continue
        base_asset = _base_asset_from_contract(contract.strip())
        if base_asset in seen:
            continue
        seen.add(base_asset)
        items.append({
            'symbol': gate_contract_to_spot_symbol(contract.strip()),
            'base_asset': base_asset,
        })
    return items


def _spot_has_data(spot_row: Dict[str, Any]) -> bool:
    for i in range(1, LEVEL + 1):
        if spot_row.get(f'spot_price_bid_{i}') is not None:
            return True
        if spot_row.get(f'spot_price_ask_{i}') is not None:
            return True
    return False


def _copy_level_fields(row: Dict[str, Any], prefix: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for side in ('bid', 'ask'):
        for i in range(1, LEVEL + 1):
            price_key = f'{prefix}_price_{side}_{i}'
            volume_key = f'{prefix}_volume_{side}_{i}'
            out[price_key] = row.get(price_key)
            out[volume_key] = row.get(volume_key)
    return out


def _empty_spot_fields() -> Dict[str, Any]:
    return {
        'spot_update_id': None,
        'spot_update_time': None,
        'spot_ready': False,
        **_SPOT_NULL_FIELDS,
    }


def merge_orderbook_records(
    future_rows: List[Dict[str, Any]],
    spot_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    按 base_asset 拼接 Gate 永续与 Binance 现货订单簿

    以外层 Gate 合约行为驱动；spot 侧按 base_asset 查找，缺失则 spot 字段为 null。

    Args:
        future_rows: Gate OrderBookManager.to_records() 输出
        spot_rows: Binance OrderBookManager.to_records() 输出

    Returns:
        合并后的宽表行列表
    """
    spot_by_base: Dict[str, Dict[str, Any]] = {}
    for row in spot_rows:
        base = row.get('base_asset')
        if base:
            spot_by_base[str(base)] = row

    merged: List[Dict[str, Any]] = []
    for future in future_rows:
        contract = future.get('contract', '')
        base_asset = future.get('base_asset') or _base_asset_from_contract(contract)
        spot = spot_by_base.get(str(base_asset))

        out: Dict[str, Any] = {
            'base_asset': base_asset,
            'contract': contract,
            'symbol': (
                spot.get('symbol') if spot
                else gate_contract_to_spot_symbol(contract)
            ),
            'future_update_id': future.get('update_id'),
            'future_update_time': future.get('update_time'),
        }
        out.update(_copy_level_fields(future, 'future'))

        if spot and _spot_has_data(spot):
            out['spot_update_id'] = spot.get('update_id')
            out['spot_update_time'] = spot.get('update_time')
            out['spot_ready'] = True
            out.update(_copy_level_fields(spot, 'spot'))
        else:
            out.update(_empty_spot_fields())

        merged.append(out)

    return merged
