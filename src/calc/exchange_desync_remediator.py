# coding: utf-8
"""交易所断腿自动处置。

Gate futures 被 ADL 自动减仓后，本地 holding 仍对应 Binance spot 多头。
本模块由实时 Gate 风险事件触发，自动卖出对应 spot，关闭本地持仓。
"""
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from calc.order_fee_resolver import build_order_execution_fields
from calc.orderbook_enricher import calc_vwap_basis_bps
from calc.real_executor import RealExecutor
from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ExchangeDesyncRemediationConfig:
    enabled: bool = True
    action: str = 'sell_spot'
    max_positions_per_run: int = 20
    min_spot_qty: float = 0.0
    close_extra_gate_position: bool = True
    remediate_binance_spot_position: bool = True
    spot_open_fee: float = 0.00075
    spot_close_fee: float = 0.00075
    future_open_fee: float = 0.0002
    future_close_fee: float = 0.0002
    future_taker_open_fee: float = 0.0005
    future_taker_close_fee: float = 0.0005


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


class ExchangeDesyncRemediator:
    """把 Gate 缺腿风险转成可审计的自动 spot 处置。"""

    def __init__(self, executor: RealExecutor, cfg: ExchangeDesyncRemediationConfig):
        self.executor = executor
        self.cfg = cfg

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
        limit = int(self.cfg.max_positions_per_run or 0)
        selected_positions = positions if limit <= 0 else positions[:limit]
        if mark_positions:
            self._mark_positions_exchange_risk(selected_positions, risk)

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
            if result.get('success'):
                remaining_available -= _float(result.get('spot_exec_qty'), target_qty)

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

    def remediate_binance_spot_desync(
        self,
        base_asset: str,
        local_qty: float,
        exchange_qty: float,
        risk: Dict,
    ) -> Dict:
        """Binance 现货与本地 holding 不一致时，按本地数量修复多余/缺少的多头。"""
        base_asset = str(base_asset or '').upper()
        if not self.cfg.enabled:
            return {'attempted': False, 'reason': 'disabled'}
        if not self.cfg.remediate_binance_spot_position:
            return {'attempted': False, 'reason': 'remediate_binance_spot_position_disabled'}

        diff = float(exchange_qty or 0) - float(local_qty or 0)
        if abs(diff) <= max(float(self.cfg.min_spot_qty or 0), 1e-8):
            return {'attempted': False, 'reason': 'binance_spot_diff<=tolerance', 'diff_qty': diff}

        trade_direction = 'sell' if diff > 0 else 'buy'
        target_qty = abs(diff)
        if trade_direction == 'sell':
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
                }

        order_uuid = str(uuid.uuid4())
        price_hint = self._estimate_binance_spot_price(base_asset, risk)
        action = 'sell_extra_binance_spot' if trade_direction == 'sell' else 'buy_missing_binance_spot'
        reason = (
            f"对账兜底自动处置|Binance现货{'多余' if diff > 0 else '缺少'}|"
            f"asset={base_asset}|local={local_qty:g}|exchange={exchange_qty:g}|diff={diff:g}|"
            f"关联风险={risk.get('type', 'unknown')}"
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
        if trade_direction == 'buy':
            order['quantity_mode'] = 'base'

        result = self.executor.place_binance_spot_order(order)
        success = bool(result.get('success'))
        self._insert_spot_order(order, result, reason, success, datetime.now())
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
            'success': success,
            'action': action,
            'base_asset': base_asset,
            'order_uuid': order_uuid,
            'target_qty': target_qty,
            'local_qty': local_qty,
            'exchange_qty': exchange_qty,
            'spot_result': result,
            'reason': result.get('reason') if not success else None,
        }

    def _estimate_binance_spot_price(self, base_asset: str, risk: Dict) -> float:
        for key in ('spot_price', 'mark_price', 'future_close_price'):
            price = _float(risk.get(key))
            if price > 0:
                return price
        getter = getattr(self.executor, '_get_binance_usdt_price', None)
        if callable(getter):
            try:
                price = _float(getter(base_asset))
                if price > 0:
                    return price
            except Exception:
                logger.debug("Binance spot price estimate failed | %s", base_asset, exc_info=True)
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

    def _quanto_multiplier(self, base_asset: str) -> float:
        meta = getattr(self.executor, 'contract_meta', {}) or {}
        try:
            return float((meta.get(base_asset) or {}).get('quanto_multiplier') or 1.0)
        except (TypeError, ValueError):
            return 1.0

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
        spot_result = self.executor.place_binance_spot_order(spot_order)

        if not spot_result.get('success'):
            self._insert_spot_order(spot_order, spot_result, close_reason, False, now)
            self._append_risk_detail(position_id, f"自动处置失败|spot_sell_rejected:{spot_result.get('reason')}")
            logger.error(
                "ADL 自动处置失败 | %s | position_id=%s | qty=%s | reason=%s",
                base_asset, position_id, target_qty, spot_result.get('reason'),
            )
            return {
                'attempted': True,
                'success': False,
                'position_id': position_id,
                'reason': spot_result.get('reason'),
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
            'success': True,
            'position_id': position_id,
            'spot_exec_qty': spot_result.get('exec_qty'),
            'spot_exec_price': spot_result.get('exec_price'),
        }

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

    def _insert_spot_order(self, order: Dict, exec_data: Dict, reason: str, success: bool, now: datetime):
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
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, payload)

    def _insert_synthetic_future_adl_order(self, pos: Dict, order_uuid: str, risk: Dict, reason: str, now: datetime):
        base_asset = str(pos.get('base_asset') or '').upper()
        future_qty = _float(pos.get('future_open_qty'))
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

    def _close_position(self, pos: Dict, spot_result: Dict, risk: Dict, reason: str, now: datetime):
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
                exchange_risk_status = 'resolved'
            WHERE id = %(position_id)s
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {
                'closed_at': now,
                'close_reason': reason,
                'spot_close_price': spot_price,
                'future_close_price': future_price if future_price > 0 else None,
                'spot_close_amount': spot_result.get('exec_amount'),
                'future_close_amount': future_qty * future_price if future_price > 0 else None,
                'close_spread_bps': round(close_spread, 2) if close_spread is not None else None,
                'position_id': pos.get('id'),
            })

    def _append_risk_detail(self, position_id: int, message: str):
        sql = """
            UPDATE mi_trade_position
            SET exchange_risk_detail = CONCAT(COALESCE(exchange_risk_detail, ''), '|', %(message)s)
            WHERE id = %(position_id)s
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, {'message': message, 'position_id': position_id})

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
