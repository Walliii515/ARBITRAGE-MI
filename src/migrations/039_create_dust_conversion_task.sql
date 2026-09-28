-- Persist the intent before calling Binance; an unknown request is never retried.
CREATE TABLE IF NOT EXISTS mi_dust_conversion_task (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    batch_uuid VARCHAR(36) NOT NULL,
    base_asset VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    requested_at DATETIME(3) NOT NULL,
    positions_json JSON NOT NULL,
    expected_qty DECIMAL(28,8) NOT NULL,
    conversion_json JSON DEFAULT NULL,
    transaction_id VARCHAR(80) DEFAULT NULL,
    event_at DATETIME(3) DEFAULT NULL,
    accounted_at DATETIME(3) DEFAULT NULL,
    accounting_json JSON DEFAULT NULL,
    net_delta_usdt DECIMAL(28,8) DEFAULT NULL,
    last_error TEXT,
    last_checked_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_batch_asset (batch_uuid, base_asset),
    UNIQUE KEY uk_receipt_asset (transaction_id, base_asset),
    KEY idx_status_asset (status, base_asset),
    KEY idx_event_at (event_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE mi_trade_order
    MODIFY target_amount DECIMAL(28,8) NOT NULL,
    MODIFY exec_amount DECIMAL(28,8) DEFAULT NULL;
ALTER TABLE mi_trade_position
    MODIFY spot_open_amount DECIMAL(28,8) NOT NULL,
    MODIFY spot_close_amount DECIMAL(28,8) DEFAULT NULL,
    MODIFY future_close_amount DECIMAL(28,8) DEFAULT NULL,
    MODIFY realized_pnl_spot DECIMAL(28,8) DEFAULT NULL,
    MODIFY realized_pnl_future DECIMAL(28,8) DEFAULT NULL,
    MODIFY realized_pnl_total DECIMAL(28,8) DEFAULT NULL,
    MODIFY total_pnl DECIMAL(28,8) DEFAULT NULL;
