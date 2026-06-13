-- 为反向持仓增加开仓时刻复盘字段。

DELIMITER //

DROP PROCEDURE IF EXISTS add_reverse_position_column_if_missing//
CREATE PROCEDURE add_reverse_position_column_if_missing(
    IN p_column_name VARCHAR(64),
    IN p_ddl TEXT
)
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA = DATABASE()
           AND TABLE_NAME = 'mi_reverse_trade_position'
           AND COLUMN_NAME = p_column_name
    ) THEN
        SET @ddl_sql = p_ddl;
        PREPARE stmt FROM @ddl_sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END//

CALL add_reverse_position_column_if_missing(
    'open_borrow_24h_bps',
    'ALTER TABLE mi_reverse_trade_position ADD COLUMN open_borrow_24h_bps DECIMAL(12,4) DEFAULT NULL COMMENT ''开仓时刻24h借币成本bps'' AFTER borrow_hourly_rate'
)//

CALL add_reverse_position_column_if_missing(
    'reverse_open_basis_p20',
    'ALTER TABLE mi_reverse_trade_position ADD COLUMN reverse_open_basis_p20 DECIMAL(12,4) DEFAULT NULL COMMENT ''开仓时刻反向开仓VWAP阈值'' AFTER reverse_close_basis_bps'
)//

CALL add_reverse_position_column_if_missing(
    'reverse_close_basis_p20',
    'ALTER TABLE mi_reverse_trade_position ADD COLUMN reverse_close_basis_p20 DECIMAL(12,4) DEFAULT NULL COMMENT ''开仓时刻反向平仓VWAP阈值'' AFTER reverse_open_basis_p20'
)//

CALL add_reverse_position_column_if_missing(
    'open_funding_rate_24h',
    'ALTER TABLE mi_reverse_trade_position ADD COLUMN open_funding_rate_24h DECIMAL(18,10) DEFAULT NULL COMMENT ''开仓时刻24h资金费率'' AFTER execution_drift_bps'
)//

DROP PROCEDURE IF EXISTS add_reverse_position_column_if_missing//

DELIMITER ;
