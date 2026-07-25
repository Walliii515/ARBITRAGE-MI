from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from calc.fund_transfer_service import FundTransferService, FundTransferSettings
from exchange_apis.fund_transfer_clients import BinanceNetworkInfo, FundApiError


class MemoryStore:
    def __init__(self, task=None):
        self.task = deepcopy(task)
        self.next_id = 1

    def get_active(self):
        if not self.task or self.task.get('active_slot') is None:
            return None
        return deepcopy(self.task)

    def get(self, task_id):
        return deepcopy(self.task) if self.task and self.task['id'] == task_id else None

    def get_unnotified_terminal(self):
        if not self.task or self.task.get('status') not in {
            'completed', 'rolled_back', 'failed_before_transfer', 'manually_reconciled'
        }:
            return None
        if (self.task.get('detail') or {}).get('terminal_notified'):
            return None
        return deepcopy(self.task)

    def create(self, values):
        self.task = {'id': self.next_id, 'active_slot': 1, **deepcopy(values)}
        self.next_id += 1
        return deepcopy(self.task)

    def update(self, task_id, **values):
        assert self.task and self.task['id'] == task_id
        self.task.update(deepcopy(values))
        if values.get('status') in {
            'completed', 'rolled_back', 'failed_before_transfer', 'manually_reconciled'
        }:
            self.task['active_slot'] = None
        return deepcopy(self.task)


class FakeBinance:
    def __init__(self):
        self.network = BinanceNetworkInfo(
            coin='USDT',
            network='BSC',
            withdraw_enabled=True,
            deposit_enabled=True,
            fee=Decimal('0.01'),
            minimum=Decimal('5'),
            precision_step=Decimal('0.00000001'),
        )
        self.forward_free = Decimal('100')
        self.master_free = Decimal('20')
        self.transfer_rows = {}
        self.withdraw_rows = []
        self.calls = []
        self.transfer_error = None
        self.withdraw_error = None

    def get_network_info(self, coin, network):
        return self.network

    def get_subaccount_free(self, email, asset):
        return self.forward_free

    def get_master_spot_free(self, asset):
        return self.master_free

    def universal_transfer_history(self, client_id, **kwargs):
        self.calls.append(('transfer_history', client_id, kwargs))
        return deepcopy(self.transfer_rows.get(client_id, []))

    def universal_transfer(self, **kwargs):
        self.calls.append(('transfer', kwargs))
        if self.transfer_error:
            raise self.transfer_error
        return {'tranId': 'bin-transfer'}

    def withdraw_history(self, **kwargs):
        self.calls.append(('withdraw_history', kwargs))
        return deepcopy(self.withdraw_rows)

    def withdraw(self, **kwargs):
        self.calls.append(('withdraw', kwargs))
        if self.withdraw_error:
            raise self.withdraw_error
        return {'id': 'withdraw-id'}


class FakeGate:
    def __init__(self):
        self.address = '0x1111111111111111111111111111111111111111'
        self.deposit_rows = []
        self.transfer_rows = []
        self.transfer_status = {'status': 'success', 'tx_id': 'gate-transfer'}
        self.calls = []
        self.transfer_error = None
        self.transfer_status_error = None

    def deposit_address(self, currency):
        return {
            'currency': currency,
            'multichain_addresses': [{'chain': 'BSC', 'address': self.address}],
        }

    def deposits(self, **kwargs):
        self.calls.append(('deposits', kwargs))
        return deepcopy(self.deposit_rows)

    def subaccount_transfer_history(self):
        self.calls.append(('gate_history', None))
        return deepcopy(self.transfer_rows)

    def transfer_to_subaccount_futures(self, **kwargs):
        self.calls.append(('gate_transfer', kwargs))
        if self.transfer_error:
            raise self.transfer_error
        return {'tx_id': 'gate-transfer'}

    def subaccount_transfer_status(self, **kwargs):
        self.calls.append(('gate_status', kwargs))
        if self.transfer_status_error:
            raise self.transfer_status_error
        return deepcopy(self.transfer_status)


def make_service(task=None, now=None):
    store = MemoryStore(task)
    binance = FakeBinance()
    gate = FakeGate()
    notifications = []
    settings = FundTransferSettings(
        coin='USDT',
        network='BSC',
        destination=gate.address,
        binance_forward_email='forward@example.com',
        gate_forward_uid='10002',
    )
    service = FundTransferService(
        store=store,
        binance=binance,
        gate=gate,
        settings=settings,
        notifier=lambda **item: notifications.append(item),
        now_fn=(lambda: now) if now else datetime.now,
    )
    return service, store, binance, gate, notifications


def create_task(service):
    return service.create_task(
        amount=Decimal('10'), user_id='7', username='admin'
    )


def test_create_task_treats_input_as_total_debit_and_subtracts_fee_once():
    service, store, _, _, _ = make_service()

    task = create_task(service)

    assert task['requested_amount'] == Decimal('10.00000000')
    assert task['withdraw_amount'] == Decimal('9.99000000')
    assert task['expected_fee'] == Decimal('0.01')
    assert task['detail']['gross_amount_semantics'] == (
        'requested_amount_includes_withdraw_fee'
    )
    assert service.open_locked is True


def test_preview_is_read_only_and_exposes_live_confirmation_values():
    service, store, binance, gate, _ = make_service()

    preview = service.preview(Decimal('10'))

    assert preview == {
        'coin': 'USDT',
        'network': 'BSC',
        'destination_masked': '0x1111...111111',
        'requested_amount': Decimal('10.00000000'),
        'fee': Decimal('0.01'),
        'received_amount': Decimal('9.99000000'),
        'minimum_received_amount': Decimal('5'),
        'binance_forward_free': Decimal('100'),
    }
    assert store.get_active() is None
    assert not binance.calls
    assert not gate.calls


def test_create_task_checks_live_forward_balance_without_fixed_cap():
    service, _, binance, _, _ = make_service()
    binance.forward_free = Decimal('9.99')

    with pytest.raises(ValueError, match='可用余额不足'):
        create_task(service)


def test_queued_queries_before_submitting_internal_transfer():
    service, store, binance, _, _ = make_service()
    task = create_task(service)

    result = service.run_once(task['id'])

    assert [call[0] for call in binance.calls[-2:]] == [
        'transfer_history', 'transfer'
    ]
    assert result['status'] == 'binance_master_funded'


def test_ambiguous_internal_transfer_is_never_automatically_resubmitted():
    service, store, binance, _, _ = make_service()
    task = create_task(service)
    binance.transfer_error = FundApiError('timeout', ambiguous=True)

    first = service.run_once(task['id'])
    second = service.run_once(task['id'])

    assert first['status'] == 'binance_transfer_submitted'
    assert second['status'] == 'binance_transfer_submitted'
    assert sum(1 for call in binance.calls if call[0] == 'transfer') == 1


def test_internal_transfer_process_waits_and_failure_never_starts_withdrawal():
    service, store, binance, _, _ = make_service()
    task = create_task(service)
    client_id = task['binance_transfer_client_id']
    binance.transfer_rows[client_id] = [{
        'tranId': '123',
        'status': 'PROCESS',
    }]

    processing = service.run_once(task['id'])
    assert processing['status'] == 'binance_transfer_submitted'
    assert not any(call[0] == 'withdraw' for call in binance.calls)

    binance.transfer_rows[client_id][0]['status'] = 'FAILURE'
    failed = service.run_once(task['id'])
    assert failed['status'] == 'failed_before_transfer'
    assert failed['active_slot'] is None
    assert not any(call[0] == 'withdraw' for call in binance.calls)


def test_forward_and_rollback_history_use_opposite_email_filters():
    service, store, binance, _, _ = make_service()
    task = create_task(service)
    service.run_once(task['id'])
    store.update(task['id'], status='rollback_pending')
    service.run_once(task['id'])

    history_calls = [call for call in binance.calls if call[0] == 'transfer_history']
    assert history_calls[0][2] == {'from_email': 'forward@example.com'}
    assert history_calls[-1][2] == {'to_email': 'forward@example.com'}


def test_explicit_withdraw_rejection_rolls_back_to_forward():
    service, store, binance, _, _ = make_service()
    task = create_task(service)
    store.update(task['id'], status='binance_master_funded')
    binance.withdraw_error = FundApiError('whitelist rejected', status_code=400)

    pending = service.run_once(task['id'])
    finished = service.run_once(task['id'])

    assert pending['status'] == 'rollback_pending'
    assert finished['status'] == 'rolled_back'
    rollback = [call for call in binance.calls if call[0] == 'transfer'][-1][1]
    assert rollback['to_email'] == 'forward@example.com'
    assert rollback['amount'] == Decimal('10.00000000')
    assert service.open_locked is False


def test_withdrawal_rechecks_live_fee_and_preserves_total_debit_semantics():
    service, store, binance, _, _ = make_service()
    task = create_task(service)
    store.update(task['id'], status='binance_master_funded')
    binance.network = BinanceNetworkInfo(
        coin='USDT',
        network='BSC',
        withdraw_enabled=True,
        deposit_enabled=True,
        fee=Decimal('0.02'),
        minimum=Decimal('5'),
        precision_step=Decimal('0.00000001'),
    )

    result = service.run_once(task['id'])

    withdraw = [call for call in binance.calls if call[0] == 'withdraw'][0][1]
    assert withdraw['amount'] == Decimal('9.98000000')
    assert result['expected_fee'] == Decimal('0.02')
    assert result['withdraw_amount'] == Decimal('9.98000000')
    assert result['requested_amount'] == Decimal('10.00000000')


def test_ambiguous_withdrawal_is_queried_without_duplicate_submission():
    service, store, binance, _, _ = make_service()
    task = create_task(service)
    store.update(task['id'], status='binance_master_funded')
    binance.withdraw_error = FundApiError('timeout', ambiguous=True)

    first = service.run_once(task['id'])
    second = service.run_once(task['id'])

    assert first['status'] == 'binance_withdraw_submitted'
    assert second['status'] == 'binance_withdraw_submitted'
    assert sum(1 for call in binance.calls if call[0] == 'withdraw') == 1


def test_completed_withdrawal_matches_gate_txid_then_moves_actual_deposit():
    service, store, binance, gate, notifications = make_service()
    task = create_task(service)
    store.update(task['id'], status='binance_withdraw_submitted')
    binance.withdraw_rows = [{
        'id': 'withdraw-id',
        'status': 6,
        'amount': '9.99',
        'transactionFee': '0.01',
        'txId': '0xABC',
    }]

    withdrawn = service.run_once(task['id'])
    assert withdrawn['status'] == 'binance_withdraw_completed'

    gate.deposit_rows = [{
        'id': 'deposit-id',
        'txid': '0xabc',
        'status': 'DONE',
        'amount': '9.99',
    }]
    deposited = service.run_once(task['id'])
    completed = service.run_once(task['id'])

    assert deposited['status'] == 'gate_deposit_confirmed'
    assert completed['status'] == 'completed'
    gate_call = [call for call in gate.calls if call[0] == 'gate_transfer'][0][1]
    assert gate_call['amount'] == Decimal('9.99')
    assert gate_call['sub_uid'] == '10002'
    assert notifications[-1]['type'] == 'success'
    assert service.open_locked is False


def test_gate_query_failure_does_not_submit_internal_transfer():
    service, store, _, gate, _ = make_service()
    task = create_task(service)
    store.update(
        task['id'],
        status='gate_deposit_confirmed',
        received_amount=Decimal('9.99'),
    )

    def fail_history():
        raise FundApiError('gate unavailable', ambiguous=True)

    gate.subaccount_transfer_history = fail_history
    result = service.run_once(task['id'])

    assert result['status'] == 'gate_deposit_confirmed'
    assert not any(call[0] == 'gate_transfer' for call in gate.calls)


def test_ambiguous_gate_transfer_then_exact_not_found_requires_safe_retry():
    service, store, _, gate, notifications = make_service()
    task = create_task(service)
    store.update(
        task['id'],
        status='gate_transfer_submitted',
        received_amount=Decimal('9.99'),
    )
    gate.transfer_status_error = FundApiError(
        'Order not found',
        status_code=400,
        code='ORDER_NOT_EXISTS',
    )

    result = service.run_once(task['id'])

    assert result['status'] == 'gate_transfer_retry_required'
    assert result['attention_required'] == 1
    assert notifications[-1]['type'] == 'error'
    assert not any(call[0] == 'gate_transfer' for call in gate.calls)


def test_gate_deposit_txid_with_wrong_network_or_address_is_blocked():
    service, store, _, gate, notifications = make_service()
    task = create_task(service)
    store.update(
        task['id'],
        status='binance_withdraw_completed',
        binance_tx_id='0xabc',
    )
    gate.deposit_rows = [{
        'id': 'deposit-id',
        'txid': '0xabc',
        'status': 'DONE',
        'amount': '9.99',
        'chain': 'ETH',
        'address': gate.address,
    }]

    result = service.run_once(task['id'])

    assert result['status'] == 'binance_withdraw_completed'
    assert result['attention_required'] == 1
    assert not any(call[0] == 'gate_transfer' for call in gate.calls)
    assert notifications[-1]['type'] == 'error'


def test_delayed_and_attention_notifications_are_deduplicated():
    now = datetime(2026, 7, 25, 12, 0, 0)
    service, store, _, _, notifications = make_service(now=now)
    task = create_task(service)
    store.update(task['id'], created_at=now - timedelta(hours=3))

    service.run_once(task['id'])
    service.run_once(task['id'])

    attention = [n for n in notifications if n['type'] == 'error']
    assert len(attention) == 1
    assert store.task['attention_required'] == 1


def test_terminal_notification_is_retried_after_task_releases_active_slot():
    service, store, _, _, notifications = make_service()
    task = create_task(service)
    store.update(
        task['id'],
        status='completed',
        completed_at=datetime.now(),
    )

    result = service.run_once()

    assert result['detail']['terminal_notified'] is True
    assert notifications[0]['user_id'] == 'default'
    assert service.open_locked is False


def test_failed_rollback_history_requires_explicit_safe_retry():
    service, store, binance, _, notifications = make_service()
    task = create_task(service)
    store.update(task['id'], status='rollback_submitted')
    binance.transfer_rows[task['binance_rollback_client_id']] = [{
        'tranId': 'rollback-id',
        'status': 'FAILURE',
    }]

    result = service.run_once(task['id'])

    assert result['status'] == 'rollback_retry_required'
    assert result['attention_required'] == 1
    assert notifications[-1]['type'] == 'error'
