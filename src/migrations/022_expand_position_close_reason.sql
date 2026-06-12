-- 平仓原因会包含盘口、旁路、执行审计等信息，varchar(500) 会导致
-- 成交成功后 mi_trade_position 状态更新失败。完整审计仍以订单表为准。
ALTER TABLE mi_trade_position
    MODIFY COLUMN close_reason TEXT NULL COMMENT '平仓原因/交易所仓位风险摘要';
