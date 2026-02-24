#!/usr/bin/env python3
"""Binance funding monitor with event-driven sampling and local historical backtracking."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import hmac
import json
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.parse
from collections import deque
from http.client import IncompleteRead
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
DEFAULT_DATA_DIR = Path.home() / ".binance_funding_monitor"


@dataclass
class ExposureSnapshot:
    timestamp: dt.datetime
    position_value: float
    account_equity: float
    actual_leverage: float
    estimated_next_fee: float
    estimated_hourly_fee: float
    weighted_rate_per_hour: float
    expected_event_window_hours: float


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
        self._premium_cache_at = 0.0
        self._premium_cache_data: dict[str, float] = {}
        self._interval_cache_at = 0.0
        self._interval_cache_data: dict[str, int] = {}

    def _load_json(self, req: request.Request, timeout: int = 30, retries: int = 2) -> object:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with request.urlopen(req, timeout=timeout) as resp:
                    payload = resp.read()
                return json.loads(payload.decode("utf-8"))
            except (IncompleteRead, json.JSONDecodeError, error.URLError, ssl.SSLError) as exc:
                last_exc = exc
                if attempt >= retries:
                    raise
                time.sleep(0.25 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unexpected json load failure")

    def _server_now_ms(self) -> int:
        return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + self.time_offset_ms

    def _parse_retry_after(self, exc: error.HTTPError, detail: str) -> float:
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                return max(float(header), 0.0)
            except ValueError:
                pass
        m = re.search(r'"retryAfter"\s*:\s*([0-9]+)', detail)
        if m:
            try:
                return max(float(m.group(1)), 0.0)
            except ValueError:
                pass
        return 0.0

    def sync_server_time(self) -> None:
        req = request.Request(url=f"{BASE_URL}{SERVER_TIME_PATH}", headers={"User-Agent": "funding-stream/6.0"})
        payload = self._load_json(req, timeout=15, retries=2)
        if not isinstance(payload, dict):
            return
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
            return self._load_json(req, timeout=30, retries=2)

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
        return self._load_json(req, timeout=30, retries=2)

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
        now = time.time()
        if (now - self._premium_cache_at) < 60 and self._premium_cache_data:
            return dict(self._premium_cache_data)

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
        self._premium_cache_at = now
        self._premium_cache_data = dict(out)
        return out

    def get_funding_intervals(self) -> dict[str, int]:
        now = time.time()
        if (now - self._interval_cache_at) < 21600 and self._interval_cache_data:
            return dict(self._interval_cache_data)

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
        self._interval_cache_at = now
        self._interval_cache_data = dict(out)
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
    def get_funding_incomes_between(self, start_ms: int, end_ms: int | None = None, limit: int = 1000) -> list[dict]:
        if start_ms <= 0:
            return []

        end_bound = end_ms if end_ms is not None else int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        if end_bound < start_ms:
            return []

        rows: list[dict] = []
        cursor_end = end_bound
        while cursor_end >= start_ms:
            data = self._signed_request(
                INCOME_HISTORY_PATH,
                {
                    "incomeType": "FUNDING_FEE",
                    "startTime": start_ms,
                    "endTime": cursor_end,
                    "limit": limit,
                },
            )
            if not isinstance(data, list) or not data:
                break

            batch: list[dict] = []
            for row in data:
                try:
                    income = float(row.get("income", 0.0))
                    t = int(row.get("time", 0))
                except (TypeError, ValueError):
                    continue
                if t < start_ms or t > cursor_end:
                    continue
                batch.append({"income": income, "time": t})

            if not batch:
                break

            batch.sort(key=lambda x: x["time"])
            rows.extend(batch)

            oldest_t = batch[0]["time"]
            if len(batch) < limit or oldest_t <= start_ms:
                break
            next_cursor_end = oldest_t - 1
            if next_cursor_end >= cursor_end:
                break
            cursor_end = next_cursor_end

        rows.sort(key=lambda x: x["time"])
        deduped: list[dict] = []
        seen: set[tuple[int, float]] = set()
        for r in rows:
            key = (int(r["time"]), float(r["income"]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        return deduped



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
            expected_event_window_hours=random.choice([1.0, 4.0, 8.0]),
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
    weighted_interval_nom = 0.0

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
        weighted_interval_nom += interval_h * abs_notional

    weighted_rate_h = weighted_rate_nom / position_value if position_value > 0 else 0.0
    expected_window_h = weighted_interval_nom / position_value if position_value > 0 else 8.0
    leverage = position_value / equity if equity > 0 else 0.0

    return ExposureSnapshot(
        timestamp=now,
        position_value=position_value,
        account_equity=equity,
        actual_leverage=leverage,
        estimated_next_fee=estimated_next_fee,
        estimated_hourly_fee=estimated_hourly_fee,
        weighted_rate_per_hour=weighted_rate_h,
        expected_event_window_hours=expected_window_h,
    )




def parse_time_value(value: str) -> dt.datetime:
    raw = value.strip()
    if raw == "":
        raise ValueError("empty datetime")

    # unix epoch support (seconds or milliseconds)
    if raw.isdigit() or (raw.startswith('-') and raw[1:].isdigit()):
        ts = int(raw)
        if abs(ts) > 10_000_000_000:
            ts = ts / 1000
        parsed = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
        return parsed

    v = raw.replace('/', '-').replace(' ', 'T')
    if v.endswith('Z'):
        v = v[:-1] + '+00:00'

    parsed = dt.datetime.fromisoformat(v)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    else:
        parsed = parsed.astimezone(dt.timezone.utc)
    return parsed


def row_pick(row: dict[str, Any], names: tuple[str, ...], default: str = "") -> str:
    target = {n.strip().lower() for n in names}
    for k, v in row.items():
        nk = str(k).replace('﻿', '').strip().lower()
        if nk not in target:
            continue
        if v is None:
            continue
        text = str(v).strip()
        if text != "":
            return text
    return default


def iter_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    text = path.read_text(encoding='utf-8', errors='ignore')
    if not text.strip():
        return []

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;	|')
        delim = dialect.delimiter
    except Exception:
        delim = ','

    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return [dict(r) for r in reader]


def row_pick_float(row: dict[str, Any], names: tuple[str, ...], default: float = 0.0) -> float:
    text = row_pick(row, names, "")
    if text == "":
        return default
    return float(text)


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
        self.expected_event_window_hours = 8.0

    def update_exposure(self, ex: ExposureSnapshot) -> None:
        self.position_value = ex.position_value
        self.account_equity = ex.account_equity
        self.actual_leverage = ex.actual_leverage
        self.expected_event_window_hours = ex.expected_event_window_hours
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
            "realized_rate_daily": pnl_daily,
            "realized_rate_yearly": pnl_daily * 365,
            "estimated_rate_daily": rate_h * 24,
            "estimated_rate_yearly": rate_h * 24 * 365,
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

        self.source_record_files: list[Path] = [self.record_file]
        legacy_default = Path("output/funding_records_stream.csv")
        if self.record_file.resolve() != legacy_default.resolve() and legacy_default.exists():
            self.source_record_files.append(legacy_default)

        if args.reset_records:
            self._init_record_file(reset=True)
        elif (not self.record_file.exists()) or self.record_file.stat().st_size == 0:
            self._init_record_file(reset=True)

        history_last_time = self._bootstrap_from_records()

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

        now = dt.datetime.now(dt.timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        history_last_ms = int(history_last_time.timestamp() * 1000) if history_last_time else now_ms
        self.last_income_time_ms = history_last_ms
        self.last_funding_sample_time = history_last_time or now
        self._history_cache_rows: list[dict[str, float | str]] = []
        self._history_cache_key: tuple[int | None, int | None] | None = None
        self._history_cache_at = 0.0
        self._history_cache_error: str | None = None

        self._last_exposure_at = 0.0
        self._last_funding_poll_at = 0.0
        self._rate_limit_backoff_s = 0.0
        self._cooldown_until = 0.0
        self._last_rate_limit_error = ""

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

    def _bootstrap_from_records(self) -> dt.datetime | None:
        last_ts: dt.datetime | None = None
        for file in self.source_record_files:
            if not file.exists():
                continue
            for row in iter_csv_rows(file):
                try:
                    ts = parse_time_value(row_pick(row, ("timestamp_utc", "timestamp", "time"), ""))
                    net = row_pick_float(row, ("realized_net_event", "realized_net", "net", "income"), 0.0)
                    recv = row_pick_float(row, ("realized_received_event", "realized_received", "recv", "received"), 0.0)
                    paid = row_pick_float(row, ("realized_paid_event", "realized_paid", "paid"), 0.0)
                    hours = max(row_pick_float(row, ("event_window_hours", "hours", "window_hours"), 0.0), 1 / 3600)
                    pos = row_pick_float(row, ("position_value", "total_abs_notional", "notional"), 0.0)
                    eq = row_pick_float(row, ("account_equity", "equity", "total_margin_balance"), 0.0)
                    lev = row_pick_float(row, ("actual_leverage", "leverage"), 0.0)
                except (TypeError, ValueError):
                    continue

                self.stats.update_funding_event(
                    FundingEventSnapshot(
                        timestamp=ts,
                        realized_net=net,
                        realized_received=recv,
                        realized_paid=paid,
                        event_window_hours=hours,
                    )
                )
                self.series.append(
                    {
                        "timestamp": ts.isoformat(),
                        "net_hourly": net / hours,
                        "received_hourly": recv / hours,
                        "paid_hourly": paid / hours,
                    }
                )

                self.stats.position_value = pos
                self.stats.account_equity = eq
                self.stats.actual_leverage = lev
                last_ts = ts

        return last_ts

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

    def refresh_exposure(self, force: bool = False) -> ExposureSnapshot:
        now_ts = time.time()
        now = dt.datetime.now(dt.timezone.utc)
        with self.lock:
            if (not force) and (now_ts - self._last_exposure_at) < self.args.exposure_poll_seconds:
                return ExposureSnapshot(
                    timestamp=now,
                    position_value=self.stats.position_value,
                    account_equity=self.stats.account_equity,
                    actual_leverage=self.stats.actual_leverage,
                    estimated_next_fee=0.0,
                    estimated_hourly_fee=0.0,
                    weighted_rate_per_hour=0.0,
                    expected_event_window_hours=max(self.stats.expected_event_window_hours, self.args.min_event_window_hours),
                )

        if self.demo_client:
            ex = self.demo_client.collect_exposure(now)
        else:
            assert self.client is not None
            ex = compute_exposure(self.client, now)

        with self.lock:
            self.stats.update_exposure(ex)
            self._last_exposure_at = now_ts
            self.write_summary(self.stats.metrics())
        return ex

    def poll_funding_event(self, ex: ExposureSnapshot, force: bool = False) -> None:
        now_ts = time.time()
        if (not force) and (now_ts - self._last_funding_poll_at) < self.args.funding_poll_seconds:
            return
        self._last_funding_poll_at = now_ts

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
        floor_h = max(self.args.min_event_window_hours, ex.expected_event_window_hours)
        elapsed_h = max((latest_time - self.last_income_time_ms) / 3_600_000.0, floor_h)

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

    def run_tick(self, force: bool = False) -> None:
        ex = self.refresh_exposure(force=force)
        self.poll_funding_event(ex, force=force)

    def _register_rate_limit(self, http_code: int, detail: str = "", retry_after_s: float = 0.0) -> None:
        if http_code == 418:
            base = 120.0
            cap = 900.0
        else:
            base = 30.0
            cap = 300.0
        self._rate_limit_backoff_s = min(cap, max(base, self._rate_limit_backoff_s * 2 if self._rate_limit_backoff_s else base))
        cooldown = max(self._rate_limit_backoff_s, retry_after_s)
        self._cooldown_until = time.time() + cooldown
        self._last_rate_limit_error = f"HTTP {http_code} cooldown {cooldown:.0f}s"
        if detail:
            self._last_rate_limit_error += f": {detail[:180]}"

    def background_loop(self) -> None:
        while not self.stop_event.is_set():
            now_ts = time.time()
            if now_ts < self._cooldown_until:
                self.stop_event.wait(min(self.args.interval_seconds, self._cooldown_until - now_ts))
                continue
            try:
                self.run_tick()
                self._rate_limit_backoff_s = 0.0
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
                retry_after_s = 0.0
                if self.client is not None:
                    retry_after_s = self.client._parse_retry_after(exc, detail)
                if exc.code in (418, 429):
                    self._register_rate_limit(exc.code, detail=detail, retry_after_s=retry_after_s)
                    print(f"[WARN] rate limit hit HTTP {exc.code}, strict cooldown active", file=sys.stderr)
                else:
                    print(f"[WARN] tick failed: HTTP {exc.code} {detail}", file=sys.stderr)
            except IncompleteRead as exc:
                print(f"[WARN] tick failed: incomplete read ({exc})", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] tick failed: {exc}", file=sys.stderr)
            self.stop_event.wait(self.args.interval_seconds)

    def start_background(self) -> None:
        t = threading.Thread(target=self.background_loop, daemon=True)
        t.start()

    def _load_records_in_range(
        self,
        start: dt.datetime | None,
        end: dt.datetime | None,
    ) -> list[dict[str, float | str]]:
        rows: list[dict[str, float | str]] = []
        for file in self.source_record_files:
            if not file.exists():
                continue
            for row in iter_csv_rows(file):
                try:
                    ts = parse_time_value(row_pick(row, ("timestamp_utc", "timestamp", "time"), ""))
                    net = row_pick_float(row, ("realized_net_event", "realized_net", "net", "income"), 0.0)
                    recv = row_pick_float(row, ("realized_received_event", "realized_received", "recv", "received"), 0.0)
                    paid = row_pick_float(row, ("realized_paid_event", "realized_paid", "paid"), 0.0)
                    hours = row_pick_float(row, ("event_window_hours", "hours", "window_hours"), 0.0)
                    pos = row_pick_float(row, ("position_value", "total_abs_notional", "notional"), 0.0)
                    eq = row_pick_float(row, ("account_equity", "equity", "total_margin_balance"), 0.0)
                    lev = row_pick_float(row, ("actual_leverage", "leverage"), 0.0)
                except (TypeError, ValueError):
                    continue

                if start and ts < start:
                    continue
                if end and ts > end:
                    continue

                rows.append(
                    {
                        "timestamp": ts.isoformat(),
                        "net": net,
                        "recv": recv,
                        "paid": paid,
                        "hours": hours,
                        "position_value": pos,
                        "account_equity": eq,
                        "actual_leverage": lev,
                    }
                )
        return rows

    def _metrics_from_records(self, rows: list[dict[str, float | str]]) -> tuple[dict[str, float], list[dict[str, str | float]]]:
        with self.lock:
            base = self.stats.metrics()

        if not rows:
            # keep exposure realtime, funding interval data as zeroed
            m = dict(base)
            m.update(
                {
                    "count": 0.0,
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
                    "realized_rate_daily": 0.0,
                    "realized_rate_yearly": 0.0,
                }
            )
            return m, []

        count = float(len(rows))
        net_total = sum(float(r["net"]) for r in rows)
        recv_total = sum(float(r["recv"]) for r in rows)
        paid_total = sum(float(r["paid"]) for r in rows)
        total_hours = sum(max(float(r["hours"]), self.args.min_event_window_hours) for r in rows)
        if total_hours <= 0:
            total_hours = max(count / 24.0, self.args.min_event_window_hours)

        net_h = net_total / total_hours
        recv_h = recv_total / total_hours
        paid_h = paid_total / total_hours
        net_daily = net_h * 24
        recv_daily = recv_h * 24
        paid_daily = paid_h * 24

        weighted_pos = sum(max(float(r["hours"]), self.args.min_event_window_hours) * float(r["position_value"]) for r in rows)
        weighted_eq = sum(max(float(r["hours"]), self.args.min_event_window_hours) * float(r["account_equity"]) for r in rows)
        weighted_lev = sum(max(float(r["hours"]), self.args.min_event_window_hours) * float(r["actual_leverage"]) for r in rows)

        position_value = weighted_pos / total_hours if total_hours > 0 else 0.0
        account_equity = weighted_eq / total_hours if total_hours > 0 else 0.0
        actual_leverage = weighted_lev / total_hours if total_hours > 0 else 0.0

        pnl_daily = net_daily / position_value if position_value > 0 else 0.0

        m = dict(base)
        m.update(
            {
                "count": count,
                "position_value": position_value,
                "account_equity": account_equity,
                "actual_leverage": actual_leverage,
                "net_total": net_total,
                "received_total": recv_total,
                "paid_total": paid_total,
                "net_hourly": net_h,
                "received_hourly": recv_h,
                "paid_hourly": paid_h,
                "net_daily": net_daily,
                "received_daily": recv_daily,
                "paid_daily": paid_daily,
                "pnl_rate_daily": pnl_daily,
                "pnl_rate_monthly": pnl_daily * 30,
                "pnl_rate_yearly": pnl_daily * 365,
                "realized_rate_daily": pnl_daily,
                "realized_rate_yearly": pnl_daily * 365,
            }
        )

        series = [
            {
                "timestamp": str(r["timestamp"]),
                "net_hourly": float(r["net"]) / max(float(r["hours"]), self.args.min_event_window_hours),
                "received_hourly": float(r["recv"]) / max(float(r["hours"]), self.args.min_event_window_hours),
                "paid_hourly": float(r["paid"]) / max(float(r["hours"]), self.args.min_event_window_hours),
            }
            for r in rows
        ]
        return m, series

    def _rows_from_income_events(self, incomes: list[dict], start: dt.datetime | None, end: dt.datetime | None) -> list[dict[str, float | str]]:
        if not incomes:
            return []

        grouped: dict[int, dict[str, float]] = {}
        for row in incomes:
            t = int(row.get("time", 0))
            income = float(row.get("income", 0.0))
            g = grouped.setdefault(t, {"net": 0.0, "recv": 0.0, "paid": 0.0})
            g["net"] += income
            if income >= 0:
                g["recv"] += income
            else:
                g["paid"] += -income

        times = sorted(grouped.keys())
        if not times:
            return []

        if len(times) >= 2:
            default_h = max((times[1] - times[0]) / 3_600_000, 1 / 3600)
        else:
            default_h = 8.0

        rows: list[dict[str, float | str]] = []
        with self.lock:
            live = self.stats.metrics()

        prev_t: int | None = None
        for t in times:
            if prev_t is None:
                h = default_h
            else:
                h = max((t - prev_t) / 3_600_000, 1 / 3600)
            prev_t = t
            ts = dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc)
            if start and ts < start:
                continue
            if end and ts > end:
                continue
            g = grouped[t]
            rows.append(
                {
                    "timestamp": ts.isoformat(),
                    "net": g["net"],
                    "recv": g["recv"],
                    "paid": g["paid"],
                    "hours": h,
                    "position_value": live["position_value"],
                    "account_equity": live["account_equity"],
                    "actual_leverage": live["actual_leverage"],
                }
            )
        return rows

    def _load_binance_rows_in_range(self, start: dt.datetime | None, end: dt.datetime | None) -> list[dict[str, float | str]]:
        if self.client is None or start is None:
            return []

        start_ms = int(start.timestamp() * 1000)
        end_ms = int((end or dt.datetime.now(dt.timezone.utc)).timestamp() * 1000)
        if end_ms < start_ms:
            return []

        key = (start_ms, end_ms)
        now = time.time()
        with self.lock:
            if now < self._cooldown_until:
                self._history_cache_error = self._last_rate_limit_error or "rate limited"
                return []
            if self._history_cache_key == key and (now - self._history_cache_at) < 30:
                return [dict(r) for r in self._history_cache_rows]

        try:
            incomes = self.client.get_funding_incomes_between(start_ms=start_ms, end_ms=end_ms)
            rows = self._rows_from_income_events(incomes, start=start, end=end)
            err_msg = None
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if hasattr(exc, "read") else ""
            retry_after_s = self.client._parse_retry_after(exc, detail)
            if exc.code in (418, 429):
                self._register_rate_limit(exc.code, detail=detail, retry_after_s=retry_after_s)
                err_msg = self._last_rate_limit_error
            else:
                err_msg = f"HTTP {exc.code}: {detail}"
            rows = []
        except Exception as exc:  # noqa: BLE001
            rows = []
            err_msg = str(exc)

        with self.lock:
            self._history_cache_key = key
            self._history_cache_at = now
            self._history_cache_rows = [dict(r) for r in rows]
            self._history_cache_error = err_msg

        return rows


    def snapshot_payload(self, start: dt.datetime | None = None, end: dt.datetime | None = None) -> dict[str, Any]:
        rows = self._load_records_in_range(start, end)
        source = "local"
        if not rows:
            rows = self._load_binance_rows_in_range(start, end)
            source = "binance" if rows else "none"

        if rows:
            metrics, series = self._metrics_from_records(rows)
            with self.lock:
                live = self.stats.metrics()
            metrics.update(
                {
                    "position_value": live["position_value"],
                    "account_equity": live["account_equity"],
                    "actual_leverage": live["actual_leverage"],
                    "estimated_rate_daily": live["estimated_rate_daily"],
                    "estimated_rate_yearly": live["estimated_rate_yearly"],
                    "rate_daily": live["rate_daily"],
                    "rate_yearly": live["rate_yearly"],
                    "avg_estimated_hourly_fee": live["avg_estimated_hourly_fee"],
                }
            )
            return {"metrics": metrics, "series": series, "source": source}

        if start is not None or end is not None:
            metrics, series = self._metrics_from_records([])
            payload = {"metrics": metrics, "series": series, "source": source}
            if self._history_cache_error and start is not None:
                payload["warning"] = f"binance history query failed: {self._history_cache_error}"
            return payload

        with self.lock:
            live_metrics = self.stats.metrics()
            live_series = list(self.series)
        payload_source = "live" if (live_metrics.get("count", 0.0) > 0 or live_series) else "none"
        return {"metrics": live_metrics, "series": live_series, "source": payload_source}


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
<h2>Binance 资金费动态监控（支持历史回算）</h2>
<div class=\"card\">默认基于本地历史记录回算统计；每秒刷新仓位/权益/杠杆；仅当检测到新资金费入账时新增样本。</div>
<div class="card">
  <label>开始时间(UTC): <input id="start" type="datetime-local"></label>
  <label style="margin-left:12px;">结束时间(UTC): <input id="end" type="datetime-local"></label>
  <button id="apply" style="margin-left:12px;">应用时间区间</button>
  <button id="clear" style="margin-left:8px;">清空</button>
  <span id="errmsg" style="margin-left:12px;color:#b00020;"></span><span id="source" style="margin-left:12px;color:#555;"></span>
</div>
<div class="card grid" id="metrics"></div>
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
['estimated_rate_daily','预计费率日化(基于当前持仓)'],
['estimated_rate_yearly','预计费率年化']
];
function fmt(k,v){
  if(['rate_daily','rate_yearly','estimated_rate_daily','estimated_rate_yearly','realized_rate_daily','realized_rate_yearly','pnl_rate_daily','pnl_rate_monthly','pnl_rate_yearly'].includes(k)) return (v*100).toFixed(4)+'%';
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
  const err=document.getElementById('errmsg');
  const src=document.getElementById('source');
  if(err) err.textContent='';
  if(src) src.textContent='';
  try{
    const sp=new URLSearchParams();
    const sEl=document.getElementById('start');
    const eEl=document.getElementById('end');
    const sv=sEl ? sEl.value : '';
    const ev=eEl ? eEl.value : '';
    if(sv) sp.set('start', sv);
    if(ev) sp.set('end', ev);
    const url='/api/live'+(sp.toString()?'?'+sp.toString():'');
    const r=await fetch(url);
    const d=await r.json();
    if(!r.ok) throw new Error(d.error || ('HTTP '+r.status));
    const el=document.getElementById('metrics');
    el.innerHTML=labels.map(([k,t])=>`<div class="metric"><div class="l">${t}</div><div class="v">${fmt(k,d.metrics[k]??0)}</div></div>`).join('');
    if(src && d.source) src.textContent='数据来源: '+d.source;
    if(err && d.warning) err.textContent=String(d.warning);
    draw(d.series||[]);
  }catch(ex){
    if(err) err.textContent=String(ex.message || ex);
  }
}
const applyBtn=document.getElementById('apply');
if(applyBtn) applyBtn.addEventListener('click', refresh);
const clearBtn=document.getElementById('clear');
if(clearBtn) clearBtn.addEventListener('click', ()=>{
  const s=document.getElementById('start');
  const e=document.getElementById('end');
  if(s) s.value=''; if(e) e.value='';
  refresh();
});
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
                q = urllib.parse.parse_qs(parsed.query)
                try:
                    start = parse_time_value(q.get("start", [""])[0]) if q.get("start", [""])[0] else None
                    end = parse_time_value(q.get("end", [""])[0]) if q.get("end", [""])[0] else None
                except ValueError:
                    self._send(400, b'{"error":"invalid datetime format"}', "application/json")
                    return
                if start and end and end < start:
                    self._send(400, b'{"error":"end must be >= start"}', "application/json")
                    return
                payload = service.snapshot_payload(start=start, end=end)
                self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
                return
            self._send(404, b'{"error":"Not Found"}', "application/json")

    return Handler


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Streaming Binance funding monitor")
    p.add_argument("--api-key")
    p.add_argument("--api-secret")
    p.add_argument("--web", action="store_true", help="启动动态网页")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000, help="默认8000")
    p.add_argument("--interval-seconds", type=float, default=1.0, help="后台循环tick间隔，默认1秒")
    p.add_argument("--exposure-poll-seconds", type=float, default=5.0, help="仓位/权益/杠杆真实API拉取间隔，默认5秒")
    p.add_argument("--funding-poll-seconds", type=float, default=15.0, help="资金费事件轮询间隔，默认15秒")
    p.add_argument("--min-event-window-hours", type=float, default=8.0, help="资金费事件换算的最小窗口小时，默认8小时")
    p.add_argument("--record-file", type=Path, default=DEFAULT_DATA_DIR / "funding_records_stream.csv")
    p.add_argument("--summary-csv", type=Path, default=DEFAULT_DATA_DIR / "funding_summary_stream.csv")
    p.add_argument("--chart-points", type=int, default=120)
    p.add_argument("--reset-records", action="store_true", help="启动时清空本地记录；默认保留并可回溯")
    p.add_argument("--demo-mode", action="store_true")
    p.add_argument("--once", action="store_true", help="仅刷新一次仓位并检测一次资金费事件")
    return p.parse_args()


def run_web(args: argparse.Namespace) -> int:
    service = FundingService(args)
    service.run_tick(force=True)
    service.start_background()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    print(f"[INFO] Web启动: http://{args.host}:{args.port}")
    print(f"[INFO] UI可每秒刷新；真实API频率: exposure={args.exposure_poll_seconds}s, funding={args.funding_poll_seconds}s")
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
        service.run_tick(force=True)
        print(json.dumps(service.snapshot_payload()["metrics"], ensure_ascii=False, indent=2))
        return 0

    print("[INFO] 持续运行中（Ctrl+C停止）")
    print(f"[INFO] UI可每秒刷新；真实API频率: exposure={args.exposure_poll_seconds}s, funding={args.funding_poll_seconds}s")
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
