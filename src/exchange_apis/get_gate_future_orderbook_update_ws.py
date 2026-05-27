# coding: utf-8
"""
Gate.io 永续合约订单簿 WebSocket 客户端
接收 order_book_update 增量数据
"""
import json
import websocket
import threading
import time
import sys
import os
from typing import Callable, Optional, Dict, List

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.logger import get_logger, log_print

logger = get_logger(__name__)


class GateFuturesOrderBookWS:
    """Gate.io 永续合约订单簿 WebSocket 客户端（含自动重连）"""

    def __init__(self, settle='usdt', reconnect_enabled=True,
                 reconnect_delay=3, max_reconnect_delay=60):
        """
        初始化 WebSocket 客户端

        Args:
            settle: 结算货币，默认 'usdt'
            reconnect_enabled: 是否启用自动重连
            reconnect_delay: 初始重连延迟（秒）
            max_reconnect_delay: 最大重连延迟（秒），指数退避上限
        """
        self.settle = settle
        self.ws_url = f"wss://fx-ws.gateio.ws/v4/ws/{settle}"
        self.ws = None
        self.ws_thread = None
        self.is_running = False
        self._was_connected = False

        # 订阅的合约列表
        self.subscriptions = []

        # 回调函数
        self.on_update_callback = None

        # 连接状态事件
        self._connected_event = threading.Event()

        # 自动重连配置
        self._reconnect_enabled = reconnect_enabled
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._should_reconnect = False  # 区分主动断开 vs 意外断连
        self._reconnect_thread: Optional[threading.Thread] = None
        
    def connect(self):
        """建立 WebSocket 连接"""
        self._connected_event.clear()
        self._should_reconnect = self._reconnect_enabled

        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )

        self.is_running = True
        self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.ws_thread.start()

        # 等待连接建立（最多等待5秒）
        if self._connected_event.wait(timeout=5):
            log_print(f"✓ WebSocket 已连接: {self.ws_url}")
        else:
            logger.error(f"✗ WebSocket 连接超时: {self.ws_url}")
        
    def disconnect(self):
        """主动断开 WebSocket 连接并清理线程与订阅（不触发自动重连）"""
        self._should_reconnect = False  # 主动关闭，禁止重连
        self.is_running = False
        self._connected_event.clear()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=5)
        self.ws = None
        self.ws_thread = None
        self.subscriptions.clear()
        log_print("WebSocket 已断开")
        
    def subscribe_order_book(self, contract: str, frequency: str = '100ms', level: int = 20):
        """
        订阅订单簿增量更新
        
        Args:
            contract: 合约标识，如 'BTC_USDT'
            frequency: 推送频率，'20ms' 或 '100ms'（注意：20ms只支持level=20）
            level: 深度档位数量，支持: 100, 50, 20 (注意：10已被Gate.io移除)
        """
        # 检查是否已经订阅过
        for sub in self.subscriptions:
            if sub['contract'] == contract:
                log_print(f" {contract} 已经订阅过，跳过重复订阅")
                return
                
        # 验证参数
        if frequency not in ['20ms', '100ms']:
            logger.warning(f"⚠ 警告: frequency={frequency} 可能不被支持，建议使用 '20ms' 或 '100ms'")
                
        if frequency == '20ms' and level != 20:
            logger.warning(f"⚠ 警告: 20ms频率只支持level=20，当前level={level}")
                
        subscription = {
            "time": int(time.time()),
            "channel": "futures.order_book_update",
            "event": "subscribe",
            "payload": [contract, frequency, str(level)]  # 正确顺序: contract, frequency, level
        }
                
        self.subscriptions.append({
            'contract': contract,
            'frequency': frequency,
            'level': level
        })
        
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.send(json.dumps(subscription))
            log_print(f"✓ 已订阅 {contract} 订单簿增量更新 (frequency={frequency}, level={level})")
        else:
            logger.warning(f"⚠ WebSocket 未连接，订阅将在连接建立后自动发送")
            
    def unsubscribe_order_book(self, contract: str):
        """取消订阅订单簿增量更新"""
        subscription = {
            "time": int(time.time()),
            "channel": "futures.order_book_update",
            "event": "unsubscribe",
            "payload": [contract]
        }
        
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.send(json.dumps(subscription))
            self.subscriptions = [s for s in self.subscriptions if s['contract'] != contract]
            log_print(f"✓ 已取消订阅 {contract} 订单簿增量更新")
            
    def set_update_callback(self, callback: Callable):
        """
        设置数据更新回调函数
        
        Args:
            callback: 回调函数，接收参数 (contract, update_data)
                     update_data 包含: {'id', 'current', 'update', 'bids', 'asks'}
        """
        self.on_update_callback = callback
        
    def _on_open(self, ws):
        """连接建立时的回调"""
        log_print("WebSocket 连接已建立")
        self._connected_event.set()  # 标记连接已建立
        self._was_connected = True
        
        # 重新订阅所有合约（仅在重连时需要）
        for sub in self.subscriptions:
            subscription = {
                "time": int(time.time()),
                "channel": "futures.order_book_update",
                "event": "subscribe",
                "payload": [sub['contract'], sub['frequency'], str(sub['level'])]  # 正确顺序: contract, frequency, level
            }
            if self.ws and self.ws.sock and self.ws.sock.connected:
                self.ws.send(json.dumps(subscription))
                log_print(f"✓ 已订阅 {sub['contract']} 订单簿增量更新 (frequency={sub['frequency']}, level={sub['level']})")
            
    def _on_message(self, ws, message):
        """接收消息的回调"""
        try:
            data = json.loads(message)
            
            # 处理订阅确认消息
            if data.get('event') == 'subscribe':
                channel = data.get('channel', '')
                result = data.get('result', {})
                status = result.get('status', '')
                log_print(f"订阅确认: channel={channel}, status={status}")
                if status == 'fail':
                    logger.error(f"❌ 订阅失败详情:")
                    logger.error(f"   完整响应: {json.dumps(data, indent=2, ensure_ascii=False)}")
                return
            
            # 处理订单簿增量更新
            if data.get('channel') == 'futures.order_book_update':
                result = data.get('result', {})
                if not result:
                    return
                
                contract = result.get('s', '')  # 合约名
                update_data = {
                    'id': result.get('u'),  # 使用 u 作为ID
                    'U': result.get('U'),   # 起始ID
                    'u': result.get('u'),   # 结束ID
                    'current': None,        # order_book_update 不包含current字段
                    'update': result.get('t'),  # 时间戳
                    'bids': result.get('b', []),  # 买单（字段名是b）
                    'asks': result.get('a', [])   # 卖单（字段名是a）
                }
                
                # 调用回调函数
                if self.on_update_callback:
                    self.on_update_callback(contract, update_data)
                    
        except json.JSONDecodeError as e:
            logger.exception(f"JSON 解析错误: {e}")
        except Exception as e:
            logger.exception(f"处理消息错误: {e}")
            
    def _on_error(self, ws, error):
        """错误回调"""
        logger.error(f"WebSocket 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        """连接关闭回调，意外断连时触发自动重连"""
        self.is_running = False
        self._connected_event.clear()
        log_print(f"WebSocket 连接已关闭: {close_status_code} - {close_msg}")

        if self._should_reconnect:
            logger.info('Gate WS 意外断连，将启动自动重连...')
            self._start_reconnect()

    def _start_reconnect(self):
        """启动重连线程（指数退避）"""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return  # 已有重连任务在运行
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        """重连循环，指数退避直到成功或被主动停止"""
        delay = self._reconnect_delay
        attempt = 0

        while self._should_reconnect:
            attempt += 1
            logger.info(f'Gate WS 重连尝试 #{attempt}，等待 {delay}s...')
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
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                self.is_running = True
                self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
                self.ws_thread.start()

                # 等待连接建立
                if self._connected_event.wait(timeout=10):
                    logger.info(f'Gate WS 重连成功（第 {attempt} 次尝试）')
                    log_print(f'✓ Gate WS 自动重连成功')
                    return  # 重连成功，_on_open 会自动重新订阅
                else:
                    logger.warning(f'Gate WS 重连超时，将继续重试...')
                    self.is_running = False

            except Exception as e:
                logger.error(f'Gate WS 重连异常: {e}')
                self.is_running = False

            # 指数退避
            delay = min(delay * 2, self._max_reconnect_delay)

        logger.info('Gate WS 重连已停止（主动关闭或达到上限）')
        
    def ping(self):
        """发送心跳"""
        if self.ws and self.ws.sock and self.ws.sock.connected:
            ping_msg = {
                "time": int(time.time()),
                "channel": "futures.ping"
            }
            self.ws.send(json.dumps(ping_msg))


def run_ws_client():
    """运行 WebSocket 客户端示例"""
    client = GateFuturesOrderBookWS(settle='usdt')
    
    # 设置回调函数
    def on_update(contract, update_data):
        log_print(f"\n{'='*80}")
        log_print(f"合约: {contract}")
        log_print(f"ID: {update_data['id']}")
        log_print(f"更新时间: {update_data['update']}")
        log_print(f"买单数量: {len(update_data['bids'])}")
        log_print(f"卖单数量: {len(update_data['asks'])}")
        
        # 显示前3档买卖单（数据格式是字典列表：[{"p": price, "s": size}, ...]）
        if update_data['bids']:
            log_print("\n买单（前5档）:")
            for i, bid in enumerate(update_data['bids'][:5]):
                # bid 是字典格式：{"p": "77376.7", "s": 59047}
                if isinstance(bid, dict):
                    price = bid.get('p', 'N/A')
                    size = bid.get('s', 'N/A')
                else:
                    price = bid[0] if len(bid) > 0 else 'N/A'
                    size = bid[1] if len(bid) > 1 else 'N/A'
                log_print(f"  {i+1}. 价格: {price}, 数量: {size}")
                
        if update_data['asks']:
            log_print("\n卖单（前5档）:")
            for i, ask in enumerate(update_data['asks'][:5]):
                # ask 是字典格式：{"p": "77375.1", "s": 0}
                if isinstance(ask, dict):
                    price = ask.get('p', 'N/A')
                    size = ask.get('s', 'N/A')
                else:
                    price = ask[0] if len(ask) > 0 else 'N/A'
                    size = ask[1] if len(ask) > 1 else 'N/A'
                log_print(f"  {i+1}. 价格: {price}, 数量: {size}")
        
    client.set_update_callback(on_update)
    
    # 连接并订阅
    client.connect()
    # connect() 会等待连接建立后才返回，所以不需要额外的 sleep
    
    # 订阅 BTC_USDT
    # 根据官方文档：
    # - frequency: '20ms' 或 '100ms'
    # - level: 100, 50, 20 (20ms频率只支持level=20)
    client.subscribe_order_book('BTC_USDT', frequency='100ms', level=5)

    # 保持运行
    try:
        while client.is_running:
            time.sleep(1)
            client.ping()  # 发送心跳
    except KeyboardInterrupt:
        log_print("\n用户中断，正在断开连接...")
        client.disconnect()


if __name__ == '__main__':
    log_print("Gate.io 永续合约订单簿 WebSocket 客户端")
    log_print("=" * 80)
    run_ws_client()
