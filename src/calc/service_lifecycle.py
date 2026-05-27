# coding: utf-8
"""
WS 服务生命周期管理器

封装 Gate/Binance 订单簿 WS 服务的启停状态、进度追踪和 managers 引用，
消除 orderbook_server 中散落的全局变量和启停逻辑。
"""
import asyncio
import threading
from typing import Callable, Dict, List, Optional

from calc.create_binance_spot_local_orderbook import OrderBookManager as BinanceSpotOrderBookManager
from calc.create_gate_futures_local_orderbook import OrderBookManager as GateOrderBookManager
from calc.merge_cross_exchange_orderbook import contracts_to_spot_items
from common.database import db_manager
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# 服务状态常量
SERVICE_IDLE = 'idle'
SERVICE_STARTING = 'starting'
SERVICE_RUNNING = 'running'
SERVICE_STOPPING = 'stopping'
SERVICE_ERROR = 'error'


class ServiceLifecycleManager:
    """WS 服务生命周期管理器"""

    def __init__(self, settle: str, batch_size: int = 40, batch_workers: int = 40):
        """
        Args:
            settle: 结算币种 (如 'usdt')
            batch_size: 快照批次大小
            batch_workers: 快照并发数
        """
        self._settle = settle
        self._batch_size = batch_size
        self._batch_workers = batch_workers

        # OrderBook managers
        self.gate_manager: Optional[GateOrderBookManager] = None
        self.spot_manager: Optional[BinanceSpotOrderBookManager] = None

        # 服务状态
        self._state = SERVICE_IDLE
        self._error: Optional[str] = None
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._cleanup_lock = threading.Lock()

        # 线程引用
        self._start_thread: Optional[threading.Thread] = None
        self._stop_thread: Optional[threading.Thread] = None

        # 进度追踪
        self._snapshot_progress: Dict[str, int] = {'current': 0, 'total': 0}
        self._subscribe_progress: Dict[str, int] = {'current': 0, 'total': 0}

        # 运行时绑定（由 set_runtime 设置）
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._broadcast_queue: Optional[asyncio.Queue] = None
        self._build_payload_fn: Optional[Callable] = None
        self._schedule_broadcast_fn: Optional[Callable] = None

    # ───── 初始化 ─────

    def init_managers(self):
        """创建 Gate/Binance OrderBookManager 实例"""
        self.gate_manager = GateOrderBookManager(settle=self._settle)
        self.spot_manager = BinanceSpotOrderBookManager()

    def register_broadcast(self, callback: Callable):
        """注册广播回调到两个 managers"""
        if self.gate_manager:
            self.gate_manager.register_broadcast(callback)
        if self.spot_manager:
            self.spot_manager.register_broadcast(callback)

    def set_runtime(self, event_loop: asyncio.AbstractEventLoop,
                    broadcast_queue: asyncio.Queue,
                    build_payload_fn: Callable,
                    schedule_broadcast_fn: Callable):
        """绑定运行时依赖（在 lifespan 中调用）"""
        self._event_loop = event_loop
        self._broadcast_queue = broadcast_queue
        self._build_payload_fn = build_payload_fn
        self._schedule_broadcast_fn = schedule_broadcast_fn

    # ───── 状态属性 ─────

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == SERVICE_RUNNING

    @property
    def error(self) -> Optional[str]:
        return self._error

    # ───── 公共方法 ─────

    def start(self):
        """启动服务（后台线程），返回 (ok, message)"""
        with self._lock:
            if self._state in (SERVICE_STARTING, SERVICE_RUNNING):
                return False, '服务已在运行或正在启动'
            if self._start_thread and self._start_thread.is_alive():
                return False, '启动任务进行中'
            if self._stop_thread and self._stop_thread.is_alive():
                return False, '请等待终止完成后再启动'

            self._state = SERVICE_STARTING
            self._push_progress()
            self._start_thread = threading.Thread(target=self._run_start, daemon=True)
            self._start_thread.start()

        return True, '正在启动'

    def stop(self):
        """停止服务（后台线程），返回 (ok, message)"""
        with self._lock:
            if self._state == SERVICE_IDLE:
                return True, '服务未运行'
            if self._stop_thread and self._stop_thread.is_alive():
                return False, '终止任务进行中'

            if self._state == SERVICE_STARTING:
                self._cancel_event.set()

            self._state = SERVICE_STOPPING
            self._push_progress()
            self._stop_thread = threading.Thread(target=self._run_stop, daemon=True)
            self._stop_thread.start()

        return True, '正在终止'

    def get_status(self) -> dict:
        """获取服务状态"""
        snap = dict(self._snapshot_progress)
        sub = dict(self._subscribe_progress)
        return {
            'state': self._state,
            'error': self._error,
            'gate_ws_connected': self._gate_ws_connected(),
            'binance_ws_connected': self._binance_ws_connected(),
            'snapshot': {**snap, 'percent': self._progress_pct(snap)},
            'subscribe': {**sub, 'percent': self._progress_pct(sub)},
            'contracts': self.gate_manager.get_all_contracts() if self.gate_manager else [],
            'spot_symbols': self.spot_manager.get_all_symbols() if self.spot_manager else [],
        }

    def get_progress_payload(self) -> dict:
        """构建进度推送载荷"""
        snap = dict(self._snapshot_progress)
        sub = dict(self._subscribe_progress)
        return {
            'type': 'service_progress',
            'state': self._state,
            'error': self._error,
            'gate_ws_connected': self._gate_ws_connected(),
            'binance_ws_connected': self._binance_ws_connected(),
            'snapshot': {**snap, 'percent': self._progress_pct(snap)},
            'subscribe': {**sub, 'percent': self._progress_pct(sub)},
        }

    def shutdown(self):
        """清理资源（lifespan 退出时调用）"""
        self._cancel_event.set()
        if self.gate_manager:
            self.gate_manager.shutdown()
        if self.spot_manager:
            self.spot_manager.shutdown()

    # ───── 合约加载 ─────

    def fetch_contracts_from_db(self) -> List[str]:
        """从 mi_base_asset 查询有效 base_asset，拼接为 Gate 合约名"""
        sql = "SELECT base_asset FROM mi_base_asset WHERE is_valid = 'Y' limit 999"
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
                settle_suffix = self._settle.upper()
                contracts = [
                    f"{row['base_asset'].strip().upper()}_{settle_suffix}"
                    for row in rows
                    if row.get('base_asset') and row['base_asset'].strip()
                ]
                log_print(f"从数据库获取到 {len(contracts)} 个合约: {contracts}")
                return contracts
        except Exception as e:
            logger.error(f"从数据库获取合约列表失败: {e}，使用默认合约", exc_info=True)
            return ['BTC_USDT', 'ETH_USDT']

    # ───── 内部：启停实现 ─────

    def _run_start(self):
        try:
            self._state = SERVICE_STARTING
            self._error = None
            self._cancel_event.clear()
            self._update_snapshot_progress(0, 0)
            self._update_subscribe_progress(0, 0)

            contracts = [c.strip() for c in self.fetch_contracts_from_db() if c and c.strip()]
            self._update_snapshot_progress(0, len(contracts))

            if self._cancel_event.is_set():
                raise InterruptedError('启动已取消')

            success = self._bulk_add_contracts(contracts)
            if self._cancel_event.is_set():
                raise InterruptedError('启动已取消')

            self._update_snapshot_progress(len(contracts), len(contracts))
            self._push_snapshot_now()

            if success:
                self._start_binance_spot_ws(success)

            self.gate_manager.start_ws()
            if not (
                self.gate_manager.ws_client
                and self.gate_manager.ws_client._connected_event.wait(timeout=8)
            ):
                raise RuntimeError('Gate WebSocket 连接超时')

            self._update_subscribe_progress(0, len(success))

            def on_sub_progress(current: int, total: int):
                self._update_subscribe_progress(current, total)

            self.gate_manager.subscribe_all(success, on_progress=on_sub_progress)
            self._update_subscribe_progress(len(success), len(success))

            self._state = SERVICE_RUNNING
            log_print('✓ 跨交易所订单簿 WS 服务已启动')

            if self._event_loop and self._broadcast_queue and self._build_payload_fn:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_queue.put(self._build_payload_fn()), self._event_loop
                )

        except InterruptedError as e:
            logger.warning(f'启动中断: {e}')
            self._cleanup()
            self._state = SERVICE_IDLE
        except Exception as e:
            logger.exception(f'启动失败: {e}')
            self._error = str(e)
            self._cleanup()
            self._state = SERVICE_ERROR
        finally:
            self._start_thread = None

    def _run_stop(self):
        try:
            self._state = SERVICE_STOPPING
            self._cancel_event.set()
            self._cleanup()
            self._state = SERVICE_IDLE
            self._error = None
            log_print('✓ 跨交易所订单簿 WS 服务已终止')

            if self._event_loop and self._broadcast_queue and self._build_payload_fn:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_queue.put(self._build_payload_fn()), self._event_loop
                )

        except Exception as e:
            logger.exception(f'终止失败: {e}')
            self._error = str(e)
            self._state = SERVICE_ERROR
        finally:
            self._stop_thread = None

    def _cleanup(self):
        with self._cleanup_lock:
            if self.gate_manager:
                self.gate_manager.shutdown()
            if self.spot_manager:
                self.spot_manager.shutdown()
            self._snapshot_progress = {'current': 0, 'total': 0}
            self._subscribe_progress = {'current': 0, 'total': 0}

    def _bulk_add_contracts(self, contracts: List[str]) -> List[str]:
        total = len(contracts)
        if total == 0:
            return []

        all_success: List[str] = []
        global_done = 0

        def on_contract_done(_done: int, _batch_total: int):
            nonlocal global_done
            global_done += 1
            self._update_snapshot_progress(global_done, total)
            if self._schedule_broadcast_fn:
                self._schedule_broadcast_fn()

        for batch_idx in range(0, total, self._batch_size):
            if self._cancel_event.is_set():
                break
            batch = contracts[batch_idx:batch_idx + self._batch_size]
            batch_no = batch_idx // self._batch_size + 1
            log_print(
                f"▶ 批次 {batch_no} 开始，本批 {len(batch)} 个合约"
                f"（总进度 {min(batch_idx + len(batch), total)}/{total}）"
            )
            success = self.gate_manager.add_contracts_bulk(
                batch,
                max_workers=self._batch_workers,
                on_progress=on_contract_done,
                cancel_event=self._cancel_event,
            )
            all_success.extend(success)
            log_print(f"✓ 批次 {batch_no} 完成")
            self._push_snapshot_now()

        return all_success

    def _start_binance_spot_ws(self, contracts: List[str]):
        spot_items = contracts_to_spot_items(contracts)
        if not spot_items:
            log_print('⚠ 无可用 Binance 现货交易对，跳过 Spot WS')
            return

        log_print(f"▶ 启动 Binance Spot WS，订阅 {len(spot_items)} 个交易对")
        self.spot_manager.add_symbols(spot_items)
        self.spot_manager.start_ws()

        if not (
            self.spot_manager.ws_client
            and self.spot_manager.ws_client._connected_event.wait(timeout=10)
        ):
            raise RuntimeError('Binance Spot WebSocket 连接超时')

        log_print('✓ Binance Spot WS 已连接')

    # ───── 内部：进度与连接状态 ─────

    def _gate_ws_connected(self) -> bool:
        if not self.gate_manager or not self.gate_manager.ws_client:
            return False
        return (
            self.gate_manager.ws_client.is_running
            and self.gate_manager.ws_client._connected_event.is_set()
        )

    def _binance_ws_connected(self) -> bool:
        if not self.spot_manager or not self.spot_manager.ws_client:
            return False
        return (
            self.spot_manager.ws_client.is_running
            and self.spot_manager.ws_client._connected_event.is_set()
        )

    def _update_snapshot_progress(self, current: int, total: int):
        self._snapshot_progress = {'current': current, 'total': total}
        self._push_progress()

    def _update_subscribe_progress(self, current: int, total: int):
        self._subscribe_progress = {'current': current, 'total': total}
        self._push_progress()

    def _push_progress(self):
        if self._state not in (SERVICE_STARTING, SERVICE_STOPPING):
            return
        if self._event_loop and self._broadcast_queue:
            payload = self.get_progress_payload()
            asyncio.run_coroutine_threadsafe(
                self._broadcast_queue.put(payload), self._event_loop
            )

    def _push_snapshot_now(self):
        if self._event_loop and self._broadcast_queue and self._build_payload_fn:
            asyncio.run_coroutine_threadsafe(
                self._broadcast_queue.put(self._build_payload_fn()), self._event_loop
            )

    @staticmethod
    def _progress_pct(progress: Dict[str, int]) -> int:
        total = progress.get('total', 0)
        if total <= 0:
            return 0
        return min(100, int(progress['current'] * 100 / total))
