# coding: utf-8
"""交易所断腿自动处置。

Gate futures 被 ADL 自动减仓后，本地 holding 仍对应 Binance spot 多头。
本模块由实时 Gate 风险事件触发，自动卖出对应 spot，关闭本地持仓。
"""
import uuid
from dataclasses import dataclass
from datetime import datetime
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

        positions = self._load_positions_to_remediate(
            base_asset,
            missing_contracts,
            require_desynced=require_desynced,
        )
        if not positions:
            return {'attempted': False, 'reason': 'no_matching_holding_positions'}

        available_qty = self._load_binance_available_qty(base_asset)
        remaining_available = available_qty
        limit = max(int(self.cfg.max_positions_per_run or 1), 1)
        selected_positions = positions[:limit]
        if mark_positions:
            self._mark_positions_exchange_risk(selected_positions, risk)

        results = []
        for pos in selected_positions:
            target_qty = min(_float(pos.get('spot_open_qty')), remaining_available)
            if target_qty <= max(float(self.cfg.min_spot_qty or 0), 0):
                results.append({
                    'attempted': True,
                    'position_id': pos.get('id'),
                    'success': False,
                    'reason': 'spot_available_qty_insufficient',
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
            'base_asset': base_asset,
            'positions': len(selected_positions),
            'matching_positions': len(positions),
            'success_count': success_count,
            'failure_count': failure_count,
            'results': results,
        }

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
        self._insert_synthetic_future_adl_order(pos, order_uuid, risk, close_reason, now)
        self._close_position(pos, spot_result, risk, close_reason, now)
        logger.warning(
            "ADL 自动处置完成 | %s | position_id=%s | spot_qty=%s | spot_px=%s | future_adl_px=%s",
            base_asset, position_id, spot_result.get('exec_qty'), spot_result.get('exec_price'), risk.get('future_close_price'),
        )
        return {
            'attempted': True,
            'success': True,
            'position_id': position_id,
            'spot_exec_qty': spot_result.get('exec_qty'),
            'spot_exec_price': spot_result.get('exec_price'),
        }

    def _build_close_reason(self, risk: Dict) -> str:
        return (
            f"交易所断腿自动处置|{risk.get('type', 'unknown')}|"
            f"{risk.get('detail', '')}|动作=Binance现货市价卖出"
        )[:1000]

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
        future_price = _float(risk.get('future_close_price') or pos.get('future_open_price'))
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
        future_price = _float(risk.get('future_close_price') or pos.get('future_open_price'))
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
                'future_close_price': future_price,
                'spot_close_amount': spot_result.get('exec_amount'),
                'future_close_amount': future_qty * future_price,
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
            cursor.execute(sql, {'message': message[:300], 'position_id': position_id})

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
            str(risk.get('detail') or '')[:1000],
            reason[:500],
            f"%交易所仓位风险:{risk.get('type')}%",
            reason[:500],
            *ids,
        ]
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, params)
