# coding: utf-8
"""
真实账户资金快照。

按分钟级频率读取 Binance 现货和 Gate 永续账户资金，并从本地策略持仓/订单
聚合已实现收益、资金费和手续费，写入 mi_capital_snapshot；开仓逻辑读取最新缓存即可。
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from calc.reconciliation import build_exchange_config
from calc.real_executor import RealExecutor
from common.config import config
from common.database import db_manager
from common.meta_loader import fetch_contract_meta, fetch_spot_meta

@dataclass
class AccountCapitalConfig:
    retention_days: int = 30
    pnl_lookback_days: int = 90
    binance_margin_enabled: bool = True
    binance_margin_warning_level: float = 3.0
    binance_margin_min_open_level: float = 2.5


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
            pnl_lookback_days=config.get_int('account_capital.pnl_lookback_days', 90),
            binance_margin_enabled=config.get_bool('account_capital.binance_margin.enabled', True),
            binance_margin_warning_level=config.get_float(
                'account_capital.binance_margin.warning_margin_level',
                3.0,
            ),
            binance_margin_min_open_level=config.get_float(
                'account_capital.binance_margin.min_open_margin_level',
                2.5,
            ),
        ),
    )


class AccountCapitalSnapshotter:
    """账户资金快照器。"""

    def __init__(self, executor: RealExecutor, cfg: Optional[AccountCapitalConfig] = None):
        self.executor = executor
        self.cfg = cfg or AccountCapitalConfig()

    def run_once(self, strategy_pnl_summary: Optional[Dict] = None) -> Dict:
        snapshot_at = datetime.now()
        pnl = self._load_exchange_pnl_summary(snapshot_at, strategy_pnl_summary)
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
                'margin': _capital_detail(binance).get('binance_cross_margin'),
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
        bnb_fee_asset = _build_bnb_fee_asset_detail(balances, prices)
        available = float(usdt.get('free') or 0)
        locked = float(usdt.get('locked') or 0)
        equity = available + locked + spot_value
        margin_detail = self._build_binance_margin_detail()
        return {
            'snapshot_at': snapshot_at,
            'exchange': 'binance',
            'equity_usdt': equity,
            'available_usdt': available,
            'locked_usdt': locked,
            'position_value_usdt': spot_value,
            'margin_used_usdt': 0.0,
            'unrealized_pnl_usdt': pnl.get('binance_spot_floating_pnl', 0.0),
            'realized_pnl_usdt': pnl['binance_realized_pnl'],
            'funding_pnl_usdt': 0.0,
            'fee_cost_usdt': pnl['binance_fee_cost'],
            'total_pnl_usdt': pnl['binance_realized_pnl'] + pnl['binance_fee_cost'],
            'detail': {
                'source': 'exchange_api',
                'balances': balances,
                'prices': prices,
                'pnl_window': pnl.get('window'),
                'binance_spot_realized': pnl.get('binance_spot_realized'),
                'pnl_source': pnl.get('source'),
                'spot_floating_pnl': pnl.get('binance_spot_floating_pnl', 0.0),
                'bnb_fee_asset': bnb_fee_asset,
                'binance_cross_margin': margin_detail,
            },
        }

    def _build_gate_row(self, snapshot_at: datetime, pnl: Dict) -> Dict:
        account = self.executor.fetch_gate_futures_account()
        available = _float(account.get('available'))
        total = _float(account.get('total'))
        account_unrealized = _float(account.get('unrealised_pnl') or account.get('unrealized_pnl'))
        position_margin = _float(account.get('position_margin'))
        isolated_position_margin = _float(account.get('isolated_position_margin'))
        position_initial_margin = _float(account.get('position_initial_margin'))
        order_margin = _float(account.get('order_margin'))
        margin_used = max(position_margin, isolated_position_margin, position_initial_margin) + order_margin
        if _has_value(account.get('total')):
            # Gate futures `total` is wallet balance including isolated margin, but it does
            # not include unrealized PnL. Add it here so equity matches net account value.
            equity = total + account_unrealized
            equity_formula = 'gate_total_plus_unrealized_pnl'
        else:
            equity = available + margin_used + account_unrealized
            equity_formula = 'available_plus_margin_plus_unrealized_pnl'
        strategy_future_floating = pnl.get('gate_future_floating_pnl', account_unrealized)
        return {
            'snapshot_at': snapshot_at,
            'exchange': 'gate',
            'equity_usdt': equity,
            'available_usdt': available,
            'locked_usdt': order_margin,
            'position_value_usdt': margin_used,
            'margin_used_usdt': margin_used,
            'unrealized_pnl_usdt': strategy_future_floating,
            'realized_pnl_usdt': pnl['gate_realized_pnl'],
            'funding_pnl_usdt': pnl['funding_pnl'],
            'fee_cost_usdt': pnl['gate_fee_cost'],
            'total_pnl_usdt': pnl['gate_realized_pnl'] + pnl['funding_pnl'] + pnl['gate_fee_cost'],
            'detail': {
                'source': 'exchange_api',
                'account': account,
                'raw_total_usdt': total,
                'account_unrealized_pnl': account_unrealized,
                'strategy_future_floating_pnl': strategy_future_floating,
                'equity_formula': equity_formula,
                'pnl_window': pnl.get('window'),
                'pnl_source': pnl.get('source'),
                'gate_strategy_realized': pnl.get('gate_strategy_realized'),
            },
        }

    def _build_total_row(self, snapshot_at: datetime, binance: Dict, gate: Dict, pnl: Dict) -> Dict:
        realized_pnl = _float(binance.get('realized_pnl_usdt')) + _float(gate.get('realized_pnl_usdt'))
        funding_pnl = _float(binance.get('funding_pnl_usdt')) + _float(gate.get('funding_pnl_usdt'))
        fee_cost = _float(binance.get('fee_cost_usdt')) + _float(gate.get('fee_cost_usdt'))
        return {
            'snapshot_at': snapshot_at,
            'exchange': 'total',
            'equity_usdt': binance['equity_usdt'] + gate['equity_usdt'],
            'available_usdt': binance['available_usdt'] + gate['available_usdt'],
            'locked_usdt': binance['locked_usdt'] + gate['locked_usdt'],
            'position_value_usdt': binance['position_value_usdt'] + gate['position_value_usdt'],
            'margin_used_usdt': gate['margin_used_usdt'],
            'unrealized_pnl_usdt': pnl.get(
                'floating_pnl',
                gate['unrealized_pnl_usdt'] + binance['unrealized_pnl_usdt'],
            ),
            'realized_pnl_usdt': realized_pnl,
            'funding_pnl_usdt': funding_pnl,
            'fee_cost_usdt': fee_cost,
            'total_pnl_usdt': realized_pnl + funding_pnl + fee_cost,
            'detail': {
                'source': 'exchange_api',
                'components': 'binance/gate account equity + local strategy positions/orders PnL',
                'equity_formula': 'binance_equity_plus_gate_equity',
                'binance_cross_margin': _capital_detail(binance).get('binance_cross_margin'),
                'pnl_window': pnl.get('window'),
                'pnl_source': pnl.get('source'),
            },
        }

    def _build_binance_margin_detail(self) -> Dict:
        if not self.cfg.binance_margin_enabled:
            return {'enabled': False}
        try:
            account = self.executor.fetch_binance_cross_margin_account()
        except AttributeError:
            return {'enabled': False, 'error': 'executor_missing_cross_margin_reader'}
        except Exception as exc:
            return {'enabled': True, 'error': str(exc)[:300]}

        assets = _margin_assets_by_symbol(account)
        usdt = assets.get('USDT', _empty_margin_asset('USDT'))
        margin_level = _float_or_none(account.get('marginLevel'))
        warning_level = float(self.cfg.binance_margin_warning_level or 0)
        min_open_level = float(self.cfg.binance_margin_min_open_level or 0)
        open_allowed = margin_level is not None and (
            min_open_level <= 0 or margin_level >= min_open_level
        )
        if margin_level is None:
            status = 'unknown'
        elif min_open_level > 0 and margin_level < min_open_level:
            status = 'blocked'
        elif warning_level > 0 and margin_level < warning_level:
            status = 'warning'
        else:
            status = 'ok'

        return {
            'enabled': True,
            'status': status,
            'open_allowed': open_allowed,
            'marginLevel': margin_level,
            'warning_margin_level': warning_level,
            'min_open_margin_level': min_open_level,
            'borrowEnabled': account.get('borrowEnabled'),
            'tradeEnabled': account.get('tradeEnabled'),
            'totalAssetOfBtc': _float_or_none(account.get('totalAssetOfBtc')),
            'totalLiabilityOfBtc': _float_or_none(account.get('totalLiabilityOfBtc')),
            'totalNetAssetOfBtc': _float_or_none(account.get('totalNetAssetOfBtc')),
            'USDT': usdt,
            'nonzero_assets': _nonzero_margin_assets(account),
        }

    def _load_exchange_pnl_summary(
        self,
        snapshot_at: datetime,
        strategy_pnl_summary: Optional[Dict] = None,
    ) -> Dict:
        start_at = snapshot_at - timedelta(days=max(int(self.cfg.pnl_lookback_days or 1), 1))
        strategy_summary = self._load_strategy_pnl_summary(start_at, snapshot_at)
        if strategy_pnl_summary:
            strategy_summary.update({
                key: strategy_pnl_summary[key]
                for key in (
                    'realized_pnl',
                    'gate_realized_pnl',
                    'funding_pnl',
                    'fee_cost',
                    'binance_spot_floating_pnl',
                    'gate_future_floating_pnl',
                    'floating_pnl',
                    'position_count',
                    'closed_count',
                    'pnl_rows',
                    'missing_realtime_rows',
                )
                if key in strategy_pnl_summary
            })
        binance_spot = strategy_summary['binance_spot_realized']
        binance_fee = strategy_summary['binance_fee_cost']
        fee_cost = strategy_summary['fee_cost']
        realized_pnl = strategy_summary['realized_pnl']
        funding_pnl = strategy_summary['funding_pnl']
        return {
            'binance_realized_pnl': binance_spot['realized_pnl'],
            'gate_realized_pnl': strategy_summary['gate_realized_pnl'],
            'realized_pnl': realized_pnl,
            'funding_pnl': funding_pnl,
            'binance_spot_floating_pnl': strategy_summary.get('binance_spot_floating_pnl', 0.0),
            'gate_future_floating_pnl': strategy_summary.get('gate_future_floating_pnl', 0.0),
            'floating_pnl': strategy_summary.get('floating_pnl', 0.0),
            'binance_fee_cost': binance_fee,
            'gate_fee_cost': strategy_summary['gate_fee_cost'],
            'fee_cost': fee_cost,
            'total_pnl': realized_pnl + funding_pnl + fee_cost,
            'binance_spot_realized': binance_spot,
            'gate_strategy_realized': strategy_summary['gate_strategy_realized'],
            'source': 'local_strategy',
            'window': {
                'from': start_at.strftime('%Y-%m-%d %H:%M:%S'),
                'to': snapshot_at.strftime('%Y-%m-%d %H:%M:%S'),
                'lookback_days': self.cfg.pnl_lookback_days,
            },
            'realtime_strategy_summary': strategy_pnl_summary,
        }

    def _load_strategy_pnl_summary(self, start_at: datetime, end_at: datetime) -> Dict:
        """
        策略收益口径：只统计系统本地持仓与订单，避免 Gate/Binance 账户流水混入
        手动交易、旧策略交易或当前系统之外的交易。
        """
        positions = self._load_strategy_positions(start_at, end_at)
        position_ids = [int(pos['id']) for pos in positions if pos.get('id') is not None]
        fee_summary = self._load_strategy_order_fee_summary(position_ids)

        funding_pnl = sum(_float(pos.get('funding_total_pnl')) for pos in positions)
        binance_spot_realized = {
            'closed_count': 0,
            'open_amount': 0.0,
            'close_amount': 0.0,
            'realized_pnl': 0.0,
        }
        strategy_realized = 0.0
        for pos in positions:
            if pos.get('status') != 'closed':
                continue
            spot_open = _float(pos.get('spot_open_amount'))
            spot_close = _float(pos.get('spot_close_amount'))
            spot_pnl = spot_close - spot_open
            binance_spot_realized['closed_count'] += 1
            binance_spot_realized['open_amount'] += spot_open
            binance_spot_realized['close_amount'] += spot_close
            binance_spot_realized['realized_pnl'] += spot_pnl
            strategy_realized += self._position_strategy_realized_pnl(pos)

        gate_realized = strategy_realized - binance_spot_realized['realized_pnl']
        return {
            'position_count': len(positions),
            'closed_count': binance_spot_realized['closed_count'],
            'realized_pnl': strategy_realized,
            'gate_realized_pnl': gate_realized,
            'funding_pnl': funding_pnl,
            'binance_fee_cost': fee_summary['binance_fee_cost'],
            'gate_fee_cost': fee_summary['gate_fee_cost'],
            'fee_cost': fee_summary['fee_cost'],
            'binance_spot_realized': binance_spot_realized,
            'gate_strategy_realized': {
                'realized_pnl': gate_realized,
                'derived_from': 'strategy_realized_pnl - binance_spot_realized_pnl',
            },
        }

    def _load_strategy_positions(self, start_at: datetime, end_at: datetime) -> List[Dict]:
        sql = """
            SELECT id, status, opened_at, closed_at,
                   spot_open_amount, spot_close_amount,
                   future_open_qty, future_open_price, future_close_amount,
                   open_spread_bps, close_spread_bps, funding_total_pnl
            FROM mi_trade_position
            WHERE opened_at <= %s
              AND (closed_at IS NULL OR closed_at >= %s)
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (end_at, start_at))
            return cursor.fetchall()

    def _load_strategy_order_fee_summary(self, position_ids: List[int]) -> Dict:
        if not position_ids:
            return {'binance_fee_cost': 0.0, 'gate_fee_cost': 0.0, 'fee_cost': 0.0}
        placeholders = ','.join(['%s'] * len(position_ids))
        sql = f"""
            SELECT market_type,
                   SUM(CASE
                         WHEN fee_amount_usdt IS NOT NULL THEN fee_amount_usdt
                         ELSE COALESCE(exec_amount, target_amount, 0) * COALESCE(fee_rate, 0)
                       END) AS fee_amount
            FROM mi_trade_order
            WHERE position_id IN ({placeholders})
              AND status = 'executed'
            GROUP BY market_type
        """
        spot_fee = future_fee = 0.0
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, position_ids)
            for row in cursor.fetchall():
                market_type = str(row.get('market_type') or '').lower()
                amount = _float(row.get('fee_amount'))
                if market_type == 'spot':
                    spot_fee += amount
                elif market_type == 'future':
                    future_fee += amount
        binance_fee_cost = -spot_fee
        gate_fee_cost = -future_fee
        return {
            'binance_fee_cost': binance_fee_cost,
            'gate_fee_cost': gate_fee_cost,
            'fee_cost': binance_fee_cost + gate_fee_cost,
        }

    def _position_strategy_realized_pnl(self, pos: Dict) -> float:
        open_spread = _float_or_none(pos.get('open_spread_bps'))
        close_spread = _float_or_none(pos.get('close_spread_bps'))
        spot_open_amount = _float_or_none(pos.get('spot_open_amount'))
        if open_spread is not None and close_spread is not None and spot_open_amount is not None:
            return (open_spread - close_spread) / 10000.0 * spot_open_amount

        spot_open = _float(pos.get('spot_open_amount'))
        spot_close = _float(pos.get('spot_close_amount'))
        future_open = _float(pos.get('future_open_qty')) * _float(pos.get('future_open_price'))
        future_close = _float(pos.get('future_close_amount'))
        return (spot_close - spot_open) + (future_open - future_close)

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


def _float_or_none(value) -> Optional[float]:
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_value(value) -> bool:
    return value is not None and str(value).strip() != ''


def _empty_margin_asset(asset: str) -> Dict:
    return {
        'asset': asset,
        'free': 0.0,
        'locked': 0.0,
        'borrowed': 0.0,
        'interest': 0.0,
        'netAsset': 0.0,
    }


def _margin_assets_by_symbol(margin_account: Dict) -> Dict[str, Dict]:
    result: Dict[str, Dict] = {}
    for item in margin_account.get('userAssets') or []:
        asset = str(item.get('asset') or '').upper()
        if not asset:
            continue
        result[asset] = {
            'asset': asset,
            'free': _float(item.get('free')),
            'locked': _float(item.get('locked')),
            'borrowed': _float(item.get('borrowed')),
            'interest': _float(item.get('interest')),
            'netAsset': _float(item.get('netAsset')),
        }
    return result


def _nonzero_margin_assets(margin_account: Dict) -> List[Dict]:
    assets = []
    for item in _margin_assets_by_symbol(margin_account).values():
        if any(
            abs(float(item.get(key) or 0)) > 1e-12
            for key in ('free', 'locked', 'borrowed', 'interest', 'netAsset')
        ):
            assets.append(item)
    return sorted(assets, key=lambda row: row['asset'])


def _capital_detail(row: Dict) -> Dict:
    detail = row.get('detail')
    if isinstance(detail, dict):
        return detail
    if isinstance(detail, str):
        try:
            parsed = json.loads(detail)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _build_bnb_fee_asset_detail(balances: List[Dict], prices: Dict) -> Dict:
    bnb = next((item for item in balances if str(item.get('asset') or '').upper() == 'BNB'), None) or {}
    free = _float(bnb.get('free'))
    locked = _float(bnb.get('locked'))
    total = _float(bnb.get('total'))
    price = _float(prices.get('BNB'))
    return {
        'asset': 'BNB',
        'free': free,
        'locked': locked,
        'total': total,
        'price_usdt': price,
        'free_value_usdt': free * price,
        'total_value_usdt': total * price,
    }


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
