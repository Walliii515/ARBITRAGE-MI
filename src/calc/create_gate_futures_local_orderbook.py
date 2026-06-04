# coding: utf-8
"""
Gate.io 永续合约本地订单簿管理器
使用 futures.obu WebSocket 维护本地盘口：
1. 订阅 ob.{contract}.50
2. full=true 消息作为完整快照，直接替换本地簿
3. 后续增量严格要求 U == local_id + 1
4. 出现缺口时重新订阅，等待下一条 full=true 快照
"""
import json
import math
import os
import sys
import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, List, Optional

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from exchange_apis.get_gate_future_orderbook_update_ws import GateFuturesOrderBookWS
from common.config import config
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# OBU 实测可用 50 / 400；本系统只需要前 20 档输出，订阅 50 档即可。
OBU_LEVEL = 50
DISPLAY_LEVEL = 20


def get_gate_obu_level() -> int:
    level = config.get_int('orderbook.gate_obu_level', OBU_LEVEL)
    if level not in (50, 400):
        logger.warning(f"Gate OBU level={level} 可能无实际推送，回退到 {OBU_LEVEL}")
        return OBU_LEVEL
    return level


def get_gate_ws_connect_timeout() -> int:
    return max(8, config.get_int('orderbook.gate_ws_connect_timeout_sec', 30))


def get_orderbook_stale_timeout() -> float:
    return max(1.0, config.get_float('orderbook.stale_timeout_sec', 30.0))


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
    """本地订单簿 - 由 Gate OBU full 快照和增量维护固定档位盘口"""

    def __init__(self, contract: str, base_asset: str = ''):
        self.contract = contract
        self.base_asset = base_asset
        self.id = 0
        self.update_time = 0
        self.asks = OrderedDict()
        self.bids = OrderedDict()
        self.last_update_time = 0.0
        self.update_count: int = 0
        self._ready = False
        self.lock = threading.Lock()

    @staticmethod
    def _price_size(item) -> tuple:
        if isinstance(item, dict):
            return float(item.get('p', 0)), float(item.get('s', 0))
        return float(item[0]), float(item[1])

    def _replace_side(self, items: List, reverse: bool = False) -> OrderedDict:
        values = OrderedDict()
        for item in items:
            price, size = self._price_size(item)
            if size > 0:
                values[price] = size
        return OrderedDict(sorted(values.items(), reverse=reverse))

    def apply_obu(self, update_data: Dict) -> bool:
        """
        应用 Gate OBU 消息。

        full=true: 完整快照，替换本地簿并 ready。
        full=false: 增量，要求 U == 当前 id + 1，否则返回 False。
        """
        with self.lock:
            full = bool(update_data.get('full'))
            U = update_data.get('U')
            u = update_data.get('u')
            if u is None:
                return False

            if full:
                self.asks = self._replace_side(update_data.get('a', []), reverse=False)
                self.bids = self._replace_side(update_data.get('b', []), reverse=True)
                self.id = int(u)
                self.update_time = update_data.get('t', self.update_time)
                self.last_update_time = time.time()
                self.update_count = 1
                self._ready = True
                return True

            if not self._ready:
                return True

            if U is None or int(U) != int(self.id) + 1:
                self._ready = False
                self.last_update_time = 0.0
                self.update_count = 0
                return False

            for ask in update_data.get('a', []):
                price, size = self._price_size(ask)
                if size > 0:
                    self.asks[price] = size
                elif price in self.asks:
                    del self.asks[price]

            for bid in update_data.get('b', []):
                price, size = self._price_size(bid)
                if size > 0:
                    self.bids[price] = size
                elif price in self.bids:
                    del self.bids[price]

            self.asks = OrderedDict(sorted(self.asks.items()))
            self.bids = OrderedDict(sorted(self.bids.items(), reverse=True))
            self.id = int(u)
            self.update_time = update_data.get('t', self.update_time)
            self.last_update_time = time.time()
            self.update_count += 1
            return True

    def is_ready(self) -> bool:
        """本地簿已收到 Gate OBU full 快照并持续接收连续增量。"""
        return self._ready and self.last_update_time > 0

    def mark_not_ready(self) -> None:
        with self.lock:
            self._ready = False
            self.last_update_time = 0.0
            self.update_count = 0

    def to_dict_row(self) -> Dict:
        """将当前订单簿转为一行字典，供 to_records() 序列化"""
        with self.lock:
            row = {
                'base_asset': self.base_asset,
                'contract': self.contract,
                'update_id': self.id,
                'update_time': self.update_time,
                'update_count': self.update_count,
                'future_ready': self.is_ready() and not self.is_stale(get_orderbook_stale_timeout()),
            }

            bids_list = list(self.bids.items())[:DISPLAY_LEVEL]
            for i in range(DISPLAY_LEVEL):
                if i < len(bids_list):
                    row[f'future_price_bid_{i+1}'] = bids_list[i][0]
                    row[f'future_volume_bid_{i+1}'] = bids_list[i][1]
                else:
                    row[f'future_price_bid_{i+1}'] = None
                    row[f'future_volume_bid_{i+1}'] = None

            asks_list = list(self.asks.items())[:DISPLAY_LEVEL]
            for i in range(DISPLAY_LEVEL):
                if i < len(asks_list):
                    row[f'future_price_ask_{i+1}'] = asks_list[i][0]
                    row[f'future_volume_ask_{i+1}'] = asks_list[i][1]
                else:
                    row[f'future_price_ask_{i+1}'] = None
                    row[f'future_volume_ask_{i+1}'] = None

            return row

    def is_stale(self, timeout: float = 30.0) -> bool:
        return (time.time() - self.last_update_time) > timeout


class OrderBookManager:
    """订单簿管理器 - 管理多个 Gate OBU 本地订单簿"""

    def __init__(self, settle: str = 'usdt'):
        self.settle = settle
        self.orderbooks: Dict[str, LocalOrderBook] = {}
        self.ws_client: Optional[GateFuturesOrderBookWS] = None
        self.ws_clients: List[GateFuturesOrderBookWS] = []
        self._contract_ws_clients: Dict[str, GateFuturesOrderBookWS] = {}
        self.lock = threading.Lock()
        self._resubscribe_lock = threading.Lock()
        self._resubscribe_inflight = set()
        self._broadcast_callbacks: List[Callable[[], None]] = []

    def _build_ws_client(self) -> GateFuturesOrderBookWS:
        client = GateFuturesOrderBookWS(
            settle=self.settle,
            connect_timeout=get_gate_ws_connect_timeout(),
        )

        def on_update(contract, update_data):
            ws_data = {
                'full': update_data.get('full', False),
                'U': update_data.get('U'),
                'u': update_data.get('u'),
                't': update_data.get('update', 0),
                'a': update_data.get('asks', []),
                'b': update_data.get('bids', []),
            }
            self._handle_ws_update(contract, ws_data)

        client.set_update_callback(on_update)
        client.connect()
        return client

    def _ensure_ws_clients(self, count: int) -> None:
        count = max(1, int(count or 1))
        while len(self.ws_clients) < count:
            client = self._build_ws_client()
            self.ws_clients.append(client)
            if self.ws_client is None:
                self.ws_client = client

    def _choose_ws_client(self, contract: str, index: int = 0) -> Optional[GateFuturesOrderBookWS]:
        if contract in self._contract_ws_clients:
            return self._contract_ws_clients[contract]
        if not self.ws_clients:
            return None
        client = self.ws_clients[index % len(self.ws_clients)]
        self._contract_ws_clients[contract] = client
        return client

    def subscribe_contract(self, contract: str, index: int = 0) -> bool:
        client = self._choose_ws_client(contract, index=index)
        if not client:
            return False
        client.subscribe_order_book(contract, level=get_gate_obu_level())
        return True

    def prepare_contracts(self, contracts: List[str]) -> None:
        """创建空本地簿，等待 OBU full 快照填充。"""
        with self.lock:
            for contract in contracts:
                if contract in self.orderbooks:
                    continue
                base_asset = contract.split('_')[0] if '_' in contract else contract
                self.orderbooks[contract] = LocalOrderBook(contract, base_asset=base_asset)

    def _handle_ws_update(self, contract: str, ws_data: Dict) -> None:
        orderbook = self.orderbooks.get(contract)
        if not orderbook:
            return
        success = orderbook.apply_obu(ws_data)
        if not success:
            logger.warning(f"⚠ {contract} Gate OBU 增量出现缺口，重新订阅等待 full 快照...")
            self._schedule_resubscribe(contract)

    def _schedule_resubscribe(self, contract: str) -> None:
        with self._resubscribe_lock:
            if contract in self._resubscribe_inflight:
                return
            self._resubscribe_inflight.add(contract)

        def _worker():
            try:
                client = self._contract_ws_clients.get(contract)
                if client:
                    client.resubscribe_order_book(contract)
            finally:
                with self._resubscribe_lock:
                    self._resubscribe_inflight.discard(contract)

        threading.Thread(target=_worker, name=f"gate-obu-resub-{contract}", daemon=True).start()

    def add_contract(self, contract: str):
        """添加合约并订阅 OBU；等待 WS full 快照填充盘口。"""
        with self.lock:
            if contract not in self.orderbooks:
                base_asset = contract.split('_')[0] if '_' in contract else contract
                self.orderbooks[contract] = LocalOrderBook(contract, base_asset=base_asset)
                log_print(f"✓ 已添加 Gate OBU 合约: {contract} (base_asset={base_asset})")
        if self.ws_clients:
            self.subscribe_contract(contract)

    def add_contracts_bulk(self, contracts: List[str], **kwargs) -> List[str]:
        """兼容旧接口：OBU 不需要 REST 初始化，创建空簿后全部视为已加入。"""
        success, _ = self.add_contracts_bulk_with_status(contracts, **kwargs)
        return success

    def add_contracts_bulk_with_status(self, contracts: List[str], **kwargs):
        self.prepare_contracts(contracts)
        return list(contracts), []

    def remove_contract(self, contract: str):
        with self.lock:
            if contract in self.orderbooks:
                del self.orderbooks[contract]
        client = self._contract_ws_clients.pop(contract, None)
        if client:
            client.unsubscribe_order_book(contract)
        log_print(f"✓ 已移除 Gate OBU 合约: {contract}")

    def get_orderbook(self, contract: str) -> Optional[LocalOrderBook]:
        return self.orderbooks.get(contract)

    def get_all_contracts(self) -> List[str]:
        return list(self.orderbooks.keys())

    def to_records(self) -> List[Dict]:
        with self.lock:
            items = list(self.orderbooks.items())
        return [orderbook.to_dict_row() for _, orderbook in items]

    def register_broadcast(self, callback: Callable[[], None]):
        self._broadcast_callbacks.append(callback)

    def _notify_broadcast(self):
        for callback in self._broadcast_callbacks:
            try:
                callback()
            except Exception as e:
                logger.exception(f"广播回调异常: {e}")

    def start_ws(self):
        if self.ws_clients:
            log_print("Gate OBU WebSocket 已运行")
            return
        shards = config.get_int('orderbook.gate_ws_shards', 1)
        self._ensure_ws_clients(shards)
        log_print(
            f"Gate OBU WebSocket 已启动：shards={len(self.ws_clients)}, "
            f"level={get_gate_obu_level()}, display_level={DISPLAY_LEVEL}"
        )

    def stop_ws(self):
        for client in list(self.ws_clients):
            client.disconnect()
        self.ws_clients.clear()
        self._contract_ws_clients.clear()
        self.ws_client = None

    def shutdown(self):
        self.stop_ws()
        with self.lock:
            self.orderbooks.clear()
        with self._resubscribe_lock:
            self._resubscribe_inflight.clear()

    def subscribe_all(
        self,
        contracts: Optional[List[str]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ):
        if not self.ws_clients:
            return
        targets = contracts if contracts is not None else list(self.orderbooks.keys())
        total = len(targets)
        shards = config.get_int('orderbook.gate_ws_shards', 1)
        self._ensure_ws_clients(shards)
        for i, contract in enumerate(targets):
            self.subscribe_contract(contract, index=i)
            if on_progress:
                on_progress(i + 1, total)
                time.sleep(0.03)

    def refresh_snapshot(self, contract: str):
        """兼容旧接口：OBU 通过重订阅刷新 full 快照。"""
        self._schedule_resubscribe(contract)
        log_print(f"✓ {contract} 已触发 Gate OBU 重订阅刷新")


def run_manager_example():
    manager = OrderBookManager(settle='usdt')
    manager.prepare_contracts(['BTC_USDT', 'ETH_USDT'])
    manager.start_ws()
    manager.subscribe_all(['BTC_USDT', 'ETH_USDT'])

    try:
        while True:
            time.sleep(2)
            records = manager.to_records()
            if records:
                log_print(f"\n{'='*100}")
                log_print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                log_print(f"{'='*100}")
                log_print(json.dumps(records, indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        log_print("\n用户中断，正在停止...")
        manager.shutdown()


if __name__ == '__main__':
    log_print("Gate.io 永续合约 OBU 本地订单簿管理器")
    log_print(f"参数: obu_level={get_gate_obu_level()}, display_level={DISPLAY_LEVEL}")
    log_print("=" * 80)
    run_manager_example()
