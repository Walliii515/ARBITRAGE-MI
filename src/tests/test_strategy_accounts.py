import os
from unittest.mock import patch

import pytest

from common.strategy_accounts import (
    get_binance_credentials,
    get_gate_futures_credentials,
)


def test_forward_mainnet_uses_only_strategy_scoped_credentials():
    with patch.dict(os.environ, {
        'FORWARD_BINANCE_API_KEY': 'forward-binance-key',
        'FORWARD_BINANCE_API_SECRET': 'forward-binance-secret',
        'BINANCE_API_KEY': 'legacy-binance-key',
        'BINANCE_API_SECRET': 'legacy-binance-secret',
        'FORWARD_GATE_FUTURES_API_KEY': 'forward-gate-key',
        'FORWARD_GATE_FUTURES_API_SECRET': 'forward-gate-secret',
        'GATE_FUTURES_API_KEY': 'legacy-gate-key',
        'GATE_FUTURES_API_SECRET': 'legacy-gate-secret',
    }, clear=True):
        binance = get_binance_credentials('forward', mainnet=True)
        gate = get_gate_futures_credentials('forward', mainnet=True)

    assert binance.api_key == 'forward-binance-key'
    assert binance.api_secret == 'forward-binance-secret'
    assert gate.api_key == 'forward-gate-key'
    assert gate.api_secret == 'forward-gate-secret'


@pytest.mark.parametrize(
    ('loader', 'legacy_env', 'expected_name'),
    [
        (
            get_binance_credentials,
            {
                'BINANCE_API_KEY': 'legacy-binance-key',
                'BINANCE_API_SECRET': 'legacy-binance-secret',
            },
            'FORWARD_BINANCE_API_KEY',
        ),
        (
            get_gate_futures_credentials,
            {
                'GATE_FUTURES_API_KEY': 'legacy-gate-key',
                'GATE_FUTURES_API_SECRET': 'legacy-gate-secret',
            },
            'FORWARD_GATE_FUTURES_API_KEY',
        ),
    ],
)
def test_forward_mainnet_rejects_legacy_only_credentials(
    loader,
    legacy_env,
    expected_name,
):
    with patch.dict(os.environ, legacy_env, clear=True):
        with pytest.raises(RuntimeError, match=expected_name):
            loader('forward', mainnet=True)


def test_testnet_credentials_remain_available_without_forward_mainnet_keys():
    with patch.dict(os.environ, {
        'BINANCE_TESTNET_API_KEY': 'testnet-binance-key',
        'BINANCE_TESTNET_API_SECRET': 'testnet-binance-secret',
        'GATE_FUTURES_TESTNET_API_KEY': 'testnet-gate-key',
        'GATE_FUTURES_TESTNET_API_SECRET': 'testnet-gate-secret',
    }, clear=True):
        binance = get_binance_credentials('forward', mainnet=False)
        gate = get_gate_futures_credentials('forward', mainnet=False)

    assert binance.api_key == 'testnet-binance-key'
    assert binance.api_secret == 'testnet-binance-secret'
    assert gate.api_key == 'testnet-gate-key'
    assert gate.api_secret == 'testnet-gate-secret'
