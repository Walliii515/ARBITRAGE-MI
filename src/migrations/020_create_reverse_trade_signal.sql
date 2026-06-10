-- 反向套利交易信号表
-- 只记录已经通过反向开仓前置条件、进入监控中的机会。
CREATE TABLE IF NOT EXISTS mi_reverse_trade_signal (
    id INT AUTO_INCREMENT PRIMARY KEY,
    base_asset VARCHAR(20) NOT NULL,
    contract VARCHAR(50) DEFAULT NULL,
    symbol VARCHAR(50) DEFAULT NULL,
    status ENUM(
        'monitoring',
        'opened',
        'conditions_lost',
        'rejected',
        'gate_rejected',
        'monitor_timeout'
    ) NOT NULL DEFAULT 'monitoring',
    signal_time DATETIME NOT NULL COMMENT '进入反向开仓监控的时间',
    resolved_time DATETIME DEFAULT NULL COMMENT '信号结束时间',
    duration_sec INT DEFAULT NULL COMMENT '监控持续时长(秒)',
    trigger_type VARCHAR(32) DEFAULT NULL COMMENT '触发方式: valley_rebound/manual',
    reject_reason TEXT DEFAULT NULL COMMENT '结束/拒绝/观察原因',
    order_uuid VARCHAR(64) DEFAULT NULL COMMENT '反向开仓订单UUID(预留)',

    funding_rate_24h DECIMAL(18,10) DEFAULT NULL COMMENT '入表时24h资金费率',
    reverse_open_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '入表时反向开仓基差',
    signal_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '信号基差快照',
    valley_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '监控期间最低反向开仓基差',
    rebound_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '触发反弹时基差',
    pre_gate_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '旁路风控重算基差',
    actual_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '实际成交基差(预留)',
    reverse_open_basis_p20 DECIMAL(10,2) DEFAULT NULL,
    reverse_close_basis_p20 DECIMAL(10,2) DEFAULT NULL,
    margin_edge_bps DECIMAL(10,2) DEFAULT NULL COMMENT '入表时边际盈亏',

    borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
    borrow_24h_bps DECIMAL(10,2) DEFAULT NULL,
    borrow_limit DECIMAL(24,8) DEFAULT NULL,
    borrow_capacity_usdt DECIMAL(18,2) DEFAULT NULL,
    open_coverage DECIMAL(10,4) DEFAULT NULL,
    capacity_usdt DECIMAL(18,2) DEFAULT NULL,
    open_amount_usdt DECIMAL(18,2) DEFAULT NULL,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_reverse_signal_time (signal_time),
    INDEX idx_reverse_status_time (status, signal_time),
    INDEX idx_reverse_asset_status (base_asset, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='反向套利交易信号';
