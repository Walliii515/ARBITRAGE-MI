# coding: utf-8
"""
对冲指标计算模块
为合并后的跨交易所订单簿行计算：对冲数量对齐、VWAP、盘口深度
"""
from functools import lru_cache
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from math import gcd
from typing import Any, Dict, List, Optional

from common.tools import truncate_to_precision
from calc.orderbook_enricher import calc_vwap

LEVEL = 20


def _lcm_fraction(a: Fraction, b: Fraction) -> Fraction:
    """计算两个 Fraction 的最小公倍数"""
    return a * b / Fraction(gcd(a.numerator * b.denominator, b.numerator * a.denominator),
                            a.denominator * b.denominator)



# truncate_to_precision 已统一到 common.tools，别名保留以减少下游修改
_truncate_to_tick = truncate_to_precision
# calc_vwap 已统一到 calc.orderbook_enricher，别名保留以减少下游修改
_calc_vwap = calc_vwap


@lru_cache(maxsize=512)
def _calc_aligned_step(step_size: float, quanto_multiplier: float) -> float:
    """
    计算对齐步长：LCM(step_size, quanto_multiplier)
    使用 Fraction 避免浮点精度问题

    结果按 (step_size, quanto_multiplier) 缓存，避免每秒重复计算。
    """
    f_step = Fraction(step_size).limit_denominator(10 ** 10)
    f_quant = Fraction(quanto_multiplier).limit_denominator(10 ** 10)
    lcm = f_step * f_quant / Fraction(gcd(
        f_step.numerator * f_quant.denominator,
        f_quant.numerator * f_step.denominator
    ), f_step.denominator * f_quant.denominator)
    return float(lcm)


def _side_total_value(prices: List[Optional[float]], volumes: List[Optional[float]],
                      qty_multiplier: float = 1.0) -> float:
    """计算某侧5档总金额（用于盘口深度计算）"""
    total = 0.0
    for price, vol in zip(prices, volumes):
        if price is None or vol is None:
            continue
        total += float(price) * float(vol) * qty_multiplier
    return total


def _get_side_data(row: Dict[str, Any], prefix: str, side: str) -> tuple:
    """
    提取指定前缀和方向的5档价格与数量列表

    Returns:
        (prices, volumes) 各为长度5的列表
    """
    prices = [row.get(f'{prefix}_price_{side}_{i}') for i in range(1, LEVEL + 1)]
    volumes = [row.get(f'{prefix}_volume_{side}_{i}') for i in range(1, LEVEL + 1)]
    return prices, volumes


def _null_fields() -> Dict[str, None]:
    """返回所有输出字段均为 None 的字典"""
    return {
        'spot_qty': None,
        'future_qty': None,
        'open_coverage': None,
        'close_coverage': None,
        'spot_open_coverage': None,
        'future_open_coverage': None,
        'spot_close_coverage': None,
        'future_close_coverage': None,
        'spot_open_vwap': None,
        'spot_close_vwap': None,
        'future_open_vwap': None,
        'future_close_vwap': None,
    }


def calculate_hedge_metrics(
    merged_rows: List[Dict],
    contract_info: Dict[str, Dict],  # base_asset -> {quanto_multiplier, order_size_min, enable_decimal}
    spot_info: Dict[str, Dict],      # base_asset -> {step_size, min_qty}
    open_amount_usdt: float
) -> List[Dict]:
    """
    为每行合并订单簿数据添加对冲计算字段后返回。

    计算内容：
    - hedged_qty: 对冲数量（已对齐最小步长）
    - open_coverage / close_coverage: 开仓/平仓盘口覆盖（>1 表示流动性不足）
    - spot_open_vwap / spot_close_vwap: 现货开仓/平仓 VWAP
    - future_open_vwap / future_close_vwap: 合约开仓/平仓 VWAP

    Args:
        merged_rows: merge_cross_exchange_orderbook 输出的合并行列表
        contract_info: Gate 合约信息，key 为 base_asset
        spot_info: Binance 现货信息，key 为 base_asset
        open_amount_usdt: 单次开仓 USDT 名义金额

    Returns:
        添加了计算字段的行列表（原行对象被修改并返回）
    """
    result: List[Dict] = []

    for row in merged_rows:
        base_asset = row.get('base_asset')

        # ---------- 边界检查 ----------
        # base_asset 在 contract_info 或 spot_info 中不存在
        if not base_asset or base_asset not in contract_info or base_asset not in spot_info:
            row.update(_null_fields())
            result.append(row)
            continue

        # spot 数据未就绪
        if not row.get('spot_ready', False):
            row.update(_null_fields())
            result.append(row)
            continue

        # Gate 本地簿必须已收到 OBU full 快照并接上连续 WS 增量
        if not row.get('future_ready', True):
            row.update(_null_fields())
            result.append(row)
            continue

        # 参考价：现货卖1价
        current_price_raw = row.get('spot_price_ask_1')
        if current_price_raw is None or float(current_price_raw) == 0:
            row.update(_null_fields())
            result.append(row)
            continue

        current_price = float(current_price_raw)

        # ---------- 读取合约/现货参数 ----------
        c_info = contract_info[base_asset]
        s_info = spot_info[base_asset]

        quanto_multiplier = float(c_info.get('quanto_multiplier', 1.0))
        step_size = float(s_info.get('step_size', 1.0))

        # ---------- 1. 对冲数量对齐 ----------
        aligned_step = _calc_aligned_step(step_size, quanto_multiplier)
        raw_qty = open_amount_usdt / current_price
        if aligned_step > 0:
            hedged_qty = round(raw_qty / aligned_step) * aligned_step
        else:
            hedged_qty = raw_qty

        # ---------- 2. 提取各侧盘口数据 ----------
        # 现货 ask 侧（开仓用，price 升序）
        spot_ask_prices, spot_ask_volumes = _get_side_data(row, 'spot', 'ask')
        # 现货 bid 侧（平仓用，price 降序，bid1 为最高价）
        spot_bid_prices, spot_bid_volumes = _get_side_data(row, 'spot', 'bid')
        # 合约 bid 侧（做空开仓用）
        future_bid_prices, future_bid_volumes = _get_side_data(row, 'future', 'bid')
        # 合约 ask 侧（做空平仓买回用）
        future_ask_prices, future_ask_volumes = _get_side_data(row, 'future', 'ask')

        # ---------- 3. VWAP 计算（市价单真实成交均价，无需floor截断） ----------
        # 说明：市价单按盘口逐档成交，VWAP是加权平均价的计算结果
        # VWAP不需要满足交易所tick_size规则（那是限价单的要求）
        # 例如：VWAP可能=0.011655，这是真实成交均价，不是提交的限价

        # 现货开仓 VWAP（买入用 ask 侧，无换算）
        spot_open_vwap = _calc_vwap(spot_ask_prices, spot_ask_volumes, hedged_qty, 1.0)
        # 现货平仓 VWAP（卖出用 bid 侧，无换算）
        spot_close_vwap = _calc_vwap(spot_bid_prices, spot_bid_volumes, hedged_qty, 1.0)
        # 合约开仓 VWAP（做空卖出用 bid 侧，volume 需乘 quanto_multiplier）
        future_open_vwap = _calc_vwap(future_bid_prices, future_bid_volumes, hedged_qty, quanto_multiplier)
        # 合约平仓 VWAP（做空买回用 ask 侧，volume 需乘 quanto_multiplier）
        future_close_vwap = _calc_vwap(future_ask_prices, future_ask_volumes, hedged_qty, quanto_multiplier)

        # ---------- 4. 盘口深度计算 ----------
        # 各侧5档总金额
        spot_ask_total = _side_total_value(spot_ask_prices, spot_ask_volumes, 1.0)
        spot_bid_total = _side_total_value(spot_bid_prices, spot_bid_volumes, 1.0)
        future_bid_total = _side_total_value(future_bid_prices, future_bid_volumes, quanto_multiplier)
        future_ask_total = _side_total_value(future_ask_prices, future_ask_volumes, quanto_multiplier)

        trade_value = hedged_qty * current_price

        # 各侧独立覆盖率
        spot_open_coverage = (trade_value / spot_ask_total) if spot_ask_total > 0 else None
        future_open_coverage = (trade_value / future_bid_total) if future_bid_total > 0 else None
        spot_close_coverage = (trade_value / spot_bid_total) if spot_bid_total > 0 else None
        future_close_coverage = (trade_value / future_ask_total) if future_ask_total > 0 else None

        # 开仓盘口覆盖：取两侧最大值（能反映最佳覆盖情况）
        open_coverage = max(x for x in [spot_open_coverage, future_open_coverage] if x is not None) \
            if any(x is not None for x in [spot_open_coverage, future_open_coverage]) else None

        # 平仓盘口覆盖：取两侧最大值（能反映最佳覆盖情况）
        close_coverage = max(x for x in [spot_close_coverage, future_close_coverage] if x is not None) \
            if any(x is not None for x in [spot_close_coverage, future_close_coverage]) else None

        # ---------- 5. 写回输出字段 ----------
        row.update({
            'spot_qty': hedged_qty,
            'future_qty': hedged_qty,
            'open_coverage': open_coverage,
            'close_coverage': close_coverage,
            'spot_open_coverage': spot_open_coverage,
            'future_open_coverage': future_open_coverage,
            'spot_close_coverage': spot_close_coverage,
            'future_close_coverage': future_close_coverage,
            'spot_open_vwap': spot_open_vwap,
            'spot_close_vwap': spot_close_vwap,
            'future_open_vwap': future_open_vwap,
            'future_close_vwap': future_close_vwap,
        })
        result.append(row)

    return result
