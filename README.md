# Binance 资金费监控（仅增量、动态图表）

按你的要求，这一版：

- 不回算历史
- 只持续记录新数据
- 实时增量加权
- 动态网页展示

并且新增：

- **默认采集频率：1秒1采（可切到整点+60秒）**
- 展示 **仓位价值**、**账户总权益**、**实际杠杆**

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

## 采集规则

默认：

- 每 **1 秒**采集一次
- 页面每 **1 秒**刷新一次

如果你要切回整点采样，可使用：

```bash
python binance_funding_monitor.py --web --align-to-hour --sample-offset-seconds 60
```


---

## 页面显示指标

- 仓位价值（USDT）
- 账户总权益（USDT）
- 实际杠杆（仓位价值 / 账户总权益）
- 净资金费 / 收到资金费 / 支付资金费（累计与小时/日化）
- 费率日化、费率年化
- 日化收益率、月化收益率、年化收益率（按 `净日化 / 仓位价值` 计算）

---

## 参数

- `--web`
- `--host 0.0.0.0`
- `--port 8081`
- `--interval-seconds 1`（默认1秒1采）
- `--align-to-hour`（默认关闭，开启后按整点偏移秒采）
- `--sample-offset-seconds 60`（整点模式下每小时第 N 秒采集）
- `--record-file output/funding_records_stream.csv`
- `--summary-csv output/funding_summary_stream.csv`
- `--chart-points 120`
- `--resume`（续写文件）
- `--demo-mode`
- `--once`

---

## 说明

默认会在启动时先采样 1 条用于初始化页面，然后按你设置的采样规则继续采集（默认1秒1采）。


---

## 收益率口径

- `日化收益率 = 净日化 / 仓位价值`
- `月化收益率 = 日化收益率 * 30`
- `年化收益率 = 日化收益率 * 365`

以上为线性年化口径。
