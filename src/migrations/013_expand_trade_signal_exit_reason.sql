-- 成功开仓的信号会把完整开仓原因写入 exit_reason，避免复盘信息被截断。
ALTER TABLE mi_trade_signal
    MODIFY COLUMN exit_reason TEXT DEFAULT NULL COMMENT '结束原因(条件消失原因/拒单原因/开仓原因)';
