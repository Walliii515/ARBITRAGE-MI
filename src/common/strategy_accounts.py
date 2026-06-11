# coding: utf-8
"""Strategy-scoped exchange API credential helpers."""
import os
from dataclasses import dataclass
from typing import Iterable, Literal


StrategyName = Literal['forward', 'reverse']


@dataclass(frozen=True)
class ApiCredentials:
    api_key: str = ''
    api_secret: str = ''


def _first_env(names: Iterable[str]) -> str:
    for name in names:
        value = os.getenv(name, '')
        if value:
            return value
    return ''


def _normalize_strategy(strategy: str) -> StrategyName:
    normalized = str(strategy or '').strip().lower()
    if normalized not in ('forward', 'reverse'):
        raise ValueError(f'未知策略账户: {strategy}')
    return normalized  # type: ignore[return-value]


def get_binance_credentials(strategy: str, *, mainnet: bool = True) -> ApiCredentials:
    """Return Binance credentials for the requested strategy.

    Forward mainnet keeps legacy fallback for rollout compatibility. Reverse mainnet
    intentionally has no legacy fallback to avoid accidentally using forward funds.
    """
    normalized = _normalize_strategy(strategy)
    if not mainnet:
        return ApiCredentials(
            api_key=os.getenv('BINANCE_TESTNET_API_KEY', ''),
            api_secret=os.getenv('BINANCE_TESTNET_API_SECRET', ''),
        )

    if normalized == 'forward':
        return ApiCredentials(
            api_key=_first_env(('FORWARD_BINANCE_API_KEY', 'BINANCE_API_KEY')),
            api_secret=_first_env(('FORWARD_BINANCE_API_SECRET', 'BINANCE_API_SECRET')),
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
        return ApiCredentials(
            api_key=_first_env(('FORWARD_GATE_FUTURES_API_KEY', 'GATE_FUTURES_API_KEY')),
            api_secret=_first_env(('FORWARD_GATE_FUTURES_API_SECRET', 'GATE_FUTURES_API_SECRET')),
        )
    return ApiCredentials(
        api_key=os.getenv('REVERSE_GATE_FUTURES_API_KEY', ''),
        api_secret=os.getenv('REVERSE_GATE_FUTURES_API_SECRET', ''),
    )
