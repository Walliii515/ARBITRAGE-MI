# Gate 永续订单簿监控前端

Vue 3 + Element Plus + AG Grid Community，通过 WebSocket 实时展示跨交易所合并订单簿（`snapshot.rows`）。

## 启动

### 1. 后端（项目根目录）

```bash
# 安装 Python 依赖
pip install -r requirements.txt

# 启动服务（自动连接 Gate WS，订阅 BTC_USDT、ETH_USDT）
python run_orderbook_service.py
```

可选环境变量：

- `ORDERBOOK_CONTRACTS=BTC_USDT,ETH_USDT` — 订阅合约列表
- `ORDERBOOK_SETTLE=usdt` — 结算货币

### 2. 前端

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 http://localhost:5173 ，侧边栏「订单簿监控」即可查看实时 5 档盘口。

## 技术栈

- Vue 3 + Vite + TypeScript
- Element Plus
- ag-grid-community（带水印）+ ag-grid-vue3
- WebSocket `/ws/orderbook`（Vite 开发代理到后端 8000 端口）
