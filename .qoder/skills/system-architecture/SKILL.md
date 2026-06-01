# 跨交易所套利系统 — 技术架构规范

> 本文档是系统级架构约束，新增功能或模块时**必须**遵守。

---

## 1. 分层架构

系统分为 **4 层**，每层只能向下依赖，**禁止反向或跨层调用**。

```
┌─────────────────────────────────────────┐
│  API 服务层  (src/api/)                  │  FastAPI 路由、WebSocket、HTTP 入口
├─────────────────────────────────────────┤
│  计算/业务层  (src/calc/)                │  纯业务逻辑，不依赖 FastAPI
├─────────────────────────────────────────┤
│  交易所接口层  (src/exchange_apis/)       │  WS/REST 客户端封装
├─────────────────────────────────────────┤
│  公共工具层  (src/common/)               │  config / database / logger / tools
└─────────────────────────────────────────┘
```

**依赖规则**：
- `common/` 不导入任何上层模块
- `exchange_apis/` 仅导入 `common/`
- `calc/` 仅导入 `common/` 和 `exchange_apis/`（如 OrderBookManager）
- `api/` 可导入所有下层模块，但**不应包含业务计算逻辑**
- **禁止循环导入**：如需双向通信，使用回调（`Callable`）注入

---

## 2. 模块职责边界

### 2.1 API 服务层 (`src/api/`)

| 文件 | 职责 | 约束 |
|------|------|------|
| `orderbook_server.py` | 主服务入口：lifespan、路由、定时任务调度、WebSocket 广播 | 仅做组装和调度，计算逻辑下沉到 `calc/` |
| `executor_service.py` | 成交引擎 HTTP 微服务（独立进程） | 通过 `VirtualExecutor` 实现虚拟成交 |
| `trading_api.py` | 交易查询 REST 路由（订单/持仓） | 仅做 DB 查询 + JSON 序列化 |

**新增路由规则**：
- 业务无关的查询路由放 `trading_api.py`
- 需要 WebSocket 或服务状态的路由放 `orderbook_server.py`
- 独立微服务新建 `xxx_service.py`

### 2.2 计算/业务层 (`src/calc/`)

按**职责域**拆分为以下子模块：

| 域 | 模块 | 职责 |
|----|------|------|
| **订单簿** | `create_gate_futures_local_orderbook.py` | Gate 期货本地快照 |
| | `create_binance_spot_local_orderbook.py` | Binance 现货本地快照 |
| | `merge_cross_exchange_orderbook.py` | 跨交易所订单簿合并 |
| | `calculate_hedge_metrics.py` | 对冲指标（VWAP、盘口深度、对齐数量） |
| | `orderbook_enricher.py` | 行情富化（注入元数据和计算字段） |
| **交易执行** | `trading_executor.py` | 开仓判断 + 风控 + 订单生成 |
| | `executor_client.py` | 成交引擎 HTTP 客户端 |
| | `virtual_executor.py` | 虚拟成交引擎（VWAP 模拟） |
| **持仓管理** | `position_tracker.py` | 持仓创建、资金费累加 |
| | `position_pnl_calculator.py` | 实时盈亏计算 |
| **数据采集** | `vwap_snapshot_recorder.py` | VWAP 基差采样落库 |
| | `calculate_funding_rate_threshold.py` | 资金费率分位阈值 |
| | `update_*.py` | 元数据 ETL 任务 |
| **调度框架** | `etl_pipeline.py` | ETL 任务注册表 + Daily 调度器 |
| **生命周期** | `service_lifecycle.py` | WS 服务启停 + 进度追踪 |

**新增模块规则**：
- 新模块归属到对应的"域"目录（当前所有域都在 `calc/` 下，按文件名前缀区分）
- 单一职责：一个模块解决一个问题，不超过 400 行
- 纯函数优先：输入 → 输出，无副作用（DB 写入除外）
- 配置通过 `dataclass` 参数传入，不直接读 `config`

---

## 3. 核心设计模式

### 3.1 虚实分离（成交引擎）

```
TradingExecutor  →  ExecutorClient (HTTP)  →  executor_service (FastAPI)
                                                    ↓
                                             VirtualExecutor（当前）
                                             RealExecutor（未来实盘）
```

- 切换实盘**只需**修改 `config.yaml` 中 `trade.executor.url`
- `TradingExecutor` 通过统一的 `ExecutorClient` 接口调用，无需修改业务代码
- 新增成交引擎（如模拟盘）时，复制 `executor_service.py` 并替换内部实现

### 3.2 回调解耦（消除循环依赖）

当下层模块需要触发上层行为时，使用**回调注入**：

```python
# 下层定义回调槽
class ServiceLifecycleManager:
    def set_runtime(self, ..., build_payload_fn: Callable, schedule_broadcast_fn: Callable):
        self._build_payload_fn = build_payload_fn

# 上层注入实现
svc.set_runtime(event_loop, broadcast_queue, build_payload, schedule_broadcast)
```

**规则**：`calc/` 模块绝不导入 `api/` 模块。

### 3.3 配置 dataclass 传递

将配置参数封装为 `@dataclass`，在 `api/` 层一次构造，传入 `calc/` 模块：

```python
# calc/orderbook_enricher.py
@dataclass
class EnrichConfig:
    open_amount_usdt: float
    funding_threshold_percentile: str
    ...

# api/orderbook_server.py
_enrich_cfg = EnrichConfig(
    open_amount_usdt=OPEN_AMOUNT_USDT,
    ...
)
enrich_snapshot_fields(rows, ..., _enrich_cfg, ...)
```

**优势**：
- `calc/` 模块不依赖 `common.config`，提高可测试性
- 配置变更影响面清晰，IDE 可追踪所有使用点

### 3.4 元数据缓存 + 定时刷新

```
启动时加载 → 全局 dict 缓存 → ETL 定时刷新（15min）→ 替换引用
```

- `_contract_meta`, `_spot_meta`, `_threshold_meta` 在 `orderbook_server.py` 的 `lifespan` 中初始化
- `calc/` 模块通过**参数注入**接收元数据，不自行加载
- 公共加载函数放 `common/meta_loader.py`
- 业务特有的加载函数（如 threshold）放 `api/orderbook_server.py`

### 3.5 ETL 任务注册表

新增数据任务时，只需在 `etl_pipeline.py` 的 `ETL_TASKS` 列表添加一项：

```python
ETLTask(
    name='update_xxx',
    description='更新 XXX 数据',
    func=update_xxx_data,
    frequency='interval',  # 或 'daily'
    enabled=True,
)
```

- `interval` 类型由 `_refresh_meta_loop()` 驱动（默认 15 分钟）
- `daily` 类型由 `DailyScheduler` 守护线程在指定时刻执行

---

## 4. 编码规范

### 4.1 文件结构

```python
# coding: utf-8
"""
模块简介（一行）

详细说明（可选，2-3 行）
"""
import ...          # 标准库
from xxx import ... # 第三方库

from common.xxx import ...     # 公共工具
from calc.xxx import ...       # 业务模块

logger = get_logger(__name__)

# 常量 / dataclass 定义
# 公共函数 / 类
```

### 4.2 数据库操作

```python
# 统一使用上下文管理器
with db_manager.get_cursor() as cursor:
    cursor.execute(sql)
    rows = cursor.fetchall()

# 批量插入使用 executemany
cursor.executemany(insert_sql, batch_data)
```

- 所有 SQL 异常用 `try/except` 包裹，记录 `logger.error`
- 只读查询直接 `fetchall()`，写入操作由 `get_connection()` 自动 commit

### 4.3 日志规范

```python
from common.logger import get_logger, log_print

logger = get_logger(__name__)       # 模块级 logger
log_print('用户可见的操作信息')       # 等同 print，同时写日志
logger.info('技术细节信息')          # 仅写日志
logger.error(f'错误: {e}')          # 异常信息
```

### 4.4 配置读取

```python
from common.config import config

# 在 api/ 层读取配置
VALUE = config.get_float('trade.xxx', default=0.0)

# 在 calc/ 层通过参数接收，不直接读 config
def calculate(data, cfg: MyConfig) -> Result:
    ...
```

---

## 5. 数据流管线

```
Binance WS ──┐
             ├→ merge_orderbook_records()
Gate WS ─────┘           │
                         ↓
              calculate_hedge_metrics()
                         │
            ┌────────────┼────────────┐
            ↓            ↓            ↓
    enrich_snapshot   enrich_trading  record_vwap
    _fields()         _fields()      _snapshots()
            │            │
            ↓            ↓
    WebSocket推送    TradingExecutor
    (前端监控)      .check_and_open()
                         │
                    ExecutorClient
                    (HTTP成交)
                         │
              ┌──────────┼──────────┐
              ↓                     ↓
         订单持久化            持仓创建
        (mi_trade_order)    (mi_trade_position)
                                    │
                              position_pnl
                              _calculator()
                                    │
                              WebSocket推送
                              (实时持仓)
```

---

## 6. 新增功能检查清单

添加新功能前，对照以下清单：

- [ ] **分层正确**：业务逻辑放 `calc/`，路由放 `api/`，公共工具放 `common/`
- [ ] **单一职责**：新模块不超过 400 行，函数不超过 80 行
- [ ] **无循环依赖**：`calc/` 不导入 `api/`，需要回调用 `Callable` 注入
- [ ] **配置传参**：`calc/` 模块通过 `dataclass` 接收配置，不直接读 `config`
- [ ] **元数据注入**：通过参数传入 `_contract_meta` 等缓存，不自行加载
- [ ] **数据库操作**：使用 `db_manager.get_cursor()` 上下文管理器
- [ ] **日志规范**：使用 `get_logger(__name__)` + `log_print()` 输出用户信息
- [ ] **异常处理**：所有 I/O 操作用 `try/except` 包裹并记录日志
- [ ] **定时任务**：通过 `ETL_TASKS` 注册表管理，不在 `orderbook_server.py` 内联
- [ ] **虚实分离**：涉及交易执行的功能通过 `ExecutorClient` 调用

---

## 7. 数据库表索引

| 表名 | 用途 |
|------|------|
| `mi_base_asset` | 有效标的资产列表 |
| `mi_gate_future_contracts` | Gate 期货合约元数据 |
| `mi_binance_spot_info` | Binance 现货元数据 |
| `mi_gate_future_funding_rate_threshold` | 资金费率分位阈值 |
| `mi_vwap_basis_snapshot` | VWAP 基差采样快照 |
| `mi_vwap_basis_threshold` | VWAP 基差分位阈值 |
| `mi_trade_order` | 交易订单记录 |
| `mi_trade_position` | 持仓记录 |

---

## 8. 关键配置项（config.yaml）

| 路径 | 含义 | 默认值 |
|------|------|--------|
| `orderbook.settle` | 结算币种 | `usdt` |
| `orderbook.broadcast_throttle_sec` | 广播节流间隔 | `1.0` |
| `trade.executor.url` | 成交引擎地址 | `http://localhost:8081` |
| `trade.open_amount_usdt` | 开仓金额 | `500` |
| `trade.fee.*` | 手续费率 | `0.00075` |
| `trade.funding_rate_threshold_percentile` | 资金费率阈值 | `percentile_30` |
| `trade.meta_refresh_interval_min` | ETL 刷新间隔 | `15` |
| `trade.open_cooldown_sec` | 开仓冷却期 | `3600` |

---

## 9. 不可调整约束（红线）

> 以下配置与参数经实盘验证，**禁止以性能优化为由进行修改**。

### 9.1 交易所 WebSocket 订阅频率

| 交易所 | 模块 | 常量 | 值 | 约束原因 |
|---------|------|------|-----|----------|
| Gate Futures | `create_gate_futures_local_orderbook.py` | `FREQUENCY` | `'100ms'` | 实盘开仓依赖低延迟盘口，1000ms 延迟会导致滑点过大 |
| Binance Spot | `create_binance_spot_local_orderbook.py` | `SPEED` | `'100ms'` | 同上，VWAP 基差计算需要实时性 |

**禁止操作**：
- ✗ 将频率降为 `1000ms` 以降低 CPU
- ✗ 在任何“性能优化”场景中修改此参数

**合法的性能优化方向**（不触碰频率）：
- ✓ 广播节流（`broadcast_throttle_sec`）
- ✓ 计算结果缓存（merge + hedge_metrics 缓存）
- ✓ 定时轮询替代逐消息回调

### 9.2 开仓热路径零 IO 约束

> `_open_position_loop`（0.5s 间隔）是系统最关键的延迟敏感路径，**必须保持纯内存操作 + 下单**。

**热路径允许的操作**：
- ✓ 读取内存 OrderBook（`gate_manager.to_records()` / `spot_manager.to_records()`）
- ✓ 纯计算（merge、hedge_metrics、enrich_trading_fields）
- ✓ 读取内存风控状态（`_holding_liq_distance`、`_peak_state`）
- ✓ 调用 ExecutorClient 下单（唯一允许的网络 IO）
- ✓ 下单成功后持久化订单（非阻塞关键路径，已在执行结果返回后）

**热路径禁止的操作**：
- ✗ 数据库查询（如 `get_holding_positions()`、`get_all_positions()`）
- ✗ REST API 调用（如实时获取费率、余额查询）
- ✗ 文件 IO、日志落盘（`logger.info` 允许，但不可做同步文件写入阻塞）
- ✗ 任何 `time.sleep()` 或同步阻塞等待

**分频架构**：将不同实时性需求的逻辑拆分为独立循环

| 循环 | 频率 | 操作类型 | 数据来源 |
|------|------|----------|----------|
| `_open_position_loop` | 0.5s | **纯内存 + 下单** | 无缓存直读 OrderBook |
| `_margin_status_loop` | 5s | DB 查询 + PnL 计算 | 带缓存合并数据 |
| `_close_position_loop` | 5s | DB 查询 + 平仓判断 | 带缓存合并数据 |
| `_position_realtime_push` | 5s | DB 查询 + 前端推送 | 带缓存合并数据 |
| `_orderbook_broadcast_loop` | 1s | 序列化 + 前端推送 | 带缓存合并数据 |

**设计原则**：
1. 凡不影响开仓决策实时性的逻辑（保证金状态、持仓推送、前端广播），**必须独立为低频循环**
2. 开仓热路径的风控状态（如距爆仓距离）由低频循环预计算并写入内存，热路径仅**读取**
3. 新增开仓前置检查时，若涉及 IO 操作，应在进入峰值监控时（首次触发）执行一次，而非每次循环执行
