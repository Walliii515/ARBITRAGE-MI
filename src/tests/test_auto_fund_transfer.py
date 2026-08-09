from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import time

import pytest

from calc.auto_fund_transfer import (
    AutoFundTransferCoordinator,
    AutoFundTransferPolicy,
    calculate_auto_transfer_amount,
)


class FakeStore:
    def __init__(self):
        self.tasks = []
        self.list_calls = 0

    def get_active(self):
        return next(
            (deepcopy(task) for task in reversed(self.tasks) if task.get('active_slot') == 1),
            None,
        )

    def list(self, limit=30):
        self.list_calls += 1
        return deepcopy(list(reversed(self.tasks))[:limit])

    def get_latest_by_initiator(self, initiator):
        self.list_calls += 1
        return next(
            (
                deepcopy(task)
                for task in reversed(self.tasks)
                if str((task.get('detail') or {}).get('initiator') or '') == initiator
            ),
            None,
        )

    def update(self, task_id, **values):
        task = next(item for item in self.tasks if item['id'] == task_id)
        task.update(deepcopy(values))
        return deepcopy(task)


class FakeService:
    def __init__(self, free=Decimal('5000')):
        self.store = FakeStore()
        self.free = free
        self.created = []
        self.limits_calls = 0
        self.profit_release_reserved = False
        self.task_creation_reserved = False

    @property
    def open_locked(self):
        return bool(
            self.profit_release_reserved
            or self.task_creation_reserved
            or self.store.get_active()
        )

    def reserve_profit_release(self):
        if self.profit_release_reserved or self.store.get_active():
            return False
        self.profit_release_reserved = True
        return True

    def release_profit_release(self):
        self.profit_release_reserved = False

    def limits(self):
        self.limits_calls += 1
        return {
            'binance_forward_free': self.free,
            'minimum_transfer_amount': Decimal('5.01'),
        }

    def create_task(self, **kwargs):
        self.created.append(deepcopy(kwargs))
        task = {
            'id': len(self.store.tasks) + 1,
            'active_slot': 1,
            'status': 'queued',
            'detail': {
                'initiator': kwargs.get('initiator'),
                **deepcopy(kwargs.get('context_detail') or {}),
            },
            'created_at': datetime(2026, 8, 9, 12, 0, 0),
        }
        self.store.tasks.append(task)
        return deepcopy(task)


def policy(**changes):
    values = {
        'trigger_mmr_pct': Decimal('500'),
        'target_mmr_pct': Decimal('700'),
        'target_buffer_ratio': Decimal('0.15'),
        'max_binance_free_ratio': Decimal('0.70'),
        'binance_equity_reserve_ratio': Decimal('0.02'),
        'binance_absolute_reserve_usdt': Decimal('50'),
        'minimum_amount_usdt': Decimal('100'),
        'minimum_mmr_uplift_pct': Decimal('50'),
        'account_summary_max_age_sec': 120,
        'completed_cooldown_sec': 30,
    }
    values.update(changes)
    return AutoFundTransferPolicy(**values)


def healthy_risk(mmr='500', equity='5000', maintenance='1000'):
    return {
        'health_status': 'healthy',
        'account_mmr_pct': Decimal(mmr),
        'account_equity_usdt': Decimal(equity),
        'maintenance_margin_usdt': Decimal(maintenance),
        'fetched_at': '2026-08-09 12:00:00',
        'account_fetched_at_ts': 1,
    }


def make_coordinator(service=None, now=None, monotonic=100.0):
    service = service or FakeService()
    notifications = []
    fixed_now = now or datetime(2026, 8, 9, 12, 0, 0)
    coordinator = AutoFundTransferCoordinator(
        service,
        policy=policy(),
        notifier=lambda **item: notifications.append(item),
        now_fn=lambda: fixed_now,
        monotonic_fn=lambda: monotonic,
    )
    return coordinator, service, notifications


def test_amount_targets_700_with_buffer_and_caps_at_70_percent_of_free():
    result = calculate_auto_transfer_amount(
        gate_margin_balance=Decimal('5000'),
        gate_maintenance_margin=Decimal('1000'),
        binance_forward_free=Decimal('2000'),
        binance_equity=Decimal('10000'),
        exchange_minimum=Decimal('5.01'),
        policy=policy(),
    )

    assert result['required_amount'] == Decimal('2000')
    assert result['buffered_required_amount'] == Decimal('2300.00')
    assert result['binance_reserve_amount'] == Decimal('200.00')
    assert result['maximum_allowed_amount'] == Decimal('1400.00')
    assert result['amount'] == Decimal('1400.00000000')
    assert result['executable'] is True


def test_amount_refuses_transfer_below_dynamic_minimum():
    result = calculate_auto_transfer_amount(
        gate_margin_balance=Decimal('5000'),
        gate_maintenance_margin=Decimal('1000'),
        binance_forward_free=Decimal('600'),
        binance_equity=Decimal('10000'),
        exchange_minimum=Decimal('5.01'),
        policy=policy(),
    )

    assert result['minimum_effective_amount'] == Decimal('500.0')
    assert result['maximum_allowed_amount'] == Decimal('400.00')
    assert result['executable'] is False


def test_amount_uses_reserve_cap_when_it_is_lower_than_seventy_percent_cap():
    result = calculate_auto_transfer_amount(
        gate_margin_balance=Decimal('100'),
        gate_maintenance_margin=Decimal('100'),
        binance_forward_free=Decimal('120'),
        binance_equity=Decimal('10000'),
        exchange_minimum=Decimal('5.01'),
        policy=policy(binance_absolute_reserve_usdt=Decimal('50')),
    )

    assert result['binance_reserve_amount'] == Decimal('200.00')
    assert result['maximum_allowed_amount'] == Decimal('0')
    assert result['amount'] == Decimal('0E-8')
    assert result['executable'] is False


def test_amount_at_exact_dynamic_minimum_is_executable():
    result = calculate_auto_transfer_amount(
        gate_margin_balance=Decimal('650'),
        gate_maintenance_margin=Decimal('100'),
        binance_forward_free=Decimal('1000'),
        binance_equity=Decimal('1000'),
        exchange_minimum=Decimal('5.01'),
        policy=policy(),
    )

    assert result['buffered_required_amount'] == Decimal('57.50')
    assert result['minimum_effective_amount'] == Decimal('100')
    assert result['amount'] == Decimal('100.00000000')
    assert result['executable'] is True


def test_exchange_minimum_can_be_the_effective_floor():
    result = calculate_auto_transfer_amount(
        gate_margin_balance=Decimal('699'),
        gate_maintenance_margin=Decimal('100'),
        binance_forward_free=Decimal('1000'),
        binance_equity=Decimal('1000'),
        exchange_minimum=Decimal('250'),
        policy=policy(),
    )

    assert result['minimum_effective_amount'] == Decimal('250')
    assert result['amount'] == Decimal('250.00000000')
    assert result['executable'] is True


def test_amount_rounds_down_without_exceeding_the_available_cap():
    result = calculate_auto_transfer_amount(
        gate_margin_balance=Decimal('0'),
        gate_maintenance_margin=Decimal('1000'),
        binance_forward_free=Decimal('1000.123456789'),
        binance_equity=Decimal('1000'),
        exchange_minimum=Decimal('5.01'),
        policy=policy(),
    )

    assert result['maximum_allowed_amount'] == Decimal('700.08641975230')
    assert result['amount'] == Decimal('700.08641975')
    assert result['amount'] <= result['maximum_allowed_amount']


def test_coordinator_creates_audited_task_only_when_forward_open_is_enabled():
    coordinator, service, notifications = make_coordinator()

    paused = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=False,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    created = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert paused['action'] == 'disabled_with_forward_open'
    assert created['action'] == 'created'
    assert service.created[0]['initiator'] == 'auto_mmr'
    assert service.created[0]['user_id'] == 'system'
    assert service.created[0]['context_detail']['trigger_mmr_pct'] == '500'
    assert service.created[0]['context_detail']['target_mmr_pct'] == '700'
    assert notifications[0]['type'] == 'warning'
    assert coordinator.is_profit_release_allowed() is False


def test_coordinator_does_not_duplicate_an_active_task_or_use_stale_equity():
    coordinator, service, _ = make_coordinator()
    service.store.tasks.append({
        'id': 9,
        'active_slot': 1,
        'status': 'binance_withdrawing',
        'detail': {'initiator': 'manual'},
    })

    active = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    service.store.tasks.clear()
    stale = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=121,
    )

    assert active['action'] == 'task_active'
    assert stale['action'] == 'binance_equity_stale'
    assert not service.created
    assert coordinator.is_profit_release_allowed() is False


def test_failed_auto_task_blocks_rewithdrawal_until_mmr_recovers_to_target():
    coordinator, service, _ = make_coordinator()
    service.store.tasks.append({
        'id': 4,
        'active_slot': None,
        'status': 'failed_before_transfer',
        'detail': {'initiator': 'auto_mmr'},
    })

    blocked = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    recovered = coordinator.evaluate(
        healthy_risk(mmr='700', equity='7000'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert blocked['action'] == 'blocked_by_previous_failure'
    assert recovered['action'] == 'target_recovered'
    assert service.store.tasks[0]['detail']['auto_episode_cleared_at']


def test_recent_completed_auto_task_has_cooldown_before_another_withdrawal():
    now = datetime(2026, 8, 9, 12, 0, 0)
    coordinator, service, _ = make_coordinator(now=now)
    service.store.tasks.append({
        'id': 6,
        'active_slot': None,
        'status': 'completed',
        'completed_at': now - timedelta(seconds=10),
        'detail': {'initiator': 'auto_mmr'},
    })

    result = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'completed_cooldown'
    assert not service.created


def test_safe_mmr_checks_persistent_recovery_latch_only_once():
    coordinator, service, _ = make_coordinator()

    first = coordinator.evaluate(
        healthy_risk(mmr='700', equity='7000'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    second = coordinator.evaluate(
        healthy_risk(mmr='800', equity='8000'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert first['action'] == second['action'] == 'target_recovered'
    assert service.store.list_calls == 1
    assert service.limits_calls == 0


def test_external_limit_checks_are_throttled_after_insufficient_result():
    coordinator, service, notifications = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )

    first = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    second = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert first['action'] == 'insufficient'
    assert second['action'] == 'attempt_cooldown'
    assert service.limits_calls == 1
    assert len(notifications) == 1
    assert coordinator.is_profit_release_allowed() is True


def test_exchange_failure_before_task_creation_notifies_for_manual_attention():
    service = FakeService()

    def fail_limits():
        service.limits_calls += 1
        raise RuntimeError('exchange unavailable')

    service.limits = fail_limits
    coordinator, _, notifications = make_coordinator(service=service)

    result = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'evaluation_error'
    assert notifications[0]['type'] == 'error'
    assert notifications[0]['source'] == 'auto_fund_transfer'
    assert 'exchange unavailable' in notifications[0]['message']
    assert coordinator.is_profit_release_allowed() is False


def test_profitable_release_wakes_immediate_recheck_and_prevents_second_release():
    service = FakeService(free=Decimal('600'))
    coordinator, _, _ = make_coordinator(service=service, monotonic=100.0)

    insufficient = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    assert insufficient['action'] == 'insufficient'
    assert coordinator.is_profit_release_allowed() is True
    assert coordinator.claim_profit_release() is True
    assert coordinator.claim_profit_release() is False

    service.free = Decimal('5000')
    coordinator.notify_binance_funds_released()
    assert coordinator.is_profit_release_allowed() is False
    created = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert created['action'] == 'created'
    assert service.limits_calls == 2
    assert coordinator.is_profit_release_allowed() is False


def test_claimed_profit_release_blocks_auto_task_until_close_finishes():
    service = FakeService(free=Decimal('600'))
    coordinator, _, _ = make_coordinator(service=service, monotonic=100.0)
    assert coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'

    assert coordinator.claim_profit_release() is True
    service.free = Decimal('5000')
    blocked = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert blocked['action'] == 'profit_release_inflight'
    assert service.created == []
    coordinator.finish_profit_release(binance_funds_released=False)
    cooldown = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    assert cooldown['action'] == 'attempt_cooldown'


def test_claim_fails_closed_when_active_task_appears_after_insufficient_decision():
    coordinator, service, _ = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )
    assert coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'
    service.store.tasks.append({
        'id': 101,
        'active_slot': 1,
        'status': 'queued',
        'detail': {'initiator': 'manual'},
    })

    assert coordinator.claim_profit_release() is False
    assert coordinator.is_profit_release_allowed() is False


def test_claim_fails_closed_when_transfer_store_is_unavailable():
    coordinator, service, _ = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )
    assert coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'
    service.store.get_active = lambda: (_ for _ in ()).throw(RuntimeError('db unavailable'))

    assert coordinator.claim_profit_release() is False
    assert coordinator.is_profit_release_allowed() is False


def test_evaluation_store_failure_revokes_previous_release_permission():
    coordinator, service, notifications = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )
    assert coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'
    service.store.get_latest_by_initiator = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('db unavailable'))
    )

    result = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'evaluation_error'
    assert 'db unavailable' in result['error']
    assert coordinator.is_profit_release_allowed() is False
    assert coordinator.claim_profit_release() is False
    assert notifications[-1]['type'] == 'error'


def test_claim_fails_fast_while_auto_evaluation_is_busy():
    coordinator, _, _ = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )
    assert coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'
    assert coordinator._lock.acquire(blocking=False) is True
    try:
        started = time.monotonic()
        assert coordinator.claim_profit_release() is False
        assert time.monotonic() - started < 0.1
    finally:
        coordinator._lock.release()
    assert coordinator.is_profit_release_allowed() is True


def test_active_manual_transfer_revokes_previous_profit_release_permission():
    coordinator, service, _ = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )
    assert coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'
    service.store.tasks.append({
        'id': 99,
        'active_slot': 1,
        'status': 'binance_withdrawing',
        'detail': {'initiator': 'manual'},
    })

    result = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'task_active'
    assert coordinator.is_profit_release_allowed() is False


def test_manual_task_creation_preflight_blocks_auto_evaluation_before_db_insert():
    coordinator, service, _ = make_coordinator()
    service.task_creation_reserved = True

    result = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'task_creation_inflight'
    assert service.limits_calls == 0
    assert service.created == []


def test_paused_stale_and_unhealthy_inputs_revoke_release_permission():
    scenarios = [
        ({'forward_open_enabled': False, 'account_summary_age_sec': 1}, healthy_risk()),
        ({'forward_open_enabled': True, 'account_summary_age_sec': 121}, healthy_risk()),
        (
            {'forward_open_enabled': True, 'account_summary_age_sec': 1},
            {**healthy_risk(), 'health_status': 'stale'},
        ),
    ]
    for kwargs, risk in scenarios:
        coordinator, _, _ = make_coordinator(
            service=FakeService(free=Decimal('600')),
        )
        assert coordinator.evaluate(
            healthy_risk(),
            forward_open_enabled=True,
            binance_equity_usdt=Decimal('10000'),
            account_summary_age_sec=1,
        )['action'] == 'insufficient'

        coordinator.evaluate(
            risk,
            binance_equity_usdt=Decimal('10000'),
            **kwargs,
        )

        assert coordinator.is_profit_release_allowed() is False


@pytest.mark.parametrize('invalid_age', [None, float('nan'), 'bad', float('inf'), -float('inf')])
def test_invalid_account_summary_age_never_preserves_release_permission(invalid_age):
    coordinator, _, _ = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )
    assert coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'

    result = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=invalid_age,
    )

    assert result['action'] == 'binance_equity_stale'
    assert coordinator.is_profit_release_allowed() is False


@pytest.mark.parametrize(
    'limits',
    [
        {'minimum_transfer_amount': Decimal('5.01')},
        {'binance_forward_free': None, 'minimum_transfer_amount': Decimal('5.01')},
        {'binance_forward_free': Decimal('NaN'), 'minimum_transfer_amount': Decimal('5.01')},
        {'binance_forward_free': Decimal('-1'), 'minimum_transfer_amount': Decimal('5.01')},
        {'binance_forward_free': Decimal('5000')},
        {'binance_forward_free': Decimal('5000'), 'minimum_transfer_amount': Decimal('Infinity')},
        {'binance_forward_free': Decimal('5000'), 'minimum_transfer_amount': Decimal('-1')},
    ],
)
def test_malformed_exchange_limits_never_authorize_profit_release(limits):
    coordinator, service, notifications = make_coordinator()
    service.limits = lambda: limits

    result = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'evaluation_error'
    assert service.created == []
    assert coordinator.is_profit_release_allowed() is False
    assert notifications[-1]['type'] == 'error'


def test_completed_cooldown_boundary_allows_new_task_at_exactly_thirty_seconds():
    now = datetime(2026, 8, 9, 12, 0, 0)
    coordinator, service, _ = make_coordinator(now=now)
    service.store.tasks.append({
        'id': 6,
        'active_slot': None,
        'status': 'completed',
        'completed_at': now - timedelta(seconds=30),
        'detail': {'initiator': 'auto_mmr'},
    })

    result = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'created'


def test_failure_latch_blocks_profit_release_as_well_as_rewithdrawal():
    coordinator, service, _ = make_coordinator()
    service.store.tasks.append({
        'id': 4,
        'active_slot': None,
        'status': 'rolled_back',
        'detail': {'initiator': 'auto_mmr'},
    })

    result = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'blocked_by_previous_failure'
    assert coordinator.is_profit_release_allowed() is False


def test_failure_latch_is_not_hidden_by_more_than_thirty_manual_tasks():
    coordinator, service, _ = make_coordinator()
    service.store.tasks.append({
        'id': 1,
        'active_slot': None,
        'status': 'failed_before_transfer',
        'detail': {'initiator': 'auto_mmr'},
    })
    service.store.tasks.extend({
        'id': index,
        'active_slot': None,
        'status': 'completed',
        'completed_at': datetime(2026, 8, 8, 12, 0, 0),
        'detail': {'initiator': 'manual'},
    } for index in range(2, 45))

    result = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'blocked_by_previous_failure'
    assert service.created == []


def test_missing_or_non_finite_financial_inputs_never_create_a_task():
    cases = [
        ({**healthy_risk(), 'account_mmr_pct': 'NaN'}, Decimal('10000')),
        ({**healthy_risk(), 'account_equity_usdt': None}, Decimal('10000')),
        ({**healthy_risk(), 'maintenance_margin_usdt': 0}, Decimal('10000')),
        ({**healthy_risk(), 'maintenance_margin_usdt': 'Infinity'}, Decimal('10000')),
        (healthy_risk(), None),
        (healthy_risk(), Decimal('NaN')),
        (healthy_risk(), Decimal('0')),
    ]
    for risk, binance_equity in cases:
        coordinator, service, notifications = make_coordinator()

        result = coordinator.evaluate(
            risk,
            forward_open_enabled=True,
            binance_equity_usdt=binance_equity,
            account_summary_age_sec=1,
        )

        assert result['action'] == 'evaluation_error'
        assert service.created == []
        assert service.limits_calls == 0
        assert coordinator.is_profit_release_allowed() is False
        assert notifications[-1]['type'] == 'error'


def test_active_task_short_circuits_missing_balance_without_spurious_alarm():
    coordinator, service, notifications = make_coordinator()
    service.store.tasks.append({
        'id': 9,
        'active_slot': 1,
        'status': 'binance_withdrawing',
        'detail': {'initiator': 'manual'},
    })

    result = coordinator.evaluate(
        {**healthy_risk(), 'maintenance_margin_usdt': None},
        forward_open_enabled=True,
        binance_equity_usdt=None,
        account_summary_age_sec=1,
    )

    assert result['action'] == 'task_active'
    assert notifications == []
    assert service.limits_calls == 0


def test_notification_failure_after_task_creation_reports_created_without_duplicate():
    service = FakeService()
    coordinator = AutoFundTransferCoordinator(
        service,
        policy=policy(),
        notifier=lambda **_item: (_ for _ in ()).throw(RuntimeError('bell unavailable')),
        now_fn=lambda: datetime(2026, 8, 9, 12, 0, 0),
        monotonic_fn=lambda: 100.0,
    )

    first = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )
    second = coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert first['action'] == 'created'
    assert second['action'] == 'task_active'
    assert len(service.created) == 1
    assert coordinator.is_profit_release_allowed() is False


def test_insufficient_notification_failure_never_authorizes_profit_release():
    service = FakeService(free=Decimal('600'))
    coordinator = AutoFundTransferCoordinator(
        service,
        policy=policy(),
        notifier=lambda **_item: (_ for _ in ()).throw(RuntimeError('bell unavailable')),
        now_fn=lambda: datetime(2026, 8, 9, 12, 0, 0),
        monotonic_fn=lambda: 100.0,
    )

    result = coordinator.evaluate(
        healthy_risk(mmr='340', equity='3400'),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'evaluation_error'
    assert 'bell unavailable' in result['error']
    assert coordinator.is_profit_release_allowed() is False
    assert coordinator.claim_profit_release() is False


def test_busy_evaluation_preserves_confirmed_insufficient_release_permission():
    coordinator, _, _ = make_coordinator(
        service=FakeService(free=Decimal('600')),
    )
    assert coordinator.evaluate(
        healthy_risk(),
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )['action'] == 'insufficient'

    coordinator._lock.acquire()
    try:
        result = coordinator.evaluate(
            healthy_risk(),
            forward_open_enabled=True,
            binance_equity_usdt=Decimal('10000'),
            account_summary_age_sec=1,
        )
    finally:
        coordinator._lock.release()

    assert result['action'] == 'busy'
    assert coordinator.is_profit_release_allowed() is True


def test_target_recovery_clears_failure_latch_even_when_forward_open_is_paused():
    coordinator, service, _ = make_coordinator()
    service.store.tasks.append({
        'id': 4,
        'active_slot': None,
        'status': 'rolled_back',
        'detail': {'initiator': 'auto_mmr'},
    })

    result = coordinator.evaluate(
        healthy_risk(mmr='700', equity='7000'),
        forward_open_enabled=False,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'target_recovered'
    assert service.store.tasks[0]['detail']['auto_episode_cleared_at']


def test_unhealthy_target_snapshot_does_not_clear_failure_latch():
    coordinator, service, _ = make_coordinator()
    service.store.tasks.append({
        'id': 4,
        'active_slot': None,
        'status': 'rolled_back',
        'detail': {'initiator': 'auto_mmr'},
    })

    result = coordinator.evaluate(
        {**healthy_risk(mmr='700', equity='7000'), 'health_status': 'stale'},
        forward_open_enabled=True,
        binance_equity_usdt=Decimal('10000'),
        account_summary_age_sec=1,
    )

    assert result['action'] == 'risk_snapshot_not_healthy'
    assert 'auto_episode_cleared_at' not in service.store.tasks[0]['detail']
