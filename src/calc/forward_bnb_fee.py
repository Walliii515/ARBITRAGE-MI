# coding: utf-8
"""Forward strategy helper for buying BNB fee balance in Binance Spot."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict
from uuid import uuid4

from calc.real_executor import RealExecutor
from calc.reconciliation import build_exchange_config, get_forward_gate_leverage
from common.logger import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class ForwardBnbBuyResult:
    success: bool
    message: str
    amount_usdt: float
    result: Dict


class ForwardBnbFeeBuyer:
    """Narrow service for topping up Binance Spot BNB used for fee discounts."""

    def __init__(self, executor: RealExecutor):
        self.executor = executor

    def buy_with_usdt(self, amount_usdt: float) -> ForwardBnbBuyResult:
        amount = _validate_usdt_amount(amount_usdt)
        available = self._spot_usdt_free()
        if amount > available:
            return ForwardBnbBuyResult(
                success=False,
                message=f'Binance USDT 可用余额不足: {available:.2f} < {amount:.2f}',
                amount_usdt=amount,
                result={'success': False, 'reason': 'insufficient_usdt', 'available_usdt': available},
            )

        order = {
            'order_uuid': f'bnb_fee_{uuid4().hex}',
            'base_asset': 'BNB',
            'trade_direction': 'buy',
            'target_amount': amount,
        }
        result = self.executor.place_binance_spot_order(order)
        success = bool(result.get('success'))
        message = (
            f'BNB 市价买入成功: {amount:g} USDT'
            if success
            else f'BNB 市价买入失败: {result.get("reason") or "unknown"}'
        )
        log_func = logger.info if success else logger.warning
        log_func(
            'FORWARD Binance BNB 手续费余额买入 | amount_usdt=%.12g | success=%s | result=%s',
            amount,
            success,
            _redact_order_result(result),
        )
        return ForwardBnbBuyResult(success=success, message=message, amount_usdt=amount, result=result)

    def _spot_usdt_free(self) -> float:
        balances = self.executor.fetch_binance_account_balances()
        usdt = next((item for item in balances if str(item.get('asset') or '').upper() == 'USDT'), None) or {}
        return float(usdt.get('free') or 0)


def build_default_forward_bnb_fee_buyer() -> ForwardBnbFeeBuyer:
    return ForwardBnbFeeBuyer(
        RealExecutor(build_exchange_config(), leverage=get_forward_gate_leverage())
    )


def _validate_usdt_amount(amount: float) -> float:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ValueError('金额必须是数字')
    if value < 5:
        raise ValueError('买入金额至少 5 USDT')
    if value > 200:
        raise ValueError('单次买入金额不能超过 200 USDT')
    return value


def _redact_order_result(result: Dict) -> Dict:
    redacted = dict(result or {})
    redacted.pop('raw', None)
    return redacted
