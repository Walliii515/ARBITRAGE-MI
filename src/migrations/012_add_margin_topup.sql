-- 自动追加保证金：持仓追保状态 + 审计日志

SET @db_name = DATABASE();

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN margin_topup_count INT NOT NULL DEFAULT 0 COMMENT ''已追加保证金次数''',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'margin_topup_count'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN margin_topup_total DECIMAL(16,6) NOT NULL DEFAULT 0 COMMENT ''累计追加保证金(USDT)''',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'margin_topup_total'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_trade_position ADD COLUMN margin_topup_last_at DATETIME NULL COMMENT ''最近一次追加保证金时间''',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_trade_position'
      AND COLUMN_NAME = 'margin_topup_last_at'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS mi_margin_topup_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    base_asset VARCHAR(20) NOT NULL COMMENT '标的资产',
    position_id BIGINT NOT NULL COMMENT '持仓ID',
    contract VARCHAR(30) NOT NULL COMMENT 'Gate合约',
    topup_amount DECIMAL(16,6) NOT NULL COMMENT '追加金额(USDT)',
    target_margin_usdt DECIMAL(16,6) NULL COMMENT '目标保证金(USDT)',
    margin_before_usdt DECIMAL(16,6) NULL COMMENT '追加前本地保证金(USDT)',
    liq_distance_before DECIMAL(8,2) NULL COMMENT '追加前距爆仓距离(%)',
    liq_distance_after DECIMAL(8,2) NULL COMMENT '预计追加后距爆仓距离(%)',
    gate_available_before DECIMAL(16,4) NULL COMMENT '追加前Gate可用余额',
    hedge_balanced TINYINT(1) NOT NULL DEFAULT 1 COMMENT '追加前现货/期货是否平衡',
    success TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否成功',
    error_msg VARCHAR(200) NULL COMMENT '失败原因',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_position (position_id),
    INDEX idx_asset_time (base_asset, created_at),
    INDEX idx_success_time (success, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='自动追加保证金日志';
