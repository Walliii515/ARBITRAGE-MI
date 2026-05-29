"""
虚拟成交引擎模块

独立的成交模拟服务，基于订单簿深度数据计算 VWAP 成交价。
后续切换实盘时，只需将 TradingExecutor 的执行器服务地址指向真实交易所执行器，
本模块不会对实盘产生任何影响。
"""
from typing import Dict

from common.logger import get_logger
from common.tools import truncate_to_precision
from calc.orderbook_enricher import calc_vwap

logger = get_logger(__name__)


class VirtualExecutor:
    """
    虚拟成交引擎

    职责：接收订单组 + 盘口数据，分别计算现货和期货的 VWAP 成交价。
    设计为无状态（元数据通过构造函数注入），可独立运行为 HTTP 服务。
    """

    def __init__(self, contract_meta: Dict, spot_meta: Dict):
        """
        Args:
            contract_meta: base_asset -> {quanto_multiplier, order_price_round, size_decimal, ...}
            spot_meta: base_asset -> {step_size, tick_size, min_qty, ...}
        """
        self.contract_meta = contract_meta
        self.spot_meta = spot_meta
        self.max_levels = 5  # 最多判断5档

    def execute(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        执行虚拟成交

        Args:
            order_group: 订单组，包含 spot_order 和 future_order
            orderbook_row: 当前盘口数据行（合并后的订单簿，含5档深度）

        Returns:
            {
                'success': bool,
                'message': str,
                'spot_order': {exec_price, exec_qty, exec_amount, coverage_ratio},
                'future_order': {exec_price, exec_qty, exec_amount, coverage_ratio}
            }
        """
        result = {'success': False, 'spot_order': None, 'future_order': None, 'message': ''}

        try:
            # 1. 现货成交计算
            spot_result = self._calc_vwap(order_group['spot_order'], orderbook_row, 'spot')
            if not spot_result['success']:
                result['message'] = f"现货拒单: {spot_result['reason']}"
                return result
            result['spot_order'] = spot_result

            # 2. 期货成交计算
            future_result = self._calc_vwap(order_group['future_order'], orderbook_row, 'future')
            if not future_result['success']:
                result['message'] = f"期货拒单: {future_result['reason']}"
                return result
            result['future_order'] = future_result

            result['success'] = True
            result['message'] = '成交成功'
        except Exception as e:
            result['message'] = f"系统异常: {str(e)}"

        return result

    def _calc_vwap(self, order: Dict, orderbook: Dict, market_type: str) -> Dict:
        """
        计算VWAP成交价

        逻辑：
        - 买入(buy) -> 看ask侧（卖盘）
        - 卖出(sell) -> 看bid侧（买盘）
        - 前缀：spot用'spot_', future用'future_'
        - 字段格式：{prefix}_price_{side}_{level}, {prefix}_volume_{side}_{level}
        - 期货volume需乘以quanto_multiplier转换为标的资产数量
        """
        target_qty = float(order['target_qty'])
        base_asset = order['base_asset']
        prefix = 'spot' if market_type == 'spot' else 'future'

        # 确定盘口侧
        side = 'ask' if order['trade_direction'] == 'buy' else 'bid'

        # 提取5档盘口
        prices = []
        volumes = []
        for i in range(1, self.max_levels + 1):
            price = orderbook.get(f'{prefix}_price_{side}_{i}')
            volume = orderbook.get(f'{prefix}_volume_{side}_{i}')
            if price is not None and volume is not None:
                prices.append(float(price))
                volumes.append(float(volume))

        if not prices:
            return {'success': False, 'reason': '盘口数据为空'}

        # 期货需要乘以quanto_multiplier
        qty_multiplier = 1.0
        if market_type == 'future':
            qty_multiplier = self._get_quanto_multiplier(base_asset)

        # 计算5档总流动性
        total_liquidity = sum(vol * qty_multiplier for vol in volumes)

        # 检查流动性是否充足
        if total_liquidity < target_qty:
            coverage_ratio = target_qty / total_liquidity if total_liquidity > 0 else float('inf')
            return {
                'success': False,
                'reason': f'盘口深度不足(覆盖率{coverage_ratio:.2f})',
                'coverage_ratio': coverage_ratio
            }

        # 计算VWAP（使用公共函数）
        exec_price = calc_vwap(prices, volumes, target_qty, qty_multiplier)
        if exec_price is None:
            return {'success': False, 'reason': 'VWAP计算失败'}

        # VWAP = 加权平均价（市价单真实成交均价，无需floor截断）
        # 说明：市价单按盘口逐档成交，VWAP是计算结果而非提交的限价
        # 例如：VWAP可能=0.011655，这是真实成交均价，不需要满足tick_size规则
        # exec_price 已经是 calc_vwap 返回的结果，直接使用
        
        # 数量需要floor截断（交易所对数量有精度要求）
        qty_precision = self._get_qty_precision(base_asset, market_type)
        exec_qty = truncate_to_precision(target_qty, qty_precision)
        exec_amount = round(exec_price * exec_qty, 2)

        coverage_ratio = target_qty / total_liquidity if total_liquidity > 0 else 0

        return {
            'success': True,
            'exec_price': exec_price,
            'exec_qty': exec_qty,
            'exec_amount': exec_amount,
            'coverage_ratio': coverage_ratio
        }

    def _get_price_precision(self, base_asset: str, market_type: str) -> int:
        """从 tick_size(Binance) / order_price_round(Gate) 派生价格小数位数"""
        if market_type == 'spot':
            if base_asset in self.spot_meta:
                tick_size = self.spot_meta[base_asset].get('tick_size')
                if tick_size:
                    return self._precision_from_tick_str(str(tick_size))
            return 8  # 安全默认值
        else:
            if base_asset in self.contract_meta:
                order_price_round = self.contract_meta[base_asset].get('order_price_round')
                if order_price_round:
                    return self._precision_from_tick_str(str(order_price_round))
            return 8  # 安全默认值

    @staticmethod
    def _precision_from_tick_str(tick_str: str) -> int:
        """从最小变动单位字符串推导小数位数，如 '0.0001' -> 4, '0.00001' -> 5"""
        from decimal import Decimal, InvalidOperation
        try:
            d = Decimal(tick_str)
            return max(0, -d.as_tuple().exponent)
        except (InvalidOperation, ValueError):
            return 8

    def _get_qty_precision(self, base_asset: str, market_type: str) -> int:
        if market_type == 'spot':
            if base_asset in self.spot_meta:
                step_size = self.spot_meta[base_asset].get('step_size', 0.00001)
                step_str = str(step_size)
                if '.' in step_str:
                    return len(step_str.split('.')[-1].rstrip('0')) or 0
            return 5
        else:
            if base_asset in self.contract_meta:
                return self.contract_meta[base_asset].get('size_decimal', 0)
            return 0

    def _get_quanto_multiplier(self, base_asset: str) -> float:
        if base_asset in self.contract_meta:
            return float(self.contract_meta[base_asset].get('quanto_multiplier', 1.0))
        return 1.0

    def reload_meta(self, contract_meta: Dict, spot_meta: Dict):
        """热更新元数据（由服务层的 /api/reload 端点调用）"""
        self.contract_meta = contract_meta
        self.spot_meta = spot_meta
        logger.info(f'虚拟成交引擎元数据已刷新: 合约 {len(contract_meta)} 条, 现货 {len(spot_meta)} 条')
