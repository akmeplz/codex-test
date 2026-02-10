# Binance 合约资金费率监控脚本

该脚本会：

1. 拉取币安 **USDⓈ-M 永续合约** 的当前资金费率。
2. 按可配置规则排序（资金费率 / 绝对值 / 交易对）。
3. 在终端打印排序结果。
4. 导出 CSV 数据。
5. 生成 SVG 柱状图（正费率绿色、负费率红色）。

## 环境要求

- Python 3.9+
- 无第三方依赖（仅使用标准库）

## 使用方式

```bash
python binance_funding_monitor.py
```

常用参数：

- `--sort-by {rate,abs,symbol}`：排序字段，默认 `abs`
- `--ascending`：升序（默认降序）
- `--table-limit 30`：终端输出前 N 条
- `--chart-limit 40`：图表展示前 N 条
- `--output-csv output/funding_rates.csv`
- `--output-chart output/funding_rates.svg`

示例：按费率从高到低排序，输出前 50 个并生成图表：

```bash
python binance_funding_monitor.py --sort-by rate --table-limit 50 --chart-limit 50
```

运行后默认生成：

- `output/funding_rates.csv`
- `output/funding_rates.svg`
