# coding: utf-8
"""
WS 服务生命周期管理器

封装 Gate/Binance 订单簿 WS 服务的启停状态、进度追踪和 managers 引用，
消除 orderbook_server 中散落的全局变量和启停逻辑。
"""
import asyncio
import time
import threading
from typing import Callable, Dict, List, Optional, Tuple

from calc.create_binance_spot_local_orderbook import OrderBookManager as BinanceSpotOrderBookManager
from calc.create_gate_futures_local_orderbook import OrderBookManager as GateOrderBookManager
from calc.merge_cross_exchange_orderbook import contracts_to_spot_items
from common.config import config
from common.database import db_manager
from common.logger import get_logger, log_print

logger = get_logger(__name__)

# 服务状态常量
SERVICE_IDLE = 'idle'
SERVICE_STARTING = 'starting'
SERVICE_RUNNING = 'running'
SERVICE_STOPPING = 'stopping'
SERVICE_ERROR = 'error'

# 连接状态常量
CONN_PENDING = 'pending'
CONN_SUCCESS = 'success'
CONN_FAILED = 'failed'


class ServiceLifecycleManager:
    """WS 服务生命周期管理器"""

    def __init__(self, settle: str):
        """
        Args:
            settle: 结算币种 (如 'usdt')
        """
        self._settle = settle

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

        # 启动阶段进度计数器（启动完成后由 get_progress_summary 实时计算替代）
        self._startup_gate_snapshot_done: int = 0
        self._startup_gate_snapshot_total: int = 0
        self._startup_gate_ws_done: int = 0
        self._startup_gate_ws_total: int = 0

        # 逐标的连接状态追踪
        # key = base_asset, value = {contract, symbol, gate_snapshot, gate_snapshot_error,
        #                            gate_ws_subscribed, binance_ws_subscribed, last_gate_update, last_binance_update}
        self._connection_status: Dict[str, Dict] = {}
        self._conn_lock = threading.Lock()

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
        progress = self.get_progress_summary()
        return {
            'state': self._state,
            'error': self._error,
            'gate_ws_connected': self._gate_ws_connected(),
            'binance_ws_connected': self._binance_ws_connected(),
            'gate_ws_latency_ms': self._calc_gate_data_age_ms(),
            'binance_ws_latency_ms': self._calc_binance_data_age_ms(),
            **progress,
            'contracts': self.gate_manager.get_all_contracts() if self.gate_manager else [],
            'spot_symbols': self.spot_manager.get_all_symbols() if self.spot_manager else [],
        }

    def get_diagnostics(self) -> dict:
        """获取行情新鲜度与 WS 分片处理指标，用于定位延迟来源。"""
        return {
            'state': self._state,
            'gate_ws_connected': self._gate_ws_connected(),
            'binance_ws_connected': self._binance_ws_connected(),
            'freshness': {
                'gate': self._freshness_summary('gate'),
                'binance': self._freshness_summary('binance'),
            },
            'ws_clients': {
                'gate': self._ws_client_metrics(self.gate_manager),
                'binance': self._ws_client_metrics(self.spot_manager),
            },
        }

    def get_connection_status(self) -> List[Dict]:
        """获取逐标的连接状态列表（供前端展示）"""
        now = time.time()
        with self._conn_lock:
            result = []
            for base_asset, info in self._connection_status.items():
                contract = info.get('contract', '')
                symbol = info.get('symbol', '')

                # 判断 Gate 订单簿是否有实时数据
                gate_ob = self.gate_manager.orderbooks.get(contract) if self.gate_manager else None
                gate_last_update = gate_ob.last_update_time if gate_ob else 0
                gate_stale = gate_ob.is_stale() if gate_ob else True
                gate_ready = gate_ob.is_ready() if gate_ob else False

                # 判断 Binance 订单簿是否有实时数据
                binance_ob = self.spot_manager.orderbooks.get(symbol) if self.spot_manager else None
                binance_last_update = binance_ob.update_time if binance_ob else 0
                binance_stale = binance_ob.is_stale() if binance_ob else True

                result.append({
                    'base_asset': base_asset,
                    'contract': contract,
                    'symbol': symbol,
                    'gate_snapshot_status': CONN_SUCCESS if gate_ready else info.get('gate_snapshot', CONN_PENDING),
                    'gate_snapshot_error': None if gate_ready else info.get('gate_snapshot_error'),
                    'gate_ws_subscribed': info.get('gate_ws_subscribed', False),
                    'gate_receiving_data': not gate_stale if gate_ob else False,
                    'gate_last_update': gate_last_update,
                    'gate_stale_sec': round(now - gate_last_update, 1) if gate_last_update > 0 else None,
                    'binance_ws_subscribed': info.get('binance_ws_subscribed', False),
                    'binance_receiving_data': not binance_stale if binance_ob else False,
                    'binance_last_update': binance_last_update,
                    'binance_stale_sec': round(now - binance_last_update, 1) if binance_last_update > 0 else None,
                })
            return result

    def get_progress_payload(self) -> dict:
        """构建进度推送载荷"""
        progress = self.get_progress_summary()
        return {
            'type': 'service_progress',
            'state': self._state,
            'error': self._error,
            'gate_ws_connected': self._gate_ws_connected(),
            'binance_ws_connected': self._binance_ws_connected(),
            'gate_ws_latency_ms': self._calc_gate_data_age_ms(),
            'binance_ws_latency_ms': self._calc_binance_data_age_ms(),
            **progress,
        }

    def get_progress_summary(self) -> dict:
        """
        从 _connection_status 实时计算 4 维度进度摘要：
        - gate_snapshot: Gate OBU full 快照（success/failed/total）
        - gate_ws: Gate WS 订阅
        - binance_ws: Binance WS 订阅
        - binance_data: Binance 实时数据接收

        启动阶段使用计数器（进度递增），运行后实时反映连接状态。
        """
        # 启动阶段：进度来自计数器
        if self._state == SERVICE_STARTING:
            total = self._startup_gate_snapshot_total
            return {
                'gate_snapshot': {'success': self._startup_gate_snapshot_done, 'failed': 0, 'total': total,
                                  'percent': self._pct(self._startup_gate_snapshot_done, total)},
                'gate_ws': {'success': self._startup_gate_ws_done, 'total': self._startup_gate_ws_total,
                            'percent': self._pct(self._startup_gate_ws_done, self._startup_gate_ws_total)},
                'binance_ws': {'success': 0, 'total': total, 'percent': 0},
                'binance_data': {'success': 0, 'total': total, 'percent': 0},
            }

        # 运行/其他状态：从 _connection_status 实时计算
        with self._conn_lock:
            items = list(self._connection_status.values())
        total = len(items)
        if total == 0:
            empty = {'success': 0, 'failed': 0, 'total': 0, 'percent': 0}
            return {'gate_snapshot': empty, 'gate_ws': {**empty}, 'binance_ws': {**empty}, 'binance_data': {**empty}}

        gate_snap_ok = 0
        gate_snap_fail = 0
        if self.gate_manager:
            for i in items:
                ob = self.gate_manager.orderbooks.get(i.get('contract', ''))
                if ob and ob.is_ready():
                    gate_snap_ok += 1
                elif i.get('gate_snapshot') == CONN_FAILED:
                    gate_snap_fail += 1
        gate_ws_ok = sum(1 for i in items if i.get('gate_ws_subscribed'))
        binance_ws_ok = sum(1 for i in items if i.get('binance_ws_subscribed'))

        # Binance 数据接收（检查订单簿是否有活跃数据）
        binance_data_ok = 0
        if self.spot_manager:
            now = time.time()
            for info in items:
                ob = self.spot_manager.orderbooks.get(info.get('symbol', ''))
                if ob and ob.update_time > 0 and (now - ob.update_time) < 30:
                    binance_data_ok += 1

        return {
            'gate_snapshot': {'success': gate_snap_ok, 'failed': gate_snap_fail, 'total': total,
                              'percent': self._pct(gate_snap_ok, total)},
            'gate_ws': {'success': gate_ws_ok, 'total': total,
                        'percent': self._pct(gate_ws_ok, total)},
            'binance_ws': {'success': binance_ws_ok, 'total': total,
                           'percent': self._pct(binance_ws_ok, total)},
            'binance_data': {'success': binance_data_ok, 'total': total,
                             'percent': self._pct(binance_data_ok, total)},
        }

    @staticmethod
    def _pct(n: int, total: int) -> int:
        return min(100, int(n * 100 / total)) if total > 0 else 0

    def shutdown(self):
        """清理资源（lifespan 退出时调用）"""
        self._cancel_event.set()
        if self.gate_manager:
            self.gate_manager.shutdown()
        if self.spot_manager:
            self.spot_manager.shutdown()

    # ───── 合约加载 ─────

    def _allowed_strategy_tiers(self) -> List[str]:
        raw = config.get('orderbook.strategy_tiers', ['A', 'B'])
        if isinstance(raw, str):
            tiers = [part.strip().upper() for part in raw.split(',')]
        elif isinstance(raw, (list, tuple, set)):
            tiers = [str(part).strip().upper() for part in raw]
        else:
            tiers = ['A', 'B']
        tiers = [tier for tier in tiers if tier in ('A', 'B', 'C')]
        return tiers or ['A', 'B']

    def fetch_contracts_from_db(self) -> List[str]:
        """从 mi_base_asset 查询有效 base_asset，拼接为 Gate 合约名。

        启动订阅前按策略分层、可交易性和成交量做基础过滤。
        默认只订阅 A/B 分层，C 分层不进入 WS，以减少订阅、合并、广播和策略扫描压力。
        资金费率是动态信号，保留到运行时开仓判断，避免启动时为负而漏掉后续转正机会。
        """
        max_contracts = config.get_int('orderbook.max_contracts', 999)
        settle_suffix = self._settle.upper()
        min_spot_volume = config.get_float('trade.filter.min_spot_volume_24h_usdt', 0)
        min_future_volume = config.get_float('trade.filter.min_future_volume_24h_usdt', 0)
        allowed_tiers = self._allowed_strategy_tiers()
        tier_placeholders = ', '.join(['%s'] * len(allowed_tiers))
        sql = """
            SELECT
                b.base_asset,
                COALESCE(b.strategy_tier, 'C') AS strategy_tier,
                g.funding_rate_24h,
                g.volume_24h_settle,
                s.quote_volume
            FROM mi_base_asset b
            INNER JOIN mi_gate_future_contracts g
                ON g.base_asset = UPPER(TRIM(b.base_asset))
               AND g.name = CONCAT(UPPER(TRIM(b.base_asset)), %s)
            INNER JOIN mi_binance_spot_info s
                ON s.base_asset = UPPER(TRIM(b.base_asset))
               AND s.symbol = CONCAT(UPPER(TRIM(b.base_asset)), %s)
            WHERE b.is_valid = 'Y'
              AND COALESCE(b.strategy_tier, 'C') IN ({tier_placeholders})
              AND g.status = 'trading'
              AND s.status = 'TRADING'
              AND s.is_spot_trading_allowed = 1
              AND UPPER(TRIM(b.base_asset)) REGEXP '^[A-Z0-9]+$'
              AND COALESCE(g.volume_24h_settle, 0) >= %s
              AND COALESCE(s.quote_volume, 0) >= %s
            ORDER BY g.funding_rate_24h DESC, g.volume_24h_settle DESC, s.quote_volume DESC
            LIMIT %s
        """.format(tier_placeholders=tier_placeholders)
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(
                    sql,
                    (
                        f"_{settle_suffix}",
                        settle_suffix,
                        *allowed_tiers,
                        min_future_volume,
                        min_spot_volume,
                        max_contracts,
                    ),
                )
                rows = cursor.fetchall()
                contracts = [
                    f"{row['base_asset'].strip().upper()}_{settle_suffix}"
                    for row in rows
                    if row.get('base_asset') and row['base_asset'].strip()
                ]
                tier_counts: Dict[str, int] = {}
                for row in rows:
                    tier = row.get('strategy_tier') or 'C'
                    tier_counts[tier] = tier_counts.get(tier, 0) + 1
                log_print(
                    f"从数据库筛选到 {len(contracts)} 个订阅合约"
                    f"（tiers={allowed_tiers}, tier_counts={tier_counts}, "
                    f"max={max_contracts}, min_spot_vol={min_spot_volume}, "
                    f"min_future_vol={min_future_volume}, funding_filter=runtime）: {contracts}"
                )
                return contracts
        except Exception as e:
            logger.error(f"从数据库获取合约列表失败: {e}，使用默认合约", exc_info=True)
            return ['BTC_USDT', 'ETH_USDT']

    def _get_asset_strategy_tier(self, base_asset: str) -> Optional[str]:
        sql = """
            SELECT COALESCE(strategy_tier, 'C') AS strategy_tier
            FROM mi_base_asset
            WHERE UPPER(TRIM(base_asset)) = %s
            LIMIT 1
        """
        try:
            with db_manager.get_cursor() as cursor:
                cursor.execute(sql, (base_asset.strip().upper(),))
                row = cursor.fetchone()
            return row.get('strategy_tier') if row else None
        except Exception as e:
            logger.warning(f"查询 {base_asset} strategy_tier 失败: {e}")
            return None

    # ───── 手动重试 ─────

    def retry_contract(self, base_asset: str) -> Tuple[bool, str]:
        """
        手动重试单个标的：重新订阅 Gate OBU + Binance WS

        Args:
            base_asset: 标的资产名，如 'CTK'

        Returns:
            (ok, message)
        """
        if self._state != SERVICE_RUNNING:
            return False, '服务未运行，无法重试'

        if not self.gate_manager:
            return False, 'Gate manager 未初始化'

        ba = base_asset.strip().upper()
        contract = f"{ba}_{self._settle.upper()}"
        symbol = f"{ba}USDT"
        tier = self._get_asset_strategy_tier(ba)
        allowed_tiers = self._allowed_strategy_tiers()
        if tier not in allowed_tiers:
            return False, f'{ba} strategy_tier={tier or "NA"} 不在订阅白名单 {allowed_tiers}，不订阅'

        # 1. Gate OBU 订阅
        with self._conn_lock:
            if ba in self._connection_status:
                self._connection_status[ba]['gate_snapshot'] = CONN_PENDING
                self._connection_status[ba]['gate_snapshot_error'] = None
            else:
                self._connection_status[ba] = {
                    'contract': contract,
                    'symbol': symbol,
                    'gate_snapshot': CONN_PENDING,
                    'gate_snapshot_error': None,
                    'gate_ws_subscribed': False,
                    'binance_ws_subscribed': False,
                }

        try:
            self.gate_manager.prepare_contracts([contract])
            if not self.gate_manager.subscribe_contract(contract):
                return False, f'{contract} Gate OBU 订阅失败'

            with self._conn_lock:
                self._connection_status[ba]['gate_snapshot'] = CONN_PENDING
                self._connection_status[ba]['gate_snapshot_error'] = None
                self._connection_status[ba]['gate_ws_subscribed'] = True

        except Exception as e:
            error_msg = str(e)[:200]
            with self._conn_lock:
                self._connection_status[ba]['gate_snapshot'] = CONN_FAILED
                self._connection_status[ba]['gate_snapshot_error'] = error_msg
            logger.error(f'重试 {contract} Gate OBU 订阅失败: {e}', exc_info=True)
            return False, f'Gate OBU订阅失败: {error_msg}'

        # 2. Binance Spot WS 订阅
        try:
            if self.spot_manager:
                if symbol not in self.spot_manager.orderbooks:
                    self.spot_manager.add_symbol(symbol, base_asset=ba)
                with self._conn_lock:
                    self._connection_status[ba]['binance_ws_subscribed'] = True
        except Exception as e:
            logger.warning(f'重试 {symbol} Binance订阅失败（非关键）: {e}')

        log_print(f'✓ 手动重试 {ba} 成功')
        return True, f'{ba} 初始化成功'

    # ───── 内部：启停实现 ─────

    def _run_start(self):
        try:
            self._state = SERVICE_STARTING
            self._error = None
            self._cancel_event.clear()
            self._startup_gate_snapshot_done = 0
            self._startup_gate_snapshot_total = 0
            self._startup_gate_ws_done = 0
            self._startup_gate_ws_total = 0

            contracts = [c.strip() for c in self.fetch_contracts_from_db() if c and c.strip()]
            self._startup_gate_snapshot_total = len(contracts)
            with self._conn_lock:
                for contract in contracts:
                    base_asset = contract.split('_')[0] if '_' in contract else contract
                    self._connection_status[base_asset] = {
                        'contract': contract,
                        'symbol': f"{base_asset}USDT",
                        'gate_snapshot': CONN_PENDING,
                        'gate_snapshot_error': None,
                        'gate_ws_subscribed': False,
                        'binance_ws_subscribed': False,
                    }
            self._push_progress()

            if self._cancel_event.is_set():
                raise InterruptedError('启动已取消')

            self.gate_manager.prepare_contracts(contracts)

            self.gate_manager.start_ws()
            gate_ws_clients = getattr(self.gate_manager, 'ws_clients', None) or []
            gate_ws_connected = (
                bool(gate_ws_clients)
                and all(c._connected_event.is_set() for c in gate_ws_clients)
            )
            if not gate_ws_connected:
                raise RuntimeError('Gate WebSocket 连接超时')

            self._startup_gate_ws_total = len(contracts)
            self._startup_gate_ws_done = 0
            self._push_progress()

            def on_sub_progress(current: int, total: int):
                self._startup_gate_ws_done = current
                self._push_progress()

            self.gate_manager.subscribe_all(contracts, on_progress=on_sub_progress)
            self._startup_gate_ws_done = len(contracts)
            self._push_progress()

            # 标记 Gate WS 订阅状态
            with self._conn_lock:
                for contract in contracts:
                    ba = contract.split('_')[0] if '_' in contract else contract
                    if ba in self._connection_status:
                        self._connection_status[ba]['gate_ws_subscribed'] = True

            self._push_snapshot_now()
            self._start_binance_spot_ws(contracts)

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

            # 使用子线程执行 cleanup，设置总超时保护，避免无限挂起
            cleanup_thread = threading.Thread(target=self._cleanup, daemon=True)
            cleanup_thread.start()
            cleanup_thread.join(timeout=15)  # 最多等 15 秒
            if cleanup_thread.is_alive():
                logger.warning('ℹ cleanup 超时，强制继续（WS 线程将作为 daemon 自动回收）')

            self._state = SERVICE_IDLE
            self._error = None
            log_print('✓ 跨交易所订单簿 WS 服务已终止')

            if self._event_loop and self._broadcast_queue and self._build_payload_fn:
                try:
                    payload = self._build_payload_fn()
                    asyncio.run_coroutine_threadsafe(
                        self._broadcast_queue.put(payload), self._event_loop
                    )
                except Exception:
                    pass  # 广播失败不影响状态转移

        except Exception as e:
            logger.exception(f'终止失败: {e}')
            self._error = str(e)
            self._state = SERVICE_IDLE  # 即使失败也转移到 idle，避免卡死
        finally:
            self._stop_thread = None

    def _cleanup(self):
        with self._cleanup_lock:
            if self.gate_manager:
                self.gate_manager.shutdown()
            if self.spot_manager:
                self.spot_manager.shutdown()
            self._startup_gate_snapshot_done = 0
            self._startup_gate_snapshot_total = 0
            self._startup_gate_ws_done = 0
            self._startup_gate_ws_total = 0
            with self._conn_lock:
                self._connection_status.clear()

    def _start_binance_spot_ws(self, contracts: List[str]):
        spot_items = contracts_to_spot_items(contracts)
        if not spot_items:
            log_print('⚠ 无可用 Binance 现货交易对，跳过 Spot WS')
            return

        log_print(f"▶ 启动 Binance Spot WS，订阅 {len(spot_items)} 个交易对")
        self.spot_manager.add_symbols(spot_items)
        self.spot_manager.start_ws()

        binance_ws_clients = getattr(self.spot_manager, 'ws_clients', None) or []
        binance_ws_connected = (
            bool(binance_ws_clients)
            and all(c._connected_event.is_set() for c in binance_ws_clients)
        )
        if not binance_ws_connected:
            raise RuntimeError('Binance Spot WebSocket 连接超时')

        # 标记 Binance WS 订阅状态
        with self._conn_lock:
            for item in spot_items:
                sym = item['symbol'] if isinstance(item, dict) else item
                ba = item.get('base_asset', sym.replace('USDT', '')) if isinstance(item, dict) else sym.replace('USDT', '')
                if ba in self._connection_status:
                    self._connection_status[ba]['binance_ws_subscribed'] = True

        log_print('✓ Binance Spot WS 已连接')

    # ───── 内部：进度与连接状态 ─────

    def _gate_ws_connected(self) -> bool:
        if not self.gate_manager:
            return False
        clients = getattr(self.gate_manager, 'ws_clients', None)
        if clients:
            return all(c.is_running and c._connected_event.is_set() for c in clients)
        if not self.gate_manager.ws_client:
            return False
        return self.gate_manager.ws_client.is_running and self.gate_manager.ws_client._connected_event.is_set()

    def _binance_ws_connected(self) -> bool:
        if not self.spot_manager:
            return False
        clients = getattr(self.spot_manager, 'ws_clients', None)
        if clients:
            return all(c.is_running and c._connected_event.is_set() for c in clients)
        if not self.spot_manager.ws_client:
            return False
        return self.spot_manager.ws_client.is_running and self.spot_manager.ws_client._connected_event.is_set()

    def _calc_gate_data_age_ms(self) -> Optional[int]:
        """计算 Gate WS 数据新鲜度 p50（订单簿距今多少 ms），作为延迟代理指标"""
        if not self.gate_manager or not self.gate_manager.orderbooks:
            return None
        ages = []
        now = time.time()
        for ob in self.gate_manager.orderbooks.values():
            if ob.last_update_time > 0:
                ages.append((now - ob.last_update_time) * 1000)
        if not ages:
            return None
        return int(self._p50(ages))

    def _calc_binance_data_age_ms(self) -> Optional[int]:
        """计算 Binance WS 数据新鲜度 p50（订单簿距今多少 ms），作为延迟代理指标"""
        if not self.spot_manager or not self.spot_manager.orderbooks:
            return None
        ages = []
        now = time.time()
        for ob in self.spot_manager.orderbooks.values():
            last_update = getattr(ob, 'last_update_time', 0) or ob.update_time
            if last_update > 0:
                ages.append((now - last_update) * 1000)
        if not ages:
            return None
        return int(self._p50(ages))

    @staticmethod
    def _p50(values: List[float]) -> float:
        ordered = sorted(values)
        index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.5))
        return ordered[index]

    def _freshness_summary(self, side: str) -> dict:
        now = time.time()
        if side == 'gate':
            books = self.gate_manager.orderbooks if self.gate_manager else {}
            key_name = 'contract'
            rows = [
                (getattr(ob, key_name, key), (now - ob.last_update_time) * 1000)
                for key, ob in books.items()
                if getattr(ob, 'last_update_time', 0) > 0
            ]
        else:
            books = self.spot_manager.orderbooks if self.spot_manager else {}
            key_name = 'symbol'
            rows = []
            for key, ob in books.items():
                last_update = getattr(ob, 'last_update_time', 0) or getattr(ob, 'update_time', 0)
                if last_update > 0:
                    rows.append((getattr(ob, key_name, key), (now - last_update) * 1000))

        ages = [age for _, age in rows]
        if not ages:
            return {'count': 0}
        ordered = sorted(ages)
        def pct(q: float) -> int:
            return int(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * q))])
        worst = sorted(rows, key=lambda item: item[1], reverse=True)[:10]
        return {
            'count': len(ages),
            'p50_ms': pct(0.5),
            'p90_ms': pct(0.9),
            'p99_ms': pct(0.99),
            'max_ms': int(ordered[-1]),
            'over_200ms': sum(1 for age in ages if age > 200),
            'over_1000ms': sum(1 for age in ages if age > 1000),
            'over_5000ms': sum(1 for age in ages if age > 5000),
            'worst': [{'key': key, 'age_ms': int(age)} for key, age in worst],
        }

    @staticmethod
    def _ws_client_metrics(manager) -> List[dict]:
        if not manager:
            return []
        clients = getattr(manager, 'ws_clients', None) or []
        metrics = []
        for index, client in enumerate(clients):
            if hasattr(client, 'get_metrics'):
                item = client.get_metrics()
            else:
                item = {}
            item['index'] = index
            metrics.append(item)
        return metrics

    def _push_progress(self):
        """WS 广播进度更新"""
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
