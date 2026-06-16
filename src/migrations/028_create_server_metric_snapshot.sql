-- 服务器关键指标快照表
-- 默认由 orderbook_server 每小时采样一次，用于“服务器状态”页面展示最近 7 天趋势。
CREATE TABLE IF NOT EXISTS mi_server_metric_snapshot (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    snapshot_at DATETIME NOT NULL COMMENT '采样时间',
    hostname VARCHAR(128) NOT NULL COMMENT '主机名',
    cpu_usage_percent DECIMAL(8,4) NULL COMMENT 'CPU使用率百分比',
    load1 DECIMAL(10,4) NULL COMMENT '1分钟负载',
    load5 DECIMAL(10,4) NULL COMMENT '5分钟负载',
    load15 DECIMAL(10,4) NULL COMMENT '15分钟负载',
    cpu_count INT NULL COMMENT 'CPU核心数',
    memory_total_bytes BIGINT NULL COMMENT '内存总量',
    memory_used_bytes BIGINT NULL COMMENT '内存已用',
    memory_usage_percent DECIMAL(8,4) NULL COMMENT '内存使用率百分比',
    disk_path VARCHAR(255) NOT NULL DEFAULT '/' COMMENT '磁盘采样路径',
    disk_total_bytes BIGINT NULL COMMENT '硬盘总量',
    disk_used_bytes BIGINT NULL COMMENT '硬盘已用',
    disk_usage_percent DECIMAL(8,4) NULL COMMENT '硬盘使用率百分比',
    uptime_sec BIGINT NULL COMMENT '系统启动秒数',
    detail JSON NULL COMMENT '附加原始信息',
    INDEX idx_snapshot_at (snapshot_at),
    INDEX idx_hostname_snapshot (hostname, snapshot_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='服务器指标快照';
