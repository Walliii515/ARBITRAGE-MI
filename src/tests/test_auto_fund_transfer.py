from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

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
