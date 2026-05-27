# 套利系统交易模块设计文档

> 本文档描述了开仓模块、虚拟成交模块、持仓模块的架构设计、数据模型和业务流程。

---

## 概述

本系统实现跨交易所套利交易的完整生命周期管理：
1. **开仓模块** - 基于风控规则自动判断并生成市价订单
2. **虚拟成交模块** - 模拟订单成交过程,返回基于盘口深度的VWAP成交价
3. **持仓模块** - 追踪持仓状态、计算盈亏、监控资金费率和价差损益

### 核心设计原则
- **自动执行**: 开仓条件满足后自动生成订单并调用虚拟成交
- **防重复开仓**: 同一合约1小时内不重复开仓
- **逐笔追踪**: 每次开仓生成独立记录,支持精细化分析
- **虚实分离**: 订单生成与成交解耦,便于后续接入真实交易所API

---

## 一、数据库设计

### 1.1 订单表 (mi_trade_order)

记录开仓/平仓订单的完整信息,现货和期货各自为一条记录。

```sql
CREATE TABLE mi_trade_order (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '订单ID',
    order_uuid VARCHAR(36) NOT NULL COMMENT '订单组UUID(同一批次现货+期货共用)',
    position_id BIGINT NULL COMMENT '关联持仓ID，4笔订单共享同一个 position_id',
    base_asset VARCHAR(20) NOT NULL COMMENT '标的资产(如BTC)',
    
    -- 交易对标识
    spot_symbol VARCHAR(30) NULL COMMENT '现货交易对(如BTCUSDT)',
    future_contract VARCHAR(30) NULL COMMENT '期货合约名(如BTC_USDT)',
    
    -- 订单类型
    order_side ENUM('open', 'close') NOT NULL COMMENT '订单方向: open=开仓, close=平仓',
    market_type ENUM('spot', 'future') NOT NULL COMMENT '市场类型: spot=现货, future=期货',
    trade_direction ENUM('buy', 'sell') NOT NULL COMMENT '交易方向: buy=买入, sell=卖出',
    
    -- 订单状态
    status ENUM('pending', 'executed', 'rejected', 'failed') NOT NULL DEFAULT 'pending' COMMENT '订单状态: pending=待执行, executed=已成交, rejected=已拒单, failed=失败',
    channel ENUM('Mock', 'SimTrade', 'Live') NOT NULL DEFAULT 'Mock' COMMENT '渠道: Mock=模拟成交, SimTrade=模拟盘, Live=实盘',
    reject_reason VARCHAR(255) NULL COMMENT '拒单原因',
    
    -- 订单参数(标的资产数量)
    target_qty DECIMAL(20,8) NOT NULL COMMENT '目标数量(标的资产)',
    target_amount DECIMAL(20,2) NOT NULL COMMENT '目标金额(USDT)',
    
    -- 虚拟成交结果
    exec_price DECIMAL(20,8) NULL COMMENT '成交VWAP价格(已按交易所精度格式化)',
    exec_qty DECIMAL(20,8) NULL COMMENT '实际成交数量(已按交易所精度格式化)',
    exec_amount DECIMAL(20,2) NULL COMMENT '实际成交金额(USDT)',
    
    -- 盘口深度信息
    coverage_ratio DECIMAL(10,4) NULL COMMENT '盘口覆盖率(>1表示5档不足)',
    
    -- 风控指标(开仓时记录)
    open_coverage DECIMAL(10,4) NULL COMMENT '开仓盘口覆盖',
    open_marginal_basis_bps DECIMAL(10,2) NULL COMMENT '开仓边际基差(bps)',
    funding_rate_24h DECIMAL(10,6) NULL COMMENT '开仓时24h资金费率',
    
    -- 时间戳
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '订单创建时间',
    executed_at DATETIME NULL COMMENT '成交时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_order_uuid (order_uuid),
    INDEX idx_position_id (position_id),
    INDEX idx_base_asset (base_asset),
    INDEX idx_status (status),
    INDEX idx_channel (channel),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='交易订单表';
```

#### 字段说明

**订单组关系**:
- `order_uuid`: 同一批次开仓的现货和期货订单共用同一个UUID
- 现货订单: `spot_symbol` 有值, `future_contract` 为NULL
- 期货订单: `future_contract` 有值, `spot_symbol` 为NULL
- 开仓时: 现货(buy) + 期货(sell) 配对
- 平仓时: 现货(sell) + 期货(buy) 配对

**交易对标识映射**:

| 字段 | Binance现货 | Gate期货 |
|------|-------------|----------|
| spot_symbol | `BTCUSDT` | NULL |
| future_contract | NULL | `BTC_USDT` |
| target_qty | 标的资产数量 | 标的资产数量(非张数) |

**精度处理说明**:
- `exec_price` 和 `exec_qty` 在虚拟成交时已按交易所规则格式化
- Binance现货: 价格2位小数,数量由step_size推导
- Gate期货: 价格/数量由合约的price_decimal/size_decimal定义
- 精度规则从元数据表动态获取,不需要存储

**订单状态流转**:
```
pending → simulated (虚拟成交成功)
pending → rejected (盘口不足被拒)
pending → failed (系统异常)
```

### 1.2 持仓表 (mi_trade_position)

合并现货和合约为一条记录,追踪价差和资金费率损益。

```sql
CREATE TABLE mi_trade_position (
    id BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '持仓ID',
    order_uuid VARCHAR(36) NOT NULL COMMENT '关联开仓订单组UUID',
    close_order_uuid VARCHAR(36) NULL COMMENT '关联平仓订单组UUID',
    base_asset VARCHAR(20) NOT NULL COMMENT '标的资产',
    
    -- 交易对标识
    spot_symbol VARCHAR(30) NOT NULL COMMENT '现货交易对(如BTCUSDT)',
    future_contract VARCHAR(30) NOT NULL COMMENT '期货合约名(如BTC_USDT)',
    
    -- 持仓状态
    status ENUM('holding', 'closed') NOT NULL DEFAULT 'holding' COMMENT '持仓状态',
    opened_at DATETIME NOT NULL COMMENT '开仓时间',
    closed_at DATETIME NULL COMMENT '平仓时间',
    
    -- 开仓成本(从订单表聚合)
    spot_open_qty DECIMAL(20,8) NOT NULL COMMENT '现货开仓数量',
    spot_open_price DECIMAL(20,8) NOT NULL COMMENT '现货开仓VWAP',
    spot_open_amount DECIMAL(20,2) NOT NULL COMMENT '现货开仓金额',
    future_open_qty DECIMAL(20,8) NOT NULL COMMENT '期货开仓数量(标的资产)',
    future_open_price DECIMAL(20,8) NOT NULL COMMENT '期货开仓VWAP',
    future_open_contracts INT NOT NULL COMMENT '期货开仓张数',
    
    -- 初始价差
    open_spread_bps DECIMAL(10,2) NOT NULL COMMENT '开仓时价差(bps) = (future-spot)/spot*10000',
    
    -- 资金费率收益(累加)
    funding_rate_sum_bps DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '累计资金费率(bps) = 24h费率*10000累加',
    funding_payments_count INT NOT NULL DEFAULT 0 COMMENT '已结算资金费次数',
    funding_total_pnl DECIMAL(20,4) NOT NULL DEFAULT 0 COMMENT '累计资金费收益(USDT)',
    next_funding_time DATETIME NULL COMMENT '下次资金费结算时间',
    
    -- 实现盈亏(平仓后填写)
    spot_close_price DECIMAL(20,8) NULL COMMENT '现货平仓VWAP',
    future_close_price DECIMAL(20,8) NULL COMMENT '期货平仓VWAP',
    close_spread_bps DECIMAL(10,2) NULL COMMENT '平仓时价差(bps) = (future_close-spot_close)/spot_close*10000',
    realized_pnl_spot DECIMAL(20,4) NULL COMMENT '现货实现盈亏',
    realized_pnl_future DECIMAL(20,4) NULL COMMENT '期货实现盈亏',
    realized_pnl_total DECIMAL(20,4) NULL COMMENT '总实现盈亏',
    
    -- 总盈亏
    total_pnl DECIMAL(20,4) NULL COMMENT '总盈亏 = realized_pnl_total + funding_total_pnl',
    total_pnl_bps DECIMAL(10,2) NULL COMMENT '总盈亏(bps)',
    
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_status (status),
    INDEX idx_base_asset (base_asset),
    INDEX idx_opened_at (opened_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='持仓表';
```

#### 字段说明

**交易对标识**:
- `spot_symbol`: 现货交易对(如 `BTCUSDT`),来自订单表
- `future_contract`: 期货合约名(如 `BTC_USDT`),来自订单表
- 两者都有值,因为持仓是现货+期货的合并记录

**价差说明**:
- `open_spread_bps`: 开仓时价差 `(future_open - spot_open) / spot_open * 10000`
- `close_spread_bps`: 平仓时价差 `(future_close - spot_close) / spot_close * 10000`
- 价差反映现货与期货的价格偏离程度

**资金费率累加**:
- `funding_rate_sum_bps`: 累计资金费率(bps),每次结算时累加 `funding_rate_24h * 10000`
- 例如: 0.0003 * 10000 = 3.0 bps, 3次结算后累计 9.0 bps

**实时数据(不存储)**:
- 当前价格、浮动盈亏等通过WebSocket实时计算并推送前端
- 数据库中只存储**静态数据**(开仓价、平仓价、累计资金费)

**盈亏计算逻辑**:

```python
# 开仓时计算
open_spread_bps = (future_open_price - spot_open_price) / spot_open_price * 10000

# 资金费结算时累加
funding_rate_sum_bps += funding_rate_24h * 10000  # 累加bps
funding_total_pnl += funding_rate_24h * future_open_qty * future_open_price

# 平仓时计算
close_spread_bps = (future_close_price - spot_close_price) / spot_close_price * 10000

# 现货实现盈亏 = (平仓价 - 开仓价) * 数量 - 开仓手续费 - 平仓手续费
spot_open_fee = spot_open_price * spot_open_qty * SPOT_OPEN_FEE
spot_close_fee = spot_close_price * spot_open_qty * SPOT_CLOSE_FEE
realized_pnl_spot = (spot_close_price - spot_open_price) * spot_open_qty - spot_open_fee - spot_close_fee

# 期货实现盈亏 = (开仓价 - 平仓价) * 数量 - 开仓手续费 - 平仓手续费
# 期货做空: 开仓卖出, 平仓买入
future_open_fee = future_open_price * future_open_qty * FUTURE_OPEN_FEE
future_close_fee = future_close_price * future_open_qty * FUTURE_CLOSE_FEE
realized_pnl_future = (future_open_price - future_close_price) * future_open_qty - future_open_fee - future_close_fee

# 总实现盈亏 = 现货盈亏 + 期货盈亏
realized_pnl_total = realized_pnl_spot + realized_pnl_future

# 总盈亏 = 实现盈亏 + 资金费收益
total_pnl = realized_pnl_total + funding_total_pnl

# 总盈亏bps (相对于开仓名义价值)
total_pnl_bps = total_pnl / (spot_open_price * spot_open_qty) * 10000
```

**前端实时计算(不存储)**:
```python
# 以下数据由后端WebSocket实时推送,不持久化
current_spot_price = 从orderbook获取
current_future_price = 从orderbook获取
current_spread_bps = (current_future - current_spot) / current_spot * 10000

# 浮动盈亏(不含手续费,因为还未实际平仓)
floating_pnl_spot = (current_spot - spot_open_price) * spot_open_qty
floating_pnl_future = (future_open_price - current_future) * future_open_qty
floating_pnl_total = floating_pnl_spot + floating_pnl_future
floating_pnl_bps = current_spread_bps - open_spread_bps

# 注意: 浮动盈亏不包含手续费,只有平仓时才扣除手续费
# 预估平仓盈亏(含手续费) 可以在前端展示时计算
estimated_close_fee_spot = current_spot * spot_open_qty * (SPOT_OPEN_FEE + SPOT_CLOSE_FEE)
estimated_close_fee_future = current_future * future_open_qty * (FUTURE_OPEN_FEE + FUTURE_CLOSE_FEE)
estimated_net_pnl = floating_pnl_total - estimated_close_fee_spot - estimated_close_fee_future
```



## 二、模块架构设计

### 2.1 模块架构

**现有后端架构分析**:
```
src/
├── api/                    # API服务层
│   └── orderbook_server.py    # FastAPI服务,WebSocket推送
├── calc/                   # 计算模块
│   ├── calculate_hedge_metrics.py    # 对冲指标计算
│   ├── create_*_local_orderbook.py   # 本地订单簿构建
│   ├── merge_cross_exchange_orderbook.py  # 订单簿合并
│   └── update_*.py                  # 数据更新ETL
├── exchange_apis/          # 交易所API层
│   ├── get_binance_*.py           # Binance现货API
│   └── get_gate_future_*.py       # Gate期货API
├── common/                 # 公共工具层
│   ├── config.py              # 配置读取
│   ├── database.py            # 数据库连接
│   ├── logger.py              # 日志工具
│   └── tools.py               # 通用工具函数(API签名、时间戳等)
├── config/                 # 配置文件
│   └── config.yaml
└── log/                    # 日志目录
```

**新增交易模块** (复用现有架构,不创建新目录):
```
src/
├── api/                    # API服务层
│   ├── orderbook_server.py    # 现有服务
│   └── trading_api.py         # 新增: 交易API路由(订单查询、持仓查询)
├── calc/                   # 计算模块
│   ├── trading_executor.py    # 新增: 开仓判断+虚拟成交
│   └── position_tracker.py    # 新增: 持仓管理+盈亏计算
├── common/                 # 公共工具层
│   └── tools.py               # 扩展现有文件,新增交易工具函数
└── config/
    └── config.yaml            # 扩展现有配置,新增交易参数
```

**设计原则**:
1. ✅ **复用现有目录结构**,不创建 `src/trading/` 新目录
2. ✅ **工具函数合并到 `common/tools.py`**,不单独创建 `utils.py`
3. ✅ **计算逻辑放在 `calc/`**,与 `calculate_hedge_metrics.py` 同级
4. ✅ **API路由放在 `api/`**,与 `orderbook_server.py` 同级
5. ✅ **配置合并到 `config.yaml`**,不创建新配置文件

### 2.2 开仓判断流程

```python
# src/calc/trading_executor.py

class TradingExecutor:
    """交易执行器(开仓判断+虚拟成交)"""
    
    def __init__(self):
        from common.database import db_manager
        from common.config import config
        from common.tools import format_price_precision, format_qty_precision
        from calc.position_tracker import PositionTracker
        
        self.db = db_manager
        self.config = config
        self.position_tracker = PositionTracker()
        self.virtual_executor = VirtualExecutor()
    
    def check_and_open(self, orderbook_rows: List[Dict]) -> List[Dict]:
        """
        检查所有合约并执行开仓
        
        Args:
            orderbook_rows: 合并后的订单簿行(来自orderbook_server)
        
        Returns:
            开仓结果列表
        """
        results = []
        
        for row in orderbook_rows:
            try:
                # 1. 风控检查
                if not self._pass_risk_check(row):
                    continue
                
                # 2. 冷却检查(查询订单表)
                if not self._pass_cooldown_check(row['base_asset']):
                    continue
                
                # 3. 生成订单
                order_group = self._create_order_group(row)
                
                # 4. 虚拟成交(传入当前盘口数据)
                exec_result = self.virtual_executor.execute(order_group, row)
                
                # 5. 持久化订单
                self._save_orders(order_group, exec_result)
                
                # 6. 创建持仓记录
                if exec_result['success']:
                    self.position_tracker.create_position(order_group, exec_result)
                
                results.append({
                    'base_asset': row['base_asset'],
                    'success': exec_result['success'],
                    'message': exec_result.get('message')
                })
                
            except Exception as e:
                logger.error(f"开仓失败 {row['base_asset']}: {e}")
                results.append({
                    'base_asset': row['base_asset'],
                    'success': False,
                    'message': str(e)
                })
        
        return results
    
    def _pass_risk_check(self, row: Dict) -> bool:
        """
        风控规则检查(与前端逻辑一致)
        
        1. 资金费率 >= 阈值
        2. 开仓盘口覆盖 <= 阈值
        3. 开仓边际基差 >= 阈值
        """
        # 资金费率检查
        funding_rate = row.get('funding_rate_24h')
        threshold = row.get(FUNDING_THRESHOLD_PERCENTILE)
        if funding_rate is None or threshold is None:
            return True  # 数据为空时通过
        if funding_rate < threshold:
            return False
        
        # 盘口覆盖检查
        open_coverage = row.get('open_coverage')
        coverage_threshold = ORDERBOOK_COVERAGE_THRESHOLD
        if open_coverage is None:
            return True
        if open_coverage > coverage_threshold:
            return False
        
        # 边际基差检查
        marginal_basis = row.get('open_marginal_basis_bps')
        basis_threshold = OPEN_MARGINAL_BASIS_THRESHOLD_BPS
        if marginal_basis is None:
            return True
        if marginal_basis < basis_threshold:
            return False
        
        return True
    
    def _pass_cooldown_check(self, base_asset: str) -> bool:
        """检查1小时冷却期(从订单表查询)"""
        sql = """
            SELECT MAX(created_at) as last_open_time 
            FROM mi_trade_order 
            WHERE base_asset = %s 
              AND market_type = 'spot' 
              AND order_side = 'open' 
              AND status = 'simulated'
        """
        with self.db.get_cursor() as cursor:
            cursor.execute(sql, (base_asset,))
            row = cursor.fetchone()
            
            if not row or not row['last_open_time']:
                return True  # 无开仓记录
            
            last_time = row['last_open_time']
            elapsed = (datetime.now() - last_time).total_seconds()
            return elapsed >= 3600  # 1小时
    
    def _create_order_group(self, row: Dict) -> Dict:
        """
        生成订单组(现货+期货)
        
        Returns:
            {
                'order_uuid': 'xxx-xxx-xxx',
                'base_asset': 'BTC',
                'contract': 'BTC_USDT',
                'spot_order': {...},
                'future_order': {...}
            }
        """
        import uuid
        
        order_uuid = str(uuid.uuid4())
        base_asset = row['base_asset']
        contract = row['contract']
        
        # 从对冲指标获取数量
        target_qty = row['spot_qty']  # 已对齐的对冲数量
        target_amount = row.get('open_amount_usdt', OPEN_AMOUNT_USDT)
        
        # 获取精度配置
        spot_precision = self._get_spot_precision(base_asset)
        future_precision = self._get_future_precision(base_asset)
        
        # 现货订单(Binance格式)
        spot_order = {
            'order_uuid': order_uuid,
            'base_asset': base_asset,
            'spot_symbol': f"{base_asset}USDT",
            'future_contract': None,
            'order_side': 'open',
            'market_type': 'spot',
            'trade_direction': 'buy',  # 开仓: 现货买入
            'status': 'pending',
            'target_qty': target_qty,
            'target_amount': target_amount,
            # 风控指标(从order_group传递)
            'open_coverage': None,  # 会在order_group层级设置
            'open_marginal_basis_bps': None,
            'funding_rate_24h': None,
            # Binance现货市价单格式
            'exchange_params': {
                'symbol': f"{base_asset}USDT",
                'side': 'BUY',
                'type': 'MARKET',
                'quantity': self._format_qty(target_qty, base_asset, 'spot'),
                'newClientOrderId': f"arb_{order_uuid[:8]}_spot"
            }
        }
        
        # 期货订单(Gate格式)
        future_order = {
            'order_uuid': order_uuid,
            'base_asset': base_asset,
            'spot_symbol': None,
            'future_contract': contract,
            'order_side': 'open',
            'market_type': 'future',
            'trade_direction': 'sell',  # 开仓: 期货做空
            'status': 'pending',
            'target_qty': target_qty,
            'target_amount': target_amount,
            # 风控指标(从order_group传递)
            'open_coverage': None,
            'open_marginal_basis_bps': None,
            'funding_rate_24h': None,
            # Gate期货市价单格式
            'exchange_params': {
                'contract': contract,
                'size': self._qty_to_contracts(target_qty, base_asset),
                'price': '0',  # 市价单
                'tif': 'ioc',
                'text': f"arb_{order_uuid[:8]}_future"
            }
        }
        
        return {
            'order_uuid': order_uuid,
            'base_asset': base_asset,
            'spot_symbol': f"{base_asset}USDT",
            'future_contract': contract,
            'spot_order': spot_order,
            'future_order': future_order,
            # 风控指标(开仓时记录,写入订单表)
            'open_coverage': row.get('open_coverage'),
            'open_marginal_basis_bps': row.get('open_marginal_basis_bps'),
            'funding_rate_24h': row.get('funding_rate_24h')
        }
    
    def _save_orders(self, order_group: Dict, exec_result: Dict):
        """持久化订单到数据库"""
        sql = """
            INSERT INTO mi_trade_order (
                order_uuid, base_asset, spot_symbol, future_contract, order_side, market_type,
                trade_direction, status, reject_reason, target_qty, target_amount,
                exec_price, exec_qty, exec_amount, coverage_ratio,
                open_coverage, open_marginal_basis_bps, funding_rate_24h
            ) VALUES (
                %(order_uuid)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                %(order_side)s, %(market_type)s, %(trade_direction)s, %(status)s,
                %(reject_reason)s, %(target_qty)s, %(target_amount)s,
                %(exec_price)s, %(exec_qty)s, %(exec_amount)s, %(coverage_ratio)s,
                %(open_coverage)s, %(open_marginal_basis_bps)s, %(funding_rate_24h)s
            )
        """
        
        for market_type in ['spot_order', 'future_order']:
            order = order_group[market_type]
            
            # 注入风控指标(从order_group)
            order['open_coverage'] = order_group.get('open_coverage')
            order['open_marginal_basis_bps'] = order_group.get('open_marginal_basis_bps')
            order['funding_rate_24h'] = order_group.get('funding_rate_24h')
            
            # 更新订单状态和成交信息
            if exec_result['success']:
                exec_data = exec_result[market_type]
                order.update({
                    'status': 'simulated',
                    'exec_price': exec_data['exec_price'],
                    'exec_qty': exec_data['exec_qty'],
                    'exec_amount': exec_data['exec_amount'],
                    'reject_reason': None,
                    'coverage_ratio': exec_data.get('coverage_ratio')
                })
            else:
                order.update({
                    'status': 'rejected',
                    'reject_reason': exec_result.get('message'),
                    'exec_price': None,
                    'exec_qty': None,
                    'exec_amount': None,
                    'coverage_ratio': None
                })
            
            with self.db.get_cursor() as cursor:
                cursor.execute(sql, order)
```

### 2.3 定时调度器

在 `orderbook_server.py` 的 lifespan 中增加开仓检查定时任务:

```python
async def _open_position_loop():
    """定时检查开仓条件"""
    interval = config.get_int('trade.open_check_interval_sec', 5)
    executor = TradingExecutor()
    
    while True:
        try:
            await asyncio.sleep(interval)
            
            # 获取最新订单簿
            if gate_manager and spot_manager:
                future_rows = gate_manager.to_records()
                spot_rows = spot_manager.to_records()
                merged_rows = merge_orderbook_records(future_rows, spot_rows)
                merged_rows = calculate_hedge_metrics(
                    merged_rows, _contract_meta, _spot_meta, OPEN_AMOUNT_USDT
                )
                
                # 执行开仓检查
                results = executor.check_and_open(merged_rows)
                
                # 推送开仓结果到前端(通过WebSocket)
                if any(r['success'] for r in results):
                    payload = {
                        'type': 'open_position_result',
                        'results': results,
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    asyncio.run_coroutine_threadsafe(
                        broadcast_queue.put(payload), event_loop
                    )
                    
        except Exception as e:
            logger.error(f"开仓检查失败: {e}")
```

### 2.4 冷却检查机制

**实现方式**: 通过查询 `mi_trade_order` 表获取最近一次成功开仓时间

**查询逻辑**:
```sql
SELECT MAX(created_at) as last_open_time 
FROM mi_trade_order 
WHERE base_asset = %s 
  AND market_type = 'spot'        -- 查现货订单即可
  AND order_side = 'open'         -- 开仓订单
  AND status = 'simulated'        -- 成功成交的订单
```

**优点**:
- ✅ 不需要单独的冷却表,减少表数量
- ✅ 数据一致性更好(订单表是唯一数据源)
- ✅ 可以追溯完整的开仓历史
- ✅ 查询效率高(created_at字段有索引)

---

## 三、虚拟成交模块设计

### 3.1 核心逻辑

`VirtualExecutor` 作为独立类，由 `TradingExecutor` 内部实例化调用，保持"虚实分离"原则。
盘口数据通过参数 `orderbook_row` 传入（即合并后的订单簿行），不存储快照。

```python
# src/calc/trading_executor.py (VirtualExecutor 部分)

class VirtualExecutor:
    """虚拟成交引擎(后续可替换为真实交易所执行器)"""
    
    def __init__(self):
        self.max_levels = 5  # 最多判断5档
    
    def execute(self, order_group: Dict, orderbook_row: Dict) -> Dict:
        """
        执行虚拟成交
        
        Args:
            order_group: 订单组(包含现货和期货订单)
            orderbook_row: 当前盘口数据行(合并后的订单簿,含5档深度)
        
        Returns:
            {
                'success': bool,
                'message': str,
                'spot_order': {exec_price, exec_qty, exec_amount, coverage_ratio},
                'future_order': {exec_price, exec_qty, exec_amount, coverage_ratio}
            }
        """
        result = {
            'success': False,
            'spot_order': None,
            'future_order': None,
            'message': ''
        }
        
        try:
            # 1. 现货成交计算
            spot_result = self._calc_vwap(
                order_group['spot_order'],
                orderbook_row,
                'spot'
            )
            
            if not spot_result['success']:
                result['message'] = f"现货拒单: {spot_result['reason']}"
                return result
            
            result['spot_order'] = spot_result
            
            # 2. 期货成交计算
            future_result = self._calc_vwap(
                order_group['future_order'],
                orderbook_row,
                'future'
            )
            
            if not future_result['success']:
                result['message'] = f"期货拒单: {future_result['reason']}"
                return result
            
            result['future_order'] = future_result
            result['success'] = True
            result['message'] = '成交成功'
            
        except Exception as e:
            result['message'] = f"系统异常: {str(e)}"
        
        return result
    
    def _calc_vwap(self, order: Dict, orderbook: Dict, market_type: str) -> Dict:
        """
        计算VWAP成交价
        
        Returns:
            {
                'success': bool,
                'exec_price': float,
                'exec_qty': float,
                'exec_amount': float,
                'coverage_ratio': float,
                'reason': str (失败时)
            }
        """
        target_qty = order['target_qty']
        prefix = 'spot' if market_type == 'spot' else 'future'
        base_asset = order['base_asset']
        
        # 确定交易方向对应的盘口侧
        # 开仓: 现货buy用ask侧, 期货sell用bid侧
        if order['trade_direction'] == 'buy':
            side = 'ask'  # 买入看卖盘
        else:
            side = 'bid'  # 卖出看买盘
        
        # 提取5档盘口
        prices = []
        volumes = []
        for i in range(1, self.max_levels + 1):
            price = orderbook.get(f'{prefix}_price_{side}_{i}')
            volume = orderbook.get(f'{prefix}_volume_{side}_{i}')
            if price is not None and volume is not None:
                prices.append(float(price))
                volumes.append(float(volume))
        
        if not prices:
            return {
                'success': False,
                'reason': '盘口数据为空'
            }
        
        # 期货需要乘以quanto_multiplier
        qty_multiplier = 1.0
        if market_type == 'future':
            qty_multiplier = self._get_quanto_multiplier(base_asset)
        
        # 计算5档总流动性
        total_liquidity = 0
        for vol in volumes:
            total_liquidity += vol * qty_multiplier
        
        # 检查流动性是否充足
        if total_liquidity < target_qty:
            coverage_ratio = target_qty / total_liquidity if total_liquidity > 0 else float('inf')
            return {
                'success': False,
                'reason': f'盘口深度不足(覆盖率{coverage_ratio:.2f})',
                'coverage_ratio': coverage_ratio
            }
        
        # 计算VWAP
        total_cost = 0.0
        total_filled = 0.0
        remaining = target_qty
        
        for price, vol in zip(prices, volumes):
            if remaining <= 0:
                break
            
            fill_qty = min(vol * qty_multiplier, remaining)
            total_cost += price * fill_qty
            total_filled += fill_qty
            remaining -= fill_qty
        
        exec_price = total_cost / total_filled if total_filled > 0 else None
        
        if exec_price is None:
            return {
                'success': False,
                'reason': 'VWAP计算失败'
            }
        
        # 按交易所规则保留精度(动态获取)
        price_precision = self._get_price_precision(base_asset, market_type)
        qty_precision = self._get_qty_precision(base_asset, market_type)
        
        exec_price = round(exec_price, price_precision)
        exec_qty = round(total_filled, qty_precision)
        exec_amount = round(exec_price * exec_qty, 2)
        
        # 计算实际覆盖率
        coverage_ratio = target_qty / total_liquidity if total_liquidity > 0 else 0
        
        return {
            'success': True,
            'exec_price': exec_price,
            'exec_qty': exec_qty,
            'exec_amount': exec_amount,
            'coverage_ratio': coverage_ratio
        }
    
    def _get_price_precision(self, base_asset: str, market_type: str) -> int:
        """获取价格精度(从元数据动态获取)"""
        from api.orderbook_server import _contract_meta, _spot_meta
        
        if market_type == 'spot':
            # Binance现货: 通常2位小数
            return 2
        else:
            # Gate期货: 从合约元数据获取
            if base_asset in _contract_meta:
                return _contract_meta[base_asset].get('price_decimal', 1)
            return 1
    
    def _get_qty_precision(self, base_asset: str, market_type: str) -> int:
        """获取数量精度(从元数据动态获取)"""
        from api.orderbook_server import _contract_meta, _spot_meta
        
        if market_type == 'spot':
            # Binance现货: 从step_size推导
            if base_asset in _spot_meta:
                step_size = _spot_meta[base_asset].get('step_size', 0.00001)
                # 0.00001 -> 5, 0.01 -> 2
                return len(str(step_size).split('.')[-1].rstrip('0'))
            return 5
        else:
            # Gate期货: 从合约元数据获取
            if base_asset in _contract_meta:
                return _contract_meta[base_asset].get('size_decimal', 0)
            return 0
    
    def _get_quanto_multiplier(self, base_asset: str) -> float:
        """获取合约面值"""
        from api.orderbook_server import _contract_meta
        if base_asset in _contract_meta:
            return _contract_meta[base_asset].get('quanto_multiplier', 1.0)
        return 1.0
```

### 3.2 拒单场景

| 场景 | 拒单原因 | 处理策略 |
|------|----------|----------|
| 5档盘口总量不足 | `盘口深度不足(覆盖率1.25)` | 记录订单状态为rejected,不创建持仓 |
| 盘口数据为空 | `盘口数据为空` | 记录订单状态为rejected |
| VWAP计算异常 | `VWAP计算失败` | 记录订单状态为failed |

---

## 四、持仓模块设计

### 4.1 持仓管理器

```python
# src/calc/position_tracker.py

class PositionTracker:
    """持仓管理器"""
    
    def __init__(self):
        self.db = db_manager
        self.contract_meta = {}
    
    def create_position(self, order_group: Dict, exec_result: Dict):
        """
        创建持仓记录(开仓成功后调用)
        
        Args:
            order_group: 订单组
            exec_result: 虚拟成交结果
        """
        spot_exec = exec_result['spot_order']
        future_exec = exec_result['future_order']
        
        # 计算开仓价差
        spot_price = spot_exec['exec_price']
        future_price = future_exec['exec_price']
        open_spread_bps = (future_price - spot_price) / spot_price * 10000
        
        sql = """
            INSERT INTO mi_trade_position (
                order_uuid, base_asset, spot_symbol, future_contract, status, opened_at,
                spot_open_qty, spot_open_price, spot_open_amount,
                future_open_qty, future_open_price, future_open_contracts,
                open_spread_bps
            ) VALUES (
                %(order_uuid)s, %(base_asset)s, %(spot_symbol)s, %(future_contract)s,
                'holding', NOW(),
                %(spot_open_qty)s, %(spot_open_price)s, %(spot_open_amount)s,
                %(future_open_qty)s, %(future_open_price)s, %(future_open_contracts)s,
                %(open_spread_bps)s
            )
        """
        
        # 计算期货张数
        quanto = self._get_quanto_multiplier(order_group['base_asset'])
        future_contracts = int(order_group['spot_order']['target_qty'] / quanto)
        
        params = {
            'order_uuid': order_group['order_uuid'],
            'base_asset': order_group['base_asset'],
            'spot_symbol': f"{order_group['base_asset']}USDT",
            'future_contract': order_group['future_contract'],
            'spot_open_qty': spot_exec['exec_qty'],
            'spot_open_price': spot_exec['exec_price'],
            'spot_open_amount': spot_exec['exec_amount'],
            'future_open_qty': future_exec['exec_qty'],
            'future_open_price': future_exec['exec_price'],
            'future_open_contracts': future_contracts,
            'open_spread_bps': open_spread_bps
        }
        
        with self.db.get_cursor() as cursor:
            cursor.execute(sql, params)
    
    def update_funding_pnl(self):
        """
        定时更新资金费收益(每8小时,资金费结算后)
        
        从mi_gate_future_contracts获取当前资金费率并累加
        """
        sql = """
            SELECT p.*, c.funding_rate_24h, c.funding_next_apply
            FROM mi_trade_position p
            LEFT JOIN mi_gate_future_contracts c ON p.future_contract = CONCAT(c.base_asset, '_USDT')
            WHERE p.status = 'holding'
        """
        
        with self.db.get_cursor() as cursor:
            cursor.execute(sql)
            positions = cursor.fetchall()
        
        for pos in positions:
            try:
                funding_rate_24h = pos.get('funding_rate_24h')
                if funding_rate_24h is None:
                    continue
                
                # 检查是否已过结算时间
                next_funding = pos.get('funding_next_apply')
                if next_funding and next_funding < datetime.now():
                    # 单次资金费收益 = 资金费率 * 期货名义价值
                    funding_pnl = funding_rate_24h * pos['future_open_qty'] * pos['future_open_price']
                    
                    # 累加资金费
                    update_sql = """
                        UPDATE mi_trade_position SET
                            funding_rate_sum_bps = funding_rate_sum_bps + %(funding_rate_24h_bps)s,
                            funding_payments_count = funding_payments_count + 1,
                            funding_total_pnl = funding_total_pnl + %(funding_pnl)s,
                            next_funding_time = %(next_funding)s
                        WHERE order_uuid = %(order_uuid)s
                    """
                    
                    with self.db.get_cursor() as cursor:
                        cursor.execute(update_sql, {
                            'funding_rate_24h_bps': funding_rate_24h * 10000,
                            'funding_pnl': funding_pnl,
                            'next_funding': next_funding + timedelta(hours=8),
                            'order_uuid': pos['order_uuid']
                        })
                    
                    logger.info(
                        f"资金费结算 | {pos['base_asset']} | "
                        f"rate={funding_rate_24h:.6f} | "
                        f"pnl={funding_pnl:.4f} | "
                        f"count={pos['funding_payments_count'] + 1}"
                    )
                
            except Exception as e:
                logger.error(f"更新资金费收益失败 {pos['order_uuid']}: {e}")
```

### 4.2 定时任务集成

在 `orderbook_server.py` 的 lifespan 中增加资金费更新任务:

```python
async def _position_funding_loop():
    """定时更新资金费收益"""
    position_mgr = PositionTracker()
    interval = config.get_int('trade.position_funding_update_sec', 28800)  # 8小时
    
    while True:
        try:
            await asyncio.sleep(interval)
            position_mgr.update_funding_pnl()
        except Exception as e:
            logger.error(f"资金费更新失败: {e}")

async def _position_realtime_push():
    """定时推送持仓实时数据(每10秒)"""
    interval = config.get_int('trade.position_price_update_sec', 10)
    
    while True:
        try:
            await asyncio.sleep(interval)
            
            # 获取所有持仓
            positions = get_holding_positions()
            
            # 计算实时数据(不存储)
            for pos in positions:
                current_spot = get_current_spot_price(pos['base_asset'])
                current_future = get_current_future_price(pos['base_asset'])
                
                pos['current_spot_price'] = current_spot
                pos['current_future_price'] = current_future
                pos['current_spread_bps'] = calc_spread_bps(current_spot, current_future)
                pos['floating_pnl_total'] = calc_floating_pnl(pos, current_spot, current_future)
                pos['floating_pnl_bps'] = pos['current_spread_bps'] - pos['open_spread_bps']
            
            # 推送前端
            if positions and event_loop and broadcast_queue:
                payload = {
                    'type': 'position_update',
                    'positions': positions,
                    'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                asyncio.run_coroutine_threadsafe(
                    broadcast_queue.put(payload), event_loop
                )
                
        except Exception as e:
            logger.error(f"持仓实时推送失败: {e}")

# 在lifespan中启动
asyncio.create_task(_position_funding_loop())
asyncio.create_task(_position_realtime_push())
```

---

## 五、前端订单管理页面设计

### 5.1 页面结构

```
frontend/src/views/
├── OrderBookMonitor.vue        # 现有盘口监控
├── OrderManagement.vue         # 订单管理页面(新增)
├── PositionMonitor.vue         # 持仓监控页面(新增)
└── orderbookTypes.ts           # 类型定义(扩展)
```

### 5.2 订单管理页面 (OrderManagement.vue)

**功能**:
- 订单列表展示(AG Grid)
- 按状态过滤(pending/simulated/rejected/failed)
- 按时间范围筛选
- 查看订单详情(覆盖率、风控指标)

**列定义**:
```typescript
const orderColumns = [
  { field: 'created_at', headerName: '下单时间', width: 180 },
  { field: 'base_asset', headerName: '标的资产', width: 100 },
  { field: 'market_type', headerName: '市场', width: 80 },
  { field: 'trade_direction', headerName: '方向', width: 80 },
  { field: 'status', headerName: '状态', width: 100 },
  { field: 'target_qty', headerName: '目标数量', width: 120 },
  { field: 'target_amount', headerName: '目标金额', width: 120 },
  { field: 'exec_price', headerName: '成交VWAP', width: 120 },
  { field: 'exec_qty', headerName: '成交数量', width: 120 },
  { field: 'exec_amount', headerName: '成交金额', width: 120 },
  { field: 'coverage_ratio', headerName: '盘口覆盖', width: 100 },
  { field: 'reject_reason', headerName: '拒单原因', width: 200 },
]
```

### 5.3 持仓监控页面 (PositionMonitor.vue)

**功能**:
- 持仓列表展示(AG Grid)
- 实时盈亏更新(WebSocket推送)
- 按状态过滤(holding/closed)
- 盈亏汇总统计

**列定义**:
```typescript
const positionColumns = [
  { field: 'opened_at', headerName: '开仓时间', width: 180 },
  { field: 'base_asset', headerName: '标的资产', width: 100 },
  { field: 'spot_symbol', headerName: '现货', width: 100 },
  { field: 'future_contract', headerName: '期货', width: 110 },
  { field: 'status', headerName: '状态', width: 80 },
  { field: 'spot_open_price', headerName: '现货开仓价', width: 120 },
  { field: 'future_open_price', headerName: '期货开仓价', width: 120 },
  { field: 'open_spread_bps', headerName: '开仓价差(bps)', width: 130 },
  { field: 'current_spot_price', headerName: '现货现价', width: 120 },
  { field: 'current_future_price', headerName: '期货现价', width: 120 },
  { field: 'current_spread_bps', headerName: '现价差(bps)', width: 130 },
  { field: 'floating_pnl_total', headerName: '浮动盈亏', width: 120 },
  { field: 'floating_pnl_bps', headerName: '浮动盈亏(bps)', width: 130 },
  { field: 'funding_total_pnl', headerName: '资金费收益', width: 120 },
  { field: 'funding_payments_count', headerName: '资金费次数', width: 120 },
  { field: 'total_pnl', headerName: '总盈亏', width: 120 },
]
```

### 5.4 后端API

```python
# src/api/trading_api.py (新增文件,复用orderbook_server的FastAPI实例)

@app.get('/api/trading/orders')
async def get_orders(
    status: Optional[str] = None,
    base_asset: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None
):
    """查询订单列表"""
    sql = "SELECT * FROM mi_trade_order WHERE 1=1"
    params = []
    
    if status:
        sql += " AND status = %s"
        params.append(status)
    if base_asset:
        sql += " AND base_asset = %s"
        params.append(base_asset)
    if start_time:
        sql += " AND created_at >= %s"
        params.append(start_time)
    if end_time:
        sql += " AND created_at <= %s"
        params.append(end_time)
    
    sql += " ORDER BY created_at DESC LIMIT 1000"
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


@app.get('/api/trading/positions')
async def get_positions(
    status: Optional[str] = None,
    base_asset: Optional[str] = None
):
    """查询持仓列表"""
    sql = "SELECT * FROM mi_trade_position WHERE 1=1"
    params = []
    
    if status:
        sql += " AND status = %s"
        params.append(status)
    if base_asset:
        sql += " AND base_asset = %s"
        params.append(base_asset)
    
    sql += " ORDER BY opened_at DESC"
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


@app.get('/api/trading/positions/summary')
async def get_positions_summary():
    """持仓汇总统计"""
    sql = """
        SELECT 
            COUNT(*) as total_positions,
            SUM(CASE WHEN status = 'holding' THEN 1 ELSE 0 END) as holding_count,
            SUM(CASE WHEN status = 'holding' THEN spot_open_amount ELSE 0 END) as total_holding_amount,
            SUM(funding_total_pnl) as total_funding_pnl,
            SUM(total_pnl) as total_pnl
        FROM mi_trade_position
    """
    
    with db_manager.get_cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchone()
```

---

## 六、配置项扩展

在 `src/config/config.yaml` 中新增:

```yaml
trade:
  # ... 现有配置 ...
  
  # 开仓检查间隔(秒)
  open_check_interval_sec: 5
  
  # 开仓冷却期(秒)
  open_cooldown_sec: 3600
  
  # 持仓价格更新间隔(秒)
  position_price_update_sec: 10
  
  # 资金费收益更新间隔(秒)
  position_funding_update_sec: 28800  # 8小时
```

---

## 七、数据流图

```
┌─────────────────────────────────────────────────────────────┐
│                     orderbook_server.py                      │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────────┐  │
│  │ Gate WS      │    │ Binance WS   │    │ 定时任务       │  │
│  │ 期货盘口      │    │ 现货盘口      │    │ - 开仓检查     │  │
│  └──────┬───────┘    └──────┬───────┘    │ - 价格推送     │  │
│         │                   │            │ - 资金费更新   │  │
│         └────────┬──────────┘            └───────┬───────┘  │
│                  │                               │          │
│            ┌─────▼──────┐                  ┌─────▼────────┐ │
│            │ merge &    │                  │TradingExecutor│ │
│            │ calculate  │─────────────────▶│ 风控+开仓判断  │ │
│            └─────┬──────┘                  └─────┬────────┘ │
│                  │                               │          │
│                  │                    ┌──────────▼───────┐  │
│                  │                    │ VirtualExecutor  │  │
│                  │                    │ VWAP虚拟成交     │  │
│                  │                    └──────────┬───────┘  │
│                  │                               │          │
│                  │                    ┌──────────▼───────┐  │
│                  │                    │ PositionTracker  │  │
│                  │                    │ 创建持仓/更新盈亏 │  │
│                  │                    └──────────┬───────┘  │
│                  │                               │          │
└──────────────────┼───────────────────────────────┼──────────┘
                   │                               │
                   │                               ▼
              ┌────▼─────┐              ┌───────────────────────┐
              │ WebSocket │              │ MySQL数据库            │
              │ 推送前端   │              │ - mi_trade_order      │
              └────┬──────┘              │ - mi_trade_position   │
                   │                     └───────────────────────┘
                   ▼
            ┌──────────────┐
            │ 前端Vue页面   │
            │ - 盘口监控    │
            │ - 订单管理    │
            │ - 持仓监控    │
            └──────────────┘
```

---



## 八、错误处理与日志

### 8.1 异常分类

| 异常类型 | 处理方式 | 日志级别 |
|---------|---------|---------|
| 风控不通过 | 跳过,不记录 | DEBUG |
| 冷却期未到 | 跳过,不记录 | DEBUG |
| 盘口不足拒单 | 记录订单status=rejected | INFO |
| VWAP计算失败 | 记录订单status=failed | ERROR |
| 数据库异常 | 回滚,记录日志 | ERROR |
| 系统异常 | 跳过该合约,继续处理其他 | ERROR |

### 8.2 日志格式

```python
logger.info(
    f"开仓成功 | {base_asset} | "
    f"spot_vwap={spot_exec['exec_price']} | "
    f"future_vwap={future_exec['exec_price']} | "
    f"spread_bps={open_spread_bps:.2f}"
)

logger.warning(
    f"开仓拒单 | {base_asset} | "
    f"reason={result['message']} | "
    f"funding={funding_rate:.4f} | "
    f"coverage={open_coverage:.2f}"
)
```

---

## 九、后续扩展

### 9.1 平仓模块(预留)

未来接入平仓信号监控后,新增:
- `ClosePositionManager`: 平仓判断与订单生成
- 平仓风控规则(与开仓类似但方向相反)
- 平仓后自动更新持仓状态和实现盈亏

### 9.2 真实交易所接入

虚拟成交模块设计为独立层,后续可替换为:
- `BinanceExecutor`: 调用Binance API下单
- `GateExecutor`: 调用Gate API下单
- 订单状态轮询与成交回报处理

### 9.3 风险控制增强

- 最大持仓数量限制
- 单日开仓次数限制
- 异常波动自动平仓
- 资金费率异常监控

---

## 十、测试策略

### 10.1 单元测试

```python
# tests/test_virtual_execution.py

def test_vwap_calculation_sufficient_liquidity():
    """测试流动性充足时的VWAP计算"""
    executor = VirtualExecutor()
    order = {
        'target_qty': 0.5,
        'trade_direction': 'buy',
        'base_asset': 'BTC'
    }
    orderbook_row = {
        'spot_price_ask_1': 50000,
        'spot_volume_ask_1': 0.3,
        'spot_price_ask_2': 50100,
        'spot_volume_ask_2': 0.4
    }
    
    result = executor._calc_vwap(order, orderbook_row, 'spot')
    assert result['success'] == True
    assert result['exec_price'] == 50042.86  # (50000*0.3 + 50100*0.2) / 0.5
    assert result['exec_qty'] == 0.5


def test_reject_insufficient_liquidity():
    """测试流动性不足拒单"""
    executor = VirtualExecutor()
    order = {
        'target_qty': 2.0,
        'trade_direction': 'buy',
        'base_asset': 'BTC'
    }
    orderbook_row = {
        'spot_price_ask_1': 50000,
        'spot_volume_ask_1': 0.3,
        # 5档总量仅0.3 < 2.0
    }
    
    result = executor._calc_vwap(order, orderbook_row, 'spot')
    assert result['success'] == False
    assert '盘口深度不足' in result['reason']
```

### 10.2 集成测试

- 开仓完整流程测试(风控→订单→虚拟成交→持仓)
- 持仓盈亏计算准确性测试
- 并发开仓场景测试(同一合约冷却期验证)

---

## 十一、性能优化

### 11.1 数据库索引

关键查询已添加索引:
- `mi_trade_order`: `idx_order_uuid`, `idx_status`, `idx_created_at`
- `mi_trade_position`: `idx_status`, `idx_base_asset`, `idx_opened_at`

### 11.2 批量更新

持仓价格更新采用批量查询+批量更新:
```python
# 一次查询所有持仓
# 批量计算盈亏
# 一次批量UPDATE(使用executemany)
```

### 11.3 内存缓存

- 合约元数据(_contract_meta)缓存,避免频繁查库
- 冷却期数据内存缓存,每5分钟刷新

---

## 十二、文件组织规范

### 12.1 新增文件清单

| 文件路径 | 功能说明 | 复用现有模块 |
|---------|---------|-------------|
| `src/calc/trading_executor.py` | 开仓判断+虚拟成交 | 复用 `common/database.py`, `common/config.py` |
| `src/calc/position_tracker.py` | 持仓管理+盈亏计算 | 复用 `common/database.py`, `common/logger.py` |
| `src/api/trading_api.py` | 交易API路由 | 复用 `orderbook_server.py` 的FastAPI实例 |
| `src/common/tools.py` | 扩展现有工具函数 | 新增精度处理、订单格式化函数 |
| `src/config/config.yaml` | 扩展现有配置 | 新增交易相关配置项 |

### 12.2 common/tools.py 扩展内容

在现有的 `tools.py` (API签名、时间戳、资金费率计算) 基础上新增:

```python
# src/common/tools.py (扩展)

def format_price_precision(price: float, base_asset: str, market_type: str) -> float:
    """
    按交易所规则格式化价格精度
    
    Args:
        price: 原始价格
        base_asset: 标的资产(如BTC)
        market_type: 'spot' 或 'future'
    
    Returns:
        格式化后的价格
    """
    from common.config import config
    
    if market_type == 'spot':
        # Binance现货: 通常2位小数
        return round(price, 2)
    else:
        # Gate期货: 从元数据获取
        # 需要查询 mi_gate_future_contracts.price_decimal
        precision = get_price_decimal_from_db(base_asset)
        return round(price, precision)


def format_qty_precision(qty: float, base_asset: str, market_type: str) -> float:
    """
    按交易所规则格式化数量精度
    
    Args:
        qty: 原始数量
        base_asset: 标的资产
        market_type: 'spot' 或 'future'
    
    Returns:
        格式化后的数量
    """
    if market_type == 'spot':
        # Binance现货: 从step_size推导
        step_size = get_step_size_from_db(base_asset)
        precision = len(str(step_size).split('.')[-1].rstrip('0'))
        return round(qty, precision)
    else:
        # Gate期货: 从元数据获取
        precision = get_size_decimal_from_db(base_asset)
        return round(qty, precision)


def format_binance_order_params(base_asset: str, qty: float, order_uuid: str) -> dict:
    """
    格式化Binance现货市价单参数
    
    Returns:
        {
            'symbol': 'BTCUSDT',
            'side': 'BUY',
            'type': 'MARKET',
            'quantity': '0.10000',
            'newClientOrderId': 'arb_xxx_spot'
        }
    """
    formatted_qty = format_qty_precision(qty, base_asset, 'spot')
    return {
        'symbol': f"{base_asset}USDT",
        'side': 'BUY',
        'type': 'MARKET',
        'quantity': f"{formatted_qty:.5f}",  # 根据精度动态调整
        'newClientOrderId': f"arb_{order_uuid[:8]}_spot"
    }


def format_gate_order_params(contract: str, qty: float, base_asset: str, order_uuid: str) -> dict:
    """
    格式化Gate期货市价单参数
    
    Returns:
        {
            'contract': 'BTC_USDT',
            'size': 1,
            'price': '0',
            'tif': 'ioc',
            'text': 'arb_xxx_future'
        }
    """
    quanto = get_quanto_multiplier_from_db(base_asset)
    contracts_qty = int(qty / quanto)
    return {
        'contract': contract,
        'size': contracts_qty,
        'price': '0',
        'tif': 'ioc',
        'text': f"arb_{order_uuid[:8]}_future"
    }
```

### 12.3 config.yaml 扩展内容

在现有的 `config.yaml` 基础上新增交易配置:

```yaml
# src/config/config.yaml (扩展)

trade:
  # 现有配置...
  open_amount_usdt: 500
  orderbook_coverage_threshold: 0.8
  risk_relief_bps: 10
  open_marginal_basis_threshold_bps: -30
  fee:
    spot_open: 0.00075
    spot_close: 0.00075
    future_open: 0.00075
    future_close: 0.00075
  funding_rate_threshold_percentile: percentile_30
  meta_refresh_interval_min: 15
  
  # ===== 新增交易模块配置 =====
  
  # 开仓检查间隔(秒)
  open_check_interval_sec: 5
  
  # 开仓冷却期(秒) - 同一合约1小时内不重复开仓
  open_cooldown_sec: 3600
  
  # 持仓资金费更新间隔(秒) - 8小时
  position_funding_update_sec: 28800
  
  # 持仓实时数据推送间隔(秒)
  position_push_interval_sec: 10
```

### 12.4 模块职责划分

| 模块 | 职责 | 类比现有模块 |
|------|------|-------------|
| `calc/trading_executor.py` | 开仓风控判断、虚拟成交计算、订单持久化 | 类似 `calculate_hedge_metrics.py` |
| `calc/position_tracker.py` | 持仓创建、资金费累加、盈亏计算 | 类似 `update_gate_future_contracts.py` |
| `api/trading_api.py` | REST API路由、订单/持仓查询接口 | 类似 `orderbook_server.py` 的路由部分 |
| `common/tools.py` | 精度格式化、订单参数格式化 | 现有API签名、时间戳工具 |

### 12.5 设计原则总结

1. ✅ **不创建新目录** - 复用 `src/calc/`, `src/api/`, `src/common/`
2. ✅ **工具函数集中管理** - 合并到 `common/tools.py`,不分散
3. ✅ **计算与API分离** - `calc/` 放计算逻辑, `api/` 放路由
4. ✅ **配置统一管理** - 所有参数在 `config.yaml`
5. ✅ **导入路径清晰** - `from common.tools import xxx`, `from calc.xxx import yyy`

---

## 总结

本设计实现了完整的套利交易生命周期管理:

1. **开仓模块**: 严格遵循风控规则,自动生成订单并持久化
2. **虚拟成交模块**: 基于5档盘口计算VWAP,流动性不足时拒单
3. **持仓模块**: 合并现货期货,追踪价差和资金费损益

核心优势:
- ✅ 自动执行,无需人工干预
- ✅ 防重复开仓(1小时冷却)
- ✅ 逐笔追踪,精细化分析
- ✅ 虚实分离,便于后续扩展
- ✅ 完整的盈亏计算(浮动+实现+资金费)

请审查此设计方案,确认后即可开始编码实现。
