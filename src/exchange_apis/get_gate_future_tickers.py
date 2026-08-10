# coding: utf-8
"""
获取 Gate.io 永续合约 Ticker 数据，主要是24小时成交量bu
API: GET /futures/usdt/tickers
"""
import requests
import os
import sys
from dotenv import load_dotenv

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# 自动加载 .env 文件（从当前目录向上查找）
load_dotenv()

host = "https://api.gateio.ws"
prefix = "/api/v4"


def get_futures_tickers(contract=None):
    """
    获取永续合约 Ticker 数据
    
    Args:
        contract: 可选，指定合约名称（如 'BTC_USDT'），不传则获取所有合约
    
    Returns:
        list: Ticker 数据列表
    """
    url = '/futures/usdt/tickers'
    query_string = f'contract={contract}' if contract else ''
    
    # 设置请求头（公共接口，无需签名）
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    
    # 发送请求
    full_url = host + prefix + url
    if query_string:
        full_url += '?' + query_string
    
    try:
        response = requests.request('GET', full_url, headers=headers)
        response.raise_for_status()
        
        tickers = response.json()
        
        # 如果指定了合约，返回单个对象
        if contract:
            return tickers[0] if tickers else None
        
        return tickers
        
    except requests.exceptions.RequestException as e:
        logger.exception(f"请求失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"错误响应: {e.response.text}")
        return None


def display_tickers(tickers):
    """显示 Ticker 数据"""
    if not tickers:
        log_print("未获取到数据")
        return
    
    log_print(f"总共获取 {len(tickers)} 个合约的 Ticker 数据")
    # print("\n前10个合约示例:")
    # print("=" * 120)
    
    # for i, ticker in enumerate(tickers[:10], 1):
    #     print(f"\n{i}. {ticker.get('contract', 'N/A')}")
    #     print(f"   24小时成交量(结算币): {ticker.get('volume_24h_settle', 'N/A')} USDT")
    #     print(f"   预测资金费率: {ticker.get('funding_rate', 'N/A')}")
    #     print("-" * 80)


if __name__ == '__main__':
    log_print("正在获取 Gate.io 永续合约 Ticker 数据...")
    
    # 获取所有合约的 Ticker
    tickers = get_futures_tickers()
    
    if tickers:
        display_tickers(tickers)

