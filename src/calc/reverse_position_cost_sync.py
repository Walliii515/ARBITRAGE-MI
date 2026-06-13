# coding: utf-8
"""反向套利持仓实际成本同步。

只读反向策略账号与反向策略表：
- Gate Futures account_book(type=fund) -> 实际资金费
- Binance Cross Margin userAssets.interest -> 当前实际借币利息
- mi_reverse_trade_order.fee_amount_usdt -> 实际成交手续费
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from calc.real_executor import ExchangeConfig, RealExecutor
from calc.reverse_account_monitor import _binance_signed_get
from calc.reverse_trade_store import ensure_reverse_trade_tables
from common.config import config
from common.database import db_manager
from common.logger import get_logger
from common.strategy_accounts import get_binance_credentials, get_gate_futures_credentials

logger = get_logger(__name__)


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_notional(pos: Dict) -> float:
    for key in ('open_amount_usdt', 'spot_open_amount', 'future_open_amount'):
        value = _as_float(pos.get(key))
        if value > 0:
            return value
    qty = _as_float(pos.get('future_open_qty')) or _as_float(pos.get('spot_open_qty')) or _as_float(pos.get('borrow_qty'))
    price = _as_float(pos.get('future_open_price')) or _as_float(pos.get('spot_open_price'))
    return max(qty * price, 0.0)


def _future_weight(pos: Dict) -> float:
    opened = _as_float(pos.get('future_open_qty'))
    closed = _as_float(pos.get('future_close_qty'))
    return max(opened - closed, 0.0) or opened


def _borrow_weight(pos: Dict) -> float:
    return max(_as_float(pos.get('borrow_qty')) - _as_float(pos.get('borrow_repaid_qty')), 0.0)


def _build_reverse_exchange_config() -> ExchangeConfig:
    env = config.get_real_executor_env()
    mainnet = env == 'mainnet'
    binance_creds = get_binance_credentials('reverse', mainnet=mainnet)
    gate_creds = get_gate_futures_credentials('reverse', mainnet=mainnet)
    return ExchangeConfig(
        binance_base_url='https://api1.binance.com' if mainnet else 'https://testnet.binance.vision',
        binance_api_key=binance_creds.api_key,
        binance_api_secret=binance_creds.api_secret,
        gate_base_url='https://api.gateio.ws' if mainnet else 'https://fx-api-testnet.gateio.ws',
        gate_api_key=gate_creds.api_key,
        gate_api_secret=gate_creds.api_secret,
        timeout_sec=config.get_int('real_executor.timeout_sec', 10),
        env=env,
    )


def _load_positions(start_time: datetime) -> List[Dict]:
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM mi_reverse_trade_position
            WHERE opened_at <= NOW()
              AND (closed_at IS NULL OR closed_at >= %s)
            """,
            [start_time],
        )
        return list(cursor.fetchall() or [])


def _fetch_gate_fund_rows(start_time: datetime, end_time: datetime) -> List[Dict]:
    executor = RealExecutor(_build_reverse_exchange_config())
    rows = executor.fetch_gate_futures_account_book(
        int(start_time.timestamp()),
        int(end_time.timestamp()),
    )
    fund_rows: List[Dict] = []
    for row in rows:
        if str(row.get('type') or '').lower() != 'fund':
            continue
        contract = str(row.get('contract') or '').upper()
        if not contract.endswith('_USDT'):
            continue
        settled_ts = int(_as_float(row.get('time')))
        if settled_ts <= 0:
            continue
        fund_rows.append({
            'contract': contract,
            'settled_at': datetime.fromtimestamp(settled_ts).replace(microsecond=0),
            'change': _as_float(row.get('change')),
        })
    fund_rows.sort(key=lambda item: (item['settled_at'], item['contract']))
    return fund_rows


def _calculate_funding_summary(fund_rows: List[Dict], positions: List[Dict]) -> Dict[int, Dict[str, float]]:
    summaries: Dict[int, Dict[str, float]] = defaultdict(lambda: {'funding_pnl_usdt': 0.0, 'funding_pnl_bps': 0.0})
    for fund in fund_rows:
        matched = [
            pos for pos in positions
            if str(pos.get('future_contract') or f"{pos.get('base_asset')}_USDT").upper() == fund['contract']
            and pos.get('opened_at') is not None
            and pos['opened_at'] <= fund['settled_at']
            and (pos.get('closed_at') is None or pos['closed_at'] > fund['settled_at'])
        ]
        if not matched:
            continue
        weights = [(pos, _future_weight(pos)) for pos in matched]
        total_weight = sum(weight for _, weight in weights)
        if total_weight <= 0:
            continue
        for pos, weight in weights:
            pnl = fund['change'] * weight / total_weight
            notional = _position_notional(pos)
            summary = summaries[int(pos['id'])]
            summary['funding_pnl_usdt'] += pnl
            if notional > 0:
                summary['funding_pnl_bps'] += pnl / notional * 10000.0
    return summaries


def _load_fee_summary() -> Dict[int, Dict[str, float]]:
    with db_manager.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT position_id, COALESCE(SUM(fee_amount_usdt), 0) AS fee_usdt
            FROM mi_reverse_trade_order
            WHERE position_id IS NOT NULL
              AND status = 'filled'
              AND fee_amount_usdt IS NOT NULL
            GROUP BY position_id
            """
        )
        rows = cursor.fetchall() or []
    return {int(row['position_id']): {'fee_total_usdt': _as_float(row.get('fee_usdt'))} for row in rows}


def _fetch_margin_interest_by_asset() -> Dict[str, float]:
    account = _binance_signed_get('/sapi/v1/margin/account')
    result: Dict[str, float] = {}
    for item in account.get('userAssets') or []:
        asset = str(item.get('asset') or '').upper()
        interest = _as_float(item.get('interest'))
        if asset and interest > 0:
            result[asset] = interest
    return result


def _calculate_borrow_summary(positions: List[Dict]) -> Dict[int, Dict[str, float]]:
    active_positions = [
        pos for pos in positions
        if str(pos.get('status') or '') in {'holding', 'closing', 'risk', 'desynced'}
        and _borrow_weight(pos) > 0
    ]
    if not active_positions:
        return {}

    try:
        interest_by_asset = _fetch_margin_interest_by_asset()
    except Exception as exc:
        logger.warning('反向持仓借币利息同步失败: %s', exc)
        return {}

    positions_by_asset: Dict[str, List[Dict]] = defaultdict(list)
    for pos in active_positions:
        asset = str(pos.get('borrow_asset') or pos.get('base_asset') or '').upper()
        if asset:
            positions_by_asset[asset].append(pos)

    summaries: Dict[int, Dict[str, float]] = {}
    for asset, asset_positions in positions_by_asset.items():
        total_interest_qty = interest_by_asset.get(asset, 0.0)
        total_weight = sum(_borrow_weight(pos) for pos in asset_positions)
        if total_interest_qty <= 0 or total_weight <= 0:
            continue
        for pos in asset_positions:
            allocated_interest_qty = total_interest_qty * _borrow_weight(pos) / total_weight
            price = _as_float(pos.get('spot_open_price')) or _as_float(pos.get('future_open_price'))
            interest_usdt = allocated_interest_qty * price
            notional = _position_notional(pos)
            summaries[int(pos['id'])] = {
                'borrow_interest_usdt': interest_usdt,
                'borrow_interest_bps': interest_usdt / notional * 10000.0 if notional > 0 else 0.0,
            }
    return summaries


def refresh_reverse_position_costs(lookback_days: Optional[int] = None) -> Dict[str, int]:
    """同步反向持仓真实资金费、借币费、手续费汇总。"""
    ensure_reverse_trade_tables()
    days = lookback_days or config.get_int('trade.position.funding_sync_lookback_days', 7)
    days = max(min(int(days or 7), 30), 1)
    end_time = datetime.now()
    start_time = end_time - timedelta(days=days)

    positions = _load_positions(start_time)
    if not positions:
        return {'positions': 0, 'funding_rows': 0, 'updated': 0}

    fee_summary = _load_fee_summary()
    try:
        fund_rows = _fetch_gate_fund_rows(start_time, end_time)
        funding_summary = _calculate_funding_summary(fund_rows, positions)
    except Exception as exc:
        logger.warning('反向持仓资金费同步失败: %s', exc)
        fund_rows = []
        funding_summary = {}
    borrow_summary = _calculate_borrow_summary(positions)

    updated = 0
    with db_manager.get_connection() as conn:
        with conn.cursor() as cursor:
            for pos in positions:
                position_id = int(pos['id'])
                notional = _position_notional(pos)
                fee_usdt = fee_summary.get(position_id, {}).get('fee_total_usdt', _as_float(pos.get('fee_total_usdt')))
                fee_bps = -fee_usdt / notional * 10000.0 if notional > 0 else 0.0
                funding = funding_summary.get(position_id, {'funding_pnl_usdt': 0.0, 'funding_pnl_bps': 0.0})
                borrow = borrow_summary.get(position_id, {
                    'borrow_interest_usdt': _as_float(pos.get('borrow_interest_usdt')),
                    'borrow_interest_bps': _as_float(pos.get('borrow_interest_bps')),
                })
                cursor.execute(
                    """
                    UPDATE mi_reverse_trade_position
                    SET fee_total_usdt = %s,
                        fee_total_bps = %s,
                        funding_pnl_usdt = %s,
                        funding_pnl_bps = %s,
                        borrow_interest_usdt = %s,
                        borrow_interest_bps = %s
                    WHERE id = %s
                    """,
                    (
                        round(fee_usdt, 8),
                        round(fee_bps, 4),
                        round(funding['funding_pnl_usdt'], 8),
                        round(funding['funding_pnl_bps'], 4),
                        round(borrow['borrow_interest_usdt'], 8),
                        round(borrow['borrow_interest_bps'], 4),
                        position_id,
                    ),
                )
                updated += cursor.rowcount
        conn.commit()

    logger.info(
        '反向持仓成本同步完成 | positions=%s | fund_rows=%s | updated=%s',
        len(positions),
        len(fund_rows),
        updated,
    )
    return {'positions': len(positions), 'funding_rows': len(fund_rows), 'updated': updated}
