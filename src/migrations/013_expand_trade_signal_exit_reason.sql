-- 成功开仓的信号会把完整开仓原因写入 exit_reason，避免复盘信息被截断。
ALTER TABLE mi_trade_signal
    MODIFY COLUMN exit_reason TEXT DEFAULT NULL COMMENT '结束原因(条件消失原因/拒单原因/开仓原因)';

UPDATE mi_trade_signal s
JOIN (
    SELECT order_uuid, MAX(reject_reason) AS open_reason
    FROM mi_trade_order
    WHERE order_side = 'open'
      AND status = 'executed'
      AND reject_reason IS NOT NULL
    GROUP BY order_uuid
) o ON o.order_uuid = s.order_uuid
SET s.exit_reason = o.open_reason
WHERE s.status = 'opened'
  AND (s.exit_reason IS NULL OR s.exit_reason = '');
