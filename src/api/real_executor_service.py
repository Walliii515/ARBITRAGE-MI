# coding: utf-8
"""
成交引擎 HTTP 服务（真实成交）

独立进程运行的 FastAPI 应用，封装 RealExecutor 为 HTTP 接口。
TradingExecutor 通过 ExecutorClient 调用本服务完成真实下单。

启动方式：
    cd src && python3 api/real_executor_service.py

接口与 virtual_executor_service.py（虚拟成交）完全一致：
    POST /api/execute  — 执行成交
    POST /api/reload   — 重新加载元数据
    GET  /api/health   — 健康检查
    GET  /api/connectivity — 测试交易所连通性（新增）

切换方式：修改 config.yaml 中 trade.executor.url 为 http://localhost:8082
"""
import argparse
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 加载 .env（从 src/ 目录向上查找项目根目录的 .env）
load_dotenv()

from calc.real_executor import RealExecutor, ExchangeConfig
from common.meta_loader import fetch_contract_meta, fetch_spot_meta
from common.config import config
from common.logger import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)

# ───── 全局状态 ─────
_executor: RealExecutor = None
_contract_meta: Dict[str, Dict] = {}
_spot_meta: Dict[str, Dict] = {}
_meta_load_time: str = ''
_exchange_config: ExchangeConfig = None


def _build_exchange_config() -> ExchangeConfig:
    """
    根据配置构建交易所配置

    从 config.yaml 的 real_executor.env 读取环境（mainnet/testnet）
    - env=testnet → 使用 Testnet URL + Testnet Key
    - env=mainnet → 使用 Mainnet URL + Mainnet Key
    """
    env = config.get_str('real_executor.env', default='testnet').lower()

    if env == 'mainnet':
        return ExchangeConfig(
            binance_base_url='https://api1.binance.com',
            binance_api_key=os.getenv('BINANCE_API_KEY', ''),
            binance_api_secret=os.getenv('BINANCE_API_SECRET', ''),
            gate_base_url='https://api.gateio.ws',
            gate_api_key=os.getenv('GATE_FUTURES_API_KEY', ''),
            gate_api_secret=os.getenv('GATE_FUTURES_API_SECRET', ''),
            timeout_sec=int(os.getenv('EXECUTOR_TIMEOUT_SEC', '10')),
            env='mainnet',
        )
    else:
        return ExchangeConfig(
            binance_base_url='https://testnet.binance.vision',
            binance_api_key=os.getenv('BINANCE_TESTNET_API_KEY', ''),
            binance_api_secret=os.getenv('BINANCE_TESTNET_API_SECRET', ''),
            gate_base_url='https://fx-api-testnet.gateio.ws',
            gate_api_key=os.getenv('GATE_FUTURES_TESTNET_API_KEY', ''),
            gate_api_secret=os.getenv('GATE_FUTURES_TESTNET_API_SECRET', ''),
            timeout_sec=int(os.getenv('EXECUTOR_TIMEOUT_SEC', '10')),
            env='testnet',
        )


def _load_meta_and_init():
    """加载元数据并初始化/刷新 RealExecutor"""
    global _executor, _contract_meta, _spot_meta, _meta_load_time, _exchange_config

    _contract_meta = fetch_contract_meta()
    _spot_meta = fetch_spot_meta()
    _meta_load_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if _executor is None:
        _exchange_config = _build_exchange_config()
        leverage = config.get_int('margin.leverage', 2)
        _executor = RealExecutor(_exchange_config, _contract_meta, spot_meta=_spot_meta, leverage=leverage)
    else:
        _executor.reload_meta(_contract_meta, _spot_meta)

    logger.info(
        f'真实成交引擎已加载: env={_exchange_config.env}, '
        f'合约 {len(_contract_meta)} 条, 现货 {len(_spot_meta)} 条 ({_meta_load_time})'
    )


# ───── Lifespan ─────
@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_meta_and_init()
    yield
    logger.info('真实成交引擎服务已关闭')


# ───── FastAPI App ─────
app = FastAPI(title='成交引擎服务（真实成交）', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)


# ───── Request 模型 ─────
class ExecuteRequest(BaseModel):
    """成交请求体（与 executor_service 完全一致）"""
    order_group: dict
    orderbook_row: dict


# ───── API 端点 ─────
@app.post('/api/execute')
async def execute(req: ExecuteRequest):
    """
    执行真实成交

    接收订单组和盘口数据，向交易所发送真实市价单并返回成交结果。
    """
    if _executor is None:
        raise HTTPException(status_code=503, detail='成交引擎未初始化')

    try:
        result = _executor.execute(req.order_group, req.orderbook_row)
        return result
    except Exception as e:
        logger.error(f'真实成交异常: {e}', exc_info=True)
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
        'engine': 'real',
        'env': _exchange_config.env if _exchange_config else 'unknown',
        'meta_loaded': _executor is not None,
        'contract_count': len(_contract_meta),
        'spot_count': len(_spot_meta),
        'loaded_at': _meta_load_time,
        'binance_url': _exchange_config.binance_base_url if _exchange_config else '',
        'gate_url': _exchange_config.gate_base_url if _exchange_config else '',
    }


@app.get('/api/connectivity')
async def connectivity():
    """
    测试交易所 API 连通性（不下单）

    验证：
    1. API Key 是否有效
    2. 签名是否正确
    3. 网络是否可达
    """
    if _executor is None:
        raise HTTPException(status_code=503, detail='成交引擎未初始化')

    try:
        result = _executor.test_connectivity()
        all_ok = result['binance']['ok'] and result['gate']['ok']
        return {
            'all_ok': all_ok,
            'env': _exchange_config.env,
            'binance': result['binance'],
            'gate': result['gate'],
        }
    except Exception as e:
        logger.error(f'连通性测试异常: {e}', exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ───── CLI 入口 ─────
if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser(description='成交引擎服务（真实成交）')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址 (默认 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8082, help='监听端口 (默认 8082)')
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port)
