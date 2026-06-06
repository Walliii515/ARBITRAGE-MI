# coding: utf-8
"""
Binance 现货订单簿 WebSocket 客户端
接收 Partial Book Depth Streams（@depth20@100ms）快照数据
使用 data-stream.binance.vision 避免地区限制
"""
import json
import websocket
import threading
import time
import sys
import os
from typing import Callable, Optional, List

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.logger import get_logger, log_print

logger = get_logger(__name__)


class BinanceSpotOrderBookWS:
    """Binance 现货 Partial Depth Stream WebSocket 客户端（含自动重连）"""

    WS_BASE_URL = "wss://data-stream.binance.vision/ws"
    WS_STREAM_URL = "wss://data-stream.binance.vision/stream"

    def __init__(self, level: int = 5, speed: str = '100ms',
                 reconnect_enabled=True, reconnect_delay=3, max_reconnect_delay=60):
        """
        初始化 WebSocket 客户端

        Args:
            level: 深度档位，Partial Depth 支持 5 / 10 / 20
            speed: 推送速度，'1000ms' 或 '100ms'
            reconnect_enabled: 是否启用自动重连
            reconnect_delay: 初始重连延迟（秒）
            max_reconnect_delay: 最大重连延迟（秒），指数退避上限
        """
        self.level = level
        self.speed = speed
        self.ws = None
        self.ws_thread = None
        self.is_running = False

        self.subscriptions: List[str] = []
        self.on_update_callback: Optional[Callable] = None
        self._connected_event = threading.Event()

        # 自动重连配置
        self._reconnect_enabled = reconnect_enabled
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._should_reconnect = False  # 区分主动断开 vs 意外断连
        self._reconnect_thread: Optional[threading.Thread] = None
        self._init_symbols: Optional[List[str]] = None  # 重连时用于重建组合流 URL
        self._metrics_lock = threading.Lock()
        self._metrics = {
            'message_count': 0,
            'last_message_at': 0.0,
            'json_ms_total': 0.0,
            'json_ms_max': 0.0,
            'callback_ms_total': 0.0,
            'callback_ms_max': 0.0,
            'slow_callback_count': 0,
        }

    def connect(self, symbols: Optional[List[str]] = None):
        """
        建立 WebSocket 连接

        Args:
            symbols: 初始订阅的交易对列表，如 ['BTCUSDT', 'ETHUSDT']
                     若提供则使用组合流 URL 一次性订阅
        """
        self._connected_event.clear()
        self._should_reconnect = self._reconnect_enabled

        # 保存初始 symbols 以便重连时重建 URL
        if symbols:
            self._init_symbols = [s.upper() for s in symbols]

        ws_url = self._build_ws_url(symbols)

        if symbols:
            self.subscriptions = [s.upper() for s in symbols]

        self.ws = websocket.WebSocketApp(
            ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        self.is_running = True
        self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.ws_thread.start()

        if self._connected_event.wait(timeout=10):
            if symbols:
                log_print(f"✓ WebSocket 已连接，订阅 {len(symbols)} 个交易对的 Partial Depth Stream")
            else:
                log_print(f"✓ WebSocket 已连接: {ws_url[:80]}...")
        else:
            logger.error("✗ WebSocket 连接超时")

    def _build_ws_url(self, symbols: Optional[List[str]] = None) -> str:
        """构建 WS URL（组合流或基础 URL）"""
        if symbols:
            streams = [
                f"{s.lower()}@depth{self.level}@{self.speed}"
                for s in symbols
            ]
            return f"{self.WS_STREAM_URL}?streams={'/'.join(streams)}"
        return self.WS_BASE_URL

    def disconnect(self):
        """主动断开 WebSocket 连接并清理（不触发自动重连）"""
        self._should_reconnect = False  # 主动关闭，禁止重连
        self.is_running = False
        self._connected_event.clear()
        # 强制关闭底层 socket（绕过可能阻塞的 close 握手）
        if self.ws:
            try:
                # 先标记 keep_running=False 让 run_forever 退出
                self.ws.keep_running = False
                # 强制关闭底层 socket，唤醒阻塞在 recv 的线程
                if self.ws.sock:
                    self.ws.sock.abort()
            except Exception:
                pass
            try:
                self.ws.close()
            except Exception:
                pass
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=3)
        self.ws = None
        self.ws_thread = None
        self.subscriptions.clear()
        self._init_symbols = None
        log_print("WebSocket 已断开")

    def subscribe_order_book(self, symbol: str):
        """
        订阅 Partial Depth Stream

        Args:
            symbol: 交易对标识，如 'BTCUSDT'
        """
        symbol_upper = symbol.upper()
        if symbol_upper in self.subscriptions:
            log_print(f"⚠ {symbol_upper} 已经订阅过，跳过重复订阅")
            return

        stream_name = f"{symbol_upper.lower()}@depth{self.level}@{self.speed}"
        subscribe_msg = {
            "method": "SUBSCRIBE",
            "params": [stream_name],
            "id": int(time.time() * 1000),
        }

        self.subscriptions.append(symbol_upper)

        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.send(json.dumps(subscribe_msg))
            log_print(f"✓ 已订阅 {symbol_upper} Partial Depth Stream")
        else:
            logger.warning("⚠ WebSocket 未连接，订阅将在连接建立后自动发送")

    def unsubscribe_order_book(self, symbol: str):
        """取消订阅 Partial Depth Stream"""
        symbol_upper = symbol.upper()
        stream_name = f"{symbol_upper.lower()}@depth{self.level}@{self.speed}"
        unsubscribe_msg = {
            "method": "UNSUBSCRIBE",
            "params": [stream_name],
            "id": int(time.time() * 1000),
        }

        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.send(json.dumps(unsubscribe_msg))
            self.subscriptions = [s for s in self.subscriptions if s != symbol_upper]
            log_print(f"✓ 已取消订阅 {symbol_upper}")

    def set_update_callback(self, callback: Callable):
        """
        设置数据更新回调函数

        Args:
            callback: 回调函数，接收参数 (symbol, update_data)
                     update_data 包含: {
                         'lastUpdateId': int,
                         'bids': [[price, qty], ...],
                         'asks': [[price, qty], ...]
                     }
        """
        self.on_update_callback = callback

    def _on_open(self, ws):
        """连接建立时的回调"""
        log_print("WebSocket 连接已建立")
        self._connected_event.set()

    def _on_message(self, ws, message):
        """
        接收 Partial Depth Stream 消息

        组合流格式:
        {
            "stream": "btcusdt@depth20@100ms",
            "data": {
                "lastUpdateId": 1234567890,
                "bids": [["price", "qty"], ...],
                "asks": [["price", "qty"], ...]
            }
        }
        """
        start = time.perf_counter()
        json_done = start
        callback_ms = 0.0
        try:
            msg = json.loads(message)
            json_done = time.perf_counter()

            if 'result' in msg and 'id' in msg:
                self._record_metrics(json_done - start, callback_ms)
                return

            if 'stream' in msg and 'data' in msg:
                stream = msg['stream']
                data = msg['data']
                symbol = stream.split('@')[0].upper()

                if self.on_update_callback:
                    cb_start = time.perf_counter()
                    self.on_update_callback(symbol, data)
                    callback_ms = (time.perf_counter() - cb_start) * 1000
                self._record_metrics(json_done - start, callback_ms)
                return

            if 'lastUpdateId' in msg:
                if self.on_update_callback and len(self.subscriptions) == 1:
                    cb_start = time.perf_counter()
                    self.on_update_callback(self.subscriptions[0], msg)
                    callback_ms = (time.perf_counter() - cb_start) * 1000
                self._record_metrics(json_done - start, callback_ms)

        except json.JSONDecodeError as e:
            logger.exception(f"JSON 解析错误: {e}")
        except Exception as e:
            logger.exception(f"处理消息错误: {e}")

    def _record_metrics(self, json_sec: float, callback_ms: float):
        json_ms = json_sec * 1000
        with self._metrics_lock:
            self._metrics['message_count'] += 1
            self._metrics['last_message_at'] = time.time()
            self._metrics['json_ms_total'] += json_ms
            self._metrics['json_ms_max'] = max(self._metrics['json_ms_max'], json_ms)
            self._metrics['callback_ms_total'] += callback_ms
            self._metrics['callback_ms_max'] = max(self._metrics['callback_ms_max'], callback_ms)
            if callback_ms > 50:
                self._metrics['slow_callback_count'] += 1

    def get_metrics(self) -> dict:
        with self._metrics_lock:
            metrics = dict(self._metrics)
        count = metrics['message_count'] or 1
        return {
            'subscriptions': len(self.subscriptions),
            'is_running': self.is_running,
            'connected': self._connected_event.is_set(),
            'message_count': metrics['message_count'],
            'last_message_at': metrics['last_message_at'],
            'message_age_ms': int((time.time() - metrics['last_message_at']) * 1000) if metrics['last_message_at'] else None,
            'json_ms_avg': round(metrics['json_ms_total'] / count, 3),
            'json_ms_max': round(metrics['json_ms_max'], 3),
            'callback_ms_avg': round(metrics['callback_ms_total'] / count, 3),
            'callback_ms_max': round(metrics['callback_ms_max'], 3),
            'slow_callback_count': metrics['slow_callback_count'],
        }

    def _on_error(self, ws, error):
        """错误回调"""
        logger.error(f"WebSocket 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        """连接关闭回调，意外断连时触发自动重连"""
        self.is_running = False
        self._connected_event.clear()
        log_print(f"WebSocket 连接已关闭: {close_status_code} - {close_msg}")

        if self._should_reconnect:
            logger.info('Binance WS 意外断连，将启动自动重连...')
            self._start_reconnect()

    def _start_reconnect(self):
        """启动重连线程（指数退避）"""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        """重连循环，指数退避直到成功或被主动停止"""
        delay = self._reconnect_delay
        attempt = 0

        while self._should_reconnect:
            attempt += 1
            logger.info(f'Binance WS 重连尝试 #{attempt}，等待 {delay}s...')
            time.sleep(delay)

            if not self._should_reconnect:
                break

            try:
                # 清理旧连接
                if self.ws:
                    try:
                        self.ws.close()
                    except Exception:
                        pass

                self._connected_event.clear()

                # 重建组合流 URL（使用保存的 subscriptions 或 _init_symbols）
                symbols_to_use = self._init_symbols or self.subscriptions
                ws_url = self._build_ws_url(symbols_to_use)

                self.ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.is_running = True
                self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
                self.ws_thread.start()

                # 等待连接建立
                if self._connected_event.wait(timeout=10):
                    logger.info(f'Binance WS 重连成功（第 {attempt} 次尝试）')
                    log_print(f'✓ Binance WS 自动重连成功')
                    return
                else:
                    logger.warning(f'Binance WS 重连超时，将继续重试...')
                    self.is_running = False

            except Exception as e:
                logger.error(f'Binance WS 重连异常: {e}')
                self.is_running = False

            # 指数退避
            delay = min(delay * 2, self._max_reconnect_delay)

        logger.info('Binance WS 重连已停止（主动关闭或达到上限）')


def run_ws_client():
    """运行 WebSocket 客户端示例"""
    client = BinanceSpotOrderBookWS(level=20, speed='100ms')

    def on_update(symbol, update_data):
        log_print(f"\n{'='*80}")
        log_print(f"交易对: {symbol}")
        log_print(f"lastUpdateId: {update_data.get('lastUpdateId')}")
        log_print(f"买单档数: {len(update_data.get('bids', []))}")
        log_print(f"卖单档数: {len(update_data.get('asks', []))}")

        bids = update_data.get('bids', [])
        if bids:
            log_print("\n买单（前5档）:")
            for i, bid in enumerate(bids[:5]):
                log_print(f"  {i + 1}. 价格: {bid[0]}, 数量: {bid[1]}")

        asks = update_data.get('asks', [])
        if asks:
            log_print("\n卖单（前5档）:")
            for i, ask in enumerate(asks[:5]):
                log_print(f"  {i + 1}. 价格: {ask[0]}, 数量: {ask[1]}")

    client.set_update_callback(on_update)
    client.connect(symbols=['BTCUSDT', 'ETHUSDT'])

    try:
        while client.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        log_print("\n用户中断，正在断开连接...")
        client.disconnect()


if __name__ == '__main__':
    log_print("Binance 现货 Partial Depth Stream WebSocket 客户端")
    log_print("=" * 80)
    run_ws_client()
