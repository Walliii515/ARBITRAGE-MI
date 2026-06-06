-- 交易所真实资金快照表
-- 按分钟级频率记录 Binance/Gate 以及合计资金趋势。
CREATE TABLE IF NOT EXISTS mi_capital_snapshot (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    snapshot_at DATETIME NOT NULL COMMENT '快照时间',
    exchange VARCHAR(16) NOT NULL COMMENT 'binance/gate/total',
    equity_usdt DECIMAL(30,10) NULL COMMENT '账户权益/净值(USDT)',
    available_usdt DECIMAL(30,10) NULL COMMENT '可用资金(USDT)',
    locked_usdt DECIMAL(30,10) NULL COMMENT '锁定/委托占用(USDT)',
    position_value_usdt DECIMAL(30,10) NULL COMMENT '现货持仓市值或期货持仓保证金/价值(USDT)',
    margin_used_usdt DECIMAL(30,10) NULL COMMENT '保证金占用(USDT)',
    unrealized_pnl_usdt DECIMAL(30,10) NULL COMMENT '交易所未实现盈亏(USDT)',
    realized_pnl_usdt DECIMAL(30,10) NULL COMMENT '本地已实现盈亏(USDT)',
    funding_pnl_usdt DECIMAL(30,10) NULL COMMENT '本地资金费累计(USDT)',
    fee_cost_usdt DECIMAL(30,10) NULL COMMENT '本地手续费成本(USDT)',
    total_pnl_usdt DECIMAL(30,10) NULL COMMENT '本地综合盈亏(USDT)',
    detail JSON NULL COMMENT '原始账户字段/估值详情',
    INDEX idx_snapshot_at (snapshot_at),
    INDEX idx_exchange_snapshot (exchange, snapshot_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='交易所资金快照';
