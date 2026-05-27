# coding: utf-8
"""
获取 Binance 现货交易对信息
通过 /api/v3/exchangeInfo 接口获取所有现货交易对的基本信息
该接口为公开接口，无需 API 认证
"""
import requests
import os
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# Binance API 基础地址（使用 data-api.binance.vision 避免地区限制）
BASE_URL = "https://data-api.binance.vision"


def _extract_filter(filters, filter_type):
    """
    从 filters 列表中提取指定类型的过滤器
    
    Args:
        filters: 交易对的 filters 列表
        filter_type: 过滤器类型名称
    
    Returns:
        dict: 匹配的过滤器字典，未找到返回空字典
    """
    for f in filters:
        if f.get('filterType') == filter_type:
            return f
    return {}


def get_binance_spot_info(quote_asset=None):
    """
    获取 Binance 现货交易对信息
    
    Args:
        quote_asset: 可选，按计价币种过滤（如 'USDT'、'BTC'），默认不过滤
    
    Returns:
        list: 交易对信息列表，每个元素为包含关键字段的字典；失败返回 None
    """
    url = f"{BASE_URL}/api/v3/exchangeInfo"
    
    # 如果指定了 quote_asset，使用 permissions 参数减少返回数据量
    params = {}
    
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        
        data = response.json()
        symbols = data.get('symbols', [])
        
        # 过滤：只保留 status=TRADING 的交易对
        filtered_symbols = [
            s for s in symbols
            if s.get('status') == 'TRADING'
        ]
        
        # 如果指定了 quote_asset，进一步过滤
        if quote_asset:
            filtered_symbols = [
                s for s in filtered_symbols
                if s.get('quoteAsset') == quote_asset.upper()
            ]
        
        # 提取关键信息
        result = []
        for symbol_info in filtered_symbols:
            filters = symbol_info.get('filters', [])
            
            # 提取各类过滤器
            price_filter = _extract_filter(filters, 'PRICE_FILTER')
            lot_size = _extract_filter(filters, 'LOT_SIZE')
            notional = _extract_filter(filters, 'NOTIONAL')
            
            item = {
                'symbol': symbol_info.get('symbol'),
                'base_asset': symbol_info.get('baseAsset'),
                'quote_asset': symbol_info.get('quoteAsset'),
                'status': symbol_info.get('status'),
                'base_asset_precision': symbol_info.get('baseAssetPrecision'),
                'quote_asset_precision': symbol_info.get('quoteAssetPrecision'),
                'base_commission_precision': symbol_info.get('baseCommissionPrecision'),
                'quote_commission_precision': symbol_info.get('quoteCommissionPrecision'),
                # 价格过滤器
                'min_price': price_filter.get('minPrice'),
                'max_price': price_filter.get('maxPrice'),
                'tick_size': price_filter.get('tickSize'),
                # 数量过滤器
                'min_qty': lot_size.get('minQty'),
                'max_qty': lot_size.get('maxQty'),
                'step_size': lot_size.get('stepSize'),
                # 最小名义价值
                'min_notional': notional.get('minNotional'),
                # 允许的订单类型
                'order_types': ','.join(symbol_info.get('orderTypes', [])),
                # 是否允许现货交易
                'is_spot_trading_allowed': symbol_info.get('isSpotTradingAllowed', False),
                'is_margin_trading_allowed': symbol_info.get('isMarginTradingAllowed', False),
            }
            result.append(item)
        
        log_print(f"总共获取 {len(symbols)} 个交易对，过滤后剩余 {len(result)} 个交易对 (status=TRADING"
                  f"{f', quoteAsset={quote_asset.upper()}' if quote_asset else ''})")
        
        return result
        
    except requests.exceptions.RequestException as e:
        logger.exception(f"请求失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"错误响应: {e.response.text}")
        return None


if __name__ == '__main__':
    log_print("正在获取 Binance 现货交易对信息...")
    
    # 默认获取 USDT 计价的交易对
    spot_info = get_binance_spot_info(quote_asset='USDT')
    
    if spot_info:
        log_print(f"\n共获取 {len(spot_info)} 个 USDT 交易对")
        log_print("\n前10个交易对示例:")
        for i, info in enumerate(spot_info[:10], 1):
            log_print(f"  {i}. {info['symbol']} | "
                      f"base={info['base_asset']} quote={info['quote_asset']} | "
                      f"tick_size={info['tick_size']} step_size={info['step_size']} | "
                      f"min_notional={info['min_notional']}")
