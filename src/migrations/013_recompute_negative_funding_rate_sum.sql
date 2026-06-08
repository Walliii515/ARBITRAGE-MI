-- 将 mi_trade_position.funding_rate_sum_bps 统一为“累计负24h资金费率 bps”。
-- 只统计 funding_rate_24h < 0 的历史结算；正资金费不再抵消负资金费风险。

UPDATE mi_trade_position p
LEFT JOIN (
    SELECT
        position_id,
        COALESCE(SUM(
            CASE
                WHEN funding_rate_24h < 0 THEN ABS(funding_rate_24h) * 10000
                ELSE 0
            END
        ), 0) AS negative_24h_bps
    FROM mi_trade_funding_fee_history
    GROUP BY position_id
) h ON h.position_id = p.id
SET p.funding_rate_sum_bps = ROUND(COALESCE(h.negative_24h_bps, 0), 2);
