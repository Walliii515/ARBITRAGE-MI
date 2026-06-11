# coding: utf-8
"""Read-only reverse strategy capital and reconciliation helpers."""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode

import requests

from common.logger import get_logger
from common.strategy_accounts import get_binance_credentials, get_gate_futures_credentials


BINANCE_BASE_URL = 'https://api1.binance.com'
GATE_BASE_URL = 'https://api.gateio.ws'
logger = get_logger(__name__)


@dataclass(frozen=True)
class ReverseAccountReadConfig:
    timeout_sec: int = 10
    recv_window_ms: int = 10000


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _binance_sign(secret: str, query_string: str) -> str:
    return hmac.new(secret.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()


def _binance_signed_get(path: str, params: Optional[Dict] = None, cfg: Optional[ReverseAccountReadConfig] = None) -> Dict:
    cfg = cfg or ReverseAccountReadConfig()
    creds = get_binance_credentials('reverse', mainnet=True)
    if not creds.api_key or not creds.api_secret:
        raise RuntimeError('reverse Binance API key/secret missing')
    payload = dict(params or {})
    payload['timestamp'] = int(time.time() * 1000)
    payload.setdefault('recvWindow', cfg.recv_window_ms)
    query = urlencode(payload)
    payload['signature'] = _binance_sign(creds.api_secret, query)
    resp = requests.get(
        BINANCE_BASE_URL + path,
        params=payload,
        headers={'X-MBX-APIKEY': creds.api_key},
        timeout=cfg.timeout_sec,
    )
    if resp.status_code != 200:
        raise RuntimeError(f'Binance reverse read HTTP {resp.status_code}: {resp.text[:200]}')
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _gate_sign(secret: str, method: str, api_path: str, query_string: str = '', body: str = '') -> Dict[str, str]:
    ts = str(int(time.time()))
    hashed_payload = hashlib.sha512(body.encode('utf-8')).hexdigest()
    sign_string = f'{method}\n{api_path}\n{query_string}\n{hashed_payload}\n{ts}'
    signature = hmac.new(secret.encode('utf-8'), sign_string.encode('utf-8'), hashlib.sha512).hexdigest()
    return {
        'KEY': '',
        'SIGN': signature,
        'Timestamp': ts,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def _gate_signed_get(api_path: str, query_string: str = '', cfg: Optional[ReverseAccountReadConfig] = None):
    cfg = cfg or ReverseAccountReadConfig()
    creds = get_gate_futures_credentials('reverse', mainnet=True)
    if not creds.api_key or not creds.api_secret:
        raise RuntimeError('reverse Gate futures API key/secret missing')
    headers = _gate_sign(creds.api_secret, 'GET', api_path, query_string, '')
    headers['KEY'] = creds.api_key
    url = GATE_BASE_URL + api_path
    if query_string:
        url += '?' + query_string
    resp = requests.get(url, headers=headers, timeout=cfg.timeout_sec)
    if resp.status_code != 200:
        raise RuntimeError(f'Gate reverse read HTTP {resp.status_code}: {resp.text[:200]}')
    return resp.json()


def _margin_assets_by_symbol(margin_account: Dict) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    for item in margin_account.get('userAssets') or []:
        asset = str(item.get('asset') or '').upper()
        if not asset:
            continue
        result[asset] = {
            'asset': asset,
            'free': _as_float(item.get('free')),
            'locked': _as_float(item.get('locked')),
            'borrowed': _as_float(item.get('borrowed')),
            'interest': _as_float(item.get('interest')),
            'netAsset': _as_float(item.get('netAsset')),
        }
    return result


def _nonzero_margin_assets(margin_account: Dict) -> List[Dict]:
    assets = []
    for item in _margin_assets_by_symbol(margin_account).values():
        if any(abs(float(item.get(key) or 0)) > 1e-12 for key in ('free', 'locked', 'borrowed', 'interest', 'netAsset')):
            assets.append(item)
    return sorted(assets, key=lambda row: row['asset'])


def _gate_positions_by_contract(positions: Iterable[Dict]) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    for item in positions or []:
        contract = str(item.get('contract') or '').upper()
        if contract:
            result[contract] = item
    return result


def get_reverse_capital_snapshot(cfg: Optional[ReverseAccountReadConfig] = None) -> Dict:
    """Return reverse account balances without touching forward accounts."""
    cfg = cfg or ReverseAccountReadConfig()
    errors: Dict[str, str] = {}
    try:
        margin_account = _binance_signed_get('/sapi/v1/margin/account', cfg=cfg)
    except Exception as exc:
        logger.warning('读取反向 Binance 全仓杠杆资金失败: %s', exc)
        errors['binance_cross_margin'] = str(exc)
        margin_account = {}
    try:
        gate_account = _gate_signed_get('/api/v4/futures/usdt/accounts', cfg=cfg)
    except Exception as exc:
        logger.warning('读取反向 Gate Futures 资金失败: %s', exc)
        errors['gate_futures'] = str(exc)
        gate_account = {}
    assets = _margin_assets_by_symbol(margin_account)
    return {
        'strategy': 'reverse',
        'timestamp': int(time.time()),
        'errors': errors,
        'binance_cross_margin': {
            'borrowEnabled': margin_account.get('borrowEnabled'),
            'tradeEnabled': margin_account.get('tradeEnabled'),
            'marginLevel': _as_float(margin_account.get('marginLevel'), 999.0),
            'totalAssetOfBtc': _as_float(margin_account.get('totalAssetOfBtc')),
            'totalLiabilityOfBtc': _as_float(margin_account.get('totalLiabilityOfBtc')),
            'totalNetAssetOfBtc': _as_float(margin_account.get('totalNetAssetOfBtc')),
            'USDT': assets.get('USDT', {'asset': 'USDT', 'free': 0, 'locked': 0, 'borrowed': 0, 'interest': 0, 'netAsset': 0}),
            'BNB': assets.get('BNB', {'asset': 'BNB', 'free': 0, 'locked': 0, 'borrowed': 0, 'interest': 0, 'netAsset': 0}),
            'nonzero_assets': _nonzero_margin_assets(margin_account),
        },
        'gate_futures': {
            'available': _as_float(gate_account.get('available')),
            'total': _as_float(gate_account.get('total')),
            'unrealised_pnl': _as_float(gate_account.get('unrealised_pnl')),
            'position_margin': _as_float(gate_account.get('position_margin')),
            'order_margin': _as_float(gate_account.get('order_margin')),
        },
    }


def build_reverse_reconciliation_rows(local_positions: List[Dict], cfg: Optional[ReverseAccountReadConfig] = None) -> Dict:
    """Compare local reverse holding rows against reverse exchange account snapshots."""
    cfg = cfg or ReverseAccountReadConfig()
    errors: Dict[str, str] = {}
    try:
        margin_account = _binance_signed_get('/sapi/v1/margin/account', cfg=cfg)
    except Exception as exc:
        logger.warning('读取反向 Binance 全仓杠杆对账快照失败: %s', exc)
        errors['binance_cross_margin'] = str(exc)
        margin_account = {}
    try:
        gate_positions_payload = _gate_signed_get('/api/v4/futures/usdt/positions', cfg=cfg)
    except Exception as exc:
        logger.warning('读取反向 Gate Futures 对账快照失败: %s', exc)
        errors['gate_futures_positions'] = str(exc)
        gate_positions_payload = []
    gate_positions = gate_positions_payload if isinstance(gate_positions_payload, list) else []
    margin_assets = _margin_assets_by_symbol(margin_account)
    gate_by_contract = _gate_positions_by_contract(gate_positions)

    rows: List[Dict] = []
    mismatch = 0
    for pos in local_positions:
        base_asset = str(pos.get('base_asset') or '').upper()
        contract = str(pos.get('future_contract') or f'{base_asset}_USDT').upper()
        local_borrow = _as_float(pos.get('borrow_qty')) - _as_float(pos.get('borrow_repaid_qty'))
        local_future = _as_float(pos.get('future_open_qty')) - _as_float(pos.get('future_close_qty'))
        margin_asset = margin_assets.get(base_asset, {})
        gate_pos = gate_by_contract.get(contract, {})
        exchange_borrowed = _as_float(margin_asset.get('borrowed'))
        exchange_interest = _as_float(margin_asset.get('interest'))
        exchange_future_size = _as_float(gate_pos.get('size'))
        is_match = (
            abs(exchange_borrowed - local_borrow) <= max(abs(local_borrow) * 0.01, 1e-8)
            and abs(abs(exchange_future_size) - abs(local_future)) <= max(abs(local_future) * 0.01, 1e-8)
        )
        if not is_match:
            mismatch += 1
        rows.append({
            'position_id': pos.get('id'),
            'base_asset': base_asset,
            'contract': contract,
            'status': pos.get('status'),
            'local_borrow_qty': local_borrow,
            'exchange_borrowed_qty': exchange_borrowed,
            'exchange_interest_qty': exchange_interest,
            'local_future_qty': local_future,
            'exchange_future_size': exchange_future_size,
            'exchange_margin_free': _as_float(margin_asset.get('free')),
            'exchange_margin_net_asset': _as_float(margin_asset.get('netAsset')),
            'is_match': is_match,
        })

    return {
        'strategy': 'reverse',
        'timestamp': int(time.time()),
        'errors': errors,
        'summary': {
            'local_holding': len(local_positions),
            'mismatch_count': mismatch,
            'match_count': len(local_positions) - mismatch,
            'marginLevel': _as_float(margin_account.get('marginLevel'), 999.0),
        },
        'rows': rows,
    }
