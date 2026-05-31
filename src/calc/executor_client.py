"""
成交引擎 HTTP 客户端

TradingExecutor 通过本客户端调用独立运行的成交引擎服务（虚拟/实盘）。
接口签名与 VirtualExecutor.execute() 完全一致，TradingExecutor 无需关心底层实现。
"""
import requests
from typing import Dict

from common.logger import get_logger

logger = get_logger(__name__)


class ExecutorClient:
    """
    成交引擎 HTTP 客户端

    通过 HTTP POST 调用成交引擎服务的 /api/execute 接口。
    切换实盘时只需修改 config.yaml 中的 trade.executor.url 指向新服务地址。
    """

    def __init__(self, base_url: str, timeout: int = 5):
        """
        Args:
            base_url: 成交引擎服务地址，如 http://localhost:8081
            timeout: HTTP 请求超时（秒）
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        logger.info(f'ExecutorClient 已初始化: url={self.base_url}, timeout={self.timeout}s')

    def execute(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        调用成交引擎服务执行成交计算

        Args:
            order_group: 订单组，包含 spot_order 和 future_order
            orderbook_row: 当前盘口数据行（合并后的订单簿，含5档深度）

        Returns:
            {
                'success': bool,
                'message': str,
                'spot_order': {exec_price, exec_qty, exec_amount, coverage_ratio} | None,
                'future_order': {exec_price, exec_qty, exec_amount, coverage_ratio} | None
            }
        """
        url = f'{self.base_url}/api/execute'

        # 构造请求体（移除不可 JSON 序列化的字段）
        payload = {
            'order_group': self._sanitize_order_group(order_group),
            'orderbook_row': self._sanitize_dict(orderbook_row)
        }

        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError as e:
            error_msg = f'成交引擎服务连接失败({self.base_url}): {e}'
            logger.error(error_msg)
            return self._error_result(error_msg)
        except requests.exceptions.Timeout as e:
            error_msg = f'成交引擎服务请求超时({self.timeout}s): {e}'
            logger.error(error_msg)
            return self._error_result(error_msg)
        except requests.exceptions.HTTPError as e:
            error_msg = f'成交引擎服务返回错误: {resp.status_code} {resp.text}'
            logger.error(error_msg)
            return self._error_result(error_msg)
        except Exception as e:
            error_msg = f'成交引擎调用异常: {e}'
            logger.error(error_msg, exc_info=True)
            return self._error_result(error_msg)

    def check_connectivity(self) -> Dict:
        """
        检查成交引擎交易所连通性

        调用 GET /api/connectivity，返回格式：
        {
            'all_ok': bool,
            'env': 'testnet'/'mainnet',
            'binance': {'ok': bool, ...},
            'gate': {'ok': bool, ...}
        }

        如果引擎不支持该接口（如虚拟成交引擎），返回 {'all_ok': True, 'virtual': True}
        """
        url = f'{self.base_url}/api/connectivity'
        try:
            resp = requests.get(url, timeout=self.timeout)
            if resp.status_code == 404:
                # 虚拟成交引擎没有此接口，视为不需要检查
                return {'all_ok': True, 'virtual': True}
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.ConnectionError:
            return {'all_ok': False, 'error': f'成交引擎服务不可达({self.base_url})'}
        except requests.exceptions.Timeout:
            return {'all_ok': False, 'error': f'连通性检查超时({self.timeout}s)'}
        except Exception as e:
            return {'all_ok': False, 'error': str(e)}

    def check_health(self) -> Dict:
        """
        检查成交引擎健康状态

        返回 /api/health 响应，可通过 'engine' 字段判断是 virtual 还是 real。
        """
        url = f'{self.base_url}/api/health'
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    @staticmethod
    def _error_result(message: str) -> Dict:
        """构造错误返回（与 VirtualExecutor.execute 返回格式一致）"""
        return {
            'success': False,
            'message': message,
            'spot_order': None,
            'future_order': None
        }

    @staticmethod
    def _sanitize_order_group(order_group: Dict) -> Dict:
        """清理订单组中不可 JSON 序列化的字段"""
        cleaned = {}
        for key, value in order_group.items():
            if key in ('spot_order', 'future_order') and isinstance(value, dict):
                # 子订单：复制一份避免修改原始对象
                cleaned[key] = dict(value)
            else:
                cleaned[key] = value
        return cleaned

    @staticmethod
    def _sanitize_dict(d: Dict) -> Dict:
        """确保 dict 中的值都是 JSON 可序列化的（处理 Decimal 等）"""
        from decimal import Decimal
        from datetime import datetime

        cleaned = {}
        for key, value in d.items():
            if isinstance(value, Decimal):
                cleaned[key] = float(value)
            elif isinstance(value, datetime):
                cleaned[key] = value.strftime('%Y-%m-%d %H:%M:%S')
            else:
                cleaned[key] = value
        return cleaned
