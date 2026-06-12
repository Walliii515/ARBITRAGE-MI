-- 标的行情画像：将策略分层(A/B/C)与行情质量画像分开管理。
-- strategy_tier 继续表示交易池质量；market_profile 表示盘口/更新特征。

SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'mi_base_asset'
      AND COLUMN_NAME = 'market_profile'
);
SET @sql := IF(
    @col_exists = 0,
    'ALTER TABLE mi_base_asset ADD COLUMN market_profile ENUM(''normal'',''thin_bursty'',''illiquid_blocked'') NOT NULL DEFAULT ''normal'' COMMENT ''行情画像：normal=连续盘口，thin_bursty=薄盘/跳动式更新，illiquid_blocked=仅观察不自动交易'' AFTER strategy_tier',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'mi_base_asset'
      AND COLUMN_NAME = 'market_profile_reason'
);
SET @sql := IF(
    @col_exists = 0,
    'ALTER TABLE mi_base_asset ADD COLUMN market_profile_reason TEXT NULL COMMENT ''行情画像原因/最近一次分类依据'' AFTER market_profile',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*)
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'mi_base_asset'
      AND COLUMN_NAME = 'market_profile_updated_at'
);
SET @sql := IF(
    @col_exists = 0,
    'ALTER TABLE mi_base_asset ADD COLUMN market_profile_updated_at DATETIME NULL COMMENT ''行情画像更新时间'' AFTER market_profile_reason',
    'SELECT 1'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE mi_base_asset ba
LEFT JOIN mi_binance_spot_info spot
    ON spot.base_asset = UPPER(TRIM(ba.base_asset))
LEFT JOIN mi_gate_future_contracts fut
    ON fut.base_asset = UPPER(TRIM(ba.base_asset))
SET
    ba.market_profile = CASE
        WHEN COALESCE(ba.strategy_tier, 'C') = 'C' THEN 'illiquid_blocked'
        WHEN COALESCE(spot.quote_volume, 0) < 1000000
          OR COALESCE(fut.volume_24h_settle, 0) < 500000 THEN 'thin_bursty'
        ELSE 'normal'
    END,
    ba.market_profile_reason = CONCAT(
        'bootstrap: tier=', COALESCE(ba.strategy_tier, 'C'),
        ',spot24h=', ROUND(COALESCE(spot.quote_volume, 0), 0),
        ',future24h=', ROUND(COALESCE(fut.volume_24h_settle, 0), 0)
    ),
    ba.market_profile_updated_at = NOW();
