# coding: utf-8
"""
行情数据富化模块

为合并后的订单簿行注入元数据和计算字段，供 WS 快照推送与开仓检查共用。
消除 orderbook_server 中 build_payload 与 _open_position_loop 的重复逻辑。
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

LEVEL = 20


def calc_vwap(prices: List[Optional[float]], volumes: List[Optional[float]],
              target_qty: float, qty_multiplier: float = 1.0) -> Optional[float]:
    """
    按多档盘口计算 VWAP（市价单真实成交均价）

    逻辑：
    - 市价单按盘口逐档成交，VWAP是加权平均价的计算结果
    - VWAP不需要满足交易所tick_size规则（那是限价单的要求）
    - 例如：VWAP可能=0.011655，这是真实成交均价，不是提交的限价

    Args:
        prices: 各档价格列表（ask 侧升序，bid 侧降序）
        volumes: 各档数量列表（原始单位）
        target_qty: 目标成交量（标的资产数量）
        qty_multiplier: 数量单位换算乘数（Gate期货张数 -> 标的资产数量）

    Returns:
        加权均价，若无任何有效数据则返回 None
    """
    total_cost = 0.0
    total_filled = 0.0
    remaining = target_qty

    for price, vol in zip(prices, volumes):
        if price is None or vol is None:
            continue
        price = float(price)
        vol = float(vol) * qty_multiplier  # 换算为标的资产数量

        if remaining <= 0:
            break
        if vol <= 0:
            continue

        # 本档实际可用数量
        fill = min(vol, remaining)
        total_cost += price * fill
        total_filled += fill
        remaining -= fill

    if total_filled <= 0:
        return None
    return total_cost / total_filled


@dataclass
class EnrichConfig:
    """富化所需的配置参数（从 config.yaml 读取，一次构造多处复用）"""
    open_amount_usdt: float
    funding_threshold_percentile: str
    risk_relief_bps: float
    spot_open_fee: float
    spot_close_fee: float
    future_open_fee: float
    future_close_fee: float
    close_threshold_col: str = 'close_basis_p20'
    open_vwap_basis_threshold_bps: float = 0.0
    funding_entry_enabled: bool = True
    funding_entry_capture_ratio: float = 0.5
    funding_entry_slippage_buffer_bps: float = 10.0
    funding_entry_min_expected_edge_bps: float = 0.0
    funding_entry_strong_funding_24h_bps: float = 50.0
    funding_entry_discount_ratio: float = 0.2
    funding_entry_max_discount_bps: float = 10.0


def calc_vwap_basis_bps(spot_vwap, future_vwap) -> Optional[float]:
    """
    VWAP 基差 bps 计算（公共函数）

    公式: (future_vwap - spot_vwap) / spot_vwap * 10000

    Returns:
        基差 bps 值，任一输入无效则返回 None
    """
    if spot_vwap is None or future_vwap is None:
        return None
    spot_vwap = float(spot_vwap)
    future_vwap = float(future_vwap)
    if spot_vwap == 0:
        return None
    return (future_vwap - spot_vwap) / spot_vwap * 10000


def calc_open_fee_bps(spot_open_fee: float, future_open_fee: float) -> float:
    """开仓手续费 BPS（负数）"""
    return round(-(spot_open_fee + future_open_fee) * 10000, 2)


def calc_full_fee_bps(spot_open_fee: float, spot_close_fee: float,
                      future_open_fee: float, future_close_fee: float) -> float:
    """全部手续费 BPS（开+平，负数）"""
    return round(-(spot_open_fee + spot_close_fee +
                   future_open_fee + future_close_fee) * 10000, 2)


def _spread_bps(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    bid = float(bid)
    ask = float(ask)
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


def calc_entry_snapshot_bps(base_asset: str, basis_bps: Optional[float],
                            funding_rate_24h: Optional[float],
                            vwap_threshold_meta: Dict[str, Dict],
                            cfg: EnrichConfig) -> Dict[str, Optional[float]]:
    """计算与 TradingExecutor 一致的 funding-adjusted 入场门槛，供监控页展示/过滤。"""
    threshold_data = vwap_threshold_meta.get(base_asset, {})
    p20 = float(threshold_data.get('p20', cfg.open_vwap_basis_threshold_bps))
    funding_24h_bps = float(funding_rate_24h or 0.0) * 10000.0
    fee_full_bps = -calc_full_fee_bps(
        cfg.spot_open_fee, cfg.spot_close_fee,
        cfg.future_open_fee, cfg.future_close_fee,
    )

    if not cfg.funding_entry_enabled:
        entry_floor = p20
        expected_funding_bps = 0.0
        carry_floor = p20
        timing_floor = p20
        discount_bps = 0.0
    else:
        expected_funding_bps = funding_24h_bps * cfg.funding_entry_capture_ratio
        carry_floor = (
            cfg.funding_entry_min_expected_edge_bps
            + fee_full_bps
            + cfg.funding_entry_slippage_buffer_bps
            - expected_funding_bps
        )
        discount_bps = min(
            cfg.funding_entry_max_discount_bps,
            max(0.0, funding_24h_bps) * cfg.funding_entry_discount_ratio,
        )
        timing_floor = p20 - discount_bps
        entry_floor = max(carry_floor, timing_floor)
        if funding_24h_bps < cfg.funding_entry_strong_funding_24h_bps:
            entry_floor = max(entry_floor, p20)

    expected_edge_bps = None
    if basis_bps is not None:
        expected_edge_bps = (
            float(basis_bps)
            + expected_funding_bps
            - fee_full_bps
            - cfg.funding_entry_slippage_buffer_bps
        )

    return {
        'entry_floor_bps': round(entry_floor, 4),
        'entry_p20_bps': round(p20, 4),
        'entry_funding_24h_bps': round(funding_24h_bps, 4),
        'entry_expected_funding_bps': round(expected_funding_bps, 4),
        'entry_carry_floor_bps': round(carry_floor, 4),
        'entry_timing_floor_bps': round(timing_floor, 4),
        'entry_funding_discount_bps': round(discount_bps, 4),
        'entry_expected_edge_bps': round(expected_edge_bps, 4) if expected_edge_bps is not None else None,
    }


def _attach_funding_support_fields(row: Dict, base_asset: str,
                                   funding_support_meta: Dict[str, Dict]) -> None:
    support = funding_support_meta.get(str(base_asset or '').strip().upper()) or {}
    row['funding_rate_24h_avg_bps'] = support.get('funding_rate_24h_avg_bps')
    row['funding_rate_24h_avg_samples'] = support.get('funding_rate_24h_avg_samples')
    row['funding_rate_24h_avg_window_hours'] = support.get('funding_rate_24h_avg_window_hours')


def enrich_trading_fields(rows: List[Dict], contract_meta: Dict[str, Dict],
                          threshold_meta: Dict[str, float], cfg: EnrichConfig,
                          asset_profile_meta: Optional[Dict[str, Dict]] = None,
                          funding_support_meta: Optional[Dict[str, Dict]] = None) -> None:
    """
    为开仓检查富化行数据（就地修改 rows）

    注入字段：
    - open_vwap_basis_bps: 开仓 VWAP 基差 (bps)
    - open_fee_bps: 开仓费率 (bps)
    - risk_relief_bps: 风险缓释 (bps)
    - open_marginal_basis_bps: 开仓边际基差 (bps)
    - funding_rate_24h: 24h 资金费率
    - funding_threshold: 资金费率阈值
    """
    open_fee_bps = calc_open_fee_bps(cfg.spot_open_fee, cfg.future_open_fee)
    asset_profile_meta = asset_profile_meta or {}
    funding_support_meta = funding_support_meta or {}

    for row in rows:
        base_asset = row.get('base_asset', '')
        contract = row.get('contract', '')
        profile = asset_profile_meta.get(base_asset, {})
        row['market_profile'] = profile.get('market_profile', 'normal')
        row['market_profile_reason'] = profile.get('market_profile_reason')

        # 1. 开仓 VWAP 基差 (bps)
        open_basis = calc_vwap_basis_bps(row.get('spot_open_vwap'), row.get('future_open_vwap'))
        row['open_vwap_basis_bps'] = round(open_basis, 2) if open_basis is not None else None

        # 2. 开仓费率 (bps)
        row['open_fee_bps'] = open_fee_bps

        # 3. 风险缓释 (bps)
        row['risk_relief_bps'] = cfg.risk_relief_bps

        # 4. 开仓边际基差 (bps) = VWAP 基差 + 开仓费率 + 风险缓释
        if row['open_vwap_basis_bps'] is not None:
            row['open_marginal_basis_bps'] = round(
                row['open_vwap_basis_bps'] + open_fee_bps + cfg.risk_relief_bps, 2
            )
        else:
            row['open_marginal_basis_bps'] = None

        # 5. 24h 资金费率
        if base_asset in contract_meta:
            row['funding_rate_24h'] = contract_meta[base_asset].get('funding_rate_24h')
        else:
            row['funding_rate_24h'] = None
        _attach_funding_support_fields(row, base_asset, funding_support_meta)

        # 6. 资金费率阈值
        if contract in threshold_meta:
            row['funding_threshold'] = threshold_meta[contract]


def enrich_snapshot_fields(rows: List[Dict], contract_meta: Dict[str, Dict],
                           spot_meta: Dict[str, Dict], threshold_meta: Dict[str, float],
                           vwap_threshold_meta: Dict[str, Dict],
                           cfg: EnrichConfig, meta_update_time: str,
                           close_vwap_threshold_meta: Optional[Dict[str, Dict]] = None,
                           asset_profile_meta: Optional[Dict[str, Dict]] = None,
                           funding_support_meta: Optional[Dict[str, Dict]] = None) -> None:
    """
    为 WS 快照推送富化完整字段（就地修改 rows）

    在 enrich_trading_fields 基础上追加展示字段：
    - volume_24h_settle / quote_volume / funding_next_apply
    - 资金费率阈值百分位 / meta_update_time
    - 平仓 VWAP 基差 / 平仓费率
    - 每档 USDT 值（现货 + 期货）
    - vwap_threshold_bps
    """
    open_fee_bps = calc_open_fee_bps(cfg.spot_open_fee, cfg.future_open_fee)
    close_fee_bps = round(-(cfg.spot_close_fee + cfg.future_close_fee) * 10000, 2)
    asset_profile_meta = asset_profile_meta or {}
    funding_support_meta = funding_support_meta or {}

    for row in rows:
        base_asset = row.get('base_asset', '')
        row['open_amount_usdt'] = cfg.open_amount_usdt
        profile = asset_profile_meta.get(base_asset, {})
        row['market_profile'] = profile.get('market_profile', 'normal')
        row['market_profile_reason'] = profile.get('market_profile_reason')
        row['market_profile_updated_at'] = profile.get('market_profile_updated_at')

        # --- 从 contract_meta 注入 ---
        quanto_multiplier = 1.0
        if base_asset in contract_meta:
            c_meta = contract_meta[base_asset]
            quanto_multiplier = c_meta.get('quanto_multiplier', 1.0)
            row['funding_rate_24h'] = c_meta.get('funding_rate_24h')
            row['volume_24h_settle'] = c_meta.get('volume_24h_settle')
            fna = c_meta.get('funding_next_apply')
            row['funding_next_apply'] = (
                fna.strftime('%Y-%m-%d %H:%M:%S') if hasattr(fna, 'strftime')
                else str(fna) if fna else None
            )
        else:
            row['funding_rate_24h'] = None
            row['volume_24h_settle'] = None
            row['funding_next_apply'] = None
        _attach_funding_support_fields(row, base_asset, funding_support_meta)

        # --- 从 spot_meta 注入 ---
        if base_asset in spot_meta:
            row['quote_volume'] = spot_meta[base_asset].get('quote_volume')
        else:
            row['quote_volume'] = None

        # --- 阈值 ---
        contract_name = f"{base_asset}_USDT"
        row[cfg.funding_threshold_percentile] = threshold_meta.get(contract_name)

        # --- meta_update_time ---
        row['meta_update_time'] = meta_update_time

        # --- 费率 bps ---
        row['open_fee_bps'] = open_fee_bps
        row['close_fee_bps'] = close_fee_bps

        # --- VWAP 基差 bps ---
        open_basis = calc_vwap_basis_bps(row.get('spot_open_vwap'), row.get('future_open_vwap'))
        row['open_vwap_basis_bps'] = open_basis

        close_basis = calc_vwap_basis_bps(row.get('spot_close_vwap'), row.get('future_close_vwap'))
        row['close_vwap_basis_bps'] = close_basis

        # --- 风险缓释 ---
        row['risk_relief_bps'] = cfg.risk_relief_bps

        # --- 开仓边际基差 ---
        if open_basis is not None:
            row['open_marginal_basis_bps'] = open_basis + open_fee_bps + cfg.risk_relief_bps
        else:
            row['open_marginal_basis_bps'] = None

        # --- 期货每档 USDT ---
        future_bid_total = 0.0
        future_ask_total = 0.0
        for i in range(1, LEVEL + 1):
            price = row.get(f'future_price_bid_{i}')
            vol = row.get(f'future_volume_bid_{i}')
            usdt = round(float(price) * float(vol) * quanto_multiplier, 2) if price is not None and vol is not None else None
            row[f'future_usdt_bid_{i}'] = usdt
            if usdt:
                future_bid_total += usdt

            price = row.get(f'future_price_ask_{i}')
            vol = row.get(f'future_volume_ask_{i}')
            usdt = round(float(price) * float(vol) * quanto_multiplier, 2) if price is not None and vol is not None else None
            row[f'future_usdt_ask_{i}'] = usdt
            if usdt:
                future_ask_total += usdt

        row['future_usdt_bid_total'] = round(future_bid_total, 2)
        row['future_usdt_ask_total'] = round(future_ask_total, 2)

        # --- 现货每档 USDT ---
        spot_bid_total = 0.0
        spot_ask_total = 0.0
        for i in range(1, LEVEL + 1):
            price = row.get(f'spot_price_bid_{i}')
            vol = row.get(f'spot_volume_bid_{i}')
            usdt = round(float(price) * float(vol), 2) if price is not None and vol is not None else None
            row[f'spot_usdt_bid_{i}'] = usdt
            if usdt:
                spot_bid_total += usdt

            price = row.get(f'spot_price_ask_{i}')
            vol = row.get(f'spot_volume_ask_{i}')
            usdt = round(float(price) * float(vol), 2) if price is not None and vol is not None else None
            row[f'spot_usdt_ask_{i}'] = usdt
            if usdt:
                spot_ask_total += usdt

        row['spot_usdt_bid_total'] = round(spot_bid_total, 2)
        row['spot_usdt_ask_total'] = round(spot_ask_total, 2)
        row['future_spread_bps'] = _spread_bps(
            row.get('future_price_bid_1'),
            row.get('future_price_ask_1'),
        )
        row['spot_spread_bps'] = _spread_bps(
            row.get('spot_price_bid_1'),
            row.get('spot_price_ask_1'),
        )
        row['future_top_bid_usdt'] = row.get('future_usdt_bid_1')
        row['future_top_ask_usdt'] = row.get('future_usdt_ask_1')
        row['spot_top_bid_usdt'] = row.get('spot_usdt_bid_1')
        row['spot_top_ask_usdt'] = row.get('spot_usdt_ask_1')

        # --- 按标的 VWAP 基差阈值（前端展示用 p20） ---
        threshold_entry = vwap_threshold_meta.get(base_asset)
        row['vwap_threshold_bps'] = threshold_entry.get('p20') if threshold_entry else None

        # --- Funding-adjusted 统一入场门槛（与 TradingExecutor 口径一致） ---
        row.update(calc_entry_snapshot_bps(
            base_asset,
            row.get('open_vwap_basis_bps'),
            row.get('funding_rate_24h'),
            vwap_threshold_meta,
            cfg,
        ))

        # --- 平仓基差阈值参考 ---
        if close_vwap_threshold_meta and base_asset in close_vwap_threshold_meta:
            close_data = close_vwap_threshold_meta[base_asset]
            row['close_vwap_threshold_bps'] = close_data.get(cfg.close_threshold_col)
        else:
            row['close_vwap_threshold_bps'] = None
