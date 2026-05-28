# 阿里云香港节点运维技能

> 当用户提到阿里云、服务器、部署、运维、排错、日志、重启等关键词时，参考本文档。

---

## 1. 环境概览

| 项目 | 值 |
|------|-----|
| 云厂商 | 阿里云 ECS（试用） |
| 地域 | 中国香港 |
| 公网 IP | `47.86.13.224` |
| OS | Ubuntu 22.04 LTS |
| 规格 | 2核4G |
| 项目路径 | `/opt/ARBITRAGE-MI` |
| Python 虚拟环境 | `/opt/ARBITRAGE-MI/.venv` |
| 前端构建产物 | `/opt/ARBITRAGE-MI/frontend/dist` |
| 应用日志 | `/opt/ARBITRAGE-MI/src/log/app.log` |
| MySQL 用户 | `arb_app@localhost`，密码 `123456Aa.` |
| MySQL 数据库 | `crypto_arbitrage` |
| GitHub 仓库 | `https://github.com/Walliii515/ARBITRAGE-MI.git` |

---

## 2. 服务架构

```
Nginx (:80)
  ├── / → 静态文件 (frontend/dist)
  ├── /api/ → proxy → orderbook_server (:19876)
  └── /ws → proxy (WebSocket) → orderbook_server (:19876)

systemd 服务：
  ├── arbitrage-orderbook  → python -m api.orderbook_server (端口 19876)
  └── arbitrage-executor   → uvicorn api.executor_service:app (端口 8081)
```

---

## 3. SSH 登录

```bash
ssh root@47.86.13.224
```

---

## 4. 常用运维命令

### 4.1 服务管理

```bash
# 查看状态
systemctl status arbitrage-orderbook
systemctl status arbitrage-executor

# 重启
systemctl restart arbitrage-orderbook
systemctl restart arbitrage-executor

# 停止
systemctl stop arbitrage-orderbook
systemctl stop arbitrage-executor

# 开机自启
systemctl enable arbitrage-orderbook
systemctl enable arbitrage-executor
```

### 4.2 日志查看

```bash
# 实时跟踪应用日志
tail -f /opt/ARBITRAGE-MI/src/log/app.log

# 查看最后 200 行
tail -200 /opt/ARBITRAGE-MI/src/log/app.log

# 搜索错误
grep -i error /opt/ARBITRAGE-MI/src/log/app.log

# systemd 日志
journalctl -u arbitrage-orderbook -f --no-pager
journalctl -u arbitrage-executor -f --no-pager

# 最近 30 分钟的 systemd 日志
journalctl -u arbitrage-orderbook --since "30 min ago"
```

### 4.3 代码更新部署

```bash
cd /opt/ARBITRAGE-MI
git pull origin main

# Python 依赖（如有变化）
source .venv/bin/activate
pip install -r requirements.txt

# 前端（如有变化）
cd frontend && npm run build && cd ..

# 重启服务
systemctl restart arbitrage-orderbook
systemctl restart arbitrage-executor
```

### 4.4 MySQL 操作

```bash
# 登录
mysql -u root -p

# 检查标的数量
mysql -u arb_app -p crypto_arbitrage -e "SELECT COUNT(*) FROM mi_base_asset WHERE is_valid = 'Y';"

# 检查持仓
mysql -u arb_app -p crypto_arbitrage -e "SELECT * FROM mi_trade_position WHERE status = 'open';"
```

### 4.5 Nginx 操作

```bash
# 检查配置语法
nginx -t

# 重载配置
systemctl reload nginx

# 配置文件位置
/etc/nginx/sites-available/arbitrage
```

### 4.6 系统资源

```bash
# 内存使用
free -h

# 磁盘使用
df -h

# CPU / 进程
top -c

# 查看占用端口
ss -tlnp | grep -E '19876|8081|3306|80'
```

---

## 5. 配置文件位置

| 文件 | 路径 | 说明 |
|------|------|------|
| 环境变量 | `/opt/ARBITRAGE-MI/.env` | 交易所 API Key + MySQL 连接 |
| 业务配置 | `/opt/ARBITRAGE-MI/src/config/config.yaml` | 交易参数、阈值、间隔 |
| Nginx | `/etc/nginx/sites-available/arbitrage` | 反代规则 |
| orderbook service | `/etc/systemd/system/arbitrage-orderbook.service` | 主服务 systemd 单元 |
| executor service | `/etc/systemd/system/arbitrage-executor.service` | 成交引擎 systemd 单元 |
| MySQL | `/etc/mysql/mysql.conf.d/mysqld.cnf` | bind-address 等 |

---

## 6. 常见问题排查

### 6.1 WS 服务只加载 BTC/ETH（默认合约）

**原因**：从 `mi_base_asset` 表查询失败，走了异常回退。

**排查**：
```bash
grep "获取合约列表失败\|mi_base_asset" /opt/ARBITRAGE-MI/src/log/app.log
mysql -u arb_app -p crypto_arbitrage -e "SELECT COUNT(*) FROM mi_base_asset WHERE is_valid = 'Y';"
```

**常见根因**：
- 数据库未迁移此表 → 重新导入
- `cryptography` 包缺失 → `pip install cryptography`

### 6.2 MySQL 认证失败（cryptography 报错）

**现象**：日志中出现 `'cryptography' package is required for sha256_password or caching_sha2_password`

**修复**：
```bash
cd /opt/ARBITRAGE-MI
source .venv/bin/activate
pip install cryptography
systemctl restart arbitrage-orderbook
```

### 6.3 前端页面访问不了

**排查顺序**：
1. 安全组是否开放 80 端口
2. Nginx 是否运行：`systemctl status nginx`
3. 前端是否构建：`ls /opt/ARBITRAGE-MI/frontend/dist/index.html`
4. Nginx 配置是否正确：`nginx -t`

### 6.4 WebSocket 连接断开 / 前端无数据

**排查**：
```bash
# 确认主服务在运行
systemctl status arbitrage-orderbook

# 确认端口在监听
ss -tlnp | grep 19876

# 查看最近错误
grep -i "error\|exception\|disconnect" /opt/ARBITRAGE-MI/src/log/app.log | tail -20
```

### 6.5 服务启动失败

```bash
# 查看详细错误
journalctl -u arbitrage-orderbook --since "5 min ago" --no-pager

# 手动启动看报错
cd /opt/ARBITRAGE-MI/src
source ../.venv/bin/activate
python -m api.orderbook_server
```

### 6.6 磁盘空间不足

```bash
# 查看磁盘使用
df -h

# 清理日志
> /opt/ARBITRAGE-MI/src/log/app.log

# 查看大文件
du -sh /opt/ARBITRAGE-MI/src/log/*
```

### 6.7 本地想远程连接 MySQL（临时）

1. 修改 `bind-address = 0.0.0.0`：`nano /etc/mysql/mysql.conf.d/mysqld.cnf`
2. `systemctl restart mysql`
3. 安全组临时开放 3306
4. 操作完毕后**立即恢复**：改回 `127.0.0.1` + 删除安全组规则

---

## 7. 安全组规则（当前）

| 端口 | 协议 | 授权对象 | 用途 | 状态 |
|------|------|----------|------|------|
| 22 | TCP | 0.0.0.0/0 | SSH | 常开 |
| 80 | TCP | 0.0.0.0/0 | 网页 | 常开 |
| 3306 | TCP | 0.0.0.0/0 | MySQL | **仅临时开放** |

> 建议后续将 22 和 80 的授权对象限制为你的固定出口 IP。

---

## 8. 注意事项

1. **试用期限**：关注 ECS 到期时间，提前备份数据
2. **MySQL 密码**：当前 root 和 arb_app 密码均为 `123456Aa.`，生产环境应更换
3. **日志轮转**：`src/log/app.log` 会持续增长，需定期清理或配置 logrotate
4. **网络延迟**：香港→东京（交易所）约 40-60ms，若频繁断线考虑迁到日本节点
5. **pip 安装新包后**：需重启对应的 systemd 服务才能生效
