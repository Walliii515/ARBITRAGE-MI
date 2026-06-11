# coding: utf-8
"""
独立盘口数据服务。

该进程只负责：
- Binance Spot WS 订阅与本地现货盘口维护
- Gate Futures OBU WS 订阅与本地合约盘口维护
- 输出原始/合并盘口快照与连接状态

常规交易业务逻辑应运行在 orderbook_server，并通过 HTTP 客户端消费本服务。
"""
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.merge_cross_exchange_orderbook import merge_orderbook_records
from calc.service_lifecycle import ServiceLifecycleManager, SERVICE_IDLE
from common.config import config
from common.logger import get_logger, log_print, setup_logging

setup_logging()
logger = get_logger(__name__)

SETTLE = config.get_str('orderbook.settle', 'usdt', env='ORDERBOOK_SETTLE')
svc: ServiceLifecycleManager = ServiceLifecycleManager(settle=SETTLE)
_last_raw_snapshot_metrics = {}
_last_merged_snapshot_metrics = {}
_last_book_metrics = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc.init_managers()
    auto_start = config.get_bool('orderbook.auto_start', False)
    if auto_start:
        log_print('ℹ 盘口数据服务 auto_start=true，自动启动 WS 订阅...')
        svc.start()
    yield
    svc.shutdown()


app = FastAPI(title='OrderBook Data Service', lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173', '*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/api/health')
async def health():
    status = svc.get_status()
    return {
        'status': 'ok',
        'service_state': status.get('state', SERVICE_IDLE),
        'gate_ws_connected': status.get('gate_ws_connected', False),
        'binance_ws_connected': status.get('binance_ws_connected', False),
    }


@app.get('/api/service/status')
async def service_status():
    return svc.get_status()


@app.get('/api/service/diagnostics')
async def service_diagnostics():
    data = svc.get_diagnostics()
    data['raw_snapshot'] = _last_raw_snapshot_metrics
    data['merged_snapshot'] = _last_merged_snapshot_metrics
    data['single_book'] = _last_book_metrics
    return data


@app.get('/api/service/connections')
async def service_connections():
    return {
        'items': svc.get_connection_status(),
        'state': svc.state,
        'gate_ws_connected': svc._gate_ws_connected(),
        'binance_ws_connected': svc._binance_ws_connected(),
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms(),
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms(),
    }


@app.post('/api/service/start')
async def service_start():
    ok, message = svc.start()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@app.post('/api/service/stop')
async def service_stop():
    ok, message = svc.stop()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@app.post('/api/service/retry-snapshot')
async def retry_snapshot(body: dict):
    base_asset = (body.get('base_asset') or '').strip()
    force = bool(body.get('force'))
    if not base_asset:
        raise HTTPException(status_code=400, detail='base_asset 不能为空')
    ok, message = svc.retry_contract(base_asset, force=force)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {'ok': True, 'message': message}


@app.get('/api/orderbook/raw-snapshot')
async def raw_snapshot():
    global _last_raw_snapshot_metrics
    start = time.perf_counter()
    future_rows = svc.gate_manager.to_records() if svc.gate_manager else []
    future_ms = (time.perf_counter() - start) * 1000
    spot_start = time.perf_counter()
    spot_rows = svc.spot_manager.to_records() if svc.spot_manager else []
    spot_ms = (time.perf_counter() - spot_start) * 1000
    merge_start = time.perf_counter()
    rows = merge_orderbook_records(future_rows, spot_rows)
    merge_ms = (time.perf_counter() - merge_start) * 1000
    total_ms = (time.perf_counter() - start) * 1000
    _last_raw_snapshot_metrics = {
        'total_ms': round(total_ms, 2),
        'future_to_records_ms': round(future_ms, 2),
        'spot_to_records_ms': round(spot_ms, 2),
        'merge_ms': round(merge_ms, 2),
        'future_rows': len(future_rows),
        'spot_rows': len(spot_rows),
        'merged_rows': len(rows),
        'at': time.time(),
    }
    if total_ms > 500:
        logger.warning(f'raw_snapshot 构建偏慢: {_last_raw_snapshot_metrics}')
    return {
        'state': svc.state,
        'gate_ws_connected': svc._gate_ws_connected(),
        'binance_ws_connected': svc._binance_ws_connected(),
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms(),
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms(),
        'future_rows': future_rows,
        'spot_rows': spot_rows,
        'rows': rows,
    }


@app.get('/api/orderbook/merged-snapshot')
async def merged_snapshot():
    """返回交易业务最常用的合并行，避免传输 future/spot/merged 三份全量数据。"""
    global _last_merged_snapshot_metrics
    start = time.perf_counter()
    future_rows = svc.gate_manager.to_records() if svc.gate_manager else []
    future_ms = (time.perf_counter() - start) * 1000
    spot_start = time.perf_counter()
    spot_rows = svc.spot_manager.to_records() if svc.spot_manager else []
    spot_ms = (time.perf_counter() - spot_start) * 1000
    merge_start = time.perf_counter()
    rows = merge_orderbook_records(future_rows, spot_rows)
    merge_ms = (time.perf_counter() - merge_start) * 1000
    total_ms = (time.perf_counter() - start) * 1000
    _last_merged_snapshot_metrics = {
        'total_ms': round(total_ms, 2),
        'future_to_records_ms': round(future_ms, 2),
        'spot_to_records_ms': round(spot_ms, 2),
        'merge_ms': round(merge_ms, 2),
        'future_rows': len(future_rows),
        'spot_rows': len(spot_rows),
        'merged_rows': len(rows),
        'at': time.time(),
    }
    if total_ms > 500:
        logger.warning(f'merged_snapshot 构建偏慢: {_last_merged_snapshot_metrics}')
    return {
        'state': svc.state,
        'gate_ws_connected': svc._gate_ws_connected(),
        'binance_ws_connected': svc._binance_ws_connected(),
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms(),
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms(),
        'rows': rows,
    }


@app.get('/api/orderbook/book')
async def single_book(
    contract: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
):
    """返回单标的盘口，供开/平仓最终旁路校验使用。"""
    global _last_book_metrics
    start = time.perf_counter()
    contract_key = (contract or '').strip()
    symbol_key = (symbol or '').strip().upper()

    future_row = None
    spot_row = None
    if contract_key and svc.gate_manager:
        gate_ob = svc.gate_manager.get_orderbook(contract_key)
        future_row = gate_ob.to_dict_row() if gate_ob else None
    if symbol_key and svc.spot_manager:
        spot_ob = svc.spot_manager.get_orderbook(symbol_key)
        spot_row = spot_ob.to_dict_row() if spot_ob else None

    rows = merge_orderbook_records([future_row], [spot_row] if spot_row else []) if future_row else []
    total_ms = (time.perf_counter() - start) * 1000
    _last_book_metrics = {
        'total_ms': round(total_ms, 2),
        'contract': contract_key,
        'symbol': symbol_key,
        'has_future': future_row is not None,
        'has_spot': spot_row is not None,
        'at': time.time(),
    }
    if total_ms > 100:
        logger.warning(f'single_book 构建偏慢: {_last_book_metrics}')
    return {
        'state': svc.state,
        'future_row': future_row,
        'spot_row': spot_row,
        'row': rows[0] if rows else None,
    }


def main():
    import uvicorn

    host = config.get_str('orderbook.data_service_host', '0.0.0.0', env='ORDERBOOK_DATA_SERVICE_HOST')
    port = config.get_int('orderbook.data_service_port', 19877, env='ORDERBOOK_DATA_SERVICE_PORT')
    log_print(f'启动独立盘口数据服务 http://{host}:{port}')
    log_print('该服务仅维护 Binance/Gate WS 与本地盘口，不承载交易业务逻辑')
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    main()
