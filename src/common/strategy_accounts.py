# coding: utf-8
"""Strategy-scoped exchange API credential helpers."""
import os
from dataclasses import dataclass
from typing import Literal


StrategyName = Literal['forward', 'reverse']


@dataclass(frozen=True)
class ApiCredentials:
    api_key: str = ''
    api_secret: str = ''


def _required_credentials(
    api_key_name: str,
    api_secret_name: str,
    *,
    account_label: str,
) -> ApiCredentials:
    credentials = ApiCredentials(
        api_key=os.getenv(api_key_name, ''),
        api_secret=os.getenv(api_secret_name, ''),
    )
    missing = [
        name
        for name, value in (
            (api_key_name, credentials.api_key),
            (api_secret_name, credentials.api_secret),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f'{account_label}凭据缺失: {", ".join(missing)}；'
            '系统不会回退到旧账户变量'
        )
    return credentials


def _normalize_strategy(strategy: str) -> StrategyName:
    normalized = str(strategy or '').strip().lower()
    if normalized not in ('forward', 'reverse'):
        raise ValueError(f'未知策略账户: {strategy}')
    return normalized  # type: ignore[return-value]


def get_binance_credentials(strategy: str, *, mainnet: bool = True) -> ApiCredentials:
    """Return Binance credentials for the requested strategy."""
    normalized = _normalize_strategy(strategy)
    if not mainnet:
        return ApiCredentials(
            api_key=os.getenv('BINANCE_TESTNET_API_KEY', ''),
            api_secret=os.getenv('BINANCE_TESTNET_API_SECRET', ''),
        )

    if normalized == 'forward':
        return _required_credentials(
            'FORWARD_BINANCE_API_KEY',
            'FORWARD_BINANCE_API_SECRET',
            account_label='正向 Binance',
        )
    return ApiCredentials(
        api_key=os.getenv('REVERSE_BINANCE_API_KEY', ''),
        api_secret=os.getenv('REVERSE_BINANCE_API_SECRET', ''),
    )


def get_gate_futures_credentials(strategy: str, *, mainnet: bool = True) -> ApiCredentials:
    """Return Gate USDT futures credentials for the requested strategy."""
    normalized = _normalize_strategy(strategy)
    if not mainnet:
        return ApiCredentials(
            api_key=os.getenv('GATE_FUTURES_TESTNET_API_KEY', ''),
            api_secret=os.getenv('GATE_FUTURES_TESTNET_API_SECRET', ''),
        )

    if normalized == 'forward':
        return _required_credentials(
            'FORWARD_GATE_FUTURES_API_KEY',
            'FORWARD_GATE_FUTURES_API_SECRET',
            account_label='正向 Gate futures',
        )
    return ApiCredentials(
        api_key=os.getenv('REVERSE_GATE_FUTURES_API_KEY', ''),
        api_secret=os.getenv('REVERSE_GATE_FUTURES_API_SECRET', ''),
    )
