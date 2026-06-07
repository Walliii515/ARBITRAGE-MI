-- 订单级执行角色与手续费字段
-- liquidity_role: 实际成交角色；future maker 未成交后 fallback IOC 记为 taker
-- fee_rate: 当前账户口径费率，不使用合约公开 maker_fee_rate/taker_fee_rate
-- fee_amount/fee_asset: 交易所返回的手续费金额；历史数据按 exec_amount * fee_rate 回填

ALTER TABLE mi_trade_order
    ADD COLUMN liquidity_role ENUM('maker','taker','unknown') DEFAULT NULL COMMENT '实际成交角色 maker/taker/unknown' AFTER funding_rate_24h;
ALTER TABLE mi_trade_order
    ADD COLUMN fee_rate DECIMAL(18,10) DEFAULT NULL COMMENT '账户实际/估算手续费率' AFTER liquidity_role;
ALTER TABLE mi_trade_order
    ADD COLUMN fee_amount DECIMAL(20,8) DEFAULT NULL COMMENT '交易所返回或估算的手续费金额' AFTER fee_rate;
ALTER TABLE mi_trade_order
    ADD COLUMN fee_asset VARCHAR(20) DEFAULT NULL COMMENT '手续费资产' AFTER fee_amount;
ALTER TABLE mi_trade_order
    ADD COLUMN exchange_order_id VARCHAR(80) DEFAULT NULL COMMENT '交易所订单ID' AFTER fee_asset;

-- 历史订单兼容回填：运行时不再解析 reject_reason，此处仅为旧数据补齐结构化字段。
UPDATE mi_trade_order
SET
    liquidity_role = CASE
        WHEN market_type = 'spot' THEN 'taker'
        WHEN market_type = 'future' AND reject_reason LIKE '%fallback_filled=Y%' THEN 'taker'
        WHEN market_type = 'future' AND reject_reason LIKE '%future_maker=Y,filled=Y%' THEN 'maker'
        WHEN market_type = 'future' THEN 'taker'
        ELSE 'unknown'
    END
WHERE liquidity_role IS NULL
  AND status = 'executed';

UPDATE mi_trade_order
SET fee_rate = CASE
        WHEN market_type = 'spot' AND order_side = 'open' THEN 0.00075
        WHEN market_type = 'spot' AND order_side = 'close' THEN 0.00075
        WHEN market_type = 'future' AND liquidity_role = 'maker' THEN 0.00020
        WHEN market_type = 'future' AND liquidity_role = 'taker' THEN 0.00050
        ELSE fee_rate
    END
WHERE fee_rate IS NULL
  AND status = 'executed';

UPDATE mi_trade_order
SET
    fee_amount = ROUND(ABS(exec_amount) * fee_rate, 8),
    fee_asset = 'USDT'
WHERE market_type = 'future'
  AND status = 'executed'
  AND fee_rate IS NOT NULL
  AND exec_amount IS NOT NULL
  AND fee_amount IS NULL;
