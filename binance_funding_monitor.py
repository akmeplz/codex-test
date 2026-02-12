#!/usr/bin/env python3
"""Binance funding monitor: streaming-only recording + weighted live dashboard."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request

BASE_URL = "https://fapi.binance.com"
PREMIUM_INDEX_PATH = "/fapi/v1/premiumIndex"
POSITION_RISK_PATH = "/fapi/v2/positionRisk"
INCOME_HISTORY_PATH = "/fapi/v1/income"
FUNDING_INFO_PATH = "/fapi/v1/fundingInfo"
ACCOUNT_INFO_PATH = "/fapi/v2/account"


@dataclass
class FundingSnapshot:
    timestamp: dt.datetime
    realized_net_window: float
    realized_received_window: float
    realized_paid_window: float
    realized_window_hours: float
    estimated_next_fee: float
    estimated_hourly_fee: float
    total_abs_notional: float
    weighted_rate_per_hour: float
    account_equity: float
    actual_leverage: float


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, recv_window: int = 5000):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window

    def _signed_request(self, path: str, params: dict[str, object]) -> object:
        q = dict(params)
        q["timestamp"] = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        q["recvWindow"] = self.recv_window
        encoded = urllib.parse.urlencode(q, doseq=True)
        sig = hmac.new(self.api_secret, encoded.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{BASE_URL}{path}?{encoded}&signature={sig}"
        req = request.Request(url=url, headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "funding-stream/5.0"})
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _public_request(self, path: str) -> object:
        req = request.Request(url=f"{BASE_URL}{path}", headers={"User-Agent": "funding-stream/5.0"})
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_positions(self) -> list[dict]:
        data = self._signed_request(POSITION_RISK_PATH, {})
        if not isinstance(data, list):
            raise RuntimeError("positionRisk response is not list")
        return data

    def get_account_equity(self) -> float:
        data = self._signed_request(ACCOUNT_INFO_PATH, {})
        # totalMarginBalance ~ 账户总权益(USDT)
        try:
            return float(data.get("totalMarginBalance", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def get_premium_index(self) -> dict[str, float]:
        data = self._public_request(PREMIUM_INDEX_PATH)
        if isinstance(data, dict):
            data = [data]
        out: dict[str, float] = {}
        for item in data:
            s = item.get("symbol")
            try:
                r = float(item.get("lastFundingRate", 0))
            except (TypeError, ValueError):
                continue
            if s:
                out[s] = r
        return out

    def get_funding_intervals(self) -> dict[str, int]:
        data = self._public_request(FUNDING_INFO_PATH)
        out: dict[str, int] = {}
        if isinstance(data, list):
            for item in data:
                s = item.get("symbol")
                try:
                    h = int(item.get("fundingIntervalHours", 8))
                except (TypeError, ValueError):
                    continue
                if s and h > 0:
                    out[s] = h
        return out

    def get_funding_income_summary(self, start: dt.datetime, end: dt.datetime) -> tuple[float, float, float]:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        page_start = start_ms
        net = 0.0
        recv = 0.0
        paid = 0.0

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
            for row in data:
                try:
                    income = float(row.get("income", 0))
                    t = int(row.get("time", 0))
                except (TypeError, ValueError):
                    continue
                net += income
                if income >= 0:
                    recv += income
                else:
                    paid += -income
                last_time = max(last_time, t)

            if len(data) < 1000:
                break
            page_start = last_time + 1

        return net, recv, paid


class DemoClient:
    def __init__(self) -> None:
        self._v = 0.0

    def collect(self, window_hours: float, now: dt.datetime) -> FundingSnapshot:
        drift = random.uniform(-0.5, 0.7)
        self._v += drift
        recv = max(drift, 0) * window_hours + random.uniform(0.0, 0.2)
        paid = max(-drift, 0) * window_hours + random.uniform(0.0, 0.2)
        net = recv - paid
        rate_h = random.uniform(-0.00008, 0.00012)
        pos_val = 10000 + random.uniform(-1200, 1500)
        eq = 5000 + random.uniform(-500, 500)
        lev = pos_val / eq if eq > 0 else 0.0
        return FundingSnapshot(
            timestamp=now,
            realized_net_window=net,
            realized_received_window=recv,
            realized_paid_window=paid,
            realized_window_hours=window_hours,
            estimated_next_fee=net * 4,
            estimated_hourly_fee=net / window_hours if window_hours > 0 else 0.0,
            total_abs_notional=pos_val,
            weighted_rate_per_hour=rate_h,
            account_equity=eq,
            actual_leverage=lev,
        )


def resolve_api_credentials(args: argparse.Namespace) -> tuple[str, str]:
    k = args.api_key or os.getenv("BINANCE_API_KEY")
    s = args.api_secret or os.getenv("BINANCE_API_SECRET")
    if not k or not s:
        raise RuntimeError("缺少 API key/secret（--api-key --api-secret 或环境变量）")
    return k, s


def collect_snapshot(client: BinanceClient, now: dt.datetime, realized_window_hours: float) -> FundingSnapshot:
    positions = client.get_positions()
    rates = client.get_premium_index()
    intervals = client.get_funding_intervals()
    account_equity = client.get_account_equity()

    total_abs_notional = 0.0
    weighted_rate_h_sum = 0.0
    estimated_next_fee = 0.0
    estimated_hourly_fee = 0.0

    for p in positions:
        try:
            amt = float(p.get("positionAmt", 0))
            mark = float(p.get("markPrice", 0))
        except (TypeError, ValueError):
            continue
        if amt == 0 or mark == 0:
            continue

        symbol = p.get("symbol")
        if not symbol:
            continue

        rate = rates.get(symbol, 0.0)
        interval_h = float(intervals.get(symbol, 8))
        if interval_h <= 0:
            interval_h = 8.0

        notional = amt * mark
        abs_notional = abs(notional)
        total_abs_notional += abs_notional

        estimated_next_fee += notional * rate
        estimated_hourly_fee += (notional * rate) / interval_h
        weighted_rate_h_sum += (rate / interval_h) * abs_notional

    weighted_rate_per_hour = weighted_rate_h_sum / total_abs_notional if total_abs_notional > 0 else 0.0
    actual_leverage = total_abs_notional / account_equity if account_equity > 0 else 0.0

    net, recv, paid = client.get_funding_income_summary(now - dt.timedelta(hours=realized_window_hours), now)

    return FundingSnapshot(
        timestamp=now,
        realized_net_window=net,
        realized_received_window=recv,
        realized_paid_window=paid,
        realized_window_hours=realized_window_hours,
        estimated_next_fee=estimated_next_fee,
        estimated_hourly_fee=estimated_hourly_fee,
        total_abs_notional=total_abs_notional,
        weighted_rate_per_hour=weighted_rate_per_hour,
        account_equity=account_equity,
        actual_leverage=actual_leverage,
    )


class RunningStats:
    def __init__(self) -> None:
        self.count = 0
        self.total_hours = 0.0
        self.net = 0.0
        self.received = 0.0
        self.paid = 0.0
        self.weighted_rate_nom = 0.0
        self.weighted_rate_den = 0.0
        self.estimated_hourly_sum = 0.0

        self.current_position_value = 0.0
        self.current_account_equity = 0.0
        self.current_actual_leverage = 0.0

    def update(self, s: FundingSnapshot) -> None:
        self.count += 1
        self.total_hours += s.realized_window_hours
        self.net += s.realized_net_window
        self.received += s.realized_received_window
        self.paid += s.realized_paid_window
        self.estimated_hourly_sum += s.estimated_hourly_fee
        self.weighted_rate_nom += s.weighted_rate_per_hour * s.total_abs_notional
        self.weighted_rate_den += s.total_abs_notional

        self.current_position_value = s.total_abs_notional
        self.current_account_equity = s.account_equity
        self.current_actual_leverage = s.actual_leverage

    def metrics(self) -> dict[str, float]:
        if self.count == 0 or self.total_hours <= 0:
            return {
                "count": 0.0,
                "position_value": 0.0,
                "account_equity": 0.0,
                "actual_leverage": 0.0,
                "net_total": 0.0,
                "received_total": 0.0,
                "paid_total": 0.0,
                "net_hourly": 0.0,
                "received_hourly": 0.0,
                "paid_hourly": 0.0,
                "net_daily": 0.0,
                "received_daily": 0.0,
                "paid_daily": 0.0,
                "pnl_rate_daily": 0.0,
                "pnl_rate_monthly": 0.0,
                "pnl_rate_yearly": 0.0,
                "rate_daily": 0.0,
                "rate_yearly": 0.0,
                "avg_estimated_hourly_fee": 0.0,
            }

        net_h = self.net / self.total_hours
        recv_h = self.received / self.total_hours
        paid_h = self.paid / self.total_hours
        rate_h = self.weighted_rate_nom / self.weighted_rate_den if self.weighted_rate_den > 0 else 0.0

        net_daily = net_h * 24
        received_daily = recv_h * 24
        paid_daily = paid_h * 24

        if self.current_position_value > 0:
            pnl_rate_daily = net_daily / self.current_position_value
        else:
            pnl_rate_daily = 0.0
        pnl_rate_monthly = pnl_rate_daily * 30
        pnl_rate_yearly = pnl_rate_daily * 365

        return {
            "count": float(self.count),
            "position_value": self.current_position_value,
            "account_equity": self.current_account_equity,
            "actual_leverage": self.current_actual_leverage,
            "net_total": self.net,
            "received_total": self.received,
            "paid_total": self.paid,
            "net_hourly": net_h,
            "received_hourly": recv_h,
            "paid_hourly": paid_h,
            "net_daily": net_daily,
            "received_daily": received_daily,
            "paid_daily": paid_daily,
            "pnl_rate_daily": pnl_rate_daily,
            "pnl_rate_monthly": pnl_rate_monthly,
            "pnl_rate_yearly": pnl_rate_yearly,
            "rate_daily": rate_h * 24,
            "rate_yearly": rate_h * 24 * 365,
            "avg_estimated_hourly_fee": self.estimated_hourly_sum / self.count,
        }


class FundingService:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.stats = RunningStats()
        self.series: deque[dict[str, Any]] = deque(maxlen=args.chart_points)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

        self.record_file = args.record_file
        self.summary_csv = args.summary_csv
        self.record_file.parent.mkdir(parents=True, exist_ok=True)
        self.summary_csv.parent.mkdir(parents=True, exist_ok=True)

        if not args.resume:
            self._init_record_file(reset=True)
        else:
            self._init_record_file(reset=not self.record_file.exists())

        self.client: BinanceClient | None = None
        self.demo_client: DemoClient | None = None
        if args.demo_mode:
            self.demo_client = DemoClient()
        else:
            api_key, api_secret = resolve_api_credentials(args)
            self.client = BinanceClient(api_key, api_secret)

    def _init_record_file(self, reset: bool) -> None:
        mode = "w" if reset else "a"
        with self.record_file.open(mode, newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if reset:
                w.writerow([
                    "timestamp_utc",
                    "realized_net_window",
                    "realized_received_window",
                    "realized_paid_window",
                    "realized_window_hours",
                    "estimated_next_fee",
                    "estimated_hourly_fee",
                    "total_abs_notional",
                    "weighted_rate_per_hour",
                    "account_equity",
                    "actual_leverage",
                ])

    def collect_once(self) -> FundingSnapshot:
        now = dt.datetime.now(dt.timezone.utc)
        window_h = float(self.args.realized_window_hours)
        if self.demo_client:
            return self.demo_client.collect(window_h, now)
        assert self.client is not None
        return collect_snapshot(self.client, now, window_h)

    def append_record(self, s: FundingSnapshot) -> None:
        with self.record_file.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                s.timestamp.isoformat(),
                f"{s.realized_net_window:.12f}",
                f"{s.realized_received_window:.12f}",
                f"{s.realized_paid_window:.12f}",
                f"{s.realized_window_hours:.12f}",
                f"{s.estimated_next_fee:.12f}",
                f"{s.estimated_hourly_fee:.12f}",
                f"{s.total_abs_notional:.12f}",
                f"{s.weighted_rate_per_hour:.12f}",
                f"{s.account_equity:.12f}",
                f"{s.actual_leverage:.12f}",
            ])

    def write_summary(self, m: dict[str, float]) -> None:
        with self.summary_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["metric", "value"])
            for k, v in m.items():
                w.writerow([k, v])

    def run_once(self) -> None:
        snap = self.collect_once()
        with self.lock:
            self.stats.update(snap)
            self.series.append({
                "timestamp": snap.timestamp.isoformat(),
                "net_hourly": snap.realized_net_window / snap.realized_window_hours,
                "received_hourly": snap.realized_received_window / snap.realized_window_hours,
                "paid_hourly": snap.realized_paid_window / snap.realized_window_hours,
            })
            m = self.stats.metrics()
            self.append_record(snap)
            self.write_summary(m)

    def seconds_until_next_hour_plus_60(self) -> float:
        now = dt.datetime.now(dt.timezone.utc)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        target = hour_start + dt.timedelta(seconds=self.args.sample_offset_seconds)
        if now >= target:
            target = hour_start + dt.timedelta(hours=1, seconds=self.args.sample_offset_seconds)
        return max((target - now).total_seconds(), 0.0)

    def background_loop(self, delay_first: bool = False) -> None:
        if delay_first:
            wait_s = self.seconds_until_next_hour_plus_60() if self.args.align_to_hour else self.args.interval_seconds
            self.stop_event.wait(wait_s)

        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] collect failed: {exc}", file=sys.stderr)

            wait_s = self.seconds_until_next_hour_plus_60() if self.args.align_to_hour else self.args.interval_seconds
            self.stop_event.wait(wait_s)

    def start_background(self, delay_first: bool = False) -> None:
        t = threading.Thread(target=self.background_loop, kwargs={"delay_first": delay_first}, daemon=True)
        t.start()

    def snapshot_payload(self) -> dict[str, Any]:
        with self.lock:
            return {"metrics": self.stats.metrics(), "series": list(self.series)}


def build_html() -> str:
    return """<!doctype html>
<html lang=\"zh-CN\"><head>
<meta charset=\"utf-8\"/><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"/>
<title>Funding Live Dashboard</title>
<style>
body{font-family:Arial,sans-serif;margin:20px;background:#f7f9fc;color:#222}
.card{background:#fff;border:1px solid #e5eaf3;border-radius:10px;padding:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.metric{background:#fafcff;border:1px solid #edf1f8;border-radius:8px;padding:10px}
.l{font-size:12px;color:#666}.v{font-size:19px;font-weight:600}
canvas{width:100%;height:360px;border:1px solid #e5eaf3;border-radius:8px;background:#fff}
</style></head><body>
<h2>Binance 资金费动态监控（仅增量，不回算历史）</h2>
<div class=\"card\">采样规则：每小时开始第 60 秒采集（UTC），图表每 5 秒刷新。</div>
<div class=\"card grid\" id=\"metrics\"></div>
<div class=\"card\"><canvas id=\"chart\" width=\"1200\" height=\"360\"></canvas></div>
<script>
const labels=[
['count','样本数'],
['position_value','仓位价值(USDT)'],
['account_equity','账户总权益(USDT)'],
['actual_leverage','实际杠杆'],
['net_total','净资金费(累计)'],
['received_total','收到资金费(累计)'],
['paid_total','支付资金费(累计)'],
['net_hourly','净每小时'],
['received_hourly','收到每小时'],
['paid_hourly','支付每小时'],
['net_daily','净日化'],
['received_daily','收到日化'],
['paid_daily','支付日化'],
['pnl_rate_daily','日化收益率(净日化/仓位价值)'],
['pnl_rate_monthly','月化收益率'],
['pnl_rate_yearly','年化收益率'],
['rate_daily','费率日化(%)'],
['rate_yearly','费率年化(%)']
];
function fmt(k,v){
  if(['rate_daily','rate_yearly','pnl_rate_daily','pnl_rate_monthly','pnl_rate_yearly'].includes(k)) return (v*100).toFixed(4)+'%';
  if(k==='count') return String(Math.round(v));
  if(k==='actual_leverage') return Number(v).toFixed(4)+'x';
  return Number(v).toFixed(6);
}
function draw(series){
  const c=document.getElementById('chart'); const g=c.getContext('2d'); g.clearRect(0,0,c.width,c.height);
  if(!series.length){g.fillText('暂无数据',20,20); return;}
  const pad=40,w=c.width-pad*2,h=c.height-pad*2;
  const vals=[]; series.forEach(s=>vals.push(s.net_hourly,s.received_hourly,s.paid_hourly));
  let min=Math.min(...vals),max=Math.max(...vals); if(min===max){min-=1;max+=1;}
  function x(i){return pad + (series.length===1? w/2 : i*(w/(series.length-1)));}
  function y(v){return pad + h - (v-min)/(max-min)*h;}
  g.strokeStyle='#ddd'; g.beginPath(); for(let i=0;i<5;i++){let yy=pad+i*h/4; g.moveTo(pad,yy); g.lineTo(pad+w,yy);} g.stroke();
  function line(key,color){g.strokeStyle=color; g.lineWidth=2; g.beginPath(); series.forEach((s,i)=>{const xx=x(i),yy=y(s[key]); if(i===0) g.moveTo(xx,yy); else g.lineTo(xx,yy);}); g.stroke();}
  line('net_hourly','#1f77b4'); line('received_hourly','#2ca02c'); line('paid_hourly','#d62728');
  g.fillStyle='#333'; g.fillText('蓝=净, 绿=收到, 红=支付（单位: USDT/h）',pad,20);
}
async function refresh(){
  const r=await fetch('/api/live'); const d=await r.json();
  const el=document.getElementById('metrics');
  el.innerHTML=labels.map(([k,t])=>`<div class=\"metric\"><div class=\"l\">${t}</div><div class=\"v\">${fmt(k,d.metrics[k]??0)}</div></div>`).join('');
  draw(d.series||[]);
}
setInterval(refresh,5000); refresh();
</script></body></html>"""


def make_handler(service: FundingService):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                self._send(200, build_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/live":
                self._send(200, json.dumps(service.snapshot_payload(), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            self._send(404, b'{"error":"Not Found"}', "application/json")

    return Handler


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Streaming Binance funding monitor")
    p.add_argument("--api-key")
    p.add_argument("--api-secret")
    p.add_argument("--web", action="store_true", help="启动动态网页")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081, help="默认8081，不用8000")
    p.add_argument("--align-to-hour", action=argparse.BooleanOptionalAction, default=True, help="按整点偏移秒采集（默认开启）")
    p.add_argument("--sample-offset-seconds", type=int, default=60, help="每小时第N秒采集，默认60")
    p.add_argument("--interval-seconds", type=int, default=3600, help="非整点模式时使用的固定间隔")
    p.add_argument("--realized-window-hours", type=int, default=24)
    p.add_argument("--record-file", type=Path, default=Path("output/funding_records_stream.csv"))
    p.add_argument("--summary-csv", type=Path, default=Path("output/funding_summary_stream.csv"))
    p.add_argument("--chart-points", type=int, default=120)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--demo-mode", action="store_true")
    p.add_argument("--once", action="store_true")
    return p.parse_args()


def run_web(args: argparse.Namespace) -> int:
    service = FundingService(args)
    try:
        service.run_once()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] initial collect failed: {exc}", file=sys.stderr)

    service.start_background(delay_first=True)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    print(f"[INFO] Web启动: http://{args.host}:{args.port}")
    print("[INFO] 采样规则：每小时开始第60秒采集（UTC）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.stop_event.set()
        server.server_close()
    return 0


def run_cli(args: argparse.Namespace) -> int:
    service = FundingService(args)
    if args.once:
        service.run_once()
        print(json.dumps(service.snapshot_payload()["metrics"], ensure_ascii=False, indent=2))
        return 0

    print("[INFO] 持续采集中（Ctrl+C停止）")
    print("[INFO] 采样规则：每小时开始第60秒采集（UTC）")
    service.background_loop(delay_first=False)
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.web:
            return run_web(args)
        return run_cli(args)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
        print(f"[ERROR] HTTP {exc.code}: {detail}", file=sys.stderr)
        return 2
    except error.URLError as exc:
        print(f"[ERROR] 网络错误: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
