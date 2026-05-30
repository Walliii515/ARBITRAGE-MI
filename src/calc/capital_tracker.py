# coding: utf-8
"""
虚拟资金跟踪模块

基于持仓数据计算两个交易所的资金占用、保证金占用和可用余额。
逐仓模式下每个持仓独立计算。

计算逻辑：
- Binance (现货):
    开仓: 扣减 spot_open_amount + 手续费
    平仓: 回收 spot_close_amount - 手续费
    浮动: 持仓中仓位的 current_spot_price * spot_open_qty (市值)

- Gate (期货逐仓):
    开仓: 扣减保证金 = future_open_amount / leverage + 手续费
    平仓: 回收保证金 + 期货盈亏 - 手续费
    浮动: 持仓中仓位的保证金 + 未实现盈亏
"""
from dataclasses import dataclass
from typing import Dict, List


@dataclass
class CapitalConfig:
    """资金跟踪配置"""
    leverage: float = 2.0
    binance_initial: float = 100000.0
    gate_initial: float = 100000.0
    fee_spot_open: float = 0.00075
    fee_spot_close: float = 0.00075
    fee_future_open: float = 0.00075
    fee_future_close: float = 0.00075


def calculate_account_summary(positions: List[Dict], cfg: CapitalConfig) -> Dict:
    """
    根据全部持仓记录计算两个交易所的资金汇总

    Args:
        positions: 全部持仓记录（含 holding 和 closed），已由 calculate_realtime_pnl 富化

    Returns:
        {
            'binance': {initial, capital_used, floating_value, realized_pnl, fees, available},
            'gate': {initial, margin_used, floating_value, realized_pnl, fees, available},
            'total': {initial, used, floating_pnl, realized_pnl, fees, available, net_value}
        }
    """
    leverage = cfg.leverage
    binance_initial = cfg.binance_initial
    gate_initial = cfg.gate_initial

    fee_spot_open = cfg.fee_spot_open
    fee_spot_close = cfg.fee_spot_close
    fee_future_open = cfg.fee_future_open
    fee_future_close = cfg.fee_future_close

    # Binance 累计
    binance_capital_used = 0.0      # 持仓中占用的资金
    binance_floating_value = 0.0    # 持仓中按当前市价的浮动市值
    binance_realized_pnl = 0.0      # 已平仓的现货端盈亏
    binance_fees = 0.0              # 所有现货手续费

    # Gate 累计
    gate_margin_used = 0.0          # 持仓中占用的保证金
    gate_floating_value = 0.0       # 持仓中保证金 + 未实现盈亏
    gate_realized_pnl = 0.0         # 已平仓的期货端盈亏
    gate_fees = 0.0                 # 所有期货手续费

    for pos in positions:
        spot_open_amount = float(pos.get('spot_open_amount') or 0)
        future_open_amount = float(pos.get('future_open_amount') or spot_open_amount)
        # 如果 future_open_amount 未存储，用 future_open_qty * future_open_price 计算
        if not pos.get('future_open_amount'):
            future_qty = float(pos.get('future_open_qty') or 0)
            future_open_price = float(pos.get('future_open_price') or 0)
            future_open_amount = future_qty * future_open_price

        initial_margin = future_open_amount / leverage

        if pos.get('status') == 'holding':
            # ── 持仓中 ──
            # Binance: 占用 = 开仓金额
            binance_capital_used += spot_open_amount
            # Binance: 浮动市值 = 当前价 * 持仓量
            current_spot = float(pos.get('current_spot_price') or 0)
            spot_qty = float(pos.get('spot_open_qty') or 0)
            binance_floating_value += current_spot * spot_qty if current_spot > 0 else spot_open_amount

            # Gate: 保证金占用 = 开仓名义价值 / 杠杆
            gate_margin_used += initial_margin
            # Gate: 浮动价值 = 初始保证金 + 未实现盈亏(空头)
            current_future = float(pos.get('current_future_price') or 0)
            future_qty = float(pos.get('future_open_qty') or 0)
            future_open_price = float(pos.get('future_open_price') or 0)
            if current_future > 0 and future_open_price > 0:
                unrealised_pnl = future_qty * (future_open_price - current_future)
                gate_floating_value += initial_margin + unrealised_pnl
            else:
                gate_floating_value += initial_margin

            # 手续费（仅开仓部分，平仓还没发生）
            binance_fees += spot_open_amount * fee_spot_open
            gate_fees += future_open_amount * fee_future_open

        elif pos.get('status') == 'closed':
            # ── 已平仓 ──
            spot_close_amount = float(pos.get('spot_close_amount') or 0)
            future_close_amount = float(pos.get('future_close_amount') or 0)
            # 如果 future_close_amount 未存储
            if not pos.get('future_close_amount'):
                future_close_qty = float(pos.get('future_open_qty') or 0)
                future_close_price = float(pos.get('future_close_price') or 0)
                future_close_amount = future_close_qty * future_close_price

            # Binance 现货端盈亏 = 卖出回收 - 买入成本
            binance_realized_pnl += spot_close_amount - spot_open_amount

            # Gate 期货端盈亏 = (开仓价 - 平仓价) * 数量 (空头)
            future_qty = float(pos.get('future_open_qty') or 0)
            future_open_price = float(pos.get('future_open_price') or 0)
            future_close_price = float(pos.get('future_close_price') or 0)
            futures_pnl = future_qty * (future_open_price - future_close_price)
            gate_realized_pnl += futures_pnl

            # 手续费（开仓 + 平仓）
            binance_fees += spot_open_amount * fee_spot_open + spot_close_amount * fee_spot_close
            gate_fees += future_open_amount * fee_future_open + future_close_amount * fee_future_close

    # 计算可用余额
    # Binance: 初始入金 - 当前持仓占用 + 已实现盈亏 - 手续费
    binance_available = binance_initial - binance_capital_used + binance_realized_pnl - binance_fees
    # Gate: 初始入金 - 当前保证金占用 + 已实现盈亏 - 手续费
    gate_available = gate_initial - gate_margin_used + gate_realized_pnl - gate_fees

    # 净值 = 可用 + 浮动价值
    binance_net = binance_available + binance_floating_value
    gate_net = gate_available + gate_floating_value

    total_initial = binance_initial + gate_initial
    total_used = binance_capital_used + gate_margin_used
    total_available = binance_available + gate_available

    # 合计部分直接复用 position_pnl_calculator 已注入的字段，确保与顶部统计栏口径一致
    total_floating_pnl = sum(float(p.get('floating_pnl_total') or 0) for p in positions)
    total_realized_pnl = sum(float(p.get('realized_pnl') or 0) for p in positions)
    total_funding_pnl = sum(float(p.get('funding_total_pnl') or 0) for p in positions)
    total_fee_cost = sum(float(p.get('fee_cost') or 0) for p in positions)
    total_pnl = sum(float(p.get('total_pnl') or 0) for p in positions)
    total_fees = binance_fees + gate_fees

    # 总净值 = 初始资金 + 总盈亏，确保公式一致性
    total_net = total_initial + total_pnl

    return {
        'binance': {
            'initial': round(binance_initial, 2),
            'capital_used': round(binance_capital_used, 2),
            'floating_value': round(binance_floating_value, 2),
            'realized_pnl': round(binance_realized_pnl, 4),
            'fees': round(binance_fees, 4),
            'available': round(binance_available, 2),
            'net_value': round(binance_net, 2),
        },
        'gate': {
            'initial': round(gate_initial, 2),
            'margin_used': round(gate_margin_used, 2),
            'floating_value': round(gate_floating_value, 2),
            'realized_pnl': round(gate_realized_pnl, 4),
            'fees': round(gate_fees, 4),
            'available': round(gate_available, 2),
            'net_value': round(gate_net, 2),
        },
        'total': {
            'initial': round(total_initial, 2),
            'used': round(total_used, 2),
            'floating_pnl': round(total_floating_pnl, 4),
            'realized_pnl': round(total_realized_pnl, 4),
            'funding_pnl': round(total_funding_pnl, 4),
            'fee_cost': round(total_fee_cost, 4),
            'total_pnl': round(total_pnl, 4),
            'fees': round(total_fees, 4),
            'available': round(total_available, 2),
            'net_value': round(total_net, 2),
        },
    }
