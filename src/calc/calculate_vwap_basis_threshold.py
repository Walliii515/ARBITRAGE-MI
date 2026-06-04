#!/usr/bin/env python3
# coding: utf-8
"""
VWAP基差分位阈值 - 每日批量统计分析脚本

从 mi_vwap_basis_snapshot 读取历史快照数据，
按 base_asset 分组计算统计指标（max, min, mean, std, 各分位值），
写入 mi_vwap_basis_threshold 表。

用法:
    python src/calc/calculate_vwap_basis_threshold.py
    python src/calc/calculate_vwap_basis_threshold.py --lookback-days 7 --percentile p20
"""
import argparse
import sys
import os
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.database import db_manager
from common.config import config
from common.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


def calculate_percentile(sorted_values: list, percentile: int) -> float:
    """
    计算分位值（线性插值法）

    Args:
        sorted_values: 已升序排列的数值列表
        percentile: 分位数(0-100)

    Returns:
        分位值
    """
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_values[0]

    # 使用线性插值
    pos = (percentile / 100.0) * (n - 1)
    lower = int(pos)
    upper = lower + 1
    weight = pos - lower

    if upper >= n:
        return sorted_values[-1]

    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def fetch_valid_base_assets() -> list:
    """
    从 mi_base_asset 获取标的列表。

    Returns:
        base_asset 列表
    """
    sql = """
        SELECT DISTINCT UPPER(TRIM(base_asset)) AS base_asset
        FROM mi_base_asset
        WHERE base_asset IS NOT NULL
          AND TRIM(base_asset) <> ''
        ORDER BY base_asset
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        return [row['base_asset'] for row in cursor.fetchall()]


def fetch_asset_snapshot_data(base_asset: str, start_time: str) -> list:
    """
    读取单个标的指定时间范围内的快照数据

    Args:
        base_asset: 标的资产
        start_time: 起始时间字符串

    Returns:
        该标的的快照行列表
    """
    sql = """
        SELECT open_vwap_basis_bps, close_vwap_basis_bps, open_coverage
        FROM mi_vwap_basis_snapshot
        WHERE base_asset = %s
          AND snapshot_time >= %s
          AND open_vwap_basis_bps IS NOT NULL
    """
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, (base_asset, start_time))
        return cursor.fetchall()


def compute_open_statistics(values: list) -> dict:
    """
    计算开仓基差统计指标

    分位含义（从大到小视角）：
    - open_basis_p10: 从大到小前10%的分界点（升序第90分位）
    - open_basis_p20: 从大到小前20%的分界点（升序第80分位）
    即 open_basis_pX 表示历史上只有 X% 的时刻基差高于此值。
    基差越大越有利（后续收敛空间更大），使用 pX 作为阈值意味着
    只在基差处于历史 top X% 时才开仓。

    Args:
        values: 开仓基差数值列表

    Returns:
        统计结果字典
    """
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mean_val = sum(sorted_vals) / n
    variance = sum((x - mean_val) ** 2 for x in sorted_vals) / n
    std_val = variance ** 0.5

    return {
        'open_sample_count': n,
        'open_basis_max': sorted_vals[-1],
        'open_basis_min': sorted_vals[0],
        'open_basis_mean': round(mean_val, 4),
        'open_basis_std': round(std_val, 4),
        # 从大到小 top X% 的分界点 = 升序第 (100-X) 分位
        'open_basis_p10': round(calculate_percentile(sorted_vals, 90), 4),
        'open_basis_p20': round(calculate_percentile(sorted_vals, 80), 4),
        'open_basis_p30': round(calculate_percentile(sorted_vals, 70), 4),
        'open_basis_p40': round(calculate_percentile(sorted_vals, 60), 4),
    }


def compute_close_statistics(values: list) -> dict:
    """
    计算平仓基差统计指标

    分位含义（从小到大视角）：
    - close_basis_p10: 升序第10分位，历史上只有10%的时刻基差低于此值
    - close_basis_p20: 升序第20分位，历史上只有20%的时刻基差低于此值
    即 close_basis_pX 表示历史上只有 X% 的时刻平仓基差低于此值。
    平仓时基差越小越有利（说明价差已收敛），使用 pX 作为阈值意味着
    只在基差处于历史最低 X% 时才平仓。

    Args:
        values: 平仓基差数值列表

    Returns:
        统计结果字典
    """
    sorted_vals = sorted(values)  # 由小到大排序
    n = len(sorted_vals)
    mean_val = sum(sorted_vals) / n
    variance = sum((x - mean_val) ** 2 for x in sorted_vals) / n
    std_val = variance ** 0.5

    return {
        'close_sample_count': n,
        'close_basis_max': sorted_vals[-1],
        'close_basis_min': sorted_vals[0],
        'close_basis_mean': round(mean_val, 4),
        'close_basis_std': round(std_val, 4),
        # 由小到大直接取分位点
        'close_basis_p10': round(calculate_percentile(sorted_vals, 10), 4),
        'close_basis_p20': round(calculate_percentile(sorted_vals, 20), 4),
        'close_basis_p30': round(calculate_percentile(sorted_vals, 30), 4),
        'close_basis_p40': round(calculate_percentile(sorted_vals, 40), 4),
    }


def run_analysis(lookback_days: int, coverage_filter: float = 1.0):
    """
    执行分析并写入阈值表（逐标的流式处理，避免全表加载）

    纯统计计算，写入所有分位值（open_basis_p10~p40 + close_basis_p10~p40）。
    使用哪个分位作为阈值由应用层根据配置决定。

    改进：不再一次性读取全部快照数据到内存，而是逐标的查询计算，
    峰值内存从 ~2000万行 降至 单标的 ~6万行。

    Args:
        lookback_days: 回溯天数
        coverage_filter: 盘口覆盖上限过滤（排除流动性不足的样本）
    """
    logger.info(
        f"开始VWAP基差分位分析: 回溯{lookback_days}天, "
        f"覆盖率过滤<={coverage_filter}"
    )

    start_time = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d %H:%M:%S')

    # 1. 从标的主表获取有效标的，避免在超大快照表上做 DISTINCT 扫描
    all_assets = fetch_valid_base_assets()
    if not all_assets:
        logger.warning("无有效标的，跳过分析")
        return

    logger.info(f"发现 {len(all_assets)} 个有效标的，开始逐标的计算...")

    # UPSERT SQL
    upsert_sql = """
        INSERT INTO mi_vwap_basis_threshold (
            base_asset, calc_date,
            open_sample_count, open_basis_max, open_basis_min, open_basis_mean, open_basis_std,
            open_basis_p10, open_basis_p20, open_basis_p30, open_basis_p40,
            close_sample_count, close_basis_max, close_basis_min, close_basis_mean, close_basis_std,
            close_basis_p10, close_basis_p20, close_basis_p30, close_basis_p40,
            updated_at
        ) VALUES (
            %(base_asset)s, %(calc_date)s,
            %(open_sample_count)s, %(open_basis_max)s, %(open_basis_min)s, %(open_basis_mean)s, %(open_basis_std)s,
            %(open_basis_p10)s, %(open_basis_p20)s, %(open_basis_p30)s, %(open_basis_p40)s,
            %(close_sample_count)s, %(close_basis_max)s, %(close_basis_min)s, %(close_basis_mean)s, %(close_basis_std)s,
            %(close_basis_p10)s, %(close_basis_p20)s, %(close_basis_p30)s, %(close_basis_p40)s,
            NOW()
        ) ON DUPLICATE KEY UPDATE
            open_sample_count = VALUES(open_sample_count),
            open_basis_max = VALUES(open_basis_max),
            open_basis_min = VALUES(open_basis_min),
            open_basis_mean = VALUES(open_basis_mean),
            open_basis_std = VALUES(open_basis_std),
            open_basis_p10 = VALUES(open_basis_p10),
            open_basis_p20 = VALUES(open_basis_p20),
            open_basis_p30 = VALUES(open_basis_p30),
            open_basis_p40 = VALUES(open_basis_p40),
            close_sample_count = VALUES(close_sample_count),
            close_basis_max = VALUES(close_basis_max),
            close_basis_min = VALUES(close_basis_min),
            close_basis_mean = VALUES(close_basis_mean),
            close_basis_std = VALUES(close_basis_std),
            close_basis_p10 = VALUES(close_basis_p10),
            close_basis_p20 = VALUES(close_basis_p20),
            close_basis_p30 = VALUES(close_basis_p30),
            close_basis_p40 = VALUES(close_basis_p40),
            updated_at = NOW()
    """

    all_keys = [
        'open_sample_count', 'open_basis_max', 'open_basis_min', 'open_basis_mean', 'open_basis_std',
        'open_basis_p10', 'open_basis_p20', 'open_basis_p30', 'open_basis_p40',
        'close_sample_count', 'close_basis_max', 'close_basis_min', 'close_basis_mean', 'close_basis_std',
        'close_basis_p10', 'close_basis_p20', 'close_basis_p30', 'close_basis_p40',
    ]

    # 2. 逐标的查询 + 计算 + 写入
    calc_date = date.today()
    results = []  # 仅用于最终汇总日志
    success_count = 0
    skip_count = 0

    for idx, base_asset in enumerate(all_assets, 1):
        try:
            # 查询单标的数据（峰值内存 ~6万行）
            rows = fetch_asset_snapshot_data(base_asset, start_time)
            if not rows:
                skip_count += 1
                continue

            # 按覆盖率过滤 + 分组
            open_values = []
            close_values = []
            for row in rows:
                coverage = float(row['open_coverage']) if row.get('open_coverage') is not None else 0.0
                if coverage > coverage_filter:
                    continue
                open_bps = row.get('open_vwap_basis_bps')
                if open_bps is not None:
                    open_values.append(float(open_bps))
                close_bps = row.get('close_vwap_basis_bps')
                if close_bps is not None:
                    close_values.append(float(close_bps))

            # 释放原始行数据
            del rows

            if len(open_values) < 10 and len(close_values) < 10:
                skip_count += 1
                continue

            result = {
                'base_asset': base_asset,
                'calc_date': calc_date,
            }

            # 开仓统计
            if len(open_values) >= 10:
                result.update(compute_open_statistics(open_values))
            else:
                result['open_sample_count'] = len(open_values)

            # 平仓统计
            if len(close_values) >= 10:
                result.update(compute_close_statistics(close_values))
            else:
                result['close_sample_count'] = len(close_values)

            # 补齐缺失 key
            for key in all_keys:
                result.setdefault(key, None)

            # 立即写入（单标的 UPSERT）
            with db_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(upsert_sql, result)
                conn.commit()

            success_count += 1
            results.append(result)

            # 每 50 个标的输出进度
            if idx % 50 == 0:
                logger.info(f"  进度: {idx}/{len(all_assets)} 标的已处理")

        except Exception as e:
            logger.error(f"  {base_asset} 计算失败: {e}")
            continue

    logger.info(f"✓ 已写入 {success_count} 个标的的VWAP基差阈值，跳过 {skip_count} 个 (日期={calc_date})")

    # 3. 输出汇总（只输出 top 20 避免日志刻刷）
    if results:
        logger.info("=" * 90)
        logger.info(f"分析完成 | 日期={calc_date} | 回溯={lookback_days}天 | 成功={success_count} 跳过={skip_count}")
        header = "{:<10} {:<7} {:<10} {:<10} {:<7} {:<10} {:<10}".format(
            '标的', '开仓N', 'open_p20', 'open_p30', '平仓N', 'close_p20', 'close_p30')
        logger.info(header)
        logger.info("-" * 90)
        for r in sorted(results, key=lambda x: x.get('open_basis_p20') or 0, reverse=True)[:20]:
            logger.info(
                f"{r['base_asset']:<10} "
                f"{r.get('open_sample_count', 0) or 0:<7} "
                f"{str(r.get('open_basis_p20', '-')):<10} "
                f"{str(r.get('open_basis_p30', '-')):<10} "
                f"{r.get('close_sample_count', 0) or 0:<7} "
                f"{str(r.get('close_basis_p20', '-')):<10} "
                f"{str(r.get('close_basis_p30', '-')):<10}"
            )
        if len(results) > 20:
            logger.info(f"  ... 省略 {len(results) - 20} 个标的")
        logger.info("=" * 90)


def main():
    parser = argparse.ArgumentParser(description='VWAP基差分位阈值计算')
    parser.add_argument(
        '--lookback-days', type=int,
        default=config.get_int('trade.vwap.threshold_lookback_days', 7),
        help='回溯天数 (默认从配置读取，fallback=7)'
    )
    parser.add_argument(
        '--coverage-filter', type=float, default=1.0,
        help='盘口覆盖率上限过滤 (默认1.0，排除覆盖率>100%%的样本)'
    )

    args = parser.parse_args()

    try:
        run_analysis(args.lookback_days, args.coverage_filter)
    except Exception as e:
        logger.error(f"分析失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
