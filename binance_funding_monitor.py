#!/usr/bin/env python3
"""Monitor Binance USDⓈ-M futures funding rates with sorting and SVG chart output."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Iterable
from urllib import error, request

BASE_URL = "https://fapi.binance.com"
EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
PREMIUM_INDEX_PATH = "/fapi/v1/premiumIndex"


def get_json(url: str) -> object:
    req = request.Request(url=url, headers={"User-Agent": "funding-monitor/1.0"})
    with request.urlopen(req, timeout=20) as resp:
        data = resp.read().decode("utf-8")
    return json.loads(data)


def fetch_perpetual_symbols() -> set[str]:
    data = get_json(f"{BASE_URL}{EXCHANGE_INFO_PATH}")
    symbols = {
        item["symbol"]
        for item in data.get("symbols", [])
        if item.get("contractType") == "PERPETUAL" and item.get("status") == "TRADING"
    }
    if not symbols:
        raise RuntimeError("No tradable perpetual symbols were returned by Binance")
    return symbols


def fetch_funding_snapshot() -> list[dict]:
    perpetual_symbols = fetch_perpetual_symbols()
    payload = get_json(f"{BASE_URL}{PREMIUM_INDEX_PATH}")

    if isinstance(payload, dict):
        payload = [payload]

    rows: list[dict] = []
    for item in payload:
        symbol = item.get("symbol")
        if symbol not in perpetual_symbols:
            continue
        try:
            funding_rate = float(item["lastFundingRate"])
            mark_price = float(item["markPrice"])
            next_funding_ms = int(item["nextFundingTime"])
        except (KeyError, TypeError, ValueError):
            continue

        rows.append(
            {
                "symbol": symbol,
                "funding_rate": funding_rate,
                "funding_rate_pct": funding_rate * 100,
                "abs_funding_rate": abs(funding_rate),
                "mark_price": mark_price,
                "next_funding_time": dt.datetime.fromtimestamp(
                    next_funding_ms / 1000, tz=dt.timezone.utc
                ),
            }
        )

    if not rows:
        raise RuntimeError("No funding-rate rows parsed from Binance premium index")
    return rows


def sort_rows(rows: list[dict], sort_by: str, descending: bool) -> list[dict]:
    key_map = {
        "rate": lambda x: x["funding_rate"],
        "abs": lambda x: x["abs_funding_rate"],
        "symbol": lambda x: x["symbol"],
    }
    return sorted(rows, key=key_map[sort_by], reverse=descending)


def print_table(rows: list[dict], limit: int) -> None:
    selected = rows[:limit]
    print(f"{'SYMBOL':<14}{'FUNDING RATE':>14}{'MARK PRICE':>16}{'NEXT FUNDING (UTC)':>24}")
    print("-" * 68)
    for row in selected:
        time_str = row["next_funding_time"].strftime("%Y-%m-%d %H:%M")
        print(
            f"{row['symbol']:<14}{row['funding_rate_pct']:>13.5f}%"
            f"{row['mark_price']:>16.8g}{time_str:>24}"
        )


def save_csv(rows: list[dict], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "symbol",
                "funding_rate",
                "funding_rate_pct",
                "abs_funding_rate",
                "mark_price",
                "next_funding_time_utc",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["symbol"],
                    row["funding_rate"],
                    row["funding_rate_pct"],
                    row["abs_funding_rate"],
                    row["mark_price"],
                    row["next_funding_time"].isoformat(),
                ]
            )


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_svg_chart(rows: list[dict], output_svg: Path, title: str, limit: int) -> None:
    chart_rows = rows[:limit]
    output_svg.parent.mkdir(parents=True, exist_ok=True)

    width, height = 1600, 900
    left, right, top, bottom = 120, 40, 80, 260
    plot_w = width - left - right
    plot_h = height - top - bottom

    values = [r["funding_rate_pct"] for r in chart_rows] or [0.0]
    max_abs = max(abs(v) for v in values) or 1.0
    y_scale = (plot_h / 2) / max_abs

    step = plot_w / max(len(chart_rows), 1)
    bar_w = step * 0.7
    baseline = top + plot_h / 2

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="40" text-anchor="middle" font-size="26" font-family="Arial">{_escape_xml(title)}</text>',
        f'<line x1="{left}" y1="{baseline}" x2="{left+plot_w}" y2="{baseline}" stroke="black" stroke-width="1"/>',
    ]

    for i, row in enumerate(chart_rows):
        val = row["funding_rate_pct"]
        bar_h = abs(val) * y_scale
        x = left + i * step + (step - bar_w) / 2
        y = baseline - bar_h if val >= 0 else baseline
        color = "#2ca02c" if val >= 0 else "#d62728"

        parts.append(
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{bar_h:.2f}" fill="{color}"/>'
        )

        label_x = x + bar_w / 2
        parts.append(
            f'<text x="{label_x:.2f}" y="{height-200}" transform="rotate(70 {label_x:.2f},{height-200})" '
            f'font-size="10" font-family="Arial">{_escape_xml(row["symbol"])}</text>'
        )

    for t in (-max_abs, -max_abs / 2, 0, max_abs / 2, max_abs):
        y = baseline - t * y_scale
        parts.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left+plot_w}" y2="{y:.2f}" stroke="#dddddd" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left-10}" y="{y+4:.2f}" text-anchor="end" font-size="12" font-family="Arial">{t:.4f}%</text>'
        )

    parts.append(
        f'<text x="{width/2}" y="{height-30}" text-anchor="middle" font-size="14" font-family="Arial">Symbols</text>'
    )
    parts.append(
        f'<text x="30" y="{height/2}" transform="rotate(-90 30,{height/2})" text-anchor="middle" font-size="14" font-family="Arial">Funding Rate (%)</text>'
    )
    parts.append("</svg>")

    output_svg.write_text("\n".join(parts), encoding="utf-8")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Binance perpetual funding rates and generate sorted charts"
    )
    parser.add_argument(
        "--sort-by",
        choices=["rate", "abs", "symbol"],
        default="abs",
        help="排序字段：rate=资金费率，abs=费率绝对值，symbol=交易对",
    )
    parser.add_argument("--ascending", action="store_true", help="启用升序排序（默认降序）")
    parser.add_argument("--table-limit", type=int, default=30, help="终端显示前 N 条")
    parser.add_argument("--chart-limit", type=int, default=40, help="图表显示前 N 条")
    parser.add_argument(
        "--output-csv", type=Path, default=Path("output/funding_rates.csv"), help="导出CSV文件路径"
    )
    parser.add_argument(
        "--output-chart", type=Path, default=Path("output/funding_rates.svg"), help="输出SVG图表路径"
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    try:
        rows = fetch_funding_snapshot()
    except error.URLError as exc:
        print(f"[ERROR] Binance API request failed: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3

    sorted_rows = sort_rows(rows, args.sort_by, descending=not args.ascending)

    print_table(sorted_rows, args.table_limit)
    save_csv(sorted_rows, args.output_csv)
    build_svg_chart(
        sorted_rows,
        output_svg=args.output_chart,
        title=f"Binance Perpetual Funding Rates ({args.sort_by}, {'ASC' if args.ascending else 'DESC'})",
        limit=args.chart_limit,
    )

    print(f"\nCSV saved to: {args.output_csv}")
    print(f"Chart saved to: {args.output_chart}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
