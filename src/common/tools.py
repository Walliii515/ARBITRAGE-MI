# coding: utf-8
"""
通用工具函数
包含 Gate.io API 签名、时间戳转换、资金费率计算等公共功能
"""
import time
import hashlib
import hmac
from datetime import datetime


def generate_signature(method, url, query_string="", body="", api_secret=""):
    """
    生成 Gate.io API 签名
    
    Args:
        method: HTTP 方法 (GET/POST/PUT/DELETE)
        url: 请求路径 (如 /api/v4/futures/usdt/contracts)
        query_string: 查询参数 (如 "contract=BTC_USDT")
        body: 请求体 (JSON 字符串)
        api_secret: API 密钥
    
    Returns:
        tuple: (signature, timestamp)
    """
    t = str(int(time.time()))
    hashed_payload = hashlib.sha512(body.encode('utf-8')).hexdigest()
    s = f"{method}\n{url}\n{query_string}\n{hashed_payload}\n{t}"
    signature = hmac.new(api_secret.encode('utf-8'), s.encode('utf-8'), hashlib.sha512).hexdigest()
    return signature, t


def timestamp_to_datetime(timestamp):
    """
    将时间戳转换为 yyyy-mm-dd hh:mm:ss 格式
    
    Args:
        timestamp: 时间戳（秒）
    
    Returns:
        str: 格式化后的时间字符串，如 "2024-01-01 12:00:00"
    """
    if not timestamp:
        return 'N/A'
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OSError):
        return 'N/A'


def calculate_24h_funding_rate(funding_rate, funding_interval):
    """
    根据当前资金费率和资金费率间隔计算24小时资金费率
    
    Args:
        funding_rate: 当前资金费率
        funding_interval: 资金费率间隔（单位：秒），通常有 4小时(14400), 8小时(28800) 等
    
    Returns:
        str: 24小时资金费率（保留8位小数），计算失败返回 'N/A'
    
    计算公式:
        24小时内的结算次数 = 86400 / funding_interval
        24小时资金费率 = funding_rate * (86400 / funding_interval)
    """
    if not funding_rate or not funding_interval:
        return 'N/A'
    try:
        funding_rate = float(funding_rate)
        funding_interval = int(funding_interval)
        periods_per_24h = 86400 / funding_interval
        funding_rate_24h = funding_rate * periods_per_24h
        # 使用 f 格式化避免科学计数法，保留8位小数
        return f"{funding_rate_24h:.8f}"
    except (ValueError, TypeError, ZeroDivisionError):
        return 'N/A'


def format_price_precision(price: float, precision: int) -> float:
    """
    按交易所规则格式化价格精度
    
    Args:
        price: 原始价格
        precision: 小数位数(Binance现货通常2位, Gate期货从price_decimal获取)
    
    Returns:
        格式化后的价格
    """
    return round(price, precision)


def format_qty_precision(qty: float, precision: int) -> float:
    """
    按交易所规则格式化数量精度
    
    Args:
        qty: 原始数量
        precision: 小数位数(Binance从step_size推导, Gate从size_decimal获取)
    
    Returns:
        格式化后的数量
    """
    return round(qty, precision)


def format_binance_order_params(base_asset: str, qty: float, qty_precision: int, order_uuid: str) -> dict:
    """
    格式化Binance现货市价单参数
    
    Args:
        base_asset: 标的资产(如BTC)
        qty: 下单数量
        qty_precision: 数量精度
        order_uuid: 订单组UUID
    
    Returns:
        Binance市价单参数字典
    """
    formatted_qty = format_qty_precision(qty, qty_precision)
    return {
        'symbol': f"{base_asset}USDT",
        'side': 'BUY',
        'type': 'MARKET',
        'quantity': str(formatted_qty),
        'newClientOrderId': f"arb_{order_uuid[:8]}_spot"
    }


def format_gate_order_params(contract: str, qty: float, quanto_multiplier: float, order_uuid: str) -> dict:
    """
    格式化Gate期货市价单参数
    
    Args:
        contract: 合约名(如BTC_USDT)
        qty: 标的资产数量
        quanto_multiplier: 合约面值乘数
        order_uuid: 订单组UUID
    
    Returns:
        Gate期货市价单参数字典
    """
    contracts_qty = int(qty / quanto_multiplier)
    return {
        'contract': contract,
        'size': contracts_qty,
        'price': '0',
        'tif': 'ioc',
        'text': f"arb_{order_uuid[:8]}_future"
    }
