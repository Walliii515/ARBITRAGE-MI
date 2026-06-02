-- 资金费结算历史表唯一索引
-- 背景：position_tracker.update_funding_pnl 历史上未事务化，导致同一
--      (position_id, payment_seq) 在服务并发/重启场景下被多次插入。
-- 加固：增加唯一索引，配合代码层 INSERT IGNORE 实现幂等写入。
--
-- 执行前提：
--   线上历史脏数据已通过下面脚本清洗（保留每对 (position_id, payment_seq)
--   中 id 最小的一条），并已用 history 反向回填 mi_trade_position 的
--   funding_payments_count / funding_total_pnl / funding_rate_sum_bps。
--
-- 注意：MySQL DROP INDEX 不支持 IF EXISTS，重复执行需先确认索引是否已存在。

ALTER TABLE mi_trade_funding_fee_history
    ADD UNIQUE KEY uk_position_seq (position_id, payment_seq);
