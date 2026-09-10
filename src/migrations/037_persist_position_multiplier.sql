-- Keep the contract unit with the position even after exchange delisting.
SET @sql = (SELECT IF(COUNT(*) = 0,
 'ALTER TABLE mi_trade_position ADD COLUMN future_quanto_multiplier DECIMAL(30,15) NULL AFTER future_open_contracts',
 'SELECT 1') FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mi_trade_position' AND COLUMN_NAME='future_quanto_multiplier');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Historical quantities are in base units; contracts are exchange units.
UPDATE mi_trade_position SET future_quanto_multiplier=future_open_qty/future_open_contracts
WHERE future_quanto_multiplier IS NULL AND future_open_qty>0 AND future_open_contracts>0;
UPDATE mi_trade_position p JOIN mi_gate_future_contracts c ON c.name=p.future_contract
SET p.future_quanto_multiplier=c.quanto_multiplier
WHERE p.future_quanto_multiplier IS NULL AND c.quanto_multiplier>0;
-- Fail migration if a historical row cannot be established, never default to 1.
ALTER TABLE mi_trade_position MODIFY future_quanto_multiplier DECIMAL(30,15) NOT NULL;
SET @sql = (SELECT IF(COUNT(*)=0,
 'ALTER TABLE mi_trade_position ADD CONSTRAINT chk_position_multiplier CHECK (future_quanto_multiplier>0)',
 'SELECT 1') FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mi_trade_position' AND CONSTRAINT_NAME='chk_position_multiplier');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
