# coding: utf-8
"""Exchange delist risk checks for monitored assets."""
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urlencode

import requests

from common.database import db_manager
from common.logger import get_logger
from common.strategy_accounts import get_binance_credentials

logger = get_logger(__name__)


@dataclass
class DelistRiskConfig:
    lookahead_days: int = 30
    timeout_sec: int = 10
    settle: str = 'USDT'


def _now() -> datetime:
    return datetime.now()


def _base_from_contract(contract: str, settle: str = 'USDT') -> str:
    value = str(contract or '').upper().strip()
    suffix = f'_{settle.upper()}'
    if value.endswith(suffix):
        return value[:-len(suffix)]
    return value.split('_', 1)[0] if '_' in value else value


def _base_from_symbol(symbol: str, quote: str = 'USDT') -> str:
    value = str(symbol or '').upper().strip()
    quote = quote.upper()
    return value[:-len(quote)] if value.endswith(quote) else value


def _dt_from_ms(value) -> Optional[datetime]:
    try:
        if value is None:
            return None
        number = int(float(value))
        if number <= 0:
            return None
        if number > 10_000_000_000:
            return datetime.fromtimestamp(number / 1000)
        return datetime.fromtimestamp(number)
    except Exception:
        return None


def _risk_key(exchange: str, base_asset: str, risk_type: str) -> str:
    return f'{exchange}:{base_asset}:{risk_type}'


class DelistRiskMonitor:
    def __init__(self, cfg: Optional[DelistRiskConfig] = None):
        self.cfg = cfg or DelistRiskConfig()

    def get_monitored_assets(self) -> Set[str]:
        """Assets that may be displayed or traded: active assets plus holdings."""
        sql = """
            SELECT UPPER(TRIM(base_asset)) AS base_asset
            FROM mi_base_asset
            WHERE is_valid = 'Y'
              AND base_asset IS NOT NULL
              AND TRIM(base_asset) <> ''
            UNION
            SELECT UPPER(TRIM(base_asset)) AS base_asset
            FROM mi_trade_position
            WHERE status = 'holding'
              AND base_asset IS NOT NULL
              AND TRIM(base_asset) <> ''
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        return {
            str(row.get('base_asset') or '').upper()
            for row in rows
            if row.get('base_asset')
        }

    def build_report(self, assets: Optional[Iterable[str]] = None) -> Dict:
        monitored = {str(a or '').upper().strip() for a in (assets or self.get_monitored_assets()) if str(a or '').strip()}
        risks: List[Dict] = []
        source_errors: Dict[str, str] = {}

        try:
            risks.extend(self._gate_risks(monitored))
        except Exception as e:
            source_errors['gate'] = str(e)[:300]
            logger.warning(f'Gate 下架风险检查失败: {e}', exc_info=True)

        try:
            risks.extend(self._binance_schedule_risks(monitored))
        except Exception as e:
            source_errors['binance_delist_schedule'] = str(e)[:300]
            logger.warning(f'Binance 下架计划检查失败: {e}')

        try:
            risks.extend(self._binance_exchange_info_risks(monitored))
        except Exception as e:
            source_errors['binance_exchange_info'] = str(e)[:300]
            logger.warning(f'Binance 现货状态检查失败: {e}', exc_info=True)

        risks = self._dedupe_risks(risks)
        risks.sort(key=lambda item: (
            {'critical': 0, 'warning': 1, 'info': 2}.get(item.get('risk_level'), 9),
            item.get('delist_at') or '9999-12-31 23:59:59',
            item.get('base_asset') or '',
        ))
        return {
            'items': risks,
            'summary': {
                'total': len(risks),
                'critical': sum(1 for item in risks if item.get('risk_level') == 'critical'),
                'warning': sum(1 for item in risks if item.get('risk_level') == 'warning'),
            },
            'source_errors': source_errors,
            'checked_at': _now().strftime('%Y-%m-%d %H:%M:%S'),
            'lookahead_days': self.cfg.lookahead_days,
        }

    def _gate_risks(self, monitored: Set[str]) -> List[Dict]:
        resp = requests.get(
            f'https://api.gateio.ws/api/v4/futures/{self.cfg.settle.lower()}/contracts',
            timeout=self.cfg.timeout_sec,
        )
        resp.raise_for_status()
        rows = resp.json() if isinstance(resp.json(), list) else []
        risks: List[Dict] = []
        for contract in rows:
            name = str(contract.get('name') or '').upper()
            base = _base_from_contract(name, self.cfg.settle)
            if base not in monitored:
                continue
            status = str(contract.get('status') or '').lower()
            in_delisting = bool(contract.get('in_delisting'))
            if status == 'trading' and not in_delisting:
                continue
            level = 'critical' if status in {'delisted', 'delisting'} or in_delisting else 'warning'
            risks.append({
                'risk_key': _risk_key('gate', base, status or 'in_delisting'),
                'base_asset': base,
                'exchange': 'gate',
                'market_type': 'future',
                'symbol': name,
                'risk_type': 'contract_status',
                'risk_level': level,
                'status': status or None,
                'delist_at': None,
                'days_left': None,
                'message': f"Gate合约状态={status or 'unknown'}" + ('，已进入下架流程' if in_delisting else ''),
            })
        return risks

    def _binance_schedule_risks(self, monitored: Set[str]) -> List[Dict]:
        creds = get_binance_credentials('forward', mainnet=True)
        if not creds.api_key or not creds.api_secret:
            return []
        params = {
            'timestamp': int(time.time() * 1000),
            'recvWindow': 5000,
        }
        query = urlencode(params)
        signature = hmac.new(creds.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f'https://api.binance.com/sapi/v1/spot/delist-schedule?{query}&signature={signature}'
        resp = requests.get(url, headers={'X-MBX-APIKEY': creds.api_key}, timeout=self.cfg.timeout_sec)
        if resp.status_code in (401, 403) or resp.status_code == 400:
            raise RuntimeError(f'Binance delist schedule HTTP {resp.status_code}: {resp.text[:200]}')
        resp.raise_for_status()
        rows = resp.json() if isinstance(resp.json(), list) else []
        cutoff = _now() + timedelta(days=max(int(self.cfg.lookahead_days or 30), 1))
        risks: List[Dict] = []
        for item in rows:
            delist_at = _dt_from_ms(item.get('delistTime') or item.get('delist_time') or item.get('delistDate'))
            if delist_at and delist_at > cutoff:
                continue
            symbols = item.get('symbols') or item.get('symbol') or []
            if isinstance(symbols, str):
                symbols = [symbols]
            for symbol in symbols:
                symbol = str(symbol or '').upper()
                base = _base_from_symbol(symbol)
                if base not in monitored:
                    continue
                days_left = (delist_at - _now()).days if delist_at else None
                risks.append({
                    'risk_key': _risk_key('binance', base, 'delist_schedule'),
                    'base_asset': base,
                    'exchange': 'binance',
                    'market_type': 'spot',
                    'symbol': symbol,
                    'risk_type': 'delist_schedule',
                    'risk_level': 'critical' if days_left is not None and days_left <= 7 else 'warning',
                    'status': 'scheduled',
                    'delist_at': delist_at.strftime('%Y-%m-%d %H:%M:%S') if delist_at else None,
                    'days_left': days_left,
                    'message': 'Binance现货已进入下架计划',
                })
        return risks

    def _binance_exchange_info_risks(self, monitored: Set[str]) -> List[Dict]:
        resp = requests.get('https://data-api.binance.vision/api/v3/exchangeInfo', timeout=self.cfg.timeout_sec)
        resp.raise_for_status()
        symbols = (resp.json() or {}).get('symbols', [])
        risks: List[Dict] = []
        for item in symbols if isinstance(symbols, list) else []:
            symbol = str(item.get('symbol') or '').upper()
            if not symbol.endswith('USDT'):
                continue
            base = str(item.get('baseAsset') or _base_from_symbol(symbol)).upper()
            if base not in monitored:
                continue
            status = str(item.get('status') or '').upper()
            spot_allowed = bool(item.get('isSpotTradingAllowed', True))
            if status == 'TRADING' and spot_allowed:
                continue
            risks.append({
                'risk_key': _risk_key('binance', base, status or 'spot_disabled'),
                'base_asset': base,
                'exchange': 'binance',
                'market_type': 'spot',
                'symbol': symbol,
                'risk_type': 'symbol_status',
                'risk_level': 'critical',
                'status': status,
                'delist_at': None,
                'days_left': None,
                'message': f'Binance现货状态={status or "unknown"}，spot_allowed={spot_allowed}',
            })
        return risks

    @staticmethod
    def _dedupe_risks(risks: List[Dict]) -> List[Dict]:
        by_key: Dict[str, Dict] = {}
        for item in risks:
            key = item.get('risk_key') or f"{item.get('exchange')}:{item.get('base_asset')}:{item.get('risk_type')}"
            existing = by_key.get(key)
            if not existing:
                by_key[key] = item
                continue
            current_rank = {'critical': 0, 'warning': 1, 'info': 2}.get(item.get('risk_level'), 9)
            existing_rank = {'critical': 0, 'warning': 1, 'info': 2}.get(existing.get('risk_level'), 9)
            if current_rank < existing_rank:
                by_key[key] = item
        return list(by_key.values())
