-- 扩展 mi_trade_signal.status 枚举
-- 1. 补 'gate_rejected'：最终风控旁路拦截（信号过期/盘口呆滞/盈利性守卫等动态条件失效）
-- 2. 新增 'monitor_timeout'：峰值监控超时但基差始终未回落到位 → 放弃本轮并进入冷却（取代原"超时直开"语义）
ALTER TABLE mi_trade_signal
    MODIFY COLUMN status ENUM(
        'monitoring',
        'opened',
        'conditions_lost',
        'rejected',
        'gate_rejected',
        'monitor_timeout'
    ) NOT NULL DEFAULT 'monitoring';

-- trigger_type 列注释同步更新（语义：pullback / monitor_timeout）
ALTER TABLE mi_trade_signal
    MODIFY COLUMN trigger_type VARCHAR(20) DEFAULT NULL COMMENT '触发方式: pullback (回落确认开仓) / monitor_timeout (监控超时未回落，放弃本轮)';
