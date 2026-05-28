source /Users/jeffrey/Documents/MyProject/Arbitrage-Mi/.venv/bin/activate

后端： 
python src/api/orderbook_server.py
python src/api/executor_service.py
前端：
cd frontend
npm run dev


# 关闭后端
kill $(lsof -t -i :19876)
kill $(lsof -t -i :8081)

# 关闭前端
kill $(lsof -t -i :5173)



永续合约：
GET /api/v4/futures/usdt/contracts - 永续合约信息，含基础信息和当期资金费率（Rest API）
GET /api/v4/futures/usdt/funding_rates - 批量查询合约历史资金费率（Rest API）
GET /api/v4/futures/usdt/tickers - 获取24小时成交量（Rest API）





.env key和db配置信息
src
｜—— exchange_api
    ｜—— get_binance_spot_info.py  获取现货基础信息
    ｜—— get_binance_spot_orderbook_ws.py   订阅现货盘口信息
    ｜—— get_binance_spot_tickers.py   获取现货24h成交量
    ｜—— get_gate_future_contracts.py   获取合约信息和当前资金费率
    ｜—— get_gate_future_his_funding_rate.py    增量获取合理历史资金费率
    ｜—— get_gate_future_tickers.py 获取合约24小时成交量
    ｜—— get_gate_future_orderbook.py 通过rest逐个合约获得盘口订单簿
    ｜—— get_gate_future_orderbook_update_ws.py 通过ws动态盘口的更新报价信息
｜—— calc
    ｜—— calculate_funding_rate_threshold.py 计算各个交易对的历史正资金费率的30分位数到表mi_gate_future_funding_rate_threshold
    ｜—— calculate_hedge_metrics.py    对齐现货和合约的交易数量，计算VWAP、盘口深度
    ｜—— calculate_vwap_basis_threshold.py    计算VWAP基差分位阈值，进入表mi_vwap_basis_threshold
    ｜—— create_binance_spot_local_orderbook.py    构建现货本地订单簿
    ｜—— create_gate_futures_local_orderbook.py    构建永续合约本地订单簿
    ｜—— etl_pipeline.py    ETL 数据管道 - 统一调度所有数据采集、计算与清理任务
    ｜—— executor_client.py    成交引擎 HTTP 客户端
    ｜—— merge_cross_exchange_orderbook.py    合并现货本地订单簿和永续合约本地订单簿
    ｜—— orderbook_enricher.py    行情数据富化模块，为合并后的订单簿行注入元数据和计算字段，供 WS 快照推送与开仓检查共用
    ｜—— position_pnl_calculator.py    计算持仓实时盈亏（浮动盈亏、已实现盈亏、总盈亏），与推送逻辑解耦
    ｜—— position_tracker.py    持仓创建、资金费累加、盈亏计算，持仓记录进入表mi_trade_position
    ｜—— service_lifecycle.py    WS 服务生命周期管理器
    ｜—— trading_executor.py    开仓判断 + 订单生成 + 持久化，交易记录进入表mi_trade_order
    ｜—— update_binance_spot_info.py    更新 Binance 现货交易对数据到表mi_binance_spot_info
    ｜—— update_gate_future_contracts.py    定期将合约信息和当前资金费率更新到表mi_gate_future_contracts
    ｜—— update_gate_future_his_funding_rate.py 增量将历史资金费率更新到表mi_gate_future_his_funding_rates
    ｜—— virtual_executor.py 独立的成交模拟服务，基于订单簿深度数据计算 VWAP 成交价并返回
    ｜—— vwap_snapshot_recorder.py 定时采样 VWAP 基差数据并批量落库，用于后续历史分位统计，表mi_vwap_basis_snapshot

｜—— api
    ｜—— executor_service.py   成交引擎 HTTP 服务（虚拟成交）
    ｜—— orderbook_server.py   将后端构建的实时盘口通过ws推送给vue前端
    ｜—— trading_api.py   交易API路由模块，订单查询，持仓查询，持仓汇总统计
｜—— config
    ｜—— config.yaml    通用的参数配置文件
｜—— log
｜—— common
    ｜—— tools.py   一些可被多次调用的函数
    ｜—— database.py   mysql数据库连接
    ｜—— logger.py   通用日志记录工具
    ｜—— config.py   通用配置读取工具
    ｜—— meta_loader.py   从数据库加载合约元数据和现货元数据，供 orderbook_server 和 executor_service 等多个服务共用



