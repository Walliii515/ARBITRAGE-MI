-- 将反向研究快照瘦身为借币侧时间序列。
-- Funding 和 VWAP/基差已有独立采样表，后续查询时关联，不在本表重复沉淀。

DELIMITER //

DROP PROCEDURE IF EXISTS add_reverse_research_column_if_missing//
CREATE PROCEDURE add_reverse_research_column_if_missing()
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'mi_reverse_research_snapshot'
          AND COLUMN_NAME = 'account_borrow_limit'
    ) THEN
        ALTER TABLE mi_reverse_research_snapshot
            ADD COLUMN account_borrow_limit DECIMAL(28,12) DEFAULT NULL
            AFTER max_borrowable_amount;
    END IF;
END//

DROP PROCEDURE IF EXISTS drop_reverse_research_column_if_exists//
CREATE PROCEDURE drop_reverse_research_column_if_exists(IN p_column_name VARCHAR(64))
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'mi_reverse_research_snapshot'
          AND COLUMN_NAME = p_column_name
    ) THEN
        SET @drop_sql = CONCAT('ALTER TABLE mi_reverse_research_snapshot DROP COLUMN ', p_column_name);
        PREPARE stmt FROM @drop_sql;
        EXECUTE stmt;
        DEALLOCATE PREPARE stmt;
    END IF;
END//

DELIMITER ;

CALL add_reverse_research_column_if_missing();

CALL drop_reverse_research_column_if_exists('funding_rate_24h');
CALL drop_reverse_research_column_if_exists('gross_funding_bps');
CALL drop_reverse_research_column_if_exists('expected_funding_bps');
CALL drop_reverse_research_column_if_exists('next_funding_time');
CALL drop_reverse_research_column_if_exists('next_funding_min');
CALL drop_reverse_research_column_if_exists('reverse_open_basis_bps');
CALL drop_reverse_research_column_if_exists('reverse_close_basis_bps');
CALL drop_reverse_research_column_if_exists('reverse_margin_edge_bps');
CALL drop_reverse_research_column_if_exists('reverse_open_coverage');
CALL drop_reverse_research_column_if_exists('spot_spread_bps');
CALL drop_reverse_research_column_if_exists('future_spread_bps');
CALL drop_reverse_research_column_if_exists('spot_top_bid_usdt');
CALL drop_reverse_research_column_if_exists('future_top_ask_usdt');
CALL drop_reverse_research_column_if_exists('spot_quote_volume_24h');
CALL drop_reverse_research_column_if_exists('future_volume_24h_settle');

DROP PROCEDURE IF EXISTS add_reverse_research_column_if_missing;
DROP PROCEDURE IF EXISTS drop_reverse_research_column_if_exists;
