# coding: utf-8
"""
统计每个交易对正的24h资金费率的30%分位数、最大值和最小值
"""
import sys
import os
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.database import db_manager
from common.logger import get_logger, log_print

logger = get_logger(__name__)


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
    log_print(f"\n参数设置:")
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
                
                # 获取最新数据
                latest_record = records[-1]
                current_rate = latest_record['funding_rate']
                current_rate_24h = latest_record['funding_rate_24h']
                latest_time = latest_record['timestamp']
                
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
            
            # 6. 清空表数据
            log_print("清空表数据...")
            cursor.execute("TRUNCATE TABLE mi_gate_future_funding_rate_threshold")
            log_print("✓ 表数据已清空")
            
            # 7. 插入数据
            log_print(f"插入 {len(stats_results)} 条数据...")
            
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
            
            insert_data = []
            for data in stats_results:
                insert_data.append((
                    data['contract'],
                    data['total_records'],
                    data['positive_count'],
                    data['percentile_20'],
                    data['percentile_30'],
                    data['percentile_40'],
                    data['min_rate'],
                    data['max_rate']
                ))
            
            cursor.executemany(insert_sql, insert_data)
            log_print(f"✓ 成功插入 {len(insert_data)} 条数据")
            
            # 8. 输出结果
            log_print("=" * 140)
            log_print(f"✓ 统计完成，共 {len(stats_results)} 个合约有正的24h资金费率\n")
            
            if not stats_results:
                log_print("没有找到符合条件的合约")
                return
            
            # 打印表头
            # print(f"{'排名':<5} {'合约':<25} {'正费率次数':<10} {'20%分位数':<12} "
            #       f"{'30%分位数':<12} {'40%分位数':<12} "
            #       f"{'最小值':<12} {'最大值':<12} {'当前24h费率':<12}")
            # print("-" * 140)
            #
            # for i, data in enumerate(stats_results, 1):
            #     print(f"{i:<5} {data['contract']:<25} "
            #           f"{data['positive_count']:>8} "
            #           f"{data['percentile_20']*100:>10.4f}% "
            #           f"{data['percentile_30']*100:>10.4f}% "
            #           f"{data['percentile_40']*100:>10.4f}% "
            #           f"{data['min_rate']*100:>10.4f}% "
            #           f"{data['max_rate']*100:>10.4f}% "
            #           f"{data['current_rate_24h']*100:>10.4f}%")
            #
            # print("-" * 140)
            
            # 9. 导出到CSV文件
            import csv
            
            # 项目根目录
            # project_root = os.path.join(os.path.dirname(__file__), '..', '..')
            # output_file = os.path.join(project_root, 'funding_rate_24h_percentile_stats.csv')
            #
            # with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
            #     writer = csv.writer(f)
            #
            #     # 写入表头
            #     writer.writerow([
            #         '排名', '合约', '总记录数', '正24h费率次数',
            #         '20%分位数', '30%分位数', '40%分位数',
            #         '最小值', '最大值', '当前24h费率', '最新时间'
            #     ])
            #
            #     # 写入数据
            #     for i, data in enumerate(stats_results, 1):
            #         latest_time_str = datetime.fromtimestamp(data['latest_time']).strftime('%Y-%m-%d %H:%M:%S')
            #
            #         writer.writerow([
            #             i,
            #             data['contract'],
            #             data['total_records'],
            #             data['positive_count'],
            #             f"{data['percentile_20']*100:.4f}%",
            #             f"{data['percentile_30']*100:.4f}%",
            #             f"{data['percentile_40']*100:.4f}%",
            #             f"{data['min_rate']*100:.4f}%",
            #             f"{data['max_rate']*100:.4f}%",
            #             f"{data['current_rate_24h']*100:.4f}%",
            #             latest_time_str
            #         ])
            #
            # print(f"\n✓ 完整结果已导出到: {output_file}")
            # print(f"✓ 共导出 {len(stats_results)} 个合约的数据")
            
    except Exception as e:
        logger.exception(f"\n✗ 分析失败: {e}")


if __name__ == '__main__':
    count_positive_funding_rates(
        max_days=30                # 最多30天历史数据
    )
