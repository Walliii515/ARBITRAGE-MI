-- mi_vwap_basis_snapshot 表索引优化
-- 解决逐标的查询和时间范围清理的性能问题
-- 
-- 数据量估算：360标的 × 8640条/天 × 14天保留 ≈ 4300万行
-- 无索引时全表扫描导致 OOM / 锁表

-- 核心索引：支持按标的+时间范围查询（阈值计算逐标的读取）
CREATE INDEX IF NOT EXISTS idx_snapshot_asset_time
ON mi_vwap_basis_snapshot (base_asset, snapshot_time);

-- 辅助索引：支持按时间范围清理过期数据
CREATE INDEX IF NOT EXISTS idx_snapshot_time
ON mi_vwap_basis_snapshot (snapshot_time);
