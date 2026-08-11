-- Gate contract 24h range metrics and persisted holding-volatility alert episodes.

SET @db_name = DATABASE();

SET @sql = (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE mi_gate_future_contracts ADD COLUMN high_24h DECIMAL(28,12) NULL COMMENT ''Gate合约24h最高价'' AFTER volume_24h_settle',
        'SELECT 1')
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name AND TABLE_NAME = 'mi_gate_future_contracts' AND COLUMN_NAME = 'high_24h'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE mi_gate_future_contracts ADD COLUMN low_24h DECIMAL(28,12) NULL COMMENT ''Gate合约24h最低价'' AFTER high_24h',
        'SELECT 1')
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name AND TABLE_NAME = 'mi_gate_future_contracts' AND COLUMN_NAME = 'low_24h'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE mi_gate_future_contracts ADD COLUMN last_price DECIMAL(28,12) NULL COMMENT ''Gate合约最新价'' AFTER low_24h',
        'SELECT 1')
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name AND TABLE_NAME = 'mi_gate_future_contracts' AND COLUMN_NAME = 'last_price'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE mi_gate_future_contracts ADD COLUMN range_24h_pct DECIMAL(18,8) NULL COMMENT ''24h振幅百分比=(high/low-1)*100'' AFTER last_price',
        'SELECT 1')
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name AND TABLE_NAME = 'mi_gate_future_contracts' AND COLUMN_NAME = 'range_24h_pct'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE mi_gate_future_contracts ADD COLUMN range_position_24h DECIMAL(18,8) NULL COMMENT ''最新价在24h高低区间的位置'' AFTER range_24h_pct',
        'SELECT 1')
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name AND TABLE_NAME = 'mi_gate_future_contracts' AND COLUMN_NAME = 'range_position_24h'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS mi_holding_volatility_alert_state (
    base_asset VARCHAR(64) NOT NULL PRIMARY KEY,
    active TINYINT(1) NOT NULL DEFAULT 0,
    episode_id BIGINT NOT NULL DEFAULT 0,
    notification_sent_at DATETIME NULL,
    triggered_at DATETIME NULL,
    recovered_at DATETIME NULL,
    last_amplitude_pct DECIMAL(18,8) NULL,
    last_range_position DECIMAL(18,8) NULL,
    last_price DECIMAL(28,12) NULL,
    high_24h DECIMAL(28,12) NULL,
    low_24h DECIMAL(28,12) NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_holding_volatility_active (active, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
