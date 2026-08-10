# coding: utf-8
import requests
import os
import sys
import json
import time
from dotenv import load_dotenv

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.tools import generate_signature, timestamp_to_datetime, calculate_24h_funding_rate
from common.logger import get_logger, log_print
from common.strategy_accounts import get_gate_futures_credentials

logger = get_logger(__name__)

# 自动加载 .env 文件（从当前目录向上查找）
load_dotenv()

# 获取 API 密钥（公共数据脚本默认使用正向 Gate 子账户）
_CREDS = get_gate_futures_credentials(os.getenv('STRATEGY_ACCOUNT', 'forward'), mainnet=True)
API_KEY = _CREDS.api_key
API_SECRET = _CREDS.api_secret

if not API_KEY or not API_SECRET:
    raise ValueError("请确保 .env 文件中配置了 FORWARD_GATE_FUTURES_API_KEY/FORWARD_GATE_FUTURES_API_SECRET")

host = "https://api.gateio.ws"
prefix = "/api/v4"


def get_futures_funding_rates(contracts=None, limit=10):
    """
    批量查询合约历史资金费率数据
    
    Args:
        contracts: 必填，合约名称列表（如 ['BTC_USDT', 'ETH_USDT']）。
                   POST 接口必须指定至少一个合约，不支持获取所有合约
        limit: 每个合约返回的历史记录数，默认 10 条
    
    Returns:
        list: 历史资金费率数据列表，每个元素包含 contract 和 data 字段
    """
    # 验证 contracts 参数
    if not contracts:
        log_print("错误: POST /futures/usdt/funding_rates 接口必须指定合约列表")
        log_print("示例: get_futures_funding_rates(contracts=['BTC_USDT', 'ETH_USDT'])")
        return None
    
    url = '/futures/usdt/funding_rates'
    query_string = ''
    
    # 构造 POST body（必须包含至少一个合约）
    body = json.dumps({"contracts": contracts, "limit": limit})
    
    # 生成签名
    signature, timestamp = generate_signature('POST', prefix + url, query_string, body, API_SECRET)
    
    # 设置请求头
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'KEY': API_KEY,
        'SIGN': signature,
        'Timestamp': timestamp,
    }
    
    # 发送 POST 请求
    full_url = host + prefix + url
    if query_string:
        full_url += '?' + query_string
    
    # 添加重试机制
    max_retries = 3
    for attempt in range(max_retries):
        try:
            log_print(f"正在请求... (尝试 {attempt + 1}/{max_retries})")
            response = requests.request('POST', full_url, headers=headers, data=body, timeout=10)
            response.raise_for_status()
            
            # 解析结果
            funding_rates = response.json()
            
            log_print(f"总共获取 {len(funding_rates)} 个合约的历史资金费率数据")
            
            return funding_rates
            
        except requests.exceptions.Timeout:
            logger.warning(f"请求超时，{max_retries - attempt - 1} 次重试机会...")
            time.sleep(2)
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"连接错误: {e}")
            if attempt < max_retries - 1:
                log_print(f"{max_retries - attempt - 1} 次重试机会，2秒后重试...")
                time.sleep(2)
            else:
                logger.exception(f"请求失败: {e}")
                return None
        except requests.exceptions.RequestException as e:
            logger.exception(f"请求失败: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"错误响应: {e.response.text}")
            return None
    
    log_print("达到最大重试次数，请求失败")
    return None


if __name__ == '__main__':
    log_print("正在批量获取 Gate.io 永续合约历史资金费率数据...")
    
    # 获取指定合约的历史资金费率（必须指定合约列表）
    specific_contracts = ['BTC_USDT', 'ETH_USDT']
    funding_rates = get_futures_funding_rates(contracts=specific_contracts, limit=5)
    
    if funding_rates:
        log_print(f"\n成功获取 {len(funding_rates)} 个合约的历史资金费率数据")
        log_print("\n合约详情:")
        log_print("=" * 100)
        
        for i, rate_data in enumerate(funding_rates, 1):
            contract_name = rate_data.get('contract', 'N/A')
            history_data = rate_data.get('data', [])
            
            log_print(f"\n{i}. {contract_name}")
            log_print(f"   历史记录数: {len(history_data)}")
            
            if history_data:
                # 显示最新的 3 条记录
                log_print("   最新 3 条历史记录:")
                for j, record in enumerate(history_data[:3], 1):
                    rate = record.get('r', 'N/A')
                    timestamp = record.get('t', 'N/A')
                    dt = timestamp_to_datetime(timestamp)
                    rate_24h = calculate_24h_funding_rate(rate, 28800)  # 假设 8 小时结算一次
                    
                    log_print(f"     {j}. 资金费率: {rate} | 24h费率: {rate_24h} | 时间: {dt}")
            
            log_print("-" * 100)
