# Binance 合约资金费用监控（支持持仓变动、加权年化、历史读取）

这个脚本适合你描述的场景：

- 读取 Binance **USDⓈ-M 合约账户**当前持仓（持仓可变动）。
- 读取当前 `lastFundingRate`，估算**下一次资金费用**。
- 读取最近 1 小时（可调）已发生的资金费用（`incomeType=FUNDING_FEE`）。
- 每次运行追加一条小时记录到本地 CSV。
- 可指定起始日期，读取历史记录并用**加权平均**计算：
  - 资金费用日化 / 月化 / 年化（USDT）
  - 资金费率日化 / 月化 / 年化（%）
- 生成 SVG 图表（累计资金费用曲线 + 年化费率柱状图 + 汇总面板）。

> 建议配合 crontab 每小时运行一次，实现持续追踪。

---

## 环境要求

- Python 3.9+
- 无第三方依赖（仅标准库）

---

## API 权限要求

Binance API Key 需要至少具备：

- **Futures 读取权限**（读取持仓、收益流水）

不需要交易权限。

---

## 使用方式

### 1) 设置 API

```bash
export BINANCE_API_KEY="你的KEY"
export BINANCE_API_SECRET="你的SECRET"
```

或者命令行直接传：`--api-key --api-secret`。

### 2) 运行（默认会追加一条小时记录）

```bash
python binance_funding_monitor.py
```

### 3) 只读取历史记录并重算（不追加新记录）

```bash
python binance_funding_monitor.py --skip-record
```

### 4) 指定统计起始时间

```bash
python binance_funding_monitor.py --start-date 2025-01-01
python binance_funding_monitor.py --start-date 2025-01-01T08:00:00
```

---

## 关键参数

- `--record-file output/funding_records.csv`
  - 小时级记录文件（每次运行 append 一行）
- `--summary-csv output/funding_summary.csv`
  - 汇总指标输出
- `--chart-file output/funding_summary.svg`
  - 图表输出
- `--start-date ...`
  - 统计起始时间（UTC 解析）
- `--realized-window-hours 1`
  - 每次采集时，回看最近几小时的已实现资金费
- `--skip-record`
  - 不调用实时采集，只读历史记录计算

---

## 加权逻辑说明

- 对费率（8h）使用 **abs(notional)** 加权：
  - `weighted_rate_8h = sum(rate_i * abs_notional_i) / sum(abs_notional_i)`
- 费率年化换算（线性 APR）：
  - 日化：`8h_rate * 3`
  - 月化：`8h_rate * 90`
  - 年化：`8h_rate * 1095`
- 资金费用（USDT）年化换算：
  - 根据记录样本的平均每小时 realized funding fee 线性外推

---

## 输出文件

默认输出：

- `output/funding_records.csv`：小时记录
- `output/funding_summary.csv`：汇总指标
- `output/funding_summary.svg`：图表

---

## 定时任务（每小时）

示例 crontab：

```cron
0 * * * * /usr/bin/python3 /path/to/binance_funding_monitor.py >> /path/to/output/cron.log 2>&1
```

这样就可以持续积累历史，后续按你选定的起始时间重算。
