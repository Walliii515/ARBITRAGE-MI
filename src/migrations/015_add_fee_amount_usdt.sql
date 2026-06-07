-- 订单手续费 USDT 折算金额
-- fee_amount 保留交易所原始手续费数量，fee_asset 保留原始币种；
-- fee_amount_usdt 专供持仓 PnL 统一按 USDT 成本汇总。

ALTER TABLE mi_trade_order
    ADD COLUMN fee_amount_usdt DECIMAL(20,8) DEFAULT NULL COMMENT '手续费折算USDT金额' AFTER fee_amount;

UPDATE mi_trade_order
SET fee_amount_usdt = fee_amount
WHERE fee_asset = 'USDT'
  AND fee_amount IS NOT NULL
  AND fee_amount_usdt IS NULL;

UPDATE mi_trade_order
SET fee_amount_usdt = ROUND(ABS(exec_amount) * fee_rate, 8)
WHERE market_type = 'future'
  AND status = 'executed'
  AND fee_rate IS NOT NULL
  AND exec_amount IS NOT NULL
  AND fee_amount_usdt IS NULL;
