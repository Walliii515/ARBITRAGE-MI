# coding: utf-8
"""
真实账户资金快照。

按分钟级频率读取 Binance 现货和 Gate 永续账户资金，汇总本地已实现盈亏、
资金费和手续费，写入 mi_capital_snapshot；开仓逻辑读取最新缓存即可。
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from calc.reconciliation import build_exchange_config
from calc.real_executor import RealExecutor
from common.config import config
from common.database import db_manager
from common.logger import get_logger
from common.meta_loader import fetch_contract_meta, fetch_spot_meta

logger = get_logger(__name__)


@dataclass
class AccountCapitalConfig:
    retention_days: int = 30


def build_default_capital_snapshotter() -> 'AccountCapitalSnapshotter':
    contract_meta = fetch_contract_meta()
    spot_meta = fetch_spot_meta()
    executor = RealExecutor(
        build_exchange_config(),
        contract_meta=contract_meta,
        spot_meta=spot_meta,
        leverage=config.get_int('margin.leverage', 2),
    )
    return AccountCapitalSnapshotter(
        executor,
        AccountCapitalConfig(
            retention_days=config.get_int('account_capital.retention_days', 30),
        ),
    )


class AccountCapitalSnapshotter:
    """账户资金快照器。"""

    def __init__(self, executor: RealExecutor, cfg: Optional[AccountCapitalConfig] = None):
        self.executor = executor
        self.cfg = cfg or AccountCapitalConfig()

    def run_once(self) -> Dict:
        snapshot_at = datetime.now()
        pnl = self._load_local_pnl_summary()
        binance = self._build_binance_row(snapshot_at, pnl)
        gate = self._build_gate_row(snapshot_at, pnl)
        total = self._build_total_row(snapshot_at, binance, gate, pnl)
        rows = [binance, gate, total]
        self._insert_rows(rows)
        self.cleanup_old_snapshots()
        return {
            'success': True,
            'snapshot_at': snapshot_at.strftime('%Y-%m-%d %H:%M:%S'),
            'summary': self.rows_to_summary(rows),
        }

    def get_latest_summary(self) -> Optional[Dict]:
        sql = """
            SELECT *
            FROM mi_capital_snapshot
            WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM mi_capital_snapshot)
            ORDER BY FIELD(exchange, 'binance', 'gate', 'total')
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        if not rows:
            return None
        return self.rows_to_summary(rows)

    @staticmethod
    def rows_to_summary(rows: List[Dict]) -> Dict:
        by_exchange = {r['exchange']: _serialize_capital_row(r) for r in rows}
        binance = by_exchange.get('binance', {})
        gate = by_exchange.get('gate', {})
        total = by_exchange.get('total', {})
        return {
            'binance': {
                'available': binance.get('available_usdt', 0),
                'locked': binance.get('locked_usdt', 0),
                'capital_used': binance.get('position_value_usdt', 0),
                'floating_value': binance.get('position_value_usdt', 0),
                'realized_pnl': binance.get('realized_pnl_usdt', 0),
                'fees': binance.get('fee_cost_usdt', 0),
                'net_value': binance.get('equity_usdt', 0),
            },
            'gate': {
                'available': gate.get('available_usdt', 0),
                'margin_used': gate.get('margin_used_usdt', 0),
                'floating_value': gate.get('position_value_usdt', 0),
                'realized_pnl': gate.get('realized_pnl_usdt', 0),
                'unrealized_pnl': gate.get('unrealized_pnl_usdt', 0),
                'fees': gate.get('fee_cost_usdt', 0),
                'net_value': gate.get('equity_usdt', 0),
            },
            'total': {
                'used': total.get('position_value_usdt', 0),
                'available': total.get('available_usdt', 0),
                'floating_pnl': total.get('unrealized_pnl_usdt', 0),
                'realized_pnl': total.get('realized_pnl_usdt', 0),
                'funding_pnl': total.get('funding_pnl_usdt', 0),
                'fee_cost': total.get('fee_cost_usdt', 0),
                'total_pnl': total.get('total_pnl_usdt', 0),
                'fees': total.get('fee_cost_usdt', 0),
                'net_value': total.get('equity_usdt', 0),
            },
            'snapshot_at': total.get('snapshot_at') or binance.get('snapshot_at') or gate.get('snapshot_at'),
        }

    def cleanup_old_snapshots(self):
        if self.cfg.retention_days <= 0:
            return
        cutoff = datetime.now() - timedelta(days=self.cfg.retention_days)
        with db_manager.get_cursor() as cursor:
            cursor.execute("DELETE FROM mi_capital_snapshot WHERE snapshot_at < %s", (cutoff,))

    def _build_binance_row(self, snapshot_at: datetime, pnl: Dict) -> Dict:
        balances = self.executor.fetch_binance_account_balances()
        usdt = next((b for b in balances if b.get('asset') == 'USDT'), None) or {}
        non_usdt = [b for b in balances if b.get('asset') != 'USDT']
        prices = self.executor.fetch_binance_ticker_prices([b.get('asset') for b in non_usdt])
        spot_value = sum(float(b.get('total') or 0) * float(prices.get(b.get('asset'), 0)) for b in non_usdt)
        available = float(usdt.get('free') or 0)
        locked = float(usdt.get('locked') or 0)
        equity = available + locked + spot_value
        return {
            'snapshot_at': snapshot_at,
            'exchange': 'binance',
            'equity_usdt': equity,
            'available_usdt': available,
            'locked_usdt': locked,
            'position_value_usdt': spot_value,
            'margin_used_usdt': 0.0,
            'unrealized_pnl_usdt': 0.0,
            'realized_pnl_usdt': pnl['binance_realized_pnl'],
            'funding_pnl_usdt': 0.0,
            'fee_cost_usdt': pnl['binance_fee_cost'],
            'total_pnl_usdt': pnl['binance_realized_pnl'] + pnl['binance_fee_cost'],
            'detail': {'balances': balances, 'prices': prices},
        }

    def _build_gate_row(self, snapshot_at: datetime, pnl: Dict) -> Dict:
        account = self.executor.fetch_gate_futures_account()
        available = _float(account.get('available'))
        total = _float(account.get('total'))
        unrealized = _float(account.get('unrealised_pnl') or account.get('unrealized_pnl'))
        position_margin = _float(account.get('position_margin'))
        isolated_position_margin = _float(account.get('isolated_position_margin'))
        position_initial_margin = _float(account.get('position_initial_margin'))
        order_margin = _float(account.get('order_margin'))
        margin_used = max(position_margin, isolated_position_margin, position_initial_margin) + order_margin
        equity = total if total else available + margin_used + unrealized
        return {
            'snapshot_at': snapshot_at,
            'exchange': 'gate',
            'equity_usdt': equity,
            'available_usdt': available,
            'locked_usdt': order_margin,
            'position_value_usdt': margin_used,
            'margin_used_usdt': margin_used,
            'unrealized_pnl_usdt': unrealized,
            'realized_pnl_usdt': pnl['gate_realized_pnl'],
            'funding_pnl_usdt': pnl['funding_pnl'],
            'fee_cost_usdt': pnl['gate_fee_cost'],
            'total_pnl_usdt': pnl['gate_realized_pnl'] + pnl['funding_pnl'] + pnl['gate_fee_cost'],
            'detail': account,
        }

    def _build_total_row(self, snapshot_at: datetime, binance: Dict, gate: Dict, pnl: Dict) -> Dict:
        return {
            'snapshot_at': snapshot_at,
            'exchange': 'total',
            'equity_usdt': binance['equity_usdt'] + gate['equity_usdt'],
            'available_usdt': binance['available_usdt'] + gate['available_usdt'],
            'locked_usdt': binance['locked_usdt'] + gate['locked_usdt'],
            'position_value_usdt': binance['position_value_usdt'] + gate['position_value_usdt'],
            'margin_used_usdt': gate['margin_used_usdt'],
            'unrealized_pnl_usdt': gate['unrealized_pnl_usdt'],
            'realized_pnl_usdt': pnl['realized_pnl'],
            'funding_pnl_usdt': pnl['funding_pnl'],
            'fee_cost_usdt': pnl['fee_cost'],
            'total_pnl_usdt': pnl['total_pnl'],
            'detail': {'source': 'binance+gate'},
        }

    def _load_local_pnl_summary(self) -> Dict:
        fee_spot_open = config.get_float('trade.fee.spot_open', 0.00075)
        fee_spot_close = config.get_float('trade.fee.spot_close', 0.00075)
        fee_future_open = config.get_float('trade.fee.future_open', 0.0005)
        fee_future_close = config.get_float('trade.fee.future_close', 0.0005)
        sql = """
            SELECT *
            FROM mi_trade_position
            WHERE status IN ('holding', 'closed')
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            positions = cursor.fetchall()

        binance_realized = gate_realized = funding = 0.0
        binance_fee = gate_fee = 0.0
        for pos in positions:
            spot_open_amount = _float(pos.get('spot_open_amount'))
            future_open_amount = _float(pos.get('future_open_amount'))
            if not future_open_amount:
                future_open_amount = _float(pos.get('future_open_qty')) * _float(pos.get('future_open_price'))
            binance_fee += spot_open_amount * fee_spot_open
            gate_fee += future_open_amount * fee_future_open
            funding += _float(pos.get('funding_total_pnl'))
            if pos.get('status') == 'closed':
                spot_close_amount = _float(pos.get('spot_close_amount'))
                future_qty = _float(pos.get('future_open_qty'))
                future_open_price = _float(pos.get('future_open_price'))
                future_close_price = _float(pos.get('future_close_price'))
                future_close_amount = _float(pos.get('future_close_amount'))
                if not future_close_amount:
                    future_close_amount = future_qty * future_close_price
                binance_realized += spot_close_amount - spot_open_amount
                gate_realized += future_qty * (future_open_price - future_close_price)
                binance_fee += spot_close_amount * fee_spot_close
                gate_fee += future_close_amount * fee_future_close
        fee_cost = -(binance_fee + gate_fee)
        return {
            'binance_realized_pnl': binance_realized,
            'gate_realized_pnl': gate_realized,
            'realized_pnl': binance_realized + gate_realized,
            'funding_pnl': funding,
            'binance_fee_cost': -binance_fee,
            'gate_fee_cost': -gate_fee,
            'fee_cost': fee_cost,
            'total_pnl': binance_realized + gate_realized + funding + fee_cost,
        }

    def _insert_rows(self, rows: List[Dict]):
        sql = """
            INSERT INTO mi_capital_snapshot (
                snapshot_at, exchange, equity_usdt, available_usdt, locked_usdt,
                position_value_usdt, margin_used_usdt, unrealized_pnl_usdt,
                realized_pnl_usdt, funding_pnl_usdt, fee_cost_usdt, total_pnl_usdt, detail
            ) VALUES (
                %(snapshot_at)s, %(exchange)s, %(equity_usdt)s, %(available_usdt)s, %(locked_usdt)s,
                %(position_value_usdt)s, %(margin_used_usdt)s, %(unrealized_pnl_usdt)s,
                %(realized_pnl_usdt)s, %(funding_pnl_usdt)s, %(fee_cost_usdt)s, %(total_pnl_usdt)s, %(detail)s
            )
        """
        payload = []
        for row in rows:
            item = dict(row)
            item['detail'] = json.dumps(row.get('detail') or {}, ensure_ascii=False, default=str)
            payload.append(item)
        with db_manager.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.executemany(sql, payload)
            finally:
                cursor.close()


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _serialize_capital_row(row: Dict) -> Dict:
    result = {}
    for key, value in row.items():
        if hasattr(value, 'strftime'):
            result[key] = value.strftime('%Y-%m-%d %H:%M:%S')
        elif key == 'detail' and isinstance(value, str):
            try:
                result[key] = json.loads(value)
            except Exception:
                result[key] = value
        elif value is None:
            result[key] = None
        else:
            try:
                result[key] = float(value)
            except (TypeError, ValueError):
                result[key] = value
    return result
