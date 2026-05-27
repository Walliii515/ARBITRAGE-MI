# coding: utf-8
"""
更新 Gate.io 永续合约历史资金费率数据到数据库，用于测算资金费率持续为正适合开仓的阈值
增量更新 mi_gate_future_his_funding_rates
"""
import sys
import os
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.database import db_manager
from exchange_apis.get_gate_future_his_funding_rate import get_futures_funding_rates
from common.tools import calculate_24h_funding_rate
from common.logger import get_logger, log_print

logger = get_logger(__name__)


def get_trading_contracts_from_db():
    """
    从数据库获取所有交易中的合约名称和资金费率间隔
    
    Returns:
        list: 合约列表，每个元素包含 name 和 funding_interval
    """
    query_sql = """
    SELECT name, funding_interval 
    FROM mi_gate_future_contracts 
    WHERE status = 'trading' AND type = 'direct'
    ORDER BY name
    """
    
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(query_sql)
            contracts = cursor.fetchall()
            
            if contracts:
                log_print(f"✓ 从数据库获取到 {len(contracts)} 个交易中的合约")
                return contracts
            else:
                log_print("✗ 数据库中未找到交易中的合约")
                return []
                
    except Exception as e:
        logger.exception(f"✗ 查询数据库失败: {e}")
        return []


def batch_fetch_funding_rates(contracts, batch_size=10):
    """
    分批获取历史资金费率数据
    
    Args:
        contracts: 合约列表，每个元素包含 name 和 funding_interval
        batch_size: 每批查询的合约数量，默认 10（API 限制最多 10 个）
    
    Returns:
        list: 历史资金费率数据列表
    """
    all_funding_rates = []
    
    # 提取合约名称列表
    contract_names = [c['name'] for c in contracts]
    
    # 分批处理
    log_print(f"\n开始分批查询历史资金费率，共 {len(contract_names)} 个合约，每批 {batch_size} 个...")
    
    for i in range(0, len(contract_names), batch_size):
        batch = contract_names[i:i + batch_size]
        log_print(f"\n[{i//batch_size + 1}/{(len(contract_names)-1)//batch_size + 1}] 查询批次 {i//batch_size + 1}...")
        
        # 调用 API 获取历史资金费率
        funding_rates = get_futures_funding_rates(contracts=batch, limit=100)
        
        if funding_rates:
            all_funding_rates.extend(funding_rates)
            log_print(f"  ✓ 本批次获取 {len(funding_rates)} 个合约的历史数据")
        else:
            log_print(f"  ✗ 本批次获取失败")
    
    log_print(f"\n✓ 总共获取 {len(all_funding_rates)} 个合约的历史资金费率数据")
    return all_funding_rates


def insert_funding_rates_incremental(funding_rates_data, contracts_info):
    """
    批量插入历史资金费率数据到数据库（使用 INSERT IGNORE）
    
    Args:
        funding_rates_data: API 返回的历史资金费率数据
        contracts_info: 合约信息列表，用于获取 funding_interval
    """
    # 构建合约名称到 funding_interval 的映射
    contract_interval_map = {c['name']: c['funding_interval'] for c in contracts_info}
    
    # 使用 INSERT IGNORE，利用唯一索引 (contract, timestamp) 自动跳过重复数据
    insert_sql = """
    INSERT IGNORE INTO mi_gate_future_his_funding_rates (
        contract, funding_rate, funding_rate_24h, timestamp, record_time
    ) VALUES (
        %(contract)s, %(funding_rate)s, %(funding_rate_24h)s, %(timestamp)s, %(record_time)s
    )
    """
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    total_inserted = 0
    total_ignored = 0
    batch_data = []
    batch_size = 500  # 每 500 条提交一次
    
    try:
        with db_manager.get_cursor() as cursor:
            # 遍历所有合约的数据
            for rate_data in funding_rates_data:
                contract_name = rate_data.get('contract')
                history_data = rate_data.get('data', [])
                
                if not contract_name or not history_data:
                    continue
                
                # 获取该合约的 funding_interval
                funding_interval = contract_interval_map.get(contract_name, 28800)  # 默认 8 小时
                
                # 批量准备数据
                for record in history_data:
                    rate = record.get('r')
                    timestamp = record.get('t')
                    
                    if not rate or not timestamp:
                        continue
                    
                    # 计算 24 小时资金费率
                    funding_rate_24h = calculate_24h_funding_rate(rate, funding_interval)
                    funding_rate_24h_value = float(funding_rate_24h) if funding_rate_24h != 'N/A' else None
                    
                    data = {
                        'contract': contract_name,
                        'funding_rate': float(rate),
                        'funding_rate_24h': funding_rate_24h_value,
                        'timestamp': int(timestamp),
                        'record_time': now
                    }
                    
                    batch_data.append(data)
                    
                    # 达到批量大小，执行插入
                    if len(batch_data) >= batch_size:
                        inserted = execute_batch_insert(cursor, insert_sql, batch_data)
                        total_inserted += inserted
                        total_ignored += (len(batch_data) - inserted)
                        batch_data = []
            
            # 插入剩余数据
            if batch_data:
                inserted = execute_batch_insert(cursor, insert_sql, batch_data)
                total_inserted += inserted
                total_ignored += (len(batch_data) - inserted)
            
            log_print(f"\n✓ 数据插入完成:")
            log_print(f"  - 新增: {total_inserted} 条")
            log_print(f"  - 跳过（已存在）: {total_ignored} 条")
            
    except Exception as e:
        logger.exception(f"✗ 插入数据失败: {e}")
        raise


def execute_batch_insert(cursor, insert_sql, batch_data):
    """
    执行批量插入
    
    Args:
        cursor: 数据库游标
        insert_sql: INSERT SQL 语句
        batch_data: 批量数据列表
    
    Returns:
        int: 实际插入的记录数
    """
    if not batch_data:
        return 0
    
    try:
        # 执行批量插入
        cursor.executemany(insert_sql, batch_data)
        
        # 返回实际插入的行数（ROW_COUNT）
        return cursor.rowcount
        
    except Exception as e:
        logger.exception(f"✗ 批量插入失败: {e}")
        raise


def update_gate_future_his_funding_rates():
    """主函数：更新 Gate.io 永续合约历史资金费率数据"""
    log_print("=" * 80)
    log_print("开始更新 Gate.io 永续合约历史资金费率数据")
    log_print("=" * 80)
    
    try:
        # 1. 从数据库获取交易中的合约
        log_print("\n[1/3] 正在从数据库获取合约信息...")
        contracts_info = get_trading_contracts_from_db()
        
        if not contracts_info:
            log_print("✗ 未获取到合约信息，退出更新")
            return
        
        # 2. 分批获取历史资金费率数据
        log_print("\n[2/3] 正在从 Gate.io API 获取历史资金费率数据...")
        funding_rates_data = batch_fetch_funding_rates(contracts_info, batch_size=10)
        
        if not funding_rates_data:
            log_print("✗ 未获取到历史资金费率数据")
            return
        
        # 3. 增量插入数据到数据库
        log_print("\n[3/3] 正在增量插入数据到数据库...")
        insert_funding_rates_incremental(funding_rates_data, contracts_info)
        
        # 4. 完成
        log_print("\n" + "=" * 80)
        log_print(f"✓ 历史资金费率数据更新完成！更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_print(f"✓ 共处理 {len(funding_rates_data)} 个合约的历史数据")
        log_print("=" * 80)
        
    except Exception as e:
        logger.exception(f"\n✗ 更新失败: {e}")
        raise


if __name__ == '__main__':
    update_gate_future_his_funding_rates()
