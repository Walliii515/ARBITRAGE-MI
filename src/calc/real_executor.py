# coding: utf-8
"""
真实成交引擎模块

对接 Binance 现货 + Gate 期货的真实下单 API。
与 VirtualExecutor 暴露完全相同的 execute() 接口契约，
TradingExecutor 通过 ExecutorClient 调用时无需关心底层实现。

设计要点：
1. 顺序下单（先现货后期货），避免单边成交风险
2. 市价单 IOC 模式，确保即时成交或拒绝
3. 支持 Testnet / Mainnet 通过配置切换
"""
import time
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode

import requests

from common.logger import get_logger
from common.tools import truncate_to_precision

logger = get_logger(__name__)


@dataclass
class ExchangeConfig:
    """交易所 API 配置（通过 dataclass 注入，符合 calc 模块配置注入规范）"""
    # Binance
    binance_base_url: str = 'https://testnet.binance.vision'
    binance_api_key: str = ''
    binance_api_secret: str = ''
    # Gate
    gate_base_url: str = 'https://fx-api-testnet.gateio.ws'
    gate_api_key: str = ''
    gate_api_secret: str = ''
    # 通用
    timeout_sec: int = 10
    # 环境标识
    env: str = 'testnet'  # testnet / mainnet


class RealExecutor:
    """
    真实成交引擎

    职责：接收订单组 + 盘口数据，向交易所发送真实市价单并返回成交结果。
    接口契约与 VirtualExecutor.execute() 完全一致。
    """

    def __init__(self, exchange_config: ExchangeConfig, contract_meta: Dict = None,
                 spot_meta: Dict = None, leverage: int = 2):
        """
        Args:
            exchange_config: 交易所 API 配置
            contract_meta: base_asset -> {quanto_multiplier, ...} 用于期货张数换算
            spot_meta: base_asset -> {step_size, min_qty, ...} 用于现货精度截断
            leverage: 期货杠杆倍数（>0 为逐仓模式，0 为全仓模式）
        """
        self.config = exchange_config
        self.contract_meta = contract_meta or {}
        self.spot_meta = spot_meta or {}
        self.leverage = leverage
        self._leverage_set: set = set()  # 已设置过杠杆的合约（避免重复调用）
        self._session = requests.Session()
        logger.info(
            f'RealExecutor 已初始化: env={self.config.env}, '
            f'binance={self.config.binance_base_url}, '
            f'gate={self.config.gate_base_url}, '
            f'leverage={self.leverage}(逐仓)'
        )

    def execute(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        执行真实成交

        流程：
        1. 并发下 Binance 现货单 和 Gate 期货单（降低基差滑点）
        2. 两边都成功则整体成功
        3. 一边失败则标记单边成交风险（需人工处理）

        Args:
            order_group: 订单组，包含 spot_order 和 future_order
            orderbook_row: 当前盘口数据行（用于日志记录，实际成交由交易所决定）

        Returns:
            与 VirtualExecutor 完全一致的格式:
            {
                'success': bool,
                'message': str,
                'spot_order': {exec_price, exec_qty, exec_amount, coverage_ratio} | None,
                'future_order': {exec_price, exec_qty, exec_amount, coverage_ratio} | None
            }
        """
        result = {'success': False, 'spot_order': None, 'future_order': None, 'message': ''}

        try:
            spot_order = order_group.get('spot_order', {})
            future_order = order_group.get('future_order', {})

            # ── 并发下单：同时向 Binance 和 Gate 发送市价单 ──
            with ThreadPoolExecutor(max_workers=2) as pool:
                spot_future = pool.submit(self._place_binance_spot_order, spot_order)
                gate_future = pool.submit(self._place_gate_futures_order, future_order)

                spot_result = spot_future.result()
                future_result = gate_future.result()

            # ── 判定结果 ──
            if spot_result['success'] and future_result['success']:
                # 两边都成功
                result['spot_order'] = spot_result
                result['future_order'] = future_result
                result['success'] = True
                result['message'] = '成交成功'
                logger.info(
                    f"真实成交成功(并发) | {spot_order.get('base_asset')} | "
                    f"spot: price={spot_result['exec_price']}, qty={spot_result['exec_qty']} | "
                    f"future: price={future_result['exec_price']}, qty={future_result['exec_qty']}"
                )
            elif spot_result['success'] and not future_result['success']:
                # 现货成功但期货失败 — 严重情况
                result['spot_order'] = spot_result
                result['message'] = (
                    f"期货拒单(现货已成交,需人工处理): {future_result['reason']} | "
                    f"spot_exec: price={spot_result.get('exec_price')}, "
                    f"qty={spot_result.get('exec_qty')}"
                )
                logger.critical(
                    f"⚠️ 单边成交风险 | {spot_order.get('base_asset')} | "
                    f"现货已成交但期货失败: {future_result['reason']}"
                )
            elif not spot_result['success'] and future_result['success']:
                # 期货成功但现货失败 — 严重情况
                result['future_order'] = future_result
                result['message'] = (
                    f"现货拒单(期货已成交,需人工处理): {spot_result['reason']} | "
                    f"future_exec: price={future_result.get('exec_price')}, "
                    f"qty={future_result.get('exec_qty')}"
                )
                logger.critical(
                    f"⚠️ 单边成交风险 | {spot_order.get('base_asset')} | "
                    f"期货已成交但现货失败: {spot_result['reason']}"
                )
            else:
                # 两边都失败
                result['message'] = (
                    f"双边拒单: 现货({spot_result['reason']}), "
                    f"期货({future_result['reason']})"
                )

        except Exception as e:
            result['message'] = f"系统异常: {str(e)}"
            logger.error(f"RealExecutor 异常: {e}", exc_info=True)

        return result

    # ──────────────────────────────────────────────────────────────────
    # Binance 现货
    # ──────────────────────────────────────────────────────────────────

    def _place_binance_spot_order(self, order: Dict) -> Dict:
        """
        向 Binance 发送现货市价单

        API: POST /api/v3/order
        Auth: HMAC SHA256

        下单模式:
        - BUY（开仓）: 使用 quoteOrderQty（指定花费 USDT 金额），Binance 自动计算可买数量
        - SELL（平仓）: 使用 quantity（指定卖出币数量）
        """
        base_asset = order.get('base_asset', '')
        symbol = f"{base_asset}USDT"
        side = 'BUY' if order.get('trade_direction') == 'buy' else 'SELL'

        # 构造参数
        timestamp = int(time.time() * 1000)
        params = {
            'symbol': symbol,
            'side': side,
            'type': 'MARKET',
            'timestamp': timestamp,
            'newClientOrderId': f"arb_{order.get('order_uuid', '')[:8]}_spot",
        }

        if side == 'BUY':
            # 开仓: 使用 quoteOrderQty（花费固定 USDT 金额买入）
            # 优势: 精确控制花费、避免 NOTIONAL 和余额不足问题
            quote_amount = order.get('target_amount', 10)
            params['quoteOrderQty'] = str(quote_amount)
        else:
            # 平仓: 使用 quantity（卖出指定数量的币）
            # 必须按 step_size 精度截断，避免 LOT_SIZE 拒单
            quantity = float(order.get('target_qty', 0))
            qty_precision = self._get_spot_qty_precision(base_asset)
            quantity = truncate_to_precision(quantity, qty_precision)
            params['quantity'] = str(quantity)

        # 签名
        query_string = urlencode(params)
        signature = self._binance_sign(query_string)
        params['signature'] = signature

        # 请求
        url = f"{self.config.binance_base_url}/api/v3/order"
        headers = {'X-MBX-APIKEY': self.config.binance_api_key}

        try:
            resp = self._session.post(
                url, params=params, headers=headers,
                timeout=self.config.timeout_sec
            )

            if resp.status_code != 200:
                error_msg = resp.text[:200]
                logger.warning(f"Binance 下单失败 | {symbol} | HTTP {resp.status_code}: {error_msg}")
                return {'success': False, 'reason': f"HTTP {resp.status_code}: {error_msg}"}

            data = resp.json()
            return self._parse_binance_response(data)

        except requests.exceptions.Timeout:
            return {'success': False, 'reason': f'Binance 请求超时({self.config.timeout_sec}s)'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'reason': f'Binance 连接失败: {str(e)[:100]}'}
        except Exception as e:
            return {'success': False, 'reason': f'Binance 异常: {str(e)[:100]}'}

    def _parse_binance_response(self, data: Dict) -> Dict:
        """
        解析 Binance 订单响应

        市价单响应字段：
        - status: FILLED
        - executedQty: 实际成交数量（总量，未扣手续费）
        - cummulativeQuoteQty: 成交金额
        - fills: [{price, qty, commission, commissionAsset}]

        注意：BUY 时手续费从买到的币中扣除（如买 25 XLM，扣 0.025 XLM 手续费，
        实际到账 24.975）。必须减去手续费，否则平仓卖出时会因余额不足被拒。
        """
        status = data.get('status', '')
        if status != 'FILLED':
            return {
                'success': False,
                'reason': f"订单状态异常: {status}, orderId={data.get('orderId')}"
            }

        exec_qty = float(data.get('executedQty', 0))
        exec_amount = float(data.get('cummulativeQuoteQty', 0))

        # 扣除以 base asset 计价的手续费（BUY 时手续费从买到的币中扣除）
        symbol = data.get('symbol', '')
        # base_asset = symbol 去掉末尾的 quote（如 XLMUSDT -> XLM）
        base_asset = symbol.replace('USDT', '') if symbol.endswith('USDT') else symbol
        fills = data.get('fills', [])
        commission_in_base = 0.0
        for fill in fills:
            if fill.get('commissionAsset', '') == base_asset:
                commission_in_base += float(fill.get('commission', 0))

        # 实际可用数量 = 成交量 - 手续费
        net_qty = exec_qty - commission_in_base
        exec_price = exec_amount / exec_qty if exec_qty > 0 else 0

        if commission_in_base > 0:
            logger.info(
                f"Binance 手续费扣减 | {symbol} | "
                f"gross_qty={exec_qty}, commission={commission_in_base} {base_asset}, "
                f"net_qty={net_qty}"
            )

        return {
            'success': True,
            'exec_price': exec_price,
            'exec_qty': net_qty,  # 返回扣除手续费后的净量
            'exec_amount': exec_amount,
            'coverage_ratio': 0,  # 实盘无覆盖率概念
            'exchange_order_id': str(data.get('orderId', '')),
        }

    def _binance_sign(self, query_string: str) -> str:
        """Binance HMAC SHA256 签名"""
        return hmac.new(
            self.config.binance_api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    def fetch_binance_account_balances(self) -> List[Dict]:
        """
        拉取 Binance 现货账户非零资产余额（只读，资金/对账使用）。

        返回字段保持贴近交易所原始口径：
        [{asset, free, locked, total}]
        """
        timestamp = int(time.time() * 1000)
        params = {'timestamp': timestamp}
        query_string = urlencode(params)
        params['signature'] = self._binance_sign(query_string)

        url = f"{self.config.binance_base_url}/api/v3/account"
        headers = {'X-MBX-APIKEY': self.config.binance_api_key}
        resp = self._session.get(url, params=params, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Binance account HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        result: List[Dict] = []
        for item in data.get('balances', []):
            asset = str(item.get('asset') or '').upper()
            if not asset or asset == 'USDT':
                continue
            free = float(item.get('free') or 0)
            locked = float(item.get('locked') or 0)
            total = free + locked
            if total == 0:
                continue
            result.append({
                'asset': asset,
                'free': free,
                'locked': locked,
                'total': total,
            })
        return result

    def fetch_binance_spot_balances(self) -> List[Dict]:
        """拉取 Binance 非 USDT 现货余额（对账使用）。"""
        return [b for b in self.fetch_binance_account_balances() if b.get('asset') != 'USDT']

    def fetch_binance_ticker_prices(self, assets: Iterable[str]) -> Dict[str, float]:
        """批量拉取 Binance 现货 USDT 价格（公开接口，资金估值使用）。"""
        symbols = [f"{str(asset).upper()}USDT" for asset in assets if str(asset).upper() != 'USDT']
        if not symbols:
            return {}
        params = {'symbols': json.dumps(symbols)}
        url = f"{self.config.binance_base_url}/api/v3/ticker/price"
        resp = self._session.get(url, params=params, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            result: Dict[str, float] = {}
            for symbol in symbols:
                single = self._session.get(
                    url,
                    params={'symbol': symbol},
                    timeout=self.config.timeout_sec,
                )
                if single.status_code != 200:
                    logger.debug(f"Binance ticker 跳过 | {symbol} | HTTP {single.status_code}")
                    continue
                item = single.json()
                if symbol.endswith('USDT'):
                    result[symbol[:-4]] = float(item.get('price') or 0)
            return result
        data = resp.json()
        result: Dict[str, float] = {}
        for item in data if isinstance(data, list) else []:
            symbol = str(item.get('symbol') or '').upper()
            if symbol.endswith('USDT'):
                result[symbol[:-4]] = float(item.get('price') or 0)
        return result

    # ──────────────────────────────────────────────────────────────────
    # Gate 期货
    # ──────────────────────────────────────────────────────────────────

    def _ensure_leverage(self, contract: str):
        """
        确保合约已设置为逐仓模式 + 指定杠杆倍数

        Gate API: POST /api/v4/futures/usdt/positions/{contract}/leverage
        - leverage > 0: 逐仓模式（isolated margin），值为杠杆倍数
        - leverage = 0: 全仓模式（cross margin）

        仅在每个合约首次下单前调用一次，后续通过缓存跳过。
        """
        if contract in self._leverage_set:
            return

        api_path = f'/api/v4/futures/usdt/positions/{contract}/leverage'
        query_string = f'leverage={self.leverage}'
        headers = self._gate_sign('POST', api_path, query_string, '')
        url = f"{self.config.gate_base_url}{api_path}?{query_string}"

        try:
            resp = self._session.post(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code == 200:
                self._leverage_set.add(contract)
                logger.info(f"Gate 杠杆设置成功 | {contract} | 逐仓 {self.leverage}x")
            else:
                logger.warning(
                    f"Gate 杠杆设置失败 | {contract} | HTTP {resp.status_code}: {resp.text[:150]}"
                )
        except Exception as e:
            logger.warning(f"Gate 杠杆设置异常 | {contract} | {e}")

    def _place_gate_futures_order(self, order: Dict) -> Dict:
        """
        向 Gate 发送期货市价单

        API: POST /api/v4/futures/usdt/orders
        Auth: HMAC SHA512
        """
        base_asset = order.get('base_asset', '')
        contract = order.get('future_contract') or f"{base_asset}_USDT"
        target_qty = float(order.get('target_qty', 0))

        # 首次下单前确保逐仓 + 杠杆设置
        self._ensure_leverage(contract)

        # 计算期货张数（空头为负数）
        quanto_multiplier = self._get_quanto_multiplier(base_asset)
        contracts_size = int(target_qty / quanto_multiplier) if quanto_multiplier > 0 else 0

        if contracts_size == 0:
            return {'success': False, 'reason': f'合约张数为0(数量{target_qty}/乘数{quanto_multiplier}), 无法下单'}

        # 期货做空 → size 为负
        direction = order.get('trade_direction', 'sell')
        if direction == 'sell':
            contracts_size = -abs(contracts_size)
        else:
            contracts_size = abs(contracts_size)

        # 构造请求体
        body = {
            'contract': contract,
            'size': contracts_size,
            'price': '0',       # 市价单
            'tif': 'ioc',       # 即时成交或取消
            'text': f"t-arb{order.get('order_uuid', '')[:8]}",
        }

        # 平仓时设置 reduce_only，防止反向开新仓
        if order.get('order_side') == 'close':
            body['reduce_only'] = True
        body_str = json.dumps(body)

        # 签名
        method = 'POST'
        api_path = '/api/v4/futures/usdt/orders'
        headers = self._gate_sign(method, api_path, '', body_str)

        # 请求
        url = f"{self.config.gate_base_url}{api_path}"

        try:
            resp = self._session.post(
                url, data=body_str, headers=headers,
                timeout=self.config.timeout_sec
            )

            if resp.status_code not in (200, 201):
                error_msg = resp.text[:200]
                logger.warning(f"Gate 下单失败 | {contract} | HTTP {resp.status_code}: {error_msg}")
                return {'success': False, 'reason': f"HTTP {resp.status_code}: {error_msg}"}

            data = resp.json()
            return self._parse_gate_response(data, quanto_multiplier)

        except requests.exceptions.Timeout:
            return {'success': False, 'reason': f'Gate 请求超时({self.config.timeout_sec}s)'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'reason': f'Gate 连接失败: {str(e)[:100]}'}
        except Exception as e:
            return {'success': False, 'reason': f'Gate 异常: {str(e)[:100]}'}

    def _parse_gate_response(self, data: Dict, quanto_multiplier: float) -> Dict:
        """
        解析 Gate 期货订单响应

        响应字段：
        - status: finished
        - size: 成交张数（负数=空头）
        - fill_price: 成交均价
        """
        status = data.get('status', '')
        if status != 'finished':
            return {
                'success': False,
                'reason': f"订单状态异常: {status}, id={data.get('id')}, "
                         f"finish_as={data.get('finish_as', 'unknown')}"
            }

        fill_price_str = data.get('fill_price', '0')
        exec_price = float(fill_price_str) if fill_price_str else 0
        size = abs(int(data.get('size', 0)))

        # 将张数转回标的资产数量
        exec_qty = size * quanto_multiplier
        exec_amount = round(exec_price * exec_qty, 2)

        if exec_price == 0 or size == 0:
            return {
                'success': False,
                'reason': f"成交数据异常: price={exec_price}, size={size}, "
                         f"finish_as={data.get('finish_as', 'unknown')}"
            }

        return {
            'success': True,
            'exec_price': exec_price,
            'exec_qty': exec_qty,
            'exec_amount': exec_amount,
            'coverage_ratio': 0,
            'exchange_order_id': str(data.get('id', '')),
        }

    def _gate_sign(self, method: str, api_path: str, query_string: str, body: str) -> Dict:
        """
        Gate.io API V4 签名

        签名格式: HMAC SHA512
        签名字符串: {method}
{api_path}
{query_string}
{hashed_body}
{timestamp}
        """
        timestamp = str(int(time.time()))
        hashed_payload = hashlib.sha512(body.encode('utf-8')).hexdigest()

        sign_string = f"{method}\n{api_path}\n{query_string}\n{hashed_payload}\n{timestamp}"
        signature = hmac.new(
            self.config.gate_api_secret.encode('utf-8'),
            sign_string.encode('utf-8'),
            hashlib.sha512
        ).hexdigest()

        return {
            'KEY': self.config.gate_api_key,
            'SIGN': signature,
            'Timestamp': timestamp,
            'Content-Type': 'application/json',
        }

    def fetch_gate_futures_positions(self) -> List[Dict]:
        """
        拉取 Gate USDT 永续持仓（只读，对账使用）。

        返回字段包含张数 size，空头为负；对账层取 abs(size) 与本地张数比较。
        """
        method = 'GET'
        api_path = '/api/v4/futures/usdt/positions'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Gate positions HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        result: List[Dict] = []
        for item in data if isinstance(data, list) else []:
            contract = str(item.get('contract') or '').upper()
            if not contract.endswith('_USDT'):
                continue
            size = float(item.get('size') or 0)
            if size == 0:
                continue
            result.append({
                'contract': contract,
                'base_asset': contract[:-5],
                'size': size,
                'entry_price': item.get('entry_price'),
                'unrealised_pnl': item.get('unrealised_pnl'),
                'leverage': item.get('leverage'),
                'margin': item.get('margin'),
                'mode': item.get('mode'),
            })
        return result

    def fetch_gate_futures_account(self) -> Dict:
        """拉取 Gate USDT 永续账户资金（只读，资金快照使用）。"""
        method = 'GET'
        api_path = '/api/v4/futures/usdt/accounts'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
        if resp.status_code != 200:
            raise RuntimeError(f"Gate futures account HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data if isinstance(data, dict) else {}

    # ──────────────────────────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────────────────────────

    def _get_quanto_multiplier(self, base_asset: str) -> float:
        """获取合约面值乘数"""
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0

    def _get_spot_qty_precision(self, base_asset: str) -> int:
        """从 spot_meta 的 step_size 推导数量小数位数"""
        if base_asset in self.spot_meta:
            step_size = self.spot_meta[base_asset].get('step_size', 0.00001)
            step_str = str(step_size)
            if '.' in step_str:
                return len(step_str.split('.')[-1].rstrip('0')) or 0
        return 5  # 安全默认值

    def reload_meta(self, contract_meta: Dict, spot_meta: Dict = None):
        """热更新元数据（与 VirtualExecutor 保持接口一致）"""
        self.contract_meta = contract_meta
        if spot_meta is not None:
            self.spot_meta = spot_meta
        logger.info(f'RealExecutor 元数据已刷新: 合约 {len(contract_meta)} 条, 现货 {len(self.spot_meta)} 条')

    def test_connectivity(self) -> Dict:
        """
        测试交易所 API 连通性（不下单，仅验证签名和网络）

        Returns:
            {'binance': {'ok': bool, 'msg': str}, 'gate': {'ok': bool, 'msg': str}}
        """
        result = {'binance': {'ok': False, 'msg': ''}, 'gate': {'ok': False, 'msg': ''}}

        # Binance: GET /api/v3/account
        try:
            timestamp = int(time.time() * 1000)
            params = {'timestamp': timestamp}
            query_string = urlencode(params)
            signature = self._binance_sign(query_string)
            params['signature'] = signature

            url = f"{self.config.binance_base_url}/api/v3/account"
            headers = {'X-MBX-APIKEY': self.config.binance_api_key}
            resp = self._session.get(url, params=params, headers=headers, timeout=self.config.timeout_sec)

            if resp.status_code == 200:
                data = resp.json()
                balances = [b for b in data.get('balances', []) if float(b.get('free', 0)) > 0]
                result['binance'] = {'ok': True, 'msg': f'账户正常, {len(balances)} 个有余额的资产'}
            else:
                result['binance'] = {'ok': False, 'msg': f'HTTP {resp.status_code}: {resp.text[:100]}'}
        except Exception as e:
            result['binance'] = {'ok': False, 'msg': str(e)[:100]}

        # Gate: GET /api/v4/futures/usdt/accounts
        try:
            method = 'GET'
            api_path = '/api/v4/futures/usdt/accounts'
            headers = self._gate_sign(method, api_path, '', '')
            url = f"{self.config.gate_base_url}{api_path}"
            resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)

            if resp.status_code == 200:
                data = resp.json()
                available = data.get('available', '0')
                result['gate'] = {'ok': True, 'msg': f'账户正常, 可用余额={available}'}
            else:
                result['gate'] = {'ok': False, 'msg': f'HTTP {resp.status_code}: {resp.text[:100]}'}
        except Exception as e:
            result['gate'] = {'ok': False, 'msg': str(e)[:100]}

        return result
