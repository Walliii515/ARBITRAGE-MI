-- 订单级杠杆记录
-- spot 现货腿固定为 1x；future 合约腿记录下单时 Gate 逐仓杠杆。

ALTER TABLE mi_trade_order
    ADD COLUMN leverage DECIMAL(10,2) DEFAULT NULL COMMENT '订单腿杠杆倍数：spot=1，future=下单时Gate逐仓杠杆' AFTER trade_direction;

UPDATE mi_trade_order
SET leverage = 1
WHERE market_type = 'spot'
  AND leverage IS NULL;

UPDATE mi_trade_order
SET leverage = CASE
    WHEN created_at >= '2026-06-09 09:45:30' THEN 5
    ELSE 2
END
WHERE market_type = 'future'
  AND leverage IS NULL;
