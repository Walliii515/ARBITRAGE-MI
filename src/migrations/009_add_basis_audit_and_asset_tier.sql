-- 开仓基差审计字段 + 标的策略分层
-- signal_basis_bps: 触发/进入开仓状态机时看到的基差
-- pre_gate_basis_bps: 下单前最终旁路重算的基差
-- actual_basis_bps: 成交后按真实成交价计算的基差

ALTER TABLE mi_trade_signal
    ADD COLUMN signal_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '触发时看到的开仓VWAP基差(bps)' AFTER entry_basis_bps;
ALTER TABLE mi_trade_signal
    ADD COLUMN pre_gate_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '下单前最终旁路重算的开仓VWAP基差(bps)' AFTER peak_basis_bps;
ALTER TABLE mi_trade_signal
    ADD COLUMN actual_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '成交后真实开仓基差(bps)' AFTER pre_gate_basis_bps;

ALTER TABLE mi_trade_order
    ADD COLUMN signal_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '触发时看到的开仓VWAP基差(bps)' AFTER open_vwap_basis_bps;
ALTER TABLE mi_trade_order
    ADD COLUMN pre_gate_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '下单前最终旁路重算的开仓VWAP基差(bps)' AFTER signal_basis_bps;
ALTER TABLE mi_trade_order
    ADD COLUMN actual_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '成交后真实开仓基差(bps)' AFTER pre_gate_basis_bps;
ALTER TABLE mi_trade_order
    MODIFY COLUMN reject_reason VARCHAR(1000) DEFAULT NULL;

ALTER TABLE mi_trade_position
    ADD COLUMN signal_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '触发时看到的开仓VWAP基差(bps)' AFTER open_spread_bps;
ALTER TABLE mi_trade_position
    ADD COLUMN pre_gate_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '下单前最终旁路重算的开仓VWAP基差(bps)' AFTER signal_basis_bps;
ALTER TABLE mi_trade_position
    ADD COLUMN actual_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '成交后真实开仓基差(bps)' AFTER pre_gate_basis_bps;
ALTER TABLE mi_trade_position
    MODIFY COLUMN open_reason VARCHAR(1000) DEFAULT NULL;

ALTER TABLE mi_base_asset
    ADD COLUMN strategy_tier ENUM('A','B','C') NOT NULL DEFAULT 'C' COMMENT '策略标的分层：A=高流动性主交易池，B=可交易观察池，C=低流动性/谨慎池' AFTER is_valid;
ALTER TABLE mi_base_asset
    ADD COLUMN tier_reason VARCHAR(255) DEFAULT NULL COMMENT '策略分层原因' AFTER strategy_tier;
ALTER TABLE mi_base_asset
    ADD COLUMN tier_updated_at DATETIME DEFAULT NULL COMMENT '策略分层更新时间' AFTER tier_reason;

UPDATE mi_base_asset ba
LEFT JOIN mi_binance_spot_info spot
    ON spot.base_asset = ba.base_asset
LEFT JOIN mi_gate_future_contracts fut
    ON fut.base_asset = ba.base_asset
SET
    ba.strategy_tier = CASE
        WHEN COALESCE(spot.quote_volume, 0) >= 10000000
         AND COALESCE(fut.volume_24h_settle, 0) >= 5000000 THEN 'A'
        WHEN COALESCE(spot.quote_volume, 0) >= 1000000
         AND COALESCE(fut.volume_24h_settle, 0) >= 500000 THEN 'B'
        ELSE 'C'
    END,
    ba.tier_reason = CONCAT(
        'spot24h=', ROUND(COALESCE(spot.quote_volume, 0), 0),
        ',future24h=', ROUND(COALESCE(fut.volume_24h_settle, 0), 0)
    ),
    ba.tier_updated_at = NOW();
