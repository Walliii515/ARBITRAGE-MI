# coding: utf-8
"""Forward strategy Binance Cross Margin manual operations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal

from calc.real_executor import RealExecutor
from calc.reconciliation import build_exchange_config
from common.logger import get_logger


MarginOperation = Literal['borrow', 'repay', 'transfer']
TransferDirection = Literal['spot_to_margin', 'margin_to_spot']

logger = get_logger(__name__)


@dataclass(frozen=True)
class ForwardMarginOperationResult:
    success: bool
    message: str
    operation: str
    asset: str
    amount: float
    result: Dict


class ForwardMarginAccountOperator:
    """Small service for protected FORWARD Binance Cross Margin actions."""

    def __init__(self, executor: RealExecutor):
        self.executor = executor

    def borrow_usdt(self, amount: float) -> ForwardMarginOperationResult:
        amount = _validate_usdt_amount(amount)
        result = self.executor.borrow_binance_cross_margin_asset('USDT', amount)
        return self._format_result('borrow', amount, result, '借款')

    def repay_usdt(self, amount: float) -> ForwardMarginOperationResult:
        amount = _validate_usdt_amount(amount)
        result = self.executor.repay_binance_cross_margin_asset('USDT', amount)
        return self._format_result('repay', amount, result, '还款')

    def transfer_usdt(self, amount: float, direction: TransferDirection) -> ForwardMarginOperationResult:
        amount = _validate_usdt_amount(amount)
        if direction not in ('spot_to_margin', 'margin_to_spot'):
            raise ValueError(f'未知划转方向: {direction}')
        result = self.executor.transfer_binance_cross_margin_asset('USDT', amount, direction)
        label = '现货转入杠杆' if direction == 'spot_to_margin' else '杠杆转出现货'
        return self._format_result(f'transfer:{direction}', amount, result, label)

    @staticmethod
    def _format_result(operation: str, amount: float, result: Dict, label: str) -> ForwardMarginOperationResult:
        success = bool(result.get('success'))
        message = f'{label}成功: {amount:g} USDT' if success else f'{label}失败: {result.get("reason") or "unknown"}'
        log_func = logger.info if success else logger.warning
        log_func(
            'FORWARD Binance Margin 手动操作 | op=%s | amount=%.12g | success=%s | result=%s',
            operation,
            amount,
            success,
            _redact_result(result),
        )
        return ForwardMarginOperationResult(
            success=success,
            message=message,
            operation=operation,
            asset='USDT',
            amount=amount,
            result=result,
        )


def build_default_forward_margin_operator() -> ForwardMarginAccountOperator:
    return ForwardMarginAccountOperator(RealExecutor(build_exchange_config()))


def _validate_usdt_amount(amount: float) -> float:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ValueError('金额必须是数字')
    if value <= 0:
        raise ValueError('金额必须大于0')
    return value


def _redact_result(result: Dict) -> Dict:
    redacted = dict(result or {})
    redacted.pop('raw', None)
    return redacted
