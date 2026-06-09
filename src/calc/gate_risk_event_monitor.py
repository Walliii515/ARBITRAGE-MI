# coding: utf-8
"""Gate 私有风险事件实时监听。

订阅 Gate futures ADL / 强平事件，收到事件后立即触发裸 spot 自动处置。
对账模块仍保留为审计和兜底，不再承担实时交易动作。
"""
import hashlib
import hmac
import json
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

import websocket

from calc.exchange_desync_remediator import (
    ExchangeDesyncRemediationConfig,
    ExchangeDesyncRemediator,
)
from calc.real_executor import RealExecutor
from calc.reconciliation import build_exchange_config
from common.config import config
from common.database import db_manager
from common.logger import get_logger, log_print
from common.meta_loader import fetch_contract_meta, fetch_spot_meta

logger = get_logger(__name__)


GATE_RISK_CHANNELS = ('futures.auto_deleverages', 'futures.liquidates')


@dataclass
class GateRiskEventMonitorConfig:
    enabled: bool = True
    settle: str = 'usdt'
    channels: List[str] = field(default_factory=lambda: list(GATE_RISK_CHANNELS))
    subscribe_all: bool = True
    reconnect_delay_sec: float = 3.0
    max_reconnect_delay_sec: float = 60.0
    reconnect_jitter_sec: float = 2.0
    connect_timeout_sec: float = 15.0
    rest_catchup_enabled: bool = True
    rest_catchup_lookback_sec: int = 300


def build_default_gate_risk_event_monitor() -> 'GateRiskEventMonitor':
    """按当前实盘配置构建 Gate 风险事件监听器。"""
    contract_meta = fetch_contract_meta()
    spot_meta = fetch_spot_meta()
    executor = RealExecutor(
        build_exchange_config(),
        contract_meta=contract_meta,
        spot_meta=spot_meta,
        leverage=config.get_int('margin.leverage', 2),
    )
    remediation_cfg = ExchangeDesyncRemediationConfig(
        enabled=config.get_bool('exchange_risk_monitor.auto_remediate.enabled', True),
        action=str(config.get('exchange_risk_monitor.auto_remediate.action', 'sell_spot') or 'sell_spot'),
        max_positions_per_run=config.get_int('exchange_risk_monitor.auto_remediate.max_positions_per_event', 20),
        min_spot_qty=config.get_float('exchange_risk_monitor.auto_remediate.min_spot_qty', 0.0),
        spot_open_fee=config.get_float('trade.fee.spot_open', 0.00075),
        spot_close_fee=config.get_float('trade.fee.spot_close', 0.00075),
        future_open_fee=config.get_float('trade.fee.future_open', 0.0002),
        future_close_fee=config.get_float('trade.fee.future_close', 0.0002),
        future_taker_open_fee=config.get_float('trade.fee.future_taker_open', 0.0005),
        future_taker_close_fee=config.get_float('trade.fee.future_taker_close', 0.0005),
    )
    monitor_cfg = GateRiskEventMonitorConfig(
        enabled=config.get_bool('exchange_risk_monitor.enabled', True),
        settle=config.get_str('exchange_risk_monitor.settle', 'usdt'),
        channels=list(config.get('exchange_risk_monitor.channels', list(GATE_RISK_CHANNELS))),
        subscribe_all=config.get_bool('exchange_risk_monitor.subscribe_all', True),
        reconnect_delay_sec=config.get_float('exchange_risk_monitor.reconnect_delay_sec', 3.0),
        max_reconnect_delay_sec=config.get_float('exchange_risk_monitor.max_reconnect_delay_sec', 60.0),
        reconnect_jitter_sec=config.get_float('exchange_risk_monitor.reconnect_jitter_sec', 2.0),
        connect_timeout_sec=config.get_float('exchange_risk_monitor.connect_timeout_sec', 15.0),
        rest_catchup_enabled=config.get_bool('exchange_risk_monitor.rest_catchup_enabled', True),
        rest_catchup_lookback_sec=config.get_int('exchange_risk_monitor.rest_catchup_lookback_sec', 300),
    )
    return GateRiskEventMonitor(executor, monitor_cfg, remediation_cfg)


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _event_time(value) -> datetime:
    ts = _float(value)
    if ts <= 0:
        return datetime.now()
    if ts > 10_000_000_000:
        ts = ts / 1000.0
    return datetime.fromtimestamp(ts)


def _base_from_contract(contract: str) -> str:
    contract = str(contract or '').upper()
    return contract[:-5] if contract.endswith('_USDT') else contract.split('_', 1)[0]


class GateRiskEventMonitor:
    """Gate 私有 ADL/强平事件监听器。"""

    def __init__(
        self,
        executor: RealExecutor,
        cfg: GateRiskEventMonitorConfig,
        remediation_cfg: ExchangeDesyncRemediationConfig,
    ):
        self.executor = executor
        self.cfg = cfg
        self.remediator = ExchangeDesyncRemediator(executor, remediation_cfg)
        self.ws_url = self._ws_url()
        self.ws: Optional[websocket.WebSocketApp] = None
        self.ws_thread: Optional[threading.Thread] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.reconnect_thread: Optional[threading.Thread] = None
        self.connected = threading.Event()
        self.stop_event = threading.Event()
        self.events: 'queue.Queue[Dict]' = queue.Queue(maxsize=1000)
        self._send_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._last_catchup_at = 0.0
        self._started_at: Optional[float] = None
        self._connected_at: Optional[float] = None
        self._last_message_at: Optional[float] = None
        self._last_event_at: Optional[float] = None
        self._last_close_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._message_count = 0
        self._event_count = 0
        self._subscription_status: Dict[str, str] = {channel: 'pending' for channel in self.cfg.channels}

    def start(self):
        if not self.cfg.enabled:
            logger.info('Gate 风险事件监听已关闭')
            return
        if not self.executor.config.gate_api_key or not self.executor.config.gate_api_secret:
            logger.error('Gate 风险事件监听缺少 API key/secret，无法启动')
            return

        self.stop_event.clear()
        with self._status_lock:
            self._started_at = time.time()
            self._last_error = None
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name='gate-risk-worker')
        self.worker_thread.start()
        self._connect()
        log_print(f"✓ Gate 风险事件监听已启动: {self.ws_url}")

    def stop(self):
        self.stop_event.set()
        self.connected.clear()
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
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=3)

    def _ws_url(self) -> str:
        settle = str(self.cfg.settle or 'usdt').lower()
        if self.executor.config.env == 'mainnet':
            return f"wss://fx-ws.gateio.ws/v4/ws/{settle}"
        return f"wss://fx-ws-testnet.gateio.ws/v4/ws/{settle}"

    def _connect(self):
        self.connected.clear()
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True, name='gate-risk-ws')
        self.ws_thread.start()
        if not self.connected.wait(timeout=max(float(self.cfg.connect_timeout_sec or 1), 1.0)):
            logger.error('Gate 风险事件 WS 连接超时: %s', self.ws_url)

    def _on_open(self, ws):
        self.connected.set()
        with self._status_lock:
            self._connected_at = time.time()
            self._last_error = None
            self._subscription_status = {channel: 'pending' for channel in self.cfg.channels}
        logger.info('Gate 风险事件 WS 已连接')
        for channel in self.cfg.channels:
            self._subscribe(channel)
        self._enqueue_rest_catchup()

    def _subscribe(self, channel: str):
        payload = ['!all'] if self.cfg.subscribe_all else []
        ts = int(time.time())
        msg = {
            'time': ts,
            'channel': channel,
            'event': 'subscribe',
            'payload': payload,
            'auth': self._auth(channel, 'subscribe', ts),
        }
        with self._send_lock:
            if self.ws and self.ws.sock and self.ws.sock.connected:
                self.ws.send(json.dumps(msg))
        logger.info('Gate 风险事件订阅发送 | channel=%s | payload=%s', channel, payload)

    def _auth(self, channel: str, event: str, ts: int) -> Dict:
        sign_string = f"channel={channel}&event={event}&time={ts}"
        sign = hmac.new(
            self.executor.config.gate_api_secret.encode('utf-8'),
            sign_string.encode('utf-8'),
            hashlib.sha512,
        ).hexdigest()
        return {
            'method': 'api_key',
            'KEY': self.executor.config.gate_api_key,
            'SIGN': sign,
        }

    def _on_message(self, ws, message: str):
        self._record_message()
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning('Gate 风险事件 WS JSON 解析失败: %s', message[:200])
            return

        event = data.get('event')
        channel = data.get('channel')
        if event == 'subscribe':
            result = data.get('result') or {}
            status = result.get('status') if isinstance(result, dict) else None
            with self._status_lock:
                self._subscription_status[str(channel)] = str(status or 'unknown')
            logger.info('Gate 风险事件订阅确认 | channel=%s | result=%s', channel, data.get('result'))
            return
        if channel not in GATE_RISK_CHANNELS:
            return

        for item in self._iter_result_items(data.get('result')):
            normalized = normalize_gate_risk_event(channel, item)
            if normalized:
                self._record_event()
                self._enqueue_event(normalized)

    def _iter_result_items(self, result) -> Iterable[Dict]:
        if isinstance(result, list):
            for item in result:
                if isinstance(item, dict):
                    yield item
        elif isinstance(result, dict):
            yield result

    def _enqueue_event(self, event: Dict):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            logger.error('Gate 风险事件队列已满，丢弃事件: %s', event)

    def _enqueue_rest_catchup(self):
        if not self.cfg.rest_catchup_enabled:
            return
        now = time.time()
        if now - self._last_catchup_at < 10:
            return
        self._last_catchup_at = now
        self._enqueue_event({'_catchup': True})

    def _record_message(self):
        with self._status_lock:
            self._last_message_at = time.time()
            self._message_count += 1

    def _record_event(self):
        with self._status_lock:
            self._last_event_at = time.time()
            self._event_count += 1

    def get_status(self) -> Dict:
        with self._status_lock:
            status = {
                'enabled': self.cfg.enabled,
                'connected': self.connected.is_set(),
                'ws_url': self.ws_url,
                'channels': dict(self._subscription_status),
                'subscribe_all': self.cfg.subscribe_all,
                'last_message_at': self._last_message_at,
                'last_event_at': self._last_event_at,
                'last_close_at': self._last_close_at,
                'last_error': self._last_error,
                'message_count': self._message_count,
                'event_count': self._event_count,
                'started_at': self._started_at,
                'connected_at': self._connected_at,
                'last_catchup_at': self._last_catchup_at or None,
            }
        now = time.time()
        status.update({
            'queue_size': self.events.qsize(),
            'worker_alive': bool(self.worker_thread and self.worker_thread.is_alive()),
            'ws_thread_alive': bool(self.ws_thread and self.ws_thread.is_alive()),
            'message_age_sec': round(now - status['last_message_at'], 1) if status['last_message_at'] else None,
            'event_age_sec': round(now - status['last_event_at'], 1) if status['last_event_at'] else None,
        })
        return status

    def _worker_loop(self):
        while not self.stop_event.is_set():
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if event.get('_catchup'):
                    self._catch_up_recent_events()
                else:
                    self._handle_event(event)
            except Exception as e:
                logger.error('Gate 风险事件处理异常 | event=%s | error=%s', event, e, exc_info=True)
            finally:
                self.events.task_done()

    def _handle_event(self, event: Dict):
        existing_status = self._load_event_status(str(event.get('event_key') or ''))
        if existing_status == 'remediated':
            logger.info('Gate 风险事件已处置，跳过重复事件: %s', event.get('event_key'))
            return
        event_id = self._upsert_event(event, status='received')
        missing_contracts = abs(_float(event.get('future_close_size')))
        if missing_contracts <= 0:
            self._update_event_status(event_id, 'ignored', {'reason': 'invalid_size'})
            return

        result = self.remediator.remediate_gate_short_desync(
            base_asset=event['base_asset'],
            missing_contracts=missing_contracts,
            risk=event,
            require_desynced=False,
            mark_positions=True,
        )
        status = 'remediated' if result.get('success') else 'failed'
        if not result.get('attempted'):
            status = 'ignored'
        self._update_event_status(event_id, status, result)
        if status == 'remediated':
            logger.warning(
                'Gate 风险事件自动处置完成 | type=%s | asset=%s | contracts=%s | result=%s',
                event.get('type'), event.get('base_asset'), missing_contracts, result,
            )
        else:
            logger.error(
                'Gate 风险事件自动处置未完成 | type=%s | asset=%s | contracts=%s | result=%s',
                event.get('type'), event.get('base_asset'), missing_contracts, result,
            )

    def _catch_up_recent_events(self):
        since = datetime.now() - timedelta(seconds=max(int(self.cfg.rest_catchup_lookback_sec or 60), 60))
        start_time = int(since.timestamp())
        end_time = int(time.time())
        for raw in self.executor.fetch_gate_futures_auto_deleverages(start_time=start_time, end_time=end_time):
            event = normalize_gate_risk_event('futures.auto_deleverages', raw)
            if event:
                self._handle_event(event)
        for raw in self.executor.fetch_gate_futures_liquidates(start_time=start_time, end_time=end_time):
            event = normalize_gate_risk_event('futures.liquidates', raw)
            if event:
                self._handle_event(event)

    def _upsert_event(self, event: Dict, status: str) -> int:
        sql = """
            INSERT INTO mi_exchange_risk_event (
                event_key, exchange, market_type, risk_type, base_asset, contract, event_at,
                exchange_order_id, exchange_trade_id, side, size, fill_price, entry_price,
                mark_price, liq_price, pnl, raw_json, status, remediation_action
            ) VALUES (
                %(event_key)s, 'gate', 'future', %(risk_type)s, %(base_asset)s, %(contract)s, %(event_at)s,
                %(exchange_order_id)s, %(exchange_trade_id)s, %(side)s, %(size)s, %(fill_price)s, %(entry_price)s,
                %(mark_price)s, %(liq_price)s, %(pnl)s, %(raw_json)s, %(status)s, 'sell_spot'
            )
            ON DUPLICATE KEY UPDATE
                id = LAST_INSERT_ID(id),
                raw_json = VALUES(raw_json),
                updated_at = CURRENT_TIMESTAMP
        """
        payload = {
            **event,
            'risk_type': event.get('type'),
            'status': status,
            'raw_json': json.dumps(event.get('raw') or {}, ensure_ascii=False, default=str),
        }
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, payload)
            return int(cursor.lastrowid or 0)

    def _load_event_status(self, event_key: str) -> Optional[str]:
        if not event_key:
            return None
        with db_manager.get_cursor() as cursor:
            cursor.execute(
                "SELECT status FROM mi_exchange_risk_event WHERE event_key = %s LIMIT 1",
                (event_key,),
            )
            row = cursor.fetchone()
        return str(row.get('status')) if row else None

    def _update_event_status(self, event_id: int, status: str, result: Dict):
        if not event_id:
            return
        sql = """
            UPDATE mi_exchange_risk_event
            SET status = %s,
                remediation_result = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql, (status, json.dumps(result, ensure_ascii=False, default=str)[:4000], event_id))

    def _on_error(self, ws, error):
        with self._status_lock:
            self._last_error = str(error)[:300]
        logger.error('Gate 风险事件 WS 错误: %s', error)

    def _on_close(self, ws, close_status_code, close_msg):
        self.connected.clear()
        with self._status_lock:
            self._last_close_at = time.time()
            self._last_error = f'closed:{close_status_code}:{close_msg}'[:300]
        logger.warning('Gate 风险事件 WS 关闭: %s - %s', close_status_code, close_msg)
        if not self.stop_event.is_set():
            self._start_reconnect()

    def _start_reconnect(self):
        if self.reconnect_thread and self.reconnect_thread.is_alive():
            return
        self.reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        self.reconnect_thread.start()

    def _reconnect_loop(self):
        delay = max(float(self.cfg.reconnect_delay_sec or 1), 1.0)
        while not self.stop_event.is_set():
            wait_sec = delay + random.uniform(0, max(float(self.cfg.reconnect_jitter_sec or 0), 0))
            time.sleep(wait_sec)
            if self.stop_event.is_set():
                break
            try:
                self._connect()
                if self.connected.is_set():
                    logger.info('Gate 风险事件 WS 重连成功')
                    return
            except Exception as e:
                logger.error('Gate 风险事件 WS 重连异常: %s', e, exc_info=True)
            delay = min(delay * 2, max(float(self.cfg.max_reconnect_delay_sec or 60), delay))


def normalize_gate_risk_event(channel: str, raw: Dict) -> Optional[Dict]:
    """把 Gate ADL/强平 WS 或 REST 事件规范成处置器可用的 risk dict。"""
    if not isinstance(raw, dict):
        return None
    contract = str(raw.get('contract') or '').upper()
    if not contract:
        return None
    risk_type = 'adl' if channel == 'futures.auto_deleverages' else 'liquidation'
    base_asset = _base_from_contract(contract)
    event_at = _event_time(
        raw.get('time_ms')
        or raw.get('time')
        or raw.get('create_time_ms')
        or raw.get('create_time')
    )
    size = abs(_float(
        raw.get('trade_size')
        or raw.get('close_size')
        or raw.get('size')
        or raw.get('position_size')
    ))
    fill_price = _float(
        raw.get('fill_price')
        or raw.get('price')
        or raw.get('order_price')
        or raw.get('liq_price')
        or raw.get('mark_price')
    )
    order_id = str(raw.get('order_id') or raw.get('id') or '')
    trade_id = str(raw.get('trade_id') or raw.get('id') or '')
    event_key = _event_key(risk_type, contract, order_id, trade_id, event_at, size)
    detail = (
        f"Gate{('ADL自动减仓' if risk_type == 'adl' else '强平')}|contract={contract}|"
        f"size={size:g}|price={fill_price:g}|order_id={order_id}|event_key={event_key}"
    )
    return {
        'event_key': event_key,
        'type': risk_type,
        'status': 'desynced',
        'base_asset': base_asset,
        'contract': contract,
        'event_at': event_at,
        'detail': detail,
        'future_close_price': fill_price,
        'future_exchange_order_id': order_id,
        'future_trade_id': trade_id,
        'future_liquidity_role': 'taker',
        'future_close_size': size,
        'future_pnl': _float(raw.get('pnl')) if raw.get('pnl') is not None else None,
        'exchange_order_id': order_id,
        'exchange_trade_id': trade_id,
        'side': raw.get('side') or raw.get('text'),
        'size': size,
        'fill_price': fill_price,
        'entry_price': _float(raw.get('entry_price')) if raw.get('entry_price') is not None else None,
        'mark_price': _float(raw.get('mark_price')) if raw.get('mark_price') is not None else None,
        'liq_price': _float(raw.get('liq_price')) if raw.get('liq_price') is not None else None,
        'pnl': _float(raw.get('pnl')) if raw.get('pnl') is not None else None,
        'raw': raw,
    }


def _event_key(
    risk_type: str,
    contract: str,
    order_id: str,
    trade_id: str,
    event_at: datetime,
    size: float,
) -> str:
    if order_id:
        return f"gate:{risk_type}:{contract}:order:{order_id}"
    if trade_id:
        return f"gate:{risk_type}:{contract}:trade:{trade_id}"
    basis = f"{risk_type}|{contract}|{event_at.isoformat()}|{size:g}"
    digest = hashlib.sha256(basis.encode('utf-8')).hexdigest()[:20]
    return f"gate:{risk_type}:{contract}:{digest}"
