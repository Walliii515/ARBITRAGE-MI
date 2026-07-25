# coding: utf-8
"""Private wallet clients used only by the Binance-to-Gate fund transfer flow."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests


class FundApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        ambiguous: bool = False,
        status_code: int = 0,
        code: str = '',
    ):
        super().__init__(message)
        self.ambiguous = ambiguous
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class BinanceNetworkInfo:
    coin: str
    network: str
    withdraw_enabled: bool
    deposit_enabled: bool
    fee: Decimal
    minimum: Decimal
    precision_step: Decimal


def _required_env(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'资金划转凭据缺失: {name}')
    return value


class BinanceFundClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = 'https://api.binance.com',
        timeout_sec: float = 12.0,
        session=None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip('/')
        self.timeout_sec = timeout_sec
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None):
        values = dict(params or {})
        values['timestamp'] = int(time.time() * 1000)
        values['recvWindow'] = 10000
        query = urlencode(values)
        signature = hmac.new(
            self.api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        encoded = f'{query}&signature={signature}'
        kwargs = {
            'headers': {'X-MBX-APIKEY': self.api_key},
            'timeout': self.timeout_sec,
        }
        if method.upper() == 'GET':
            kwargs['params'] = encoded
        else:
            kwargs['data'] = encoded
            kwargs['headers']['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            response = self.session.request(
                method.upper(), f'{self.base_url}{path}', **kwargs
            )
        except requests.RequestException as exc:
            raise FundApiError(str(exc), ambiguous=True) from exc
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not response.ok:
            message = str(payload.get('msg') or payload.get('message') or response.text)[:500]
            raise FundApiError(
                message or f'Binance HTTP {response.status_code}',
                ambiguous=response.status_code >= 500 or response.status_code == 429,
                status_code=response.status_code,
                code=str(payload.get('code') or '') if isinstance(payload, dict) else '',
            )
        return payload

    def get_api_permissions(self) -> Dict[str, Any]:
        return self._request('GET', '/sapi/v1/account/apiRestrictions')

    def get_network_info(self, coin: str, network: str) -> BinanceNetworkInfo:
        rows = self._request('GET', '/sapi/v1/capital/config/getall')
        coin = coin.upper()
        network = network.upper()
        for item in rows or []:
            if str(item.get('coin', '')).upper() != coin:
                continue
            for row in item.get('networkList') or []:
                if str(row.get('network', '')).upper() != network:
                    continue
                return BinanceNetworkInfo(
                    coin=coin,
                    network=network,
                    withdraw_enabled=bool(row.get('withdrawEnable')),
                    deposit_enabled=bool(row.get('depositEnable')),
                    fee=Decimal(str(row.get('withdrawFee') or '0')),
                    minimum=Decimal(str(row.get('withdrawMin') or '0')),
                    precision_step=Decimal(str(row.get('withdrawIntegerMultiple') or '0.00000001')),
                )
        raise FundApiError(f'Binance 不支持 {coin} {network} 提现')

    def get_subaccount_free(self, email: str, asset: str) -> Decimal:
        payload = self._request('GET', '/sapi/v3/sub-account/assets', {'email': email})
        for row in payload.get('balances') or payload.get('assets') or []:
            if str(row.get('asset', '')).upper() == asset.upper():
                return Decimal(str(row.get('free') or '0'))
        return Decimal('0')

    def get_master_spot_free(self, asset: str) -> Decimal:
        payload = self._request('GET', '/api/v3/account', {'omitZeroBalances': 'true'})
        for row in payload.get('balances') or []:
            if str(row.get('asset', '')).upper() == asset.upper():
                return Decimal(str(row.get('free') or '0'))
        return Decimal('0')

    def universal_transfer(
        self,
        *,
        asset: str,
        amount: Decimal,
        client_id: str,
        from_email: Optional[str] = None,
        to_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            'fromAccountType': 'SPOT',
            'toAccountType': 'SPOT',
            'asset': asset,
            'amount': format(amount, 'f'),
            'clientTranId': client_id,
        }
        if from_email:
            params['fromEmail'] = from_email
        if to_email:
            params['toEmail'] = to_email
        return self._request('POST', '/sapi/v1/sub-account/universalTransfer', params)

    def universal_transfer_history(
        self,
        client_id: str,
        *,
        from_email: Optional[str] = None,
        to_email: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        params = {'clientTranId': client_id}
        if from_email:
            params['fromEmail'] = from_email
        if to_email:
            params['toEmail'] = to_email
        payload = self._request(
            'GET',
            '/sapi/v1/sub-account/universalTransfer',
            params,
        )
        return list(payload.get('result') or [])

    def withdraw(
        self,
        *,
        coin: str,
        network: str,
        address: str,
        amount: Decimal,
        order_id: str,
    ) -> Dict[str, Any]:
        return self._request('POST', '/sapi/v1/capital/withdraw/apply', {
            'coin': coin,
            'network': network,
            'address': address,
            'amount': format(amount, 'f'),
            'withdrawOrderId': order_id,
            'walletType': 0,
        })

    def withdraw_history(self, *, coin: str, order_id: str) -> List[Dict[str, Any]]:
        payload = self._request('GET', '/sapi/v1/capital/withdraw/history', {
            'coin': coin,
            'withdrawOrderId': order_id,
        })
        return list(payload or [])


class GateFundClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = 'https://api.gateio.ws',
        timeout_sec: float = 12.0,
        session=None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip('/')
        self.timeout_sec = timeout_sec
        self.session = session or requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ):
        query = urlencode(params or {})
        body_text = json.dumps(body, ensure_ascii=False, separators=(',', ':')) if body else ''
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha512(body_text.encode()).hexdigest()
        api_path = f'/api/v4{path}'
        sign_text = f'{method.upper()}\n{api_path}\n{query}\n{body_hash}\n{timestamp}'
        signature = hmac.new(
            self.api_secret.encode(), sign_text.encode(), hashlib.sha512
        ).hexdigest()
        try:
            response = self.session.request(
                method.upper(),
                f'{self.base_url}{api_path}',
                params=query or None,
                data=body_text or None,
                headers={
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'KEY': self.api_key,
                    'Timestamp': timestamp,
                    'SIGN': signature,
                },
                timeout=self.timeout_sec,
            )
        except requests.RequestException as exc:
            raise FundApiError(str(exc), ambiguous=True) from exc
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not response.ok:
            message = str(
                payload.get('message') or payload.get('label') or response.text
            )[:500]
            raise FundApiError(
                message or f'Gate HTTP {response.status_code}',
                ambiguous=response.status_code >= 500 or response.status_code == 429,
                status_code=response.status_code,
                code=str(payload.get('label') or '') if isinstance(payload, dict) else '',
            )
        return payload

    def deposit_address(self, currency: str) -> Dict[str, Any]:
        return self._request(
            'GET', '/wallet/deposit_address', params={'currency': currency}
        )

    def deposits(self, *, currency: str, start_at: int) -> List[Dict[str, Any]]:
        payload = self._request('GET', '/wallet/deposits', params={
            'currency': currency,
            'from': start_at,
            'limit': 500,
        })
        return list(payload or [])

    def subaccount_futures_balances(
        self, *, sub_uid: str, settle: str
    ) -> List[Dict[str, Any]]:
        payload = self._request(
            'GET',
            '/wallet/sub_account_futures_balances',
            params={'sub_uid': sub_uid, 'settle': settle},
        )
        return list(payload or [])

    def transfer_to_subaccount_futures(
        self,
        *,
        sub_uid: str,
        currency: str,
        amount: Decimal,
        client_id: str,
    ) -> Dict[str, Any]:
        return self._request('POST', '/wallet/sub_account_transfers', body={
            'sub_account': sub_uid,
            'sub_account_type': 'futures',
            'currency': currency,
            'amount': format(amount, 'f'),
            'direction': 'to',
            'client_order_id': client_id,
        })

    def subaccount_transfer_history(self) -> List[Dict[str, Any]]:
        payload = self._request('GET', '/wallet/sub_account_transfers')
        return list(payload or [])

    def subaccount_transfer_status(self, *, client_id: str) -> Dict[str, Any]:
        payload = self._request(
            'GET',
            '/wallet/order_status',
            params={'client_order_id': client_id},
        )
        return dict(payload or {})


def build_default_fund_clients() -> tuple[BinanceFundClient, GateFundClient]:
    return (
        BinanceFundClient(
            _required_env('BINANCE_MASTER_FUND_API_KEY'),
            _required_env('BINANCE_MASTER_FUND_API_SECRET'),
        ),
        GateFundClient(
            _required_env('GATE_MASTER_FUND_API_KEY'),
            _required_env('GATE_MASTER_FUND_API_SECRET'),
        ),
    )
