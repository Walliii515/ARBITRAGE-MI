import json
from decimal import Decimal

import pytest
import requests

from exchange_apis.fund_transfer_clients import (
    BinanceFundClient,
    FundApiError,
    GateFundClient,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_binance_network_and_subaccount_balance_are_parsed():
    session = FakeSession([
        FakeResponse([{
            'coin': 'USDT',
            'networkList': [{
                'network': 'BSC',
                'withdrawEnable': True,
                'depositEnable': True,
                'withdrawFee': '0.01',
                'withdrawMin': '5',
                'withdrawIntegerMultiple': '0.00000001',
            }],
        }]),
        FakeResponse({'balances': [{'asset': 'USDT', 'free': '123.45'}]}),
    ])
    client = BinanceFundClient('key', 'secret', session=session)

    info = client.get_network_info('usdt', 'bsc')
    balance = client.get_subaccount_free('forward@example.com', 'USDT')

    assert info.fee == Decimal('0.01')
    assert info.minimum == Decimal('5')
    assert info.withdraw_enabled is True
    assert balance == Decimal('123.45')


def test_binance_universal_transfer_uses_stable_client_id_and_subaccount_email():
    session = FakeSession([FakeResponse({'tranId': 123, 'clientTranId': 'ft_1_bin'})])
    client = BinanceFundClient('key', 'secret', session=session)

    client.universal_transfer(
        asset='USDT',
        amount=Decimal('10'),
        client_id='ft_1_bin',
        from_email='forward@example.com',
    )

    query = session.calls[0][2]['params']
    assert 'clientTranId=ft_1_bin' in query
    assert 'fromEmail=forward%40example.com' in query
    assert 'toEmail=' not in query
    assert 'data' not in session.calls[0][2]


def test_binance_transfer_history_queries_the_correct_direction():
    session = FakeSession([
        FakeResponse({'result': []}),
        FakeResponse({'result': []}),
    ])
    client = BinanceFundClient('key', 'secret', session=session)

    client.universal_transfer_history(
        'ft_1_bin', from_email='forward@example.com'
    )
    client.universal_transfer_history(
        'ft_1_rollback', to_email='forward@example.com'
    )

    forward_query = session.calls[0][2]['params']
    rollback_query = session.calls[1][2]['params']
    assert 'fromEmail=forward%40example.com' in forward_query
    assert 'toEmail=' not in forward_query
    assert 'toEmail=forward%40example.com' in rollback_query
    assert 'fromEmail=' not in rollback_query


def test_binance_withdraw_always_sends_network_address_and_order_id():
    session = FakeSession([FakeResponse({'id': 'withdraw-1'})])
    client = BinanceFundClient('key', 'secret', session=session)

    client.withdraw(
        coin='USDT',
        network='BSC',
        address='0xabc',
        amount=Decimal('9.99'),
        order_id='ft_1_withdraw',
    )

    query = session.calls[0][2]['params']
    assert 'network=BSC' in query
    assert 'address=0xabc' in query
    assert 'amount=9.99' in query
    assert 'withdrawOrderId=ft_1_withdraw' in query
    assert 'data' not in session.calls[0][2]


def test_transport_error_is_marked_ambiguous():
    session = FakeSession([requests.Timeout('timed out')])
    client = BinanceFundClient('key', 'secret', session=session)

    with pytest.raises(FundApiError) as exc:
        client.withdraw_history(coin='USDT', order_id='ft_1')

    assert exc.value.ambiguous is True


def test_gate_transfer_targets_futures_and_has_client_order_id():
    session = FakeSession([FakeResponse({'tx_id': 99})])
    client = GateFundClient('key', 'secret', session=session)

    client.transfer_to_subaccount_futures(
        sub_uid='10002',
        currency='USDT',
        amount=Decimal('9.99'),
        client_id='ft_1_gate',
    )

    payload = json.loads(session.calls[0][2]['data'])
    assert payload == {
        'sub_account': '10002',
        'sub_account_type': 'futures',
        'currency': 'USDT',
        'amount': '9.99',
        'direction': 'to',
        'client_order_id': 'ft_1_gate',
    }


def test_gate_server_error_is_ambiguous():
    session = FakeSession([FakeResponse({'message': 'busy'}, status=503)])
    client = GateFundClient('key', 'secret', session=session)

    with pytest.raises(FundApiError) as exc:
        client.deposits(currency='USDT', start_at=1)

    assert exc.value.ambiguous is True


def test_gate_error_preserves_machine_readable_label():
    session = FakeSession([
        FakeResponse(
            {'label': 'ORDER_NOT_EXISTS', 'message': 'Order not found'},
            status=400,
        )
    ])
    client = GateFundClient('key', 'secret', session=session)

    with pytest.raises(FundApiError) as exc:
        client.subaccount_transfer_status(client_id='missing')

    assert exc.value.status_code == 400
    assert exc.value.code == 'ORDER_NOT_EXISTS'
    assert exc.value.ambiguous is False


def test_gate_transfer_status_queries_stable_client_order_id():
    session = FakeSession([FakeResponse({'status': 'success', 'tx_id': '123'})])
    client = GateFundClient('key', 'secret', session=session)

    result = client.subaccount_transfer_status(client_id='ft_1_gate')

    assert result['status'] == 'success'
    assert 'client_order_id=ft_1_gate' in session.calls[0][2]['params']
