# coding: utf-8
"""
Binance 现货本地订单簿管理器
使用 Partial Book Depth Streams（@depth5@1000ms）维护5档盘口
每条 WS 消息就是完整的5档快照，无需 REST 铺底和增量同步
"""
import json
import math
import time
import threading
from typing import Callable, Dict, List, Optional

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from exchange_apis.get_binance_spot_orderbook_ws import BinanceSpotOrderBookWS
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# 固定参数
SPEED = '1000ms'
LEVEL = 5


def _json_safe_scalar(val):
    """转为 JSON 可序列化的原生标量（NaN/Inf → None）"""
    if val is None:
        return None
    if hasattr(val, 'item'):
        val = val.item()
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    return val


class LocalOrderBook:
    """本地订单簿 - 维护5档盘口（由 Partial Depth Stream 直接更新）"""

    def __init__(self, symbol: str, base_asset: str = ''):
        self.symbol = symbol
        self.base_asset = base_asset
        self.last_update_id = 0
        self.update_time = 0.0

        self.bids: List[tuple] = []
        self.asks: List[tuple] = []

        self.lock = threading.Lock()

    def update_from_partial_depth(self, data: Dict):
        """
        从 Partial Book Depth Stream 消息更新订单簿

        消息格式:
        {
            "lastUpdateId": 1234567890,
            "bids": [["price", "qty"], ...],
            "asks": [["price", "qty"], ...]
        }
        """
        with self.lock:
            self.last_update_id = data.get('lastUpdateId', 0)
            self.bids = [
                (float(b[0]), float(b[1]))
                for b in data.get('bids', [])[:LEVEL]
            ]
            self.asks = [
                (float(a[0]), float(a[1]))
                for a in data.get('asks', [])[:LEVEL]
            ]
            self.update_time = time.time()

    def to_dict_row(self) -> Dict:
        """将当前订单簿转为一行字典，供 to_records() 序列化"""
        with self.lock:
            row = {
                'base_asset': self.base_asset,
                'symbol': self.symbol,
                'update_id': self.last_update_id,
                'update_time': self.update_time,
            }

            for i in range(LEVEL):
                if i < len(self.bids):
                    row[f'spot_price_bid_{i+1}'] = self.bids[i][0]
                    row[f'spot_volume_bid_{i+1}'] = self.bids[i][1]
                else:
                    row[f'spot_price_bid_{i+1}'] = None
                    row[f'spot_volume_bid_{i+1}'] = None

            for i in range(LEVEL):
                if i < len(self.asks):
                    row[f'spot_price_ask_{i+1}'] = self.asks[i][0]
                    row[f'spot_volume_ask_{i+1}'] = self.asks[i][1]
                else:
                    row[f'spot_price_ask_{i+1}'] = None
                    row[f'spot_volume_ask_{i+1}'] = None

            return row

    def is_stale(self, timeout: float = 30.0) -> bool:
        """检查订单簿是否过期"""
        return (time.time() - self.update_time) > timeout


class OrderBookManager:
    """
    订单簿管理器 - 管理多个交易对的本地订单簿
    使用 Partial Book Depth Streams (@depth5@100ms)
    """

    def __init__(self):
        self.orderbooks: Dict[str, LocalOrderBook] = {}
        self.ws_client: Optional[BinanceSpotOrderBookWS] = None
        self.lock = threading.Lock()
        self._broadcast_callbacks: List[Callable[[], None]] = []

    def add_symbols(self, symbols: List[str]):
        """
        添加交易对

        Args:
            symbols: 交易对列表，如 ['BTCUSDT', 'ETHUSDT']
                     或 [{'symbol': 'BTCUSDT', 'base_asset': 'BTC'}, ...]
        """
        with self.lock:
            for item in symbols:
                if isinstance(item, dict):
                    sym = item['symbol'].upper()
                    base_asset = item.get('base_asset', sym.replace('USDT', ''))
                else:
                    sym = item.upper()
                    base_asset = sym.replace('USDT', '')
                if sym not in self.orderbooks:
                    self.orderbooks[sym] = LocalOrderBook(sym, base_asset=base_asset)
                    log_print(f"✓ 已添加交易对: {sym} (base_asset={base_asset})")

    def add_symbol(self, symbol, base_asset: str = ''):
        """添加单个交易对"""
        if isinstance(symbol, dict):
            self.add_symbols([symbol])
            sym = symbol['symbol'].upper()
        else:
            sym = symbol.upper()
            ba = base_asset or sym.replace('USDT', '')
            self.add_symbols([{'symbol': sym, 'base_asset': ba}])

        if self.ws_client:
            self.ws_client.subscribe_order_book(sym)

    def remove_symbol(self, symbol: str):
        """移除交易对"""
        symbol_upper = symbol.upper()
        with self.lock:
            if symbol_upper in self.orderbooks:
                del self.orderbooks[symbol_upper]
                log_print(f"✓ 已移除交易对: {symbol_upper}")
        if self.ws_client:
            self.ws_client.unsubscribe_order_book(symbol_upper)

    def get_orderbook(self, symbol: str) -> Optional[LocalOrderBook]:
        """获取指定交易对的本地订单簿"""
        return self.orderbooks.get(symbol.upper())

    def get_all_symbols(self) -> List[str]:
        """获取所有管理的交易对列表"""
        return list(self.orderbooks.keys())

    def to_records(self) -> List[Dict]:
        """将订单簿转为 JSON 友好的 dict 列表"""
        with self.lock:
            items = list(self.orderbooks.items())
        return [
            {k: _json_safe_scalar(v) for k, v in orderbook.to_dict_row().items()}
            for _, orderbook in items
        ]

    def register_broadcast(self, callback: Callable[[], None]):
        """注册数据变更广播回调"""
        self._broadcast_callbacks.append(callback)

    def _notify_broadcast(self):
        """通知所有注册的广播回调"""
        for callback in self._broadcast_callbacks:
            try:
                callback()
            except Exception as e:
                logger.exception(f"广播回调异常: {e}")

    def start_ws(self, on_progress: Optional[Callable[[int, int], None]] = None):
        """启动 WebSocket 并订阅所有已添加交易对的 Partial Depth Stream"""
        if self.ws_client:
            log_print("WebSocket 已运行")
            return

        symbols = list(self.orderbooks.keys())
        if not symbols:
            log_print("⚠ 没有交易对需要订阅")
            return

        self.ws_client = BinanceSpotOrderBookWS(level=LEVEL, speed=SPEED)

        def on_update(symbol, update_data):
            orderbook = self.orderbooks.get(symbol)
            if not orderbook:
                return
            orderbook.update_from_partial_depth(update_data)
            self._notify_broadcast()

        self.ws_client.set_update_callback(on_update)
        self.ws_client.connect(symbols=symbols)

        if on_progress:
            for i, sym in enumerate(symbols):
                on_progress(i + 1, len(symbols))

    def subscribe_all(
        self,
        symbols: Optional[List[str]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ):
        """为交易对列表订阅 WS（兼容 Gate.io 接口风格）"""
        if not self.ws_client:
            return
        targets = symbols if symbols is not None else list(self.orderbooks.keys())
        total = len(targets)
        for i, sym in enumerate(targets):
            self.ws_client.subscribe_order_book(sym)
            if on_progress:
                on_progress(i + 1, total)
                time.sleep(0.03)

    def stop_ws(self):
        """停止 WebSocket 客户端"""
        if self.ws_client:
            self.ws_client.disconnect()
            self.ws_client = None

    def shutdown(self):
        """停止 WS 并清空所有本地订单簿"""
        self.stop_ws()
        with self.lock:
            self.orderbooks.clear()


def run_manager_example():
    """运行订单簿管理器示例"""
    manager = OrderBookManager()
    symbols = ['BTCUSDT', 'ETHUSDT']

    manager.add_symbols(symbols)
    manager.start_ws()
    time.sleep(2)

    try:
        while True:
            time.sleep(2)

            rows = manager.to_records()
            if rows:
                log_print(f"\n{'='*100}")
                log_print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                log_print(f"{'='*100}")
                log_print(json.dumps(rows, indent=2, ensure_ascii=False))
                log_print()

    except KeyboardInterrupt:
        log_print("\n用户中断，正在停止...")
        manager.shutdown()


if __name__ == '__main__':
    log_print("Binance 现货本地订单簿管理器")
    log_print(f"参数: Partial Depth Stream @depth{LEVEL}@{SPEED}")
    log_print("=" * 80)
    run_manager_example()
