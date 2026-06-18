-- 交易对上新事件表
-- 由 ETL 在更新 Gate 永续 / Binance 现货元数据后刷新，用于页面展示和固定时间弹窗提醒。
CREATE TABLE IF NOT EXISTS mi_listing_event (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    base_asset VARCHAR(64) NOT NULL COMMENT '标的资产',
    gate_contract VARCHAR(80) NULL COMMENT 'Gate 永续合约名',
    binance_symbol VARCHAR(80) NULL COMMENT 'Binance 现货交易对',
    candidate_status ENUM('matched','gate_only','binance_only') NOT NULL COMMENT '上新配对状态',
    action_status ENUM('pending','acknowledged','ignored','disabled','added_to_monitor') NOT NULL DEFAULT 'pending' COMMENT '处理状态',
    is_actionable TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否值得弹窗提醒',
    gate_status VARCHAR(40) NULL COMMENT 'Gate 合约状态',
    binance_status VARCHAR(40) NULL COMMENT 'Binance 现货状态',
    gate_volume_24h_settle DECIMAL(30,10) NULL COMMENT 'Gate 24h 成交额',
    binance_quote_volume DECIMAL(30,10) NULL COMMENT 'Binance 24h 报价成交额',
    gate_funding_rate_24h DECIMAL(18,10) NULL COMMENT 'Gate 24h 资金费率',
    first_seen_at DATETIME NOT NULL COMMENT '首次发现时间',
    last_seen_at DATETIME NOT NULL COMMENT '最近仍存在时间',
    acknowledged_at DATETIME NULL COMMENT '确认时间',
    action_at DATETIME NULL COMMENT '处理时间',
    action_reason VARCHAR(255) NULL COMMENT '处理原因',
    source_payload JSON NULL COMMENT '原始来源摘要',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_listing_event_asset (base_asset),
    INDEX idx_listing_action (action_status, is_actionable, last_seen_at),
    INDEX idx_listing_candidate (candidate_status, last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='交易对上新事件';
