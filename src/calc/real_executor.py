# coding: utf-8
"""
真实成交引擎模块

对接 Binance 现货 + Gate 期货的真实下单 API。
与 VirtualExecutor 暴露完全相同的 execute() 接口契约，
TradingExecutor 通过 ExecutorClient 调用时无需关心底层实现。

设计要点：
1. 默认并发双腿下单，降低基差滑点
2. 开仓可切换为 Gate future post-only maker → Binance spot taker hedge
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

            if self._is_future_maker_order(future_order):
                return self._execute_future_maker_then_spot(order_group, orderbook_row)

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

    @staticmethod
    def _is_future_maker_order(future_order: Dict) -> bool:
        return (
            future_order.get('market_type') == 'future'
            and future_order.get('execution_style') == 'maker'
        )

    def _execute_future_maker_then_spot(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        Gate future post-only maker 先成交，成交多少就用 Binance spot taker 对冲多少。
        未成交时不动现货，直接放弃本轮开/平仓。
        """
        result = {'success': False, 'spot_order': None, 'future_order': None, 'message': ''}
        spot_order = dict(order_group.get('spot_order') or {})
        future_order = dict(order_group.get('future_order') or {})
        base_asset = future_order.get('base_asset') or spot_order.get('base_asset')
        order_side = future_order.get('order_side', '')

        future_result = self._place_gate_futures_order(future_order)
        stats = future_result.get('execution_stats') or {}
        if not future_result.get('success'):
            result['future_order'] = future_result if future_result.get('exchange_order_id') else None
            result['message'] = f"future maker未成交/拒单: {future_result.get('reason', 'unknown')}"
            result['execution_stats'] = stats
            logger.info(f"future maker 放弃{order_side} | {base_asset} | {result['message']}")
            return result

        hedge_order = dict(spot_order)
        future_target_qty = float(future_order.get('target_qty') or 0)
        spot_target_qty = float(spot_order.get('target_qty') or 0)
        hedge_ratio = spot_target_qty / future_target_qty if future_target_qty > 0 else 1.0
        hedge_order['target_qty'] = future_result['exec_qty'] * hedge_ratio
        hedge_order['target_amount'] = future_result['exec_amount']
        hedge_order['quantity_mode'] = 'base'
        spot_result = self._place_binance_spot_order(hedge_order)

        maker_stats = stats.setdefault('future_maker', {})
        maker_stats['future_exec_price'] = future_result.get('exec_price')
        if spot_result.get('success'):
            maker_stats['spot_exec_price'] = spot_result.get('exec_price')

        if spot_result.get('success'):
            result.update({
                'success': True,
                'spot_order': spot_result,
                'future_order': future_result,
                'message': f'成交成功({order_side} future maker + spot taker)',
                'execution_stats': stats,
            })
            logger.info(
                f"真实成交成功({order_side} future maker + spot taker) | {base_asset} | "
                f"fill_ratio={maker_stats.get('fill_ratio', 0):.2f} | "
                f"wait={maker_stats.get('wait_ms', 0):.0f}ms | "
                f"spot: price={spot_result['exec_price']}, qty={spot_result['exec_qty']} | "
                f"future: price={future_result['exec_price']}, qty={future_result['exec_qty']}"
            )
        else:
            result.update({
                'future_order': future_result,
                'message': (
                    f"现货拒单(期货maker已成交,需人工处理): {spot_result.get('reason')} | "
                    f"future_exec: price={future_result.get('exec_price')}, "
                    f"qty={future_result.get('exec_qty')}"
                ),
                'execution_stats': stats,
            })
            logger.critical(
                f"⚠️ 单边成交风险 | {base_asset} | "
                f"期货maker已成交但现货失败: {spot_result.get('reason')}"
            )
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

        if side == 'BUY' and order.get('quantity_mode') == 'base':
            quantity = float(order.get('target_qty', 0))
            qty_precision = self._get_spot_qty_precision(base_asset)
            quantity = truncate_to_precision(quantity, qty_precision)
            params['quantity'] = str(quantity)
        elif side == 'BUY':
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
        拉取 Binance 现货账户非零资产余额（只读，资金使用）。

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
            if not asset:
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

    def fetch_binance_my_trades(self, symbol: str, start_time_ms: Optional[int] = None, limit: int = 1000) -> List[Dict]:
        """拉取 Binance 现货账户成交记录（只读，资金快照手续费使用）。"""
        symbol = str(symbol or '').upper()
        if not symbol:
            return []

        all_rows: List[Dict] = []
        from_id: Optional[int] = None
        while True:
            params = {
                'symbol': symbol,
                'limit': min(max(int(limit or 1000), 1), 1000),
                'timestamp': int(time.time() * 1000),
            }
            if start_time_ms is not None and from_id is None:
                params['startTime'] = int(start_time_ms)
            if from_id is not None:
                params['fromId'] = from_id

            query_string = urlencode(params)
            params['signature'] = self._binance_sign(query_string)
            url = f"{self.config.binance_base_url}/api/v3/myTrades"
            headers = {'X-MBX-APIKEY': self.config.binance_api_key}
            resp = self._session.get(url, params=params, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code != 200:
                raise RuntimeError(f"Binance myTrades {symbol} HTTP {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            rows = data if isinstance(data, list) else []
            all_rows.extend(rows)
            if len(rows) < params['limit']:
                break
            last_id = rows[-1].get('id')
            if last_id is None:
                break
            from_id = int(last_id) + 1
            if len(all_rows) >= 10000:
                logger.warning(f"Binance myTrades {symbol} reached 10000 row cap for capital snapshot")
                break
        return all_rows

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

        price = '0'
        tif = 'ioc'
        protective_price = order.get('protective_price')
        if order.get('order_side') == 'close' and protective_price is not None:
            price = self._format_gate_price(base_asset, float(protective_price))
        if self._is_future_maker_order(order):
            maker_price = float(order.get('maker_price') or 0)
            if maker_price <= 0:
                return {'success': False, 'reason': 'future maker缺少有效挂单价'}
            price = self._format_gate_price(base_asset, maker_price)
            tif = 'poc'

        # 构造请求体
        body = {
            'contract': contract,
            'size': contracts_size,
            'price': price,     # 0=市价IOC；非0=保护限价/POC
            'tif': tif,         # ioc=即时成交或取消；poc=post-only
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
            if tif == 'poc':
                return self._wait_gate_maker_fill(
                    order=order,
                    initial_order=data,
                    quanto_multiplier=quanto_multiplier,
                    requested_contracts=abs(contracts_size),
                    maker_price=float(price),
                    taker_reference_price=order.get('maker_taker_reference_price'),
                    spot_reference_price=order.get('maker_spot_reference_price'),
                )
            return self._parse_gate_response(data, quanto_multiplier)

        except requests.exceptions.Timeout:
            return {'success': False, 'reason': f'Gate 请求超时({self.config.timeout_sec}s)'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'reason': f'Gate 连接失败: {str(e)[:100]}'}
        except Exception as e:
            return {'success': False, 'reason': f'Gate 异常: {str(e)[:100]}'}

    def _wait_gate_maker_fill(
        self,
        order: Dict,
        initial_order: Dict,
        quanto_multiplier: float,
        requested_contracts: int,
        maker_price: float,
        taker_reference_price: Optional[float] = None,
        spot_reference_price: Optional[float] = None,
    ) -> Dict:
        order_id = str(initial_order.get('id') or '')
        contract = order.get('future_contract') or f"{order.get('base_asset')}_USDT"
        ttl_ms = max(int(order.get('maker_ttl_ms') or 0), 0)
        deadline = time.monotonic() + ttl_ms / 1000.0
        latest = dict(initial_order)

        while order_id and time.monotonic() < deadline:
            filled_contracts = self._gate_filled_contracts(latest)
            if filled_contracts >= requested_contracts:
                break
            sleep_sec = min(0.1, max(deadline - time.monotonic(), 0))
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            fresh = self._get_gate_futures_order(contract, order_id)
            if fresh:
                latest = fresh

        filled_contracts = self._gate_filled_contracts(latest)
        if order_id and filled_contracts < requested_contracts:
            cancelled = self._cancel_gate_futures_order(contract, order_id)
            if cancelled:
                latest = cancelled
                filled_contracts = self._gate_filled_contracts(latest)

        elapsed_ms = (ttl_ms / 1000.0 - max(deadline - time.monotonic(), 0)) * 1000.0
        result = self._parse_gate_response(latest, quanto_multiplier, allow_partial=True)
        fill_ratio = (filled_contracts / requested_contracts) if requested_contracts > 0 else 0
        improvement_bps = None
        exec_price = result.get('exec_price')
        if exec_price and taker_reference_price and spot_reference_price:
            if order.get('trade_direction') == 'buy':
                improvement_bps = (float(taker_reference_price) - float(exec_price)) / float(spot_reference_price) * 10000.0
            else:
                improvement_bps = (float(exec_price) - float(taker_reference_price)) / float(spot_reference_price) * 10000.0

        stats = {
            'future_maker': {
                'attempted': True,
                'filled': bool(result.get('success')),
                'order_side': order.get('order_side'),
                'trade_direction': order.get('trade_direction'),
                'fill_ratio': round(fill_ratio, 4),
                'wait_ms': round(elapsed_ms, 0),
                'ttl_ms': ttl_ms,
                'maker_price': maker_price,
                'future_exec_price': exec_price,
                'requested_contracts': requested_contracts,
                'filled_contracts': filled_contracts,
                'exchange_order_id': order_id,
                'improvement_bps': round(improvement_bps, 2) if improvement_bps is not None else None,
            }
        }
        result['execution_stats'] = stats
        if not result.get('success'):
            result['reason'] = (
                f"future maker未成交(fill={fill_ratio:.0%},wait={elapsed_ms:.0f}ms,"
                f"ttl={ttl_ms}ms,id={order_id})"
            )
        return result

    def _get_gate_futures_order(self, contract: str, order_id: str) -> Optional[Dict]:
        method = 'GET'
        api_path = f'/api/v4/futures/usdt/orders/{order_id}'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        try:
            resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code != 200:
                logger.debug(f"Gate 查询订单失败 | {contract} | {order_id} | HTTP {resp.status_code}: {resp.text[:120]}")
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.debug(f"Gate 查询订单异常 | {contract} | {order_id} | {e}")
            return None

    def _cancel_gate_futures_order(self, contract: str, order_id: str) -> Optional[Dict]:
        method = 'DELETE'
        api_path = f'/api/v4/futures/usdt/orders/{order_id}'
        headers = self._gate_sign(method, api_path, '', '')
        url = f"{self.config.gate_base_url}{api_path}"
        try:
            resp = self._session.delete(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code not in (200, 201):
                logger.warning(f"Gate 撤单失败 | {contract} | {order_id} | HTTP {resp.status_code}: {resp.text[:150]}")
                return None
            data = resp.json() if resp.text else {}
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"Gate 撤单异常 | {contract} | {order_id} | {e}")
            return None

    @staticmethod
    def _gate_int(value) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    def _gate_filled_contracts(self, data: Dict) -> int:
        size = abs(self._gate_int(data.get('size')))
        if data.get('left') is None:
            return size if data.get('status') == 'finished' else 0
        left = abs(self._gate_int(data.get('left')))
        return max(size - left, 0)

    def _parse_gate_response(self, data: Dict, quanto_multiplier: float, allow_partial: bool = False) -> Dict:
        """
        解析 Gate 期货订单响应

        响应字段：
        - status: finished
        - size: 成交张数（负数=空头）
        - fill_price: 成交均价
        """
        status = data.get('status', '')
        filled_size = self._gate_filled_contracts(data)
        if status != 'finished' and not (allow_partial and filled_size > 0):
            return {
                'success': False,
                'reason': f"订单状态异常: {status}, id={data.get('id')}, "
                         f"finish_as={data.get('finish_as', 'unknown')}"
            }

        fill_price_str = data.get('fill_price', '0')
        exec_price = float(fill_price_str) if fill_price_str else 0
        size = filled_size

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

    def fetch_gate_futures_account_book(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Dict]:
        """拉取 Gate USDT 永续账户账务流水（只读，资金快照收益使用）。"""
        method = 'GET'
        api_path = '/api/v4/futures/usdt/account_book'
        all_rows: List[Dict] = []
        offset = 0
        page_limit = min(max(int(limit or 1000), 1), 1000)

        while True:
            params = {'limit': page_limit, 'offset': offset}
            if start_time is not None:
                params['from'] = int(start_time)
            if end_time is not None:
                params['to'] = int(end_time)
            query_string = urlencode(params)
            headers = self._gate_sign(method, api_path, query_string, '')
            url = f"{self.config.gate_base_url}{api_path}?{query_string}"
            resp = self._session.get(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code != 200:
                raise RuntimeError(f"Gate account_book HTTP {resp.status_code}: {resp.text[:200]}")

            data = resp.json()
            rows = data if isinstance(data, list) else []
            all_rows.extend(rows)
            if len(rows) < page_limit:
                break
            offset += page_limit
            if len(all_rows) >= 10000:
                logger.warning("Gate account_book reached 10000 row cap for capital snapshot")
                break
        return all_rows

    def topup_gate_margin(self, contract: str, amount: float) -> Dict:
        """
        追加 Gate 逐仓保证金。

        API: POST /api/v4/futures/usdt/positions/{contract}/margin?change=...
        """
        contract = str(contract or '').upper()
        change = round(float(amount or 0), 6)
        if not contract or change <= 0:
            return {'success': False, 'message': f'追加金额无效: contract={contract}, amount={amount}'}

        method = 'POST'
        api_path = f'/api/v4/futures/usdt/positions/{contract}/margin'
        query_string = urlencode({'change': f'{change:.6f}'})
        headers = self._gate_sign(method, api_path, query_string, '')
        url = f"{self.config.gate_base_url}{api_path}?{query_string}"

        try:
            resp = self._session.post(url, headers=headers, timeout=self.config.timeout_sec)
            if resp.status_code not in (200, 201):
                return {
                    'success': False,
                    'message': f'Gate追保失败 HTTP {resp.status_code}: {resp.text[:200]}',
                }
            data = resp.json() if resp.text else {}
            logger.info(f"Gate 追加保证金成功 | {contract} | amount={change:.6f}")
            return {'success': True, 'message': 'Gate追保成功', 'data': data, 'amount': change}
        except requests.exceptions.Timeout:
            return {'success': False, 'message': f'Gate追保请求超时({self.config.timeout_sec}s)'}
        except requests.exceptions.ConnectionError as e:
            return {'success': False, 'message': f'Gate追保连接失败: {str(e)[:100]}'}
        except Exception as e:
            logger.error(f"Gate 追加保证金异常 | {contract} | {e}", exc_info=True)
            return {'success': False, 'message': f'Gate追保异常: {str(e)[:100]}'}

    # ──────────────────────────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────────────────────────

    def _get_quanto_multiplier(self, base_asset: str) -> float:
        """获取合约面值乘数"""
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0

    def _format_gate_price(self, base_asset: str, price: float) -> str:
        """按 Gate 合约价格最小变动单位格式化保护限价。"""
        if price <= 0:
            return '0'
        precision = 8
        if base_asset in self.contract_meta:
            order_price_round = self.contract_meta[base_asset].get('order_price_round')
            if order_price_round:
                try:
                    precision = self._precision_from_tick_str(str(order_price_round))
                except Exception:
                    precision = 8
        return f"{price:.{precision}f}"

    @staticmethod
    def _precision_from_tick_str(tick_str: str) -> int:
        from decimal import Decimal, InvalidOperation
        try:
            d = Decimal(tick_str)
            return max(0, -d.as_tuple().exponent)
        except (InvalidOperation, ValueError):
            return 8

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
