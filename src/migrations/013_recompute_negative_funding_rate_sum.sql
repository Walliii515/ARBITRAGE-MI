-- 将 mi_trade_position.funding_rate_sum_bps 统一为“历史实际已支付负资金费累计 bps”。
-- 只统计 funding_rate < 0 的单期结算；正资金费不再抵消负资金费风险。

UPDATE mi_trade_position p
LEFT JOIN (
    SELECT
        position_id,
        COALESCE(SUM(
            CASE
                WHEN funding_rate < 0 THEN ABS(funding_rate) * 10000
                ELSE 0
            END
        ), 0) AS negative_paid_bps
    FROM mi_trade_funding_fee_history
    GROUP BY position_id
) h ON h.position_id = p.id
SET p.funding_rate_sum_bps = ROUND(COALESCE(h.negative_paid_bps, 0), 2);
