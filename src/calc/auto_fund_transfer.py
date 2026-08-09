# coding: utf-8
"""Automatic Binance-to-Gate funding driven by fresh Gate account MMR."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

from calc.fund_transfer_service import FundTransferService
from calc.popup_notification_store import upsert_popup_notification
from common.config import config
from common.logger import get_logger

logger = get_logger(__name__)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or '0'))


def _datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AutoFundTransferPolicy:
    trigger_mmr_pct: Decimal = Decimal('500')
    target_mmr_pct: Decimal = Decimal('700')
    target_buffer_ratio: Decimal = Decimal('0.15')
    max_binance_free_ratio: Decimal = Decimal('0.70')
    binance_equity_reserve_ratio: Decimal = Decimal('0.02')
    binance_absolute_reserve_usdt: Decimal = Decimal('50')
    minimum_amount_usdt: Decimal = Decimal('100')
    minimum_mmr_uplift_pct: Decimal = Decimal('50')
    account_summary_max_age_sec: float = 120.0
    attempt_cooldown_sec: float = 10.0
    completed_cooldown_sec: float = 30.0

    @classmethod
    def from_config(cls) -> 'AutoFundTransferPolicy':
        prefix = 'account_capital.gate_cross_risk.auto_fund_transfer'
        return cls(
            trigger_mmr_pct=_decimal(config.get_float(f'{prefix}.trigger_mmr_pct', 500.0)),
            target_mmr_pct=_decimal(config.get_float(f'{prefix}.target_mmr_pct', 700.0)),
            target_buffer_ratio=_decimal(config.get_float(f'{prefix}.target_buffer_ratio', 0.15)),
            max_binance_free_ratio=_decimal(config.get_float(f'{prefix}.max_binance_free_ratio', 0.70)),
            binance_equity_reserve_ratio=_decimal(
                config.get_float(f'{prefix}.binance_equity_reserve_ratio', 0.02)
            ),
            binance_absolute_reserve_usdt=_decimal(
                config.get_float(f'{prefix}.binance_absolute_reserve_usdt', 50.0)
            ),
            minimum_amount_usdt=_decimal(
                config.get_float(f'{prefix}.minimum_amount_usdt', 100.0)
            ),
            minimum_mmr_uplift_pct=_decimal(
                config.get_float(f'{prefix}.minimum_mmr_uplift_pct', 50.0)
            ),
            account_summary_max_age_sec=config.get_float(
                f'{prefix}.account_summary_max_age_sec', 120.0
            ),
            attempt_cooldown_sec=config.get_float(
                f'{prefix}.attempt_cooldown_sec', 10.0
            ),
            completed_cooldown_sec=config.get_float(
                f'{prefix}.completed_cooldown_sec', 30.0
            ),
        )


def calculate_auto_transfer_amount(
    *,
    gate_margin_balance: Decimal,
    gate_maintenance_margin: Decimal,
    binance_forward_free: Decimal,
    binance_equity: Decimal,
    exchange_minimum: Decimal,
    policy: AutoFundTransferPolicy,
) -> Dict[str, Decimal | bool | str]:
    """Calculate one executable transfer without spending the Binance reserve."""
    maintenance = max(_decimal(gate_maintenance_margin), Decimal('0'))
    gate_balance = max(_decimal(gate_margin_balance), Decimal('0'))
    forward_free = max(_decimal(binance_forward_free), Decimal('0'))
    binance_total = max(_decimal(binance_equity), Decimal('0'))
    target_multiple = policy.target_mmr_pct / Decimal('100')
    uplift_multiple = policy.minimum_mmr_uplift_pct / Decimal('100')

    required = max(maintenance * target_multiple - gate_balance, Decimal('0'))
    buffered_required = required * (Decimal('1') + policy.target_buffer_ratio)
    reserve = max(
        binance_total * policy.binance_equity_reserve_ratio,
        policy.binance_absolute_reserve_usdt,
    )
    ratio_cap = forward_free * policy.max_binance_free_ratio
    reserve_cap = max(forward_free - reserve, Decimal('0'))
    maximum = min(ratio_cap, reserve_cap)
    minimum = max(
        _decimal(exchange_minimum),
        policy.minimum_amount_usdt,
        maintenance * uplift_multiple,
    )
    desired = max(buffered_required, minimum)
    amount = min(desired, maximum).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
    executable = amount >= minimum and amount > 0
    reason = 'ready' if executable else 'insufficient_binance_transferable_balance'
    return {
        'executable': executable,
        'reason': reason,
        'amount': amount,
        'required_amount': required,
        'buffered_required_amount': buffered_required,
        'minimum_effective_amount': minimum,
        'maximum_allowed_amount': maximum,
        'binance_reserve_amount': reserve,
    }


class AutoFundTransferCoordinator:
    """Create at most one durable automatic task from each eligible risk snapshot."""

    def __init__(
        self,
        service: FundTransferService,
        *,
        policy: Optional[AutoFundTransferPolicy] = None,
        notifier=upsert_popup_notification,
        now_fn=datetime.now,
        monotonic_fn=time.monotonic,
    ):
        self.service = service
        self.policy = policy or AutoFundTransferPolicy.from_config()
        self.notifier = notifier
        self.now_fn = now_fn
        self.monotonic_fn = monotonic_fn
        self._lock = threading.Lock()
        self._last_external_attempt_monotonic = 0.0
        self._target_recovery_checked = False

    def evaluate(
        self,
        risk: Dict[str, Any],
        *,
        forward_open_enabled: bool,
        binance_equity_usdt: Any,
        account_summary_age_sec: Optional[float],
    ) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {'action': 'busy'}
        try:
            return self._evaluate_locked(
                risk,
                forward_open_enabled=forward_open_enabled,
                binance_equity_usdt=binance_equity_usdt,
                account_summary_age_sec=account_summary_age_sec,
            )
        finally:
            self._lock.release()

    def _evaluate_locked(
        self,
        risk: Dict[str, Any],
        *,
        forward_open_enabled: bool,
        binance_equity_usdt: Any,
        account_summary_age_sec: Optional[float],
    ) -> Dict[str, Any]:
        mmr = _decimal((risk or {}).get('account_mmr_pct'))
        if str((risk or {}).get('health_status') or '') != 'healthy':
            return {'action': 'risk_snapshot_not_healthy', 'mmr_pct': mmr}
        if mmr >= self.policy.target_mmr_pct:
            if not self._target_recovery_checked:
                self._clear_failure_latch(self._latest_auto_task())
                self._target_recovery_checked = True
            return {'action': 'target_recovered', 'mmr_pct': mmr}
        self._target_recovery_checked = False
        if not forward_open_enabled:
            return {'action': 'disabled_with_forward_open', 'mmr_pct': mmr}
        if mmr <= 0 or mmr > self.policy.trigger_mmr_pct:
            return {'action': 'not_triggered', 'mmr_pct': mmr}
        if account_summary_age_sec is None or (
            account_summary_age_sec < -5
            or account_summary_age_sec > self.policy.account_summary_max_age_sec
        ):
            return {'action': 'binance_equity_stale', 'mmr_pct': mmr}
        active = self.service.store.get_active()
        if active:
            return {'action': 'task_active', 'task_id': active.get('id'), 'mmr_pct': mmr}
        latest_auto = self._latest_auto_task()
        if self._auto_failure_blocks(latest_auto):
            return {'action': 'blocked_by_previous_failure', 'task_id': latest_auto.get('id')}
        if self._completed_cooldown_active(latest_auto):
            return {'action': 'completed_cooldown', 'task_id': latest_auto.get('id')}

        now_monotonic = self.monotonic_fn()
        if (
            self._last_external_attempt_monotonic > 0
            and now_monotonic - self._last_external_attempt_monotonic
            < self.policy.attempt_cooldown_sec
        ):
            return {'action': 'attempt_cooldown', 'mmr_pct': mmr}
        self._last_external_attempt_monotonic = now_monotonic
        try:
            limits = self.service.limits()
            decision = calculate_auto_transfer_amount(
                gate_margin_balance=_decimal(risk.get('account_equity_usdt')),
                gate_maintenance_margin=_decimal(risk.get('maintenance_margin_usdt')),
                binance_forward_free=_decimal(limits.get('binance_forward_free')),
                binance_equity=_decimal(binance_equity_usdt),
                exchange_minimum=_decimal(limits.get('minimum_transfer_amount')),
                policy=self.policy,
            )
        except Exception as exc:
            self._notify_evaluation_failure(mmr, exc)
            logger.error('Gate自动补资前置核验失败: %s', exc, exc_info=True)
            return {'action': 'evaluation_error', 'mmr_pct': mmr, 'error': str(exc)[:300]}
        if not decision['executable']:
            self._notify_insufficient(risk, decision)
            return {'action': 'insufficient', 'mmr_pct': mmr, 'decision': decision}

        context = {
            'auto_trigger': 'gate_cross_mmr',
            'trigger_mmr_pct': str(mmr),
            'target_mmr_pct': str(self.policy.target_mmr_pct),
            'gate_margin_balance': str(risk.get('account_equity_usdt') or '0'),
            'gate_maintenance_margin': str(risk.get('maintenance_margin_usdt') or '0'),
            'binance_equity': str(_decimal(binance_equity_usdt)),
            'amount_calculation': {
                key: str(value)
                for key, value in decision.items()
                if isinstance(value, Decimal)
            },
            'risk_snapshot_at': risk.get('fetched_at'),
            'risk_snapshot_ts': risk.get('account_fetched_at_ts'),
        }
        try:
            task = self.service.create_task(
                amount=decision['amount'],
                user_id='system',
                username='auto_mmr',
                initiator='auto_mmr',
                context_detail=context,
            )
        except Exception as exc:
            self._notify_evaluation_failure(mmr, exc)
            logger.error('Gate自动补资任务创建失败: %s', exc, exc_info=True)
            return {'action': 'task_create_error', 'mmr_pct': mmr, 'error': str(exc)[:300]}
        self.notifier(
            title='Gate全仓MMR自动补资',
            message=(
                f"MMR={mmr:.2f}%<=500%，已创建资金划转 #{task.get('id')}，"
                f"金额={decision['amount']:.8f} USDT，目标MMR=700%"
            ),
            type='warning',
            source='auto_fund_transfer',
            dedup_key=f"auto_fund_transfer:{task.get('id')}:created",
            event_at=self.now_fn(),
            payload={'task_id': task.get('id'), 'trigger_mmr_pct': str(mmr)},
            user_id='default',
        )
        logger.critical(
            'Gate全仓MMR自动补资任务已创建 | task=%s | mmr=%s%% | amount=%s',
            task.get('id'), mmr, decision['amount'],
        )
        return {'action': 'created', 'task': task, 'decision': decision}

    def _latest_auto_task(self) -> Optional[Dict[str, Any]]:
        for task in self.service.store.list(limit=30):
            if str((task.get('detail') or {}).get('initiator') or '') == 'auto_mmr':
                return task
        return None

    def _auto_failure_blocks(self, task: Optional[Dict[str, Any]]) -> bool:
        if not task:
            return False
        detail = task.get('detail') or {}
        return (
            str(task.get('status') or '') in {
                'failed_before_transfer', 'rolled_back', 'manually_reconciled'
            }
            and not detail.get('auto_episode_cleared_at')
        )

    def _clear_failure_latch(self, task: Optional[Dict[str, Any]]) -> None:
        if not self._auto_failure_blocks(task):
            return
        detail = dict(task.get('detail') or {})
        detail['auto_episode_cleared_at'] = self.now_fn().isoformat(sep=' ', timespec='seconds')
        self.service.store.update(task['id'], detail=detail)

    def _completed_cooldown_active(self, task: Optional[Dict[str, Any]]) -> bool:
        if not task or str(task.get('status') or '') != 'completed':
            return False
        completed_at = _datetime(task.get('completed_at'))
        if completed_at is None:
            return False
        return (self.now_fn() - completed_at).total_seconds() < self.policy.completed_cooldown_sec

    def _notify_insufficient(self, risk: Dict[str, Any], decision: Dict[str, Any]) -> None:
        mmr = _decimal(risk.get('account_mmr_pct'))
        bucket = int(time.time() // 300)
        self.notifier(
            title='Gate自动补资资金不足',
            message=(
                f"MMR={mmr:.2f}%，可自动划转={decision['maximum_allowed_amount']:.2f} USDT，"
                f"最低有效额度={decision['minimum_effective_amount']:.2f} USDT"
            ),
            type='error',
            source='auto_fund_transfer',
            dedup_key=f'auto_fund_transfer:insufficient:{bucket}',
            event_at=self.now_fn(),
            payload={'trigger_mmr_pct': str(mmr)},
            user_id='default',
        )

    def _notify_evaluation_failure(self, mmr: Decimal, exc: Exception) -> None:
        bucket = int(time.time() // 300)
        self.notifier(
            title='Gate自动补资异常',
            message=f'MMR={mmr:.2f}%，自动补资前置核验失败，需要人工检查: {str(exc)[:300]}',
            type='error',
            source='auto_fund_transfer',
            dedup_key=f'auto_fund_transfer:evaluation_error:{bucket}',
            event_at=self.now_fn(),
            payload={'trigger_mmr_pct': str(mmr), 'error': str(exc)[:300]},
            user_id='default',
        )
