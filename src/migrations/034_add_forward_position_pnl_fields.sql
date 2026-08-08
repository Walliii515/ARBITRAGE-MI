-- 正向套利已平仓订单级收益字段。

SET @db_name = DATABASE();

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN realized_pnl DECIMAL(24,8) DEFAULT NULL COMMENT ''订单级价差已实现盈亏(USDT)'' AFTER funding_total_pnl',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'realized_pnl'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN realized_pnl_bps DECIMAL(12,4) DEFAULT NULL COMMENT ''订单级价差已实现盈亏(bps)'' AFTER realized_pnl',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'realized_pnl_bps'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN total_pnl DECIMAL(24,8) DEFAULT NULL COMMENT ''订单级总盈亏：价差+资金费-手续费(USDT)'' AFTER realized_pnl_bps',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'total_pnl'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN total_pnl_bps DECIMAL(12,4) DEFAULT NULL COMMENT ''订单级总盈亏(bps)'' AFTER total_pnl',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'total_pnl_bps'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN fee_cost DECIMAL(24,8) DEFAULT NULL COMMENT ''订单级手续费成本，负数展示(USDT)'' AFTER total_pnl_bps',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'fee_cost'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN fee_bps DECIMAL(12,4) DEFAULT NULL COMMENT ''订单级手续费成本(bps，负数)'' AFTER fee_cost',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'fee_bps'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
