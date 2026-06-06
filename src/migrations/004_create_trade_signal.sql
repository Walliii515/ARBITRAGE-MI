-- 交易信号日志表
-- 记录每个开仓机会从"进入峰值监控"到最终结局的完整生命周期
CREATE TABLE IF NOT EXISTS mi_trade_signal (
    id INT AUTO_INCREMENT PRIMARY KEY,
    base_asset VARCHAR(20) NOT NULL,
    signal_time DATETIME NOT NULL COMMENT '信号首次检测时间(进入峰值监控)',
    resolved_time DATETIME DEFAULT NULL COMMENT '信号结束时间',
    status ENUM('monitoring', 'opened', 'conditions_lost', 'rejected') NOT NULL DEFAULT 'monitoring',
    entry_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '进入监控时基差(bps)',
    peak_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '监控期间峰值基差(bps)',
    exit_basis_bps DECIMAL(10,2) DEFAULT NULL COMMENT '结束时基差(bps)',
    exit_reason TEXT DEFAULT NULL COMMENT '结束原因(条件消失原因/拒单原因/开仓原因)',
    duration_sec INT DEFAULT NULL COMMENT '监控持续时长(秒)',
    trigger_type VARCHAR(20) DEFAULT NULL COMMENT '触发方式: pullback/timeout (仅opened/rejected)',
    order_uuid VARCHAR(50) DEFAULT NULL COMMENT '关联订单UUID(仅opened)',
    INDEX idx_base_asset (base_asset),
    INDEX idx_signal_time (signal_time),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易信号日志';
