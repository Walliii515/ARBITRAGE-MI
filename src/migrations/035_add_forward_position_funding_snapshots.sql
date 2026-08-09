-- 正向持仓开仓/平仓时刻的实时折算 24h funding 快照，并为订单管理分页补索引。

SET @db_name = DATABASE();

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN open_funding_rate_24h DECIMAL(18,10) DEFAULT NULL COMMENT ''开仓时刻实时折算24h资金费率'' AFTER open_spread_bps',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'open_funding_rate_24h'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN close_funding_rate_24h DECIMAL(18,10) DEFAULT NULL COMMENT ''平仓时刻实时折算24h资金费率'' AFTER close_spread_bps',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'close_funding_rate_24h'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE mi_trade_position p
SET p.open_funding_rate_24h = (
    SELECT o.funding_rate_24h
    FROM mi_trade_order o
    WHERE o.position_id = p.id
      AND o.order_side = 'open'
      AND o.status = 'executed'
      AND o.funding_rate_24h IS NOT NULL
    ORDER BY COALESCE(o.executed_at, o.created_at) ASC, o.id ASC
    LIMIT 1
)
WHERE p.open_funding_rate_24h IS NULL;

UPDATE mi_trade_position p
SET p.close_funding_rate_24h = (
    SELECT o.funding_rate_24h
    FROM mi_trade_order o
    WHERE o.position_id = p.id
      AND o.order_side = 'close'
      AND o.status = 'executed'
      AND o.funding_rate_24h IS NOT NULL
    ORDER BY COALESCE(o.executed_at, o.created_at) DESC, o.id DESC
    LIMIT 1
)
WHERE p.status = 'closed'
  AND p.close_funding_rate_24h IS NULL;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX idx_trade_position_status_opened ON mi_trade_position(status, opened_at, id)',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND INDEX_NAME = 'idx_trade_position_status_opened'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'CREATE INDEX idx_trade_position_status_closed ON mi_trade_position(status, closed_at, id)',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.STATISTICS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND INDEX_NAME = 'idx_trade_position_status_closed'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
