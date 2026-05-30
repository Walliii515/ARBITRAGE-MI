-- 为 mi_gate_future_contracts 表添加维持保证金率字段
-- 数据来源: Gate API /futures/usdt/contracts 返回的 maintenance_rate 字段
-- 用途: 逐仓模式下计算爆仓价和距爆仓距离

ALTER TABLE mi_gate_future_contracts
ADD COLUMN maintenance_rate DECIMAL(10, 6) NULL
    COMMENT '维持保证金率(Gate API maintenance_rate字段)'
    AFTER taker_fee_rate;
