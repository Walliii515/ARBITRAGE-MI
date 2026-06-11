-- 反向套利真实交易边界表。
-- 独立于正向 mi_trade_position / mi_trade_order，避免 short spot + long future
-- 被正向买现货 + 空合约的语义误读。

CREATE TABLE IF NOT EXISTS mi_reverse_trade_position (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_uuid VARCHAR(64) NOT NULL COMMENT '反向开仓订单组 UUID',
    signal_id BIGINT DEFAULT NULL COMMENT '关联 mi_reverse_trade_signal.id',
    base_asset VARCHAR(32) NOT NULL,
    spot_symbol VARCHAR(64) NOT NULL,
    future_contract VARCHAR(64) NOT NULL,
    status ENUM('holding','closing','closed','risk','desynced') NOT NULL DEFAULT 'holding',

    opened_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME DEFAULT NULL,
    close_reason TEXT DEFAULT NULL,

    open_amount_usdt DECIMAL(24,8) DEFAULT NULL,
    close_amount_usdt DECIMAL(24,8) DEFAULT NULL,

    borrow_asset VARCHAR(32) DEFAULT NULL,
    borrow_qty DECIMAL(30,12) DEFAULT NULL,
    borrow_repaid_qty DECIMAL(30,12) DEFAULT NULL,
    borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
    borrow_interest_usdt DECIMAL(24,8) NOT NULL DEFAULT 0,
    borrow_interest_bps DECIMAL(12,4) NOT NULL DEFAULT 0,

    spot_open_qty DECIMAL(30,12) DEFAULT NULL COMMENT 'Binance margin sell 数量',
    spot_open_price DECIMAL(24,12) DEFAULT NULL,
    spot_open_amount DECIMAL(24,8) DEFAULT NULL,
    spot_close_qty DECIMAL(30,12) DEFAULT NULL COMMENT 'Binance margin buy 回补数量',
    spot_close_price DECIMAL(24,12) DEFAULT NULL,
    spot_close_amount DECIMAL(24,8) DEFAULT NULL,

    future_open_qty DECIMAL(30,12) DEFAULT NULL COMMENT 'Gate futures long 张数/数量',
    future_open_price DECIMAL(24,12) DEFAULT NULL,
    future_open_amount DECIMAL(24,8) DEFAULT NULL,
    future_close_qty DECIMAL(30,12) DEFAULT NULL,
    future_close_price DECIMAL(24,12) DEFAULT NULL,
    future_close_amount DECIMAL(24,8) DEFAULT NULL,

    reverse_open_basis_bps DECIMAL(12,4) DEFAULT NULL,
    reverse_close_basis_bps DECIMAL(12,4) DEFAULT NULL,
    signal_basis_bps DECIMAL(12,4) DEFAULT NULL,
    pre_gate_basis_bps DECIMAL(12,4) DEFAULT NULL,
    actual_basis_bps DECIMAL(12,4) DEFAULT NULL,
    execution_drift_bps DECIMAL(12,4) DEFAULT NULL,

    funding_pnl_usdt DECIMAL(24,8) NOT NULL DEFAULT 0,
    funding_pnl_bps DECIMAL(12,4) NOT NULL DEFAULT 0,
    fee_total_usdt DECIMAL(24,8) NOT NULL DEFAULT 0,
    fee_total_bps DECIMAL(12,4) NOT NULL DEFAULT 0,
    realized_pnl_usdt DECIMAL(24,8) DEFAULT NULL,
    realized_pnl_bps DECIMAL(12,4) DEFAULT NULL,

    exchange_risk_status ENUM('normal','desynced','resolved') NOT NULL DEFAULT 'normal',
    exchange_risk_type VARCHAR(64) DEFAULT NULL,
    exchange_risk_at DATETIME DEFAULT NULL,
    exchange_risk_detail TEXT DEFAULT NULL,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE KEY uk_reverse_position_order_uuid (order_uuid),
    INDEX idx_reverse_position_status_time (status, opened_at),
    INDEX idx_reverse_position_asset_status (base_asset, status),
    INDEX idx_reverse_position_signal (signal_id),
    INDEX idx_reverse_position_risk (exchange_risk_status, exchange_risk_type, exchange_risk_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='反向套利持仓';

CREATE TABLE IF NOT EXISTS mi_reverse_trade_order (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_uuid VARCHAR(64) NOT NULL,
    position_id BIGINT DEFAULT NULL,
    signal_id BIGINT DEFAULT NULL,
    base_asset VARCHAR(32) NOT NULL,
    spot_symbol VARCHAR(64) DEFAULT NULL,
    future_contract VARCHAR(64) DEFAULT NULL,
    order_side ENUM('open','close','repay','unwind') NOT NULL,
    market_type ENUM('margin_spot','future','margin_repay') NOT NULL,
    trade_direction ENUM('buy','sell','borrow','repay') NOT NULL,
    status ENUM('pending','filled','partial','failed','cancelled','skipped') NOT NULL DEFAULT 'pending',

    target_qty DECIMAL(30,12) DEFAULT NULL,
    target_amount DECIMAL(24,8) DEFAULT NULL,
    exec_price DECIMAL(24,12) DEFAULT NULL,
    exec_qty DECIMAL(30,12) DEFAULT NULL,
    exec_amount DECIMAL(24,8) DEFAULT NULL,

    exchange_order_id VARCHAR(128) DEFAULT NULL,
    client_order_id VARCHAR(128) DEFAULT NULL,
    liquidity_role VARCHAR(16) DEFAULT NULL,
    fee_rate DECIMAL(18,10) DEFAULT NULL,
    fee_amount DECIMAL(30,12) DEFAULT NULL,
    fee_asset VARCHAR(32) DEFAULT NULL,
    fee_amount_usdt DECIMAL(24,8) DEFAULT NULL,

    reduce_only TINYINT(1) DEFAULT NULL,
    protective_price DECIMAL(24,12) DEFAULT NULL,
    execution_style VARCHAR(32) DEFAULT NULL,
    reject_reason TEXT DEFAULT NULL,
    raw_response JSON DEFAULT NULL,

    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_reverse_order_uuid (order_uuid),
    INDEX idx_reverse_order_position (position_id),
    INDEX idx_reverse_order_signal (signal_id),
    INDEX idx_reverse_order_asset_time (base_asset, created_at),
    INDEX idx_reverse_order_status_time (status, created_at),
    CONSTRAINT fk_reverse_order_position
        FOREIGN KEY (position_id) REFERENCES mi_reverse_trade_position(id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='反向套利订单';
