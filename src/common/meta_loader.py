"""
元数据加载模块

从数据库加载合约元数据和现货元数据，供 orderbook_server 和 virtual/real_executor_service 等多个服务共用。
"""
from datetime import timedelta
from typing import Dict

from common.database import db_manager
from common.logger import get_logger

logger = get_logger(__name__)


def fetch_contract_meta() -> Dict[str, Dict]:
    """
    从 mi_gate_future_contracts 加载合约元数据，按 base_asset 索引

    Returns:
        base_asset -> {quanto_multiplier, order_price_round, order_size_min,
                       enable_decimal, funding_rate, funding_rate_24h,
                       funding_interval, funding_next_apply, funding_last_apply, ...}
    """
    sql = """
        SELECT base_asset, quanto_multiplier, order_price_round, order_size_min,
               enable_decimal, funding_rate, funding_rate_24h, funding_interval,
               volume_24h_settle, funding_next_apply, maintenance_rate,
               maker_fee_rate, taker_fee_rate
        FROM mi_gate_future_contracts
    """
    result = {}
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            for row in rows:
                funding_interval = int(row['funding_interval']) if row.get('funding_interval') is not None else None
                funding_next_apply = row.get('funding_next_apply')
                funding_last_apply = (
                    funding_next_apply - timedelta(seconds=funding_interval)
                    if funding_next_apply and funding_interval
                    else None
                )
                result[row['base_asset']] = {
                    'quanto_multiplier': float(row['quanto_multiplier']),
                    'order_price_round': float(row['order_price_round']) if row.get('order_price_round') is not None else None,
                    'order_size_min': int(row['order_size_min']),
                    'enable_decimal': bool(row['enable_decimal']),
                    'funding_rate': float(row['funding_rate']) if row.get('funding_rate') is not None else None,
                    'funding_rate_24h': float(row['funding_rate_24h']) if row.get('funding_rate_24h') is not None else None,
                    'funding_interval': funding_interval,
                    'volume_24h_settle': float(row['volume_24h_settle']) if row.get('volume_24h_settle') is not None else None,
                    'funding_next_apply': funding_next_apply,
                    'funding_last_apply': funding_last_apply,
                    'maintenance_rate': float(row['maintenance_rate']) if row.get('maintenance_rate') is not None else None,
                    'maker_fee_rate': float(row['maker_fee_rate']) if row.get('maker_fee_rate') is not None else None,
                    'taker_fee_rate': float(row['taker_fee_rate']) if row.get('taker_fee_rate') is not None else None,
                }
    except Exception as e:
        logger.error(f'加载合约元数据失败: {e}', exc_info=True)
    return result


def fetch_spot_meta() -> Dict[str, Dict]:
    """
    从 mi_binance_spot_info 加载现货元数据，按 base_asset 索引

    Returns:
        base_asset -> {step_size, tick_size, min_qty, quote_volume}
    """
    sql = "SELECT base_asset, step_size, tick_size, min_qty, min_notional, quote_volume FROM mi_binance_spot_info"
    result = {}
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            for row in rows:
                result[row['base_asset']] = {
                    'step_size': float(row['step_size']),
                    'tick_size': float(row['tick_size']),
                    'min_qty': float(row['min_qty']),
                    'min_notional': float(row['min_notional']) if row.get('min_notional') is not None else None,
                    'quote_volume': float(row['quote_volume']) if row.get('quote_volume') is not None else None,
                }
    except Exception as e:
        logger.error(f'加载现货元数据失败: {e}', exc_info=True)
    return result


def fetch_asset_tier_meta() -> Dict[str, str]:
    """
    从 mi_base_asset 加载策略分层，按 base_asset 索引。

    Returns:
        base_asset -> strategy_tier ('A'/'B'/'C')
    """
    sql = "SELECT base_asset, COALESCE(strategy_tier, 'C') AS strategy_tier FROM mi_base_asset"
    result = {}
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            for row in rows:
                base_asset = row.get('base_asset')
                if base_asset:
                    result[base_asset.strip().upper()] = (row.get('strategy_tier') or 'C').strip().upper()
    except Exception as e:
        logger.error(f'加载标的策略分层失败: {e}', exc_info=True)
    return result
