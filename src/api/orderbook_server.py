# coding: utf-8
"""
跨交易所订单簿 FastAPI 桥接服务
合并 Gate 永续与 Binance 现货本地订单簿，通过 WebSocket 推送给前端
支持前端控制 WS 服务的启动/终止及进度查询
"""
import asyncio
import json
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Set

try:
    import orjson
    _HAS_ORJSON = True
except ImportError:
    _HAS_ORJSON = False

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.calculate_hedge_metrics import calculate_hedge_metrics
from calc.merge_cross_exchange_orderbook import merge_orderbook_records
from calc.etl_pipeline import start_daily_schedulers, stop_daily_schedulers, ETL_TASKS, _etl_config
from common.config import config
from common.database import db_manager
from common.logger import get_logger, log_print, setup_logging
from common.meta_loader import fetch_contract_meta, fetch_spot_meta

from api.trading_api import router as trading_router
from api.auth import router as auth_router, verify_token_dependency, verify_ws_token
from calc.trading_executor import TradingExecutor
from calc.position_tracker import PositionTracker
from calc.orderbook_enricher import EnrichConfig, enrich_trading_fields, enrich_snapshot_fields
from calc.position_pnl_calculator import PnlConfig, calculate_realtime_pnl
from calc.vwap_snapshot_recorder import record_vwap_snapshots
from calc.service_lifecycle import ServiceLifecycleManager, SERVICE_IDLE, SERVICE_STARTING, SERVICE_RUNNING, SERVICE_STOPPING

setup_logging()
logger = get_logger(__name__)


def _json_dumps(data) -> str:
    """高性能 JSON 序列化（优先使用 orjson，回退 stdlib json）"""
    if _HAS_ORJSON:
        # orjson 原生支持 float/int/str/None/datetime，Decimal 需转 float
        return orjson.dumps(
            data,
            default=_orjson_default,
            option=orjson.OPT_NON_STR_KEYS,
        ).decode('utf-8')
    else:
        def _default(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            if isinstance(obj, datetime):
                return obj.strftime('%Y-%m-%d %H:%M:%S')
            raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')
        return json.dumps(data, default=_default, ensure_ascii=False)


def _orjson_default(obj):
    """orjson 的 default 回调（仅处理 Decimal）"""
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')

_meta_update_time: str = ''  # 上次缓存刷新时间

# 以下参数从 src/config/config.yaml 加载，环境变量可覆盖
SETTLE = config.get_str('orderbook.settle', 'usdt', env='ORDERBOOK_SETTLE')
BROADCAST_THROTTLE_SEC = config.get_float('orderbook.broadcast_throttle_sec', 1.0)
SNAPSHOT_BATCH_SIZE = config.get_int('orderbook.snapshot_batch_size', 40)
SNAPSHOT_BATCH_WORKERS = config.get_int('orderbook.snapshot_batch_workers', 40)
# 每次开仓金额（USDT），作为新列推送给前端
OPEN_AMOUNT_USDT = config.get_float('trade.open_amount_usdt', 5000.0)
# 资金费率阈值百分位字段名（对应 mi_gate_future_funding_rate_threshold 表的列名）
FUNDING_THRESHOLD_PERCENTILE = config.get_str('trade.funding_rate_threshold_percentile', 'percentile_30')
# 盘口覆盖阈值
ORDERBOOK_COVERAGE_THRESHOLD = config.get_float('trade.orderbook_coverage_threshold', 0.8)
# 风险缓释（bps）
RISK_RELIEF_BPS = config.get_float('trade.risk_relief_bps', 10)
# 开仓边际基差阈值（bps）
OPEN_VWAP_BASIS_THRESHOLD_BPS = config.get_float('trade.open_vwap_basis_threshold_bps', -60)
# 24小时成交量过滤阈值（USDT）
MIN_SPOT_VOLUME_24H_USDT = config.get_float('trade.min_spot_volume_24h_usdt', 0)
MIN_FUTURE_VOLUME_24H_USDT = config.get_float('trade.min_future_volume_24h_usdt', 0)
# 费率配置（bps 计算用）
SPOT_OPEN_FEE = config.get_float('trade.fee.spot_open', 0.00075)
SPOT_CLOSE_FEE = config.get_float('trade.fee.spot_close', 0.00075)
FUTURE_OPEN_FEE = config.get_float('trade.fee.future_open', 0.00075)
FUTURE_CLOSE_FEE = config.get_float('trade.fee.future_close', 0.00075)

# 富化配置实例（快照推送、开仓检查共用）
_enrich_cfg = EnrichConfig(
    open_amount_usdt=OPEN_AMOUNT_USDT,
    funding_threshold_percentile=FUNDING_THRESHOLD_PERCENTILE,
    risk_relief_bps=RISK_RELIEF_BPS,
    spot_open_fee=SPOT_OPEN_FEE,
    spot_close_fee=SPOT_CLOSE_FEE,
    future_open_fee=FUTURE_OPEN_FEE,
    future_close_fee=FUTURE_CLOSE_FEE,
)

# 盈亏计算配置实例（持仓实时推送用）
_pnl_cfg = PnlConfig(
    open_amount_usdt=OPEN_AMOUNT_USDT,
    spot_open_fee=SPOT_OPEN_FEE,
    spot_close_fee=SPOT_CLOSE_FEE,
    future_open_fee=FUTURE_OPEN_FEE,
    future_close_fee=FUTURE_CLOSE_FEE,
    risk_relief_bps=RISK_RELIEF_BPS,
)

# 服务生命周期管理器（在 lifespan 中初始化）
svc: Optional[ServiceLifecycleManager] = None

# 运行时全局状态（广播相关）
event_loop: Optional[asyncio.AbstractEventLoop] = None
broadcast_queue: Optional[asyncio.Queue] = None
ws_clients: Set[WebSocket] = set()
last_broadcast_time = 0.0
pending_broadcast = False

# 元数据缓存（启动时加载）
_contract_meta: Dict[str, Dict] = {}
_spot_meta: Dict[str, Dict] = {}
_threshold_meta: Dict[str, float] = {}
_vwap_threshold_meta: Dict[str, float] = {}  # base_asset -> threshold_bps
_close_vwap_threshold_meta: Dict[str, Dict] = {}  # base_asset -> {close_basis_p10..p40}

# 开仓/平仓检查执行器单例（避免每次循环重复创建 ExecutorClient）
_trading_executor: Optional['TradingExecutor'] = None
_closing_executor: Optional['ClosingExecutor'] = None

# 合并+对冲指标缓存（避免多个后台循环重复计算）
_cached_merged_rows: List[Dict] = []
_cached_merged_ts: float = 0.0

# 完整广播 payload 缓存（预序列化 JSON 字符串，避免多客户端重复序列化）
_cached_payload_json: str = ''
_cached_payload_ts: float = 0.0


def _get_merged_rows() -> List[Dict]:
    """获取合并+对冲指标数据，带短期缓存避免重复计算

    缓存有效期 = BROADCAST_THROTTLE_SEC（默认 1 秒）。
    同一秒内多个消费者（广播/开仓/平仓/持仓/VWAP快照）共享同一份计算结果。
    """
    global _cached_merged_rows, _cached_merged_ts
    now = time.time()
    if _cached_merged_rows and (now - _cached_merged_ts) < BROADCAST_THROTTLE_SEC:
        return _cached_merged_rows

    future_rows = svc.gate_manager.to_records() if svc and svc.gate_manager else []
    spot_rows = svc.spot_manager.to_records() if svc and svc.spot_manager else []
    rows = merge_orderbook_records(future_rows, spot_rows)
    rows = calculate_hedge_metrics(rows, _contract_meta, _spot_meta, OPEN_AMOUNT_USDT)

    _cached_merged_rows = rows
    _cached_merged_ts = now
    return rows



def fetch_threshold_meta() -> Dict[str, float]:
    """从 mi_gate_future_funding_rate_threshold 表加载阈值百分位字段，按 contract 索引"""
    sql = f"SELECT contract, {FUNDING_THRESHOLD_PERCENTILE} FROM mi_gate_future_funding_rate_threshold"
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                contract = row['contract']
                result[contract] = float(row[FUNDING_THRESHOLD_PERCENTILE]) if row[FUNDING_THRESHOLD_PERCENTILE] is not None else None
            logger.info(f'已加载阈值元数据 {len(result)} 条')
            return result
    except Exception as e:
        logger.error(f'加载阈值元数据失败: {e}')
        return {}


def fetch_vwap_threshold_meta() -> Dict[str, float]:
    """从 mi_vwap_basis_threshold 加载最新一天的按标的VWAP基差阈值（开仓）

    根据配置项 trade.vwap_open_threshold_percentile 动态选择对应的 pX 列。
    """
    col = config.get_str('trade.vwap_open_threshold_percentile', 'open_basis_p20')
    # 防注入校验
    valid_cols = ('open_basis_p10', 'open_basis_p20', 'open_basis_p30', 'open_basis_p40')
    if col not in valid_cols:
        logger.warning(f'vwap_open_threshold_percentile 配置值无效: {col}，回退为 open_basis_p20')
        col = 'open_basis_p20'

    sql = f"""
        SELECT base_asset, {col} AS threshold_bps
        FROM mi_vwap_basis_threshold
        WHERE calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
          AND {col} IS NOT NULL
    """
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                result[row['base_asset']] = float(row['threshold_bps'])
            logger.info(f'已加载VWAP开仓基差阈值 {len(result)} 条 (col={col})')
            return result
    except Exception as e:
        logger.error(f'加载VWAP开仓基差阈值失败: {e}')
        return {}


def fetch_close_vwap_threshold_meta() -> Dict[str, Dict]:
    """从 mi_vwap_basis_threshold 加载最新一天的全部4个平仓分位基差阈值

    返回格式: base_asset -> {close_basis_p10, close_basis_p20, close_basis_p30, close_basis_p40}
    """
    sql = """
        SELECT base_asset, close_basis_p10, close_basis_p20, close_basis_p30, close_basis_p40
        FROM mi_vwap_basis_threshold
        WHERE calc_date = (SELECT MAX(calc_date) FROM mi_vwap_basis_threshold)
    """
    try:
        with db_manager.get_cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
            result: Dict[str, Dict] = {}
            for row in rows:
                ba = row['base_asset']
                result[ba] = {
                    'close_basis_p10': float(row['close_basis_p10']) if row.get('close_basis_p10') is not None else None,
                    'close_basis_p20': float(row['close_basis_p20']) if row.get('close_basis_p20') is not None else None,
                    'close_basis_p30': float(row['close_basis_p30']) if row.get('close_basis_p30') is not None else None,
                    'close_basis_p40': float(row['close_basis_p40']) if row.get('close_basis_p40') is not None else None,
                }
            logger.info(f'已加载平仓VWAP基差阈值 {len(result)} 条（4个分位）')
            return result
    except Exception as e:
        logger.error(f'加载平仓VWAP基差阈值失败: {e}')
        return {}


def build_payload() -> dict:
    """构建 WebSocket 推送载荷（Gate + Binance 合并宽表，附带开仓金额）"""
    rows = _get_merged_rows()

    # 重要：enrich_snapshot_fields 会就地修改 rows，但由于 _cached_merged_rows
    # 每秒重建一次，这里的就地修改不影响其他消费者
    enrich_snapshot_fields(
        rows, _contract_meta, _spot_meta, _threshold_meta,
        _vwap_threshold_meta, _enrich_cfg, _meta_update_time
    )

    return {
        'type': 'snapshot',
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'open_amount_usdt': OPEN_AMOUNT_USDT,
        'funding_threshold_percentile': FUNDING_THRESHOLD_PERCENTILE,
        'orderbook_coverage_threshold': ORDERBOOK_COVERAGE_THRESHOLD,
        'risk_relief_bps': RISK_RELIEF_BPS,
        'open_vwap_basis_threshold_bps': OPEN_VWAP_BASIS_THRESHOLD_BPS,
        'min_spot_volume_24h_usdt': MIN_SPOT_VOLUME_24H_USDT,
        'min_future_volume_24h_usdt': MIN_FUTURE_VOLUME_24H_USDT,
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms() if svc else None,
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms() if svc else None,
        'rows': rows,
    }


def build_payload_json() -> str:
    """构建并预序列化广播载荷 JSON 字符串（缓存复用，避免重复序列化）"""
    global _cached_payload_json, _cached_payload_ts
    now = time.time()
    # 与 merged_rows 缓存同步：同一秒内复用已序列化的 JSON
    if _cached_payload_json and (now - _cached_payload_ts) < BROADCAST_THROTTLE_SEC:
        return _cached_payload_json
    payload = build_payload()
    _cached_payload_json = _json_dumps(payload)
    _cached_payload_ts = now
    return _cached_payload_json


def schedule_broadcast():
    """调度盘口快照广播（启动阶段与运行阶段均推送，带节流）"""
    global pending_broadcast, last_broadcast_time

    if not svc or svc.state not in (SERVICE_RUNNING, SERVICE_STARTING):
        return

    now = time.time()
    if now - last_broadcast_time >= BROADCAST_THROTTLE_SEC:
        last_broadcast_time = now
        pending_broadcast = False
        payload = build_payload()
        if event_loop and broadcast_queue:
            asyncio.run_coroutine_threadsafe(broadcast_queue.put(payload), event_loop)
    else:
        if not pending_broadcast:
            pending_broadcast = True
            delay = BROADCAST_THROTTLE_SEC - (now - last_broadcast_time)
            threading_timer = threading.Timer(delay, _delayed_broadcast)
            threading_timer.daemon = True
            threading_timer.start()


def _delayed_broadcast():
    global last_broadcast_time, pending_broadcast
    if not svc or svc.state not in (SERVICE_RUNNING, SERVICE_STARTING):
        return
    pending_broadcast = False
    last_broadcast_time = time.time()
    payload = build_payload()
    if event_loop and broadcast_queue:
        asyncio.run_coroutine_threadsafe(broadcast_queue.put(payload), event_loop)


async def broadcast_worker():
    while True:
        payload = await broadcast_queue.get()
        # 预序列化一次，所有客户端共享同一份 JSON 字符串
        if isinstance(payload, str):
            text = payload  # 已经是预序列化的 JSON
        else:
            text = _json_dumps(payload)
        dead_clients = []
        for ws in list(ws_clients):
            try:
                await ws.send_text(text)
            except Exception:
                dead_clients.append(ws)
        for ws in dead_clients:
            ws_clients.discard(ws)


async def _orderbook_broadcast_loop():
    """定时轮询广播盘口快照，替代每条 WS 消息触发的 callback 模式

    优势：
    - 彻底解耦「数据更新」与「广播推送」，消除 callback 风暴
    - 固定频率重建 payload，无论 WS 消息量多少，广播开销恒定
    - 无前端连接时自动跳过计算，降低空负CPU
    - 预序列化 JSON，多客户端共享同一份字符串
    """
    while True:
        await asyncio.sleep(BROADCAST_THROTTLE_SEC)
        try:
            if not svc or svc.state not in (SERVICE_RUNNING, SERVICE_STARTING):
                continue
            # 无前端连接时跳过计算
            if not ws_clients:
                continue
            # 直接放入预序列化的 JSON 字符串，broadcast_worker 无需再次序列化
            payload_json = build_payload_json()
            await broadcast_queue.put(payload_json)
        except Exception as e:
            logger.error(f"盘口广播失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global svc, event_loop, broadcast_queue

    event_loop = asyncio.get_running_loop()
    broadcast_queue = asyncio.Queue()
    worker_task = asyncio.create_task(broadcast_worker())

    global _contract_meta, _spot_meta, _threshold_meta, _vwap_threshold_meta, _close_vwap_threshold_meta, _meta_update_time
    _contract_meta = fetch_contract_meta()
    _spot_meta = fetch_spot_meta()
    _threshold_meta = fetch_threshold_meta()
    _vwap_threshold_meta = fetch_vwap_threshold_meta()
    _close_vwap_threshold_meta = fetch_close_vwap_threshold_meta()
    _meta_update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_print(f'已加载合约元数据 {len(_contract_meta)} 条，现货元数据 {len(_spot_meta)} 条，阈値元数据 {len(_threshold_meta)} 条，VWAP阈値 {len(_vwap_threshold_meta)} 条，平仓阈値 {len(_close_vwap_threshold_meta)} 条')

    async def _refresh_meta_cache_loop():
        """定时刷新内存缓存（ETL由各任务的IntervalScheduler独立执行）"""
        # 获取所有 interval 任务的最小间隔作为缓存刷新频率
        interval_tasks = [t for t in ETL_TASKS if t.schedule == 'interval' and t.enabled]
        if interval_tasks:
            min_interval = min(t.interval_minutes for t in interval_tasks)
        else:
            min_interval = _etl_config.get('default_interval_minutes', 15)
        
        interval = min_interval * 60  # 转换为秒
        
        while True:
            await asyncio.sleep(interval)
            try:
                logger.info(f'开始定时刷新内存缓存 (间隔: {min_interval} 分钟)...')
                # 刷新内存缓存
                global _contract_meta, _spot_meta, _threshold_meta, _vwap_threshold_meta, _close_vwap_threshold_meta, _meta_update_time
                _contract_meta = fetch_contract_meta()
                _spot_meta = fetch_spot_meta()
                _threshold_meta = fetch_threshold_meta()
                _vwap_threshold_meta = fetch_vwap_threshold_meta()
                _close_vwap_threshold_meta = fetch_close_vwap_threshold_meta()
                _meta_update_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                logger.info(f'内存缓存刷新完成: 合约 {len(_contract_meta)} 条, 现货 {len(_spot_meta)} 条, 阈値 {len(_threshold_meta)} 条, VWAP阈値 {len(_vwap_threshold_meta)} 条, 平仓阈値 {len(_close_vwap_threshold_meta)} 条')
                # 重置执行器单例，下次循环用新元数据重建
                global _trading_executor, _closing_executor
                _trading_executor = None
                _closing_executor = None
            except Exception as e:
                logger.error(f'内存缓存刷新失败: {e}')

    asyncio.create_task(_refresh_meta_cache_loop())
    asyncio.create_task(_open_position_loop())
    asyncio.create_task(_close_position_loop())
    asyncio.create_task(_position_funding_loop())
    asyncio.create_task(_position_realtime_push())
    asyncio.create_task(_vwap_snapshot_loop())

    # 启动所有 daily 类型任务的定时调度器（如 VWAP 基差分位阈值每日 00:00 计算）
    start_daily_schedulers()

    svc = ServiceLifecycleManager(
        settle=SETTLE, batch_size=SNAPSHOT_BATCH_SIZE, batch_workers=SNAPSHOT_BATCH_WORKERS
    )
    svc.init_managers()
    # 不再注册 per-message 回调，改为定时轮询广播（见 _orderbook_broadcast_loop）
    # svc.register_broadcast(schedule_broadcast)
    svc.set_runtime(event_loop, broadcast_queue, build_payload, schedule_broadcast)
    asyncio.create_task(_orderbook_broadcast_loop())

    yield

    svc.shutdown()
    stop_daily_schedulers()
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title='Cross-Exchange OrderBook Monitor', lifespan=lifespan)
app.include_router(trading_router)
app.include_router(auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173', '*'],  # 生产环境建议配置具体域名
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/api/health')
async def health():
    """健康检查接口（无需认证，用于系统监控）"""
    status = svc.get_status() if svc else {}
    return {
        'status': 'ok',
        'gate_ws_connected': status.get('gate_ws_connected', False),
        'binance_ws_connected': status.get('binance_ws_connected', False),
        'contracts': status.get('contracts', []),
        'spot_symbols': status.get('spot_symbols', []),
        'client_count': len(ws_clients),
        'service_state': status.get('state', SERVICE_IDLE),
    }


@app.get('/api/service/status')
async def service_status():
    """获取服务运行状态（无需认证，用于前端健康检查）"""
    return svc.get_status() if svc else {'state': SERVICE_IDLE}


@app.get('/api/service/connections')
async def service_connections():
    """获取逐标的资产 REST 快照 / WS 订阅状态（无需认证，用于连接监控）"""
    if not svc:
        return {'items': [], 'state': SERVICE_IDLE}
    return {
        'items': svc.get_connection_status(),
        'state': svc.state,
        'gate_ws_connected': svc._gate_ws_connected(),
        'binance_ws_connected': svc._binance_ws_connected(),
        'gate_ws_latency_ms': svc._calc_gate_data_age_ms(),
        'binance_ws_latency_ms': svc._calc_binance_data_age_ms(),
    }


@app.post('/api/service/retry-snapshot', dependencies=[Depends(verify_token_dependency)])
async def retry_snapshot(body: dict):
    """手动重试单个标的的 REST 快照初始化 + WS 订阅"""
    base_asset = (body.get('base_asset') or '').strip()
    if not base_asset:
        raise HTTPException(status_code=400, detail='base_asset 不能为空')
    if not svc:
        raise HTTPException(status_code=400, detail='服务未初始化')
    ok, message = svc.retry_contract(base_asset)
    if not ok:
        raise HTTPException(status_code=400, detail=message)
    return {'ok': True, 'message': message}


@app.get('/api/orderbook/snapshot', dependencies=[Depends(verify_token_dependency)])
async def orderbook_snapshot():
    return build_payload()


@app.post('/api/service/start', dependencies=[Depends(verify_token_dependency)])
async def service_start():
    ok, message = svc.start()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@app.post('/api/service/stop', dependencies=[Depends(verify_token_dependency)])
async def service_stop():
    ok, message = svc.stop()
    if not ok:
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}


@app.websocket('/ws/orderbook')
async def ws_orderbook(websocket: WebSocket, token: str = Query(None)):
    # 验证 token
    try:
        verify_ws_token(token)
    except HTTPException as e:
        await websocket.close(code=4001, reason=e.detail)
        return
    
    await websocket.accept()
    ws_clients.add(websocket)

    try:
        # 初始连接时发送当前快照（复用缓存的 JSON）
        await websocket.send_text(build_payload_json())
        if svc and svc.state in (SERVICE_STARTING, SERVICE_STOPPING):
            await websocket.send_text(_json_dumps(svc.get_progress_payload()))

        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                # 处理前端 ping 请求，回复 pong（用于延迟测量）
                try:
                    msg = json.loads(raw)
                    if isinstance(msg, dict) and msg.get('type') == 'ping':
                        await websocket.send_text(json.dumps({'type': 'pong', 'ts': msg.get('ts')}))
                except (json.JSONDecodeError, TypeError):
                    pass
            except asyncio.TimeoutError:
                await websocket.send_json({'type': 'ping'})
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)


async def _open_position_loop():
    """定时检查开仓条件"""
    interval = config.get_int('trade.open_check_interval_sec', 5)

    while True:
        try:
            await asyncio.sleep(interval)

            if not svc or svc.state != SERVICE_RUNNING:
                continue

            # WS 已断连时跳过，避免用陈旧缓存数据触发开仓
            if not svc._gate_ws_connected() or not svc._binance_ws_connected():
                continue

            if svc.gate_manager and svc.spot_manager:
                merged_rows = _get_merged_rows()

                if not merged_rows:
                    continue

                # 补充资金费率和阈值数据（使用公共富化模块）
                enrich_trading_fields(merged_rows, _contract_meta, _threshold_meta, _enrich_cfg)

                global _trading_executor
                if _trading_executor is None:
                    _trading_executor = TradingExecutor(
                        _contract_meta, _spot_meta, _threshold_meta,
                        _vwap_threshold_meta, _close_vwap_threshold_meta
                    )
                results = _trading_executor.check_and_open(merged_rows)

                # 推送开仓结果(如有成功)
                if any(r.get('success') for r in results):
                    from calc.position_tracker import PositionTracker
                    # 通过WebSocket推送开仓通知
                    if event_loop and broadcast_queue:
                        payload = {
                            'type': 'open_position_result',
                            'results': results,
                            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                        }
                        asyncio.run_coroutine_threadsafe(
                            broadcast_queue.put(payload), event_loop
                        )

        except Exception as e:
            logger.error(f"开仓检查失败: {e}")


async def _position_funding_loop():
    """定时更新资金费收益（启动后立即执行一次，之后每小时检查）
    结算完成后通过 WS 推送 funding_history_update 事件，前端按需更新。
    """
    interval = config.get_int('trade.position_funding_update_sec', 3600)

    # 启动后等待服务就绪再执行第一次
    await asyncio.sleep(10)

    while True:
        try:
            tracker = PositionTracker(_contract_meta)
            tracker.update_funding_pnl()
            # 结算后推送一次性的资金费历史更新事件
            histories = tracker.get_all_funding_histories()
            if histories and broadcast_queue:
                payload = {
                    'type': 'funding_history_update',
                    'funding_histories': histories,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                await broadcast_queue.put(payload)
        except Exception as e:
            logger.error(f"资金费更新失败: {e}")
        await asyncio.sleep(interval)


async def _close_position_loop():
    """定时检查平仓条件，触发平仓执行"""
    interval = config.get_int('trade.close_check_interval_sec', 5)

    while True:
        try:
            await asyncio.sleep(interval)

            if not svc or svc.state != SERVICE_RUNNING:
                continue

            # WS 已断连时跳过，避免用陈旧缓存数据触发平仓
            if not svc._gate_ws_connected() or not svc._binance_ws_connected():
                continue

            if not svc.gate_manager or not svc.spot_manager:
                continue

            merged_rows = _get_merged_rows()

            if not merged_rows:
                continue

            # 构建平仓 VWAP 索引 & 盘口行索引
            close_vwaps: Dict[str, Dict] = {}
            orderbook_rows_by_asset: Dict[str, Dict] = {}
            for row in merged_rows:
                ba = row.get('base_asset', '')
                if not ba:
                    continue
                orderbook_rows_by_asset[ba] = row
                spot_cv = row.get('spot_close_vwap')
                future_cv = row.get('future_close_vwap')
                if spot_cv is not None and future_cv is not None:
                    close_vwaps[ba] = {
                        'spot_close_vwap': float(spot_cv),
                        'future_close_vwap': float(future_cv),
                    }

            # 获取持仓中的持仓
            tracker = PositionTracker(_contract_meta)
            positions = tracker.get_holding_positions()

            if not positions:
                continue

            # 富化实时指标（current_spread_bps, funding_rate_24h 等）
            calculate_realtime_pnl(positions, close_vwaps, _contract_meta, _pnl_cfg)

            # 检查并执行平仓
            global _closing_executor
            if _closing_executor is None:
                from calc.closing_executor import ClosingExecutor
                _closing_executor = ClosingExecutor(_contract_meta, _spot_meta)
            results = _closing_executor.check_and_close(
                positions, _close_vwap_threshold_meta, orderbook_rows_by_asset
            )

            # 如有平仓成功，推送通知
            if any(r.get('success') for r in results):
                if event_loop and broadcast_queue:
                    payload = {
                        'type': 'close_position_result',
                        'results': results,
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    await broadcast_queue.put(payload)

        except Exception as e:
            logger.error(f"平仓检查失败: {e}")


async def _position_realtime_push():
    """定时推送持仓实时数据（含已平仓，使用平仓 VWAP 作为实时价格）
    注意：funding_history 不在此推送（低频数据），仅通过 REST 初始加载 + 结算后事件推送。
    """
    interval = config.get_float('trade.position_push_interval_sec', 5.0)

    while True:
        try:
            await asyncio.sleep(interval)

            if not svc or svc.state != SERVICE_RUNNING:
                continue

            tracker = PositionTracker(_contract_meta)
            positions = tracker.get_all_positions()

            if not positions:
                continue

            # 从缓存获取平仓 VWAP
            close_vwaps: Dict[str, Dict] = {}
            if svc.gate_manager and svc.spot_manager:
                merged_rows = _get_merged_rows()

                for row in merged_rows:
                    ba = row.get('base_asset', '')
                    spot_cv = row.get('spot_close_vwap')
                    future_cv = row.get('future_close_vwap')
                    if ba and spot_cv is not None and future_cv is not None:
                        close_vwaps[ba] = {
                            'spot_close_vwap': float(spot_cv),
                            'future_close_vwap': float(future_cv),
                        }

            # 计算实时盈亡（已平仓持仓用DB存储的价格，不依赖 close_vwaps）
            calculate_realtime_pnl(positions, close_vwaps, _contract_meta, _pnl_cfg)

            # 推送（不含 funding_history，保持消息精简）
            payload = {
                'type': 'position_update',
                'positions': positions,
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            await broadcast_queue.put(payload)

        except Exception as e:
            logger.error(f"持仓实时推送失败: {e}")


async def _vwap_snapshot_loop():
    """定时采样VWAP基差数据落库，用于历史分位统计"""
    interval = config.get_int('trade.vwap_snapshot_interval_sec', 10)

    while True:
        try:
            await asyncio.sleep(interval)

            if not svc or svc.state != SERVICE_RUNNING:
                continue

            if not svc.gate_manager or not svc.spot_manager:
                continue

            merged_rows = _get_merged_rows()

            if not merged_rows:
                continue

            record_vwap_snapshots(merged_rows, OPEN_AMOUNT_USDT)

        except Exception as e:
            logger.error(f"VWAP快照落库失败: {e}")


def main():
    import uvicorn

    host = '0.0.0.0'
    port = 19876

    log_print(f'启动订单簿服务 http://{host}:{port}')
    log_print('WS 服务需通过前端或 POST /api/service/start 手动启动')
    uvicorn.run(app, host=host, port=port)


if __name__ == '__main__':
    main()
