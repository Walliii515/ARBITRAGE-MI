# coding: utf-8
"""
更新 Gate.io 永续合约数据到数据库，包含永续合约基本信息和当期费率
全删全进 mi_gate_future_contracts
"""
import sys
import os
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.database import db_manager
from exchange_apis.get_gate_future_contracts import get_futures_contracts, parse_base_asset
from exchange_apis.get_gate_future_tickers import get_futures_tickers
from common.logger import get_logger, log_print

logger = get_logger(__name__)

def merge_contracts_with_tickers(contracts, tickers):
    """
    将合约数据与 Ticker 数据拼接
    
    Args:
        contracts: 合约详情列表
        tickers: Ticker 数据列表
    
    Returns:
        list: 合并后的合约数据列表
    """
    # 将 tickers 转换为字典，以 contract 为 key
    ticker_dict = {t['contract']: t for t in tickers}
    
    merged_data = []
    for contract in contracts:
        contract_name = contract.get('name')
        ticker = ticker_dict.get(contract_name)
        
        # 合并数据
        merged_contract = contract.copy()
        if ticker:
            # 添加 24h 成交量（结算币）
            merged_contract['volume_24h_settle'] = float(ticker.get('volume_24h_settle', 0))
        else:
            merged_contract['volume_24h_settle'] = 0
        
        merged_data.append(merged_contract)
    
    return merged_data


def calculate_24h_funding_rate_value(funding_rate, funding_interval):
    """
    计算24小时资金费率数值（用于存储到数据库）
    返回数值类型，而不是格式化字符串
    """
    if not funding_rate or not funding_interval:
        return None
    try:
        funding_rate = float(funding_rate)
        funding_interval = int(funding_interval)
        periods_per_24h = 86400 / funding_interval
        funding_rate_24h = funding_rate * periods_per_24h
        return funding_rate_24h
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def ensure_base_asset_column():
    """确保 mi_gate_future_contracts 表存在 base_asset 列"""
    sql = """
    SELECT COUNT(*) AS cnt
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'mi_gate_future_contracts'
      AND COLUMN_NAME = 'base_asset'
    """
    alter_sql = """
    ALTER TABLE mi_gate_future_contracts
    ADD COLUMN base_asset VARCHAR(64) NULL
        COMMENT '标的资产，取自 name 下划线前半段，如 AAPLX_USDT -> AAPLX'
    AFTER name
    """
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            if cursor.fetchone()['cnt'] == 0:
                cursor.execute(alter_sql)
                log_print('✓ 已添加列 base_asset')
    except Exception as e:
        logger.exception(f'✗ 检查/添加 base_asset 列失败: {e}')
        raise


def clear_table():
    """清空表数据"""
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute("DELETE FROM mi_gate_future_contracts")
            log_print("✓ 已清空表数据")
    except Exception as e:
        logger.exception(f"✗ 清空表失败: {e}")
        raise


def insert_contracts(contracts):
    """插入合约数据到数据库"""
    insert_sql = """
    INSERT INTO mi_gate_future_contracts (
        name, base_asset, type, quanto_multiplier, order_price_round, order_size_min, order_size_max,
        enable_decimal, leverage_min, leverage_max, maker_fee_rate, 
        taker_fee_rate, funding_rate, funding_rate_24h, funding_interval,
        funding_next_apply, status, funding_rate_limit, 
        volume_24h_settle,
        updated_at
    ) VALUES (
        %(name)s, %(base_asset)s, %(type)s, %(quanto_multiplier)s, %(order_price_round)s, %(order_size_min)s, %(order_size_max)s,
        %(enable_decimal)s, %(leverage_min)s, %(leverage_max)s, %(maker_fee_rate)s,
        %(taker_fee_rate)s, %(funding_rate)s, %(funding_rate_24h)s, %(funding_interval)s,
        %(funding_next_apply)s, %(status)s, %(funding_rate_limit)s,
        %(volume_24h_settle)s,
        %(updated_at)s
    )
    """
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    success_count = 0
    
    try:
        with db_manager.get_cursor() as cursor:
            for contract in contracts:
                # 处理数据
                data = {
                    'name': contract.get('name'),
                    'base_asset': contract.get('base_asset') or parse_base_asset(contract.get('name')),
                    'type': contract.get('type'),
                    'quanto_multiplier': float(contract.get('quanto_multiplier', 0)),
                    'order_price_round': contract.get('order_price_round'),
                    'order_size_min': int(contract.get('order_size_min', 0)),
                    'order_size_max': int(contract.get('order_size_max', 0)),
                    'enable_decimal': 1 if contract.get('enable_decimal') else 0,
                    'leverage_min': int(contract.get('leverage_min', 0)),
                    'leverage_max': int(contract.get('leverage_max', 0)),
                    'maker_fee_rate': float(contract.get('maker_fee_rate', 0)),
                    'taker_fee_rate': float(contract.get('taker_fee_rate', 0)),
                    'funding_rate': float(contract.get('funding_rate', 0)),
                    'funding_rate_24h': calculate_24h_funding_rate_value(
                        contract.get('funding_rate'), 
                        contract.get('funding_interval')
                    ),
                    'funding_interval': int(contract.get('funding_interval', 0)),
                    'funding_next_apply': datetime.fromtimestamp(
                        int(contract.get('funding_next_apply', 0))
                    ).strftime('%Y-%m-%d %H:%M:%S') if contract.get('funding_next_apply') else None,
                    'status': contract.get('status'),
                    'funding_rate_limit': float(contract.get('funding_rate_limit', 0)),
                    'volume_24h_settle': contract.get('volume_24h_settle', 0),
                    'updated_at': now
                }
                
                cursor.execute(insert_sql, data)
                success_count += 1
            
            log_print(f"✓ 成功插入 {success_count} 条合约数据")
            
    except Exception as e:
        logger.exception(f"✗ 插入数据失败: {e}")
        raise


def update_gate_future_contracts():
    """主函数：更新 Gate.io 永续合约数据"""
    log_print("=" * 60)
    log_print("开始更新 Gate.io 永续合约数据")
    log_print("=" * 60)
    
    try:        
        # 1. 获取合约数据
        log_print("\n[1/3] 正在从 Gate.io API 获取合约数据...")
        filtered_contracts = get_futures_contracts()
        
        if not filtered_contracts:
            log_print("✗ 未获取到合约数据")
            return
        
        log_print(f"✓ 获取到 {len(filtered_contracts)} 个符合条件的合约")
        
        # 2. 获取 Ticker 数据
        log_print("\n[2/3] 正在获取 Ticker 数据...")
        tickers = get_futures_tickers()
        
        if not tickers:
            log_print("✗ 未获取到 Ticker 数据")
            return
        
        log_print(f"✓ 获取到 {len(tickers)} 个合约的 Ticker 数据")
        
        # 3. 合并合约数据和 Ticker 数据
        log_print("\n正在合并合约数据和 Ticker 数据...")
        merged_contracts = merge_contracts_with_tickers(filtered_contracts, tickers)
        log_print(f"✓ 成功合并 {len(merged_contracts)} 条数据")
        
        # 4. 确保 base_asset 列存在
        ensure_base_asset_column()

        # 5. 清空表
        log_print("\n[3/3] 正在清空表数据...")
        clear_table()
        
        # 6. 插入新数据
        log_print("\n正在插入新数据...")
        insert_contracts(merged_contracts)
        
        # 7. 完成
        log_print("\n" + "=" * 60)
        log_print(f"✓ 数据更新完成！更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_print(f"✓ 共更新 {len(merged_contracts)} 个合约")
        log_print("=" * 60)
        
    except Exception as e:
        logger.exception(f"\n✗ 更新失败: {e}")
        raise


if __name__ == '__main__':
    update_gate_future_contracts()
