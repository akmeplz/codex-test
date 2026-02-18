# Binance 资金费监控（历史回算 + 实时刷新）

当前版本是“历史回算 + 实时刷新”：

- 每秒更新：**仓位价值 / 账户总权益 / 实际杠杆**
- 仅当出现新的 `FUNDING_FEE` 入账时：**样本数 +1**，并新增资金费样本点

默认会基于本地历史记录回算资金费统计，并支持网页按时间区间重算。

默认端口：`8000`。

---

## 启动

### 1) 配置 API

```bash
export BINANCE_API_KEY="你的KEY"
export BINANCE_API_SECRET="你的SECRET"
```

### 2) 启动网页版

```bash
python binance_funding_monitor.py --web --port 8000
```

打开：`http://127.0.0.1:8000`

### 3) 本地演示（无需 API）

```bash
python binance_funding_monitor.py --web --demo-mode --port 8000
```

---

## 行为说明（重点）

- 仓位价值、账户权益、实际杠杆：按 `--interval-seconds` 轮询更新（默认 1 秒）。
- 样本数（count）：仅在检测到新的 Binance 资金费事件时增加。
- 历史回算：API 默认会读取本地 `record-file` 全量历史来回算资金费统计；你也可以用开始/结束时间做区间回算。
- 图表：展示回算区间内每条资金费事件样本（净/收到/支付）。

---

## 收益率口径

- `日化收益率 = 净日化 / 仓位价值`
- `月化收益率 = 日化收益率 * 30`
- `年化收益率 = 日化收益率 * 365`

---

## 参数

- `--web`
- `--host 0.0.0.0`
- `--port 8000`
- `--interval-seconds 1`（仓位/权益/杠杆刷新间隔）
- `--record-file output/funding_records_stream.csv`
- `--summary-csv output/funding_summary_stream.csv`
- `--chart-points 120`
- `--reset-records`（启动时清空历史记录；默认不清空）
- `--demo-mode`
- `--once`


---

## 时间同步说明

已内置 Binance 服务器时间自动同步。若出现 `-1021`（本地时间超前/滞后）会自动校时并重试一次请求。

另外已对接口返回不完整（`IncompleteRead`）增加自动重试，减少偶发网络抖动导致的 `[WARN] tick failed`。


---

## 时间区间筛选

网页支持手动选择 `开始时间` 和 `结束时间`（UTC）。

- 选择区间后，资金费相关统计（样本数/净值/收到/支付/收益率）会按该区间重算。
- 区间内的仓位价值 / 账户总权益 / 实际杠杆使用事件窗口时长做加权平均。
- 不选择时间时默认按本地历史全量回算（并叠加展示当前实时仓位/权益/杠杆）。
