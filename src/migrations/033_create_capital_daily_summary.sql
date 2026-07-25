CREATE TABLE IF NOT EXISTS mi_capital_daily_summary (
    summary_date DATE NOT NULL PRIMARY KEY,
    first_snapshot_at DATETIME NOT NULL,
    last_snapshot_at DATETIME NOT NULL,
    first_equity_usdt DECIMAL(30,10) NULL,
    last_equity_usdt DECIMAL(30,10) NULL,
    equity_sum_usdt DECIMAL(38,10) NOT NULL DEFAULT 0,
    sample_count INT UNSIGNED NOT NULL DEFAULT 0,
    first_gross_pnl_usdt DECIMAL(30,10) NULL,
    last_gross_pnl_usdt DECIMAL(30,10) NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_capital_daily_last_snapshot (last_snapshot_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='每日资金收益汇总，用于长期年化收益统计';
