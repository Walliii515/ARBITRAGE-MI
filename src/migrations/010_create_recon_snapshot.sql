-- 基础对账快照表
-- 记录 Binance 现货 / Gate 期货真实持仓与本地 mi_trade_position 聚合值的差异。
CREATE TABLE IF NOT EXISTS mi_recon_snapshot (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    snapshot_at DATETIME NOT NULL COMMENT '本轮对账时间',
    exchange VARCHAR(16) NOT NULL COMMENT 'binance/gate',
    base_asset VARCHAR(32) NOT NULL COMMENT '标的资产',
    dimension VARCHAR(16) NOT NULL COMMENT 'position/balance/margin/upnl/error',
    local_value DECIMAL(30,10) NULL COMMENT '本地聚合数值',
    exchange_value DECIMAL(30,10) NULL COMMENT '交易所返回数值',
    diff_value DECIMAL(30,10) NULL COMMENT 'exchange_value - local_value',
    diff_ratio DECIMAL(20,10) NULL COMMENT '差异占比',
    is_match TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否在容差范围内一致',
    detail JSON NULL COMMENT '原始字段或错误摘要',
    INDEX idx_snapshot_at (snapshot_at),
    INDEX idx_asset (base_asset, exchange)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='交易所持仓对账快照';
