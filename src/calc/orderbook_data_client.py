# coding: utf-8
"""
独立盘口服务客户端。

业务服务通过该客户端消费 orderbook_data_service 暴露的快照/状态接口，
避免在 orderbook_server 进程内直接维护 Binance/Gate WebSocket 与本地盘口。
"""
import json
import time
import urllib.error
import urllib.request
from copy import deepcopy
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from calc.service_lifecycle import SERVICE_IDLE
from common.config import config
from common.logger import get_logger

logger = get_logger(__name__)


class RemoteOrderBook:
    """把远端一行盘口记录适配成 executor 旁路风控期望的本地簿接口。"""

    def __init__(self, row: Dict, ready_key: str):
        self._row = row
        self._ready_key = ready_key
        self.last_update_time = float(row.get('last_update_time') or 0)
        self.update_time = float(row.get('last_update_time') or row.get('update_time') or 0)
        self.update_count = int(row.get('update_count') or 0)

    def to_dict_row(self) -> Dict:
        return dict(self._row)

    def is_ready(self) -> bool:
        return bool(self._row.get(self._ready_key, True))


class RemoteGateManager:
    def __init__(self, client: 'OrderBookDataClient'):
        self._client = client

    def to_records(self) -> List[Dict]:
        return self._client.get_future_rows()

    def get_orderbook(self, contract: str) -> Optional[RemoteOrderBook]:
        for row in self.to_records():
            if row.get('contract') == contract:
                return RemoteOrderBook(row, 'future_ready')
        return None

    def get_all_contracts(self) -> List[str]:
        status = self._client.get_status()
        return status.get('contracts', [])


class RemoteSpotManager:
    def __init__(self, client: 'OrderBookDataClient'):
        self._client = client

    def to_records(self) -> List[Dict]:
        return self._client.get_spot_rows()

    def get_orderbook(self, symbol: str) -> Optional[RemoteOrderBook]:
        symbol = symbol.upper()
        for row in self.to_records():
            if str(row.get('symbol', '')).upper() == symbol:
                return RemoteOrderBook(row, 'spot_ready')
        return None

    def get_all_symbols(self) -> List[str]:
        status = self._client.get_status()
        return status.get('spot_symbols', [])


class OrderBookDataClient:
    """HTTP 客户端，兼容 orderbook_server 原先使用的生命周期接口。"""

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[float] = None):
        self.base_url = (base_url or config.get_str(
            'orderbook.data_service_url',
            'http://127.0.0.1:19877',
            env='ORDERBOOK_DATA_SERVICE_URL',
        )).rstrip('/')
        self.timeout = float(timeout or config.get_float('orderbook.data_service_timeout_sec', 2.0))
        self._status_cache: Tuple[float, Dict] = (0.0, {'state': SERVICE_IDLE})
        self._raw_cache: Tuple[float, Dict] = (0.0, {'future_rows': [], 'spot_rows': [], 'rows': []})
        self._status_ttl = 0.5
        self._raw_ttl = 0.05
        self.gate_manager = RemoteGateManager(self)
        self.spot_manager = RemoteSpotManager(self)
        self._request_metrics: Dict[str, Dict] = defaultdict(lambda: {
            'count': 0,
            'total_ms': 0.0,
            'max_ms': 0.0,
            'slow_count': 0,
            'last_ms': 0.0,
            'last_at': 0.0,
        })

    def _request(self, method: str, path: str, body: Optional[Dict] = None) -> Dict:
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = urllib.request.Request(
            f'{self.base_url}{path}',
            data=data,
            method=method,
            headers={'Content-Type': 'application/json'},
        )
        try:
            start = time.perf_counter()
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
            elapsed_ms = (time.perf_counter() - start) * 1000
            self._record_request_metric(path, elapsed_ms)
            return json.loads(payload.decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='ignore')
            raise RuntimeError(f'盘口服务请求失败 {method} {path}: HTTP {e.code} {detail}') from e

    def _record_request_metric(self, path: str, elapsed_ms: float) -> None:
        metric = self._request_metrics[path]
        metric['count'] += 1
        metric['total_ms'] += elapsed_ms
        metric['max_ms'] = max(metric['max_ms'], elapsed_ms)
        metric['last_ms'] = elapsed_ms
        metric['last_at'] = time.time()
        if elapsed_ms > 500:
            metric['slow_count'] += 1

    @property
    def state(self) -> str:
        return self.get_status().get('state', SERVICE_IDLE)

    def get_status(self, force: bool = False) -> Dict:
        now = time.time()
        ts, cached = self._status_cache
        if not force and now - ts < self._status_ttl:
            return cached
        try:
            status = self._request('GET', '/api/service/status')
            self._status_cache = (now, status)
            return status
        except Exception as e:
            logger.warning(f'读取盘口服务状态失败: {e}')
            fallback = {'state': SERVICE_IDLE, 'error': str(e)}
            self._status_cache = (now, fallback)
            return fallback

    def get_connection_status(self) -> List[Dict]:
        return self._request('GET', '/api/service/connections').get('items', [])

    def get_diagnostics(self) -> Dict:
        data = self._request('GET', '/api/service/diagnostics')
        data['client_request_metrics'] = self.get_request_metrics()
        return data

    def get_request_metrics(self) -> Dict:
        result = {}
        for path, metric in self._request_metrics.items():
            count = metric['count'] or 1
            result[path] = {
                'count': metric['count'],
                'avg_ms': round(metric['total_ms'] / count, 2),
                'max_ms': round(metric['max_ms'], 2),
                'last_ms': round(metric['last_ms'], 2),
                'last_at': metric['last_at'],
                'slow_count': metric['slow_count'],
            }
        return result

    def get_raw_snapshot(self, force: bool = False) -> Dict:
        now = time.time()
        ts, cached = self._raw_cache
        if not force and now - ts < self._raw_ttl:
            return deepcopy(cached)
        try:
            payload = self._request('GET', '/api/orderbook/raw-snapshot')
            self._raw_cache = (now, payload)
            return deepcopy(payload)
        except Exception as e:
            logger.warning(f'读取盘口快照失败: {e}')
            return deepcopy(cached)

    def get_future_rows(self) -> List[Dict]:
        return self.get_raw_snapshot().get('future_rows', [])

    def get_spot_rows(self) -> List[Dict]:
        return self.get_raw_snapshot().get('spot_rows', [])

    def get_merged_rows(self, force: bool = False) -> List[Dict]:
        return self.get_raw_snapshot(force=force).get('rows', [])

    def start(self) -> Tuple[bool, str]:
        res = self._request('POST', '/api/service/start')
        self.get_status(force=True)
        return bool(res.get('ok')), res.get('message', '')

    def stop(self) -> Tuple[bool, str]:
        res = self._request('POST', '/api/service/stop')
        self.get_status(force=True)
        return bool(res.get('ok')), res.get('message', '')

    def retry_contract(self, base_asset: str) -> Tuple[bool, str]:
        res = self._request('POST', '/api/service/retry-snapshot', {'base_asset': base_asset})
        return bool(res.get('ok')), res.get('message', '')

    def shutdown(self) -> None:
        pass

    def _gate_ws_connected(self) -> bool:
        return bool(self.get_status().get('gate_ws_connected', False))

    def _binance_ws_connected(self) -> bool:
        return bool(self.get_status().get('binance_ws_connected', False))

    def _calc_gate_data_age_ms(self):
        return self.get_status().get('gate_ws_latency_ms')

    def _calc_binance_data_age_ms(self):
        return self.get_status().get('binance_ws_latency_ms')
