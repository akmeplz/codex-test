#!/usr/bin/env python3
"""Binance funding monitor: increment samples only on new funding events, refresh exposure every second."""

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
SERVER_TIME_PATH = "/fapi/v1/time"


@dataclass
class ExposureSnapshot:
    timestamp: dt.datetime
    position_value: float
    account_equity: float
    actual_leverage: float
    estimated_next_fee: float
    estimated_hourly_fee: float
    weighted_rate_per_hour: float


@dataclass
class FundingEventSnapshot:
    timestamp: dt.datetime
    realized_net: float
    realized_received: float
    realized_paid: float
    event_window_hours: float


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, recv_window: int = 5000):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window
        self.time_offset_ms = 0

    def _server_now_ms(self) -> int:
        return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + self.time_offset_ms

    def sync_server_time(self) -> None:
        req = request.Request(url=f"{BASE_URL}{SERVER_TIME_PATH}", headers={"User-Agent": "funding-stream/6.0"})
        with request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        server_ms = int(payload.get("serverTime", 0))
        local_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        if server_ms > 0:
            self.time_offset_ms = server_ms - local_ms

    def _signed_request(self, path: str, params: dict[str, object]) -> object:
        def _do_req() -> object:
            q = dict(params)
            q["timestamp"] = self._server_now_ms()
            q["recvWindow"] = self.recv_window
            encoded = urllib.parse.urlencode(q, doseq=True)
            sig = hmac.new(self.api_secret, encoded.encode("utf-8"), hashlib.sha256).hexdigest()
            url = f"{BASE_URL}{path}?{encoded}&signature={sig}"
            req = request.Request(url=url, headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "funding-stream/6.0"})
            with request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            return _do_req()
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            if exc.code == 400 and '"code":-1021' in detail.replace(' ', ''):
                self.sync_server_time()
                return _do_req()
            raise

    def _public_request(self, path: str) -> object:
        req = request.Request(url=f"{BASE_URL}{path}", headers={"User-Agent": "funding-stream/6.0"})
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_positions(self) -> list[dict]:
        data = self._signed_request(POSITION_RISK_PATH, {})
        if not isinstance(data, list):
            raise RuntimeError("positionRisk response is not list")
        return data

    def get_account_equity(self) -> float:
        data = self._signed_request(ACCOUNT_INFO_PATH, {})
        try:
            return float(data.get("totalMarginBalance", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def get_premium_index(self) -> dict[str, float]:
        data = self._public_request(PREMIUM_INDEX_PATH)
        if isinstance(data, dict):
            data = [data]
        out: dict[str, float] = {}
        for row in data:
            s = row.get("symbol")
            try:
                r = float(row.get("lastFundingRate", 0.0))
            except (TypeError, ValueError):
                continue
            if s:
                out[s] = r
        return out

    def get_funding_intervals(self) -> dict[str, int]:
        data = self._public_request(FUNDING_INFO_PATH)
        out: dict[str, int] = {}
        if isinstance(data, list):
            for row in data:
                s = row.get("symbol")
                try:
                    h = int(row.get("fundingIntervalHours", 8))
                except (TypeError, ValueError):
                    continue
                if s and h > 0:
                    out[s] = h
        return out

    def get_new_funding_incomes(self, since_ms: int) -> list[dict]:
        # Binance returns newest-first; we'll sort later.
        data = self._signed_request(
            INCOME_HISTORY_PATH,
            {"incomeType": "FUNDING_FEE", "startTime": since_ms, "limit": 1000},
        )
        if not isinstance(data, list):
            return []
        rows: list[dict] = []
        for row in data:
            try:
                income = float(row.get("income", 0.0))
                t = int(row.get("time", 0))
            except (TypeError, ValueError):
                continue
            rows.append({"income": income, "time": t})
        rows.sort(key=lambda x: x["time"])
        return rows


class DemoClient:
    def __init__(self) -> None:
        self._step = 0

    def collect_exposure(self, now: dt.datetime) -> ExposureSnapshot:
        pos = 26000 + random.uniform(-1200, 1500)
        eq = 25000 + random.uniform(-1000, 1000)
        lev = pos / eq if eq > 0 else 0.0
        rate_h = random.uniform(-0.00008, 0.00012)
        return ExposureSnapshot(
            timestamp=now,
            position_value=pos,
            account_equity=eq,
            actual_leverage=lev,
            estimated_next_fee=random.uniform(-20, 20),
            estimated_hourly_fee=random.uniform(-3, 3),
            weighted_rate_per_hour=rate_h,
        )

    def poll_new_funding(self, since_ms: int) -> list[dict]:
        self._step += 1
        if self._step % 8 != 0:
            return []
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        inc = random.uniform(-15, 20)
        return [{"income": inc, "time": now_ms}]


def resolve_api_credentials(args: argparse.Namespace) -> tuple[str, str]:
    k = args.api_key or os.getenv("BINANCE_API_KEY")
    s = args.api_secret or os.getenv("BINANCE_API_SECRET")
    if not k or not s:
        raise RuntimeError("缺少 API key/secret（--api-key --api-secret 或环境变量）")
    return k, s


def compute_exposure(client: BinanceClient, now: dt.datetime) -> ExposureSnapshot:
    positions = client.get_positions()
    rates = client.get_premium_index()
    intervals = client.get_funding_intervals()
    equity = client.get_account_equity()

    position_value = 0.0
    weighted_rate_nom = 0.0
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
        position_value += abs_notional

        estimated_next_fee += notional * rate
        estimated_hourly_fee += (notional * rate) / interval_h
        weighted_rate_nom += (rate / interval_h) * abs_notional

    weighted_rate_h = weighted_rate_nom / position_value if position_value > 0 else 0.0
    leverage = position_value / equity if equity > 0 else 0.0

    return ExposureSnapshot(
        timestamp=now,
        position_value=position_value,
        account_equity=equity,
        actual_leverage=leverage,
        estimated_next_fee=estimated_next_fee,
        estimated_hourly_fee=estimated_hourly_fee,
        weighted_rate_per_hour=weighted_rate_h,
    )


class RunningStats:
    def __init__(self) -> None:
        self.count = 0
        self.total_hours = 0.0
        self.net = 0.0
        self.received = 0.0
        self.paid = 0.0
        self.rate_weighted_nom = 0.0
        self.rate_weighted_den = 0.0
        self.estimated_hourly_sum = 0.0

        self.position_value = 0.0
        self.account_equity = 0.0
        self.actual_leverage = 0.0

    def update_exposure(self, ex: ExposureSnapshot) -> None:
        self.position_value = ex.position_value
        self.account_equity = ex.account_equity
        self.actual_leverage = ex.actual_leverage
        self.estimated_hourly_sum += ex.estimated_hourly_fee
        self.rate_weighted_nom += ex.weighted_rate_per_hour * ex.position_value
        self.rate_weighted_den += ex.position_value

    def update_funding_event(self, ev: FundingEventSnapshot) -> None:
        self.count += 1
        self.total_hours += ev.event_window_hours
        self.net += ev.realized_net
        self.received += ev.realized_received
        self.paid += ev.realized_paid

    def metrics(self) -> dict[str, float]:
        if self.count == 0:
            net_h = recv_h = paid_h = 0.0
        else:
            hours = self.total_hours if self.total_hours > 0 else 1.0
            net_h = self.net / hours
            recv_h = self.received / hours
            paid_h = self.paid / hours

        net_daily = net_h * 24
        recv_daily = recv_h * 24
        paid_daily = paid_h * 24

        pnl_daily = net_daily / self.position_value if self.position_value > 0 else 0.0
        rate_h = self.rate_weighted_nom / self.rate_weighted_den if self.rate_weighted_den > 0 else 0.0

        return {
            "count": float(self.count),
            "position_value": self.position_value,
            "account_equity": self.account_equity,
            "actual_leverage": self.actual_leverage,
            "net_total": self.net,
            "received_total": self.received,
            "paid_total": self.paid,
            "net_hourly": net_h,
            "received_hourly": recv_h,
            "paid_hourly": paid_h,
            "net_daily": net_daily,
            "received_daily": recv_daily,
            "paid_daily": paid_daily,
            "pnl_rate_daily": pnl_daily,
            "pnl_rate_monthly": pnl_daily * 30,
            "pnl_rate_yearly": pnl_daily * 365,
            "rate_daily": rate_h * 24,
            "rate_yearly": rate_h * 24 * 365,
            "avg_estimated_hourly_fee": self.estimated_hourly_sum / max(self.count, 1),
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
        elif not self.record_file.exists():
            self._init_record_file(reset=True)

        self.client: BinanceClient | None = None
        self.demo_client: DemoClient | None = None
        if args.demo_mode:
            self.demo_client = DemoClient()
        else:
            api_key, api_secret = resolve_api_credentials(args)
            self.client = BinanceClient(api_key, api_secret)
            try:
                self.client.sync_server_time()
            except Exception:
                pass

        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        self.last_income_time_ms = now_ms
        self.last_funding_sample_time = dt.datetime.now(dt.timezone.utc)

    def _init_record_file(self, reset: bool) -> None:
        mode = "w" if reset else "a"
        with self.record_file.open(mode, newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if reset:
                w.writerow(
                    [
                        "timestamp_utc",
                        "realized_net_event",
                        "realized_received_event",
                        "realized_paid_event",
                        "event_window_hours",
                        "position_value",
                        "account_equity",
                        "actual_leverage",
                    ]
                )

    def write_summary(self, m: dict[str, float]) -> None:
        with self.summary_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["metric", "value"])
            for k, v in m.items():
                w.writerow([k, v])

    def append_funding_record(self, ev: FundingEventSnapshot, ex: ExposureSnapshot) -> None:
        with self.record_file.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    ev.timestamp.isoformat(),
                    f"{ev.realized_net:.12f}",
                    f"{ev.realized_received:.12f}",
                    f"{ev.realized_paid:.12f}",
                    f"{ev.event_window_hours:.12f}",
                    f"{ex.position_value:.12f}",
                    f"{ex.account_equity:.12f}",
                    f"{ex.actual_leverage:.12f}",
                ]
            )

    def refresh_exposure(self) -> ExposureSnapshot:
        now = dt.datetime.now(dt.timezone.utc)
        if self.demo_client:
            ex = self.demo_client.collect_exposure(now)
        else:
            assert self.client is not None
            ex = compute_exposure(self.client, now)

        with self.lock:
            self.stats.update_exposure(ex)
            self.write_summary(self.stats.metrics())
        return ex

    def poll_funding_event(self, ex: ExposureSnapshot) -> None:
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        # Binance may reject future startTime with HTTP 400. Clamp to server-now window.
        since = min(self.last_income_time_ms + 1, now_ms - 1)
        if since <= 0:
            return

        if self.demo_client:
            rows = self.demo_client.poll_new_funding(since)
        else:
            assert self.client is not None
            rows = self.client.get_new_funding_incomes(since)

        if not rows:
            return

        latest_time = max(r["time"] for r in rows)
        net = sum(r["income"] for r in rows)
        recv = sum(r["income"] for r in rows if r["income"] >= 0)
        paid = sum(-r["income"] for r in rows if r["income"] < 0)

        now = dt.datetime.now(dt.timezone.utc)
        elapsed_h = max((now - self.last_funding_sample_time).total_seconds() / 3600.0, 1 / 3600)

        ev = FundingEventSnapshot(
            timestamp=now,
            realized_net=net,
            realized_received=recv,
            realized_paid=paid,
            event_window_hours=elapsed_h,
        )

        with self.lock:
            self.stats.update_funding_event(ev)
            self.series.append(
                {
                    "timestamp": now.isoformat(),
                    "net_hourly": ev.realized_net / ev.event_window_hours,
                    "received_hourly": ev.realized_received / ev.event_window_hours,
                    "paid_hourly": ev.realized_paid / ev.event_window_hours,
                }
            )
            self.append_funding_record(ev, ex)
            self.write_summary(self.stats.metrics())

        self.last_income_time_ms = latest_time
        self.last_funding_sample_time = now

    def run_tick(self) -> None:
        ex = self.refresh_exposure()
        self.poll_funding_event(ex)

    def background_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_tick()
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
                print(f"[WARN] tick failed: HTTP {exc.code} {detail}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] tick failed: {exc}", file=sys.stderr)
            self.stop_event.wait(self.args.interval_seconds)

    def start_background(self) -> None:
        t = threading.Thread(target=self.background_loop, daemon=True)
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
<div class=\"card\">每秒刷新仓位/权益/杠杆；仅当检测到新资金费入账时才新增样本。</div>
<div class=\"card grid\" id=\"metrics\"></div>
<div class=\"card\"><canvas id=\"chart\" width=\"1200\" height=\"360\"></canvas></div>
<script>
const labels=[
['count','样本数(仅资金费事件)'],
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
  if(!series.length){g.fillText('暂无资金费事件样本',20,20); return;}
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
setInterval(refresh,1000); refresh();
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
    p.add_argument("--interval-seconds", type=float, default=1.0, help="刷新仓位/权益/杠杆的轮询间隔，默认1秒")
    p.add_argument("--record-file", type=Path, default=Path("output/funding_records_stream.csv"))
    p.add_argument("--summary-csv", type=Path, default=Path("output/funding_summary_stream.csv"))
    p.add_argument("--chart-points", type=int, default=120)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--demo-mode", action="store_true")
    p.add_argument("--once", action="store_true", help="仅刷新一次仓位并检测一次资金费事件")
    return p.parse_args()


def run_web(args: argparse.Namespace) -> int:
    service = FundingService(args)
    service.run_tick()
    service.start_background()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    print(f"[INFO] Web启动: http://{args.host}:{args.port}")
    print("[INFO] 每秒更新仓位/权益/杠杆；仅新资金费事件会增加样本")
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
        service.run_tick()
        print(json.dumps(service.snapshot_payload()["metrics"], ensure_ascii=False, indent=2))
        return 0

    print("[INFO] 持续运行中（Ctrl+C停止）")
    print("[INFO] 每秒更新仓位/权益/杠杆；仅新资金费事件会增加样本")
    try:
        service.background_loop()
    except KeyboardInterrupt:
        pass
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
