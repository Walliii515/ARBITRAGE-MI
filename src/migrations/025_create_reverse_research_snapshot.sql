-- 反向套利研究快照：只记录借币侧观察数据。
-- Funding 和 VWAP/基差使用既有历史表，查询时关联，不在这里重复采样。
-- 只用于分析和复盘，不参与正向或反向交易执行判断。

CREATE TABLE IF NOT EXISTS mi_reverse_research_snapshot (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    snapshot_time DATETIME NOT NULL,
    base_asset VARCHAR(32) NOT NULL,
    contract VARCHAR(64) DEFAULT NULL,
    symbol VARCHAR(64) DEFAULT NULL,
    sample_source VARCHAR(32) NOT NULL DEFAULT 'loop',
    borrowable TINYINT DEFAULT NULL,
    max_borrowable_amount DECIMAL(28,12) DEFAULT NULL,
    account_borrow_limit DECIMAL(28,12) DEFAULT NULL,
    borrow_capacity_usdt DECIMAL(20,4) DEFAULT NULL,
    borrow_hourly_rate DECIMAL(18,10) DEFAULT NULL,
    borrow_24h_bps DECIMAL(12,4) DEFAULT NULL,
    borrow_unavailable_reason VARCHAR(128) DEFAULT NULL,
    reverse_status VARCHAR(64) DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_snapshot_time (snapshot_time),
    KEY idx_asset_time (base_asset, snapshot_time),
    KEY idx_status_time (reverse_status, snapshot_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
