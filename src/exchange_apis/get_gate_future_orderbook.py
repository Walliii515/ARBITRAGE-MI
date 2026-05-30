# coding: utf-8
"""
获取 Gate.io 永续合约订单簿深度数据，用来初始化订单簿
API: GET /futures/{settle}/order_book
"""
import requests
import os
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.logger import get_logger, log_print

logger = get_logger(__name__)

host = "https://api.gateio.ws"
prefix = "/api/v4"


def get_futures_order_book(contract, settle='usdt', interval=0, limit=10, with_id=False):
    """
    获取永续合约订单簿深度数据
    
    Args:
        contract: 合约标识，如 'BTC_USDT'
        settle: 结算货币，默认 'usdt'
        interval: 合并深度的价格精度，0为不合并，不指定则默认为0
        limit: 深度档位数量，默认10
        with_id: 是否返回深度更新ID，默认False
    
    Returns:
        dict: 订单簿数据，包含 asks（卖单）、bids（买单）、current（当前价格）等
    """
    url = f'/futures/{settle}/order_book'
    
    # 构建查询参数
    params = {
        'contract': contract,
        'interval': interval,
        'limit': limit,
    }
    
    if with_id:
        params['with_id'] = 'true'
    
    # 设置请求头（公共接口，无需签名）
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    
    # 发送请求
    full_url = host + prefix + url
    
    try:
        response = requests.request('GET', full_url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        
        order_book = response.json()
        return order_book
        
    except requests.exceptions.RequestException as e:
        logger.exception(f"请求失败: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"错误响应: {e.response.text}")
        return None


def display_order_book(order_book):
    """显示订单簿数据"""
    if not order_book:
        log_print("未获取到数据")
        return
    
    # 打印原始数据结构以便调试
    log_print("原始返回数据:")
    log_print(order_book)
    log_print("\n" + "=" * 80)
    
    # 合约信息可能在顶层或者需要从其他地方获取
    log_print(f"当前价格: {order_book.get('current', 'N/A')}")
    log_print(f"更新时间: {order_book.get('update', 'N/A')}")
    if 'id' in order_book:
        log_print(f"深度ID: {order_book.get('id')}")
    log_print("=" * 80)
    
    # 显示卖单（asks）- 价格从低到高
    asks = order_book.get('asks', [])
    if asks:
        log_print(f"\n卖单（前{len(asks)}档）:")
        log_print(f"{'价格':<20} {'数量':<20}")
        log_print("-" * 40)
        for ask in asks:
            # asks 是字典列表，每个元素有 'p'(价格) 和 's'(数量) 字段
            if isinstance(ask, dict):
                price = ask.get('p', 'N/A')
                size = ask.get('s', 'N/A')
            else:
                # 如果是列表格式 [price, size]
                price = ask[0] if len(ask) > 0 else 'N/A'
                size = ask[1] if len(ask) > 1 else 'N/A'
            log_print(f"{str(price):<20} {str(size):<20}")
    
    # 显示买单（bids）- 价格从高到低
    bids = order_book.get('bids', [])
    if bids:
        log_print(f"\n买单（前{len(bids)}档）:")
        log_print(f"{'价格':<20} {'数量':<20}")
        log_print("-" * 40)
        for bid in bids:
            # bids 是字典列表，每个元素有 'p'(价格) 和 's'(数量) 字段
            if isinstance(bid, dict):
                price = bid.get('p', 'N/A')
                size = bid.get('s', 'N/A')
            else:
                # 如果是列表格式 [price, size]
                price = bid[0] if len(bid) > 0 else 'N/A'
                size = bid[1] if len(bid) > 1 else 'N/A'
            log_print(f"{str(price):<20} {str(size):<20}")


def get_order_book_spread(order_book):
    """
    计算买卖价差
    
    Args:
        order_book: 订单簿数据
    
    Returns:
        dict: 包含买卖价差信息
    """
    if not order_book:
        return None
    
    asks = order_book.get('asks', [])
    bids = order_book.get('bids', [])
    
    if not asks or not bids:
        return None
    
    # 处理字典格式或列表格式
    first_ask = asks[0]
    first_bid = bids[0]
    
    if isinstance(first_ask, dict):
        best_ask = float(first_ask.get('p', 0))
    else:
        best_ask = float(first_ask[0]) if len(first_ask) > 0 else 0
    
    if isinstance(first_bid, dict):
        best_bid = float(first_bid.get('p', 0))
    else:
        best_bid = float(first_bid[0]) if len(first_bid) > 0 else 0
    
    if best_ask == 0 or best_bid == 0:
        return None
    
    spread = best_ask - best_bid
    spread_percent = (spread / best_ask) * 100 if best_ask else 0
    
    return {
        'best_ask': best_ask,
        'best_bid': best_bid,
        'spread': spread,
        'spread_percent': spread_percent,
        'mid_price': (best_ask + best_bid) / 2
    }


if __name__ == '__main__':
    log_print("正在获取 Gate.io 永续合约订单簿深度数据...")
    log_print("")
    
    # 示例：获取 BTC_USDT 的深度数据
    contract = 'BTC_USDT'
    log_print(f"获取 {contract} 的订单簿数据...\n")
    
    order_book = get_futures_order_book(
        contract=contract,
        settle='usdt',
        interval=0,      # 不合并深度
        limit=5,        # 获取5档深度
        with_id=False    # 不返回深度ID
    )
    
    if order_book:
        display_order_book(order_book)

        # 计算并显示买卖价差
        # print("\n")
        # spread_info = get_order_book_spread(order_book)
        # if spread_info:
        #     print("买卖价差信息:")
        #     print(f"  最佳卖价: {spread_info['best_ask']}")
        #     print(f"  最佳买价: {spread_info['best_bid']}")
        #     print(f"  价差: {spread_info['spread']}")
        #     print(f"  价差百分比: {spread_info['spread_percent']:.6f}%")
        #     print(f"  中间价: {spread_info['mid_price']}")
