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
        self.source_errors = {}

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
        assets = {
            str(row.get('base_asset') or '').upper()
            for row in rows
            if row.get('base_asset')
        }
        from common.config import config
        if config.get_trade_mode() != 'virtual':
            from calc.reconciliation import build_exchange_config, get_ignored_binance_spot_assets
            from calc.real_executor import RealExecutor
            executor = RealExecutor(build_exchange_config(), leverage=0)
            ignored = get_ignored_binance_spot_assets() | {'USDT', 'USDC', 'FDUSD'}
            for source, fetch in [('gate_positions', executor.fetch_gate_futures_positions),
                                  ('binance_balances', executor.fetch_binance_account_balances)]:
                try:
                    for row in fetch():
                        asset = str(row.get('base_asset') or row.get('asset') or '').upper()
                        quantity = row.get('size') if source == 'gate_positions' else row.get('total')
                        if quantity is None:
                            quantity = float(row.get('free') or 0) + float(row.get('locked') or 0)
                        if asset and asset not in ignored and abs(float(quantity or 0)) > 0:
                            assets.add(asset)
                except Exception as exc:
                    self.source_errors[source] = str(exc)[:300]
        return assets

    def build_report(self, assets: Optional[Iterable[str]] = None) -> Dict:
        self.source_errors = {}
        monitored = {str(a or '').upper().strip() for a in (assets or self.get_monitored_assets()) if str(a or '').strip()}
        risks: List[Dict] = []
        source_errors = self.source_errors

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
        payload = resp.json()
        rows = payload if isinstance(payload, list) else []
        present = {str(row.get('name') or '').upper() for row in rows}
        for asset in sorted(monitored):
            contract = f'{asset}_{self.cfg.settle.upper()}'
            if contract in present:
                continue
            try:
                single = requests.get(
                    f'https://api.gateio.ws/api/v4/futures/{self.cfg.settle.lower()}/contracts/{contract}',
                    timeout=self.cfg.timeout_sec,
                )
                single.raise_for_status()
                item = single.json()
                if not isinstance(item, dict) or item.get('name') != contract:
                    raise ValueError('contract response identity mismatch')
                rows.append(item)
            except Exception as exc:
                self.source_errors[f'gate:{asset}'] = str(exc)[:300]
                rows.append({'name': contract, 'status': 'unknown'})
        now = _now()
        cutoff = now + timedelta(days=max(int(self.cfg.lookahead_days or 30), 1))
        risks: List[Dict] = []
        for contract in rows:
            name = str(contract.get('name') or '').upper()
            base = _base_from_contract(name, self.cfg.settle)
            if base not in monitored:
                continue
            status = str(contract.get('status') or '').lower()
            in_delisting = bool(contract.get('in_delisting'))
            reduce_only_at = _dt_from_ms(contract.get('delisting_time'))
            delisted_at = _dt_from_ms(contract.get('delisted_time'))
            schedule_at = reduce_only_at or delisted_at
            is_active_delist = status in {'delisting', 'delisted'} or in_delisting
            is_upcoming_delist = bool(schedule_at and schedule_at <= cutoff)
            if status == 'trading' and not is_active_delist and not is_upcoming_delist:
                continue

            effective_delist_at = delisted_at or reduce_only_at
            days_left = None
            if effective_delist_at is not None:
                days_left = round((effective_delist_at - now).total_seconds() / 86400, 2)

            if is_active_delist:
                risk_type = 'contract_status'
                risk_status = status or 'in_delisting'
                level = 'critical'
                message = f"Gate合约状态={status or 'unknown'}，已进入下架流程"
            elif is_upcoming_delist:
                risk_type = 'delist_schedule'
                risk_status = 'scheduled'
                level = 'critical' if days_left is not None and days_left <= 7 else 'warning'
                message = f"Gate合约已计划下架，当前状态={status or 'unknown'}"
            else:
                risk_type = 'contract_status'
                risk_status = status or 'unknown'
                level = 'warning'
                message = f"Gate合约状态={status or 'unknown'}"

            risks.append({
                'risk_key': _risk_key('gate', base, risk_type),
                'base_asset': base,
                'exchange': 'gate',
                'market_type': 'future',
                'symbol': name,
                'risk_type': risk_type,
                'risk_level': level,
                'status': risk_status,
                'contract_status': status or None,
                'reduce_only_at': reduce_only_at.strftime('%Y-%m-%d %H:%M:%S') if reduce_only_at else None,
                'delist_at': effective_delist_at.strftime('%Y-%m-%d %H:%M:%S') if effective_delist_at else None,
                'days_left': days_left,
                'message': message,
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
        present = {str(item.get('symbol') or '').upper() for item in symbols}
        for asset in sorted(monitored):
            symbol = f'{asset}USDT'
            if symbol in present:
                continue
            try:
                single = requests.get('https://data-api.binance.vision/api/v3/exchangeInfo',
                                      params={'symbol': symbol}, timeout=self.cfg.timeout_sec)
                single.raise_for_status()
                matches = (single.json() or {}).get('symbols', [])
                if not matches or any(item.get('symbol') != symbol for item in matches):
                    raise ValueError('spot response identity mismatch')
                symbols.extend(matches)
            except Exception as exc:
                self.source_errors[f'binance:{asset}'] = str(exc)[:300]
                symbols.append({'symbol': symbol, 'baseAsset': asset, 'status': 'UNKNOWN'})
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
                'risk_level': 'warning' if status == 'UNKNOWN' else 'critical',
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
