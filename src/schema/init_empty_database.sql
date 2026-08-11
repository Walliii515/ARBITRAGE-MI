
-- Arbitrage-Mi empty database initialization schema.
--
-- Generated from the current Alibaba Cloud production schema with data removed.
-- Backup/temporary tables are intentionally excluded.
--
-- Usage:
--   CREATE DATABASE IF NOT EXISTS crypto_arbitrage
--     DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--   mysql -u arb_app -p crypto_arbitrage < src/schema/init_empty_database.sql
--
-- This script contains DROP TABLE IF EXISTS statements for repeatable empty-db
-- initialization. Run it only against a new or disposable database.

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
DROP TABLE IF EXISTS `ag_grid_column_config`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `ag_grid_column_config` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '用户ID（支持多用户个性化配置）',
  `page_key` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '页面标识，如 orderbook_monitor, position_monitor, order_management',
  `col_id` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '列ID（对应AG Grid column.field 或 column.colId）',
  `sort_order` int NOT NULL DEFAULT '0' COMMENT '显示顺序（升序排列）',
  `is_visible` tinyint(1) NOT NULL DEFAULT '1' COMMENT '是否显示：1=显示，0=隐藏',
  `width` int DEFAULT NULL COMMENT '列宽（px）',
  `pinned` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '固定位置：left / right / null',
  `sort` varchar(16) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '排序状态：asc / desc / null',
  `filter_model` json DEFAULT NULL COMMENT '筛选条件（AG Grid FilterModel 序列化）',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_page_col` (`user_id`,`page_key`,`col_id`),
  KEY `idx_user_page` (`user_id`,`page_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AG Grid列配置表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_users`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_users` (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(50) NOT NULL,
  `password` varchar(100) NOT NULL,
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_base_asset`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_base_asset` (
  `base_asset` varchar(16) NOT NULL COMMENT '基础币种（如 BTC）',
  `spot_symbol` varchar(32) NOT NULL COMMENT '交易对名称（如 BTCUSDT）',
  `future_name` varchar(50) DEFAULT NULL COMMENT '合约名称',
  `is_valid` varchar(1) NOT NULL DEFAULT '',
  `strategy_tier` enum('A','B','C') NOT NULL DEFAULT 'C' COMMENT '策略标的分层：A=高流动性主交易池，B=可交易观察池，C=低流动性/谨慎池',
  `market_profile` enum('normal','thin_bursty','illiquid_blocked') NOT NULL DEFAULT 'normal' COMMENT '行情画像：normal=连续盘口，thin_bursty=薄盘/跳动式更新，illiquid_blocked=仅观察不自动交易',
  `market_profile_reason` text COMMENT '行情画像原因/最近一次分类依据',
  `market_profile_updated_at` datetime DEFAULT NULL COMMENT '行情画像更新时间',
  `tier_reason` text COMMENT '策略分层原因',
  `tier_updated_at` datetime DEFAULT NULL COMMENT '策略分层更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_binance_spot_info`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_binance_spot_info` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `symbol` varchar(32) NOT NULL COMMENT '交易对名称（如 BTCUSDT）',
  `base_asset` varchar(16) NOT NULL COMMENT '基础币种（如 BTC）',
  `quote_asset` varchar(16) NOT NULL COMMENT '计价币种（如 USDT）',
  `status` varchar(16) NOT NULL COMMENT '交易对状态（TRADING/HALT等）',
  `base_asset_precision` tinyint DEFAULT NULL COMMENT '基础币种精度',
  `quote_asset_precision` tinyint DEFAULT NULL COMMENT '计价币种精度',
  `base_commission_precision` tinyint DEFAULT NULL COMMENT '基础币种手续费精度',
  `quote_commission_precision` tinyint DEFAULT NULL COMMENT '计价币种手续费精度',
  `min_price` varchar(32) DEFAULT NULL COMMENT '最小价格',
  `max_price` varchar(32) DEFAULT NULL COMMENT '最大价格',
  `tick_size` varchar(32) DEFAULT NULL COMMENT '价格步长（最小价格变动）',
  `min_qty` varchar(32) DEFAULT NULL COMMENT '最小下单数量',
  `max_qty` varchar(32) DEFAULT NULL COMMENT '最大下单数量',
  `step_size` varchar(32) DEFAULT NULL COMMENT '数量步长（最小数量变动）',
  `min_notional` varchar(32) DEFAULT NULL COMMENT '最小名义价值（最小下单金额）',
  `quote_volume` decimal(30,8) DEFAULT '0.00000000' COMMENT '24h成交额（USDT）',
  `order_types` varchar(256) DEFAULT NULL COMMENT '允许的订单类型（逗号分隔）',
  `is_spot_trading_allowed` tinyint(1) DEFAULT '0' COMMENT '是否允许现货交易',
  `is_margin_trading_allowed` tinyint(1) DEFAULT '0' COMMENT '是否允许杠杆交易',
  `updated_at` datetime NOT NULL COMMENT '数据更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_symbol` (`symbol`),
  KEY `idx_base_asset` (`base_asset`),
  KEY `idx_quote_asset` (`quote_asset`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Binance现货交易对信息';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_gate_future_contracts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_gate_future_contracts` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `name` varchar(50) NOT NULL COMMENT '合约名称',
  `base_asset` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '合约名称',
  `type` varchar(20) NOT NULL COMMENT '合约类型',
  `quanto_multiplier` decimal(20,8) DEFAULT NULL COMMENT '合约乘数',
  `order_price_round` varchar(32) DEFAULT NULL COMMENT '最小价格变动(如 0.0001)',
  `order_size_min` bigint DEFAULT NULL COMMENT '最小下单量',
  `order_size_max` bigint DEFAULT NULL COMMENT '最大下单量',
  `enable_decimal` tinyint(1) DEFAULT NULL COMMENT '是否支持小数下单',
  `leverage_min` int DEFAULT NULL COMMENT '最小杠杆',
  `leverage_max` int DEFAULT NULL COMMENT '最大杠杆',
  `maker_fee_rate` decimal(10,8) DEFAULT NULL COMMENT '挂单费率',
  `taker_fee_rate` decimal(10,8) DEFAULT NULL COMMENT '吃单费率',
  `maintenance_rate` decimal(10,6) DEFAULT NULL COMMENT '维持保证金率(Gate API maintenance_rate字段)',
  `funding_rate` decimal(10,8) DEFAULT NULL COMMENT '当前资金费率',
  `funding_rate_24h` decimal(10,8) DEFAULT NULL COMMENT '24小时资金费率',
  `funding_interval` int DEFAULT NULL COMMENT '资金费率应用间隔(秒)',
  `funding_next_apply` datetime DEFAULT NULL COMMENT '下次资金费率应用时间',
  `status` varchar(20) DEFAULT NULL COMMENT '合约状态',
  `funding_rate_limit` decimal(10,8) DEFAULT NULL COMMENT '资金费率上限',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `volume_24h_settle` decimal(20,2) DEFAULT NULL COMMENT '24小时成交量USDT',
  `high_24h` decimal(28,12) DEFAULT NULL COMMENT 'Gate合约24h最高价',
  `low_24h` decimal(28,12) DEFAULT NULL COMMENT 'Gate合约24h最低价',
  `last_price` decimal(28,12) DEFAULT NULL COMMENT 'Gate合约最新价',
  `range_24h_pct` decimal(18,8) DEFAULT NULL COMMENT '24h振幅百分比=(high/low-1)*100',
  `range_position_24h` decimal(18,8) DEFAULT NULL COMMENT '最新价在24h高低区间的位置',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_name` (`name`),
  KEY `idx_status` (`status`),
  KEY `idx_funding_rate` (`funding_rate_24h`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Gate.io永续合约详情表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_holding_volatility_alert_state`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_holding_volatility_alert_state` (
  `base_asset` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `active` tinyint(1) NOT NULL DEFAULT '0',
  `episode_id` bigint NOT NULL DEFAULT '0',
  `notification_sent_at` datetime DEFAULT NULL,
  `triggered_at` datetime DEFAULT NULL,
  `recovered_at` datetime DEFAULT NULL,
  `last_amplitude_pct` decimal(18,8) DEFAULT NULL,
  `last_range_position` decimal(18,8) DEFAULT NULL,
  `last_price` decimal(28,12) DEFAULT NULL,
  `high_24h` decimal(28,12) DEFAULT NULL,
  `low_24h` decimal(28,12) DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`base_asset`),
  KEY `idx_holding_volatility_active` (`active`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_gate_future_funding_rate_threshold`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_gate_future_funding_rate_threshold` (
  `id` int NOT NULL AUTO_INCREMENT,
  `contract` varchar(50) NOT NULL COMMENT '合约名称',
  `total_records` int NOT NULL COMMENT '总记录数',
  `positive_count` int NOT NULL COMMENT '正24h费率次数',
  `percentile_20` decimal(10,6) DEFAULT NULL COMMENT '20%%分位数（24h格式）',
  `percentile_30` decimal(10,6) DEFAULT NULL COMMENT '30%%分位数（24h格式）',
  `percentile_40` decimal(10,6) DEFAULT NULL COMMENT '40%%分位数（24h格式）',
  `min_rate` decimal(10,6) DEFAULT NULL COMMENT '最小值（24h格式）',
  `max_rate` decimal(10,6) DEFAULT NULL COMMENT '最大值（24h格式）',
  `update_time` timestamp NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_contract` (`contract`),
  KEY `idx_percentile_30` (`percentile_30`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='资金费率阈值统计表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_gate_future_his_funding_rates`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_gate_future_his_funding_rates` (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `contract` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL COMMENT '合约名称',
  `funding_rate` decimal(20,10) NOT NULL COMMENT '资金费率',
  `funding_rate_24h` decimal(20,10) DEFAULT NULL COMMENT '24小时资金费率',
  `timestamp` int unsigned NOT NULL DEFAULT '0' COMMENT '资金费率时间戳（秒）',
  `record_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录插入时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_contract_timestamp` (`contract`,`timestamp`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='Gate.io永续合约详情表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_vwap_basis_snapshot`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_vwap_basis_snapshot` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `snapshot_time` datetime NOT NULL COMMENT '快照时间',
  `base_asset` varchar(20) NOT NULL COMMENT '标的资产(如BTC, ETH)',
  `open_amount_usdt` decimal(12,2) NOT NULL COMMENT '开仓金额(USDT)',
  `spot_open_vwap` decimal(20,8) DEFAULT NULL COMMENT '现货开仓VWAP',
  `future_open_vwap` decimal(20,8) DEFAULT NULL COMMENT '合约开仓VWAP',
  `spot_close_vwap` decimal(20,8) DEFAULT NULL COMMENT '现货平仓VWAP',
  `future_close_vwap` decimal(20,8) DEFAULT NULL COMMENT '合约平仓VWAP',
  `open_vwap_basis_bps` decimal(10,4) DEFAULT NULL COMMENT '开仓VWAP基差(bps)',
  `close_vwap_basis_bps` decimal(10,4) DEFAULT NULL COMMENT '平仓VWAP基差(bps)',
  `open_coverage` decimal(8,4) DEFAULT NULL COMMENT '开仓盘口覆盖率',
  `close_coverage` decimal(8,4) DEFAULT NULL COMMENT '平仓盘口覆盖率',
  PRIMARY KEY (`id`),
  KEY `idx_base_time` (`base_asset`,`snapshot_time`),
  KEY `idx_time` (`snapshot_time`),
  KEY `idx_snapshot_asset_time` (`base_asset`,`snapshot_time`),
  KEY `idx_snapshot_time` (`snapshot_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='VWAP基差历史快照';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_vwap_basis_threshold`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_vwap_basis_threshold` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `base_asset` varchar(20) NOT NULL COMMENT '标的资产',
  `calc_date` date NOT NULL COMMENT '计算日期',
  `open_basis_max` decimal(10,4) DEFAULT NULL COMMENT '开仓基差最大值(bps)',
  `open_basis_min` decimal(10,4) DEFAULT NULL COMMENT '开仓基差最小值(bps)',
  `open_basis_p1` decimal(10,4) DEFAULT NULL COMMENT '开仓基差 top1% 分位',
  `open_basis_p2` decimal(10,4) DEFAULT NULL COMMENT '开仓基差 top2% 分位',
  `open_basis_p3` decimal(10,4) DEFAULT NULL COMMENT '开仓基差 top3% 分位',
  `open_basis_p5` decimal(10,4) DEFAULT NULL COMMENT '开仓基差 top5% 分位',
  `open_basis_p10` decimal(10,4) DEFAULT NULL COMMENT 'top10%分界点(bps)-只有10%时刻基差高于此值',
  `open_basis_p20` decimal(10,4) DEFAULT NULL COMMENT 'top20%分界点(bps)-只有20%时刻基差高于此值',
  `close_basis_max` decimal(10,4) DEFAULT NULL COMMENT '平仓基差最大值(bps)',
  `close_basis_min` decimal(10,4) DEFAULT NULL COMMENT '平仓基差最小值(bps)',
  `close_basis_p1` decimal(10,4) DEFAULT NULL COMMENT '平仓基差 bot1% 分位',
  `close_basis_p2` decimal(10,4) DEFAULT NULL COMMENT '平仓基差 bot2% 分位',
  `close_basis_p3` decimal(10,4) DEFAULT NULL COMMENT '平仓基差 bot3% 分位',
  `close_basis_p5` decimal(10,4) DEFAULT NULL COMMENT '平仓基差 bot5% 分位',
  `close_basis_p10` decimal(10,4) DEFAULT NULL COMMENT 'top10% 时刻分界点(bps)-只有10%基差低于此值',
  `close_basis_p20` decimal(10,4) DEFAULT NULL COMMENT 'top20%分界点(bps)-只有20%时刻基差低于此值',
  `reverse_open_basis_p20` decimal(10,4) DEFAULT NULL COMMENT '反向开仓P20：spot bid + future ask，升序20分位，越低越有利',
  `reverse_close_basis_p20` decimal(10,4) DEFAULT NULL COMMENT '反向平仓P20：spot ask + future bid，降序top20分界，越高越有利',
  `updated_at` datetime DEFAULT NULL COMMENT '数据写入/更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_base_date` (`base_asset`,`calc_date`),
  KEY `idx_date` (`calc_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='VWAP基差分位阈值(每日更新)';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_trade_signal`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_trade_signal` (
  `id` int NOT NULL AUTO_INCREMENT,
  `base_asset` varchar(20) NOT NULL,
  `signal_time` datetime NOT NULL COMMENT '信号首次检测时间(进入峰值监控)',
  `resolved_time` datetime DEFAULT NULL COMMENT '信号结束时间',
  `status` enum('monitoring','opened','conditions_lost','rejected','gate_rejected','monitor_timeout') NOT NULL DEFAULT 'monitoring',
  `entry_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '进入监控时基差(bps)',
  `signal_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '触发时看到的开仓VWAP基差(bps)',
  `peak_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '监控期间峰值基差(bps)',
  `pre_gate_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '下单前最终旁路重算的开仓VWAP基差(bps)',
  `actual_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '成交后真实开仓基差(bps)',
  `exit_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '结束时基差(bps)',
  `exit_reason` text COMMENT '结束原因(条件消失原因/拒单原因/开仓原因)',
  `duration_sec` int DEFAULT NULL COMMENT '监控持续时长(秒)',
  `trigger_type` varchar(20) DEFAULT NULL COMMENT '触发方式: pullback (回落确认开仓) / monitor_timeout (监控超时未回落，放弃本轮)',
  `order_uuid` varchar(50) DEFAULT NULL COMMENT '关联订单UUID(仅opened)',
  PRIMARY KEY (`id`),
  KEY `idx_base_asset` (`base_asset`),
  KEY `idx_signal_time` (`signal_time`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='交易信号日志';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_trade_position`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_trade_position` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '持仓ID',
  `order_uuid` varchar(36) NOT NULL COMMENT '关联订单组UUID',
  `close_order_uuid` varchar(36) DEFAULT NULL COMMENT '平仓订单组UUID',
  `base_asset` varchar(20) NOT NULL COMMENT '标的资产',
  `spot_symbol` varchar(30) NOT NULL COMMENT '现货交易对(如BTCUSDT)',
  `future_contract` varchar(30) NOT NULL COMMENT '期货合约名(如BTC_USDT)',
  `status` enum('holding','closed') NOT NULL DEFAULT 'holding' COMMENT '持仓状态',
  `opened_at` datetime NOT NULL COMMENT '开仓时间',
  `closed_at` datetime DEFAULT NULL COMMENT '平仓时间',
  `close_reason` text COMMENT '平仓原因/交易所仓位风险摘要',
  `spot_open_qty` decimal(20,8) NOT NULL COMMENT '现货开仓数量',
  `spot_open_price` decimal(20,8) NOT NULL COMMENT '现货开仓VWAP',
  `spot_open_amount` decimal(20,2) NOT NULL COMMENT '现货开仓金额',
  `future_open_qty` decimal(20,8) NOT NULL COMMENT '期货开仓数量(标的资产)',
  `future_open_price` decimal(20,8) NOT NULL COMMENT '期货开仓VWAP',
  `future_open_contracts` int NOT NULL COMMENT '期货开仓张数',
  `open_spread_bps` decimal(10,2) NOT NULL COMMENT '开仓时价差(bps)',
  `open_funding_rate_24h` decimal(18,10) DEFAULT NULL COMMENT '开仓时刻实时折算24h资金费率',
  `signal_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '触发时看到的开仓VWAP基差(bps)',
  `pre_gate_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '下单前最终旁路重算的开仓VWAP基差(bps)',
  `actual_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '成交后真实开仓基差(bps)',
  `open_reason` text COMMENT '开仓原因/执行审计',
  `funding_rate_sum_bps` decimal(10,2) NOT NULL DEFAULT '0.00' COMMENT '累计资金费率(bps)',
  `funding_payments_count` int NOT NULL DEFAULT '0' COMMENT '已结算资金费次数',
  `funding_total_pnl` decimal(20,4) NOT NULL DEFAULT '0.0000' COMMENT '累计资金费收益(USDT)',
  `realized_pnl` decimal(24,8) DEFAULT NULL COMMENT '订单级价差已实现盈亏(USDT)',
  `realized_pnl_bps` decimal(12,4) DEFAULT NULL COMMENT '订单级价差已实现盈亏(bps)',
  `next_funding_time` datetime DEFAULT NULL COMMENT '下次资金费结算时间',
  `spot_close_price` decimal(20,8) DEFAULT NULL COMMENT '现货平仓VWAP',
  `future_close_price` decimal(20,8) DEFAULT NULL COMMENT '期货平仓VWAP',
  `spot_close_amount` decimal(20,4) DEFAULT NULL COMMENT '现货平仓成交金额(USDT)',
  `future_close_amount` decimal(20,4) DEFAULT NULL COMMENT '期货平仓成交金额(USDT)',
  `close_spread_bps` decimal(10,2) DEFAULT NULL COMMENT '平仓时价差(bps)',
  `close_funding_rate_24h` decimal(18,10) DEFAULT NULL COMMENT '平仓时刻实时折算24h资金费率',
  `realized_pnl_spot` decimal(20,4) DEFAULT NULL COMMENT '现货实现盈亏',
  `realized_pnl_future` decimal(20,4) DEFAULT NULL COMMENT '期货实现盈亏',
  `realized_pnl_total` decimal(20,4) DEFAULT NULL COMMENT '总实现盈亏',
  `total_pnl` decimal(20,4) DEFAULT NULL COMMENT '总盈亏',
  `total_pnl_bps` decimal(10,2) DEFAULT NULL COMMENT '总盈亏(bps)',
  `fee_cost` decimal(24,8) DEFAULT NULL COMMENT '订单级手续费成本，负数展示(USDT)',
  `fee_bps` decimal(12,4) DEFAULT NULL COMMENT '订单级手续费成本(bps，负数)',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `margin_topup_count` int NOT NULL DEFAULT '0' COMMENT '已追加保证金次数',
  `margin_topup_total` decimal(16,6) NOT NULL DEFAULT '0.000000' COMMENT '累计追加保证金(USDT)',
  `margin_topup_last_at` datetime DEFAULT NULL COMMENT '最近一次追加保证金时间',
  `exchange_risk_status` enum('normal','desynced','resolved') NOT NULL DEFAULT 'normal' COMMENT '交易所侧仓位风险状态',
  `exchange_risk_type` varchar(40) DEFAULT NULL COMMENT '交易所侧风险类型：adl/missing_gate_position/qty_mismatch',
  `exchange_risk_at` datetime DEFAULT NULL COMMENT '交易所侧风险发生/识别时间',
  `exchange_risk_detail` text COMMENT '交易所侧风险详情',
  PRIMARY KEY (`id`),
  UNIQUE KEY `order_uuid` (`order_uuid`),
  KEY `idx_status` (`status`),
  KEY `idx_base_asset` (`base_asset`),
  KEY `idx_opened_at` (`opened_at`),
  KEY `idx_trade_position_exchange_risk` (`exchange_risk_status`,`exchange_risk_type`,`exchange_risk_at`),
  KEY `idx_trade_position_status_opened` (`status`,`opened_at`,`id`),
  KEY `idx_trade_position_status_closed` (`status`,`closed_at`,`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='持仓表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_trade_order`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_trade_order` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '订单ID',
  `order_uuid` varchar(36) NOT NULL COMMENT '订单组UUID(同一批次现货+期货共用)',
  `position_id` bigint DEFAULT NULL COMMENT '关联持仓ID，4笔订单共享同一个 position_id',
  `base_asset` varchar(20) NOT NULL COMMENT '标的资产(如BTC)',
  `spot_symbol` varchar(30) DEFAULT NULL COMMENT '现货交易对(如BTCUSDT)',
  `future_contract` varchar(30) DEFAULT NULL COMMENT '期货合约名(如BTC_USDT)',
  `order_side` enum('open','close') NOT NULL COMMENT '订单方向: open=开仓, close=平仓',
  `market_type` enum('spot','future') NOT NULL COMMENT '市场类型: spot=现货, future=期货',
  `trade_direction` enum('buy','sell') NOT NULL COMMENT '交易方向: buy=买入, sell=卖出',
  `leverage` decimal(10,2) DEFAULT NULL COMMENT '订单腿杠杆倍数：spot=1，future=下单时Gate逐仓杠杆',
  `status` enum('pending','executed','rejected','failed') NOT NULL DEFAULT 'pending' COMMENT '订单状态: pending=待执行, executed=已成交, rejected=已拒单, failed=失败',
  `channel` enum('Mock','SimTrade','Live') NOT NULL DEFAULT 'Mock' COMMENT '渠道: Mock=模拟成交, SimTrade=模拟盘, Live=实盘',
  `reject_reason` text COMMENT '订单复盘原因/拒单原因/执行审计',
  `target_qty` decimal(20,8) NOT NULL COMMENT '目标数量(标的资产)',
  `target_amount` decimal(20,2) NOT NULL COMMENT '目标金额(USDT)',
  `exec_price` decimal(20,8) DEFAULT NULL COMMENT '成交VWAP价格',
  `exec_qty` decimal(20,8) DEFAULT NULL COMMENT '实际成交数量',
  `exec_amount` decimal(20,2) DEFAULT NULL COMMENT '实际成交金额(USDT)',
  `coverage_ratio` decimal(10,4) DEFAULT NULL COMMENT '盘口覆盖率',
  `open_coverage` decimal(10,4) DEFAULT NULL COMMENT '开仓盘口覆盖',
  `open_vwap_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '开仓VWAP基差(bps)',
  `signal_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '触发时看到的开仓VWAP基差(bps)',
  `pre_gate_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '下单前最终旁路重算的开仓VWAP基差(bps)',
  `actual_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '成交后真实开仓基差(bps)',
  `risk_relief_bps` decimal(10,2) DEFAULT NULL COMMENT '风险缓释(bps)',
  `open_marginal_basis_bps` decimal(10,2) DEFAULT NULL COMMENT '开仓边际基差(bps)',
  `funding_rate_24h` decimal(10,6) DEFAULT NULL COMMENT '开仓时24h资金费率',
  `liquidity_role` enum('maker','taker','unknown') DEFAULT NULL COMMENT '实际成交角色 maker/taker/unknown',
  `fee_rate` decimal(18,10) DEFAULT NULL COMMENT '账户实际/估算手续费率',
  `fee_amount` decimal(20,8) DEFAULT NULL COMMENT '交易所返回或估算的手续费金额',
  `fee_amount_usdt` decimal(20,8) DEFAULT NULL COMMENT '手续费折算USDT金额',
  `fee_asset` varchar(20) DEFAULT NULL COMMENT '手续费资产',
  `exchange_order_id` varchar(80) DEFAULT NULL COMMENT '交易所订单ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '订单创建时间',
  `executed_at` datetime DEFAULT NULL COMMENT '成交时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_order_uuid` (`order_uuid`),
  KEY `idx_base_asset` (`base_asset`),
  KEY `idx_status` (`status`),
  KEY `idx_created_at` (`created_at`),
  KEY `idx_position_id` (`position_id`),
  KEY `idx_channel` (`channel`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='交易订单表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_trade_funding_fee_history`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_trade_funding_fee_history` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键',
  `position_id` bigint NOT NULL COMMENT '关联持仓ID (mi_trade_position.id)',
  `base_asset` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '标的资产',
  `payment_seq` int NOT NULL COMMENT '第几次结算(从1开始)',
  `funding_rate` decimal(18,10) NOT NULL COMMENT '本次单期资金费率(8h费率, 即24h/3)',
  `funding_rate_24h` decimal(18,10) DEFAULT NULL COMMENT '当时的24h资金费率',
  `funding_pnl` decimal(18,6) NOT NULL COMMENT '本次资金费收益(USDT)',
  `future_notional` decimal(18,6) DEFAULT NULL COMMENT '期货名义价值(qty * price)',
  `settled_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '结算时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_position_seq` (`position_id`,`payment_seq`),
  KEY `idx_position_id` (`position_id`),
  KEY `idx_base_asset` (`base_asset`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='资金费结算历史表';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_margin_topup_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_margin_topup_log` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `base_asset` varchar(20) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '标的资产',
  `position_id` bigint NOT NULL COMMENT '持仓ID',
  `contract` varchar(30) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Gate合约',
  `topup_amount` decimal(16,6) NOT NULL COMMENT '追加金额(USDT)',
  `target_margin_usdt` decimal(16,6) DEFAULT NULL COMMENT '目标保证金(USDT)',
  `margin_before_usdt` decimal(16,6) DEFAULT NULL COMMENT '追加前本地保证金(USDT)',
  `liq_distance_before` decimal(8,2) DEFAULT NULL COMMENT '追加前距爆仓距离(%)',
  `liq_distance_after` decimal(8,2) DEFAULT NULL COMMENT '预计追加后距爆仓距离(%)',
  `gate_available_before` decimal(16,4) DEFAULT NULL COMMENT '追加前Gate可用余额',
  `hedge_balanced` tinyint(1) NOT NULL DEFAULT '1' COMMENT '追加前现货/期货是否平衡',
  `success` tinyint(1) NOT NULL DEFAULT '1' COMMENT '是否成功',
  `error_msg` text COLLATE utf8mb4_unicode_ci COMMENT '失败原因',
  `created_at` datetime DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_position` (`position_id`),
  KEY `idx_asset_time` (`base_asset`,`created_at`),
  KEY `idx_success_time` (`success`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='自动追加保证金日志';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_recon_snapshot`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_recon_snapshot` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键',
  `snapshot_at` datetime NOT NULL COMMENT '本轮对账时间',
  `exchange` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'binance/gate',
  `base_asset` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '标的资产',
  `dimension` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'position/balance/margin/upnl/error',
  `local_value` decimal(30,10) DEFAULT NULL COMMENT '本地聚合数值',
  `exchange_value` decimal(30,10) DEFAULT NULL COMMENT '交易所返回数值',
  `diff_value` decimal(30,10) DEFAULT NULL COMMENT 'exchange_value - local_value',
  `diff_ratio` decimal(20,10) DEFAULT NULL COMMENT '差异占比',
  `is_match` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否在容差范围内一致',
  `detail` json DEFAULT NULL COMMENT '原始字段或错误摘要',
  PRIMARY KEY (`id`),
  KEY `idx_snapshot_at` (`snapshot_at`),
  KEY `idx_asset` (`base_asset`,`exchange`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='交易所持仓对账快照';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_capital_snapshot`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_capital_snapshot` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键',
  `snapshot_at` datetime NOT NULL COMMENT '快照时间',
  `exchange` varchar(16) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'binance/gate/total',
  `equity_usdt` decimal(30,10) DEFAULT NULL COMMENT '账户权益/净值(USDT)',
  `available_usdt` decimal(30,10) DEFAULT NULL COMMENT '可用资金(USDT)',
  `locked_usdt` decimal(30,10) DEFAULT NULL COMMENT '锁定/委托占用(USDT)',
  `position_value_usdt` decimal(30,10) DEFAULT NULL COMMENT '现货持仓市值或期货持仓保证金/价值(USDT)',
  `margin_used_usdt` decimal(30,10) DEFAULT NULL COMMENT '保证金占用(USDT)',
  `unrealized_pnl_usdt` decimal(30,10) DEFAULT NULL COMMENT '交易所未实现盈亏(USDT)',
  `realized_pnl_usdt` decimal(30,10) DEFAULT NULL COMMENT '本地已实现盈亏(USDT)',
  `funding_pnl_usdt` decimal(30,10) DEFAULT NULL COMMENT '本地资金费累计(USDT)',
  `fee_cost_usdt` decimal(30,10) DEFAULT NULL COMMENT '本地手续费成本(USDT)',
  `total_pnl_usdt` decimal(30,10) DEFAULT NULL COMMENT '本地综合盈亏(USDT)',
  `detail` json DEFAULT NULL COMMENT '原始账户字段/估值详情',
  PRIMARY KEY (`id`),
  KEY `idx_snapshot_at` (`snapshot_at`),
  KEY `idx_exchange_snapshot` (`exchange`,`snapshot_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='交易所资金快照';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_capital_daily_summary`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_capital_daily_summary` (
  `summary_date` date NOT NULL,
  `first_snapshot_at` datetime NOT NULL,
  `last_snapshot_at` datetime NOT NULL,
  `first_equity_usdt` decimal(30,10) DEFAULT NULL,
  `last_equity_usdt` decimal(30,10) DEFAULT NULL,
  `equity_sum_usdt` decimal(38,10) NOT NULL DEFAULT '0.0000000000',
  `sample_count` int unsigned NOT NULL DEFAULT '0',
  `first_gross_pnl_usdt` decimal(30,10) DEFAULT NULL,
  `last_gross_pnl_usdt` decimal(30,10) DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`summary_date`),
  KEY `idx_capital_daily_last_snapshot` (`last_snapshot_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='每日资金收益汇总，用于长期年化收益统计';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_exchange_risk_event`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_exchange_risk_event` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `event_key` varchar(160) NOT NULL COMMENT '事件幂等键',
  `exchange` varchar(20) NOT NULL COMMENT '交易所',
  `market_type` varchar(20) NOT NULL COMMENT '市场类型',
  `risk_type` varchar(40) NOT NULL COMMENT 'adl/liquidation/unknown',
  `base_asset` varchar(30) NOT NULL COMMENT '标的资产',
  `contract` varchar(60) NOT NULL COMMENT '合约',
  `event_at` datetime(3) NOT NULL COMMENT '交易所事件时间',
  `exchange_order_id` varchar(80) DEFAULT NULL COMMENT '交易所订单ID',
  `exchange_trade_id` varchar(80) DEFAULT NULL COMMENT '交易所成交ID',
  `side` varchar(40) DEFAULT NULL COMMENT '事件方向/文本',
  `size` decimal(28,10) DEFAULT NULL COMMENT '事件涉及合约张数',
  `fill_price` decimal(28,12) DEFAULT NULL COMMENT '成交/处置价格',
  `entry_price` decimal(28,12) DEFAULT NULL COMMENT '原入场价格',
  `mark_price` decimal(28,12) DEFAULT NULL COMMENT '标记价格',
  `liq_price` decimal(28,12) DEFAULT NULL COMMENT '强平价格',
  `pnl` decimal(28,10) DEFAULT NULL COMMENT '事件PnL',
  `raw_json` longtext COMMENT '交易所原始事件JSON',
  `status` enum('received','remediated','failed','ignored') NOT NULL DEFAULT 'received' COMMENT '事件处理状态',
  `remediation_action` varchar(40) DEFAULT NULL COMMENT '自动处置动作',
  `remediation_result` longtext COMMENT '自动处置结果JSON',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_exchange_risk_event_key` (`event_key`),
  KEY `idx_exchange_risk_event_asset` (`base_asset`,`risk_type`,`event_at`),
  KEY `idx_exchange_risk_event_status` (`status`,`event_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='交易所ADL/强平风险事件';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_reverse_trade_signal`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_reverse_trade_signal` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `base_asset` varchar(32) NOT NULL,
  `contract` varchar(64) DEFAULT NULL,
  `symbol` varchar(50) DEFAULT NULL,
  `signal_time` datetime NOT NULL,
  `resolved_time` datetime DEFAULT NULL,
  `duration_sec` int DEFAULT NULL,
  `trigger_type` varchar(32) DEFAULT NULL,
  `last_seen_time` datetime DEFAULT NULL,
  `status` enum('monitoring','opened','conditions_lost','rejected','gate_rejected','monitor_timeout') NOT NULL DEFAULT 'monitoring',
  `reverse_status` varchar(64) DEFAULT NULL,
  `trigger_reason` text,
  `reject_reason` text COMMENT '结束/拒绝/观察原因',
  `order_uuid` varchar(64) DEFAULT NULL,
  `funding_rate_24h` decimal(18,10) DEFAULT NULL,
  `reverse_open_basis_bps` decimal(10,2) DEFAULT NULL,
  `signal_basis_bps` decimal(10,2) DEFAULT NULL,
  `valley_basis_bps` decimal(10,2) DEFAULT NULL,
  `rebound_basis_bps` decimal(10,2) DEFAULT NULL,
  `pre_gate_basis_bps` decimal(10,2) DEFAULT NULL,
  `actual_basis_bps` decimal(10,2) DEFAULT NULL,
  `funding_rate_2h` decimal(18,10) DEFAULT NULL,
  `reverse_gross_funding_bps` decimal(12,4) DEFAULT NULL,
  `reverse_expected_funding_bps` decimal(12,4) DEFAULT NULL,
  `reverse_basis_bps` decimal(12,4) DEFAULT NULL,
  `reverse_close_basis_bps` decimal(12,4) DEFAULT NULL,
  `reverse_p20_edge_bps` decimal(12,4) DEFAULT NULL,
  `reverse_margin_edge_bps` decimal(12,4) DEFAULT NULL,
  `reverse_open_coverage` decimal(12,8) DEFAULT NULL,
  `reverse_borrow_hourly_rate` decimal(18,10) DEFAULT NULL,
  `reverse_borrow_24h_bps` decimal(12,4) DEFAULT NULL,
  `reverse_borrow_limit` decimal(24,8) DEFAULT NULL,
  `reverse_capacity_usdt` decimal(24,8) DEFAULT NULL,
  `reverse_open_basis_p20` decimal(12,4) DEFAULT NULL,
  `reverse_close_basis_p20` decimal(12,4) DEFAULT NULL,
  `margin_edge_bps` decimal(10,2) DEFAULT NULL,
  `borrow_hourly_rate` decimal(18,10) DEFAULT NULL,
  `borrow_24h_bps` decimal(10,2) DEFAULT NULL,
  `borrow_limit` decimal(24,8) DEFAULT NULL,
  `borrow_capacity_usdt` decimal(18,2) DEFAULT NULL,
  `open_coverage` decimal(10,4) DEFAULT NULL,
  `capacity_usdt` decimal(18,2) DEFAULT NULL,
  `open_amount_usdt` decimal(18,2) DEFAULT NULL,
  `funding_next_apply` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_signal_time` (`signal_time`),
  KEY `idx_base_asset_time` (`base_asset`,`signal_time`),
  KEY `idx_status_time` (`status`,`signal_time`),
  KEY `idx_active_asset` (`base_asset`,`resolved_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_reverse_trade_position`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_reverse_trade_position` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `order_uuid` varchar(64) NOT NULL COMMENT '反向开仓订单组 UUID',
  `signal_id` bigint DEFAULT NULL COMMENT '关联 mi_reverse_trade_signal.id',
  `base_asset` varchar(32) NOT NULL,
  `spot_symbol` varchar(64) NOT NULL,
  `future_contract` varchar(64) NOT NULL,
  `status` enum('holding','closing','closed','risk','desynced') NOT NULL DEFAULT 'holding',
  `opened_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `closed_at` datetime DEFAULT NULL,
  `close_reason` text,
  `open_amount_usdt` decimal(24,8) DEFAULT NULL,
  `close_amount_usdt` decimal(24,8) DEFAULT NULL,
  `borrow_asset` varchar(32) DEFAULT NULL,
  `borrow_qty` decimal(30,12) DEFAULT NULL,
  `borrow_repaid_qty` decimal(30,12) DEFAULT NULL,
  `borrow_hourly_rate` decimal(18,10) DEFAULT NULL,
  `open_borrow_24h_bps` decimal(12,4) DEFAULT NULL,
  `borrow_interest_usdt` decimal(24,8) NOT NULL DEFAULT '0.00000000',
  `borrow_interest_bps` decimal(12,4) NOT NULL DEFAULT '0.0000',
  `spot_open_qty` decimal(30,12) DEFAULT NULL COMMENT 'Binance margin sell 数量',
  `spot_open_price` decimal(24,12) DEFAULT NULL,
  `spot_open_amount` decimal(24,8) DEFAULT NULL,
  `spot_close_qty` decimal(30,12) DEFAULT NULL COMMENT 'Binance margin buy 回补数量',
  `spot_close_price` decimal(24,12) DEFAULT NULL,
  `spot_close_amount` decimal(24,8) DEFAULT NULL,
  `future_open_qty` decimal(30,12) DEFAULT NULL COMMENT 'Gate futures long 张数/数量',
  `future_open_price` decimal(24,12) DEFAULT NULL,
  `future_open_amount` decimal(24,8) DEFAULT NULL,
  `future_close_qty` decimal(30,12) DEFAULT NULL,
  `future_close_price` decimal(24,12) DEFAULT NULL,
  `future_close_amount` decimal(24,8) DEFAULT NULL,
  `reverse_open_basis_bps` decimal(12,4) DEFAULT NULL,
  `reverse_close_basis_bps` decimal(12,4) DEFAULT NULL,
  `reverse_open_basis_p20` decimal(12,4) DEFAULT NULL,
  `reverse_close_basis_p20` decimal(12,4) DEFAULT NULL,
  `signal_basis_bps` decimal(12,4) DEFAULT NULL,
  `pre_gate_basis_bps` decimal(12,4) DEFAULT NULL,
  `actual_basis_bps` decimal(12,4) DEFAULT NULL,
  `execution_drift_bps` decimal(12,4) DEFAULT NULL,
  `open_funding_rate_24h` decimal(18,10) DEFAULT NULL,
  `funding_pnl_usdt` decimal(24,8) NOT NULL DEFAULT '0.00000000',
  `funding_pnl_bps` decimal(12,4) NOT NULL DEFAULT '0.0000',
  `fee_total_usdt` decimal(24,8) NOT NULL DEFAULT '0.00000000',
  `fee_total_bps` decimal(12,4) NOT NULL DEFAULT '0.0000',
  `realized_pnl_usdt` decimal(24,8) DEFAULT NULL,
  `realized_pnl_bps` decimal(12,4) DEFAULT NULL,
  `exchange_risk_status` enum('normal','desynced','resolved') NOT NULL DEFAULT 'normal',
  `exchange_risk_type` varchar(64) DEFAULT NULL,
  `exchange_risk_at` datetime DEFAULT NULL,
  `exchange_risk_detail` text,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_reverse_position_order_uuid` (`order_uuid`),
  KEY `idx_reverse_position_status_time` (`status`,`opened_at`),
  KEY `idx_reverse_position_asset_status` (`base_asset`,`status`),
  KEY `idx_reverse_position_signal` (`signal_id`),
  KEY `idx_reverse_position_risk` (`exchange_risk_status`,`exchange_risk_type`,`exchange_risk_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='反向套利持仓';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_reverse_trade_order`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_reverse_trade_order` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `order_uuid` varchar(64) NOT NULL,
  `position_id` bigint DEFAULT NULL,
  `signal_id` bigint DEFAULT NULL,
  `base_asset` varchar(32) NOT NULL,
  `spot_symbol` varchar(64) DEFAULT NULL,
  `future_contract` varchar(64) DEFAULT NULL,
  `order_side` enum('open','close','repay','unwind') NOT NULL,
  `market_type` enum('margin_spot','future','margin_repay') NOT NULL,
  `trade_direction` enum('buy','sell','borrow','repay') NOT NULL,
  `status` enum('pending','filled','partial','failed','cancelled','skipped') NOT NULL DEFAULT 'pending',
  `target_qty` decimal(30,12) DEFAULT NULL,
  `target_amount` decimal(24,8) DEFAULT NULL,
  `exec_price` decimal(24,12) DEFAULT NULL,
  `exec_qty` decimal(30,12) DEFAULT NULL,
  `exec_amount` decimal(24,8) DEFAULT NULL,
  `exchange_order_id` varchar(128) DEFAULT NULL,
  `client_order_id` varchar(128) DEFAULT NULL,
  `liquidity_role` varchar(16) DEFAULT NULL,
  `fee_rate` decimal(18,10) DEFAULT NULL,
  `fee_amount` decimal(30,12) DEFAULT NULL,
  `fee_asset` varchar(32) DEFAULT NULL,
  `fee_amount_usdt` decimal(24,8) DEFAULT NULL,
  `reduce_only` tinyint(1) DEFAULT NULL,
  `protective_price` decimal(24,12) DEFAULT NULL,
  `execution_style` varchar(32) DEFAULT NULL,
  `reject_reason` text,
  `raw_response` json DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_reverse_order_uuid` (`order_uuid`),
  KEY `idx_reverse_order_position` (`position_id`),
  KEY `idx_reverse_order_signal` (`signal_id`),
  KEY `idx_reverse_order_asset_time` (`base_asset`,`created_at`),
  KEY `idx_reverse_order_status_time` (`status`,`created_at`),
  CONSTRAINT `fk_reverse_order_position` FOREIGN KEY (`position_id`) REFERENCES `mi_reverse_trade_position` (`id`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='反向套利订单';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_reverse_research_snapshot`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_reverse_research_snapshot` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `snapshot_time` datetime NOT NULL,
  `base_asset` varchar(32) NOT NULL,
  `contract` varchar(64) DEFAULT NULL,
  `symbol` varchar(64) DEFAULT NULL,
  `sample_source` varchar(32) NOT NULL DEFAULT 'loop',
  `borrowable` tinyint DEFAULT NULL,
  `max_borrowable_amount` decimal(28,12) DEFAULT NULL,
  `account_borrow_limit` decimal(28,12) DEFAULT NULL,
  `borrow_capacity_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_hourly_rate` decimal(18,10) DEFAULT NULL,
  `borrow_24h_bps` decimal(12,4) DEFAULT NULL,
  `borrow_unavailable_reason` varchar(128) DEFAULT NULL,
  `reverse_status` varchar(64) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_snapshot_time` (`snapshot_time`),
  KEY `idx_asset_time` (`base_asset`,`snapshot_time`),
  KEY `idx_status_time` (`reverse_status`,`snapshot_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_server_metric_snapshot`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_server_metric_snapshot` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键',
  `snapshot_at` datetime NOT NULL COMMENT '采样时间',
  `hostname` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '主机名',
  `cpu_usage_percent` decimal(8,4) DEFAULT NULL COMMENT 'CPU使用率百分比',
  `load1` decimal(10,4) DEFAULT NULL COMMENT '1分钟负载',
  `load5` decimal(10,4) DEFAULT NULL COMMENT '5分钟负载',
  `load15` decimal(10,4) DEFAULT NULL COMMENT '15分钟负载',
  `cpu_count` int DEFAULT NULL COMMENT 'CPU核心数',
  `memory_total_bytes` bigint DEFAULT NULL COMMENT '内存总量',
  `memory_used_bytes` bigint DEFAULT NULL COMMENT '内存已用',
  `memory_usage_percent` decimal(8,4) DEFAULT NULL COMMENT '内存使用率百分比',
  `disk_path` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '/' COMMENT '磁盘采样路径',
  `disk_total_bytes` bigint DEFAULT NULL COMMENT '硬盘总量',
  `disk_used_bytes` bigint DEFAULT NULL COMMENT '硬盘已用',
  `disk_usage_percent` decimal(8,4) DEFAULT NULL COMMENT '硬盘使用率百分比',
  `uptime_sec` bigint DEFAULT NULL COMMENT '系统启动秒数',
  `detail` json DEFAULT NULL COMMENT '附加原始信息',
  PRIMARY KEY (`id`),
  KEY `idx_snapshot_at` (`snapshot_at`),
  KEY `idx_hostname_snapshot` (`hostname`,`snapshot_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='服务器指标快照';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_listing_event`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_listing_event` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键',
  `base_asset` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '标的资产',
  `gate_contract` varchar(80) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'Gate 永续合约名',
  `binance_symbol` varchar(80) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'Binance 现货交易对',
  `candidate_status` enum('matched','gate_only','binance_only') COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '上新配对状态',
  `action_status` enum('pending','acknowledged','ignored','disabled','added_to_monitor') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending' COMMENT '处理状态',
  `is_actionable` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否值得弹窗提醒',
  `gate_status` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'Gate 合约状态',
  `binance_status` varchar(40) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT 'Binance 现货状态',
  `gate_volume_24h_settle` decimal(30,10) DEFAULT NULL COMMENT 'Gate 24h 成交额',
  `binance_quote_volume` decimal(30,10) DEFAULT NULL COMMENT 'Binance 24h 报价成交额',
  `gate_funding_rate_24h` decimal(18,10) DEFAULT NULL COMMENT 'Gate 24h 资金费率',
  `first_seen_at` datetime NOT NULL COMMENT '首次发现时间',
  `last_seen_at` datetime NOT NULL COMMENT '最近仍存在时间',
  `acknowledged_at` datetime DEFAULT NULL COMMENT '确认时间',
  `action_at` datetime DEFAULT NULL COMMENT '处理时间',
  `action_reason` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL COMMENT '处理原因',
  `source_payload` json DEFAULT NULL COMMENT '原始来源摘要',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_listing_event_asset` (`base_asset`),
  KEY `idx_listing_action` (`action_status`,`is_actionable`,`last_seen_at`),
  KEY `idx_listing_candidate` (`candidate_status`,`last_seen_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='交易对上新事件';
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_popup_notification`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_popup_notification` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'default',
  `dedup_key` varchar(220) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `source` varchar(64) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `type` enum('warning','error','success','info') COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'info',
  `title` varchar(255) COLLATE utf8mb4_unicode_ci NOT NULL,
  `message` text COLLATE utf8mb4_unicode_ci NOT NULL,
  `payload` json DEFAULT NULL,
  `event_at` datetime DEFAULT NULL,
  `read_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_popup_user_dedup` (`user_id`,`dedup_key`),
  KEY `idx_popup_user_read_created` (`user_id`,`read_at`,`created_at`),
  KEY `idx_popup_user_source_created` (`user_id`,`source`,`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_reverse_funding_prediction`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_reverse_funding_prediction` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `generated_at` datetime NOT NULL,
  `model_version` varchar(64) NOT NULL,
  `threshold_rate` decimal(18,10) NOT NULL,
  `lookback_days` int NOT NULL,
  `base_asset` varchar(32) NOT NULL,
  `contract` varchar(64) NOT NULL,
  `strategy_tier` varchar(8) DEFAULT NULL,
  `expected_funding_bps` decimal(12,4) DEFAULT NULL,
  `borrowable` tinyint DEFAULT NULL,
  `borrow_capacity_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_hourly_rate` decimal(18,10) DEFAULT NULL,
  `borrow_24h_bps` decimal(12,4) DEFAULT NULL,
  `max_borrowable_amount` decimal(28,8) DEFAULT NULL,
  `borrow_snapshot_time` datetime DEFAULT NULL,
  `follow_score` decimal(12,4) DEFAULT NULL,
  `follow_reason` varchar(512) DEFAULT NULL,
  `funding_change_1h_bps` decimal(12,4) DEFAULT NULL,
  `funding_change_4h_bps` decimal(12,4) DEFAULT NULL,
  `funding_change_12h_bps` decimal(12,4) DEFAULT NULL,
  `borrow_capacity_drop_1h_pct` decimal(12,4) DEFAULT NULL,
  `borrow_capacity_drop_4h_pct` decimal(12,4) DEFAULT NULL,
  `borrow_capacity_drop_12h_pct` decimal(12,4) DEFAULT NULL,
  `borrow_capacity_drop_max_pct` decimal(12,4) DEFAULT NULL,
  `borrow_capacity_drop_1h_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_capacity_drop_4h_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_capacity_drop_12h_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_capacity_change_1h_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_capacity_change_4h_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_capacity_change_12h_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_capacity_24h_high_usdt` decimal(20,4) DEFAULT NULL,
  `borrow_capacity_drawdown_24h_pct` decimal(12,4) DEFAULT NULL,
  `borrow_capacity_to_24h_high_pct` decimal(12,4) DEFAULT NULL,
  `borrow_availability_1h_pct` decimal(12,4) DEFAULT NULL,
  `borrow_availability_4h_pct` decimal(12,4) DEFAULT NULL,
  `borrow_availability_12h_pct` decimal(12,4) DEFAULT NULL,
  `borrow_pressure_score` decimal(12,4) DEFAULT NULL,
  `current_funding_rate_24h` decimal(18,10) DEFAULT NULL,
  `previous_funding_rate_24h` decimal(18,10) DEFAULT NULL,
  `funding_rate_change` decimal(18,10) DEFAULT NULL,
  `current_bucket` varchar(32) DEFAULT NULL,
  `current_bucket_label` varchar(32) DEFAULT NULL,
  `sample_count` int DEFAULT NULL,
  `conditional_sample_count` int DEFAULT NULL,
  `high_negative_count` int DEFAULT NULL,
  `high_negative_frequency` decimal(12,6) DEFAULT NULL,
  `negative_count` int DEFAULT NULL,
  `negative_frequency` decimal(12,6) DEFAULT NULL,
  `min_funding_rate_24h` decimal(18,10) DEFAULT NULL,
  `max_funding_rate_24h` decimal(18,10) DEFAULT NULL,
  `avg_funding_rate_24h` decimal(18,10) DEFAULT NULL,
  `p_next_1` decimal(12,6) DEFAULT NULL,
  `p_next_2` decimal(12,6) DEFAULT NULL,
  `p_next_3` decimal(12,6) DEFAULT NULL,
  `base_p_next_1` decimal(12,6) DEFAULT NULL,
  `base_p_next_2` decimal(12,6) DEFAULT NULL,
  `base_p_next_3` decimal(12,6) DEFAULT NULL,
  `conditional_p_next_1` decimal(12,6) DEFAULT NULL,
  `conditional_p_next_2` decimal(12,6) DEFAULT NULL,
  `conditional_p_next_3` decimal(12,6) DEFAULT NULL,
  `confidence` decimal(12,6) DEFAULT NULL,
  `last_history_time` datetime DEFAULT NULL,
  `last_high_negative_time` datetime DEFAULT NULL,
  `funding_next_apply` datetime DEFAULT NULL,
  `current_updated_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_generated` (`generated_at`),
  KEY `idx_model_latest` (`threshold_rate`,`lookback_days`,`generated_at`),
  KEY `idx_asset_generated` (`base_asset`,`generated_at`),
  KEY `idx_contract_generated` (`contract`,`generated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
DROP TABLE IF EXISTS `mi_fund_transfer_task`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `mi_fund_transfer_task` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `task_key` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `active_slot` tinyint DEFAULT '1',
  `user_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'default',
  `username` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status` varchar(48) COLLATE utf8mb4_unicode_ci NOT NULL,
  `step` varchar(48) COLLATE utf8mb4_unicode_ci NOT NULL,
  `status_message` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `coin` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `network` varchar(32) COLLATE utf8mb4_unicode_ci NOT NULL,
  `destination_masked` varchar(128) COLLATE utf8mb4_unicode_ci NOT NULL,
  `requested_amount` decimal(24,8) NOT NULL,
  `expected_fee` decimal(24,8) NOT NULL,
  `withdraw_amount` decimal(24,8) NOT NULL,
  `actual_fee` decimal(24,8) DEFAULT NULL,
  `received_amount` decimal(24,8) DEFAULT NULL,
  `binance_transfer_client_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `binance_transfer_id` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `binance_rollback_client_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `binance_rollback_id` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `binance_withdraw_order_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `binance_withdraw_id` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `binance_tx_id` varchar(255) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `gate_deposit_id` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `gate_transfer_client_id` varchar(64) COLLATE utf8mb4_unicode_ci NOT NULL,
  `gate_transfer_id` varchar(128) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `attention_required` tinyint(1) NOT NULL DEFAULT '0',
  `last_error` text COLLATE utf8mb4_unicode_ci,
  `detail` json DEFAULT NULL,
  `delayed_notified_at` datetime DEFAULT NULL,
  `attention_notified_at` datetime DEFAULT NULL,
  `last_checked_at` datetime DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_fund_transfer_task_key` (`task_key`),
  UNIQUE KEY `uk_fund_transfer_binance_client` (`binance_transfer_client_id`),
  UNIQUE KEY `uk_fund_transfer_binance_rollback_client` (`binance_rollback_client_id`),
  UNIQUE KEY `uk_fund_transfer_withdraw_order` (`binance_withdraw_order_id`),
  UNIQUE KEY `uk_fund_transfer_gate_client` (`gate_transfer_client_id`),
  UNIQUE KEY `uk_fund_transfer_active_slot` (`active_slot`),
  KEY `idx_fund_transfer_created` (`created_at`),
  KEY `idx_fund_transfer_status` (`status`,`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;
