export interface OrderBookRow {
  base_asset: string
  contract: string
  symbol: string
  future_update_id: number | null
  future_update_time: number | null
  spot_update_id: number | null
  spot_update_time: number | null
  spot_ready?: boolean
  market_profile?: string | null
  market_profile_reason?: string | null
  market_profile_updated_at?: string | null
  /** 每次开仓金额（USDT），后端从配置文件推送 */
  open_amount_usdt?: number
  spot_qty?: number | null
  future_qty?: number | null
  funding_rate_24h?: number | null
  funding_rate_24h_avg_bps?: number | null
  funding_rate_24h_avg_samples?: number | null
  funding_rate_24h_avg_window_hours?: number | null
  funding_next_apply?: string | null
  percentile_30?: number | null
  meta_update_time?: string | null
  volume_24h_settle?: number | null
  future_range_24h_pct?: number | null
  quote_volume?: number | null
  open_fee_bps?: number | null
  close_fee_bps?: number | null
  open_coverage?: number | null
  close_coverage?: number | null
  spot_open_coverage?: number | null
  future_open_coverage?: number | null
  spot_close_coverage?: number | null
  future_close_coverage?: number | null
  spot_open_vwap?: number | null
  spot_close_vwap?: number | null
  future_open_vwap?: number | null
  future_close_vwap?: number | null
  // 后端预计算的每档USDT值
  future_usdt_bid_1?: number | null
  future_usdt_bid_2?: number | null
  future_usdt_bid_3?: number | null
  future_usdt_bid_4?: number | null
  future_usdt_bid_5?: number | null
  future_usdt_ask_1?: number | null
  future_usdt_ask_2?: number | null
  future_usdt_ask_3?: number | null
  future_usdt_ask_4?: number | null
  future_usdt_ask_5?: number | null
  spot_usdt_bid_1?: number | null
  spot_usdt_bid_2?: number | null
  spot_usdt_bid_3?: number | null
  spot_usdt_bid_4?: number | null
  spot_usdt_bid_5?: number | null
  spot_usdt_ask_1?: number | null
  spot_usdt_ask_2?: number | null
  spot_usdt_ask_3?: number | null
  spot_usdt_ask_4?: number | null
  spot_usdt_ask_5?: number | null
  // 汇总
  future_usdt_bid_total?: number | null
  future_usdt_ask_total?: number | null
  spot_usdt_bid_total?: number | null
  spot_usdt_ask_total?: number | null
  future_spread_bps?: number | null
  spot_spread_bps?: number | null
  future_top_bid_usdt?: number | null
  future_top_ask_usdt?: number | null
  spot_top_bid_usdt?: number | null
  spot_top_ask_usdt?: number | null
  // VWAP基差相关
  open_vwap_basis_bps?: number | null
  close_vwap_basis_bps?: number | null
  open_marginal_basis_bps?: number | null
  risk_relief_bps?: number | null
  /** 按标的VWAP基差阈值(bps)，后端按标的下发 */
  vwap_threshold_bps?: number | null
  /** Funding-adjusted 统一入场门槛(bps)，与后端开仓状态机一致 */
  entry_floor_bps?: number | null
  entry_p20_bps?: number | null
  entry_funding_24h_bps?: number | null
  entry_expected_funding_bps?: number | null
  entry_carry_floor_bps?: number | null
  entry_timing_floor_bps?: number | null
  entry_funding_discount_bps?: number | null
  entry_expected_edge_bps?: number | null
  /** 平仓基差阈值参考(bps)，后端按标的下发 */
  close_vwap_threshold_bps?: number | null
  /** 反向策略：short spot + long future */
  reverse_strategy?: string | null
  reverse_spot_open_vwap?: number | null
  reverse_future_open_vwap?: number | null
  reverse_basis_bps?: number | null
  reverse_spot_close_vwap?: number | null
  reverse_future_close_vwap?: number | null
  reverse_close_basis_bps?: number | null
  reverse_open_basis_p20?: number | null
  reverse_close_basis_p20?: number | null
  reverse_p20_edge_bps?: number | null
  reverse_open_coverage?: number | null
  reverse_spot_open_coverage?: number | null
  reverse_future_open_coverage?: number | null
  reverse_gross_funding_bps?: number | null
  reverse_expected_funding_bps?: number | null
  reverse_funding_capture_ratio?: number | null
  reverse_funding_pass?: boolean | null
  reverse_funding_carry_pass?: boolean | null
  reverse_funding_carry_next_min?: number | null
  reverse_funding_carry_basis_ceiling_bps?: number | null
  reverse_funding_carry_min_24h_bps?: number | null
  reverse_funding_carry_min_margin_edge_bps?: number | null
  reverse_funding_carry_basis_relax_bps?: number | null
  reverse_borrow_hourly_rate?: number | null
  reverse_borrow_24h_bps?: number | null
  reverse_borrow_limit?: number | null
  reverse_max_borrowable_amount?: number | null
  reverse_borrowable?: boolean | null
  reverse_borrow_data_missing?: boolean | null
  reverse_borrow_pass?: boolean | null
  reverse_borrow_capacity_usdt?: number | null
  reverse_depth_capacity_usdt?: number | null
  reverse_capacity_usdt?: number | null
  reverse_coverage_pass?: boolean | null
  reverse_fee_bps?: number | null
  reverse_margin_edge_bps?: number | null
  reverse_margin_edge_pass?: boolean | null
  reverse_open_data_missing?: boolean | null
  reverse_status?: string | null
  [key: string]: string | number | boolean | null | undefined
}

export type LevelSide = 'bid' | 'ask'
export type MarketPrefix = 'future' | 'spot'

export type FuturePriceField = `future_price_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
export type FutureVolumeField = `future_volume_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
export type SpotPriceField = `spot_price_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
export type SpotVolumeField = `spot_volume_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
