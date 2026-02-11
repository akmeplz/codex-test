# Binance 合约资金费用监控（支持 1h/4h/8h 结算周期）

这个脚本支持你描述的实际场景：**持仓会变化、不同币对结算周期不同、按小时持续记录并用历史加权统计**。

核心能力：

- 读取 Binance USDⓈ-M 持仓（动态变化）。
- 读取每个持仓币对的资金费率并估算：
  - 下一次结算资金费（next fee）
  - 按结算周期折算后的每小时估算资金费（hourly normalized）
- 自动识别各币对结算周期（1h / 4h / 8h，默认缺省按 8h）。
- 每次运行追加一条记录到 CSV。
- 可按 `--start-date` 读取历史并重新计算加权统计。
- 生成 SVG 图表（累计资金费 + 年化费率 + 统计面板）。

---

## 为什么之前会“数据看起来不对”

如果把一次结算资金费（例如 8 小时一次）直接当成“1 小时收益”去年化，会被放大 8 倍。

当前版本已修正：

1. **费率年化**使用“每小时费率”统一口径：
   - `hourly_rate = funding_rate / interval_hours`
   - 再换算到日/月/年。
2. **资金费金额年化**基于“覆盖小时数”计算：
   - `avg_hourly_realized = sum(realized_fee_window) / sum(realized_window_hours)`
   - 避免 1h/4h/8h 不同结算频率造成误判。

---

## 环境要求

- Python 3.9+
- 无第三方依赖（仅标准库）

---

## API 权限

Binance API Key 需具备 Futures 读取权限（读取持仓、资金费流水）。

---

## 使用方式

### 1) 设置 API

```bash
export BINANCE_API_KEY="你的KEY"
export BINANCE_API_SECRET="你的SECRET"
```

### 2) 正常采集（会追加一条记录）

```bash
python binance_funding_monitor.py
```

> 默认 `--realized-window-hours 24`，用于减少 1h/4h/8h 结算噪声。

### 3) 只读取历史重算（不追加新记录）

```bash
python binance_funding_monitor.py --skip-record
```

### 4) 手动选择统计起始日期

```bash
python binance_funding_monitor.py --start-date 2025-01-01
python binance_funding_monitor.py --start-date 2025-01-01T08:00:00
```

---

## 常用参数

- `--record-file output/funding_records.csv`
- `--summary-csv output/funding_summary.csv`
- `--chart-file output/funding_summary.svg`
- `--start-date ...`
- `--realized-window-hours 24`
- `--skip-record`

---

## 输出文件

- `output/funding_records.csv`：小时记录
- `output/funding_summary.csv`：汇总指标
- `output/funding_summary.svg`：图表

---

## 建议定时任务（每小时）

```cron
0 * * * * /usr/bin/python3 /path/to/binance_funding_monitor.py >> /path/to/output/cron.log 2>&1
```

持续运行后，你可以随时指定 `--start-date` 基于历史重算统计。
