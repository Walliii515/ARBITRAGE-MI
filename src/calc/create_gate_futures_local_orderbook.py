# coding: utf-8
"""
Gate.io 永续合约本地订单簿管理器
合并 REST 快照和 WebSocket 增量更新
统一使用 frequency=100ms, level=5 维护5档盘口
"""
import json
import math
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional, Tuple
from collections import OrderedDict

import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from exchange_apis.get_gate_future_orderbook import get_futures_order_book
from exchange_apis.get_gate_future_orderbook_update_ws import GateFuturesOrderBookWS
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# 固定参数
FREQUENCY = '100ms'
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
    """本地订单簿 - 维护5档盘口"""
    
    def __init__(self, contract: str, base_asset: str = ''):
        self.contract = contract
        self.base_asset = base_asset  # 标的资产，如 BTC、ETH
        self.id = 0  # 当前订单簿ID
        self.update_time = 0  # 最后更新时间戳(ms)
        
        # 使用有序字典存储价格和数量，保持排序
        # asks: 价格 -> 数量 (升序，最低卖价在前)
        # bids: 价格 -> 数量 (降序，最高买价在前)
        self.asks = OrderedDict()  # 卖单
        self.bids = OrderedDict()  # 买单
        
        self._first_update_applied = False  # 第一条增量是否已应用
        self.last_update_time = 0.0  # 本地时间
        # WS 增量更新次数累计（每成功 apply_update +1，快照重建时清零）
        # 用于 sustain/旁路风控检测“盘口呆滞”：快照重建后临时取到的购卖五档可能为异常价格，
        # 无后续增量补上时不应被认为是“新鲜”数据。
        self.update_count: int = 0
        self.lock = threading.Lock()
        
    def update_from_snapshot(self, snapshot_data: Dict):
        """
        从 REST API 快照初始化订单簿
        
        Args:
            snapshot_data: REST API 返回的快照数据
        """
        with self.lock:
            self.id = snapshot_data.get('id', 0)
            self.update_time = float(snapshot_data.get('update', 0))
            
            # 清空现有数据
            self.asks.clear()
            self.bids.clear()
            
            # 添加卖单（升序）
            for ask in snapshot_data.get('asks', []):
                if isinstance(ask, dict):
                    price = float(ask.get('p', 0))
                    size = float(ask.get('s', 0))
                else:
                    price = float(ask[0])
                    size = float(ask[1])
                if size > 0:
                    self.asks[price] = size
            
            # 按价格升序排序
            self.asks = OrderedDict(sorted(self.asks.items()))
            
            # 添加买单（降序）
            for bid in snapshot_data.get('bids', []):
                if isinstance(bid, dict):
                    price = float(bid.get('p', 0))
                    size = float(bid.get('s', 0))
                else:
                    price = float(bid[0])
                    size = float(bid[1])
                if size > 0:
                    self.bids[price] = size
            
            # 按价格降序排序
            self.bids = OrderedDict(sorted(self.bids.items(), reverse=True))
            self.last_update_time = time.time()
            self._first_update_applied = False  # 重置，等待新的第一条增量
            # 快照重建【清零 update_count】，避免呆滞检测误判
            self.update_count = 0
            
    def apply_update(self, update_data: Dict) -> bool:
        """
        应用 WebSocket 增量更新
        
        Gate.io 增量更新数据格式:
        {
            't': 时间戳(ms),
            'U': 起始ID,
            'u': 结束ID,
            's': 合约名,
            'a': [{'p': price, 's': size}, ...],  # 卖单变化
            'b': [{'p': price, 's': size}, ...]   # 买单变化
        }
        
        Gate.io 文档规定的处理流程:
        1. 如果 u <= lastUpdateId，丢弃
        2. 第一条消息满足 U <= lastUpdateId+1 <= u，开始处理
        3. 后续消息严格要求 U == lastUpdateId+1
        
        规则: size > 0 更新/添加, size == 0 删除该档位
        
        Returns:
            bool: 是否成功应用更新
        """
        with self.lock:
            U = update_data.get('U', 0)  # 起始ID
            u = update_data.get('u', 0)  # 结束ID
            
            # 步骤1: 如果 u <= 本地 id，丢弃（重复或过期）
            if u <= self.id:
                return False
            
            # 步骤2: 第一条增量消息（快照后的第一条）
            # 由于快照和订阅之间有时间差，第一条消息只要 u > self.id 就接受
            # 对于5档100ms的场景，数据会快速自修正
            if not self._first_update_applied:
                self._first_update_applied = True
            else:
                # 后续消息严格要求连续性
                if U != self.id + 1:
                    self._first_update_applied = False
                    return False
            
            # 应用卖单增量（字段名是 'a'）
            for ask in update_data.get('a', []):
                price = float(ask['p'])
                size = float(ask['s'])
                if size > 0:
                    self.asks[price] = size
                elif price in self.asks:
                    del self.asks[price]
            
            # 应用买单增量（字段名是 'b'）
            for bid in update_data.get('b', []):
                price = float(bid['p'])
                size = float(bid['s'])
                if size > 0:
                    self.bids[price] = size
                elif price in self.bids:
                    del self.bids[price]
            
            # 保持排序
            self.asks = OrderedDict(sorted(self.asks.items()))
            self.bids = OrderedDict(sorted(self.bids.items(), reverse=True))
            
            # 更新ID和时间
            self.id = u
            self.update_time = update_data.get('t', self.update_time)
            self.last_update_time = time.time()
            # 增量成功应用，计数 +1（供 sustain / 旁路风控检测盘口是否呆滞）
            self.update_count += 1

            return True
            
    def to_dict_row(self) -> Dict:
        """将当前订单簿转为一行字典，供 to_records() 序列化"""
        with self.lock:
            row = {
                'base_asset': self.base_asset,
                'contract': self.contract,
                'update_id': self.id,
                'update_time': self.update_time,
                'update_count': self.update_count,
            }
            
            # 买单（前5档）
            bids_list = list(self.bids.items())[:LEVEL]
            for i in range(LEVEL):
                if i < len(bids_list):
                    row[f'future_price_bid_{i+1}'] = bids_list[i][0]
                    row[f'future_volume_bid_{i+1}'] = bids_list[i][1]
                else:
                    row[f'future_price_bid_{i+1}'] = None
                    row[f'future_volume_bid_{i+1}'] = None
            
            # 卖单（前5档）
            asks_list = list(self.asks.items())[:LEVEL]
            for i in range(LEVEL):
                if i < len(asks_list):
                    row[f'future_price_ask_{i+1}'] = asks_list[i][0]
                    row[f'future_volume_ask_{i+1}'] = asks_list[i][1]
                else:
                    row[f'future_price_ask_{i+1}'] = None
                    row[f'future_volume_ask_{i+1}'] = None
            
            return row
    
    def is_stale(self, timeout: float = 30.0) -> bool:
        """检查订单簿是否过期"""
        return (time.time() - self.last_update_time) > timeout


class OrderBookManager:
    """订单簿管理器 - 管理多个合约的本地订单簿，统一100ms/5档"""
    
    def __init__(self, settle: str = 'usdt'):
        """
        初始化订单簿管理器
        
        Args:
            settle: 结算货币
        """
        self.settle = settle
        self.orderbooks: Dict[str, LocalOrderBook] = {}
        self.ws_client: Optional[GateFuturesOrderBookWS] = None
        self.lock = threading.Lock()
        self._broadcast_callbacks: List[Callable[[], None]] = []
        
    def add_contract(self, contract: str):
        """
        添加合约并初始化本地订单簿
        统一使用 frequency=100ms, level=5
        
        Args:
            contract: 合约标识，如 'BTC_USDT'
        """
        with self.lock:
            if contract in self.orderbooks:
                log_print(f"合约 {contract} 已存在")
                return
            
            # 创建本地订单簿（从合约名提取 base_asset，如 BTC_USDT -> BTC）
            base_asset = contract.split('_')[0] if '_' in contract else contract
            self.orderbooks[contract] = LocalOrderBook(contract, base_asset=base_asset)
            log_print(f"✓ 已添加合约: {contract} (base_asset={base_asset})")
            
            # 从 REST API 获取初始快照
            log_print(f"  正在获取 {contract} 的初始快照...")
            snapshot = get_futures_order_book(
                contract=contract,
                settle=self.settle,
                limit=LEVEL,
                with_id=True
            )
            
            if snapshot:
                self.orderbooks[contract].update_from_snapshot(snapshot)
                log_print(f"  ✓ {contract} 初始快照加载成功 (id={snapshot.get('id')})")
            else:
                log_print(f"  ✗ {contract} 初始快照获取失败")
                del self.orderbooks[contract]
                return
            
            # 如果 WebSocket 已连接，立即订阅
            if self.ws_client:
                self.ws_client.subscribe_order_book(contract, frequency=FREQUENCY, level=LEVEL)

    def add_contracts_bulk(
        self,
        contracts: List[str],
        max_workers: int = 20,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> List[str]:
        """
        批量添加合约：并发调用 REST 获取初始快照（锁外并发）
        
        Returns:
            初始化成功的合约列表
        """
        success, _ = self.add_contracts_bulk_with_status(
            contracts, max_workers=max_workers,
            on_progress=on_progress, cancel_event=cancel_event
        )
        return success

    def add_contracts_bulk_with_status(
        self,
        contracts: List[str],
        max_workers: int = 20,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[List[str], List[Tuple[str, str]]]:
        """
        批量添加合约：并发调用 REST 获取初始快照（锁外并发）
        
        Returns:
            (success_list, failed_details)
            - success_list: 初始化成功的合约列表
            - failed_details: 失败合约及原因 [(contract, error_message), ...]
        """
        # 1. 收集待拉快照的合约（成功后再写入 orderbooks，避免空行占位）
        with self.lock:
            new_contracts = [
                c for c in contracts
                if c not in self.orderbooks
            ]
            skipped = len(contracts) - len(new_contracts)
        if skipped:
            log_print(f"跳过 {skipped} 个已存在合约")
        if not new_contracts:
            return [], []
        
        log_print(f"▶ 并发拉取 {len(new_contracts)} 个合约的初始快照（并发数={max_workers}）...")
        
        def _fetch_snapshot(contract: str):
            return contract, get_futures_order_book(
                contract=contract,
                settle=self.settle,
                limit=LEVEL,
                with_id=True
            )
        
        # 2. 锁外并发 REST，每完成一个立即写入本地订单簿
        success: List[str] = []
        failed_details: List[Tuple[str, str]] = []
        done_count = 0
        total_count = len(new_contracts)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_fetch_snapshot, c): c for c in new_contracts}
            for future in as_completed(futures):
                if cancel_event and cancel_event.is_set():
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                contract = futures[future]
                try:
                    _, snapshot = future.result()
                    if snapshot:
                        with self.lock:
                            base_asset = contract.split('_')[0] if '_' in contract else contract
                            ob = LocalOrderBook(contract, base_asset=base_asset)
                            ob.update_from_snapshot(snapshot)
                            self.orderbooks[contract] = ob
                        success.append(contract)
                        log_print(
                            f"  ✓ {contract} 初始快照加载成功"
                            f" (id={snapshot.get('id')})"
                        )
                    else:
                        failed_details.append((contract, 'REST快照返回空（瞬时错误，可重试）'))
                        log_print(f"  ✗ {contract} 初始快照返回空（瞬时错误）")
                except Exception as e:
                    error_msg = str(e)
                    if 'CONTRACT_NOT_FOUND' in error_msg or '404' in error_msg:
                        failed_details.append((contract, '合约已下架或不存在'))
                    elif 'timeout' in error_msg.lower() or 'Timeout' in error_msg:
                        failed_details.append((contract, 'REST请求超时'))
                    elif 'Connection' in error_msg:
                        failed_details.append((contract, '网络连接失败'))
                    else:
                        failed_details.append((contract, f'异常: {error_msg[:100]}'))
                    logger.exception(f"  ✗ {contract} 初始快照异常: {e}")
                done_count += 1
                if on_progress:
                    on_progress(done_count, total_count)
        
        # 3. 若 WS 已连接，为成功的合约补订阅
        if self.ws_client:
            for contract in success:
                self.ws_client.subscribe_order_book(contract, frequency=FREQUENCY, level=LEVEL)
        
        log_print(f"▶ 批量初始化完成：成功 {len(success)}，失败 {len(failed_details)}")
        return success, failed_details
                
    def remove_contract(self, contract: str):
        """移除合约"""
        with self.lock:
            if contract in self.orderbooks:
                del self.orderbooks[contract]
                if self.ws_client:
                    self.ws_client.unsubscribe_order_book(contract)
                log_print(f"✓ 已移除合约: {contract}")
                
    def get_orderbook(self, contract: str) -> Optional[LocalOrderBook]:
        """获取指定合约的本地订单簿"""
        return self.orderbooks.get(contract)
        
    def get_all_contracts(self) -> List[str]:
        """获取所有管理的合约列表"""
        return list(self.orderbooks.keys())
    
    def to_records(self) -> List[Dict]:
        """将订单簿转为 JSON 友好的 dict 列表

        注：Gate 订单簿的值已经是 float/int/str/None 原生类型
        （apply_update 中的 float() 转换保证了这一点），
        无需额外的 _json_safe_scalar 检查。
        """
        with self.lock:
            items = list(self.orderbooks.items())
        return [orderbook.to_dict_row() for _, orderbook in items]

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

    def start_ws(self):
        """启动 WebSocket 客户端并订阅所有已添加的合约"""
        if self.ws_client:
            log_print("WebSocket 已运行")
            return
            
        self.ws_client = GateFuturesOrderBookWS(settle=self.settle)
        
        # 设置增量更新回调：直接传入 WebSocket 原始 result 数据
        def on_update(contract, update_data):
            orderbook = self.orderbooks.get(contract)
            if not orderbook:
                return
            
            # update_data 来自 ws_gate_future_orderbook_update.py 的回调
            # 格式: {'id', 'U', 'u', 'update', 'bids', 'asks'}
            # 需要转换为 apply_update 所需的格式
            ws_data = {
                'U': update_data.get('U', 0),
                'u': update_data.get('u', 0),
                't': update_data.get('update', 0),
                'a': update_data.get('asks', []),
                'b': update_data.get('bids', []),
            }
            
            success = orderbook.apply_update(ws_data)
            if not success:
                # 更新失败，重新获取快照
                logger.warning(f"⚠ {contract} 增量更新失败，重新获取快照...")
                snapshot = get_futures_order_book(
                    contract=contract,
                    settle=self.settle,
                    limit=LEVEL,
                    with_id=True
                )
                if snapshot:
                    orderbook.update_from_snapshot(snapshot)
                    log_print(f"✓ {contract} 快照重新加载成功")
            # 注：不再调用 _notify_broadcast()，广播已改为定时轮询模式
        
        self.ws_client.set_update_callback(on_update)
        self.ws_client.connect()
        # 订阅由 subscribe_all() 单独执行，便于上报进度
        
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

    def subscribe_all(
        self,
        contracts: Optional[List[str]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ):
        """为合约列表订阅 WS 增量（可带进度回调）"""
        if not self.ws_client:
            return
        targets = contracts if contracts is not None else list(self.orderbooks.keys())
        total = len(targets)
        for i, contract in enumerate(targets):
            self.ws_client.subscribe_order_book(contract, frequency=FREQUENCY, level=LEVEL)
            if on_progress:
                on_progress(i + 1, total)
                time.sleep(0.03)  # 留出推送间隔，便于前端看到订阅进度变化
            
    def refresh_snapshot(self, contract: str):
        """手动刷新指定合约的快照"""
        orderbook = self.orderbooks.get(contract)
        if not orderbook:
            log_print(f"合约 {contract} 不存在")
            return
            
        snapshot = get_futures_order_book(
            contract=contract,
            settle=self.settle,
            limit=LEVEL,
            with_id=True
        )
        
        if snapshot:
            orderbook.update_from_snapshot(snapshot)
            log_print(f"✓ {contract} 快照刷新成功")
        else:
            log_print(f"✗ {contract} 快照刷新失败")


def run_manager_example():
    """运行订单簿管理器示例"""
    manager = OrderBookManager(settle='usdt')
    
    # 添加合约（会自动获取快照）
    manager.add_contract('BTC_USDT')
    manager.add_contract('ETH_USDT')
    
    # 启动 WebSocket 接收增量更新
    manager.start_ws()
    
    # 等待WebSocket连接和数据
    time.sleep(3)
    
    # 定期打印订单簿快照
    try:
        while True:
            time.sleep(2)

            records = manager.to_records()
            if records:
                log_print(f"\n{'='*100}")
                log_print(f"当前时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                log_print(f"{'='*100}")
                log_print(json.dumps(records, indent=2, ensure_ascii=False))
                log_print()
                        
    except KeyboardInterrupt:
        log_print("\n用户中断，正在停止...")
        manager.stop_ws()


if __name__ == '__main__':
    log_print("Gate.io 永续合约本地订单簿管理器")
    log_print(f"参数: frequency={FREQUENCY}, level={LEVEL}")
    log_print("=" * 80)
    run_manager_example()
