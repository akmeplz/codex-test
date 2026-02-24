# Binance 资金费监控（历史回算 + 实时刷新）

当前版本是“历史回算 + 实时刷新”：

- 每秒更新：**仓位价值 / 账户总权益 / 实际杠杆**
- 仅当出现新的 `FUNDING_FEE` 入账时：**样本数 +1**，并新增资金费样本点

默认会优先基于本地历史记录回算；若本地无该区间数据且你提供了开始时间，会自动向 Binance 拉取该区间资金费历史并回算。

默认端口：`8000`。

默认数据目录为 `~/.binance_funding_monitor`（跨重启、跨工作目录更稳定）；若检测到旧路径 `./output/funding_records_stream.csv` 会自动兼容读取。

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

- 页面可每秒刷新，但真实 API 拉取默认已降频：
  - 仓位价值/账户权益/杠杆：`--exposure-poll-seconds`（默认 5 秒）
  - 资金费事件轮询：`--funding-poll-seconds`（默认 15 秒）
- 样本数（count）：仅在检测到新的 Binance 资金费事件时增加。
- 对新事件的小时化换算增加最小窗口小时保护（默认8小时，可按策略调小），防止新增第一条样本时因时间差过小导致收益率异常放大。
- 历史回算：API 默认读取本地 `record-file` 全量历史；若本地为空且给了开始时间，会自动请求 Binance `income` 历史来回算。
- 图表：展示回算区间内每条资金费事件样本（净/收到/支付）。
- 当前仅统计资金费现金流，因此“日化/月化/年化收益率”与“已实现资金费费率”本质等价，UI已去重避免重复展示造成误解。
- “预计费率”基于当前持仓+当前资金费率，是前瞻估算，可能与已实现收益方向相反。
- 回算建议：为了避免每秒重复拉取历史，区间查询结果会做短时缓存（约30秒）。
- 若 Binance 历史接口异常，前端会显示 warning，且 `source` 会提示当前是否来自 local/binance。
- 若遇到 Binance `429/418`，程序会自动指数退避冷却（429: 30s起步；418: 120s起步），并在收到 `429` 时优先遵守 `Retry-After` 退让时间。
- 公共行情类 REST（如资金费率与结算周期）已做本地缓存，避免每次tick都重复请求。

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
- `--interval-seconds 1`（后台主循环tick间隔）
- `--exposure-poll-seconds 5`（仓位/权益/杠杆API拉取间隔）
- `--funding-poll-seconds 15`（资金费事件API轮询间隔）
- `--min-event-window-hours 8`（避免新事件窗口过短导致小时/日化夸大）
- `--record-file ~/.binance_funding_monitor/funding_records_stream.csv`
- `--summary-csv ~/.binance_funding_monitor/funding_summary_stream.csv`
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
