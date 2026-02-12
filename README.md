# Binance 资金费监控（仅增量、动态图表）

你这个需求我已经按“事件驱动”改了：

- 每秒更新：**仓位价值 / 账户总权益 / 实际杠杆**
- 仅当出现新的 `FUNDING_FEE` 入账时：**样本数 +1**，并新增资金费样本点

所以不会再出现“每秒都涨样本数”的问题。

默认端口：`8081`（不使用 8000）。

---

## 启动

### 1) 配置 API

```bash
export BINANCE_API_KEY="你的KEY"
export BINANCE_API_SECRET="你的SECRET"
```

### 2) 启动网页版

```bash
python binance_funding_monitor.py --web --port 8081
```

打开：`http://127.0.0.1:8081`

### 3) 本地演示（无需 API）

```bash
python binance_funding_monitor.py --web --demo-mode --port 8081
```

---

## 行为说明（重点）

- 仓位价值、账户权益、实际杠杆：按 `--interval-seconds` 轮询更新（默认 1 秒）。
- 样本数（count）：仅在检测到新的 Binance 资金费事件时增加。
- 图表：只画资金费事件样本（净/收到/支付），没有新资金费时曲线不新增点。

---

## 收益率口径

- `日化收益率 = 净日化 / 仓位价值`
- `月化收益率 = 日化收益率 * 30`
- `年化收益率 = 日化收益率 * 365`

---

## 参数

- `--web`
- `--host 0.0.0.0`
- `--port 8081`
- `--interval-seconds 1`（仓位/权益/杠杆刷新间隔）
- `--record-file output/funding_records_stream.csv`
- `--summary-csv output/funding_summary_stream.csv`
- `--chart-points 120`
- `--resume`（续写文件）
- `--demo-mode`
- `--once`


---

## 时间同步说明

已内置 Binance 服务器时间自动同步。若出现 `-1021`（本地时间超前/滞后）会自动校时并重试一次请求。
