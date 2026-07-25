# coding: utf-8
"""Crash-safe Binance forward account to Gate futures fund transfer workflow."""
from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional

from calc.fund_transfer_store import FundTransferStore, TERMINAL_STATUSES
from calc.popup_notification_store import upsert_popup_notification
from common.logger import get_logger
from common.fund_transfer_config import get_fund_transfer_destination
from exchange_apis.fund_transfer_clients import (
    BinanceFundClient,
    FundApiError,
    GateFundClient,
    build_default_fund_clients,
)

logger = get_logger(__name__)

BINANCE_WITHDRAW_PENDING = {0, 2, 4}
BINANCE_WITHDRAW_FAILED = {1, 3, 5}


@dataclass(frozen=True)
class FundTransferSettings:
    coin: str
    network: str
    destination: str
    binance_forward_email: str
    gate_forward_uid: str
    delayed_minutes: int = 10
    attention_minutes: int = 120

    @classmethod
    def from_env(cls) -> 'FundTransferSettings':
        destination = get_fund_transfer_destination()
        values = {
            'coin': destination.coin,
            'network': destination.network,
            'destination': destination.address,
        }
        for field, name in {
            'binance_forward_email': 'BINANCE_FORWARD_SUBACCOUNT_EMAIL',
            'gate_forward_uid': 'GATE_FORWARD_SUBACCOUNT_UID',
        }.items():
            value = os.getenv(name, '').strip()
            if not value:
                raise RuntimeError(f'资金划转配置缺失: {name}')
            values[field] = value
        return cls(**values)


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or '0'))


def _mask_address(address: str) -> str:
    address = str(address or '').strip()
    if len(address) <= 12:
        return address
    return f'{address[:6]}...{address[-6:]}'


def _created_at(task: Dict[str, Any]) -> datetime:
    value = task.get('created_at')
    if isinstance(value, datetime):
        return value
    if value:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).replace(tzinfo=None)
    return datetime.now()


def _find_gate_address(payload: Dict[str, Any], network: str) -> str:
    target = network.upper()
    for row in payload.get('multichain_addresses') or []:
        chain = str(row.get('chain') or row.get('network') or '').upper()
        if chain == target:
            return str(row.get('address') or '').strip()
    return str(payload.get('address') or '').strip()


def _transfer_record_id(row: Dict[str, Any]) -> str:
    return str(row.get('tranId') or row.get('id') or row.get('txId') or '')


class FundTransferService:
    def __init__(
        self,
        *,
        store: FundTransferStore,
        binance: BinanceFundClient,
        gate: GateFundClient,
        settings: FundTransferSettings,
        notifier=upsert_popup_notification,
        now_fn=datetime.now,
    ):
        self.store = store
        self.binance = binance
        self.gate = gate
        self.settings = settings
        self.notifier = notifier
        self.now_fn = now_fn
        self._run_lock = threading.Lock()
        self._open_locked = bool(self.store.get_active())

    @property
    def open_locked(self) -> bool:
        return self._open_locked

    def create_task(
        self,
        *,
        amount: Decimal,
        user_id: str,
        username: str,
    ) -> Dict[str, Any]:
        if self.store.get_active():
            raise ValueError('已有划转任务或待处理异常，请先完成恢复')
        preview = self.preview(amount)
        requested = preview['requested_amount']
        net_amount = preview['received_amount']
        forward_free = preview['binance_forward_free']
        master_free = self.binance.get_master_spot_free(self.settings.coin)

        key = secrets.token_hex(10)
        values = {
            'task_key': key,
            'user_id': str(user_id or 'default'),
            'username': str(username or ''),
            'status': 'queued',
            'step': 'binance_forward_to_master',
            'status_message': '等待从 Binance 子账户划转到主账户',
            'coin': self.settings.coin,
            'network': self.settings.network,
            'destination_masked': _mask_address(self.settings.destination),
            'requested_amount': requested,
            'expected_fee': preview['fee'],
            'withdraw_amount': net_amount,
            'binance_transfer_client_id': f'ft_{key}_b',
            'binance_rollback_client_id': f'ft_{key}_r',
            'binance_withdraw_order_id': f'ft_{key}_w',
            'gate_transfer_client_id': f'ft_{key}_g',
        }
        self._open_locked = True
        try:
            task = self.store.create(values)
        except Exception:
            self._open_locked = bool(self.store.get_active())
            raise
        task = self.store.update(
            task['id'],
            started_at=self.now_fn(),
            detail={
                'binance_forward_free_before': str(forward_free),
                'binance_master_free_before': str(master_free),
                'gross_amount_semantics': 'requested_amount_includes_withdraw_fee',
            },
        )
        logger.warning(
            '资金划转任务已创建: id=%s amount=%s coin=%s network=%s',
            task.get('id'), requested, self.settings.coin, self.settings.network,
        )
        return task

    def preview(self, amount: Decimal) -> Dict[str, Any]:
        """Validate live constraints without creating a task or moving money."""
        requested = _decimal(amount).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        if requested <= 0:
            raise ValueError('划转金额必须大于 0')
        network = self.binance.get_network_info(
            self.settings.coin, self.settings.network
        )
        if not network.withdraw_enabled:
            raise ValueError(f'Binance {network.network} 提现当前不可用')
        gate_address = _find_gate_address(
            self.gate.deposit_address(self.settings.coin),
            self.settings.network,
        )
        if gate_address.lower() != self.settings.destination.lower():
            raise ValueError('Gate API 返回的充值地址与固定配置不一致')
        net_amount = (requested - network.fee).quantize(
            network.precision_step, rounding=ROUND_DOWN
        )
        if net_amount < network.minimum:
            minimum_gross = network.minimum + network.fee
            raise ValueError(f'划转金额至少为 {minimum_gross:f} {self.settings.coin}')
        forward_free = self.binance.get_subaccount_free(
            self.settings.binance_forward_email, self.settings.coin
        )
        if forward_free < requested:
            raise ValueError(
                f'Binance 子账户可用余额不足: {forward_free:f} {self.settings.coin}'
            )
        return {
            'coin': self.settings.coin,
            'network': self.settings.network,
            'destination_masked': _mask_address(self.settings.destination),
            'requested_amount': requested,
            'fee': network.fee,
            'received_amount': net_amount,
            'minimum_received_amount': network.minimum,
            'binance_forward_free': forward_free,
        }

    def run_once(self, task_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            return self.store.get(task_id) if task_id else self.store.get_active()
        try:
            task = self.store.get(task_id) if task_id else self.store.get_active()
            if not task and not task_id:
                task = self.store.get_unnotified_terminal()
            self._open_locked = bool(
                task
                and task.get('active_slot') == 1
                and task.get('status') not in TERMINAL_STATUSES
            )
            if not task:
                return task
            if task.get('status') in TERMINAL_STATUSES:
                return self._apply_notifications(task)
            try:
                task = self._advance(task)
            except Exception as exc:
                logger.error(
                    '资金划转状态推进异常: id=%s status=%s error=%s',
                    task.get('id'), task.get('status'), exc, exc_info=True,
                )
                task = self.store.update(
                    task['id'],
                    last_error=str(exc)[:2000],
                    status_message='状态检查异常，系统将继续核验，不会重复发起资金动作',
                    last_checked_at=self.now_fn(),
                )
            task = self._apply_notifications(task)
            self._open_locked = task.get('status') not in TERMINAL_STATUSES
            return task
        finally:
            self._run_lock.release()

    def request_retry(self, task_id: int) -> Dict[str, Any]:
        task = self.store.get(task_id)
        if not task:
            raise ValueError('资金划转任务不存在')
        status = str(task.get('status') or '')
        if status in TERMINAL_STATUSES:
            return task
        transitions = {
            'gate_transfer_retry_required': (
                'gate_deposit_confirmed',
                'gate_master_to_forward_futures',
                '已请求重试 Gate 主账户到 forward 合约账户划转',
            ),
            'rollback_retry_required': (
                'rollback_pending',
                'rollback_to_binance_forward',
                '已请求重试退回 Binance forward 子账户',
            ),
        }
        if status in transitions:
            next_status, step, message = transitions[status]
            task = self.store.update(
                task_id,
                status=next_status,
                step=step,
                status_message=message,
                attention_required=0,
                last_error=None,
                last_checked_at=self.now_fn(),
            )
        return self.run_once(task_id) or task

    def _advance(self, task: Dict[str, Any]) -> Dict[str, Any]:
        status = str(task.get('status') or '')
        handlers = {
            'queued': self._start_binance_transfer,
            'binance_transfer_submitted': self._check_binance_transfer,
            'binance_master_funded': self._start_withdrawal,
            'binance_withdraw_submitted': self._check_withdrawal,
            'binance_withdrawing': self._check_withdrawal,
            'binance_withdraw_completed': self._check_gate_deposit,
            'gate_deposit_confirmed': self._start_gate_transfer,
            'gate_transfer_submitted': self._check_gate_transfer,
            'rollback_pending': self._run_rollback,
            'rollback_submitted': self._check_rollback,
        }
        handler = handlers.get(status)
        if not handler:
            return self.store.update(
                task['id'],
                attention_required=1,
                status_message=f'未知任务状态 {status}，需要人工处理',
                last_checked_at=self.now_fn(),
            )
        return handler(task)

    def _binance_transfer_rows(
        self, client_id: str, *, rollback: bool = False
    ) -> list[Dict[str, Any]]:
        if rollback:
            return self.binance.universal_transfer_history(
                client_id,
                to_email=self.settings.binance_forward_email,
            )
        return self.binance.universal_transfer_history(
            client_id,
            from_email=self.settings.binance_forward_email,
        )

    @staticmethod
    def _internal_transfer_status(row: Dict[str, Any]) -> str:
        return str(row.get('status') or 'SUCCESS').strip().upper()

    def _apply_binance_transfer_record(
        self, task: Dict[str, Any], row: Dict[str, Any]
    ) -> Dict[str, Any]:
        status = self._internal_transfer_status(row)
        if status == 'SUCCESS':
            return self._mark_binance_master_funded(task, row)
        if status == 'FAILURE':
            return self._finish(
                task,
                'failed_before_transfer',
                'Binance 子账户到主账户划转失败，未发生外部提现',
            )
        return self.store.update(
            task['id'],
            status='binance_transfer_submitted',
            step='verify_binance_internal_transfer',
            status_message=f'Binance 内部划转处理中(status={status})',
            binance_transfer_id=_transfer_record_id(row),
            last_checked_at=self.now_fn(),
        )

    def _start_binance_transfer(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rows = self._binance_transfer_rows(task['binance_transfer_client_id'])
        except FundApiError as exc:
            return self._query_error(task, exc, '无法核验 Binance 内部划转，未发起新动作')
        if rows:
            return self._apply_binance_transfer_record(task, rows[0])
        try:
            result = self.binance.universal_transfer(
                asset=task['coin'],
                amount=_decimal(task['requested_amount']),
                client_id=task['binance_transfer_client_id'],
                from_email=self.settings.binance_forward_email,
            )
        except FundApiError as exc:
            if exc.ambiguous:
                return self.store.update(
                    task['id'],
                    status='binance_transfer_submitted',
                    step='verify_binance_internal_transfer',
                    status_message='Binance 内部划转结果不确定，仅查询状态',
                    last_error=str(exc),
                    last_checked_at=self.now_fn(),
                )
            return self._finish(
                task, 'failed_before_transfer', f'Binance 内部划转失败: {exc}'
            )
        return self._mark_binance_master_funded(task, result)

    def _check_binance_transfer(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rows = self._binance_transfer_rows(task['binance_transfer_client_id'])
        except FundApiError as exc:
            return self._query_error(task, exc, '继续核验 Binance 内部划转')
        if not rows:
            return self.store.update(
                task['id'],
                status_message='尚未查到 Binance 内部划转记录，不会重复提交',
                last_checked_at=self.now_fn(),
            )
        return self._apply_binance_transfer_record(task, rows[0])

    def _mark_binance_master_funded(
        self, task: Dict[str, Any], result: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self.store.update(
            task['id'],
            status='binance_master_funded',
            step='binance_withdraw',
            status_message='资金已到 Binance 主账户，准备提现',
            binance_transfer_id=_transfer_record_id(result),
            last_error=None,
            last_checked_at=self.now_fn(),
        )

    def _start_withdrawal(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rows = self.binance.withdraw_history(
                coin=task['coin'], order_id=task['binance_withdraw_order_id']
            )
        except FundApiError as exc:
            return self._query_error(task, exc, '无法核验 Binance 提现，未发起新动作')
        if rows:
            return self._apply_withdrawal_record(task, rows[0])
        try:
            network = self.binance.get_network_info(task['coin'], task['network'])
        except FundApiError as exc:
            return self._query_error(
                task, exc, '无法复核 Binance 实时提现费率，未发起提现'
            )
        if not network.withdraw_enabled:
            return self.store.update(
                task['id'],
                status='rollback_pending',
                step='rollback_to_binance_forward',
                status_message='Binance 提现网络已关闭，准备原路退回',
                last_checked_at=self.now_fn(),
            )
        current_net = (_decimal(task['requested_amount']) - network.fee).quantize(
            network.precision_step, rounding=ROUND_DOWN
        )
        if current_net < network.minimum:
            return self.store.update(
                task['id'],
                status='rollback_pending',
                step='rollback_to_binance_forward',
                status_message='实时手续费变化后净额低于 Binance 提现下限，准备原路退回',
                last_checked_at=self.now_fn(),
            )
        if (
            network.fee != _decimal(task['expected_fee'])
            or current_net != _decimal(task['withdraw_amount'])
        ):
            task = self.store.update(
                task['id'],
                expected_fee=network.fee,
                withdraw_amount=current_net,
                status_message='已按 Binance 实时手续费更新预计到账金额',
                last_checked_at=self.now_fn(),
            )
        try:
            result = self.binance.withdraw(
                coin=task['coin'],
                network=task['network'],
                address=self.settings.destination,
                # Binance deducts the fee from this gross amount. Passing the
                # expected net amount would leave one fee unit in master SPOT.
                amount=_decimal(task['requested_amount']),
                order_id=task['binance_withdraw_order_id'],
            )
        except FundApiError as exc:
            if exc.ambiguous:
                return self.store.update(
                    task['id'],
                    status='binance_withdraw_submitted',
                    step='verify_binance_withdrawal',
                    status_message='Binance 提现结果不确定，仅查询提现记录',
                    last_error=str(exc),
                    last_checked_at=self.now_fn(),
                )
            return self.store.update(
                task['id'],
                status='rollback_pending',
                step='rollback_to_binance_forward',
                status_message=f'Binance 提现明确失败，准备原路退回: {exc}',
                last_error=str(exc),
                last_checked_at=self.now_fn(),
            )
        return self.store.update(
            task['id'],
            status='binance_withdraw_submitted',
            step='wait_binance_withdrawal',
            status_message='Binance 提现已提交，等待链上确认',
            binance_withdraw_id=str(result.get('id') or ''),
            last_error=None,
            last_checked_at=self.now_fn(),
        )

    def _check_withdrawal(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rows = self.binance.withdraw_history(
                coin=task['coin'], order_id=task['binance_withdraw_order_id']
            )
        except FundApiError as exc:
            return self._query_error(task, exc, '继续核验 Binance 提现状态')
        if not rows:
            return self.store.update(
                task['id'],
                status_message='尚未查到 Binance 提现记录，不会重复提交',
                last_checked_at=self.now_fn(),
            )
        return self._apply_withdrawal_record(task, rows[0])

    def _apply_withdrawal_record(
        self, task: Dict[str, Any], row: Dict[str, Any]
    ) -> Dict[str, Any]:
        status = int(row.get('status', -1))
        common = {
            'binance_withdraw_id': str(row.get('id') or task.get('binance_withdraw_id') or ''),
            'actual_fee': _decimal(row.get('transactionFee') or task.get('expected_fee')),
            'last_checked_at': self.now_fn(),
            'last_error': None,
        }
        if status == 6:
            return self.store.update(
                task['id'],
                status='binance_withdraw_completed',
                step='wait_gate_deposit',
                status_message='Binance 提现完成，等待 Gate 入账',
                binance_tx_id=str(row.get('txId') or ''),
                received_amount=_decimal(row.get('amount') or task.get('withdraw_amount')),
                **common,
            )
        if status in BINANCE_WITHDRAW_FAILED:
            return self.store.update(
                task['id'],
                status='rollback_pending',
                step='rollback_to_binance_forward',
                status_message=f'Binance 提现终止(status={status})，准备原路退回',
                **common,
            )
        if status in BINANCE_WITHDRAW_PENDING:
            return self.store.update(
                task['id'],
                status='binance_withdrawing',
                step='wait_binance_withdrawal',
                status_message=f'Binance 正在处理提现(status={status})',
                **common,
            )
        return self.store.update(
            task['id'],
            attention_required=1,
            status_message=f'Binance 返回未知提现状态 {status}，仅继续查询',
            **common,
        )

    def _check_gate_deposit(self, task: Dict[str, Any]) -> Dict[str, Any]:
        tx_id = str(task.get('binance_tx_id') or '').lower()
        if not tx_id:
            return self.store.update(
                task['id'],
                attention_required=1,
                status_message='Binance 提现完成但缺少 txId，需要人工处理',
                last_checked_at=self.now_fn(),
            )
        try:
            rows = self.gate.deposits(
                currency=task['coin'],
                start_at=max(0, int(_created_at(task).timestamp()) - 300),
            )
        except FundApiError as exc:
            return self._query_error(task, exc, '等待 Gate 充值记录')
        row = next(
            (item for item in rows if str(item.get('txid') or '').lower() == tx_id),
            None,
        )
        if not row:
            return self.store.update(
                task['id'],
                status_message='链上已发出，等待 Gate 充值入账',
                last_checked_at=self.now_fn(),
            )
        row_chain = str(row.get('chain') or '').strip().upper()
        row_address = str(row.get('address') or '').strip()
        if (
            (row_chain and row_chain != str(task['network']).upper())
            or (
                row_address
                and row_address.lower() != self.settings.destination.lower()
            )
        ):
            return self.store.update(
                task['id'],
                attention_required=1,
                status_message='Gate 充值记录的网络或地址与固定配置不一致，已停止后续划转',
                gate_deposit_id=str(row.get('id') or ''),
                last_checked_at=self.now_fn(),
            )
        status = str(row.get('status') or '').upper()
        if status != 'DONE':
            return self.store.update(
                task['id'],
                status_message=f'Gate 充值处理中(status={status or "unknown"})',
                gate_deposit_id=str(row.get('id') or ''),
                last_checked_at=self.now_fn(),
            )
        return self.store.update(
            task['id'],
            status='gate_deposit_confirmed',
            step='gate_master_to_forward_futures',
            status_message='Gate 主账户已入账，准备划入 forward 合约账户',
            gate_deposit_id=str(row.get('id') or ''),
            received_amount=_decimal(row.get('amount') or task.get('received_amount')),
            last_error=None,
            last_checked_at=self.now_fn(),
        )

    def _gate_transfer_record(self, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rows = self.gate.subaccount_transfer_history()
        client_id = task['gate_transfer_client_id']
        return next(
            (row for row in rows if str(row.get('client_order_id') or '') == client_id),
            None,
        )

    def _start_gate_transfer(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            existing = self._gate_transfer_record(task)
        except FundApiError as exc:
            return self._query_error(task, exc, '无法核验 Gate 内部划转，未发起新动作')
        if existing:
            return self._apply_gate_transfer_record(task, existing)
        amount = _decimal(task.get('received_amount') or task.get('withdraw_amount'))
        try:
            result = self.gate.transfer_to_subaccount_futures(
                sub_uid=self.settings.gate_forward_uid,
                currency=task['coin'],
                amount=amount,
                client_id=task['gate_transfer_client_id'],
            )
        except FundApiError as exc:
            if exc.ambiguous:
                return self.store.update(
                    task['id'],
                    status='gate_transfer_submitted',
                    step='verify_gate_internal_transfer',
                    status_message='Gate 内部划转结果不确定，仅查询状态',
                    last_error=str(exc),
                    last_checked_at=self.now_fn(),
                )
            return self.store.update(
                task['id'],
                status='gate_transfer_retry_required',
                step='retry_gate_internal_transfer',
                attention_required=1,
                status_message=f'Gate 内部划转失败，可安全重试核验: {exc}',
                last_error=str(exc),
                last_checked_at=self.now_fn(),
            )
        return self._complete(
            task,
            gate_transfer_id=str(
                result.get('tx_id') or result.get('id') or result.get('client_order_id') or ''
            ),
        )

    def _check_gate_transfer(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            row = self.gate.subaccount_transfer_status(
                client_id=task['gate_transfer_client_id']
            )
        except FundApiError as exc:
            if exc.status_code == 400 and exc.code == 'ORDER_NOT_EXISTS':
                return self.store.update(
                    task['id'],
                    status='gate_transfer_retry_required',
                    step='retry_gate_internal_transfer',
                    attention_required=1,
                    status_message='Gate 明确未找到内部划转记录，可重新验证后安全重试',
                    last_error=str(exc),
                    last_checked_at=self.now_fn(),
                )
            return self._query_error(task, exc, '继续核验 Gate 内部划转')
        return self._apply_gate_transfer_record(task, row)

    def _apply_gate_transfer_record(
        self, task: Dict[str, Any], row: Dict[str, Any]
    ) -> Dict[str, Any]:
        status = str(row.get('status') or '').lower()
        if status == 'success':
            return self._complete(
                task,
                gate_transfer_id=str(
                    row.get('tx_id') or row.get('id') or row.get('client_order_id') or ''
                ),
            )
        return self.store.update(
            task['id'],
            status_message=f'Gate 内部划转处理中(status={status or "unknown"})',
            last_checked_at=self.now_fn(),
        )

    def _run_rollback(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rows = self._binance_transfer_rows(
                task['binance_rollback_client_id'], rollback=True
            )
        except FundApiError as exc:
            return self._query_error(task, exc, '无法核验回滚状态，未发起新动作')
        if rows:
            return self._apply_rollback_record(task, rows[0])
        try:
            master_free = self.binance.get_master_spot_free(task['coin'])
        except FundApiError as exc:
            return self._query_error(task, exc, '无法核验 Binance 主账户余额，未回滚')
        amount = _decimal(task['requested_amount'])
        if master_free < amount:
            return self.store.update(
                task['id'],
                status='rollback_retry_required',
                step='retry_binance_rollback',
                attention_required=1,
                status_message='Binance 主账户余额不足以自动回滚，需要人工核验资金位置',
                last_checked_at=self.now_fn(),
            )
        try:
            result = self.binance.universal_transfer(
                asset=task['coin'],
                amount=amount,
                client_id=task['binance_rollback_client_id'],
                to_email=self.settings.binance_forward_email,
            )
        except FundApiError as exc:
            if exc.ambiguous:
                return self.store.update(
                    task['id'],
                    status='rollback_submitted',
                    step='verify_binance_rollback',
                    status_message='回滚结果不确定，仅查询状态',
                    last_error=str(exc),
                    last_checked_at=self.now_fn(),
                )
            return self.store.update(
                task['id'],
                attention_required=1,
                status_message=f'自动回滚失败，需要人工处理: {exc}',
                last_error=str(exc),
                last_checked_at=self.now_fn(),
            )
        return self._finish(
            task,
            'rolled_back',
            '提现未执行，资金已退回 Binance forward 子账户',
            binance_rollback_id=_transfer_record_id(result),
        )

    def _check_rollback(self, task: Dict[str, Any]) -> Dict[str, Any]:
        try:
            rows = self._binance_transfer_rows(
                task['binance_rollback_client_id'], rollback=True
            )
        except FundApiError as exc:
            return self._query_error(task, exc, '继续核验 Binance 回滚状态')
        if not rows:
            return self.store.update(
                task['id'],
                status_message='尚未查到 Binance 回滚记录，不会重复提交',
                last_checked_at=self.now_fn(),
            )
        return self._apply_rollback_record(task, rows[0])

    def _apply_rollback_record(
        self, task: Dict[str, Any], row: Dict[str, Any]
    ) -> Dict[str, Any]:
        status = self._internal_transfer_status(row)
        if status == 'SUCCESS':
            return self._finish(
                task,
                'rolled_back',
                '提现未执行，资金已退回 Binance forward 子账户',
                binance_rollback_id=_transfer_record_id(row),
            )
        if status == 'FAILURE':
            return self.store.update(
                task['id'],
                status='rollback_retry_required',
                step='retry_binance_rollback',
                attention_required=1,
                status_message='Binance 回滚记录为 FAILURE，需要人工确认后重试',
                binance_rollback_id=_transfer_record_id(row),
                last_checked_at=self.now_fn(),
            )
        return self.store.update(
            task['id'],
            status='rollback_submitted',
            step='verify_binance_rollback',
            status_message=f'Binance 回滚处理中(status={status})',
            binance_rollback_id=_transfer_record_id(row),
            last_checked_at=self.now_fn(),
        )

    def _complete(self, task: Dict[str, Any], **values: Any) -> Dict[str, Any]:
        return self._finish(
            task,
            'completed',
            '资金已划入 Gate forward 全仓合约账户',
            **values,
        )

    def _finish(
        self, task: Dict[str, Any], status: str, message: str, **values: Any
    ) -> Dict[str, Any]:
        return self.store.update(
            task['id'],
            status=status,
            step='finished',
            status_message=message,
            completed_at=self.now_fn(),
            last_checked_at=self.now_fn(),
            **values,
        )

    def _query_error(
        self, task: Dict[str, Any], exc: Exception, message: str
    ) -> Dict[str, Any]:
        return self.store.update(
            task['id'],
            status_message=message,
            last_error=str(exc)[:2000],
            last_checked_at=self.now_fn(),
        )

    def _apply_notifications(self, task: Dict[str, Any]) -> Dict[str, Any]:
        now = self.now_fn()
        status = str(task.get('status') or '')
        task_id = task['id']
        user_id = 'default'
        detail_value = task.get('detail')
        detail = detail_value if isinstance(detail_value, dict) else {}
        if status in TERMINAL_STATUSES and not detail.get('terminal_notified'):
            kind = 'success' if status == 'completed' else 'warning'
            self.notifier(
                title='资金划转完成' if status == 'completed' else '资金划转已终止',
                message=str(task.get('status_message') or ''),
                type=kind,
                source='fund_transfer',
                dedup_key=f'fund_transfer:{task_id}:{status}',
                event_at=now,
                payload={'task_id': task_id, 'status': status},
                user_id=user_id,
            )
            detail = dict(detail)
            detail['terminal_notified'] = True
            return self.store.update(task_id, detail=detail)

        age = now - _created_at(task)
        needs_attention = bool(task.get('attention_required'))
        if needs_attention or age >= timedelta(minutes=self.settings.attention_minutes):
            if not task.get('attention_notified_at'):
                self.notifier(
                    title='资金划转需要人工关注',
                    message=(
                        f"任务 #{task_id}：{task.get('status_message') or status}"
                        if needs_attention
                        else f"任务 #{task_id} 已持续 {self.settings.attention_minutes} 分钟："
                             f"{task.get('status_message') or status}"
                    ),
                    type='error',
                    source='fund_transfer',
                    dedup_key=f'fund_transfer:{task_id}:attention',
                    event_at=now,
                    payload={'task_id': task_id, 'status': status},
                    user_id=user_id,
                )
                return self.store.update(
                    task_id, attention_required=1, attention_notified_at=now
                )
        elif age >= timedelta(minutes=self.settings.delayed_minutes):
            if not task.get('delayed_notified_at'):
                self.notifier(
                    title='资金划转仍在处理中',
                    message=f"任务 #{task_id} 已超过 {self.settings.delayed_minutes} 分钟："
                            f"{task.get('status_message') or status}",
                    type='warning',
                    source='fund_transfer',
                    dedup_key=f'fund_transfer:{task_id}:delayed',
                    event_at=now,
                    payload={'task_id': task_id, 'status': status},
                    user_id=user_id,
                )
                return self.store.update(task_id, delayed_notified_at=now)
        return task


_service: Optional[FundTransferService] = None
_service_lock = threading.Lock()


def get_fund_transfer_service() -> FundTransferService:
    global _service
    if _service is not None:
        return _service
    with _service_lock:
        if _service is None:
            binance, gate = build_default_fund_clients()
            _service = FundTransferService(
                store=FundTransferStore(),
                binance=binance,
                gate=gate,
                settings=FundTransferSettings.from_env(),
            )
    return _service


def fund_transfer_open_locked() -> bool:
    return bool(_service and _service.open_locked)
