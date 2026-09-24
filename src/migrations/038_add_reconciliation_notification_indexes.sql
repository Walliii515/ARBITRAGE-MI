-- Additive online indexes only; preserve all historical reconciliation rows.
-- Keep metadata-lock waits short on the live trading database.
SET @previous_lock_wait_timeout = @@SESSION.lock_wait_timeout;
SET SESSION lock_wait_timeout = 5;

SET @sql = (SELECT IF(COUNT(*) = 0,
 'ALTER TABLE mi_recon_snapshot ADD INDEX idx_recon_history (base_asset,exchange,dimension,snapshot_at,is_match), ALGORITHM=INPLACE, LOCK=NONE',
 'SELECT 1') FROM INFORMATION_SCHEMA.STATISTICS
 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mi_recon_snapshot' AND INDEX_NAME='idx_recon_history');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (SELECT IF(COUNT(*) = 0,
 'ALTER TABLE mi_recon_snapshot ADD INDEX idx_recon_mismatch_time (is_match,snapshot_at), ALGORITHM=INPLACE, LOCK=NONE',
 'SELECT 1') FROM INFORMATION_SCHEMA.STATISTICS
 WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='mi_recon_snapshot' AND INDEX_NAME='idx_recon_mismatch_time');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET SESSION lock_wait_timeout = @previous_lock_wait_timeout;
