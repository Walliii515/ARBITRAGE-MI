# coding: utf-8
"""
Gate.io 永续合约 OBU WebSocket 客户端
接收 futures.obu 推送：首帧 full=true 为完整快照，后续为增量。
"""
import json
import os
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

import websocket

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from common.logger import get_logger, log_print

logger = get_logger(__name__)


class GateFuturesOrderBookWS:
    """Gate.io 永续合约 OBU WebSocket 客户端（含自动重连）"""

    def __init__(self, settle='usdt', reconnect_enabled=True,
                 reconnect_delay=3, max_reconnect_delay=60, connect_timeout=30):
        self.settle = settle
        self.ws_url = f"wss://fx-ws.gateio.ws/v4/ws/{settle}"
        self.ws = None
        self.ws_thread = None
        self.is_running = False

        self.subscriptions: List[Dict] = []
        self.on_update_callback: Optional[Callable] = None
        self._connected_event = threading.Event()

        self._reconnect_enabled = reconnect_enabled
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay
        self._connect_timeout = connect_timeout
        self._should_reconnect = False
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
            on_close=self._on_close,
        )
        self.is_running = True
        self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.ws_thread.start()

        if self._connected_event.wait(timeout=self._connect_timeout):
            log_print(f"✓ Gate OBU WebSocket 已连接: {self.ws_url}")
        else:
            logger.error(
                f"✗ Gate OBU WebSocket 连接超时: {self.ws_url}, "
                f"timeout={self._connect_timeout}s"
            )

    def disconnect(self):
        """主动断开 WebSocket 连接并清理线程与订阅（不触发自动重连）"""
        self._should_reconnect = False
        self.is_running = False
        self._connected_event.clear()
        if self.ws:
            try:
                self.ws.keep_running = False
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
        log_print("Gate OBU WebSocket 已断开")

    @staticmethod
    def _stream_name(contract: str, level: int) -> str:
        return f"ob.{contract}.{level}"

    def subscribe_order_book(self, contract: str, frequency: str = '100ms', level: int = 50):
        """
        订阅 OBU 订单簿。

        Args:
            contract: 合约标识，如 BTC_USDT
            frequency: 保留兼容参数，futures.obu 的频率由档位决定
            level: OBU 深度，实测可用 50 / 400；本系统默认 50
        """
        for sub in self.subscriptions:
            if sub['contract'] == contract:
                log_print(f" {contract} 已经订阅过 Gate OBU，跳过重复订阅")
                return

        if level not in (50, 400):
            logger.warning(f"Gate OBU level={level} 可能无实际推送，建议使用 50 或 400")

        sub = {'contract': contract, 'level': level}
        self.subscriptions.append(sub)
        self._send_subscribe(sub)

    def unsubscribe_order_book(self, contract: str):
        """取消订阅 OBU 订单簿"""
        matches = [s for s in self.subscriptions if s['contract'] == contract]
        for sub in matches:
            self._send_unsubscribe(sub)
        self.subscriptions = [s for s in self.subscriptions if s['contract'] != contract]

    def resubscribe_order_book(self, contract: str):
        """取消并重新订阅，触发 Gate OBU 重新推送 full=true 快照。"""
        matches = [s for s in self.subscriptions if s['contract'] == contract]
        if not matches:
            return
        for sub in matches:
            self._send_unsubscribe(sub)
            time.sleep(0.05)
            self._send_subscribe(sub)

    def set_update_callback(self, callback: Callable):
        self.on_update_callback = callback

    def _send_subscribe(self, sub: Dict):
        msg = {
            "time": int(time.time()),
            "channel": "futures.obu",
            "event": "subscribe",
            "payload": [self._stream_name(sub['contract'], sub['level'])],
        }
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.send(json.dumps(msg))
            log_print(f"✓ 已订阅 {sub['contract']} Gate OBU (level={sub['level']})")
        else:
            logger.warning("⚠ Gate OBU WebSocket 未连接，订阅将在连接建立后自动发送")

    def _send_unsubscribe(self, sub: Dict):
        msg = {
            "time": int(time.time()),
            "channel": "futures.obu",
            "event": "unsubscribe",
            "payload": [self._stream_name(sub['contract'], sub['level'])],
        }
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.send(json.dumps(msg))
            log_print(f"✓ 已取消订阅 {sub['contract']} Gate OBU")

    def _on_open(self, ws):
        log_print("Gate OBU WebSocket 连接已建立")
        self._connected_event.set()
        for sub in self.subscriptions:
            self._send_subscribe(sub)

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if data.get('event') == 'subscribe':
                result = data.get('result', {})
                status = result.get('status', '')
                log_print(f"Gate OBU 订阅确认: status={status}")
                if status == 'fail':
                    logger.error(f"Gate OBU 订阅失败: {json.dumps(data, ensure_ascii=False)}")
                return

            if data.get('channel') != 'futures.obu':
                return
            result = data.get('result', {})
            if not isinstance(result, dict):
                return

            stream = result.get('s') or data.get('payload', [None])[0]
            contract = self._contract_from_stream(stream)
            if not contract:
                return

            update_data = {
                'contract': contract,
                'full': bool(result.get('full')),
                'U': result.get('U'),
                'u': result.get('u'),
                'update': result.get('t') or data.get('time_ms'),
                'bids': result.get('b', []),
                'asks': result.get('a', []),
            }
            if self.on_update_callback:
                self.on_update_callback(contract, update_data)
        except json.JSONDecodeError as e:
            logger.exception(f"Gate OBU JSON 解析错误: {e}")
        except Exception as e:
            logger.exception(f"Gate OBU 处理消息错误: {e}")

    @staticmethod
    def _contract_from_stream(stream: Optional[str]) -> str:
        if not stream:
            return ''
        parts = stream.split('.')
        return parts[1] if len(parts) >= 3 else ''

    def _on_error(self, ws, error):
        logger.error(f"Gate OBU WebSocket 错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self.is_running = False
        self._connected_event.clear()
        log_print(f"Gate OBU WebSocket 连接已关闭: {close_status_code} - {close_msg}")
        if self._should_reconnect:
            logger.info('Gate OBU WS 意外断连，将启动自动重连...')
            self._start_reconnect()

    def _start_reconnect(self):
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _reconnect_loop(self):
        delay = self._reconnect_delay
        attempt = 0
        while self._should_reconnect:
            attempt += 1
            logger.info(f'Gate OBU WS 重连尝试 #{attempt}，等待 {delay}s...')
            time.sleep(delay)
            if not self._should_reconnect:
                break
            try:
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
                    on_close=self._on_close,
                )
                self.is_running = True
                self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
                self.ws_thread.start()
                if self._connected_event.wait(timeout=10):
                    logger.info(f'Gate OBU WS 重连成功（第 {attempt} 次尝试）')
                    log_print('✓ Gate OBU WS 自动重连成功')
                    return
                self.is_running = False
            except Exception as e:
                logger.error(f'Gate OBU WS 重连异常: {e}')
                self.is_running = False
            delay = min(delay * 2, self._max_reconnect_delay)

        logger.info('Gate OBU WS 重连已停止（主动关闭或达到上限）')

    def ping(self):
        if self.ws and self.ws.sock and self.ws.sock.connected:
            self.ws.send(json.dumps({"time": int(time.time()), "channel": "futures.ping"}))
