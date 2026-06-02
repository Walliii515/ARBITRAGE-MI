-- 资金费结算历史表
-- 记录每次资金费结算的详细信息，支持前端展示"第几次、费率、金额"

CREATE TABLE IF NOT EXISTS mi_trade_funding_fee_history (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    position_id BIGINT NOT NULL COMMENT '关联持仓ID (mi_trade_position.id)',
    base_asset VARCHAR(64) NOT NULL COMMENT '标的资产',
    payment_seq INT NOT NULL COMMENT '第几次结算(从1开始)',
    funding_rate DECIMAL(18, 10) NOT NULL COMMENT '本次单期资金费率(8h费率, 即24h/3)',
    funding_rate_24h DECIMAL(18, 10) NULL COMMENT '当时的24h资金费率',
    funding_pnl DECIMAL(18, 6) NOT NULL COMMENT '本次资金费收益(USDT)',
    future_notional DECIMAL(18, 6) NULL COMMENT '期货名义价值(qty * price)',
    settled_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '结算时间',
    INDEX idx_position_id (position_id),
    INDEX idx_base_asset (base_asset)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='资金费结算历史表';
