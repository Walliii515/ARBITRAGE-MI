-- AG Grid 列配置表
-- 用于统一管理所有页面的列显示、隐藏、顺序、宽度等配置

CREATE TABLE IF NOT EXISTS ag_grid_column_config (
  id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
  user_id VARCHAR(64) NOT NULL COMMENT '用户ID（支持多用户个性化配置）',
  page_key VARCHAR(64) NOT NULL COMMENT '页面标识，如 orderbook_monitor, position_monitor, order_management',
  col_id VARCHAR(128) NOT NULL COMMENT '列ID（对应AG Grid column.field 或 column.colId）',
  sort_order INT NOT NULL DEFAULT 0 COMMENT '显示顺序（升序排列）',
  is_visible TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否显示：1=显示，0=隐藏',
  width INT NULL COMMENT '列宽（px）',
  pinned VARCHAR(16) NULL COMMENT '固定位置：left / right / null',
  sort VARCHAR(16) NULL COMMENT '排序状态：asc / desc / null',
  filter_model JSON NULL COMMENT '筛选条件（AG Grid FilterModel 序列化）',
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
  UNIQUE KEY uk_user_page_col (user_id, page_key, col_id),
  INDEX idx_user_page (user_id, page_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AG Grid列配置表';

-- 可选：初始化默认列配置（OrderBookMonitor）
-- 取消下面的注释以初始化默认配置
/*
INSERT INTO ag_grid_column_config (user_id, page_key, col_id, sort_order, is_visible, width, pinned)
VALUES 
  ('default', 'orderbook_monitor', 'base_asset', 0, 1, 90, 'left'),
  ('default', 'orderbook_monitor', 'open_amount_usdt', 1, 1, 120, NULL),
  ('default', 'orderbook_monitor', 'spot_qty', 2, 1, 110, NULL),
  ('default', 'orderbook_monitor', 'future_qty', 3, 1, 110, NULL),
  ('default', 'orderbook_monitor', 'funding_rate_24h', 4, 1, 120, NULL),
  ('default', 'orderbook_monitor', 'threshold_pct', 5, 1, 120, NULL),
  ('default', 'orderbook_monitor', 'spread_bps', 6, 1, 120, NULL),
  ('default', 'orderbook_monitor', 'open_coverage', 7, 1, 120, NULL),
  ('default', 'orderbook_monitor', 'marginal_basis', 8, 1, 120, NULL),
  ('default', 'orderbook_monitor', 'gate_update_time', 9, 1, 160, NULL),
  ('default', 'orderbook_monitor', 'binance_update_time', 10, 1, 160, NULL)
ON DUPLICATE KEY UPDATE sort_order = VALUES(sort_order);
*/
