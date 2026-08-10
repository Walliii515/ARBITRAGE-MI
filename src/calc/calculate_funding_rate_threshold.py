# coding: utf-8
"""
统计每个交易对正的24h资金费率的30%分位数、最大值和最小值
"""
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.database import db_manager
from common.logger import get_logger, log_print

logger = get_logger(__name__)


def replace_funding_thresholds(cursor, stats_results) -> int:
    """Replace derived thresholds inside the caller's transaction."""
    if not stats_results:
        return 0
    insert_sql = """
        INSERT INTO mi_gate_future_funding_rate_threshold (
            contract, total_records, positive_count,
            percentile_20, percentile_30, percentile_40,
            min_rate, max_rate,
            update_time
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, NOW()
        )
    """
    insert_data = [(
        data['contract'],
        data['total_records'],
        data['positive_count'],
        data['percentile_20'],
        data['percentile_30'],
        data['percentile_40'],
        data['min_rate'],
        data['max_rate'],
    ) for data in stats_results]
    cursor.execute("DELETE FROM mi_gate_future_funding_rate_threshold")
    cursor.executemany(insert_sql, insert_data)
    return len(insert_data)


def count_positive_funding_rates(
    max_days                # 最大历史天数
):
    """
    统计每个合约正的24h资金费率的30%分位数、最大值和最小值
    
    算法：
    1. 获取所有符合条件的合约
    2. 获取每个合约的历史资金费率记录
    3. 收集所有正的24h资金费率，从小到大排序
    4. 计算30%分位数、最大值和最小值
    """
    log_print("=" * 100)
    log_print("资金费率正负统计")
    log_print("=" * 100)
    log_print("\n参数设置:")
    log_print(f"  - 最大历史天数: {max_days} 天\n")

    try:
        with db_manager.get_cursor() as cursor:
            # 1. 获取所有合约及其历史资金费率（最多max_days天，按时间升序）
            query_sql = """
            SELECT 
                contract,
                funding_rate,
                funding_rate_24h,
                timestamp
            FROM mi_gate_future_his_funding_rates
            WHERE timestamp >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL %s DAY))
            ORDER BY contract, timestamp ASC
            """
            
            cursor.execute(query_sql, (max_days,))
            all_data = cursor.fetchall()
            
            log_print(f"✓ 从数据库获取到 {len(all_data)} 条历史记录\n")
            
            # 2. 按合约分组
            contracts_data = {}
            for row in all_data:
                contract = row['contract']
                if contract not in contracts_data:
                    contracts_data[contract] = []
                contracts_data[contract].append({
                    'funding_rate': float(row['funding_rate']),
                    'funding_rate_24h': float(row['funding_rate_24h']) if row['funding_rate_24h'] else 0,
                    'timestamp': row['timestamp']
                })
            
            log_print(f"共 {len(contracts_data)} 个合约\n")
            
            # 3. 统计每个合约正的24h资金费率的30%分位数、最大值和最小值
            stats_results = []
            
            for contract, records in contracts_data.items():
                # 收集所有正的24h资金费率
                positive_rates = []
                
                for r in records:
                    rate_24h = r['funding_rate_24h']
                    if rate_24h > 0:
                        positive_rates.append(rate_24h)
                
                if not positive_rates:
                    continue
                
                # 从小到大排序
                positive_rates.sort()
                
                # 计算分位数
                n = len(positive_rates)
                
                # 20%分位数
                index_20 = 0.2 * (n - 1)
                lower_20 = int(index_20)
                upper_20 = lower_20 + 1
                if upper_20 >= n:
                    percentile_20 = positive_rates[-1]
                else:
                    weight_20 = index_20 - lower_20
                    percentile_20 = positive_rates[lower_20] * (1 - weight_20) + positive_rates[upper_20] * weight_20
                
                # 30%分位数
                index_30 = 0.3 * (n - 1)
                lower_30 = int(index_30)
                upper_30 = lower_30 + 1
                if upper_30 >= n:
                    percentile_30 = positive_rates[-1]
                else:
                    weight_30 = index_30 - lower_30
                    percentile_30 = positive_rates[lower_30] * (1 - weight_30) + positive_rates[upper_30] * weight_30
                
                # 40%分位数
                index_40 = 0.4 * (n - 1)
                lower_40 = int(index_40)
                upper_40 = lower_40 + 1
                if upper_40 >= n:
                    percentile_40 = positive_rates[-1]
                else:
                    weight_40 = index_40 - lower_40
                    percentile_40 = positive_rates[lower_40] * (1 - weight_40) + positive_rates[upper_40] * weight_40
                
                # 最小值和最大值
                min_rate = positive_rates[0]
                max_rate = positive_rates[-1]
                
                stats_results.append({
                    'contract': contract,
                    'positive_count': n,
                    'total_records': len(records),
                    'percentile_20': percentile_20,
                    'percentile_30': percentile_30,
                    'percentile_40': percentile_40,
                    'min_rate': min_rate,
                    'max_rate': max_rate
                })
            
            # 4. 排序（按30%分位数从高到低）
            stats_results.sort(key=lambda x: x['percentile_30'], reverse=True)
            
            if not stats_results:
                log_print("没有可用统计结果，保留上一版资金费率阈值")
                return

            # DELETE 与后续 INSERT 共用当前事务，读方不会看到空表。
            log_print("原子替换阈值表数据...")
            inserted = replace_funding_thresholds(cursor, stats_results)
            log_print(f"✓ 成功插入 {inserted} 条数据")
            
            log_print("=" * 140)
            log_print(f"✓ 统计完成，共 {len(stats_results)} 个合约有正的24h资金费率\n")
            
    except Exception as e:
        logger.exception(f"\n✗ 分析失败: {e}")


if __name__ == '__main__':
    count_positive_funding_rates(
        max_days=30                # 最多30天历史数据
    )
