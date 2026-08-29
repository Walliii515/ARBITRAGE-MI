# coding: utf-8
"""交易所断腿自动处置。

Gate futures 被 ADL 自动减仓后，本地 holding 仍对应 Binance spot 多头。
本模块由实时 Gate 风险事件触发，自动卖出对应 spot，关闭本地持仓。
"""
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from typing import Dict, List, Optional

from calc.order_fee_resolver import build_order_execution_fields
from calc.orderbook_enricher import calc_vwap_basis_bps
from calc.real_executor import RealExecutor
from calc.asset_reduction_guard import asset_reduction_guard
from calc.closed_position_pnl import (
    compute_closed_position_pnl,
    existing_position_columns,
    fetch_executed_position_orders,
    update_closed_position_pnl,
)
from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)

POST_CLOSE_SPOT_DUST_PENDING = 'post_close_spot_dust_pending'


@dataclass
class ExchangeDesyncRemediationConfig:
    enabled: bool = True
    action: str = 'sell_spot'
    max_positions_per_run: int = 0
    min_spot_qty: float = 0.0
    close_extra_gate_position: bool = True
    remediate_binance_spot_position: bool = True
    spot_open_fee: float = 0.00075
    spot_close_fee: float = 0.00075
    future_open_fee: float = 0.0002
    future_close_fee: float = 0.0002
    future_taker_open_fee: float = 0.0005
    future_taker_close_fee: float = 0.0005
    low_notional_buffer_ratio: float = 1.2
    low_notional_fok_slippage_bps: float = 100.0
    low_notional_retry_cooldown_sec: float = 300.0


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _guard_asset_reduction(owner: str):
    def decorate(func):
        @wraps(func)
        def wrapped(self, base_asset, *args, **kwargs):
            asset = str(base_asset or '').upper()
            with asset_reduction_guard.claim(asset, owner) as acquired:
                if not acquired:
                    active_owner = asset_reduction_guard.owner(asset)
                    logger.warning(
                        "同币种已有减仓执行，对账处置等待新快照 | %s | owner=%s | requested=%s",
                        asset,
                        active_owner,
                        owner,
                    )
                    return {
                        'attempted': False,
                        'success': False,
                        'base_asset': asset,
                        'reason': 'asset_reduction_inflight',
                        'active_owner': active_owner,
                    }
                return func(self, asset, *args, **kwargs)
        return wrapped
    return decorate


class ExchangeDesyncRemediator:
    """把 Gate 缺腿风险转成可审计的自动 spot 处置。"""

    def __init__(self, executor: RealExecutor, cfg: ExchangeDesyncRemediationConfig):
        self.executor = executor
        self.cfg = cfg
        self._position_columns_cache: Optional[set[str]] = None
        self._low_notional_retry_after: Dict[str, datetime] = {}

    @_guard_asset_reduction('reconciliation_gate_missing')
    def remediate_gate_short_desync(
        self,
        base_asset: str,
        missing_contracts: float,
        risk: Dict,
        *,
        require_desynced: bool = True,
        mark_positions: bool = False,
    ) -> Dict:
        base_asset = str(base_asset or '').upper()
        if not self.cfg.enabled:
            return {'attempted': False, 'reason': 'disabled'}
        if self.cfg.action != 'sell_spot':
            return {'attempted': False, 'reason': f'unsupported_action:{self.cfg.action}'}
        if missing_contracts <= 0:
            return {'attempted': False, 'reason': 'missing_contracts<=0'}
        risk = self._risk_with_recent_liquidation(base_asset, risk)

        positions = self._load_positions_to_remediate(
            base_asset,
            missing_contracts,
            require_desynced=require_desynced,
        )
        if not positions:
            return {'attempted': False, 'reason': 'no_matching_holding_positions'}

        available_qty = self._load_binance_available_qty(base_asset)
        remaining_available = available_qty
        selected_positions = positions
        if mark_positions:
            self._mark_positions_exchange_risk(selected_positions, risk)

        dust_result = self._try_remediate_full_asset_dust(
            base_asset=base_asset,
            positions=selected_positions,
            available_qty=available_qty,
            missing_contracts=missing_contracts,
            risk=risk,
        )
        if dust_result is not None:
            return dust_result

        results = []
        for pos in selected_positions:
            target_qty = min(_float(pos.get('spot_open_qty')), remaining_available)
            if target_qty <= max(float(self.cfg.min_spot_qty or 0), 0):
                prior_result = self._close_position_from_prior_spot_fill(pos, risk)
                if prior_result.get('success'):
                    results.append(prior_result)
                else:
                    results.append({
                        'attempted': True,
                        'position_id': pos.get('id'),
                        'success': False,
                        'reason': prior_result.get('reason') or 'spot_available_qty_insufficient',
                    })
                continue
            min_notional_reason = self._below_spot_min_notional(pos, target_qty)
            if min_notional_reason:
                self._append_risk_detail(pos.get('id'), f"自动处置跳过|{min_notional_reason}")
                logger.warning(
                    "Gate 缺腿自动处置跳过低名义残余 | %s | position_id=%s | qty=%s | %s",
                    base_asset, pos.get('id'), target_qty, min_notional_reason,
                )
                results.append({
                    'attempted': True,
                    'success': False,
                    'position_id': pos.get('id'),
                    'reason': min_notional_reason,
                })
                continue

            result = self._sell_spot_and_close_position(pos, target_qty, risk)
            results.append(result)
            remaining_available -= _float(result.get('spot_exec_qty'))
            if not result.get('success'):
                break

        success_count = sum(1 for item in results if item.get('success'))
        failure_count = sum(1 for item in results if item.get('attempted') and not item.get('success'))
        return {
            'attempted': True,
            'success': failure_count == 0 and success_count == len(selected_positions),
            'action': 'sell_spot',
            'base_asset': base_asset,
            'positions': len(selected_positions),
            'matching_positions': len(positions),
            'success_count': success_count,
            'failure_count': failure_count,
            'results': results,
        }

    def remediate_post_close_spot_dust(
        self,
        base_asset: str,
        local_spot_qty: float,
        exchange_spot_qty: float,
    ) -> Dict:
        """Convert a fully reconciled spot-only close remainder and retire its local positions."""
        result = self.remediate_post_close_spot_dust_batch([{
            'base_asset': base_asset,
            'local_spot_qty': local_spot_qty,
            'exchange_spot_qty': exchange_spot_qty,
        }])
        return result

    def remediate_post_close_spot_dust_batch(self, candidates: List[Dict]) -> Dict:
        """Convert multiple fully reconciled spot-only close remainders in one Binance request."""
        if not self.cfg.enabled:
            return {'attempted': False, 'reason': 'disabled'}
        if not candidates:
            return {'attempted': False, 'reason': 'no_post_close_spot_dust_candidates'}
        cooldown_remaining = self._dust_conversion_cooldown_remaining_sec()
        if cooldown_remaining > 0:
            return {
                'attempted': False,
                'reason': 'binance_dust_conversion_cooldown',
                'cooldown_remaining_sec': cooldown_remaining,
            }

        prepared_items: List[Dict] = []
        skipped: List[Dict] = []
        for candidate in candidates:
            base_asset = str(candidate.get('base_asset') or '').upper()
            local_spot_qty = _float(candidate.get('local_spot_qty'))
            exchange_spot_qty = _float(candidate.get('exchange_spot_qty'))
            if not base_asset or local_spot_qty <= 0 or exchange_spot_qty <= 0:
                skipped.append({'base_asset': base_asset, 'reason': 'spot_qty<=0'})
                continue
            positions = self._load_post_close_spot_dust_positions(base_asset)
            if not positions:
                skipped.append({'base_asset': base_asset, 'reason': 'no_post_close_spot_dust_positions'})
                continue
            available_qty = self._load_binance_available_qty(base_asset)
            prepared = self._prepare_dust_cleanup_candidate(
                base_asset,
                positions,
                {'total': exchange_spot_qty, 'free': available_qty},
                {'size': 0, 'mark_price': self._estimate_binance_spot_price(base_asset, {})},
            )
            if not prepared.get('eligible'):
                skipped.append({
                    'base_asset': base_asset,
                    'reason': prepared.get('reason') or 'post_close_spot_not_convertible',
                })
                continue
            prepared['risk_type'] = 'post_close_spot_dust'
            prepared_items.append(prepared)

        if not prepared_items:
            return {
                'attempted': False,
                'reason': 'post_close_spot_not_convertible',
                'skipped': skipped,
            }
        result = self._execute_dust_cleanup_batch(prepared_items)
        result['skipped'] = skipped
        return result

    def cleanup_post_close_dust(
        self,
        binance_balances: List[Dict],
        gate_positions: List[Dict],
    ) -> Dict:
        """Manually close a fully reconstructed tiny hedge and convert its spot dust."""
        if not self.cfg.enabled:
            return {'success': False, 'attempted': False, 'reason': 'disabled'}
        balances_by_asset = {
            str(row.get('asset') or '').upper(): row for row in (binance_balances or [])
        }
        gate_by_asset = {
            str(row.get('base_asset') or '').upper(): row for row in (gate_positions or [])
        }
        positions = self._load_holding_positions_with_execution_remainders()
        settled = self._settle_spot_only_dust_positions(
            positions,
            balances_by_asset,
            gate_by_asset,
        )
        if settled:
            positions = self._load_holding_positions_with_execution_remainders()

        grouped: Dict[str, List[Dict]] = {}
        for pos in positions:
            grouped.setdefault(str(pos.get('base_asset') or '').upper(), []).append(pos)

        recovered = self._recover_completed_dust_cleanup(
            grouped,
            balances_by_asset,
            gate_by_asset,
        )
        if recovered is not None:
            return recovered

        cooldown_remaining = self._dust_conversion_cooldown_remaining_sec()
        if cooldown_remaining > 0:
            if settled:
                return {
                    'success': True,
                    'attempted': True,
                    'action': 'settle_spot_only_dust_pending',
                    'positions': len(settled),
                    'base_assets': sorted({item['base_asset'] for item in settled}),
                    'settled_pending_positions': settled,
                    'conversion_deferred_reason': 'binance_dust_conversion_cooldown',
                    'cooldown_remaining_sec': round(cooldown_remaining, 1),
                    'message': '低名义现货残差已结算，小额兑换等待 Binance 冷却结束',
                }
            return {
                'success': False,
                'attempted': False,
                'reason': 'binance_dust_conversion_cooldown',
                'cooldown_remaining_sec': round(cooldown_remaining, 1),
            }

        skipped: List[Dict] = []
        prepared_items: List[Dict] = []
        for base_asset in sorted(grouped):
            prepared = self._prepare_dust_cleanup_candidate(
                base_asset,
                grouped[base_asset],
                balances_by_asset.get(base_asset),
                gate_by_asset.get(base_asset),
            )
            if not prepared.get('eligible'):
                if prepared.get('candidate'):
                    skipped.append({
                        'base_asset': base_asset,
                        'reason': prepared.get('reason'),
                    })
                continue
            prepared['risk_type'] = 'post_close_dust'
            prepared_items.append(prepared)

        if prepared_items:
            result = self._execute_dust_cleanup_batch(prepared_items)
            result['skipped'] = skipped
            result['settled_pending_positions'] = settled
            return result

        if settled:
            return {
                'success': True,
                'attempted': True,
                'action': 'settle_spot_only_dust_pending',
                'positions': len(settled),
                'base_assets': sorted({item['base_asset'] for item in settled}),
                'settled_pending_positions': settled,
                'message': '低名义现货残差已从持仓中结算，等待整币种最终兑换',
                'skipped': skipped,
            }

        return {
            'success': True,
            'attempted': False,
            'action': 'no_safe_dust_found',
            'message': '未发现可安全兑换的小额残余',
            'skipped': skipped,
        }

    def _settle_spot_only_dust_positions(
        self,
        positions: List[Dict],
        balances_by_asset: Dict[str, Dict],
        gate_by_asset: Dict[str, Dict],
    ) -> List[Dict]:
        """Close economic history while retaining an unsellable spot remainder in exposure."""
        grouped: Dict[str, List[Dict]] = {}
        for pos in positions:
            grouped.setdefault(str(pos.get('base_asset') or '').upper(), []).append(pos)

        settled: List[Dict] = []
        for base_asset, asset_positions in grouped.items():
            spot_meta = (getattr(self.executor, 'spot_meta', {}) or {}).get(base_asset) or {}
            min_notional = _float(spot_meta.get('min_notional'))
            step_size = _float(spot_meta.get('step_size'))
            qty_tolerance = max(step_size * 1e-6, 1e-8)
            if min_notional <= 0:
                continue

            ledger_spot = sum(_float(pos.get('_spot_remaining_qty')) for pos in asset_positions)
            exchange_spot = _float((balances_by_asset.get(base_asset) or {}).get('total'))
            multiplier = self._quanto_multiplier(base_asset)
            ledger_gate_contracts = sum(
                _float(pos.get('_future_remaining_qty')) / multiplier
                for pos in asset_positions
                if multiplier > 0
            )
            exchange_gate_contracts = abs(
                _float((gate_by_asset.get(base_asset) or {}).get('size'))
            )
            if abs(ledger_spot - exchange_spot) > qty_tolerance:
                continue
            if abs(ledger_gate_contracts - exchange_gate_contracts) > 1e-6:
                continue

            price = self._estimate_binance_spot_price(base_asset, {})
            if price <= 0:
                price = max(
                    (_float(pos.get('spot_open_price')) for pos in asset_positions),
                    default=0.0,
                )
            if price <= 0:
                continue

            for pos in asset_positions:
                if str(pos.get('status') or '').lower() != 'holding':
                    continue
                if not self._is_low_notional_residual_position(pos):
                    continue
                spot_remaining = _float(pos.get('_spot_remaining_qty'))
                future_remaining = _float(pos.get('_future_remaining_qty'))
                if spot_remaining <= qty_tolerance or future_remaining > 1e-8:
                    continue
                if abs(_float(pos.get('spot_open_qty')) - spot_remaining) > qty_tolerance:
                    continue
                if spot_remaining * price + 1e-9 >= min_notional:
                    continue
                if not self._mark_spot_dust_pending(pos, spot_remaining, price):
                    continue
                settled.append({
                    'position_id': int(pos['id']),
                    'base_asset': base_asset,
                    'spot_qty': spot_remaining,
                    'spot_notional': spot_remaining * price,
                })
        return settled

    def _mark_spot_dust_pending(self, pos: Dict, spot_qty: float, price: float) -> bool:
        position_id = int(pos['id'])
        orders = fetch_executed_position_orders(position_id)
        close_values = self._close_execution_values(
            orders,
            str(pos.get('base_asset') or ''),
        )
        pnl_values = compute_closed_position_pnl(pos, orders)
        closed_at = next((
            order.get('executed_at')
            for order in reversed(orders)
            if str(order.get('order_side') or '').lower() == 'close'
            and order.get('executed_at') is not None
        ), datetime.now())
        close_funding_rate_24h = next((
            order.get('funding_rate_24h')
            for order in reversed(orders)
            if str(order.get('order_side') or '').lower() == 'close'
            and order.get('funding_rate_24h') is not None
        ), None)
        reason = (
            f'低名义现货残差待兑换|qty={spot_qty:g}|price={price:g}|'
            f'notional={spot_qty * price:.4f}USDT|暂按0回收价值结算'
        )
        sql = """
            UPDATE mi_trade_position SET
                status = 'closed',
                closed_at = %(closed_at)s,
                close_reason = CONCAT(COALESCE(close_reason, ''), '|', %(reason)s),
                spot_open_qty = %(spot_open_qty)s,
                future_open_qty = %(future_open_qty)s,
                future_open_contracts = %(future_open_contracts)s,
                spot_open_price = %(spot_open_price)s,
                future_open_price = %(future_open_price)s,
                spot_open_amount = %(spot_open_amount)s,
                spot_close_price = %(spot_close_price)s,
                future_close_price = %(future_close_price)s,
                spot_close_amount = %(spot_close_amount)s,
                future_close_amount = %(future_close_amount)s,
                close_spread_bps = %(close_spread_bps)s,
                close_funding_rate_24h = %(close_funding_rate_24h)s,
                exchange_risk_status = 'normal',
                exchange_risk_type = %(pending_type)s,
                exchange_risk_detail = CONCAT(COALESCE(exchange_risk_detail, ''), '|', %(reason)s)
            WHERE id = %(position_id)s
              AND status = 'holding'
              AND COALESCE(ABS(future_open_qty), 0) <= 1e-8
              AND COALESCE(ABS(future_open_contracts), 0) <= 1e-8
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'closed_at': closed_at,
                'reason': reason,
                'position_id': position_id,
                'pending_type': POST_CLOSE_SPOT_DUST_PENDING,
                'close_funding_rate_24h': close_funding_rate_24h,
                **close_values,
            })
            updated = int(getattr(cursor, 'rowcount', 0) or 0)
            if updated and pnl_values:
                update_closed_position_pnl(
                    cursor,
                    position_id,
                    pnl_values,
                    self._position_columns(),
                )
        return bool(updated)

    def _recover_completed_dust_cleanup(
        self,
        grouped: Dict[str, List[Dict]],
        balances_by_asset: Dict[str, Dict],
        gate_by_asset: Dict[str, Dict],
    ) -> Optional[Dict]:
        """Finalize history after both exchange actions succeeded but DB finalization stopped."""
        for base_asset in sorted(grouped):
            positions = grouped[base_asset]
            if not positions or any(
                not self._is_low_notional_residual_position(pos)
                for pos in positions
            ):
                continue
            ledger_spot = sum(_float(pos.get('_spot_remaining_qty')) for pos in positions)
            ledger_future = sum(_float(pos.get('_future_remaining_qty')) for pos in positions)
            local_spot = sum(_float(pos.get('spot_open_qty')) for pos in positions)
            exchange_spot = _float((balances_by_asset.get(base_asset) or {}).get('total'))
            exchange_gate = abs(_float((gate_by_asset.get(base_asset) or {}).get('size')))
            dust_order_count = sum(int(pos.get('dust_cleanup_order_count') or 0) for pos in positions)
            if (
                local_spot <= 1e-8
                or ledger_spot > 1e-8
                or ledger_future > 1e-8
                or exchange_spot > 1e-8
                or exchange_gate > 1e-8
                or dust_order_count <= 0
            ):
                continue

            reason = (
                f'平仓残余尘埃处置恢复|asset={base_asset}|'
                f'positions={len(positions)}|两腿交易所及订单账本均已归零'
            )
            self._finalize_dust_positions(positions, reason)
            return {
                'success': True,
                'attempted': True,
                'action': 'finalize_completed_dust_cleanup',
                'base_asset': base_asset,
                'positions': len(positions),
                'message': f'{base_asset} 小额残余交易已完成，历史持仓已恢复结算',
            }
        return None

    def _prepare_dust_cleanup_candidate(
        self,
        base_asset: str,
        positions: List[Dict],
        balance: Optional[Dict],
        gate_position: Optional[Dict],
    ) -> Dict:
        if not positions:
            return {'eligible': False, 'candidate': False, 'reason': 'contains_active_position'}

        spot_meta = (getattr(self.executor, 'spot_meta', {}) or {}).get(base_asset) or {}
        min_notional = _float(spot_meta.get('min_notional'))
        step_size = _float(spot_meta.get('step_size'))
        qty_tolerance = max(step_size * 1e-6, 1e-8)
        if min_notional <= 0:
            return {'eligible': False, 'candidate': True, 'reason': 'missing_spot_min_notional'}

        def _local_remaining_qty(pos: Dict, field: str, ledger_field: str) -> float:
            if (
                str(pos.get('status') or '').lower() == 'closed'
                and str(pos.get('exchange_risk_type') or '') == POST_CLOSE_SPOT_DUST_PENDING
            ):
                return max(0.0, _float(pos.get(ledger_field)))
            return max(0.0, _float(pos.get(field)))

        ledger_spot_qty = sum(max(0.0, _float(pos.get('_spot_remaining_qty'))) for pos in positions)
        local_spot_qty = sum(
            _local_remaining_qty(pos, 'spot_open_qty', '_spot_remaining_qty')
            for pos in positions
        )
        exchange_spot_qty = _float((balance or {}).get('total'))
        free_spot_qty = _float((balance or {}).get('free'), exchange_spot_qty)
        if abs(local_spot_qty - ledger_spot_qty) > qty_tolerance:
            return {'eligible': False, 'candidate': True, 'reason': 'local_spot_not_explained_by_orders'}
        if abs(exchange_spot_qty - ledger_spot_qty) > qty_tolerance:
            return {'eligible': False, 'candidate': True, 'reason': 'exchange_spot_not_explained_by_orders'}
        if abs(free_spot_qty - exchange_spot_qty) > qty_tolerance:
            return {'eligible': False, 'candidate': True, 'reason': 'spot_balance_locked'}

        price = self._estimate_binance_spot_price(base_asset, {})
        if price <= 0:
            price = max((_float(pos.get('spot_open_price')) for pos in positions), default=0.0)
        spot_notional = exchange_spot_qty * price
        if price <= 0 or spot_notional + 1e-9 >= min_notional:
            return {'eligible': False, 'candidate': True, 'reason': 'spot_not_dust'}

        multiplier = self._quanto_multiplier(base_asset)
        ledger_future_qty = sum(max(0.0, _float(pos.get('_future_remaining_qty'))) for pos in positions)
        local_future_qty = sum(
            _local_remaining_qty(pos, 'future_open_qty', '_future_remaining_qty')
            for pos in positions
        )
        future_qty_tolerance = max(multiplier * 1e-6, 1e-8)
        if abs(local_future_qty - ledger_future_qty) > future_qty_tolerance:
            return {'eligible': False, 'candidate': True, 'reason': 'local_gate_not_explained_by_orders'}
        gate_size = _float((gate_position or {}).get('size'))
        if multiplier <= 0 and (
            ledger_future_qty > future_qty_tolerance
            or abs(gate_size) > 1e-9
        ):
            return {'eligible': False, 'candidate': True, 'reason': 'missing_quanto_multiplier'}
        ledger_contracts = ledger_future_qty / multiplier if multiplier > 0 else 0.0
        if ledger_spot_qty <= qty_tolerance and ledger_contracts <= 1e-6:
            return {'eligible': False, 'candidate': False, 'reason': 'no_execution_remainder'}
        if gate_size > 1e-9:
            return {'eligible': False, 'candidate': True, 'reason': 'gate_position_not_short'}
        exchange_contracts = abs(gate_size)
        if abs(exchange_contracts - ledger_contracts) > 1e-6:
            return {'eligible': False, 'candidate': True, 'reason': 'gate_position_not_explained_by_orders'}
        gate_mark_price = _float((gate_position or {}).get('mark_price'), price)
        gate_notional = exchange_contracts * multiplier * gate_mark_price
        if gate_notional + 1e-9 >= min_notional:
            return {'eligible': False, 'candidate': True, 'reason': 'gate_position_not_dust'}

        return {
            'eligible': True,
            'candidate': True,
            'base_asset': base_asset,
            'positions': positions,
            'spot_qty': exchange_spot_qty,
            'spot_notional': spot_notional,
            'gate_contracts': exchange_contracts,
            'gate_qty': ledger_future_qty,
            'gate_mark_price': gate_mark_price,
            'gate_position': gate_position or {},
        }

    def _execute_dust_cleanup_batch(self, prepared_items: List[Dict]) -> Dict:
        ready: List[Dict] = []
        results: List[Dict] = []
        for prepared in sorted(prepared_items, key=lambda item: str(item.get('base_asset') or '')):
            gate_result = self._close_gate_dust_before_conversion(prepared)
            if gate_result.get('failed'):
                results.append(gate_result['result'])
                continue
            prepared['_gate_result'] = gate_result.get('gate_result')
            prepared['_cleanup_reason'] = gate_result.get('reason')
            ready.append(prepared)

        if not ready:
            return {
                'success': False,
                'attempted': True,
                'action': 'cleanup_post_close_dust_batch',
                'reason': 'no_dust_ready_for_conversion',
                'results': results,
                'success_count': 0,
                'failure_count': len(results),
            }

        conversions = self._convert_binance_dust_assets([
            str(item.get('base_asset') or '').upper()
            for item in ready
        ])
        if not conversions.get('success') and not conversions.get('results'):
            failure = {
                'success': False,
                'attempted': True,
                'action': 'convert_binance_dust_to_bnb_batch',
                'base_assets': [item.get('base_asset') for item in ready],
                'reason': conversions.get('reason') or 'dust_conversion_failed',
                'conversion': conversions,
            }
            results.append(failure)
            return {
                'success': False,
                'attempted': True,
                'action': 'cleanup_post_close_dust_batch',
                'reason': failure['reason'],
                'results': results,
                'success_count': 0,
                'failure_count': len(results),
            }

        conversion_by_asset = conversions.get('results') or {}
        for prepared in ready:
            base_asset = str(prepared.get('base_asset') or '').upper()
            conversion = conversion_by_asset.get(base_asset)
            gate_result = prepared.get('_gate_result')
            if not conversion or not conversion.get('success'):
                results.append({
                    'success': False,
                    'attempted': True,
                    'action': 'convert_binance_dust_to_bnb',
                    'base_asset': base_asset,
                    'reason': (
                        (conversion or {}).get('reason')
                        or 'dust_conversion_missing_transfer_result'
                    ),
                    'gate_result': gate_result,
                    'conversion': conversion,
                })
                continue
            if abs(_float(conversion.get('source_qty')) - _float(prepared.get('spot_qty'))) > 1e-8:
                results.append({
                    'success': False,
                    'attempted': True,
                    'action': 'convert_binance_dust_to_bnb',
                    'base_asset': base_asset,
                    'reason': 'dust_conversion_qty_mismatch',
                    'gate_result': gate_result,
                    'conversion': conversion,
                })
                continue

            self._close_positions_after_dust_conversion(
                prepared['positions'],
                conversion,
                {
                    'type': prepared.get('risk_type') or 'post_close_dust',
                    'detail': prepared.get('_cleanup_reason'),
                },
            )
            results.append({
                'success': True,
                'attempted': True,
                'action': 'cleanup_post_close_dust',
                'base_asset': base_asset,
                'positions': len(prepared['positions']),
                'spot_qty': conversion.get('source_qty'),
                'bnb_qty': conversion.get('bnb_qty'),
                'gate_contracts_closed': _float(prepared.get('gate_contracts')),
                'transaction_id': conversion.get('transaction_id'),
                'gate_result': gate_result,
            })

        success_results = [item for item in results if item.get('success')]
        failure_results = [item for item in results if item.get('attempted') and not item.get('success')]
        closed_positions = sum(int(item.get('positions') or 0) for item in success_results)
        if success_results and failure_results:
            message = (
                f"小额残余批量部分完成，成功资产 {len(success_results)} 个/"
                f"持仓 {closed_positions} 笔，失败 {len(failure_results)} 个"
            )
        elif success_results:
            message = (
                f"小额残余批量清理完成，资产 {len(success_results)} 个，"
                f"持仓 {closed_positions} 笔"
            )
        else:
            message = '小额残余批量清理失败'
        summary = {
            'success': bool(success_results) and not failure_results,
            'attempted': True,
            'action': 'cleanup_post_close_dust_batch',
            'base_assets': [item.get('base_asset') for item in success_results],
            'asset_count': len(success_results),
            'positions': closed_positions,
            'success_count': closed_positions,
            'asset_success_count': len(success_results),
            'failure_count': len(failure_results),
            'results': results,
            'message': message,
        }
        if len(success_results) == 1:
            summary.update({
                'base_asset': success_results[0].get('base_asset'),
                'spot_qty': success_results[0].get('spot_qty'),
                'bnb_qty': success_results[0].get('bnb_qty'),
                'gate_contracts_closed': success_results[0].get('gate_contracts_closed'),
                'transaction_id': success_results[0].get('transaction_id'),
                'message': (
                    f"{success_results[0].get('base_asset')} 小额残余已清理，"
                    f"共关闭 {success_results[0].get('positions')} 笔持仓"
                ),
            })
        if failure_results:
            summary['reason'] = failure_results[0].get('reason') or 'dust_cleanup_partial_failed'
        return summary

    def _close_gate_dust_before_conversion(self, prepared: Dict) -> Dict:
        base_asset = prepared['base_asset']
        positions = prepared['positions']
        gate_contracts = _float(prepared.get('gate_contracts'))
        gate_result: Optional[Dict] = None
        reason = (
            f"手动小额兑换|asset={base_asset}|spot={prepared.get('spot_qty'):g}|"
            f"gate_contracts={gate_contracts:g}"
        )
        if gate_contracts > 0:
            gate_result = self.remediate_gate_extra_position(
                base_asset=base_asset,
                extra_contracts=gate_contracts,
                risk={
                    'type': 'post_close_dust',
                    'contract': f'{base_asset}_USDT',
                    'exchange_size': _float((prepared.get('gate_position') or {}).get('size')),
                    'mark_price': prepared.get('gate_mark_price'),
                },
            )
            if not gate_result.get('success'):
                return {
                    'failed': True,
                    'result': {
                        'success': False,
                        'attempted': True,
                        'action': 'close_gate_dust_future',
                        'base_asset': base_asset,
                        'reason': gate_result.get('reason') or 'gate_dust_close_failed',
                        'gate_result': gate_result,
                    },
                }
            future_result = gate_result.get('future_result') or {}
            expected_qty = _float(prepared.get('gate_qty'))
            if _float(future_result.get('exec_qty')) + 1e-9 < expected_qty:
                return {
                    'failed': True,
                    'result': {
                        'success': False,
                        'attempted': True,
                        'action': 'close_gate_dust_future',
                        'base_asset': base_asset,
                        'reason': 'gate_dust_close_partial',
                        'gate_result': gate_result,
                    },
                }
            self._record_allocated_dust_orders(
                positions,
                market_type='future',
                total_qty=expected_qty,
                exec_price=_float(future_result.get('exec_price')),
                exchange_order_id=future_result.get('exchange_order_id'),
                liquidity_role=future_result.get('liquidity_role') or 'taker',
                fee_amount=_float(future_result.get('fee_amount')),
                fee_amount_usdt=_float(future_result.get('fee_amount_usdt')),
                fee_asset=future_result.get('fee_asset') or 'USDT',
                reason=reason,
            )
            self._zero_local_future_dust(positions, reason)

        return {
            'failed': False,
            'gate_result': gate_result,
            'reason': reason,
        }

    def _convert_binance_dust_assets(self, assets: List[str]) -> Dict:
        batch_converter = getattr(self.executor, 'convert_binance_spot_dust_to_bnb_batch', None)
        if callable(batch_converter):
            return batch_converter(assets)
        single_converter = getattr(self.executor, 'convert_binance_spot_dust_to_bnb', None)
        if not callable(single_converter):
            return {'success': False, 'reason': 'dust_converter_missing', 'results': {}}
        results: Dict[str, Dict] = {}
        failures: List[Dict] = []
        for asset in assets:
            conversion = single_converter(asset)
            if conversion.get('success'):
                results[str(asset or '').upper()] = conversion
            else:
                failures.append({'asset': asset, 'reason': conversion.get('reason')})
        return {
            'success': bool(results) and not failures,
            'results': results,
            'failures': failures,
            'reason': failures[0]['reason'] if failures else None,
        }

    def _try_remediate_full_asset_dust(
        self,
        base_asset: str,
        positions: List[Dict],
        available_qty: float,
        missing_contracts: float,
        risk: Dict,
        expected_spot_qty: Optional[float] = None,
        exchange_spot_qty: Optional[float] = None,
    ) -> Optional[Dict]:
        """仅在 Gate 已归零且全部现货都是尘埃时转换并核销本地残仓。"""
        converter = getattr(self.executor, 'convert_binance_spot_dust_to_bnb', None)
        if not callable(converter) or not positions or available_qty <= 0:
            return None
        risk_type = str(risk.get('type') or '')
        if risk_type not in {'missing_gate_position', 'post_close_spot_dust'}:
            return None
        if abs(_float(risk.get('exchange_contracts'))) > 1e-9:
            return None

        selected_contracts = sum(abs(_float(pos.get('future_open_contracts'))) for pos in positions)
        if risk_type == 'missing_gate_position' and abs(selected_contracts - float(missing_contracts or 0)) > 1e-9:
            return None
        if risk_type == 'post_close_spot_dust' and selected_contracts > 1e-9:
            return None
        selected_spot_qty = sum(max(0.0, _float(pos.get('spot_open_qty'))) for pos in positions)
        spot_meta = (getattr(self.executor, 'spot_meta', {}) or {}).get(base_asset) or {}
        qty_tolerance = max(_float(spot_meta.get('step_size')) * 1e-6, 1e-8)
        if abs(selected_spot_qty - available_qty) > qty_tolerance:
            return None
        if expected_spot_qty is not None and abs(selected_spot_qty - expected_spot_qty) > qty_tolerance:
            return None
        if exchange_spot_qty is not None and abs(available_qty - exchange_spot_qty) > qty_tolerance:
            return None

        min_notional = _float(spot_meta.get('min_notional'))
        price = self._estimate_binance_spot_price(base_asset, risk)
        if price <= 0:
            price = max((_float(pos.get('spot_open_price')) for pos in positions), default=0.0)
        if min_notional <= 0 or price <= 0 or available_qty * price + 1e-9 >= min_notional:
            return None

        conversion = converter(base_asset)
        if not conversion.get('success'):
            reason = conversion.get('reason') or 'dust_conversion_failed'
            for pos in positions:
                self._append_risk_detail(pos.get('id'), f"尘埃转换失败|{reason}")
            return {
                'attempted': True,
                'success': False,
                'action': 'convert_binance_dust_to_bnb',
                'base_asset': base_asset,
                'positions': len(positions),
                'matching_positions': len(positions),
                'success_count': 0,
                'failure_count': len(positions),
                'reason': reason,
                'results': [],
            }

        converted_qty = _float(conversion.get('source_qty'))
        if abs(converted_qty - available_qty) > qty_tolerance:
            reason = f'dust_conversion_qty_mismatch:{converted_qty:g}!={available_qty:g}'
            for pos in positions:
                self._append_risk_detail(pos.get('id'), reason)
            return {
                'attempted': True,
                'success': False,
                'action': 'convert_binance_dust_to_bnb',
                'base_asset': base_asset,
                'positions': len(positions),
                'matching_positions': len(positions),
                'success_count': 0,
                'failure_count': len(positions),
                'reason': reason,
                'results': [],
            }

        self._close_positions_after_dust_conversion(positions, conversion, risk)
        logger.warning(
            "尘埃处置完成 | %s | type=%s | positions=%s | spot_qty=%s | bnb=%s | tran_id=%s",
            base_asset, risk_type,
            len(positions), conversion.get('source_qty'),
            conversion.get('bnb_qty'), conversion.get('transaction_id'),
        )
        results = [
            {'attempted': True, 'success': True, 'position_id': pos.get('id')}
            for pos in positions
        ]
        return {
            'attempted': True,
            'success': True,
            'action': 'convert_binance_dust_to_bnb',
            'base_asset': base_asset,
            'positions': len(positions),
            'matching_positions': len(positions),
            'success_count': len(positions),
            'failure_count': 0,
            'source_qty': conversion.get('source_qty'),
            'bnb_qty': conversion.get('bnb_qty'),
            'transaction_id': conversion.get('transaction_id'),
            'results': results,
        }

    def _close_positions_after_dust_conversion(
        self,
        positions: List[Dict],
        conversion: Dict,
        risk: Dict,
    ) -> None:
        ids = [int(pos['id']) for pos in positions if pos.get('id') is not None]
        if not ids:
            return
        reason_prefix = (
            '平仓残余尘埃处置'
            if str(risk.get('type') or '') in {'post_close_spot_dust', 'post_close_dust'}
            else '交易所断腿尘埃处置'
        )
        reason = (
            f"{reason_prefix}|Binance小额资产转BNB|"
            f"asset={conversion.get('asset')}|qty={conversion.get('source_qty')}|"
            f"bnb={conversion.get('bnb_qty')}|tran_id={conversion.get('transaction_id')}|"
            f"关联风险={risk.get('type', 'unknown')}"
        )
        gross_price = (
            _float(conversion.get('gross_exec_price_usdt'))
            or _float(conversion.get('exec_price_usdt'))
        )
        self._record_allocated_dust_orders(
            positions,
            market_type='spot',
            total_qty=_float(conversion.get('source_qty')),
            exec_price=gross_price,
            exchange_order_id=f"dust:{conversion.get('transaction_id') or ''}",
            liquidity_role='unknown',
            fee_amount=_float(conversion.get('service_charge_bnb')),
            fee_amount_usdt=_float(conversion.get('service_charge_usdt')),
            fee_asset='BNB',
            reason=reason,
        )

        self._finalize_dust_positions(positions, reason)

    def _finalize_dust_positions(self, positions: List[Dict], reason: str) -> None:
        """Close local history from the already complete executed-order ledger."""

        now = datetime.now()
        for pos in positions:
            position_id = int(pos['id'])
            orders = fetch_executed_position_orders(position_id)
            close_funding_rate_24h = next((
                order.get('funding_rate_24h')
                for order in reversed(orders)
                if str(order.get('order_side') or '').lower() == 'close'
                and order.get('funding_rate_24h') is not None
            ), None)
            close_values = self._close_execution_values(
                orders,
                str(pos.get('base_asset') or ''),
            )
            pnl_values = compute_closed_position_pnl(pos, orders)
            sql = """
                UPDATE mi_trade_position SET
                    status = 'closed',
                    closed_at = COALESCE(closed_at, %(closed_at)s),
                    close_reason = CONCAT(COALESCE(close_reason, ''), '|', %(reason)s),
                    spot_open_qty = %(spot_open_qty)s,
                    future_open_qty = %(future_open_qty)s,
                    future_open_contracts = %(future_open_contracts)s,
                    spot_open_price = %(spot_open_price)s,
                    future_open_price = %(future_open_price)s,
                    spot_open_amount = %(spot_open_amount)s,
                    spot_close_price = %(spot_close_price)s,
                    future_close_price = %(future_close_price)s,
                    spot_close_amount = %(spot_close_amount)s,
                    future_close_amount = %(future_close_amount)s,
                    close_spread_bps = %(close_spread_bps)s,
                    close_funding_rate_24h = %(close_funding_rate_24h)s,
                    exchange_risk_status = CASE
                        WHEN exchange_risk_status = 'desynced' THEN 'resolved'
                        ELSE exchange_risk_status
                    END,
                    exchange_risk_type = NULL
                WHERE id = %(position_id)s
                  AND (
                      status = 'holding'
                      OR (status = 'closed' AND exchange_risk_type = %(pending_type)s)
                  )
            """
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, {
                    'closed_at': now,
                    'reason': reason,
                    'position_id': position_id,
                    'pending_type': POST_CLOSE_SPOT_DUST_PENDING,
                    'close_funding_rate_24h': close_funding_rate_24h,
                    **close_values,
                })
                updated = int(getattr(cursor, 'rowcount', 0) or 0)
                if updated and pnl_values:
                    update_closed_position_pnl(
                        cursor,
                        position_id,
                        pnl_values,
                        self._position_columns(),
                    )

    def _close_execution_values(self, orders: List[Dict], base_asset: str) -> Dict:
        def _sum(order_side: str, market_type: str, field: str) -> float:
            return sum(
                _float(order.get(field))
                for order in orders
                if str(order.get('order_side') or '').lower() == order_side
                and str(order.get('market_type') or '').lower() == market_type
                and str(order.get('status') or '').lower() == 'executed'
            )

        spot_open_qty = _sum('open', 'spot', 'exec_qty')
        spot_open_amount = _sum('open', 'spot', 'exec_amount')
        future_open_qty = _sum('open', 'future', 'exec_qty')
        future_open_amount = _sum('open', 'future', 'exec_amount')
        spot_qty = _sum('close', 'spot', 'exec_qty')
        spot_amount = _sum('close', 'spot', 'exec_amount')
        future_qty = _sum('close', 'future', 'exec_qty')
        future_amount = _sum('close', 'future', 'exec_amount')
        spot_open_price = spot_open_amount / spot_open_qty if spot_open_qty > 0 else None
        future_open_price = future_open_amount / future_open_qty if future_open_qty > 0 else None
        multiplier = self._quanto_multiplier(base_asset)
        future_open_contracts = (
            future_open_qty / multiplier
            if future_open_qty > 0 and multiplier > 0
            else 0.0
        )
        spot_price = spot_amount / spot_qty if spot_qty > 0 else None
        future_price = future_amount / future_qty if future_qty > 0 else None
        close_spread = (
            calc_vwap_basis_bps(spot_price, future_price)
            if spot_price and future_price
            else None
        )
        return {
            'spot_open_qty': spot_open_qty,
            'future_open_qty': future_open_qty,
            'future_open_contracts': future_open_contracts,
            'spot_open_price': spot_open_price,
            'future_open_price': future_open_price,
            'spot_open_amount': spot_open_amount,
            'spot_close_price': spot_price,
            'future_close_price': future_price,
            'spot_close_amount': spot_amount or None,
            'future_close_amount': future_amount or None,
            'close_spread_bps': round(close_spread, 4) if close_spread is not None else None,
        }

    def _record_allocated_dust_orders(
        self,
        positions: List[Dict],
        *,
        market_type: str,
        total_qty: float,
        exec_price: float,
        exchange_order_id: Optional[str],
        liquidity_role: str,
        fee_amount: float,
        fee_amount_usdt: float,
        fee_asset: str,
        reason: str,
    ) -> None:
        if total_qty <= 0 or exec_price <= 0:
            return
        remaining = total_qty
        order_uuid = str(uuid.uuid4())
        key = '_spot_remaining_qty' if market_type == 'spot' else '_future_remaining_qty'
        eligible = [pos for pos in positions if _float(pos.get(key)) > 0]
        expected_total = sum(_float(pos.get(key)) for pos in eligible)
        if abs(expected_total - total_qty) > 1e-8:
            raise ValueError(
                f'dust_allocation_qty_mismatch:{market_type}:{expected_total:g}!={total_qty:g}'
            )
        allocated_orders = []
        for pos in eligible:
            expected_qty = _float(pos.get(key))
            qty = min(expected_qty, remaining)
            if qty <= 0:
                continue
            ratio = qty / total_qty
            amount = qty * exec_price
            order = {
                'order_uuid': order_uuid,
                'position_id': int(pos['id']),
                'base_asset': str(pos.get('base_asset') or '').upper(),
                'spot_symbol': pos.get('spot_symbol'),
                'future_contract': pos.get('future_contract'),
                'order_side': 'close',
                'market_type': market_type,
                'trade_direction': 'sell' if market_type == 'spot' else 'buy',
                'leverage': 1.0 if market_type == 'spot' else 0.0,
                'status': 'executed',
                'channel': 'Live',
                'reject_reason': reason,
                'target_qty': qty,
                'target_amount': amount,
                'exec_price': exec_price,
                'exec_qty': qty,
                'exec_amount': amount,
                'coverage_ratio': 0.0,
                'liquidity_role': liquidity_role,
                'fee_rate': None,
                'fee_amount': fee_amount * ratio,
                'fee_amount_usdt': fee_amount_usdt * ratio,
                'fee_asset': fee_asset,
                'exchange_order_id': exchange_order_id,
                'executed_at': datetime.now(),
            }
            allocated_orders.append(order)
            remaining = max(0.0, remaining - qty)

        with db_manager.get_cursor() as cursor:
            for order in allocated_orders:
                self._insert_allocated_close_order(order, cursor=cursor)

    @staticmethod
    def _insert_allocated_close_order(order: Dict, cursor=None) -> None:
        sql = """
            INSERT INTO mi_trade_order (
                order_uuid, position_id, base_asset, spot_symbol, future_contract,
                order_side, market_type, trade_direction, leverage, status, channel,
                reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_vwap_basis_bps, risk_relief_bps,
                open_marginal_basis_bps, funding_rate_24h,
                liquidity_role, fee_rate, fee_amount, fee_amount_usdt, fee_asset,
                exchange_order_id, executed_at
            ) VALUES (
                %(order_uuid)s, %(position_id)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                %(order_side)s, %(market_type)s, %(trade_direction)s, %(leverage)s, %(status)s, %(channel)s,
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                NULL, NULL, NULL, NULL, NULL,
                %(liquidity_role)s, %(fee_rate)s, %(fee_amount)s, %(fee_amount_usdt)s, %(fee_asset)s,
                %(exchange_order_id)s, %(executed_at)s
            )
        """
        if cursor is not None:
            cursor.execute(sql, order)
            return
        with db_manager.get_cursor() as owned_cursor:
            owned_cursor.execute(sql, order)

    @staticmethod
    def _zero_local_future_dust(positions: List[Dict], reason: str) -> None:
        ids = [int(pos['id']) for pos in positions if _float(pos.get('_future_remaining_qty')) > 0]
        if not ids:
            return
        placeholders = ','.join(['%s'] * len(ids))
        sql = f"""
            UPDATE mi_trade_position
            SET future_open_qty = 0,
                future_open_contracts = 0,
                close_reason = CONCAT(COALESCE(close_reason, ''), '|', %s)
            WHERE id IN ({placeholders})
              AND status = 'holding'
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (f'{reason}|Gate小额空头已归零', *ids))

    def _load_post_close_spot_dust_positions(self, base_asset: str) -> List[Dict]:
        return [
            pos for pos in self._load_holding_positions_with_execution_remainders(base_asset)
            if _float(pos.get('_future_remaining_qty')) <= 1e-8
            and self._is_low_notional_residual_position(pos)
        ]

    @staticmethod
    def _is_low_notional_residual_position(pos: Dict) -> bool:
        reason = str(pos.get('close_reason') or '')
        return (
            '部分平仓保留剩余' in reason
            or '低名义残仓跳过平仓' in reason
        )

    @staticmethod
    def _load_holding_positions_with_execution_remainders(
        base_asset: Optional[str] = None,
    ) -> List[Dict]:
        asset_clause = "AND UPPER(p.base_asset) = %s" if base_asset else ""
        sql = f"""
            SELECT p.*,
                   COALESCE(SUM(CASE WHEN o.status = 'executed' AND o.order_side = 'open'
                                      AND o.market_type = 'spot' THEN ABS(o.exec_qty) ELSE 0 END), 0)
                       AS order_spot_open_qty,
                   COALESCE(SUM(CASE WHEN o.status = 'executed' AND o.order_side = 'close'
                                      AND o.market_type = 'spot' THEN ABS(o.exec_qty) ELSE 0 END), 0)
                       AS order_spot_close_qty,
                   COALESCE(SUM(CASE WHEN o.status = 'executed' AND o.order_side = 'open'
                                      AND o.market_type = 'future' THEN ABS(o.exec_qty) ELSE 0 END), 0)
                       AS order_future_open_qty,
                   COALESCE(SUM(CASE WHEN o.status = 'executed' AND o.order_side = 'close'
                                      AND o.market_type = 'future' THEN ABS(o.exec_qty) ELSE 0 END), 0)
                       AS order_future_close_qty,
                   COALESCE(SUM(CASE WHEN o.status = 'executed' AND o.order_side = 'close'
                                      AND o.reject_reason LIKE '%%小额资产转BNB%%' THEN 1 ELSE 0 END), 0)
                       AS dust_cleanup_order_count
            FROM mi_trade_position p
            LEFT JOIN mi_trade_order o ON o.position_id = p.id
            WHERE (
                    p.status = 'holding'
                    OR (
                        p.status = 'closed'
                        AND p.exchange_risk_type = %s
                    )
                  )
              {asset_clause}
            GROUP BY p.id
            ORDER BY UPPER(p.base_asset), p.opened_at, p.id
        """
        params = (
            (POST_CLOSE_SPOT_DUST_PENDING, base_asset)
            if base_asset
            else (POST_CLOSE_SPOT_DUST_PENDING,)
        )
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
            rows = list(cursor.fetchall())
        for row in rows:
            row['_spot_remaining_qty'] = max(
                0.0,
                _float(row.get('order_spot_open_qty')) - _float(row.get('order_spot_close_qty')),
            )
            row['_future_remaining_qty'] = max(
                0.0,
                _float(row.get('order_future_open_qty')) - _float(row.get('order_future_close_qty')),
            )
        return rows

    @staticmethod
    def _dust_conversion_cooldown_remaining_sec() -> float:
        """Binance accepts at most one dust conversion request per account each hour."""
        sql = """
            SELECT MAX(closed_at) AS last_converted_at
            FROM mi_trade_position
            WHERE status = 'closed'
              AND closed_at >= DATE_SUB(NOW(), INTERVAL 1 HOUR)
              AND close_reason LIKE '%%Binance小额资产转BNB%%'
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone() or {}
        last_converted_at = row.get('last_converted_at')
        if not last_converted_at:
            return 0.0
        elapsed = (datetime.now() - last_converted_at).total_seconds()
        return max(0.0, 3600.0 - elapsed)

    @_guard_asset_reduction('reconciliation_gate_extra')
    def remediate_gate_extra_position(
        self,
        base_asset: str,
        extra_contracts: float,
        risk: Dict,
    ) -> Dict:
        """Gate 比本地多出空头时，直接 reduce-only 买回多出的合约腿。"""
        base_asset = str(base_asset or '').upper()
        if not self.cfg.enabled:
            return {'attempted': False, 'reason': 'disabled'}
        if not self.cfg.close_extra_gate_position:
            return {'attempted': False, 'reason': 'close_extra_gate_position_disabled'}
        if extra_contracts <= 0:
            return {'attempted': False, 'reason': 'extra_contracts<=0'}

        exchange_size = _float(risk.get('exchange_size'))
        if exchange_size >= 0:
            return {
                'attempted': False,
                'reason': 'extra_gate_position_not_confirmed_short',
                'exchange_size': exchange_size,
            }

        quanto_multiplier = self._quanto_multiplier(base_asset)
        if quanto_multiplier <= 0:
            return {
                'attempted': False,
                'reason': 'missing_contract_multiplier',
                'base_asset': base_asset,
            }
        target_qty = float(extra_contracts) * quanto_multiplier
        if target_qty <= 0:
            return {'attempted': False, 'reason': 'target_qty<=0'}

        order_uuid = str(uuid.uuid4())
        order = {
            'order_uuid': order_uuid,
            'base_asset': base_asset,
            'spot_symbol': None,
            'future_contract': risk.get('contract') or f'{base_asset}_USDT',
            'order_side': 'close',
            'market_type': 'future',
            'trade_direction': 'buy',
            'status': 'pending',
            'target_qty': target_qty,
            'target_contracts': extra_contracts,
            'target_amount': target_qty * _float(risk.get('mark_price') or risk.get('future_close_price')),
        }
        result = self.executor.place_gate_futures_order(order)
        success = bool(result.get('success'))
        if success:
            logger.warning(
                "Gate 多余空头自动 reduce-only 处置完成 | %s | contracts=%s | qty=%s | px=%s",
                base_asset, extra_contracts, result.get('exec_qty'), result.get('exec_price'),
            )
        else:
            logger.error(
                "Gate 多余空头自动 reduce-only 处置失败 | %s | contracts=%s | reason=%s",
                base_asset, extra_contracts, result.get('reason'),
            )
        return {
            'attempted': True,
            'exchange_order_submitted': True,
            'success': success,
            'action': 'close_extra_gate_future',
            'base_asset': base_asset,
            'order_uuid': order_uuid,
            'future_contract': order['future_contract'],
            'extra_contracts': extra_contracts,
            'target_qty': target_qty,
            'future_result': result,
            'reason': result.get('reason') if not success else None,
        }

    @_guard_asset_reduction('reconciliation_binance_extra')
    def remediate_binance_spot_desync(
        self,
        base_asset: str,
        local_qty: float,
        exchange_qty: float,
        risk: Dict,
    ) -> Dict:
        """Binance 现货多于本地 holding 时，仅卖出交易所侧多余数量。"""
        base_asset = str(base_asset or '').upper()
        if not self.cfg.enabled:
            return {'attempted': False, 'reason': 'disabled'}
        if not self.cfg.remediate_binance_spot_position:
            return {'attempted': False, 'reason': 'remediate_binance_spot_position_disabled'}

        diff = float(exchange_qty or 0) - float(local_qty or 0)
        qty_tolerance = self._spot_qty_tolerance(base_asset)
        if abs(diff) < qty_tolerance:
            return {'attempted': False, 'reason': 'binance_spot_diff<=tolerance', 'diff_qty': diff}

        if diff < 0:
            return {
                'attempted': False,
                'success': False,
                'action': 'leave_missing_binance_spot_unfilled',
                'base_asset': base_asset,
                'local_qty': local_qty,
                'exchange_qty': exchange_qty,
                'diff_qty': diff,
                'reason': 'reduce_only_policy_does_not_buy_missing_spot',
            }

        trade_direction = 'sell'
        target_qty = abs(diff)
        available_qty = self._load_binance_available_qty(base_asset)
        if available_qty + 1e-9 < target_qty:
            return {
                'attempted': True,
                'success': False,
                'action': 'sell_extra_binance_spot',
                'base_asset': base_asset,
                'target_qty': target_qty,
                'reason': 'spot_available_qty_insufficient',
                'available_qty': available_qty,
                'retry_needed': True,
            }

        order_uuid = str(uuid.uuid4())
        price_hint = self._estimate_binance_spot_price(base_asset, risk)
        action = 'sell_extra_binance_spot'
        reason = (
            f"对账兜底自动处置|Binance现货多余|"
            f"asset={base_asset}|local={local_qty:g}|exchange={exchange_qty:g}|diff={diff:g}|"
            f"关联风险={risk.get('type', 'unknown')}"
        )
        explained_positions = []
        if (
            str(risk.get('type') or '') == 'binance_spot_excess'
            and not risk.get('_skip_local_allocation')
        ):
            explained_positions = self._load_spot_excess_positions_to_remediate(
                base_asset,
                target_qty,
            )
        explained_qty = sum(
            _float(pos.get('_spot_excess_qty')) for pos in explained_positions
        )
        allocation_tolerance = self._spot_qty_tolerance(base_asset)
        allocate_to_positions = (
            bool(explained_positions)
            and explained_qty + allocation_tolerance > target_qty
        )
        min_notional = max(
            0.0,
            _float(
                ((getattr(self.executor, 'spot_meta', {}) or {}).get(base_asset) or {}).get(
                    'min_notional'
                )
            ),
        )
        if min_notional > 0 and target_qty * price_hint + 1e-9 < min_notional:
            return self._remediate_low_notional_binance_excess(
                base_asset=base_asset,
                target_qty=target_qty,
                available_qty=available_qty,
                price_hint=price_hint,
                min_notional=min_notional,
                risk=risk,
                positions=explained_positions,
                explained_qty=explained_qty,
            )
        order = {
            'order_uuid': order_uuid,
            'position_id': None,
            'base_asset': base_asset,
            'spot_symbol': f'{base_asset}USDT',
            'future_contract': risk.get('contract') or f'{base_asset}_USDT',
            'order_side': 'close',
            'market_type': 'spot',
            'trade_direction': trade_direction,
            'leverage': 1.0,
            'target_qty': target_qty,
            'target_amount': target_qty * price_hint,
        }
        result = dict(self._place_binance_spot_reduction(order) or {})
        reported_exec_qty = max(0.0, _float(result.get('exec_qty')))
        effective_exec_qty = min(reported_exec_qty, target_qty)
        if reported_exec_qty > 0 and effective_exec_qty < reported_exec_qty:
            ratio = effective_exec_qty / reported_exec_qty
            for key in ('exec_amount', 'fee_amount', 'fee_amount_usdt'):
                if result.get(key) is not None:
                    result[key] = max(0.0, _float(result.get(key))) * ratio
            result['exec_qty'] = effective_exec_qty
        if effective_exec_qty > 0 and _float(result.get('exec_amount')) <= 0:
            result['exec_amount'] = effective_exec_qty * max(
                0.0,
                _float(result.get('exec_price')),
            )
        remaining_qty = max(0.0, target_qty - effective_exec_qty)
        fill_exceeds_target = reported_exec_qty > target_qty + allocation_tolerance
        exchange_complete = remaining_qty < allocation_tolerance
        exchange_success = (
            exchange_complete
            and not fill_exceeds_target
            and (bool(result.get('success')) or effective_exec_qty > 0)
        )
        if fill_exceeds_target:
            result['reported_exec_qty'] = reported_exec_qty
            result['reason'] = (
                f"spot_fill_exceeds_target({reported_exec_qty:g}>{target_qty:g})"
            )
        elif not exchange_complete:
            result['reason'] = result.get('reason') or (
                f"spot_reduction_incomplete({effective_exec_qty:g}/{target_qty:g})"
            )
        success = exchange_success
        has_fill = effective_exec_qty > 0
        allocation = None
        allocation_error = None
        if has_fill and allocate_to_positions:
            try:
                allocation = self._allocate_spot_reduction_to_positions(
                    order=order,
                    result=result,
                    positions=explained_positions,
                    risk=risk,
                    reason=reason,
                    now=datetime.now(),
                )
            except Exception as exc:
                allocation_error = f"local_spot_allocation_failed:{exc}"
                success = False
                logger.exception(
                    "Binance 现货已减仓但本地持仓分摊失败，保留资产级 desynced | %s | qty=%s",
                    base_asset,
                    result.get('exec_qty'),
                )
                try:
                    self._insert_spot_order(
                        order,
                        result,
                        f"{reason}|{allocation_error}",
                        True,
                        datetime.now(),
                    )
                except Exception as audit_exc:
                    allocation_error += f";aggregate_spot_audit_failed:{audit_exc}"
                    logger.exception(
                        "Binance 聚合减仓本地分摊和资产级审计均失败 | %s",
                        base_asset,
                    )
            else:
                unallocated_qty = max(0.0, _float(allocation.get('unallocated_qty')))
                if unallocated_qty > 1e-8:
                    allocated_qty = max(0.0, _float(allocation.get('allocated_qty')))
                    ratio = unallocated_qty / max(effective_exec_qty, 1e-8)
                    unallocated_order = dict(order)
                    unallocated_order.update({
                        'position_id': None,
                        'target_qty': unallocated_qty,
                        'target_amount': max(
                            0.0,
                            _float(result.get('exec_amount')),
                        ) * ratio,
                    })
                    unallocated_result = dict(result)
                    unallocated_result.update({
                        'exec_qty': unallocated_qty,
                        'exec_amount': max(
                            0.0,
                            _float(result.get('exec_amount')),
                        ) * ratio,
                        'fee_amount': self._proportional_optional_amount(
                            result.get('fee_amount'), ratio,
                        ),
                        'fee_amount_usdt': self._proportional_optional_amount(
                            result.get('fee_amount_usdt'), ratio,
                        ),
                    })
                    try:
                        self._insert_spot_order(
                            unallocated_order,
                            unallocated_result,
                            f"{reason}|本地未分配成交={unallocated_qty:g}",
                            True,
                            datetime.now(),
                        )
                    except Exception as exc:
                        allocation_error = f"unallocated_spot_audit_failed:{exc}"
                        success = False
                        logger.exception(
                            "Binance 聚合减仓未分配成交审计失败 | %s | qty=%s",
                            base_asset,
                            unallocated_qty,
                        )
                    else:
                        logger.warning(
                            "Binance 聚合减仓含本地未分配成交 | %s | allocated=%s | unallocated=%s",
                            base_asset,
                            allocated_qty,
                            unallocated_qty,
                        )
                if exchange_success and not allocation_error:
                    try:
                        self._resolve_balanced_spot_excess_positions(base_asset)
                    except Exception as exc:
                        allocation_error = f"local_spot_risk_cleanup_failed:{exc}"
                        success = False
                        logger.warning(
                            "Binance 现货及本地持仓分摊已完成，但平衡持仓风险标记清理失败 | %s | %s",
                            base_asset,
                            exc,
                            exc_info=True,
                        )
        else:
            try:
                self._insert_spot_order(
                    order,
                    result,
                    reason,
                    exchange_success or has_fill,
                    datetime.now(),
                )
            except Exception as exc:
                allocation_error = f"spot_order_persistence_failed:{exc}"
                success = False
                logger.exception(
                    "Binance 现货对账处置订单落库失败 | %s | qty=%s",
                    base_asset,
                    effective_exec_qty,
                )
        if (
            exchange_success
            and not allocation_error
            and str(risk.get('type') or '') == 'binance_spot_excess'
            and not allocate_to_positions
            and not risk.get('_skip_local_allocation')
        ):
            try:
                self._resolve_balanced_spot_excess_positions(base_asset)
            except Exception as exc:
                allocation_error = f"local_spot_risk_cleanup_failed:{exc}"
                success = False
                logger.warning(
                    "Binance 现货已减仓，但平衡持仓风险标记清理失败 | %s | %s",
                    base_asset,
                    exc,
                    exc_info=True,
                )
        if success:
            logger.warning(
                "Binance 现货对账自动处置完成 | %s | action=%s | qty=%s | px=%s",
                base_asset, action, result.get('exec_qty'), result.get('exec_price'),
            )
        else:
            logger.error(
                "Binance 现货对账自动处置失败 | %s | action=%s | qty=%s | reason=%s",
                base_asset, action, target_qty, result.get('reason'),
            )
        return {
            'attempted': True,
            'exchange_order_submitted': True,
            'success': success,
            'action': action,
            'base_asset': base_asset,
            'order_uuid': order_uuid,
            'target_qty': target_qty,
            'local_qty': local_qty,
            'exchange_qty': exchange_qty,
            'spot_result': result,
            'exchange_success': exchange_success,
            'allocated_position_count': (
                allocation.get('allocated_position_count') if allocation else 0
            ),
            'allocated_qty': allocation.get('allocated_qty') if allocation else 0.0,
            'remaining_qty': remaining_qty,
            'retry_needed': (
                not exchange_complete
                or fill_exceeds_target
                or bool(allocation_error)
            ),
            'reason': allocation_error or (result.get('reason') if not success else None),
        }

    def _remediate_low_notional_binance_excess(
        self,
        *,
        base_asset: str,
        target_qty: float,
        available_qty: float,
        price_hint: float,
        min_notional: float,
        risk: Dict,
        positions: List[Dict],
        explained_qty: float,
    ) -> Dict:
        """Clear a tiny spot excess by trimming a small matched hedge with Gate FOK first."""
        now = datetime.now()
        retry_after = self._low_notional_retry_after.get(base_asset)
        if retry_after and now < retry_after:
            return {
                'attempted': False,
                'success': False,
                'action': 'wait_low_notional_paired_trim',
                'base_asset': base_asset,
                'target_qty': target_qty,
                'reason': 'low_notional_retry_cooldown',
                'retry_after': retry_after,
            }

        def defer(reason: str, *, attempted: bool = False, **extra) -> Dict:
            cooldown = max(0.0, float(self.cfg.low_notional_retry_cooldown_sec or 0.0))
            self._low_notional_retry_after[base_asset] = now + timedelta(seconds=cooldown)
            logger.warning(
                "Binance小额净敞口等待安全合并减仓 | %s | qty=%s | reason=%s",
                base_asset,
                target_qty,
                reason,
            )
            return {
                'attempted': attempted,
                'success': False,
                'action': 'wait_low_notional_paired_trim',
                'base_asset': base_asset,
                'target_qty': target_qty,
                'reason': reason,
                'retry_needed': False,
                **extra,
            }

        if str(risk.get('type') or '') != 'binance_spot_excess':
            return defer('low_notional_excess_not_pairable')
        tolerance = self._spot_qty_tolerance(base_asset)
        if not positions or explained_qty + tolerance <= target_qty:
            return defer('low_notional_excess_not_fully_explained')

        multiplier = self._quanto_multiplier(base_asset)
        if multiplier <= 0:
            return defer('missing_contract_multiplier')
        available_contracts = 0
        for pos in positions:
            spot_qty = max(0.0, _float(pos.get('spot_open_qty')))
            future_qty = max(0.0, _float(pos.get('future_open_qty')))
            selected_excess = max(0.0, _float(pos.get('_spot_excess_qty')))
            actual_excess = max(0.0, spot_qty - future_qty)
            if abs(selected_excess - actual_excess) >= tolerance:
                return defer(
                    'position_spot_excess_not_fully_selected',
                    position_id=pos.get('id'),
                    selected_excess=selected_excess,
                    actual_excess=actual_excess,
                )
            contracts = abs(_float(pos.get('future_open_contracts')))
            rounded = int(round(contracts))
            if rounded <= 0 or abs(contracts - rounded) > 1e-8:
                return defer(
                    'invalid_position_contract_count',
                    position_id=pos.get('id'),
                    future_open_contracts=contracts,
                )
            expected_future_qty = rounded * multiplier
            if abs(future_qty - expected_future_qty) >= tolerance:
                return defer(
                    'position_contract_multiplier_mismatch',
                    position_id=pos.get('id'),
                    future_open_qty=future_qty,
                    expected_future_qty=expected_future_qty,
                    quanto_multiplier=multiplier,
                )
            available_contracts += rounded
        mark_price = _float(risk.get('mark_price') or risk.get('future_close_price'))
        if price_hint <= 0 or mark_price <= 0:
            return defer('missing_low_notional_repair_price')

        spot_meta = ((getattr(self.executor, 'spot_meta', {}) or {}).get(base_asset) or {})
        step_size = max(0.0, _float(spot_meta.get('step_size')))
        buffered_notional = min_notional * max(
            1.0,
            float(self.cfg.low_notional_buffer_ratio or 1.0),
        )
        minimum_sale_qty = buffered_notional / price_hint
        if step_size > 0:
            minimum_sale_qty = math.ceil(
                minimum_sale_qty / step_size - 1e-12
            ) * step_size

        required_pair_qty = max(0.0, minimum_sale_qty - target_qty)
        required_contracts = max(1, int(math.ceil(required_pair_qty / multiplier - 1e-12)))
        while (
            target_qty + required_contracts * multiplier
        ) * price_hint + 1e-9 < buffered_notional:
            required_contracts += 1
        if required_contracts > available_contracts:
            return defer(
                'insufficient_matched_gate_position_for_low_notional_repair',
                required_contracts=required_contracts,
                available_contracts=available_contracts,
            )

        paired_qty = required_contracts * multiplier
        combined_spot_qty = target_qty + paired_qty
        if available_qty + 1e-9 < combined_spot_qty:
            return defer(
                'spot_available_qty_insufficient_for_low_notional_repair',
                required_spot_qty=combined_spot_qty,
                available_qty=available_qty,
            )

        order_uuid = str(uuid.uuid4())
        protective_price = mark_price * (
            1.0 + max(0.0, float(self.cfg.low_notional_fok_slippage_bps or 0.0)) / 10000.0
        )
        reason = (
            f"对账兜底小额净敞口合并减仓|asset={base_asset}|"
            f"excess={target_qty:g}|paired={paired_qty:g}|spot_total={combined_spot_qty:g}|"
            f"min_notional={min_notional:g}"
        )
        future_order = {
            'order_uuid': order_uuid,
            'position_id': None,
            'base_asset': base_asset,
            'spot_symbol': None,
            'future_contract': risk.get('contract') or f'{base_asset}_USDT',
            'order_side': 'close',
            'market_type': 'future',
            'trade_direction': 'buy',
            'target_qty': paired_qty,
            'target_contracts': required_contracts,
            'target_amount': paired_qty * protective_price,
            'protective_price': protective_price,
            'time_in_force': 'fok',
        }
        future_result = dict(self.executor.place_gate_futures_order(future_order) or {})
        if not future_result.get('success'):
            return defer(
                future_result.get('reason') or 'low_notional_gate_fok_failed',
                attempted=True,
                exchange_order_submitted=True,
                future_result=future_result,
            )
        future_exec_qty = max(0.0, _float(future_result.get('exec_qty')))
        gate_exec_tolerance = max(1e-8, multiplier * 1e-8)
        if abs(future_exec_qty - paired_qty) > gate_exec_tolerance:
            return defer(
                f'low_notional_gate_fok_qty_mismatch:{future_exec_qty:g}!={paired_qty:g}',
                attempted=True,
                exchange_order_submitted=True,
                future_result=future_result,
                retry_needed=True,
            )

        spot_order = {
            'order_uuid': order_uuid,
            'position_id': None,
            'base_asset': base_asset,
            'spot_symbol': f'{base_asset}USDT',
            'future_contract': risk.get('contract') or f'{base_asset}_USDT',
            'order_side': 'close',
            'market_type': 'spot',
            'trade_direction': 'sell',
            'target_qty': combined_spot_qty,
            'target_amount': combined_spot_qty * price_hint,
        }
        spot_result = dict(self._place_binance_spot_reduction(spot_order) or {})
        spot_exec_qty = max(0.0, _float(spot_result.get('exec_qty')))
        spot_exec_tolerance = max(1e-8, step_size * 1e-8)
        if (
            not spot_result.get('success')
            or abs(spot_exec_qty - combined_spot_qty) > spot_exec_tolerance
        ):
            return defer(
                spot_result.get('reason') or (
                    f'low_notional_spot_qty_mismatch:{spot_exec_qty:g}!={combined_spot_qty:g}'
                ),
                attempted=True,
                exchange_order_submitted=True,
                future_result=future_result,
                spot_result=spot_result,
                retry_needed=True,
            )

        try:
            allocation = self._persist_low_notional_paired_trim(
                positions=positions,
                excess_qty=target_qty,
                paired_contracts=required_contracts,
                multiplier=multiplier,
                future_order=future_order,
                future_result=future_result,
                spot_order=spot_order,
                spot_result=spot_result,
                reason=reason,
                now=now,
            )
        except Exception as exc:
            logger.exception("小额净敞口已成交但本地分配失败 | %s", base_asset)
            return defer(
                f'low_notional_local_allocation_failed:{exc}',
                attempted=True,
                exchange_order_submitted=True,
                future_result=future_result,
                spot_result=spot_result,
                retry_needed=True,
            )

        self._low_notional_retry_after.pop(base_asset, None)
        logger.warning(
            "Binance小额净敞口合并减仓完成 | %s | excess=%s | paired=%s | spot=%s",
            base_asset,
            target_qty,
            paired_qty,
            combined_spot_qty,
        )
        return {
            'attempted': True,
            'exchange_order_submitted': True,
            'success': True,
            'action': 'paired_trim_low_notional_binance_excess',
            'base_asset': base_asset,
            'target_qty': target_qty,
            'paired_qty': paired_qty,
            'spot_exec_qty': spot_exec_qty,
            'future_result': future_result,
            'spot_result': spot_result,
            'allocation': allocation,
            'retry_needed': False,
        }

    def _persist_low_notional_paired_trim(
        self,
        *,
        positions: List[Dict],
        excess_qty: float,
        paired_contracts: int,
        multiplier: float,
        future_order: Dict,
        future_result: Dict,
        spot_order: Dict,
        spot_result: Dict,
        reason: str,
        now: datetime,
    ) -> Dict:
        """Allocate one paired repair to the exact broken rows in a single DB transaction."""
        remaining_excess = excess_qty
        remaining_contracts = int(paired_contracts)
        allocations = []
        tolerance = self._spot_qty_tolerance(str(spot_order.get('base_asset') or ''))

        for pos in positions:
            excess = min(max(0.0, _float(pos.get('_spot_excess_qty'))), remaining_excess)
            available_contracts = int(round(abs(_float(pos.get('future_open_contracts')))))
            contract_qty = min(available_contracts, remaining_contracts)
            paired_qty = contract_qty * multiplier
            spot_qty = excess + paired_qty
            if spot_qty <= 0 and contract_qty <= 0:
                continue
            allocations.append({
                'pos': pos,
                'excess_qty': excess,
                'paired_contracts': contract_qty,
                'paired_qty': paired_qty,
                'spot_qty': spot_qty,
            })
            remaining_excess = max(0.0, remaining_excess - excess)
            remaining_contracts -= contract_qty
        if remaining_excess >= tolerance or remaining_contracts != 0:
            raise ValueError(
                f'paired_trim_allocation_shortfall:excess={remaining_excess:g},'
                f'contracts={remaining_contracts}'
            )

        total_spot_qty = sum(item['spot_qty'] for item in allocations)
        total_future_qty = sum(item['paired_qty'] for item in allocations)
        closed_positions = []
        with db_manager.get_connection() as connection:
            cursor = connection.cursor()
            try:
                for item in allocations:
                    pos = item['pos']
                    spot_ratio = item['spot_qty'] / total_spot_qty
                    future_ratio = item['paired_qty'] / total_future_qty
                    allocated_spot_result = self._proportional_execution_result(
                        spot_result, spot_ratio, item['spot_qty']
                    )
                    allocated_future_result = self._proportional_execution_result(
                        future_result, future_ratio, item['paired_qty']
                    )
                    self._insert_allocated_close_order(
                        self._paired_trim_order_payload(
                            pos,
                            spot_order,
                            allocated_spot_result,
                            item['spot_qty'],
                            reason,
                            now,
                        ),
                        cursor=cursor,
                    )
                    if item['paired_qty'] > 0:
                        self._insert_allocated_close_order(
                            self._paired_trim_order_payload(
                                pos,
                                future_order,
                                allocated_future_result,
                                item['paired_qty'],
                                reason,
                                now,
                            ),
                            cursor=cursor,
                        )

                    spot_remaining = max(0.0, _float(pos.get('spot_open_qty')) - item['spot_qty'])
                    future_remaining = max(0.0, _float(pos.get('future_open_qty')) - item['paired_qty'])
                    contracts_remaining = max(
                        0.0,
                        abs(_float(pos.get('future_open_contracts'))) - item['paired_contracts'],
                    )
                    balanced = abs(spot_remaining - future_remaining) < tolerance
                    if not balanced:
                        raise ValueError(
                            f'paired_trim_unbalanced_position:{pos.get("id")}: '
                            f'{spot_remaining:g}!={future_remaining:g}'
                        )
                    closed = spot_remaining < tolerance and future_remaining < tolerance
                    cursor.execute(
                        """
                        UPDATE mi_trade_position
                        SET spot_open_qty = %(spot_qty)s,
                            spot_open_amount = %(spot_amount)s,
                            future_open_qty = %(future_qty)s,
                            future_open_contracts = %(future_contracts)s,
                            status = %(status)s,
                            closed_at = CASE WHEN %(closed)s = 1 THEN %(now)s ELSE closed_at END,
                            close_reason = CONCAT(COALESCE(close_reason, ''), '|', %(reason)s),
                            exchange_risk_status = 'resolved',
                            exchange_risk_type = NULL,
                            exchange_risk_at = %(now)s,
                            exchange_risk_detail = CONCAT(
                                COALESCE(exchange_risk_detail, ''),
                                '|小额净敞口合并减仓后已平衡'
                            )
                        WHERE id = %(position_id)s
                          AND status = 'holding'
                        """,
                        {
                            'spot_qty': 0.0 if closed else spot_remaining,
                            'spot_amount': 0.0 if closed else (
                                spot_remaining * _float(pos.get('spot_open_price'))
                            ),
                            'future_qty': 0.0 if closed else future_remaining,
                            'future_contracts': 0.0 if closed else contracts_remaining,
                            'status': 'closed' if closed else 'holding',
                            'closed': 1 if closed else 0,
                            'now': now,
                            'reason': reason,
                            'position_id': int(pos['id']),
                        },
                    )
                    if int(cursor.rowcount or 0) != 1:
                        raise ValueError(f'paired_trim_position_update_lost:{pos.get("id")}')
                    pos['spot_open_qty'] = 0.0 if closed else spot_remaining
                    pos['future_open_qty'] = 0.0 if closed else future_remaining
                    pos['future_open_contracts'] = 0.0 if closed else contracts_remaining
                    pos['exchange_risk_status'] = 'resolved'
                    pos['exchange_risk_type'] = None
                    if closed:
                        pos['status'] = 'closed'
                        closed_positions.append(pos)
            finally:
                cursor.close()

        for pos in closed_positions:
            self._refresh_closed_position_pnl(pos)
        return {
            'position_count': len(allocations),
            'closed_position_count': len(closed_positions),
            'spot_qty': total_spot_qty,
            'future_qty': total_future_qty,
        }

    @staticmethod
    def _proportional_execution_result(result: Dict, ratio: float, qty: float) -> Dict:
        allocated = dict(result)
        allocated['exec_qty'] = qty
        allocated['exec_amount'] = max(0.0, _float(result.get('exec_amount'))) * ratio
        for key in ('fee_amount', 'fee_amount_usdt'):
            if result.get(key) is not None:
                allocated[key] = _float(result.get(key)) * ratio
        return allocated

    def _paired_trim_order_payload(
        self,
        pos: Dict,
        order: Dict,
        result: Dict,
        qty: float,
        reason: str,
        now: datetime,
    ) -> Dict:
        market_type = str(order.get('market_type') or '')
        fields = self._execution_fields(
            'spot_order' if market_type == 'spot' else 'future_order',
            order,
            result,
            True,
        )
        return {
            'order_uuid': order.get('order_uuid'),
            'position_id': int(pos['id']),
            'base_asset': str(pos.get('base_asset') or '').upper(),
            'spot_symbol': pos.get('spot_symbol'),
            'future_contract': pos.get('future_contract'),
            'order_side': 'close',
            'market_type': market_type,
            'trade_direction': 'sell' if market_type == 'spot' else 'buy',
            'leverage': 1.0 if market_type == 'spot' else 0.0,
            'status': 'executed',
            'channel': 'Live',
            'reject_reason': reason,
            'target_qty': qty,
            'target_amount': max(0.0, _float(result.get('exec_amount'))),
            'exec_price': result.get('exec_price'),
            'exec_qty': qty,
            'exec_amount': result.get('exec_amount'),
            'coverage_ratio': result.get('coverage_ratio') or 0.0,
            'liquidity_role': fields.get('liquidity_role'),
            'fee_rate': fields.get('fee_rate'),
            'fee_amount': fields.get('fee_amount'),
            'fee_amount_usdt': fields.get('fee_amount_usdt'),
            'fee_asset': fields.get('fee_asset'),
            'exchange_order_id': fields.get('exchange_order_id'),
            'executed_at': now,
        }

    @_guard_asset_reduction('reconciliation_spot_only')
    def remediate_binance_spot_only_exposure(
        self,
        base_asset: str,
        spot_qty: float,
        risk: Dict,
    ) -> Dict:
        """Sell Binance spot that has no matching Gate short leg and retire local spot-only rows."""
        base_asset = str(base_asset or '').upper()
        spot_qty = float(spot_qty or 0)
        if not self.cfg.enabled:
            return {'attempted': False, 'reason': 'disabled'}
        if not self.cfg.remediate_binance_spot_position:
            return {'attempted': False, 'reason': 'remediate_binance_spot_position_disabled'}
        if spot_qty <= max(float(self.cfg.min_spot_qty or 0), 1e-8):
            return {'attempted': False, 'reason': 'spot_qty<=tolerance', 'spot_qty': spot_qty}

        positions = self._load_spot_only_positions_to_remediate(base_asset, spot_qty)
        if not positions:
            return self.remediate_binance_spot_desync(
                base_asset=base_asset,
                local_qty=0.0,
                exchange_qty=spot_qty,
                risk={**risk, '_skip_local_allocation': True},
            )

        self._mark_positions_exchange_risk(positions, risk)
        available_qty = self._load_binance_available_qty(base_asset)
        remaining = min(spot_qty, available_qty)
        if remaining <= max(float(self.cfg.min_spot_qty or 0), 1e-8):
            return {
                'attempted': True,
                'success': False,
                'action': 'sell_spot_only_binance_exposure',
                'base_asset': base_asset,
                'target_qty': spot_qty,
                'available_qty': available_qty,
                'reason': 'spot_available_qty_insufficient',
            }

        results = []
        for pos in positions:
            target_qty = min(_float(pos.get('spot_open_qty')), remaining)
            if target_qty <= max(float(self.cfg.min_spot_qty or 0), 1e-8):
                continue
            min_notional_reason = self._below_spot_min_notional(pos, target_qty)
            if min_notional_reason:
                self._append_risk_detail(pos.get('id'), f"自动处置跳过|{min_notional_reason}")
                results.append({
                    'attempted': True,
                    'success': False,
                    'position_id': pos.get('id'),
                    'reason': min_notional_reason,
                })
                continue
            result = self._sell_spot_and_close_position(pos, target_qty, risk)
            results.append(result)
            remaining -= _float(result.get('spot_exec_qty'))
            if not result.get('success'):
                break
            if remaining <= max(float(self.cfg.min_spot_qty or 0), 1e-8):
                break

        success_count = sum(1 for item in results if item.get('success'))
        failure_count = sum(1 for item in results if item.get('attempted') and not item.get('success'))
        return {
            'attempted': True,
            'success': failure_count == 0 and success_count > 0,
            'action': 'sell_spot_only_binance_exposure',
            'base_asset': base_asset,
            'target_qty': spot_qty,
            'available_qty': available_qty,
            'positions': len(positions),
            'success_count': success_count,
            'failure_count': failure_count,
            'results': results,
        }

    def _estimate_binance_spot_price(self, base_asset: str, risk: Dict) -> float:
        spot_price = _float(risk.get('spot_price'))
        if spot_price > 0:
            return spot_price
        getter = getattr(self.executor, '_get_binance_usdt_price', None)
        if callable(getter):
            try:
                price = _float(getter(base_asset))
                if price > 0:
                    return price
            except Exception:
                logger.debug("Binance spot price estimate failed | %s", base_asset, exc_info=True)
        for key in ('mark_price', 'future_close_price'):
            price = _float(risk.get(key))
            if price > 0:
                return price
        return 0.0

    def _below_spot_min_notional(self, pos: Dict, target_qty: float) -> Optional[str]:
        base_asset = str(pos.get('base_asset') or '').upper()
        meta = getattr(self.executor, 'spot_meta', {}) or {}
        min_notional = _float((meta.get(base_asset) or {}).get('min_notional'))
        if min_notional <= 0 or target_qty <= 0:
            return None
        price = _float(pos.get('spot_open_price'))
        if price <= 0:
            return None
        notional = target_qty * price
        if notional + 1e-9 >= min_notional:
            return None
        return (
            f"spot_notional_below_min|qty={target_qty:g}|price={price:g}|"
            f"notional={notional:.4f}<min_notional={min_notional:g}USDT"
        )

    def _load_positions_to_remediate(
        self,
        base_asset: str,
        missing_contracts: float,
        *,
        require_desynced: bool = True,
    ) -> List[Dict]:
        risk_clause = "AND p.exchange_risk_status = 'desynced'" if require_desynced else ""
        sql = """
            SELECT p.*,
                   MAX(CASE WHEN o.order_side = 'open' AND o.market_type = 'future' THEN o.leverage END)
                       AS future_open_leverage
            FROM mi_trade_position p
            LEFT JOIN mi_trade_order o
              ON o.position_id = p.id
             AND o.status = 'executed'
            WHERE p.status = 'holding'
              AND UPPER(p.base_asset) = %s
              {risk_clause}
            GROUP BY p.id
            ORDER BY p.opened_at ASC, p.id ASC
        """.format(risk_clause=risk_clause)
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (base_asset,))
            rows = cursor.fetchall()

        selected: List[Dict] = []
        remaining = float(missing_contracts)
        for row in rows:
            contracts = abs(_float(row.get('future_open_contracts')))
            if contracts <= 0:
                continue
            if contracts <= remaining + 1e-9:
                selected.append(row)
                remaining -= contracts
            if remaining <= 1e-9:
                break
        return selected

    def _load_spot_only_positions_to_remediate(self, base_asset: str, spot_qty: float) -> List[Dict]:
        sql = """
            SELECT p.*,
                   MAX(CASE WHEN o.order_side = 'open' AND o.market_type = 'future' THEN o.leverage END)
                       AS future_open_leverage
            FROM mi_trade_position p
            LEFT JOIN mi_trade_order o
              ON o.position_id = p.id
             AND o.status = 'executed'
            WHERE p.status = 'holding'
              AND UPPER(p.base_asset) = %s
              AND COALESCE(ABS(p.future_open_contracts), 0) <= 1e-9
              AND COALESCE(ABS(p.future_open_qty), 0) <= 1e-9
              AND COALESCE(p.spot_open_qty, 0) > 0
            GROUP BY p.id
            ORDER BY p.opened_at ASC, p.id ASC
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (base_asset,))
            rows = cursor.fetchall()

        selected: List[Dict] = []
        remaining = float(spot_qty or 0)
        for row in rows:
            qty = _float(row.get('spot_open_qty'))
            if qty <= 0:
                continue
            selected.append(row)
            remaining -= qty
            if remaining <= 1e-8:
                break
        return selected

    def _load_spot_excess_positions_to_remediate(
        self,
        base_asset: str,
        target_qty: float,
    ) -> List[Dict]:
        """Load local broken-leg rows that explain Binance spot exceeding Gate short."""
        sql = """
            SELECT p.*
            FROM mi_trade_position p
            WHERE p.status = 'holding'
              AND UPPER(p.base_asset) = %s
              AND p.exchange_risk_status = 'desynced'
              AND COALESCE(p.spot_open_qty, 0) > COALESCE(p.future_open_qty, 0)
            ORDER BY p.exchange_risk_at ASC, p.opened_at ASC, p.id ASC
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (base_asset,))
            rows = cursor.fetchall()

        selected: List[Dict] = []
        remaining = max(0.0, float(target_qty or 0))
        tolerance = self._spot_qty_tolerance(base_asset)
        for row in rows:
            excess = max(
                0.0,
                _float(row.get('spot_open_qty')) - _float(row.get('future_open_qty')),
            )
            if excess < tolerance:
                continue
            item = dict(row)
            item['_spot_excess_qty'] = min(excess, remaining)
            selected.append(item)
            remaining -= item['_spot_excess_qty']
            if remaining < tolerance:
                break
        return selected

    def _allocate_spot_reduction_to_positions(
        self,
        *,
        order: Dict,
        result: Dict,
        positions: List[Dict],
        risk: Dict,
        reason: str,
        now: datetime,
    ) -> Dict:
        """Allocate one Binance reduction fill back to the local broken-leg rows."""
        total_exec_qty = max(0.0, _float(result.get('exec_qty')))
        if total_exec_qty <= 0:
            return {'allocated_position_count': 0, 'allocated_qty': 0.0}

        total_exec_amount = max(0.0, _float(result.get('exec_amount')))
        if total_exec_amount <= 0:
            total_exec_amount = total_exec_qty * max(0.0, _float(result.get('exec_price')))
        remaining = total_exec_qty
        allocations = []
        closed_positions = []
        tolerance = self._spot_qty_tolerance(str(order.get('base_asset') or ''))

        with db_manager.get_connection() as connection:
            cursor = connection.cursor()
            try:
                for pos in positions:
                    expected = max(0.0, _float(pos.get('_spot_excess_qty')))
                    qty = min(expected, remaining)
                    if qty < tolerance:
                        continue
                    ratio = qty / total_exec_qty
                    allocated_result = dict(result)
                    allocated_result.update({
                        'success': True,
                        'exec_qty': qty,
                        'exec_amount': total_exec_amount * ratio,
                        'fee_amount': self._proportional_optional_amount(
                            result.get('fee_amount'), ratio,
                        ),
                        'fee_amount_usdt': self._proportional_optional_amount(
                            result.get('fee_amount_usdt'), ratio,
                        ),
                    })
                    allocated_order = dict(order)
                    allocated_order.update({
                        'position_id': int(pos['id']),
                        'target_qty': qty,
                        'target_amount': total_exec_amount * ratio,
                    })
                    self._insert_spot_order(
                        allocated_order,
                        allocated_result,
                        reason,
                        True,
                        now,
                        cursor=cursor,
                    )
                    state = self._apply_allocated_spot_reduction(
                        pos,
                        qty,
                        allocated_result,
                        risk,
                        reason,
                        now,
                        cursor=cursor,
                    )
                    allocations.append({
                        'position_id': int(pos['id']),
                        'qty': qty,
                        'state': state,
                    })
                    if state == 'closed':
                        closed_positions.append(pos)
                    remaining = max(0.0, remaining - qty)
                    if remaining < tolerance:
                        break

                if remaining >= tolerance:
                    raise ValueError(
                        f"spot_reduction_allocation_shortfall:{total_exec_qty:g}!="
                        f"{total_exec_qty - remaining:g}"
                    )
            finally:
                cursor.close()

        for pos in closed_positions:
            try:
                self._refresh_closed_position_pnl(pos)
            except Exception as exc:
                logger.warning(
                    "聚合现货处置后的收益刷新失败 | position_id=%s | %s",
                    pos.get('id'),
                    exc,
                    exc_info=True,
                )
        return {
            'allocated_position_count': len(allocations),
            'allocated_qty': total_exec_qty - remaining,
            'unallocated_qty': remaining,
            'allocations': allocations,
        }

    def _apply_allocated_spot_reduction(
        self,
        pos: Dict,
        allocated_qty: float,
        spot_result: Dict,
        risk: Dict,
        reason: str,
        now: datetime,
        cursor=None,
    ) -> str:
        base_asset = str(pos.get('base_asset') or '').upper()
        tolerance = self._spot_qty_tolerance(base_asset)
        spot_open_price = _float(pos.get('spot_open_price'))
        spot_remaining = max(0.0, _float(pos.get('spot_open_qty')) - allocated_qty)
        future_remaining = max(0.0, _float(pos.get('future_open_qty')))
        if spot_remaining < tolerance:
            spot_remaining = 0.0
        if future_remaining < tolerance:
            future_remaining = 0.0

        if spot_remaining == 0.0 and future_remaining == 0.0:
            close_risk = self._risk_with_prior_future_fill(pos, risk)
            if cursor is None:
                self._close_position(pos, spot_result, close_risk, reason, now)
            else:
                self._close_position(
                    pos,
                    spot_result,
                    close_risk,
                    reason,
                    now,
                    cursor=cursor,
                    defer_pnl=True,
                )
            pos['spot_open_qty'] = 0.0
            pos['spot_open_amount'] = 0.0
            pos['exchange_risk_status'] = 'resolved'
            return 'closed'

        balanced = abs(spot_remaining - future_remaining) < tolerance
        detail = (
            f"对账聚合现货分配|filled={allocated_qty:g}|spot_remaining={spot_remaining:g}|"
            f"future_remaining={future_remaining:g}"
        )
        sql = """
            UPDATE mi_trade_position
            SET spot_open_qty = %(spot_open_qty)s,
                spot_open_amount = %(spot_open_amount)s,
                exchange_risk_status = %(risk_status)s,
                exchange_risk_type = %(risk_type)s,
                exchange_risk_at = %(risk_at)s,
                exchange_risk_detail = CONCAT(COALESCE(exchange_risk_detail, ''), '|', %(detail)s)
            WHERE id = %(position_id)s
              AND status = 'holding'
        """
        payload = {
                'spot_open_qty': spot_remaining,
                'spot_open_amount': spot_remaining * spot_open_price,
                'risk_status': 'resolved' if balanced else 'desynced',
                'risk_type': None if balanced else (risk.get('type') or 'binance_spot_excess'),
                'risk_at': now,
                'detail': detail,
                'position_id': int(pos['id']),
        }
        if cursor is None:
            with db_manager.get_cursor() as db_cursor:
                db_cursor.execute(sql, payload)
        else:
            cursor.execute(sql, payload)
        pos['spot_open_qty'] = spot_remaining
        pos['spot_open_amount'] = spot_remaining * spot_open_price
        pos['exchange_risk_status'] = 'resolved' if balanced else 'desynced'
        pos['exchange_risk_type'] = None if balanced else (risk.get('type') or 'binance_spot_excess')
        return 'balanced' if balanced else 'desynced'

    def _resolve_balanced_spot_excess_positions(self, base_asset: str) -> int:
        """Clear broad asset risk marks from holding rows whose two legs are balanced."""
        tolerance = self._spot_qty_tolerance(base_asset)
        sql = """
            UPDATE mi_trade_position
            SET exchange_risk_status = 'resolved',
                exchange_risk_type = NULL,
                exchange_risk_at = %(resolved_at)s,
                exchange_risk_detail = CONCAT(
                    COALESCE(exchange_risk_detail, ''),
                    '|对账聚合现货处置后两腿已平衡'
                )
            WHERE status = 'holding'
              AND UPPER(base_asset) = %(base_asset)s
              AND exchange_risk_status = 'desynced'
              AND exchange_risk_type = 'binance_spot_excess'
              AND ABS(
                    COALESCE(spot_open_qty, 0) - COALESCE(future_open_qty, 0)
                  ) < %(tolerance)s
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'resolved_at': datetime.now(),
                'base_asset': str(base_asset or '').upper(),
                'tolerance': tolerance,
            })
            return int(getattr(cursor, 'rowcount', 0) or 0)

    @staticmethod
    def _proportional_optional_amount(value, ratio: float):
        if value is None:
            return None
        return max(0.0, _float(value)) * ratio

    def _spot_qty_tolerance(self, base_asset: str) -> float:
        meta = getattr(self.executor, 'spot_meta', {}) or {}
        step_size = _float((meta.get(str(base_asset or '').upper()) or {}).get('step_size'))
        return max(step_size * (1.0 - 1e-9), float(self.cfg.min_spot_qty or 0), 1e-8)

    def _quanto_multiplier(self, base_asset: str) -> float:
        meta = getattr(self.executor, 'contract_meta', {}) or {}
        try:
            multiplier = float((meta.get(base_asset) or {}).get('quanto_multiplier') or 0.0)
            return multiplier if multiplier > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _load_binance_available_qty(self, base_asset: str) -> float:
        balances = self.executor.fetch_binance_account_balances()
        for row in balances:
            if str(row.get('asset') or '').upper() == base_asset:
                return _float(row.get('free') if row.get('free') is not None else row.get('total'))
        return 0.0

    def _sell_spot_and_close_position(self, pos: Dict, target_qty: float, risk: Dict) -> Dict:
        position_id = int(pos['id'])
        order_uuid = str(uuid.uuid4())
        base_asset = str(pos.get('base_asset') or '').upper()
        now = datetime.now()
        risk = self._risk_with_prior_future_fill(pos, risk)
        close_reason = self._build_close_reason(risk)

        spot_order = {
            'order_uuid': order_uuid,
            'position_id': position_id,
            'base_asset': base_asset,
            'spot_symbol': pos.get('spot_symbol') or f'{base_asset}USDT',
            'future_contract': pos.get('future_contract') or f'{base_asset}_USDT',
            'order_side': 'close',
            'market_type': 'spot',
            'trade_direction': 'sell',
            'leverage': 1.0,
            'target_qty': target_qty,
            'target_amount': target_qty * _float(pos.get('spot_open_price')),
        }
        spot_result = self._place_binance_spot_reduction(spot_order)
        spot_exec_qty = _float(spot_result.get('exec_qty'))
        spot_remaining_qty = max(0.0, target_qty - spot_exec_qty)

        if not spot_result.get('success'):
            self._insert_spot_order(
                spot_order,
                spot_result,
                close_reason,
                spot_exec_qty > 0,
                now,
            )
            if spot_exec_qty > 0:
                self._keep_spot_only_remainder(pos, spot_exec_qty, spot_result, risk)
            self._append_risk_detail(position_id, f"自动处置失败|spot_sell_rejected:{spot_result.get('reason')}")
            logger.error(
                "ADL 自动处置失败 | %s | position_id=%s | qty=%s | reason=%s",
                base_asset, position_id, target_qty, spot_result.get('reason'),
            )
            return {
                'attempted': True,
                'exchange_order_submitted': True,
                'success': False,
                'position_id': position_id,
                'spot_exec_qty': spot_exec_qty,
                'spot_remaining_qty': spot_remaining_qty,
                'reason': spot_result.get('reason'),
            }

        if self._spot_has_tradable_remainder(base_asset, spot_remaining_qty):
            self._insert_spot_order(spot_order, spot_result, close_reason, True, now)
            self._keep_spot_only_remainder(pos, spot_exec_qty, spot_result, risk)
            return {
                'attempted': True,
                'exchange_order_submitted': True,
                'success': False,
                'position_id': position_id,
                'spot_exec_qty': spot_exec_qty,
                'spot_remaining_qty': spot_remaining_qty,
                'reason': 'spot_close_partial_after_retry',
            }

        position_remaining_qty = max(
            0.0,
            _float(pos.get('spot_open_qty')) - spot_exec_qty,
        )
        if self._spot_has_tradable_remainder(base_asset, position_remaining_qty):
            self._insert_spot_order(spot_order, spot_result, close_reason, True, now)
            self._keep_spot_only_remainder(pos, spot_exec_qty, spot_result, risk)
            return {
                'attempted': True,
                'exchange_order_submitted': True,
                'success': False,
                'position_id': position_id,
                'spot_exec_qty': spot_exec_qty,
                'spot_remaining_qty': position_remaining_qty,
                'reason': 'position_partially_reduced_waiting_fresh_snapshot',
            }

        self._insert_spot_order(spot_order, spot_result, close_reason, True, now)
        if not risk.get('reused_prior_future_fill'):
            self._insert_synthetic_future_adl_order(pos, order_uuid, risk, close_reason, now)
        self._close_position(pos, spot_result, risk, close_reason, now)
        logger.warning(
            "Gate 缺腿自动处置完成 | %s | position_id=%s | spot_qty=%s | spot_px=%s | future_close_px=%s",
            base_asset, position_id, spot_result.get('exec_qty'), spot_result.get('exec_price'), risk.get('future_close_price'),
        )
        return {
            'attempted': True,
            'exchange_order_submitted': True,
            'success': True,
            'position_id': position_id,
            'spot_exec_qty': spot_result.get('exec_qty'),
            'spot_exec_price': spot_result.get('exec_price'),
        }

    def _place_binance_spot_reduction(self, order: Dict) -> Dict:
        retry = getattr(self.executor, 'place_binance_spot_close_with_retry', None)
        if callable(retry):
            return retry(order)
        return self.executor.place_binance_spot_order(order)

    def _spot_has_tradable_remainder(self, base_asset: str, remaining_qty: float) -> bool:
        step_size = _float((getattr(self.executor, 'spot_meta', {}) or {}).get(base_asset, {}).get('step_size'))
        minimum = max(step_size, float(self.cfg.min_spot_qty or 0))
        remaining_qty = max(0.0, float(remaining_qty or 0))
        if minimum > 0:
            return remaining_qty >= minimum * (1.0 - 1e-9)
        return remaining_qty > 1e-8

    def _keep_spot_only_remainder(
        self,
        pos: Dict,
        spot_exec_qty: float,
        spot_result: Dict,
        risk: Dict,
    ) -> None:
        open_qty = max(0.0, _float(pos.get('spot_open_qty')))
        remaining_qty = max(0.0, open_qty - max(0.0, spot_exec_qty))
        open_price = _float(pos.get('spot_open_price'))
        detail = (
            f"对账兜底Binance部分成交|filled={spot_exec_qty:g}|"
            f"remaining={remaining_qty:g}|attempts={spot_result.get('retry_attempts') or 1}"
        )
        sql = """
            UPDATE mi_trade_position
            SET spot_open_qty = %(spot_open_qty)s,
                spot_open_amount = %(spot_open_amount)s,
                exchange_risk_status = 'desynced',
                exchange_risk_type = %(risk_type)s,
                exchange_risk_at = %(risk_at)s,
                exchange_risk_detail = CONCAT(COALESCE(exchange_risk_detail, ''), '|', %(detail)s)
            WHERE id = %(position_id)s
              AND status = 'holding'
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'spot_open_qty': remaining_qty,
                'spot_open_amount': remaining_qty * open_price,
                'risk_type': risk.get('type') or 'binance_spot_excess',
                'risk_at': datetime.now(),
                'detail': detail,
                'position_id': pos.get('id'),
            })
        pos['spot_open_qty'] = remaining_qty
        pos['spot_open_amount'] = remaining_qty * open_price

    def _risk_with_prior_future_fill(self, pos: Dict, risk: Dict) -> Dict:
        """系统风险平仓期货腿已成交时，复用真实成交价关闭后续 spot 兜底。"""
        prior = self._load_prior_future_fill(int(pos.get('id') or 0))
        if not prior:
            return risk

        updated = dict(risk)
        updated['future_close_price'] = prior['exec_price']
        updated['future_exchange_order_id'] = prior.get('exchange_order_id')
        updated['future_liquidity_role'] = prior.get('liquidity_role') or 'taker'
        updated['future_close_size'] = abs(_float(pos.get('future_open_contracts')))
        updated['reused_prior_future_fill'] = True
        detail = str(updated.get('detail') or '')
        updated['detail'] = (
            f"{detail}|复用风险平仓已成交Gate期货|future_exec: "
            f"price={prior['exec_price']}, qty={prior['exec_qty']}, "
            f"order_id={prior.get('exchange_order_id') or ''}"
        )
        return updated

    def _load_prior_future_fill(self, position_id: int) -> Optional[Dict]:
        if position_id <= 0:
            return None
        sql = """
            SELECT id, order_uuid, exec_price, exec_qty, exec_amount,
                   liquidity_role, exchange_order_id, executed_at, reject_reason
            FROM mi_trade_order
            WHERE position_id = %s
              AND order_side = 'close'
              AND market_type = 'future'
              AND status = 'executed'
              AND exec_price IS NOT NULL
              AND exec_qty IS NOT NULL
              AND reject_reason LIKE '%%期货已成交%%'
            ORDER BY executed_at DESC, id DESC
            LIMIT 1
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (position_id,))
            row = cursor.fetchone()
        if not row:
            return None
        exec_price = _float(row.get('exec_price'))
        exec_qty = _float(row.get('exec_qty'))
        if exec_price <= 0 or exec_qty <= 0:
            return None
        return {
            **row,
            'exec_price': exec_price,
            'exec_qty': exec_qty,
            'exec_amount': _float(row.get('exec_amount')),
        }

    def _close_position_from_prior_spot_fill(self, pos: Dict, risk: Dict) -> Dict:
        """风险平仓已卖出现货、随后 Gate ADL 时，复用已成交现货补齐本地闭合。"""
        position_id = int(pos['id'])
        prior = self._load_prior_spot_fill(position_id)
        if not prior:
            return {'attempted': True, 'success': False, 'reason': 'spot_available_qty_insufficient'}

        expected_qty = _float(pos.get('spot_open_qty'))
        if expected_qty > 0 and prior['exec_qty'] + 1e-9 < expected_qty:
            return {
                'attempted': True,
                'success': False,
                'reason': 'prior_spot_fill_partial',
                'spot_exec_qty': prior['exec_qty'],
                'expected_qty': expected_qty,
            }

        risk = self._risk_with_prior_liquidation(pos, risk)
        now = datetime.now()
        reason = f"{self._build_close_reason(risk)}|复用风险平仓已成交现货"
        spot_result = {
            'exec_price': prior['exec_price'],
            'exec_qty': prior['exec_qty'],
            'exec_amount': prior['exec_amount'],
            'coverage_ratio': 0,
        }
        self._mark_prior_spot_order_executed(prior, pos, spot_result, reason, now)
        self._insert_synthetic_future_adl_order(pos, prior['order_uuid'], risk, reason, now)
        self._close_position(pos, spot_result, risk, reason, now)
        logger.warning(
            "ADL 自动处置补记完成 | %s | position_id=%s | prior_spot_qty=%s | "
            "prior_spot_px=%s | future_adl_px=%s",
            pos.get('base_asset'), position_id, prior['exec_qty'], prior['exec_price'],
            risk.get('future_close_price'),
        )
        return {
            'attempted': True,
            'success': True,
            'position_id': position_id,
            'spot_exec_qty': prior['exec_qty'],
            'spot_exec_price': prior['exec_price'],
            'reused_prior_spot_fill': True,
        }

    def _risk_with_prior_liquidation(self, pos: Dict, risk: Dict) -> Dict:
        if _float(risk.get('future_close_price')) > 0:
            return risk

        text = "|".join(
            str(pos.get(key) or '')
            for key in ('close_reason', 'exchange_risk_detail')
        )
        price_match = re.search(r"(?:price|fill_price)=([0-9.]+)", text)
        if not price_match:
            return risk

        updated = dict(risk)
        updated['future_close_price'] = _float(price_match.group(1))
        if '强平' in text or 'liquidation' in text:
            updated.setdefault('type', 'liquidation')
        order_match = re.search(r"order_id=([^|,\s]+)", text)
        if order_match:
            updated.setdefault('future_exchange_order_id', order_match.group(1))
        updated.setdefault('future_liquidity_role', 'taker')
        return updated

    def _risk_with_recent_liquidation(self, base_asset: str, risk: Dict) -> Dict:
        """对账兜底缺少成交价时，复用同标的最近 Gate 强平/ADL 事件价。"""
        if _float(risk.get('future_close_price')) > 0:
            return risk
        event_at = risk.get('event_at')
        if not isinstance(event_at, datetime):
            event_at = datetime.now()
        start_at = event_at - timedelta(minutes=10)
        end_at = event_at + timedelta(seconds=30)
        sql = """
            SELECT risk_type, event_at, exchange_order_id, exchange_trade_id,
                   fill_price, size
            FROM mi_exchange_risk_event
            WHERE UPPER(base_asset) = %s
              AND risk_type IN ('liquidation', 'adl')
              AND fill_price IS NOT NULL
              AND fill_price > 0
              AND event_at BETWEEN %s AND %s
            ORDER BY event_at DESC, id DESC
            LIMIT 1
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (base_asset, start_at, end_at))
            row = cursor.fetchone()
        if not row:
            return risk
        updated = dict(risk)
        updated['future_close_price'] = _float(row.get('fill_price'))
        updated['future_exchange_order_id'] = row.get('exchange_order_id')
        updated['future_trade_id'] = row.get('exchange_trade_id')
        updated['future_liquidity_role'] = 'taker'
        updated['type'] = updated.get('type') or row.get('risk_type')
        detail = str(updated.get('detail') or '')
        updated['detail'] = (
            f"{detail}|复用最近Gate{row.get('risk_type')}事件成交价:"
            f"price={updated['future_close_price']},event_at={row.get('event_at')},"
            f"order_id={row.get('exchange_order_id') or ''}"
        )
        return updated

    def _load_prior_spot_fill(self, position_id: int) -> Optional[Dict]:
        sql = """
            SELECT id, order_uuid, base_asset, spot_symbol, future_contract,
                   target_qty, target_amount, reject_reason, created_at
            FROM mi_trade_order
            WHERE position_id = %s
              AND order_side = 'close'
              AND market_type = 'spot'
              AND status = 'rejected'
              AND reject_reason LIKE '%%现货已成交%%'
              AND reject_reason LIKE '%%spot_exec:%%'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (position_id,))
            row = cursor.fetchone()
        if not row:
            return None

        match = re.search(
            r"spot_exec:\s*price=([0-9.]+),\s*qty=([0-9.]+)",
            str(row.get('reject_reason') or ''),
        )
        if not match:
            return None
        exec_price = _float(match.group(1))
        exec_qty = _float(match.group(2))
        if exec_price <= 0 or exec_qty <= 0:
            return None
        return {
            **row,
            'exec_price': exec_price,
            'exec_qty': exec_qty,
            'exec_amount': exec_price * exec_qty,
        }

    def _mark_prior_spot_order_executed(
        self,
        prior: Dict,
        pos: Dict,
        spot_result: Dict,
        reason: str,
        now: datetime,
    ):
        order = {
            'order_side': 'close',
            'market_type': 'spot',
            'trade_direction': 'sell',
        }
        fields = self._execution_fields('spot_order', order, spot_result, True)
        fee_rate = fields.get('fee_rate')
        fee_amount_usdt = fields.get('fee_amount_usdt')
        if fee_amount_usdt is None and fee_rate is not None:
            fee_amount_usdt = _float(spot_result.get('exec_amount')) * _float(fee_rate)
        sql = """
            UPDATE mi_trade_order
            SET status = 'executed',
                reject_reason = %(reject_reason)s,
                exec_price = %(exec_price)s,
                exec_qty = %(exec_qty)s,
                exec_amount = %(exec_amount)s,
                coverage_ratio = %(coverage_ratio)s,
                liquidity_role = %(liquidity_role)s,
                fee_rate = %(fee_rate)s,
                fee_amount = %(fee_amount)s,
                fee_amount_usdt = %(fee_amount_usdt)s,
                fee_asset = %(fee_asset)s,
                exchange_order_id = %(exchange_order_id)s,
                executed_at = %(executed_at)s
            WHERE id = %(order_id)s
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'reject_reason': reason,
                'exec_price': spot_result.get('exec_price'),
                'exec_qty': spot_result.get('exec_qty'),
                'exec_amount': spot_result.get('exec_amount'),
                'coverage_ratio': spot_result.get('coverage_ratio'),
                'liquidity_role': fields.get('liquidity_role'),
                'fee_rate': fee_rate,
                'fee_amount': fields.get('fee_amount'),
                'fee_amount_usdt': fee_amount_usdt,
                'fee_asset': fields.get('fee_asset'),
                'exchange_order_id': fields.get('exchange_order_id'),
                'executed_at': prior.get('created_at') or now,
                'order_id': prior.get('id'),
            })

    def _build_close_reason(self, risk: Dict) -> str:
        return (
            f"交易所断腿自动处置|{risk.get('type', 'unknown')}|"
            f"{risk.get('detail', '')}|动作=Binance现货市价卖出"
        )

    def _insert_spot_order(
        self,
        order: Dict,
        exec_data: Dict,
        reason: str,
        success: bool,
        now: datetime,
        *,
        cursor=None,
    ):
        fields = self._execution_fields('spot_order', order, exec_data, success)
        sql = """
            INSERT INTO mi_trade_order (
                order_uuid, position_id, base_asset, spot_symbol, future_contract,
                order_side, market_type, trade_direction, leverage, status, channel,
                reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_vwap_basis_bps, risk_relief_bps,
                open_marginal_basis_bps, funding_rate_24h,
                liquidity_role, fee_rate, fee_amount, fee_amount_usdt, fee_asset, exchange_order_id, executed_at
            ) VALUES (
                %(order_uuid)s, %(position_id)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                %(order_side)s, %(market_type)s, %(trade_direction)s, %(leverage)s, %(status)s, %(channel)s,
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                NULL, NULL, NULL, NULL, NULL,
                %(liquidity_role)s, %(fee_rate)s, %(fee_amount)s, %(fee_amount_usdt)s, %(fee_asset)s, %(exchange_order_id)s, %(executed_at)s
            )
        """
        payload = {
            **order,
            'status': 'executed' if success else 'rejected',
            'channel': 'Live',
            'reject_reason': reason if success else f"{reason}|拒单:{exec_data.get('reason')}",
            'exec_price': exec_data.get('exec_price') if success else None,
            'exec_qty': exec_data.get('exec_qty') if success else None,
            'exec_amount': exec_data.get('exec_amount') if success else None,
            'coverage_ratio': exec_data.get('coverage_ratio') if success else None,
            **fields,
            'executed_at': now if success else None,
        }
        if cursor is None:
            with db_manager.get_cursor() as db_cursor:
                db_cursor.execute(sql, payload)
        else:
            cursor.execute(sql, payload)

    def _insert_synthetic_future_adl_order(self, pos: Dict, order_uuid: str, risk: Dict, reason: str, now: datetime):
        base_asset = str(pos.get('base_asset') or '').upper()
        future_qty = _float(pos.get('future_open_qty'))
        if future_qty <= 0:
            return
        future_price = _float(risk.get('future_close_price'))
        if future_price <= 0:
            logger.warning(
                "Gate 缺腿自动处置缺少期货成交价，跳过合成期货平仓单 | %s | position_id=%s",
                base_asset, pos.get('id'),
            )
            return
        future_exec = {
            'exec_price': future_price,
            'exec_qty': future_qty,
            'exec_amount': future_qty * future_price,
            'coverage_ratio': 0,
            'exchange_order_id': risk.get('future_exchange_order_id'),
            'fee_amount': 0,
            'fee_amount_usdt': 0,
            'fee_asset': 'USDT',
        }
        order = {
            'order_uuid': order_uuid,
            'position_id': pos.get('id'),
            'base_asset': base_asset,
            'spot_symbol': pos.get('spot_symbol') or f'{base_asset}USDT',
            'future_contract': pos.get('future_contract') or f'{base_asset}_USDT',
            'order_side': 'close',
            'market_type': 'future',
            'trade_direction': 'buy',
            'leverage': _float(pos.get('future_open_leverage'), 1.0),
            'target_qty': future_qty,
            'target_amount': future_qty * future_price,
        }
        fields = self._execution_fields('future_order', order, future_exec, True)
        fields.update({
            'liquidity_role': risk.get('future_liquidity_role') or 'taker',
            'fee_rate': 0.0,
            'fee_amount': 0.0,
            'fee_amount_usdt': 0.0,
            'fee_asset': 'USDT',
            'exchange_order_id': risk.get('future_exchange_order_id'),
        })
        sql = """
            INSERT INTO mi_trade_order (
                order_uuid, position_id, base_asset, spot_symbol, future_contract,
                order_side, market_type, trade_direction, leverage, status, channel,
                reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_vwap_basis_bps, risk_relief_bps,
                open_marginal_basis_bps, funding_rate_24h,
                liquidity_role, fee_rate, fee_amount, fee_amount_usdt, fee_asset, exchange_order_id, executed_at
            ) VALUES (
                %(order_uuid)s, %(position_id)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                %(order_side)s, %(market_type)s, %(trade_direction)s, %(leverage)s, 'executed', 'Live',
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                NULL, NULL, NULL, NULL, NULL,
                %(liquidity_role)s, %(fee_rate)s, %(fee_amount)s, %(fee_amount_usdt)s, %(fee_asset)s, %(exchange_order_id)s, %(executed_at)s
            )
        """
        payload = {
            **order,
            'reject_reason': f"{reason}|Gate腿由ADL成交记录补记",
            'exec_price': future_exec['exec_price'],
            'exec_qty': future_exec['exec_qty'],
            'exec_amount': future_exec['exec_amount'],
            'coverage_ratio': 0,
            **fields,
            'executed_at': now,
        }
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, payload)

    def _execution_fields(self, market_key: str, order: Dict, exec_data: Dict, success: bool) -> Dict:
        if not success:
            return {
                'liquidity_role': None,
                'fee_rate': None,
                'fee_amount': None,
                'fee_amount_usdt': None,
                'fee_asset': None,
                'exchange_order_id': None,
            }
        exec_result = {'execution_stats': {}, market_key: exec_data}
        return build_order_execution_fields(
            market_key,
            order,
            exec_data,
            exec_result,
            spot_open_fee=self.cfg.spot_open_fee,
            spot_close_fee=self.cfg.spot_close_fee,
            future_open_fee=self.cfg.future_open_fee,
            future_close_fee=self.cfg.future_close_fee,
            future_taker_open_fee=self.cfg.future_taker_open_fee,
            future_taker_close_fee=self.cfg.future_taker_close_fee,
        )

    def _close_position(
        self,
        pos: Dict,
        spot_result: Dict,
        risk: Dict,
        reason: str,
        now: datetime,
        *,
        cursor=None,
        defer_pnl: bool = False,
    ):
        spot_price = _float(spot_result.get('exec_price'))
        future_price = _float(risk.get('future_close_price'))
        future_qty = _float(pos.get('future_open_qty'))
        close_spread = calc_vwap_basis_bps(spot_price, future_price) if spot_price > 0 and future_price > 0 else None
        sql = """
            UPDATE mi_trade_position SET
                status = 'closed',
                closed_at = %(closed_at)s,
                close_reason = %(close_reason)s,
                spot_close_price = %(spot_close_price)s,
                future_close_price = %(future_close_price)s,
                spot_close_amount = %(spot_close_amount)s,
                future_close_amount = %(future_close_amount)s,
                close_spread_bps = %(close_spread_bps)s,
                close_funding_rate_24h = %(close_funding_rate_24h)s,
                exchange_risk_status = 'resolved'
            WHERE id = %(position_id)s
        """
        payload = {
                'closed_at': now,
                'close_reason': reason,
                'spot_close_price': spot_price,
                'future_close_price': future_price if future_price > 0 else None,
                'spot_close_amount': spot_result.get('exec_amount'),
                'future_close_amount': future_qty * future_price if future_price > 0 else None,
                'close_spread_bps': round(close_spread, 2) if close_spread is not None else None,
                'close_funding_rate_24h': (
                    risk.get('funding_rate_24h')
                    if risk.get('funding_rate_24h') is not None
                    else pos.get('funding_rate_24h')
                ),
                'position_id': pos.get('id'),
        }

        def apply(db_cursor):
            db_cursor.execute(sql, payload)
            if defer_pnl:
                return
            pnl_values = self._compute_closed_position_pnl(pos)
            if pnl_values:
                update_closed_position_pnl(
                    db_cursor,
                    int(pos.get('id')),
                    pnl_values,
                    self._position_columns(),
                )

        if cursor is None:
            with db_manager.get_cursor() as db_cursor:
                apply(db_cursor)
        else:
            apply(cursor)

    def _refresh_closed_position_pnl(self, pos: Dict):
        pnl_values = self._compute_closed_position_pnl(pos)
        if not pnl_values:
            return
        with db_manager.get_cursor() as cursor:
            update_closed_position_pnl(
                cursor,
                int(pos.get('id')),
                pnl_values,
                self._position_columns(),
            )

    def _position_columns(self) -> set[str]:
        if self._position_columns_cache is None:
            self._position_columns_cache = existing_position_columns()
        return self._position_columns_cache

    def _compute_closed_position_pnl(self, pos: Dict) -> Optional[Dict]:
        position_id = pos.get('id')
        if position_id is None:
            return None
        try:
            orders = fetch_executed_position_orders(int(position_id))
            return compute_closed_position_pnl(pos, orders)
        except Exception as e:
            logger.warning(f"断腿兜底收益计算失败 | position_id={position_id} | err={e}")
            return None

    def _append_risk_detail(self, position_id: int, message: str):
        sql = """
            UPDATE mi_trade_position
            SET exchange_risk_detail = CONCAT(COALESCE(exchange_risk_detail, ''), '|', %(message)s)
            WHERE id = %(position_id)s
              AND COALESCE(exchange_risk_detail, '') NOT LIKE %(message_like)s
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'message': message,
                'message_like': f'%{message}%',
                'position_id': position_id,
            })

    def _mark_positions_exchange_risk(self, positions: List[Dict], risk: Dict):
        ids = [int(row['id']) for row in positions if row.get('id') is not None]
        if not ids:
            return

        reason = f"交易所仓位风险:{risk.get('type')}|{risk.get('detail')}"
        placeholders = ','.join(['%s'] * len(ids))
        sql = f"""
            UPDATE mi_trade_position
            SET exchange_risk_status = 'desynced',
                exchange_risk_type = %s,
                exchange_risk_at = %s,
                exchange_risk_detail = %s,
                close_reason = CASE
                    WHEN close_reason IS NULL OR close_reason = '' THEN %s
                    WHEN close_reason NOT LIKE %s THEN CONCAT(close_reason, '|', %s)
                    ELSE close_reason
                END
            WHERE id IN ({placeholders})
        """
        params = [
            risk.get('type') or 'unknown',
            risk.get('event_at') or datetime.now(),
            str(risk.get('detail') or ''),
            reason,
            f"%交易所仓位风险:{risk.get('type')}%",
            reason,
            *ids,
        ]
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
