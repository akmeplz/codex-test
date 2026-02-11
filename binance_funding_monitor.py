#!/usr/bin/env python3
"""Binance USDⓈ-M funding fee tracker with weighted annualization and SVG charts."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib import error, request

BASE_URL = "https://fapi.binance.com"
PREMIUM_INDEX_PATH = "/fapi/v1/premiumIndex"
POSITION_RISK_PATH = "/fapi/v2/positionRisk"
INCOME_HISTORY_PATH = "/fapi/v1/income"


@dataclass
class FundingSnapshot:
    timestamp: dt.datetime
    realized_fee_1h: float
    estimated_next_fee: float
    total_abs_notional: float
    weighted_rate_8h: float


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, recv_window: int = 5000):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window

    def _signed_request(self, path: str, params: dict[str, object]) -> object:
        query = dict(params)
        query["timestamp"] = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        query["recvWindow"] = self.recv_window
        encoded = urllib.parse.urlencode(query, doseq=True)
        signature = hmac.new(self.api_secret, encoded.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{BASE_URL}{path}?{encoded}&signature={signature}"
        req = request.Request(url=url, headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "funding-tracker/2.0"})
        with request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
        return json.loads(payload)

    def _public_request(self, path: str) -> object:
        req = request.Request(url=f"{BASE_URL}{path}", headers={"User-Agent": "funding-tracker/2.0"})
        with request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8")
        return json.loads(payload)

    def get_positions(self) -> list[dict]:
        data = self._signed_request(POSITION_RISK_PATH, {})
        if not isinstance(data, list):
            raise RuntimeError("positionRisk response is not a list")
        return data

    def get_premium_index(self) -> dict[str, float]:
        data = self._public_request(PREMIUM_INDEX_PATH)
        if isinstance(data, dict):
            data = [data]
        rates: dict[str, float] = {}
        for item in data:
            symbol = item.get("symbol")
            try:
                rate = float(item["lastFundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            if symbol:
                rates[symbol] = rate
        return rates

    def get_funding_income_sum(self, start: dt.datetime, end: dt.datetime) -> float:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        page_start = start_ms
        total = 0.0

        while page_start < end_ms:
            data = self._signed_request(
                INCOME_HISTORY_PATH,
                {
                    "incomeType": "FUNDING_FEE",
                    "startTime": page_start,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not isinstance(data, list) or not data:
                break

            last_time = page_start
            for item in data:
                try:
                    income = float(item.get("income", 0))
                    income_time = int(item.get("time", 0))
                except (TypeError, ValueError):
                    continue
                total += income
                last_time = max(last_time, income_time)

            if len(data) < 1000:
                break
            page_start = last_time + 1

        return total


def parse_datetime(date_str: str) -> dt.datetime:
    normalized = date_str.strip().replace(" ", "T")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    else:
        parsed = parsed.astimezone(dt.timezone.utc)
    return parsed


def resolve_api_credentials(args: argparse.Namespace) -> tuple[str, str]:
    api_key = args.api_key or os.getenv("BINANCE_API_KEY")
    api_secret = args.api_secret or os.getenv("BINANCE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("API key/secret missing. Use --api-key/--api-secret or env BINANCE_API_KEY/BINANCE_API_SECRET")
    return api_key, api_secret


def collect_snapshot(client: BinanceClient, now: dt.datetime, realized_window_hours: int) -> FundingSnapshot:
    positions = client.get_positions()
    rates = client.get_premium_index()

    total_abs_notional = 0.0
    weighted_rate_sum = 0.0
    estimated_next_fee = 0.0

    for row in positions:
        try:
            amt = float(row.get("positionAmt", 0))
            mark_price = float(row.get("markPrice", 0))
        except (TypeError, ValueError):
            continue
        if amt == 0 or mark_price == 0:
            continue

        symbol = row.get("symbol")
        rate = rates.get(symbol, 0.0)
        notional = amt * mark_price
        abs_notional = abs(notional)

        total_abs_notional += abs_notional
        weighted_rate_sum += rate * abs_notional
        estimated_next_fee += notional * rate

    weighted_rate_8h = weighted_rate_sum / total_abs_notional if total_abs_notional > 0 else 0.0
    realized_fee_1h = client.get_funding_income_sum(now - dt.timedelta(hours=realized_window_hours), now)

    return FundingSnapshot(
        timestamp=now,
        realized_fee_1h=realized_fee_1h,
        estimated_next_fee=estimated_next_fee,
        total_abs_notional=total_abs_notional,
        weighted_rate_8h=weighted_rate_8h,
    )


def save_record(snapshot: FundingSnapshot, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "timestamp_utc",
                    "realized_fee_1h",
                    "estimated_next_fee_8h",
                    "total_abs_notional",
                    "weighted_rate_8h",
                ]
            )
        writer.writerow(
            [
                snapshot.timestamp.isoformat(),
                f"{snapshot.realized_fee_1h:.12f}",
                f"{snapshot.estimated_next_fee:.12f}",
                f"{snapshot.total_abs_notional:.12f}",
                f"{snapshot.weighted_rate_8h:.12f}",
            ]
        )


def load_records(csv_path: Path, start_time: dt.datetime | None) -> list[FundingSnapshot]:
    if not csv_path.exists():
        return []

    records: list[FundingSnapshot] = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = parse_datetime(row["timestamp_utc"])
                realized = float(row["realized_fee_1h"])
                estimated = float(row["estimated_next_fee_8h"])
                abs_notional = float(row["total_abs_notional"])
                rate = float(row["weighted_rate_8h"])
            except (KeyError, TypeError, ValueError):
                continue
            if start_time and ts < start_time:
                continue
            records.append(
                FundingSnapshot(
                    timestamp=ts,
                    realized_fee_1h=realized,
                    estimated_next_fee=estimated,
                    total_abs_notional=abs_notional,
                    weighted_rate_8h=rate,
                )
            )
    return records


def compute_metrics(records: list[FundingSnapshot]) -> dict[str, float]:
    if not records:
        return {
            "count": 0,
            "avg_hourly_realized": 0.0,
            "daily_fee": 0.0,
            "monthly_fee": 0.0,
            "yearly_fee": 0.0,
            "avg_weighted_rate_8h": 0.0,
            "daily_rate": 0.0,
            "monthly_rate": 0.0,
            "yearly_rate": 0.0,
            "total_realized": 0.0,
        }

    count = len(records)
    total_realized = sum(r.realized_fee_1h for r in records)
    avg_hourly_realized = total_realized / count

    sum_notional = sum(r.total_abs_notional for r in records)
    if sum_notional > 0:
        avg_weighted_rate_8h = sum(r.weighted_rate_8h * r.total_abs_notional for r in records) / sum_notional
    else:
        avg_weighted_rate_8h = 0.0

    return {
        "count": float(count),
        "avg_hourly_realized": avg_hourly_realized,
        "daily_fee": avg_hourly_realized * 24,
        "monthly_fee": avg_hourly_realized * 24 * 30,
        "yearly_fee": avg_hourly_realized * 24 * 365,
        "avg_weighted_rate_8h": avg_weighted_rate_8h,
        "daily_rate": avg_weighted_rate_8h * 3,
        "monthly_rate": avg_weighted_rate_8h * 90,
        "yearly_rate": avg_weighted_rate_8h * 1095,
        "total_realized": total_realized,
    }


def write_summary_csv(metrics: dict[str, float], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        for key, value in metrics.items():
            writer.writerow([key, value])


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_charts(records: list[FundingSnapshot], metrics: dict[str, float], output_svg: Path) -> None:
    output_svg.parent.mkdir(parents=True, exist_ok=True)
    width, height = 1500, 900

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="750" y="42" text-anchor="middle" font-size="28" font-family="Arial">Binance Funding Fee Monitor</text>',
    ]

    # Panel 1: cumulative realized funding fee line chart.
    p1 = {"x": 90, "y": 90, "w": 1320, "h": 430}
    parts.append(f'<rect x="{p1["x"]}" y="{p1["y"]}" width="{p1["w"]}" height="{p1["h"]}" fill="#fafafa" stroke="#dddddd"/>')
    parts.append('<text x="105" y="120" font-size="18" font-family="Arial">Cumulative Realized Funding Fee (USDT)</text>')

    cumulative = []
    total = 0.0
    for row in records:
        total += row.realized_fee_1h
        cumulative.append(total)

    if cumulative:
        min_v = min(cumulative)
        max_v = max(cumulative)
        if min_v == max_v:
            min_v -= 1
            max_v += 1

        def map_x(i: int) -> float:
            if len(cumulative) == 1:
                return p1["x"] + p1["w"] / 2
            return p1["x"] + (i / (len(cumulative) - 1)) * p1["w"]

        def map_y(v: float) -> float:
            return p1["y"] + p1["h"] - ((v - min_v) / (max_v - min_v)) * p1["h"]

        poly = " ".join(f"{map_x(i):.1f},{map_y(v):.1f}" for i, v in enumerate(cumulative))
        parts.append(f'<polyline fill="none" stroke="#1f77b4" stroke-width="2.5" points="{poly}"/>')
        for tick in range(5):
            val = min_v + (max_v - min_v) * tick / 4
            y = map_y(val)
            parts.append(f'<line x1="{p1["x"]}" y1="{y:.1f}" x2="{p1["x"]+p1["w"]}" y2="{y:.1f}" stroke="#efefef"/>')
            parts.append(f'<text x="{p1["x"]-10}" y="{y+4:.1f}" text-anchor="end" font-size="12" font-family="Arial">{val:.4f}</text>')

    # Panel 2: annualized rates bar chart.
    p2 = {"x": 90, "y": 560, "w": 650, "h": 290}
    parts.append(f'<rect x="{p2["x"]}" y="{p2["y"]}" width="{p2["w"]}" height="{p2["h"]}" fill="#fafafa" stroke="#dddddd"/>')
    parts.append('<text x="105" y="590" font-size="18" font-family="Arial">Weighted Funding Rate Annualization</text>')
    rate_vals = [
        ("Daily", metrics["daily_rate"] * 100),
        ("Monthly", metrics["monthly_rate"] * 100),
        ("Yearly", metrics["yearly_rate"] * 100),
    ]
    max_rate = max(abs(v) for _, v in rate_vals) or 1.0
    base_y = p2["y"] + p2["h"] - 35
    bar_w = 120
    spacing = 170
    for idx, (name, val) in enumerate(rate_vals):
        x = p2["x"] + 90 + idx * spacing
        h = (abs(val) / max_rate) * (p2["h"] - 90)
        y = base_y - h if val >= 0 else base_y
        color = "#2ca02c" if val >= 0 else "#d62728"
        parts.append(f'<rect x="{x}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" fill="{color}"/>')
        parts.append(f'<text x="{x+bar_w/2}" y="{base_y+22}" text-anchor="middle" font-size="13" font-family="Arial">{name}</text>')
        parts.append(f'<text x="{x+bar_w/2}" y="{y-8:.1f}" text-anchor="middle" font-size="12" font-family="Arial">{val:.4f}%</text>')

    # Panel 3: annualized fee numbers.
    p3 = {"x": 770, "y": 560, "w": 640, "h": 290}
    parts.append(f'<rect x="{p3["x"]}" y="{p3["y"]}" width="{p3["w"]}" height="{p3["h"]}" fill="#fafafa" stroke="#dddddd"/>')
    parts.append('<text x="785" y="590" font-size="18" font-family="Arial">Realized Funding Fee Projection</text>')

    info_lines = [
        f"Samples: {int(metrics['count'])}",
        f"Total realized: {metrics['total_realized']:.8f} USDT",
        f"Avg hourly realized: {metrics['avg_hourly_realized']:.8f} USDT",
        f"Daily projection: {metrics['daily_fee']:.8f} USDT",
        f"Monthly projection: {metrics['monthly_fee']:.8f} USDT",
        f"Yearly projection: {metrics['yearly_fee']:.8f} USDT",
        f"Weighted avg 8h rate: {metrics['avg_weighted_rate_8h']*100:.6f}%",
    ]
    for idx, line in enumerate(info_lines):
        y = 630 + idx * 30
        parts.append(
            f'<text x="790" y="{y}" font-size="16" font-family="Arial" fill="#222222">{_escape_xml(line)}</text>'
        )

    parts.append("</svg>")
    output_svg.write_text("\n".join(parts), encoding="utf-8")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track Binance position funding fee and annualized metrics")
    parser.add_argument("--api-key", help="Binance API key (or env BINANCE_API_KEY)")
    parser.add_argument("--api-secret", help="Binance API secret (or env BINANCE_API_SECRET)")
    parser.add_argument("--record-file", type=Path, default=Path("output/funding_records.csv"), help="小时记录文件")
    parser.add_argument("--summary-csv", type=Path, default=Path("output/funding_summary.csv"), help="汇总结果CSV")
    parser.add_argument("--chart-file", type=Path, default=Path("output/funding_summary.svg"), help="汇总图表SVG")
    parser.add_argument("--start-date", help="统计起始时间，支持 2025-01-01 或 2025-01-01T08:00:00")
    parser.add_argument("--realized-window-hours", type=int, default=1, help="每次采集时回看的已实现资金费窗口（小时）")
    parser.add_argument("--skip-record", action="store_true", help="只读历史记录并计算，不向CSV追加新记录")
    return parser.parse_args(list(argv))


def print_metrics(metrics: dict[str, float], start_date: dt.datetime | None) -> None:
    print("=" * 72)
    print("Binance 资金费用统计（加权）")
    print("=" * 72)
    if start_date:
        print(f"统计起始时间(UTC): {start_date.isoformat()}")
    print(f"样本数量: {int(metrics['count'])}")
    print(f"总已实现资金费: {metrics['total_realized']:.8f} USDT")
    print(f"平均每小时已实现资金费: {metrics['avg_hourly_realized']:.8f} USDT")
    print(f"资金费投影(日化): {metrics['daily_fee']:.8f} USDT")
    print(f"资金费投影(月化): {metrics['monthly_fee']:.8f} USDT")
    print(f"资金费投影(年化): {metrics['yearly_fee']:.8f} USDT")
    print("-" * 72)
    print(f"加权平均8h费率: {metrics['avg_weighted_rate_8h']*100:.6f}%")
    print(f"费率日化(3次/天): {metrics['daily_rate']*100:.6f}%")
    print(f"费率月化(30天): {metrics['monthly_rate']*100:.6f}%")
    print(f"费率年化(365天): {metrics['yearly_rate']*100:.6f}%")


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    start_dt = parse_datetime(args.start_date) if args.start_date else None

    try:
        if not args.skip_record:
            api_key, api_secret = resolve_api_credentials(args)
            client = BinanceClient(api_key, api_secret)
            now = dt.datetime.now(dt.timezone.utc)
            snapshot = collect_snapshot(client, now, realized_window_hours=args.realized_window_hours)
            save_record(snapshot, args.record_file)
            print(f"[INFO] 已写入小时记录: {args.record_file}")

        records = load_records(args.record_file, start_dt)
        metrics = compute_metrics(records)
        write_summary_csv(metrics, args.summary_csv)
        build_charts(records, metrics, args.chart_file)
        print_metrics(metrics, start_dt)
        print(f"\n汇总CSV: {args.summary_csv}")
        print(f"图表SVG: {args.chart_file}")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
        print(f"[ERROR] HTTP {exc.code}: {detail}", file=sys.stderr)
        return 2
    except error.URLError as exc:
        print(f"[ERROR] API network failed: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
