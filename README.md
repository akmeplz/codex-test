# Binance 资金费监控（完全改版：仅增量记录 + 动态图表）

按你的要求，这一版是**流式增量模式**：

- ❌ 不回算之前历史
- ✅ 只从当前启动时刻开始，不停记录新的
- ✅ 每次新样本进来后做增量加权
- ✅ 动态网页实时展示指标和曲线

默认端口：`8081`（不使用 8000）。

---

## 功能

- 持续采集 Binance USDⓈ-M 资金费相关数据。
- 自动处理 1h/4h/8h 结算周期（按小时归一后再加权）。
- 资金费拆分为：
  - 净值（net）
  - 收到（received）
  - 支付（paid）
- 增量更新日化/月化/年化。
- 动态网页（自动刷新）：
  - 指标卡片
  - 三条动态线：净/收到/支付（USDT/h）

---

## 运行方式

### 1) 设置 API

```bash
export BINANCE_API_KEY="你的KEY"
export BINANCE_API_SECRET="你的SECRET"
```

### 2) 启动动态网页（推荐）

```bash
python binance_funding_monitor.py --web --port 8081 --interval-seconds 3600
```

浏览器打开：`http://127.0.0.1:8081`

### 3) 仅命令行持续采集

```bash
python binance_funding_monitor.py --interval-seconds 3600
```

### 4) 调试单次采样

```bash
python binance_funding_monitor.py --once
```

### 5) 无 API 本地演示模式

```bash
python binance_funding_monitor.py --web --demo-mode --interval-seconds 5
```

---


## 启动样本数说明

现在网页模式启动后会先立即采样 1 次，然后后台会等一个完整采样周期再采下一次。
所以刚启动时应看到 **1 条样本**，不会再出现瞬间变成 2 条的情况。

---

## 关键参数

- `--web`：启动网页模式
- `--host 0.0.0.0`
- `--port 8081`（默认，不占用 8000）
- `--interval-seconds 3600`：采样间隔
- `--realized-window-hours 24`：每次采集回看窗口小时数
- `--record-file output/funding_records_stream.csv`
- `--summary-csv output/funding_summary_stream.csv`
- `--chart-points 120`：动态图保留最近N点
- `--resume`：续写记录文件（默认不续写，启动时重置文件）
- `--demo-mode`：本地模拟数据，不调用 Binance API

---

## 关于“不回算之前的”

默认行为：

- 启动服务时会重置本次记录文件（新会话）
- 统计完全来自本次会话之后的新采样
- 不读取旧 CSV 做历史重算

如果你想续写文件，可加 `--resume`，但依然不会把旧数据再扫描回算到当前会话统计里。
