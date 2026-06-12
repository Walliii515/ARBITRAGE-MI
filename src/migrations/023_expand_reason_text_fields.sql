-- 统一把复盘/结束/拒绝/风险/错误原因字段改为 TEXT，避免成交或风控动作
-- 已完成但状态更新被 VARCHAR 长度限制卡住。
ALTER TABLE mi_trade_order
    MODIFY COLUMN reject_reason TEXT NULL COMMENT '订单复盘原因/拒单原因/执行审计';

ALTER TABLE mi_trade_position
    MODIFY COLUMN open_reason TEXT NULL COMMENT '开仓原因/执行审计',
    MODIFY COLUMN exchange_risk_detail TEXT NULL COMMENT '交易所侧风险详情';

ALTER TABLE mi_reverse_trade_signal
    MODIFY COLUMN trigger_reason TEXT NULL,
    MODIFY COLUMN reject_reason TEXT NULL COMMENT '结束/拒绝/观察原因';

ALTER TABLE mi_margin_topup_log
    MODIFY COLUMN error_msg TEXT NULL COMMENT '失败原因';

ALTER TABLE mi_base_asset
    MODIFY COLUMN tier_reason TEXT NULL COMMENT '策略分层原因';
