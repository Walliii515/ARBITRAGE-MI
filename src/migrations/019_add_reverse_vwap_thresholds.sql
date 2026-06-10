-- 反向套利 VWAP 基差阈值
-- 反向开仓: short spot + long future = spot bid + future ask，等价于现有 close_vwap_basis_bps 样本，越低越好。
-- 反向平仓: buy spot + sell future = spot ask + future bid，等价于现有 open_vwap_basis_bps 样本，越高越好。

SET @db_name = DATABASE();

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_vwap_basis_threshold ADD COLUMN reverse_open_basis_p20 DECIMAL(10,4) NULL COMMENT ''反向开仓P20：spot bid + future ask，升序20分位，越低越有利'' AFTER close_basis_p20',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_vwap_basis_threshold'
      AND COLUMN_NAME = 'reverse_open_basis_p20'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = (
    SELECT IF(
        COUNT(*) = 0,
        'ALTER TABLE mi_vwap_basis_threshold ADD COLUMN reverse_close_basis_p20 DECIMAL(10,4) NULL COMMENT ''反向平仓P20：spot ask + future bid，降序top20分界，越高越有利'' AFTER reverse_open_basis_p20',
        'SELECT 1'
    )
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = @db_name
      AND TABLE_NAME = 'mi_vwap_basis_threshold'
      AND COLUMN_NAME = 'reverse_close_basis_p20'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

UPDATE mi_vwap_basis_threshold
SET
    reverse_open_basis_p20 = COALESCE(reverse_open_basis_p20, close_basis_p20),
    reverse_close_basis_p20 = COALESCE(reverse_close_basis_p20, open_basis_p20)
WHERE reverse_open_basis_p20 IS NULL
   OR reverse_close_basis_p20 IS NULL;
