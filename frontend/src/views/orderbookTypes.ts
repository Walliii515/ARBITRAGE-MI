export interface OrderBookRow {
  base_asset: string
  contract: string
  symbol: string
  future_update_id: number | null
  future_update_time: number | null
  spot_update_id: number | null
  spot_update_time: number | null
  spot_ready?: boolean
  /** 每次开仓金额（USDT），后端从配置文件推送 */
  open_amount_usdt?: number
  spot_qty?: number | null
  future_qty?: number | null
  funding_rate_24h?: number | null
  funding_next_apply?: string | null
  percentile_30?: number | null
  meta_update_time?: string | null
  volume_24h_settle?: number | null
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
  // VWAP基差相关
  open_vwap_basis_bps?: number | null
  close_vwap_basis_bps?: number | null
  open_marginal_basis_bps?: number | null
  risk_relief_bps?: number | null
  /** 按标的VWAP基差阈值(bps)，后端按标的下发 */
  vwap_threshold_bps?: number | null
  [key: string]: string | number | boolean | null | undefined
}

export type LevelSide = 'bid' | 'ask'
export type MarketPrefix = 'future' | 'spot'

export type FuturePriceField = `future_price_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
export type FutureVolumeField = `future_volume_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
export type SpotPriceField = `spot_price_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
export type SpotVolumeField = `spot_volume_${LevelSide}_${1 | 2 | 3 | 4 | 5}`
