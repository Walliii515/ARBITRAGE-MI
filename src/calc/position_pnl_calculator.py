# coding: utf-8
"""
持仓盈亏计算模块

计算持仓实时盈亏（浮动盈亏、已实现盈亏、总盈亏），与推送逻辑解耦。
"""
from dataclasses import dataclass
from typing import Dict, List

from calc.orderbook_enricher import calc_vwap_basis_bps


@dataclass
class PnlConfig:
    """盈亏计算配置"""
    open_amount_usdt: float
    spot_open_fee: float
    spot_close_fee: float
    future_open_fee: float
    future_close_fee: float
    risk_relief_bps: float


def calculate_realtime_pnl(positions: List[Dict], close_vwaps: Dict[str, Dict],
                           contract_meta: Dict[str, Dict], cfg: PnlConfig) -> List[Dict]:
    """
    计算持仓实时盈亏（就地修改 positions 并返回）

    Args:
        positions: PositionTracker.get_holding_positions() 返回的持仓列表
        close_vwaps: base_asset -> {'spot_close_vwap': float, 'future_close_vwap': float}
        contract_meta: base_asset -> {funding_rate_24h, funding_next_apply, ...}
        cfg: 盈亏计算配置

    Returns:
        注入了 PnL 字段的持仓列表（同一引用）
    """
    fee_bps = round(-(cfg.spot_open_fee + cfg.spot_close_fee +
                      cfg.future_open_fee + cfg.future_close_fee) * 10000, 2)

    for pos in positions:
        ba = pos.get('base_asset', '')
        vwap_data = close_vwaps.get(ba)

        # 注入费率 (bps)
        pos['fee_bps'] = fee_bps

        # 注入风险缓释 (bps)
        pos['risk_relief_bps'] = cfg.risk_relief_bps

        # 注入 24h 资金费率 & 下次支付时间
        c_meta = contract_meta.get(ba, {})
        pos['funding_rate_24h'] = c_meta.get('funding_rate_24h')
        fna = c_meta.get('funding_next_apply')
        pos['funding_next_apply'] = (
            fna.strftime('%Y-%m-%d %H:%M:%S') if hasattr(fna, 'strftime')
            else str(fna) if fna else None
        )

        # ── 已平仓分支：浮动盈产归零，用实际平仓VWAP锁定已实现盈产 ──
        if pos.get('status') == 'closed':
            pos['floating_pnl_total'] = 0
            pos['floating_pnl_bps'] = 0
            # 平仓后展示平仓成交价
            pos['current_spot_price'] = (
                float(pos['spot_close_price']) if pos.get('spot_close_price') else None
            )
            pos['current_future_price'] = (
                float(pos['future_close_price']) if pos.get('future_close_price') else None
            )
            pos['current_spread_bps'] = (
                float(pos['close_spread_bps']) if pos.get('close_spread_bps') else None
            )

            # 资金费收益 (bps)
            funding_pnl = float(pos.get('funding_total_pnl') or 0)
            funding_pnl_bps = (
                round(funding_pnl / cfg.open_amount_usdt * 10000, 2)
                if cfg.open_amount_usdt else 0.0
            )
            pos['funding_pnl_bps'] = funding_pnl_bps

            # 费率金额 (USDT)
            fee_cost_usdt = round(fee_bps / 10000 * cfg.open_amount_usdt, 4)

            # 已实现盈产 (bps) = (开仓基差 - 平仓基差) + 费率bps + 资金费bps
            open_spread = float(pos.get('open_spread_bps') or 0)
            close_spread = float(pos.get('close_spread_bps') or 0)
            spread_pnl_bps = open_spread - close_spread  # 正数=盈利
            pos['realized_pnl_bps'] = round(spread_pnl_bps + fee_bps + funding_pnl_bps, 2)
            pos['realized_pnl'] = round(
                pos['realized_pnl_bps'] / 10000 * cfg.open_amount_usdt, 4
            )
            pos['total_pnl_bps'] = pos['realized_pnl_bps']
            pos['total_pnl'] = pos['realized_pnl']
            continue

        if vwap_data:
            current_spot = vwap_data['spot_close_vwap']
            current_future = vwap_data['future_close_vwap']
            pos['current_spot_price'] = current_spot
            pos['current_future_price'] = current_future

            # 实时价差 (bps)
            basis = calc_vwap_basis_bps(current_spot, current_future)
            pos['current_spread_bps'] = round(basis, 2) if basis is not None else None

            # 浮动盈亏 (bps) = 开仓价差 - 实时价差
            pos['floating_pnl_bps'] = round(
                float(pos['open_spread_bps']) - pos['current_spread_bps'], 2
            ) if pos['current_spread_bps'] is not None else None

            # 浮动盈亏 (绝对值)
            spot_open_price = float(pos['spot_open_price'])
            future_open_price = float(pos['future_open_price'])
            spot_qty = float(pos['spot_open_qty'])
            future_qty = float(pos['future_open_qty'])

            floating_spot = (current_spot - spot_open_price) * spot_qty
            floating_future = (future_open_price - current_future) * future_qty
            pos['floating_pnl_total'] = round(floating_spot + floating_future, 4)

            # 资金费收益 (bps)
            funding_pnl = float(pos.get('funding_total_pnl') or 0)
            funding_pnl_bps = round(
                funding_pnl / cfg.open_amount_usdt * 10000, 2
            ) if cfg.open_amount_usdt else 0.0
            pos['funding_pnl_bps'] = funding_pnl_bps

            # 费率金额 (USDT)
            fee_cost_usdt = round(fee_bps / 10000 * cfg.open_amount_usdt, 4)

            # 已实现盈亏 (bps) = 费率bps + 资金费收益bps
            pos['realized_pnl_bps'] = round(fee_bps + funding_pnl_bps, 2)
            # 已实现盈亏 (金额) = 费率金额 + 资金费收益
            pos['realized_pnl'] = round(fee_cost_usdt + funding_pnl, 4)

            # 总盈亏 (bps) = 浮动盈亏bps + 费率bps + 资金费收益bps
            pos['total_pnl_bps'] = round(
                (pos['floating_pnl_bps'] or 0) + fee_bps + funding_pnl_bps, 2
            )
            # 总盈亏 (金额) = 浮动盈亏 + 资金费收益 + 费率金额
            pos['total_pnl'] = round(
                pos['floating_pnl_total'] + funding_pnl + fee_cost_usdt, 4
            )
        else:
            pos['current_spot_price'] = None
            pos['current_future_price'] = None
            pos['current_spread_bps'] = None
            pos['floating_pnl_total'] = None
            pos['floating_pnl_bps'] = None
            pos['funding_pnl_bps'] = None
            pos['realized_pnl_bps'] = None
            pos['realized_pnl'] = None
            pos['total_pnl_bps'] = None
            pos['total_pnl'] = None

    return positions
