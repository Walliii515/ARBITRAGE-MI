import asyncio
import inspect
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from common.errors import AppError

from api.auth import verify_user_password
from api.trading_api import (
    FundTransferCreateRequest,
    create_fund_transfer,
    get_fund_transfer_limits,
)
from calc.fund_transfer_service import FundTransferService


class CursorContext:
    def __init__(self, row):
        self.cursor = MagicMock()
        self.cursor.fetchone.return_value = row

    def __enter__(self):
        return self.cursor

    def __exit__(self, *_):
        return False


def test_password_reauthentication_uses_token_user_id():
    with patch('api.auth.db_manager.get_cursor', return_value=CursorContext({'ok': 1})) as get_cursor:
        assert verify_user_password(user_id=7, password='secret') is True

    get_cursor.return_value.cursor.execute.assert_called_once_with(
        "SELECT 1 AS ok FROM mi_users WHERE id = %s AND password = %s LIMIT 1",
        (7, 'secret'),
    )


def test_create_api_rejects_wrong_current_password_before_service_call():
    with (
        patch('api.trading_api.config.get_trade_mode', return_value='real'),
        patch('api.trading_api.verify_user_password', return_value=False),
        patch('api.trading_api.get_fund_transfer_service') as get_service,
    ):
        with pytest.raises(AppError) as exc:
            asyncio.run(
                create_fund_transfer(
                    FundTransferCreateRequest(amount=Decimal('10'), password='bad'),
                    {'user_id': 7, 'username': 'admin'},
                )
            )

    assert exc.value.status_code == 403
    get_service.assert_not_called()


def test_limits_api_serializes_current_live_bounds():
    service = MagicMock()
    service.limits.return_value = {
        'coin': 'USDT',
        'minimum_transfer_amount': Decimal('5.01'),
        'maximum_transfer_amount': Decimal('123.45'),
        '_network_info': object(),
    }
    with patch('api.trading_api.get_fund_transfer_service', return_value=service):
        result = asyncio.run(get_fund_transfer_limits())

    assert result == {
        'success': True,
        'limits': {
            'coin': 'USDT',
            'minimum_transfer_amount': 5.01,
            'maximum_transfer_amount': 123.45,
        },
    }


def test_forward_open_loop_checks_independent_fund_transfer_lock():
    from api import orderbook_server

    source = inspect.getsource(orderbook_server._run_open_position_check_once)
    assert 'if fund_transfer_open_locked()' in source
    assert source.index('if fund_transfer_open_locked()') < source.index(
        'merged_rows = _get_fresh_trading_rows()'
    )


def test_retry_only_allows_location_safe_status_transitions():
    assert 'request_retry' in FundTransferService.__dict__
    source = inspect.getsource(FundTransferService.request_retry)
    assert 'gate_transfer_retry_required' in source
    assert 'rollback_retry_required' in source
    assert 'binance_withdraw_submitted' not in source
