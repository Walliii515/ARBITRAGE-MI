# coding: utf-8
"""
成交引擎 HTTP 服务（虚拟成交）

独立进程运行的 FastAPI 应用，封装 VirtualExecutor 为 HTTP 接口。
TradingExecutor 通过 ExecutorClient 调用本服务完成成交计算。

启动方式：
    cd src && python -m uvicorn api.executor_service:app --port 8081

切换实盘时，只需启动实盘执行器服务并修改 config.yaml 中的 trade.executor.url，
本服务不会对实盘产生任何影响。
"""
import argparse
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from calc.virtual_executor import VirtualExecutor
from common.meta_loader import fetch_contract_meta, fetch_spot_meta
from common.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

# ───── 全局状态 ─────
_executor: VirtualExecutor = None
_contract_meta: Dict[str, Dict] = {}
_spot_meta: Dict[str, Dict] = {}
_meta_load_time: str = ''


def _load_meta_and_init():
    """加载元数据并初始化/刷新 VirtualExecutor"""
    global _executor, _contract_meta, _spot_meta, _meta_load_time
    _contract_meta = fetch_contract_meta()
    _spot_meta = fetch_spot_meta()
    _meta_load_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if _executor is None:
        _executor = VirtualExecutor(_contract_meta, _spot_meta)
    else:
        _executor.reload_meta(_contract_meta, _spot_meta)

    logger.info(
        f'成交引擎元数据已加载: 合约 {len(_contract_meta)} 条, '
        f'现货 {len(_spot_meta)} 条 ({_meta_load_time})'
    )


# ───── Lifespan ─────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_meta_and_init()
    yield
    logger.info('成交引擎服务已关闭')


# ───── FastAPI App ─────
app = FastAPI(title='成交引擎服务（虚拟成交）', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)


# ───── Request / Response 模型 ─────
class ExecuteRequest(BaseModel):
    """成交请求体"""
    order_group: dict
    orderbook_row: dict


def _json_default(obj):
    """JSON 序列化兜底"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    raise TypeError(f'Object of type {type(obj).__name__} is not JSON serializable')


# ───── API 端点 ─────
@app.post('/api/execute')
async def execute(req: ExecuteRequest):
    """
    执行成交计算

    接收订单组和盘口数据，返回成交结果（含现货/期货的 VWAP 成交价）。
    """
    if _executor is None:
        raise HTTPException(status_code=503, detail='成交引擎未初始化')

    try:
        result = _executor.execute(req.order_group, req.orderbook_row)
        return result
    except Exception as e:
        logger.error(f'成交计算异常: {e}', exc_info=True)
        return {
            'success': False,
            'message': f'成交引擎异常: {str(e)}',
            'spot_order': None,
            'future_order': None
        }


@app.post('/api/reload')
async def reload_meta():
    """重新加载元数据（合约信息、现货信息）"""
    try:
        _load_meta_and_init()
        return {
            'ok': True,
            'message': f'元数据已刷新: 合约 {len(_contract_meta)} 条, 现货 {len(_spot_meta)} 条',
            'loaded_at': _meta_load_time
        }
    except Exception as e:
        logger.error(f'元数据刷新失败: {e}', exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get('/api/health')
async def health():
    """健康检查"""
    return {
        'status': 'ok',
        'engine': 'virtual',
        'meta_loaded': _executor is not None,
        'contract_count': len(_contract_meta),
        'spot_count': len(_spot_meta),
        'loaded_at': _meta_load_time
    }


# ───── CLI 入口 ─────
if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description='成交引擎服务（虚拟成交）')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址 (默认 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8081, help='监听端口 (默认 8081)')
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
