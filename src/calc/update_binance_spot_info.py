# coding: utf-8
"""
更新 Binance 现货交易对数据到数据库
全删全进 mi_binance_spot_info
合并 exchangeInfo 基本信息 + 24h Ticker 的 quoteVolume
"""
import sys
import os
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.database import db_manager
from exchange_apis.get_binance_spot_info import get_binance_spot_info
from exchange_apis.get_binance_spot_tickers import get_spot_tickers_usdt
from common.logger import get_logger, log_print

logger = get_logger(__name__)


def merge_spot_info_with_tickers(spot_info_list, tickers):
    """
    将交易对信息与 24h Ticker 数据合并，添加 quote_volume 字段
    
    Args:
        spot_info_list: get_binance_spot_info 返回的交易对列表
        tickers: get_spot_tickers_usdt 返回的 Ticker 数据列表
    
    Returns:
        list: 合并了 quote_volume 的交易对列表
    """
    # 将 tickers 转换为字典，以 symbol 为 key
    ticker_dict = {t['symbol']: t for t in tickers}
    
    merged_data = []
    for item in spot_info_list:
        merged_item = item.copy()
        ticker = ticker_dict.get(item['symbol'])
        if ticker:
            merged_item['quote_volume'] = float(ticker.get('quoteVolume', 0))
        else:
            merged_item['quote_volume'] = 0
        merged_data.append(merged_item)
    
    matched = sum(1 for item in merged_data if item['quote_volume'] > 0)
    log_print(f"✓ 成功匹配 {matched}/{len(merged_data)} 个交易对的 quoteVolume")
    
    return merged_data


def clear_table():
    """清空表数据"""
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute("DELETE FROM mi_binance_spot_info")
            log_print("✓ 已清空表数据")
    except Exception as e:
        logger.exception(f"✗ 清空表失败: {e}")
        raise


def insert_spot_info(spot_info_list):
    """插入交易对数据到数据库"""
    insert_sql = """
    INSERT INTO mi_binance_spot_info (
        symbol, base_asset, quote_asset, status,
        base_asset_precision, quote_asset_precision,
        base_commission_precision, quote_commission_precision,
        min_price, max_price, tick_size,
        min_qty, max_qty, step_size,
        min_notional, quote_volume, order_types,
        is_spot_trading_allowed, is_margin_trading_allowed,
        updated_at
    ) VALUES (
        %(symbol)s, %(base_asset)s, %(quote_asset)s, %(status)s,
        %(base_asset_precision)s, %(quote_asset_precision)s,
        %(base_commission_precision)s, %(quote_commission_precision)s,
        %(min_price)s, %(max_price)s, %(tick_size)s,
        %(min_qty)s, %(max_qty)s, %(step_size)s,
        %(min_notional)s, %(quote_volume)s, %(order_types)s,
        %(is_spot_trading_allowed)s, %(is_margin_trading_allowed)s,
        %(updated_at)s
    )
    """
    
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    success_count = 0
    
    try:
        with db_manager.get_cursor() as cursor:
            for item in spot_info_list:
                data = {
                    'symbol': item.get('symbol'),
                    'base_asset': item.get('base_asset'),
                    'quote_asset': item.get('quote_asset'),
                    'status': item.get('status'),
                    'base_asset_precision': item.get('base_asset_precision'),
                    'quote_asset_precision': item.get('quote_asset_precision'),
                    'base_commission_precision': item.get('base_commission_precision'),
                    'quote_commission_precision': item.get('quote_commission_precision'),
                    'min_price': item.get('min_price'),
                    'max_price': item.get('max_price'),
                    'tick_size': item.get('tick_size'),
                    'min_qty': item.get('min_qty'),
                    'max_qty': item.get('max_qty'),
                    'step_size': item.get('step_size'),
                    'min_notional': item.get('min_notional'),
                    'quote_volume': item.get('quote_volume', 0),
                    'order_types': item.get('order_types'),
                    'is_spot_trading_allowed': 1 if item.get('is_spot_trading_allowed') else 0,
                    'is_margin_trading_allowed': 1 if item.get('is_margin_trading_allowed') else 0,
                    'updated_at': now
                }
                
                cursor.execute(insert_sql, data)
                success_count += 1
            
            log_print(f"✓ 成功插入 {success_count} 条交易对数据")
            
    except Exception as e:
        logger.exception(f"✗ 插入数据失败: {e}")
        raise


def update_binance_spot_info():
    """主函数：更新 Binance 现货交易对数据"""
    log_print("=" * 60)
    log_print("开始更新 Binance 现货交易对数据")
    log_print("=" * 60)
    
    try:
        # 1. 获取交易对基本信息
        log_print("\n[1/4] 正在从 Binance API 获取交易对信息...")
        spot_info = get_binance_spot_info(quote_asset='USDT')
        
        if not spot_info:
            log_print("✗ 未获取到交易对数据")
            return
        
        log_print(f"✓ 获取到 {len(spot_info)} 个符合条件的交易对")
        
        # 2. 获取 Ticker 数据
        log_print("\n[2/4] 正在获取 24h Ticker 数据...")
        tickers = get_spot_tickers_usdt(use_mini=True)
        
        if not tickers:
            log_print("✗ 未获取到 Ticker 数据")
            return
        
        log_print(f"✓ 获取到 {len(tickers)} 个交易对的 Ticker 数据")
        
        # 3. 合并数据
        log_print("\n正在合并交易对信息和 Ticker 数据...")
        merged_data = merge_spot_info_with_tickers(spot_info, tickers)
        log_print(f"✓ 成功合并 {len(merged_data)} 条数据")
        
        # 4. 清空表
        log_print("\n[3/4] 正在清空表数据...")
        clear_table()
        
        # 5. 插入新数据
        log_print("\n[4/4] 正在插入新数据...")
        insert_spot_info(merged_data)
        
        # 6. 完成
        log_print("\n" + "=" * 60)
        log_print(f"✓ 数据更新完成！更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_print(f"✓ 共更新 {len(merged_data)} 个交易对")
        log_print("=" * 60)
        
    except Exception as e:
        logger.exception(f"\n✗ 更新失败: {e}")
        raise


if __name__ == '__main__':
    update_binance_spot_info()
