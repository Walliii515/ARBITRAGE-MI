import os
from unittest.mock import patch

import pytest

from common.fund_transfer_config import get_fund_transfer_destination


def test_destination_is_loaded_only_from_fixed_environment_variables():
    with patch.dict(os.environ, {
        'FUND_TRANSFER_COIN': 'usdt',
        'FUND_TRANSFER_NETWORK': 'bsc',
        'FUND_TRANSFER_GATE_DEPOSIT_ADDRESS': '0x1234567890abcdef',
    }, clear=True):
        destination = get_fund_transfer_destination()

    assert destination.coin == 'USDT'
    assert destination.network == 'BSC'
    assert destination.address == '0x1234567890abcdef'


@pytest.mark.parametrize(
    'missing_name',
    [
        'FUND_TRANSFER_COIN',
        'FUND_TRANSFER_NETWORK',
        'FUND_TRANSFER_GATE_DEPOSIT_ADDRESS',
    ],
)
def test_destination_rejects_missing_required_values(missing_name):
    values = {
        'FUND_TRANSFER_COIN': 'USDT',
        'FUND_TRANSFER_NETWORK': 'BSC',
        'FUND_TRANSFER_GATE_DEPOSIT_ADDRESS': '0x1234567890abcdef',
    }
    values.pop(missing_name)

    with patch.dict(os.environ, values, clear=True):
        with pytest.raises(RuntimeError, match=missing_name):
            get_fund_transfer_destination()


def test_destination_rejects_placeholder_address():
    with patch.dict(os.environ, {
        'FUND_TRANSFER_COIN': 'USDT',
        'FUND_TRANSFER_NETWORK': 'BSC',
        'FUND_TRANSFER_GATE_DEPOSIT_ADDRESS': '<Gate 主账户对应网络的充值地址>',
    }, clear=True):
        with pytest.raises(RuntimeError, match='占位值'):
            get_fund_transfer_destination()


def test_destination_rejects_address_with_whitespace():
    with patch.dict(os.environ, {
        'FUND_TRANSFER_COIN': 'USDT',
        'FUND_TRANSFER_NETWORK': 'BSC',
        'FUND_TRANSFER_GATE_DEPOSIT_ADDRESS': '0x1234 5678',
    }, clear=True):
        with pytest.raises(RuntimeError, match='地址不能包含空白字符'):
            get_fund_transfer_destination()
