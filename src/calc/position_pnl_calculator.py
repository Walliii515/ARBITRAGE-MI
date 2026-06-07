# coding: utf-8
"""
持仓盈亏计算模块

计算持仓实时盈亏（浮动盈亏、已实现盈亏、总盈亏），与推送逻辑解耦。
"""
from dataclasses import dataclass
from typing import Dict, List, Tuple

from calc.orderbook_enricher import calc_vwap_basis_bps, calc_open_fee_bps, calc_full_fee_bps


@dataclass
class PnlConfig:
    """盈亏计算配置"""
    open_amount_usdt: float
    spot_open_fee: float
    spot_close_fee: float
    future_open_fee: float
    future_close_fee: float
    risk_relief_bps: float
    margin_leverage: float = 2.0
    margin_default_mmr: float = 0.005


def _calc_funding_bps(funding_total_pnl, open_amount_usdt: float) -> Tuple[float, float]:
    """计算资金费收益金额及BPS"""
    funding_pnl = float(funding_total_pnl or 0)
    funding_pnl_bps = (
        round(funding_pnl / open_amount_usdt * 10000, 2)
        if open_amount_usdt else 0.0
    )
    return funding_pnl, funding_pnl_bps


def _calc_total_pnl(floating_pnl_bps, realized_pnl_bps: float,
                    funding_pnl_bps: float, fee_bps: float,
                    floating_pnl: float, realized_pnl: float,
                    funding_pnl: float, fee_cost: float) -> Tuple[float, float]:
    """总盈亏 = 浮动 + 已实现 + 资金费 + 手续费（BPS和金额）"""
    total_bps = round((floating_pnl_bps or 0) + realized_pnl_bps + funding_pnl_bps + fee_bps, 2)
    total_amt = round(floating_pnl + realized_pnl + funding_pnl + fee_cost, 4)
    return total_bps, total_amt


def _format_dt(value) -> str | None:
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value) if value else None


def calculate_realtime_pnl(positions: List[Dict], close_vwaps: Dict[str, Dict],
                           contract_meta: Dict[str, Dict], cfg: PnlConfig) -> List[Dict]:
    """
    计算持仓实时盈亏（就地修改 positions 并返回）

    Args:
        positions: PositionTracker.get_holding_positions() 返回的持仓列表
        close_vwaps: base_asset -> {'spot_close_vwap': float, 'future_close_vwap': float}
        contract_meta: base_asset -> {funding_rate_24h, funding_next_apply, maintenance_rate, ...}
        cfg: 盈亏计算配置

    Returns:
        注入了 PnL 字段的持仓列表（同一引用）
    """
    fee_bps_full = calc_full_fee_bps(cfg.spot_open_fee, cfg.spot_close_fee,
                                      cfg.future_open_fee, cfg.future_close_fee)
    fee_bps_open = calc_open_fee_bps(cfg.spot_open_fee, cfg.future_open_fee)

    # 保证金风控配置（通过 PnlConfig 注入）
    margin_leverage = cfg.margin_leverage
    margin_default_mmr = cfg.margin_default_mmr

    for pos in positions:
        ba = pos.get('base_asset', '')
        vwap_data = close_vwaps.get(ba)

        # 注入费率 (bps) - 根据状态区分：持仓中只显示开仓费，已平仓显示全部费
        if pos.get('status') == 'closed':
            pos['fee_bps'] = fee_bps_full
        else:
            pos['fee_bps'] = fee_bps_open

        # 注入风险缓释 (bps)
        pos['risk_relief_bps'] = cfg.risk_relief_bps

        # 注入实时资金费率与支付窗口
        c_meta = contract_meta.get(ba, {})
        pos['funding_rate'] = c_meta.get('funding_rate')
        pos['funding_rate_24h'] = c_meta.get('funding_rate_24h')
        interval_sec = c_meta.get('funding_interval')
        pos['funding_interval'] = interval_sec
        pos['funding_interval_hours'] = (
            round(float(interval_sec) / 3600, 4) if interval_sec else None
        )
        pos['funding_last_apply'] = _format_dt(c_meta.get('funding_last_apply'))
        pos['funding_next_apply'] = _format_dt(c_meta.get('funding_next_apply'))

        # ── 已平仓分支：浮动盈亏归零，用实际平仓VWAP锁定已实现盈亏 ──
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

            # 资金费收益
            funding_pnl, funding_pnl_bps = _calc_funding_bps(
                pos.get('funding_total_pnl'), cfg.open_amount_usdt
            )
            pos['funding_pnl_bps'] = funding_pnl_bps

            # 手续费金额 - 已平仓用全部手续费(30bps)
            fee_cost_usdt = round(fee_bps_full / 10000 * cfg.open_amount_usdt, 4)
            pos['fee_cost'] = fee_cost_usdt

            # 已实现盈亏 = 纯价差利润（开仓基差 - 平仓基差）
            open_spread = float(pos.get('open_spread_bps') or 0)
            close_spread = float(pos.get('close_spread_bps') or 0)
            spread_pnl_bps = open_spread - close_spread
            pos['realized_pnl_bps'] = round(spread_pnl_bps, 2)
            pos['realized_pnl'] = round(
                spread_pnl_bps / 10000 * cfg.open_amount_usdt, 4
            )

            # 总盈亏
            pos['total_pnl_bps'], pos['total_pnl'] = _calc_total_pnl(
                0, spread_pnl_bps, funding_pnl_bps, fee_bps_full,
                0, pos['realized_pnl'], funding_pnl, fee_cost_usdt
            )
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

            # 资金费收益
            funding_pnl, funding_pnl_bps = _calc_funding_bps(
                pos.get('funding_total_pnl'), cfg.open_amount_usdt
            )
            pos['funding_pnl_bps'] = funding_pnl_bps

            # 手续费金额 - 持仓中只计开仓手续费(15bps)
            fee_cost_usdt = round(fee_bps_open / 10000 * cfg.open_amount_usdt, 4)
            pos['fee_cost'] = fee_cost_usdt

            # 已实现盈亏 = 0（持仓中尚无价差利润）
            pos['realized_pnl_bps'] = 0
            pos['realized_pnl'] = 0

            # 总盈亏
            pos['total_pnl_bps'], pos['total_pnl'] = _calc_total_pnl(
                pos['floating_pnl_bps'], 0, funding_pnl_bps, fee_bps_open,
                pos['floating_pnl_total'], 0, funding_pnl, fee_cost_usdt
            )

            # ── 保证金风控指标注入（逐仓模式空头爆仓价 + 距爆仓距离）──
            maintenance_rate = c_meta.get('maintenance_rate') or margin_default_mmr
            initial_margin = future_open_price * future_qty / margin_leverage
            topup_total = float(pos.get('margin_topup_total') or 0)
            total_margin = initial_margin + topup_total
            pos['margin_initial'] = round(initial_margin, 6)
            pos['current_margin'] = round(total_margin, 6)
            liq_price = future_open_price + total_margin / future_qty - maintenance_rate * future_open_price
            pos['liq_price'] = round(liq_price, 6)
            if current_future > 0:
                liq_distance_pct = (liq_price - current_future) / current_future * 100
                pos['liq_distance_pct'] = round(liq_distance_pct, 2)
            else:
                pos['liq_distance_pct'] = None
        else:
            pos['current_spot_price'] = None
            pos['current_future_price'] = None
            pos['current_spread_bps'] = None
            pos['floating_pnl_total'] = None
            pos['floating_pnl_bps'] = None
            pos['funding_pnl_bps'] = None
            pos['fee_cost'] = None
            pos['realized_pnl_bps'] = None
            pos['realized_pnl'] = None
            pos['total_pnl_bps'] = None
            pos['total_pnl'] = None
            pos['margin_initial'] = None
            pos['current_margin'] = None
            pos['liq_price'] = None
            pos['liq_distance_pct'] = None

    return positions
