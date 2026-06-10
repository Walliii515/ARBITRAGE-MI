# coding: utf-8
"""
Binance Margin 借币利率与可借额度查询。

只读接口：
- /sapi/v1/margin/next-hourly-interest-rate
- /sapi/v1/margin/maxBorrowable
"""
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode

import requests

from common.logger import get_logger

logger = get_logger(__name__)


@dataclass
class BinanceMarginBorrowConfig:
    base_url: str
    api_key: str
    api_secret: str
    timeout_sec: int = 10
    recv_window_ms: int = 5000


class BinanceMarginBorrowClient:
    """Binance Margin 只读借币数据客户端。"""

    def __init__(self, cfg: BinanceMarginBorrowConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._time_offset_ms = 0

    def _sign(self, query_string: str) -> str:
        return hmac.new(
            self.cfg.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    def _sync_server_time(self) -> None:
        resp = self._session.get(f'{self.cfg.base_url}/api/v3/time', timeout=self.cfg.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f'Binance server time HTTP {resp.status_code}: {resp.text[:200]}')
        server_time = int(resp.json().get('serverTime') or 0)
        if server_time > 0:
            self._time_offset_ms = server_time - int(time.time() * 1000)

    def _signed_get_once(self, path: str, params: Dict) -> Dict | List:
        payload = dict(params)
        payload.setdefault('recvWindow', self.cfg.recv_window_ms)
        payload['timestamp'] = int(time.time() * 1000) + self._time_offset_ms
        query_string = urlencode(payload)
        payload['signature'] = self._sign(query_string)
        headers = {'X-MBX-APIKEY': self.cfg.api_key}
        resp = self._session.get(
            f'{self.cfg.base_url}{path}',
            params=payload,
            headers=headers,
            timeout=self.cfg.timeout_sec,
        )
        if resp.status_code != 200:
            raise RuntimeError(f'Binance margin {path} HTTP {resp.status_code}: {resp.text[:200]}')
        return resp.json()

    def _signed_get(self, path: str, params: Dict) -> Dict | List:
        try:
            return self._signed_get_once(path, params)
        except RuntimeError as exc:
            if '"code":-1021' not in str(exc) and 'outside of the recvWindow' not in str(exc):
                raise
            self._sync_server_time()
            return self._signed_get_once(path, params)

    @staticmethod
    def _chunks(items: List[str], size: int) -> Iterable[List[str]]:
        for i in range(0, len(items), size):
            yield items[i:i + size]

    def get_next_hourly_interest_rates(self, assets: Iterable[str], is_isolated: bool = False) -> Dict[str, float]:
        """查询资产下一小时借币利率，返回 asset -> hourly decimal rate。"""
        clean_assets = sorted({str(asset or '').strip().upper() for asset in assets if str(asset or '').strip()})
        if not clean_assets:
            return {}

        result: Dict[str, float] = {}
        for batch in self._chunks(clean_assets, 20):
            data = self._signed_get('/sapi/v1/margin/next-hourly-interest-rate', {
                'assets': ','.join(batch),
                'isIsolated': 'TRUE' if is_isolated else 'FALSE',
            })
            if not isinstance(data, list):
                continue
            for item in data:
                asset = str(item.get('asset') or '').upper()
                rate = item.get('nextHourlyInterestRate')
                if asset and rate is not None:
                    result[asset] = float(rate)
        return result

    def get_max_borrowable(self, asset: str, isolated_symbol: Optional[str] = None) -> Dict:
        """查询当前账号某资产最大可借额度。"""
        params = {'asset': asset.upper()}
        if isolated_symbol:
            params['isolatedSymbol'] = isolated_symbol.upper()
        data = self._signed_get('/sapi/v1/margin/maxBorrowable', params)
        if not isinstance(data, dict):
            return {}
        return {
            'amount': float(data.get('amount') or 0),
            'borrowLimit': float(data.get('borrowLimit') or 0),
        }

    def get_cross_margin_borrow_meta(
        self,
        assets: Iterable[str],
        max_borrowable_assets: int = 20,
    ) -> Dict[str, Dict]:
        """批量构建反向策略需要的借币元数据。"""
        clean_assets: List[str] = []
        seen = set()
        for asset in assets:
            clean_asset = str(asset or '').strip().upper()
            if not clean_asset or clean_asset in seen:
                continue
            seen.add(clean_asset)
            clean_assets.append(clean_asset)
        hourly_rates = self.get_next_hourly_interest_rates(clean_assets, is_isolated=False)
        result: Dict[str, Dict] = {
            asset: {
                'borrowable': asset in hourly_rates,
                'hourly_interest_rate': hourly_rates.get(asset),
                'borrow_limit': None,
            }
            for asset in clean_assets
        }

        for asset in clean_assets[:max(0, max_borrowable_assets)]:
            try:
                borrowable = self.get_max_borrowable(asset)
            except Exception as exc:
                logger.warning(f'Binance maxBorrowable 查询失败 | {asset} | {exc}')
                continue
            amount = borrowable.get('amount')
            limit = borrowable.get('borrowLimit')
            result.setdefault(asset, {})
            result[asset]['borrowable'] = (amount or 0) > 0
            result[asset]['borrow_limit'] = amount if amount is not None else limit
            result[asset]['account_borrow_limit'] = limit

        return result
