# coding: utf-8
"""
获取 Binance 现货 24h Ticker 数据，主要是24小时成交量(USDT)
API: GET /api/v3/ticker/24hr
使用 data-api.binance.vision 避免地区限制
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import sys
import time

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# 使用 data-api.binance.vision 避免地区限制
BASE_URL = "https://data-api.binance.vision"


def _create_session():
    """创建带重试机制的 requests session"""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    return session


def get_spot_tickers(symbol=None, use_mini=False):
    """
    获取 Binance 现货 24h Ticker 数据
    
    Args:
        symbol: 可选，指定交易对（如 'BTCUSDT'），不传则获取所有交易对
        use_mini: 是否使用 MINI 模式（返回更少字段，响应更小更快）
    
    Returns:
        list/dict: 不指定symbol返回列表，指定symbol返回单个字典；失败返回 None
    
    MINI 模式返回字段: symbol, openPrice, highPrice, lowPrice, lastPrice, 
                       volume, quoteVolume, openTime, closeTime, count
    FULL 模式额外包含: priceChange, priceChangePercent, weightedAvgPrice,
                       prevClosePrice, lastQty, bidPrice, bidQty, askPrice, askQty
    """
    url = f"{BASE_URL}/api/v3/ticker/24hr"
    
    params = {}
    if symbol:
        params['symbol'] = symbol.upper()
    if use_mini:
        params['type'] = 'MINI'
    
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    
    session = _create_session()
    
    try:
        response = session.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        
        tickers = response.json()
        
        # 如果指定了 symbol，API 直接返回单个对象
        if symbol:
            return tickers
        
        return tickers
        
    except requests.exceptions.RequestException as e:
        logger.exception(f"请求失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"错误响应: {e.response.text}")
        return None
    finally:
        session.close()


def get_spot_tickers_usdt(use_mini=False):
    """
    获取所有 USDT 交易对的 24h Ticker 数据
    
    Args:
        use_mini: 是否使用 MINI 模式（响应更小更快，推荐批量获取时使用）
    
    Returns:
        list: USDT 交易对的 Ticker 数据列表；失败返回 None
    """
    tickers = get_spot_tickers(use_mini=use_mini)
    if not tickers:
        return None
    
    # 过滤只保留 USDT 结尾的交易对
    usdt_tickers = [t for t in tickers if t.get('symbol', '').endswith('USDT')]
    
    log_print(f"总共获取 {len(tickers)} 个交易对，过滤后 {len(usdt_tickers)} 个 USDT 交易对")
    
    return usdt_tickers


def display_tickers(tickers, top_n=10):
    """显示 Ticker 数据（按24h成交额USDT排序）"""
    if not tickers:
        log_print("未获取到数据")
        return
    
    # 按21h quoteVolume（USDT成交额）降序排序
    sorted_tickers = sorted(tickers, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
    
    log_print(f"\n24h USDT成交额 TOP {top_n}:")
    log_print(f"{'交易对':<12} {'最新价':<14} {'涨跌幅':<10} {'24h成交额(USDT)':<22} {'24h成交量':<18}")
    log_print("-" * 80)
    
    for i, ticker in enumerate(sorted_tickers[:top_n], 1):
        symbol = ticker.get('symbol', 'N/A')
        last_price = ticker.get('lastPrice', 'N/A')
        quote_volume = float(ticker.get('quoteVolume', 0))
        volume = ticker.get('volume', 'N/A')
        
        # 涨跌幅: FULL模式直接有，MINI模式需要计算
        change_pct = ticker.get('priceChangePercent')
        if change_pct is None:
            # MINI 模式，从 openPrice 和 lastPrice 计算
            try:
                open_p = float(ticker.get('openPrice', 0))
                last_p = float(last_price)
                if open_p > 0:
                    change_pct = f"{((last_p - open_p) / open_p) * 100:.2f}"
                else:
                    change_pct = '0.00'
            except (ValueError, TypeError):
                change_pct = 'N/A'
        
        log_print(f"{symbol:<12} {last_price:<14} {change_pct + '%':<10} {quote_volume:>18,.2f}    {volume:<18}")


if __name__ == '__main__':
    log_print("正在获取 Binance 现货 24h Ticker 数据...")
    
    # 获取 USDT 交易对的 Ticker（使用 MINI 模式减少响应体积）
    usdt_tickers = get_spot_tickers_usdt(use_mini=True)
    
    if usdt_tickers:
        display_tickers(usdt_tickers, top_n=15)
