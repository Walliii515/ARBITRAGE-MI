-- 交易所侧仓位风险标记：用于记录 ADL / 交易所实际仓位与本地持仓不一致。

SET @db_name = DATABASE();

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN exchange_risk_status ENUM(''normal'',''desynced'',''resolved'') NOT NULL DEFAULT ''normal'' COMMENT ''交易所侧仓位风险状态'' AFTER margin_topup_last_at',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'exchange_risk_status'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN exchange_risk_type VARCHAR(40) NULL COMMENT ''交易所侧风险类型：adl/missing_gate_position/qty_mismatch'' AFTER exchange_risk_status',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'exchange_risk_type'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN exchange_risk_at DATETIME NULL COMMENT ''交易所侧风险发生/识别时间'' AFTER exchange_risk_type',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'exchange_risk_at'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN exchange_risk_detail VARCHAR(1000) NULL COMMENT ''交易所侧风险详情'' AFTER exchange_risk_at',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'exchange_risk_detail'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX idx_trade_position_exchange_risk ON mi_trade_position(exchange_risk_status, exchange_risk_type, exchange_risk_at)',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND INDEX_NAME = 'idx_trade_position_exchange_risk'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
