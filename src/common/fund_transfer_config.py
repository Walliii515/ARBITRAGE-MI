# coding: utf-8
"""Fixed destination configuration for Binance-to-Gate fund transfers."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class FundTransferDestination:
    coin: str
    network: str
    address: str


_PLACEHOLDER_VALUES = {'changeme', 'todo'}


def _required_env(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'资金划转配置缺失: {name}')
    if (
        value.lower() in _PLACEHOLDER_VALUES
        or (value.startswith('<') and value.endswith('>'))
    ):
        raise RuntimeError(f'资金划转配置仍为占位值: {name}')
    return value


def get_fund_transfer_destination() -> FundTransferDestination:
    """Load the only allowed on-chain withdrawal destination from env."""
    coin = _required_env('FUND_TRANSFER_COIN').upper()
    network = _required_env('FUND_TRANSFER_NETWORK').upper()
    address = _required_env('FUND_TRANSFER_GATE_DEPOSIT_ADDRESS')

    if any(char.isspace() for char in coin):
        raise RuntimeError('资金划转币种不能包含空白字符')
    if any(char.isspace() for char in network):
        raise RuntimeError('资金划转网络不能包含空白字符')
    if any(char.isspace() for char in address):
        raise RuntimeError('Gate 充值地址不能包含空白字符')

    return FundTransferDestination(
        coin=coin,
        network=network,
        address=address,
    )
