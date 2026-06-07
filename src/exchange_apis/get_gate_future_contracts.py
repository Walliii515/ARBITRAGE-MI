# coding: utf-8
import requests
import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from common.tools import generate_signature, timestamp_to_datetime, calculate_24h_funding_rate
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# 自动加载 .env 文件（从当前目录向上查找）
load_dotenv()

# 获取 API 密钥
API_KEY = os.getenv('GATE_FUTURES_API_KEY')
API_SECRET = os.getenv('GATE_FUTURES_API_SECRET')

if not API_KEY or not API_SECRET:
    raise ValueError("请确保 .envs 文件中配置了 GATE_FUTURES_API_KEY 和 GATE_FUTURES_API_SECRET")

host = "https://api.gateio.ws"
prefix = "/api/v4"


def parse_base_asset(contract_name: str) -> str | None:
    """
    从合约 name 解析 base_asset（下划线前半段）
    例如 AAPLX_USDT -> AAPLX
    """
    if not contract_name:
        return None
    return contract_name.split('_', 1)[0]


def get_futures_contracts():
    """获取永续合约列表"""
    url = '/futures/usdt/contracts'
    query_string = ''
    body = ''
    
    # 生成签名
    signature, timestamp = generate_signature('GET', prefix + url, query_string, body, API_SECRET)
    
    # 设置请求头
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'KEY': API_KEY,
        'SIGN': signature,
        'Timestamp': timestamp,
    }
    
    # 发送请求
    full_url = host + prefix + url
    if query_string:
        full_url += '?' + query_string
    
    try:
        response = requests.request('GET', full_url, headers=headers)
        response.raise_for_status()
        
        # 打印结果
        contracts = response.json()
        
        # 过滤：只保留 type=direct 且 status=trading 的合约
        filtered_contracts = [
            c for c in contracts 
            if c.get('type') == 'direct' and c.get('status') == 'trading'
        ]
        for contract in filtered_contracts:
            contract['base_asset'] = parse_base_asset(contract.get('name'))

        log_print(f"总共获取 {len(contracts)} 个合约，过滤后剩余 {len(filtered_contracts)} 个合约 (type=direct, status=trading)")
        # print("\n前20个符合条件的合约示例:")
        
        # for i, contract in enumerate(filtered_contracts[:20], 1):
            # print(f"\n{i}. {contract.get('name', 'N/A')}")
            # print(f"   类型: {contract.get('type', 'N/A')}")
            # print(f"   合约乘数: {contract.get('quanto_multiplier', 'N/A')}")
            # print(f"   最小下单量: {contract.get('order_size_min', 'N/A')}")
            # print(f"   最大下单量: {contract.get('order_size_max', 'N/A')}")
            # print(f"   是否支持小数下单: {contract.get('enable_decimal', 'N/A')}")
            # print(f"   最小杠杆: {contract.get('leverage_min', 'N/A')}")
            # print(f"   最大杠杆: {contract.get('leverage_max', 'N/A')}")
            # print(f"   挂单费率: {contract.get('maker_fee_rate', 'N/A')}")
            # print(f"   吃单费率: {contract.get('taker_fee_rate', 'N/A')}")
            # print(f"   当前资金费率: {contract.get('funding_rate', 'N/A')}")
            # print(f"   24小时资金费率: {calculate_24h_funding_rate(contract.get('funding_rate'), contract.get('funding_interval'))}")
            # print(f"   资金费率应用间隔（秒）: {contract.get('funding_interval', 'N/A')}")
            # print(f"   资金费率应用间隔（小时）: {int(contract.get('funding_interval', 0)/3600)}")
            # print(f"   下次资金费率应用时间: {timestamp_to_datetime(contract.get('funding_next_apply'))}")
            # #	合约状态 类型包含：prelaunch（预上线）, trading（交易中）,delisting（下架中）, delisted（已下架）, circuit_breaker（熔断)
            # print(f"   合约状态: {contract.get('status', 'N/A')}")
            # print(f"   资金费率上限: {contract.get('funding_rate_limit', 'N/A')}")
        
        return filtered_contracts
        
    except requests.exceptions.RequestException as e:
        logger.exception(f"请求失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"错误响应: {e.response.text}")
        return None


def get_single_contract_funding_info(contract_name: str) -> dict | None:
    """
    实时获取单个合约资金费信息（用于开仓前最终校验和展示）。
    
    Args:
        contract_name: 合约名称，如 'BTC_USDT'
    
    Returns:
        {
            funding_rate: 单次资金费率,
            funding_rate_24h: 折算 24h 资金费率,
            funding_interval: 支付间隔（秒）,
            funding_next_apply: 下次支付时间字符串,
            funding_last_apply: 上次支付时间字符串,
        }
        失败时返回 None
    """
    url = f'/futures/usdt/contracts/{contract_name}'
    query_string = ''
    body = ''
    
    signature, timestamp = generate_signature('GET', prefix + url, query_string, body, API_SECRET)
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'KEY': API_KEY,
        'SIGN': signature,
        'Timestamp': timestamp,
    }
    
    try:
        response = requests.get(host + prefix + url, headers=headers, timeout=3)
        response.raise_for_status()
        contract = response.json()
        funding_rate = contract.get('funding_rate')
        funding_interval = contract.get('funding_interval')
        if funding_rate is not None and funding_interval:
            interval_sec = int(funding_interval)
            rate = float(funding_rate)
            next_apply = contract.get('funding_next_apply')
            next_dt = datetime.fromtimestamp(int(next_apply)) if next_apply else None
            last_dt = next_dt - timedelta(seconds=interval_sec) if next_dt else None
            return {
                'funding_rate': rate,
                'funding_rate_24h': rate * (86400 / interval_sec),
                'funding_interval': interval_sec,
                'funding_next_apply': next_dt.strftime('%Y-%m-%d %H:%M:%S') if next_dt else None,
                'funding_last_apply': last_dt.strftime('%Y-%m-%d %H:%M:%S') if last_dt else None,
            }
        return None
    except Exception as e:
        logger.warning(f"实时获取资金费率失败 {contract_name}: {e}")
        return None


def get_single_contract_funding_rate(contract_name: str) -> float | None:
    """
    实时获取单个合约的 24h 资金费率（兼容旧调用）。
    """
    info = get_single_contract_funding_info(contract_name)
    return info.get('funding_rate_24h') if info else None


if __name__ == '__main__':
    log_print("正在获取 Gate.io 永续合约列表...")
    get_futures_contracts()
