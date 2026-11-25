"""
walls_app.py - on-demand GEX call/put walls. Type a ticker, get:
  * Weekly  : this week's expiration (Friday, or Thursday on holiday weeks)
  * ~30 DTE : expiration closest to 30 days out
  * ~60 DTE : expiration closest to 60 days out
For each: put wall, call wall, net GEX, put/call OI, a GEX-by-strike chart,
and the nearest puts at/below the put wall (bid/ask/delta/IV/OI/yield).
Header: spot price + RSI(14). Data pulled live from CBOE delayed quotes on every lookup.

Run on Windows:
  pip install pandas requests
  python walls_app.py            -> opens http://127.0.0.1:8050 in your browser; also reachable from your
                                    phone on home Wi-Fi ("lan_default": true in alerts_config.json, the default)
  python walls_app.py --local    -> this PC only (no phone access)
  python walls_app.py --cli      -> text mode: type tickers at a prompt
  python walls_app.py --watch    -> headless downside-alert monitor (no page)
  python walls_app.py --check-once -> run the alert check one time and exit

Downside alerts (watchlist, any ticker; checked every N min in market hours, CBOE ~15 min delayed):
  * spot breaks below the put wall          * spot within X% above the put wall
  * net GEX flips negative                  * net GEX falls >= Y% vs prior close
  * put wall steps down to a lower strike   * spot near / below your short strike
  Notifications: Windows toast, browser notification, optional phone push via ntfy.sh.

Walls (naive GEX, dealers long calls / short puts):
  GEX per contract = gamma * OI * 100 * spot^2 * 0.01   (puts negative)
  CALL WALL = strike at/above spot with the largest call GEX
  PUT WALL  = strike at/below spot with the largest |put GEX|
"""

import argparse
import base64
import hashlib
import html
import io
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
import zlib
import email.utils
from concurrent.futures import ThreadPoolExecutor
from statistics import NormalDist
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import numpy as np
import pandas as pd
import requests

CBOE_CHAIN = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
CBOE_HIST = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{sym}.json"
INDEXES = {"SPX", "XSP", "NDX", "RUT", "VIX", "DJX", "OEX", "XEO", "MRUT"}
SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<type>[CP])(?P<strike>\d{8})$")
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
HEADERS = {"User-Agent": "Mozilla/5.0"}
APP_VERSION = "2026-11-25b Accuracy audit: every pick now shows Expected value per contract (3,478 settled picks won 71% of the time and still lost 10.1% of capital per trade, because the average win was $47 and the average loss $211); picks are ranked by expected return per dollar at risk instead of win rate; only positive-expected-value picks are counted in the record; a banner says plainly when nothing is worth taking; tracked picks show where the risk is concentrated and are capped per ticker and per expiration; tracked history is append-only so it cannot be lost; 2026-11-23b CBOE now refuses scripted requests (HTTP 403): one 403 switches CBOE off for 30 min and everything uses Schwab - background GEX recorder, sector GEX, watchlist monitor, history, IV30 (computed from the chain); SPX fetched from Schwab in 10-day slices (it refuses wider requests); picks scanner no longer blocks the app's scans; nothing starts when Schwab is logged out and CBOE is blocked, and a red bar says so; queued jobs name the job actually running; 2026-11-21a Early moves: a setup whose forward paper log (20+ closed) is below arbitrary entries is paused for swing picks (still logged/graded); 2026-11-18b Picks: one fixed set of tracked picks locked each market day at the first live re-price at or after 9:45 ET, with IDs (T1118-07), exact legs, entry, risk and chance; live cards show whether they are tracked; How the picks have done grades only the locked picks at their locked entry (the old every-refresh count kept in a closed section); 2026-11-18b Picks scanner built into GEX Walls (one app: full scan each weekday morning, live re-prices, universe rebuild Sundays); shown chance of profit = the most accurate one on a walk-forward test (the old one-way shrink made it less accurate and is gone); 2026-11-18a phone (Wi-Fi) access on by default, even when walls_app.py is double-clicked (--local = this PC only); 2026-11-17b sector GEX changes say Up / Down with amount, % and since-when; columns renamed (since last check / since yesterday’s close); 2026-11-17a Early moves / swing / algos use the last session even when CBOE's daily history lags (Schwab final close added after hours); 2026-11-16a long error/status messages wrap on phones (no sideways scroll); 2026-11-15b live + automatic: Schwab now streams prices (open trades and their hold / cut / take calls re-valued within ~10 s of a quote change, no polling); spreads, sell puts and calls re-price themselves every 10 min in market hours (header button shows it, click = refresh now); one shared copy of each option chain for all scans, trades and the picks re-price; rotation scan uses live prices (2 batch quotes) instead of re-downloading 266 histories; sector GEX snapshot no longer holds up the scans behind it; calls-scan odds computed in one pass (same numbers, ~3x faster); 2026-11-15a outside market hours the daily bar, RSI and day change use the official regular-session close (Schwab regular quote), not the after-hours trade; 2026-11-14c My trades: add any strategy by hand (spreads, condor, puts, calls, covered call, shares, custom legs); every open trade gets one call (HOLD ON / CUT LOSSES / TAKE PROFIT NOW!) from live odds, trend and profit captured; 2026-11-14b header buttons wrap on phones (no sideways scroll); 2026-11-14a usage counts only user-opened sections (auto-opened panels no longer inflate usage); 2026-11-13d faster scans: SPX chain straight from CBOE (Schwab stalls 67 s on it), 6 chains at a time, vectorized 50%-target odds (same numbers), 15-min recorder yields to scans; 2026-11-13c faster Refresh all: jobs run in priority order, daily-data jobs skipped when fresh, keep-alive connections; 2026-11-13b no single point of failure on Nasdaq.com: stock list rebuilt from Nasdaq Trader listings + Schwab when Nasdaq is unavailable, all prices Schwab-first, Nasdaq requests paced; 2026-11-13a Nasdaq block guard (pause + readable errors), one shared cached Nasdaq screener, live prices from Schwab first, Schwab status button in the header; 2026-11-12g cut-losses / salvage / hold verdict with repair plans on losing trades; 2026-11-12f one Refresh-all button at the top; 2026-11-12e tax-loss bounce screen in Early moves (this year's losers near 52-week lows, season calendar, put / call / call-spread setups from the real chain); 2026-11-12d dealer card: hedging size vs 20-day dollar volume; 2026-11-12c clover alignment (8+ of 12 bullish conditions); 2026-11-12b leveraged ETFs (tested short-term rules); 2026-11-12a early moves (factor moves, theme leaders), short-squeeze monitor, algos, swing rebuilt, sortable columns; 2026-11-09d no adjusted Schwab contracts (LCID1-style), index roots kept; 2026-11-09b earnings IV-crush card; 2026-11-09a ET dates for expiry/DTE, no empty Friday chip; 2026-11-06b faster (background trade values), roll trades, realistic fills, direction labels, Schwab"
CACHE_SECONDS = 300
MIN_OI = 500  # total contracts in an expiration below this = "thin"
_cache: dict = {}
_lock = threading.Lock()

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "alerts_config.json"
STATE_PATH = APP_DIR / "alerts_state.json"
LOG_PATH = APP_DIR / "alerts_log.jsonl"
LOCK_PATH = APP_DIR / "alerts.lock"
_io_lock = threading.Lock()
TAILNET = ipaddress.ip_network("100.64.0.0/10")          # Tailscale private addresses (phone away from home)
_monitor = {"running": False, "last_run": None, "next_run": None, "note": ""}


def clean_ticker(t: str) -> str:
    t = (t or "").strip().upper().lstrip("$^").replace("/", ".")
    if not TICKER_RE.match(t):
        raise ValueError("Enter a ticker like SPY, MRVL or BRK.B")
    return t


# ---------------------------------------------------------------- data
_rate = {"next": 0.0}
_rate_lock = threading.Lock()
_json_cache: dict = {}
CBOE_MIN_GAP = 0.3     # seconds between CBOE requests (their CDN returns HTTP 429 if hammered)


class _Busy:                                           # stand-in response while CBOE is throttling us
    status_code = 429
    headers: dict = {}

    def json(self):
        return {}


class _Blocked(_Busy):                                  # stand-in response while CBOE refuses this PC (HTTP 403)
    status_code = 403


# Since Nov 2026 CBOE's free delayed-quotes files answer HTTP 403 to scripts (Cloudflare), from any network. One 403
# switches CBOE off for 30 minutes: every CBOE call then answers at once without touching the network, background jobs
# use Schwab instead, and the header says so. It is retried every 30 minutes in case CBOE opens again.
_cboe_off = {"until": 0.0, "since": None, "n": 0}
CBOE_OFF_MIN = 30


def cboe_down() -> bool:
    return time.time() < _cboe_off["until"]


def _bulk_cboe() -> bool:
    """True inside a background bulk job that should use CBOE (only while CBOE works)."""
    return getattr(_tl, "cboe", False) and not cboe_down()


def no_data_source() -> str | None:
    """Why no market data can be had right now (Schwab logged out and CBOE blocked), else None."""
    if _sw_live() or not cboe_down():
        return None
    return "No market data: the Schwab login has expired and CBOE blocks scripted requests - log in to Schwab (header chip)"


def _cboe_get(url: str):
    """Polite GET: global pacing plus backoff/retry on HTTP 429 / 5xx.
    Background bulk jobs (flagged by _bg_cboe) are patient: slower pacing and up to 5 tries with long backoff.
    Everything else (what you click, trade values, scans) never waits more than ~4 s for CBOE: while CBOE is throttling
    (HTTP 429) it gets an immediate "busy" answer, and callers fall back (Schwab history, cached IV30)."""
    if cboe_down():
        return _Blocked()
    patient = _bulk_cboe()
    waited = 0.0
    r = _Busy()
    for attempt in range(5 if patient else 2):
        with _rate_lock:
            if not patient and _rate.get("block", 0) - time.time() > 4:
                return r
            wait = max(_rate["next"], _rate.get("block", 0)) - time.time()
            if not patient and wait > 4 - waited:
                return r
            _rate["next"] = max(time.time(), _rate["next"], _rate.get("block", 0)) + (CBOE_MIN_GAP * 2 if patient else CBOE_MIN_GAP)
        if wait > 0:
            time.sleep(wait)
            waited += wait
        r = requests.get(url, timeout=30, headers=HEADERS)
        if r.status_code == 403:
            if not cboe_down():
                _cboe_off.update(since=_cboe_off["since"] or et_now().strftime("%Y-%m-%d %H:%M ET"))
            _cboe_off["until"] = time.time() + CBOE_OFF_MIN * 60
            _cboe_off["n"] += 1
            return r
        if r.status_code == 200 and _cboe_off["since"]:
            _cboe_off.update(since=None, until=0.0)
        if r.status_code == 429 or r.status_code >= 500:
            back = min(float(r.headers.get("Retry-After") or 0) or 2 * 2 ** attempt, 60)
            with _rate_lock:
                _rate["block"] = max(_rate.get("block", 0), time.time() + back)
            continue
        return r
    return r


def _get_json(url_tmpl: str, ticker: str) -> dict:
    hist = url_tmpl == CBOE_HIST
    key = (url_tmpl, ticker)
    hit = _json_cache.get(key)
    if hist and hit and time.time() - hit[0] < 1800:   # daily bars: reuse for 30 min
        return hit[1]
    syms = [f"_{ticker}", ticker] if ticker in INDEXES else [ticker, f"_{ticker}"]
    err = "not found"
    for s in syms:
        try:
            r = _cboe_get(url_tmpl.format(sym=s))
            if r.status_code == 200:
                j = r.json()
                if hist:
                    _json_cache[key] = (time.time(), j)
                elif isinstance(j, dict):
                    j["_lm"] = r.headers.get("Last-Modified")   # when CBOE last rebuilt this chain file
                return j
            err = f"HTTP {r.status_code}"
            if r.status_code == 429:
                break
        except (requests.RequestException, ValueError) as e:
            err = str(e)
    if hist and _sw_live():                         # CBOE busy / missing: Schwab daily bars (same shape)
        sh = schwab_history(ticker)
        if sh:
            _json_cache[key] = (time.time(), sh)
            return sh
    raise RuntimeError(f"CBOE has no data for {ticker} ({err})")


def schwab_history(ticker: str) -> dict | None:
    """Daily OHLCV for ~2 years from Schwab, in CBOE's history shape. None if not connected or on any problem."""
    try:
        j = _sw_get("/marketdata/v1/pricehistory", {"symbol": f"${ticker}" if ticker in INDEXES else ticker,
                                                     "periodType": "year", "period": 2, "frequencyType": "daily",
                                                     "frequency": 1})
        rows = [{"date": datetime.fromtimestamp(c["datetime"] / 1000, et_now().tzinfo).date().isoformat(),
                 "open": c.get("open"), "high": c.get("high"), "low": c.get("low"), "close": c.get("close"),
                 "volume": c.get("volume")} for c in (j or {}).get("candles") or [] if c.get("datetime")]
        return {"data": rows, "_src": "schwab"} if len(rows) > 20 else None
    except Exception:
        return None


def daily_history(ticker: str, last_bar: dict | None = None) -> pd.DataFrame:
    """CBOE daily OHLCV; appends the option chain's session bar if CBOE history lags a day."""
    h = pd.DataFrame(_get_json(CBOE_HIST, ticker)["data"])
    for col in ("open", "high", "low", "close", "volume"):
        h[col] = pd.to_numeric(h.get(col), errors="coerce")
    h["date"] = h["date"].astype(str)
    h = h.dropna(subset=["close"]).drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    if last_bar and last_bar.get("date") and (h.empty or str(h["date"].iloc[-1]) < last_bar["date"]):
        h = pd.concat([h, pd.DataFrame([last_bar])], ignore_index=True)
    return h


def rsi14(ticker: str, last_close: float, last_date: str, hist: pd.DataFrame | None = None) -> tuple[float | None, str]:
    try:
        h = hist if hist is not None else daily_history(ticker, {"date": last_date, "close": last_close})
    except Exception:
        return None, "n/a"
    if h.empty:
        return None, "n/a"
    c = pd.to_numeric(h["close"], errors="coerce").dropna()
    if len(c) < 15:
        return None, "n/a"
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    return (100.0 if l == 0 else float(100 - 100 / (1 + g / l))), str(h["date"].iloc[-1])


# ---------------------------------------------------------------- Schwab Trader API: real-time option chains (optional)
# Joe registers an app at developer.schwab.com, types its App Key / Secret / Callback URL into this page (they stay in
# schwab_keys.json on this PC and are never sent back to the page), logs in to Schwab himself, and pastes the address
# he lands on. Tokens: access 30 min (refreshed automatically), refresh 7 days (then log in again).
# When connected, load_chain() uses Schwab's chain first and falls back to CBOE's free delayed chain.
SCHWAB_KEYS = APP_DIR / "schwab_keys.json"
SCHWAB_TOKEN = APP_DIR / "schwab_token.json"
SCHWAB_API = "https://api.schwabapi.com"
SCHWAB_MIN_GAP = 0.55            # seconds between Schwab market-data requests (stays under ~110 / minute)
_sw_rate = {"next": 0.0}
_sw_rate_lock = threading.Lock()
_sw_tok_lock = threading.Lock()
_sw_state = {"error": None, "last_ok": None, "ok": 0, "fail": 0, "delayed": None}


def _sw_keys() -> dict | None:
    k = _read_json(SCHWAB_KEYS, {}) or {}
    return k if k.get("app_key") and k.get("app_secret") else None


def _sw_callback(k: dict) -> str:
    return (k.get("callback") or "https://127.0.0.1").strip()


def schwab_save_keys(body: dict) -> dict:
    old = _read_json(SCHWAB_KEYS, {}) or {}
    k = {"app_key": str(body.get("app_key") or old.get("app_key") or "").strip(),
         "app_secret": str(body.get("app_secret") or old.get("app_secret") or "").strip(),
         "callback": str(body.get("callback") or old.get("callback") or "https://127.0.0.1").strip()}
    if not k["callback"].startswith("https://"):
        raise ValueError("the Callback URL must start with https:// and match the one in your Schwab app exactly")
    _write_json(SCHWAB_KEYS, k)
    return schwab_status()


def schwab_auth_url() -> str:
    k = _sw_keys()
    if not k:
        raise ValueError("save your Schwab App Key and Secret first")
    return f"{SCHWAB_API}/v1/oauth/authorize?" + urlencode({"client_id": k["app_key"], "redirect_uri": _sw_callback(k)})


def _sw_token_req(data: dict, keep_refresh_exp: float | None = None) -> dict:
    k = _sw_keys()
    if not k:
        raise ValueError("no Schwab keys saved")
    basic = base64.b64encode(f'{k["app_key"]}:{k["app_secret"]}'.encode()).decode()
    r = requests.post(f"{SCHWAB_API}/v1/oauth/token", data=data, timeout=25,
                      headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code != 200:
        try:
            why = (r.json() or {}).get("error_description") or (r.json() or {}).get("error") or ""
        except ValueError:
            why = ""
        raise RuntimeError(f"Schwab login step failed (HTTP {r.status_code}{': ' + str(why)[:120] if why else ''})")
    t = r.json()
    old = _read_json(SCHWAB_TOKEN, {}) or {}
    now = time.time()
    tok = {"access_token": t["access_token"], "refresh_token": t.get("refresh_token") or old.get("refresh_token"),
           "access_exp": now + int(t.get("expires_in") or 1800) - 90,
           "refresh_exp": keep_refresh_exp or (now + 7 * 86400 - 1800), "connected_at": old.get("connected_at") or now}
    if keep_refresh_exp is None:
        tok["connected_at"] = now
    _write_json(SCHWAB_TOKEN, tok)
    return tok


def schwab_connect(returned_url: str) -> dict:
    """Exchange the ?code= from the address Schwab sent Joe to for tokens."""
    q = parse_qs(urlparse(str(returned_url or "").strip()).query)
    code = (q.get("code") or [""])[0]
    if not code:
        raise ValueError("that address has no code=… in it — paste the full address from the browser bar after you log in")
    k = _sw_keys()
    if not k:
        raise ValueError("save your Schwab App Key and Secret first")
    _sw_token_req({"grant_type": "authorization_code", "code": code, "redirect_uri": _sw_callback(k)})
    _sw_state.update(error=None)
    return schwab_status()


def _sw_access(force: bool = False) -> str | None:
    tok = _read_json(SCHWAB_TOKEN, {}) or {}
    if not tok.get("refresh_token") or time.time() > tok.get("refresh_exp", 0):
        return None
    if not force and time.time() < tok.get("access_exp", 0):
        return tok.get("access_token")
    with _sw_tok_lock:
        tok = _read_json(SCHWAB_TOKEN, {}) or {}
        if not force and time.time() < tok.get("access_exp", 0):
            return tok.get("access_token")
        try:
            return _sw_token_req({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]},
                                 keep_refresh_exp=tok.get("refresh_exp"))["access_token"]
        except Exception as e:
            _sw_state.update(error=str(e)[:200])
            return None


def schwab_status() -> dict:
    k = _sw_keys() or {}
    tok = _read_json(SCHWAB_TOKEN, {}) or {}
    left = (tok.get("refresh_exp", 0) - time.time()) / 86400 if tok.get("refresh_token") else None
    return {"keys": bool(k), "key_tail": (k.get("app_key") or "")[-4:], "callback": k.get("callback") or "https://127.0.0.1",
            "connected": bool(left and left > 0), "days_left": round(left, 1) if left is not None else None,
            "error": _sw_state["error"], "last_ok": _sw_state["last_ok"], "ok": _sw_state["ok"], "fail": _sw_state["fail"],
            "delayed": _sw_state["delayed"]}


def _sw_get(path: str, params: dict) -> dict | None:
    tk = _sw_access()
    if not tk:
        return None
    for attempt in range(3):
        with _sw_rate_lock:
            wait = _sw_rate["next"] - time.time()
            _sw_rate["next"] = max(time.time(), _sw_rate["next"]) + SCHWAB_MIN_GAP
        if wait > 0:
            time.sleep(wait)
        r = requests.get(SCHWAB_API + path, params=params, timeout=40,
                         headers={"Authorization": f"Bearer {tk}", "Accept": "application/json"})
        if r.status_code == 401 and attempt == 0:
            tk = _sw_access(force=True)
            if not tk:
                return None
            continue
        if r.status_code == 429:
            back = float(r.headers.get("Retry-After") or 0) or 5 * (attempt + 1)
            with _sw_rate_lock:
                _sw_rate["next"] = max(_sw_rate["next"], time.time() + back)
            continue
        if r.status_code != 200:
            _sw_state.update(error=f"Schwab {path.rsplit('/', 1)[-1]} HTTP {r.status_code}", fail=_sw_state["fail"] + 1)
            return None
        return r.json()
    _sw_state.update(error="Schwab rate limit (HTTP 429)", fail=_sw_state["fail"] + 1)
    return None


def _swn(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if x != x or x <= -999 or x in (float("inf"), float("-inf")) else x


_iv30_cache: dict = {}


def _cboe_iv30(ticker: str) -> float | None:
    """CBOE's 30-day implied volatility (small quote file, cached 15 min) — kept so IV rank history stays consistent."""
    hit = _iv30_cache.get(ticker)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    if hit and not _bulk_cboe() and (_rate.get("block", 0) > time.time() or cboe_down()):
        return hit[1]                                  # CBOE throttling: keep the last value rather than wait
    try:
        r = _cboe_get(f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{'_' + ticker if ticker in INDEXES else ticker}.json")
        v = ((r.json() or {}).get("data") or {}).get("iv30") if r.status_code == 200 else None
    except Exception:
        v = None
    if v is None and hit and hit[1] is not None:
        _iv30_cache[ticker] = (time.time() - 600, hit[1])    # keep the old value, retry in ~5 min
        return hit[1]
    _iv30_cache[ticker] = (time.time(), v)
    return v


def _iv30_from_opts(opts: list, spot: float, today_) -> float | None:
    """30-day ATM implied volatility (%) from a chain: each expiration's IV at the strike nearest the price (call and
    put averaged), variance-interpolated to 30 days. Used when CBOE's own IV30 figure is unavailable."""
    try:
        by = {}
        for o in opts:
            c = o["option"]
            iv = float(o.get("iv") or 0)
            if not (0.01 < iv < 5):
                continue
            ex = date(2000 + int(c[-15:-13]), int(c[-13:-11]), int(c[-11:-9]))
            dte = (ex - today_).days
            if dte < 2:
                continue
            k = int(c[-8:]) / 1000
            by.setdefault(dte, []).append((abs(k - spot), iv))
        pts = []
        for dte, L in sorted(by.items()):
            L.sort()
            near = [iv for d, iv in L if d <= L[0][0] + 1e-9][:2]
            pts.append((dte, sum(near) / len(near)))
        if not pts:
            return None
        lo = [p for p in pts if p[0] <= 30]
        hi = [p for p in pts if p[0] >= 30]
        if lo and hi:
            (d1, v1), (d2, v2) = lo[-1], hi[0]
            if d1 == d2:
                return round(v1 * 100, 2)
            w_ = (30 - d1) / (d2 - d1)
            var = (v1 * v1 * d1 * (1 - w_) + v2 * v2 * d2 * w_) / 30
            return round(math.sqrt(max(var, 0)) * 100, 2)
        return round((lo or hi)[-1 if lo else 0][1] * 100, 2)
    except Exception:
        return None


def _iv_from_mid(F: np.ndarray, K: np.ndarray, T: np.ndarray, px: np.ndarray, call: np.ndarray, r: float = 0.04) -> np.ndarray:
    """Implied volatility from each option's mid (Black-76 on the forward implied by put-call parity, so dividends are
    handled — calls and puts at a strike get the same IV, like CBOE's file). Schwab's own volatility field jumps around
    on thin long-dated contracts (TLT 2028: 6% / 8% / 17% on neighbouring strikes), so the app derives IV itself.
    NaN where the price is outside no-arbitrage bounds."""
    lo, hi = np.full(K.shape, 1e-3), np.full(K.shape, 5.0)
    disc = np.exp(-r * T)
    intr = disc * np.where(call, np.maximum(F - K, 0), np.maximum(K - F, 0))
    ok = (px > intr + 1e-4) & (T > 0) & (px > 0)
    sq = np.sqrt(np.maximum(T, 1e-9))
    for _ in range(30):
        mid = (lo + hi) / 2
        d1 = (np.log(F / K) + 0.5 * mid * mid * T) / (mid * sq)
        d2 = d1 - mid * sq
        val = disc * np.where(call, F * _ncdf(d1) - K * _ncdf(d2), K * _ncdf(-d2) - F * _ncdf(-d1))
        hi = np.where(val > px, mid, hi)
        lo = np.where(val > px, lo, mid)
    return np.where(ok, (lo + hi) / 2, np.nan)


_sw_win: dict = {}


def schwab_chain(ticker: str) -> dict | None:
    """Schwab option chain converted to the CBOE file's shape, so the rest of the app is unchanged.
    Very large chains (SPY, SPX, QQQ) come back as HTTP 502 for all expirations, so those retry with a date window."""
    sym = f"${ticker}" if ticker in INDEXES else ticker
    base = {"symbol": sym, "contractType": "ALL", "includeUnderlyingQuote": "true", "strategy": "SINGLE"}
    today_ = et_now().date()
    wins = [{"range": "ALL"}, {"fromDate": str(today_), "toDate": str(today_ + timedelta(days=400))},
            {"fromDate": str(today_), "toDate": str(today_ + timedelta(days=120))}]
    j, used = None, None
    for i in range(_sw_win.get(ticker, 0), len(wins)):    # remember which window works for this ticker
        j = _sw_get("/marketdata/v1/chains", {**base, **wins[i]})
        if j and str(j.get("status") or "SUCCESS").upper() == "SUCCESS":
            _sw_win[ticker], used = i, i
            break
        j = None
    if not j and (cboe_down() or ticker in INDEXES):
        # SPX (30,000+ contracts) is refused even 30 days at a time (HTTP 502), but 10 days come back in ~4 s:
        # fetch the next ~3 months in slices and join them (CBOE used to supply SPX; it no longer can)
        cuts = [0, 10, 21, 35, 56, 91]
        for a, b in zip(cuts, cuts[1:]):
            jj = _sw_get("/marketdata/v1/chains", {**base, "fromDate": str(today_ + timedelta(days=a)),
                                                   "toDate": str(today_ + timedelta(days=b - 1))})
            if not jj or str(jj.get("status") or "SUCCESS").upper() != "SUCCESS":
                if j is None:
                    break                                  # first slice refused: give up
                continue
            if j is None:
                j = jj
            else:
                for side in ("callExpDateMap", "putExpDateMap"):
                    j.setdefault(side, {}).update(jj.get(side) or {})
        if j:
            _sw_win[ticker], used = len(wins), "slices"
    if not j:
        return None
    u = j.get("underlying") or {}
    spot = _swn(j.get("underlyingPrice")) or _swn(u.get("last")) or _swn(u.get("mark"))
    opts = []
    for side, cp in (("callExpDateMap", "C"), ("putExpDateMap", "P")):
        for ek, strikes in (j.get(side) or {}).items():
            ex = str(ek).split(":")[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ex):
                continue
            for ks, lst in (strikes or {}).items():
                for c in lst or []:
                    k = _swn(c.get("strikePrice")) or _swn(ks)
                    if k <= 0:
                        continue
                    # adjusted contracts (after reverse splits / mergers: LCID1 etc., deliverable not 100 shares) carry the
                    # same strike/expiry but near-zero quotes; Schwab lists them in the same map. They are not standard
                    # options and wrecked prices, IV and GEX (e.g. LCID Jan-27 5.5C showed 0.00/0.01 next to the real one).
                    root = re.sub(r"[^A-Z0-9]", "", str(c.get("symbol") or "").split(" ")[0].upper())
                    base = re.sub(r"[^A-Z0-9]", "", ticker.upper())
                    if c.get("nonStandard") or _swn(c.get("multiplier") or 100) != 100 or re.fullmatch(base + r"\d+", root):
                        continue
                    # indexes keep their own root (SPX AM-settled vs SPXW PM-settled share strikes/dates); stocks use the ticker
                    nm_root = root if (ticker in INDEXES and root.startswith(base)) else base
                    opts.append({"option": f"{nm_root}{ex[2:4]}{ex[5:7]}{ex[8:10]}{cp}{int(round(k * 1000)):08d}",
                                 "bid": _swn(c.get("bid")), "ask": _swn(c.get("ask")), "iv": _swn(c.get("volatility")) / 100,
                                 "open_interest": _swn(c.get("openInterest")), "volume": _swn(c.get("totalVolume")),
                                 "delta": _swn(c.get("delta")), "gamma": _swn(c.get("gamma")), "theta": _swn(c.get("theta")),
                                 "vega": _swn(c.get("vega")), "last_trade_price": _swn(c.get("last"))})
    if not spot or not opts:
        return None
    if used == 2:          # huge chain (SPY, SPX…): Schwab real time to 120 days, CBOE (15-min delayed) beyond that
        try:
            cut = (today_ + timedelta(days=120)).strftime("%y%m%d")
            have = {o["option"] for o in opts}
            for o in (_get_json(CBOE_CHAIN, ticker).get("data") or {}).get("options") or []:
                code = o.get("option", "")
                cr = code[:-15].lstrip("_")
                if len(code) >= 15 and code[-15:-9] > cut and (cr == ticker.replace(".", "") or
                                                               (ticker in INDEXES and cr.startswith(ticker))):
                    nm = code.lstrip("_") if ticker in INDEXES else f"{ticker.replace('.', '')}{code[-15:]}"
                    if nm not in have:
                        opts.append({**{k: _swn(o.get(k)) for k in ("bid", "ask", "iv", "open_interest", "volume", "delta",
                                                                     "gamma", "theta", "vega", "last_trade_price")}, "option": nm})
        except Exception:
            pass
    try:                                                   # derive IV from each mid (see _iv_from_mid)
        ex_ = np.array([(date(2000 + int(o["option"][-15:-13]), int(o["option"][-13:-11]), int(o["option"][-11:-9])) - today_).days
                        for o in opts], dtype=float)
        K_ = np.array([int(o["option"][-8:]) / 1000 for o in opts])
        cl_ = np.array([o["option"][-9] == "C" for o in opts])
        px_ = np.array([(o["bid"] + o["ask"]) / 2 if o["bid"] > 0 and o["ask"] >= o["bid"] else 0.0 for o in opts])
        T_ = np.maximum(ex_, 0.5) / 365
        F_ = float(spot) * np.exp(0.04 * T_)                # default forward; replaced by put-call parity per expiration
        for e in np.unique(ex_):
            m = ex_ == e
            cm = {k: p for k, p, c in zip(K_[m], px_[m], cl_[m]) if c and p > 0}
            pm = {k: p for k, p, c in zip(K_[m], px_[m], cl_[m]) if not c and p > 0}
            both = [k for k in cm if k in pm]
            if both:
                k0 = min(both, key=lambda k: abs(cm[k] - pm[k]))
                f = k0 + (cm[k0] - pm[k0]) * math.exp(0.04 * T_[m][0])
                if 0.5 * spot < f < 1.5 * spot:
                    F_[m] = f
        ivs = _iv_from_mid(F_, K_, T_, px_, cl_)
        for o, v in zip(opts, ivs):
            if v == v:
                o["iv"] = float(v)
    except Exception:
        pass
    qt = u.get("quoteTime") or u.get("tradeTime")
    try:
        lt = datetime.fromtimestamp(float(qt) / 1000, et_now().tzinfo).strftime("%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError, OSError):
        lt = et_now().strftime("%Y-%m-%dT%H:%M:%S")
    _sw_state.update(last_ok=et_now().strftime("%H:%M:%S ET"), ok=_sw_state["ok"] + 1, error=None,
                     delayed=bool(j.get("isDelayed")) if j.get("isDelayed") is not None else None)
    d = {"current_price": spot, "options": opts, "last_trade_time": lt, "price_change_percent": u.get("percentChange"),
         "open": u.get("openPrice"), "high": u.get("highPrice"), "low": u.get("lowPrice"), "volume": u.get("totalVolume"),
         "close": spot, "prev_day_close": u.get("close"), "iv30": _cboe_iv30(ticker) or _iv30_from_opts(opts, spot, today_)}
    return {"timestamp": et_now().strftime("%Y-%m-%d %H:%M:%S"), "data": d, "_src": "schwab",
            "_lm": email.utils.format_datetime(datetime.now(timezone.utc), usegmt=True)}


_tl = threading.local()


def _bg_cboe(fn):
    """Run fn as a background bulk job: its option chains come from CBOE, leaving Schwab free for what you click."""
    def w(*a, **k):
        prev = getattr(_tl, "cboe", False)
        _tl.cboe = True
        try:
            return fn(*a, **k)
        finally:
            _tl.cboe = prev
    return w


def _sw_live() -> bool:
    tok = _read_json(SCHWAB_TOKEN, {}) or {}
    return bool(_sw_keys() and tok.get("refresh_token") and time.time() < tok.get("refresh_exp", 0))


# Schwab cannot serve some chains: SPX (30,000+ contracts) waits ~67 s and then answers HTTP 502, which froze the
# scan on its last ticker. Those go straight to CBOE; any other ticker that behaves the same is learned for 6 hours.
_sw_skip: dict = {"SPX": float("inf")}


def _sw_ok(ticker: str) -> bool:
    """Ask Schwab for this chain? The skip list only applies while CBOE can stand in."""
    return cboe_down() or time.time() > _sw_skip.get(ticker, 0)


def _chain_json(ticker: str) -> dict:
    """Schwab real-time chain when connected (not for background bulk jobs), otherwise CBOE's free delayed chain."""
    if not _bulk_cboe() and _sw_live() and _sw_ok(ticker):
        t0 = time.time()
        try:
            p = schwab_chain(ticker)
            if p:
                return p
        except Exception as e:
            _sw_state.update(error=f"Schwab: {str(e)[:150]}", fail=_sw_state["fail"] + 1)
        if time.time() - t0 > 8 and not cboe_down():      # Schwab took long and still had no chain: use CBOE for 6 h
            _sw_skip[ticker] = time.time() + 6 * 3600
    if cboe_down():
        raise RuntimeError(f"no option chain for {ticker}: " + ("Schwab returned none" if _sw_live() else
                                                                "Schwab login expired and CBOE blocks scripted requests"))
    p = _get_json(CBOE_CHAIN, ticker)
    if isinstance(p, dict):
        p.setdefault("_src", "cboe")
    return p


CHAIN_MAX_AGE_MIN = 45   # CBOE quotes are 15-min delayed; an active chain file is rebuilt every few minutes


def _last_session_close(now: datetime) -> datetime:
    """4:00 PM ET of the most recent weekday session that has closed (holidays not modelled)."""
    d = now.date()
    if now.weekday() >= 5 or (now.hour, now.minute) < (16, 0):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime(d.year, d.month, d.day, 16, 0)


def chain_freshness(lm_header: str | None, last_trade: str | None) -> dict:
    """How old CBOE's option-chain file is. Some symbols' files are only rebuilt a few times a day (or not until the
    next day), so their option quotes AND underlying price can be hours old even though CBOE calls them 15-min delayed.
    stale = market open and file older than CHAIN_MAX_AGE_MIN (or from before today's open);
            market closed and file from before the last session's close."""
    now = et_now().replace(tzinfo=None)
    ct = None
    try:
        if lm_header:
            ct = email.utils.parsedate_to_datetime(lm_header).astimezone(et_now().tzinfo).replace(tzinfo=None)
    except Exception:
        ct = None
    lt = None
    try:
        lt = datetime.fromisoformat(str(last_trade)[:19]) if last_trade else None
    except ValueError:
        lt = None
    if lt and (ct is None or lt < ct):          # a file rebuilt late can still hold old quotes: use the older time
        ct = lt
    if ct is None:
        return {"chain_time": None, "chain_age_min": None, "stale": False}
    age = (now - ct).total_seconds() / 60
    if market_open_now():
        opened = now.replace(hour=9, minute=30, second=0, microsecond=0)
        stale = age > CHAIN_MAX_AGE_MIN or ct < opened
    else:
        stale = ct < _last_session_close(now) - timedelta(minutes=20)
    return {"chain_time": ct.strftime("%Y-%m-%d %H:%M ET"), "chain_age_min": round(age), "stale": bool(stale)}


_last_src: dict = {}


# One shared copy of each option chain. Every scan, the trade valuations, the 15-minute recorder and the page all used
# to download the same tickers separately (and the picks re-price a third time); with Schwab allowing ~110 requests a
# minute that was most of a refresh. Now a chain downloaded for one of them is reused by the others while it is fresh:
# 75 s while the market is open, 10 min before/after hours, 4 h when closed (quotes do not move then). Two callers
# wanting the same ticker at once share one download.
_chain_cache: dict = {}          # ticker -> (fetched at, (spot, df, meta))
_chain_locks: dict = {}
_chain_guard = threading.Lock()
_chain_stat = {"hit": 0, "miss": 0}
_raw_chain: dict = {}            # ticker -> (fetched at, chain json): chains the Strategy Scanner pulled via /api/chainraw


def chain_ttl() -> float:
    st = _session_status()
    return 75.0 if st == "Market Open" else 600.0 if st in ("Pre-Market", "After-Hours") else 4 * 3600.0


def _src_ok(ticker: str, src: str | None) -> bool:
    """A delayed CBOE copy is fine for background bulk jobs, and for tickers Schwab cannot serve; anything you look at
    or trade from uses Schwab's real-time chain whenever Schwab is connected."""
    return _bulk_cboe() or src == "schwab" or not _sw_live() or not _sw_ok(ticker)


def _raw_get(ticker: str, schwab_only: bool = False):
    hit = _raw_chain.get(ticker)
    if not hit or time.time() - hit[0] >= chain_ttl():
        return None
    src = hit[1].get("_src", "cboe")
    return hit[1] if (src == "schwab" if schwab_only else _src_ok(ticker, src)) else None


def _raw_put(ticker: str, p: dict) -> dict:
    _raw_chain.pop(ticker, None)
    _raw_chain[ticker] = (time.time(), p)
    while len(_raw_chain) > 40:
        _raw_chain.pop(next(iter(_raw_chain)), None)
    return p


def _chain_hit(ticker: str, max_age: float):
    hit = _chain_cache.get(ticker)
    if not hit or time.time() - hit[0] >= max_age or not _src_ok(ticker, hit[1][2].get("source")):
        return None
    return hit[1]


def _chain_shared(ticker: str, max_age: float | None = None):
    """(spot, df, meta) from the shared cache, downloading it if there is no fresh copy. The df is shared: do not
    change it (load_chain hands out a private copy)."""
    max_age = chain_ttl() if max_age is None else max_age
    if getattr(_tl, "cboe", False):
        max_age = max(max_age, 900.0)                       # background bulk jobs: a 15-min-old real-time copy still
    v = _chain_hit(ticker, max_age)                         # beats CBOE's 15-min-delayed file
    if v is not None:
        _chain_stat["hit"] += 1
        return v
    with _chain_guard:
        lk = _chain_locks.setdefault(ticker, threading.Lock())
    with lk:
        v = _chain_hit(ticker, max_age)
        if v is not None:
            _chain_stat["hit"] += 1
            return v
        v = _load_chain_fresh(ticker)
        _chain_stat["miss"] += 1
        _chain_cache[ticker] = (time.time(), v)
        if len(_chain_cache) > 260:
            for k, _v in sorted(_chain_cache.items(), key=lambda kv: kv[1][0])[:60]:
                _chain_cache.pop(k, None)
        return v


def load_chain(ticker: str, max_age: float | None = None) -> tuple[float, pd.DataFrame, dict]:
    """Option chain as (spot, DataFrame, meta). max_age (seconds) = the oldest shared copy you accept; default: fresh
    for the current session (see chain_ttl). Returns a private copy of the table."""
    spot, df, meta = _chain_shared(ticker, max_age)
    return spot, df.copy(), dict(meta)


def _load_chain_fresh(ticker: str) -> tuple[float, pd.DataFrame, dict]:
    p = _raw_get(ticker) or _chain_json(ticker)
    _last_src[ticker] = p.get("_src", "cboe") if isinstance(p, dict) else "cboe"
    d = p["data"]
    spot = float(d["current_price"])
    df = pd.DataFrame(d["options"])
    parts = df["option"].str.extract(SYMBOL_RE)
    df["type"] = parts["type"]
    df["strike"] = parts["strike"].astype(float) / 1000.0
    df["expiration"] = pd.to_datetime(parts["exp"], format="%y%m%d")
    df = df.dropna(subset=["type", "strike"])
    for col in ("gamma", "open_interest", "bid", "ask", "delta", "iv"):
        if col not in df:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    today = pd.Timestamp(et_today())
    df = df[df["expiration"] >= today].copy()
    df["dte"] = (df["expiration"] - today).dt.days
    # Gamma recomputed with Black-Scholes at the displayed spot (Cboe's greeks can use a stale underlying, which can
    # flip the sign of net GEX — see the matrix-gex open-source GEX survey). Time to the 4 PM ET close, at least 30 min.
    df["gamma_cboe"] = df["gamma"]
    try:
        now_et = et_now().replace(tzinfo=None)
        secs = ((df["expiration"] + pd.Timedelta(hours=16)) - now_et).dt.total_seconds().clip(lower=1800)
        t = secs / (365 * 86400)
        iv = df["iv"].where(df["iv"] > 0)
        sd = iv * np.sqrt(t)
        d1 = (np.log(spot / df["strike"]) + (0.04 + iv ** 2 / 2) * t) / sd
        g = np.exp(-d1 ** 2 / 2) / np.sqrt(2 * np.pi) / (spot * sd)
        df["gamma"] = g.where(g.notna() & np.isfinite(g), df["gamma_cboe"])
        gamma_src = "bs_spot"
    except Exception:
        gamma_src = "cboe"
    df["gex"] = df["gamma"] * df["open_interest"] * 100 * spot ** 2 * 0.01
    df.loc[df["type"] == "P", "gex"] *= -1
    meta = {"stamp": p.get("timestamp", ""), "last_trade": d.get("last_trade_time", ""), "gamma_source": gamma_src,
            **chain_freshness(p.get("_lm"), d.get("last_trade_time")), "source": p.get("_src", "cboe"),
            "close": spot,
            "chg_pct": d.get("price_change_percent"),
            "bar": {"date": str(d.get("last_trade_time", ""))[:10] or None,
                    "open": d.get("open"), "high": d.get("high"), "low": d.get("low"),
                    "close": spot, "volume": d.get("volume")}}
    return spot, df, meta


def pick_expirations(df: pd.DataFrame) -> list[tuple[str, pd.Timestamp]]:
    exps = pd.Series(sorted(df["expiration"].unique()))
    if exps.empty:
        return []
    dte = (exps - pd.Timestamp(et_today())).dt.days
    # Weekly = last expiration in the first calendar week that still has one after today
    # (on a Friday that is next Friday, not the contracts expiring today)
    fut = exps[dte >= 1] if (dte >= 1).any() else exps
    week = fut.dt.to_period("W-SAT")
    weekly = fut[week == week.iloc[0]].max()
    picks = [("Weekly", weekly)]
    oi = df.groupby("expiration")["open_interest"].sum()
    for target in (30, 60):
        # closest expiration to target that has real open interest (>= MIN_OI):
        # look within +/-7 days, then +/-21; if all thin, take the most-OI one within 21
        e = None
        # prefer the dominant (usually monthly) expiration within +/-10 days of target
        near10 = exps[(dte - target).abs() <= 10]
        if not near10.empty and oi.reindex(near10).max() >= MIN_OI:
            e = oi.reindex(near10).idxmax()
        for win in (7, 21) if e is None else ():
            near = exps[(dte - target).abs() <= win]
            liquid = near[oi.reindex(near).values >= MIN_OI]
            if not liquid.empty:
                e = liquid.iloc[(liquid.map(lambda x: (x - pd.Timestamp(et_today())).days) - target).abs().argmin()]
                break
        if e is None:
            near = exps[(dte - target).abs() <= 21]
            e = oi.reindex(near).idxmax() if not near.empty else exps.iloc[(dte - target).abs().argmin()]
        picks.append((f"~{target} DTE", e))
    # every standard monthly (3rd Friday, or the Thursday before a Friday holiday) out to ~100 days, so no month is skipped
    mon = exps[(dte >= 1) & (dte <= 100) & exps.dt.day.between(15, 21) & exps.dt.weekday.isin([3, 4])]
    for _, g in mon.groupby(mon.dt.to_period("M")):
        picks.append((f"{g.iloc[0]:%b} monthly", oi.reindex(g).idxmax() if oi.reindex(g).notna().any() else g.iloc[0]))
    # merge duplicates (keep both names), then fill any remaining hole > 35 days with the busiest expiration inside it
    by_e: dict = {}
    for lab, e in picks:
        by_e.setdefault(pd.Timestamp(e), []).append(lab)
    horizon = max(by_e)
    for _ in range(6):
        ks = sorted(by_e)
        holes = [(a, b) for a, b in zip(ks, ks[1:]) if (b - a).days > 35]
        if not holes:
            break
        a, b = holes[0]
        mid_ = exps[(exps > a + pd.Timedelta(days=7)) & (exps < b - pd.Timedelta(days=7))]
        if mid_.empty:
            break
        e = oi.reindex(mid_).idxmax() if oi.reindex(mid_).notna().any() else mid_.iloc[len(mid_) // 2]
        by_e.setdefault(pd.Timestamp(e), []).append(f"{pd.Timestamp(e):%b %d}")
    picks = []
    for e in sorted(k for k in by_e if k <= horizon):
        labs = by_e[e]
        lab = "Weekly" if "Weekly" in labs else " · ".join(dict.fromkeys(sorted(labs, key=lambda x: (not x.startswith("~"), x))))
        picks.append((lab, e))
    out, seen = [], {}
    for label, e in picks:  # merge duplicates (e.g. monthly-only chains)
        if e in seen:
            out[seen[e]] = (out[seen[e]][0] + " / " + label, e)
        else:
            seen[e] = len(out)
            out.append((label, e))
    return out


def analyze_exp(spot: float, df: pd.DataFrame, band: float) -> dict:
    lo, hi = spot * (1 - band), spot * (1 + band)
    g = df[(df["strike"] >= lo) & (df["strike"] <= hi)]
    c = g[g["type"] == "C"].groupby("strike")["gex"].sum()
    p = g[g["type"] == "P"].groupby("strike")["gex"].sum()
    ca, pb = c[c.index >= spot], p[p.index <= spot]
    call_wall = float(ca.idxmax()) if not ca.empty and ca.max() > 0 else None
    put_wall = float(pb.idxmin()) if not pb.empty and pb.min() < 0 else None
    strikes = sorted(set(c.index) | set(p.index))
    poi = float(df[df["type"] == "P"]["open_interest"].sum())
    coi = float(df[df["type"] == "C"]["open_interest"].sum())

    puts = []
    if put_wall is not None:
        q = df[(df["type"] == "P") & (df["strike"] <= put_wall) & (df["bid"] > 0)]
        if (q["open_interest"] > 0).any():
            q = q[q["open_interest"] > 0]
        q = q.sort_values("strike", ascending=False).head(4)
        floor_any = return_floors()[1] * 100
        for _, r in q.iterrows():
            mid = (r.bid + r.ask) / 2
            dte = max(int(r.dte), 1)
            if mid / r.strike * 100 < floor_any:             # no 0.7% trades ($950 at risk for $7)
                continue
            puts.append({"strike": r.strike, "bid": r.bid, "ask": r.ask, "mid": mid,
                         "delta": r.delta, "iv": r.iv * 100, "oi": int(r.open_interest),
                         "otm": (r.strike / spot - 1) * 100, "spread": (r.ask - r.bid) / mid * 100 if mid else None,
                         "roc": mid / r.strike * 100, "ann": mid / r.strike * 100 * 365 / dte,
                         "be": r.strike - mid, "tp30": mid * 0.70, "tp50": mid * 0.50})
    return {
        "put_wall": put_wall, "call_wall": call_wall,
        "put_wall_gex": float(pb.min()) if put_wall is not None else None,
        "call_wall_gex": float(ca.max()) if call_wall is not None else None,
        "net_gex": float(df["gex"].sum()),
        "put_oi": int(poi), "call_oi": int(coi), "pc_oi": (poi / coi) if coi else None,
        "strikes": [{"k": k, "c": float(c.get(k, 0.0)), "p": float(p.get(k, 0.0))} for k in strikes],
        "puts": puts,
    }


NASDAQ_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
_nq_class: dict = {}
_nq_cache: dict = {}
_nq_sess = requests.Session()                      # keep-alive: live price batches reuse connections

# Nasdaq.com (Akamai) answers "Access Denied" (HTTP 403) when one IP asks too often. While that lasts every request
# just extends the block, so all api.nasdaq.com calls pause (15 min, then 30, up to 60) and return a stand-in 403
# whose error message says what is going on. Prices fall back to Schwab; screener-based scans use the last saved copy.
_NQ_BLOCK = {"until": 0.0, "since": None, "hits": 0}
NQ_MIN_GAP = 0.4                                   # seconds between api.nasdaq.com requests (was: bursts of up to 200)
_nq_pace = {"next": 0.0}
_nq_pace_lock = threading.Lock()


class _NqBlocked:
    status_code, ok, text, content, headers = 403, False, "", b"", {}

    def json(self):
        raise ValueError("Nasdaq.com is blocking this PC right now (HTTP 403 Access Denied). It usually clears within "
                         "an hour; the app pauses Nasdaq requests meanwhile and uses Schwab for prices.")


def nasdaq_blocked() -> dict:
    left = _NQ_BLOCK["until"] - time.time()
    return {"blocked": left > 0, "since": _NQ_BLOCK["since"], "retry_min": max(0, round(left / 60))}


def _nq_guard(fn):
    def g(url, *a, **k):
        if "api.nasdaq.com" not in str(url):
            return fn(url, *a, **k)
        if time.time() < _NQ_BLOCK["until"]:
            return _NqBlocked()
        with _nq_pace_lock:                              # never burst: one Nasdaq request per NQ_MIN_GAP, app-wide
            wait = _nq_pace["next"] - time.time()
            if wait > 4 and "/info?" in str(url):        # a price cell is not worth queueing for; it refreshes later
                return _NqBlocked()
            _nq_pace["next"] = max(time.time(), _nq_pace["next"]) + NQ_MIN_GAP
        if wait > 0:
            time.sleep(wait)
        r = fn(url, *a, **k)
        if r.status_code == 403:
            _NQ_BLOCK["hits"] += 1
            _NQ_BLOCK["since"] = _NQ_BLOCK["since"] or et_now().strftime("%H:%M ET")
            _NQ_BLOCK["until"] = time.time() + min(900 * _NQ_BLOCK["hits"], 3600)
            return _NqBlocked()
        if r.status_code == 200:
            _NQ_BLOCK.update(until=0.0, since=None, hits=0)
        return r
    return g


_http = requests.Session()
for _pfx in ("https://", "http://"):
    _http.mount(_pfx, requests.adapters.HTTPAdapter(pool_connections=24, pool_maxsize=48))
requests.get = _nq_guard(_http.get)
_nq_sess.get = _nq_guard(_nq_sess.get)


class _NqRows:
    def __init__(self, rows):
        self.rows, self.status_code = rows, 200

    def json(self):
        return {"data": {"rows": self.rows}}


NQ_SCREEN_PATH = APP_DIR / "nasdaq_screener_cache.json"
_nq_scr = {"ts": 0.0, "rows": None}
_nq_scr_lock = threading.Lock()


def nasdaq_stock_rows(max_age: float = 1800) -> list:
    """Nasdaq.com's full stock screener, downloaded once per 30 min and shared by every scan (it was fetched by each
    scan separately). When Nasdaq is unavailable, the last saved copy (up to 7 days old) is used."""
    with _nq_scr_lock:
        if _nq_scr["rows"] and time.time() - _nq_scr["ts"] < max_age:
            return _nq_scr["rows"]
        err = ""
        try:
            r = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true",
                             headers=NASDAQ_HDR, timeout=60)
            rows = ((r.json() or {}).get("data") or {}).get("rows") or []
        except (requests.RequestException, ValueError) as e:
            rows, err = [], str(e)
        if len(rows) > 1000:
            _nq_scr.update(ts=time.time(), rows=rows, src="nasdaq")
            try:
                with _io_lock:
                    _write_json(NQ_SCREEN_PATH, {"ts": time.time(), "rows": rows})
            except Exception:
                pass
            return rows
        c = _read_json(NQ_SCREEN_PATH, {}) or {}
        try:                                             # Nasdaq.com unavailable: rebuild the list without it
            fb = _universe_rows_fallback(c.get("rows") or [])
        except Exception as e:
            fb, err = [], f"{err}; fallback: {e}"
        if len(fb) > 1000:
            _nq_scr.update(ts=time.time() - max_age + 900, rows=fb, src="schwab")   # try Nasdaq again in 15 min
            return fb
        if c.get("rows") and time.time() - c.get("ts", 0) < 7 * 86400:
            _nq_scr.update(ts=time.time() - max_age + 600, rows=c["rows"])     # try Nasdaq again in 10 min
            return c["rows"]
        raise RuntimeError(err or "Nasdaq.com stock screener returned no rows")


NQT_LISTS = ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
             "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt")


def _universe_rows_fallback(old_rows: list) -> list:
    """The stock list without api.nasdaq.com: every listed common stock from Nasdaq Trader's official symbol directory
    (a different server), priced by Schwab (last, volume, shares outstanding -> market cap). Sector / industry are
    carried over from the last saved Nasdaq copy when there is one. Same row shape as Nasdaq's screener."""
    if not _sw_live():
        raise RuntimeError("Schwab is not connected")
    names = {}
    for u in NQT_LISTS:
        t = requests.get(u, timeout=30, headers=HEADERS).text
        lines = t.splitlines()
        hd = lines[0].split("|")
        i_sym, i_name, i_etf, i_test = (hd.index("Symbol") if "Symbol" in hd else hd.index("ACT Symbol"),
                                        hd.index("Security Name"), hd.index("ETF"), hd.index("Test Issue"))
        for ln in lines[1:]:
            f = ln.split("|")
            if len(f) <= max(i_sym, i_name, i_etf, i_test) or f[i_etf] == "Y" or f[i_test] == "Y":
                continue
            if re.fullmatch(r"[A-Z]{1,5}", f[i_sym]) and not re.search(r"Warrant|Right|Unit|Preferred|Depositary Shares? .*Preferred|Notes? due", f[i_name]):
                names[f[i_sym]] = f[i_name]
    if len(names) < 3000:
        raise RuntimeError("Nasdaq Trader symbol directory looked incomplete")
    old = {str(x.get("symbol", "")).strip().upper(): x for x in old_rows}
    syms, rows = sorted(names), []
    for i in range(0, len(syms), 200):
        j = _sw_get("/marketdata/v1/quotes", {"symbols": ",".join(syms[i:i + 200]), "fields": "quote,fundamental",
                                               "indicative": "false"})
        for sym, v in (j or {}).items():
            if not isinstance(v, dict):
                continue
            q, fu = v.get("quote") or {}, v.get("fundamental") or {}
            px = q.get("lastPrice") or q.get("closePrice")
            if not px:
                continue
            sh = fu.get("sharesOutstanding") or 0
            o = old.get(sym) or {}
            rows.append({"symbol": sym, "name": o.get("name") or names.get(sym, ""), "lastsale": f"{float(px):.4f}",
                         "volume": str(int(q.get("totalVolume") or 0)), "marketCap": f"{float(px) * float(sh):.0f}" if sh else "",
                         "pctchange": f"{float(q.get('netPercentChange') or 0):.3f}%", "netchange": str(q.get("netChange") or ""),
                         "sector": o.get("sector") or "", "industry": o.get("industry") or "", "country": o.get("country") or "",
                         "_fb": True})
    return rows


def _session_status() -> str:
    n = et_now()
    if market_open_now():
        return "Market Open"
    if n.weekday() < 5 and (4, 0) <= (n.hour, n.minute) < (9, 30):
        return "Pre-Market"
    if n.weekday() < 5 and 16 <= n.hour < 20:
        return "After-Hours"
    return "Closed"


def schwab_live(tks: list) -> dict:
    """Live last price for the page's price cells from Schwab (one request per 200 symbols)."""
    out = {}
    if not tks or not _sw_live():
        return out
    st = _session_status()
    rest = []
    for t in tks:                                          # pushed by Schwab's stream: no request needed
        q = stream_quote(t)
        if q:
            out[t] = q
        else:
            rest.append(t)
    tks = rest
    for i in range(0, len(tks), 200):
        j = _sw_get("/marketdata/v1/quotes", {"symbols": ",".join(tks[i:i + 200]), "fields": "quote,regular", "indicative": "false"})
        for sym, v in (j or {}).items():
            q = (v.get("quote") or {}) if isinstance(v, dict) else {}
            px = q.get("lastPrice")
            if px:
                ts = q.get("tradeTime") or q.get("quoteTime")
                out[sym] = {"price": float(px), "chg": q.get("netChange"), "chg_pct": q.get("netPercentChange"),
                            "ts": datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(et_now().tzinfo).strftime("%H:%M:%S ET") if ts else None,
                            "realtime": bool(v.get("realtime", True)), "status": st, "close": None, "src": "schwab",
                            "low": q.get("lowPrice") or None, "high": q.get("highPrice") or None}
                rg = v.get("regular") or {}                # outside 9:30-16:00: official regular-session close
                if st != "Market Open" and rg.get("regularMarketLastPrice"):   # (lastPrice is the extended-hours trade)
                    out[sym].update(close=float(rg["regularMarketLastPrice"]), close_chg=rg.get("regularMarketNetChange"),
                                    close_chg_pct=rg.get("regularMarketPercentChange"))
                _nq_cache[sym] = (time.time(), out[sym])
    return out


# ---------------------------------------------------------------- Schwab streaming quotes (real-time push, no polling)
# One websocket to Schwab's streamer: Schwab pushes every quote change for the symbols the app asks for, so prices,
# open-trade values and the hold / cut / take calls follow the market within seconds without spending any of the
# ~120-requests-a-minute allowance. Market data only. The stream's address comes from Schwab's userPreference call;
# only its streamerInfo block is used - nothing else in that answer is read, kept or logged. Nothing is ever sent
# to Schwab except the login and the list of symbols to watch.
SW_EQ_FIELDS = "0,1,2,3,8,10,11,12,17,18,29,31,34,35,36,42,43"   # bid ask last volume high low close open chg reg-close ...
SW_OP_FIELDS = "0,2,3,4,9,10,28,35,37,38"                         # bid ask last OI vol delta underlying mark quote-time
SW_STREAM_MAX = {"eq": 400, "op": 200}
_STREAM_OK = re.compile(r"[A-Z]{1,6}")


class _SwStream:
    def __init__(self):
        self.eq, self.op = {}, {}
        self.want = {"eq": {}, "op": {}}
        self.sent = {"eq": set(), "op": set()}
        self.connected, self.err, self.since, self.last_msg, self.n_msg, self.rev = False, None, None, 0.0, 0, 0
        self.lock = threading.Lock()

    def ask(self, kind: str, syms) -> None:
        """Add symbols to the watch list (sent to Schwab within a second). Extra symbols past the cap are ignored."""
        with self.lock:
            w = self.want[kind]
            for x in syms:
                if x and x not in w and len(w) < SW_STREAM_MAX[kind]:
                    w[x] = None

    def status(self) -> dict:
        return {"on": bool(self.connected), "since": self.since, "error": self.err, "stocks": len(self.eq),
                "options": len(self.op), "updates": self.n_msg,
                "last_s": round(time.time() - self.last_msg, 1) if self.last_msg else None}

    def run(self) -> None:
        back = 5
        while True:
            try:
                if _sw_live():
                    import asyncio
                    asyncio.run(self._session())
                    back = 5
            except Exception as e:
                self.err = (str(e) or type(e).__name__)[:160]
            self.connected = False
            time.sleep(back)
            back = min(back * 2, 300)

    async def _session(self) -> None:
        import asyncio
        import websockets
        si = ((_sw_get("/trader/v1/userPreference", {}) or {}).get("streamerInfo") or [{}])[0]
        tk = _sw_access()
        if not si.get("streamerSocketUrl") or not tk:
            raise RuntimeError("Schwab did not give a stream address (log in again?)")
        base = {"SchwabClientCustomerId": si.get("schwabClientCustomerId"), "SchwabClientCorrelId": si.get("schwabClientCorrelId")}
        rid = [0]

        def req(service, command, params):
            rid[0] += 1
            return {"requestid": str(rid[0]), "service": service, "command": command, **base, "parameters": params}
        async with websockets.connect(si["streamerSocketUrl"], max_size=None, ping_interval=None, open_timeout=20) as ws:
            await ws.send(json.dumps({"requests": [req("ADMIN", "LOGIN", {
                "Authorization": tk, "SchwabClientChannel": si.get("schwabClientChannel"),
                "SchwabClientFunctionId": si.get("schwabClientFunctionId")})]}))
            m = json.loads(await asyncio.wait_for(ws.recv(), 20))
            c = ((m.get("response") or [{}])[0].get("content") or {})
            if c.get("code") != 0:
                raise RuntimeError(f"Schwab stream login refused (code {c.get('code')})")
            with self.lock:
                self.sent = {"eq": set(), "op": set()}
                self.eq.clear()
                self.op.clear()
            self.connected, self.err, self.last_msg = True, None, time.time()
            self.since = et_now().strftime("%Y-%m-%d %H:%M:%S ET")
            while True:
                out = []
                with self.lock:
                    for kind, svc, fields in (("eq", "LEVELONE_EQUITIES", SW_EQ_FIELDS), ("op", "LEVELONE_OPTIONS", SW_OP_FIELDS)):
                        new = [x for x in self.want[kind] if x not in self.sent[kind]]
                        for i in range(0, len(new), 100):
                            part = new[i:i + 100]
                            out.append(req(svc, "ADD" if self.sent[kind] else "SUBS", {"keys": ",".join(part), "fields": fields}))
                            self.sent[kind].update(part)
                if out:
                    await ws.send(json.dumps({"requests": out}))
                try:
                    raw = await asyncio.wait_for(ws.recv(), 1.0)
                except asyncio.TimeoutError:
                    if time.time() - self.last_msg > 90:       # Schwab sends a heartbeat every few seconds
                        raise RuntimeError("Schwab stream went quiet - reconnecting")
                    if not _sw_live():
                        raise RuntimeError("Schwab login expired")
                    continue
                self.last_msg = time.time()
                m = json.loads(raw)
                for x in m.get("data") or []:
                    tgt = self.eq if x.get("service") == "LEVELONE_EQUITIES" else self.op if x.get("service") == "LEVELONE_OPTIONS" else None
                    if tgt is None:
                        continue
                    now = time.time()
                    for row in x.get("content") or []:
                        k = row.get("key")
                        if not k:
                            continue
                        d = dict(tgt.get(k) or {})
                        d.update({f: v for f, v in row.items() if f.isdigit()})
                        d["_t"] = now
                        tgt[k] = d                             # swapped in whole: readers never see a half-updated quote
                    self.rev += 1
                    self.n_msg += 1
                for x in m.get("response") or []:
                    c = x.get("content") or {}
                    if c.get("code") not in (0, None):
                        self.err = f"{x.get('service')} {x.get('command')}: {str(c.get('msg'))[:100]}"


_stream = _SwStream()


def _sw_osym(ticker: str, exp: str, cp: str, k: float) -> str:
    """Schwab's option symbol: root padded to 6, YYMMDD, C/P, strike x 1000 in 8 digits."""
    return f"{ticker:<6}{exp[2:4]}{exp[5:7]}{exp[8:10]}{cp}{int(round(float(k) * 1000)):08d}"


def stream_quote(sym: str) -> dict | None:
    """Live stock quote pushed by Schwab, in schwab_live()'s shape. None when the stream is down or has not sent this
    symbol yet (it is then added to the watch list, and the caller uses a normal request this once)."""
    s = _stream
    if not s.connected or sym in INDEXES or not _STREAM_OK.fullmatch(sym or ""):
        return None
    d = s.eq.get(sym)
    if not d or not d.get("3"):
        s.ask("eq", [sym])
        return None
    st = _session_status()
    ts = d.get("35") or d.get("34")
    q = {"price": float(d["3"]), "chg": d.get("18"), "chg_pct": d.get("42"),
         "ts": datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(et_now().tzinfo).strftime("%H:%M:%S ET") if ts else None,
         "realtime": True, "status": st, "close": None, "src": "schwab", "low": d.get("11") or None, "high": d.get("10") or None,
         "bid": d.get("1"), "ask": d.get("2"), "stream": True}
    if st != "Market Open" and d.get("29"):               # outside 9:30-16:00: official regular-session close
        q.update(close=float(d["29"]), close_chg=d.get("31"), close_chg_pct=d.get("43"))
    return q


_nq_sess.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def nasdaq_quote(ticker: str) -> dict | None:
    """Real-time last sale from Nasdaq.com (Nasdaq Last Sale). None if Nasdaq has no quote for the symbol."""
    if ticker in INDEXES or not re.fullmatch(r"[A-Z]{1,6}", ticker):
        return None
    sq = stream_quote(ticker)                          # pushed by Schwab: current to the second, no request
    if sq:
        return sq
    hit = _nq_cache.get(ticker)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    try:                                               # Schwab first when connected; Nasdaq.com only as the backup
        q = schwab_live([ticker]).get(ticker)
        if q:
            return q
    except Exception:
        pass
    for cls in ([_nq_class[ticker]] if ticker in _nq_class else ["stocks", "etf"]):
        try:
            r = _nq_sess.get(f"https://api.nasdaq.com/api/quote/{ticker}/info?assetclass={cls}",
                             headers=NASDAQ_HDR, timeout=8)
            d = (r.json() or {}).get("data")
        except (requests.RequestException, ValueError):
            return None
        if not d or not d.get("primaryData"):
            continue
        _nq_class[ticker] = cls
        p, s2 = d["primaryData"], d.get("secondaryData") or {}
        price = _num(p.get("lastSalePrice"))
        if price is None:
            return None
        q = {"price": price, "chg": _num(p.get("netChange")), "chg_pct": _num(p.get("percentageChange")),
             "ts": p.get("lastTradeTimestamp"), "realtime": bool(p.get("isRealTime")),
             "status": d.get("marketStatus") or "", "close": None, "low": None, "high": None}
        rng = str((((d.get("keyStats") or {}).get("dayrange") or {}).get("value")) or "")
        m = re.match(r"\s*([\d.,]+)\s*-\s*([\d.,]+)", rng)
        if m:
            q["low"], q["high"] = _num(m.group(1)), _num(m.group(2))
        if s2 and _num(s2.get("lastSalePrice")) is not None:      # outside regular hours: official close
            q.update(close=_num(s2.get("lastSalePrice")), close_chg=_num(s2.get("netChange")),
                     close_chg_pct=_num(s2.get("percentageChange")), close_ts=s2.get("lastTradeTimestamp"))
        _nq_cache[ticker] = (time.time(), q)
        return q
    return None


def _live_one(ticker: str) -> dict | None:
    """Live stock price for the page's price cells: Nasdaq last sale (real time, incl. pre-market / after-hours)."""
    try:
        q = nasdaq_quote(ticker)
    except Exception:
        return None
    if not q:
        return None
    return {k: q.get(k) for k in ("price", "chg", "chg_pct", "ts", "realtime", "status", "close")}


def nasdaq_open(ticker: str) -> float | None:
    """Today's regular-session opening print from Nasdaq.com's intraday chart (first trade at/after 9:30 ET)."""
    cls = _nq_class.get(ticker, "stocks")
    try:
        d = requests.get(f"https://api.nasdaq.com/api/quote/{ticker}/chart?assetclass={cls}",
                         headers=NASDAQ_HDR, timeout=8).json().get("data") or {}
    except (requests.RequestException, ValueError, AttributeError):
        return None
    for pt in d.get("chart") or []:     # x is Eastern wall-clock time encoded as if it were UTC
        t = datetime.fromtimestamp(pt["x"] / 1000, timezone.utc) if isinstance(pt.get("x"), (int, float)) else None
        if t and (t.hour, t.minute) >= (9, 30):
            return _num(pt.get("y"))
    return None


def _rsi_closes(closes: list) -> float | None:
    c = pd.Series([x for x in closes if x is not None], dtype=float)
    if len(c) < 15:
        return None
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    return 100.0 if l == 0 else float(100 - 100 / (1 + g / l))


# ---------------------------------------------------------------- GEX history (15-min snapshots, session change)
GEX_HIST_PATH = APP_DIR / "gex_history.json"
_gh_lock = threading.Lock()
_recent_lookups: list = []


def record_gex(ticker: str, meta: dict, spot: float, df: pd.DataFrame, exps_out: list) -> None:
    """Save one snapshot per CBOE quote time: total net GEX, and per expiration net GEX / put wall / call wall."""
    lt = str(meta.get("last_trade") or "")
    if not re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", lt):
        return
    snap = {"ts": lt[:19], "spot": float(spot), "net": float(df["gex"].sum()),
            "exps": {e["exp"]: [e["net_gex"], e["put_wall"], e["call_wall"]] for e in exps_out}}
    day = lt[:10]
    with _gh_lock:
        h = _read_json(GEX_HIST_PATH, {})
        th = h.setdefault(ticker, {})
        lst = th.setdefault(day, [])
        if lst and lst[-1]["ts"] == snap["ts"]:
            lst[-1]["exps"].update(snap["exps"])           # same quote time: keep, fill in any new expirations
        elif lst and (datetime.fromisoformat(snap["ts"]) - datetime.fromisoformat(lst[-1]["ts"])).total_seconds() < 300:
            lst[-1] = snap                                 # quotes less than 5 min apart: keep the newer one
        else:
            lst.append(snap)
        del lst[:-80]
        for d in sorted(th)[:-4]:                          # keep the last 4 sessions
            del th[d]
        _write_json(GEX_HIST_PATH, h)


def gex_changes(ticker: str, ts: str) -> dict | None:
    """Current snapshot, the one ~15 min earlier, today's first, and the prior session's last (its close)."""
    h = _read_json(GEX_HIST_PATH, {}).get(ticker) or {}
    day = str(ts)[:10]
    today = h.get(day) or []
    if not today:
        return None
    cur = next((s for s in reversed(today) if s["ts"] == str(ts)[:19]), today[-1])
    tc = datetime.fromisoformat(cur["ts"])
    prev = next((s for s in reversed(today) if 600 <= (tc - datetime.fromisoformat(s["ts"])).total_seconds()), None)
    older = sorted(d for d in h if d < day)
    return {"cur": cur, "prev": prev, "first": today[0] if today[0] is not cur else None,
            "prior_close": h[older[-1]][-1] if older else None, "n_today": len(today)}


def gex_recorder() -> None:
    """Every 15 min in market hours (plus once after the close) re-pull watched tickers so the history fills in."""
    done_close = None
    while True:
        try:
            now = et_now()
            mins = now.hour * 60 + now.minute
            weekday = now.weekday() < 5
            in_session = weekday and 9 * 60 + 35 <= mins <= 16 * 60
            close_run = weekday and 16 * 60 + 18 <= mins <= 17 * 60 and done_close != now.date()
            if in_session or close_run:
                cfg = load_config()
                tks = [w["ticker"] for w in cfg.get("watchlist", [])] + [t["ticker"] for t in load_trades(True)] \
                    + list(cfg.get("puts_watchlist") or [])[:10] + _recent_lookups[-8:]
                def _rec(tk):
                    for _ in range(300):                         # a scan / refresh is running: let it have the CPU first
                        if not _heavy.locked():
                            break
                        time.sleep(2)
                    with _lock:
                        _cache.pop((tk, 0.15), None)
                    try:
                        _lookup_core(tk, 0.15)
                    except Exception:
                        pass
                with ThreadPoolExecutor(4) as ex:             # 100+ tickers finish in a few minutes
                    list(ex.map(_bg_cboe(_rec), dict.fromkeys(tks)))
                if close_run:
                    done_close = now.date()
        except Exception:
            pass
        time.sleep(15 * 60 if not (et_now().hour == 16) else 5 * 60)


FIB_RATIOS = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)


def fib_levels(bars: list) -> dict | None:
    """Fibonacci retracements of the dominant swing in these daily bars [date, o, h, l, c, ...].
    Up-swing (low came first): levels count down from the high and act as support; down-swing: up from the low."""
    bars = [b for b in bars if b[2] is not None and b[3] is not None]
    if len(bars) < 10:
        return None
    hi_i = max(range(len(bars)), key=lambda i: bars[i][2])
    lo_i = min(range(len(bars)), key=lambda i: bars[i][3])
    hi, lo = float(bars[hi_i][2]), float(bars[lo_i][3])
    if hi <= lo:
        return None
    up = lo_i < hi_i
    return {"dir": "up" if up else "down", "hi": hi, "lo": lo, "hi_date": str(bars[hi_i][0]), "lo_date": str(bars[lo_i][0]),
            "bars": len(bars), "levels": [{"r": r, "price": hi - (hi - lo) * r if up else lo + (hi - lo) * r} for r in FIB_RATIOS]}


def lookup(ticker: str, band: float = 0.15) -> dict:
    """CBOE option analytics (cached 5 min) + Nasdaq real-time price (refreshed every call, 15 s cache)."""
    core = _lookup_core(ticker, band)
    tk_ = core["ticker"]
    if tk_ in _recent_lookups:
        _recent_lookups.remove(tk_)
    _recent_lookups.append(tk_)
    del _recent_lookups[:-12]
    out = dict(core)
    try:
        live = nasdaq_quote(core["ticker"])
    except Exception:
        live = None
    out["live"] = live
    if not live:
        return out
    open_now = live["status"].lower() == "open"
    if not open_now and live.get("close") is None and live["status"].lower() == "closed":
        # Overnight/weekend Nasdaq drops secondaryData; the primary last sale is then the official close
        # (its lastTradeTimestamp can lag a day, so the session date comes from the price bars instead).
        live = dict(live, close=live["price"], close_chg=live.get("chg"), close_chg_pct=live.get("chg_pct"))
        out["live"] = live
    bar_px = live["price"] if open_now else live.get("close")
    candles = [list(c) for c in core["candles"]]
    if candles and bar_px:
        today = et_now().date().isoformat()
        if open_now and candles[-1][0] < today:         # CBOE has no bar for today yet
            o = nasdaq_open(core["ticker"]) or bar_px
            candles.append([today, o, live.get("high") or max(o, bar_px), live.get("low") or min(o, bar_px), bar_px, None])
        last = candles[-1]
        last[4] = bar_px                                 # live price in session, official close outside it
        last[2] = max(last[2], bar_px) if last[2] is not None else bar_px
        last[3] = min(last[3], bar_px) if last[3] is not None else bar_px
        out["candles"] = candles
        out["rsi"] = _rsi_closes([c[4] for c in candles])
        out["rsi_date"] = last[0]
    if open_now and live.get("chg_pct") is not None:
        out["chg_pct"] = live["chg_pct"]
    elif live.get("close_chg_pct") is not None:
        out["chg_pct"] = live["close_chg_pct"]
    return out


def dealer_exposures(spot: float, df: pd.DataFrame) -> dict:
    """Dealer positioning from the CBOE chain, all at the displayed spot with Black-Scholes:
    gamma (GEX), vanna (delta change per 1 vol point) and charm (delta change per day), the gamma-flip level
    nearest spot, and whether the net-GEX regime holds under three dealer sign conventions."""
    d = df[(df["iv"] > 0) & (df["open_interest"] > 0)].copy()
    if d.empty:
        return {}
    now_et = et_now().replace(tzinfo=None)
    secs = ((d["expiration"] + pd.Timedelta(hours=16)) - now_et).dt.total_seconds().clip(lower=1800)
    t = (secs / (365 * 86400)).to_numpy()
    K, iv, oi = d["strike"].to_numpy(float), d["iv"].to_numpy(float), d["open_interest"].to_numpy(float)
    sgn = np.where(d["type"].to_numpy() == "C", 1.0, -1.0)          # naive: dealers long calls, short puts
    r = 0.04

    def d1(S, sig, tt):
        return (np.log(S / K) + (r + sig * sig / 2) * tt) / (sig * np.sqrt(tt))

    ncdf = _ncdf

    def pdf(x):
        return np.exp(-x * x / 2) / math.sqrt(2 * math.pi)
    x1 = d1(spot, iv, t)
    gam = pdf(x1) / (spot * iv * np.sqrt(t))
    gex_c = gam * oi * 100 * spot ** 2 * 0.01                         # $ per 1% move, before sign
    vanna = ncdf(d1(spot, iv + 0.01, t)) - ncdf(x1)                 # delta change per +1 vol point
    t1 = np.maximum(t - 1 / 365, 1e-6)
    charm = ncdf(d1(spot, iv, t1)) - ncdf(x1)                         # delta change after one day passes
    vex = sgn * vanna * oi * 100 * spot                               # dealer $ delta change per +1 vol pt
    cex = sgn * charm * oi * 100 * spot                               # dealer $ delta change per day
    conv = {"naive (dealers long calls, short puts)": float((sgn * gex_c).sum()),
            "dealers short all options": float(-gex_c.sum()),
            "reverse (dealers short calls, long puts)": float((-sgn * gex_c).sum())}
    signs = {k: (v > 0) for k, v in conv.items()}
    # gamma flip: where net GEX (naive) changes sign as the price moves, the crossing nearest spot
    grid = spot * np.linspace(0.85, 1.15, 121)
    tot = []
    for S in grid:
        g_ = pdf(d1(S, iv, t)) / (S * iv * np.sqrt(t))
        tot.append(float((sgn * g_ * oi * 100 * S ** 2 * 0.01).sum()))
    tot = np.array(tot)
    flips = []
    for i in range(1, len(grid)):
        if (tot[i - 1] < 0) != (tot[i] < 0):
            a, b = tot[i - 1], tot[i]
            flips.append(float(grid[i - 1] + (grid[i] - grid[i - 1]) * (a / (a - b))))
    flip = min(flips, key=lambda f: abs(f - spot)) if flips else None
    by = []
    exps = d["expiration"].to_numpy()
    for e in sorted(set(exps))[:8]:
        m = exps == e
        by.append({"exp": pd.Timestamp(e).strftime("%Y-%m-%d"), "gex": float((sgn * gex_c)[m].sum()),
                   "vex": float(vex[m].sum()), "cex": float(cex[m].sum())})
    return {"gex": conv["naive (dealers long calls, short puts)"], "vex": float(vex.sum()), "cex": float(cex.sum()),
            "flip": flip, "flip_pct": (flip / spot - 1) * 100 if flip else None, "flips": flips[:6],
            "curve": [[round(float(gx), 4), round(v, 0)] for gx, v in zip(grid[::4], tot[::4])],
            "conventions": conv, "robust": signs[list(conv)[0]] == signs[list(conv)[1]], "by_exp": by}


def gex_size(ticker: str, gex, hist: pd.DataFrame) -> dict | None:
    """How big dealer hedging is next to the stock's own trading (idea from simonro/Edge-GEX-Lab, MIT):
    net GEX ($ per 1% move) and net GEX over a typical day's move (14-session ATR) as a share of the
    20-session average dollar volume. Indexes: none (their hedging trades mostly in futures)."""
    if gex is None or ticker in INDEXES or hist is None or len(hist) < 21:
        return None
    h = hist.tail(21)
    c = pd.to_numeric(h["close"], errors="coerce")
    hi = pd.to_numeric(h["high"], errors="coerce")
    lo = pd.to_numeric(h["low"], errors="coerce")
    v = pd.to_numeric(h["volume"], errors="coerce")
    dv = (c * v).tail(20).dropna()
    adv = float(dv.mean()) if len(dv) >= 15 else 0.0
    if not adv > 0:
        return None
    tr = pd.concat([hi - lo, (hi - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1).tail(14).dropna()
    atr_pct = float(tr.mean() / c.iloc[-1] * 100) if len(tr) >= 10 and c.iloc[-1] else None
    out = {"adv20": adv, "pct_1pct": abs(gex) / adv * 100, "atr_pct": atr_pct}
    if atr_pct:
        out["pct_day"] = abs(gex) * atr_pct / adv * 100
    return out


def earnings_iv(spot: float, df: pd.DataFrame, hist: pd.DataFrame, ed: str | None, iv30=None) -> dict | None:
    """IV-crush check for a ticker reporting within 60 days, from the CBOE/Schwab chain and daily closes:
    ATM IV of the last expiration before the report vs the first one after it, the earnings-day move that
    term structure implies (event variance = (IV_after^2 - IV_before^2) x T_after), and IV vs 30-day realized vol."""
    if not ed:
        return None
    try:
        e_day = date.fromisoformat(str(ed)[:10])
    except ValueError:
        return None
    today = et_today()
    if not today <= e_day <= today + timedelta(days=60):
        return None
    out = {"date": e_day.isoformat(), "days": (e_day - today).days, "iv30": iv30}
    cl = pd.to_numeric(hist["close"], errors="coerce").dropna() if len(hist) else pd.Series(dtype=float)
    if len(cl) >= 22:
        r = np.log(cl.tail(22)).diff().dropna()
        out["rv30"] = float(r.std() * np.sqrt(252) * 100)

    def atm_iv(e):
        sub = df[(df["expiration"] == e) & (df["iv"] > 0)]
        if sub.empty:
            return None
        k = sub.iloc[(sub["strike"] - spot).abs().argsort()[:1]]["strike"].iloc[0]
        v = sub[sub["strike"] == k]["iv"].mean()
        return float(v * 100) if 0 < v < 5 else None

    exps = sorted(e for e in df["expiration"].unique() if pd.Timestamp(e).date() >= today)
    pre = [e for e in exps if pd.Timestamp(e).date() < e_day]
    post = [e for e in exps if pd.Timestamp(e).date() > e_day]
    if pre:
        out["pre_exp"], out["pre_iv"] = pd.Timestamp(pre[-1]).strftime("%Y-%m-%d"), atm_iv(pre[-1])
    if post:
        out["post_exp"], out["post_iv"] = pd.Timestamp(post[0]).strftime("%Y-%m-%d"), atm_iv(post[0])
        if len(post) > 1:
            out["post2_iv"] = atm_iv(post[1])
    t = max((pd.Timestamp(post[0]).date() - today).days, 1) / 365 if post else None
    base = out.get("pre_iv")
    if not base and out.get("post_iv") and out.get("post2_iv"):
        # no expiration before the report: both carry the event, so back out the ex-event vol from the two
        t2 = max((pd.Timestamp(post[1]).date() - today).days, 1) / 365
        bv = ((out["post2_iv"] / 100) ** 2 * t2 - (out["post_iv"] / 100) ** 2 * t) / (t2 - t) if t2 > t else -1
        base = float(np.sqrt(bv) * 100) if bv > 0 else None
        out["base_iv_from_term"] = base
    if out.get("post_iv") and base:
        ev = (out["post_iv"] / 100) ** 2 * t - (base / 100) ** 2 * t
        out["move_pct"] = float(np.sqrt(ev) * 100) if ev > 0 else 0.0
        out["crush_pts"] = out["post_iv"] - base
    rv = out.get("rv30")
    if out.get("post_iv") and rv:
        out["iv_rv"] = out["post_iv"] / rv
    out["rich"] = bool((out.get("iv_rv") or 0) >= 1.3 and (out.get("crush_pts") or 0) >= 5)
    return out


def _lookup_core(ticker: str, band: float = 0.15) -> dict:
    ticker = clean_ticker(ticker)
    key = (ticker, band)
    bg, sw = getattr(_tl, "cboe", False), _sw_live()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < (CACHE_SECONDS if bg or not sw else 60) \
                and (bg or not sw or hit[1].get("source") == "schwab"):
            return hit[1]
    spot, df, meta = load_chain(ticker, None if bg else 20)
    try:
        hist = daily_history(ticker, meta["bar"])
    except Exception:
        hist = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    rsi, rsi_date = rsi14(ticker, meta["close"], str(meta["last_trade"])[:10], hist)
    chg = meta["chg_pct"]
    if len(hist) >= 2 and meta["bar"].get("date"):
        prior = hist[hist["date"].astype(str) < meta["bar"]["date"]]
        if not prior.empty and prior["close"].iloc[-1]:
            chg = (spot / float(prior["close"].iloc[-1]) - 1) * 100  # vs prior daily close
    trend = None
    cl = pd.to_numeric(hist["close"], errors="coerce").dropna() if len(hist) else pd.Series(dtype=float)
    if len(cl) >= 50:
        sma20, sma50, last = float(cl.tail(20).mean()), float(cl.tail(50).mean()), float(cl.iloc[-1])
        trend = {"sma20": sma20, "sma50": sma50, "trend": int(last > sma20) + int(last > sma50) + int(sma20 > sma50),
                 "r1m": (last / float(cl.iloc[-22]) - 1) * 100 if len(cl) >= 22 else None}
    fib = None
    try:
        h6 = hist.tail(126)
        fib = fib_levels([[str(r.date), r.open, r.high, r.low, r.close] for r in h6.itertuples()])
    except Exception:
        fib = None
    try:
        earn = nasdaq_earnings()
    except Exception:
        earn = None
    try:
        strategy = spread_strategies(spot, df, earn.get(ticker) if earn else None, earn is not None, band, fib)
    except Exception as e:  # never let the strategy block break the lookup
        strategy = {"error": str(e)}
    try:
        earn_iv = earnings_iv(spot, df, hist, earn.get(ticker) if earn else None, meta.get("iv30"))
    except Exception:
        earn_iv = None
    short_puts = {}
    for w_ in week_keys(later=True):
        m_ = w_
        try:
            cs = [c for c in _csp_candidates(spot, df, m_) if c["pop"] >= (80 if is_short_win(w_) else 70)
                  and not (earn and earn.get(ticker) and earn.get(ticker) <= c["exp"])][:10]
            if w_ != "0dte":
                add_p50(spot, cs, "lk" + ticker + w_)
            short_puts[w_] = cs
        except Exception:
            short_puts[w_] = []
    try:
        bull_call = bull_call_setups(spot, df, earn.get(ticker) if earn else None, ticker)
    except Exception as e:
        bull_call = {"error": str(e)}
    tail = hist.tail(1300)   # ~5 years: daily view + weekly candles
    candles = [[str(r.date), r.open, r.high, r.low, r.close, r.volume]
               for r in tail.itertuples() if pd.notna(r.open) and pd.notna(r.high) and pd.notna(r.low)]
    exps = []
    for label, e in pick_expirations(df):
        sub = df[df["expiration"] == e]
        a = analyze_exp(spot, sub, band)
        a.update({"label": label, "exp": e.strftime("%Y-%m-%d"), "dow": e.strftime("%a"),
                  "dte": int(sub["dte"].iloc[0])})
        iv_ = sub[sub["iv"] > 0]
        if not iv_.empty:                              # 1-sigma expected move to expiration from at-the-money IV
            near = iv_.iloc[(iv_["strike"] - spot).abs().argsort()[:4]]
            a["atm_iv"] = float(near["iv"].mean())
            a["em"] = float(spot * a["atm_iv"] * math.sqrt(max(a["dte"], 1) / 365))
        exps.append(a)
    # this week's Wednesday and Friday expirations (today's counts until the 4 PM close)
    now = et_now()
    et_today = pd.Timestamp(now.date())
    after_close = now.hour >= 16
    week = []
    for wd, name in ((2, "Wednesday"), (4, "Friday")):
        cands = [e for e in sorted(df["expiration"].unique()) if pd.Timestamp(e).weekday() == wd
                 and 0 <= (pd.Timestamp(e) - et_today).days <= 7 and not (after_close and pd.Timestamp(e) == et_today)]
        if cands:
            e = pd.Timestamp(cands[0])
            sub = df[df["expiration"] == e]
            a = analyze_exp(spot, sub, band)
            a.update({"label": name, "exp": e.strftime("%Y-%m-%d"), "dow": e.strftime("%a"),
                      "dte": int((e - et_today).days)})
            week.append(a)
        else:
            week.append({"label": name, "missing": True})
    # every week's GEX: the last expiration of each calendar week for the next ~9 weeks (tickers with weekly options)
    by_week: dict = {}
    for e in sorted(df["expiration"].unique()):
        ts = pd.Timestamp(e)
        d = int((ts - et_today).days)
        if d < 0 or (after_close and d == 0):
            continue
        if d > 66:
            break
        by_week.setdefault((ts - pd.Timedelta(days=ts.weekday())).date(), []).append(ts)
    first6 = [wk for wk in by_week if (pd.Timestamp(wk) - et_today).days < 42]
    has_weeklies = len(first6) >= 4
    weeks = []
    for wk in sorted(by_week)[:10] if has_weeklies else []:
        es = by_week[wk]
        e = max(es)
        sub = df[df["expiration"] == e]
        a = analyze_exp(spot, sub, band)
        a.update({"label": f"Wk {e.strftime('%m/%d')}", "exp": e.strftime("%Y-%m-%d"), "dow": e.strftime("%a"),
                  "dte": int((e - et_today).days), "week_of": str(wk), "days": [x.strftime("%a") for x in es],
                  "week_net_gex": float(df[df["expiration"].isin(es)]["gex"].sum())})
        iv_ = sub[sub["iv"] > 0]
        if not iv_.empty:
            near = iv_.iloc[(iv_["strike"] - spot).abs().argsort()[:4]]
            a["atm_iv"] = float(near["iv"].mean())
            a["em"] = float(spot * a["atm_iv"] * math.sqrt(max(a["dte"], 1) / 365))
        a.pop("puts", None)
        weeks.append(a)
    try:
        dealer = dealer_exposures(spot, df)
    except Exception as e:
        dealer = {"error": str(e)}
    try:
        if dealer and not dealer.get("error"):
            dealer["size"] = gex_size(ticker, dealer.get("gex"), hist)
    except Exception:
        pass
    gh = None
    try:
        gex4 = gex_weeks4(spot, df)
    except Exception:
        gex4 = []
    if band == 0.15:
        try:
            record_gex(ticker, meta, spot, df, exps + [w_ for w_ in week if not w_.get("missing")])
            gh = gex_changes(ticker, str(meta["last_trade"]))
        except Exception:
            gh = None
        try:
            record_gex_daily(ticker, meta, spot, df, gex4)
        except Exception:
            pass
    out = {"ticker": ticker, "spot": spot, "rsi": rsi, "rsi_date": rsi_date, "gex_hist": gh, "week_exps": week, "fib": fib,
           "net_gex_total": float(df["gex"].sum()),
           "stamp": meta["stamp"], "last_trade": meta["last_trade"], "band": band,
           "chain_time": meta.get("chain_time"), "chain_age_min": meta.get("chain_age_min"), "chain_stale": meta.get("stale"),
           "source": meta.get("source", "cboe"),
           "chg_pct": chg, "candles": candles, "trend": trend, "strategy": strategy, "short_puts": short_puts, "bull_call": bull_call,
           "weeks": weeks, "has_weeklies": has_weeklies, "gamma_source": meta.get("gamma_source"),
           "dealer": dealer, "earn_iv": earn_iv, "gex4": gex4, "gex_trend": (gex_trend([ticker]).get(ticker) or {}).get("days"),
           "windows": [{"k": w["k"], "label": w["label"], "lo": str(w["lo"]), "hi": str(w["hi"])} for w in exp_windows()],
           "fetched": time.strftime("%Y-%m-%d %H:%M:%S"), "expirations": exps}
    out = _clean(json.loads(json.dumps(out, default=str)))
    with _lock:
        _cache[key] = (time.time(), out)
    return out


# ---------------------------------------------------------------- text mode
def money(v):
    if v is None:
        return "-"
    a, s = abs(v), "-" if v < 0 else ""
    return f"{s}${a/1e9:,.2f}Bn" if a >= 1e9 else f"{s}${a/1e6:,.1f}M" if a >= 1e6 else f"{s}${a/1e3:,.1f}K"


def print_text(r: dict) -> None:
    s = r["spot"]
    rsi = f"{r['rsi']:.1f}" if r["rsi"] is not None else "n/a"
    print(f"\n{r['ticker']}  spot {s:,.2f}  RSI(14) {rsi}  | CBOE last trade {r['last_trade']} ET")
    for e in r["expirations"]:
        pw = f"{e['put_wall']:g} ({(e['put_wall']/s-1)*100:+.1f}%)" if e["put_wall"] else "none"
        cw = f"{e['call_wall']:g} ({(e['call_wall']/s-1)*100:+.1f}%)" if e["call_wall"] else "none"
        pc = f"{e['pc_oi']:.2f}" if e["pc_oi"] else "-"
        thin = "  [thin OI]" if e["put_oi"] + e["call_oi"] < MIN_OI else ""
        print(f"  {e['label']:<16} {e['exp']} {e['dow']} {e['dte']:>3}d  put wall {pw:<16} "
              f"call wall {cw:<16} net {money(e['net_gex']):>9}  P/C OI {pc}{thin}")
        for p in e["puts"][:3]:
            print(f"      {p['strike']:g}P  {p['bid']:.2f}/{p['ask']:.2f}  d {p['delta']:.2f}  IV {p['iv']:.0f}%  "
                  f"OI {p['oi']:,}  ROC {p['roc']:.2f}% ({p['ann']:.0f}%/yr)  B/E {p['be']:.2f}  "
                  f"close@30% {p['tp30']:.2f} / @50% {p['tp50']:.2f}")


def cli() -> None:
    print("Type a ticker (blank to quit).")
    while True:
        t = input("\nTicker: ").strip()
        if not t:
            return
        try:
            print_text(lookup(t))
        except Exception as e:
            print(f"  {e}")


# ---------------------------------------------------------------- downside alerts
DEFAULT_CONFIG = {
    "watchlist": [],            # [{"ticker": "MRVL", "strike": 240}]  strike = your short put (optional)
    "interval_min": 15,         # CBOE quotes are ~15 min delayed; faster adds nothing
    "near_pct": 2.0,            # alert when spot is within this % above the put wall
    "gex_drop_pct": 50.0,       # alert when net GEX falls this % vs prior close / day start
    "strike_near_pct": 3.0,     # alert when spot is within this % above your short strike
    "include_weekly": False,    # also watch this week's expiration (noisier)
    "market_hours_only": True,
    "toast": True,              # Windows notification
    "ntfy": False,              # phone push via ntfy.sh (free, no account)
    "ntfy_topic": "",
    "puts_watchlist": [],       # tickers for the Sell puts tab (any price)
    "puts_max_price": 30.0,     # screener: stocks / ETFs under this price
    "puts_etfs": True,          # include liquid optionable ETFs under puts_max_price
    "puts_daily": True,         # run the put screener automatically each trading day (~10:05 ET)
    "puts_screener": True,      # include the most-traded stocks under puts_max_price in put scans
    "blocks_on": True,          # live block-trade tape (Nasdaq Last Sale Plus) in market hours
    "block_tickers": [],        # empty = watchlists + trades + SPY/QQQ
    "block_min_usd": 1000000.0, # tape: prints at least this many dollars...
    "block_min_shares": 20000.0,  # ...or this many shares
    "block_alert_usd": 5000000.0, # pop-up / phone alert for prints this big
    "min_return_pct": 15.0,     # credit spreads: credit must be at least this % of the max loss (no $96 risk for $4)
    "min_any_return_pct": 10.0, # every listed trade (single puts, covered calls too): profit >= this % of capital
    "picks_min_return_pct": 20.0,  # Picks tab (Strategy Scanner picks, Best use of capital, swing): at least this return
    "star_firms": [],           # optional: extra analyst firms to mark (edit in alerts_config.json)
    "top_analysts_extra": [],   # more ranked analysts to star: [{"name": "...", "firm": "...", "src": "...", "label": "..."}]
}


def _clean(o):
    """NaN/Infinity are not valid JSON for browsers: turn them into null, recursively."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_clean(obj), indent=1, default=str, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict:
    with _io_lock:
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(_read_json(CONFIG_PATH, {}))
        if not cfg.get("ntfy_topic"):
            cfg["ntfy_topic"] = "gexwalls-" + secrets.token_hex(8)  # unguessable topic
            _write_json(CONFIG_PATH, cfg)
        return cfg


WATCH_MAX = 100     # tickers per watchlist
_floor_cache = [0.0, 0.15, 0.10]


def apply_floors(o):
    """Drop saved results (older scans, watchlist status) that break today's return floors."""
    sf, af = return_floors()

    def bad(x):
        if not isinstance(x, dict):
            return False
        cr, w = x.get("credit"), x.get("width")
        if isinstance(cr, (int, float)) and isinstance(w, (int, float)) and w > 0 and x.get("long") is not None:
            return w <= cr or cr / (w - cr) < sf - 1e-9
        cash, prem = x.get("cash"), x.get("prem")
        if isinstance(cash, (int, float)) and cash > 0 and isinstance(prem, (int, float)):
            return prem / cash < af - 1e-9
        return False
    if isinstance(o, list):
        return [apply_floors(v) for v in o if not bad(v)]
    if isinstance(o, dict):
        return {k: (None if bad(v) else apply_floors(v)) for k, v in o.items()}
    return o


def return_floors() -> tuple[float, float]:
    """(credit-spread floor, floor for every other listed trade) as fractions, from alerts_config.json (cached 60 s)."""
    if time.time() - _floor_cache[0] > 60:
        cfg = load_config()
        _floor_cache[:] = [time.time(), max(float(cfg.get("min_return_pct", 15.0)), 10.0) / 100,
                           max(float(cfg.get("min_any_return_pct", 10.0)), 10.0) / 100]
    return _floor_cache[1], _floor_cache[2]


def save_config(new: dict) -> dict:
    cfg = load_config()
    wl = []
    for it in (new.get("watchlist", cfg["watchlist"]) or [])[:WATCH_MAX]:
        t = clean_ticker(str(it.get("ticker", "")))
        k = it.get("strike")
        k = float(k) if k not in (None, "", 0, "0") else None
        if t not in [x["ticker"] for x in wl]:
            wl.append({"ticker": t, "strike": k})
    cfg["watchlist"] = wl
    if "puts_watchlist" in new:
        pw = []
        for t in (new.get("puts_watchlist") or [])[:WATCH_MAX]:
            t = clean_ticker(str(t))
            if t not in pw:
                pw.append(t)
        cfg["puts_watchlist"] = pw
    if "puts_max_price" in new:
        cfg["puts_max_price"] = min(max(float(new["puts_max_price"]), 2.0), 1000.0)
    if "puts_screener" in new:
        cfg["puts_screener"] = bool(new["puts_screener"])
    for k_ in ("puts_etfs", "puts_daily"):
        if k_ in new:
            cfg[k_] = bool(new[k_])
    if "blocks_on" in new:
        cfg["blocks_on"] = bool(new["blocks_on"])
    if "block_tickers" in new:
        cfg["block_tickers"] = [clean_ticker(str(t)) for t in (new.get("block_tickers") or [])][:25]
    for f, lo, hi in (("block_min_usd", 1e5, 1e9), ("block_min_shares", 1000, 1e8), ("block_alert_usd", 2.5e5, 1e10)):
        if f in new:
            cfg[f] = min(max(float(new[f]), lo), hi)
    clamp = {"interval_min": (5, 120), "near_pct": (0.5, 10), "gex_drop_pct": (10, 200), "strike_near_pct": (0.5, 15)}
    for f, (lo, hi) in clamp.items():
        if f in new:
            cfg[f] = min(max(float(new[f]), lo), hi)
    for f in ("include_weekly", "market_hours_only", "toast", "ntfy"):
        if f in new:
            cfg[f] = bool(new[f])
    if "ntfy_topic" in new:
        tp = str(new["ntfy_topic"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", tp):
            raise ValueError("ntfy topic: 8-64 letters, digits, - or _")
        cfg["ntfy_topic"] = tp
    with _io_lock:
        _write_json(CONFIG_PATH, cfg)
    return cfg


def et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # tzdata missing: approximate with EDT
        return datetime.now(timezone(timedelta(hours=-4)))


def et_today() -> date:
    """Today's date in New York. Option expirations and DTE use this, not the PC clock (Joe's PC is on Hawaii time,
    5-6 h behind ET, so date.today() is still 'yesterday' between midnight ET and the HST midnight)."""
    return et_now().date()


def market_open_now() -> bool:
    n = et_now()
    m = n.hour * 60 + n.minute
    return n.weekday() < 5 and 9 * 60 + 30 <= m <= 16 * 60 + 20


def _toast(title: str, msg: str) -> None:
    if os.name != "nt":
        return
    q = lambda x: x.replace("'", "''")
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
          "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
          "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
          f"$n=$t.GetElementsByTagName('text');$n.Item(0).AppendChild($t.CreateTextNode('{q(title)}'))>$null;"
          f"$n.Item(1).AppendChild($t.CreateTextNode('{q(msg)}'))>$null;"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($t))")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       timeout=20, capture_output=True, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def _ntfy(topic: str, title: str, msg: str, urgent: bool) -> None:
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode("utf-8"), timeout=15,
                      headers={"Title": title.encode("ascii", "replace").decode(),
                               "Priority": "high" if urgent else "default", "Tags": "chart_with_downwards_trend"})
    except requests.RequestException:
        pass


def notify(alert: dict, cfg: dict) -> None:
    if cfg.get("toast"):
        _toast(alert["title"], alert["msg"])
    if cfg.get("ntfy") and cfg.get("ntfy_topic"):
        _ntfy(cfg["ntfy_topic"], alert["title"], alert["msg"], alert["level"] == "danger")


def read_alerts(since: int = 0, limit: int = 50) -> list:
    out = []
    try:
        with LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                try:
                    a = json.loads(line)
                except ValueError:
                    continue
                if a.get("id", 0) > since:
                    out.append(a)
    except OSError:
        pass
    return out[-limit:]


def _emit(alerts: list, cfg: dict, tk: str, kind: str, level: str, title: str, msg: str) -> None:
    a = {"id": int(time.time() * 1000) + len(alerts), "ts": et_now().strftime("%Y-%m-%d %H:%M ET"),
         "ticker": tk, "kind": kind, "level": level, "title": title, "msg": msg}
    alerts.append(a)
    with _io_lock:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(a) + "\n")
    notify(a, cfg)


def _usd(v):
    return money(v).replace("$-", "-$")


_SETUP_KEYS = ("exp", "dte", "short", "long", "width", "credit", "nat", "pop", "cw", "maxloss", "roc", "p50", "d50",
               "d50_date", "flat_date", "below_wall", "prem", "cash", "ann", "otm", "sdelta", "be", "earn_cross", "put_wall",
               "debit", "cost", "maxprofit", "rr", "p_max", "p_up50", "d_up50_date", "be_pct", "ldelta")


def _watch_setups(r: dict) -> dict:
    """Top pick per window for the watchlist table: put credit spread per risk profile + best short put."""
    out = {}
    wins = ((r.get("strategy") or {}).get("windows")) or {}
    for w_ in week_keys(later=True):
        prof = (wins.get(w_) or {}).get("profiles") or {}
        sp = (r.get("short_puts") or {}).get(w_) or []
        bw = ((r.get("bull_call") or {}).get(w_) or {}).get("profiles") or {}
        near = (wins.get(w_) or {}).get("nearest") or {}
        pcs = {k: {f: c.get(f) for f in _SETUP_KEYS} for k, v in prof.items() for c in v[:1]}
        for k, v in near.items():
            if k not in pcs and v:
                pcs[k] = {**{f: v[0].get(f) for f in _SETUP_KEYS}, "fallback": True, "issues": v[0].get("issues", [])[:3]}
        out[w_] = {"pcs": pcs,
                   "put": {f: sp[0].get(f) for f in _SETUP_KEYS} if sp else None,
                   "puts": [{f: c.get(f) for f in _SETUP_KEYS} for c in sp[:10]],
                   "bcs": {k: {f: c.get(f) for f in _SETUP_KEYS} for k, v in bw.items() for c in v[:1]}}
    return out


def check_watchlist(cfg: dict | None = None) -> dict:
    """Run every downside rule once for the watchlist. Returns {"alerts": [...], "status": {...}}."""
    cfg = cfg or load_config()
    state = _read_json(STATE_PATH, {})
    today = et_now().date().isoformat()
    near, drop, sn = cfg["near_pct"] / 100, cfg["gex_drop_pct"] / 100, cfg["strike_near_pct"] / 100
    alerts, status = [], {}

    def _fresh(tk):
        with _lock:
            _cache.pop((tk, 0.15), None)  # always pull fresh for alerts
        try:
            return tk, lookup(tk, 0.15), None
        except Exception as e:
            return tk, None, e
    with ThreadPoolExecutor(4) as ex:     # up to 100 tickers: fetch in parallel, then apply the rules in order
        got = {tk: (r_, e_) for tk, r_, e_ in ex.map(_bg_cboe(_fresh), list(dict.fromkeys(i["ticker"] for i in cfg["watchlist"])))}
    for item in cfg["watchlist"]:
        tk, strike = item["ticker"], item.get("strike")
        r, err = got.get(tk, (None, RuntimeError("not fetched")))
        if r is None:  # keep checking the rest
            status[tk] = {"error": str(err), "checked": et_now().strftime("%H:%M ET")}
            continue
        spot = (r.get("live") or {}).get("price") or r["spot"]
        chg = r.get("chg_pct")
        ctx = f"Spot {spot:,.2f}" + (f" ({chg:+.1f}% today)" if isinstance(chg, (int, float)) else "")
        st = {"spot": spot, "chg_pct": chg, "rsi": r["rsi"], "strike": strike, "gex4": r.get("gex4"),
              "checked": et_now().strftime("%Y-%m-%d %H:%M ET"), "exps": []}
        for e in r["expirations"]:
            if e["label"] == "Weekly" and not cfg["include_weekly"]:
                continue
            key = f"{tk}|{e['exp']}"
            prev = state.get(key, {})
            flags = set(prev.get("flags", []))
            pw, net = e["put_wall"], e["net_gex"]
            tag = f"{tk} {e['exp'][5:]} ({e['dte']}d)"
            pws = f"{pw:g}" if pw else "none"
            # reference for GEX-drop: prior day's last reading, fixed for the whole day
            if prev and prev.get("day") != today:
                base = prev.get("net")
            else:
                base = prev.get("base", net)
            # 1-2. put wall break / approach
            if pw:
                if spot < pw:
                    if "below_wall" not in flags:
                        _emit(alerts, cfg, tk, "wall_break", "danger", f"{tk} broke put wall {pw:g}",
                              f"{tag}: {ctx} is below put wall {pw:g}. Net GEX {_usd(net)}.")
                        flags.add("below_wall")
                    flags.discard("near_wall")
                else:
                    if spot >= pw * 1.005:
                        flags.discard("below_wall")
                    if spot <= pw * (1 + near):
                        if "near_wall" not in flags:
                            _emit(alerts, cfg, tk, "wall_near", "warn", f"{tk} near put wall {pw:g}",
                                  f"{tag}: {ctx} is {(spot / pw - 1) * 100:.1f}% above put wall {pw:g}. Net GEX {_usd(net)}.")
                            flags.add("near_wall")
                    elif spot > pw * (1 + near * 1.5):
                        flags.discard("near_wall")
            # 3. net GEX flips negative (only on a transition we observed)
            if net < 0:
                if "neg_gex" not in flags and prev and prev.get("net", 0) >= 0:
                    _emit(alerts, cfg, tk, "gex_flip", "danger", f"{tk} GEX flipped negative",
                          f"{tag}: net GEX {_usd(prev.get('net'))} -> {_usd(net)}; dealers now amplify moves. {ctx}. Put wall {pws}.")
                flags.add("neg_gex")
            else:
                flags.discard("neg_gex")
            # 4. net GEX drop vs prior close / day start
            if base is not None and f"drop:{today}" not in flags:
                if (base - net) / max(abs(base), 1.0) >= drop:
                    _emit(alerts, cfg, tk, "gex_drop", "warn", f"{tk} GEX down {(base - net) / max(abs(base), 1) * 100:.0f}%",
                          f"{tag}: net GEX {_usd(base)} -> {_usd(net)}. {ctx}. Put wall {pws}.")
                    flags.add(f"drop:{today}")
            # 5. put wall stepped down
            ppw = prev.get("pw")
            if pw and ppw and pw < ppw and f"walldown:{today}" not in flags:
                _emit(alerts, cfg, tk, "wall_down", "warn", f"{tk} put wall moved down {ppw:g} -> {pw:g}",
                      f"{tag}: support stepped lower to {pw:g}. {ctx}. Net GEX {_usd(net)}.")
                flags.add(f"walldown:{today}")
            flags = {f for f in flags if ":" not in f or f.endswith(today)}
            state[key] = {"day": today, "base": base, "net": net, "pw": pw, "spot": spot, "flags": sorted(flags)}
            st["exps"].append({"label": e["label"], "exp": e["exp"], "dte": e["dte"], "put_wall": pw,
                               "call_wall": e["call_wall"], "net_gex": net,
                               "dist_pct": (spot / pw - 1) * 100 if pw else None})
        st["setups"] = {} if r.get("chain_stale") else _watch_setups(r)
        if r.get("chain_stale"):
            st["stale"] = r.get("chain_time")
        st["net_total"] = r.get("net_gex_total")
        gh = r.get("gex_hist") or {}
        cur = (gh.get("cur") or {}).get("net")
        prev, base = (gh.get("prev") or {}).get("net"), (gh.get("prior_close") or gh.get("first") or {}).get("net")
        st["gex_chg15"] = cur - prev if cur is not None and prev is not None else None
        st["gex_chg_sess"] = cur - base if cur is not None and base is not None else None
        st["gex_since"] = "prior close" if gh.get("prior_close") else ("today's first reading" if gh.get("first") else None)
        # 6. your short strike
        if strike:
            key = f"{tk}|strike|{strike:g}"
            flags = set(state.get(key, {}).get("flags", []))
            if spot < strike:
                if "below_strike" not in flags:
                    _emit(alerts, cfg, tk, "strike_break", "danger", f"{tk} below your {strike:g} strike",
                          f"{ctx} is under your short {strike:g} put.")
                    flags.add("below_strike")
            elif spot <= strike * (1 + sn):
                flags.discard("below_strike")
                if "near_strike" not in flags:
                    _emit(alerts, cfg, tk, "strike_near", "warn", f"{tk} near your {strike:g} strike",
                          f"{ctx} is {(spot / strike - 1) * 100:.1f}% above your short {strike:g} put.")
                    flags.add("near_strike")
            elif spot > strike * (1 + sn * 1.5):
                flags -= {"near_strike", "below_strike"}
            state[key] = {"flags": sorted(flags)}
        status[tk] = st
    state["_status"] = status
    with _io_lock:
        _write_json(STATE_PATH, state)
    return {"alerts": alerts, "status": status}


def _lock_is_mine(interval_s: float) -> bool:
    """One monitor at a time (page app vs --watch): heartbeat file."""
    info = _read_json(LOCK_PATH, {})
    if info.get("pid") not in (None, os.getpid()) and time.time() - info.get("ts", 0) < interval_s * 2 + 60:
        return False
    with _io_lock:
        _write_json(LOCK_PATH, {"pid": os.getpid(), "ts": time.time()})
    return True


def monitor_loop(verbose: bool = False) -> None:
    _monitor["running"] = True
    while True:
        cfg = load_config()
        iv = cfg["interval_min"] * 60
        if not cfg["watchlist"] and not load_trades(True):
            _monitor["note"] = "watchlist empty"
        elif cfg["market_hours_only"] and not market_open_now():
            _monitor["note"] = "market closed"
        elif not _lock_is_mine(iv):
            _monitor["note"] = "another monitor is running"
        else:
            res = check_watchlist(cfg) if cfg["watchlist"] else {"alerts": [], "status": {}}
            try:
                res["alerts"] += check_trades(cfg)
            except Exception as e:  # never stop the monitor over a trade check
                _monitor["trade_error"] = str(e)
            _monitor["last_run"] = et_now().strftime("%Y-%m-%d %H:%M ET")
            _monitor["note"] = f"{len(res['alerts'])} new alert(s)"
            if verbose:
                print(f"[{_monitor['last_run']}] checked {len(cfg['watchlist'])} tickers, {len(res['alerts'])} alert(s)")
                for a in res["alerts"]:
                    print(f"  ! {a['title']}: {a['msg']}")
        _monitor["next_run"] = (et_now() + timedelta(seconds=iv)).strftime("%H:%M ET")
        time.sleep(iv)


# ---------------------------------------------------------------- put credit spread scanner
# Universe: sector/thematic ETFs + liquid optionable stocks (+ your watchlist). Edit freely.
SCAN_ETFS = {
    "SPY": "Broad market", "QQQ": "Nasdaq 100", "IWM": "Small caps", "DIA": "Dow 30",
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care", "XLI": "Industrials",
    "XLY": "Cons. Discretionary", "XLP": "Cons. Staples", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication", "SMH": "Semiconductors", "XBI": "Biotech",
    "KRE": "Regional Banks", "ITA": "Aerospace & Defense", "XHB": "Homebuilders", "XRT": "Retail",
    "GDX": "Gold Miners", "GLD": "Gold", "SLV": "Silver", "TLT": "Long Treasuries", "USO": "Oil",
    "IBIT": "Bitcoin", "URA": "Uranium",
    # expanded rotation map: industries, themes, commodities, international, rates/credit
    "IGV": "Software", "CIBR": "Cybersecurity", "SKYY": "Cloud", "BOTZ": "Robotics & AI", "ARKK": "Innovation",
    "XME": "Metals & Mining", "COPX": "Copper Miners", "SIL": "Silver Miners", "LIT": "Lithium & Battery",
    "XOP": "Oil & Gas E&P", "OIH": "Oil Services", "UNG": "Natural Gas", "TAN": "Solar", "ICLN": "Clean Energy",
    "NLR": "Nuclear", "PAVE": "Infrastructure", "JETS": "Airlines", "IYT": "Transports", "KBE": "Banks",
    "IAI": "Brokers & Exchanges", "KIE": "Insurance", "IBB": "Large Biotech", "IHI": "Medical Devices",
    "XPH": "Pharma", "KWEB": "China Internet", "FXI": "China Large Cap", "EWZ": "Brazil", "EWJ": "Japan",
    "INDA": "India", "EEM": "Emerging Markets", "EFA": "Developed ex-US", "HYG": "High Yield Credit",
    "IEF": "7-10y Treasuries", "UUP": "US Dollar", "ETHA": "Ethereum",
}
SCAN_STOCKS = {
    "AAPL": "Technology", "MSFT": "Technology", "ORCL": "Technology", "CRM": "Technology", "ADBE": "Technology",
    "PLTR": "Technology", "ANET": "Technology", "APLD": "Technology", "IREN": "Technology",
    "NVDA": "Semiconductors", "AMD": "Semiconductors", "AVGO": "Semiconductors", "MRVL": "Semiconductors",
    "MU": "Semiconductors", "QCOM": "Semiconductors", "ALAB": "Semiconductors", "SNDK": "Semiconductors",
    "GOOGL": "Communication", "META": "Communication", "NFLX": "Communication", "DIS": "Communication",
    "AMZN": "Cons. Discretionary", "TSLA": "Cons. Discretionary", "HD": "Cons. Discretionary",
    "NKE": "Cons. Discretionary", "CMG": "Cons. Discretionary",
    "JPM": "Financials", "BAC": "Financials", "GS": "Financials", "MS": "Financials", "WFC": "Financials",
    "C": "Financials", "SCHW": "Financials", "COIN": "Financials", "HOOD": "Financials",
    "XOM": "Energy", "CVX": "Energy", "OXY": "Energy", "SLB": "Energy",
    "UNH": "Health Care", "LLY": "Health Care", "JNJ": "Health Care", "ABBV": "Health Care", "MRK": "Health Care",
    "PFE": "Health Care", "CAT": "Industrials", "GE": "Industrials", "BA": "Industrials", "DE": "Industrials",
    "UBER": "Industrials", "LMT": "Aerospace & Defense", "RTX": "Aerospace & Defense", "KTOS": "Aerospace & Defense",
    "FCX": "Materials", "NEM": "Gold Miners", "WMT": "Cons. Staples", "COST": "Cons. Staples", "KO": "Cons. Staples",
    "PG": "Cons. Staples", "VST": "Utilities", "CEG": "Utilities", "NEE": "Utilities",
    # expanded
    "NOW": "Software", "INTU": "Software", "SHOP": "Software", "APP": "Software", "WDAY": "Software",
    "PANW": "Cybersecurity", "CRWD": "Cybersecurity", "ZS": "Cybersecurity", "FTNT": "Cybersecurity",
    "SNOW": "Cloud", "NET": "Cloud", "DDOG": "Cloud", "MDB": "Cloud",
    "TSM": "Semiconductors", "INTC": "Semiconductors", "ARM": "Semiconductors", "LRCX": "Semiconductors",
    "AMAT": "Semiconductors", "KLAC": "Semiconductors", "ON": "Semiconductors", "COHR": "Semiconductors",
    "CRDO": "Semiconductors", "SMCI": "Technology", "DELL": "Technology", "IONQ": "Innovation", "RGTI": "Innovation",
    "RDDT": "Communication", "SPOT": "Communication", "T": "Communication", "VZ": "Communication",
    "MCD": "Cons. Discretionary", "SBUX": "Cons. Discretionary", "ABNB": "Cons. Discretionary", "BKNG": "Cons. Discretionary",
    "GM": "Cons. Discretionary", "F": "Cons. Discretionary", "CVNA": "Cons. Discretionary", "DKNG": "Cons. Discretionary",
    "LOW": "Retail", "TGT": "Retail", "TJX": "Retail", "LULU": "Retail",
    "V": "Financials", "MA": "Financials", "AXP": "Financials", "BLK": "Financials", "BX": "Financials", "KKR": "Financials",
    "PYPL": "Financials", "SOFI": "Financials", "AFRM": "Financials", "IBKR": "Brokers & Exchanges", "CME": "Brokers & Exchanges",
    "PGR": "Insurance", "COP": "Oil & Gas E&P", "EOG": "Oil & Gas E&P", "DVN": "Oil & Gas E&P",
    "HAL": "Oil Services", "BKR": "Oil Services", "EQT": "Natural Gas",
    "SO": "Utilities", "DUK": "Utilities", "OKLO": "Nuclear", "SMR": "Nuclear", "CCJ": "Uranium", "LEU": "Uranium",
    "NUE": "Metals & Mining", "AA": "Metals & Mining", "MP": "Metals & Mining", "SCCO": "Copper Miners",
    "ALB": "Lithium & Battery", "AEM": "Gold Miners", "KGC": "Gold Miners", "PAAS": "Silver Miners", "HL": "Silver Miners",
    "ISRG": "Medical Devices", "BSX": "Medical Devices", "TMO": "Health Care", "HIMS": "Health Care",
    "AMGN": "Large Biotech", "VRTX": "Large Biotech", "GILD": "Large Biotech", "REGN": "Large Biotech", "NVO": "Pharma",
    "HON": "Industrials", "ETN": "Industrials", "GEV": "Industrials", "VRT": "Industrials", "PWR": "Infrastructure",
    "URI": "Infrastructure", "UPS": "Transports", "FDX": "Transports", "DAL": "Airlines", "UAL": "Airlines",
    "NOC": "Aerospace & Defense", "GD": "Aerospace & Defense", "RKLB": "Aerospace & Defense", "AVAV": "Aerospace & Defense",
    "PEP": "Cons. Staples", "PM": "Cons. Staples", "MO": "Cons. Staples", "PLD": "Real Estate", "AMT": "Real Estate",
    "BABA": "China Internet", "PDD": "China Internet", "JD": "China Internet", "MSTR": "Bitcoin", "MARA": "Bitcoin",
    "FSLR": "Solar", "ENPH": "Solar", "BE": "Clean Energy",
}
SECTOR_ETF = {v: k for k, v in SCAN_ETFS.items() if k not in ("SPY", "QQQ", "IWM", "DIA")}
QUAD_SCORE = {"Leading": 1.0, "Improving": 0.75, "Weakening": 0.4, "Lagging": 0.0, "Benchmark": 0.5}
SCAN_PATH = APP_DIR / "scan_results.json"
EARN_PATH = APP_DIR / "earnings_cache.json"
# TradingView Lightweight Charts v4.2.3 (Apache-2.0), npm package lightweight-charts@4.2.3, file dist/lightweight-charts.standalone.production.js.
# Served to the page only if the file next to this script matches the known hash (no CDN, no network).
LWC_FILE = APP_DIR / "lightweight-charts.standalone.production.js"
LWC_SHA256 = "c7dda807d662a95b3d257119ed315cec669e3bdf5aaece75c480a39307f23540"
_lwc_body: bytes | None = None


def lwc_js() -> bytes | None:
    global _lwc_body
    if _lwc_body is None:
        try:
            b = LWC_FILE.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(b).hexdigest() != LWC_SHA256:
            return None
        _lwc_body = b
    return _lwc_body
ALWAYS_SHORT = {"SPY": ("ETF", "Broad market"), "QQQ": ("ETF", "Nasdaq 100"), "IWM": ("ETF", "Small caps"),
                "SPX": ("Index", "S&P 500 index")}   # daily expirations: scanned every run for 0 DTE / 1-5 DTE
SCAN_TOP_CHAINS = 26          # how many momentum leaders get a full option-chain scan
_scan = {"running": False, "progress": "", "error": None}


def nasdaq_earnings(days: int = 50) -> dict | None:
    """Upcoming earnings dates {symbol: 'YYYY-MM-DD'} from Nasdaq.com's earnings calendar (cached daily)."""
    cache = _read_json(EARN_PATH, {})
    today = date.today().isoformat()
    if cache.get("fetched") == today and cache.get("map") is not None:
        return cache["map"]
    days_list = [date.today() + timedelta(days=i) for i in range(days)]
    days_list = [d for d in days_list if d.weekday() < 5]

    def one(d):
        try:
            r = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}", timeout=20,
                             headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            rows = ((r.json().get("data") or {}).get("rows")) or []
            return d.isoformat(), [str(x.get("symbol", "")).upper() for x in rows]
        except (requests.RequestException, ValueError, AttributeError):
            return d.isoformat(), None

    m, ok = {}, 0
    with ThreadPoolExecutor(6) as ex:
        for ds, syms in ex.map(one, days_list):
            if syms is None:
                continue
            ok += 1
            for s_ in syms:
                if s_ and (s_ not in m or ds < m[s_]):
                    m[s_] = ds
    if ok >= len(days_list) * 0.7:
        with _io_lock:
            _write_json(EARN_PATH, {"fetched": today, "map": m})
        return m
    return m if ok else None


def _momentum(h: pd.DataFrame, spy: pd.DataFrame) -> dict | None:
    c = pd.Series(h["close"].astype(float).values, index=h["date"].astype(str))
    s = pd.Series(spy["close"].astype(float).values, index=spy["date"].astype(str))
    c, s = c[~c.index.duplicated(keep="last")], s[~s.index.duplicated(keep="last")]
    j = pd.concat([c, s], axis=1, join="inner").dropna().tail(170)
    if len(j) < 75:
        return None
    cc, ss = j.iloc[:, 0], j.iloc[:, 1]
    r1, r3 = cc.iloc[-1] / cc.iloc[-22] - 1, cc.iloc[-1] / cc.iloc[-64] - 1
    s1, s3 = ss.iloc[-1] / ss.iloc[-22] - 1, ss.iloc[-1] / ss.iloc[-64] - 1
    sma20, sma50, last = cc.rolling(20).mean().iloc[-1], cc.rolling(50).mean().iloc[-1], cc.iloc[-1]
    trend = int(last > sma20) + int(last > sma50) + int(sma20 > sma50)
    rs = cc / ss
    ratio = 100 * rs / rs.rolling(50).mean()          # relative-rotation style: RS-ratio
    mom = 100 * ratio / ratio.shift(10)                 # and RS-momentum (10-day)
    rr, rm = float(ratio.iloc[-1]), float(mom.iloc[-1])
    if abs(rr - 100) < 1e-9 and abs(rm - 100) < 1e-9:
        quad = "Benchmark"
    else:
        quad = ("Leading" if rm >= 100 else "Weakening") if rr >= 100 else ("Improving" if rm >= 100 else "Lagging")
    return {"r1": r1 * 100, "r3": r3 * 100, "rs1": (r1 - s1) * 100, "rs3": (r3 - s3) * 100, "trend": trend,
            "quad": quad, "rs_ratio": rr, "rs_mom": rm, "sma50": float(sma50)}


SCAN_WINDOWS = {"mid": (13, 29)}       # scanner-only: 2-4 weeks out (old scans)


def exp_windows() -> list:
    """The one set of expiration chips every section uses: 0 DTE, each calendar week (Mon–Sun, named by its Friday)
    for 9 weeks (≈60 days), then 2–9 months and LEAPS. Dates are Eastern."""
    today = et_now().date()
    out = [{"k": "0dte", "label": "0 DTE", "lo": today, "hi": today}]
    # from Friday on, the current week has no expiration left after today (today's is in 0 DTE) -> start next week
    mon = today - timedelta(days=today.weekday()) + (timedelta(days=7) if today.weekday() >= 4 else timedelta(0))
    for i in range(9):
        m = mon + timedelta(days=7 * i)
        fri = m + timedelta(days=4)
        out.append({"k": "w" + fri.isoformat(), "label": f"Fri {fri.month}/{fri.day}", "lo": max(m, today + timedelta(days=1)),
                    "hi": m + timedelta(days=6)})
    last = out[-1]["hi"]
    out.append({"k": "later", "label": "2–9 mo", "lo": last + timedelta(days=1), "hi": today + timedelta(days=269)})
    out.append({"k": "leaps", "label": "LEAPS", "lo": today + timedelta(days=270), "hi": today + timedelta(days=1100)})
    return out


def week_keys(with_0dte: bool = True, later: bool = False, leaps: bool = False) -> list:
    return [w["k"] for w in exp_windows() if (w["k"].startswith("w") or (with_0dte and w["k"] == "0dte")
                                              or (later and w["k"] == "later") or (leaps and w["k"] == "leaps"))]


def win_range(key: str):
    if not isinstance(key, str) or not (key.startswith("w") or key in ("later", "leaps")):
        return None
    for w in exp_windows():
        if w["k"] == key:
            return w["lo"], w["hi"]
    if key.startswith("w2") and len(key) == 11:           # a week key from an older page load
        try:
            fri = date.fromisoformat(key[1:])
            return fri - timedelta(days=4), fri + timedelta(days=2)
        except ValueError:
            return None
    return None


def is_short_win(key: str) -> bool:
    if key in ("0dte", "d5"):
        return True
    r = win_range(key)
    return bool(r and key.startswith("w") and (r[1] - et_now().date()).days <= 6)


def _in_win(exps: list, key: str, cap: int = 5) -> list:
    lo, hi = win_range(key)
    exps = [e for e in exps if lo <= pd.Timestamp(e).date() <= hi]
    if len(exps) > cap:
        exps = [exps[round(i * (len(exps) - 1) / (cap - 1))] for i in range(cap)]
    return exps
# put credit spread expiration windows (DTE), contiguous from 0 to 90 days: 0 DTE and 1-5 DTE are handled separately
SPREAD_WINDOWS = {"weekly": (6, 12), "w2": (13, 20), "w3": (21, 29), "monthly": (30, 45), "m60": (46, 60), "m90": (61, 90)}


def _put_iv(price: float, S: float, K: float, t: float) -> float | None:
    """Implied volatility of a put from its price (bisection on Black-Scholes)."""
    if price <= max(K * math.exp(-0.04 * t) - S, 0) or t <= 0:
        return None
    lo, hi = 0.01, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        v = float(_bs_put(np.array([S]), K, t, mid)[0])
        lo, hi = (mid, hi) if v < price else (lo, mid)
    return (lo + hi) / 2


def _spread_candidates(spot: float, df: pd.DataFrame, mode: str, min_cw: float = 0.12,
                       min_oi: tuple | None = None, limit: bool = True, band: float = 0.25, relaxed: bool = False) -> list:
    """Put credit spread candidates. relaxed=True widens every gate (POP, delta, open interest, quote width, width)
    so non-ideal spreads are still listed — each one is labelled with what is not ideal (see _pcs_issues)."""
    today = pd.Timestamp(et_now().date())
    exps = sorted(df["expiration"].unique())
    short_mode = is_short_win(mode)
    if mode == "0dte":                       # contracts expiring today (Eastern date), only while the market is open
        now = et_now()
        mins_left = (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60
        et_today = pd.Timestamp(now.date())
        exps = [e for e in exps if pd.Timestamp(e) == et_today] if mins_left > 0 else []
    elif mode == "d5":                       # 1-5 calendar days out
        et_today = pd.Timestamp(et_now().date())
        exps = [e for e in exps if 1 <= (pd.Timestamp(e) - et_today).days <= 5]
    elif win_range(mode):                    # a uniform window: one expiration week, 2-9 months or LEAPS
        exps = _in_win(exps, mode)
    else:                                    # every listed expiration in the window (up to 5 for daily-expiry tickers)
        lo_d, hi_d = SCAN_WINDOWS.get(mode) or SPREAD_WINDOWS[mode]
        exps = [e for e in exps if lo_d <= (pd.Timestamp(e) - today).days <= hi_d]
        if len(exps) > 5:
            exps = [exps[round(i * (len(exps) - 1) / 4)] for i in range(5)]
    out = []
    min_ret = return_floors()[0]                         # hard floor: credit / max loss
    targets = (0.0075, 0.015, 0.025, 0.04, 0.06)        # widths as % of spot
    for e in exps:
        sub = df[df["expiration"] == e]
        a = analyze_exp(spot, sub, band)
        dte = int(sub["dte"].iloc[0])
        t = max(dte, 1) / 365
        if mode == "0dte":
            dte, t = 0, max(mins_left, 15) / (365 * 1440)   # time left today, not a full day
        elif mode == "d5" or win_range(mode):
            dte = int((pd.Timestamp(e) - today).days)
            t = max(dte, 1) / 365
        p = sub[(sub["type"] == "P") & (sub["strike"] < spot)]
        p = p[~p["strike"].duplicated()].set_index("strike").sort_index()
        ks = list(p.index)
        atm = sub[(sub["type"] == "P") & (sub["iv"] > 0)]
        atm_iv = float(atm.iloc[(atm["strike"] - spot).abs().argmin()]["iv"]) if not atm.empty else None
        em = spot * atm_iv * math.sqrt(t) if atm_iv else None      # 1-sigma expected move to expiration
        min_s, min_l = (10, 5) if relaxed else (min_oi or ((50, 20) if mode == "weekly" else (100, 50)))
        d_lo, d_hi = ((-0.45, -0.005) if short_mode else (-0.50, -0.04)) if relaxed else \
            ((-0.30, -0.01) if short_mode else (-0.40, -0.07))
        for i, k in enumerate(ks):
            s = p.loc[k]
            if s.bid <= 0 or s.iv <= 0 or s.open_interest < min_s or not (d_lo <= s.delta <= d_hi):
                continue
            longs = ks[:i]
            if not longs:
                continue
            picks = {min(longs, key=lambda x: abs((k - x) - spot * tw)) for tw in targets}
            for kl in picks:
                w, l = k - kl, p.loc[kl]
                if w <= 0 or w > spot * (0.10 if relaxed else 0.07) or l.ask <= 0 or l.open_interest < min_l:
                    continue
                mid0 = (s.bid + s.ask) / 2 - (l.bid + l.ask) / 2
                nat = s.bid - l.ask
                if mid0 <= 0 or (mid0 - nat) / mid0 > (0.90 if relaxed else 0.60):   # wide after hours; ranked down below
                    continue
                mid = (mid0 + max(nat, 0.0)) / 2             # realistic fill: halfway between mid and natural
                if mid <= 0 or mid >= w or mid / (w - mid) < min_ret - 1e-9 \
                        or mid / w < (min(min_cw, 0.03) if relaxed else min_cw):
                    continue
                be = k - mid
                iv_s = float(s.iv)
                if mode == "0dte":                   # re-derive IV from the short put's own mid over the minutes left
                    iv_s = _put_iv((s.bid + s.ask) / 2, spot, k, t) or iv_s
                d2 = (math.log(spot / be) + (0.04 - iv_s ** 2 / 2) * t) / (iv_s * math.sqrt(t))
                pop = NormalDist().cdf(d2) * 100
                if pop < ((60 if short_mode else 40) if relaxed else (80 if short_mode else 55)):
                    continue                     # strict: short-dated modes 80%+ POP only
                out.append({"exp": pd.Timestamp(e).strftime("%Y-%m-%d"), "dte": dte, "short": float(k), "long": float(kl),
                            "width": float(w), "credit": float(mid), "nat": float(nat), "mid": float(mid0),
                            "q": [float(s.bid), float(s.ask), float(l.bid), float(l.ask)], "maxloss": float((w - mid) * 100),
                            "cw": float(mid / w), "pop": float(pop), "be": float(be), "sdelta": float(s.delta),
                            "s_oi": int(s.open_interest), "l_oi": int(l.open_interest), "slip": float((mid0 - nat) / mid0 * 100),
                            "otm": float((k / spot - 1) * 100), "put_wall": a["put_wall"], "net_gex": a["net_gex"],
                            "below_wall": bool(a["put_wall"] is not None and k <= a["put_wall"]),
                            "em": em, "em_mult": float((spot - k) / em) if em else None,
                            "s_iv": float(s.iv), "l_iv": float(l.iv) if l.iv > 0 else float(s.iv), "atm_iv": atm_iv,
                            "score": float(pop * mid / w)})
    out.sort(key=lambda x: -x["score"])
    if not limit:
        return out
    if relaxed:      # keep the high-POP ones first so low-POP / high-credit spreads don't crowd them out
        floor = 80 if short_mode else 60
        hi_ = [x for x in out if x["pop"] >= floor]
        keep = hi_[:100] + [x for x in out if x["pop"] < floor][:40]
        return keep
    keep, seen = out[:80], set()
    for x in out:                                         # keep low-risk choices too, for the risk filter
        if x["maxloss"] <= 600 and len(keep) < 120:
            key = (x["exp"], x["short"], x["long"])
            if key not in seen and x not in keep:
                keep.append(x)
                seen.add(key)
    return keep


# ---- odds of reaching 50% of max profit, and how fast (Monte Carlo) ----
P50_PATHS = 1500


def _ncdf(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF (Zelen & Severo, |error| < 7.5e-8), vectorized without scipy."""
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.2316419 * a)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    c = 1.0 - np.exp(-0.5 * a * a) / math.sqrt(2 * math.pi) * poly
    return np.where(x >= 0, c, 1.0 - c)


def _bs_put(S: np.ndarray, K: float, tau: float, iv: float, r: float = 0.04) -> np.ndarray:
    if tau <= 1e-9:
        return np.maximum(K - S, 0.0)
    sd = iv * math.sqrt(tau)
    d1 = (np.log(S / K) + (r + 0.5 * iv * iv) * tau) / sd
    return K * math.exp(-r * tau) * _ncdf(-(d1 - sd)) - S * _ncdf(-d1)


def _tday(today: date, n: float, exp: str) -> str:
    """today + n calendar days, rolled forward to a weekday, never past expiration."""
    d = today + timedelta(days=int(round(n)))
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return min(d.isoformat(), exp)


def add_p50(spot: float, cands: list, seed_key: str = "") -> None:
    """For each put credit spread: probability its value falls to 50% of the entry credit at some daily close
    before expiration (p50, %) and the median calendar days until it does (d50). Simulates daily price paths at
    the expiration's ATM implied vol and re-prices each leg at its own IV (IV held constant, no early assignment)."""
    by_exp: dict = {}
    for c in cands:
        if c.get("dte") and c.get("s_iv") and c.get("atm_iv"):
            by_exp.setdefault((c["exp"], c["dte"], c["atm_iv"]), []).append(c)
    today = et_now().date()
    for (exp, dte, sig), group in by_exp.items():
        days = max(int(dte), 1)
        rng = np.random.default_rng(zlib.crc32(f"{seed_key}|{exp}".encode()))
        dt = 1 / 365
        z = rng.standard_normal((P50_PATHS // 2, days))
        z = np.vstack([z, -z])                                  # antithetic paths: lower noise
        paths = spot * np.exp(np.cumsum((0.04 - 0.5 * sig * sig) * dt + sig * math.sqrt(dt) * z, axis=1))
        taus = [(days - d) / 365 for d in range(1, days + 1)]
        tv = np.array(taus)
        live_t = tv > 1e-9                                      # the last simulated day is expiration (intrinsic)
        tsafe = np.where(live_t, tv, 1.0)
        logp, sq_t, disc_t = np.log(paths), np.sqrt(tsafe), np.exp(-0.04 * tsafe)

        def put_all(logS, S, K, iv):                            # every path x every day in one pass (same maths as _bs_put)
            sd = iv * sq_t
            d1 = (logS - math.log(K) + (0.04 + 0.5 * iv * iv) * tsafe) / sd
            return np.where(live_t, K * disc_t * _ncdf(-(d1 - sd)) - S * _ncdf(-d1), np.maximum(K - S, 0.0))
        spot_row, logspot = np.full(days, float(spot)), math.log(spot)
        memo: dict = {}                                         # (strike, iv) -> leg values on every path: the same short

        def leg(K, iv):                                         # strike sits in several spreads and in the put list
            k = (K, iv)
            if k not in memo:
                if len(memo) >= 10:
                    memo.clear()                                # bounded (each grid is paths x days floats)
                memo[k] = put_all(logp, paths, K, iv)
            return memo[k]
        for c in group:
            v0 = float(_bs_put(np.array([spot]), c["short"], days / 365, c["s_iv"])[0]
                       - (0 if c["long"] is None else _bs_put(np.array([spot]), c["long"], days / 365, c["l_iv"])[0]))
            if v0 <= 0:
                continue
            target = v0 * c.get("target_ratio", 0.5)
            v = leg(c["short"], c["s_iv"])
            if c["long"] is not None:
                v = v - leg(c["long"], c["l_iv"])
            ok = v <= target
            hit_day = np.where(ok.any(axis=1), ok.argmax(axis=1) + 1, -1)       # first day at or under the target
            hits = hit_day[hit_day > 0]
            c["p50"] = float(len(hits) / len(hit_day) * 100)
            c["d50"] = float(np.median(hits)) if len(hits) else None
            # dates: simulated days are calendar days from today; a weekend hit shows on the next trading day
            if len(hits):
                lo, hi = np.percentile(hits, [25, 75])
                c["d50_date"], c["d50_lo_date"], c["d50_hi_date"] = (_tday(today, x, c["exp"]) for x in (c["d50"], lo, hi))
            # if the stock just sits at today's price: the day time decay alone gets it to 50%
            vf = put_all(logspot, spot_row, c["short"], c["s_iv"])
            if c["long"] is not None:
                vf = vf - put_all(logspot, spot_row, c["long"], c["l_iv"])
            okf = vf <= target
            flat = int(okf.argmax()) + 1 if okf.any() else None
            c["flat_days"] = flat
            c["flat_date"] = _tday(today, flat, c["exp"]) if flat else None
            c["p50_fast"] = float(c["p50"] / 100 * 30 / max(c["d50"], 1)) if c["d50"] else 0.0


# Put credit spread risk profiles for the ticker lookup.
SPREAD_PROFILES = {
    "conservative": {"pop": (78, 101), "delta": 0.20, "slip": 40, "earnings": False},   # 15%+ return caps POP near 87%
    "balanced": {"pop": (66, 78), "delta": 0.30, "slip": 50, "earnings": False},
    "aggressive": {"pop": (55, 68), "delta": 0.40, "slip": 60, "earnings": True},
}


# 0 DTE / 1-5 DTE: 80%+ POP only, split into three bands
SHORT_PROFILES = {
    "conservative": {"pop": (84, 101), "delta": 0.20, "slip": 40, "earnings": False},
    "balanced": {"pop": (80, 84), "delta": 0.25, "slip": 50, "earnings": False},
    "aggressive": {"pop": (72, 80), "delta": 0.30, "slip": 60, "earnings": False},
}


def _profile_rank(name: str, c: dict) -> float:
    pen = 1 - min(c["slip"], 60) / 300                     # wide bid/ask costs you on entry and exit
    pen *= min(1.0, (min(c["s_oi"], 2 * c["l_oi"]) / 500) ** 0.25)   # thin open interest: harder fills and exits
    if name == "conservative":                             # safety first: strongly prefer support under the short
        return c["pop"] * c["cw"] * (1.15 if c["below_wall"] else 0.9) * pen / (1 + c["width"] / c["short"] * 4)
    if name == "balanced":                                 # premium x probability, same score as the scanner
        return c["pop"] * c["cw"] * (1.05 if c["below_wall"] else 1.0) * pen
    return c["cw"] * 100 * math.sqrt(c["pop"] / 100) * pen  # aggressive: premium first


def _pcs_strict(c: dict, win: str) -> bool:
    """The original (strict) candidate gates, before widening."""
    short_mode = is_short_win(win)
    return (c["s_oi"] >= 50 and c["l_oi"] >= 20 and c["cw"] >= 0.05 and c["slip"] <= 60
            and c["pop"] >= (80 if short_mode else 55)
            and ((-0.30 <= c["sdelta"] <= -0.01) if short_mode else (-0.40 <= c["sdelta"] <= -0.07)))


def _pcs_issues(c: dict, win: str, earn_date: str | None, P: dict | None = None, pname: str = "") -> list:
    """Plain-English reasons a put credit spread is not ideal (vs a risk profile, or in general)."""
    out = []
    short_mode = is_short_win(win)
    if P:
        lo, hi = P["pop"]
        if c["pop"] < lo:
            out.append(f"POP {c['pop']:.0f}% is below the {pname} minimum of {lo}%")
        elif c["pop"] >= hi and hi <= 100:
            out.append(f"POP {c['pop']:.0f}% is above the {pname} band ({lo}–{hi}%) — safer, but less premium than this profile aims for")
        if -c["sdelta"] > P["delta"]:
            out.append(f"short put delta {c['sdelta']:.2f} is past the {pname} limit of -{P['delta']:.2f} (closer to the money)")
        if c["slip"] > P["slip"]:
            out.append(f"wide quotes: mid {c['credit']:.2f} vs natural {c['nat']:.2f} — a fill near mid is uncertain")
    else:
        if c["pop"] < (80 if short_mode else 55):
            out.append(f"POP {c['pop']:.0f}% is below the usual {80 if short_mode else 55}% minimum for this window")
        if c["slip"] > 50:
            out.append(f"wide quotes: mid {c['credit']:.2f} vs natural {c['nat']:.2f}")
    if earn_date and earn_date <= c["exp"]:
        out.append(f"earnings {earn_date} before expiration — a gap can jump both strikes")
    if c["s_oi"] < 50 or c["l_oi"] < 20:
        out.append(f"thin open interest ({c['s_oi']:,} short / {c['l_oi']:,} long) — harder to fill and exit")
    if c["cw"] < 0.10:
        out.append(f"credit is only {c['cw'] * 100:.0f}% of the width — small payoff for the risk")
    roc = c["credit"] / (c["width"] - c["credit"]) * 100 if c["width"] > c["credit"] else None
    if roc is not None and roc < 20:
        out.append(f"return on risk only {roc:.0f}% (credit ÷ max loss)")
    if c.get("put_wall") is not None and not c["below_wall"]:
        out.append(f"short strike is above the put wall ({c['put_wall']:g}) — no dealer support under it")
    return out


def _pcs_why(c: dict, n: int, spot: float, fib: dict | None) -> str:
    good = [f"POP {c['pop']:.0f}%", f"credit {c['cw'] * 100:.0f}% of width"]
    if c["below_wall"]:
        good.append("short strike at/below the put wall")
    if any(c["short"] <= lv["price"] < spot for lv in (fib or {}).get("levels", []) if 0 < lv["r"] < 1):
        good.append("Fibonacci support above the short strike")
    if c["slip"] <= 25:
        good.append("tight quotes")
    if c["s_oi"] >= 500:
        good.append("good open interest")
    return f"Best available of {n} spreads in this window: " + ", ".join(good) + "."


def spread_strategies(spot: float, df: pd.DataFrame, earn_date: str | None, earn_known: bool, band: float = 0.15,
                      fib: dict | None = None) -> dict:
    """Best put credit spreads for one ticker: 3 risk profiles x 3 expiration windows, top pick + 2 alternates.
    When a profile has nothing that meets all its rules, the closest spreads are returned as `nearest` with reasons."""
    wins = {}
    for win in week_keys():
        pool = _spread_candidates(spot, df, win, min_cw=0.03, limit=False, band=band, relaxed=True)
        cands = [c for c in pool if _pcs_strict(c, win)]
        prof, nearest = {}, {}
        profs = SHORT_PROFILES if is_short_win(win) else SPREAD_PROFILES
        for name, P in profs.items():
            lo, hi = P["pop"]
            ok = [c for c in cands if lo <= c["pop"] < hi and -c["sdelta"] <= P["delta"] and c["slip"] <= P["slip"]
                  and (P["earnings"] or not earn_date or earn_date > c["exp"])]
            ok.sort(key=lambda c: -_profile_rank(name, c))
            picks, seen = [], set()
            for c in ok:
                key = (c["exp"], c["short"])
                if key in seen:
                    continue
                seen.add(key)
                fsup = [lv for lv in (fib or {}).get("levels", []) if 0 < lv["r"] < 1 and c["short"] <= lv["price"] < spot]
                picks.append({**c, "rank": _profile_rank(name, c), "earn_cross": bool(earn_date and earn_date <= c["exp"]),
                              "fib_support": fsup,
                              "tp30": c["credit"] * 0.70, "tp50": c["credit"] * 0.50,
                              "roc": c["credit"] / (c["width"] - c["credit"]) * 100 if c["width"] > c["credit"] else None})
                if len(picks) == 3:
                    break
            prof[name] = picks
            if not picks and pool:          # nothing meets every rule: closest spreads, with what is off and why
                scored = []
                for c in pool:
                    iss = _pcs_issues(c, win, earn_date, P, name.capitalize())
                    scored.append((_profile_rank(name, c) * (0.8 ** len(iss)), c, iss))
                scored.sort(key=lambda x: -x[0])
                near, seen = [], set()
                for _, c, iss in scored:
                    if (c["exp"], c["short"]) in seen:
                        continue
                    seen.add((c["exp"], c["short"]))
                    near.append({**c, "rank": _profile_rank(name, c), "issues": iss, "fallback": True,
                                 "why": _pcs_why(c, len(pool), spot, fib), "earn_cross": bool(earn_date and earn_date <= c["exp"]),
                                 "fib_support": [lv for lv in (fib or {}).get("levels", []) if 0 < lv["r"] < 1 and c["short"] <= lv["price"] < spot],
                                 "tp30": c["credit"] * 0.70, "tp50": c["credit"] * 0.50,
                                 "roc": c["credit"] / (c["width"] - c["credit"]) * 100 if c["width"] > c["credit"] else None})
                    if len(near) == 3:
                        break
                nearest[name] = near
        # best available in the window regardless of profile (balanced score, penalized per issue)
        best = []
        if pool:
            sc = sorted(((c["score"] * (1 - min(c["slip"], 90) / 300) * (0.85 ** len(_pcs_issues(c, win, earn_date))), c)
                         for c in pool), key=lambda x: -x[0])
            seen = set()
            for _, c in sc:
                if (c["exp"], c["short"]) in seen:
                    continue
                seen.add((c["exp"], c["short"]))
                best.append({**c, "issues": _pcs_issues(c, win, earn_date), "why": _pcs_why(c, len(pool), spot, fib),
                             "earn_cross": bool(earn_date and earn_date <= c["exp"]),
                             "roc": c["credit"] / (c["width"] - c["credit"]) * 100 if c["width"] > c["credit"] else None})
                if len(best) == 5:
                    break
        if win != "0dte":                   # 50% odds/dates need at least a day of decay
            add_p50(spot, [c for P in prof.values() for c in P] + [c for P in nearest.values() for c in P] + best, win)
        listed = [pd.Timestamp(e).strftime("%Y-%m-%d") for e in _in_win(sorted(df["expiration"].unique()), win, cap=99)] \
            if win_range(win) else []
        quoted = [e for e in listed if ((df["expiration"] == pd.Timestamp(e)) & (df["type"] == "P") & (df["bid"] > 0)).any()]
        wins[win] = {"profiles": prof, "nearest": nearest, "best": best, "n": len(cands), "n_all": len(pool),
                     "exps": sorted({c["exp"] for c in pool}), "listed": listed, "quoted": quoted}
    return {"windows": wins, "earnings": earn_date, "earnings_known": earn_known}


# ---------------------------------------------------------------- my trades (put credit spreads)
TRADES_PATH = APP_DIR / "trades.json"
def chain_cached(ticker: str, ttl: float = 90):
    """Shared chain no older than ttl seconds (longer outside market hours, when quotes do not move). Read-only df."""
    return _chain_shared(ticker, ttl if _session_status() == "Market Open" else max(ttl, chain_ttl()))


def load_trades(open_only: bool = False) -> list:
    t = _read_json(TRADES_PATH, [])
    t = t if isinstance(t, list) else []
    return [x for x in t if x.get("status") != "closed"] if open_only else t


LEG_RX = re.compile(r"([+\u2212-])\s*(\d+(?:\.\d+)?)\s*([PC])\b")


def parse_legs(txt: str) -> list:
    """'−666P / +665P' or '+100 sh + −30C' -> [{"cp","K","side","n"}] (side +1 long / −1 short; STK = 1 share)."""
    txt = str(txt or "")
    legs = [{"cp": cp, "K": float(k), "side": -1 if sg in "\u2212-" else 1, "n": 1} for sg, k, cp in LEG_RX.findall(txt)]
    if re.search(r"\b100\s*sh", txt) or re.search(r"\bshares?\b", txt, re.I):
        legs.insert(0, {"cp": "STK", "K": None, "side": 1, "n": 1})
    return legs


def _legs_value(legs: list, S: float, quotes: dict | None = None) -> float | None:
    """Per-share value of a position (long legs +, short legs −). quotes {(cp,K): mid}; None = intrinsic at S."""
    v = 0.0
    for l in legs:
        if l["cp"] == "STK":
            px_ = S
        elif quotes is None:
            px_ = max(S - l["K"], 0.0) if l["cp"] == "C" else max(l["K"] - S, 0.0)
        else:
            px_ = quotes.get((l["cp"], float(l["K"])))
            if px_ is None:
                return None
        v += l["side"] * l.get("n", 1) * px_
    return v


def _trade_legs(t: dict) -> list:
    if t.get("legs"):
        return t["legs"]
    return [{"cp": "P", "K": t["short"], "side": -1, "n": 1}, {"cp": "P", "K": t["long"], "side": 1, "n": 1}]


def _trade_open_value(t: dict) -> float:
    """Signed per-share value at entry: a credit received is negative, a debit paid is positive."""
    if t.get("open_value") is not None:
        return float(t["open_value"])
    return -float(t["credit"])


def _trade_mult(t: dict) -> int:
    return 1 if all(l["cp"] == "STK" for l in _trade_legs(t)) else 100


def _settle_expired(t: dict) -> dict | None:
    """Close an open trade whose expiration has passed at that day's official close (intrinsic value)."""
    try:
        h = daily_history(t["ticker"])
    except Exception:
        return None
    rows = h[h["date"] <= t["exp"]]
    if rows.empty:
        return None
    S = float(rows["close"].iloc[-1])
    v = _legs_value(_trade_legs(t), S)
    return {"close_value": v, "close_date": t["exp"], "settle_price": S, "closed_how": "expired"}


def _finish(t: dict, close_value: float) -> dict:
    pnl = (close_value - _trade_open_value(t)) * _trade_mult(t) * t.get("qty", 1)
    risk = t.get("capital") or (abs(_trade_open_value(t)) * _trade_mult(t) * t.get("qty", 1)) or None
    return {**t, "status": "closed", "close_value": close_value, "pnl": pnl, "win": bool(pnl > 0),
            "pnl_pct": pnl / risk * 100 if risk else None}


_trades_lock = threading.RLock()


def save_trades(body: dict) -> list:
    with _trades_lock:
        out = _save_trades(body)
    _tr_kick(force=True)                           # re-value in the background; the reply does not wait for it
    return out


def _save_trades(body: dict) -> list:
    trades = load_trades()
    roll = body.get("roll") if isinstance(body.get("roll"), dict) else None
    if roll:                                       # roll = buy back the old trade + open the new one, linked
        old = next((t for t in trades if t.get("id") == roll.get("id")), None)
        if not old:
            raise ValueError("trade to roll not found")
        try:
            if not 0 <= float(roll.get("close_price")) <= 100000:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("enter what you paid to buy back the old trade (per share, 0 or more)") from None
        if old.get("status") == "closed":          # e.g. it was auto-settled at expiration: replace that with the real buy-back
            _strip = ("status", "close_value", "pnl", "win", "pnl_pct", "close_date", "close_price", "closed_how",
                      "settle_price", "rolled_to", "rolled_to_txt")
            trades = [{k: v for k, v in t.items() if k not in _strip} if t is old else t for t in trades]
            old = next(t for t in trades if t.get("id") == roll.get("id"))
        if roll.get("link"):
            if not any(t.get("id") == roll["link"] and t.get("status") != "closed" and t.get("id") != old["id"] for t in trades):
                raise ValueError("the new trade to link was not found in your open trades")
        else:
            n = roll.get("new") or {}
            body["add_legs"] = {**n, "ticker": old["ticker"], "kind": old.get("kind") or "credit",
                                "strat": old.get("strat") or "", "src": ("Rolled · " + str(old.get("src") or ""))[:80],
                                "entry_date": roll.get("date"), "_force": True}
        body["close"] = {"id": old["id"], "price": roll.get("close_price"), "date": roll.get("date")}
    if body.get("remove"):
        trades = [t for t in trades if t.get("id") != body["remove"]]
    if body.get("close"):                          # you closed it: price paid to buy back a credit trade / received for a debit
        c = body["close"]
        cd = str(c.get("date") or et_now().date().isoformat())
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cd):
            raise ValueError("close date must be YYYY-MM-DD")
        price = float(c["price"])
        if price < 0 or price > 100000:
            raise ValueError("close price must be 0 or more (per share, like the fill price)")
        for i, t in enumerate(trades):
            if t.get("id") == c.get("id") and t.get("status") != "closed":
                cv = -price if _trade_open_value(t) < 0 else price
                trades[i] = {**_finish(t, cv), "close_date": cd, "close_price": price, "closed_how": "closed"}
    if body.get("reopen"):
        for i, t in enumerate(trades):
            if t.get("id") == body["reopen"]:
                trades[i] = {k: v for k, v in t.items() if k not in ("status", "close_value", "pnl", "win", "pnl_pct",
                                                                     "close_date", "close_price", "closed_how", "settle_price",
                                                                     "rolled_to", "rolled_to_txt")}
    if body.get("add_legs"):                       # any recommended trade: legs + fill price
        a = body["add_legs"]
        tk = clean_ticker(str(a.get("ticker", "")))
        exp = str(a.get("exp") or "")
        legs = a.get("legs") if isinstance(a.get("legs"), list) else parse_legs(a.get("legs_text", ""))
        legs = [{"cp": str(l["cp"]).upper()[:3], "K": None if l.get("K") in (None, "") else float(l["K"]),
                 "side": 1 if int(l.get("side", 1)) > 0 else -1, "n": max(1, int(l.get("n", 1)))} for l in legs][:8]
        if not legs or any(l["cp"] not in ("P", "C", "STK") or (l["cp"] != "STK" and not l["K"]) for l in legs):
            raise ValueError("could not read the legs of this trade")
        if any(l["cp"] != "STK" for l in legs) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
            raise ValueError("expiration must be YYYY-MM-DD")
        price = float(a["price"])
        kind = "credit" if a.get("kind") == "credit" else "debit"
        qty = int(a.get("qty") or 1)
        if not (price > 0) or not (1 <= qty <= 100000):
            raise ValueError("enter the fill price (per share) and how many contracts / shares")
        entry = str(a.get("entry_date") or et_now().date().isoformat())
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry):
            raise ValueError("entry date must be YYYY-MM-DD")
        for o in trades:                           # double-click / repeated Save: same trade within 10 minutes
            if not a.get("_force") and o.get("status") != "closed" and o.get("ticker") == tk and (o.get("exp") or "") == exp \
                    and o.get("legs") == legs and abs(float(o.get("fill") or 0) - price) < 1e-9 and o.get("qty") == qty \
                    and time.time() - float(o.get("added_at") or 0) < 600:
                return trades
        t = {"id": secrets.token_hex(4), "ticker": tk, "exp": exp or None, "strat": str(a.get("strat") or "")[:12],
             "legs": legs, "kind": kind, "open_value": -price if kind == "credit" else price, "fill": price, "qty": qty,
             "entry_date": entry, "src": str(a.get("src") or "")[:80], "rec_p": a.get("rec_p"),
             "capital": float(a["capital"]) * qty if a.get("capital") not in (None, "") else None,
             "note": str(a.get("note", ""))[:120], "added_at": time.time()}
        try:
            S0 = nasdaq_quote(tk)
        except Exception:
            S0 = None
        t["entry_spot"] = S0["price"] if S0 and entry == et_now().date().isoformat() else None
        trades.append(t)
    if body.get("add"):
        a = body["add"]
        tk = clean_ticker(str(a.get("ticker", "")))
        exp = str(a.get("exp", ""))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
            raise ValueError("expiration must be YYYY-MM-DD")
        short, long_, credit = float(a["short"]), float(a["long"]), float(a["credit"])
        qty = int(a.get("qty") or 1)
        if not (0 < long_ < short) or not (0 < credit < short - long_) or not (1 <= qty <= 1000):
            raise ValueError("check strikes, credit (less than the width) and contracts")
        entry = str(a.get("entry_date") or et_now().date().isoformat())
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry):
            raise ValueError("entry date must be YYYY-MM-DD")
        es = a.get("entry_spot")
        if es in (None, ""):                       # remember the stock price at entry (for the on-schedule check)
            q = nasdaq_quote(tk)
            es = q["price"] if q and entry == et_now().date().isoformat() else None
        trades.append({"id": secrets.token_hex(4), "ticker": tk, "exp": exp, "short": short, "long": long_,
                       "qty": qty, "credit": credit, "entry_date": entry, "strat": "PCS",
                       "entry_spot": float(es) if es not in (None, "") else None,
                       "capital": (short - long_ - credit) * 100 * qty, "src": str(a.get("src") or "")[:80],
                       "rec_p": a.get("rec_p"), "note": str(a.get("note", ""))[:120]})
    if roll:
        new_id = roll.get("link") or (trades[-1]["id"] if body.get("add_legs") else None)
        nt = next((t for t in trades if t.get("id") == new_id), None)
        for i, t in enumerate(trades):
            if t.get("id") == roll.get("id"):
                trades[i] = {**t, "closed_how": "rolled", "rolled_to": new_id,
                             "rolled_to_txt": (f"{nt.get('exp') or ''} " + " / ".join(
                                 f"{'-' if l['side'] < 0 else '+'}{l['K']:g}{l['cp']}" for l in nt.get("legs") or [])) if nt else ""}
            elif t.get("id") == new_id:
                ids = list(dict.fromkeys((t.get("rolled_from") or []) + [roll["id"]]))
                trades[i] = {**t, "rolled_from": ids}
    if len(trades) > 2000:
        raise ValueError("too many trades")
    with _io_lock:
        _write_json(TRADES_PATH, trades)
    return trades


def _spread_val(S, t: dict, tau: float, s_iv: float, l_iv: float) -> float:
    return float(_bs_put(np.array([S]), t["short"], tau, s_iv)[0] - _bs_put(np.array([S]), t["long"], tau, l_iv)[0])


def trade_status(t: dict) -> dict:
    """Live state of one put credit spread: P/L vs entry, progress to 50%, schedule, risk signal, exits and rolls."""
    out = {**t}
    spot_c, df, meta = _trade_chain(t)
    q = nasdaq_quote(t["ticker"])
    px = q["price"] if q else spot_c
    exp = pd.Timestamp(t["exp"])
    sub = df[(df["expiration"] == exp) & (df["type"] == "P")]
    legs = {k: sub[sub["strike"] == k] for k in (t["short"], t["long"])}
    if any(v.empty for v in legs.values()):
        out["error"] = "these strikes / expiration are not in CBOE's chain (expired or mistyped?)"
        return out
    s, l = legs[t["short"]].iloc[0], legs[t["long"]].iloc[0]
    credit, w, n = t["credit"], t["short"] - t["long"], t["qty"]
    mid = max(0.0, (s.bid + s.ask) / 2 - (l.bid + l.ask) / 2)
    nat = max(0.0, s.ask - l.bid)                              # what buying it back costs right now
    dte = max(int((exp - pd.Timestamp(et_today())).days), 0)
    s_iv = float(s.iv) if s.iv > 0 else 0.5
    l_iv = float(l.iv) if l.iv > 0 else s_iv
    prof = (credit - mid) / credit * 100
    out.update(price=px, chain_spot=spot_c, quotes_as_of=meta["last_trade"], source=meta.get("source"), dte=dte, mid=mid, close_nat=nat,
               profit_pct=prof, pl=(credit - mid) * 100 * n, pl_nat=(credit - nat) * 100 * n,
               max_profit=credit * 100 * n, max_loss=(w - credit) * 100 * n,
               tp30=credit * 0.70, tp50=credit * 0.50, stop=min(credit * 2, w), be=t["short"] - credit,
               cushion=(px / t["short"] - 1) * 100, sdelta=float(s.delta), s_iv=s_iv)
    a = analyze_exp(spot_c, df[df["expiration"] == exp], 0.15)
    out.update(put_wall=a["put_wall"], net_gex=a["net_gex"])
    tau = max(dte, 0) / 365
    if tau > 0:
        d2 = (math.log(px / out["be"]) + (0.04 - s_iv ** 2 / 2) * tau) / (s_iv * math.sqrt(tau))
        out["pop_now"] = NormalDist().cdf(d2) * 100
    # on schedule? profit time decay alone would have earned by now if the stock sat at the entry price
    try:
        ed = date.fromisoformat(t["entry_date"])
        dte0 = max((exp.date() - ed).days, 1)
        out["days_in"], out["dte0"] = (et_today() - ed).days, dte0
        if t.get("entry_spot") and tau > 0:
            v0 = _spread_val(t["entry_spot"], t, dte0 / 365, s_iv, l_iv)
            vn = _spread_val(t["entry_spot"], t, tau, s_iv, l_iv)
            out["sched_pct"] = (1 - vn / v0) * 100 if v0 > 0 else None
    except ValueError:
        pass
    # odds and date of reaching the 50% target from here
    if mid > credit * 0.5 and dte > 0:
        c = {"exp": t["exp"], "dte": dte, "short": t["short"], "long": t["long"], "s_iv": s_iv, "l_iv": l_iv,
             "atm_iv": s_iv, "target_ratio": credit * 0.5 / mid if mid > 0 else 0.5}
        add_p50(px, [c], t["id"])
        out.update({k: c.get(k) for k in ("p50", "d50", "d50_date", "d50_lo_date", "d50_hi_date", "flat_date")})
    # signal + plan
    acts, sig = [], "ok"
    behind = out.get("sched_pct") is not None and prof < out["sched_pct"] - 15
    if prof >= 50:
        sig = "target"
        acts.append(f"50% reached — buy to close at {credit * 0.5:.2f} or better (mid {mid:.2f}). Locks about +${(credit - mid) * 100 * n:,.0f}.")
    elif px < t["short"] or s.delta <= -0.45 or mid >= credit * 2:
        sig = "danger"
        acts.append(f"Short {t['short']:g} put is in trouble (price {px:,.2f}, short delta {s.delta:.2f}). "
                    f"Close now costs about {nat:.2f} (loss ${(nat - credit) * 100 * n:,.0f}), or roll down and out for a credit (below).")
        acts.append(f"Stop guide: close if the spread trades at {min(credit * 2, w):.2f}+ (loss about 1× the credit).")
    elif s.delta <= -0.30 or out["cushion"] < 3 or (a["put_wall"] and px < a["put_wall"]) or behind or (dte <= 21 and prof < 30):
        sig = "watch"
        if out["cushion"] < 3:
            acts.append(f"Price is only {out['cushion']:.1f}% above your short strike.")
        if a["put_wall"] and px < a["put_wall"]:
            acts.append(f"Price is below this expiration's put wall ({a['put_wall']:g}) — support is gone.")
        if behind:
            acts.append(f"Behind schedule: {prof:.0f}% profit vs {out['sched_pct']:.0f}% expected from time decay alone.")
        if dte <= 21 and prof < 30:
            acts.append(f"{dte} DTE left with {prof:.0f}% profit — gamma risk rises from here; consider closing or rolling out.")
    else:
        if out.get("sched_pct") is not None and prof > out["sched_pct"] + 5:
            acts.append(f"Ahead of schedule: {prof:.0f}% profit vs {out['sched_pct']:.0f}% from time decay alone.")
        if 30 <= prof < 50 and dte <= 21:
            acts.append(f"{prof:.0f}% profit with {dte} DTE left — taking it here is reasonable.")
    if sig != "target":
        acts.append(f"Profit orders: GTC buy-to-close at {credit * 0.5:.2f} (50%) or {credit * 0.7:.2f} (30%).")
    out["signal"], out["actions"] = sig, acts
    # roll ideas: same width, later expiration, short strike at or below today's, opened for a net credit
    if sig in ("danger", "watch") or dte <= 21:
        out["rolls"] = _roll_ideas(t, df, px, nat, mid, dte)
    try:
        out["verdict"] = trade_verdict(t, out, df, px)
    except Exception:
        pass
    return out


def _roll_ideas(t: dict, df: pd.DataFrame, px: float, close_nat: float, close_mid: float, dte: int) -> list:
    w = t["short"] - t["long"]
    today = pd.Timestamp(et_today())
    exps = [e for e in sorted(df["expiration"].unique())
            if max(dte + 5, 21) <= (pd.Timestamp(e) - today).days <= dte + 60 and pd.Timestamp(e) > pd.Timestamp(t["exp"])]
    ideas = []
    for e in exps:
        p = df[(df["expiration"] == e) & (df["type"] == "P")]
        p = p[~p["strike"].duplicated()].set_index("strike")
        nd = int((pd.Timestamp(e) - today).days)
        a = analyze_exp(px, df[df["expiration"] == e], 0.15)
        for k in p.index:
            if k > t["short"] or (k - w) not in p.index:
                continue
            sl, ll = p.loc[k], p.loc[k - w]
            if sl.bid <= 0 or ll.ask <= 0 or sl.open_interest < 20:
                continue
            new_nat = sl.bid - ll.ask
            new_mid = (sl.bid + sl.ask) / 2 - (ll.bid + ll.ask) / 2
            net = new_mid - close_mid                          # roll as one order, priced near mid
            if net < 0:
                continue
            iv = float(sl.iv) if sl.iv > 0 else 0.5
            be = k - (t["credit"] + net)                       # total credit kept after the roll
            d2 = (math.log(px / be) + (0.04 - iv ** 2 / 2) * nd / 365) / (iv * math.sqrt(nd / 365)) if be > 0 else 9
            ideas.append({"exp": pd.Timestamp(e).strftime("%Y-%m-%d"), "dte": nd, "short": float(k), "long": float(k - w),
                          "new_credit_nat": float(new_nat), "new_mid": float(new_mid), "net": float(net),
                          "net_nat": float(new_nat - close_nat), "be": float(be),
                          "pop": NormalDist().cdf(d2) * 100, "sdelta": float(sl.delta),
                          "below_wall": bool(a["put_wall"] and k <= a["put_wall"]), "put_wall": a["put_wall"]})
    if not ideas:
        return []
    picks = []
    lowest = min(ideas, key=lambda x: (x["short"], -x["net"]))                     # most cushion for a credit
    picks.append({**lowest, "why": "Most cushion: lowest short strike that still rolls for a credit"})
    same = [x for x in ideas if x["short"] == t["short"]]
    if same:
        o = min(same, key=lambda x: x["dte"])
        picks.append({**o, "why": "Roll out only: same strikes, nearest later expiration for a credit"})
    best = max(ideas, key=lambda x: x["pop"] * (1 + x["net"] / max(w, 0.01)))
    picks.append({**best, "why": "Balanced: best probability for the credit"})
    seen, out = set(), []
    for x in picks:
        key = (x["exp"], x["short"])
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def generic_status(t: dict) -> dict:
    """Live P/L for any tracked trade (legs priced at CBOE mids; shares at the Nasdaq price)."""
    out = {**t}
    q = nasdaq_quote(t["ticker"])
    legs = _trade_legs(t)
    quotes, meta = {}, {}
    if any(l["cp"] != "STK" for l in legs):
        spot_c, df, meta = _trade_chain(t)
        sub = df[df["expiration"] == pd.Timestamp(t["exp"])]
        for l in legs:
            if l["cp"] == "STK":
                continue
            r_ = sub[(sub["type"] == l["cp"]) & (sub["strike"] == l["K"])]
            if not r_.empty:
                quotes[(l["cp"], float(l["K"]))] = float((r_["bid"].iloc[0] + r_["ask"].iloc[0]) / 2)
        px = q["price"] if q else spot_c
    else:
        px = q["price"] if q else float(daily_history(t["ticker"])["close"].iloc[-1])
    v = _legs_value(legs, px, quotes)
    if v is None:
        out["error"] = "these strikes / expiration are not in CBOE's chain (expired or mistyped?)"
        return out
    o, m, n = _trade_open_value(t), _trade_mult(t), t.get("qty", 1)
    out.update(price=px, value_now=v, pl=(v - o) * m * n, quotes_as_of=meta.get("last_trade"), source=meta.get("source"),
               dte=max(int((pd.Timestamp(t["exp"]) - pd.Timestamp(et_today())).days), 0) if t.get("exp") else None,
               close_now=abs(v), signal="ok", generic=True)
    try:
        out["verdict"] = trade_verdict(t, out, df if any(l["cp"] != "STK" for l in legs) else None, px)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- cut losses / salvage / hold (losing trades)
# How it decides: the stock's price at expiration is spread out using the options' own implied volatility (no drift,
# no forecast), and every outcome is run through the position's payoff. That gives the chance the trade still ends at
# break-even or better and the chance it ends worse than closing now. The stock's trend (20/50-day averages, 5-day
# move) says whether the move is still pushing against the position. Repairs are priced from the live chain.
_VZ = np.array([NormalDist().inv_cdf((i + 0.5) / 800) for i in range(800)])


def _legs_payoff(legs: list, S) -> np.ndarray:
    S = np.asarray(S, dtype=float)
    v = np.zeros_like(S)
    for l in legs:
        q = l["side"] * l.get("n", 1)
        if l["cp"] == "STK":
            v = v + q * S
        elif l["cp"] == "C":
            v = v + q * np.maximum(S - l["K"], 0.0)
        else:
            v = v + q * np.maximum(l["K"] - S, 0.0)
    return v


def _st(px: float, iv: float, days: float) -> np.ndarray:
    tau = max(days, 0.5) / 365
    return px * np.exp(-0.5 * iv * iv * tau + iv * math.sqrt(tau) * _VZ)


def _v_row(df, exp, cp: str, K: float):
    if df is None or df.empty:
        return None
    r = df[(df["expiration"] == pd.Timestamp(exp)) & (df["type"] == cp) & (df["strike"] == float(K))]
    return None if r.empty else r.iloc[0]


def _v_iv(legs: list, df, exp, px: float) -> float:
    ivs = []
    for l in legs:
        if l["cp"] != "STK" and exp:
            r = _v_row(df, exp, l["cp"], l["K"])
            if r is not None and r.iv > 0:
                ivs.append((l["side"] < 0, float(r.iv)))
    if ivs:
        return ([v for s, v in ivs if s] or [ivs[0][1]])[0]
    try:
        e = df[df["expiration"] >= pd.Timestamp(et_today())]
        e = e[(e["expiration"] == e["expiration"].min()) & (e["iv"] > 0)]
        k = e.iloc[(e["strike"] - px).abs().argsort()[:6]]
        v = float(k["iv"].median())
        if v > 0:
            return v
    except Exception:
        pass
    return 0.35


def _v_trend(ticker: str, px: float) -> dict | None:
    try:
        h = daily_history(ticker)
    except Exception:
        return None
    c = pd.to_numeric(h["close"], errors="coerce").dropna()
    if len(c) < 55:
        return None
    s20, s50, s20p = float(c.tail(20).mean()), float(c.tail(50).mean()), float(c.iloc[-25:-5].mean())
    r5 = (px / float(c.iloc[-6]) - 1) * 100
    d = c.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / 14).mean().iloc[-1], (-d.clip(upper=0)).ewm(alpha=1 / 14).mean().iloc[-1]
    rsi = float(100 - 100 / (1 + up / dn)) if dn > 0 else 100.0
    score = (1 if px > s20 else -1) + (1 if s20 > s50 else -1) + (1 if s20 > s20p else -1) + (1 if r5 > 0 else -1)
    return {"s20": s20, "s50": s50, "r5": r5, "rsi": rsi, "score": score}


def _v_quote_side(df, exp, l: dict, px: float, closing: bool) -> float | None:
    """Price to trade one leg now: closing a long sells at the bid, closing a short buys at the ask."""
    if l["cp"] == "STK":
        return px
    r = _v_row(df, exp, l["cp"], l["K"])
    if r is None:
        return None
    if closing:
        return float(r.bid if l["side"] > 0 else r.ask)
    return float((r.bid + r.ask) / 2)


def _v_rolls(legs, df, px, close_mid, dte, exp, credit_total) -> list:
    """Credit rolls for a short vertical or a single short option: same width, later expiration, short strike at or
    further away than today's, opened as one order for a net credit (priced at mid)."""
    sh = [l for l in legs if l["side"] < 0][0]
    lg = [l for l in legs if l["side"] > 0]
    cp, Ks = sh["cp"], float(sh["K"])
    w = abs(Ks - float(lg[0]["K"])) if lg else None
    today = pd.Timestamp(et_today())
    exps = [e for e in sorted(df["expiration"].unique())
            if max(dte + 5, 14) <= (pd.Timestamp(e) - today).days <= dte + 60 and pd.Timestamp(e) > pd.Timestamp(exp)]
    ideas = []
    for e in exps:
        p = df[(df["expiration"] == e) & (df["type"] == cp)]
        p = p[~p["strike"].duplicated()].set_index("strike")
        nd = int((pd.Timestamp(e) - today).days)
        for k in p.index:
            if (cp == "P" and k > Ks) or (cp == "C" and k < Ks):
                continue
            kl = None if w is None else (k - w if cp == "P" else k + w)
            if kl is not None and kl not in p.index:
                continue
            s_ = p.loc[k]
            if s_.bid <= 0 or s_.open_interest < 20:
                continue
            new_mid = (s_.bid + s_.ask) / 2
            new_nat = s_.bid
            if kl is not None:
                l_ = p.loc[kl]
                if l_.ask <= 0:
                    continue
                new_mid -= (l_.bid + l_.ask) / 2
                new_nat -= l_.ask
            net = float(new_mid - close_mid)
            if net < 0:
                continue
            iv = float(s_.iv) if s_.iv > 0 else 0.5
            be = k - (credit_total + net) if cp == "P" else k + (credit_total + net)
            S = _st(px, iv, nd)
            pr = float(np.mean(S >= be) if cp == "P" else np.mean(S <= be)) * 100
            ideas.append({"exp": pd.Timestamp(e).strftime("%Y-%m-%d"), "dte": nd, "short": float(k), "long": kl,
                          "net": net, "be": float(be), "p": pr})
    if not ideas:
        return []
    far = (lambda x: x["short"]) if cp == "P" else (lambda x: -x["short"])
    picks = [{**min(ideas, key=lambda x: (far(x), -x["net"])), "why": "most room"}]
    same = [x for x in ideas if x["short"] == Ks]
    if same:
        picks.append({**min(same, key=lambda x: x["dte"]), "why": "same strikes, more time"})
    picks.append({**max(ideas, key=lambda x: x["p"]), "why": "best odds"})
    seen, out = set(), []
    for x in picks:
        if (x["exp"], x["short"]) not in seen:
            seen.add((x["exp"], x["short"]))
            out.append(x)
    return out


def trade_verdict(t: dict, out: dict, df, px: float) -> dict | None:
    pl = out.get("pl")
    if pl is None or not px:
        return None
    legs, n, m, o = _trade_legs(t), t.get("qty", 1), _trade_mult(t), _trade_open_value(t)
    noise = max(25.0, 0.10 * abs(o) * m * n)          # a few dollars either way is noise
    losing = pl <= -noise
    exp, dte = t.get("exp"), out.get("dte") or 0
    tk = t["ticker"]
    opt = [l for l in legs if l["cp"] != "STK"]
    if opt and (df is None or df.empty):
        return None
    if not opt and (df is None or df.empty):
        try:
            _, df, _ = chain_cached(tk)
        except Exception:
            df = None
    iv = _v_iv(legs, df, exp, px)
    horizon = dte if exp else 30
    S = _st(px, iv, horizon)
    final = (_legs_payoff(legs, S) - o) * m * n             # P/L if held to expiration (shares: 30 days out)
    nat = []
    for l in legs:
        q = _v_quote_side(df, exp, l, px, True) if exp or l["cp"] == "STK" else None
        nat.append(q)
    v_nat = sum(l["side"] * l.get("n", 1) * q for l, q in zip(legs, nat)) if all(q is not None for q in nat) else None
    pl_nat = (v_nat - o) * m * n if v_nat is not None else pl
    pl_cut = (pl + pl_nat) / 2                             # realistic fill: halfway between mid and the quoted bid/ask
    wide = (f"The bid/ask is wide right now{'' if market_open_now() else ' (market closed)'}: closing at the quoted prices "
            f"would be {usd_py(pl_nat)} vs {usd_py(pl)} at mid. Use a limit order near the mid."
            if pl - pl_nat > max(2 * noise, 0.25 * abs(pl)) else None)
    p_rec = float(np.mean(final >= 0)) * 100
    p_worse = float(np.mean(final < pl_cut)) * 100
    ev_hold = float(np.mean(final))
    grid = np.linspace(0.0, px * 4, 801)
    pay = _legs_payoff(legs, grid)
    bounded = abs(pay[-1] - pay[-2]) < 1e-9 and abs(pay[1] - pay[0]) < 1e-9 * max(1, px)
    worst = float((pay.min() - o) * m * n)
    p_max = float(np.mean(final <= worst + abs(worst) * 0.02)) * 100 if bounded and worst < 0 else None
    up = float(np.mean(_legs_payoff(legs, S * 1.02)) - np.mean(_legs_payoff(legs, S * 0.98)))
    wants = "up" if up > 0 else "down"
    tr = _v_trend(tk, px)
    against = bool(tr and ((up > 0 and tr["score"] <= -2) or (up < 0 and tr["score"] >= 2)))
    with_you = bool(tr and ((up > 0 and tr["score"] >= 2) or (up < 0 and tr["score"] <= -2)))
    shorts = [l for l in opt if l["side"] < 0]
    short_itm = any((l["cp"] == "P" and px < l["K"]) or (l["cp"] == "C" and px > l["K"]) for l in shorts)
    top_flat = abs(pay[-1] - pay[-2]) < 1e-9 or pay[-1] < pay.max() - 1e-9
    max_gain = float((pay.max() - o) * m * n) if top_flat else None
    if not losing:                                    # winning or flat: take the profit now, or hold on?
        cap = pl / max_gain * 100 if max_gain and max_gain > 0 else None
        left = max_gain - pl if max_gain is not None else None
        p_keep = 100 - p_worse
        risk_txt = f" with {usd_py(worst)} still at risk" if p_max is not None else ""
        if out.get("signal") == "target" or (cap is not None and cap >= 50):
            call, head = "take", (f"Take profit now — {cap:.0f}% of the max profit is in hand" if cap is not None else
                                  "Take profit now — your 50% target is reached") + \
                f" (about {usd_py(pl_cut)} if you close now). Only {usd_py(left)} more is possible{risk_txt}." \
                if left is not None else "Take profit now — your target is reached."
        elif pl > noise and exp and dte <= 5 and p_worse >= 40:
            call, head = "take", (f"Take profit now — {dte} day{'s' if dte != 1 else ''} left and a {p_worse:.0f}% chance it ends "
                                  f"worse than the {usd_py(pl_cut)} you can lock today.")
        elif pl > noise and against and p_worse >= 45:
            call, head = "take", (f"Take profit now — the trend has turned against this trade and there is a {p_worse:.0f}% "
                                  f"chance it ends worse than the {usd_py(pl_cut)} you can lock today.")
        elif pl > noise and not with_you and ev_hold < pl_cut - 0.10 * abs(pl_cut) and p_worse >= 50:
            call, head = "take", (f"Take profit now — holding averages {usd_py(ev_hold)}, less than the {usd_py(pl_cut)} "
                                  f"you can lock today.")
        else:
            call, head = "hold", (f"Hold on — {p_keep:.0f}% chance it ends at least as well as closing now"
                                  + (f"; {cap:.0f}% of the max profit so far" if cap is not None and pl > noise else "")
                                  + ("; the trend is on your side." if with_you else
                                     "; the trend is against you — re-check daily." if against else "."))
        why = [f"{p_rec:.0f}% chance it finishes at break-even or better{' by ' + dshort_py(exp) if exp else ' within 30 days'}; "
               f"{p_worse:.0f}% chance it ends worse than closing now ({usd_py(pl_cut)})."]
        if tr:
            side = "above" if px > tr["s20"] else "below"
            why.append(f"{tk} {px:,.2f} is {side} its 20-day average ({tr['s20']:,.2f}); 5-day move {tr['r5']:+.1f}%, RSI {tr['rsi']:.0f}.")
        if wide:
            why.append(wide)
        if left is not None:
            why.append(f"Left to gain: {usd_py(left)}." + (f" Still at risk: {usd_py(worst)}." if p_max is not None else ""))
        return {"call": call, "losing": False, "headline": head, "why": why, "p_recover": p_rec, "p_worse": p_worse,
                "p_max": p_max, "pl_cut": pl_cut, "ev_hold": ev_hold, "worst": worst if p_max is not None else None,
                "captured": cap, "plans": [],
                "note": "Odds come from the options' own implied volatility with no forecast of direction; the trend check is "
                        "separate. A guide, not a promise. No orders are placed."}
    # the call
    if p_rec < 30 or (p_rec < 45 and against) or (exp and dte <= 5 and short_itm and not with_you):
        call = "cut"
    elif p_rec >= 50 and not against:
        call = "hold"
    else:
        call = "salvage"
    little_left = p_max is not None and (pl_cut - worst) < 0.15 * abs(worst)
    if call == "cut" and little_left:
        call = "salvage"
    why = [f"{p_rec:.0f}% chance it still finishes at break-even or better{' by ' + dshort_py(exp) if exp else ' within 30 days'}; "
           f"{p_worse:.0f}% chance it ends worse than closing now (closing now locks about {usd_py(pl_cut)})."]
    if p_max is not None:
        why.append(f"{p_max:.0f}% chance of the full max loss ({usd_py(worst)}).")
    if tr:
        side = "above" if px > tr["s20"] else "below"
        why.append(f"{tk} {px:,.2f} is {side} its 20-day average ({tr['s20']:,.2f}); 5-day move {tr['r5']:+.1f}%, RSI {tr['rsi']:.0f}. "
                   + ("The trend is pushing against this trade." if against else
                      "The trend is now on your side." if with_you else "No clear trend either way."))
    if wide:
        why.append(wide)
    if short_itm:
        why.append("Your short strike is in the money" + (f" with {dte} days left — time is no longer on your side." if exp else "."))
    why.append(f"If it just drifts at the volatility its options price in ({iv * 100:.0f}% IV), holding averages "
               f"{usd_py(ev_hold)} vs {usd_py(pl_cut)} for closing now.")
    head = {"cut": f"Most likely keeps losing — close it. Cutting now locks about {usd_py(pl_cut)}"
                   + (f" instead of risking {usd_py(worst)}." if p_max is not None else "."),
            "salvage": (f"Already near max loss — closing now saves only about ${pl_cut - worst:,.0f}, so holding to expiration "
                        f"costs little more and keeps the {p_rec:.0f}% chance of a rebound. Don't add money to it." if little_left else
                        f"Can still be saved — {p_rec:.0f}% chance it recovers as is; the repairs below can improve that."),
            "hold": f"Hold on — {p_rec:.0f}% chance it recovers and the trend is not against you. Re-check if that changes."}[call]
    plans = []
    try:
        plans = _v_plans(t, out, legs, df, px, exp, dte, n, o, pl_cut, worst if p_max is not None else None, p_rec, iv, wants)
    except Exception:
        plans = []
    return {"call": call, "losing": True, "headline": head, "why": why, "p_recover": p_rec, "p_worse": p_worse, "p_max": p_max,
            "pl_cut": pl_cut, "ev_hold": ev_hold, "worst": worst if p_max is not None else None, "plans": plans,
            "note": "Odds come from the options' own implied volatility with no forecast of direction; the trend check is "
                    "separate. A guide, not a promise. No orders are placed."}


def dshort_py(d: str) -> str:
    try:
        x = date.fromisoformat(str(d)[:10])
        return f"{x.strftime('%b')} {x.day}"
    except Exception:
        return str(d)


def usd_py(x: float) -> str:
    return ("-$" if x < 0 else "+$") + f"{abs(x):,.0f}"


def _v_plans(t, out, legs, df, px, exp, dte, n, o, pl_cut, worst, p_rec, iv, wants) -> list:
    tk, plans = t["ticker"], []
    opt = [l for l in legs if l["cp"] != "STK"]
    stk = [l for l in legs if l["cp"] == "STK"]
    m = _trade_mult(t)
    vert = len(opt) == 2 and not stk and opt[0]["cp"] == opt[1]["cp"] and opt[0]["side"] != opt[1]["side"]
    single = len(opt) == 1 and not stk
    sh = [l for l in opt if l["side"] < 0]
    close_mid = -(_legs_value(legs, px, {(l["cp"], float(l["K"])): _v_quote_side(df, exp, l, px, False) for l in opt})
                  or 0.0) if opt else 0.0
    credit = -o
    # 1) credit roll (short vertical or a single short option)
    if sh and (vert or single) and o < 0 and exp:
        if t.get("legs") is None and out.get("rolls") is not None:
            pass                                       # the classic put-spread card already lists its rolls
        else:
            rl = _v_rolls(legs, df, px, close_mid, dte, exp, credit)
            if not rl:
                plans.append({"title": "Roll for a credit: not available right now", "p": None,
                              "how": "No later expiration (2 to 9 weeks out) at the same or a safer strike pays enough to buy this "
                                     "back and still collect a credit. Paying to roll only adds to the loss, so it isn't listed.",
                              "risk": ""})
            for r in rl:
                cp = sh[0]["cp"]
                legtxt = f"{r['short']:g}/{r['long']:g}{cp}" if r["long"] is not None else f"{r['short']:g}{cp}"
                plans.append({"title": f"Roll for a credit ({r['why']}): to {legtxt} {dshort_py(r['exp'])} ({r['dte']}d)",
                              "p": r["p"],
                              "how": f"One order: buy back your spread and sell the {legtxt} {dshort_py(r['exp'])} for a net credit of about "
                                     f"{r['net']:.2f} at mid (+${r['net'] * 100 * n:,.0f}). Everything you have collected is then "
                                     f"{credit + r['net']:.2f} per share, so the whole trade breaks even at {r['be']:,.2f} on {dshort_py(r['exp'])}. "
                                     f"You give {tk} more time and more room instead of taking the loss now.",
                              "risk": "Same width means the same max loss on the new spread; you are extending a losing trade, not "
                                      "erasing it. Only roll for a credit — never pay to roll."})
    # 2) add the other side (credit vertical -> iron condor): extra credit lowers the loss and moves break-even
    if vert and o < 0 and exp:
        cp = sh[0]["cp"]
        w = abs(opt[0]["K"] - opt[1]["K"])
        other = "C" if cp == "P" else "P"
        sub = df[(df["expiration"] == pd.Timestamp(exp)) & (df["type"] == other)]
        sub = sub[~sub["strike"].duplicated()].set_index("strike")
        cand = []
        for k in sub.index:
            kl = k + w if other == "C" else k - w
            ks0 = float(sh[0]["K"])
            if kl not in sub.index or (other == "C" and k < max(px, ks0)) or (other == "P" and k > min(px, ks0)):
                continue
            s_, l_ = sub.loc[k], sub.loc[kl]
            if s_.bid <= 0 or s_.open_interest < 20:
                continue
            mid = (s_.bid + s_.ask) / 2 - (l_.bid + l_.ask) / 2
            if mid < 0.05 * w:
                continue
            cand.append((abs(abs(float(s_.delta)) - 0.30), k, kl, float(mid), float(s_.bid - l_.ask), float(s_.delta)))
        if cand:
            _, k, kl, mid, nat_, dl = min(cand)
            newlegs = legs + [{"cp": other, "K": float(k), "side": -1, "n": 1}, {"cp": other, "K": float(kl), "side": 1, "n": 1}]
            S = _st(px, iv, dte)
            fin = (_legs_payoff(newlegs, S) - (o - mid)) * m * n
            pr = float(np.mean(fin >= 0)) * 100
            new_worst = float((_legs_payoff(newlegs, np.linspace(0.0, px * 4, 801)).min() - (o - mid)) * m * n)
            ks = sh[0]["K"]
            be = ks - (credit + mid) if cp == "P" else ks + (credit + mid)
            plans.append({"title": f"Add the other side: sell the {k:g}/{kl:g}{other} {dshort_py(exp)} (turns it into an iron condor)",
                          "p": pr,
                          "how": f"Sell a {'call' if other == 'C' else 'put'} credit spread on the far side of today's price, same "
                                 f"expiration and width, for about {mid:.2f} (natural {nat_:.2f}; short delta {dl:.2f}). "
                                 f"{tk} can only finish on one side, so the max loss shrinks by that credit "
                                 f"(+${mid * 100 * n:,.0f}) and your break-even moves to {be:,.2f}. "
                                 f"This works when {tk} stalls or drifts back; it also needs no extra margin for the same width.",
                          "risk": f"Worst case becomes {usd_py(new_worst)} instead of {usd_py(worst) if worst is not None else 'today’s max loss'}, "
                                  f"but if {tk} reverses hard below {k:g} the new side loses instead"
                                  .replace("below", "below" if other == "P" else "above")
                                  + (f". The chance of finishing green drops to {pr:.0f}% (from {p_rec:.0f}%): this trades odds for a smaller worst case."
                                     if pr < p_rec else ".")})
    # 3) close part now
    if (vert or single or stk) and n >= 2:
        h = n // 2
        plans.append({"title": f"Close {h} of {n} now, keep {n - h}",
                      "p": p_rec,
                      "how": f"Locks about {usd_py(pl_cut * h / n)} on {h} and leaves {n - h} with the same {p_rec:.0f}% chance to "
                             f"recover. Use it when you can't decide: it caps the damage and keeps some upside."
                             + (f" Worst case on what's left: {usd_py(worst * (n - h) / n)}." if worst is not None else ""),
                      "risk": "Halves both the further loss and the recovery."})
    # 4) put spread you'd be happy to own -> wheel
    if vert and o < 0 and sh[0]["cp"] == "P" and exp:
        ks = sh[0]["K"]
        lg = [l for l in opt if l["side"] > 0][0]
        r = _v_row(df, exp, "P", lg["K"])
        bid = float(r.bid) if r is not None else 0.0
        basis = ks - credit - bid
        if r is not None: plans.append({"title": "Only if you'd be happy to own the shares: convert to a cash-secured put and wheel",
                      "p": None,
                      "how": f"Sell your long {lg['K']:g} put (about {bid:.2f}) and keep the short {ks:g} put. If {tk} is under "
                             f"{ks:g} at expiration you buy {100 * n:,} shares at {ks:g}; after both credits your cost is about "
                             f"{basis:,.2f}. Then sell covered calls at or above {basis:,.2f} each month until the shares are "
                             f"called away at break-even or better. Cash needed: ${ks * 100 * n:,.0f}.",
                      "risk": "Removes your protection: below the old long strike the loss is no longer capped. Only for a "
                              "stock you want to own at that price."})
    # 5) single long option -> spread
    if single and opt[0]["side"] > 0 and exp:
        l0 = opt[0]
        cp = l0["cp"]
        sub = df[(df["expiration"] == pd.Timestamp(exp)) & (df["type"] == cp)]
        sub = sub[(sub["bid"] > 0) & ((sub["strike"] > l0["K"]) if cp == "C" else (sub["strike"] < l0["K"]))]
        if not sub.empty:
            sub = sub.assign(dd=(sub["delta"].abs() - 0.30).abs()).sort_values("dd")
            k2, b2 = float(sub["strike"].iloc[0]), float(sub["bid"].iloc[0])
            newlegs = legs + [{"cp": cp, "K": k2, "side": -1, "n": 1}]
            S = _st(px, iv, dte)
            fin = (_legs_payoff(newlegs, S) - (o - b2)) * m * n
            if float(np.mean(fin >= 0)) * 100 >= p_rec + 3: plans.append({"title": f"Sell the {k2:g}{cp} against it (turn it into a {'call' if cp == 'C' else 'put'} spread)",
                          "p": float(np.mean(fin >= 0)) * 100,
                          "how": f"Selling the {k2:g}{cp} {dshort_py(exp)} brings in about {b2:.2f} (${b2 * 100 * n:,.0f}), cutting your "
                                 f"cost from {o:.2f} to {o - b2:.2f}, so {tk} needs a smaller move to get you back to even.",
                          "risk": f"Caps the gain at {k2:g}."})
    # 6) shares: covered call / repair
    if stk and not opt and df is not None and not df.empty:
        basis = o
        today = pd.Timestamp(et_today())
        c = df[(df["type"] == "C") & (df["bid"] > 0)]
        e1 = [e for e in sorted(c["expiration"].unique()) if 25 <= (pd.Timestamp(e) - today).days <= 50]
        if e1:
            s1 = c[(c["expiration"] == e1[0]) & (c["strike"] >= basis)].sort_values("strike")
            if not s1.empty:
                k, b = float(s1["strike"].iloc[0]), float(s1["bid"].iloc[0])
                plans.append({"title": f"Covered call: sell the {k:g}C {dshort_py(pd.Timestamp(e1[0]).strftime('%Y-%m-%d'))}",
                              "p": None,
                              "how": f"Sell 1 call per 100 shares at {k:g} (at or above your {basis:,.2f} cost) for about {b:.2f} "
                                     f"(${b * 100 * max(n // 100, 1):,.0f} on {max(n // 100, 1)} contract{'s' if n >= 200 else ''}). Repeat monthly; each premium lowers your cost.",
                              "risk": f"If {tk} jumps past {k:g} the shares are called away there — still at break-even or better."})
        e2 = [e for e in sorted(c["expiration"].unique()) if 40 <= (pd.Timestamp(e) - today).days <= 100]
        if e2 and basis > px * 1.03:
            ce = c[c["expiration"] == e2[0]].drop_duplicates("strike").set_index("strike")
            ka = ce.index[np.abs(ce.index - px).argmin()]
            tgt = (px + basis) / 2
            kb = ce.index[np.abs(ce.index - tgt).argmin()]
            if kb > ka:
                cost = float(ce.loc[ka].ask - 2 * ce.loc[kb].bid)
                at = (kb - basis) + (kb - ka) - cost
                plans.append({"title": f"Repair strategy: buy 1 {ka:g}C, sell 2 {kb:g}C {dshort_py(pd.Timestamp(e2[0]).strftime('%Y-%m-%d'))} per 100 shares",
                              "p": None,
                              "how": f"Net {'cost' if cost > 0 else 'credit'} about {abs(cost):.2f}. If {tk} gets back to {kb:g} by "
                                     f"expiration you're within about {usd_py(at * n)} of break-even (shares plus calls) instead of needing "
                                     f"{basis:,.2f} — it roughly halves the rally you need to break even.",
                              "risk": f"Above {kb:g} the extra gain is capped; below {ka:g} the calls expire and you're where you are now "
                                      f"(less the small cost)."})
    return plans


def trades_report() -> list:
    """Open trades with live status (expired ones are settled and moved to closed first). Slow (loads chains):
    run in the background via _tr_refresh; the page reads trades_report_fast()."""
    today = et_now().date().isoformat()
    settled = {}
    for t in load_trades():                        # settle outside the file lock (needs price history)
        if t.get("status") != "closed" and t.get("exp") and t["exp"] < today:
            st = _settle_expired(t)
            if st and st["close_value"] is not None:
                settled[t["id"]] = st
    with _trades_lock:                             # re-read so a trade saved meanwhile is never lost
        trades = load_trades()
        changed = False
        for i, t in enumerate(trades):
            st = settled.get(t.get("id"))
            if st and t.get("status") != "closed":
                trades[i] = {**_finish(t, st["close_value"]), **st}
                changed = True
        if changed:
            with _io_lock:
                _write_json(TRADES_PATH, trades)
    out = []
    for t in trades:
        if t.get("status") == "closed":
            continue
        try:
            out.append(trade_status(t) if not t.get("legs") else generic_status(t))
        except Exception as e:
            out.append({**t, "error": str(e)})
    return out


_tr_cache = {"t": 0.0, "by_id": {}, "busy": False, "asof": ""}
_tr_lock = threading.Lock()


def _tr_refresh() -> None:
    try:
        rep_ = trades_report()
        _tr_cache.update(t=time.time(), by_id={r["id"]: r for r in rep_ if r.get("id")},
                         asof=et_now().strftime("%H:%M:%S ET"))
    except Exception:
        _tr_cache["t"] = time.time() - 90                         # retry in ~30 s
    finally:
        _tr_cache["busy"] = False


def _tr_kick(force: bool = False) -> None:
    with _tr_lock:
        if _tr_cache["busy"] or (not force and time.time() - _tr_cache["t"] < 120):
            return
        _tr_cache["busy"] = True
    threading.Thread(target=_tr_refresh, daemon=True).start()


def trades_report_fast() -> list:
    """Open trades with the latest background valuation (refreshed every ~2 min) — instant."""
    _tr_kick()
    out = []
    for t in load_trades():
        if t.get("status") == "closed":
            continue
        r = _tr_cache["by_id"].get(t.get("id"))
        out.append({**r, **t, "valued_at": _tr_cache["asof"]} if r else {**t, "valuing": True})
    return out


def _trade_osyms(t: dict) -> list:
    """Schwab option symbols of a trade's own legs (for the quote stream)."""
    if not t.get("exp") or t.get("ticker") in INDEXES or not _STREAM_OK.fullmatch(t.get("ticker") or ""):
        return []
    try:
        return [_sw_osym(t["ticker"], t["exp"], l["cp"], l["K"]) for l in _trade_legs(t) if l["cp"] in ("C", "P")]
    except Exception:
        return []


def _trade_chain(t: dict):
    """The shared chain for a trade, with the bid/ask of the trade's own legs replaced by Schwab's pushed quotes when
    those are newer than the chain download. So P/L and the hold / cut / take call use quotes that are seconds old,
    while the rest of the chain (other strikes, for rolls and repairs) is refreshed every ~90 s."""
    spot, df, meta = chain_cached(t["ticker"])
    s_ = _stream
    if not s_.connected:
        return spot, df, meta
    t_chain = (_chain_cache.get(t["ticker"]) or (0,))[0]
    upd = []
    for l in _trade_legs(t):
        if l["cp"] not in ("C", "P"):
            continue
        d = s_.op.get(_sw_osym(t["ticker"], t["exp"], l["cp"], l["K"]))
        if d and d.get("_t", 0) > t_chain and d.get("2") is not None and d.get("3") is not None and float(d["3"]) > 0:
            upd.append((l["cp"], float(l["K"]), float(d["2"]), float(d["3"])))
    if upd:
        df = df.copy()
        exp = pd.Timestamp(t["exp"])
        for cp, k, b, a in upd:
            m = (df["expiration"] == exp) & (df["type"] == cp) & (df["strike"] == k)
            df.loc[m, "bid"] = b
            df.loc[m, "ask"] = a
        meta = {**meta, "legs_live": len(upd)}
    return spot, df, meta


def trades_live_loop() -> None:
    """With Schwab's stream up: re-value open trades within seconds of a change in the stock or in the trade's own
    option quotes (at most every 10 s). Nothing runs while quotes stand still (nights, weekends)."""
    last_sig, last_t = None, 0.0
    while True:
        try:
            s_ = _stream
            if s_.connected:
                trades = load_trades(True)
                eq = [t["ticker"] for t in trades if t.get("ticker")] + [w["ticker"] for w in load_config().get("watchlist", [])]
                op = [o for t in trades for o in _trade_osyms(t)]
                s_.ask("eq", eq)
                s_.ask("op", op)
                sig = tuple((k, (s_.eq.get(k) or {}).get("3")) for k in sorted(set(eq))) + \
                    tuple((k, (s_.op.get(k) or {}).get("2"), (s_.op.get(k) or {}).get("3")) for k in sorted(set(op)))
                if trades and sig != last_sig and time.time() - last_t >= 10 and not _tr_cache["busy"]:
                    last_sig, last_t = sig, time.time()
                    _tr_kick(force=True)
        except Exception:
            pass
        time.sleep(2)


def trades_loop() -> None:
    """Keep open-trade values fresh: every 2 min while the market is open, every 15 min otherwise."""
    while True:
        try:
            if load_trades(True):
                _tr_kick(force=True)
        except Exception:
            pass
        time.sleep(120 if market_open_now() else 900)


def trade_results() -> dict:
    """Your real results: every closed or expired tracked trade, with totals by strategy and by source."""
    closed = sorted([t for t in load_trades() if t.get("status") == "closed"],
                    key=lambda t: t.get("close_date") or "", reverse=True)

    def agg(L):
        w = sum(1 for t in L if t.get("win"))
        pl = sum(t.get("pnl") or 0 for t in L)
        return {"n": len(L), "won": w, "lost": len(L) - w, "pnl": pl,
                "avg_win": (sum(t["pnl"] for t in L if t.get("win")) / w) if w else None,
                "avg_loss": (sum(t["pnl"] for t in L if not t.get("win")) / (len(L) - w)) if len(L) > w else None,
                "rec_p": (sum(t["rec_p"] for t in L if t.get("rec_p") is not None) /
                          max(1, sum(1 for t in L if t.get("rec_p") is not None))) if any(t.get("rec_p") is not None for t in L) else None}
    by_s, by_src = {}, {}
    for t in closed:
        by_s.setdefault(t.get("strat") or "PCS", []).append(t)
        by_src.setdefault((t.get("src") or "added by hand").split(" · ")[0], []).append(t)
    return _clean({"total": agg(closed), "by_strat": {k: agg(v) for k, v in by_s.items()},
                   "by_source": {k: agg(v) for k, v in by_src.items()}, "trades": closed[:500],
                   "open": sum(1 for t in load_trades() if t.get("status") != "closed")})


def check_trades(cfg: dict) -> list:
    """Alerts when a trade reaches 50%, turns to 'defend', or slips to 'watch'."""
    state = _read_json(STATE_PATH, {})
    alerts = []
    for r in trades_report():
        if r.get("error") or r.get("generic"):
            continue
        key = f"trade|{r['id']}"
        prev = state.get(key, {}).get("signal")
        name = f"{r['ticker']} {r['short']:g}/{r['long']:g}P {r['exp'][5:]}"
        if r["signal"] != prev:
            if r["signal"] == "target":
                _emit(alerts, cfg, r["ticker"], "trade_target", "warn", f"{name}: 50% profit reached",
                      f"Spread {r['mid']:.2f} vs credit {r['credit']:.2f} (+{r['profit_pct']:.0f}%). Buy to close at {r['tp50']:.2f} or better.")
            elif r["signal"] == "danger":
                _emit(alerts, cfg, r["ticker"], "trade_danger", "danger", f"{name}: defend",
                      " ".join(r["actions"][:1]))
            elif r["signal"] == "watch" and prev in (None, "ok"):
                _emit(alerts, cfg, r["ticker"], "trade_watch", "warn", f"{name}: watch", " ".join(r["actions"][:2]))
        state[key] = {"signal": r["signal"], "ts": et_now().isoformat()}
    with _io_lock:
        _write_json(STATE_PATH, state)
    return alerts


HIST_DIR = APP_DIR / "hist_cache"


def live_bars(tks: list) -> dict:
    """The current (or last) regular session's daily bar per ticker from Schwab batch quotes, 200 symbols a request:
    {tk: {"date", "open", "high", "low", "close", "volume", "live"}}. While the market is open the close is the live
    last price; otherwise it is the official regular-session close. {} when Schwab is not connected."""
    out = {}
    if not _sw_live():
        return out
    tks = [t for t in dict.fromkeys(tks) if t not in INDEXES and _STREAM_OK.fullmatch(t or "")]
    live, tz = _session_status() == "Market Open", et_now().tzinfo
    for i in range(0, len(tks), 200):
        j = _sw_get("/marketdata/v1/quotes", {"symbols": ",".join(tks[i:i + 200]), "fields": "quote,regular", "indicative": "false"})
        for sym, v in (j or {}).items():
            q = (v.get("quote") or {}) if isinstance(v, dict) else {}
            rg = (v.get("regular") or {}) if isinstance(v, dict) else {}
            ts = rg.get("regularMarketTradeTime") or q.get("tradeTime")
            px = q.get("lastPrice") if live else (rg.get("regularMarketLastPrice") or q.get("closePrice"))
            if not ts or not px:
                continue
            out[sym] = {"date": datetime.fromtimestamp(ts / 1000, timezone.utc).astimezone(tz).date().isoformat(),
                        "open": q.get("openPrice") or None, "high": q.get("highPrice") or None, "low": q.get("lowPrice") or None,
                        "close": float(px), "volume": q.get("totalVolume") or None, "live": live}
    return out


def _with_bar(h: pd.DataFrame | None, bar: dict | None) -> pd.DataFrame | None:
    """Daily bars with the live session bar applied: appended when the history does not have that day yet, updated in
    place when it does and the market is open. Final (after-close) bars already in the history are left alone."""
    if h is None or not bar or len(h) == 0:
        return h
    last = str(h["date"].iloc[-1])[:10]
    if bar["date"] > last:
        row = {c: bar.get(c) for c in ("date", "open", "high", "low", "close", "volume")}
        for c in ("open", "high", "low"):
            row[c] = row[c] or bar["close"]
        return pd.concat([h, pd.DataFrame([row])], ignore_index=True)
    if bar["date"] == last and bar.get("live"):
        h = h.copy()
        i = h.index[-1]
        for c in ("close", "open", "high", "low", "volume"):
            if bar.get(c) and c in h.columns:
                h.loc[i, c] = bar[c]
    return h


def scan_history(tk: str, live_ok: bool = False) -> pd.DataFrame:
    """Daily bars for the momentum/rotation scan, cached on disk (last 520 sessions): 30 min while the market is open,
    12 h otherwise — so a rescan of 250+ tickers doesn't re-download everything from CBOE.
    live_ok: the caller adds today's bar itself from live quotes (_with_bar), so during the session a file that already
    has every completed day is good as it is — no re-download until after the close."""
    HIST_DIR.mkdir(exist_ok=True)
    f = HIST_DIR / f"{tk}.json"
    mo = market_open_now()
    ttl = 1800 if mo else 12 * 3600
    age = time.time() - f.stat().st_mtime if f.exists() else None
    if age is not None and (age < ttl or (live_ok and mo and age < 30 * 3600)):
        h = pd.DataFrame(_read_json(f, []))
        if len(h) >= 80 and len(h) != 300:                 # exactly 300 = the old short cache: refetch once for 520 bars
            if age < ttl or str(h["date"].iloc[-1])[:10] >= _last_session_close(et_now().replace(tzinfo=None)).date().isoformat():
                return h
    h = daily_history(tk).tail(520)
    _write_json(f, h.to_dict("records"))
    return h.reset_index(drop=True)


def run_scan() -> dict:
    """Momentum/rotation filter on the universe, then put credit spread candidates for the leaders."""
    _scan.update(running=True, error=None, progress="loading price history")
    try:
        cfg = load_config()
        uni = {**{k: ("ETF", v) for k, v in SCAN_ETFS.items()}, **{k: ("Stock", v) for k, v in SCAN_STOCKS.items()}}
        for it in cfg.get("watchlist", []):
            uni.setdefault(it["ticker"], ("Watchlist", "Watchlist"))
        for k, v in ALWAYS_SHORT.items():
            uni.setdefault(k, v)
        try:                                               # today's bar for every ticker: 2 Schwab requests, real time
            bars = live_bars(["SPY", *uni])
        except Exception:
            bars = {}
        spy = _with_bar(scan_history("SPY", bool(bars.get("SPY"))), bars.get("SPY"))

        def hist(tk):
            try:
                return tk, _with_bar(scan_history(tk, bool(bars.get(tk))), bars.get(tk))
            except Exception:
                return tk, None
        rows = {}
        with ThreadPoolExecutor(6) as ex:
            for n_, (tk, h) in enumerate(ex.map(hist, uni), 1):
                _scan["progress"] = f"price history {n_}/{len(uni)}"
                if h is None or len(h) < 80:
                    continue
                m = _momentum(h, spy)
                if not m:
                    continue
                rsi, _ = rsi14(tk, 0, "", h)
                rows[tk] = {"ticker": tk, "kind": uni[tk][0], "sector": uni[tk][1],
                            "price": float(h["close"].iloc[-1]), "rsi": rsi, **m}
        mdf = pd.DataFrame(rows.values())
        for col in ("rs1", "rs3"):
            mdf["p_" + col] = mdf[col].rank(pct=True)
        mdf["mom"] = 100 * (0.35 * mdf["p_rs1"] + 0.25 * mdf["p_rs3"] + 0.25 * mdf["trend"] / 3
                            + 0.15 * mdf["quad"].map(QUAD_SCORE).fillna(0.3))
        quads = {r.sector: r.quad for r in mdf.itertuples() if r.kind == "ETF"}
        mdf["sector_quad"] = [quads.get(r.sector) if r.kind != "ETF" else r.quad for r in mdf.itertuples()]
        mdf["sector_quad"] = mdf["sector_quad"].astype(object).where(mdf["sector_quad"].notna(), None)
        mdf.loc[mdf["kind"] != "ETF", "mom"] += mdf["sector_quad"].isin(["Leading", "Improving"]).astype(float) * 5
        mdf["mom"] = mdf["mom"].clip(upper=100)
        mdf["qualified"] = (mdf["trend"] >= 2) & (mdf["rs1"] > -3)
        q = mdf[mdf["qualified"]].sort_values("mom", ascending=False)
        etfs = q[q["kind"] == "ETF"].head(18)              # the 18 strongest uptrending ETFs get a chain scan
        leaders = pd.concat([etfs, q[q["kind"] != "ETF"].head(max(16, SCAN_TOP_CHAINS - len(etfs)))])
        extra = mdf[mdf["ticker"].isin(list(ALWAYS_SHORT)) & ~mdf["ticker"].isin(leaders["ticker"])]
        leaders = pd.concat([leaders, extra])

        rot_cols = ["ticker", "sector", "price", "rsi", "r1", "rs1", "rs3", "trend", "quad", "mom"]
        stock_rot = mdf[mdf["kind"] != "ETF"].sort_values("mom", ascending=False)
        rot_now = {"rotation": mdf[mdf["kind"] == "ETF"].sort_values("mom", ascending=False)[rot_cols].round(2).to_dict("records"),
                   "stock_rotation": stock_rot[rot_cols + ["sector_quad"]].round(2).to_dict("records"),
                   "rotation_ts": et_now().strftime("%Y-%m-%d %H:%M ET"), "universe": len(rows)}
        prev = _read_json(SCAN_PATH, None) or {}
        with _io_lock:                                     # rotation shows right away; spreads fill in after the chains
            _write_json(SCAN_PATH, _clean(json.loads(json.dumps({**prev, **rot_now}, default=str))))
        _scan["progress"] = "earnings calendar (Nasdaq.com)"
        earn = nasdaq_earnings()

        def chains(tk):
            try:
                spot, df, meta = load_chain(tk)
                spreads, puts = [], []
                if meta.get("stale"):                        # CBOE hasn't rebuilt this chain: no trades from old quotes
                    return tk, spot, [], [], None, meta.get("chain_time") or "unknown"
                for w_ in week_keys():                       # 0 DTE + every expiration week to ~60 days
                    spreads += _spread_candidates(spot, df, w_, min_cw=0.03, relaxed=True)[:20]
                    try:
                        puts += _csp_candidates(spot, df, w_)[:15]
                    except Exception:
                        pass
                add_p50(spot, [c for c in spreads + puts if c["dte"] > 0], tk)
                try:
                    wk4 = gex_weeks4(spot, df)
                    record_gex_daily(tk, meta, spot, df, wk4)
                except Exception:
                    wk4 = None
                return tk, spot, spreads, puts, wk4, None
            except Exception as e:
                return tk, None, str(e), None, None, None
        items, stale = {}, {}
        with ThreadPoolExecutor(6) as ex:
            for n_, (tk, spot, spreads, puts, wk4, _x) in enumerate(ex.map(chains, list(leaders["ticker"])), 1):
                _scan["progress"] = f"option chains {n_}/{len(leaders)}"
                if spot is None:
                    continue
                if _x:
                    stale[tk] = _x
                r = rows[tk]
                items[tk] = {**{k: r[k] for k in ("ticker", "kind", "sector", "rsi", "rs1", "rs3", "r1", "trend", "quad")},
                             "price": spot, "src": _last_src.get(tk), "mom": float(mdf.loc[mdf["ticker"] == tk, "mom"].iloc[0]),
                             "sector_quad": mdf.loc[mdf["ticker"] == tk, "sector_quad"].iloc[0] or None,
                             "earnings": None if earn is None or r["kind"] == "ETF" else earn.get(tk),
                             "spreads": spreads, "puts": puts, "gex4": wk4}
        rot = mdf[mdf["kind"] == "ETF"].sort_values("mom", ascending=False)
        res = {"ts": et_now().strftime("%Y-%m-%d %H:%M ET"), "universe": len(rows),
               "earnings_ok": earn is not None, "items": items, "stale": stale,
               "rotation": rot[["ticker", "sector", "price", "rsi", "r1", "rs1", "rs3", "trend", "quad", "mom"]]
               .round(2).to_dict("records"),
               "stock_rotation": rot_now["stock_rotation"], "rotation_ts": rot_now["rotation_ts"],
               "leaders": leaders[["ticker", "kind", "sector", "mom", "rs1", "quad"]].round(2).to_dict("records")}
        res = _clean(json.loads(json.dumps(res, default=str)))
        with _io_lock:
            _write_json(SCAN_PATH, res)
        try:                                              # sector ETF GEX snapshot: in the background, at most every 45 min
            if not SECTOR_GEX_PATH.exists() or time.time() - SECTOR_GEX_PATH.stat().st_mtime > 45 * 60:
                threading.Thread(target=_sector_gex_quiet, daemon=True).start()   # (it held up every scan behind it for minutes)
        except Exception:
            pass
        _scan["progress"] = f"done {res['ts']}"
        return res
    except Exception as e:
        _scan["error"] = str(e)
        raise
    finally:
        _scan["running"] = False


# ---------------------------------------------------------------- sell puts under $20 (0 DTE / 1-5 DTE, POP 80%+)
CSP_PATH = APP_DIR / "csp_results.json"
CSP_MAX_PRICE = 30.0
# liquid, optionable ETFs (incl. leveraged / single-stock) — only those trading under the price limit are scanned
PUT_ETFS = ("XLF", "XLE", "XLU", "XLP", "XLB", "XLRE", "KRE", "XBI", "XOP", "GDX", "GDXJ", "SLV", "USO", "UNG", "TLT", "HYG",
            "EEM", "EFA", "FXI", "KWEB", "EWZ", "EWJ", "ARKK", "TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "LABU", "UVXY",
            "VXX", "SPXU", "SPXS", "TMF", "NUGT", "JNUG", "BOIL", "KOLD", "UCO", "SCO", "FAS", "FAZ", "YINN", "BITO", "IBIT",
            "ETHA", "MSOS", "TAN", "ICLN", "JETS", "IYR", "XHB", "SIL", "COPX", "URA", "LIT", "BITX", "CONL", "MSTU", "NVDL",
            "TSLL", "AMDL", "SLVP", "SILJ", "GLDM", "IAU", "PSLV", "SPLG", "SCHD", "QYLD", "JEPQ", "JEPI", "XLC", "XLI")
_etf_px: dict = {"ts": 0.0, "px": {}}


def nasdaq_etf_prices() -> dict:
    """Last price for every US ETF from Nasdaq.com's ETF screener (cached 1 h)."""
    if time.time() - _etf_px["ts"] < 3600 and _etf_px["px"]:
        return _etf_px["px"]
    r = requests.get("https://api.nasdaq.com/api/screener/etf?tableonly=true&limit=25&offset=0&download=true",
                     headers=NASDAQ_HDR, timeout=60)
    d = (r.json() or {}).get("data") or {}
    rows = (d.get("data") or {}).get("rows") or d.get("rows") or []
    px = {str(x.get("symbol", "")).upper(): _num(x.get("lastSalePrice")) for x in rows}
    _etf_px.update(ts=time.time(), px={k: v for k, v in px.items() if v})
    return _etf_px["px"]
CSP_WINDOWS = {"d5": (1, 5), "d14": (6, 14), "d45": (15, 45), "d90": (46, 90)}
CSP_EXTRA_WINDOWS = {"wk": (6, 12), "w2": (13, 20), "w3": (21, 29), "mid": (13, 29), "m45": (30, 45), "m60": (46, 60),
                     "m90": (61, 90)}      # scanner / watchlist windows (same buckets as the spreads, no gaps)
CSP_UNIVERSE = 45            # most-traded sub-$20 stocks (Nasdaq.com screener) that get an option-chain check
_csp = {"running": False, "progress": "", "error": None}


def nasdaq_screener_under(max_price: float = CSP_MAX_PRICE, min_vol: float = 2e6, min_cap: float = 3e8) -> list:
    """Most-traded US-listed stocks under max_price from Nasdaq.com's stock screener."""
    r = _NqRows(nasdaq_stock_rows())
    rows = ((r.json() or {}).get("data") or {}).get("rows") or []
    out = []
    for x in rows:
        px, vol, cap = _num(x.get("lastsale")), _num(x.get("volume")), _num(x.get("marketCap"))
        sym = str(x.get("symbol", "")).strip().upper()
        if px and px < max_price and (vol or 0) >= min_vol and (cap or 0) >= min_cap and re.fullmatch(r"[A-Z]{1,5}", sym):
            out.append({"ticker": sym, "price": px, "volume": vol, "cap": cap, "name": x.get("name", ""),
                        "sector": x.get("sector") or ""})
    out.sort(key=lambda d: -d["volume"])
    return out


def _csp_candidates(spot: float, df: pd.DataFrame, mode: str) -> list:
    """Single short puts (cash-secured) with POP >= 80% for 0 DTE or 1-5 DTE."""
    now = et_now()
    et_today = pd.Timestamp(now.date())
    mins_left = (now.replace(hour=16, minute=0, second=0, microsecond=0) - now).total_seconds() / 60
    exps = sorted(df["expiration"].unique())
    if mode == "0dte":
        exps = [e for e in exps if pd.Timestamp(e) == et_today] if mins_left > 0 else []
    elif win_range(mode):
        exps = _in_win(exps, mode)
    else:
        lo_d, hi_d = CSP_WINDOWS.get(mode) or CSP_EXTRA_WINDOWS[mode]
        exps = [e for e in exps if lo_d <= (pd.Timestamp(e) - et_today).days <= hi_d]
        if len(exps) > 5:                    # daily-expiry tickers: 5 spread across the window
            exps = [exps[round(i * (len(exps) - 1) / 4)] for i in range(5)]
    out = []
    floor_any = return_floors()[1] * 100
    for e in exps:
        sub = df[df["expiration"] == e]
        a = analyze_exp(spot, sub, 0.15)
        dte = 0 if mode == "0dte" else int((pd.Timestamp(e) - et_today).days)
        t = max(mins_left, 15) / (365 * 1440) if mode == "0dte" else dte / 365
        p = sub[(sub["type"] == "P") & (sub["strike"] < spot)]
        p = p[~p["strike"].duplicated()]
        for s in p.itertuples():
            if s.bid < 0.05 or s.ask <= 0 or s.open_interest < (20 if mode == "0dte" else 50) or not (-0.30 <= s.delta <= -0.005):
                continue
            mid0 = (s.bid + s.ask) / 2
            slip = (mid0 - s.bid) / mid0 * 100
            mid = (mid0 + s.bid) / 2                          # realistic fill: halfway between mid and bid
            if slip > 50:
                continue
            iv = (_put_iv(mid, spot, s.strike, t) if mode == "0dte" else None) or (float(s.iv) if s.iv > 0 else None)
            if not iv:
                continue
            be = s.strike - mid
            if be <= 0:
                continue
            d2 = (math.log(spot / be) + (0.04 - iv * iv / 2) * t) / (iv * math.sqrt(t))
            pop = NormalDist().cdf(d2) * 100
            if pop < 60:                     # the page filters further (Min POP 60–95%)
                continue
            roc = mid / s.strike * 100
            if roc < floor_any:                  # premium must be >= 10% of the cash tied up
                continue
            out.append({"exp": pd.Timestamp(e).strftime("%Y-%m-%d"), "dte": dte, "short": float(s.strike), "long": None,
                        "credit": float(mid), "nat": float(s.bid), "bid": float(s.bid), "ask": float(s.ask), "mid": float(mid0),
                        "pop": float(pop), "be": float(be), "sdelta": float(s.delta), "otm": float((s.strike / spot - 1) * 100),
                        "cash": float(s.strike * 100), "prem": float(mid * 100), "roc": float(roc),
                        "roc_day": float(roc / max(dte, 1)), "ann": float(roc * 365 / max(dte, 1)),
                        "s_oi": int(s.open_interest), "slip": float(slip), "s_iv": float(iv), "l_iv": float(iv), "atm_iv": float(iv),
                        "put_wall": a["put_wall"], "net_gex": a["net_gex"],
                        "below_wall": bool(a["put_wall"] is not None and s.strike <= a["put_wall"]),
                        "score": float(pop / 100 * roc / max(dte, 1) * (1 - min(slip, 50) / 200))})
    out.sort(key=lambda c: -c["score"])
    return out[:120]


def run_csp_scan(scope: str = "all") -> dict:
    _csp.update(running=True, error=None, progress="starting")
    try:
        cfg = load_config()
        mx = float(cfg.get("puts_max_price") or CSP_MAX_PRICE)
        watch = list(cfg.get("puts_watchlist") or [])
        uni = []
        if scope != "watchlist" and cfg.get("puts_screener", True):
            _csp["progress"] = f"Nasdaq.com screener: stocks under ${mx:g}"
            uni = nasdaq_screener_under(mx)[:CSP_UNIVERSE]
        if scope != "watchlist" and cfg.get("puts_etfs", True):
            _csp["progress"] = f"Nasdaq.com ETF prices: ETFs under ${mx:g}"
            try:
                epx = nasdaq_etf_prices()
            except Exception:
                epx = {}
            if not epx:                                  # Nasdaq.com unavailable: Schwab prices for the ETF list
                try:
                    epx = {k: v["price"] for k, v in schwab_live(list(PUT_ETFS)).items()}
                except Exception:
                    epx = {}
            have_ = {u["ticker"] for u in uni}
            for tk in PUT_ETFS:
                if tk not in have_ and epx.get(tk) and epx[tk] < mx:
                    uni.append({"ticker": tk, "price": epx[tk], "volume": None, "cap": None, "name": "", "sector": "ETF"})
        have = {u["ticker"] for u in uni}
        for tk in watch:                                 # your puts watchlist, any price
            if tk not in have:
                uni.append({"ticker": tk, "price": None, "volume": None, "cap": None, "name": "", "sector": "Puts watchlist"})
        try:
            earn = nasdaq_earnings()
        except Exception:
            earn = None

        def one(u):
            tk = u["ticker"]
            try:
                spot, df, meta = load_chain(tk)
            except Exception as e:
                return tk, None, str(e)
            if meta.get("stale"):
                return tk, None, "stale:" + str(meta.get("chain_time") or "unknown")
            allp = []
            for w_ in week_keys(later=True):
                allp += _csp_candidates(spot, df, w_)[:40]
            wins = {"puts": allp}
            if not allp and tk not in watch:
                return tk, None, "no put with 60%+ POP, a 0.05+ bid and 10%+ return"
            add_p50(spot, [c for c in allp if c["dte"] > 0], "csp" + tk)
            rsi, trend = None, None
            try:
                h = daily_history(tk, meta["bar"])
                rsi, _ = rsi14(tk, 0, "", h)
                cl = pd.to_numeric(h["close"], errors="coerce").dropna()
                if len(cl) >= 50:
                    s20, s50, last = cl.tail(20).mean(), cl.tail(50).mean(), cl.iloc[-1]
                    trend = int(last > s20) + int(last > s50) + int(s20 > s50)
            except Exception:
                pass
            return tk, {**u, "price": spot, "rsi": rsi, "trend": trend, "quotes_as_of": meta["last_trade"], "src": meta.get("source"),
                        "watch": tk in watch, "earnings": earn.get(tk) if earn else None, **wins}, None

        items, skipped, stale = {}, 0, {}
        if scope == "watchlist":                        # keep the last full scan, refresh only your names
            items = {k: v for k, v in ((_read_json(CSP_PATH, {}) or {}).get("items") or {}).items() if k not in watch}
        with ThreadPoolExecutor(6) as ex:
            for n_, (tk, it, err) in enumerate(ex.map(one, uni), 1):
                _csp["progress"] = f"option chains {n_}/{len(uni)}"
                if it:
                    items[tk] = it
                else:
                    skipped += 1
                    if str(err).startswith("stale:"):
                        stale[tk] = str(err)[6:]
        res = _clean(json.loads(json.dumps({"ts": et_now().strftime("%Y-%m-%d %H:%M ET"), "scope": scope, "max_price": mx,
                                            "screened": len(uni),
                                            "skipped": skipped, "earnings_ok": earn is not None, "items": items,
                                            "stale": stale},
                                           default=str)))
        with _io_lock:
            _write_json(CSP_PATH, res)
        _csp["progress"] = f"done {res['ts']}"
        return res
    except Exception as e:
        _csp["error"] = str(e)
        raise
    finally:
        _csp["running"] = False


def csp_daily_loop() -> None:
    """Run the put screener at 10:05, 12:05 and 14:05 ET on trading days (fresh quotes through the day)."""
    while True:
        try:
            now = et_now()
            cfg = load_config()
            res = _read_json(CSP_PATH, {}) or {}
            today = now.date().isoformat()
            slot = max([f"{h:02d}:05" for h in (10, 12, 14) if (now.hour, now.minute) >= (h, 5)] or ["99:99"])
            ran = str(res.get("ts", "")).startswith(today) and res.get("scope") == "all" and str(res.get("ts", ""))[11:16] >= slot
            if cfg.get("puts_daily", True) and now.weekday() < 5 and (10, 5) <= (now.hour, now.minute) <= (15, 30) \
                    and not ran and not _csp["running"] and market_open_now():
                res = run_csp_scan("all")
                best = []
                for tk, it in (res.get("items") or {}).items():
                    for w_ in ("dte5", "dte14", "dte45"):
                        for c in it.get(w_) or []:
                            if c["pop"] >= 70 and c["prem"] >= 15:
                                best.append((c["roc"] * c["pop"] / 100, tk, w_, c))
                best.sort(key=lambda x: -x[0])
                if best:
                    msg = "; ".join(f"{tk} {c['exp'][5:]} {c['short']:g}P ${c['prem']:.0f} on ${c['cash']:,.0f} "
                                    f"({c['roc']:.1f}%, POP {c['pop']:.0f}%)" for _, tk, w_, c in best[:3])
                    _emit([], cfg, best[0][1], "puts_daily", "warn", "Daily put scan: best premium ÷ cash × POP", msg)
        except Exception:
            pass
        time.sleep(300)


_heavy = threading.Lock()          # one CBOE-heavy job at a time: parallel scans trigger CBOE 429 rate limits
_heavy_who = [""]


def _queued(status: dict, name: str, fn, *a):
    """Run fn after any other heavy job finishes (scans, daily GEX snapshot)."""
    why = no_data_source()
    if why:
        status.update(running=False, progress="", error=why)
        return None
    if not _heavy.acquire(blocking=False):
        status.update(progress="queued")
        _heavy.acquire()
    _heavy_who[0] = name
    try:
        return fn(*a)
    finally:
        _heavy_who[0] = ""
        _heavy.release()


def _csp_bg(scope: str = "all") -> None:
    try:
        _queued(_csp, "sell-puts scan", run_csp_scan, scope)
    except Exception as e:
        _csp.update(running=False, error=str(e))


# ---------------------------------------------------------------- calls: buy calls, LEAPS, call debit spreads, covered calls
CALL_PATH = APP_DIR / "calls_results.json"
CALL_WINDOWS = {"short": (7, 29), "swing": (30, 89), "mid": (90, 269), "leaps": (270, 900)}   # no gaps from 7 days to LEAPS
CALL_UNIVERSE = 40
_calls = {"running": False, "progress": "", "error": None}


def _bs_call(S, K: float, tau: float, iv: float, r: float = 0.04):
    S = np.asarray(S, dtype=float)
    if tau <= 1e-9:
        return np.maximum(S - K, 0.0)
    sd = iv * math.sqrt(tau)
    d1 = (np.log(S / K) + (r + 0.5 * iv * iv) * tau) / sd
    return S * _ncdf(d1) - K * math.exp(-r * tau) * _ncdf(d1 - sd)


def _p_above(spot: float, level: float, iv: float, t: float, r: float = 0.04) -> float:
    """Chance (%) the stock finishes above `level` at expiration (lognormal at the option's IV)."""
    if level <= 0:
        return 100.0
    d2 = (math.log(spot / level) + (r - iv * iv / 2) * t) / (iv * math.sqrt(t))
    return NormalDist().cdf(d2) * 100


def _call_greeks(S: float, K: float, tau: float, iv: float, r: float = 0.04) -> dict:
    sd = iv * math.sqrt(tau)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * tau) / sd
    nd1 = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    theta = (-S * nd1 * iv / (2 * math.sqrt(tau)) - r * K * math.exp(-r * tau) * NormalDist().cdf(d1 - sd)) / 365
    return {"delta": NormalDist().cdf(d1), "theta": theta, "vega": S * nd1 * math.sqrt(tau) / 100, "gamma": nd1 / (S * sd)}


def _call_candidates(spot: float, df: pd.DataFrame, win: str, earn_date: str | None, nkeep: int = 0,
                     exps_force: list | None = None) -> dict:
    """Long calls (incl. LEAPS), call debit spreads and covered calls for one expiration window."""
    today = pd.Timestamp(et_now().date())
    wr = win_range(win)
    if exps_force is not None:                      # caller picked the expirations (tax-loss bounce)
        exps = list(exps_force)
    elif wr:
        exps = [e for e in sorted(df["expiration"].unique()) if wr[0] <= pd.Timestamp(e).date() <= wr[1]
                and (pd.Timestamp(e) - today).days >= 2]
    else:
        lo_d, hi_d = CALL_WINDOWS[win]
        exps = [e for e in sorted(df["expiration"].unique()) if lo_d <= (pd.Timestamp(e) - today).days <= hi_d]
    if win == "leaps":
        exps = exps[:5]
    else:
        oi = df[df["expiration"].isin(exps)].groupby("expiration")["open_interest"].sum()
        n_ = 3 if win in ("later", "short", "swing", "mid") else 2      # weekly windows: the 2 most-traded expirations
        exps = sorted(oi.sort_values(ascending=False).index[:n_])
    longs, spreads, ccs = [], [], []
    min_oi = 10 if win == "leaps" else 50
    for e in exps:
        sub = df[df["expiration"] == e]
        dte = int((pd.Timestamp(e) - today).days)
        t = dte / 365
        c = sub[(sub["type"] == "C") & (sub["bid"] > 0) & (sub["ask"] > 0) & (sub["iv"] > 0)]
        c = c[~c["strike"].duplicated()].set_index("strike").sort_index()
        if c.empty:
            continue
        atm = c.iloc[(pd.Series(c.index) - spot).abs().argsort()[:2]]
        atm_iv = float(atm["iv"].mean())
        em = spot * atm_iv * math.sqrt(t)
        a = analyze_exp(spot, sub, 0.25)
        exs = pd.Timestamp(e).strftime("%Y-%m-%d")
        ecross = bool(earn_date and earn_date <= exs)
        base = {"exp": exs, "dte": dte, "atm_iv": atm_iv, "em": em, "call_wall": a["call_wall"], "put_wall": a["put_wall"],
                "earn_cross": ecross}
        for k, s in c.iterrows():
            mid = (s.bid + s.ask) / 2
            slip = (s.ask - mid) / mid * 100
            iv = float(s.iv)
            if s.open_interest < min_oi or slip > (30 if win == "leaps" else 25):
                continue
            g = _call_greeks(spot, k, t, iv)
            # ---- buy a call
            dlo, dhi = (0.65, 0.88) if win == "leaps" else (0.30, 0.75)
            if dlo <= g["delta"] <= dhi:
                be = k + mid
                intrinsic = max(spot - k, 0.0)
                ext = max(mid - intrinsic, 0.0)
                lev = g["delta"] * spot / mid
                ext_yr = ext / spot * 100 / t
                pop = _p_above(spot, be, iv, t)
                liq = 1 - min(slip, 30) / 100
                score = (g["delta"] / (1 + ext_yr / 4)) * liq if win == "leaps" else pop / 100 * math.sqrt(min(lev, 20)) * liq
                longs.append({**base, "strat": "long", "long": float(k), "short": None, "credit": None, "debit": float(mid),
                              "nat": float(s.ask), "cost": float(mid * 100), "be": float(be), "be_pct": float((be / spot - 1) * 100),
                              "be_em": float((be - spot) / em) if em else None, "pop": float(pop),
                              "p_itm": float(_p_above(spot, k, iv, t)), "delta": float(g["delta"]),
                              "theta_day": float(g["theta"] * 100), "vega": float(g["vega"] * 100), "lev": float(lev),
                              "intrinsic": float(intrinsic), "ext": float(ext), "ext_pct": float(ext / mid * 100),
                              "ext_yr": float(ext_yr), "iv": iv, "oi": int(s.open_interest), "slip": float(slip),
                              "l_iv": iv, "s_iv": None, "score": float(score)})
            # ---- covered call: sell an out-of-the-money call against 100 shares
            if (win in ("short", "swing") or (wr is not None and win != "leaps")) and k > spot and 0.08 <= g["delta"] <= 0.40 and s.bid >= 0.05 \
                    and mid / spot >= return_floors()[1]:
                ret = mid / spot * 100
                pc = _p_above(spot, k, iv, t)
                ccs.append({**base, "strat": "cc", "short": float(k), "long": None, "credit": float(mid), "nat": float(s.bid),
                            "prem": float(mid * 100), "ret": float(ret), "ann": float(ret * 365 / max(dte, 1)),
                            "p_called": float(pc), "cap_pct": float((k / spot - 1) * 100),
                            "if_called": float((k - spot + mid) / spot * 100), "be": float(spot - mid),
                            "delta": float(g["delta"]), "theta_day": float(-g["theta"] * 100), "iv": iv,
                            "oi": int(s.open_interest), "slip": float(slip), "above_wall": bool(a["call_wall"] and k >= a["call_wall"]),
                            "score": float(ret * 365 / max(dte, 1) * math.sqrt(max(1 - pc / 100, 0)) * (1 - min(slip, 30) / 100))})
        # ---- call debit spread (bull call): buy a near-the-money call, sell a higher one
        if win == "leaps":
            continue
        ks = list(c.index)
        for i, k1 in enumerate(ks):
            s1 = c.loc[k1]
            if s1.open_interest < min_oi:
                continue
            g1 = _call_greeks(spot, k1, t, float(s1.iv))
            if not (0.40 <= g1["delta"] <= 0.72):
                continue
            highs = ks[i + 1:]
            if not highs:
                continue
            picks = {min(highs, key=lambda x: abs((x - k1) - spot * tw)) for tw in (0.02, 0.04, 0.06, 0.09)}
            for k2 in picks:
                s2 = c.loc[k2]
                if s2.open_interest < min_oi // 2:
                    continue
                w = k2 - k1
                debit = (s1.bid + s1.ask) / 2 - (s2.bid + s2.ask) / 2
                nat = s1.ask - s2.bid
                if debit <= 0.05 or debit >= w * 0.85:
                    continue
                mp = w - debit
                be = k1 + debit
                pop = _p_above(spot, be, float(s1.iv), t)
                pmax = _p_above(spot, k2, float(s2.iv), t)
                slip = (nat - debit) / debit * 100
                if slip > 100:                     # wide outside market hours: kept, ranked down and flagged
                    continue
                rr = mp / debit
                spreads.append({**base, "strat": "spread", "long": float(k1), "short": float(k2), "width": float(w),
                                "debit": float(debit), "nat": float(nat), "cost": float(debit * 100), "maxprofit": float(mp * 100),
                                "rr": float(rr), "be": float(be), "be_pct": float((be / spot - 1) * 100), "pop": float(pop),
                                "p_max": float(pmax), "ev": float((pop / 100 * mp - (1 - pop / 100) * debit) * 100),
                                "l_iv": float(s1.iv), "s_iv": float(s2.iv), "iv": float(s1.iv), "slip": float(slip),
                                "ldelta": float(g1["delta"]), "sdelta": float(_call_greeks(spot, k2, t, float(s2.iv))["delta"]),
                                "otm_short": float((k2 / spot - 1) * 100), "call_wall_hit": bool(a["call_wall"] and k2 <= a["call_wall"]),
                                "oi": int(min(s1.open_interest, s2.open_interest)),
                                "score": float(pop / 100 * min(rr, 4) * (1 - min(slip, 100) / 160))})
    out = {}
    for name, lst, n in (("long", longs, nkeep or 12), ("spread", spreads, nkeep or 10), ("cc", ccs, nkeep or 10)):
        lst.sort(key=lambda x: -x["score"])
        keep, seen = [], set()
        for x in lst:
            key = (x["exp"], x.get("long"), x.get("short"))
            if key not in seen:
                seen.add(key)
                keep.append(x)
            if len(keep) >= n:
                break
        out[name] = keep
    return out


BCS_PROFILES = {
    "conservative": "Buy an in-the-money call (delta 0.58+), highest chance of profit · skips trades that hold through earnings",
    "balanced": "Best chance of profit × reward/risk · skips trades that hold through earnings",
    "aggressive": "Cheaper, more upside: reward/risk 1.5:1 or better, lower odds · earnings allowed",
}


def bull_call_setups(spot: float, df: pd.DataFrame, earn_date: str | None, ticker: str = "") -> dict:
    """Bull call (call debit) spreads for the ticker lookup / watchlist: 3 profiles x 3 expiration windows."""
    out = {}
    for w_ in week_keys(with_0dte=False, later=True):
        cands = _call_candidates(spot, df, w_, earn_date, nkeep=30)["spread"]
        prof = {}
        for name in BCS_PROFILES:
            if name == "conservative":
                ok = [c for c in cands if c["ldelta"] >= 0.58 and c["rr"] >= 0.6 and not c["earn_cross"]]
                ok.sort(key=lambda c: -(c["pop"] + 5 * min(c["rr"], 2)))
            elif name == "balanced":
                ok = [c for c in cands if c["rr"] >= 0.8 and not c["earn_cross"]]
                ok.sort(key=lambda c: -c["score"])
            else:
                ok = [c for c in cands if c["rr"] >= 1.5]
                ok.sort(key=lambda c: -(min(c["rr"], 6) * math.sqrt(c["pop"] / 100)))
            picks, seen = [], set()
            for c in ok:
                if (c["exp"], c["long"]) in seen:
                    continue
                seen.add((c["exp"], c["long"]))
                picks.append(dict(c, tp50=c["debit"] * 1.5, tp100=c["debit"] * 2))
                if len(picks) == 3:
                    break
            prof[name] = picks
        add_call_odds(spot, [c for P in prof.values() for c in P], "bcs" + ticker + w_)
        out[w_] = {"profiles": prof, "n": len(cands), "exps": sorted({c["exp"] for c in cands})}
    return out


def add_call_odds(spot: float, cands: list, seed_key: str = "") -> None:
    """Long calls / call debit spreads: chance the position is up 50% and up 100% at some close before expiration,
    and the median days to +50%. Simulated price paths at the expiration's ATM IV; legs re-priced at their own IV."""
    by_exp: dict = {}
    for c in cands:
        if c.get("dte") and c.get("atm_iv") and c.get("l_iv"):
            by_exp.setdefault((c["exp"], c["dte"], c["atm_iv"]), []).append(c)
    today = et_now().date()
    for (exp, dte, sig), group in by_exp.items():
        steps = dte if dte <= 120 else int(math.ceil(dte / 5))
        dt = dte / steps / 365
        rng = np.random.default_rng(zlib.crc32(f"{seed_key}|{exp}|call".encode()))
        z = rng.standard_normal((P50_PATHS // 2, steps))
        z = np.vstack([z, -z])
        paths = spot * np.exp(np.cumsum((0.04 - 0.5 * sig * sig) * dt + sig * math.sqrt(dt) * z, axis=1))
        # every path x every step in one pass (same maths as _bs_call), and each leg priced once per expiration even
        # when it appears in several spreads - the step-by-step loop was most of the calls scan's time
        tv = np.array([max(dte / 365 - (d + 1) * dt, 0.0) for d in range(steps)])
        live_t = tv > 1e-9
        tsafe = np.where(live_t, tv, 1.0)
        sq_t = np.array([math.sqrt(t) for t in tsafe])
        disc_t = np.array([math.exp(-0.04 * t) for t in tsafe])
        days_at = np.array([(d + 1) * dt * 365 for d in range(steps)])
        memo: dict = {}

        def leg(K, iv):
            k = (K, iv)
            if k not in memo:
                if len(memo) >= 10:
                    memo.clear()
                sd = iv * sq_t
                d1 = (np.log(paths / K) + (0.04 + 0.5 * iv * iv) * tsafe) / sd
                memo[k] = np.where(live_t, paths * _ncdf(d1) - K * disc_t * _ncdf(d1 - sd), np.maximum(paths - K, 0.0))
            return memo[k]
        for c in group:
            def val(S, tau):
                v = _bs_call(S, c["long"], tau, c["l_iv"])
                return v - (_bs_call(S, c["short"], tau, c["s_iv"]) if c.get("short") is not None else 0)
            v0 = float(val(np.array([spot]), dte / 365)[0])
            if v0 <= 0:
                continue
            v = leg(c["long"], c["l_iv"])
            if c.get("short") is not None:
                v = v - leg(c["short"], c["s_iv"])
            ok50 = v >= 1.5 * v0
            h50 = np.where(ok50.any(axis=1), days_at[ok50.argmax(axis=1)], -1.0)
            h100 = (v >= 2 * v0).any(axis=1)
            hits = h50[h50 > 0]
            c["p_up50"] = float(len(hits) / len(h50) * 100)
            c["p_2x"] = float(h100.mean() * 100)
            c["d_up50"] = float(np.median(hits)) if len(hits) else None
            c["d_up50_date"] = _tday(today, c["d_up50"], c["exp"]) if len(hits) else None


def _calls_universe(custom: list | None) -> list:
    if custom:
        return [{"ticker": t, "kind": "Your list", "sector": ""} for t in custom][:CALL_UNIVERSE]
    cfg = load_config()
    res = _read_json(SCAN_PATH, {}) or {}
    rows = [r for r in (res.get("rotation") or []) + (res.get("stock_rotation") or []) if (r.get("trend") or 0) >= 2]
    rows.sort(key=lambda r: -(r.get("mom") or 0))
    uni, seen = [], set()
    for t in [w["ticker"] for w in cfg.get("watchlist", [])][:12]:
        if t not in seen:
            seen.add(t)
            uni.append({"ticker": t, "kind": "Watchlist", "sector": "Watchlist"})
    for t in ("SPY", "QQQ"):
        if t not in seen:
            seen.add(t)
            uni.append({"ticker": t, "kind": "ETF", "sector": "Benchmark"})
    for r in rows:
        if len(uni) >= CALL_UNIVERSE:
            break
        if r["ticker"] not in seen:
            seen.add(r["ticker"])
            uni.append({"ticker": r["ticker"], "kind": "Momentum", "sector": r.get("sector") or ""})
    return uni


def run_call_scan(custom: list | None = None) -> dict:
    _calls.update(running=True, error=None, progress="starting")
    try:
        uni = _calls_universe(custom)
        rot = {r["ticker"]: r for r in ((_read_json(SCAN_PATH, {}) or {}).get("rotation") or [])
               + ((_read_json(SCAN_PATH, {}) or {}).get("stock_rotation") or [])}
        try:
            earn = nasdaq_earnings()
        except Exception:
            earn = None

        def one(u):
            tk = u["ticker"]
            try:
                spot, df, meta = load_chain(tk)
            except Exception as e:
                return tk, None, str(e)
            if meta.get("stale"):
                return tk, None, "stale:" + str(meta.get("chain_time") or "unknown")
            ed = earn.get(tk) if earn else None
            wins = {w_: _call_candidates(spot, df, w_, ed) for w_ in week_keys(with_0dte=False, later=True, leaps=True)}
            add_call_odds(spot, [c for w_ in wins.values() for c in w_["long"] + w_["spread"]], tk)
            r = rot.get(tk) or {}
            rsi, trend = r.get("rsi"), r.get("trend")
            if rsi is None:
                try:
                    h = scan_history(tk)
                    rsi, _ = rsi14(tk, 0, "", h)
                    cl = pd.to_numeric(h["close"], errors="coerce").dropna()
                    if len(cl) >= 50:
                        s20, s50, last = cl.tail(20).mean(), cl.tail(50).mean(), cl.iloc[-1]
                        trend = int(last > s20) + int(last > s50) + int(s20 > s50)
                except Exception:
                    pass
            return tk, {**u, "price": spot, "rsi": rsi, "trend": trend, "mom": r.get("mom"), "quad": r.get("quad"),
                        "rs1": r.get("rs1"), "earnings": ed, "quotes_as_of": meta["last_trade"], "windows": wins,
                        "src": meta.get("source")}, None

        items, errs = {}, {}
        with ThreadPoolExecutor(6) as ex:
            for n_, (tk, it, err) in enumerate(ex.map(one, uni), 1):
                _calls["progress"] = f"option chains {n_}/{len(uni)}"
                if it:
                    items[tk] = it
                else:
                    errs[tk] = err
        res = _clean(json.loads(json.dumps({"ts": et_now().strftime("%Y-%m-%d %H:%M ET"), "screened": len(uni),
                                            "custom": bool(custom), "earnings_ok": earn is not None,
                                            "items": items, "errors": errs,
                                            "stale": {k: v[6:] for k, v in errs.items() if str(v).startswith("stale:")}},
                                           default=str)))
        with _io_lock:
            _write_json(CALL_PATH, res)
        _calls["progress"] = f"done {res['ts']}"
        return res
    except Exception as e:
        _calls["error"] = str(e)
        raise
    finally:
        _calls["running"] = False


def _calls_bg(custom: list | None = None) -> None:
    try:
        _queued(_calls, "calls scan", run_call_scan, custom)
    except Exception as e:
        _calls.update(running=False, error=str(e))


def chain_view(ticker: str, exp: str | None = None) -> dict:
    """Strikes for the options calculator: one expiration's calls and puts near the money."""
    tk = clean_ticker(ticker)
    spot, df, meta = chain_cached(tk, 120)
    today = pd.Timestamp(et_today())
    exps = sorted(df["expiration"].unique())
    lst = [{"exp": pd.Timestamp(e).strftime("%Y-%m-%d"), "dte": int((pd.Timestamp(e) - today).days)} for e in exps]
    if not lst:
        raise ValueError("no expirations")
    sel = exp if exp in [x["exp"] for x in lst] else next((x["exp"] for x in lst if x["dte"] >= 25), lst[-1]["exp"])
    sub = df[df["expiration"] == pd.Timestamp(sel)]
    sub = sub[(sub["strike"] >= spot * 0.5) & (sub["strike"] <= spot * 1.6)]
    rows = {}
    for s in sub.itertuples():
        side = "calls" if s.type == "C" else "puts"
        rows.setdefault(side, {})[float(s.strike)] = {"k": float(s.strike), "bid": float(s.bid), "ask": float(s.ask),
                                                     "iv": float(s.iv), "delta": float(s.delta), "oi": int(s.open_interest)}
    return {"ticker": tk, "spot": spot, "as_of": meta["last_trade"], "exps": lst, "exp": sel,
            "dte": next(x["dte"] for x in lst if x["exp"] == sel),
            "calls": sorted(rows.get("calls", {}).values(), key=lambda x: x["k"]),
            "puts": sorted(rows.get("puts", {}).values(), key=lambda x: x["k"])}


# ---------------------------------------------------------------- saved calculator trades
CALC_PATH = APP_DIR / "saved_calcs.json"
CALC_STRATS = {"buycall", "leaps", "buyput", "csp", "cc", "pcs", "cds", "ccs", "pds"}
CALC_LEGS = {"buycall": [("C", 1, "k1", "p1")], "leaps": [("C", 1, "k1", "p1")], "buyput": [("P", 1, "k1", "p1")],
             "csp": [("P", -1, "k1", "p1")], "cc": [("S", 1, None, "sb"), ("C", -1, "k1", "p1")],
             "pcs": [("P", -1, "k1", "p1"), ("P", 1, "k2", "p2")], "cds": [("C", 1, "k1", "p1"), ("C", -1, "k2", "p2")],
             "ccs": [("C", -1, "k1", "p1"), ("C", 1, "k2", "p2")], "pds": [("P", 1, "k1", "p1"), ("P", -1, "k2", "p2")]}


def load_calcs() -> list:
    v = _read_json(CALC_PATH, [])
    return v if isinstance(v, list) else []


def _fnum(x, lo: float = -1e9, hi: float = 1e9) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and lo <= f <= hi else None


def save_calcs(body: dict) -> list:
    items = load_calcs()
    if body.get("remove"):
        items = [c for c in items if c.get("id") != body["remove"]]
    if body.get("note") and isinstance(body["note"], dict):
        for c in items:
            if c.get("id") == body["note"].get("id"):
                c["note"] = str(body["note"].get("text", ""))[:200]
    if body.get("add"):
        a = body["add"]
        strat = str(a.get("strat", ""))
        if strat not in CALC_STRATS:
            raise ValueError("unknown strategy")
        tk = str(a.get("tk") or "").strip().upper()
        tk = clean_ticker(tk) if tk else ""
        exp = str(a.get("exp") or "")
        if exp and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
            raise ValueError("bad expiration")
        S, dte, iv, qty = _fnum(a.get("S"), 0.01, 1e7), _fnum(a.get("dte"), 0, 3650), _fnum(a.get("iv"), 1, 1000), _fnum(a.get("qty"), 1, 10000)
        if None in (S, dte, iv, qty):
            raise ValueError("check stock price, days, IV and contracts")
        v = {}
        for k in ("k1", "p1", "k2", "p2", "sb"):
            f = _fnum((a.get("v") or {}).get(k), 0, 1e7)
            if f is not None:
                v[k] = f
        for t_, s_, kk, pk in CALC_LEGS[strat]:
            if (kk and kk not in v) or pk not in v:
                raise ValueError("missing a strike or premium")
        snap = {k: _fnum((a.get("snap") or {}).get(k)) for k in ("net", "mx", "mn", "pop", "cap")}
        snap["bes"] = [b for b in ((_fnum(x, 0, 1e7)) for x in (a.get("snap") or {}).get("bes", [])[:3]) if b is not None]
        snap["mx_unl"] = bool((a.get("snap") or {}).get("mx_unl"))
        snap["mn_unl"] = bool((a.get("snap") or {}).get("mn_unl"))
        items.insert(0, {"id": secrets.token_hex(4), "saved": et_now().strftime("%Y-%m-%d %H:%M ET"), "tk": tk, "strat": strat,
                         "exp": exp or None, "S": S, "dte": int(dte), "iv": iv, "qty": int(qty), "v": v,
                         "ivk": {k: f for k, f in ((k, _fnum(x, 1, 1000)) for k, x in (a.get("ivk") or {}).items()
                                                   if k in ("k1", "k2")) if f is not None},
                         "snap": snap, "note": str(a.get("note", ""))[:200]})
    if len(items) > 300:
        raise ValueError("too many saved trades (300) — delete some first")
    with _io_lock:
        _write_json(CALC_PATH, items)
    return items


def calcs_live(items: list) -> dict:
    """Current mid prices for saved trades that have a ticker + expiration: value now and P/L vs the saved prices."""
    out, chains = {}, {}
    for c in items[:60]:
        if not c.get("tk") or not c.get("exp"):
            continue
        tk = c["tk"]
        if tk not in chains:
            if len(chains) >= 20:
                continue
            try:
                chains[tk] = chain_cached(tk, 120)
            except Exception as e:
                chains[tk] = e
        ch = chains[tk]
        if isinstance(ch, Exception):
            out[c["id"]] = {"error": str(ch)}
            continue
        spot, df, meta = ch
        sub = df[df["expiration"] == pd.Timestamp(c["exp"])]
        if sub.empty:
            out[c["id"]] = {"spot": spot, "error": "expired" if c["exp"] < et_now().date().isoformat() else "expiration not found"}
            continue
        pl, ok, legs = 0.0, True, []
        for t_, s_, kk, pk in CALC_LEGS[c["strat"]]:
            if t_ == "S":
                pl += s_ * (spot - c["v"][pk])
                continue
            row = sub[(sub["type"] == t_) & (sub["strike"] == c["v"][kk])]
            if row.empty:
                ok = False
                break
            r0 = row.iloc[0]
            mid = (r0.bid + r0.ask) / 2 if r0.ask > 0 else float(r0.bid)
            legs.append({"k": c["v"][kk], "t": t_, "mid": float(mid), "bid": float(r0.bid), "ask": float(r0.ask)})
            pl += s_ * (mid - c["v"][pk])
        if not ok:
            out[c["id"]] = {"spot": spot, "error": "strike not in the chain"}
            continue
        pl *= 100 * c["qty"]
        cap = (c.get("snap") or {}).get("cap")
        out[c["id"]] = {"spot": spot, "pl": pl, "pl_pct": pl / cap * 100 if cap else None, "legs": legs,
                        "as_of": meta["last_trade"],
                        "dte_now": int((pd.Timestamp(c["exp"]) - pd.Timestamp(et_now().date())).days)}
    return out


# ---------------------------------------------------------------- Strategy Scanner picks (local, read from its output)
# 2026-11-18b: the strategy scanner is part of GEX Walls now (APP_DIR/strategy, copied from the old separate
# StrategyScanner folder with its pick history). This app runs its full scan, universe rebuild and live re-prices;
# the separate Windows tasks are off. STRATEGY_SCANNER_DIR still overrides the folder.
SS_DIR = Path(os.environ.get("STRATEGY_SCANNER_DIR", str(APP_DIR / "strategy")))
_picks = {"running": False, "started": None, "finished": None, "error": None, "last_out": ""}
_ssfull = {"running": False, "started": None, "finished": None, "error": None, "last_out": "", "kind": ""}


def _ss_python() -> Path:
    """Python for the strategy scanner: this app's own .venv (numpy, requests, tzdata), else the running Python."""
    for c in (APP_DIR / ".venv" / "Scripts" / "python.exe", APP_DIR / ".venv" / "bin" / "python", SS_DIR / ".venv" / "Scripts" / "python.exe"):
        if c.exists():
            return c
    return Path(sys.executable)


def _ss_full_date() -> str:
    """ET date of the last FULL scan (the live re-price keeps full_generated from it)."""
    meta = (_read_json(SS_DIR / "output" / "latest.json", {}) or {}).get("meta") or {}
    ts = meta.get("full_generated") or (meta.get("generated_utc") if meta.get("mode") != "fast" else None)
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(et_now().tzinfo).date().isoformat()
    except ValueError:
        return str(ts)[:10]


def _run_ss_job(kind: str, args: list, timeout: int) -> None:
    py = _ss_python()
    _ssfull.update(running=True, error=None, kind=kind, started=et_now().strftime("%Y-%m-%d %H:%M ET"))
    try:
        if _scanner_running():
            for _ in range(60):
                time.sleep(30)
                if not _scanner_running():
                    break
        (SS_DIR / "logs").mkdir(exist_ok=True)
        fo_p, fe_p = SS_DIR / "logs" / f"{kind}_last.out", SS_DIR / "logs" / f"{kind}_last.err"
        with open(fo_p, "w", encoding="utf-8") as fo, open(fe_p, "w", encoding="utf-8") as fe:
            r = subprocess.run([str(py), "-u", *args], cwd=str(SS_DIR), stdout=fo, stderr=fe, timeout=timeout,
                               check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = fo_p.read_text(encoding="utf-8", errors="replace") + fe_p.read_text(encoding="utf-8", errors="replace")
        _ssfull["last_out"] = "\n".join(out.strip().splitlines()[-3:])
        if r.returncode != 0:
            raise RuntimeError(f"{kind} exited {r.returncode}: " + " | ".join(out.strip().splitlines()[-3:]))
    except Exception as e:
        _ssfull["error"] = str(e)
    finally:
        _ssfull.update(running=False, finished=et_now().strftime("%Y-%m-%d %H:%M ET"))


SS_FULL_AT = (6, 40)        # weekdays, ET (= 1:40 AM HST, as the old Windows task) - prior-close quotes
SS_UNIVERSE_AT = (8, 0)     # Sundays, ET (= 3:00 AM HST)


def strategy_loop() -> None:
    """Full strategy scan each weekday morning (catches up if the PC was off at 6:40 ET, until 15:00 ET) and the
    weekly universe rebuild on Sundays - formerly the separate 'StrategyScanner Daily/Universe' Windows tasks."""
    time.sleep(90)                                          # let the app finish starting
    while True:
        try:
            now = et_now()
            hm = (now.hour, now.minute)
            if SS_DIR.exists() and not _ssfull["running"] and not _picks["running"]:
                try:
                    if lock_today_picks():
                        locked_update(force=True)
                    else:
                        locked_update()
                except Exception:
                    pass
            if SS_DIR.exists() and not _ssfull["running"] and not _picks["running"]:
                if now.weekday() < 5 and SS_FULL_AT <= hm < (15, 0) and _ss_full_date() != now.date().isoformat():
                    if not no_data_source():
                        _run_ss_job("full", ["run_daily.py"], 7200)
                elif now.weekday() == 6 and hm >= SS_UNIVERSE_AT:
                    uf = SS_DIR / "universe_auto.txt"
                    age_d = (time.time() - uf.stat().st_mtime) / 86400 if uf.exists() else 99
                    if age_d > 5:
                        _queued(_ssfull, "strategy universe rebuild", _run_ss_job, "universe",
                                ["tools/build_universe.py"], 7200)
        except Exception as e:
            _ssfull["error"] = str(e)
        time.sleep(300)


def picks_data() -> dict:
    """Latest Strategy Scanner results (output/latest.json + the week-ahead calendar), trimmed for the Picks tab."""
    lp = SS_DIR / "output" / "latest.json"
    if not lp.exists():
        return {"error": f"Strategy Scanner output not found ({lp})", "status": _picks}
    d = _read_json(lp, {}) or {}
    top = d.get("top") or {}
    tks = {p.get("ticker") for v in top.values() for p in (v or [])}
    ev = _read_json(SS_DIR / "output" / "events_week.json", {}) or {}
    return {"meta": d.get("meta") or {}, "top": top, "catalysts": d.get("catalysts") or {},
            "learning": {k: v for k, v in (d.get("learning") or {}).items() if k != "by_group"} | {
                "by_group": (d.get("learning") or {}).get("by_group") or []},
            "tickers": {k: v for k, v in (d.get("tickers") or {}).items() if k in tks},
            "events": ev.get("days") or [], "events_status": ev.get("status") or {},
            "file_time": datetime.fromtimestamp(lp.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "my_results": trade_results(), "status": _picks, "locked": locked_view()}


def picks_trades(tf: str, strat: str, limit: int = 300) -> dict:
    """The individual finished picks behind one row of "How the picks have done" (Strategy Scanner DB, read-only)."""
    db = SS_DIR / "data" / "scanner.db"
    if not db.exists():
        return {"error": f"Strategy Scanner database not found ({db})"}
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("""SELECT date, ticker, expiry, dte, legs, price, credit, capital, pop_cons, settle_price, pnl,
                                     pnl_pct, win, hit50, hit50_date, resolved_date FROM picks
                              WHERE status='closed' AND timeframe=? AND strategy=? AND COALESCE(roc, 0) >= 0.0999999
                              ORDER BY expiry DESC, date DESC LIMIT ?""",
                           (str(tf)[:20], str(strat)[:20], int(limit))).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            legs = [{"cp": l.get("cp"), "K": l.get("K"), "side": l.get("side")} for l in json.loads(r["legs"] or "[]")]
        except ValueError:
            legs = []
        d = dict(r)
        d["legs"] = legs
        out.append(d)
    return _clean({"tf": tf, "strategy": strat, "trades": out})


def _scanner_running() -> bool:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                              "Where-Object { $_.CommandLine -match 'run_daily.py' } | Measure-Object | % Count"],
                             capture_output=True, text=True, timeout=30, check=False)
        return out.stdout.strip() not in ("", "0")
    except Exception:
        return False


def run_picks_refresh(alert: bool = False) -> None:
    """Re-price the picks, queued behind any other CBOE-heavy job."""
    why = no_data_source()
    if why:
        _picks.update(running=False, error=why, finished=et_now().strftime("%H:%M ET"))
        return
    _picks.update(running=True, error=None, started=et_now().strftime("%H:%M ET"), progress="running")
    _run_picks_refresh(alert)


def _run_picks_refresh(alert: bool = False) -> None:
    """Live re-price of the scanner's shortlist (run_daily.py --fast, ~1-2 min), then apply today's catalyst review."""
    py = _ss_python()
    _picks.update(running=True, error=None, started=et_now().strftime("%H:%M ET"))
    try:
        if _scanner_running():
            for _ in range(60):                          # a full scan is running: wait for it (up to 30 min)
                time.sleep(30)
                if not _scanner_running():
                    break
        (SS_DIR / "logs").mkdir(exist_ok=True)
        with open(SS_DIR / "logs" / "fast_last.out", "w", encoding="utf-8") as fo, \
                open(SS_DIR / "logs" / "fast_last.err", "w", encoding="utf-8") as fe:
            r = subprocess.run([str(py), "-u", "run_daily.py", "--fast"], cwd=str(SS_DIR), stdout=fo, stderr=fe,
                               timeout=1800, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (SS_DIR / "logs" / "fast_last.out").read_text(encoding="utf-8", errors="replace")
        _picks["last_out"] = "\n".join(out.strip().splitlines()[-3:])
        if r.returncode != 0 or "Done in" not in out:
            err = (SS_DIR / "logs" / "fast_last.err").read_text(encoding="utf-8", errors="replace")
            raise RuntimeError("scanner did not finish: " + " | ".join((out + err).strip().splitlines()[-3:]))
        meta = (_read_json(SS_DIR / "output" / "latest.json", {}) or {}).get("meta") or {}
        cat = SS_DIR / "output" / f"catalysts_{meta.get('run_date')}.json"
        if cat.exists():
            subprocess.run([str(py), "tools/add_catalysts.py", str(cat)], cwd=str(SS_DIR), capture_output=True,
                           timeout=300, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if alert:
            d = picks_data()
            lines = []
            for tf in ("daily", "weekly", "monthly", "leaps"):
                p = ((d.get("top") or {}).get(tf) or [None])[0]
                if p:
                    lines.append(f"{tf.title()}: {p['ticker']} ${p.get('price', 0):.2f} RSI {p.get('rsi14') or 0:.0f} | "
                                 f"{p.get('strategy_name')} {p.get('legs')} {p.get('expiry')} | "
                                 f"POP {(p.get('pop_cons') or 0) * 100:.0f}% | capital ${p.get('capital') or 0:,.0f} | "
                                 f"return {(p.get('roc') or 0) * 100:.0f}%")
            if lines:
                _emit([], load_config(), "PICKS", "picks", "warn", f"Picks updated {et_now():%H:%M} ET", "\n".join(lines))
        star_alerts()
        try:
            if lock_today_picks():
                locked_update(force=True)
            else:
                locked_update()
        except Exception as e:
            _picks["error"] = f"locking today's picks: {e}"
    except Exception as e:
        _picks["error"] = str(e)
    finally:
        _picks.update(running=False, finished=et_now().strftime("%H:%M ET"))


# ---------------------------------------------------------------- tracked (locked) picks
# The picks list re-prices every 30 minutes, so its strikes, credits and even tickers change during the day, and the
# scanner logged every version it ever showed (~500 a day) into its record. Now one set is LOCKED each trading day:
# the list from the first live re-price at or after 9:45 ET (the 9:50 run, so ~9:52). Each locked pick gets an ID
# (T1118-07 = Nov 18, 7th of the day) and keeps the exact legs, expiration, credit/debit, stock price and chance it
# had at that moment. Later refreshes never change it. "How the picks have done" grades only these, at the locked
# entry, settled at the official close on expiration day (same rules as the scanner). Stored in locked_picks.json.
LOCK_PATH = APP_DIR / "locked_picks.json"
LOCK_ARCHIVE = APP_DIR / "locked_picks_archive.jsonl"   # append-only; the working file above can be lost
LOCK_AT = (9, 45)
LOCK_TF = ("daily", "weekly", "monthly", "quarterly", "leaps")
_lock_io = threading.Lock()
_lock_upd = {"t": 0.0}


def _lock_legs(p: dict) -> list:
    """Exact legs as [{"side": "sell"|"buy", "cp": "P"|"C"|"STK", "K": float|None}] from a latest.json pick."""
    raw = p.get("legs")
    if isinstance(raw, list):
        src = raw
    else:
        txt = str(raw or "")
        if txt.strip().startswith("["):
            try:
                src = json.loads(txt)
            except ValueError:
                src = []
        else:
            src = [{"side": "sell" if m.group(1) in "−-" else "buy", "cp": m.group(3), "K": float(m.group(2))}
                   for m in re.finditer(r"([−+-])\s*\$?([\d.]+)\s*([CP])\b", txt)]
    out = [{"side": l.get("side"), "cp": l.get("cp"), "K": None if l.get("K") is None else float(l["K"])} for l in src]
    if p.get("strategy") in ("CC", "BW") and not any(l["cp"] == "STK" for l in out):
        out.insert(0, {"side": "buy", "cp": "STK", "K": None})
    return out


def _lock_pnl_ps(strategy: str, credit: float, price: float, legs: list, S: float, val) -> float:
    """P/L per share (same formula as the scanner's learn.position_pnl_per_share). val(leg) = the leg's value."""
    if strategy in ("CC", "BW"):
        call = next(l for l in legs if l["cp"] == "C")
        return (S - price) + credit - val(call)
    if strategy == "LCALL":
        return val(next(l for l in legs if l["cp"] == "C")) + credit
    cost = 0.0
    for l in legs:
        if l["cp"] in ("C", "P"):
            v = val(l)
            cost += v if l["side"] == "sell" else -v
    return credit - cost


def _intr(l: dict, S: float) -> float:
    return max(S - l["K"], 0.0) if l["cp"] == "C" else max(l["K"] - S, 0.0) if l["cp"] == "P" else 0.0


def _lock_load() -> dict:
    """The working state, rebuilt from the append-only archive when it is missing or behind.

    2026-11-25: the 2026-11-18 and 2026-11-23 locks were lost -- each new lock ended up as the only
    day in the file, so the track record could never accumulate. Every locked pick is now also
    appended to locked_picks_archive.jsonl, which is never rewritten, and the working file is
    repaired from it on load. A parse failure no longer silently starts from an empty record.
    """
    raw = LOCK_PATH.read_text(encoding="utf-8") if LOCK_PATH.exists() else ""
    try:
        d = json.loads(raw) if raw.strip() else {}
    except ValueError:
        d = {}
        if raw.strip():                                   # keep the damaged file for inspection
            try:
                LOCK_PATH.with_suffix(".bad.json").write_text(raw, encoding="utf-8")
            except OSError:
                pass
    d.setdefault("days", {})
    d.setdefault("picks", [])
    have = {p.get("id") for p in d["picks"]}
    arch, days = [], {}
    if LOCK_ARCHIVE.exists():
        for line in LOCK_ARCHIVE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                p = json.loads(line)
            except ValueError:
                continue
            if p.get("id") and p["id"] not in have:
                arch.append(p)
                have.add(p["id"])
            if p.get("date"):
                days.setdefault(p["date"], {"locked_at": p.get("locked_at"), "quotes_at": p.get("quotes_at"), "n": 0})
                days[p["date"]]["n"] += 1
    if arch:                                              # the archive knew about picks the file had lost
        d["picks"] = arch + d["picks"]
    for day, info in days.items():
        d["days"].setdefault(day, info)
    return d


def _lock_archive(picks: list) -> None:
    try:
        with LOCK_ARCHIVE.open("a", encoding="utf-8") as fh:
            for p in picks:
                fh.write(json.dumps(p, default=str) + "\n")
    except OSError:
        pass


def lock_today_picks() -> str | None:
    """Lock today's picks if it is time and they are not locked yet. Returns the lock time when it locks."""
    now = et_now()
    today = now.date().isoformat()
    if now.weekday() >= 5 or not market_open_now() or (now.hour, now.minute) < LOCK_AT:
        return None
    lp = SS_DIR / "output" / "latest.json"
    d = _read_json(lp, {}) or {}
    meta = d.get("meta") or {}
    qt = str(meta.get("quotes_last_trade") or "")
    if meta.get("market_closed") or qt[:10] != today or qt[11:16] < f"{LOCK_AT[0]:02d}:{LOCK_AT[1]:02d}":
        return None                                       # the list is not from today's live quotes yet
    with _lock_io:
        st = _lock_load()
        if today in st["days"]:
            return None
        md, n, seen, new = now.strftime("%m%d"), 0, set(), []
        for tf in LOCK_TF:
            for rank, p in enumerate((d.get("top") or {}).get(tf) or [], 1):
                key = f"{p.get('ticker')}|{p.get('strategy')}|{p.get('expiry')}|{p.get('legs')}"
                if key in seen:
                    continue
                seen.add(key)
                n += 1
                pop = p.get("pop_cal") if p.get("pop_cal") is not None else p.get("pop_cons")
                new.append({"id": f"T{md}-{n:02d}", "date": today, "locked_at": now.strftime("%H:%M ET"),
                            "quotes_at": qt[11:16] + " ET", "tf": tf, "rank": rank, "key": key,
                            "ticker": p.get("ticker"), "strategy": p.get("strategy"),
                            "strategy_name": p.get("strategy_name") or p.get("strategy"), "expiry": p.get("expiry"),
                            "dte": p.get("dte"), "legs_text": p.get("legs") if isinstance(p.get("legs"), str) else None,
                            "legs": _lock_legs(p), "credit": p.get("credit"), "price": p.get("price"),
                            "capital": p.get("capital"), "roc": p.get("roc"), "pop": pop, "p50": p.get("p50"),
                            "be_lo": p.get("be_lo"), "be_hi": p.get("be_hi"), "rsi14": p.get("rsi14"),
                            "ev": p.get("ev"), "ev_pass": p.get("ev_pass"), "ev_fail": p.get("ev_fail"),
                            "iv_short": p.get("iv_short"), "rv": p.get("rv"), "status": "open"})
        if not new:
            return None
        # concentration caps: the scanner's own record once held 1,217 positions and $367,900 of
        # capital on one expiration date, 68% of it pointing the same way, and one week's selloff
        # took the lot. A track record should model a portfolio somebody could actually hold.
        cfg_l = load_config()
        per_tk = int(cfg_l.get("lock_max_per_ticker") or 2)
        per_exp = int(cfg_l.get("lock_max_per_expiry") or 6)
        ntk, nexp, kept = {}, {}, []
        for p in new:
            tk, ex = p.get("ticker"), p.get("expiry")
            if ntk.get(tk, 0) >= per_tk or nexp.get(ex, 0) >= per_exp:
                continue
            ntk[tk] = ntk.get(tk, 0) + 1
            nexp[ex] = nexp.get(ex, 0) + 1
            kept.append(p)
        for i, p in enumerate(kept, 1):
            p["id"] = f"T{md}-{i:02d}"
        st["days"][today] = {"locked_at": now.strftime("%H:%M ET"), "quotes_at": qt[11:16] + " ET", "n": len(kept),
                             "considered": len(new), "run": meta.get("generated_utc")}
        st["picks"].extend(kept)
        _lock_archive(kept)
        _write_json(LOCK_PATH, st)
        new = kept
    return now.strftime("%H:%M ET")


def _lock_db_rows(con, p: dict) -> dict | None:
    """The scanner's own row for the same trade (same ticker, strategy, expiration and legs), for marks/settlement."""
    want = sorted((l["side"], l["cp"], round(l["K"], 3)) for l in p["legs"] if l["cp"] in ("C", "P"))
    best = None
    for r in con.execute("SELECT * FROM picks WHERE ticker=? AND strategy=? AND expiry=?",
                         (p["ticker"], p["strategy"], p["expiry"])):
        try:
            got = sorted((l["side"], l["cp"], round(float(l["K"]), 3)) for l in json.loads(r["legs"] or "[]")
                         if l.get("cp") in ("C", "P"))
        except (ValueError, TypeError):
            continue
        if got == want and (best is None or (r["last_mark_date"] or "") > (best["last_mark_date"] or "")):
            best = r
    return best


def _settle_close(tk: str, exp: str, db_row) -> float | None:
    """Official close on expiration day: the scanner's settlement if it has one, else daily history / Schwab."""
    if db_row is not None and db_row["status"] == "closed" and db_row["settle_price"]:
        return float(db_row["settle_price"])
    now = et_now()
    if exp > now.date().isoformat() or (exp == now.date().isoformat() and (now.hour, now.minute) < (16, 15)):
        return None
    try:
        h = scan_history(tk)
        rows = h[h["date"].astype(str).str[:10] <= exp]
        if len(rows) and (str(rows["date"].iloc[-1])[:10] == exp or (h["date"].astype(str).str[:10] > exp).any()):
            return float(rows["close"].iloc[-1])
    except Exception:
        pass
    try:
        b = live_bars([tk]).get(tk)
        if b and b["date"] == exp and not b.get("live"):
            return float(b["close"])
    except Exception:
        pass
    return None


def locked_update(force: bool = False) -> None:
    """Mark open locked picks from the scanner's latest marks and settle the expired ones (at most every 10 min)."""
    if not force and time.time() - _lock_upd["t"] < 600:
        return
    _lock_upd["t"] = time.time()
    db = SS_DIR / "data" / "scanner.db"
    with _lock_io:
        st = _lock_load()
        if not st["picks"]:
            return
        con = None
        try:
            if db.exists():
                con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
                con.row_factory = sqlite3.Row
            for p in st["picks"]:
                if p.get("status") != "open" or p.get("credit") is None:
                    continue
                r = _lock_db_rows(con, p) if con is not None else None
                cr, px, cap = float(p["credit"]), float(p.get("price") or 0), p.get("capital")
                if r is not None and r["last_mark"] is not None and r["last_mark_date"] and r["last_mark_date"] >= p["date"]:
                    mine = float(r["last_mark"]) + (cr - float(r["credit"]))
                    if p["strategy"] in ("CC", "BW"):
                        mine += float(r["price"] or 0) - px
                    pct = mine / (abs(cr) or 1e-9)
                    p.update(now_pnl=mine * 100, now_pct=pct, now_at=r["last_mark_date"])
                    p["max_pct"] = pct if p.get("max_pct") is None else max(p["max_pct"], pct)
                    if pct >= 0.5 and not p.get("hit50"):
                        p.update(hit50=r["last_mark_date"])
                S = _settle_close(p["ticker"], p["expiry"], r)
                if S is not None:
                    pnl = _lock_pnl_ps(p["strategy"], cr, px, p["legs"], S, lambda l: _intr(l, S)) * 100
                    p.update(status="closed", settle=S, pnl=pnl, pnl_pct=(pnl / cap if cap else None), win=pnl > 0,
                             resolved=et_now().date().isoformat())
        finally:
            if con is not None:
                con.close()
        _write_json(LOCK_PATH, st)


def lock_exposure(picks: list) -> dict:
    """Where the open tracked risk actually sits. The one number that mattered in November was how
    much capital sat on a single expiration date pointing a single way."""
    by_exp, by_tk, direction, ev = {}, {}, {"drop": 0.0, "rally": 0.0, "other": 0.0}, []
    for p in picks:
        if p.get("status") != "open":
            continue
        cap = float(p.get("capital") or 0)
        by_exp[p.get("expiry")] = by_exp.get(p.get("expiry"), 0.0) + cap
        by_tk[p.get("ticker")] = by_tk.get(p.get("ticker"), 0.0) + cap
        k = ("drop" if p.get("strategy") in ("PCS", "CSP", "IC")
             else "rally" if p.get("strategy") in ("CCS", "CC") else "other")
        direction[k] += cap
        if p.get("ev") is not None:
            ev.append(float(p["ev"]))
    tot = sum(by_exp.values())
    one_way = max(direction["drop"], direction["rally"]) / tot if tot else 0.0
    worst = max(by_exp.items(), key=lambda kv: kv[1]) if by_exp else (None, 0.0)
    return _clean({"total": tot, "by_expiry": by_exp, "worst_expiry": worst[0], "worst_expiry_capital": worst[1],
                   "worst_expiry_share": (worst[1] / tot) if tot else 0.0, "direction": direction,
                   "one_way_share": one_way, "n_open": sum(1 for p in picks if p.get("status") == "open"),
                   "top_tickers": dict(sorted(by_tk.items(), key=lambda kv: -kv[1])[:5]),
                   "ev_mean": (sum(ev) / len(ev)) if ev else None,
                   "ev_negative": sum(1 for x in ev if x <= 0), "ev_n": len(ev)})


def locked_view(days: int = 120) -> dict:
    """Today's lock, every open locked pick, the finished ones from the last `days` days, and the record by group."""
    st = _lock_load()
    now = et_now()
    today = now.date().isoformat()
    cut = (now.date() - timedelta(days=days)).isoformat()
    P = [p for p in st["picks"] if p.get("status") == "open" or (p.get("resolved") or p["date"]) >= cut]
    groups: dict = {}
    for p in st["picks"]:
        if p.get("status") != "closed":
            continue
        g = groups.setdefault((p["tf"], p["strategy"]), {"timeframe": p["tf"], "strategy": p["strategy"], "n": 0, "w": 0,
                                                           "m": 0, "pnl_pct": [], "pop": []})
        g["n"] += 1
        g["w"] += bool(p.get("win"))
        g["m"] += bool(p.get("win") or p.get("hit50"))
        if p.get("pnl_pct") is not None:
            g["pnl_pct"].append(p["pnl_pct"])
        if p.get("pop") is not None:
            g["pop"].append(p["pop"])
    G = [{"timeframe": g["timeframe"], "strategy": g["strategy"], "n": g["n"], "win_rate": g["w"] / g["n"],
          "managed_win_rate": g["m"] / g["n"], "avg_pnl_pct": (sum(g["pnl_pct"]) / len(g["pnl_pct"])) if g["pnl_pct"] else None,
          "avg_pop": (sum(g["pop"]) / len(g["pop"])) if g["pop"] else None} for g in groups.values()]
    lock_hm = f"{LOCK_AT[0]}:{LOCK_AT[1]:02d} ET"
    return _clean({"today": today, "locked_today": st["days"].get(today), "lock_at": lock_hm,
                   "exposure": lock_exposure(st["picks"]),
                   "clusters": len({(p.get("ticker"), p.get("expiry")) for p in st["picks"]
                                    if p.get("status") == "closed"}),
                   "last_day": max(st["days"]) if st["days"] else None, "days": st["days"], "picks": P, "by_group": G,
                   "n_open": sum(1 for p in st["picks"] if p.get("status") == "open"),
                   "n_closed": sum(1 for p in st["picks"] if p.get("status") == "closed")})


def star_alerts() -> None:
    """Alert once for every new ★★ pick (high return AND high chance of profit) in the Strategy Scanner output."""
    d = picks_data()
    if d.get("error"):
        return
    state = _read_json(STATE_PATH, {})
    seen = set(state.get("_star_seen") or [])
    cfg, fresh = load_config(), []
    for tf, L in (d.get("top") or {}).items():
        for p in L or []:
            pp = p.get("pop_cal") if p.get("pop_cal") is not None else p.get("pop_cons")
            if (p.get("roc") or 0) < 0.20 or pick_stars(pp, p.get("roc")) < 2:
                continue
            key = f"{p['ticker']}|{p.get('strategy')}|{p.get('expiry')}|{p.get('legs')}"
            if key in seen:
                continue
            seen.add(key)
            fresh.append(f"★★ {p['ticker']} ${p.get('price', 0):.2f} RSI {p.get('rsi14') or 0:.0f} | {p.get('strategy_name')} "
                         f"{p.get('legs')} {p.get('expiry')} | chance {pp * 100:.0f}% | return {p['roc'] * 100:.0f}% | "
                         f"capital ${p.get('capital') or 0:,.0f}")
    if fresh:
        _emit([], cfg, "PICKS", "stars", "warn", f"★★ {len(fresh)} standout pick{'s' if len(fresh) > 1 else ''}", "\n".join(fresh[:6]))
    state["_star_seen"] = sorted(seen)[-500:]
    with _io_lock:
        _write_json(STATE_PATH, state)


PICKS_SLOTS = tuple((h, m) for h in range(9, 16) for m in (20, 50) if (h, m) >= (9, 50))   # 9:50, 10:20 … 15:50 ET


def picks_loop() -> None:
    """Re-price the picks every 30 minutes while the market is open (9:50 … 15:50 ET; the full scan runs before the open
    on last-close quotes, which cannot change until options trade)."""
    done = set()
    while True:
        try:
            now = et_now()
            if now.weekday() < 5 and market_open_now() and SS_DIR.exists():
                for hh, mm in PICKS_SLOTS:
                    key = f"{now.date()} {hh}:{mm}"
                    if key not in done and (now.hour, now.minute) >= (hh, mm) and (now.hour * 60 + now.minute) - (hh * 60 + mm) < 60:
                        done.add(key)
                        if not _picks["running"]:
                            run_picks_refresh(alert=(hh, mm) == (9, 50))
        except Exception:
            pass
        time.sleep(60)


_auto = {"last_start": 0.0, "last_done": "", "secs": None, "running": False, "rounds": 0}
AUTO_EVERY_MIN = 10


def auto_round() -> None:
    """One rolling round: spreads & puts scan, sell puts, calls - one after another (they share one data allowance
    and one CPU core, so in turn is faster than together). Chains are shared between them."""
    _auto.update(running=True, last_start=time.time())
    try:
        for st, fn, args in ((_scan, _scan_bg, ()), (_csp, _csp_bg, ("all",)), (_calls, _calls_bg, (None,))):
            if st["running"]:
                continue                                   # already running (you pressed its button): don't double up
            st.update(running=True, progress="starting (automatic)", error=None)
            fn(*args)
        _auto.update(last_done=et_now().strftime("%H:%M ET"), secs=round(time.time() - _auto["last_start"]),
                     rounds=_auto["rounds"] + 1)
    finally:
        _auto["running"] = False


def auto_status() -> dict:
    cfg = load_config()
    on = bool(cfg.get("auto_refresh", True))
    every = max(4.0, float(cfg.get("auto_refresh_min") or AUTO_EVERY_MIN)) if _sw_live() else 120.0
    return {"on": on, "every_min": every, "market_open": market_open_now(), "running": _auto["running"],
            "last_done": _auto["last_done"], "secs": _auto["secs"], "rounds": _auto["rounds"],
            "next_s": max(0, round(_auto["last_start"] + every * 60 - time.time())) if on and market_open_now() and _auto["last_start"] else None,
            "stream": _stream.status(), "chains": dict(_chain_stat)}


def auto_refresh_loop() -> None:
    """Rolling refresh while the market is open (9:40-16:20 ET): a round of spreads -> sell puts -> calls starts every
    10 minutes ("auto_refresh_min" in alerts_config.json; minimum 4), so every list is re-priced on real-time Schwab
    quotes all day without pressing anything. Without Schwab (CBOE's delayed, rate-limited files) it runs every 2 hours.
    The daily-data screens keep their own schedules (early moves, squeeze, tax-loss); picks: picks_loop.
    Turn off with "auto_refresh": false."""
    while True:
        try:
            now = et_now()
            cfg = load_config()
            every = (max(4.0, float(cfg.get("auto_refresh_min") or AUTO_EVERY_MIN)) if _sw_live() else 120.0) * 60
            if now.weekday() < 5 and market_open_now() and (now.hour, now.minute) >= (9, 40) and cfg.get("auto_refresh", True) \
                    and not no_data_source() \
                    and time.time() - _auto["last_start"] >= every and not _auto["running"] \
                    and not (_rf["thread"] is not None and _rf["thread"].is_alive()):
                auto_round()
        except Exception:
            pass
        time.sleep(15)


# ---------------------------------------------------------------- mining stock earnings (Nasdaq.com calendar)
MINING_INDUSTRIES = {"Precious Metals", "Metal Mining", "Mining & Quarrying of Nonmetallic Minerals (No Fuels)",
                     "Coal Mining", "Other Metals and Minerals"}
MINING_EXTRA = {"ALB": "Metal Mining", "AA": "Metal Mining", "CENX": "Metal Mining", "SGML": "Metal Mining",
                "ATLX": "Metal Mining", "SBSW": "Precious Metals", "PLG": "Precious Metals", "DNN": "Other Metals and Minerals",
                "URG": "Other Metals and Minerals"}   # miners/producers the screener files under other industries
MINING_UNI_PATH = APP_DIR / "mining_universe.json"
MINING_EARN_PATH = APP_DIR / "mining_earnings.json"
_mining_lock = threading.Lock()


def mining_universe() -> dict:
    """US-listed miners (gold, silver, copper, uranium, lithium, coal, rare earths...) from Nasdaq.com's stock
    screener industry field, market cap >= $200M. Cached for a day."""
    c = _read_json(MINING_UNI_PATH, {}) or {}
    if c.get("fetched") == date.today().isoformat() and c.get("map"):
        return c["map"]
    r = _NqRows(nasdaq_stock_rows())
    rows = ((r.json() or {}).get("data") or {}).get("rows") or []
    if c.get("map") and rows and not any(x.get("industry") for x in rows[:400]):
        return c["map"]
    m = {}
    for x in rows:
        sym = str(x.get("symbol", "")).strip().upper()
        ind = x.get("industry") if x.get("industry") in MINING_INDUSTRIES else MINING_EXTRA.get(sym)
        if ind and (_num(x.get("marketCap")) or 0) >= 2e8 and re.fullmatch(r"[A-Z.]{1,6}", sym):
            m[sym] = {"name": x.get("name", ""), "industry": ind, "price": _num(x.get("lastsale")),
                      "cap": _num(x.get("marketCap")), "chg_pct": _num(x.get("pctchange"))}
    if m:
        with _io_lock:
            _write_json(MINING_UNI_PATH, {"fetched": date.today().isoformat(), "map": m})
    return m


def mining_earnings(days: int = 21, force: bool = False) -> dict:
    """Upcoming earnings for mining stocks over the next `days` calendar days, with price and RSI(14). Cached 6 h."""
    with _mining_lock:
        c = _read_json(MINING_EARN_PATH, {}) or {}
        if not force and c.get("days") == days and time.time() - c.get("ts", 0) < 6 * 3600 and c.get("rows") is not None:
            return c
        uni = mining_universe()
        start = et_now().date()
        dl = [start + timedelta(days=i) for i in range(days + 1)]
        dl = [d for d in dl if d.weekday() < 5]

        def one(d):
            try:
                r = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}", timeout=20,
                                 headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
                return d.isoformat(), ((r.json().get("data") or {}).get("rows")) or []
            except (requests.RequestException, ValueError, AttributeError):
                return d.isoformat(), None

        rows, failed = [], []
        with ThreadPoolExecutor(6) as ex:
            for ds, rr in ex.map(one, dl):
                if rr is None:
                    failed.append(ds)
                    continue
                for x in rr:
                    sym = str(x.get("symbol", "")).upper()
                    if sym in uni:
                        u = uni[sym]
                        rows.append({"date": ds, "symbol": sym, "name": x.get("name") or u["name"], "industry": u["industry"],
                                     "time": {"time-pre-market": "before open", "time-after-hours": "after close"}.get(
                                         x.get("time"), "time not given"),
                                     "eps_est": x.get("epsForecast") or "", "eps_last_year": x.get("lastYearEPS") or "",
                                     "n_est": x.get("noOfEsts") or "", "quarter": x.get("fiscalQuarterEnding") or "",
                                     "cap": u["cap"], "price": u["price"], "chg_pct": u.get("chg_pct")})

        def rsi_of(sym):
            try:
                h = scan_history(sym)
                v, _ = rsi14(sym, 0, "", h)
                return sym, v, float(h["close"].iloc[-1])
            except Exception:
                return sym, None, None
        with ThreadPoolExecutor(4) as ex:
            rs = {sym: (v, px) for sym, v, px in ex.map(rsi_of, sorted({r["symbol"] for r in rows}))}
        for r in rows:
            r["rsi"] = rs.get(r["symbol"], (None, None))[0]
            r["price"] = r["price"] or rs.get(r["symbol"], (None, None))[1]
        rows.sort(key=lambda r: (r["date"], -(r["cap"] or 0)))
        out = {"ts": time.time(), "asof": et_now().strftime("%Y-%m-%d %H:%M ET"), "days": days, "rows": rows,
               "universe": len(uni), "failed_days": failed}
        with _io_lock:
            _write_json(MINING_EARN_PATH, out)
        return out


# ---------------------------------------------------------------- sector ETF GEX history (for rotation: change vs previous / day / week / month)
SECTOR_GEX_PATH = APP_DIR / "sector_gex.json"
_sgex = {"running": False, "last": None}


def record_sector_gex() -> None:
    """One net-GEX snapshot per sector/theme ETF (CBOE delayed chains). Kept ~75 days."""
    if _sgex["running"]:
        return
    _sgex["running"] = True
    try:
        tks = [t for t in SCAN_ETFS if t != "SPY"]

        def one(tk):
            try:
                spot, df, meta = load_chain(tk)
                try:
                    record_gex_daily(tk, meta, spot, df)
                except Exception:
                    pass
                return tk, str(meta.get("last_trade") or "")[:19], float(df["gex"].sum()), float(spot)
            except Exception:
                return tk, None, None, None
        with ThreadPoolExecutor(3) as ex:
            res = list(ex.map(_bg_cboe(one), tks))
        with _io_lock:
            h = _read_json(SECTOR_GEX_PATH, {}) or {}
            cutoff = (et_now().date() - timedelta(days=75)).isoformat()
            for tk, ts, net, spot in res:
                if not ts or net is None or not re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", ts):
                    continue
                lst = h.setdefault(tk, [])
                if lst and lst[-1][0] == ts:
                    lst[-1] = [ts, net, spot]
                else:
                    lst.append([ts, net, spot])
                day_rows: dict = {}
                for row in lst:                        # keep at most 6 snapshots per day
                    day_rows.setdefault(row[0][:10], []).append(row)
                lst[:] = [r_ for d_, rr in sorted(day_rows.items()) if d_ >= cutoff for r_ in (rr if len(rr) <= 6 else rr[:5] + rr[-1:])]
            _write_json(SECTOR_GEX_PATH, h)
        _sgex["last"] = et_now().strftime("%Y-%m-%d %H:%M ET")
    finally:
        _sgex["running"] = False


def sector_gex_changes() -> dict:
    """Per ETF: current net GEX and the change vs the previous snapshot, the prior day's close,
    ~1 week ago and ~1 month ago (whichever snapshots exist)."""
    h = _read_json(SECTOR_GEX_PATH, {}) or {}
    out = {}
    for tk, lst in h.items():
        if not lst:
            continue
        cur = lst[-1]
        cd = date.fromisoformat(cur[0][:10])
        prev = next((r for r in reversed(lst[:-1]) if r[0] != cur[0]), None)
        day = next((r for r in reversed(lst) if r[0][:10] < cur[0][:10]), None)
        wk = next((r for r in reversed(lst) if r[0][:10] <= (cd - timedelta(days=7)).isoformat()), None)
        mo = next((r for r in reversed(lst) if r[0][:10] <= (cd - timedelta(days=28)).isoformat()), None)
        ch = lambda b: None if b is None else {"chg": cur[1] - b[1], "pct": (cur[1] - b[1]) / max(abs(b[1]), 1.0) * 100, "at": b[0]}
        out[tk] = {"net": cur[1], "at": cur[0], "spot": cur[2], "prev": ch(prev), "d1": ch(day), "w1": ch(wk), "m1": ch(mo),
                   "first": lst[0][0]}
    last = _sgex["last"]
    if not last and SECTOR_GEX_PATH.exists():          # after a restart: when the file was last written
        last = datetime.fromtimestamp(SECTOR_GEX_PATH.stat().st_mtime, et_now().tzinfo).strftime("%Y-%m-%d %H:%M ET")
    return {"etfs": out, "last_run": last, "running": _sgex["running"]}


def _sector_gex_quiet() -> None:
    try:
        record_sector_gex()
    except Exception:
        pass


def sector_gex_loop() -> None:
    """Snapshot sector ETF GEX a few times a day on weekdays: 10:05, 12:05, 14:05, 15:50 and 16:25 ET (close)."""
    done = set()
    if not SECTOR_GEX_PATH.exists():                     # first run: seed with the latest close
        try:
            record_sector_gex()
        except Exception:
            pass
    while True:
        try:
            now = et_now()
            if now.weekday() < 5:
                for hh, mm in ((10, 5), (12, 5), (14, 5), (15, 50), (16, 25)):
                    key = f"{now.date()} {hh}:{mm}"
                    m_now, m_t = now.hour * 60 + now.minute, hh * 60 + mm
                    if key not in done and 0 <= m_now - m_t < 50:
                        done.add(key)
                        record_sector_gex()
        except Exception:
            pass
        time.sleep(60)


# ---------------------------------------------------------------- best use of capital: every strategy, one table
_cap_cache: dict = {"ts": 0.0, "data": None}


def _cap_rows() -> tuple[list, dict]:
    """Normalize every candidate the app and the Strategy Scanner already found into one shape:
    capital tied up, chance of the winning outcome (p), gain % if it wins (g), loss % if it doesn't (l), days."""
    rows, src = [], {}

    floor_any = return_floors()[1]

    def add(**k):
        if k.get("capital") and k["capital"] > 0 and k.get("p") is not None and (k.get("g") or 0) >= floor_any - 1e-9:
            rows.append(k)

    # Strategy Scanner (credit spreads / iron condors), chance lowered by its own track record
    lj = _read_json(SS_DIR / "output" / "latest.json", {}) or {}
    if lj:
        src["Strategy Scanner"] = (lj.get("meta") or {}).get("run_date")
        cal = {(g["timeframe"], g["strategy"]): g["win_rate"] / g["avg_pop"]
               for g in ((lj.get("learning") or {}).get("by_group") or [])
               if g.get("n", 0) >= 10 and g.get("avg_pop") and g["win_rate"] < g["avg_pop"]}
        cats = ((lj.get("catalysts") or {}).get("tickers")) or {}
        for tf, L in (lj.get("top") or {}).items():
            for x in L or []:
                if x.get("strategy") not in ("PCS", "CCS", "IC") or not x.get("credit") or x["credit"] <= 0:
                    continue
                f = max(0.5, cal.get((tf, x["strategy"]), 1.0))
                cat = cats.get(x["ticker"]) or {}
                add(src="Strategy Scanner", type="credit", strat=x["strategy"], name=x.get("strategy_name"), ticker=x["ticker"],
                    price=x.get("price"), rsi=x.get("rsi14"), legs=x.get("legs"), exp=x.get("expiry"), dte=x.get("dte") or 0,
                    capital=x.get("capital"), collect=x["credit"] * 100, p=(x.get("pop_cons") or 0) * f, p_model=x.get("pop_cons"),
                    g=(x.get("roc") or 0), l=1.0, days=max(x.get("dte") or 0, 1),
                    flag=cat.get("note") if cat.get("risk") else None,
                    calc={"strat": x["strategy"].lower() if x["strategy"] in ("PCS", "CCS") else None})

    def sym(k): return f"{k:g}"
    # this app's put credit spread + short put scan
    sr = _read_json(SCAN_PATH, {}) or {}
    if sr.get("items"):
        src["Spread & put scan"] = sr.get("ts")
        for tk, it in sr["items"].items():
            px, rsi = it.get("price"), it.get("rsi")
            pool = (it.get("spreads") or []) + [c for key in ("dte0", "dte5", "weekly", "mid", "monthly") for c in (it.get(key) or [])[:15]]
            for c in pool:
                if True:
                    if c.get("pop", 0) < 60 or c["maxloss"] <= 0:
                        continue
                    add(src="Spread & put scan", type="credit", strat="PCS", name="Put credit spread", ticker=tk, price=px, rsi=rsi,
                        legs=f"−{sym(c['short'])}P / +{sym(c['long'])}P", exp=c["exp"], dte=c["dte"], capital=c["maxloss"],
                        collect=c["credit"] * 100, p=c["pop"] / 100, g=c["credit"] * 100 / c["maxloss"], l=1.0,
                        days=max(c["dte"], 1), flag="earnings before expiration" if it.get("earnings") and it["earnings"] <= c["exp"] else None,
                        calc={"strat": "pcs", "k1": c["short"], "k2": c["long"]},
                        quote={"legs": [[f"sell {sym(c['short'])}P", *c["q"][:2]], [f"buy {sym(c['long'])}P", *c["q"][2:]]],
                               "mid": c.get("mid"), "nat": c.get("nat"), "fill": c["credit"], "asof": it.get("quotes_as_of"),
                               "src": it.get("src")} if c.get("q") else None)
            for c in (it.get("puts") or []) + [c for key in ("puts0", "puts5", "putsw", "putsmid", "putsm") for c in (it.get(key) or [])[:10]]:
                _add_put(add, "Spread & put scan", tk, px, rsi, c, it.get("earnings"))
    cr = _read_json(CSP_PATH, {}) or {}
    if cr.get("items"):
        src["Sell puts scan"] = cr.get("ts")
        for tk, it in cr["items"].items():
            for c in (it.get("puts") or []) + [c for key in ("dte0", "dte5", "dte14", "dte45", "dte90") for c in (it.get(key) or [])[:10]]:
                _add_put(add, "Sell puts scan", tk, it.get("price"), it.get("rsi"), c, it.get("earnings"))
    # calls scan: long calls, bull call spreads, covered calls, LEAPS
    cl = _read_json(CALL_PATH, {}) or {}
    if cl.get("items"):
        src["Calls scan"] = cl.get("ts")
        for tk, it in cl["items"].items():
            px, rsi = it.get("price"), it.get("rsi")
            for w_, W in (it.get("windows") or {}).items():
                for c in (W.get("long") or [])[:6]:
                    if c.get("p_up50") is None:
                        continue
                    lp = w_ == "leaps"
                    add(src="Calls scan", type="leaps" if lp else "call", strat="LEAPS" if lp else "LC",
                        name="LEAPS call" if lp else "Buy a call", ticker=tk, price=px, rsi=rsi, legs=f"+{sym(c['long'])}C",
                        exp=c["exp"], dte=c["dte"], capital=c["cost"], collect=c["cost"] * 0.5, p=c["p_up50"] / 100, g=0.5, l=1.0,
                        days=max(c.get("d_up50") or c["dte"], 1), target="sell at +50%",
                        flag="earnings before expiration" if c.get("earn_cross") else None,
                        calc={"strat": "leaps" if lp else "buycall", "k1": c["long"]})
                for c in (W.get("spread") or [])[:6]:
                    if c.get("p_up50") is None:
                        continue
                    add(src="Calls scan", type="bcs", strat="BCS", name="Bull call spread", ticker=tk, price=px, rsi=rsi,
                        legs=f"+{sym(c['long'])}C / −{sym(c['short'])}C", exp=c["exp"], dte=c["dte"], capital=c["cost"],
                        collect=c["cost"] * 0.5, p=c["p_up50"] / 100, g=0.5, l=1.0, days=max(c.get("d_up50") or c["dte"], 1),
                        target=f"sell at +50% (max +${c['maxprofit']:,.0f})",
                        flag="earnings before expiration" if c.get("earn_cross") else None,
                        calc={"strat": "cds", "k1": c["long"], "k2": c["short"]})
                for c in (W.get("cc") or [])[:6]:
                    if not px or not c.get("iv"):
                        continue
                    t = max(c["dte"], 1) / 365
                    cap = px * 100 - c["prem"]
                    em = px * c["iv"] * math.sqrt(t) * 100
                    add(src="Calls scan", type="cc", strat="CC", name="Covered call (buy 100 sh)", ticker=tk, price=px, rsi=rsi,
                        legs=f"100 sh + −{sym(c['short'])}C", exp=c["exp"], dte=c["dte"], capital=cap, collect=c["prem"],
                        p=_p_above(px, c["be"], c["iv"], t) / 100, g=c["prem"] / cap, l=min(1.0, em / cap),
                        days=max(c["dte"], 1), target=f"if called +{c['if_called']:.1f}%",
                        flag="earnings before expiration" if c.get("earn_cross") else None,
                        calc={"strat": "cc", "k1": c["short"]})
    return rows, src


def _add_put(add, source, tk, px, rsi, c, earn):
    if c.get("pop", 0) < 60 or not c.get("cash"):
        return
    iv = c.get("s_iv") or c.get("atm_iv") or 0
    em = (px or c["short"]) * iv * math.sqrt(max(c["dte"], 1) / 365) * 100     # one expected move, per contract
    add(src=source, type="put", strat="CSP", name="Sell a put (cash-secured)", ticker=tk, price=px, rsi=rsi,
        legs=f"−{c['short']:g}P", exp=c["exp"], dte=c["dte"], capital=c["cash"], collect=c["prem"], p=c["pop"] / 100,
        g=c["prem"] / c["cash"], l=min(1.0, em / c["cash"]) if em else 0.15, days=max(c["dte"], 1),
        flag="earnings before expiration" if earn and earn <= c["exp"] else None, calc={"strat": "csp", "k1": c["short"]},
        quote={"legs": [[f"sell {c['short']:g}P", c.get("bid"), c.get("ask")]], "mid": c.get("mid"), "nat": c.get("nat"),
               "fill": c.get("credit"), "asof": c.get("quotes_as_of"), "src": c.get("src")} if c.get("bid") is not None else None)


SWING_V2 = "2026-11-12"          # swing setups rebuilt on back-tested rules; the old list went 1 for 32


def _swing_rows() -> list:
    """Live swing setups from the edge engine (run_edge): fixed rules, each with its own back-test."""
    d = _read_json(EDGE_PATH, None)
    if not d:
        d = run_edge(fetch=False)
    return list(d.get("swing") or [])


_swing_trk = {"ts": 0.0, "v": None}


def _swing_track() -> tuple | None:
    """The app's own graded swing record (audit/picks_log.jsonl: target before stop = win; picks paying < 10% not
    counted). (won, n) once 10+ are closed, else None. Cached 10 min."""
    if time.time() - _swing_trk["ts"] < 600:
        return _swing_trk["v"]
    v = None
    try:
        won = n = 0
        for ln in (AUDIT_DIR / "picks_log.jsonl").read_text(encoding="utf-8").splitlines():
            x = json.loads(ln) if ln.strip() else {}
            if x.get("type") == "swing" and x.get("status") == "closed" and x.get("price") \
                    and str(x.get("logged") or "") >= SWING_V2:
                n += 1
                won += bool(x.get("win"))
        v = (won, n) if n >= 10 else None
    except Exception:
        v = None
    _swing_trk.update(ts=time.time(), v=v)
    return v


def capital_data(force: bool = False) -> dict:
    if not force and _cap_cache["data"] and time.time() - _cap_cache["ts"] < 300:
        return _cap_cache["data"]
    rows, src = _cap_rows()
    today = et_now().date()
    keep = []
    for r in rows:                      # scans can be days old: re-date, drop anything already expired
        try:
            dte_now = (date.fromisoformat(str(r["exp"])[:10]) - today).days
        except Exception:
            continue
        if dte_now < 0:
            continue
        if dte_now != r["dte"]:
            if r["type"] in ("credit", "put", "cc"):
                r["days"] = max(dte_now, 1)
            else:
                r["days"] = max(1, min(r["days"], dte_now or 1))
            r["dte"] = dte_now
        keep.append(r)
    rows = keep
    for r in rows:
        r["ev"] = r["p"] * r["g"] - (1 - r["p"]) * r["l"]
        r["evd"] = r["ev"] / r["days"]
    # trim: the best 2 per ticker, strategy and expiry bucket by efficiency, plus the highest-chance one
    bk = lambda d_: 0 if d_ == 0 else 1 if d_ <= 7 else 2 if d_ <= 14 else 3 if d_ <= 30 else 4
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["ticker"], r["type"], bk(r["dte"])), []).append(r)
    rows = []
    for g in groups.values():
        g.sort(key=lambda r: -r["evd"])
        pick = g[:2]
        hp = max(g, key=lambda r: r["p"])
        if hp not in pick:
            pick.append(hp)
        rows.extend(pick)
    try:
        swing = _swing_rows()
    except Exception as e:
        swing, src["swing_error"] = [], str(e)
    trk = _swing_track()
    if trk:                             # blend the back-test with the app's own live record since the rebuild
        won_, n_ = trk
        for x in swing:
            x["p_model"] = x["p"]
            x["p"] = (won_ + 40 * x["p"]) / (n_ + 40)
            x["track"] = f"{won_}/{n_}"
    stale = {}
    for pth in (SCAN_PATH, CSP_PATH, CALL_PATH):
        stale.update(((_read_json(pth, {}) or {}).get("stale")) or {})
    d = _clean(json.loads(json.dumps({"rows": rows, "swing": swing, "sources": src, "stale": stale,
                                      "asof": et_now().strftime("%Y-%m-%d %H:%M ET")}, default=str)))
    _cap_cache.update(ts=time.time(), data=d)
    return d


# ---------------------------------------------------------------- usage counts + audit scorecard (for the daily review)
USAGE_PATH = APP_DIR / "usage.json"
AUDIT_DIR = APP_DIR / "audit"


def record_usage(body: dict) -> None:
    ev = body.get("events") or {}
    if not isinstance(ev, dict):
        return
    with _io_lock:
        u = _read_json(USAGE_PATH, {}) or {}
        day = u.setdefault("days", {}).setdefault(et_now().date().isoformat(), {})
        for k, v in list(ev.items())[:60]:
            k = re.sub(r"[^\w:.\- &/]", "", str(k))[:60]
            if k and isinstance(v, (int, float)) and 0 < v < 1000:
                day[k] = day.get(k, 0) + int(v)
        for d in sorted(u["days"])[:-60]:                 # keep 60 days
            del u["days"][d]
        _write_json(USAGE_PATH, u)


def health_data() -> dict:
    hist = []
    hp = AUDIT_DIR / "history.jsonl"
    if hp.exists():
        for ln in hp.read_text(encoding="utf-8").splitlines()[-90:]:
            try:
                hist.append(json.loads(ln))
            except ValueError:
                pass
    return {"latest": _read_json(AUDIT_DIR / "latest.json", None), "history": hist}


# ---------------------------------------------------------------- dark pool (FINRA) & insider trades (SEC Form 4)
FINRA_DIR = APP_DIR / "finra_cache"
SEC_UA = {"User-Agent": "GEXWalls personal research tool (local app)", "Accept-Encoding": "gzip, deflate"}
_flow_cache: dict = {}


def finra_daily(d: date) -> dict | None:
    """FINRA Reg SHO daily short-sale volume for off-exchange trades (TRF/ADF/ORF = dark pools + wholesalers).
    {symbol: [short_volume, total_off_exchange_volume]}. Cached on disk; None if FINRA has no file for that day."""
    FINRA_DIR.mkdir(exist_ok=True)
    f = FINRA_DIR / f"{d:%Y%m%d}.json"
    if f.exists():
        v = _read_json(f, None)
        return v or None
    if d >= et_now().date() and et_now().hour < 19:     # today's file appears in the evening
        return None
    try:
        r = requests.get(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d:%Y%m%d}.txt", headers=SEC_UA, timeout=30)
    except requests.RequestException:
        return None
    if r.status_code != 200 or "|" not in r.text[:200]:
        _write_json(f, {}) if r.status_code in (403, 404) and d < et_now().date() - timedelta(days=1) else None
        return None
    m = {}
    for line in r.text.splitlines()[1:]:
        p = line.split("|")
        if len(p) >= 5 and p[1]:
            try:
                m[p[1]] = [round(float(p[2])), round(float(p[4]))]
            except ValueError:
                continue
    _write_json(f, m)
    for old in sorted(FINRA_DIR.glob("*.json"))[:-45]:      # keep ~2 months
        try:
            old.unlink()
        except OSError:
            pass
    return m


def finra_days(n: int = 25) -> list:
    """Most recent n FINRA daily files as [(date, dict)], newest last."""
    out, d, tries = [], et_now().date(), 0
    while len(out) < n and tries < n * 2 + 10:
        if d.weekday() < 5:
            m = finra_daily(d)
            if m:
                out.append((d, m))
        d -= timedelta(days=1)
        tries += 1
    return out[::-1]


def finra_ats_weekly(ticker: str, weeks: int = 10) -> dict:
    """FINRA OTC Transparency: weekly ATS (dark pool) volume for one stock, plus venues for the latest week.
    Tier 1 (NMS) data is published with a 2-week delay."""
    start = (et_now().date() - timedelta(days=7 * (weeks + 4))).isoformat()

    def q(st):
        body = {"limit": 500, "compareFilters": [
            {"compareType": "EQUAL", "fieldName": "issueSymbolIdentifier", "fieldValue": ticker},
            {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": st}],
            "dateRangeFilters": [{"fieldName": "weekStartDate", "startDate": start, "endDate": et_now().date().isoformat()}]}
        r = requests.post("https://api.finra.org/data/group/otcMarket/name/weeklySummary", json=body,
                          headers={**SEC_UA, "Accept": "application/json"}, timeout=30)
        j = r.json()
        return j if isinstance(j, list) else []
    tot = sorted(q("ATS_W_SMBL"), key=lambda x: x["weekStartDate"])[-weeks:]
    firms = q("ATS_W_SMBL_FIRM")
    last = tot[-1]["weekStartDate"] if tot else None
    venues = sorted([f for f in firms if f["weekStartDate"] == last], key=lambda f: -f["totalWeeklyShareQuantity"])[:6]
    return {"weeks": [{"week": x["weekStartDate"], "shares": x["totalWeeklyShareQuantity"], "trades": x["totalWeeklyTradeCount"],
                       "avg": x["totalWeeklyShareQuantity"] / max(x["totalWeeklyTradeCount"], 1)} for x in tot],
            "venues": [{"name": v.get("marketParticipantName"), "shares": v["totalWeeklyShareQuantity"],
                        "trades": v["totalWeeklyTradeCount"]} for v in venues]}


def finra_short_interest(ticker: str, n: int = 6) -> dict:
    """FINRA consolidated short interest (exchange + OTC), published twice a month ~8 business days after each
    settlement date. Newest n settlement dates: short shares, change vs prior, avg daily volume, days to cover."""
    start = (et_now().date() - timedelta(days=16 * (n + 2))).isoformat()
    body = {"limit": 50, "compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": ticker}],
            "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start, "endDate": et_now().date().isoformat()}]}
    r = requests.post("https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest", json=body,
                      headers={**SEC_UA, "Accept": "application/json"}, timeout=30)
    j = r.json() if r.status_code == 200 else []
    rows = sorted([x for x in (j if isinstance(j, list) else []) if x.get("settlementDate")],
                  key=lambda x: x["settlementDate"])[-n:]
    out = [{"date": x["settlementDate"], "short": x.get("currentShortPositionQuantity"),
            "prev": x.get("previousShortPositionQuantity"), "adv": x.get("averageDailyVolumeQuantity"),
            "dtc": x.get("daysToCoverQuantity")} for x in rows]
    return {"rows": out, "source": "FINRA consolidated short interest"}


def _oi_table(url: str) -> list:
    """Rows of an OpenInsider 'tinytable' as dicts (header -> text) plus the SEC Form 4 link when present."""
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    s_ = r.text
    i = s_.find('class="tinytable"')
    if i < 0:
        return []
    t = s_[i:s_.find("</table>", i)]
    heads = [html.unescape(re.sub(r"<[^>]+>", "", h)).replace("\xa0", " ").strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", t, re.S)]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t.split("<tbody>", 1)[-1], re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).replace("\xa0", " ").strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) != len(heads):
            continue
        d = dict(zip(heads, cells))
        m = re.search(r'href="(https?://www\.sec\.gov/[^"]+)"', tr)
        d["sec"] = m.group(1).replace("http://", "https://") if m else None
        rows.append(d)
    return rows


def _insider_row(d: dict) -> dict:
    tk = re.findall(r"[A-Z][A-Z0-9.]{0,9}", d.get("Ticker", "").split(">")[-1])
    return {"filed": d.get("Filing Date", "")[:10], "date": d.get("Trade Date", ""), "ticker": tk[-1] if tk else "",
            "who": d.get("Insider Name") or d.get("Company Name", ""), "title": d.get("Title") or d.get("Industry", ""),
            "n_ins": d.get("Ins"), "type": d.get("Trade Type", ""), "price": _num(d.get("Price")), "qty": _num(d.get("Qty")),
            "own_chg": d.get("ΔOwn") or d.get("ΔOwn", ""), "value": _num(d.get("Value")), "sec": d.get("sec")}


def insider_trades(ticker: str) -> dict:
    rows = [_insider_row(d) for d in _oi_table(f"http://openinsider.com/{ticker}")]
    cutoff = (et_now().date() - timedelta(days=90)).isoformat()
    recent = [x for x in rows if x["date"] >= cutoff]
    buys = [x for x in recent if x["type"].startswith("P")]
    sells = [x for x in recent if x["type"].startswith("S")]
    return {"rows": rows[:25], "buy_value": sum(x["value"] or 0 for x in buys), "sell_value": sum(x["value"] or 0 for x in sells),
            "buyers": len({x["who"] for x in buys}), "sellers": len({x["who"] for x in sells}), "days": 90}


def flow_ticker(ticker: str) -> dict:
    ticker = clean_ticker(ticker)
    hit = _flow_cache.get(("t", ticker))
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    days = finra_days(25)
    vol = {}
    try:                                                   # consolidated volume (all venues) for the off-exchange share
        h = daily_history(ticker)
        vol = {str(r.date): float(r.volume) for r in h.tail(60).itertuples() if pd.notna(r.volume) and r.volume}
    except Exception:
        vol = {}
    series = []
    for d, m in days:
        v = m.get(ticker)
        if not v:
            continue
        sv, tv = v
        cv = vol.get(d.isoformat())
        series.append({"date": d.isoformat(), "short": sv, "off": tv, "short_pct": sv / tv * 100 if tv else None,
                       "total": cv, "off_pct": tv / cv * 100 if cv and cv >= tv else None})
    sp = [x["short_pct"] for x in series if x["short_pct"] is not None]
    stats = None
    if len(sp) >= 6:
        base = sp[:-1][-20:]
        mu = sum(base) / len(base)
        sd = (sum((x - mu) ** 2 for x in base) / len(base)) ** 0.5
        offs = [x["off"] for x in series][:-1][-20:]
        stats = {"short_pct": sp[-1], "avg20": mu, "z": (sp[-1] - mu) / sd if sd else 0.0,
                 "off_ratio": series[-1]["off"] / (sum(offs) / len(offs)) if offs else None,
                 "off_pct": next((x["off_pct"] for x in reversed(series) if x["off_pct"] is not None), None)}
    out = {"ticker": ticker, "finra_daily": series, "stats": stats, "asof": series[-1]["date"] if series else None}
    try:
        out["ats"] = finra_ats_weekly(ticker)
    except Exception as e:
        out["ats"] = {"error": str(e)}
    try:
        out["si"] = finra_short_interest(ticker)
    except Exception as e:
        out["si"] = {"error": str(e)}
    try:
        out["insiders"] = insider_trades(ticker)
    except Exception as e:
        out["insiders"] = {"error": str(e)}
    try:
        q = nasdaq_quote(ticker)
        out["price"] = q["price"] if q else None
        h = daily_history(ticker)
        out["rsi"], _ = rsi14(ticker, 0, "", h)
    except Exception:
        out["price"], out["rsi"] = None, None
    out = _clean(json.loads(json.dumps(out, default=str)))
    _flow_cache[("t", ticker)] = (time.time(), out)
    return out


def flow_market() -> dict:
    hit = _flow_cache.get(("m",))
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    days = finra_days(21)
    cfg = load_config()
    uni = set(SCAN_ETFS) | set(SCAN_STOCKS) | {w["ticker"] for w in cfg.get("watchlist", [])} | set(cfg.get("puts_watchlist") or [])
    unusual = []
    if len(days) >= 8:
        last_d, last = days[-1]
        for tk in uni:
            v = last.get(tk)
            if not v or not v[1] or v[1] < 200_000:
                continue
            hist = [m[tk] for _, m in days[:-1] if tk in m and m[tk][1]]
            if len(hist) < 8:
                continue
            ratios = [a / b * 100 for a, b in hist[-20:]]
            mu = sum(ratios) / len(ratios)
            sd = (sum((x - mu) ** 2 for x in ratios) / len(ratios)) ** 0.5 or 1e-9
            sp = v[0] / v[1] * 100
            vavg = sum(b for _, b in hist[-20:]) / len(hist[-20:])
            unusual.append({"ticker": tk, "short_pct": sp, "avg20": mu, "z": (sp - mu) / sd, "off_vol": v[1],
                            "vol_ratio": v[1] / vavg if vavg else None, "sector": SCAN_ETFS.get(tk) or SCAN_STOCKS.get(tk) or "Watchlist"})
    out = {"asof": days[-1][0].isoformat() if days else None, "sessions": len(days),
           "high_short": sorted([u for u in unusual if u["z"] >= 1.5], key=lambda u: -u["z"])[:15],
           "low_short": sorted([u for u in unusual if u["z"] <= -1.5], key=lambda u: u["z"])[:15],
           "vol_spike": sorted([u for u in unusual if (u["vol_ratio"] or 0) >= 1.8], key=lambda u: -u["vol_ratio"])[:15]}
    try:
        out["insider_buys"] = [_insider_row(d) for d in _oi_table("http://openinsider.com/latest-insider-purchases-25k")][:40]
    except Exception as e:
        out["insider_buys_error"] = str(e)
    try:
        out["cluster_buys"] = [_insider_row(d) for d in _oi_table("http://openinsider.com/latest-cluster-buys")][:25]
    except Exception as e:
        out["cluster_error"] = str(e)
    out = _clean(json.loads(json.dumps(out, default=str)))
    _flow_cache[("m",)] = (time.time(), out)
    return out


# ---------------------------------------------------------------- big money: SEC 13F institutions + 13D/13G 5%+ owners
H13F_DB = APP_DIR / "h13f.sqlite"
SEC_TICKERS_PATH = APP_DIR / "sec_tickers.json"
SEC_13F_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_FTD_PAGE = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"
H13F_MIN_VALUE = 100_000          # positions under $100K are dropped from the local index (keeps it small)
_h13f = {"state": "idle", "msg": "", "started": None, "done": None, "error": None}
_h13f_lock = threading.Lock()
_bm_cache: dict = {}
_sec_last = [0.0]


def sec_user_agent() -> str | None:
    """SEC asks automated tools to declare a contact in the User-Agent (www.sec.gov answers 403 without one).
    Uses 'sec_user_agent' in alerts_config.json, else the Strategy Scanner's configured value."""
    ua = str(load_config().get("sec_user_agent") or "").strip()
    if not ua:
        ua = str((_read_json(SS_DIR / "config.json", {}) or {}).get("sec_user_agent") or "").strip()
    return ua if "@" in ua else None


def _sec_get(url: str, timeout: int = 60, params: dict | None = None) -> requests.Response:
    ua = sec_user_agent()
    if not ua:
        raise RuntimeError("SEC needs a contact in the User-Agent: set sec_user_agent in alerts_config.json")
    wait = 0.15 - (time.time() - _sec_last[0])        # stay far under SEC's 10 requests/second
    if wait > 0:
        time.sleep(wait)
    _sec_last[0] = time.time()
    r = requests.get(url, params=params, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=timeout)
    r.raise_for_status()
    return r


def _sec_links(page: str, pat: str) -> list:
    hrefs = re.findall(pat, _sec_get(page, 30).text)
    return [("https://www.sec.gov" + h) if h.startswith("/") else h for h in dict.fromkeys(hrefs)]


def sec_cik(ticker: str) -> tuple[int | None, str]:
    """Ticker -> (CIK, company name) from SEC company_tickers.json (cached a week)."""
    m = _read_json(SEC_TICKERS_PATH, None)
    fresh = SEC_TICKERS_PATH.exists() and time.time() - SEC_TICKERS_PATH.stat().st_mtime < 7 * 86400
    if not m or not fresh:
        try:
            raw = _sec_get("https://www.sec.gov/files/company_tickers.json", 30).json()
            m = {v["ticker"].upper(): [int(v["cik_str"]), v["title"]] for v in raw.values()}
            _write_json(SEC_TICKERS_PATH, m)
        except Exception:
            m = m or {}
            if not m:                                      # fall back to the Strategy Scanner's cached copy
                raw = _read_json(SS_DIR / "data" / "cache" / "sec_company_tickers.json", {}) or {}
                m = {v["ticker"].upper(): [int(v["cik_str"]), v["title"]] for v in raw.values()} if isinstance(raw, dict) else {}
    v = m.get(ticker.upper()) or m.get(ticker.upper().replace(".", "-"))
    return (v[0], v[1]) if v else (None, "")


def _tsv(z, name: str, cols: list, chunks: int | None = None):
    fn = next(n for n in z.namelist() if n.upper().endswith(name + ".TSV"))
    return pd.read_csv(z.open(fn), sep="\t", dtype=str, usecols=cols, quoting=3, on_bad_lines="skip",
                       encoding="utf-8", encoding_errors="replace", keep_default_na=False, chunksize=chunks)


def _h13f_one(url: str, con) -> str:
    """Load one quarterly SEC Form 13F data set into the local index. Returns the report period (YYYY-MM-DD)."""
    _h13f["msg"] = "Downloading " + url.rsplit("/", 1)[-1] + " (~100 MB) from SEC…"
    z = zipfile.ZipFile(io.BytesIO(_sec_get(url, 600).content))
    sub = _tsv(z, "SUBMISSION", ["ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"])
    cov = _tsv(z, "COVERPAGE", ["ACCESSION_NUMBER", "ISAMENDMENT", "AMENDMENTTYPE", "FILINGMANAGER_NAME", "REPORTTYPE"])
    s = sub[sub["SUBMISSIONTYPE"].isin(["13F-HR", "13F-HR/A"])].merge(cov, on="ACCESSION_NUMBER", how="inner")
    s = s[~s["REPORTTYPE"].str.upper().str.contains("NOTICE")]
    s["per"] = pd.to_datetime(s["PERIODOFREPORT"], format="%d-%b-%Y", errors="coerce")
    s["fd"] = pd.to_datetime(s["FILING_DATE"], format="%d-%b-%Y", errors="coerce")
    per = s.loc[s["SUBMISSIONTYPE"] == "13F-HR", "per"].mode().iloc[0]
    s = s[s["per"] == per].sort_values("fd")
    keep, mgr = {}, {}
    for cik, g in s.groupby("CIK"):                        # restatement replaces; "new holdings" amendments add
        amend = g["ISAMENDMENT"].str.upper() == "Y"
        rest = g[amend & g["AMENDMENTTYPE"].str.upper().str.startswith("RESTATE")]
        base = rest.iloc[-1] if len(rest) else (g[~amend].iloc[-1] if (~amend).any() else None)
        if base is None:
            continue
        adds = g[amend & g["AMENDMENTTYPE"].str.upper().str.startswith("NEW") & (g["fd"] >= base["fd"])]
        for a in [base["ACCESSION_NUMBER"], *adds["ACCESSION_NUMBER"]]:
            keep[a] = cik
        mgr[cik] = base["FILINGMANAGER_NAME"].strip()
    agg, names = [], {}
    cols = ["ACCESSION_NUMBER", "NAMEOFISSUER", "TITLEOFCLASS", "CUSIP", "VALUE", "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL"]
    for i, ch in enumerate(_tsv(z, "INFOTABLE", cols, chunks=400_000)):
        _h13f["msg"] = f"Indexing {per:%b %d %Y} 13F holdings… {(i + 1) * 0.4:.1f}M rows"
        ch = ch[ch["ACCESSION_NUMBER"].isin(keep)]
        if ch.empty:
            continue
        ch = ch.assign(cik=ch["ACCESSION_NUMBER"].map(keep), cusip=ch["CUSIP"].str.strip().str.upper(),
                       pc=ch["PUTCALL"].str.strip().str.upper().str[:1],
                       val=pd.to_numeric(ch["VALUE"], errors="coerce").fillna(0),
                       sh=pd.to_numeric(ch["SSHPRNAMT"], errors="coerce").fillna(0))
        ch = ch[(ch["cusip"].str.len() == 9) & (ch["SSHPRNAMTTYPE"].str.upper() == "SH")]
        for c, n, t in ch[["cusip", "NAMEOFISSUER", "TITLEOFCLASS"]].drop_duplicates("cusip").itertuples(index=False):
            names.setdefault(c, (n.strip(), t.strip()))
        agg.append(ch.groupby(["cik", "cusip", "pc"], as_index=False)[["sh", "val"]].sum())
    h = pd.concat(agg).groupby(["cik", "cusip", "pc"], as_index=False)[["sh", "val"]].sum()
    tot = h[h["pc"] == ""].groupby("cik")["val"].sum()
    h = h[(h["val"] >= H13F_MIN_VALUE) | (h["pc"] != "")]
    p = per.strftime("%Y-%m-%d")
    con.executemany("insert into hold values (?,?,?,?,?,?)", ((p, *r) for r in h.itertuples(index=False)))
    con.executemany("insert into filers values (?,?,?,?)", ((p, c, n, float(tot.get(c, 0))) for c, n in mgr.items()))
    con.executemany("insert or ignore into iss values (?,?,?)", ((c, n, t) for c, (n, t) in names.items()))
    con.execute("insert into periods values (?,?,?)", (p, url, len(mgr)))
    con.commit()
    return p


def _h13f_ftd(con) -> int:
    """Ticker -> CUSIP map from SEC fails-to-deliver files (each row lists CUSIP and symbol), newest wins."""
    m = {}
    for url in _sec_links(SEC_FTD_PAGE, r'href="([^"]+cnsfails\d+[ab]\.zip)"')[:8][::-1]:
        _h13f["msg"] = "Building the ticker→CUSIP map from " + url.rsplit("/", 1)[-1]
        z = zipfile.ZipFile(io.BytesIO(_sec_get(url, 120).content))
        for fn in z.namelist():
            for line in z.read(fn).decode("latin-1").splitlines()[1:]:
                p = line.split("|")
                if len(p) >= 5 and len(p[1].strip()) == 9 and p[2].strip():
                    m[p[2].strip().upper()] = (p[1].strip().upper(), p[4].strip(), p[0].strip())
    con.executemany("insert or replace into tick values (?,?,?,?)", ((k, *v) for k, v in m.items()))
    con.commit()
    return len(m)


def h13f_build(force: bool = False) -> None:
    """(Re)build the local 13F index from the two newest SEC quarterly data sets, if SEC has published a newer one."""
    with _h13f_lock:
        if _h13f["state"] == "building":
            return
        _h13f.update(state="building", started=et_now().isoformat(timespec="seconds"), error=None, msg="Checking SEC…")
    tmp = H13F_DB.with_suffix(".building")
    try:
        zips = _sec_links(SEC_13F_PAGE, r'href="([^"]+_form13f\.zip)"')[:2]
        if len(zips) < 2:
            raise RuntimeError("could not find the SEC 13F data-set links")
        if H13F_DB.exists() and not force:
            with sqlite3.connect(H13F_DB) as c0:
                have = {r[0] for r in c0.execute("select src from periods")}
            if have == set(zips):
                _h13f.update(state="ready", msg="Up to date", done=et_now().isoformat(timespec="seconds"))
                os.utime(H13F_DB)
                return
        if tmp.exists():
            tmp.unlink()
        con = sqlite3.connect(tmp)
        con.executescript("""create table hold(period text, cik text, cusip text, pc text, sh real, val real);
            create table filers(period text, cik text, name text, total real);
            create table iss(cusip text primary key, name text, cls text);
            create table tick(sym text primary key, cusip text, descr text, d text);
            create table periods(period text, src text, n integer);""")
        for url in zips:
            _h13f_one(url, con)
        _h13f_ftd(con)
        _h13f["msg"] = "Indexing…"
        con.executescript("create index ix_h on hold(cusip, period); create index ix_f on filers(period, cik);")
        con.close()
        os.replace(tmp, H13F_DB)
        _bm_cache.clear()
        _h13f.update(state="ready", msg="Built", done=et_now().isoformat(timespec="seconds"))
    except Exception as e:
        _h13f.update(state="error", error=str(e), msg="Build failed: " + str(e), fail_ts=time.time())
        try:
            tmp.unlink()
        except OSError:
            pass


def h13f_loop() -> None:
    """Check SEC for a newer 13F data set once a day (they're published quarterly)."""
    time.sleep(90)
    while True:
        try:
            old = not H13F_DB.exists() or time.time() - H13F_DB.stat().st_mtime > 86400
            if old and sec_user_agent():
                h13f_build()
        except Exception:
            pass
        time.sleep(3600)


def h13f_ticker(ticker: str) -> dict:
    """Institutional holders of one stock/ETF from the local 13F index: latest quarter vs the one before."""
    if not H13F_DB.exists():
        return {"status": _h13f}
    con = sqlite3.connect(f"file:{H13F_DB.as_posix()}?mode=ro", uri=True)
    try:
        t = con.execute("select cusip, descr, d from tick where sym=?", (ticker,)).fetchone()
        if not t:
            return {"error": f"No CUSIP found for {ticker} in SEC fails-to-deliver files (rarely traded or new listing)."}
        cusip = t[0]
        iss = con.execute("select name, cls from iss where cusip=?", (cusip,)).fetchone()
        pers = [r[0] for r in con.execute("select period from periods order by period desc")]
        p1, p0 = pers[0], pers[1] if len(pers) > 1 else None
        names = {c: n for c, n in con.execute("select cik, name from filers where period=?", (p1,))}
        names0 = {c: n for c, n in con.execute("select cik, name from filers where period=?", (p0,))}
        f1, f0 = set(names), set(names0)
        rows = {p: {} for p in pers}
        for p, cik, pc, sh, val in con.execute("select period, cik, pc, sh, val from hold where cusip=?", (cusip,)):
            rows.setdefault(p, {})[(cik, pc)] = (sh, val)
    finally:
        con.close()
    cur, prev = rows.get(p1, {}), rows.get(p0, {}) if p0 else {}
    sh1 = {k[0]: v for k, v in cur.items() if k[1] == ""}
    sh0 = {k[0]: v for k, v in prev.items() if k[1] == ""}
    px1 = sum(v[1] for v in sh1.values()) / max(sum(v[0] for v in sh1.values()), 1)

    def item(cik):
        s1, v1 = sh1.get(cik, (0, 0))
        s0, v0 = sh0.get(cik, (0, 0))
        st = ("new" if s0 == 0 and cik in f0 else "no prior 13F" if cik not in f0 else
              "exited" if s1 == 0 else "added" if s1 > s0 * 1.005 else "cut" if s1 < s0 * 0.995 else "same")
        return {"cik": cik, "name": names.get(cik) or names0.get(cik) or cik, "sh": s1, "val": v1, "sh0": s0,
                "dsh": s1 - s0 if cik in f0 and cik in f1 else None, "dval": (s1 - s0) * px1 if cik in f0 and cik in f1 else None,
                "pct_chg": (s1 / s0 - 1) * 100 if s0 and s1 else None, "status": st}
    allc = [item(c) for c in set(sh1) | {c for c in sh0 if c in f1}]
    hold = sorted([x for x in allc if x["sh"] > 0], key=lambda x: -x["sh"])
    both = [x for x in allc if x["dsh"] is not None]

    def opt(kind):
        d1 = {k[0]: v for k, v in cur.items() if k[1] == kind}
        d0 = {k[0]: v for k, v in prev.items() if k[1] == kind}
        top = sorted(d1.items(), key=lambda kv: -kv[1][1])[:8]
        return {"val": sum(v[1] for v in d1.values()), "val0": sum(v[1] for v in d0.values()), "n": len(d1),
                "top": [{"name": names.get(c, c), "sh": v[0], "val": v[1], "val0": d0.get(c, (0, 0))[1] if c in f0 else None} for c, v in top]}
    return {"period": p1, "prev_period": p0, "cusip": cusip, "issuer": iss[0] if iss else t[1], "cls": iss[1] if iss else "",
            "n_holders": len(hold), "n_prev": len(sh0), "sh": sum(x["sh"] for x in hold), "val": sum(x["val"] for x in hold),
            "sh_prev": sum(v[0] for v in sh0.values()),
            "net_dsh": sum(x["dsh"] for x in both), "n_add": sum(x["status"] == "added" for x in allc),
            "n_cut": sum(x["status"] == "cut" for x in allc), "n_new": sum(x["status"] == "new" for x in allc),
            "n_exit": sum(x["status"] == "exited" for x in allc), "filers": len(f1), "filers_prev": len(f0),
            "top": hold[:15],
            "adds": sorted([x for x in both if x["dsh"] > 0 and x["status"] != "new"], key=lambda x: -x["dval"])[:10],
            "cuts": sorted([x for x in both if x["dsh"] < 0 and x["status"] != "exited"], key=lambda x: x["dval"])[:10],
            "new": sorted([x for x in allc if x["status"] == "new"], key=lambda x: -x["val"])[:10],
            "exits": sorted([x for x in allc if x["status"] == "exited"], key=lambda x: -x["sh0"])[:10],
            "puts": opt("P"), "calls": opt("C"), "min_value": H13F_MIN_VALUE}


def five_pct_owners(ticker: str) -> dict:
    """5%+ owners from Schedule 13D (activist) / 13G (passive) filings in the last 2 years, via SEC full-text search.
    Latest filing per filer, with % of class and shares parsed from the filing when it's in SEC's XML format."""
    cik, cname = sec_cik(ticker)
    if not cik:
        return {"error": f"{ticker} isn't in SEC's ticker list (ETFs and funds often aren't)."}
    end = et_now().date()
    forms = "SC 13D,SC 13D/A,SC 13G,SC 13G/A,SCHEDULE 13D,SCHEDULE 13D/A,SCHEDULE 13G,SCHEDULE 13G/A"
    hits = []
    for frm in (0, 100):
        j = _sec_get("https://efts.sec.gov/LATEST/search-index", 30, params={
            "q": "", "ciks": f"{cik:010d}", "forms": forms, "startdt": (end - timedelta(days=730)).isoformat(),
            "enddt": end.isoformat(), "from": frm}).json()
        hh = (j.get("hits") or {}).get("hits") or []
        hits += hh
        if len(hh) < 100:
            break
    me = f"{cik:010d}"
    latest = {}
    for h in sorted(hits, key=lambda h: h["_source"].get("file_date", ""), reverse=True):
        s = h["_source"]
        others = [c for c in s.get("ciks", []) if c != me]
        if not others:
            continue
        key = tuple(sorted(others))
        if key in latest:
            latest[key]["n"] += 1
            continue
        acc, fn = h["_id"].split(":", 1)
        filer = [re.sub(r"\s*\(CIK \d+\)", "", n).strip() for n in s.get("display_names", []) if me not in n]
        latest[key] = {"filer": "; ".join(filer) or others[0], "form": s.get("form"), "filed": s.get("file_date"),
                       "activist": "13D" in (s.get("form") or ""), "n": 1, "pct": None, "shares": None, "event": None,
                       "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{fn}"}
    rows = sorted(latest.values(), key=lambda x: x["filed"] or "", reverse=True)
    for x in rows[:15]:                                 # parse % of class from the filing itself
        try:
            t = _sec_get(x["url"], 30).text
        except Exception:
            continue
        ic = re.search(r"<issuerCik>(\d+)</issuerCik>", t)
        if ic and int(ic.group(1)) != cik:
            x["skip"] = True                            # this company filed it about someone else
            continue
        if "<classPercent>" in t:
            pc = [float(v) for v in re.findall(r"<classPercent>\s*([\d.]+)\s*<", t)]
            am = [float(v) for v in re.findall(r"<(?:\w+:)?amountBeneficiallyOwned>\s*([\d.,]+)\s*<", t.replace(",", ""))]
            ev = re.search(r"<eventDate\w*>([^<]+)<", t)
            x["pct"], x["shares"] = (max(pc) if pc else None), (max(am) if am else None)
            x["event"] = ev.group(1) if ev else None
        else:
            txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))
            m = re.search(r"percent of class[^%]{0,250}?(\d{1,3}(?:\.\d+)?)\s*%", txt, re.I)
            x["pct"] = float(m.group(1)) if m else None
    rows = [x for x in rows if not x.get("skip")]
    return {"cik": cik, "company": cname, "rows": rows[:25], "n_filings": len(hits),
            "search": f"https://efts.sec.gov/LATEST/search-index?q=&ciks={me}&forms=SC%2013D,SC%2013G,SCHEDULE%2013D,SCHEDULE%2013G"}


def shares_outstanding(cik: int) -> tuple[float | None, str | None]:
    try:
        j = _sec_get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/EntityCommonStockSharesOutstanding.json", 30).json()
        pts = [p for u in (j.get("units") or {}).values() for p in u]
        p = max(pts, key=lambda p: p.get("end", ""))
        return float(p["val"]), p.get("end")
    except Exception:
        return None, None


def big_money(ticker: str) -> dict:
    ticker = clean_ticker(ticker)
    hit = _bm_cache.get(ticker)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    if not sec_user_agent():
        return {"ticker": ticker, "error": "SEC requires a contact in the User-Agent. Add \"sec_user_agent\": "
                "\"GEXWalls/1.0 (personal research; you@example.com)\" to alerts_config.json and restart."}
    out = {"ticker": ticker, "status": dict(_h13f)}
    retry_ok = _h13f["state"] != "error" or time.time() - _h13f.get("fail_ts", 0) > 1800
    if retry_ok and (not H13F_DB.exists() or _h13f["state"] == "idle" and H13F_DB.stat().st_mtime < time.time() - 7 * 86400):
        threading.Thread(target=h13f_build, daemon=True).start()
        time.sleep(0.2)
        out["status"] = dict(_h13f)
    try:
        out["f13"] = h13f_ticker(ticker)
    except Exception as e:
        out["f13"] = {"error": str(e)}
    try:
        out["owners"] = five_pct_owners(ticker)
        cik = out["owners"].get("cik")
        if cik:
            out["shares_out"], out["shares_out_date"] = shares_outstanding(cik)
    except Exception as e:
        out["owners"] = {"error": str(e)}
    try:
        q = nasdaq_quote(ticker)
        out["price"] = q["price"] if q else None
        out["rsi"], _ = rsi14(ticker, 0, "", daily_history(ticker))
    except Exception:
        out["price"], out["rsi"] = None, None
    out = _clean(json.loads(json.dumps(out, default=str)))
    if (out.get("f13") or {}).get("period"):
        _bm_cache[ticker] = (time.time(), out)
    return out


# ---------------------------------------------------------------- GEX trend: daily history + next 4 expiration weeks
GEX_DAILY_PATH = APP_DIR / "gex_daily.json"
_gd_lock = threading.Lock()


def gex_weeks4(spot: float, df: pd.DataFrame, n: int = 4) -> list:
    """Net GEX of each of the next n calendar weeks of expirations (Mon–Sun, named by Friday), with the put and call
    wall of that week's busiest expiration. Today's expiration counts in this week until the 4 PM close."""
    now = et_now()
    today = now.date()
    mon = today - timedelta(days=today.weekday()) + (timedelta(days=7) if today.weekday() >= 5 else timedelta(0))
    exps = pd.Series(sorted(df["expiration"].unique()))
    oi = df.groupby("expiration")["open_interest"].sum()
    out = []
    for i in range(n):
        m = mon + timedelta(days=7 * i)
        lo = max(m, today if now.hour < 16 else today + timedelta(days=1))
        hi = m + timedelta(days=6)
        es = [e for e in exps if lo <= pd.Timestamp(e).date() <= hi]
        fri = m + timedelta(days=4)
        if not es:
            out.append({"fri": fri.isoformat(), "net": None})
            continue
        main = max(es, key=lambda e: float(oi.get(e, 0)))
        a = analyze_exp(spot, df[df["expiration"] == main], 0.15)
        out.append({"fri": fri.isoformat(), "net": float(df[df["expiration"].isin(es)]["gex"].sum()),
                    "exp": pd.Timestamp(main).strftime("%Y-%m-%d"), "pw": a["put_wall"], "cw": a["call_wall"]})
    return out


def record_gex_daily(ticker: str, meta: dict, spot: float, df: pd.DataFrame, w4: list | None = None) -> None:
    """One row per ticker per quote date (the latest snapshot of the day wins, so after the close it is the close):
    total net GEX, price, and the next-4-weeks GEX. Kept ~35 sessions."""
    lt = str(meta.get("last_trade") or "")
    if not re.match(r"\d{4}-\d{2}-\d{2}", lt):
        return
    try:
        w4 = w4 if w4 is not None else gex_weeks4(spot, df)
    except Exception:
        w4 = []
    row = [float(df["gex"].sum()), float(spot), [w.get("net") for w in w4], [w.get("pw") for w in w4],
           [w.get("fri") for w in w4], lt[:16]]
    with _gd_lock:
        h = _read_json(GEX_DAILY_PATH, {}) or {}
        th = h.setdefault(ticker, {})
        th[lt[:10]] = row
        for d in sorted(th)[:-35]:
            del th[d]
        _write_json(GEX_DAILY_PATH, h)


def gex_trend(tickers: list, n: int = 20) -> dict:
    """{ticker: {"days": [[date, net, spot], ...last n], "w4": [{fri, net, pw}] from the latest snapshot}}."""
    h = _read_json(GEX_DAILY_PATH, {}) or {}
    out = {}
    for tk in tickers:
        th = h.get(tk) or {}
        if not th:
            continue
        ds = sorted(th)[-n:]
        last = th[ds[-1]]
        w4 = [{"fri": f, "net": v, "pw": p} for v, p, f in zip(last[2] or [], last[3] or [], last[4] or [])]
        out[tk] = {"days": [[d, th[d][0], th[d][1]] for d in ds], "w4": w4, "asof": last[5] if len(last) > 5 else ds[-1]}
    return out


def record_gex_universe(tickers: list) -> int:
    return _queued({}, "daily GEX snapshot", _record_gex_universe, tickers)


def _record_gex_universe(tickers: list) -> int:
    """Load each chain once and save today's GEX row (used a few times a day for the watchlist, rotation and trades)."""
    def one(tk):
        try:
            spot, df, meta = load_chain(tk)
            record_gex_daily(tk, meta, spot, df)
            return 1
        except Exception:
            return 0
    with ThreadPoolExecutor(3) as ex:
        return sum(ex.map(_bg_cboe(one), list(dict.fromkeys(tickers))))


def gex_daily_loop() -> None:
    """Weekdays 12:10 and 16:25 ET: GEX rows for the rotation universe, watchlists and open trades."""
    done = set()
    time.sleep(120)
    if not GEX_DAILY_PATH.exists() or time.time() - GEX_DAILY_PATH.stat().st_mtime > 20 * 3600:
        try:                                              # first run / stale: fill every list once so trends show at once
            cfg = load_config()
            record_gex_universe([w["ticker"] for w in cfg.get("watchlist", [])] + [t["ticker"] for t in load_trades(True)]
                                + list(SCAN_ETFS) + list(SCAN_STOCKS))
        except Exception:
            pass
    while True:
        try:
            now = et_now()
            if now.weekday() < 5:
                for hh, mm in ((12, 10), (16, 25)):
                    key = f"{now.date()} {hh}:{mm}"
                    m_now, m_t = now.hour * 60 + now.minute, hh * 60 + mm
                    if key not in done and 0 <= m_now - m_t < 50:
                        done.add(key)
                        cfg = load_config()
                        record_gex_universe(list(SCAN_ETFS) + list(SCAN_STOCKS) + [w["ticker"] for w in cfg.get("watchlist", [])]
                                            + [t["ticker"] for t in load_trades(True)] + list(cfg.get("puts_watchlist") or []))
        except Exception:
            pass
        time.sleep(120)


# ---------------------------------------------------------------- 24-hour (overnight) trading + extended hours
OVERNIGHT_PATH = APP_DIR / "overnight.json"
OVERNIGHT_ATS = {"BLUE": "Blue Ocean ATS", "MOON": "MOON ATS", "BOSS": "Bruce ATS"}   # the overnight (8 PM–4 AM ET) venues
_ext_cache: dict = {}


def overnight_symbols(force: bool = False) -> dict:
    """Symbols that traded on the overnight ATSs (Blue Ocean, which Webull and Moomoo route to, MOON ATS and Bruce) in
    FINRA's latest published ATS week (FINRA OTC Transparency, ~2-week lag). {"week", "symbols": {sym: shares}}."""
    d = _read_json(OVERNIGHT_PATH, None)
    if d and not force and time.time() - OVERNIGHT_PATH.stat().st_mtime < 3 * 86400:
        return d
    start = (et_now().date() - timedelta(days=35)).isoformat()
    week = None
    syms: dict = {}
    for mpid in OVERNIGHT_ATS:
        off = 0
        rows_all = []
        while off < 20000:
            body = {"limit": 5000, "offset": off, "compareFilters": [
                {"compareType": "EQUAL", "fieldName": "MPID", "fieldValue": mpid},
                {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL_FIRM"}],
                "dateRangeFilters": [{"fieldName": "weekStartDate", "startDate": start, "endDate": et_now().date().isoformat()}]}
            r = requests.post("https://api.finra.org/data/group/otcMarket/name/weeklySummary", json=body,
                              headers={**SEC_UA, "Accept": "application/json"}, timeout=60)
            rows = r.json() if r.status_code == 200 else []
            rows = rows if isinstance(rows, list) else []
            rows_all += rows
            if len(rows) < 5000:
                break
            off += 5000
        if not rows_all:
            continue
        wk = max(x["weekStartDate"] for x in rows_all)
        week = max(week or wk, wk)
        for x in rows_all:
            if x["weekStartDate"] == wk and x.get("issueSymbolIdentifier"):
                s_ = x["issueSymbolIdentifier"]
                syms[s_] = syms.get(s_, 0) + int(x.get("totalWeeklyShareQuantity") or 0)
    if not syms:
        return d or {"week": None, "symbols": {}}
    d = {"week": week, "symbols": syms, "venues": list(OVERNIGHT_ATS.values()), "built": et_now().isoformat(timespec="minutes")}
    _write_json(OVERNIGHT_PATH, d)
    return d


def ext_hours(ticker: str) -> dict:
    """Pre-market (vs the previous close) and after-hours (vs that day's regular close) last trade, volume and range
    (Nasdaq.com + CBOE quote, cached 3 min). "pick" is the most recent session."""
    hit = _ext_cache.get(ticker)
    if hit and time.time() - hit[0] < 180:
        return hit[1]
    out = {}
    cls = "etf" if ticker in SCAN_ETFS or ticker in PUT_ETFS else "stocks"
    for mt in ("pre", "post"):
        try:
            r = requests.get(f"https://api.nasdaq.com/api/quote/{ticker}/extended-trading?assetclass={cls}&markettype={mt}",
                             headers=NASDAQ_HDR, timeout=15)
            d = (r.json() or {}).get("data") or {}
            if not d and cls == "stocks":
                r = requests.get(f"https://api.nasdaq.com/api/quote/{ticker}/extended-trading?assetclass=etf&markettype={mt}",
                                 headers=NASDAQ_HDR, timeout=15)
                d = (r.json() or {}).get("data") or {}
            rows = ((d.get("infoTable") or {}).get("rows")) or []
            if not rows:
                continue
            x = rows[0]
            m = re.match(r"\$?([\d,.]+)\s+([+-][\d.,]+)\s+\(([+-][\d.]+)%\)", str(x.get("consolidated") or ""))
            upd = " ".join(d.get("lastUpdateInfo") or [])[:80]
            dm = re.search(r"updated\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})", upd)
            try:
                sdate = datetime.strptime(dm.group(1), "%b %d, %Y").strftime("%Y-%m-%d") if dm else None
            except ValueError:
                sdate = None
            out[mt] = {"last": _num(m.group(1)) if m else None, "chg": _num(m.group(2)) if m else None,
                       "pct": _num(m.group(3)) if m else None, "vol": _num(x.get("volume")),
                       "hi": _num(str(x.get("highPrice") or "").split(" ")[0]), "lo": _num(str(x.get("lowPrice") or "").split(" ")[0]),
                       "close": _num(str(d.get("previousInfo") or "").split("$")[-1]), "updated": upd, "date": sdate,
                       "ref": "previous close"}
        except Exception:
            continue
    # Nasdaq's after-hours % is measured from the PREVIOUS day's close; re-base it on that day's regular close (CBOE quote).
    po = out.get("post")
    if po and po.get("last") and po.get("date"):
        try:
            r = _cboe_get(f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/{'_' + ticker if ticker in INDEXES else ticker}.json")
            q = (r.json() or {}).get("data") or {} if r.status_code == 200 else {}
            if str(q.get("last_trade_time") or "")[:10] == po["date"] and q.get("close"):
                rc = float(q["close"])
                po.update(close=rc, chg=round(po["last"] - rc, 4), pct=round((po["last"] / rc - 1) * 100, 2), ref="regular close")
            else:
                po.update(chg=None, pct=None, ref="regular close unavailable")
        except Exception:
            po.update(chg=None, pct=None, ref="regular close unavailable")
    pr = out.get("pre")
    cand = [k for k in ("post", "pre") if (out.get(k) or {}).get("last")]
    out["pick"] = max(cand, key=lambda k: (out[k].get("date") or "", 1 if k == "post" else 0)) if cand else None
    if pr and po and out["pick"] == "post" and (pr.get("date") or "") < (po.get("date") or ""):
        pr["stale"] = True
    ov = (overnight_symbols() or {})
    out["overnight"] = {"traded": ticker in (ov.get("symbols") or {}), "shares": (ov.get("symbols") or {}).get(ticker),
                        "week": ov.get("week")}
    _ext_cache[ticker] = (time.time(), out)
    return out


# ---------------------------------------------------------------- insights: candles, analysts, news, health, dilution
INSIGHT_PATH = APP_DIR / "insights_cache.json"
_ins_mem: dict = {}
_ins_lock = threading.Lock()


def candle_signals(bars: list, levels: list | None = None) -> list:
    """Classic daily candlestick patterns on the last 1–3 bars [date, o, h, l, c, v], each with a strength score.
    'Extra' strength when it forms at a support level (put wall, key Fib, 50-day average) on above-average volume."""
    b = [x for x in bars if all(v is not None for v in x[1:5])]
    if len(b) < 25:
        return []
    o, h, l, c = ([x[i] for x in b] for i in (1, 2, 3, 4))
    vol = [x[5] if len(x) > 5 and x[5] else 0 for x in b]
    body = lambda i: abs(c[i] - o[i])
    rng = lambda i: max(h[i] - l[i], 1e-9)
    up = lambda i: c[i] > o[i]
    avgb = sum(body(i) for i in range(-21, -1)) / 20
    down_trend = c[-2] < sum(c[-12:-2]) / 10            # short-term pullback into the pattern
    up_trend = c[-2] > sum(c[-12:-2]) / 10
    v20 = sum(vol[-21:-1]) / 20 if any(vol[-21:-1]) else 0
    vol_ok = bool(v20 and vol[-1] >= 1.3 * v20)
    out = []

    def add(name, bull, score, why):
        lv = [x for x in (levels or []) if x.get("price") and abs(l[-1] - x["price"]) / x["price"] <= 0.012] if bull else \
             [x for x in (levels or []) if x.get("price") and abs(h[-1] - x["price"]) / x["price"] <= 0.012]
        s = score + (1 if lv else 0) + (1 if vol_ok else 0)
        out.append({"name": name, "bull": bull, "score": s, "why": why, "at": [x["name"] for x in lv][:3], "volume_surge": vol_ok,
                    "date": b[-1][0], "extra": s >= 4})
    i, j = -1, -2
    if down_trend and not up(j) and up(i) and o[i] <= c[j] and c[i] >= o[j] and body(i) > body(j) * 1.1:
        add("Bullish engulfing", True, 3, "today's up candle fully covers yesterday's down candle after a pullback")
    if up_trend and up(j) and not up(i) and o[i] >= c[j] and c[i] <= o[j] and body(i) > body(j) * 1.1:
        add("Bearish engulfing", False, 3, "today's down candle fully covers yesterday's up candle after a run-up")
    lower = min(o[i], c[i]) - l[i]
    upper = h[i] - max(o[i], c[i])
    if down_trend and lower >= 2 * max(body(i), rng(i) * 0.05) and upper <= body(i) + rng(i) * 0.1 and c[i] >= l[i] + rng(i) * 0.6:
        add("Hammer", True, 2, "long lower wick — buyers rejected lower prices after a pullback")
    if up_trend and upper >= 2 * max(body(i), rng(i) * 0.05) and lower <= body(i) + rng(i) * 0.1 and c[i] <= l[i] + rng(i) * 0.4:
        add("Shooting star", False, 2, "long upper wick — sellers rejected higher prices after a run-up")
    k = -3
    if down_trend and not up(k) and body(k) > avgb and body(j) < body(k) * 0.4 and up(i) and c[i] > (o[k] + c[k]) / 2:
        add("Morning star", True, 3, "big down day, small indecision day, then a strong up day")
    if up_trend and up(k) and body(k) > avgb and body(j) < body(k) * 0.4 and not up(i) and c[i] < (o[k] + c[k]) / 2:
        add("Evening star", False, 3, "big up day, small indecision day, then a strong down day")
    if all(up(x) for x in (-3, -2, -1)) and c[-1] > c[-2] > c[-3] and all(body(x) > avgb * 0.8 for x in (-3, -2, -1)):
        add("Three white soldiers", True, 2, "three strong up days in a row")
    if all(not up(x) for x in (-3, -2, -1)) and c[-1] < c[-2] < c[-3] and all(body(x) > avgb * 0.8 for x in (-3, -2, -1)):
        add("Three black crows", False, 2, "three strong down days in a row")
    if down_trend and not up(j) and up(i) and o[i] < l[j] and c[i] > (o[j] + c[j]) / 2 and c[i] < o[j]:
        add("Piercing line", True, 2, "opened below yesterday's low and closed above its midpoint")
    if len(c) >= 200:
        s50 = [sum(c[x - 50:x]) / 50 for x in (len(c) - 1, len(c))]
        s200 = [sum(c[x - 200:x]) / 200 for x in (len(c) - 1, len(c))]
        if s50[0] <= s200[0] and s50[1] > s200[1]:
            add("Golden cross", True, 3, "50-day average just crossed above the 200-day")
        if s50[0] >= s200[0] and s50[1] < s200[1]:
            add("Death cross", False, 3, "50-day average just crossed below the 200-day")
    hi52 = max(h[-252:-1]) if len(h) > 30 else None
    if hi52 and c[-1] > hi52 and vol_ok:
        add("52-week-high breakout", True, 3, "closed above the prior 52-week high on heavy volume")
    out.sort(key=lambda x: -x["score"])
    return out


def _nasdaq_json(url: str, timeout: int = 20) -> dict:
    r = requests.get(url, headers=NASDAQ_HDR, timeout=timeout)
    return (r.json() or {}).get("data") or {}


_mb_lock = threading.Lock()
_mb_last = [0.0]
MB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GEXWalls/1.0 personal research"
_MB_STRIP = re.compile(r"\s*(?:\d+(?:\.\d+)? of 5 stars|Not Rated|Subscribe to MarketBeat.*)$")
BULL_RATINGS = re.compile(r"buy|overweight|outperform|positive|accumulate|add\b", re.I)
BEAR_RATINGS = re.compile(r"sell|underweight|underperform|negative|reduce", re.I)


def mb_ratings(ticker: str) -> list:
    """Analyst rating actions for one stock — date, firm, analyst, action, rating, target — from MarketBeat's public
    forecast page (an aggregator: brokers don't publish their ratings, so there is no primary source). 1 request / 3 s."""
    with _mb_lock:
        wait = 3.0 - (time.time() - _mb_last[0])
        if wait > 0:
            time.sleep(wait)
        _mb_last[0] = time.time()
    r = requests.get(f"https://www.marketbeat.com/stocks/NASDAQ/{ticker}/forecast/",
                     headers={"User-Agent": MB_UA, "Accept": "text/html"}, timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"MarketBeat HTTP {r.status_code}")
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", r.text, re.S):
        if "analyst-rankings" not in row and "ratings-screener" not in row:
            continue
        cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", cells[0]) if cells else None
        if not m or len(cells) < 5:
            continue
        tg = re.findall(r"\$([\d,]+(?:\.\d+)?)", cells[5] if len(cells) > 5 else "")
        out.append({"date": f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}", "firm": _MB_STRIP.sub("", cells[1]),
                    "analyst": _MB_STRIP.sub("", cells[2]), "action": cells[3], "rating": cells[4],
                    "rating_new": re.split(r"\s*(?:➝|->|→)\s*", cells[4])[-1].strip(),
                    "target": _num(tg[-1]) if tg else None})
    return out


# The ranked analysts that earn a ★ — only the top of each published ranking (update when a new ranking comes out).
#  TipRanks: top 10 U.S. analysts of 2025, ranked on success rate + average return of their calls (CNBC, 2026-01-25).
#  Extel (ex-Institutional Investor) 2026 All-America: #1 ("First Team") per sector, voted by 4,451 buy-side
#  investors. Extel's full table is subscriber-only, so these are the #1s the firms published themselves
#  (Jefferies results page, Evercore ISI press release 2026-10-20) — other firms' #1s are not in yet.
#  StarMine (LSEG): no stock-analyst awards found after 2020; today's StarMine awards cover economists / FX only.
TOP_SOURCES = {"TipRanks": "TipRanks top 10 U.S. analysts of 2025 (CNBC, Jan 25, 2026) — ranked on how their calls worked out",
               "Extel": "Extel 2026 All-America Research survey, #1 in sector (buy-side vote; #1s published by Jefferies and Evercore ISI)"}
TOP_ANALYSTS = [{"src": "TipRanks", "rank": r, "name": n, "firm": f, "label": f"TipRanks #{r} of 2025"} for r, n, f in (
    (1, "Sam Slutsky", "LifeSci Capital"), (2, "Richard Shannon", "Craig-Hallum"), (3, "Joseph Stringer", "Needham"),
    (4, "Myles Minter", "William Blair"), (5, "Ruben Roy", "Stifel Nicolaus"), (6, "Seamus Fernandez", "Guggenheim"),
    (7, "Thomas Smith", "Leerink Partners"), (8, "Mark Miller", "Benchmark"), (9, "Allison Bratzel", "Piper Sandler"),
    (10, "Julian Harrison", "BTIG"))] + [{"src": "Extel", "rank": 1, "name": n, "firm": f, "label": f"Extel #1 {sec}", "sic": sic} for n, f, sec, sic in (
    # sic: SEC industry codes where a rating from this firm with no analyst name is most likely this analyst's
    # (left empty when the firm has other ranked analysts in the same industries)
    ("Akash Tewari", "Jefferies", "Biotech Small & Midcap", []), ("Julien Dumoulin-Smith", "Jefferies", "Utilities", []),
    ("Sheila Kahyaoglu", "Jefferies", "Aerospace & Defense Electronics", [(3720, 3729), (3760, 3769), (3812, 3812)]),
    ("Philip Ng", "Jefferies", "Paper & Packaging", [(2600, 2679), (3220, 3221), (3410, 3412)]),
    ("Tycho Peterson", "Jefferies", "Life Science & Diagnostic Tools", [(3826, 3826)]),
    ("Chris LaFemina", "Jefferies", "Metals & Mining", [(1000, 1099), (3310, 3399)]),
    ("John Pancari", "Evercore ISI", "Banks Midcap", []), ("Umer Raffat", "Evercore ISI", "Biotech Largecap, Pharma Major & Specialty", []),
    ("Elizabeth Anderson", "Evercore ISI", "Health Care Tech & Distribution", [(5047, 5047), (5122, 5122)]),
    ("Stephen Kim", "Evercore ISI", "Homebuilders & Building Products", [(1520, 1531)]),
    ("Thomas Gallagher", "Evercore ISI", "Insurance Life", [(6311, 6311)]),
    ("Amit Daryanani", "Evercore ISI", "IT Hardware & EMS", [(3570, 3579), (3661, 3669), (3670, 3673), (3675, 3679)]),
    ("David Raso", "Evercore ISI", "Machinery, Engineering & Construction", [(3510, 3558), (3560, 3569)]),
    ("Steve Sakwa", "Evercore ISI", "REITs", [(6798, 6798)]), ("David Palmer", "Evercore ISI", "Restaurants", [(5812, 5812)]))]


def top_analyst_hits(ratings: list, sic: int | None = None) -> list:
    """Latest rating (last 12 months) on this stock by each ranked analyst (TOP_ANALYSTS + config 'top_analysts_extra'),
    matched by last name + firm. When MarketBeat leaves the analyst's name blank, a row from the same firm counts as
    'likely' theirs only if the company's SEC industry code is in that analyst's coverage list. The direction (Buy /
    Hold / Sell) comes from their latest row that states a rating (a bare price-target change carries none)."""
    cfg = load_config()
    top = TOP_ANALYSTS + [t for t in (cfg.get("top_analysts_extra") or []) if isinstance(t, dict)]
    cutoff = (et_now().date() - timedelta(days=365)).isoformat()
    rows = sorted((x for x in ratings if x.get("date", "") >= cutoff), key=lambda x: x["date"], reverse=True)
    hits: dict = {}
    for t in top:
        if not t.get("name") or not t.get("firm"):
            continue
        last = str(t["name"]).split()[-1].lower()
        fk = str(t["firm"]).split()[0].lower().rstrip(".")
        named = [x for x in rows if fk in x["firm"].lower() and x.get("analyst") and last in x["analyst"].lower()]
        guess = [] if named or not sic else [x for x in rows if fk in x["firm"].lower() and not x.get("analyst")
                                             and any(lo <= sic <= hi for lo, hi in (t.get("sic") or []))]
        mine = named or guess
        if not mine:
            continue
        rn = next((x.get("rating_new") for x in mine if x.get("rating_new")), "") or ""
        hits[t["name"]] = {**mine[0], "rank": t.get("rank"), "top_name": t["name"], "top_firm": t["firm"],
                           "src": t.get("src") or "your list", "label": t.get("label") or t.get("src") or "",
                           "likely": not named, "dir_rating": rn,
                           "bull": bool(BULL_RATINGS.search(rn)), "bear": bool(BEAR_RATINGS.search(rn))}
    return sorted(hits.values(), key=lambda h: (h.get("rank") or 99, h["src"]))


def top_consensus(hits: list) -> dict | None:
    """How the ranked analysts who cover a stock line up: counts by direction, sources, and whether they all agree."""
    if not hits:
        return None
    rated = [h for h in hits if h.get("dir_rating")]
    bull = sum(1 for h in rated if h.get("bull"))
    bear = sum(1 for h in rated if h.get("bear"))
    hold = len(rated) - bull - bear
    srcs = sorted({h.get("src") or "your list" for h in hits})
    agree = len(rated) >= 2 and (bull == len(rated) or bear == len(rated) or hold == len(rated))
    lean = ("Buy" if bull > max(bear, hold) else "Sell" if bear > max(bull, hold) else "Hold" if hold > max(bull, bear)
            else "Mixed") if rated else "No rating stated"
    return {"n": len(hits), "rated": len(rated), "buy": bull, "hold": hold, "sell": bear, "sources": srcs, "agree": agree, "lean": lean,
            "cross_source": agree and len(srcs) >= 2}


def analyst_view(ticker: str) -> dict:
    """Nasdaq.com analyst data: consensus rating, buy/hold/sell counts, price targets, EPS revisions, covering firms."""
    out = {}
    try:
        t = _nasdaq_json(f"https://api.nasdaq.com/api/analyst/{ticker}/targetprice")
        co = t.get("consensusOverview") or {}
        hc = t.get("historicalConsensus") or []
        out.update(target=_num(co.get("priceTarget")), low=_num(co.get("lowPriceTarget")), high=_num(co.get("highPriceTarget")),
                   buy=co.get("buy"), hold=co.get("hold"), sell=co.get("sell"),
                   consensus=((hc[-1].get("z") or {}).get("consensus") if hc else None),
                   target_3m_ago=(hc[-4].get("y") if len(hc) >= 4 else None))
    except Exception:
        pass
    try:
        r = _nasdaq_json(f"https://api.nasdaq.com/api/analyst/{ticker}/ratings")
        out.update(mean_rating=r.get("meanRatingType"), firms=r.get("brokerNames") or [],
                   changes=r.get("upgradesDowngrades") or [])
    except Exception:
        pass
    try:
        m = _nasdaq_json(f"https://api.nasdaq.com/api/analyst/{ticker}/estimate-momentum")
        ec = m.get("estimatesChanged") or {}
        out["revisions_4w"] = {"q_up": (ec.get("quarterDataUp") or {}).get("changeValue"),
                               "q_down": (ec.get("quarterDataDown") or {}).get("changeValue"),
                               "y_up": (ec.get("yearDataUp") or {}).get("changeValue"),
                               "y_down": (ec.get("yearDataDown") or {}).get("changeValue")}
    except Exception:
        pass
    star_firms = [f.upper() for f in (load_config().get("star_firms") or [])]
    out["star_firms"] = [f for f in out.get("firms", []) if any(s in f.upper() for s in star_firms)]
    return out


def news_view(ticker: str, cik: int | None) -> dict:
    """Latest headlines (Nasdaq.com; company press releases flagged) and the company's own SEC filings (8-K etc.)."""
    out = {"headlines": [], "filings": []}
    try:
        d = _nasdaq_json(f"https://api.nasdaq.com/api/news/topic/articlebysymbol?q={ticker}|stocks&offset=0&limit=8")
        for x in (d.get("rows") or [])[:8]:
            pub = x.get("publisher") or ""
            out["headlines"].append({"title": html.unescape(x.get("title") or ""), "publisher": pub, "ago": x.get("ago"),
                                     "date": x.get("created"), "url": "https://www.nasdaq.com" + (x.get("url") or ""),
                                     "press_release": bool(re.search(r"GlobeNewswire|Business ?Wire|PR ?Newswire|ACCESSWIRE|Newsfile", pub, re.I))})
    except Exception:
        pass
    return out


def _fact(facts: dict, names: list, unit: str = "USD", forms=("10-K", "10-Q")) -> list:
    """[(end, val, form, fp)] for the first us-gaap/dei concept that has data, newest last."""
    for ns, nm in names:
        try:
            pts = facts["facts"][ns][nm]["units"][unit]
        except KeyError:
            continue
        pts = [p for p in pts if p.get("form") in forms and p.get("end")]
        if pts:
            seen = {}
            for p in pts:
                seen[(p["end"], p.get("fp"))] = p
            return sorted(((p["end"], p["val"], p.get("form"), p.get("fp"), p.get("start")) for p in seen.values()),
                          key=lambda x: x[0])
    return []


HEALTH_V = 5          # bump to refresh cached health grades / badges after a rule change
FUND_NOTE = "ETF / fund / commodity trust — no company health grade or dilution check (share count changes are creations and redemptions)"
_FUND_RE = re.compile(r"\b(ETF|ETN|Fund|iShares|SPDR|ProShares|Direxion|Invesco QQQ)\b", re.I)


def health_view(ticker: str) -> dict:
    """Financial health and dilution from the company's own SEC filings (XBRL company facts + filing index).
    ETFs, funds and commodity trusts get no grade: their share counts move with creations/redemptions."""
    cik, name = sec_cik(ticker)
    if not cik:
        return {"error": "not in SEC's ticker list (ETFs and funds have no company financials)"}
    out = {"cik": cik, "company": name}
    if ticker in SCAN_ETFS or ticker in PUT_ETFS or _FUND_RE.search(name or ""):
        return {**out, "fund": True, "error": FUND_NOTE}
    try:
        f = _sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", 40).json()
    except Exception as e:
        if "404" in str(e):
            return {**out, "error": "no XBRL financial statements on file (ETFs, trusts and funds don't file them)"}
        return {**out, "error": f"SEC company facts unavailable ({str(e).split(' for url')[0][:80]})"}
    G = lambda *n: _fact(f, [("us-gaap", x) for x in n])
    cash = G("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash")
    sti = G("ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent", "MarketableSecuritiesCurrent")
    debt = G("LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "ConvertibleNotesPayable")
    ca, cl = G("AssetsCurrent"), G("LiabilitiesCurrent")
    assets, liab = G("Assets"), G("Liabilities")
    rev = G("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax")
    ni = G("NetIncomeLoss", "ProfitLoss")
    ocf = G("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations")
    sh = _fact(f, [("dei", "EntityCommonStockSharesOutstanding")], "shares", forms=("10-K", "10-Q", "10-K/A", "10-Q/A"))
    last = lambda L: L[-1][1] if L else None
    out["asof"] = cash[-1][0] if cash else (assets[-1][0] if assets else None)
    out["cash"] = (last(cash) or 0) + (last(sti) or 0) if cash else None
    out["debt"] = last(debt)
    out["current_ratio"] = last(ca) / last(cl) if last(ca) and last(cl) else None
    out["liab_to_assets"] = last(liab) / last(assets) if last(liab) and last(assets) else None
    # trailing-12-month figures from 10-K/10-Q durations: use the latest annual (FY) value
    def fy(L):                                          # full-year durations only (start→end about a year)
        o = []
        for x in L:
            try:
                if x[3] == "FY" and x[4] and 350 <= (date.fromisoformat(x[0]) - date.fromisoformat(x[4])).days <= 380:
                    o.append(x)
            except ValueError:
                pass
        return o
    rv, nv, ov = fy(rev), fy(ni), fy(ocf)
    out["revenue_fy"] = rv[-1][1] if rv else None
    out["revenue_growth"] = (rv[-1][1] / rv[-2][1] - 1) * 100 if len(rv) >= 2 and rv[-2][1] else None
    out["net_income_fy"] = nv[-1][1] if nv else None
    out["op_cash_flow_fy"] = ov[-1][1] if ov else None
    try:
        sub = _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", 30).json()
    except Exception:
        sub = {}
    try:
        sic = int(sub.get("sic") or 0)
    except ValueError:
        sic = 0
    out["sic"], out["industry"] = sic, sub.get("sicDescription")
    if sic == 6221 or (not sic and str(sub.get("entityType") or "") == "other"):
        return {"cik": cik, "company": name, "sic": sic, "industry": sub.get("sicDescription"), "fund": True, "error": FUND_NOTE}
    fin = 6000 <= sic < 6800                          # banks, lenders, insurers: current ratio / cash burn don't apply
    out["financial_co"] = fin
    if fin:
        out["current_ratio"] = None
    if (not fin and out["op_cash_flow_fy"] is not None and out["op_cash_flow_fy"] < 0 and out["cash"]
            and (out["net_income_fy"] is None or out["net_income_fy"] < 0)):
        out["runway_months"] = out["cash"] / (-out["op_cash_flow_fy"] / 12)
    if sh:
        out["shares_now"], out["shares_date"] = sh[-1][1], sh[-1][0]
        yr = [x for x in sh if x[0] <= (date.fromisoformat(sh[-1][0]) - timedelta(days=330)).isoformat()]
        if yr and yr[-1][1]:
            out["shares_1y_ago"] = yr[-1][1]
            ratio = sh[-1][1] / yr[-1][1]
            if ratio >= 1.8 and abs(ratio - round(ratio)) < 0.08:
                out["split_note"] = f"share count up {ratio:.0f}× — most likely a {round(ratio)}-for-1 stock split, not dilution"
            elif ratio <= 0.55 and abs(1 / ratio - round(1 / ratio)) < 0.12:
                out["split_note"] = f"share count down to 1/{round(1 / ratio)} — most likely a reverse split"
                out["reverse_split"] = True
            else:
                out["dilution_1y_pct"] = (ratio - 1) * 100
    try:
        rc = sub.get("filings", {}).get("recent", {})
        cutoff = (et_now().date() - timedelta(days=365)).isoformat()
        offer = [{"form": fm, "date": d_, "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"}
                 for fm, d_, acc, doc in zip(rc.get("form", []), rc.get("filingDate", []), rc.get("accessionNumber", []),
                                             rc.get("primaryDocument", []))
                 if d_ >= cutoff and fm in ("S-3", "S-3ASR", "S-3/A", "S-1", "S-1/A", "424B5", "424B3", "424B4", "424B2", "F-3", "F-1")]
        out["offering_filings_12m"] = offer[:12]
        out["n_424b_12m"] = sum(1 for x in offer if x["form"].startswith("424B"))
        out["recent_8k"] = [{"date": d_, "items": it, "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"}
                            for fm, d_, acc, doc, it in zip(rc.get("form", []), rc.get("filingDate", []), rc.get("accessionNumber", []),
                                                            rc.get("primaryDocument", []), rc.get("items", [""] * len(rc.get("form", []))))
                            if fm == "8-K"][:6]
    except Exception:
        pass
    # plain grade
    pts, why = 0, []
    if out.get("current_ratio") is not None:
        pts += 1 if out["current_ratio"] >= 1.5 else 0 if out["current_ratio"] >= 1 else -1
        why.append(f"current ratio {out['current_ratio']:.1f}")
    if out.get("net_income_fy") is not None:
        pts += 1 if out["net_income_fy"] > 0 else -1
        why.append("profitable" if out["net_income_fy"] > 0 else "losing money")
    if out.get("op_cash_flow_fy") is not None and not fin:
        pts += 1 if out["op_cash_flow_fy"] > 0 else -1
    if out.get("reverse_split"):
        pts -= 1
        why.append(out["split_note"])
    if out.get("runway_months") is not None and out["runway_months"] < 12:
        pts -= 2
        why.append(f"~{out['runway_months']:.0f} months of cash at the current burn")
    dil = out.get("dilution_1y_pct")
    if dil is not None:
        pts -= 2 if dil > 10 else 1 if dil > 3 else 0
        if dil > 3:
            why.append(f"shares up {dil:.0f}% in a year (dilution)")
        elif dil < -1:
            pts += 1
            why.append(f"buying back shares ({dil:.0f}% in a year)")
    if out.get("n_424b_12m"):
        pts -= 1
        why.append(f"{out['n_424b_12m']} offering prospectus filing(s) (424B — stock or debt) in 12 months")
    if out.get("revenue_growth") is not None:
        pts += 1 if out["revenue_growth"] > 10 else -1 if out["revenue_growth"] < -10 else 0
    out["grade"] = "A" if pts >= 3 else "B" if pts >= 1 else "C" if pts >= -1 else "D" if pts >= -3 else "F"
    out["grade_why"] = why
    out["dilution_risk"] = bool((dil or 0) > 10 or ((out.get("n_424b_12m") or 0) >= 2 and (dil is None or dil > 3)) or
                                (out.get("runway_months") is not None and out["runway_months"] < 12))
    return out


def insights(ticker: str, bars: list | None = None, levels: list | None = None, light: bool = False) -> dict:
    """Everything beyond the option chain for one ticker. Fundamentals cached 12 h, news/analysts 1 h, on disk."""
    ticker = clean_ticker(ticker)
    now = time.time()
    with _ins_lock:
        cache = _ins_mem or _read_json(INSIGHT_PATH, {}) or {}
        _ins_mem.update(cache)
    c = _ins_mem.get(ticker) or {}
    out = {"ticker": ticker}
    if (now - c.get("t_health", 0) > 12 * 3600 or (c.get("health") or {}).get("hv") != HEALTH_V) and sec_user_agent():
        try:
            c["health"] = health_view(ticker)
        except Exception as e:
            c["health"] = {"error": str(e)}
        c["health"]["hv"] = HEALTH_V
        c["t_health"] = now
    if now - c.get("t_an", 0) > 3600:
        c["analyst"] = analyst_view(ticker)
        c["t_an"] = now
    if ticker not in SCAN_ETFS and ticker not in PUT_ETFS and (now - c.get("t_mb", 0) > 24 * 3600 or c.get("mbv") != HEALTH_V):
        c["mbv"] = HEALTH_V
        try:
            c["top"] = top_analyst_hits(mb_ratings(ticker), (c.get("health") or {}).get("sic"))
            c["t_mb"], c["top_err"] = now, None
        except Exception as e:
            c["top_err"], c["t_mb"] = str(e)[:120], now - 20 * 3600          # retry in ~4 h
    if not light and now - c.get("t_news", 0) > 900:
        c["news"] = news_view(ticker, (c.get("health") or {}).get("cik"))
        c["t_news"] = now
    with _ins_lock:
        _ins_mem[ticker] = c
        if len(_ins_mem) > 600:
            for k in sorted(_ins_mem, key=lambda k: _ins_mem[k].get("t_an", 0))[:100]:
                _ins_mem.pop(k, None)
        _write_json(INSIGHT_PATH, _ins_mem)
    out.update({k: c.get(k) for k in ("health", "analyst", "news") if c.get(k) is not None})
    out["top_analysts"], out["top_err"] = c.get("top") or [], c.get("top_err")
    out["top_consensus"] = top_consensus(out["top_analysts"])
    out["top_sources"] = TOP_SOURCES
    if bars is None:
        try:
            h = daily_history(ticker)
            bars = [[r.date, r.open, r.high, r.low, r.close, r.volume] for r in h.tail(300).itertuples()]
        except Exception:
            bars = []
    out["price"] = bars[-1][4] if bars else None
    out["candles"] = candle_signals(bars, levels)
    try:
        out["ext"] = ext_hours(ticker)
    except Exception:
        out["ext"] = {}
    out["badges"] = insight_badges(out)
    with _ins_lock:
        _ins_mem.setdefault(ticker, {}).update(badges=out["badges"], t_badges=time.time())
    return _clean(json.loads(json.dumps(out, default=str)))


def insight_badges(x: dict) -> list:
    """Short labels for tables: ✦ pattern, ★ analysts, ⚠ dilution / weak health, 24h."""
    b = []
    for cs in (x.get("candles") or [])[:1]:
        if cs["score"] >= 3:
            b.append({"t": ("✦ " if cs.get("extra") else "") + cs["name"], "c": "call" if cs["bull"] else "put",
                      "tip": cs["why"] + (" · at " + ", ".join(cs["at"]) if cs.get("at") else "") + (" · volume surge" if cs.get("volume_surge") else "")})
    a = x.get("analyst") or {}
    n = sum(v or 0 for v in (a.get("buy"), a.get("hold"), a.get("sell")))
    if n >= 3 and a.get("buy") and a["buy"] / n >= 0.75:
        up = (a["target"] / x["price"] - 1) * 100 if a.get("target") and x.get("price") else None
        b.append({"t": "Analysts " + f"{a['buy']}/{n} Buy" + (f" · target {up:+.0f}%" if up is not None else ""), "c": "acc",
                  "tip": "Nasdaq.com consensus" + (" · your starred firms: " + ", ".join(a.get("star_firms") or []) if a.get("star_firms") else "")})
    elif a.get("star_firms"):
        b.append({"t": a["star_firms"][0].title(), "c": "acc", "tip": "covered by your listed firm(s): " + ", ".join(a["star_firms"])})
    tops = x.get("top_analysts") or []
    tc = x.get("top_consensus") or top_consensus(tops)
    if tops:
        who = "; ".join(f"{h['label']}: {h['top_name']}{' (likely — MarketBeat omits the name)' if h.get('likely') else ''} ({h['top_firm']}) {h.get('action', '')} {h.get('rating') or h.get('dir_rating', '')}".strip()
                        + (f" target ${h['target']:,.2f}" if h.get("target") else "") + f" on {h['date']}" for h in tops)
        cls = "call" if tc["lean"] == "Buy" else "put" if tc["lean"] == "Sell" else "acc"
        if tc["n"] == 1:
            h0 = tops[0]
            t = f"★ {h0['src']} {'#' + str(h0['rank']) + ' ' if h0['src'] == 'TipRanks' else '#1 '}{h0['top_name'].split()[-1]}: " \
                + (h0.get("dir_rating") or "target") + (f" ${h0['target']:,.0f}" if h0.get("target") and not h0.get("rating_new") else "") \
                + (" (likely)" if h0.get("likely") else "")
        else:
            t = ("★★ " if tc["agree"] else "★ ") + f"{tc['n']} top analysts: " + \
                (f"all {tc['lean']}" if tc["agree"] else f"{tc['buy']} Buy · {tc['hold']} Hold · {tc['sell']} Sell"
                                     + (f" · {tc['n'] - tc['rated']} no rating" if tc["n"] > tc["rated"] else "")) + \
                (" (" + " + ".join(tc["sources"]) + ")" if len(tc["sources"]) >= 2 else "")
        b.insert(0, {"t": t, "c": cls, "tip": who + " · ratings via MarketBeat; rankings: " + "; ".join(TOP_SOURCES[sx] for sx in tc["sources"] if sx in TOP_SOURCES)})
    h = x.get("health") or {}
    if h.get("dilution_risk"):
        b.append({"t": "⚠ Dilution", "c": "put", "tip": "; ".join(h.get("grade_why") or [])})
    if h.get("grade") in ("D", "F"):
        b.append({"t": f"Health {h['grade']}", "c": "put", "tip": "; ".join(h.get("grade_why") or [])})
    elif h.get("grade") == "A":
        b.append({"t": "Health A", "c": "call", "tip": "; ".join(h.get("grade_why") or [])})
    ov = ((x.get("ext") or {}).get("overnight")) or {}
    if ov.get("traded"):
        b.append({"t": "24h", "c": "fib", "tip": f"traded overnight (8 PM–4 AM ET) on Blue Ocean / MOON / Bruce ATS — FINRA week of {ov.get('week')}"})
    return b


_badge_q: dict = {"running": False}


def badges_for(tickers: list) -> dict:
    """Cached badge lists for many tickers at once; missing ones are filled in the background (light insights)."""
    out, missing = {}, []
    ov = (overnight_symbols() or {}).get("symbols") or {}
    for tk in tickers:
        c = _ins_mem.get(tk) or (_read_json(INSIGHT_PATH, {}) or {}).get(tk)
        if c and c.get("badges") is not None and time.time() - c.get("t_badges", 0) < 12 * 3600 and c.get("bv") == HEALTH_V:
            out[tk] = c["badges"]
        else:
            out[tk] = [{"t": "24h", "c": "fib", "tip": "traded overnight (8 PM–4 AM ET) per FINRA ATS data"}] if tk in ov else []
            missing.append(tk)
    if missing and not _badge_q["running"]:
        def fill(lst):
            _badge_q["running"] = True
            try:
                def one(tk):
                    try:
                        bd = insights(tk, light=True).get("badges") or []
                    except Exception:
                        bd = []
                    with _ins_lock:
                        c = _ins_mem.setdefault(tk, {})
                        c["badges"], c["t_badges"], c["bv"] = bd, time.time(), HEALTH_V
                with ThreadPoolExecutor(3) as ex:
                    list(ex.map(one, lst[:80]))
                with _ins_lock:
                    _write_json(INSIGHT_PATH, _ins_mem)
            finally:
                _badge_q["running"] = False
        threading.Thread(target=fill, args=(missing,), daemon=True).start()
    return {"badges": out, "pending": len(missing)}


# ---------------------------------------------------------------- stars: unusually good return for the chance of profit
def pick_stars(p: float | None, g: float | None, l: float = 1.0) -> int:
    """★ / ★★ when the model's chance and the return together give a clearly positive expected value.
    p = chance of profit (0-1), g = return if it works (fraction of capital), l = loss if it doesn't (fraction)."""
    if p is None or g is None:
        return 0
    ev = p * g - (1 - p) * l
    if ev >= 0.12 and p >= 0.75 and g >= 0.30:
        return 2
    if ev >= 0.05 and p >= 0.70 and g >= 0.25:
        return 1
    return 0


# ---------------------------------------------------------------- live block-trade tape + nightly dark-pool alerts
BLOCKS_LOG = APP_DIR / "blocks_log.jsonl"
_blocks: dict = {"recent": [], "seen": {}, "last_poll": None, "note": "", "tickers": []}
_blocks_lock = threading.Lock()


def nasdaq_trades(ticker: str, limit: int = 1000) -> list:
    """Real-time prints from Nasdaq Last Sale Plus (Nasdaq's exchanges + trades reported to the FINRA/Nasdaq TRF,
    where many dark-pool trades print). Venue isn't given per print."""
    cls = _nq_class.get(ticker) or ("etf" if ticker in SCAN_ETFS else "stocks")
    r = requests.get(f"https://api.nasdaq.com/api/quote/{ticker}/realtime-trades?&limit={limit}&fromTime=00:00",
                     headers=NASDAQ_HDR, timeout=15)
    d = (r.json() or {}).get("data") or {}
    rows = d.get("rows") or []
    out = []
    for x in rows:
        t = str(x.get("nlsTime") or x.get("time") or "").strip()
        px_ = _num(x.get("nlsPrice") or x.get("price"))
        sz = _num(x.get("nlsShareVolume") or x.get("volume") or x.get("size"))
        if t and px_ and sz:
            out.append({"t": t, "px": px_, "sz": sz})
    return out


def block_tickers(cfg: dict) -> list:
    tks = list(cfg.get("block_tickers") or [])
    if not tks:
        tks = [w["ticker"] for w in cfg.get("watchlist", [])] + [t["ticker"] for t in load_trades(True)] \
            + list(cfg.get("puts_watchlist") or [])[:10] + ["SPY", "QQQ"]
    return [t for t in dict.fromkeys(tks) if re.fullmatch(r"[A-Z]{1,6}", t)][:25]


def poll_blocks(cfg: dict) -> list:
    """Find new prints >= the dollar or share threshold for the tape; alert on the biggest ones."""
    min_usd, min_sh, alert_usd = float(cfg.get("block_min_usd", 1e6)), float(cfg.get("block_min_shares", 20000)), \
        float(cfg.get("block_alert_usd", 5e6))
    day = et_now().date().isoformat()
    new_all, alerts = [], []
    tks = block_tickers(cfg)
    _blocks["tickers"] = tks
    for tk in tks:
        try:
            trades = nasdaq_trades(tk)
        except Exception:
            continue
        seen = _blocks["seen"].setdefault(tk, set())
        for x in trades:
            usd = x["px"] * x["sz"]
            if usd < min_usd and x["sz"] < min_sh:
                continue
            key = f"{day}|{x['t']}|{x['px']}|{x['sz']}"
            if key in seen:
                continue
            seen.add(key)
            b = {"day": day, "t": x["t"], "ticker": tk, "px": x["px"], "sz": x["sz"], "usd": usd}
            new_all.append(b)
    if new_all:
        new_all.sort(key=lambda b: b["t"])
        with _blocks_lock:
            _blocks["recent"] = (_blocks["recent"] + new_all)[-300:]
        with _io_lock:
            with BLOCKS_LOG.open("a", encoding="utf-8") as f:
                for b in new_all:
                    f.write(json.dumps(b) + "\n")
        big = [b for b in new_all if b["usd"] >= alert_usd]
        by_tk: dict = {}
        for b in big:
            by_tk.setdefault(b["ticker"], []).append(b)
        for tk, bs in by_tk.items():
            tot = sum(b["usd"] for b in bs)
            top = max(bs, key=lambda b: b["usd"])
            _emit(alerts, cfg, tk, "block", "warn", f"{tk} block print{'s' if len(bs) > 1 else ''}: {money(tot)}",
                  f"{len(bs)} print(s) ≥ {money(alert_usd)}; largest {int(top['sz']):,} sh @ {top['px']:.2f} at {top['t']} ET "
                  f"(Nasdaq Last Sale Plus — exchange or FINRA/Nasdaq TRF).")
    _blocks["last_poll"] = et_now().strftime("%H:%M:%S ET")
    return alerts


def nightly_darkpool_alerts(cfg: dict) -> list:
    """After FINRA posts the day's off-exchange file (~6 PM ET): alert on unusual short % or volume for watched names."""
    alerts = []
    days = finra_days(21)
    if len(days) < 8:
        return alerts
    d_last, last = days[-1]
    state = _read_json(STATE_PATH, {})
    if state.get("_dp_alert_day") == d_last.isoformat():
        return alerts
    for tk in block_tickers(cfg):
        v = last.get(tk)
        hist = [m[tk] for _, m in days[:-1] if tk in m and m[tk][1]]
        if not v or not v[1] or len(hist) < 8:
            continue
        ratios = [a / b * 100 for a, b in hist[-20:]]
        mu = sum(ratios) / len(ratios)
        sd = (sum((x - mu) ** 2 for x in ratios) / len(ratios)) ** 0.5 or 1e-9
        sp, vavg = v[0] / v[1] * 100, sum(b for _, b in hist[-20:]) / len(hist[-20:])
        z, vr = (sp - mu) / sd, v[1] / vavg if vavg else 0
        if abs(z) >= 2 or vr >= 2:
            _emit(alerts, cfg, tk, "darkpool", "warn", f"{tk} dark pool {d_last:%m/%d}: short {sp:.0f}% (z {z:+.1f})",
                  f"Off-exchange short % {sp:.1f}% vs 20-day avg {mu:.1f}%; off-exchange volume {vr:.1f}× normal (FINRA daily file).")
    state = _read_json(STATE_PATH, {})
    state["_dp_alert_day"] = d_last.isoformat()
    with _io_lock:
        _write_json(STATE_PATH, state)
    return alerts


def blocks_loop() -> None:
    while True:
        try:
            cfg = load_config()
            now = et_now()
            mins = now.hour * 60 + now.minute
            wk = now.weekday() < 5
            if cfg.get("blocks_on", True) and wk and 9 * 60 + 30 <= mins <= 16 * 60 + 15:
                poll_blocks(cfg)
                _blocks["note"] = "watching"
            else:
                _blocks["note"] = "market closed" if cfg.get("blocks_on", True) else "off"
            if wk and mins >= 19 * 60:
                nightly_darkpool_alerts(cfg)
        except Exception as e:
            _blocks["note"] = f"error: {e}"
        time.sleep(60)


def blocks_today() -> list:
    """Today's (or the last session's) logged blocks, newest first."""
    with _blocks_lock:
        rec = list(_blocks["recent"])
    if not rec and BLOCKS_LOG.exists():
        try:
            lines = BLOCKS_LOG.read_text(encoding="utf-8").splitlines()[-400:]
            rec = [json.loads(x) for x in lines if x.strip()]
            if rec:
                last_day = rec[-1]["day"]
                rec = [b for b in rec if b["day"] == last_day]
        except (OSError, ValueError):
            rec = []
    return rec[::-1][:200]


# ---------------------------------------------------------------- edge: early moves, theme leaders, swing setups, algos
# Ideas borrowed from how institutions and dealers work, kept to what free daily data can support:
#   * factor move  = a day's gain of 1.5+ ATR on above-average volume (range expansion = institutions arriving)
#   * RS line      = stock / SPY and stock / its theme basket; a new high in the line before price = quiet leadership
#   * coil         = volatility contraction (Bollinger width in its lowest fifth, or Bollinger inside Keltner)
#   * accumulation = up-day volume vs down-day volume (50 days) and "pocket pivots"
# Every setup is a fixed rule that is back-tested on the cached daily history, so each one carries its own record.
EDGE_PATH = APP_DIR / "edge_results.json"
PAPER_PATH = APP_DIR / "algo_paper.json"
EDGE_THEMES = {
    "AI infrastructure": ["IREN", "APLD", "CIFR", "NBIS", "CRWV", "WULF", "CORZ", "HUT"],
    "Drones & defense tech": ["KTOS", "AVAV", "RCAT", "ONDS", "UMAC", "RKLB", "PLTR"],
    "Quantum": ["IONQ", "RGTI", "QBTS", "QUBT"],
    "Nuclear & uranium": ["OKLO", "SMR", "CCJ", "LEU", "NNE", "UEC", "BWXT"],
    "Gold & silver miners": ["HL", "CDE", "AG", "EXK", "PAAS", "AEM", "KGC", "NEM"],
    "Crypto": ["COIN", "MSTR", "MARA", "RIOT", "HOOD"],
    "AI semis": ["NVDA", "AMD", "AVGO", "MRVL", "MU", "ALAB", "CRDO", "TSM"],
    "AI power": ["VST", "CEG", "GEV", "VRT", "BE", "ETN"],
    "Space": ["RKLB", "ASTS", "LUNR", "RDW"],
}
# Rules were chosen on 2022-2024 bars and then checked once on 2025-2026 (see audit changelog 2026-11-12). Tested and
# dropped because they did not beat arbitrary entries: plain coil breakouts, pullbacks to the 20-day, RSI(2) dips.
EDGE_SETUPS = {
    "THF": ("Theme-leader factor move", "Up 1.5+ ATR on above-average volume, closing in the top half of the day, above the "
            "50-day average, while the stock's theme is beating SPY (theme ÷ SPY above its 50-day average and higher than a month ago). "
            "Buy next open; stop 1.5 ATR; target 2× the risk; out after 15 sessions."),
    "PWR": ("Power factor move", "A factor move of 2.5+ ATR, or one on 3× normal volume. Buy next open; stop under the "
            "factor day's low (at most 1.5 ATR); target 2× the risk; out after 15 sessions."),
    "THL": ("Theme leader: RS before price", "Stock ÷ its theme basket at a new 6-month high while the price is still 3%+ under "
            "its own high (first time in 5 sessions). Buy next open; stop 1.5 ATR; target 3× the risk; out after 20 sessions."),
    "THN": ("Theme RS new high", "Stock ÷ its theme basket makes a new 6-month high (first in 5 sessions): the stock is beating "
            "its own group. Buy next open; stop 1.5 ATR; target 3× the risk; out after 20 sessions."),
    "RSL": ("RS line leads price (vs SPY)", "Stock ÷ SPY at a new 6-month high while the price is still 3%+ under its own high "
            "(first time in 5 sessions). Buy next open; stop 1.5 ATR; target 3× the risk; out after 20 sessions."),
}
EDGE_CLV = {"trend": ("Trend stack", "Price above the 20-day, 20 above 50, 50 above 200, and the 50-day rising"),
            "rs": ("RS high vs SPY", "Stock ÷ SPY made a 6-month high in the last 5 sessions"),
            "rsth": ("RS high vs theme", "Stock ÷ its theme basket made a 6-month high in the last 5 sessions"),
            "theme": ("Theme leading", "The stock's theme is beating SPY"),
            "factor": ("Factor move", "A 1.5+ ATR up day on above-average volume in the last 5 sessions, still above that day's midpoint"),
            "accum": ("Accumulation", "Up-day volume at least 1.3× down-day volume over 50 sessions"),
            "pocket": ("Pocket pivots", "2+ up days in the last 10 whose volume beat every down day of the prior 10"),
            "fib": ("Above the 78.6% Fib", "In the top part of its 6-month range: above the 78.6% retracement level"),
            "macd": ("MACD rising", "MACD above zero and above its signal line, with the gap widening"),
            "rsi": ("RSI 55–75", "14-day RSI strong but not overbought"),
            "vol": ("Volume surge", "Last session up on 1.5×+ its average volume"),
            "release": ("Squeeze released", "Volatility was coiled in the last 5 sessions and price closed above the prior 20-day high")}
EDGE_HOLD = {"THF": 15, "PWR": 15, "THL": 20, "THN": 20, "RSL": 20, "BASE": 15}
EDGE_TGT = {"THF": 2.0, "PWR": 2.0, "THL": 3.0, "THN": 3.0, "RSL": 3.0, "BASE": 2.0}
_edge = {"running": False, "progress": "", "error": None}
_edge_lock = threading.Lock()


def _edge_frame(tk: str) -> pd.DataFrame | None:
    rows = _read_json(HIST_DIR / f"{tk}.json", None)
    if not rows:
        return None
    h = pd.DataFrame(rows)
    for c in ("open", "high", "low", "close", "volume"):
        if c not in h:
            return None
        h[c] = pd.to_numeric(h[c], errors="coerce")
    h["date"] = h["date"].astype(str).str[:10]
    h = h.dropna(subset=["open", "high", "low", "close"]).drop_duplicates("date", keep="last").sort_values("date")
    h["volume"] = h["volume"].fillna(0.0)
    h = h[(h["close"] > 0) & (h["high"] >= h["low"])].reset_index(drop=True)
    return h if len(h) >= 130 else None


def _edge_feat(h: pd.DataFrame, spy: pd.Series, theme: pd.Series | None, theme_up: pd.Series | None) -> pd.DataFrame:
    """Daily features and setup signals for one ticker. spy / theme: closes indexed by date string."""
    c, o, hi, lo, v = h["close"], h["open"], h["high"], h["low"], h["volume"]
    pc = c.shift()
    tr = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    atrp = atr.shift()
    va = v.rolling(50, min_periods=20).mean().shift()
    f = pd.DataFrame({"date": h["date"], "o": o, "h": hi, "l": lo, "c": c, "v": v, "atr": atr})
    f["fm"] = (c - pc) / atrp
    f["rvol"] = (v / va).where(va > 0)
    rng = (hi - lo)
    f["cloc"] = ((c - lo) / rng.where(rng > 0)).fillna(0.5)
    f["s5"], f["s20"], f["s50"] = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(50).mean()
    f["s200"] = c.rolling(200, min_periods=150).mean()
    f["ext"] = (c - f["s50"]) / atr
    f["pre_ext"] = (pc - f["s50"].shift()) / atrp
    f["factor"] = (f["fm"] >= 1.5) & (c > pc) & (f["rvol"] > 1.0) & (f["cloc"] >= 0.5)
    sp = spy.reindex(h["date"]).ffill()
    sp.index = h.index
    rs = c / sp
    f["rs"] = rs
    f["rs_nh"] = rs >= rs.rolling(126, min_periods=100).max() * 0.999
    p_hi = c.rolling(126, min_periods=100).max()
    f["p_nh"] = c >= p_hi * 0.999
    f["rs_lead"] = f["rs_nh"] & (c < p_hi * 0.97)
    f["rs63"] = rs / rs.shift(63) - 1
    if theme is not None:
        th = theme.reindex(h["date"]).ffill()
        th.index = h.index
        trt = c / th
        f["th_nh"] = trt >= trt.rolling(126, min_periods=100).max() * 0.999
        f["th_lead"] = f["th_nh"] & (c < p_hi * 0.97)
        f["th63"] = trt / trt.shift(63) - 1
        tu = theme_up.reindex(h["date"]).ffill()
        tu.index = h.index
        f["theme_up"] = tu.fillna(False).astype(bool)
    else:
        f["th_nh"], f["th_lead"], f["th63"], f["theme_up"] = False, False, np.nan, False
    sd = c.rolling(20).std()
    bbw = 4 * sd / f["s20"]
    f["bbw_pct"] = bbw.rolling(120, min_periods=60).rank(pct=True)
    f["coil"] = (f["bbw_pct"] <= 0.2) | (2 * sd < 1.5 * atr)
    f["coil5"] = f["coil"].astype(float).rolling(5).max().shift().fillna(0) > 0
    up = c > pc
    dn_v = v.where(c < pc, 0.0)
    f["udvr"] = (v.where(up, 0.0).rolling(50, min_periods=30).sum() / dn_v.rolling(50, min_periods=30).sum().where(lambda x: x > 0))
    f["pocket"] = up & (v > dn_v.shift().rolling(10).max()) & (c > f["s50"])
    f["pp10"] = f["pocket"].astype(float).rolling(10).sum()
    f["brk"] = c > hi.shift().rolling(20).max()
    d = c.diff()
    g2 = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean()
    l2 = (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
    f["rsi2"] = (100 - 100 / (1 + g2 / l2.where(l2 > 0))).fillna(100)
    rising = f["s50"] > f["s50"].shift(10)
    f["rising"] = rising
    a50 = c > f["s50"]
    first = lambda s, n: s & ~(s.astype(float).shift().rolling(n).max().fillna(0) > 0)
    # clover alignment: 12 bullish conditions, counted (tested 2022-24, checked 2025-26: 8+ at once is rare, ~2-3% of days)
    hi6, lo6 = c.rolling(126, min_periods=100).max(), c.rolling(126, min_periods=100).min()
    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = e12 - e26
    hist_ = macd - macd.ewm(span=9, adjust=False).mean()
    dd_ = c.diff()
    g14 = dd_.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l14 = (-dd_.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    r14 = 100 - 100 / (1 + g14 / l14.where(l14 > 0))
    fmid = ((hi + lo) / 2).where(f["factor"]).ffill(limit=4)
    recent5 = lambda s_: s_.astype(float).rolling(5).max().fillna(0) > 0
    f["K_trend"] = (c > f["s20"]) & (f["s20"] > f["s50"]) & (f["s50"] > f["s200"]) & rising
    f["K_rs"] = recent5(f["rs_nh"])
    f["K_rsth"] = recent5(f["th_nh"].astype(bool))
    f["K_theme"] = f["theme_up"].astype(bool)
    f["K_factor"] = recent5(f["factor"]) & (c >= fmid)
    f["K_accum"] = f["udvr"] >= 1.3
    f["K_pocket"] = f["pp10"] >= 2
    f["K_fib"] = ((c - lo6) / (hi6 - lo6).where(hi6 > lo6)) >= 0.786
    f["K_macd"] = (hist_ > 0) & (macd > 0) & (hist_ > hist_.shift(3))
    f["K_rsi"] = (r14 >= 55) & (r14 <= 75)
    f["K_vol"] = (f["rvol"] >= 1.5) & (c > pc)
    f["K_release"] = f["coil5"] & f["brk"]
    f["clv"] = f[[k_ for k_ in f.columns if k_.startswith("K_")]].fillna(False).astype(int).sum(axis=1)
    ok = c >= 3
    f["S_THF"] = f["factor"] & f["theme_up"] & a50 & ok
    f["S_PWR"] = f["factor"] & ((f["fm"] >= 2.5) | (f["rvol"] >= 3)) & ok
    f["S_THL"] = first(f["th_lead"].astype(bool), 5) & ok
    f["S_THN"] = first(f["th_nh"].astype(bool), 5) & ok
    f["S_RSL"] = first(f["rs_lead"], 5) & ok
    return f


def _edge_plan(f: pd.DataFrame, i: int, key: str, entry: float) -> tuple | None:
    """(stop, target) for a signal on bar i entered at `entry`. None if the entry is already through the stop."""
    a = float(f["atr"].iat[i])
    if not a or a <= 0:
        return None
    stop = entry - 1.5 * a
    if key == "PWR":
        stop = max(float(f["l"].iat[i]) - 0.1 * a, stop)
    if entry - stop < 0.3 * a:
        return None
    return stop, entry + EDGE_TGT[key] * (entry - stop)


def _edge_sim(f: pd.DataFrame, i: int, key: str) -> dict | None:
    """Trade a signal from bar i: in at the next open, out at stop / target / time. Stop is checked before target on
    the same bar (conservative). R = result in units of the initial risk, after 0.1% round-trip cost."""
    n = len(f)
    if i + 1 >= n:
        return {"status": "pending"}
    entry = float(f["o"].iat[i + 1])
    pl = _edge_plan(f, i, key, entry)
    if not pl:
        return None
    stop, target = pl
    risk = entry - stop
    hold = EDGE_HOLD[key]
    last = min(n - 1, i + hold)
    for j in range(i + 1, last + 1):
        o_, h_, l_, c_ = (float(f[k].iat[j]) for k in ("o", "h", "l", "c"))
        out = why = None
        if l_ <= stop:
            out, why = (min(stop, o_) if j > i + 1 else stop), "stop"
        elif h_ >= target:
            out, why = (max(target, o_) if j > i + 1 else target), "target"
        elif j == i + hold:
            out, why = c_, "time"
        if out is not None:
            r = (out - entry) / risk - 0.001 * entry / risk
            if abs(r) > 15:                                    # bad bar in the data
                return None
            return {"status": "closed", "entry_date": f["date"].iat[i + 1], "entry": entry, "stop": stop, "target": target,
                    "exit_date": f["date"].iat[j], "exit": out, "why": why, "r": r, "bars": j - i}
    return {"status": "open", "entry_date": f["date"].iat[i + 1], "entry": entry, "stop": stop, "target": target,
            "r": (float(f["c"].iat[n - 1]) - entry) / risk, "bars": n - 1 - i}


def _edge_stats(tr: list, recent: str | None = None) -> dict:
    """tr: [(signal_date, exit_date, R, bars, why)] closed trades."""
    if not tr:
        return {"n": 0}
    tr = sorted(tr, key=lambda x: x[1])
    r = np.array([x[2] for x in tr], dtype=float)
    wins, loss = r[r > 0], r[r <= 0]
    eq = np.cumsum(r)
    dd = float((np.maximum.accumulate(np.maximum(eq, 0)) - eq).max()) if len(eq) else 0.0
    half = len(r) // 2
    step = max(1, len(eq) // 60)
    cut = sorted({x[0] for x in tr})
    rec = np.array([x[2] for x in tr if recent and x[0] >= recent], dtype=float)
    return {"n": int(len(r)), "win": float((r > 0).mean()), "avg_r": float(r.mean()),
            "pf": float(wins.sum() / -loss.sum()) if len(loss) and loss.sum() < 0 else None,
            "target": float(np.mean([x[4] == "target" for x in tr])), "stop": float(np.mean([x[4] == "stop" for x in tr])),
            "bars": float(np.mean([x[3] for x in tr])), "tot_r": float(r.sum()), "dd_r": dd,
            "h1": float(r[:half].mean()) if half else None, "h2": float(r[half:].mean()) if half else None,
            "rec_n": int(len(rec)), "rec_r": float(rec.mean()) if len(rec) else None,
            "rec_win": float((rec > 0).mean()) if len(rec) else None, "first": cut[0],
            "from": tr[0][0], "to": tr[-1][1], "eq": [round(float(x), 2) for x in eq[::step]] + [round(float(eq[-1]), 2)]}


def _edge_universe() -> dict:
    """{ticker: (kind, theme)} — the scan universe, watchlists and the fine-grained themes."""
    cfg = load_config()
    uni = {**{k: ("ETF", v) for k, v in SCAN_ETFS.items()}, **{k: ("Stock", v) for k, v in SCAN_STOCKS.items()}}
    for th, ms in EDGE_THEMES.items():
        for tk in ms:
            uni[tk] = ("Stock", th) if tk not in uni or uni[tk][0] != "ETF" else uni[tk]
    for key in ("watchlist", "puts_watchlist"):
        for it in cfg.get(key) or []:
            tk = it.get("ticker") if isinstance(it, dict) else it
            if tk and TICKER_RE.match(str(tk)):
                uni.setdefault(str(tk), ("Watchlist", ""))
    for fp in HIST_DIR.glob("*.json"):                       # anything else the scans already keep history for
        uni.setdefault(fp.stem, ("Other", ""))
    return uni


def _rotation_algo(frames: dict) -> dict | None:
    """Sector / theme ETF rotation: every 5 sessions hold the 3 ETFs with the best blend of 3-month and 1-month
    return, only those rising and above their 50-day average (otherwise that slot stays in cash). Compared with SPY."""
    etfs = [k for k in SCAN_ETFS if k not in ("SPY", "QQQ", "IWM", "DIA") and k in frames]
    if len(etfs) < 8 or "SPY" not in frames:
        return None
    px = pd.DataFrame({k: frames[k].set_index("date")["close"] for k in etfs + ["SPY"]}).sort_index().ffill()
    px = px.tail(520)
    if len(px) < 140:
        return None
    score = 0.6 * (px / px.shift(63) - 1) + 0.4 * (px / px.shift(21) - 1)
    ok = (px > px.rolling(50).mean()) & (score > 0)
    ret = px.pct_change().fillna(0.0)
    start = 70
    hold, eq, spy_eq, held_hist = [], [1.0], [1.0], []
    dates = list(px.index)
    for i in range(start, len(px)):
        if (i - start) % 5 == 0:                               # rebalance on yesterday's close, trade today
            s = score.iloc[i - 1][etfs].where(ok.iloc[i - 1][etfs]).dropna().sort_values(ascending=False)
            new = list(s.index[:3])
            cost = 0.0005 * len(set(new) ^ set(hold)) / 3
            hold = new
            held_hist.append((dates[i], hold))
        else:
            cost = 0.0
        day = float(np.mean([ret[k].iat[i] for k in hold]) * len(hold) / 3) if hold else 0.0
        eq.append(eq[-1] * (1 + day - cost))
        spy_eq.append(spy_eq[-1] * (1 + float(ret["SPY"].iat[i])))
    e, s_ = np.array(eq), np.array(spy_eq)
    mdd = lambda a: float((1 - a / np.maximum.accumulate(a)).max())
    step = max(1, len(e) // 60)
    s_now = score.iloc[-1][etfs].where(ok.iloc[-1][etfs]).dropna().sort_values(ascending=False)
    nxt = 5 - ((len(px) - start) % 5)
    return {"from": dates[start], "to": dates[-1], "ret": float(e[-1] - 1), "spy": float(s_[-1] - 1), "dd": mdd(e),
            "spy_dd": mdd(s_), "eq": [round(float(x), 4) for x in e[::step]] + [round(float(e[-1]), 4)],
            "spy_eq": [round(float(x), 4) for x in s_[::step]] + [round(float(s_[-1]), 4)],
            "holding": [{"ticker": k, "name": SCAN_ETFS.get(k, "")} for k in hold],
            "next": [{"ticker": k, "name": SCAN_ETFS.get(k, ""), "score": float(v) * 100} for k, v in s_now.head(3).items()],
            "next_in": int(nxt % 5), "rebalances": len(held_hist)}


def run_edge(fetch: bool = True) -> dict:
    """Build early-move rows, theme table, live swing setups and the back-test of every setup. ~10 s on cached bars."""
    if not _edge_lock.acquire(blocking=False):
        return _read_json(EDGE_PATH, {}) or {}
    _edge.update(running=True, error=None, progress="loading price history")
    try:
        uni = _edge_universe()
        if fetch:                                              # themes may name tickers the scans have never loaded
            need = [t for t in uni if not (HIST_DIR / f"{t}.json").exists()][:60]
            for n_, tk in enumerate(need, 1):
                _edge["progress"] = f"price history for new tickers {n_}/{len(need)}"
                try:
                    scan_history(tk)
                except Exception:
                    pass
        frames = {}
        for tk in uni:
            h = _edge_frame(tk)
            if h is not None:
                frames[tk] = h
        if "SPY" not in frames:
            try:
                scan_history("SPY")
                frames["SPY"] = _edge_frame("SPY")
            except Exception:
                pass
        if frames.get("SPY") is None:
            raise RuntimeError("no SPY history yet — run the scanner (Walls & spreads) once")
        if _session_status() != "Market Open":                 # CBOE's daily history can lag a whole session (11-16 bars still
            try:                                                # missing at 00:15 ET 11-17): add Schwab's final regular-session bar
                fb = live_bars(list(frames))                    # to every file that is exactly one session behind (not saved)
            except Exception:
                fb = {}
            prev = max(h["date"].iat[-1] for h in frames.values())
            today_ = et_now().date().isoformat()
            for tk, b in fb.items():
                h = frames.get(tk)
                if h is not None and not b.get("live") and prev < b["date"] <= today_ and h["date"].iat[-1] == prev:
                    h2 = _with_bar(h, b)
                    h2["volume"] = pd.to_numeric(h2["volume"], errors="coerce").fillna(0.0)
                    frames[tk] = h2
        last_day = frames["SPY"]["date"].iat[-1]
        frames = {k: h for k, h in frames.items() if h["date"].iat[-1] >= frames["SPY"]["date"].iat[-4]}   # drop stale files
        spy = frames["SPY"].set_index("date")["close"]
        # theme baskets: equal-weight index of the member stocks' daily returns (3+ members), else the sector ETF
        members: dict = {}
        for tk, (kind, th) in uni.items():
            if kind == "Stock" and th and tk in frames:
                members.setdefault(th, []).append(tk)
        for th, ms in EDGE_THEMES.items():                     # a stock can sit in its sector and in a theme
            members[th] = [t for t in ms if t in frames]
        for tk, sec in SCAN_STOCKS.items():
            if tk in frames and tk not in members.setdefault(sec, []):
                members[sec].append(tk)
        closes = {tk: frames[tk].set_index("date")["close"] for tk in frames}
        theme_idx, theme_up = {}, {}
        for th, ms in members.items():
            if len(ms) >= 3:
                rets = pd.DataFrame({m: closes[m].pct_change() for m in ms}).sort_index().tail(520)
                idx = (1 + rets.mean(axis=1, skipna=True).fillna(0)).cumprod()
            elif SECTOR_ETF.get(th) in closes:
                idx = closes[SECTOR_ETF[th]]
            else:
                continue
            rel = idx / spy.reindex(idx.index).ffill()
            theme_idx[th] = idx
            theme_up[th] = (rel > rel.rolling(50, min_periods=30).mean()) & (rel > rel.shift(21))
        _edge["progress"] = "signals"
        feats, sims = {}, {k: [] for k in EDGE_SETUPS}
        clv_all: dict = {k: [] for k in range(12)}
        _sd = frames["SPY"]["date"]
        clv_from = _sd.iat[min(200, max(0, len(_sd) - 60))]
        base, live, rows = [], [], []
        earn = (_read_json(EARN_PATH, {}) or {}).get("map") or {}
        paper = _read_json(PAPER_PATH, {}) or {}
        plog = paper.get("signals") or []
        pseen = {(p["algo"], p["ticker"], p["sig"]) for p in plog}
        ptrades = []
        today_s = et_now().date().isoformat()
        for n_, (tk, h) in enumerate(frames.items(), 1):
            kind, th = uni.get(tk, ("Other", ""))
            th_ = th if th in theme_idx else None
            try:
                f = _edge_feat(h, spy, theme_idx.get(th_) if kind != "ETF" else None, theme_up.get(th_) if kind != "ETF" else None)
            except Exception:
                continue
            feats[tk] = f
            n = len(f)
            warm = min(200, n - 60)
            for key in EDGE_SETUPS:
                idxs = np.flatnonzero(f["S_" + key].fillna(False).to_numpy())
                busy_until = -1
                for i in idxs:
                    if i < warm or i <= busy_until:
                        continue
                    r = _edge_sim(f, int(i), key)
                    if not r:
                        continue
                    if r["status"] == "closed":
                        sims[key].append((f["date"].iat[i], r["exit_date"], r["r"], r["bars"], r["why"]))
                        busy_until = i + r["bars"]
                    else:
                        busy_until = n
                    if n - 1 - i <= 2:                          # fresh signal (today or the last 2 sessions)
                        live.append((tk, key, int(i), r))
            for i in range(warm + (hash(tk) % 10 if False else sum(map(ord, tk)) % 10), n - 16, 10):                   # baseline: same exit rules from arbitrary days
                if f["c"].iat[i] < 3:
                    continue
                r = _edge_sim(f, i, "BASE")
                if r and r["status"] == "closed":
                    base.append((f["date"].iat[i], r["exit_date"], r["r"], r["bars"], r["why"]))
            for p in plog:                                      # forward (paper) record of signals published live
                if p["ticker"] == tk and p["algo"] in EDGE_SETUPS:
                    ix = np.flatnonzero((f["date"] == p["sig"]).to_numpy())
                    if len(ix):
                        r = _edge_sim(f, int(ix[0]), p["algo"])
                        if r:
                            ptrades.append({**p, **{k: v for k, v in r.items()}})
            p20 = (f["c"].shift(-20) / f["o"].shift(-1) - 1).to_numpy()   # next open -> 20 sessions later
            cv = f["clv"].to_numpy()
            dts_ = f["date"].to_numpy()
            for i in range(warm, n - 21):
                if f["c"].iat[i] >= 3 and np.isfinite(p20[i]) and abs(p20[i]) < 3 and dts_[i] >= clv_from:
                    clv_all[min(int(cv[i]), 11)].append(p20[i])
            # ---- latest-bar row
            L = f.iloc[-1]
            if kind == "ETF" and tk in ("SPY",):
                continue
            fi = np.flatnonzero(f["factor"].fillna(False).to_numpy()[-11:])
            fm_days = int(10 - fi[-1]) if len(fi) else None
            fm_row = f.iloc[n - 1 - fm_days] if fm_days is not None else None
            px = float(L["c"])
            held = bool(fm_row is not None and px >= (fm_row["h"] + fm_row["l"]) / 2)
            above_low = bool(fm_row is not None and px >= fm_row["l"])
            ext = float(L["ext"]) if pd.notna(L["ext"]) else 0.0
            trend = bool(px > L["s50"] and (pd.isna(L["s200"]) or L["s50"] > L["s200"]) and L["rising"])
            coil_now = bool(L["coil"])
            coil_pre = bool(fm_row is not None and f["coil5"].iat[n - 1 - fm_days])
            sc = 0.0
            if fm_days is not None and fm_days <= 2 and held:
                sc += 25 * min(float(fm_row["fm"]) / 2.5, 1.0)
            elif fm_days is not None and above_low:
                sc += 12
            sc += (15 if L["th_nh"] else 0) + (8 if L["th_lead"] else 0) + (6 if L["rs_nh"] else 0) + (6 if L["rs_lead"] else 0)
            sc += 12 if L["theme_up"] else 0
            sc += 4 if (coil_now or coil_pre) else 0
            sc += (6 if pd.notna(L["udvr"]) and L["udvr"] >= 1.3 else 0) + (4 if L["pp10"] >= 2 else 0)
            sc += 4 if pd.notna(L["rvol"]) and L["rvol"] >= 1.5 and px > f["c"].iat[-2] else 0
            sc += 8 if trend else 0
            sc -= (20 if ext > 6 else 0) + (10 if px < L["s50"] else 0)
            sc = max(0.0, min(100.0, sc))
            if ext > 6:
                stage = "Extended"
            elif fm_days is not None and fm_days <= 2 and held and float(fm_row["pre_ext"] if pd.notna(fm_row["pre_ext"]) else 0) <= 4:
                stage = "Ignition"
            elif fm_days is not None and above_low and px > L["s50"]:
                stage = "Follow-through"
            elif L["rs_lead"] or L["th_lead"]:
                stage = "RS lead"
            elif coil_now and (px > L["s50"] or (pd.notna(L["rs63"]) and L["rs63"] > 0)):
                stage = "Coiling"
            else:
                stage = ""
            rsi, _ = rsi14(tk, 0, "", h)
            rows.append({"ticker": tk, "kind": kind, "theme": th or "", "price": px, "rsi": rsi, "score": sc, "stage": stage,
                         "fm": float(L["fm"]) if pd.notna(L["fm"]) else None, "rvol": float(L["rvol"]) if pd.notna(L["rvol"]) else None,
                         "fm_days": fm_days, "fm_x": float(fm_row["fm"]) if fm_row is not None else None,
                         "fm_rvol": float(fm_row["rvol"]) if fm_row is not None else None,
                         "fm_low": float(fm_row["l"]) if fm_row is not None else None, "fm_date": fm_row["date"] if fm_row is not None else None,
                         "held": held, "rs_nh": bool(L["rs_nh"]), "rs_lead": bool(L["rs_lead"]), "th_nh": bool(L["th_nh"]),
                         "th_lead": bool(L["th_lead"]), "theme_up": bool(L["theme_up"]),
                         "rs63": float(L["rs63"]) * 100 if pd.notna(L["rs63"]) else None,
                         "th63": float(L["th63"]) * 100 if pd.notna(L["th63"]) else None,
                         "coil": coil_now, "bbw_pct": float(L["bbw_pct"]) * 100 if pd.notna(L["bbw_pct"]) else None,
                         "udvr": float(L["udvr"]) if pd.notna(L["udvr"]) else None, "pp10": int(L["pp10"]) if pd.notna(L["pp10"]) else 0,
                         "ext": ext, "atr": float(L["atr"]), "d1": float(px / f["c"].iat[-2] - 1) * 100,
                         "earn": earn.get(tk) if kind != "ETF" else None, "asof": L["date"],
                         "clv": int(L["clv"]), "clv_on": [k_ for k_ in EDGE_CLV if bool(L["K_" + k_])],
                         "clv_days": int(next((j for j in range(0, min(n, 60)) if f["clv"].iat[n - 1 - j] < 8), 60)),
                         "clv_max5": int(f["clv"].tail(5).max())})
        _edge["progress"] = "back-test"
        sd = frames["SPY"]["date"]
        recent = sd.iat[max(0, len(sd) - 120)]                  # "recent" = the last 120 sessions
        t0 = sd.iat[min(200, max(0, len(sd) - 60))]             # test window = SPY's bars after the warm-up
        stats = {k: _edge_stats([x for x in v if x[0] >= t0], recent) for k, v in sims.items()}
        bstat = _edge_stats([x for x in base if x[0] >= t0], recent)
        b_r = bstat.get("avg_r") or 0.0
        # live setups -> swing rows: only rules whose back-test paid, trade still valid at the latest close
        swing, shadow = [], []
        # 12a rule (2026-11-21a): a setup whose forward (paper) log has 20+ closed trades averaging below the
        # arbitrary-entry baseline is paused - its signals are still logged and graded (so it can come back), not shown as picks
        fwd_off = {}
        for k_ in EDGE_SETUPS:
            fr_ = [float(p["r"]) for p in ptrades if p["algo"] == k_ and p.get("status") == "closed" and p.get("r") is not None]
            fwd_off[k_] = bool(len(fr_) >= 20 and float(np.mean(fr_)) < b_r)
        for tk, key, i, r in live:
            st = stats.get(key) or {}
            if st.get("n", 0) < 30 or st.get("avg_r", 0) <= max(0.0, b_r):   # must have paid, and beaten arbitrary entries
                continue
            f = feats[tk]
            px, a = float(f["c"].iat[-1]), float(f["atr"].iat[-1])
            if r.get("status") == "closed":
                continue
            if r.get("status") == "open":                       # entered at an earlier open: keep that stop and target
                stop, target = r["stop"], r["target"]
                if px <= stop or (target - px) / (px - stop) < 1.5:   # broke the stop, or too much of the move is gone
                    continue
            else:
                pl = _edge_plan(f, i, key, px)
                if not pl:
                    continue
                stop, target = pl
            kind, th = uni.get(tk, ("Other", ""))
            row = next((x for x in rows if x["ticker"] == tk), {})
            n_, w_ = st["n"], st["win"]
            (shadow if fwd_off.get(key) else swing).append({"ticker": tk, "sector": th, "setup": key, "kind": EDGE_SETUPS[key][0], "price": px,
                          "rsi": row.get("rsi"), "stop": stop, "target": target, "up": (target / px - 1) * 100,
                          "down": (1 - stop / px) * 100, "rr": (target - px) / (px - stop),
                          "p": (w_ * n_ + 0.36 * 30) / (n_ + 30), "bt_n": n_, "bt_win": w_, "bt_r": st["avg_r"],
                          "bt_rec": st.get("rec_r"), "bt_rec_n": st.get("rec_n"), "base_r": b_r,
                          "days": int(round(st["bars"])), "atr": a, "sig_date": f["date"].iat[i], "age": len(f) - 1 - i,
                          "score": row.get("score") or 0, "stage": row.get("stage") or "", "earn": row.get("earn"),
                          "quad": None, "mom": None, "sma20": float(f["s20"].iat[-1]), "sma50": float(f["s50"].iat[-1])})
            if (key, tk, f["date"].iat[i]) not in pseen:
                plog.append({"algo": key, "ticker": tk, "sig": f["date"].iat[i], "logged": today_s})
                pseen.add((key, tk, f["date"].iat[i]))
        best: dict = {}
        for s_ in sorted(swing, key=lambda x: (-x["bt_r"], -x["score"])):   # one row per ticker: its best-paying setup
            if s_["ticker"] in best:
                best[s_["ticker"]]["also"] = best[s_["ticker"]].get("also", []) + [s_["kind"]]
            else:
                best[s_["ticker"]] = s_
        swing = sorted(best.values(), key=lambda x: -(x["bt_r"] * 40 + x["score"]))
        # themes
        themes = []
        by_tk = {r_["ticker"]: r_ for r_ in rows}
        for th, idx in theme_idx.items():
            ms = [m for m in members.get(th, []) if m in feats]
            if not ms:
                continue
            rel = (idx / spy.reindex(idx.index).ffill()).dropna()
            if len(rel) < 70:
                continue
            ab = [m for m in ms if feats[m]["c"].iat[-1] > feats[m]["s50"].iat[-1]]
            lead = sorted(ms, key=lambda m: (-(by_tk.get(m, {}).get("th_nh") or 0), -(by_tk.get(m, {}).get("th63") or -999)))[:3]
            themes.append({"theme": th, "n": len(ms), "basket": len(members.get(th, [])) >= 3,
                           "r1": float(idx.iloc[-1] / idx.iloc[-22] - 1) * 100, "r3": float(idx.iloc[-1] / idx.iloc[-64] - 1) * 100,
                           "rs1": float(rel.iloc[-1] / rel.iloc[-22] - 1) * 100, "rs3": float(rel.iloc[-1] / rel.iloc[-64] - 1) * 100,
                           "rs_nh": bool(rel.iloc[-1] >= rel.tail(126).max() * 0.999), "up": bool(theme_up[th].iloc[-1]),
                           "breadth": 100 * len(ab) / len(ms),
                           "fm3": sum(1 for m in ms if (by_tk.get(m, {}).get("fm_days") is not None and by_tk[m]["fm_days"] <= 2)),
                           "leaders": [{"ticker": m, "th_nh": bool(by_tk.get(m, {}).get("th_nh")), "th63": by_tk.get(m, {}).get("th63")} for m in lead]})
        themes.sort(key=lambda x: -(x["rs1"] + x["rs3"] / 2))
        # paper (forward) results per algo
        pstat = {}
        for key in EDGE_SETUPS:
            cl = [(p["sig"], p["exit_date"], p["r"], p["bars"], p["why"]) for p in ptrades if p["algo"] == key and p.get("status") == "closed"]
            pstat[key] = {**{k: v for k, v in _edge_stats(cl).items() if k in ("n", "win", "avg_r", "tot_r")},
                          "open": sum(1 for p in ptrades if p["algo"] == key and p.get("status") in ("open", "pending"))}
        plog = plog[-3000:]
        with _io_lock:
            _write_json(PAPER_PATH, {"signals": plog, "note": "signals this app published live; outcomes are re-computed "
                                     "from daily bars with the fixed rules — no orders are ever placed"})
        algos = [{"key": k, "name": EDGE_SETUPS[k][0], "rules": EDGE_SETUPS[k][1], "bt": stats[k], "paper": pstat[k],
                  "live": [{"ticker": tk, "sig": feats[tk]["date"].iat[i], "status": r.get("status"), "entry": r.get("entry"),
                            "stop": r.get("stop"), "target": r.get("target"), "r": r.get("r"), "price": float(feats[tk]["c"].iat[-1])}
                           for tk, key, i, r in live if key == k and r.get("status") != "closed"],
                  "beats": bool(stats[k].get("n", 0) >= 30 and (stats[k].get("avg_r") or 0) > max(0.0, b_r)),
                  "paused": fwd_off.get(k, False)} for k in EDGE_SETUPS]
        rows.sort(key=lambda x: -x["score"])
        gexd = _read_json(GEX_DAILY_PATH, {}) or {}
        sqz = {x["ticker"]: x for x in ((_read_json(SQZ_PATH, {}) or {}).get("rows") or [])}
        for r_ in rows:                                         # live extras: shown next to the clovers, not in the back-test
            g_ = gexd.get(r_["ticker"]) or {}
            r_["gamma_pos"] = bool(g_ and (g_[sorted(g_)[-1]][0] or 0) > 0) if g_ else None
            q_ = sqz.get(r_["ticker"])
            r_["sq_fuel"] = bool(q_ and q_.get("si_pct", 0) >= 15 and q_.get("dtc", 0) >= 3)
        allp = np.array([x for v in clv_all.values() for x in v], dtype=float)
        tier = lambda lo_, hi_: np.array([x for k_, v in clv_all.items() if lo_ <= k_ <= hi_ for x in v], dtype=float)
        cstat = lambda a_: {"n": int(len(a_)), "up": float((a_ > 0).mean()) if len(a_) else None, "avg": float(a_.mean() * 100) if len(a_) else None,
                            "med": float(np.median(a_) * 100) if len(a_) else None, "big": float((a_ >= 0.10).mean()) if len(a_) else None,
                            "bad": float((a_ <= -0.10).mean()) if len(a_) else None}
        clover = {"all": cstat(allp), "t1": cstat(tier(8, 8)), "t2": cstat(tier(9, 9)), "t3": cstat(tier(10, 10)), "t4": cstat(tier(11, 11)),
                  "t8": cstat(tier(8, 11)), "low": cstat(tier(0, 3)), "from": clv_from,
                  "names": {k_: list(v) for k_, v in EDGE_CLV.items()},
                  "share": float(len(tier(8, 11)) / len(allp)) if len(allp) else None}
        rows = [r_ for r_ in rows if r_["stage"] or r_["score"] >= 25 or r_["kind"] in ("Stock", "Watchlist") or r_["clv"] >= 7]
        res = {"ts": et_now().strftime("%Y-%m-%d %H:%M ET"), "asof": last_day, "universe": len(frames), "rows": rows, "clover": clover,
               "themes": themes, "swing": swing, "algos": algos, "baseline": bstat, "test_from": t0, "rotation": _rotation_algo(frames),
               "paper_trades": sorted([{k: p.get(k) for k in ("algo", "ticker", "sig", "logged", "status", "entry", "stop", "target",
                                                                 "exit", "exit_date", "why", "r", "bars")} for p in ptrades],
                                      key=lambda p: p["sig"], reverse=True)[:200],
               "bars": int(np.median([len(h) for h in frames.values()]))}
        try:
            res["lev"] = run_lev(fetch)
        except Exception as e:
            res["lev"] = {"error": str(e), "rows": [], "stats": {}}
        res = _clean(json.loads(json.dumps(res, default=str)))
        with _io_lock:
            _write_json(EDGE_PATH, res)
        _edge["progress"] = f"done {res['ts']}"
        return res
    except Exception as e:
        _edge["error"] = str(e)
        raise
    finally:
        _edge["running"] = False
        _edge_lock.release()


# ---------------------------------------------------------------- leveraged ETFs: short-term rules, tested
# (underlying, bull ETF, bear ETF, leverage). Signals come from the UNDERLYING's daily bars; the trade is the bull ETF
# from the next open. Tested 2022-2024, checked on 2025 and 2026 (30 pairs): only the three rules below were positive
# in every period. Not kept: holding (median -21% / +23% / -11% with 50-66% drops), trend filters (50/200-day),
# 20-day breakouts, momentum holds, low-volatility holds, and every inverse-ETF rule tried (holding an inverse ETF
# lost a median 40-55% a year; shorting rips, breakdowns and down factor moves all lost).
LEV_PAIRS = [("QQQ", "TQQQ", "SQQQ", 3), ("SPY", "UPRO", "SPXU", 3), ("SMH", "SOXL", "SOXS", 3), ("IWM", "TNA", "TZA", 3),
             ("XLF", "FAS", "FAZ", 3), ("XBI", "LABU", "LABD", 3), ("GDX", "NUGT", "DUST", 2), ("GDXJ", "JNUG", "JDST", 2),
             ("XLE", "ERX", "ERY", 2), ("XOP", "GUSH", "DRIP", 2), ("TLT", "TMF", "TMV", 3), ("FXI", "YINN", "YANG", 3),
             ("XLK", "TECL", "TECS", 3), ("DIA", "UDOW", "SDOW", 3), ("KRE", "DPST", None, 3), ("XHB", "NAIL", None, 3),
             ("UNG", "BOIL", "KOLD", 2), ("USO", "UCO", "SCO", 2), ("SLV", "AGQ", "ZSL", 2), ("GLD", "UGL", "GLL", 2),
             ("IBIT", "BITX", None, 2), ("NVDA", "NVDL", None, 2), ("TSLA", "TSLL", None, 2), ("MSTR", "MSTU", None, 2),
             ("COIN", "CONL", None, 2), ("KWEB", "CWEB", None, 2), ("ITA", "DFEN", None, 3), ("XLU", "UTSL", None, 3),
             ("XLV", "CURE", None, 3), ("EEM", "EDC", "EDZ", 3)]
LEV_RULES = {
    "DIP": ("Dip in an uptrend", "Underlying above its 200-day average and its 2-day RSI under 5. Buy the bull ETF at the next "
            "open; sell at the open after the underlying closes above its 5-day average, or after 7 sessions.", 7),
    "WASH": ("Washout bounce", "Underlying below its 200-day average and its 2-day RSI under 5. Buy the bull ETF at the next "
             "open; sell at the open after the underlying closes above its 5-day average, or after 5 sessions.", 5),
    "FM": ("Factor move in an uptrend", "Underlying up 1.5+ ATR on above-average volume while above its 50-day average. Buy the "
           "bull ETF at the next open; sell at the open after the underlying closes below its 10-day average, or after 10 sessions.", 10),
}


def _lev_frame(tk: str, fetch: bool) -> pd.DataFrame | None:
    try:
        h = scan_history(tk) if fetch or not (HIST_DIR / f"{tk}.json").exists() else pd.DataFrame(_read_json(HIST_DIR / f"{tk}.json", []))
    except Exception:
        return None
    if h is None or len(h) < 120:
        return None
    h = h.copy()
    for c in ("open", "high", "low", "close", "volume"):
        h[c] = pd.to_numeric(h[c], errors="coerce")
    h["date"] = h["date"].astype(str).str[:10]
    h = h.dropna(subset=["open", "close"]).drop_duplicates("date", keep="last").sort_values("date").tail(520)
    return h.set_index("date")


def run_lev(fetch: bool = True) -> dict:
    rows, pooled = [], {k: [] for k in LEV_RULES}
    spy = _lev_frame("SPY", False)
    recent = spy.index[max(0, len(spy) - 120)] if spy is not None else ""
    for n_, (U, BU, BE, L) in enumerate(LEV_PAIRS, 1):
        _edge["progress"] = f"leveraged ETFs {n_}/{len(LEV_PAIRS)}"
        u, x = _lev_frame(U, fetch), _lev_frame(BU, fetch)
        if u is None or x is None:
            continue
        b = _lev_frame(BE, fetch) if BE else None
        c = u["close"]
        s5, s10, s50 = c.rolling(5).mean(), c.rolling(10).mean(), c.rolling(50).mean()
        s200 = c.rolling(200, min_periods=150).mean()
        d = c.diff()
        g2, l2 = d.clip(lower=0).ewm(alpha=0.5, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=0.5, adjust=False).mean()
        rsi2 = (100 - 100 / (1 + g2 / l2.where(l2 > 0))).fillna(100)
        pc = c.shift()
        atr = pd.concat([u["high"] - u["low"], (u["high"] - pc).abs(), (u["low"] - pc).abs()], axis=1).max(axis=1).ewm(alpha=1 / 14, adjust=False).mean()
        fm = (c - pc) / atr.shift()
        rvol = u["volume"] / u["volume"].rolling(50, min_periods=20).mean().shift()
        j = pd.DataFrame({"o": x["open"], "c": x["close"]}).reindex(u.index)
        ru, cc = c.pct_change(), j["c"].pct_change()
        intr = (j["c"] / j["o"] - 1).fillna(0.0)
        ov = ((1 + cc) / (1 + intr) - 1).where((cc - L * ru).abs() <= 0.25, 0.0).fillna(0.0)   # reverse splits / bad bars
        have = j["c"].notna().to_numpy()
        ok = s200.notna().to_numpy() & have
        ent = {"DIP": ((c > s200) & (rsi2 < 5)).to_numpy(), "WASH": ((c < s200) & (rsi2 < 5)).to_numpy(),
               "FM": ((fm >= 1.5) & (rvol > 1) & (c > s50)).to_numpy()}
        ext = {"DIP": (c > s5).to_numpy(), "WASH": (c > s5).to_numpy(), "FM": (c < s10).to_numpy()}
        dates, ovv, inv = list(u.index), ov.to_numpy(), intr.to_numpy()
        now, per = {}, {}
        for key, (_nm, _rl, maxd) in LEV_RULES.items():
            tr, i, n = [], 0, len(dates)
            while i < n - 1:
                if not (ent[key][i] and ok[i]):
                    i += 1
                    continue
                eq, k, out = 1.0 + inv[i + 1], i + 1, None            # in at the open after the signal
                while True:
                    if ext[key][k] or k - i >= maxd:
                        out = k
                        break
                    if k + 1 >= n:
                        break
                    k += 1
                    eq *= (1 + ovv[k]) * (1 + inv[k])
                if out is None:                                        # still open at the last bar
                    now[key] = {"state": "in", "since": dates[i + 1], "days": k - i, "left": maxd - (k - i), "pl": (eq - 1) * 100}
                    break
                if out + 1 < n:
                    eq *= 1 + ovv[out + 1]                             # out at the next open
                    tr.append((dates[i], dates[out + 1], (eq - 1 - 0.001) * 100, out + 1 - i))
                else:
                    now[key] = {"state": "exit", "since": dates[i + 1], "days": k - i, "pl": (eq - 1) * 100}
                i = out + 1
            if key not in now and ent[key][n - 1] and ok[n - 1]:
                now[key] = {"state": "buy"}
            pooled[key] += tr
            r_ = [t[2] for t in tr]
            per[key] = {"n": len(r_), "win": float(np.mean([v > 0 for v in r_])) if r_ else None, "avg": float(np.mean(r_)) if r_ else None}
        r63 = lambda s_: float(s_.iloc[-1] / s_.iloc[-64] - 1) * 100 if len(s_.dropna()) > 64 and s_.iloc[-64] else None
        bh = float(np.prod((1 + ovv[-63:]) * (1 + inv[-63:])) - 1) * 100
        rsi, _ = rsi14(U, 0, "", u.reset_index())
        rows.append({"u": U, "bull": BU, "bear": BE, "lev": L, "u_px": float(c.iloc[-1]), "u_rsi": rsi, "rsi2": float(rsi2.iloc[-1]),
                     "bull_px": float(x["close"].iloc[-1]), "bear_px": float(b["close"].iloc[-1]) if b is not None else None,
                     "bull_rsi": rsi14(BU, 0, "", x.reset_index())[0], "bear_rsi": rsi14(BE, 0, "", b.reset_index())[0] if b is not None else None,
                     "a200": bool(c.iloc[-1] > s200.iloc[-1]) if pd.notna(s200.iloc[-1]) else None, "a50": bool(c.iloc[-1] > s50.iloc[-1]),
                     "s5": float(s5.iloc[-1]), "s10": float(s10.iloc[-1]), "rv20": float(ru.tail(20).std() * math.sqrt(252) * 100),
                     "u63": r63(c), "bull63": bh, "drag": (bh - L * r63(c)) if r63(c) is not None else None,
                     "fm": float(fm.iloc[-1]) if pd.notna(fm.iloc[-1]) else None, "now": now, "bt": per, "asof": dates[-1]})
    stats = {}
    for key, tr in pooled.items():
        r_ = np.array([t[2] for t in tr], dtype=float)
        rec = np.array([t[2] for t in tr if t[0] >= recent], dtype=float)
        stats[key] = {"name": LEV_RULES[key][0], "rules": LEV_RULES[key][1], "n": int(len(r_)),
                      "win": float((r_ > 0).mean()) if len(r_) else None, "avg": float(r_.mean()) if len(r_) else None,
                      "med": float(np.median(r_)) if len(r_) else None, "worst": float(r_.min()) if len(r_) else None,
                      "best": float(r_.max()) if len(r_) else None, "days": float(np.mean([t[3] for t in tr])) if tr else None,
                      "rec_n": int(len(rec)), "rec_avg": float(rec.mean()) if len(rec) else None,
                      "rec_win": float((rec > 0).mean()) if len(rec) else None}
    return {"rows": rows, "stats": stats, "pairs": len(rows)}


def edge_data() -> dict:
    d = _read_json(EDGE_PATH, None)
    if not d:
        return {"empty": True, "status": _edge}
    d["status"] = _edge
    return d


def _edge_bg(fetch: bool = True) -> None:
    try:
        run_edge(fetch)
    except Exception as e:
        _edge.update(running=False, error=str(e))


def edge_loop() -> None:
    """Rebuild the early-move tables after each rotation scan and once after the close (when daily bars are final)."""
    last = ""
    while True:
        try:
            now = et_now()
            sr = (_read_json(SCAN_PATH, {}) or {}).get("rotation_ts") or ""
            ed = (_read_json(EDGE_PATH, {}) or {}).get("ts") or ""
            key = f"{now.date()} close" if (now.weekday() < 5 and now.hour * 60 + now.minute >= 16 * 60 + 25) else ""
            try:
                aged = time.time() - EDGE_PATH.stat().st_mtime > 2 * 3600      # built on daily bars: every scan is too often
            except OSError:
                aged = True
            if not _edge["running"] and ((sr and sr > ed and aged) or (key and key != last and ed < f"{now.date()} 16:25") or not ed):
                last = key or last
                _edge_bg(fetch=bool(key) or not ed)
        except Exception:
            pass
        time.sleep(120)


# ---------------------------------------------------------------- short squeeze monitor
# Free primary data: FINRA consolidated short interest (twice a month), FINRA daily off-exchange short volume,
# Nasdaq.com screener (price, market cap, volume), SEC public float, Interactive Brokers' public stock-loan file
# (borrow fee and shares available; a snapshot is saved each run so the trend builds up locally).
SQZ_PATH = APP_DIR / "squeeze_results.json"
BORROW_PATH = APP_DIR / "borrow_hist.json"
FLOAT_PATH = APP_DIR / "float_cache.json"
FINRA_SI_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
_sqz = {"running": False, "progress": "", "error": None}
_SQZ_SKIP = re.compile(r"acquisition|warrant|\bunits?\b|\brights?\b|preferred|depositary sh|notes due|\bSPAC\b|blank check", re.I)


def finra_si_bulk() -> tuple[str | None, dict]:
    """Latest FINRA short-interest settlement date and every symbol's row for it:
    {sym: {"short", "prev", "adv", "dtc", "chg"}}. ~22,000 rows in pages of 5,000."""
    hdr = {**SEC_UA, "Accept": "application/json"}
    start = (et_now().date() - timedelta(days=50)).isoformat()
    r = requests.post(FINRA_SI_URL, headers=hdr, timeout=30, json={
        "limit": 20, "compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": "SPY"}],
        "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start, "endDate": et_now().date().isoformat()}]})
    ds = sorted(x["settlementDate"] for x in (r.json() if r.status_code == 200 else []) if x.get("settlementDate"))
    if not ds:
        return None, {}
    d, out, off = ds[-1], {}, 0
    while off < 40000:
        r = requests.post(FINRA_SI_URL, headers=hdr, timeout=90, json={
            "limit": 5000, "offset": off, "compareFilters": [{"compareType": "EQUAL", "fieldName": "settlementDate", "fieldValue": d}]})
        rows = r.json() if r.status_code == 200 else []
        if not isinstance(rows, list) or not rows:
            break
        for x in rows:
            s = str(x.get("symbolCode") or "").upper()
            if s and x.get("currentShortPositionQuantity"):
                out[s] = {"short": x["currentShortPositionQuantity"], "prev": x.get("previousShortPositionQuantity"),
                          "adv": x.get("averageDailyVolumeQuantity"), "dtc": x.get("daysToCoverQuantity"), "chg": x.get("changePercent")}
        if len(rows) < 5000:
            break
        off += 5000
    return d, out


def ibkr_borrow() -> tuple[str | None, dict]:
    """Interactive Brokers' public stock-loan availability file (the one their own short-stock page points to):
    {sym: [fee_rate_pct, shares_available]}. Public read-only data, no account; plain FTP, so it is used as an
    indication only. None on any problem."""
    import ftplib   # nosec B402 - public market-data file, read only, nothing sent
    for host in ("ftp2.interactivebrokers.com", "ftp3.interactivebrokers.com"):
        try:
            buf = io.BytesIO()
            ftp = ftplib.FTP(host, timeout=30)   # nosec B321 - public read-only data file (IBKR publishes it only this way)
            ftp.login("shortstock", "")
            ftp.retrbinary("RETR usa.txt", buf.write)
            ftp.quit()
            out, stamp = {}, None
            for ln in buf.getvalue().decode("latin-1").splitlines():
                p = ln.split("|")
                if ln.startswith("#BOF") and len(p) >= 3:
                    stamp = f"{p[1].replace('.', '-')} {p[2][:5]} ET"
                if ln.startswith("#") or len(p) < 8 or p[1] != "USD":
                    continue
                try:
                    out[p[0].replace(" ", ".")] = [float(p[6]), float(p[7].lstrip(">"))]
                except ValueError:
                    continue
            if len(out) > 1000:
                return stamp, out
        except Exception:
            continue
    return None, {}


def sec_float_shares(ticker: str, hist: pd.DataFrame | None) -> tuple[float | None, str | None]:
    """Free-float estimate: the public float in dollars the company reports on its 10-K cover (dei:EntityPublicFloat,
    as of the end of its 2nd fiscal quarter) ÷ the closing price that day. Cached 30 days."""
    cache = _read_json(FLOAT_PATH, {}) or {}
    hit = cache.get(ticker)
    if hit and time.time() - hit[0] < 30 * 86400:
        return hit[1], hit[2]
    val = asof = None
    try:
        cik, _ = sec_cik(ticker)
        if cik and hist is not None and len(hist):
            j = _sec_get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/EntityPublicFloat.json", 30).json()
            pts = [p for u in (j.get("units") or {}).values() for p in u if p.get("end") and p.get("val")]
            if pts:
                p = max(pts, key=lambda p: p["end"])
                hh = hist[hist["date"].astype(str).str[:10] <= p["end"]]
                if len(hh) and (date.fromisoformat(p["end"]) - date.fromisoformat(str(hh["date"].iat[-1])[:10])).days <= 7:
                    px = float(hh["close"].iat[-1])
                    if px > 0:
                        val, asof = float(p["val"]) / px, p["end"]
    except Exception:
        val = asof = None
    cache[ticker] = [time.time(), val, asof]
    with _io_lock:
        _write_json(FLOAT_PATH, cache)
    return val, asof


def run_squeeze() -> dict:
    _sqz.update(running=True, error=None, progress="Nasdaq screener")
    try:
        r = _NqRows(nasdaq_stock_rows())
        scr = {}
        for x in ((r.json() or {}).get("data") or {}).get("rows") or []:
            sym = str(x.get("symbol", "")).strip().upper()
            px, cap = _num(x.get("lastsale")), _num(x.get("marketCap"))
            if re.fullmatch(r"[A-Z]{1,5}", sym) and px and cap and not _SQZ_SKIP.search(x.get("name") or ""):
                scr[sym] = {"px": px, "cap": cap, "vol": _num(x.get("volume")) or 0, "chg": _num(str(x.get("pctchange") or "").replace("%", "")),
                            "name": (x.get("name") or "")[:60], "sector": x.get("sector") or "", "industry": x.get("industry") or "",
                            "country": x.get("country") or ""}
        if len(scr) < 2000:
            raise RuntimeError("Nasdaq screener returned too few stocks")
        _sqz["progress"] = "FINRA short interest (all stocks)"
        si_date, si = finra_si_bulk()
        if not si:
            raise RuntimeError("FINRA short interest not available right now")
        _sqz["progress"] = "borrow fee & availability (Interactive Brokers public file)"
        b_stamp, borrow = ibkr_borrow()
        bh = _read_json(BORROW_PATH, {}) or {}
        _sqz["progress"] = "FINRA daily short volume"
        days = finra_days(20)
        earn = nasdaq_earnings() or {}
        gexd = _read_json(GEX_DAILY_PATH, {}) or {}
        today = et_now().date()
        cands = []
        for sym, x in si.items():
            s = scr.get(sym)
            if not s or s["px"] < 1 or not x.get("adv"):
                continue
            shares = s["cap"] / s["px"]
            si_pct = 100 * x["short"] / shares if shares else 0
            dtc = x.get("dtc") or (x["short"] / x["adv"] if x["adv"] else 0)
            if si_pct < 10 or dtc < 2 or x["adv"] < 200000 or s["cap"] < 5e7 or si_pct > 150:
                continue
            sv5 = [d_[1].get(sym) for d_ in days[-5:]]
            sv20 = [d_[1].get(sym) for d_ in days]
            rat = lambda L: (sum(a[0] for a in L if a) / max(sum(a[1] for a in L if a), 1)) if any(L) else None
            b = borrow.get(sym)
            cands.append({"ticker": sym, **s, "shares": shares, "short": x["short"], "si_pct": si_pct, "dtc": dtc, "adv": x["adv"],
                          "si_chg": x.get("chg"), "rvol": s["vol"] / x["adv"] if x["adv"] else None,
                          "svr5": rat(sv5), "svr20": rat(sv20), "fee": b[0] if b else None, "avail": b[1] if b else None,
                          "earn": earn.get(sym)})
        # borrow history: keep candidates' fee / availability per day (trend = today vs the oldest of the last 6 snapshots)
        if borrow:
            bh[today.isoformat()] = {c["ticker"]: [c["fee"], c["avail"]] for c in cands if c["fee"] is not None}
            for old in sorted(bh)[:-30]:
                bh.pop(old, None)
            with _io_lock:
                _write_json(BORROW_PATH, bh)
        bdays = sorted(d_ for d_ in bh if d_ < today.isoformat())[-5:]
        for c in cands:
            old = next((bh[d_][c["ticker"]] for d_ in bdays if c["ticker"] in bh.get(d_, {})), None)
            c["fee_then"], c["avail_then"] = (old[0], old[1]) if old else (None, None)
            c["b_days"] = len(bdays)
        pre = lambda c: c["si_pct"] + 4 * min(c["dtc"], 10) + (15 if (c["rvol"] or 0) >= 1.5 else 0) + min(c["fee"] or 0, 60) / 2 \
            + (25 if c["cap"] <= 3e9 and c["shares"] <= 6e7 else 0)
        cands.sort(key=pre, reverse=True)
        deep = cands[:45]
        for n_, c in enumerate(deep, 1):
            _sqz["progress"] = f"price history & float {n_}/{len(deep)}"
            try:
                h = scan_history(c["ticker"])
            except Exception:
                h = None
            if h is not None and len(h) >= 60:
                h = h.copy()
                for col in ("open", "high", "low", "close", "volume"):
                    h[col] = pd.to_numeric(h[col], errors="coerce")
                cl, hi, lo, v = h["close"], h["high"], h["low"], h["volume"]
                pc = cl.shift()
                atr = pd.concat([hi - lo, (hi - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1).ewm(alpha=1 / 14, adjust=False).mean()
                fm = (cl - pc) / atr.shift()
                va = v.rolling(50, min_periods=20).mean().shift()
                fac = (fm >= 1.5) & (v > va)
                c["rsi"], _ = rsi14(c["ticker"], 0, "", h)
                c["r5"] = float(cl.iat[-1] / cl.iat[-6] - 1) * 100
                c["fm_days"] = next((k for k in range(0, 4) if bool(fac.iat[-1 - k])), None)
                c["base_brk"] = bool(cl.iat[-1] > hi.shift().rolling(60, min_periods=40).max().iat[-1])
                c["above50"] = bool(cl.iat[-1] > cl.tail(50).mean())
                c["hist_px"] = float(cl.iat[-1])
            if 1.5e7 < c["shares"] <= 1.2e8:                    # float can only be smaller than shares outstanding
                fl_, c["float_asof"] = sec_float_shares(c["ticker"], h)
                c["float"] = min(fl_, c["shares"]) if fl_ else None
            elif c["shares"] <= 1.5e7:
                c["float"], c["float_asof"] = c["shares"], "≤ shares outstanding"
        for c in cands:
            fl = c.get("float")
            c["si_float"] = 100 * c["short"] / fl if fl else None
            si_use = c["si_float"] if c["si_float"] is not None and c["si_float"] <= 200 else c["si_pct"]
            fails = []
            if not 1e8 <= c["cap"] <= 3e9:
                fails.append("market cap outside $100M–$3B")
            if fl is None:
                fails.append("float over 15M (shares outstanding %.0fM)" % (c["shares"] / 1e6) if c["shares"] > 1.2e8 else "float not confirmed")
            elif fl > 1.5e7:
                fails.append("float %.1fM (over 15M)" % (fl / 1e6))
            if c["adv"] <= 5e5:
                fails.append("average volume under 500K")
            if si_use < 20:
                fails.append("short interest under 20%")
            if c["dtc"] <= 3:
                fails.append("days to cover 3 or less")
            run = (c.get("r5") or 0) > 60
            if run:
                fails.append("already up %.0f%% in 5 days" % c["r5"])
            c["fails"] = fails
            c["tier"] = "pass" if not fails else ("near" if len(fails) == 1 else "watch")
            # weighted rank (Joe's weights). Trend parts use the saved borrow snapshots; until 3+ days exist they fall
            # back to the level and are marked as such.
            pts, why = 0.0, []
            fee, av = c["fee"], c["avail"]
            c["trend_ok"] = c["b_days"] >= 3 and c["fee_then"] is not None
            if fee is not None:
                if c["trend_ok"]:
                    up = fee - c["fee_then"]
                    p_ = 25 * max(0.0, min(1.0, up / max(5.0, 0.5 * c["fee_then"]))) if up > 0 else 0
                else:
                    p_ = 25 * (1.0 if fee >= 50 else 0.75 if fee >= 20 else 0.5 if fee >= 10 else 0.3 if fee >= 5 else 0.1 if fee >= 1 else 0) * 0.7
                pts += p_
                if p_ >= 8:
                    why.append("borrow fee %.1f%%%s" % (fee, " ↑" if c["trend_ok"] else ""))
            if av is not None:
                if c["trend_ok"] and c["avail_then"]:
                    drop = 1 - av / c["avail_then"]
                    p_ = 20 * max(0.0, min(1.0, drop / 0.5))
                else:
                    ratio = av / c["short"] if c["short"] else 1
                    p_ = 20 * (1.0 if av == 0 or ratio <= 0.01 else 0.7 if ratio <= 0.05 else 0.4 if ratio <= 0.15 else 0) * 0.7
                pts += p_
                if p_ >= 8:
                    why.append("few shares to borrow")
            rv = c["rvol"] or 0
            p_ = 15 * (1.0 if rv >= 3 else 0.75 if rv >= 2 else 0.5 if rv >= 1.5 else 0.25 if rv >= 1.2 else 0)
            pts += p_
            if p_ >= 7:
                why.append("volume %.1f× normal" % rv)
            try:
                ed = (date.fromisoformat(c["earn"]) - today).days if c["earn"] else None
            except ValueError:
                ed = None
            if ed is not None and 0 <= ed <= 30:
                pts += 15
                why.append("earnings in %dd" % ed)
            if fl:
                pts += 10 if fl <= 8e6 else 6 if fl <= 1.5e7 else 3 if fl <= 3e7 else 0
            g = gexd.get(c["ticker"]) or {}
            if g:
                net = g[sorted(g)[-1]][0]
                c["gex"] = net
                if net and net > 0:
                    pts += 10
                    why.append("call-heavy gamma")
            if c.get("base_brk") or c.get("fm_days") is not None:
                pts += 5
                why.append("breakout" if c.get("base_brk") else "factor move")
            c["score"], c["why"] = pts, why
            c["state"] = "Running" if run else "Igniting" if (c.get("fm_days") is not None or (rv >= 2 and (c["chg"] or 0) >= 5)) \
                else "Building" if rv >= 1.2 and (c["chg"] or 0) > 0 else "Quiet"
        order = {"pass": 0, "near": 1, "watch": 2}
        cands.sort(key=lambda c: (order[c["tier"]], -c["score"], -c["si_pct"]))
        prev = _read_json(SQZ_PATH, {}) or {}
        prev_rank = prev.get("rank") or {}
        if prev.get("day") == today.isoformat():
            prev_rank = prev.get("prev_rank") or {}
        top = [c for c in cands if c["tier"] != "watch"][:40] + [c for c in cands if c["tier"] == "watch"][:80]
        rank = {c["ticker"]: n_ for n_, c in enumerate(top, 1)}
        for c in top:
            c["prev"] = prev_rank.get(c["ticker"])
        for n_, c in enumerate([c for c in top if "rsi" not in c][:90], 1):     # price + RSI(14) for every row shown
            _sqz["progress"] = f"RSI for the rest of the list {n_}"
            try:
                c["rsi"], _ = rsi14(c["ticker"], 0, "", scan_history(c["ticker"]))
            except Exception:
                c["rsi"] = None
        res = {"ts": et_now().strftime("%Y-%m-%d %H:%M ET"), "day": today.isoformat(), "si_date": si_date, "borrow_asof": b_stamp,
               "borrow_days": len(bdays), "short_vol_days": len(days), "screened": len(si), "candidates": len(cands),
               "rows": top, "rank": rank, "prev_rank": prev_rank,
               "removed": [t for t, n_ in sorted(prev_rank.items(), key=lambda kv: kv[1]) if t not in rank and n_ <= 40][:15]}
        res = _clean(json.loads(json.dumps(res, default=str)))
        with _io_lock:
            _write_json(SQZ_PATH, res)
        _sqz["progress"] = f"done {res['ts']}"
        return res
    except Exception as e:
        _sqz["error"] = str(e)
        raise
    finally:
        _sqz["running"] = False


def _sqz_bg() -> None:
    try:
        run_squeeze()
    except Exception as e:
        _sqz.update(running=False, error=str(e))


def squeeze_data() -> dict:
    d = _read_json(SQZ_PATH, None) or {"empty": True}
    d["status"] = _sqz
    return d


def squeeze_loop() -> None:
    """One run each weekday evening (18:40 ET, after FINRA posts the day's short volume), plus one the first time."""
    while True:
        try:
            now = et_now()
            d = _read_json(SQZ_PATH, {}) or {}
            due = now.weekday() < 5 and now.hour * 60 + now.minute >= 18 * 60 + 40 and (d.get("ts") or "") < f"{now.date()} 18:40"
            if not _sqz["running"] and (due or not d.get("ts")):
                _sqz.update(running=True, progress="starting", error=None)
                _sqz_bg()
        except Exception:
            pass
        time.sleep(300)


# ---------------------------------------------------------------- tax-loss bounce (year-end selling -> January rebound)
# Strategy notes: TAX_LOSS_BOUNCE.md. This year's losers get sold for the tax loss into the December monthly
# expiration and tend to rebound into mid-January (small caps most). The screen finds them; every option setup
# comes from the real chain (CBOE, or Schwab when connected) - nothing is estimated.
TLB_PATH = APP_DIR / "taxloss_results.json"
_tlb = {"running": False, "progress": "", "error": None, "tried": 0.0}
TLB = {"ytd": -30.0, "ytd_near": -20.0, "low": 10.0, "low_near": 20.0, "cap_lo": 3e8, "cap_hi": 1e10, "px": 3.0, "adv_usd": 5e6}
TLB_DEEP = 30            # top names that get a filings check and an option-chain look


def _third_friday(y: int, m: int) -> date:
    d = date(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7 + 14)


def tlb_calendar(today: date | None = None) -> dict:
    """Season dates. Season year y runs from Oct 1 (y) to the end of February (y+1); March-September is off season."""
    today = today or et_now().date()
    y = today.year - 1 if today.month <= 2 else today.year
    one = timedelta(days=1)
    opex = _third_friday(y, 12)
    buy_a, buy_b = opex - timedelta(days=4), date(y, 12, 23)          # Monday of December expiration week .. Dec 23
    sell_a = date(y + 1, 1, 3 if date(y + 1, 1, 1).weekday() == 6 else 2)
    while sell_a.weekday() >= 5:
        sell_a += one
    sell_b, puts_a = date(y + 1, 1, 20), date(y, 11, 20)
    steps = [
        ("watch", date(y, 10, 1), puts_a - one, "Build the list",
         "Selling pressure is building. Rank the losers; no entries yet unless a put already pays enough."),
        ("puts", puts_a, buy_a - one, "Sell puts",
         "Sell cash-secured puts 30-60 days out on losers you would be happy to own - below the put wall when there is one."),
        ("buy", buy_a, buy_b, "Buy window",
         f"Selling usually peaks around the December monthly expiration ({opex:%b} {opex.day}). Buy calls or call spreads "
         "expiring February / March, or the shares."),
        ("hold", buy_b + one, sell_a - one, "Hold", "Hold. The rebound often starts in the last week of December."),
        ("exit", sell_a, sell_b, "Exit window",
         "Take profits into strength. Do not hold through late-January earnings without a separate reason."),
        ("done", sell_b + one, date(y + 1, 3, 1) - one, "Season over", "Close what is left - the seasonal edge is spent."),
    ]
    cur = next((s for s in steps if s[1] <= today <= s[2]), None)
    return {"year": y, "today": today.isoformat(), "phase": cur[0] if cur else "off",
            "name": cur[3] if cur else "Off season",
            "do": cur[4] if cur else f"Nothing to do until the list starts building on October 1, {y}.",
            "opex": opex.isoformat(), "call_exps": [_third_friday(y + 1, 2).isoformat(), _third_friday(y + 1, 3).isoformat()],
            "steps": [{"key": k, "from": a.isoformat(), "to": b.isoformat(), "name": n, "do": d_} for k, a, b, n, d_ in steps]}


def _tlb_universe() -> dict:
    r = _NqRows(nasdaq_stock_rows())
    out = {}
    for x in ((r.json() or {}).get("data") or {}).get("rows") or []:
        sym = str(x.get("symbol", "")).strip().upper()
        px, cap = _num(x.get("lastsale")), _num(x.get("marketCap"))
        if re.fullmatch(r"[A-Z]{1,5}", sym) and px and cap and not _SQZ_SKIP.search(x.get("name") or ""):
            out[sym] = {"px": px, "cap": cap, "vol": _num(x.get("volume")) or 0, "name": (x.get("name") or "")[:60],
                        "sector": x.get("sector") or "", "industry": x.get("industry") or ""}
    if len(out) < 2000:
        raise RuntimeError("Nasdaq screener returned too few stocks")
    return out


def schwab_quotes(syms: list) -> dict:
    """Batch quotes from Schwab when connected: {sym: {"px", "lo52", "hi52"}}. {} when not connected."""
    out = {}
    if not _sw_live():
        return out
    for i in range(0, len(syms), 200):
        j = _sw_get("/marketdata/v1/quotes", {"symbols": ",".join(syms[i:i + 200]), "fields": "quote", "indicative": "false"})
        for s, v in (j or {}).items():
            q = (v.get("quote") or {}) if isinstance(v, dict) else {}
            px = q.get("lastPrice") or q.get("closePrice")
            if px and q.get("52WeekLow") and q.get("52WeekHigh"):
                out[s] = {"px": float(px), "lo52": float(q["52WeekLow"]), "hi52": float(q["52WeekHigh"])}
    return out


def _tlb_stats(h: pd.DataFrame | None, y: int, px: float | None = None) -> dict | None:
    """Return for year y, distance from the 52-week low / high, RSI(14), trading value and a simple state, from daily bars."""
    if h is None or len(h) < 120:
        return None
    h = h.copy()
    for col in ("high", "low", "close", "volume"):
        h[col] = pd.to_numeric(h[col], errors="coerce")
    h["date"] = h["date"].astype(str).str[:10]
    h = h.dropna(subset=["close", "low", "high"]).reset_index(drop=True)
    this = h[h["date"] >= f"{y}-01-01"]
    if len(h) < 120 or this.empty:
        return None
    cl = h["close"]
    last = float(px or cl.iat[-1])
    prev = h[h["date"] < f"{y}-01-01"]
    base = float(prev["close"].iat[-1]) if len(prev) else float(this["close"].iat[0])
    yr = h.tail(252)
    lo, hi = min(float(yr["low"].min()), last), max(float(yr["high"].max()), last)
    if base <= 0 or lo <= 0:
        return None
    ratio = (cl / cl.shift()).tail(252)
    since_low = int(len(h) - 1 - yr["low"].idxmin())
    r5 = float(last / cl.iat[-6] - 1) * 100
    rsi, _ = rsi14("", 0, "", h)
    state = "Still falling" if since_low <= 2 else "Turning" if last > float(cl.tail(10).mean()) and r5 > 0 else "Basing"
    return {"px": last, "ytd": (last / base - 1) * 100, "new_listing": not len(prev), "lo52": lo, "hi52": hi,
            "above_low": (last / lo - 1) * 100, "off_high": (last / hi - 1) * 100, "rsi": rsi, "r5": r5,
            "since_low": since_low, "state": state, "adv_usd": float((cl * h["volume"]).tail(20).mean()),
            "split": bool(((ratio > 2.5) | (ratio < 0.4)).any()), "asof": str(h["date"].iat[-1])}


def _tlb_score(r: dict) -> tuple[float, list]:
    """0-100: depth of this year's loss 30, closeness to the 52-week low 20, RSI 15, filings grade 15, small size 10,
    short interest 10; minus points for weak filings or dilution risk."""
    pts, why = 30 * min(-r["ytd"], 70) / 70, ["down %.0f%% this year" % -r["ytd"]]
    pts += 20 * max(0.0, 1 - r["above_low"] / 20)
    if r["above_low"] <= 5:
        why.append("%.0f%% above its low" % r["above_low"])
    rsi = r.get("rsi")
    if rsi is not None and rsi <= 50:
        pts += 15 if rsi <= 30 else 10 if rsi <= 40 else 5
        if rsi <= 30:
            why.append("RSI %.0f oversold" % rsi)
    pts += 10 if r["cap"] <= 2e9 else 6 if r["cap"] <= 5e9 else 3
    g = r.get("grade")
    pts += {"A": 15, "B": 15, "C": 8, "D": 0, "F": -10}.get(g, 5)
    if g in ("A", "B"):
        why.append(f"filings grade {g}")
    if r.get("dilution"):
        pts -= 10
    si = r.get("si_pct") or 0
    if si >= 10:
        pts += 10 if si >= 20 else 5
        why.append("short interest %.0f%%" % si)
    if r.get("state") == "Turning":
        why.append("turning up")
    return max(0.0, min(100.0, pts)), why


def _tlb_setups(tk: str, cal: dict, earn: str | None) -> dict:
    """Option setups from the real chain: a cash-secured put 30 days out to the January monthly expiration, and a call / call spread expiring in
    February-April of the rebound year (past the January window, so time decay does not eat a late bounce).
    Ranked Joe's way: highest chance of profit (5-point steps), then lowest capital, then highest return."""
    spot, df, _meta = chain_cached(tk, 600)
    out = {"spot": float(spot), "src": _last_src.get(tk, "cboe")}
    today = et_now().date()
    CSP_EXTRA_WINDOWS["tlb"] = (30, max(60, (_third_friday(cal["year"] + 1, 1) - today).days))   # out to the January monthly
    cands = _csp_candidates(spot, df, "tlb")                 # already POP 60%+ and the minimum return on cash
    puts = [c for c in cands if c["pop"] >= 70] or cands     # nothing at 70%+: show the closest, marked as short of it
    puts.sort(key=lambda c: (-int(c["pop"] // 5), c["cash"], -c["roc"]))
    if puts:
        c = puts[0]
        out["put"] = {**{k: c[k] for k in ("exp", "dte", "short", "credit", "nat", "pop", "be", "cash", "prem", "roc", "ann",
                                          "sdelta", "s_oi", "put_wall", "below_wall")},
                      "earn_cross": bool(earn and earn <= c["exp"]), "weak": bool(c["pop"] < 70)}
    lo_d = max(date.fromisoformat(cal["call_exps"][0]) - timedelta(days=7), today + timedelta(days=21))
    hi_d = date(cal["year"] + 1, 4, 30)
    exps = [e for e in sorted(df["expiration"].unique()) if lo_d <= pd.Timestamp(e).date() <= hi_d][:2]
    out["call_exps"] = [pd.Timestamp(e).strftime("%Y-%m-%d") for e in exps]
    if exps:
        cc = _call_candidates(spot, df, "swing", earn, nkeep=40, exps_force=exps)
        longs = [c for c in cc["long"] if 0.40 <= c["delta"] <= 0.70]
        longs.sort(key=lambda c: (-int(c["pop"] // 5), c["cost"], -c["lev"]))
        if longs:
            out["call"] = {k: longs[0][k] for k in ("exp", "dte", "long", "debit", "nat", "cost", "be", "be_pct", "pop",
                                                    "delta", "theta_day", "iv", "oi", "slip", "earn_cross")}
        spreads = [c for c in cc["spread"] if c["rr"] >= 1.0 and c["slip"] <= 40]
        spreads.sort(key=lambda c: (-int(c["pop"] // 5), c["cost"], -c["rr"]))
        if spreads:
            out["spread"] = {k: spreads[0][k] for k in ("exp", "dte", "long", "short", "width", "debit", "nat", "cost",
                                                        "maxprofit", "rr", "be", "be_pct", "pop", "p_max", "oi", "slip",
                                                        "earn_cross")}
    return out


@_bg_cboe
def run_taxloss() -> dict:
    _tlb.update(running=True, error=None, progress="Nasdaq screener", tried=time.time())
    try:
        cal, R = tlb_calendar(), TLB
        y, today = cal["year"], et_now().date()
        scr = _tlb_universe()
        uni = {s: x for s, x in scr.items() if R["cap_lo"] <= x["cap"] <= R["cap_hi"] and x["px"] >= R["px"]}
        _tlb["progress"] = f"52-week lows for {len(uni)} small and mid caps"
        q = schwab_quotes(sorted(uni))
        if len(q) >= 0.5 * len(uni):                      # full screen: every stock's 52-week range in a few requests
            src = "schwab"
            near = sorted((s for s in uni if s in q and q[s]["px"] <= 1.25 * q[s]["lo52"] and q[s]["px"] <= 0.80 * q[s]["hi52"]),
                          key=lambda s: q[s]["px"] / q[s]["lo52"])[:260]
        else:                                             # Schwab not connected: only the 300 most-traded get checked
            src, q = "partial", {}
            near = sorted(uni, key=lambda s: -uni[s]["px"] * uni[s]["vol"])[:300]
        rows = []
        for n_, s in enumerate(near, 1):
            _tlb["progress"] = f"price history {n_}/{len(near)}"
            try:
                st = _tlb_stats(scan_history(s), y, (q.get(s) or {}).get("px"))
            except Exception:
                st = None
            if not st or st["px"] < R["px"]:
                continue
            fails = []
            if st["ytd"] > R["ytd"]:
                fails.append("down only %.0f%% this year" % -st["ytd"])
            if st["above_low"] > R["low"]:
                fails.append("%.0f%% above its 52-week low" % st["above_low"])
            if st["adv_usd"] < R["adv_usd"]:
                fails.append("trades under $5M a day")
            if st["split"]:
                fails.append("possible split in the last year - check the chart")
            if st["ytd"] > R["ytd_near"] or st["above_low"] > R["low_near"] or len(fails) > 1:
                continue
            rows.append({"ticker": s, **uni[s], **st, "fails": fails, "tier": "pass" if not fails else "near"})
        _tlb["progress"] = "FINRA short interest"
        try:
            si_date, si = finra_si_bulk()
        except Exception:
            si_date, si = None, {}
        earn = nasdaq_earnings() or {}
        for r in rows:
            x, shares = si.get(r["ticker"]), r["cap"] / r["px"]
            r["si_pct"] = 100 * x["short"] / shares if x and shares else None
            r["dtc"] = x.get("dtc") if x else None
            r["earn"] = earn.get(r["ticker"])
            r["score"], r["why"] = _tlb_score(r)
        order = {"pass": 0, "near": 1}
        rows.sort(key=lambda r: (order[r["tier"]], -r["score"]))
        old = {r["ticker"]: r for r in ((_read_json(TLB_PATH, {}) or {}).get("rows") or [])}
        week_ago = (today - timedelta(days=7)).isoformat()
        deep = rows[:TLB_DEEP]
        for n_, r in enumerate(deep, 1):                  # the company's own SEC filings: is the business still sound?
            _tlb["progress"] = f"SEC filings check {n_}/{len(deep)}"
            o = old.get(r["ticker"]) or {}
            if (o.get("grade_day") or "") >= week_ago and "grade" in o:
                r.update({k: o.get(k) for k in ("grade", "grade_why", "dilution", "grade_day")})
            else:
                try:
                    hv = health_view(r["ticker"])
                except Exception:
                    hv = {}
                r.update(grade=hv.get("grade"), grade_why=(hv.get("grade_why") or [])[:4],
                         dilution=bool(hv.get("dilution_risk")), grade_day=today.isoformat())
            r["knife"] = bool(r["grade"] == "F" or r["dilution"])
            r["score"], r["why"] = _tlb_score(r)
        rows.sort(key=lambda r: (order[r["tier"]], bool(r.get("knife")), -r["score"]))
        todo = [r for r in rows if "grade" in r and not r.get("knife")]
        for n_, r in enumerate(todo, 1):
            _tlb["progress"] = f"option chains {n_}/{len(todo)}"
            try:
                r["opt"] = _tlb_setups(r["ticker"], cal, r.get("earn"))
            except Exception as e:
                r["opt"] = {"error": str(e)[:120]}
        etf = None
        try:                                              # the whole effect in one trade: small-cap ETF calls
            st = _tlb_stats(scan_history("IWM"), y)
            etf = {"ticker": "IWM", "name": "iShares Russell 2000 ETF (small caps)",
                   **{k: st[k] for k in ("px", "ytd", "rsi", "above_low", "off_high")}, "opt": _tlb_setups("IWM", cal, None)}
            etf["px"] = etf["opt"].get("spot") or etf["px"]
        except Exception:
            pass
        res = {"ts": et_now().strftime("%Y-%m-%d %H:%M ET"), "day": today.isoformat(), "src": src, "si_date": si_date,
               "universe": len(uni), "checked": len(near), "n_pass": sum(r["tier"] == "pass" for r in rows),
               "n_near": sum(r["tier"] == "near" for r in rows), "rules": R, "rows": rows[:100], "etf": etf}
        res = _clean(json.loads(json.dumps(res, default=str)))
        with _io_lock:
            _write_json(TLB_PATH, res)
        _tlb["progress"] = f"done {res['ts']}"
        return res
    except Exception as e:
        _tlb["error"] = str(e)
        raise
    finally:
        _tlb["running"] = False


def _tlb_bg() -> None:
    try:
        _queued(_tlb, "tax-loss screen", run_taxloss)
    except Exception as e:
        _tlb.update(running=False, error=str(e))


def taxloss_data() -> dict:
    d = _read_json(TLB_PATH, None) or {"empty": True}
    d["status"] = {k: _tlb[k] for k in ("running", "progress", "error")}
    d["cal"] = tlb_calendar()
    return d


def taxloss_loop() -> None:
    """In season (October-February): weekdays at 10:40 ET (fresh option quotes) and 19:05 ET (after the close).
    A failed run is retried no sooner than an hour later."""
    while True:
        try:
            now = et_now()
            hm = now.hour * 60 + now.minute
            slot = "19:05" if hm >= 19 * 60 + 5 else "10:40" if hm >= 10 * 60 + 40 else None
            d = _read_json(TLB_PATH, {}) or {}
            if slot and now.weekday() < 5 and tlb_calendar(now.date())["phase"] != "off" and not _tlb["running"] \
                    and (d.get("ts") or "") < f"{now.date()} {slot}" and time.time() - _tlb["tried"] > 3600:
                _tlb.update(running=True, progress="starting", error=None, tried=time.time())
                _tlb_bg()
        except Exception:
            pass
        time.sleep(300)


def _scan_bg() -> None:
    try:
        _queued(_scan, "spreads & puts scan", run_scan)
    except Exception as e:  # shown in the page
        _scan.update(running=False, error=str(e))


def top_spreads(res: dict, mode: str, min_pop: float = 70, max_risk: float | None = None,
                allow_earnings: bool = False, n: int = 10) -> list:
    """Best spread per ticker, ranked by POP x credit/width, nudged by momentum and put-wall support."""
    out = []
    for tk, it in res.get("items", {}).items():
        best = None
        for c in it.get(mode) or []:
            if c["pop"] < min_pop or (max_risk and c["maxloss"] > max_risk):
                continue
            if not allow_earnings and it.get("earnings") and it["earnings"] <= c["exp"]:
                continue
            rank = (c["score"] * (0.85 + 0.3 * it["mom"] / 100) * (1.05 if c["below_wall"] else 1.0)
                    * (1 - min(c["slip"], 60) / 300))  # wide bid/ask costs you on entry and exit
            if best is None or rank > best["rank"]:
                best = {**c, "rank": rank}
        if best:
            out.append({**{k: v for k, v in it.items() if k not in ("weekly", "monthly", "dte0", "dte5")}, **best})
    return sorted(out, key=lambda x: -x["rank"])[:n]


# ---------------------------------------------------------------- web mode
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEX Walls</title>
<style>
:root{--fib:#7c3aed;--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#6b7280;--line:#e3e6eb;--call:#1f9d55;--put:#d64545;--spot:#16181d;--accent:#2f6fde;--amber:#b7791f}
@media (prefers-color-scheme:dark){:root{--fib:#b197fc;--bg:#0f1115;--card:#171a21;--ink:#e8eaf0;--mute:#9aa3b2;--line:#272b35;--call:#3ecf8e;--put:#ff6b6b;--spot:#e8eaf0;--accent:#6ea0ff;--amber:#e0a43a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}
#f{display:flex;gap:8px;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:2}#out,#wl,#scn,#trk,#pwcard,#ptcard{scroll-margin-top:72px}#f .jump{flex:none;font-size:13px;padding:6px 10px}@media (max-width:520px){#f .jump{display:none}}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.row input{flex:1;min-width:110px;font-size:15px;padding:8px 10px}
.row button,.ghost{font-size:14px;padding:8px 12px}.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
summary{cursor:pointer;padding:2px 0}.al{padding:8px 10px;border-left:3px solid var(--put);margin:6px 0;background:var(--bg);border-radius:6px}
.al.warn{border-color:#d4a017}.al b{display:block}.x{background:none;color:var(--mute);padding:0 6px;font-size:16px}
.sets{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px 14px;margin:8px 0}.sets label{font-size:13px;color:var(--mute)}
.sets input[type=number],.sets input[type=text]{width:100%;font-size:14px;padding:6px 8px;text-transform:none}
#banner .al{margin:0 0 8px}h3{font-size:15px;margin:14px 0 4px}
input{flex:1;font-size:18px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);text-transform:uppercase}
button{font-size:16px;padding:10px 16px;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}
.recent{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 12px}.recent span{padding:3px 10px;border:1px solid var(--line);border-radius:99px;cursor:pointer;color:var(--mute);font-size:13px}
.head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin:6px 0 2px}.head h1{margin:0;font-size:26px}.big{font-size:22px;font-weight:600}
.mute{color:var(--mute);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:12px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:8px 0}
.kpi .l{font-size:12px;color:var(--mute)}.kpi .v{font-size:18px;font-weight:600}
.put{color:var(--put)}.call{color:var(--call)}.lkbox{border:1px solid var(--line,#ccd);border-left:4px solid var(--call);border-radius:8px;padding:10px 12px;margin:6px 0 12px}.lkt td,.lkt th{font-size:12.5px}p.put{overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:5px 6px;text-align:right;border-bottom:1px solid var(--line)}th{color:var(--mute);font-weight:500}td:first-child,th:first-child{text-align:left}
.tw{overflow-x:auto}th{cursor:pointer;user-select:none}th.sa::after{content:" ▲";color:var(--accent)}th.sd::after{content:" ▼";color:var(--accent)}table.nosort th{cursor:default}
.egtape{border:1px solid var(--line);border-left:4px solid var(--mute);border-radius:10px;padding:7px 10px;margin:4px 0 10px;font-size:13px;line-height:1.45;overflow-wrap:anywhere}.egtape.ok{border-left-color:var(--call)}.egtape.weak{border-left-color:var(--put)}
#tabEdge td .chip,#ceswing td .chip{white-space:nowrap}#sqlist td:nth-child(2),#eglist td:first-child{min-width:150px}
.g4r{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}svg{width:100%;height:auto;display:block}.err{color:var(--put);padding:12px 0}
h2{margin:0 0 2px;font-size:17px}a{color:var(--accent)}
.cc{position:relative;overflow:hidden}select{font-size:14px;padding:5px 6px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
.chip{display:inline-block;font-size:11px;padding:1px 7px;border-radius:99px;border:1px solid currentColor}.chip.acc{color:var(--accent)}.chip.amb,.amb{color:var(--amber)}.chip.mute{color:var(--mute)}
#scres td,#scrot td{vertical-align:top}#scres td:nth-child(2),#scres th:nth-child(2),#scres td:nth-child(4),#scres th:nth-child(4),#scrot td:nth-child(2),#scrot th:nth-child(2),#scrot td:nth-child(3),#scrot th:nth-child(3){text-align:left}
.row input[type=checkbox]{flex:none;min-width:0}.cc svg{max-width:100%;height:auto}.ax{font-size:11px;fill:var(--mute)}.lb{font-size:11px;font-weight:600}
.ctl{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px}.seg{background:transparent;color:var(--mute);border:1px solid var(--line);font-size:12px;padding:4px 9px;border-radius:99px}
.seg.on{background:var(--accent);color:#fff;border-color:var(--accent)}.tip{position:absolute;display:none;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12px;line-height:1.35;box-shadow:0 4px 14px rgba(0,0,0,.15);z-index:3}
.cleg{position:absolute;left:8px;top:6px;z-index:3;font-size:12px;line-height:1.5;pointer-events:none;padding:2px 6px;border-radius:6px;background:var(--card);opacity:.92;font-variant-numeric:tabular-nums}.vsep{display:inline-block;width:1px;height:18px;background:var(--line);margin:0 4px}.trade{border:1px solid var(--line);border-left:4px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0;background:var(--bg)}
.trade.b-call{border-left-color:var(--call)}.trade.b-amb{border-left-color:var(--amber)}.trade.b-put{border-left-color:var(--put)}.trade.b-acc{border-left-color:var(--accent)}
.th{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.th .x{margin-left:auto}.chip.big{font-size:12px;font-weight:700;padding:2px 9px}.chip.call{color:var(--call)}.chip.put{color:var(--put)}
.pbar{position:relative;height:20px;border-radius:99px;background:var(--card);border:1px solid var(--line);margin:8px 0;overflow:hidden}
.pfill{position:absolute;left:0;top:0;bottom:0;background:var(--call);opacity:.55}.pfill.done{background:var(--accent);opacity:.7}.pfill.neg{background:var(--put)}
.psch{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--ink)}.plab{position:relative;font-size:12px;padding-left:8px;line-height:20px;font-weight:600}
.tgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:4px 14px;font-size:13px}.tgrid .l{display:block;color:var(--mute);font-size:11px}
.acts{margin:8px 0 4px;padding-left:18px;font-size:13px}
.rfx{font-weight:500;opacity:.9}@media(max-width:700px){.rfx{display:none}}
.vd{border:1px solid var(--line);border-left:4px solid var(--line);border-radius:9px;padding:8px 10px;margin:8px 0;font-size:13px}.vd-cut{border-left-color:var(--put);background:color-mix(in srgb,var(--put) 7%,transparent)}.vd-salvage{border-left-color:var(--amber);background:color-mix(in srgb,var(--amber) 7%,transparent)}.vd-hold{border-left-color:var(--call)}.vd-take{border-left-color:var(--accent);background:color-mix(in srgb,var(--accent) 7%,transparent)}.vdc{font-weight:800;letter-spacing:.3px;margin-right:4px}.vplan{margin:7px 0;padding:6px 0 0;border-top:1px dashed var(--line)}.vd summary{cursor:pointer;margin-top:2px}.acts li{margin:2px 0}.tform input{min-width:120px}.tform label{display:flex;flex-direction:column;font-size:11px}
.tabs{display:flex;gap:6px;margin:4px 0 0}.tabs button{background:transparent;color:var(--mute);border:1px solid var(--line);font-size:14px;padding:7px 14px;border-radius:10px}
.tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent)}.pchips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
.pchip{display:inline-flex;align-items:center;gap:2px;padding:2px 4px 2px 10px;border:1px solid var(--line);border-radius:99px;font-size:13px}.pchip .x{font-size:15px;padding:0 4px}
.pfil{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.pfil select{width:100%;font-size:14px}.pfil label{display:flex;flex-direction:column;gap:3px}
.pfil label:has(input[type=checkbox]){flex-direction:row;align-items:center}tr.wrow td{background:color-mix(in srgb,var(--accent) 6%,transparent)}
.wkgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px}.wk h3{margin:4px 0}.wk .kpis{grid-template-columns:repeat(3,1fr)}@media (max-width:600px){.wkgrid{grid-template-columns:1fr}}
.glance .ghead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}.gtiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.gt{border:1px solid var(--line);border-left:4px solid var(--line);border-radius:10px;padding:7px 10px;background:var(--bg)}.gt .v{font-size:17px;font-weight:650}.gt .l{font-size:11.5px;color:var(--mute);overflow-wrap:anywhere}#pknote,.mkn,.al{overflow-wrap:anywhere}.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%) translateY(20px);background:var(--card,#222);color:var(--fg,#fff);border:1px solid var(--call,#2a8);border-radius:10px;padding:10px 14px;font-size:14px;opacity:0;pointer-events:none;transition:.2s;z-index:9999;max-width:92vw}.toast.on{opacity:1;transform:translateX(-50%)}.lps{font-size:10.5px;color:var(--mute);text-transform:uppercase;letter-spacing:.02em}.mkn{font-size:12px;color:var(--mute);margin:0 0 6px;line-height:1.45}tr.lpbad>td{background:color-mix(in srgb,var(--put) 9%,transparent)}tr.lpwarn>td{background:color-mix(in srgb,var(--amb,#c80) 7%,transparent)}.pkc.lpbad{border-color:var(--put)}.pkc.lpwarn{border-color:var(--amb,#c80)}.gt{min-width:0}
.gt.b-call{border-left-color:var(--call)}.gt.b-put{border-left-color:var(--put)}.gt.b-amb{border-left-color:var(--amber)}.gt.b-fib{border-left-color:var(--fib)}.gt.b-mute{border-left-color:var(--mute)}
.chip.fib{color:var(--fib)}.ladder{margin-top:8px}.ladder table{max-width:560px}.ladder tr.now td{border-top:2px solid var(--ink);border-bottom:2px solid var(--ink)}
.lmap{margin:6px 0 2px}.lmap svg{max-height:70px}details.ptab>summary{margin:6px 0}
#tabFlow details>summary{margin:10px 0 4px;cursor:pointer}.tape{overflow:hidden;white-space:nowrap;border:1px solid var(--line);border-radius:10px;background:var(--card);margin:6px 0 0;font-size:13px;padding:5px 0}
.tapein{display:inline-block;padding-left:100%;animation:tape 60s linear infinite}.tape:hover .tapein{animation-play-state:paused}.tp{margin-right:28px}
@keyframes tape{from{transform:translateX(0)}to{transform:translateX(-100%)}}.mbar{display:inline-block;width:46px;height:6px;border-radius:3px;background:var(--line);margin-right:6px;vertical-align:middle;overflow:hidden}.mbar i{display:block;height:100%;background:var(--accent)}
.lbtn{display:inline-flex;align-items:center;text-decoration:none;border-radius:10px;font-size:13px;padding:6px 10px;border:1px solid var(--line)}
.pick{border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:4px 0 10px;background:var(--bg)}.ptitle{font-size:18px;margin-bottom:4px}.flags{font-size:13px;margin-top:6px;line-height:1.5}.shead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px}.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--mute);margin-top:6px}.legend .ln{display:inline-block;width:22px;border-top:2px dashed var(--mute);vertical-align:middle;margin-right:5px}
.legend .dd1{border-top-style:dotted}.legend .dd3{border-top-width:2px;border-top-style:solid}.legend .bx{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:3px;vertical-align:middle}
#wlist td:nth-child(6),#wlist th:nth-child(6){text-align:left;min-width:200px}#wlist td:nth-child(4){min-width:120px}@media (max-width:600px){#wlist th:nth-child(5),#wlist td:nth-child(5),#wlist th:nth-child(7),#wlist td:nth-child(7),#wlist th:nth-child(9),#wlist td:nth-child(9){display:none}#wlist table{font-size:12px}}

/* ---- refreshed look ---- */
:root{--bg:#f3f5f9;--card:#ffffff;--ink:#0f172a;--mute:#64748b;--line:#e2e8f0;--accent:#2563eb;--call:#059669;--put:#dc2626;--amber:#b45309;--fib:#7c3aed;--spot:#0f172a;--shadow:0 1px 2px rgba(15,23,42,.06),0 4px 16px rgba(15,23,42,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#0a0d13;--card:#111723;--ink:#e6edf7;--mute:#8b98ad;--line:#1f2937;--accent:#5b8cff;--call:#34d399;--put:#f87171;--amber:#fbbf24;--fib:#c4b5fd;--spot:#e6edf7;--shadow:0 1px 0 rgba(255,255,255,.03) inset,0 8px 24px rgba(0,0,0,.35)}}
body{font:14.5px/1.45 "Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,Inter,sans-serif;font-feature-settings:"tnum" 1;-webkit-font-smoothing:antialiased}
main{max-width:1120px;padding:14px 18px 40px}
.brand{display:flex;align-items:center;gap:10px;padding:6px 2px 10px}.brand .logo{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),var(--fib));color:#fff;font-weight:800;font-size:14px}
.brand b{font-size:17px;letter-spacing:.2px}.brand .tag{color:var(--mute);font-size:12.5px}.pill{margin-left:auto;font-size:12px;padding:3px 10px;border-radius:99px;border:1px solid var(--line);color:var(--mute)}.pill.open{color:var(--call);border-color:color-mix(in srgb,var(--call) 45%,transparent)}
.card{border-radius:16px;padding:16px 18px;box-shadow:var(--shadow);border:1px solid var(--line)}
h2{font-size:16px;font-weight:650;letter-spacing:.1px}h2::before{content:"";display:inline-block;width:4px;height:14px;border-radius:2px;background:var(--accent);margin-right:8px;vertical-align:-1px}
.glance h2::before,.ghead h2::before{background:var(--fib)}
.tabs{gap:4px;padding:4px;background:var(--card);border:1px solid var(--line);border-radius:12px;width:max-content;max-width:100%;overflow-x:auto;box-shadow:var(--shadow)}
.tabs button{border:0;border-radius:9px;padding:7px 14px;font-weight:600;font-size:13.5px}.tabs button.on{box-shadow:0 2px 8px color-mix(in srgb,var(--accent) 40%,transparent)}
#f{padding:10px 0;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px)}
input,select{border-radius:10px}input:focus,select:focus{outline:2px solid color-mix(in srgb,var(--accent) 50%,transparent);outline-offset:1px}
button{font-weight:600}.ghost{font-weight:600}
.seg{font-weight:600;transition:background .12s}.seg:hover{border-color:var(--accent)}
table{font-size:13px}th{text-transform:uppercase;font-size:10.5px;letter-spacing:.6px;color:var(--mute);font-weight:650;background:var(--card);position:sticky;top:0}
tr:nth-child(even) td{background:color-mix(in srgb,var(--line) 28%,transparent)}tr:hover td{background:color-mix(in srgb,var(--accent) 8%,transparent)}
.kpi .v{font-size:19px}.kpi .l{font-size:11.5px}
.chip{font-weight:650}.seg.dim{opacity:.38}.ufb{border:1px solid var(--line);border-radius:12px;padding:8px 10px 2px;margin:4px 0 10px;background:var(--bg)}.ufb .ctl{margin-bottom:6px}.ufb .ctl>.mute{min-width:58px;font-size:12px}.ufb .seg .mute{font-weight:400;font-size:10.5px}.g4{white-space:normal}.g4c{white-space:nowrap}.ufb .ufbf{display:flex;flex-wrap:wrap;gap:6px 14px;margin:2px 0 8px}.ufb .ufbf label{flex-direction:row;align-items:center;gap:5px;font-size:12px;white-space:nowrap}.ufb .ufbf select{width:auto;font-size:12.5px;padding:3px 6px}.ufb .ufbf input[type=number]{font-size:12.5px;padding:3px 6px}@media(max-width:600px){.ufb .ufbf label{white-space:normal}.ufb .ufbf{gap:6px 10px}}.hdrx .bdg,#hdrx .bdg{margin-right:4px}.g4c{font-size:11.5px;border:1px solid currentColor;border-radius:6px;padding:0 4px;margin-right:2px;display:inline-block}.bdg{font-size:10.5px;margin:1px 4px 1px 0;white-space:nowrap}.gtrend{margin:6px 0;line-height:1.9}.xh{font-size:12.5px}.chip.big{font-size:12px;padding:3px 11px}
.gt{border-radius:12px}.gt .v{font-size:16.5px}
.tlist{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:8px;margin-top:8px}
.trow{display:flex;gap:10px;align-items:flex-start;padding:9px 11px;border:1px solid var(--line);border-radius:12px;background:var(--bg)}
.tsig{flex:none;width:24px;height:24px;border-radius:7px;display:grid;place-items:center;font-size:12px;font-weight:800;background:color-mix(in srgb,currentColor 14%,transparent)}
.tname{font-weight:650;font-size:13.5px}.tval{font-weight:500;color:var(--mute);font-size:12.5px;margin-left:4px}.ttext{font-size:13px;margin-top:2px;line-height:1.4}
.fibhelp{margin-top:10px}.fibhelp ul{margin:6px 0;padding-left:18px;font-size:13px}
details>summary{list-style:none}details>summary::-webkit-details-marker{display:none}details>summary::before{content:"▸";display:inline-block;width:14px;color:var(--mute);transition:transform .15s}details[open]>summary::before{transform:rotate(90deg)}
.legend{gap:4px 12px}
.tabs button{white-space:nowrap;flex:none}.tabs{flex-wrap:nowrap}
.cab{margin:10px 0;padding-left:18px;font-size:14px;line-height:1.5}.cab li{margin:3px 0}#cain input[type=number]{width:100%;font-size:15px;height:38px;flex:none;padding:6px 10px}#cain label{justify-content:flex-start}#tabCalc .gtiles{margin-top:6px}
input[type=range]{width:100%;accent-color:var(--fib)}#cadesc{margin:4px 0 8px}
.casave{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0 4px}.casave input{flex:1;min-width:200px;text-transform:none}
#cslist td{vertical-align:top}#cslist td:nth-child(2),#cslist th:nth-child(2),#cslist td:nth-child(3),#cslist th:nth-child(3),#cslist td:nth-child(9),#cslist th:nth-child(9),#cslist td:nth-child(10),#cslist th:nth-child(10){text-align:left}
.srt{cursor:pointer;text-decoration:underline dotted}.srt:hover{color:var(--accent)}
.pkc{border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:10px 0;background:var(--bg)}.pkh{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
.pkn{font-weight:800;color:var(--mute)}.pkt{font-size:19px}.pkw{margin:6px 0 8px;font-size:14.5px}.pkwin{margin-top:8px;font-size:14px}.pkf{margin-top:6px;font-size:13px;color:var(--amber)}
.pkc .gtiles{grid-template-columns:repeat(4,1fr)}@media (max-width:600px){.pkc .gtiles{grid-template-columns:repeat(2,1fr)}}.pkday{margin:6px 0}.pkday ul{margin:4px 0 8px;padding-left:18px;font-size:13.5px}
#abbrbtn{background:transparent;color:var(--accent);border:1px solid var(--accent);font-weight:650;font-size:12px;padding:3px 10px}@media (max-width:600px){.brand .tag{display:none}}@media (max-width:700px){.brand{flex-wrap:wrap;row-gap:6px}}
.abbrdlg{position:fixed;inset:0;z-index:50;background:rgba(0,0,0,.55);display:flex;align-items:flex-start;justify-content:center;padding:4vh 12px;overflow:auto}
.abbrdlg[hidden]{display:none}.abbrbox{max-width:900px;width:100%;margin:0}.abbrgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:2px 18px;margin-top:8px}
.abbrsec{grid-column:1/-1;margin:10px 0 2px;font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--mute);font-weight:700}
.abbrrow{display:flex;gap:10px;padding:5px 0;border-bottom:1px solid var(--line);font-size:13.5px;line-height:1.4}.abbrrow b{flex:none;min-width:96px;color:var(--accent)}
@media (max-width:600px){.abbrgrid{grid-template-columns:1fr}.abbrrow b{min-width:80px}}
.wlbar{display:flex;width:110px;height:10px;border-radius:5px;overflow:hidden;background:var(--line)}.wlbar .w{background:var(--call)}.wlbar .l{background:var(--put)}
.hbtn{cursor:pointer;background:transparent;font-weight:650;font-size:12px;padding:3px 10px;margin-left:6px}.hbtn.hgood{color:var(--call);border-color:var(--call)}.hbtn.hok{color:var(--amber);border-color:var(--amber)}.hbtn.hbad{color:var(--put);border-color:var(--put)}
#totop{position:fixed;right:16px;bottom:18px;z-index:40;width:42px;height:42px;border-radius:50%;padding:0;font-size:18px;opacity:0;pointer-events:none;transition:opacity .2s;box-shadow:var(--shadow)}#totop.show{opacity:.9;pointer-events:auto}
details.howto{margin:6px 0}details.howto>summary{font-size:12.5px;cursor:pointer}
body.dense{font-size:13.5px}body.dense main{padding:8px 12px 30px}body.dense .card{padding:10px 12px;margin:8px 0;border-radius:12px}body.dense h2{font-size:15px}
body.dense table{font-size:12.5px}body.dense td,body.dense th{padding:3px 6px}body.dense .gt{padding:5px 8px}body.dense .gt .v{font-size:15px}body.dense .kpi .v{font-size:16px}
body.dense .seg{padding:3px 8px;font-size:11.5px}body.dense .pkc{padding:8px 10px;margin:6px 0}body.dense .trow{padding:6px 8px}
@media (max-width:600px){#densbtn{display:none}}
</style></head><body><main>
<div id="nodata" hidden style="background:var(--put,#c0392b);color:#fff;font-weight:700;padding:8px 14px;text-align:center;cursor:pointer" onclick="document.getElementById('swchip').click()"></div><header class="brand"><span class="logo">GW</span><b>GEX Walls</b><span class="tag">gamma walls · spreads · puts · dark pool &amp; insiders</span><button type="button" id="swchip" class="pill hbtn" style="margin-left:auto;white-space:nowrap;flex:none" title="Schwab real-time data — click to open the connection panel">Schwab …</button><span id="nqchip" class="pill amb" hidden style="white-space:nowrap;flex:none;font-size:12px" title="">⚠<span class="rfx"> Nasdaq backup on</span></span><button type="button" id="rfall" class="pill hbtn" style="margin-left:8px;font-weight:700;white-space:nowrap;flex:none;background:var(--accent);color:#fff;border-color:var(--accent)" title="Lists refresh on their own while the market is open. Click to refresh everything right now.">⟳<span class="rfx"> Refresh all</span></button><button type="button" id="hlthbtn" class="pill hbtn" style="margin-left:8px" title="Daily accuracy &amp; speed audit">Health –</button><button type="button" id="densbtn" class="pill hbtn" title="Compact / comfortable layout">Compact</button><button type="button" id="abbrbtn" class="pill" style="cursor:pointer" title="What the abbreviations mean">ABC? Legend</button><span id="mkt" class="pill" style="margin-left:6px">market</span></header>
<div id="tkdlg" class="abbrdlg" hidden><div class="abbrbox card" style="max-width:520px"><div class="shead"><h2>I took this trade</h2><button type="button" class="ghost" onclick="$('#tkdlg').hidden=true" style="margin-left:auto">Close ✕</button></div>
<p id="tkdesc"></p><div class="sets pfil"><label><span id="tkpl">Fill price (per share)</span><input id="tkp" inputmode="decimal"></label><label><span id="tkql">Contracts</span><input id="tkq" inputmode="numeric" value="1"></label><label>Opened<input id="tkd" type="date"></label></div>
<p class="mute" style="font-size:12.5px">Enter the price you actually got filled at. It shows in My trades with live P/L; when you close it (or it expires) the result counts in “How the picks have done → Your real trades”.</p>
<button type="button" id="tksave">Save to My trades</button> <span id="tkmsg" class="put"></span></div></div>
<div id="tcdlg" class="abbrdlg" hidden><div class="abbrbox card" style="max-width:520px"><div class="shead"><h2>Close trade</h2><button type="button" class="ghost" onclick="$('#tcdlg').hidden=true" style="margin-left:auto">Close ✕</button></div>
<p id="tcdesc"></p><div class="sets pfil"><label><span id="tcpl">Price (per share)</span><input id="tcp" inputmode="decimal"></label><label>Closed on<input id="tcd" type="date"></label></div>
<p class="mute" style="font-size:12.5px" id="tchint"></p><button type="button" id="tcsave">Save result</button> <span id="tcmsg" class="put"></span></div></div>
<div id="trldlg" class="abbrdlg" hidden><div class="abbrbox card" style="max-width:560px"><div class="shead"><h2>Roll trade</h2><button type="button" class="ghost" onclick="$('#trldlg').hidden=true" style="margin-left:auto">Close ✕</button></div>
<p id="trldesc"></p><div class="sets pfil"><label><span id="trlcl">Paid to buy back the old one (per share)</span><input id="trlc" inputmode="decimal"></label><label>Rolled on<input id="trld" type="date"></label></div>
<p style="margin:8px 0 4px"><b>New position</b></p><div class="sets pfil"><label>Already in My trades?<select id="trll"><option value="">No — enter it here</option></select></label></div>
<div id="trlnew" class="sets pfil"><label>New expiration<input id="trle" type="date"></label><label>New strikes<input id="trlk" placeholder="28/27" autocomplete="off"></label><label><span id="trlpl">New credit (per share)</span><input id="trlp" inputmode="decimal"></label><label>Contracts<input id="trlq" inputmode="numeric"></label></div>
<p class="mute" style="font-size:12.5px" id="trlhint"></p><button type="button" id="trlsave">Save roll</button> <span id="trlmsg" class="put"></span></div></div>
<div id="hlthdlg" class="abbrdlg" hidden><div class="abbrbox card"><div class="shead"><h2>App health</h2><span id="hlthnote" class="mute"></span><button type="button" class="ghost" id="hlthx" style="margin-left:auto">Close ✕</button></div><div id="hlthbody"></div></div></div>
<button type="button" id="totop" title="Back to top">↑</button>
<div id="abbrdlg" class="abbrdlg" hidden><div class="abbrbox card"><div class="shead"><h2>Abbreviations &amp; terms</h2><input id="abbrq" placeholder="Search (e.g. CC, POP, GEX)" autocomplete="off" style="max-width:240px;text-transform:none"><button type="button" class="ghost" id="abbrx" style="margin-left:auto">Close ✕</button></div><div id="abbrall"></div></div></div>
<nav class="tabs"><button type="button" data-tab="picks">Picks</button><button type="button" data-tab="edge">Early moves</button><button type="button" data-tab="main" class="on">Walls &amp; spreads</button><button type="button" data-tab="puts">Sell puts</button><button type="button" data-tab="calls">Calls &amp; LEAPS</button><button type="button" data-tab="calc">Calculator</button><button type="button" data-tab="flow">Dark pool &amp; insiders</button></nav>
<div id="tape" class="tape" hidden><div id="tapein" class="tapein"></div></div>
<form id="f"><input id="t" placeholder="Ticker (e.g. UMAC)" autocomplete="off" autocapitalize="characters" spellcheck="false"><button>Go</button><button type="button" class="ghost jump" data-j="trk" title="My trades">Trades</button><button type="button" class="ghost jump" data-j="wl" title="Watchlist &amp; alerts">Watchlist</button><button type="button" class="ghost jump" data-j="scn" title="Put credit spread scanner">Scanner</button></form>
<div id="tabEdge" hidden>
<div class="ctl pkjump"><span class="mute">Jump to:</span><button type="button" class="seg" onclick="pkJump('cvlist')">🍀 Aligned</button><button type="button" class="seg" onclick="pkJump('eglist')">Early moves</button><button type="button" class="seg" onclick="pkJump('thlist')">Theme leaders</button><button type="button" class="seg" onclick="pkJump('sqlist')">Short squeeze</button><button type="button" class="seg" onclick="pkJump('tllist')">Tax-loss bounce</button><button type="button" class="seg" onclick="pkJump('lvlist')">Leveraged ETFs</button><button type="button" class="seg" onclick="pkJump('aglist')">Algos</button><button type="button" class="seg" onclick="tab('picks');setTimeout(()=>pkJump('swcard'),300)">Swing trades</button></div>
<div class="card"><div class="shead"><h2>🍀 Everything aligned</h2><span class="mute">· stocks and ETFs where 8 or more of 12 bullish conditions are true at once — rare</span></div><div id="cvlist" class="tw"><p class="mute">Loading…</p></div></div>
<div class="card"><div class="shead"><h2>Early moves</h2><span class="mute">· institutions arriving: factor moves, relative-strength highs, theme leaders</span><span id="egnote" class="mute"></span>
<button type="button" id="egref" class="ghost" style="margin-left:auto;font-size:12.5px;padding:4px 11px">Rebuild</button></div>
<div id="eglist" class="tw"><p class="mute">Loading…</p></div>
<details class="fibhelp"><summary class="mute">How to read this</summary><p class="mute"><b>Factor move</b> = a day’s gain of 1.5+ ATR (its normal daily range) on above-average volume, closing in the top half — range expansion is usually the first footprint of large buyers. <b>RS line</b> = the stock divided by a benchmark; when that line makes a new high the stock is out-performing more than at any time in 6 months. <b>RS vs theme</b> divides by an equal-weight basket of the stock’s own theme, so a ★ there marks a true theme leader, and “before price” means the strength showed up before the breakout. <b>Stages</b>: Ignition (factor move in the last 3 sessions, still holding) → Follow-through → Extended (6+ ATR over the 50-day: late). Tested on 2022–2024 bars and checked on 2025–2026: factor moves in a leading theme, power factor moves and theme-RS highs beat arbitrary entries by roughly 0.1–0.2R per trade; coils, pullbacks to the 20-day and RSI(2) dips did not, so they are shown as context only. Click any column header to sort.</p></details></div>
<div class="card"><div class="shead"><h2>Theme leaders</h2><span class="mute">· which themes lead SPY, and which stocks lead their theme</span></div><div id="thlist" class="tw"><p class="mute">Loading…</p></div></div>
<div class="card"><div class="shead"><h2>Short squeeze monitor</h2><span class="mute">· crowded shorts, tight borrow, volume waking up</span><span id="sqnote" class="mute"></span>
<button type="button" id="sqref" class="ghost" style="margin-left:auto;font-size:12.5px;padding:4px 11px">Run now</button></div>
<div id="sqlist" class="tw"><p class="mute">Loading…</p></div></div>
<div class="card"><div class="shead"><h2>Tax-loss bounce</h2><span class="mute">&middot; this year&#39;s losers near their lows: sold for the tax loss into December, tend to rebound into mid-January</span><span id="tlnote" class="mute"></span>
<button type="button" id="tlref" class="ghost" style="margin-left:auto;font-size:12.5px;padding:4px 11px">Run now</button></div>
<div id="tllist" class="tw"><p class="mute">Loading&hellip;</p></div>
<details class="fibhelp"><summary class="mute">How to read this</summary><p class="mute"><b>The pattern</b>: investors sell their losing stocks before year-end to cut their tax bill, which pushes the year&#39;s losers lower into the December monthly expiration; when the selling stops they tend to rebound into mid-January, small caps most. <b>The plan</b>: sell cash-secured puts on the losers you would own while the selling builds, buy calls or call spreads in the buy window, take profits in the exit window. Calls expire February or later so a late bounce is not lost to time decay. <b>What it is not</b>: this is a well-documented seasonal tendency, not a rule tested in this app like the Early moves signals - it fails in years when the whole market falls in January, and a stock that is down for a real reason can keep falling, which is why the company&#39;s own filings are graded and names with an F or dilution risk get no setup. Full write-up: TAX_LOSS_BOUNCE.md in the app folder.</p></details></div>
<div class="card"><div class="shead"><h2>Leveraged ETFs</h2><span class="mute">· short-term trades in 2× / 3× funds, only rules that held up in testing</span></div><div id="lvlist" class="tw"><p class="mute">Loading…</p></div>
<details class="fibhelp"><summary class="mute">What the testing showed</summary><p class="mute">30 pairs, rules chosen on 2022–2024 and checked on 2025 and 2026. Signals come from the underlying; the trade is the bull fund from the next open. <b>Kept</b> (positive in all three periods): buying sharp dips (2-day RSI under 5) above or below the 200-day average, and riding a factor move in an uptrend. <b>Not kept</b>: simply holding (median pair −21% / +23% / −11%, with 50–66% drops along the way), 50- and 200-day trend filters, 20-day breakouts, momentum holds — and every inverse-fund rule tried. Holding an inverse fund lost a median 40–55% a year, and shorting rips, breakdowns and down factor moves with them all lost, so no bearish signal is shown. <b>Drag</b> compares the fund’s 3-month return with leverage × the underlying’s: daily resetting costs money in choppy markets and adds in steady trends. These funds can gap far past any exit; the “worst trade” column is the honest size guide.</p></details></div>
<div class="card"><div class="shead"><h2>Algos</h2><span class="mute">· rule-based strategies: back-test, live signals, forward record</span></div><div id="aglist" class="tw"><p class="mute">Loading…</p></div></div>
</div>
<div id="tabPicks" hidden>
<div class="ctl pkjump"><span class="mute">Jump to:</span><button type="button" class="seg" onclick="pkJump('pklist')">Today’s picks</button><button type="button" class="seg" onclick="pkJump('cecard')">Best use of capital</button><button type="button" class="seg" onclick="pkJump('swcard')">Swing trades</button><button type="button" class="seg" onclick="pkJump('pkev')">Week ahead</button><button type="button" class="seg" onclick="pkJump('mnlist')">Mining earnings</button><button type="button" class="seg" onclick="pkJump('pklearn')">How the picks have done</button></div>
<div class="card"><div class="shead"><h2>Today’s picks</h2><span id="pknote" class="mute"></span>
<button type="button" id="pkref" class="ghost" style="font-size:12.5px;padding:4px 11px;margin-left:auto">Refresh now (live prices)</button></div>
<div id="pkevb"></div>
<div id="pklock"></div>
<div id="pkufb"></div>
<div id="pklist"></div></div>
<div class="card" id="cecard"><div class="shead"><h2>Best use of capital</h2><span class="mute">· most return per dollar tied up, highest chance of profit — every strategy</span><span id="cenote" class="mute"></span>
<button type="button" id="ceref" class="ghost" style="margin-left:auto;font-size:12.5px;padding:4px 11px">Refresh</button></div>
<div id="ceufb"></div>
<div id="celist" class="tw"><p class="mute">Loading…</p></div>
<details class="fibhelp"><summary class="mute">How this is ranked</summary><p class="mute">Every trade the app’s scans and the Strategy Scanner found is put on the same footing. <b>Capital</b> = the most you can lose (spreads, calls), the cash set aside (short puts) or the share cost minus premium (covered calls). <b>Chance</b> = chance of the winning outcome: finishing past breakeven for premium-selling trades (Strategy Scanner chances lowered by its own track record where it has run hot), and reaching +50% before expiration for calls, bull call spreads and LEAPS (the take-profit most traders use). <b>Expected return</b> = chance × gain − (1 − chance) × loss, where the loss is the full capital for defined-risk trades and long options (a strict worst case) and about one expected move for short puts and covered calls (losses there are a stock decline, not a total loss). <b>Per day</b> divides by the days your money is tied up — to expiration for premium selling, to the typical day +50% is reached for calls. Option prices are fair bets by construction, so expected returns cluster near zero; the ranking shows which trades cost you least for the chance you get and free your capital fastest. Swing trades are ranked separately: a no-drift price model can’t give stocks an edge, so they are ranked by momentum × reward:risk instead. Quotes: CBOE, ~15 min delayed. Not financial advice.</p></details></div>
<div class="card" id="swcard"><div class="shead"><h2>Swing trades — buy shares</h2><span class="mute">· early-move setups with a back-tested record, a stop and a target</span></div><div id="ceswing" class="tw"><p class="mute">Loading…</p></div></div>
<div class="card"><h2>Week ahead</h2><div id="pkev"></div></div>
<div class="card"><div class="shead"><h2>Mining stock earnings</h2><span id="mnnote" class="mute"></span><span style="margin-left:auto"></span>
<select id="mndays" style="width:auto;font-size:13px"><option value="7">Next 7 days</option><option value="14">Next 14 days</option><option value="21" selected>Next 3 weeks</option><option value="35">Next 5 weeks</option></select>
<button type="button" id="mnref" class="ghost" style="font-size:12.5px;padding:4px 11px">Refresh</button></div>
<div class="ctl" id="mnind"></div><div id="mnlist" class="tw"><p class="mute">Loading…</p></div>
<p class="mute">Gold, silver, copper, uranium, lithium, coal, rare-earth and other miners listed in the US (Nasdaq.com stock screener industries: Precious Metals, Metal Mining, Other Metals and Minerals, Coal Mining, Non-metallic Mining; market cap $200M+). Dates and estimates from the Nasdaq.com earnings calendar — confirm the date on the company’s investor-relations page before trading around it. Earnings can move miners 5–15% and options usually lose value right after the report (IV crush).</p></div>
<div class="card"><h2>How the picks have done</h2><div id="pklearn"></div>
<p class="mute">Built into GEX Walls (strategy folder): a full scan of ~860 liquid optionable stocks/ETFs each weekday at 6:40 AM ET (1:40 AM HST; it catches up if the PC was off), and live re-pricing here every 30 minutes from 9:50 AM to 3:50 PM ET (or press Refresh now). Chance of profit is the option-market model's; a learned correction is used only when it predicted better on a walk-forward test of the graded picks (today: none did). Ranked by highest chance of profit, then lowest capital, then highest return; every pick pays at least 20% of its capital (the credit or premium itself 20%+ too). Quotes: CBOE, ~15 min delayed — check the live chain before trading. Not financial advice.</p></div>
</div>
<div id="tabMain">
<div class="recent" id="recent"></div>
<div id="banner"></div>
<details class="card" id="trk" open><summary><b>My trades</b> <span id="trnote" class="mute"></span> <button type="button" class="ghost" id="trref" style="font-size:12px;padding:3px 9px;margin-left:6px">Refresh</button></summary>
<div id="trlist"><p class="mute">Loading…</p></div>
<details id="tradd"><summary class="mute">+ Add a trade (any strategy)</summary>
<form id="tf" class="row tform"><label class="mute">Strategy<select name="st"></select></label><input name="tk" placeholder="Ticker" autocapitalize="characters" required>
<label class="mute" id="tfexp">Expiration<input name="exp" type="date"></label><input name="sh" placeholder="Short (sold) put" inputmode="decimal">
<input name="lg" placeholder="Long (bought) put" inputmode="decimal"><input name="k3" placeholder="" inputmode="decimal" hidden><input name="k4" placeholder="" inputmode="decimal" hidden>
<input name="lt" placeholder="Legs, e.g. -100P / +95P / -110C / +115C" hidden style="min-width:260px"><input name="qty" placeholder="Contracts" inputmode="numeric" value="1">
<input name="cr" placeholder="Credit per spread (e.g. 2.83)" inputmode="decimal" required><label class="mute" id="tfkind" hidden>Fill was a<select name="kd"><option value="credit">credit</option><option value="debit">debit</option></select></label><label class="mute">Opened<input name="ed" type="date"></label>
<input name="es" placeholder="Stock price at entry (optional)" inputmode="decimal"><button>Add</button></form><div id="tfhint" class="mute" style="font-size:12px"></div><div id="trmsg" class="mute"></div></details>
<p class="mute">Each trade gets one call from the latest prices: <span class="acc">TAKE PROFIT NOW!</span> half the max profit is in hand, or a winner whose odds or trend have turned (or that expires within 5 days with a 40%+ chance of ending worse) · <span class="call">HOLD ON</span> the odds still favor waiting · <span class="put">CUT LOSSES</span> a loser with under 30% odds of recovering (under 45% with the trend against it), or a short strike in the money with 5 days or less left. Hover the label for the reason. Trades are checked with your watchlist alerts (pop-up / phone push on target, watch and defend).</p>
</details>
<div id="out"><p class="mute">Enter a ticker. Data is pulled from CBOE delayed quotes each time (cached 5 min).</p></div>
<details class="card" id="wl"><summary><b>Watchlist &amp; downside alerts</b> <span id="wlnote" class="mute"></span></summary>
<form id="wf" class="row"><input id="wt" placeholder="Ticker (any)" autocomplete="off" autocapitalize="characters"><input id="wk" placeholder="Your short strike (optional)" inputmode="decimal" style="text-transform:none"><button>Add</button></form>
<div class="row" id="wlctl" style="align-items:center"></div>
<div id="wlist" class="tw"></div>
<div class="row"><button type="button" id="chk">Check now</button><button type="button" id="tst" class="ghost">Send test alert</button><button type="button" id="bn" class="ghost">Browser notifications</button></div>
<details><summary class="mute">Alert settings</summary><div class="sets" id="sets"></div>
<p class="mute">Alerts fire when: spot breaks or nears the put wall · net GEX flips negative · net GEX drops by the % below vs prior close · the put wall steps down · spot nears or breaks your short strike. Checked every interval during market hours while this app (or the background monitor) is running.</p></details>
<h3>Recent alerts</h3><div id="alog"><p class="mute">None yet.</p></div>
</details>
<details class="card" id="swb"><summary><b>Schwab real-time data</b> <span id="swnote" class="mute"></span></summary>
<div id="swbody"><p class="mute">Loading…</p></div>
</details>
<details class="card" id="scn"><summary><b>Options scanner</b> <span id="scnote" class="mute">· momentum + sector rotation</span></summary>
<div class="row" style="align-items:center"><button type="button" id="scrun">Run scan</button></div>
<div id="scufb"></div>
<div id="scres" class="tw"></div>
<details open><summary><b>Sector rotation</b> <span class="mute">· sectors, themes and stocks, with GEX changes</span></summary><div id="scrot" class="tw"></div></details>
<p class="mute">How it works: ~90 ETFs and liquid stocks (plus your watchlist) are ranked by momentum: 1- and 3-month return vs SPY, trend (price above its 20- and 50-day averages), and rotation quadrant (Leading / Improving / Weakening / Lagging vs SPY). Stocks get a bonus when their sector ETF is Leading or Improving. Every uptrending ETF and the top momentum stocks get a full CBOE option-chain scan. Each spread is scored POP × credit/width (premium balanced against probability), nudged up for momentum and for a short strike at or below the put wall, and down for wide bid/ask. One spread per ticker; top 10 shown. Earnings dates come from Nasdaq.com's calendar; spreads with earnings before expiration are excluded unless you allow them. POP is a model estimate from CBOE implied volatility. Quotes are 15-min delayed and widen after the close, so run it during market hours before trading. <b>Fastest to 50% profit</b>: for every spread, 1,500 simulated daily price paths (at the expiration's implied volatility, legs re-priced at their own IV, IV held constant) give the chance the spread is worth half the credit at some daily close before expiration (“Hits 50%”) and the median days to get there; ranked by that chance × speed, with wide quotes ranked down. Use “Both” to compare weekly and 30–45 DTE spreads together. Estimates only — an IV drop gets you there sooner, an IV spike later. <b>When 50%</b>: the most likely date (median of simulated paths that get there), the range covering the middle half of them, and “flat” = the date time decay alone gets you to 50% if the price doesn't move. Weekend dates roll to Monday.</p>
</details>
</div>
<div id="tabPuts" hidden>
<div class="card" id="pwcard"><h2>Put watchlist</h2>
<form id="pwf" class="row"><input id="pwt" placeholder="Add tickers (e.g. SOFI, F, NU)" autocomplete="off" autocapitalize="characters"><button>Add</button></form>
<div id="pwchips" class="pchips"></div><div id="pwmsg" class="mute"></div>
<div class="row" style="align-items:center"><button type="button" id="ptscanw">Scan watchlist</button><button type="button" id="ptscana" class="ghost">Scan watchlist + screener</button>
<label class="mute"><input type="checkbox" id="pscr" checked> screener: most-traded stocks under $<input id="pmax" type="number" min="2" step="1" value="30" style="width:70px;min-width:0;flex:none;font-size:14px;padding:4px 6px"></label>
<label class="mute"><input type="checkbox" id="petf" checked> + liquid ETFs under that price</label><label class="mute"><input type="checkbox" id="pday" checked> scan automatically every trading day (~10:05 ET)</label></div>
<div id="pwres" class="tw"></div></div>
<div class="card" id="ptcard"><h2>Put screener <span id="ptnote" class="mute" style="font-size:13px;font-weight:400"></span></h2>
<div id="ptufb"></div>
<div id="ptres" class="tw"></div>
<p class="mute">Cash → premium is what you tie up (strike × 100) and what you collect at mid; the bid is the worst-case fill. Return = premium ÷ cash secured. The minimum premium and return filters drop trades that tie up a lot of cash for a few dollars. POP = chance the stock finishes above your breakeven (your cost if assigned). Scans cover your put watchlist (any price) plus, with the screener on, the ~45 most-traded stocks and the liquid optionable ETFs under your price limit (Nasdaq.com stock and ETF screeners). <b>Premium ÷ cash × POP</b> ranks by what you collect per dollar tied up, weighted by the chance of keeping it. With daily scanning on, the scan runs by itself each trading day around 10:05 ET (while this app is running) and sends one alert with the top 3. Puts with POP 60%+, a 0.05+ bid and real open interest are kept; the filters above do the rest. Quotes: CBOE, ~15 min delayed. Not financial advice.</p></div>
</div>
<div id="tabCalls" hidden>
<div class="card"><h2>Call scanner <span id="clnote" class="mute" style="font-size:13px;font-weight:400"></span></h2>
<p class="mute" id="clhow"></p>
<form id="clf" class="row"><input id="cltk" placeholder="Tickers, optional (e.g. NVDA, MRVL, SPY) — blank = watchlist + momentum leaders" autocomplete="off" autocapitalize="characters"><button>Run scan</button></form>
<div id="clufb"></div>
<div id="clres" class="tw"></div>
<p class="mute">How to read it: <b>Chance of profit</b> = chance the stock finishes above breakeven at expiration. <b>Chance +50%</b> / <b>2×</b> = chance the position is worth 50% / 100% more than you paid at some daily close before expiration (1,500 simulated price paths at the option's implied volatility, IV held constant) — most call buyers take profits early, so this matters more than the expiration odds. <b>Decay / day</b> = what time decay costs you per day. <b>Acts like</b> = delta × 100 shares. <b>Time value</b> = the part of the price above intrinsic value; for LEAPS it is shown per year. Earnings before expiration usually inflate option prices that then drop after the report (IV crush). Scans your list, or your watchlist plus the strongest uptrending names from the last momentum scan. Quotes: CBOE, ~15 min delayed. Click <b>Calculator</b> on any row to see its profit/loss chart. Not financial advice.</p></div>
</div>
<div id="tabCalc" hidden>
<div class="card" id="calc"><h2>Options calculator <span class="mute" style="font-size:13px;font-weight:400">· see what can happen before you trade</span></h2>
<div class="ctl" id="castrat"></div>
<p id="cadesc"></p>
<form id="caload" class="row"><input id="catk" placeholder="Ticker (optional — loads real prices)" autocomplete="off" autocapitalize="characters"><select id="caexp" style="flex:none;max-width:180px"><option value="">Expiration</option></select><button>Load</button></form>
<div id="canote" class="mute" style="font-size:12.5px"></div>
<div class="sets pfil" id="cain"></div>
<div id="caout"></div>
<div class="casave"><input id="canote_in" placeholder="Note (optional) — e.g. plan, target, why" maxlength="200"><button type="button" id="casavebtn">Save this trade</button><span id="casavemsg" class="mute"></span></div>
<details class="fibhelp"><summary class="mute">Options words in plain English</summary><ul>
<li><b>Call</b> — the right to <i>buy</i> 100 shares at the strike price until expiration. Buyers want the stock to go up.</li>
<li><b>Put</b> — the right to <i>sell</i> 100 shares at the strike. Buyers want the stock to go down; sellers are paid to promise to buy.</li>
<li><b>Strike</b> — the fixed price in the contract. <b>Expiration</b> — the last day the contract exists.</li>
<li><b>Premium</b> — the option's price, quoted per share. One contract = 100 shares, so a $2.50 premium costs $250.</li>
<li><b>Bid / ask</b> — what buyers pay / sellers want. Mid = halfway; you often fill near the mid with a limit order.</li>
<li><b>Breakeven</b> — the stock price where you neither make nor lose money at expiration.</li>
<li><b>In / out of the money</b> — a call is in the money when the stock is above the strike (a put: below). Out-of-the-money options are cheaper but more likely to expire worthless.</li>
<li><b>Delta</b> — how much the option moves for a $1 move in the stock (0.50 = $50 per contract). Also a rough chance of finishing in the money.</li>
<li><b>Theta</b> — time decay: value lost each day. Buyers pay it, sellers earn it.</li>
<li><b>Implied volatility (IV)</b> — how big a move the market expects. Higher IV = pricier options. It often drops right after earnings.</li>
<li><b>Assignment</b> — if you sold an option and it ends in the money, you must buy (put) or sell (call) the shares.</li>
<li><b>Chance of profit</b> — a model estimate from IV, not a prediction.</li></ul></details>
</div>
<div class="card" id="cssaved"><h2>Saved trades <span id="csnote" class="mute" style="font-size:13px;font-weight:400"></span> <button type="button" id="csref" class="ghost" style="font-size:12px;padding:3px 9px">Refresh live P/L</button></h2>
<div id="cslist" class="tw"><p class="mute">Loading…</p></div>
<p class="mute">Saved on this computer (saved_calcs.json in the app folder), so they show on your phone too. <b>Now</b> = profit/loss if you closed at today's mid prices vs the premiums you saved (CBOE, ~15-min delayed) — only for trades saved with a ticker and expiration. <b>Open</b> puts the saved numbers back into the calculator. Not financial advice.</p></div>
</div>
<div id="tabFlow" hidden>
<div class="card"><h2>Live block trades <span id="bknote" class="mute" style="font-size:13px;font-weight:400"></span></h2>
<div class="sets pfil"><label><span><input type="checkbox" id="bkon" checked> watch during market hours</span></label>
<label>Tape: min value $<input id="bkmin" type="number" step="100000" min="100000"></label><label>or min shares<input id="bksh" type="number" step="1000" min="1000"></label>
<label>Alert (pop-up / phone) at $<input id="bkal" type="number" step="500000" min="250000"></label>
<label style="grid-column:span 2">Tickers (blank = your watchlists + trades + SPY, QQQ)<input id="bktk" type="text" placeholder="e.g. MRVL, NVDA, SPY" style="text-transform:uppercase"></label></div>
<div id="bklist"></div>
<p class="mute">Prints come from Nasdaq Last Sale Plus (free, real-time): Nasdaq's exchanges plus trades reported to the FINRA/Nasdaq TRF, where many dark-pool and block trades print. Venue isn't shown per print, and trades reported to the other TRFs (NYSE) aren't included — a full dark-pool print feed needs a paid consolidated (SIP) subscription. Checked every minute for up to 25 tickers; big prints raise an alert. After FINRA posts each day's off-exchange file (~6 PM ET) you also get an alert for any watched ticker with unusual dark-pool short % or volume.</p></div>
<div class="card"><h2>Ticker: dark pool &amp; insider activity</h2>
<form id="flf" class="row"><input id="flt" placeholder="Ticker (e.g. MRVL)" autocomplete="off" autocapitalize="characters"><button>Load</button>
<a id="flx" class="ghost lbtn" href="https://xynth.com/" target="_blank" rel="noopener" title="Opens Xynth in your browser (your own login)">Open in Xynth ↗</a>
<a id="floi" class="ghost lbtn" href="http://openinsider.com/" target="_blank" rel="noopener">OpenInsider ↗</a><a id="flsec" class="ghost lbtn" href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&amp;type=4" target="_blank" rel="noopener">SEC Form 4s ↗</a></form>
<div id="flres"><p class="mute">Enter a ticker, or click one in the lists below.</p></div></div>
<div class="card"><h2>Market: unusual dark pool activity &amp; insider buying <button type="button" id="flmr" class="ghost" style="font-size:12px;padding:3px 9px">Refresh</button></h2><div id="flmkt"></div>
<p class="mute">Sources, all free and without an account: <b>FINRA</b> Reg SHO daily short-sale volume for off-exchange trades (TRF/ADF/ORF — dark pools plus wholesaler internalization; published each evening). <b>FINRA OTC Transparency</b> weekly ATS (dark pool) volume by venue (2-week delay for NMS stocks). <b>SEC EDGAR Form 4</b> insider filings, located through OpenInsider — every row links to the filing on sec.gov. Off-exchange short volume is not the same as short interest: market makers short to fill customer buys, so high short % often accompanies buying. Not financial advice.</p></div>
</div>
<p class="mute" style="margin-top:24px">GEX Walls · version __VER__</p>
</main><script src="/lwc.js"></script><script>
const $=s=>document.querySelector(s);
const fmt=(v,d=2)=>v==null?'–':Number(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const money=v=>{if(v==null)return'–';const a=Math.abs(v),s=v<0?'-':'';return a>=1e9?s+'$'+(a/1e9).toFixed(2)+'Bn':a>=1e6?s+'$'+(a/1e6).toFixed(1)+'M':s+'$'+(a/1e3).toFixed(1)+'K'};
const pct=(k,s)=>(k/s*100-100).toFixed(1);
function recent(add){let r=[];try{r=JSON.parse(localStorage.getItem('walls_recent')||'[]')}catch(e){}
 if(add){r=[add,...r.filter(x=>x!==add)].slice(0,10);try{localStorage.setItem('walls_recent',JSON.stringify(r))}catch(e){}}
 $('#recent').innerHTML=r.map(x=>`<span>${x}</span>`).join('');$('#recent').querySelectorAll('span').forEach(el=>el.onclick=()=>go(el.textContent))}
function chart(e,spot,ww,zoom){let st=e.strikes;if(!st.length)return'<p class="mute">No strikes in range.</p>';
 if(zoom){const lo=Math.min(spot*(1-zoom),e.put_wall??Infinity),hi=Math.max(spot*(1+zoom),e.call_wall??-Infinity),z=st.filter(q=>q.k>=lo&&q.k<=hi);if(z.length>=5)st=z;else zoom=0}
 const W=Math.round(ww||Math.min(900,Math.max(320,($('#out').clientWidth||900)-30))),H=W<600?220:260,pl=8,pr=8,pt=16,pb=26,iw=W-pl-pr,ih=H-pt-pb;
 const ks=st.map(s=>s.k),kmin=Math.min(...ks,spot),kmax=Math.max(...ks,spot);
 const m=Math.max(...st.map(s=>Math.max(s.c,-s.p)),1);
 const x=k=>pl+(kmax===kmin?iw/2:(k-kmin)/(kmax-kmin)*iw),y0=pt+ih/2,sc=v=>v/m*(ih/2);
 const bw=Math.max(2,Math.min(18,iw/st.length*0.7));
 let g=`<line x1="${pl}" x2="${W-pr}" y1="${y0}" y2="${y0}" stroke="var(--line)"/>`;
 st.forEach(s=>{if(s.c>0)g+=`<rect x="${x(s.k)-bw/2}" y="${y0-sc(s.c)}" width="${bw}" height="${sc(s.c)}" fill="var(--call)" opacity=".85"><title>${s.k} call ${money(s.c)}</title></rect>`;
  if(s.p<0)g+=`<rect x="${x(s.k)-bw/2}" y="${y0}" width="${bw}" height="${sc(-s.p)}" fill="var(--put)" opacity=".85"><title>${s.k} put ${money(s.p)}</title></rect>`});
 const vl=(k,c,lab,dash,top,anc,dx)=>k==null?'':`<line x1="${x(k)}" x2="${x(k)}" y1="${pt}" y2="${H-pb}" stroke="${c}" stroke-width="1.5" ${dash?'stroke-dasharray="5 4"':''}/><text x="${x(k)+(dx||0)}" y="${top?pt-2:H-8}" fill="${c}" font-size="12" text-anchor="${anc||'middle'}">${lab}</text>`;
 g+=vl(e.put_wall,'var(--put)','PW '+e.put_wall,1,0,'end',-3)+vl(e.call_wall,'var(--call)','CW '+e.call_wall,1,0,'start',3)+vl(spot,'var(--spot)','spot '+fmt(spot),0,1);
 return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="GEX by strike">${g}</svg><div class="mute">Green = call GEX (above axis), red = put GEX (below). Strikes within ±${Math.round((zoom||window._band)*100)}% of spot${zoom?' (plus the walls)':''}.</div>`}
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const RANGES={D:[[63,'3M'],[126,'6M'],[252,'1Y'],[504,'2Y']],W:[[26,'6M'],[52,'1Y'],[104,'2Y'],[260,'5Y']]};
let CC={tf:'D',range:{D:(innerWidth||1000)<600?63:126,W:104},exp:1,r:null,ch:null,key:'',applied:'',fibOn:true,ov:{ma:true,bb:false,vwap:true}};try{CC.fibOn=localStorage.getItem('walls_fib')!=='0';Object.assign(CC.ov,JSON.parse(localStorage.getItem('walls_ov')||'{}'))}catch(e){}
function niceTicks(lo,hi,n){const sp=(hi-lo)/n,p=Math.pow(10,Math.floor(Math.log10(sp))),f=sp/p,st=(f<1.5?1:f<3?2:f<7?5:10)*p;const out=[];for(let v=Math.ceil(lo/st)*st;v<=hi;v+=st)out.push(+v.toFixed(6));return out}
const fmtP=v=>v>=1000?v.toLocaleString(undefined,{maximumFractionDigits:0}):v>=100?v.toFixed(0):v>=10?v.toFixed(1):v.toFixed(2);
const fmtPx=v=>v<=0?'':v>=1000?v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}):v.toFixed(2);
const fmtVol=v=>v==null?'–':v>=1e9?(v/1e9).toFixed(2)+'B':v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':String(v);
// weekly bars from daily bars: [date of first session, open, high, low, close, volume]
function weekly(C){const out=[];let cur=null,wk=null;
 for(const d of C){const t=new Date(d[0]+'T12:00:00Z'),mon=new Date(t-((t.getUTCDay()+6)%7)*864e5).toISOString().slice(0,10);
  if(mon!==wk){if(cur)out.push(cur);wk=mon;cur=[d[0],d[1],d[2],d[3],d[4],d[5]||0]}
  else{cur[2]=Math.max(cur[2],d[2]);cur[3]=Math.min(cur[3],d[3]);cur[4]=d[4];cur[5]+=d[5]||0}}
 if(cur)out.push(cur);return out}
function bars(){const C=(CC.r&&CC.r.candles)||[];return CC.tf=='W'?weekly(C):C}
const gexList=r=>[...(r.expirations||[]),...((r.weeks||[]).filter(w=>!(r.expirations||[]).some(e=>e.exp===w.exp)))];
function chartBlock(r){if(CC.ch){try{CC.ch.remove()}catch(e){}CC.ch=null}CC.key='';CC.applied='';if(!CC.r||CC.r.ticker!==r.ticker){const i=(r.expirations||[]).findIndex(e=>/~30/.test(e.label));CC.exp=i>=0?i:Math.min(1,(r.expirations||[]).length-1)}CC.r=r;if(CC.exp>=gexList(r).length)CC.exp=r.expirations.length-1;
 return `<div class="card"><div class="ctl" id="cctl"></div><div id="cc" class="cc"></div>
 <div class="legend"><span><i class="ln" style="border-color:var(--put)"></i>Put wall</span><span><i class="ln" style="border-color:var(--call)"></i>Call wall</span>
 <span><i class="ln dd1"></i>nearest expiration</span><span><i class="ln dd2"></i>next</span><span><i class="ln dd3"></i>later ones (PW/CW labels show days to expiration)</span><span><i class="ln" style="border-color:var(--amber);border-top-style:solid"></i>Your strike</span>
 <span><i class="ln" style="border-color:var(--fib);border-top-style:dashed"></i>Fib (★ key: 38.2 / 50 / 61.8%)</span><span><i class="ln" style="border-color:#22d3ee;border-top-style:solid"></i>20d</span><span><i class="ln" style="border-color:#f59e0b;border-top-style:solid"></i>50d</span><span><i class="ln" style="border-color:#e879f9;border-top-style:solid"></i>200d</span><span><i class="ln" style="border-color:#94a3b8"></i>Bollinger</span><span><i class="ln" style="border-color:#facc15;border-top-style:solid"></i>VWAP</span><span><i class="bx" style="background:var(--call)"></i><i class="bx" style="background:var(--put)"></i>Call / put GEX by strike (right panel)</span><span id="cchint"></span></div></div>`}
function ctlHtml(){const r=CC.r;
 const tf=[['D','Daily'],['W','Weekly']].map(([k,l])=>`<button class="seg${CC.tf==k?' on':''}" onclick="setTf('${k}')">${l}</button>`).join('');
 const rb=RANGES[CC.tf].map(([n,l])=>`<button class="seg${CC.range[CC.tf]==n?' on':''}" onclick="setRange(${n})">${l}</button>`).join('');
 const GL=gexList(r),nE=r.expirations.length,eb=r.expirations.map((e,i)=>`<button class="seg${CC.exp==i?' on':''}" onclick="setExp(${i})">${esc(e.label)} · ${e.exp.slice(5)}</button>`).join('')+
  (GL.length>nE?`<select class="seg${CC.exp>=nE?' on':''}" style="padding:3px 6px" onchange="setExp(+this.value)" title="GEX profile for any week"><option value="-1"${CC.exp<nE?' selected':''}>Other weeks…</option>${GL.slice(nE).map((e,j)=>`<option value="${nE+j}"${CC.exp==nE+j?' selected':''}>${dshort(e.exp)} · ${e.dte}d</option>`).join('')}</select>`:'');
 return `${tf}<span class="vsep"></span>${rb}<button class="seg${CC.fibOn?' on':''}" style="${CC.fibOn?'background:var(--fib);border-color:var(--fib)':''}" onclick="setFib()" title="Fibonacci retracements of the swing in the selected range">Fib</button>${OVL.map(([k,l,c,t])=>`<button class="seg${CC.ov[k]?' on':''}" style="${CC.ov[k]?'background:'+c+';border-color:'+c+';color:#0b0e14':''}" onclick="setOv('${k}')" title="${t}">${l}</button>`).join('')}<span class="mute" style="margin-left:8px">GEX profile</span>${eb}`}
const OVL=[['ma','MAs','#f59e0b','Moving averages: 20/50/200-day (10/20/40-week on weekly)'],['bb','Bollinger','#94a3b8','Bollinger Bands (20, 2σ)'],['vwap','VWAP','#facc15','VWAP anchored at the Fib swing start (from daily bars)']];
function setTf(k){if(CC.tf!==k){CC.tf=k;drawChart()}}
function setRange(n){CC.range[CC.tf]=n;CC.applied='';drawChart()}
function setExp(i){if(i<0)return;CC.exp=i;drawChart()}
function wallLevels(T){const r=CC.r,s=r.spot,wl=W&&W.config?W.config.watchlist.find(x=>x.ticker==r.ticker):null,strike=wl&&wl.strike,lv=[];
 r.expirations.forEach((e,i)=>{const tag=e.label=='Weekly'?'wk':e.dte+'d';if(e.put_wall!=null)lv.push({k:e.put_wall,t:'PW',i,tag,c:T.put});if(e.call_wall!=null)lv.push({k:e.call_wall,t:'CW',i,tag,c:T.call})});
 if(strike)lv.push({k:strike,t:'Your',i:-1,tag:'',c:T.amber});
 const grp={};lv.filter(l=>l.k>s*0.7&&l.k<s*1.3).forEach(l=>{const key=l.t+l.k;(grp[key]=grp[key]||{...l,tags:[]}).tags.push(l.tag)});
 return Object.values(grp)}
const wallTitle=(l,narrow)=>`${l.t} ${fmtP(l.k)}`+(!narrow&&l.tags.filter(Boolean).length?' · '+l.tags.filter(Boolean).join(' '):'');
function drawChart(){const r=CC.r,el=$('#cc');if(!r||!el)return;$('#cctl').innerHTML=ctlHtml();
 if(bars().length<5){if(CC.ch){try{CC.ch.remove()}catch(e){}CC.ch=null}el.style.height='';el.innerHTML='<p class="mute">No daily price history from CBOE for this ticker.</p>';return}
 if(window.LightweightCharts){try{lwcDraw();return}catch(e){console.error('chart',e);if(CC.ch){try{CC.ch.remove()}catch(_){}}CC.ch=null;CC.key=''}}
 svgDraw()}
// ---- interactive chart (TradingView Lightweight Charts, Apache-2.0, served locally from /lwc.js) ----
const hexA=(h,a)=>{h=h.replace('#','');if(h.length==3)h=h.split('').map(c=>c+c).join('');const n=parseInt(h,16);return `rgba(${n>>16&255},${n>>8&255},${n&255},${a})`};
function theme(){const cs=getComputedStyle(document.documentElement),v=n=>cs.getPropertyValue(n).trim();return{bg:v('--card'),pane:v('--bg'),ink:v('--ink'),mute:v('--mute'),line:v('--line'),put:v('--put'),call:v('--call'),amber:v('--amber'),fib:v('--fib')}}
const paneW=()=>(CC.ch&&CC.ch.timeScale().width())||(($('#cc').clientWidth||900)-60);
const profW=w=>Math.round(Math.min(130,Math.max(54,w*0.15)));
class GexProfile{
 constructor(){this.p=null;this.st=[];this.title='';this.T=null;this.lv=[];this.walls=[];this.narrow=false}
 attached(p){this.p=p} detached(){this.p=null}
 set(st,title,T,walls,narrow){this.st=st;this.title=title;this.T=T;this.walls=walls;this.lv=walls.map(l=>l.k);this.narrow=narrow;if(this.p)this.p.requestUpdate()}
 updateAllViews(){}
 autoscaleInfo(){return this.lv.length?{priceRange:{minValue:Math.min(...this.lv),maxValue:Math.max(...this.lv)}}:null}
 paneViews(){const me=this;return[{zOrder:()=>'bottom',renderer:()=>({draw:t=>t.useMediaCoordinateSpace(sc=>me.draw(sc.context,sc.mediaSize))})},
  {zOrder:()=>'top',renderer:()=>({draw:t=>t.useMediaCoordinateSpace(sc=>me.labels(sc.context,sc.mediaSize))})}]}
 labels(x,m){const s=this.p&&this.p.series,T=this.T;if(!s||!T||!this.walls.length)return;const xr=m.width-profW(m.width)-8;
  x.font='600 11px system-ui,-apple-system,Segoe UI,sans-serif';x.textAlign='right';x.textBaseline='middle';
  const L=this.walls.map(l=>({l,y:s.priceToCoordinate(l.k)})).filter(o=>o.y!=null&&o.y>-10&&o.y<m.height+10).sort((a,b)=>a.y-b.y);let prev=-99;
  for(const o of L){o.ly=Math.max(o.y,prev+15);prev=o.ly}
  for(const o of L){const txt=wallTitle(o.l,this.narrow),w=x.measureText(txt).width+10;x.fillStyle=T.bg;x.strokeStyle=o.l.c;x.lineWidth=1;
   x.beginPath();if(x.roundRect)x.roundRect(xr-w+0.5,o.ly-7.5,w,15,3);else x.rect(xr-w+0.5,o.ly-7.5,w,15);x.fill();x.stroke();x.fillStyle=o.l.c;x.fillText(txt,xr-5,o.ly+0.5)}}
 draw(x,m){const s=this.p&&this.p.series,T=this.T;if(!s||!T)return;
  const pw=profW(m.width),x0=m.width-pw-2,cx=x0+pw/2;
  x.fillStyle=hexA(T.pane,0.9);x.fillRect(x0,0,pw,m.height);x.fillStyle=T.line;x.fillRect(Math.round(cx),0,1,m.height);
  x.fillStyle=T.mute;x.font='11px system-ui,-apple-system,Segoe UI,sans-serif';x.textAlign='center';x.fillText(this.title,cx,13);
  const ys=this.st.map(q=>[q,s.priceToCoordinate(q.k)]).filter(([q,y])=>y!=null&&y>-20&&y<m.height+20);
  if(!ys.length)return;
  const mx=Math.max(1,...ys.map(([q])=>Math.max(q.c,-q.p))),yy=ys.map(a=>a[1]).sort((a,b)=>a-b);
  let dy=99;for(let i=1;i<yy.length;i++)dy=Math.min(dy,yy[i]-yy[i-1]);const bh=Math.max(1.5,Math.min(9,dy*0.7)),L=v=>Math.abs(v)/mx*(pw/2-4);
  for(const [q,y] of ys){if(q.c>0){x.fillStyle=T.call;x.fillRect(cx+1,y-bh/2,L(q.c),bh)}if(q.p<0){x.fillStyle=T.put;x.fillRect(cx-L(q.p),y-bh/2,L(q.p),bh)}}}
}
function lwcDraw(){const r=CC.r,el=$('#cc'),T=theme(),LW=LightweightCharts,key=[r.ticker,r.fetched,CC.tf,T.bg].join('|');
 if(!CC.ch||CC.key!==key){if(CC.ch){try{CC.ch.remove()}catch(e){}}
  const w=el.clientWidth||900,narrow=w<600;el.innerHTML='<div id="ccl" class="cleg"></div>';el.style.height=(narrow?340:430)+'px';
  const ch=LW.createChart(el,{autoSize:true,
   layout:{background:{type:'solid',color:T.bg},textColor:T.mute,fontSize:11,fontFamily:getComputedStyle(document.body).fontFamily},
   grid:{vertLines:{visible:false},horzLines:{color:T.line}},crosshair:{mode:LW.CrosshairMode.Normal},
   rightPriceScale:{borderColor:T.line,scaleMargins:{top:0.08,bottom:0.2}},
   timeScale:{borderColor:T.line,minBarSpacing:0.3,fixLeftEdge:true},localization:{locale:'en-US',priceFormatter:fmtPx}});
  const s=ch.addCandlestickSeries({upColor:T.call,downColor:T.put,borderUpColor:T.call,borderDownColor:T.put,wickUpColor:T.call,wickDownColor:T.put,priceLineColor:T.mute});
  const D=bars();s.setData(D.map(d=>({time:d[0],open:d[1],high:d[2],low:d[3],close:d[4]})));
  const v=ch.addHistogramSeries({priceScaleId:'vol',priceFormat:{type:'volume'},lastValueVisible:false,priceLineVisible:false});
  ch.priceScale('vol').applyOptions({scaleMargins:{top:0.84,bottom:0}});
  v.setData(D.map(d=>({time:d[0],value:d[5]||0,color:hexA(d[4]>=d[1]?T.call:T.put,0.35)})));
  const sty=[LW.LineStyle.Dotted,LW.LineStyle.Dashed,LW.LineStyle.LargeDashed],walls=wallLevels(T);
  walls.forEach(l=>s.createPriceLine({price:l.k,color:l.c,lineWidth:l.i<0?2:1,lineStyle:l.i<0?LW.LineStyle.Solid:(sty[l.i]??LW.LineStyle.Dashed),axisLabelVisible:true,title:''}));
  const gp=new GexProfile();s.attachPrimitive(gp);
  Object.assign(CC,{ch,s,v,gp,D,T,key,n:D.length,walls,narrow,idx:new Map(D.map((d,i)=>[d[0],i])),applied:''});
  {const C=D.map(d=>d[4]),P=CC.tf==='W'?[10,20,40]:[20,50,200],mk=(vals,color,w,st)=>{const x=ch.addLineSeries({color,lineWidth:w||1,lineStyle:st||0,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});x.setData(D.map((d,i)=>vals[i]==null?{time:d[0]}:{time:d[0],value:vals[i]}));return x};
   const bb=TA.bb(C);CC.ovS={ma:[mk(TA.sma(C,P[0]),'#22d3ee',1),mk(TA.sma(C,P[1]),'#f59e0b',2),mk(TA.sma(C,P[2]),'#e879f9',2)],
    bb:[mk(bb.map(x=>x&&x.u),'#94a3b8',1,2),mk(bb.map(x=>x&&x.l),'#94a3b8',1,2)],vwap:[mk(D.map(()=>null),'#facc15',2)]};ovApply()}
  ch.subscribeCrosshairMove(chLegend);ch.subscribeDblClick(()=>{CC.applied='';applyRange()});
  $('#cchint').textContent='· drag to pan · scroll to zoom · double-click to reset'}
 applyRange();fibApply();chLegend(null)}
function fibApply(){if(!CC.ch||!CC.D)return;(CC.fibLines||[]).forEach(l=>{try{CC.s.removePriceLine(l)}catch(e){}});CC.fibLines=[];
 const D=CC.D,k=Math.min(CC.range[CC.tf],D.length),f=CC.fibOn?fibCalc(D.slice(-k)):null;CC.fibCur=f;
 if(f)CC.fibLines=f.levels.map(x=>CC.s.createPriceLine({price:x.price,color:CC.T.fib,lineWidth:[0.382,0.5,0.618].includes(x.r)?2:1,lineStyle:[0.382,0.5,0.618].includes(x.r)?LightweightCharts.LineStyle.Dashed:LightweightCharts.LineStyle.SparseDotted,axisLabelVisible:false,title:''}));
 const fl=f?f.levels.filter(x=>x.r>0&&x.r<1).map(x=>({k:x.price,t:'Fib '+fibName(x.r)+([0.382,0.5,0.618].includes(x.r)?' ★':''),i:-2,tag:'',tags:[],c:CC.T.fib})):[];
 if(CC.ovS&&CC.ovS.vwap){const fd=f?(f.dir==='up'?f.lo_date:f.hi_date):null,i0=fd?D.findIndex(d=>d[0]>=fd):Math.max(0,D.length-k),vw=TA.avwap(D,Math.max(0,i0));CC.ovS.vwap[0].setData(D.map((d,i)=>vw[i]==null?{time:d[0]}:{time:d[0],value:vw[i]}))}
 const sel=gexList(CC.r)[CC.exp];CC.gp.set(sel?sel.strikes||[]:[],sel?`GEX ${sel.label} ${sel.exp.slice(5)}`:'',CC.T,[...CC.walls,...fl],CC.narrow);
 const hint=$('#cchint');if(hint)hint.textContent='· drag to pan · scroll to zoom · double-click to reset'+(f?` · Fib: ${f.dir==='up'?'swing low '+fmt(f.lo)+' ('+f.lo_date.slice(5)+') → high '+fmt(f.hi)+' ('+f.hi_date.slice(5)+'), levels act as support':'swing high '+fmt(f.hi)+' ('+f.hi_date.slice(5)+') → low '+fmt(f.lo)+' ('+f.lo_date.slice(5)+'), levels act as resistance'} — over the selected range`:'')}
function ovApply(){if(!CC.ovS)return;for(const k in CC.ovS)CC.ovS[k].forEach(x=>x.applyOptions({visible:!!CC.ov[k]}))}
function setOv(k){CC.ov[k]=!CC.ov[k];try{localStorage.setItem('walls_ov',JSON.stringify(CC.ov))}catch(e){}$('#cctl').innerHTML=ctlHtml();ovApply()}
function setFib(){CC.fibOn=!CC.fibOn;try{localStorage.setItem('walls_fib',CC.fibOn?'1':'0')}catch(e){}drawChart();fibApply()}
function applyRange(){const a=CC.tf+CC.range[CC.tf];if(!CC.ch||CC.applied===a)return;CC.applied=a;
 const n=CC.n,k=Math.min(CC.range[CC.tf],n),w=paneW(),pw=profW(w),lw=Math.max(0,...(CC.walls||[]).map(l=>wallTitle(l,CC.narrow).length*6.6+16)),extra=Math.ceil(k*(pw+lw+10)/Math.max(120,w-pw-lw));
 CC.ch.timeScale().setVisibleLogicalRange({from:n-k-0.5,to:n-0.5+extra});fibApply()}
const tstr=t=>typeof t=='string'?t:t&&t.year?`${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`:'';
function chLegend(p){const el=$('#ccl');if(!el||!CC.D)return;const D=CC.D;let i=D.length-1;
 const b=p&&p.seriesData?p.seriesData.get(CC.s):null;if(b){const j=CC.idx.get(tstr(b.time));if(j!=null)i=j}
 const d=D[i],pc=i>0?(d[4]/D[i-1][4]-1)*100:null;
 let h=`<b>${CC.r.ticker}</b> <span class="mute">${CC.tf=='W'?'week of ':''}${d[0]}</span> O ${fmtPx(d[1])} H ${fmtPx(d[2])} L ${fmtPx(d[3])} C ${fmtPx(d[4])}${pc==null?'':` <span class="${pc<0?'put':'call'}">${pc>=0?'+':''}${pc.toFixed(2)}%</span>`} <span class="mute">Vol ${fmtVol(d[5])}</span>`;
 if(p&&p.point){const w=paneW(),pw=profW(w);
  if(p.point.x>w-pw-4){const sel=gexList(CC.r)[CC.exp],pr=CC.s.coordinateToPrice(p.point.y);
   if(sel&&pr!=null&&sel.strikes&&sel.strikes.length){const q=sel.strikes.reduce((a,c)=>Math.abs(c.k-pr)<Math.abs(a.k-pr)?c:a);
    h+=`<br><span class="mute">${esc(sel.label)} ${sel.exp.slice(5)} · strike</span> <b>${fmtP(q.k)}</b> <span class="call">call ${money(q.c)}</span> <span class="put">put ${money(q.p)}</span>`}}}
 el.innerHTML=h}
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{CC.key='';drawChart()});
// ---- fallback: static SVG chart if the chart library could not load ----
function svgDraw(){const r=CC.r,el=$('#cc'),C=bars();el.style.height='';
 const full=Math.round(Math.max(320,el.clientWidth||900)),narrow=full<600,H=narrow?300:380,pt=16,pb=22,axW=narrow?42:52,profW2=narrow?64:120,gap=6,pw=full-axW-profW2-gap*2;
 const data=C.slice(-CC.range[CC.tf]),n=data.length,s=r.spot,ex=r.expirations,sel=gexList(r)[CC.exp];
 let lo=Math.min(...data.map(d=>d[3])),hi=Math.max(...data.map(d=>d[2]));
 const T={put:'var(--put)',call:'var(--call)',amber:'var(--amber)'},walls=wallLevels(T);walls.forEach(l=>{lo=Math.min(lo,l.k);hi=Math.max(hi,l.k)});
 const pad=(hi-lo)*0.05;lo-=pad;hi+=pad;const y=v=>pt+(hi-v)/(hi-lo)*(H-pt-pb);
 const labW=Math.max(0,...walls.map(l=>wallTitle(l,narrow).length*6.3+10)),pc=Math.max(120,pw-labW-6);
 const step=pc/n,cw=Math.max(1,Math.min(9,step*0.65)),x=i=>i*step+step/2,ax=pw+gap+profW2+gap;
 let g='';niceTicks(lo,hi,narrow?4:6).forEach(t=>{g+=`<line x1="0" x2="${pc}" y1="${y(t)}" y2="${y(t)}" stroke="var(--line)"/><text x="${ax+4}" y="${y(t)+4}" class="ax">${fmtP(t)}</text>`});
 const yearly=n>1&&(new Date(data[n-1][0])-new Date(data[0][0]))>550*864e5;let lm='';
 data.forEach((d,i)=>{const m=yearly?d[0].slice(0,4):d[0].slice(0,7);if(m!==lm){if(lm&&x(i)>14&&x(i)<pc-10)g+=`<text x="${x(i)}" y="${H-5}" class="ax" text-anchor="middle">${yearly?m:MON[+d[0].slice(5,7)-1]}</text>`;lm=m}});
 data.forEach((d,i)=>{const up=d[4]>=d[1],col=up?'var(--call)':'var(--put)',yo=y(d[1]),yc=y(d[4]);
  g+=`<line x1="${x(i)}" x2="${x(i)}" y1="${y(d[2])}" y2="${y(d[3])}" stroke="${col}"/><rect x="${x(i)-cw/2}" y="${Math.min(yo,yc)}" width="${cw}" height="${Math.max(1,Math.abs(yc-yo))}" fill="${up?'var(--card)':col}" stroke="${col}"/>`});
 const cx=pw+gap+profW2/2,st=((sel&&sel.strikes)||[]).filter(q=>q.k>=lo&&q.k<=hi),mx=Math.max(1,...st.map(q=>Math.max(q.c,-q.p)));
 const ks=st.map(q=>q.k).sort((a,b)=>a-b),dk=ks.length>1?Math.min(...ks.slice(1).map((k,i)=>k-ks[i])):1,bh=Math.max(1.5,Math.min(8,Math.abs(y(0)-y(dk))*0.7));
 g+=`<rect x="${pw+gap}" y="${pt}" width="${profW2}" height="${H-pt-pb}" fill="var(--bg)" rx="4"/><line x1="${cx}" x2="${cx}" y1="${pt}" y2="${H-pb}" stroke="var(--line)"/>`;
 st.forEach(q=>{const L=v=>Math.abs(v)/mx*(profW2/2-3);if(q.c>0)g+=`<rect x="${cx}" y="${y(q.k)-bh/2}" width="${L(q.c)}" height="${bh}" fill="var(--call)" rx="1"><title>${q.k} call GEX ${money(q.c)}</title></rect>`;
  if(q.p<0)g+=`<rect x="${cx-L(q.p)}" y="${y(q.k)-bh/2}" width="${L(q.p)}" height="${bh}" fill="var(--put)" rx="1"><title>${q.k} put GEX ${money(q.p)}</title></rect>`});
 if(sel)g+=`<text x="${pw+gap+profW2/2}" y="${pt-4}" class="ax" text-anchor="middle">${esc(sel.label)} ${sel.exp.slice(5)}</text>`;
 const dash=['2 3','7 4','14 4'];walls.forEach(l=>{g+=`<line x1="0" x2="${pw+gap+profW2}" y1="${y(l.k)}" y2="${y(l.k)}" stroke="${l.c}" stroke-width="1.5" ${l.i>=0?`stroke-dasharray="${dash[l.i]||'4 4'}"`:''} opacity=".9"/>`});
 const labs=walls.slice().sort((a,b)=>y(a.k)-y(b.k));let prevY=-99;labs.forEach(l=>{l.ly=Math.max(y(l.k),prevY+14);prevY=l.ly});
 labs.forEach(l=>{const txt=wallTitle(l,narrow),w=txt.length*6.3+8;g+=`<rect x="${pc+4}" y="${l.ly-9}" width="${w}" height="15" rx="3" fill="var(--card)" stroke="${l.c}"/><text x="${pc+4+w/2}" y="${l.ly+2.5}" class="lb" text-anchor="middle" fill="${l.c}">${txt}</text>`});
 g+=`<rect x="${ax}" y="${y(s)-9}" width="${axW}" height="18" rx="3" fill="var(--spot)"/><text x="${ax+axW/2}" y="${y(s)+4}" class="ax" text-anchor="middle" style="fill:var(--bg)">${s>=1000?fmtP(s):fmt(s)}</text>`;
 g+=`<line id="chx" x1="0" x2="0" y1="${pt}" y2="${H-pb}" stroke="var(--mute)" stroke-dasharray="3 3" visibility="hidden"/><rect id="chit" x="0" y="${pt}" width="${pc}" height="${H-pt-pb}" fill="transparent"/>`;
 el.innerHTML=`<svg viewBox="0 0 ${full} ${H}" width="${full}" height="${H}" role="img" aria-label="${r.ticker} ${CC.tf=='W'?'weekly':'daily'} candles with put and call walls">${g}</svg><div class="tip" id="ctip"></div>`;
 const hit=el.querySelector('#chit'),chx=el.querySelector('#chx'),tip=el.querySelector('#ctip');
 const mv=ev=>{const b=hit.getBoundingClientRect(),mxp=(ev.touches?ev.touches[0].clientX:ev.clientX)-b.left,i=Math.max(0,Math.min(n-1,Math.floor(mxp/(b.width/n)))),d=data[i],chg=i>0?(d[4]/data[i-1][4]-1)*100:null;
  chx.setAttribute('x1',x(i));chx.setAttribute('x2',x(i));chx.setAttribute('visibility','visible');
  tip.innerHTML=`<b>${CC.tf=='W'?'Week of ':''}${d[0]}</b><br>O ${fmt(d[1])} H ${fmt(d[2])}<br>L ${fmt(d[3])} C ${fmt(d[4])}${chg==null?'':` <span class="${chg<0?'put':'call'}">${fmt(chg,1)}%</span>`}<br><span class="mute">Vol ${d[5]==null?'–':Number(d[5]).toLocaleString()}</span>`;
  tip.style.display='block';const left=x(i)/full*el.clientWidth;tip.style.left=(left>el.clientWidth/2?left-tip.offsetWidth-10:left+10)+'px';tip.style.top='8px'};
 hit.addEventListener('mousemove',mv);hit.addEventListener('touchmove',mv,{passive:true});hit.addEventListener('mouseleave',()=>{tip.style.display='none';chx.setAttribute('visibility','hidden')});
 $('#cchint').textContent=window.LightweightCharts?'':'· interactive chart library not loaded — showing static chart'}
let _rz;window.addEventListener('resize',()=>{clearTimeout(_rz);_rz=setTimeout(()=>{if(!CC.ch)drawChart()},150)});
// ---- put credit spread setups for the looked-up ticker ----
let ST={win:'monthly',prof:'balanced'};try{const v=JSON.parse(localStorage.getItem('walls_strat')||'{}');if(v.win)ST.win=v.win;if(v.prof)ST.prof=v.prof}catch(e){}
const PROF={conservative:['Conservative','POP ≥ 78% · short delta ≤ 0.20 · favors a short strike at or below the put wall · skips spreads that hold through earnings'],
 balanced:['Balanced','POP 66–78% · short delta ≤ 0.30 · best POP × credit/width (same score as the scanner) · skips spreads that hold through earnings'],
 aggressive:['Aggressive','POP 55–68% · short delta ≤ 0.40 · most premium per dollar of width · may hold through earnings (flagged)']};
const SWIN={'0dte':'0 DTE',d5:'1–5 DTE',weekly:'6–12 DTE',w2:'13–20 DTE',w3:'21–29 DTE',monthly:'30–45 DTE',m60:'46–60 DTE',m90:'61–90 DTE'};
const SPROF={conservative:'POP ≥ 84% · short delta ≤ 0.20 · no earnings before expiration',balanced:'POP 80–84% · short delta ≤ 0.25 · no earnings before expiration',aggressive:'POP 72–80% · short delta ≤ 0.30 · no earnings before expiration'};
const isShortWin=w=>w==='0dte'||w==='d5';
const fmtK=k=>Number.isInteger(k)?String(k):String(+(+k).toFixed(2));
function stratSet(k,v){ST[k]=v;try{localStorage.setItem('walls_strat',JSON.stringify(ST))}catch(e){}stratDraw()}
function regime(r,c){const t=r.trend,o=[];
 if(t)o.push(t.trend>=2?'<span class="call">Uptrend</span> (above 20/50-day avg)':t.trend==0?'<span class="put">Downtrend</span> (below 20/50-day avg)':'Mixed trend');
 if(c&&c.net_gex!=null)o.push(c.net_gex>=0?'positive gamma at this expiration':'<span class="put">negative gamma</span> at this expiration');
 if(r.rsi!=null&&r.rsi<30)o.push('RSI oversold');else if(r.rsi!=null&&r.rsi>70)o.push('<span class="amb">RSI overbought</span>');
 let v='';if(t&&c){if(t.trend==0&&c.net_gex<0)v=' — bullish spreads fight the trend here; stick to Conservative or wait';else if(t.trend>=2&&c.net_gex>=0)v=' — supportive backdrop for put credit spreads'}
 return o.join(' · ')+v}
function setupsDraw(){const r=CC.r,el=$('#setufb');if(!r||!el)return;const rows=[];
 for(const [k,w] of Object.entries((r.strategy||{}).windows||{}))for(const c of ((w||{}).profiles||{})[GF.prof]||[])rows.push(pcsNorm(c));
 for(const [k,L] of Object.entries(r.short_puts||{}))for(const c of L||[])rows.push({exp:c.exp,strat:'CSP'});
 for(const [k,w] of Object.entries(r.bull_call||{}))for(const c of ((w||{}).profiles||{})[GF.prof]||[])rows.push({exp:c.exp,strat:'BCS'});
 el.innerHTML=`<div class="card" style="padding-bottom:4px"><div class="shead"><h2>Trade setups for ${esc(r.ticker)}</h2><span class="mute">· put credit spreads, short puts and bull call spreads — the same selections as every other tab</span></div>${ufb('setup',rows,{strats:['PCS','CSP','BCS'],prof:true})}</div>`;
 const st=gst('setup');for(const [id,k] of [['strat','PCS'],['sputs','CSP'],['bcs','BCS']]){const c=$('#'+id);if(c)c.hidden=!(st==='all'||st===k)}
 stratDraw();spDraw();bcsDraw()}
function spDraw(){const el=$('#sputs'),r=CC.r;if(!el||!r)return;const SP=r.short_puts||{};
 let h=`<div class="shead"><h2>Short put setups</h2><span class="mute">cash-secured · ${esc(r.ticker)} ${fmt(px(r))} · RSI(14) ${r.rsi==null?'n/a':fmt(r.rsi,1)}</span></div>`;
 const all=[];for(const L of Object.values(SP))for(const c of L||[])all.push({c,exp:c.exp,strat:'CSP',pop:c.pop,ret:c.roc,cap:c.cash,prem:c.prem,best:c.roc*(c.pop/100),p50:c.p50,dte:c.dte,flag:!!c.earn_cross});
 const seen=new Set(),L=gfSort(gfFilter(all,{})).filter(x=>{const k=x.exp+x.c.short;if(seen.has(k))return false;seen.add(k);return true}).slice(0,Math.min(GF.n,10));
 if(!L.length){el.innerHTML=h+`<p class="mute">${all.length?'None pass these selections.':'No short put pays 10%+ of the cash tied up with a 70%+ chance of profit here (that takes very high implied volatility).'} A put credit spread does the same job with far less cash.</p>`;return}
 h+='<div class="tw"><table><tr><th>Expiration</th><th>Sell put</th><th>Cash → premium</th><th>Return</th><th>POP</th><th>Breakeven</th><th>Hits 50%</th><th>Put wall</th><th></th></tr>'+L.map(x=>{const c=x.c;return `<tr><td>${dshort(c.exp)} <span class="mute">${c.dte}d</span></td><td><b>${fmtK(c.short)}P</b> ${STARS(starsOf(c.pop/100,c.roc/100,0.15))}<br><span class="mute">${fmt(c.otm,1)}% OTM · Δ ${fmt(c.sdelta)}</span></td><td>$${fmt(c.cash,0)} → <b class="call">+$${fmt(c.prem,0)}</b></td><td><b>${fmt(c.roc,1)}%</b><br><span class="mute">${fmt(c.ann,0)}%/yr</span></td><td>${fmt(c.pop,1)}%</td><td>${fmt(c.be)}</td><td>${c.p50==null?'–':fmt(c.p50,0)+'%'}</td><td>${c.put_wall==null?'–':fmtK(c.put_wall)}${c.below_wall?' <span class="call">≤ wall</span>':''}</td><td>${trkBtn({tk:r.ticker,exp:c.exp,strat:'CSP',legs:[{cp:'P',K:c.short,side:-1,n:1}],what:'sell '+c.short+'P',kind:'credit',price:c.credit,capital:c.cash,rec_p:c.pop/100,src:'Walls & spreads · short put'})}</td></tr>`}).join('')+'</table></div>';
 el.innerHTML=h}
// ---- news, analysts, financial health, dilution, candle patterns (loads after the ticker) ----
const INS={};
async function insLoad(t){const el=$('#insg');if(!el)return;try{const x=INS[t]&&Date.now()-INS[t].ts<600000?INS[t].x:await (await fetch('/api/insights?t='+encodeURIComponent(t))).json();INS[t]={ts:Date.now(),x};if(CC.r&&CC.r.ticker===t)insDraw(x)}catch(e){el.innerHTML='<h2>News, analysts &amp; financial health</h2><p class="err">'+esc(e.message)+'</p>'}}
function insDraw(x){const el=$('#insg');if(!el)return;BADGES[x.ticker]=x.badges||[];
 const hx=$('#hdrx');if(hx){const e=x.ext||{},p=extPick(e,['After hours','Pre-market']);
  hx.innerHTML=(x.badges||[]).map(b=>`<span class="chip ${b.c||'mute'} bdg" title="${esc(b.tip||'')}">${esc(b.t)}</span>`).join('')+(p?` <span class="${p[1].pct<0?'put':'call'}" title="${esc((p[1].ref?'% vs '+p[1].ref+(p[1].close?' $'+fmt(p[1].close):'')+' · ':'')+(p[1].updated||''))}">${p[0]} ${fmt(p[1].last)}${p[1].pct==null?'':' '+(p[1].pct>=0?'+':'')+fmt(p[1].pct,2)+'%'} · vol ${big(p[1].vol)}${p[1].hi?' · range '+fmt(p[1].lo)+'–'+fmt(p[1].hi):''}</span>`:'')+((e.overnight||{}).traded?` <span class="mute" title="FINRA ATS weekly data, ~2-week lag">· overnight volume ${big(e.overnight.shares)} sh (wk of ${esc(e.overnight.week||'')})</span>`:'')}
 const a=x.analyst||{},h=x.health||{},n=x.news||{},cs=x.candles||[];const tot=(a.buy||0)+(a.hold||0)+(a.sell||0),up=a.target&&x.price?(a.target/x.price-1)*100:null;
 const tile=(l,v,s_,c)=>`<div class="gt b-${c||'mute'}"><div class="l">${l}</div><div class="v">${v}</div>${s_?`<div class="l">${s_}</div>`:''}</div>`;
 let o='<h2>News, analysts &amp; financial health</h2><div class="gtiles">';
 o+=tile('Analysts (Nasdaq.com)',tot?`${esc(a.consensus||a.mean_rating||'')} · ${a.buy||0} buy / ${a.hold||0} hold / ${a.sell||0} sell`:'no coverage',a.target?`target ${fmt(a.target)} (${up>=0?'+':''}${fmt(up,0)}%) · range ${fmt(a.low)}–${fmt(a.high)}`:'',tot&&a.buy/tot>=0.6?'call':tot&&a.sell/tot>=0.3?'put':'mute');
 const rv=a.revisions_4w||{};o+=tile('EPS estimate revisions, 4 wks',`${(rv.q_up||0)+(rv.y_up||0)} up · ${(rv.q_down||0)+(rv.y_down||0)} down`,'quarter + year estimates',((rv.q_up||0)+(rv.y_up||0))>((rv.q_down||0)+(rv.y_down||0))?'call':((rv.q_down||0)+(rv.y_down||0))>((rv.q_up||0)+(rv.y_up||0))?'put':'mute');
 o+=tile('Financial health (SEC filings)',h.error?'–':`<b>${esc(h.grade||'–')}</b>`,h.error?esc(h.error):esc((h.grade_why||[]).join(' · ')),{A:'call',B:'call',C:'amb',D:'put',F:'put'}[h.grade]||'mute');
 o+=tile('Dilution',h.dilution_1y_pct==null?'–':`${h.dilution_1y_pct>=0?'+':''}${fmt(h.dilution_1y_pct,1)}% shares / yr`,`${h.n_424b_12m?h.n_424b_12m+' offering prospectus (424B) filings · ':''}${h.runway_months!=null?'cash runway ~'+fmt(h.runway_months,0)+' months':''}`,h.dilution_risk?'put':'mute');
 o+='</div>';
 if(cs.length)o+=`<h3>Candle signals (daily)</h3><ul class="acts">${cs.slice(0,4).map(c=>`<li><b class="${c.bull?'call':'put'}">${c.extra?'✦ ':''}${esc(c.name)}</b> <span class="mute">${esc(c.date)}</span> — ${esc(c.why)}${(c.at||[]).length?` · <b>at ${esc(c.at.join(', '))}</b>`:''}${c.volume_surge?' · volume surge':''}</li>`).join('')}</ul>`;
 else o+='<p class="mute">No notable candle pattern on the latest daily bar.</p>';
 if(h&&!h.error){o+=`<details><summary class="mute">Balance sheet & cash flow (SEC XBRL, as of ${esc(h.asof||'')})</summary><div class="tw"><table><tr><th>Cash + short-term inv.</th><th>Debt</th><th>Current ratio</th><th>Revenue (FY)</th><th>Revenue growth</th><th>Net income (FY)</th><th>Operating cash flow (FY)</th><th>Shares out</th></tr><tr><td>${usd2(h.cash)}</td><td>${usd2(h.debt)}</td><td>${h.current_ratio==null?'–':fmt(h.current_ratio,2)}</td><td>${usd2(h.revenue_fy)}</td><td>${h.revenue_growth==null?'–':fmt(h.revenue_growth,0)+'%'}</td><td>${usd2(h.net_income_fy)}</td><td>${usd2(h.op_cash_flow_fy)}</td><td>${big(h.shares_now)}${h.shares_1y_ago?` <span class="mute">(yr ago ${big(h.shares_1y_ago)})</span>`:''}</td></tr></table></div>`+
  ((h.offering_filings_12m||[]).length?`<p>Offering / shelf filings in the last 12 months: ${(h.offering_filings_12m||[]).map(f=>`<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.form)} ${esc(f.date)}</a>`).join(' · ')}</p>`:'')+
  ((h.recent_8k||[]).length?`<p>Recent 8-Ks: ${(h.recent_8k||[]).map(f=>`<a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.date)}${f.items?' ('+esc(f.items)+')':''}</a>`).join(' · ')}</p>`:'')+'</details>'}
 if((n.headlines||[]).length)o+=`<h3>Latest news</h3><ul class="acts">${n.headlines.slice(0,6).map(x=>`<li>${x.press_release?'<span class="chip acc" title="company press release">PR</span> ':''}<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a> <span class="mute">· ${esc(x.publisher)} · ${esc(x.ago||x.date||'')}</span></li>`).join('')}</ul>`;
 {const T=x.top_analysts||[],C=x.top_consensus,S=x.top_sources||{};o+=`<h3>★ Top-ranked analysts</h3>`+(C?`<p><b class="${C.lean==='Buy'?'call':C.lean==='Sell'?'put':'acc'}">${C.agree?'★★ ':'★ '}${C.n} ranked analyst${C.n>1?'s':''} cover this: ${C.buy} Buy · ${C.hold} Hold · ${C.sell} Sell${C.n>C.rated?' · '+(C.n-C.rated)+' target only':''}</b>${C.agree?` — <b>they agree (${esc(C.lean)})</b>`:C.rated>1?' — <span class="amb">they disagree</span>':''}${C.sources.length>1?` <span class="mute">· across ${C.sources.map(esc).join(' + ')}</span>`:''}</p>`:'')+(T.length?`<ul class="acts">${T.map(t=>`<li><span class="chip mute">${esc(t.label||t.src)}</span> <b class="${t.bull?'call':t.bear?'put':'acc'}">${esc(t.top_name)}</b>${t.likely?' <span class="mute" title="MarketBeat lists this rating without the analyst name; it is from their firm in their sector">(likely)</span>':''} (${esc(t.top_firm)}) · ${esc(t.action||'')} <b>${esc(t.rating||(t.dir_rating?t.dir_rating+' (earlier)':''))}</b>${t.target?' · target '+fmt(t.target):''} <span class="mute">· ${esc(t.date)}</span></li>`).join('')}</ul>`:`<p class="mute">${x.top_err?'Could not load analyst ratings ('+esc(x.top_err)+').':'None of the top-ranked analysts rated this stock in the last 12 months.'}</p>`)+`<p class="mute" style="font-size:12px">${Object.entries(S).map(([k,v])=>'<b>'+esc(k)+'</b>: '+esc(v)).join(' · ')}. ★★ = two or more ranked analysts all on the same side. StarMine: no stock-analyst awards published since 2020 (its current awards cover economists and FX), so it is not used. Rankings change a lot from year to year — a top analyst can still be wrong.</p>`}
 if((a.firms||[]).length)o+=`<p class="mute" style="font-size:12px">Covering firms (${a.firms.length}): ${a.firms.map(f=>(a.star_firms||[]).includes(f)?`<b class="acc">${esc(f)}</b>`:esc(f)).join(', ')}.</p>`;
 o+='<p class="mute" style="font-size:12px">Sources: company financials and filings from SEC EDGAR (primary); headlines, analyst consensus and estimates from Nasdaq.com; individual analyst ratings from MarketBeat (aggregator — brokers do not publish ratings, so there is no primary source); top-analyst list from the TipRanks ranking as published; extended-hours trades from Nasdaq.com; overnight (8 PM–4 AM ET) volume from FINRA ATS data (Blue Ocean, MOON, Bruce ATS). Candle signals are computed here from CBOE daily bars. Not financial advice.</p>';
 el.innerHTML=o}
const wkLabel=k=>(WCHIPS().find(w=>w.k===k)||{}).label||k;
const shortSel=()=>{if(GF.wk==='0dte')return true;const w=WEEKS().find(x=>x.k===GF.wk);if(!w)return false;const d=new Date(etToday()+'T12:00:00Z');return (new Date(w.hi+'T12:00:00Z')-d)/864e5<=6};
function mergeWin(WS){WS=WS||{};if(GF.wk!=='all')return WS[GF.wk]||{profiles:{},nearest:{},best:[],n:0,exps:[]};
 const out={profiles:{},nearest:{},best:[],n:0,n_all:0,exps:[]};
 for(const [k,w] of Object.entries(WS)){if(!w)continue;out.n+=w.n||0;out.n_all+=w.n_all||0;out.exps.push(...(w.exps||[]));
  for(const [p,L] of Object.entries(w.profiles||{}))(out.profiles[p]=out.profiles[p]||[]).push(...L);
  for(const [p,L] of Object.entries(w.nearest||{}))(out.nearest[p]=out.nearest[p]||[]).push(...L);out.best.push(...(w.best||[]))}
 for(const o of [out.profiles,out.nearest])for(const p in o)o[p].sort((a,b)=>(b.rank||0)-(a.rank||0));out.best.sort((a,b)=>(b.rank||0)-(a.rank||0));out.best=out.best.slice(0,6);return out}
const pcsNorm=c=>({c,exp:c.exp,strat:'PCS',pop:c.pop,ret:c.roc,cap:c.maxloss,prem:(c.credit||0)*100,best:c.rank||0,p50:c.p50,dte:c.dte,flag:!!c.earn_cross});
const pcsFilt=L=>gfSort(gfFilter(L.map(pcsNorm),{noWk:true})).map(x=>x.c).slice(0,Math.max(3,Math.min(GF.n,6)));
function stratDraw(){const el=$('#strat'),r=CC.r;if(!el||!r)return;const S=r.strategy,d2=v=>fmt(v,2);
 let h=`<div class="shead"><h2>Put credit spread setups</h2><span class="mute">${esc(r.ticker)} ${fmt(px(r))} · RSI(14) ${r.rsi==null?'n/a':fmt(r.rsi,1)} · option quotes as of ${esc(hhmm(r.last_trade))} ET (CBOE, 15-min delayed)</span></div>`;
 if(!S||S.error){el.innerHTML=h+`<p class="mute">Not available${S&&S.error?': '+esc(S.error):''}.</p>`;return}
 h+=`<div class="mute" style="margin:-2px 0 8px">${shortSel()?SPROF[GF.prof]:PROF[GF.prof][1]} · every spread pays 15%+ of its max loss</div>`+(shortSel()?`<div class="al warn" style="margin:0 0 8px"><b>${wkLabel(GF.wk)}</b>${GF.wk==='0dte'?'Expiring today; POP uses the minutes left to 4:00 PM ET. ':''}CBOE quotes are about 15 minutes delayed and short-dated premiums move fast — re-check the live bid/ask before the order.</div>`:'');
 const Wn=mergeWin(S.windows);let P=pcsFilt((Wn.profiles||{})[GF.prof]||[]);const fb=!P.length&&pcsFilt((Wn.nearest||{})[GF.prof]||[]).length>0;if(fb)P=pcsFilt(Wn.nearest[GF.prof]);
 const earnTxt=S.earnings?`<span class="amb">Earnings ${S.earnings}</span>`:S.earnings_known?'No earnings in the next ~7 weeks (Nasdaq.com calendar)':'Earnings date unavailable';
 if(!P.length){let why=GF.wk==='0dte'&&!Wn.n?'No 0 DTE spreads right now. Same-day contracts only exist on an expiration day while the market is open (SPY, QQQ, IWM and SPX expire every weekday; most stocks only on Fridays).':Wn.n?`No ${wkLabel(GF.wk)} spread meets the ${PROF[GF.prof][0]} rules.`:(Wn.listed||[]).length&&!(Wn.quoted||[]).length?`${wkLabel(GF.wk)}: the ${Wn.listed.join(', ')} expiration is listed but has no bids or open interest yet (newly listed) — check back closer to the date.`:Wn.listed&&!Wn.listed.length&&GF.wk!=='0dte'?`No expiration is listed for ${wkLabel(GF.wk)}.`:`No liquid put spreads for ${wkLabel(GF.wk)} (open interest under 50 / 20, or quotes too wide).`;
  if(Wn.n&&S.earnings&&GF.prof!=='aggressive'&&(Wn.exps||[]).some(x=>S.earnings<=x))why+=` Earnings (${S.earnings}) come before expiration and this profile skips spreads that hold through earnings — try Aggressive or a shorter window.`;
  else if(Wn.n)why+=' Try another profile or expiration window.';
  el.innerHTML=h+`<p class="mute">${why}</p>${bestAvail(Wn,null)}<p class="mute">${regime(r,null)} · ${earnTxt}</p>`;return}
 const c=P[0],flags=[];
 if(fb)h+=`<div class="al warn" style="margin:0 0 8px"><b>No ${wkLabel(GF.wk)} spread meets every ${PROF[GF.prof][0]} rule — closest available</b>
  Not ideal because:<ul style="margin:4px 0 4px 18px">${(c.issues||[]).map(x=>`<li>${esc(x)}</li>`).join('')||'<li>it sits just outside this profile’s ranges</li>'}</ul><span>${esc(c.why||'')}</span> Size it smaller or wait for better quotes; another profile or window may fit cleanly.</div>`;
 if(c.earn_cross)flags.push(`<span class="amb">⚠ Earnings ${S.earnings} before expiration — a gap can blow through both strikes</span>`);
 if(c.slip>30)flags.push(`<span class="amb">⚠ Wide quotes: mid ${d2(c.credit)} vs natural ${d2(c.nat)} — work a limit near mid</span>`);
 if(c.s_oi<200||c.l_oi<100)flags.push(`<span class="amb">⚠ Thin open interest (${c.s_oi.toLocaleString()} short / ${c.l_oi.toLocaleString()} long)</span>`);
 h+=`<div class="pick"><div class="ptitle">${setupSig(c,r.trend?r.trend.trend:null)} Sell <b>${fmtK(c.short)}P</b> / Buy <b>${fmtK(c.long)}P</b> <span class="mute">· ${c.exp} (${c.dte} DTE) · $${fmtK(c.width)} wide</span> <button class="ghost" style="font-size:12px;padding:3px 9px" onclick="prefillTrade('${r.ticker}','${c.exp}',${c.short},${c.long},${c.credit},'Walls & spreads · put credit spread',${c.pop})">+ Track</button></div>
 <div class="kpis">
 <div class="kpi"><div class="l">Credit (fill) · mid / natural</div><div class="v">${d2(c.credit)} <span class="mute">· ${c.mid!=null?d2(c.mid)+' / ':''}${d2(c.nat)}</span></div><div class="l">$${fmt(c.credit*100,0)} per spread</div></div>
 <div class="kpi"><div class="l">Max loss</div><div class="v put">$${fmt(c.maxloss,0)}</div><div class="l">return on risk ${c.roc==null?'–':fmt(c.roc,0)+'%'}</div></div>
 <div class="kpi"><div class="l">POP (est.)</div><div class="v">${fmt(c.pop,1)}%</div><div class="l">credit / width ${fmt(c.cw*100,0)}%</div></div>
 <div class="kpi"><div class="l">Breakeven</div><div class="v">${d2(c.be)}</div><div class="l">${fmt((c.be/px(r)-1)*100,1)}% from current price</div></div>
 <div class="kpi"><div class="l">Short put</div><div class="v">Δ ${d2(c.sdelta)}</div><div class="l">${fmt(c.otm,1)}% OTM${c.em_mult!=null?' · '+fmt(c.em_mult,2)+'× expected move':''}</div></div>
 <div class="kpi"><div class="l">Take profit · buy back at</div><div class="v">${d2(c.tp30)} <span class="mute">/</span> ${d2(c.tp50)}</div><div class="l">30% +$${fmt(c.credit*30,0)} · 50% +$${fmt(c.credit*50,0)}</div></div>${c.p50==null?'':`<div class="kpi"><div class="l">When 50% profit (est.)</div><div class="v">${c.d50_date?dshort(c.d50_date):'–'}</div><div class="l">${fmt(c.p50,0)}% odds${c.d50_date?' · likely '+dshort(c.d50_lo_date)+' – '+dshort(c.d50_hi_date):''}${c.flat_date?'<br>if price holds flat: '+dshort(c.flat_date):''}</div></div>`}
 <div class="kpi"><div class="l">Support above your short</div><div class="v">${(c.fib_support||[]).length+(c.below_wall?1:0)}</div><div class="l">${[c.below_wall&&c.put_wall!=null?'put wall '+fmtK(c.put_wall):'',...(c.fib_support||[]).map(f=>'Fib '+fibName(f.r)+' '+fmt(f.price))].filter(Boolean).join(' · ')||'<span class="amb">none between price and strike</span>'}</div></div>
 <div class="kpi"><div class="l">Put wall (this expiration)</div><div class="v">${c.put_wall==null?'–':fmtK(c.put_wall)}</div><div class="l">${c.below_wall?'<span class="call">short at/below wall ✓</span>':'short is above the wall'}</div></div>
 <div class="kpi"><div class="l">Net GEX (this expiration)</div><div class="v ${c.net_gex<0?'put':'call'}">${money(c.net_gex)}</div><div class="l">${c.net_gex>=0?'dampening / range':'amplifying / trend'}</div></div>
 </div>${flags.length?'<div class="flags">'+flags.join('<br>')+'</div>':''}</div>`;
 if(P.length>1)h+=`<div class="tw"><table><tr><th>${fb?'Other close options':'Alternatives'}</th><th>Expiration</th><th title="Realistic fill: halfway between mid and natural">Credit (fill)</th><th>Max loss</th><th>POP</th><th>Hits 50%</th><th>B/E</th><th>Δ</th><th>Cr/W</th><th>Wall</th>${fb?'<th>Not ideal because</th>':''}</tr>`+
  P.slice(1).map(a=>`<tr><td>${fmtK(a.short)} / ${fmtK(a.long)}P${a.earn_cross?' <span class="amb" title="earnings before expiration">⚠E</span>':''}</td><td>${a.exp.slice(5)} <span class="mute">${a.dte}d</span></td><td>${d2(a.credit)} <span class="mute">(${d2(a.nat)})</span></td><td>$${fmt(a.maxloss,0)}</td><td>${fmt(a.pop,1)}%</td><td>${a.p50==null?'–':fmt(a.p50,0)+'%'+(a.d50_date?' <span class="mute">'+dshort(a.d50_date)+'</span>':'')}</td><td>${d2(a.be)}</td><td>${d2(a.sdelta)}</td><td>${fmt(a.cw*100,0)}%</td><td>${a.below_wall?'<span class="call">≤ wall</span>':'above'}</td>${fb?`<td class="amb" style="text-align:left;font-size:12px">${(a.issues||[]).map(esc).join('<br>')}</td>`:''}</tr>`).join('')+'</table></div>';
 h+=bestAvail(Wn,c);
 h+=`<p class="mute" style="margin-top:8px">${regime(r,c)} · ${earnTxt}</p>
 <p class="mute">POP is a lognormal estimate at breakeven from CBOE implied volatility; quotes are 15-min delayed and widen after the close. Enter with a limit near mid and confirm the live bid/ask first. Not financial advice.</p>`;
 el.innerHTML=h}
const px=r=>r.live&&r.live.price?r.live.price:r.spot;
const hhmm=t=>{t=String(t||'');const m=t.match(/(\d{2}:\d{2})/);if(!m)return t||'n/a';let today='';try{today=new Date().toLocaleDateString('en-CA',{timeZone:'America/New_York'})}catch(e){}const d=t.slice(0,10);return /^\d{4}-\d{2}-\d{2}$/.test(d)&&today&&d!==today?d.slice(5)+' '+m[1]:m[1]};
function staleNote(r){return (r.chain_stale?`<div class="al bad" style="margin:8px 0 0"><b>CBOE has not refreshed ${esc(r.ticker)}'s option quotes since ${esc(String(r.chain_time||'').slice(5))}</b> — walls, credits, chances and returns on this page are from then, not now. Do not trade from these numbers; check your broker. They update when CBOE rebuilds the chain (this happens for less-traded symbols).</div>`:'')+staleNote0(r)}
function staleNote0(r){if(!r.live||!r.live.price||!r.spot)return'';const mv=(r.live.price/r.spot-1)*100;if(Math.abs(mv)<0.5)return'';
 return `<div class="al warn" style="margin:8px 0 0"><b>Price has moved ${mv>=0?'+':''}${fmt(mv,1)}% since the option quotes</b>${esc(r.ticker)} is ${fmt(r.live.price)} now vs ${fmt(r.spot)} in CBOE's delayed snapshot (${esc(hhmm(r.last_trade))} ET). Walls are levels and still apply; credits, POP and % OTM below are from the delayed snapshot — confirm in your broker.</div>`}
// ---- easy indicators ----
function setupSig(c,trend){if(!c)return'';const why=[];let lvl='ok';
 if(c.earn_cross){lvl='bad';why.push('earnings before expiration')}
 if(c.slip>45){lvl='bad';why.push('very wide quotes')}
 if(trend===0&&c.net_gex<0){lvl='bad';why.push('downtrend + negative gamma')}
 if(lvl!=='bad'&&c.pop>=75&&(c.p50==null||c.p50>=85)&&(c.below_wall||(c.fib_support||[]).length)&&c.slip<=30&&trend!==0){lvl='good';why.push('high POP, support (put wall/Fib) above the short strike, tight quotes')}
 if(lvl==='ok')why.push('meets the rules; no strong edge either way');
 const L={good:['call','● Strong'],ok:['amb','● OK'],bad:['put','● Caution']}[lvl];
 return `<span class="chip ${L[0]}" title="${esc(why.join(' · '))}">${L[1]}</span>`}
// ---- my trades ----
const usd=v=>(v<0?'−':'+')+'$'+fmt(Math.abs(v),0);
const TS={take:['acc','💰 TAKE PROFIT NOW!'],hold:['call','✋ HOLD ON'],cut:['put','✂ CUT LOSSES']};
const tcall=t=>{const v=t.verdict;if(v)return v.call==='cut'?'cut':v.call==='take'?'take':'hold';return t.signal==='target'?'take':'hold'};
const tchip=t=>{const k=tcall(t),S=TS[k],v=t.verdict;return `<span class="chip ${S[0]} big" title="${esc(v?v.headline:(t.valuing?'Working out the odds…':'Based on your 50% target'))}">${S[1]}</span>`};
let TR={list:[],asof:''};
async function tload(){try{const j=await (await fetch('/api/trades')).json();TR.list=j.trades||[];TR.res=j.results||null;TR.asof=j.asof||'';TR._t=Date.now();
 const el=$('#trlist'),sig=JSON.stringify([TR.list,TR.res&&TR.res.total]),typing=!!el&&el.contains(document.activeElement)&&/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName);
 if(sig!==TR.sig&&!typing){TR.sig=sig;const op=el?[...el.querySelectorAll('details')].map(d=>d.open):[];trender();
  if(el){const ds=[...el.querySelectorAll('details')];if(ds.length===op.length)ds.forEach((d,i)=>{if(op[i])d.open=true})}}
 if(j.valuing||(TR.list||[]).some(t=>t.valuing))setTimeout(tload,8000)}catch(e){$('#trnote').textContent='· could not load trades: '+e.message}}
async function tsave(p){try{const j=await post('/api/trades',p);TR.list=j.trades||[];TR.res=j.results||TR.res;trender();if(PK.d){PK.d.my_results=TR.res;try{pkRender()}catch(_){}}return true}catch(e){TR.err=e.message;$('#trmsg').textContent=e.message;return false}}
function tbar(t){const p=t.profit_pct||0,sch=t.sched_pct;
 if(p<0)return `<div class="pbar"><div class="pfill neg" style="width:${Math.min(100,-p)}%"></div><span class="plab">${fmt(p,0)}% (loss)</span></div>`;
 return `<div class="pbar"><div class="pfill ${p>=50?'done':''}" style="width:${Math.min(100,p/50*100)}%"></div>${sch!=null&&sch>0&&sch<50?`<div class="psch" style="left:${sch/50*100}%" title="where time decay alone would have it today"></div>`:''}<span class="plab">${fmt(p,0)}% of max profit · target 50%</span></div>`}
function trender(){const el=$('#trlist');if(!el)return;const L=TR.list;const CB=closedBlock();
 const tot=L.reduce((a,t)=>a+(t.pl||0),0),cnt=k=>L.filter(t=>tcall(t)==k).length;
 $('#trnote').innerHTML=(L.length?`· ${L.length} open · P/L <span class="${tot<0?'put':'call'}">${usd(tot)}</span>`:'· none open')+(TR.res&&TR.res.total.n?` · closed ${TR.res.total.won}/${TR.res.total.n} won <span class="${TR.res.total.pnl<0?'put':'call'}">${usd(TR.res.total.pnl)}</span>`:'')+(L.length?''+['take','cut','hold'].filter(k=>cnt(k)).map(k=>` · <span class="${TS[k][0]}">${cnt(k)} ${TS[k][1].replace(/^\S+ /,'').toLowerCase()}</span>`).join(''):'');
 if(!L.length){el.innerHTML='<p class="mute">Nothing open. Press “+ I took it” on any recommended trade (Picks, Best use of capital, Sell puts, bull call spreads) or “+ Track” on a spread setup.</p>'+CB;return}
 const order={danger:0,target:1,watch:2,ok:3};
 el.innerHTML=L.slice().sort((a,b)=>(order[a.signal]??4)-(order[b.signal]??4)).map(t=>{
  if(t.legs)return genericCard(t);
  const head=`<b>${esc(t.ticker)}</b> Put credit spread${dirTag('PCS')} ${fmtK(t.short)}/${fmtK(t.long)}P · ${dshort(t.exp)} · ×${t.qty}`;
  if(t.error)return `<div class="trade"><div class="th">${head} <span class="chip put">ERROR</span> <button class="ghost" style="font-size:12px;padding:3px 9px" onclick="tcOpen('${t.id}')">Close trade</button><button class="x" onclick="if(confirm('Remove this trade?'))tsave({remove:'${t.id}'})" title="Remove">×</button></div><p class="mute">${esc(t.error)}</p></div>`;
  const S=TS[tcall(t)];
  const when=t.signal==='target'?'<b class="call">at target now</b>':t.d50_date?`est. <b>${dshort(t.d50_date)}</b> (${fmt(t.p50,0)}% odds, likely ${dshort(t.d50_lo_date)}–${dshort(t.d50_hi_date)})`:t.p50!=null?`${fmt(t.p50,0)}% odds before expiration`:'';
  const sched=t.sched_pct==null?'':t.profit_pct>=t.sched_pct+5?`<span class="call">ahead of schedule</span> (decay alone: ${fmt(t.sched_pct,0)}%)`:t.profit_pct<t.sched_pct-15?`<span class="put">behind schedule</span> (decay alone: ${fmt(t.sched_pct,0)}%)`:`on schedule (decay alone: ${fmt(t.sched_pct,0)}%)`;
  let h=`<div class="trade b-${S[0]}"><div class="th">${tchip(t)} ${head} <span class="mute">${t.dte} DTE · opened ${dshort(t.entry_date)} @ ${fmt(t.credit)}${t.src?' · '+esc(t.src):''}</span><button class="ghost" style="font-size:12px;padding:3px 9px;margin-left:auto" onclick="tcOpen('${t.id}')">Close trade</button><button class="x" onclick="if(confirm('Remove this trade?'))tsave({remove:'${t.id}'})" title="Remove">×</button></div>
  ${tbar(t)}
  <div class="tgrid"><div><span class="l">P/L now</span><b class="${t.pl<0?'put':'call'}">${usd(t.pl)}</b> <span class="mute">of $${fmt(t.max_profit,0)} max</span></div>
  <div><span class="l">Buy back now</span><b>${fmt(t.mid)}</b> <span class="mute">mid · ${fmt(t.close_nat)} nat</span></div>
  <div><span class="l">50% target</span><b>${fmt(t.tp50)}</b> <span class="mute">30%: ${fmt(t.tp30)}</span></div>
  <div><span class="l">When 50%</span>${when||'–'}</div>
  <div><span class="l">${esc(t.ticker)}</span><b>${fmt(t.price)}</b> <span class="${t.cushion<3?'put':'mute'}">${t.cushion>=0?fmt(t.cushion,1)+'% above short':fmt(-t.cushion,1)+'% BELOW short'}</span></div>
  <div><span class="l">Risk</span>short Δ <b class="${t.sdelta<=-0.30?'amb':''}">${fmt(t.sdelta)}</b> · POP ${t.pop_now==null?'–':fmt(t.pop_now,0)+'%'} · BE ${fmt(t.be)}</div>
  <div><span class="l">Put wall (this exp.)</span>${t.put_wall==null?'–':fmtK(t.put_wall)} ${t.put_wall!=null?(t.price>=t.put_wall?'<span class="call">holding above</span>':'<span class="put">below it</span>'):''} · net GEX <span class="${t.net_gex<0?'put':'call'}">${money(t.net_gex)}</span></div>
  <div><span class="l">Schedule</span>${sched||'<span class="mute">add entry price to compare</span>'}</div></div>
  <ul class="acts">${(t.actions||[]).map(a=>`<li>${esc(a)}</li>`).join('')}</ul>`+verdictHtml(t);
  if(t.rolls&&t.rolls.length)h+=`<div class="tw"><table><tr><th>Roll idea</th><th>To</th><th>Net credit (mid / nat)</th><th>New BE</th><th>POP</th><th>Δ</th><th>Wall</th></tr>`+t.rolls.map(x=>`<tr><td>${esc(x.why)}</td><td>${fmtK(x.short)}/${fmtK(x.long)}P ${dshort(x.exp)} <span class="mute">${x.dte}d</span></td><td><b>+${fmt(x.net)}</b> <span class="mute">/ ${x.net_nat>=0?'+':''}${fmt(x.net_nat)}</span></td><td>${fmt(x.be)}</td><td>${fmt(x.pop,0)}%</td><td>${fmt(x.sdelta)}</td><td>${x.below_wall?'<span class="call">≤ wall</span>':'above'}</td></tr>`).join('')+`</table></div><p class="mute">Roll = one order: buy back this spread and sell the new one. Net credit is priced at mid; natural is the worst-case fill.</p>`;
  else if(t.rolls)h+=`<p class="mute">No roll to a later expiration brings in a credit right now — closing is the cleaner exit if you want out.</p>`;
  return h+`<div class="mute" style="font-size:12px">option quotes ${esc(hhmm(t.quotes_as_of))} ET (${srcTxt(t.source)}) · price Nasdaq real-time${t.valued_at?' · updated '+esc(t.valued_at):''}</div></div>`}).join('')+CB;}
const todayET=()=>{try{return new Date().toLocaleDateString('en-CA',{timeZone:'America/New_York'})}catch(e){return new Date().toISOString().slice(0,10)}};
const TRKQ=[];let TRK={o:null,c:null};
function trkBtn(o){TRKQ.push(o);return `<button type="button" class="ghost" style="font-size:12px;padding:3px 9px" onclick="trkOpen(${TRKQ.length-1});return false" title="Record that you took this trade — it then counts in How the picks have done">+ I took it</button>`}
function trkOpen(i){const o=TRKQ[i];if(!o)return;TRK.o=o;
 $('#tkdesc').innerHTML=`<b>${esc(o.tk)}</b> · ${esc(o.what||o.legs_text||'')}${o.exp?' · expires '+dshort(o.exp):''}<br><span class="mute">${esc(o.src||'')}${o.rec_p!=null?' · chance of profit when recommended '+fmt(o.rec_p*100,0)+'%':''}</span>`;
 $('#tkpl').textContent=o.kind==='credit'?'Credit you got (per share)':'Price you paid (per share)';$('#tkql').textContent=o.shares?'Shares':'Contracts';
 $('#tkp').value=(+o.price).toFixed(2);$('#tkq').value=o.qty||1;$('#tkd').value=todayET();$('#tkmsg').textContent='';$('#tkdlg').hidden=false;$('#tkp').focus()}
$('#tksave').onclick=async()=>{const o=TRK.o,b=$('#tksave');if(!o||b.disabled)return;$('#tkmsg').textContent='';b.disabled=true;const b0=b.textContent;b.textContent='Saving…';
 const ok=await tsave({add_legs:{ticker:o.tk,exp:o.exp||'',strat:o.strat,legs:o.legs||null,legs_text:o.legs_text||'',kind:o.kind,price:+$('#tkp').value,qty:+$('#tkq').value,entry_date:$('#tkd').value||null,src:o.src,rec_p:o.rec_p??null,capital:o.capital??null}});
 b.disabled=false;b.textContent=b0;
 if(ok){$('#tkdlg').hidden=true;toast('Saved ✓ — '+o.tk+' is in My trades (values update in a few seconds)');setTimeout(tload,6000);setTimeout(tload,20000)}else $('#tkmsg').textContent=TR.err||'could not save'};
function toast(m){let t=$('#toast');if(!t){t=document.createElement('div');t.id='toast';t.className='toast';document.body.appendChild(t)}t.textContent=m;t.classList.add('on');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('on'),4000)}
$('#tkdlg').onclick=e=>{if(e.target.id==='tkdlg')$('#tkdlg').hidden=true};$('#tcdlg').onclick=e=>{if(e.target.id==='tcdlg')$('#tcdlg').hidden=true};
function legsTxt(t){if(!t.legs)return `${fmtK(t.short)}/${fmtK(t.long)}P`;return t.legs.map(l=>l.cp==='STK'?'shares':`${l.side<0?'−':'+'}${fmtK(l.K)}${l.cp}`).join(' / ')}
function tcOpen(id){const t=TR.list.find(x=>x.id===id);if(!t)return;TRK.c=t;const cr=t.legs?t.kind==='credit':true;
 $('#tcdesc').innerHTML=`<b>${esc(t.ticker)}</b> ${esc(legsTxt(t))} ${t.exp?'· '+dshort(t.exp):''} · ×${t.qty} · opened ${dshort(t.entry_date)} @ ${fmt(t.legs?t.fill:t.credit)}`;
 $('#tcpl').textContent=cr?'Price you paid to buy it back (per share)':'Price you sold it for (per share)';
 const now=t.legs?t.close_now:t.mid;$('#tcp').value=now!=null?(+now).toFixed(2):'';$('#tcd').value=todayET();
 $('#tchint').textContent=cr?'Buying back below your credit = a win; above it = a loss. Enter 0 if it expired worthless.':'Selling above what you paid = a win.';$('#tcmsg').textContent='';$('#tcdlg').hidden=false;$('#tcp').focus()}
function trlOpen(id){const t=TR.list.find(x=>x.id===id);if(!t||!t.legs)return;TRK.r=t;const cr=t.kind==='credit';
 $('#trldesc').innerHTML=`<b>${esc(t.ticker)}</b> ${esc(STRATN[t.strat]||t.strat||'')}${dirTag(t.strat)} ${esc(legsTxt(t))} · ${dshort(t.exp)} · ×${t.qty} · opened ${dshort(t.entry_date)} @ ${fmt(t.fill)} ${t.kind}`;
 $('#trlcl').textContent=cr?'Paid to buy back the old one (per share)':'Sold the old one for (per share)';$('#trlpl').textContent=cr?'New credit (per share)':'New debit (per share)';
 $('#trlc').value=t.close_now!=null?(+t.close_now).toFixed(2):'';$('#trld').value=todayET();$('#trle').value='';$('#trlp').value='';$('#trlq').value=t.qty;
 $('#trlk').value=t.legs.filter(l=>l.cp!=='STK').map(l=>fmtK(l.K)).join('/');
 const o=TR.list.filter(x=>x.id!==t.id&&x.ticker===t.ticker&&x.legs&&x.status!=='closed');
 $('#trll').innerHTML='<option value="">No — enter it here</option>'+o.map(x=>`<option value="${x.id}">${esc(legsTxt(x))} · ${esc(x.exp||'')} · ×${x.qty} @ ${fmt(x.fill)} (opened ${esc(x.entry_date||'')})</option>`).join('');
 $('#trll').value=o.length===1&&o[0].exp>t.exp?o[0].id:'';$('#trlnew').hidden=!!$('#trll').value;
 $('#trlhint').textContent='Records the old trade as closed (rolled) at the buy-back price and links the new one, so My trades shows the roll and the combined P/L. Strikes in the same order as the old ones (sell first).';$('#trlmsg').textContent='';$('#trldlg').hidden=false;$('#trlc').focus()}
$('#trll').onchange=()=>{$('#trlnew').hidden=!!$('#trll').value};
$('#trlsave').onclick=async()=>{const t=TRK.r;if(!t)return;const b=$('#trlsave');const r={id:t.id,close_price:+$('#trlc').value,date:$('#trld').value||null};
 if($('#trlc').value===''||!(+$('#trlc').value>=0)){$('#trlmsg').textContent='enter the buy-back price';return}
 if($('#trll').value)r.link=$('#trll').value;else{const K=$('#trlk').value.split(/[\/ ,]+/).filter(Boolean).map(Number),L=t.legs.filter(l=>l.cp!=='STK');
  if(K.length!==L.length||K.some(k=>!(k>0))){$('#trlmsg').textContent='enter '+L.length+' strikes like '+L.map(l=>fmtK(l.K)).join('/');return}
  if(!$('#trle').value||!(+$('#trlp').value>0)){$('#trlmsg').textContent='enter the new expiration and price';return}
  r.new={exp:$('#trle').value,legs:L.map((l,i)=>({...l,K:K[i]})),price:+$('#trlp').value,qty:+$('#trlq').value||t.qty,capital:null}}
 b.disabled=true;b.textContent='Saving…';const ok=await tsave({roll:r});b.disabled=false;b.textContent='Save roll';
 if(ok){$('#trldlg').hidden=true;toast('Saved ✓ — roll recorded');setTimeout(tload,6000)}else $('#trlmsg').textContent=TR.err||'could not save'};
$('#trldlg').onclick=e=>{if(e.target.id==='trldlg')$('#trldlg').hidden=true};
$('#tcsave').onclick=async()=>{const t=TRK.c;if(!t)return;const ok=await tsave({close:{id:t.id,price:+$('#tcp').value,date:$('#tcd').value||null}});if(ok)$('#tcdlg').hidden=true;else $('#tcmsg').textContent=TR.err||'could not save'};
function closedBlock(){const R=TR.res;if(!R||!R.trades||!R.trades.length)return'';const T=R.total;
 return `<details class="ptab"><summary><b>Closed trades</b> <span class="mute">(${T.n})</span> · <b class="call">✓ ${T.won} won</b> · <b class="put">✗ ${T.lost} lost</b> · P/L <b class="${T.pnl<0?'put':'call'}">${usd(T.pnl)}</b></summary>${myTradesTable(R.trades,true)}</details>`}
function myTradesTable(L,actions){return '<div class="tw"><table><tr><th>Closed</th><th>Ticker</th><th>Trade</th><th>From</th><th>Opened @</th><th>Closed @</th><th>Result</th><th>P/L</th>'+(actions?'<th></th>':'')+'</tr>'+
 L.map(t=>`<tr><td>${esc(t.close_date||'')}<br><span class="mute">${t.closed_how==='expired'?'expired'+(t.settle_price!=null?' (stock '+fmt(t.settle_price)+')':''):t.closed_how==='rolled'?'rolled → '+esc(t.rolled_to_txt||''):'you closed'}</span></td><td><b>${esc(t.ticker)}</b></td><td style="text-align:left;white-space:nowrap">${esc(STRATN[t.strat]||t.strat||'PCS')}<br><span class="mute">${esc(legsTxt(t))}${t.exp?' · '+esc(t.exp):''} · ×${t.qty}</span></td>
 <td class="mute" style="text-align:left">${esc(t.src||'added by hand')}</td><td>${fmt(t.legs?t.fill:t.credit)}<br><span class="mute">${(t.legs?t.kind:'credit')==='credit'?'credit':'debit'} · ${esc(t.entry_date||'')}</span></td>
 <td>${t.close_value==null?'–':fmt(Math.abs(t.close_value))}</td><td class="${t.win?'call':'put'}"><b>${t.win?'✓ Won':'✗ Lost'}</b></td>
 <td class="${(t.pnl||0)>=0?'call':'put'}"><b>${usd(t.pnl||0)}</b>${t.pnl_pct!=null?` <span class="mute">(${t.pnl_pct>=0?'+':''}${fmt(t.pnl_pct,0)}%)</span>`:''}</td>
 ${actions?`<td><button class="ghost" style="font-size:11.5px;padding:2px 7px" onclick="tsave({reopen:'${t.id}'})" title="Undo the close">reopen</button> <button class="x" onclick="if(confirm('Delete this trade from your record?'))tsave({remove:'${t.id}'})" title="Delete">×</button></td>`:''}</tr>`).join('')+'</table></div>'}
function rollLine(t){if(!t.rolled_from||!t.rolled_from.length)return'';const C=((TR.res||{}).trades||[]).filter(x=>t.rolled_from.includes(x.id));if(!C.length)return'';
 const real=C.reduce((a,x)=>a+(x.pnl||0),0),tot=t.pl!=null&&!t.valuing?real+t.pl:null;
 return `<div class="mute" style="font-size:12.5px">↻ rolled from ${C.map(x=>esc(legsTxt(x))+' '+dshort(x.exp)+' ×'+x.qty).join(' + ')} · realized on the old ${C.length>1?'trades':'trade'} <b class="${real<0?'put':'call'}">${usd(real)}</b>${tot!=null?` · combined P/L now <b class="${tot<0?'put':'call'}">${usd(tot)}</b>`:''}</div>`}
function verdictHtml(t){const v=t.verdict;if(!v||(v.losing===false&&v.call!=='take'))return'';
 const C={cut:['put','✂ CUT LOSSES'],salvage:['amb','✋ HOLD ON · can be salvaged'],hold:['call','✋ HOLD ON'],take:['acc','💰 TAKE PROFIT NOW!']}[v.call]||['mute',v.call];
 let h=`<div class="vd vd-${v.call}"><div class="vdh"><span class="vdc ${C[0]}">${C[1]}</span> <b>${esc(v.headline)}</b></div><ul class="acts">${(v.why||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`;
 if(v.plans&&v.plans.length)h+=`<details${v.call==='salvage'?' open':''}><summary><b>How to salvage or recover</b> <span class="mute">(${v.plans.length} option${v.plans.length>1?'s':''}, priced from the live chain)</span></summary>`+v.plans.map(p=>`<div class="vplan"><b>${esc(p.title)}</b>${p.p!=null?` <span class="${p.p>v.p_recover+2?'call':p.p<v.p_recover-2?'put':'mute'}">· ${fmt(p.p,0)}% chance to finish at break-even or better</span> <span class="mute">(as is: ${fmt(v.p_recover,0)}%)</span>`:''}<div>${esc(p.how)}</div>${p.risk?`<div class="mute">Risk: ${esc(p.risk)}</div>`:''}</div>`).join('')+`</details>`;
 return h+`<div class="mute" style="font-size:11.5px">${esc(v.note)}</div></div>`}
function genericCard(t){const head=`<b>${esc(t.ticker)}</b> ${esc(STRATN[t.strat]||t.strat||'')}${dirTag(t.strat)} ${esc(legsTxt(t))}${t.exp?' · '+dshort(t.exp):''} · ×${t.qty}`;
 if(t.error)return `<div class="trade"><div class="th">${head} <span class="chip put">ERROR</span> <button class="ghost" style="font-size:12px;padding:3px 9px" onclick="tcOpen('${t.id}')">Close</button><button class="x" onclick="if(confirm('Remove this trade?'))tsave({remove:'${t.id}'})" title="Remove">×</button></div><p class="mute">${esc(t.error)}</p></div>`;
 return `<div class="trade b-${TS[tcall(t)][0]}"><div class="th">${tchip(t)} ${head} <span class="mute">${t.dte!=null?t.dte+' DTE · ':''}opened ${dshort(t.entry_date)} @ ${fmt(t.fill)} ${t.kind}${t.src?' · '+esc(t.src):''}</span>
 <button class="ghost" style="font-size:12px;padding:3px 9px;margin-left:auto" onclick="tcOpen('${t.id}')">Close trade</button>${t.legs.some(l=>l.cp!=='STK')?`<button class="ghost" style="font-size:12px;padding:3px 9px" onclick="trlOpen('${t.id}')">Roll</button>`:''}<button class="x" onclick="if(confirm('Remove this trade?'))tsave({remove:'${t.id}'})" title="Remove">×</button></div>
 ${rollLine(t)}<div class="tgrid"><div><span class="l">P/L now</span>${t.valuing?'<b class="mute">valuing…</b>':`<b class="${t.pl<0?'put':'call'}">${usd(t.pl)}</b>`}</div><div><span class="l">${t.kind==='credit'?'Buy back now':'Worth now'}</span><b>${fmt(t.close_now)}</b> <span class="mute">per share (mid)</span></div>
 <div><span class="l">${esc(t.ticker)}</span><b>${fmt(t.price)}</b></div></div>${verdictHtml(t)}
 <div class="mute" style="font-size:12px">${t.quotes_as_of?'option quotes '+esc(hhmm(t.quotes_as_of))+' ET ('+srcTxt(t.source)+') · ':''}price Nasdaq${t.valued_at?' · updated '+esc(t.valued_at):''}</div></div>`}
function prefillTrade(tk,exp,sh,lg,cr,src,pop){const f=$('#tf');f.dataset.src=src||'Walls & spreads';f.dataset.pop=pop==null?'':pop;f.st.value='PCS';tfShape();f.tk.value=tk;f.exp.value=exp;f.sh.value=sh;f.lg.value=lg;f.cr.value=(+cr).toFixed(2);f.qty.value=f.qty.value||1;
 try{f.ed.value=new Date().toLocaleDateString('en-CA',{timeZone:'America/New_York'})}catch(e){}
 $('#trk').open=true;$('#trmsg').textContent='Check the credit you actually got filled at, then press Add.';$('#trk').scrollIntoView({block:'start'})}
function gexChg(now,then){if(now==null||then==null)return'<span class="mute">–</span>';const d=now-then,p=then?d/Math.abs(then)*100:null;
 return `<span class="${d<0?'put':'call'}">${d>=0?'+':'−'}${money(Math.abs(d))}${p==null||!isFinite(p)||Math.abs(then)<1?'':` (${p>=0?'+':''}${fmt(p,0)}%)`}</span>`}
function wallMove(now,then,lab){return then==null||now==null||then===now?'':` <span class="amb">${lab} ${fmtK(then)}→${fmtK(now)}</span>`}
function gexLine(r,e){const G=r.gex_hist;if(!G)return'<div class="l mute">GEX change: collecting (first reading now)</div>';
 const k=e?e.exp:null,cur=e?(G.cur.exps||{})[k]:[G.cur.net],v=s=>s?(e?((s.exps||{})[k]||[])[0]:s.net):null;
 const closed=r.live&&r.live.status&&r.live.status.toLowerCase()!=='open',base=G.prior_close||G.first,bl=G.prior_close?(closed?'<b>full session</b> (close vs prior close ':'session so far (vs prior close ')+dshort(G.prior_close.ts.slice(0,10))+')':'since '+hhmm(G.first&&G.first.ts)+' ET (no prior-session reading yet)';
 const nowv=e?e.net_gex:G.cur.net;
 let t=`<div class="l">15-min: ${G.prev?gexChg(nowv,v(G.prev))+' <span class="mute">vs '+hhmm(G.prev.ts)+'</span>':'<span class="mute">needs a reading 10+ min older</span>'}</div>`;
 t+=`<div class="l">${bl}: ${base?gexChg(nowv,v(base)):'<span class="mute">–</span>'}</div>`;
 if(e){const pv=G.prev&&(G.prev.exps||{})[k],bv=base&&(base.exps||{})[k];
  const mv=(pv?wallMove(e.put_wall,pv[1],'PW')+wallMove(e.call_wall,pv[2],'CW'):'')||(bv?wallMove(e.put_wall,bv[1],'PW')+wallMove(e.call_wall,bv[2],'CW'):'');if(mv)t+=`<div class="l">walls moved:${mv}</div>`}
 return t}
function weekBlock(r){const W=r.week_exps||[],have=W.filter(e=>!e.missing);if(!have.length)return'';const s=px(r),ow=($('#out').clientWidth||900)-30,cw=ow>=800?Math.max(360,Math.round((ow-14)/2)):null;
 const one=e=>e.missing?`<div class="wk"><h3>${e.label}</h3><p class="mute">${esc(r.ticker)} has no ${e.label} expiration in the next 7 days.</p></div>`:
  `<div class="wk"><h3>${e.label} ${dshort(e.exp)} <span class="mute" style="font-weight:400">· ${e.dte===0?'expires today':e.dte+' DTE'}</span></h3>
  <div class="kpis"><div class="kpi"><div class="l">Put wall</div><div class="v put">${e.put_wall==null?'none':fmtK(e.put_wall)+' <span class="mute" style="font-size:12px">('+pct(e.put_wall,s)+'%)</span>'}</div></div>
  <div class="kpi"><div class="l">Call wall</div><div class="v call">${e.call_wall==null?'none':fmtK(e.call_wall)+' <span class="mute" style="font-size:12px">('+(pct(e.call_wall,s)>0?'+':'')+pct(e.call_wall,s)+'%)</span>'}</div></div>
  <div class="kpi"><div class="l">Net GEX</div><div class="v ${e.net_gex<0?'put':'call'}">${money(e.net_gex)}</div><div class="l">${e.net_gex>=0?'dampening / range':'amplifying / trend'}</div>${gexLine(r,e)}</div></div>
  ${e.put_oi+e.call_oi<500?'<p class="put" style="font-size:13px">Thin open interest — walls unreliable.</p>':''}${chart(e,s,cw,0.06)}</div>`;
 return `<div class="card"><h2>This week: Wednesday &amp; Friday GEX</h2><div class="wkgrid">${W.map(one).join('')}</div></div>`}


// ---- dealer positioning: gamma flip, vanna, charm, sign-convention check ----
function earnBlock(r){const E=r.earn_iv;if(!E)return'';const f1=v=>v==null?'–':fmt(v,0)+'%';
 const lines=[`Reports <strong>${esc(E.date)}</strong> (${E.days} day${E.days==1?'':'s'}) — any trade expiring after that date holds through the report.`];
 if(E.post_iv!=null)lines.push(`ATM IV: <strong>${f1(E.pre_iv??E.base_iv_from_term)}</strong> ${E.pre_iv!=null?`before (${esc(E.pre_exp)})`:'without the event (from term structure)'} → <strong>${f1(E.post_iv)}</strong> first expiration after (${esc(E.post_exp)})${E.crush_pts!=null?` · about ${fmt(E.crush_pts,0)} pts of IV likely to drop out after the report`:''}`);
 if(E.move_pct!=null)lines.push(`Options imply an earnings-day move of about <strong>±${fmt(E.move_pct,1)}%</strong> (1 sd, ±$${fmt(px(r)*E.move_pct/100)})`);
 if(E.rv30!=null)lines.push(`IV vs the last 30 days' actual moves: ${f1(E.post_iv??E.iv30)} vs ${f1(E.rv30)}${E.iv_rv!=null?` (<strong>${fmt(E.iv_rv,1)}×</strong>)`:''}`);
 const head=E.rich?'⚠ Earnings: options look rich — IV crush likely':'Earnings before some expirations';
 return `<div class="al ${E.rich?'warn':''}" style="margin:8px 0"><b>${head}</b>${lines.join('<br>')}<br><span class="mute">Sellers collect the extra premium but take the gap risk; buyers of calls/puts through the report can lose even if right on direction. Estimates from the CBOE/Schwab chain.</span></div>`}
function dealerBlock(r){const D=r.dealer;if(!D||D.error||D.gex==null)return'';const s=px(r),tk=esc(r.ticker);
 const flow=v=>`${v>=0?'buy':'sell'} ~${money(Math.abs(v)).replace('-','')}`;
 const reg=D.gex>=0?'<span class="call">positive gamma — dealers dampen moves (buy dips, sell rips)</span>':'<span class="put">negative gamma — dealers amplify moves (sell into drops, buy into rallies)</span>';
 const fl=D.flip==null?`<span class="mute">no flip within ±15% — the regime holds across that whole range</span>`:`<span class="mute">${D.flip_pct>=0?'+':''}${fmt(D.flip_pct,1)}% from now · ${s>D.flip?`above the flip: a drop below $${fmt(D.flip)} would turn dealers into amplifiers`:`below the flip: a rally above $${fmt(D.flip)} would turn dealers into stabilizers`}</span>`;
 const tile=(l,v,c,sub)=>`<div class="gt b-${c}"><div class="l">${l}</div><div class="v">${v}</div>${sub?`<div class="l">${sub}</div>`:''}</div>`;
 // net GEX vs price curve
 const C=D.curve||[],W=Math.min(620,Math.max(300,($('#out').clientWidth||700)-40)),Hh=150,pl=56,pr=10,pt=10,pb=22;
 let svg='';if(C.length>3){const xs=C.map(c=>c[0]),ys=C.map(c=>c[1]),x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(0,...ys),y1=Math.max(0,...ys);
  const X=x=>pl+(x-x0)/(x1-x0)*(W-pl-pr),Y=y=>pt+(y1-y)/((y1-y0)||1)*(Hh-pt-pb),line=C.map((c,i)=>`${i?'L':'M'}${X(c[0]).toFixed(1)},${Y(c[1]).toFixed(1)}`).join('');
  svg=`<svg viewBox="0 0 ${W} ${Hh}" style="max-width:${W}px"><line x1="${pl}" x2="${W-pr}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--mute)"/>
   <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="2"/><line x1="${X(s)}" x2="${X(s)}" y1="${pt}" y2="${Hh-pb}" stroke="var(--ink)" stroke-dasharray="3 3"/>
   <text x="${X(s)+3}" y="${pt+9}" font-size="10.5" fill="var(--mute)">now ${fmtP(s)}</text>${D.flip?`<circle cx="${X(D.flip)}" cy="${Y(0)}" r="4.5" fill="var(--amber)"/><text x="${X(D.flip)}" y="${Y(0)-7}" text-anchor="middle" font-size="10.5" fill="var(--amber)">flip ${fmtP(D.flip)}</text>`:''}
   <text x="${pl-5}" y="${Y(y1)+4}" text-anchor="end" font-size="10" fill="var(--mute)">${money(y1)}</text><text x="${pl-5}" y="${Y(y0)}" text-anchor="end" font-size="10" fill="var(--mute)">${money(y0)}</text>
   <text x="${pl}" y="${Hh-6}" font-size="10" fill="var(--mute)">${fmtP(x0)}</text><text x="${W-pr}" y="${Hh-6}" text-anchor="end" font-size="10" fill="var(--mute)">${fmtP(x1)}</text></svg>
   <div class="legend mute" style="font-size:12px"><span>net GEX if ${tk} were at each price (today’s open interest)</span></div>`}
 const rows=(D.by_exp||[]).slice(0,6).map(e=>`<tr><td>${dshort(e.exp)}</td><td class="${e.gex<0?'put':'call'}">${money(e.gex)}</td><td>${flow(e.vex)}</td><td>${flow(-e.cex)}</td></tr>`).join('');
 return `<div class="card"><h2>Dealer positioning <span class="mute" style="font-size:13px;font-weight:400">· gamma, vanna &amp; charm from the CBOE chain (Black-Scholes at today’s price)</span></h2>
  <div class="gtiles">${tile('Net gamma (GEX)',money(D.gex),D.gex>=0?'call':'put',reg)}${tile('Gamma flip level',D.flip==null?'none nearby':'$'+fmt(D.flip),'amb',fl)}
  ${tile('Vanna: if implied volatility drops 1 pt',`dealers ${flow(D.vex)}`,'acc','of '+tk+' to stay hedged (rises 1 pt: the opposite)')}
  ${tile('Charm: each day that passes',`dealers ${flow(-D.cex)}`,'fib','of '+tk+' as time decay shifts their hedges')}
  ${tile('Convention check',D.robust?'regime holds':'depends on assumption',D.robust?'call':'amb',D.robust?'naive and “dealers short everything” views agree on the sign':'the GEX sign flips if dealers are short all options — treat the regime as uncertain')}
  ${D.size&&D.size.pct_1pct!=null?tile('Hedging vs trading volume',fmt(D.size.pct_1pct,1)+'% per 1% move','acc',(D.size.pct_day!=null?`${fmt(D.size.pct_day,1)}% over a typical day’s move (±${fmt(D.size.atr_pct,1)}%) · `:'')+`of ${tk}’s ~${money(D.size.adv20)}/day traded (20-day avg). Bigger share = walls and the flip matter more`):''}</div>
  ${svg}<div class="tw"><table><tr><th>Expiration</th><th>Net GEX</th><th title="Hedge flow if implied volatility drops 1 point">If IV −1 pt</th><th title="Hedge flow per day from time decay">Per day (charm)</th></tr>${rows}</table></div>
  <p class="mute">How to read it: dealers (market makers) hedge the options they hold. <b>Gamma</b> sets whether their hedging damps or amplifies moves; the <b>flip</b> is the price where net gamma changes sign. <b>Vanna</b> is how their hedge changes when implied volatility moves — after events, IV usually falls, so a “buy” here supports the price (a “vanna rally”). <b>Charm</b> is how hedges shift as time passes — big into monthly expiration. Positioning assumes dealers are long calls and short puts (the common “naive” view); the convention check reruns it assuming dealers are short all options. Open interest updates once a day. Not financial advice.</p></div>`}
// ---- week-by-week GEX ----
function weeksBlock(r){const W=r.weeks||[];
 if(!r.has_weeklies)return `<div class="card"><h2>Week-by-week GEX</h2><p class="mute">${esc(r.ticker)} has no weekly options (monthly expirations only), so there is no week-by-week view. The expiration cards below cover its monthlies.</p></div>`;
 if(!W.length)return'';const s=px(r),nE=r.expirations.length,GL=gexList(r);
 const ow=Math.min(1060,Math.max(320,($('#out').clientWidth||900)-40)),H=230,pl=56,pr=14,pt=14,pb=34,iw=ow-pl-pr,ih=H-pt-pb,n=W.length;
 let lo=s,hi=s;W.forEach(w=>{for(const v of [w.put_wall,w.call_wall,w.em!=null?s-w.em:null,w.em!=null?s+w.em:null])if(v!=null){lo=Math.min(lo,v);hi=Math.max(hi,v)}});
 const pad=(hi-lo)*0.08||s*0.02;lo-=pad;hi+=pad;const X=i=>pl+(n===1?iw/2:iw*i/(n-1)),Y=v=>pt+(hi-v)/(hi-lo)*ih;
 const band=W.map((w,i)=>w.em!=null?[X(i),Y(s+w.em),Y(s-w.em)]:null);
 const up=band.filter(Boolean).map(b=>`${b[0].toFixed(1)},${b[1].toFixed(1)}`),dn=band.filter(Boolean).reverse().map(b=>`${b[0].toFixed(1)},${b[2].toFixed(1)}`);
 const path=k=>{let d='',pen=false;W.forEach((w,i)=>{if(w[k]==null){pen=false;return}d+=`${pen?'L':'M'}${X(i).toFixed(1)},${Y(w[k]).toFixed(1)}`;pen=true});return d};
 const mx=Math.max(...W.map(w=>Math.abs(w.net_gex)),1);
 let g='';for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;g+=`<line x1="${pl}" x2="${ow-pr}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${pl-6}" y="${Y(v)+4}" text-anchor="end" font-size="11" fill="var(--mute)">${fmtP(v)}</text>`}
 const dots=W.map((w,i)=>`${w.put_wall!=null?`<circle cx="${X(i)}" cy="${Y(w.put_wall)}" r="4.5" fill="var(--put)"><title>${dshort(w.exp)} put wall ${w.put_wall}</title></circle>`:''}${w.call_wall!=null?`<circle cx="${X(i)}" cy="${Y(w.call_wall)}" r="4.5" fill="var(--call)"><title>${dshort(w.exp)} call wall ${w.call_wall}</title></circle>`:''}
  <rect x="${X(i)-7}" y="${H-pb+6}" width="14" height="${Math.max(2,Math.abs(w.net_gex)/mx*12)}" fill="${w.net_gex<0?'var(--put)':'var(--call)'}" opacity=".7"><title>net GEX ${money(w.net_gex)}</title></rect>
  <text x="${X(i)}" y="${H-4}" text-anchor="middle" font-size="10.5" fill="var(--mute)">${w.exp.slice(5).replace('-','/')}</text>`).join('');
 const svg=`<svg viewBox="0 0 ${ow} ${H}" style="max-width:${ow}px">${g}${up.length>1?`<polygon points="${[...up,...dn].join(' ')}" fill="var(--accent)" opacity=".10"/>`:''}
  <line x1="${pl}" x2="${ow-pr}" y1="${Y(s)}" y2="${Y(s)}" stroke="var(--ink)" stroke-dasharray="4 3" opacity=".7"/><text x="${ow-pr}" y="${Y(s)-4}" text-anchor="end" font-size="11" fill="var(--mute)">now ${fmtP(s)}</text>
  <path d="${path('call_wall')}" fill="none" stroke="var(--call)" stroke-width="2"/><path d="${path('put_wall')}" fill="none" stroke="var(--put)" stroke-width="2"/>${dots}</svg>
  <div class="legend mute" style="font-size:12px"><span><b class="call">●</b> call wall</span><span><b class="put">●</b> put wall</span><span><b style="color:var(--accent)">▮</b> expected move (±1σ) to that week's expiration</span><span>bars: net GEX (green dampens moves, red amplifies)</span></div>`;
 let t='<div class="tw"><table><tr><th>Week</th><th>Put wall</th><th>Call wall</th><th title="Net GEX of that expiration">Net GEX</th><th title="All expirations in that week combined">Week total</th><th>Expected move</th><th>Put / call OI</th><th></th></tr>';
 W.forEach((w,i)=>{const gi=GL.findIndex(e=>e.exp===w.exp),thin=w.put_oi+w.call_oi<500;
  t+=`<tr${thin?' class="mute"':''}><td><b>${dshort(w.exp)}</b> <span class="mute">${w.dte}d</span><br><span class="mute">${w.days.length>1?w.days.join(' · ')+' exps':'1 exp'}${thin?' · thin OI':''}</span></td>
  <td class="put">${w.put_wall==null?'–':fmtK(w.put_wall)+` <span class="mute">${pct(w.put_wall,s)}%</span>`}</td><td class="call">${w.call_wall==null?'–':fmtK(w.call_wall)+` <span class="mute">${pct(w.call_wall,s)>0?'+':''}${pct(w.call_wall,s)}%</span>`}</td>
  <td class="${w.net_gex<0?'put':'call'}">${money(w.net_gex)}</td><td class="${w.week_net_gex<0?'put':'call'}">${money(w.week_net_gex)}</td>
  <td>${w.em==null?'–':'±'+fmt(w.em)+`<br><span class="mute">${fmtP(s-w.em)} – ${fmtP(s+w.em)}</span>`}</td><td>${w.put_oi.toLocaleString()} / ${w.call_oi.toLocaleString()}${w.pc_oi!=null?`<br><span class="mute">P/C ${fmt(w.pc_oi,2)}</span>`:''}</td>
  <td><a href="#" onclick="setExp(${gi});$('#cc').scrollIntoView({behavior:'smooth',block:'center'});return false">chart</a></td></tr>`});
 return `<div class="card"><h2>Week-by-week GEX <span class="mute" style="font-size:13px;font-weight:400">· last expiration of each week, next ${W.length} weeks</span></h2>${svg}${t}</table></div>
  <p class="mute">Walls that hold in the same place week after week are stronger levels; a put wall stepping up over the coming weeks is bullish support, a call wall stepping down caps rallies. Far-out weeks have less open interest, so their walls are weaker until traders build positions. “chart” shows that week's GEX profile on the price chart (or pick it in the chart's “Other weeks” menu). CBOE, 15-min delayed.</p></div>`}
// ---- Fibonacci + at-a-glance ----
function fibCalc(bars){const b=bars.filter(x=>x[2]!=null&&x[3]!=null);if(b.length<10)return null;let hi=0,lo=0;
 b.forEach((x,i)=>{if(x[2]>b[hi][2])hi=i;if(x[3]<b[lo][3])lo=i});const H=b[hi][2],L=b[lo][3];if(!(H>L))return null;const up=lo<hi;
 return{dir:up?'up':'down',hi:H,lo:L,hi_date:b[hi][0],lo_date:b[lo][0],levels:[0,.236,.382,.5,.618,.786,1].map(r=>({r,price:up?H-(H-L)*r:L+(H-L)*r}))}}
const fibName=r=>(r*100).toFixed(r===0||r===1||r===.5?0:1)+'%';
function keyLevels(r){const s=px(r),L=[];
 (r.expirations||[]).forEach(e=>{const tag=e.label=='Weekly'?'wk':e.dte+'d';if(e.put_wall!=null)L.push({k:e.put_wall,t:'Put wall '+tag,c:'put'});if(e.call_wall!=null)L.push({k:e.call_wall,t:'Call wall '+tag,c:'call'})});
 if(r.fib)r.fib.levels.forEach(f=>{if(f.r>0&&f.r<1)L.push({k:f.price,t:'Fib '+fibName(f.r),c:'fib'})});
 if(r.trend){L.push({k:r.trend.sma20,t:'20-day avg',c:'mute'});L.push({k:r.trend.sma50,t:'50-day avg',c:'mute'})}
 const wl=W&&W.config?W.config.watchlist.find(x=>x.ticker==r.ticker):null;if(wl&&wl.strike)L.push({k:wl.strike,t:'Your short strike',c:'amb'});
 // merge identical strikes / tags
 const m={};L.filter(l=>l.k>s*0.75&&l.k<s*1.25).forEach(l=>{const key=(+l.k).toFixed(2);(m[key]=m[key]||{k:+l.k,ts:[],cs:[]});m[key].ts.push(l.t);m[key].cs.push(l.c)});
 const arr=Object.values(m).sort((a,b)=>b.k-a.k);
 arr.forEach(a=>{a.conf=arr.filter(o=>o!==a&&Math.abs(o.k/a.k-1)<0.0075).length>0||a.ts.length>1});
 return{above:arr.filter(a=>a.k>s),below:arr.filter(a=>a.k<=s)}}
function glance(r){const s=px(r),t=r.trend,e30=(r.expirations||[]).find(e=>/30/.test(e.label))||(r.expirations||[])[0],K=keyLevels(r);
 const rsiZ=r.rsi==null?['mute','n/a']:r.rsi<=30?['call','oversold']:r.rsi>=70?['put','overbought']:r.rsi<45?['amb','soft']:['mute','neutral'];
 const ab=t?(s>t.sma20?'above':'below')+' 20d · '+(s>t.sma50?'above':'below')+' 50d':'';
 const tr=!t?['mute','n/a']:s>t.sma20&&s>t.sma50&&t.sma20>t.sma50?['call','strong uptrend']:s>t.sma50?['call','uptrend']:s<t.sma20&&s<t.sma50&&t.sma20<t.sma50?['put','downtrend']:s<t.sma20&&s<t.sma50?['amb','pullback · below both avgs']:['amb','mixed'];
 const gm=!e30?['mute','n/a']:e30.net_gex>=0?['call','positive · pinning']:['put','negative · moves amplified'];
 const sup=K.below[0],res=K.above[K.above.length-1];
 const why=[];let v='amb',vt='Mixed';
 if(t&&t.trend>=2&&e30&&e30.net_gex>=0){v='call';vt='Favorable';why.push('uptrend + positive gamma')}
 else if(t&&t.trend===0&&e30&&e30.net_gex<0){v='put';vt='Unfavorable';why.push('downtrend + negative gamma')}
 else why.push(t&&t.trend>=2?'uptrend but negative gamma':t&&t.trend===0?'downtrend, gamma not negative':'trend mixed');
 if(r.rsi!=null&&r.rsi>=70)why.push('RSI stretched — sell further out');if(r.rsi!=null&&r.rsi<=30)why.push('oversold — bounces common, but trend is down');
 const S=r.strategy||{};if(S.earnings)why.push('earnings '+dshort(S.earnings));
 const tile=(lab,val,sub,c)=>`<div class="gt b-${c}"><div class="l">${lab}</div><div class="v">${val}</div>${sub?`<div class="l">${sub}</div>`:''}</div>`;
 let h=`<div class="card glance"><div class="ghead"><h2>At a glance</h2><span class="chip ${v} big">Put selling: ${vt}</span><span class="mute">${esc(why.join(' · '))}</span></div><div class="gtiles">`;
 h+=tile('RSI(14)',r.rsi==null?'n/a':fmt(r.rsi,1),rsiZ[1],rsiZ[0]);
 h+=tile('Trend',tr[1],t?`${ab} (${fmt(t.sma20)} / ${fmt(t.sma50)})`:'',tr[0]);
 h+=tile('Gamma'+(e30?' ('+e30.exp.slice(5)+')':''),gm[1],e30?money(e30.net_gex):'',gm[0]);
 if(e30&&e30.em)h+=tile('Expected move to '+e30.exp.slice(5),'±'+fmt(e30.em),`${fmt(s-e30.em)} – ${fmt(s+e30.em)} (1σ, ~68%)`,'mute');
 h+=tile('Nearest support',sup?fmt(sup.k):'–',sup?`${esc(sup.ts.join(' + '))} · ${fmt((sup.k/s-1)*100,1)}%${sup.conf?' · ★ confluence':''}`:'','call');
 h+=tile('Nearest resistance',res?fmt(res.k):'–',res?`${esc(res.ts.join(' + '))} · +${fmt((res.k/s-1)*100,1)}%${res.conf?' · ★ confluence':''}`:'','put');
 if(r.fib)h+=tile('Fib swing (6 mo)',`${r.fib.dir==='up'?'↑':'↓'} ${fmt(r.fib.lo)} → ${fmt(r.fib.hi)}`,`${r.fib.dir==='up'?'low '+dshort(r.fib.lo_date)+' → high '+dshort(r.fib.hi_date)+' · levels = support':'high '+dshort(r.fib.hi_date)+' → low '+dshort(r.fib.lo_date)+' · levels = resistance'}`,'fib');
 h+=`</div><details class="ladder"><summary class="mute">Key levels ladder (${K.above.length} above · ${K.below.length} below)</summary><table>`+
  K.above.slice(-6).map(a=>`<tr><td class="put">${fmt(a.k)}</td><td>${esc(a.ts.join(' + '))}${a.conf?' <span class="chip amb">★ confluence</span>':''}</td><td class="mute">+${fmt((a.k/s-1)*100,1)}%</td></tr>`).join('')+
  `<tr class="now"><td><b>${fmt(s)}</b></td><td><b>price now</b></td><td></td></tr>`+
  K.below.slice(0,6).map(a=>`<tr><td class="call">${fmt(a.k)}</td><td>${esc(a.ts.join(' + '))}${a.conf?' <span class="chip amb">★ confluence</span>':''}</td><td class="mute">${fmt((a.k/s-1)*100,1)}%</td></tr>`).join('')+
  `</table><p class="mute">★ confluence = two or more levels within 0.75% of each other — stronger support/resistance. A short put at or below a confluence has more cushion.</p></details></div>`;
 return h}
function levelMap(e,r){const s=px(r),fib=r.fib,pts=[s];if(e.put_wall!=null)pts.push(e.put_wall);if(e.call_wall!=null)pts.push(e.call_wall);if(e.em){pts.push(s-e.em,s+e.em)}
 const fl=fib?fib.levels.filter(f=>f.price>s*0.8&&f.price<s*1.2):[];fl.forEach(f=>pts.push(f.price));
 const wl=W&&W.config?W.config.watchlist.find(x=>x.ticker==r.ticker):null,ks=wl&&wl.strike;if(ks)pts.push(ks);
 let lo=Math.min(...pts),hi=Math.max(...pts);const pad=(hi-lo)*0.06||1;lo-=pad;hi+=pad;
 const Wd=900,H=58,x=v=>12+(v-lo)/(hi-lo)*(Wd-24);let g=`<rect x="12" y="24" width="${Wd-24}" height="10" rx="5" fill="var(--bg)" stroke="var(--line)"/>`;
 if(e.em)g+=`<rect x="${x(s-e.em)}" y="24" width="${x(s+e.em)-x(s-e.em)}" height="10" rx="5" fill="var(--accent)" opacity=".22"><title>1σ expected move ±${fmt(e.em)}</title></rect>`;
 fl.forEach(f=>{g+=`<line x1="${x(f.price)}" x2="${x(f.price)}" y1="20" y2="38" stroke="var(--fib)" stroke-width="1.5"><title>Fib ${fibName(f.r)} ${fmt(f.price)}</title></line>`});
 const mk=(v,c,lab,top)=>`<line x1="${x(v)}" x2="${x(v)}" y1="14" y2="44" stroke="${c}" stroke-width="2.5"/><text x="${x(v)}" y="${top?11:55}" fill="${c}" font-size="12" text-anchor="middle" font-weight="600">${lab}</text>`;
 if(e.put_wall!=null)g+=mk(e.put_wall,'var(--put)','PW '+fmtK(e.put_wall),0);if(e.call_wall!=null)g+=mk(e.call_wall,'var(--call)','CW '+fmtK(e.call_wall),0);
 if(ks)g+=mk(ks,'var(--amber)','yours '+fmtK(ks),1);
 g+=`<circle cx="${x(s)}" cy="29" r="6" fill="var(--ink)"/><text x="${x(s)}" y="11" fill="var(--ink)" font-size="12" text-anchor="middle" font-weight="700">${fmt(s)}</text>`;
 return `<div class="lmap"><svg viewBox="0 0 ${Wd} ${H}" role="img" aria-label="price map">${g}</svg><div class="legend"><span><i class="bx" style="background:var(--put)"></i>put wall</span><span><i class="bx" style="background:var(--call)"></i>call wall</span><span><i class="bx" style="background:var(--accent);opacity:.4"></i>1σ expected move${e.em?' ±'+fmt(e.em):''}</span><span><i class="bx" style="background:var(--fib)"></i>Fib levels (6-mo swing)</span><span><i class="bx" style="background:var(--ink)"></i>price</span></div></div>`}
// ---- technical analysis (plain English) ----
const TA={
 sma:(a,n)=>a.map((_,i)=>i<n-1?null:a.slice(i-n+1,i+1).reduce((x,y)=>x+y,0)/n),
 ema:(a,n)=>{const k=2/(n+1);let e=null;return a.map((v,i)=>{e=e==null?(i>=n-1?a.slice(0,n).reduce((x,y)=>x+y,0)/n:null):v*k+e*(1-k);return i<n-1?null:e})},
 rsi:(a,n=14)=>{let g=0,l=0;const out=a.map(()=>null);for(let i=1;i<a.length;i++){const d=a[i]-a[i-1],up=Math.max(d,0),dn=Math.max(-d,0);
  if(i<=n){g+=up/n;l+=dn/n;if(i===n)out[i]=l===0?100:100-100/(1+g/l)}else{g=(g*(n-1)+up)/n;l=(l*(n-1)+dn)/n;out[i]=l===0?100:100-100/(1+g/l)}}return out},
 bb:(a,n=20,k=2)=>a.map((_,i)=>{if(i<n-1)return null;const w=a.slice(i-n+1,i+1),m=w.reduce((x,y)=>x+y,0)/n,sd=Math.sqrt(w.reduce((x,y)=>x+(y-m)**2,0)/n);return{m,u:m+k*sd,l:m-k*sd,bw:(4*k*sd/2)/m}}),
 avwap:(D,from)=>{let pv=0,vv=0;return D.map((d,i)=>{if(i<from)return null;const tp=(d[2]+d[3]+d[4])/3,v=d[5]||0;pv+=tp*v;vv+=v;return vv?pv/vv:null})}};
const FIBKEY={0.382:'shallow pullback — strong trends often hold here',0.5:'halfway — a normal, healthy pullback',0.618:'the “golden ratio” — the most-watched level; deepest pullback a healthy trend usually takes',0.786:'deep — if this breaks, the whole move is usually failing'};
function candleRead(D){const n=D.length,c=D[n-1],p=D[n-2],out=[];if(!c||!p)return out;
 const body=x=>Math.abs(x[4]-x[1]),rng=x=>x[2]-x[3],up=x=>x[4]>=x[1];
 const avgR=D.slice(-15,-1).reduce((a,x)=>a+rng(x),0)/14;
 if(up(c)&&!up(p)&&c[4]>=p[1]&&c[1]<=p[4]&&body(c)>body(p))out.push(['Bullish engulfing','the last candle swallowed the prior red candle — buyers took control','bull']);
 if(!up(c)&&up(p)&&c[1]>=p[4]&&c[4]<=p[1]&&body(c)>body(p))out.push(['Bearish engulfing','the last candle swallowed the prior green candle — sellers took control','bear']);
 const lw=Math.min(c[1],c[4])-c[3],uw=c[2]-Math.max(c[1],c[4]);
 if(rng(c)>0&&lw>=2*body(c)&&uw<=body(c)*0.6&&lw>rng(c)*0.55)out.push(['Hammer','long lower wick — sellers pushed down but buyers bought it back; bullish if it holds','bull']);
 if(rng(c)>0&&uw>=2*body(c)&&lw<=body(c)*0.6&&uw>rng(c)*0.55)out.push(['Shooting star','long upper wick — buyers pushed up but sellers rejected it; bearish if it holds','bear']);
 if(rng(c)>0&&body(c)<=rng(c)*0.1)out.push(['Doji','open ≈ close — indecision; watch the next candle for direction','neu']);
 if(c[2]<=p[2]&&c[3]>=p[3])out.push(['Inside bar','range inside the prior candle — a pause; a break of '+fmt(p[2])+' or '+fmt(p[3])+' often sets the next move','neu']);
 if(rng(c)>=1.8*avgR)out.push([up(c)?'Wide-range up day':'Wide-range down day','a much bigger move than usual — strong '+(up(c)?'buying':'selling')+' pressure',up(c)?'bull':'bear']);
 return out}
function techRead(D,tf,r){const C=D.map(d=>d[4]),n=C.length,last=C[n-1],rows=[],L=n-1;if(n<60)return null;
 const p=tf==='W'?{a:10,b:20,c:40,an:'10-week',bn:'20-week',cn:'40-week'}:{a:20,b:50,c:200,an:'20-day',bn:'50-day',cn:'200-day'};
 const A=TA.sma(C,p.a),B=TA.sma(C,p.b),Cc=TA.sma(C,p.c),e9=TA.ema(C,9),e21=TA.ema(C,21),R=TA.rsi(C),BB=TA.bb(C);
 // trend
 const ab=v=>v!=null&&last>v,s3=[A[L],B[L],Cc[L]];
 let tr,ts;if(ab(s3[0])&&ab(s3[1])&&(s3[2]==null||ab(s3[2]))&&s3[0]>s3[1]){tr='Price is above all its averages and they are stacked upward — a clean uptrend.';ts='bull'}
 else if(!ab(s3[0])&&!ab(s3[1])&&(s3[2]==null||!ab(s3[2]))&&s3[0]<s3[1]){tr='Price is below all its averages and they point down — a downtrend.';ts='bear'}
 else if(s3[2]!=null&&ab(s3[2])&&!ab(s3[0])){tr=`Above the ${p.cn} average (long-term uptrend intact) but below the ${p.an} — a short-term pullback.`;ts='neu'}
 else if(s3[2]!=null&&!ab(s3[2])&&ab(s3[0])){tr=`Bouncing above the ${p.an} average but still under the ${p.cn} — a rebound inside a longer downtrend.`;ts='neu'}
 else{tr='Averages are mixed — no clear trend.';ts='neu'}
 rows.push(['Trend (moving averages)',`${p.an} ${fmt(A[L])} · ${p.bn} ${fmt(B[L])}${Cc[L]!=null?' · '+p.cn+' '+fmt(Cc[L]):''}`,tr,ts]);
 // EMA 9/21 cross
 let k=0;for(let i=L;i>L-30&&i>0;i--){if(e9[i]==null||e21[i]==null||e9[i-1]==null)break;if((e9[i]>e21[i])!==(e9[i-1]>e21[i-1])){k=L-i;break}}
 const eu=e9[L]>e21[L];rows.push(['9 / 21 EMA (short-term momentum)',`${fmt(e9[L])} / ${fmt(e21[L])}`,(eu?'Fast average above slow — short-term momentum is up':'Fast average below slow — short-term momentum is down')+(k?`; they crossed ${k} ${tf==='W'?'week':'bar'}${k>1?'s':''} ago.`:'.'),eu?'bull':'bear']);
 // 50/200 cross
 if(Cc[L]!=null){let x=null;for(let i=L;i>L-60&&i>0;i--){if(B[i-1]==null||Cc[i-1]==null)break;if((B[i]>Cc[i])!==(B[i-1]>Cc[i-1])){x={ago:L-i,gold:B[i]>Cc[i]};break}}
  rows.push([`${p.bn} vs ${p.cn} (big-picture trend)`,B[L]>Cc[L]?'above':'below',x?(x.gold?`“Golden cross” ${x.ago} ${tf==='W'?'weeks':'days'} ago — the medium-term average moved above the long-term one (bullish).`:`“Death cross” ${x.ago} ${tf==='W'?'weeks':'days'} ago — the medium-term average fell below the long-term one (bearish).`):(B[L]>Cc[L]?'Medium-term average is above the long-term — the big picture is up.':'Medium-term average is below the long-term — the big picture is down.'),B[L]>Cc[L]?'bull':'bear'])}
 // RSI
 const rv=R[L],rp=R[L-5];if(rv!=null){const dir=rp!=null?(rv>rp+2?'rising':rv<rp-2?'falling':'flat'):'';
  const z=rv>=70?['Overbought — the run is stretched; pullbacks are common from here.','bear']:rv<=30?['Oversold — selling looks exhausted; bounces are common, but wait for a turn up.','bull']:rv>=55?['Healthy strength — buyers in control without being stretched.','bull']:rv<=45?['Soft — sellers have the edge, not yet oversold.','bear']:['Neutral — no momentum edge.','neu'];
  rows.push(['RSI(14) — momentum, 0–100',`${fmt(rv,1)} ${dir}`,z[0],z[1]])}
 // Bollinger
 const b=BB[L];if(b){const pb=(last-b.l)/(b.u-b.l),bws=BB.slice(-120).filter(Boolean).map(x=>x.bw).sort((x,y)=>x-y),pct=bws.findIndex(x=>x>=b.bw)/bws.length;
  let t=pb>1?'Closed above the upper band — a strong push; often stretched short-term.':pb<0?'Closed below the lower band — a hard sell-off; often stretched to the downside.':pb>0.8?'Riding near the upper band — strength.':pb<0.2?'Near the lower band — weakness, or a possible support test.':'Inside the middle of the bands — normal range.';
  if(pct>=0&&pct<0.15)t+=' Bands are unusually tight (a “squeeze”) — a bigger move often follows.';
  rows.push(['Bollinger Bands (20, 2σ)',`${fmt(b.l)} – ${fmt(b.u)} · mid ${fmt(b.m)}`,t,pb>0.8&&pb<=1?'bull':pb<0.2&&pb>=0?'bear':'neu'])}
 // VWAP (anchored, from daily bars)
 const f=r.fib,fromDate=f?(f.dir==='up'?f.lo_date:f.hi_date):null;let ai=fromDate?D.findIndex(d=>d[0]>=fromDate):-1;
 if(tf==='D'){const ms=D.findIndex(d=>d[0].slice(0,7)===D[L][0].slice(0,7));const mv=TA.avwap(D,ms)[L];
  if(mv)rows.push(['VWAP — month to date (anchored)',fmt(mv),last>=mv?'Price is above the average price paid this month — buyers this month are in profit (supportive).':'Price is below the average price paid this month — this month’s buyers are under water (overhead supply).',last>=mv?'bull':'bear'])}
 if(ai>0){const av=TA.avwap(D,ai)[L];if(av)rows.push([`VWAP from the ${f.dir==='up'?'swing low':'swing high'} (${dshort(fromDate)})`,fmt(av),last>=av?`Everyone who bought since the ${f.dir==='up'?'low':'high'} is, on average, in profit — this level (${fmt(av)}) tends to act as support.`:`The average buyer since the ${f.dir==='up'?'low':'high'} is losing money — ${fmt(av)} tends to act as resistance.`,last>=av?'bull':'bear'])}
 // Fib
 if(f){const lv=f.levels.filter(x=>x.r>0&&x.r<1);const below=lv.filter(x=>x.price<=last).sort((x,y)=>y.price-x.price)[0],above=lv.filter(x=>x.price>last).sort((x,y)=>x.price-y.price)[0];
  let t=f.dir==='up'?`Measured on the 6-month rally ${fmt(f.lo)} → ${fmt(f.hi)}. `:`Measured on the 6-month drop ${fmt(f.hi)} → ${fmt(f.lo)}. `;
  t+=below&&above?`Price sits between the ${fibName(above.r)} (${fmt(above.price)}) and ${fibName(below.r)} (${fmt(below.price)}) levels.`:below?`Price is above every retracement level.`:`Price is below every retracement level.`;
  const k6=lv.find(x=>x.r===0.618);if(f.dir==='up'&&k6)t+=last>=k6.price?` The key level to hold is the 61.8% at ${fmt(k6.price)}.`:` It is below the 61.8% (${fmt(k6.price)}) — the rally’s structure is damaged.`;
  rows.push(['Fibonacci retracement',below?fibName(below.r)+' – '+(above?fibName(above.r):'top'):'below 78.6%',t,f.dir==='up'?(k6&&last>=k6.price?'bull':'bear'):(last<(lv.find(x=>x.r===0.382)||{price:1e12}).price?'bear':'neu')])}
 // volume
 const V=D.map(d=>d[5]||0),va=V.slice(-21,-1).reduce((x,y)=>x+y,0)/20;if(va&&V[L]){const vr=V[L]/va;rows.push(['Volume',`${fmt(vr,1)}× average`,vr>=1.5?`Heavy volume — the last move had conviction (${D[L][4]>=D[L][1]?'buyers':'sellers'}).`:vr<=0.6?'Light volume — the last move had little participation.':'Normal volume.',vr>=1.5?(D[L][4]>=D[L][1]?'bull':'bear'):'neu'])}
 const cr=candleRead(D);cr.forEach(c=>rows.push(['Candle pattern',c[0],c[1],c[2]]));
 const sc=rows.reduce((a,x)=>a+(x[3]==='bull'?1:x[3]==='bear'?-1:0),0);
 return{rows,score:sc,verdict:sc>=3?['call','Bullish']:sc<=-3?['put','Bearish']:sc>0?['call','Leaning bullish']:sc<0?['put','Leaning bearish']:['amb','Neutral']}}
let TV={tf:'D'};
function techBlock(r){return `<div class="card" id="tech"></div>`}
function techDraw(){const r=CC.r,el=$('#tech');if(!r||!el)return;const D=TV.tf==='W'?weekly(r.candles||[]):(r.candles||[]),T=techRead(D,TV.tf,r);
 const sig={bull:['call','▲'],bear:['put','▼'],neu:['mute','●']};
 let h=`<div class="ghead"><h2>Technical read</h2><button class="seg${TV.tf==='D'?' on':''}" onclick="TV.tf='D';techDraw()">Daily</button><button class="seg${TV.tf==='W'?' on':''}" onclick="TV.tf='W';techDraw()">Weekly</button>${T?`<span class="chip ${T.verdict[0]} big">${T.verdict[1]}</span><span class="mute">${T.rows.filter(x=>x[3]==='bull').length} bullish · ${T.rows.filter(x=>x[3]==='bear').length} bearish · ${T.rows.filter(x=>x[3]==='neu').length} neutral signals</span>`:''}</div>`;
 if(!T){el.innerHTML=h+'<p class="mute">Not enough price history.</p>';return}
 h+='<div class="tlist">'+T.rows.map(x=>`<div class="trow"><span class="tsig ${sig[x[3]][0]}">${sig[x[3]][1]}</span><div><div class="tname">${esc(x[0])} <span class="tval">${esc(x[1])}</span></div><div class="ttext">${esc(x[2])}</div></div></div>`).join('')+'</div>';
 h+=`<details class="fibhelp"><summary class="mute">Which Fibonacci levels matter?</summary><ul>${Object.entries(FIBKEY).map(([k,v])=>`<li><b>${fibName(+k)}</b> — ${v}</li>`).join('')}<li><b>0% / 100%</b> — the swing high and low themselves.</li></ul><p class="mute">On a rally, retracement levels are where pullbacks often stop (support). Look for them lining up with a put wall or moving average (★ confluence in the levels ladder) — that is where a short put strike has the most cushion.</p></details>`;
 el.innerHTML=h}
function render(r){window._band=r.band;const s=px(r),L=r.live,open_=L&&L.status.toLowerCase()=='open';
 const chg=r.chg_pct==null?'':` <span class="${r.chg_pct<0?'put':'call'}">${r.chg_pct>=0?'+':''}${fmt(r.chg_pct,2)}%</span>`;
 const ext=L&&!open_&&L.close!=null&&Math.abs(L.close-L.price)>1e-9?` <span class="mute" style="font-size:13px">close ${fmt(L.close)} · ${esc(L.status||'closed').toLowerCase()} ${fmt(L.price)}</span>`:'';
 const lastD=(r.candles||[]).length?r.candles[r.candles.length-1][0]:'';const src=L?(open_?`Price: Nasdaq real-time · ${esc(L.ts||'')}`:`Price: Nasdaq · official close ${esc(lastD)} · ${esc(L.status||'')}`):'Price: CBOE (15-min delayed — no Nasdaq quote for this symbol)';
 let h=`<div class="head"><h1>${r.ticker}</h1><span class="big">${fmt(L&&!open_&&L.close!=null?L.close:s)}</span>${chg}${ext}<span>RSI(14) <b>${r.rsi==null?'n/a':fmt(r.rsi,1)}</b></span><button class="ghost" onclick="addWatch('${r.ticker}')">+ Watch</button></div><div id="hdrx" class="xh"></div>
 <div class="mute">${src}<br>Net GEX, all expirations: <b>${money(r.net_gex_total)}</b> · ${gexLine(r,null).replace(/<div class="l">/g,'').replace(/<\/div>/g,' · ')}<br>Walls &amp; option quotes: ${srcTxt(r.source)} · as of ${esc(hhmm(r.last_trade))} ET (price then ${fmt(r.spot)}) · pulled ${esc(r.fetched)}</div>${staleNote(r)}`+earnBlock(r)+`<div class="gtwrap">${gexTrendBlock(r.gex_trend,r.gex4)}</div>`+glance(r)+chartBlock(r)+techBlock(r)+dealerBlock(r)+weekBlock(r)+weeksBlock(r)+'<div id="setufb"></div><div class="card" id="strat"></div><div class="card" id="sputs"></div><div class="card" id="bcs"></div><div class="card" id="insg"><h2>News, analysts &amp; financial health</h2><p class="mute">Loading SEC filings, Nasdaq.com news and analyst data…</p></div>';
 for(const e of r.expirations){
  const pw=e.put_wall==null?'none':`${e.put_wall} <span class="mute">(${pct(e.put_wall,s)}%)</span>`;
  const cw=e.call_wall==null?'none':`${e.call_wall} <span class="mute">(${pct(e.call_wall,s)>0?'+':''}${pct(e.call_wall,s)}%)</span>`;
  const thin=e.put_oi+e.call_oi<500?' <span class="put" style="font-size:13px">· thin open interest — walls unreliable</span>':'';
  h+=`<div class="card"><h2>${e.label} — ${e.exp} (${e.dow}, ${e.dte} DTE)${thin}</h2>
  <div class="kpis"><div class="kpi"><div class="l">Put wall</div><div class="v put">${pw}</div></div>
  <div class="kpi"><div class="l">Call wall</div><div class="v call">${cw}</div></div>
  <div class="kpi"><div class="l">Net GEX</div><div class="v">${money(e.net_gex)}</div><div class="l">${e.net_gex>=0?'dampening / range':'amplifying / trend'}</div>${gexLine(r,e)}</div>
  <div class="kpi"><div class="l">Put/Call OI</div><div class="v">${e.pc_oi==null?'–':fmt(e.pc_oi)}</div><div class="l">${e.put_oi.toLocaleString()} / ${e.call_oi.toLocaleString()}</div></div></div>
  ${levelMap(e,r)}${chart(e,s)}`;
  if(e.puts.length){h+=`<details class="ptab"><summary class="mute">Single puts at/below the put wall (${e.puts.length})</summary><div class="tw"><table><tr><th>Put</th><th>Bid/Ask</th><th>Delta</th><th>OI</th><th>ROC</th><th>Ann.</th><th>Close 30%</th><th>Close 50%</th><th>OTM</th><th>B/E</th><th>Sprd</th><th>IV</th></tr>`+
   e.puts.map(p=>`<tr><td>${p.strike}P</td><td>${fmt(p.bid)}/${fmt(p.ask)}</td><td>${fmt(p.delta)}</td><td>${p.oi.toLocaleString()}</td><td>${fmt(p.roc)}%</td><td>${fmt(p.ann,0)}%</td><td>${fmt(p.tp30)}</td><td>${fmt(p.tp50)}</td><td>${fmt(p.otm,1)}%</td><td>${fmt(p.be)}</td><td>${p.spread==null?'–':fmt(p.spread,0)+'%'}</td><td>${fmt(p.iv,0)}%</td></tr>`).join('')+`</table></div></details>`}
  else h+=`<p class="mute">No quoted puts at/below the put wall.</p>`;
  h+=`</div>`}
 h+=`<p class="mute">Walls use naive GEX (dealers long calls / short puts); OI updates once a day. Quotes are delayed — confirm the live bid before trading. Close 30%/50% = buy-to-close limit at 70%/50% of the mid credit.</p>`;
 $('#out').innerHTML=h;drawChart();techDraw();setupsDraw();insLoad(r.ticker)}
function showOut(){const o=$('#out');if(o.getBoundingClientRect().top<70||o.getBoundingClientRect().top>innerHeight*0.6)o.scrollIntoView({block:'start'})}
async function go(t){t=(t||'').trim().toUpperCase();if(!t)return;try{track('lookup')}catch(e){}if($('#tabMain').hidden)tab('main');$('#t').value=t;$('#out').innerHTML='<p class="mute">Pulling '+t+' from CBOE…</p>';showOut();
 try{const res=await fetch('/api?t='+encodeURIComponent(t));const j=await res.json();if(j.error)throw new Error(j.error);render(j);recent(t);history.replaceState(null,'','?t='+t);showOut()}
 catch(e){$('#out').innerHTML='<p class="err">'+e.message+'</p>'}}
$('#f').onsubmit=ev=>{ev.preventDefault();go($('#t').value)};recent();
document.querySelectorAll('#f .jump').forEach(b=>b.onclick=()=>{const d=$('#'+b.dataset.j);d.open=true;d.scrollIntoView({block:'start'})});
const q=new URLSearchParams(location.search).get('t');if(q)go(q);else $('#t').focus();
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const H={'Content-Type':'application/json','X-GexWalls':'1'};let W=null,lastId=0;
// ---- one set of selections for every section (expiration chips, strategy, filters, ranking) ----
const GF0={wk:'all',prof:'balanced',px:0,pop:0,ret:0,cap:0,sort:'best',n:10,flag:false};
let GF=Object.assign({},GF0);try{Object.assign(GF,JSON.parse(localStorage.getItem('walls_gf')||'{}'))}catch(e){}
let GST={};try{GST=JSON.parse(localStorage.getItem('walls_gst')||'{}')||{}}catch(e){}
const gfSave=()=>{try{localStorage.setItem('walls_gf',JSON.stringify(GF));localStorage.setItem('walls_gst',JSON.stringify(GST))}catch(e){}};
const etToday=()=>{try{return new Date().toLocaleDateString('en-CA',{timeZone:'America/New_York'})}catch(e){return new Date().toISOString().slice(0,10)}};
function weeksLocal(){const t=etToday(),d=new Date(t+'T12:00:00Z'),wd=(d.getUTCDay()+6)%7,iso=x=>x.toISOString().slice(0,10),add=(x,n)=>new Date(x.getTime()+n*864e5);
 let mon=add(d,-wd);if(wd>=5)mon=add(mon,7);const out=[{k:'0dte',label:'0 DTE',lo:t,hi:t}];
 for(let i=0;i<9;i++){const m=add(mon,7*i),f=add(m,4),lo=iso(m)>t?iso(m):iso(add(d,1));out.push({k:'w'+iso(f),label:`Fri ${f.getUTCMonth()+1}/${f.getUTCDate()}`,lo,hi:iso(add(m,6))})}
 const last=out[out.length-1].hi;out.push({k:'later',label:'2–9 mo',lo:iso(add(new Date(last+'T12:00:00Z'),1)),hi:iso(add(d,269))});out.push({k:'leaps',label:'LEAPS',lo:iso(add(d,270)),hi:iso(add(d,1100))});return out}
const WEEKS=()=>{const w=W&&W.weeks;return w&&w.length&&w[0].lo===etToday()?w:weeksLocal()};
const WCHIPS=()=>{const w=WEEKS();return [...w.filter(x=>x.k==='0dte'||x.k[0]==='w'),{k:'all',label:'All'},...w.filter(x=>x.k==='later'||x.k==='leaps')]};
function wkPass(exp,k){k=k||GF.wk;if(k==='all')return true;if(!exp)return false;const w=WEEKS();
 const x=w.find(x=>x.k===k);return !!x&&exp>=x.lo&&exp<=x.hi}
const GSTRATS=[['all','All'],['PCS','Put credit spread'],['CCS','Call credit spread'],['IC','Iron condor'],['CSP','Short put'],['CC','Covered call'],['CALL','Buy call'],['BCS','Bull call spread'],['LEAPS','LEAPS call'],['SWING','Swing (shares)']];
const GPX=[[0,'Any'],[10,'Under $10'],[20,'Under $20'],[30,'Under $30'],[50,'Under $50'],[100,'Under $100'],[-100,'$100+']];
const GSORT=[['best','Best overall'],['pop','Highest chance of profit'],['ret','Highest return'],['cap','Least capital'],['prem','Biggest premium / profit $'],['soon','Soonest expiration'],['fast','Fastest to 50% profit'],['gex','Highest GEX (net, all expirations)']];
let GEXMAP={};const rtk=r=>(r.p&&r.p.ticker)||(r.it&&r.it.ticker)||(r.r&&r.r.ticker)||r.t||'';
const gexOf=t=>{const g=GEXMAP[t];return g&&g.net!=null?g.net:null};
async function loadGexMap(){try{GEXMAP=(await (await fetch('/api/gexmap')).json()).gex||{};if(GF.sort==='gex'||(typeof RV!=='undefined'&&RV.sort==='gex'))gfRefresh()}catch(e){}}
setTimeout(loadGexMap,1500);setInterval(loadGexMap,600000);
const pxPass=p=>!GF.px||p==null||(GF.px>0?p<GF.px:p>=-GF.px);
const gst=sec=>GST[sec]||'all';
// rows: [{exp, strat, price, pop, ret, cap, prem, flag, best, p50, dte}] — o: {strats:[available], prof, minRet, extra, note}
// ---- Schwab connection (real-time option chains) ----
let SWS={},SWRE=false;
async function swLoad(){try{const s=await (await fetch('/api/schwab')).json();swDraw(s)}catch(e){}}
function swDraw(s){SWS=s||{};const n=$('#swnote'),b=$('#swbody');if(!b)return;
 n.textContent=s.connected?`· connected · real-time chains · log in again within ${fmt(s.days_left,1)} days`:s.keys?'· keys saved — log in to connect':'· not set up (using CBOE free delayed data)';
 const loc=['localhost','127.0.0.1'].includes(location.hostname);
 b.innerHTML=`<p class="mute">When connected, every option chain comes from your Schwab account (real time) instead of CBOE's free 15-min delayed files; if Schwab has a problem the app quietly falls back to CBOE. Your App Key and Secret stay in <code>schwab_keys.json</code> on this PC and are never shown again. Schwab makes you log in again every 7 days.</p>`+
 (s.error?`<p class="put">Last Schwab problem: ${esc(s.error)}</p>`:'')+(s.connected?`<p>✓ Connected${s.last_ok?' · last chain '+esc(s.last_ok):''} · ${s.ok} chains from Schwab, ${s.fail} fell back to CBOE${s.delayed?' · <b class="amb">Schwab says your data is delayed</b>':''}</p>`:'')+
 (s.connected&&!SWRE?`<div class="row"><b class="call">✓ Connected</b> <span class="mute">log in again within ${fmt(s.days_left,1)} days — Schwab requires it every 7 days</span>${loc?' <button type="button" class="ghost" onclick="SWRE=true;swDraw(SWS)">Reconnect</button>':''}</div>`:
 !loc?'<p class="amb">Set this up on the PC itself (not from the phone).</p>':`
 <div class="row"><b>1.</b> App Key <input id="swk" placeholder="${s.keys?'saved …'+esc(s.key_tail):'App Key'}" autocomplete="off" style="text-transform:none"> Secret <input id="sws" type="password" placeholder="${s.keys?'saved':'App Secret'}" autocomplete="off"> Callback URL <input id="swc" value="${esc(s.callback||'https://127.0.0.1')}" style="text-transform:none;width:190px"> <button type="button" onclick="swSave()">Save</button></div>
 <div class="row"><b>2.</b> <button type="button" onclick="swOpen()" ${s.keys?'':'disabled'}>Open Schwab login</button> <span class="mute">Log in, approve, and you land on a page that won't load — that's expected. Copy its whole address.</span></div>
 <div class="row"><b>3.</b> <input id="swu" placeholder="Paste the address here (https://127.0.0.1/?code=…)" style="text-transform:none;flex:1;min-width:220px"> <button type="button" onclick="swConnect()" ${s.keys?'':'disabled'}>Connect</button>${s.connected?' <button type="button" class="ghost" onclick="SWRE=false;swDraw(SWS)">Cancel</button> <button type="button" class="ghost" onclick="swPost(\'/api/schwab/disconnect\',{})">Disconnect</button>':''}</div>`)}
async function swPost(u,b){try{const r=await fetch(u,{method:'POST',headers:{'X-GexWalls':'1','Content-Type':'application/json'},body:JSON.stringify(b)});const j=await r.json();if(j.error){alert(j.error);return null}if(j.url)return j;swDraw(j);return j}catch(e){alert(String(e));return null}}
function swSave(){const b={app_key:($('#swk')||{}).value||'',app_secret:($('#sws')||{}).value||'',callback:($('#swc')||{}).value||''};swPost('/api/schwab/keys',b)}
async function swOpen(){const j=await swPost('/api/schwab/authurl',{});if(j&&j.url)window.open(j.url,'_blank','noopener')}
async function swConnect(){const u=$('#swu');if(!u||!u.value)return;const v=u.value;u.value='';const j=await swPost('/api/schwab/connect',{url:v});if(j&&j.connected){SWRE=false;swDraw(j);toast('Schwab connected ✓')}}
setTimeout(swLoad,1200);
// ---- live stock prices in every list: <span data-lp> cells, filled from /api/live (Nasdaq last sale, incl. pre/after hours)
const LIVE={};let liveBusy=false,livePend=false,liveDeb=0;
const legK=s=>{const sp=[],sc=[];String(s||'').replace(/[\u2212-]\s*\$?(\d+(?:\.\d+)?)\s*([PC])/g,(m,k,t)=>{(t==='P'?sp:sc).push(+k);return m});return {sp,sc,up:/\+\s*\$?\d+(?:\.\d+)?\s*C/.test(String(s||''))&&!sc.length}};
function LP(t,q,o){o=o||{};return `<span class="lp" data-lp="${esc(t)}" data-q="${q==null?'':q}" data-sp="${(o.sp||[]).join(',')}" data-sc="${(o.sc||[]).join(',')}" data-up="${o.up?1:''}" data-stop="${o.stop||''}" data-d="${o.d||''}">${q==null?'–':(o.d||'')+fmt(q)}</span>`}
function lpPaint(el){const L=LIVE[el.dataset.lp];if(!L||!L.price)return;const q=+el.dataset.q,p=L.price,d=el.dataset.d||'';
 const st=String(L.status||''),ses=/pre/i.test(st)?'pre-market':/after/i.test(st)?'after-hours':/open/i.test(st)?'live':'last';
 let html=`<b>${d}${fmt(p)}</b> <span class="lps">${ses}</span>`,bad='',warn='';
 if(q){const mv=(p/q-1)*100;html+=` <span class="${mv<0?'put':'call'}" title="vs ${d}${fmt(q)}, the price this trade was priced at">${mv>=0?'+':''}${fmt(mv,1)}%</span>`;
  for(const k of (el.dataset.sp||'').split(',').filter(Boolean).map(Number)){if(p<=k)bad=bad||`now below the ${fmtK(k)} short put`;else if((p-k)/p<0.015)warn=warn||`within 1.5% of the ${fmtK(k)} short put`}
  for(const k of (el.dataset.sc||'').split(',').filter(Boolean).map(Number)){if(p>=k)bad=bad||`now above the ${fmtK(k)} short call`;else if((k-p)/p<0.015)warn=warn||`within 1.5% of the ${fmtK(k)} short call`}
  const stop=+el.dataset.stop;if(stop&&p<=stop)bad=bad||`below the $${fmt(stop)} stop`;
  if(el.dataset.up&&mv<=-3)warn=warn||`down ${fmt(-mv,1)}% since priced`;
  if(!bad&&!warn&&Math.abs(mv)>=3)warn=`moved ${mv>=0?'+':''}${fmt(mv,1)}% since priced — re-check`;
  if(Math.abs(mv)>=1)html+=`<br><span class="mute" style="font-size:11px">priced at ${d}${fmt(q)}</span>`;
  if(bad||warn)html+=`<br><span class="${bad?'put':'amb'}" style="font-size:12px">⚠ ${bad||warn}</span>`}
 if(el._h!==html){el._h=html;el.innerHTML=html}
 const row=el.closest('tr,.pkc');if(row){row.classList.toggle('lpbad',!!bad);row.classList.toggle('lpwarn',!bad&&!!warn)}}
function paintLive(){document.querySelectorAll('[data-lp]').forEach(lpPaint)}
var STREAM=false,LIVEt=0;
async function liveFetch(force){if(liveBusy){livePend=true;return}livePend=false;const now=Date.now();
 const tks=[...new Set([...document.querySelectorAll('[data-lp]')].filter(e=>e.offsetParent!==null).map(e=>e.dataset.lp))].filter(t=>force||!LIVE[t]||now-LIVE[t]._t>(STREAM?4000:55000));
 if(!tks.length){paintLive();return}liveBusy=true;
 try{for(let i=0;i<tks.length;i+=60){const part=tks.slice(i,i+60),j=await (await fetch('/api/live?t='+encodeURIComponent(part.join(',')))).json(),n=Date.now();
  STREAM=!!j.stream;for(const t of part)LIVE[t]=Object.assign({_t:n},(j.quotes||{})[t]||{});paintLive()}}catch(e){}finally{liveBusy=false;if(livePend)setTimeout(()=>liveFetch(false),50)}}
function liveWatch(){if(!document.body)return setTimeout(liveWatch,200);
 new MutationObserver(ms=>{if(ms.every(m=>m.target.closest&&m.target.closest('[data-lp]')))return;clearTimeout(liveDeb);liveDeb=setTimeout(()=>liveFetch(false),400)}).observe(document.body,{childList:true,subtree:true});
 setInterval(()=>{if(!document.hidden&&(STREAM||Date.now()-LIVEt>55000)){LIVEt=Date.now();liveFetch(false)}},5000)}
liveWatch();
function staleLine(st){const e=Object.entries(st||{});if(!e.length)return'';
 return `<div class="mkn"><b class="amb">⚠ ${e.length} ticker${e.length>1?'s':''} skipped — CBOE has not refreshed their option quotes</b> (no trades are built from old quotes; they come back when CBOE updates): ${e.slice(0,40).map(([t,v])=>`<a href="?t=${esc(t)}">${esc(t)}</a> <span class="mute">${esc(String(v).slice(5,16))}</span>`).join(', ')}${e.length>40?' …':''}</div>`}
function mktNote(asof,st,src){return mktNote0(asof,src)+staleLine(st)}
const srcTxt=s=>s==='schwab'?'Schwab real-time':'CBOE, 15-min delayed';
function srcOf(items){const v=Object.values(items||{}).map(i=>i&&i.src).filter(Boolean);if(!v.length)return '';const n=v.filter(x=>x==='schwab').length;return n===v.length?'Schwab real-time':n?`Schwab real-time (${n} of ${v.length}; rest CBOE delayed)`:'CBOE 15-min delayed'}
function mktNote0(asof,srcMix){const open=W&&W.market_open;return `<div class="mkn">${open?'<b class="call">● Market open</b> · option prices: '+(srcMix||'CBOE 15-min delayed')+', re-priced automatically during the day':'<b class="amb">Market closed</b> · option prices are from the last close and cannot change until options trade at 9:30 ET — everything re-prices automatically from 9:47 ET'}${asof?' · priced '+esc(asof):''} · the stock price beside each trade is <b>live</b> (Nasdaq${open?'':', pre-market / after-hours'}) with its move since pricing; <span class="put">⚠</span> = the move breaks or threatens the trade</div>`}
function ufb(sec,rows,o){o=o||{};rows=rows||[];const st=gst(sec),avail=new Set(o.strats||[]);
 const cntW=k=>rows.filter(r=>(st==='all'||r.strat===st)&&wkPass(r.exp,k)).length,cntS=s=>rows.filter(r=>(s==='all'||r.strat===s)&&wkPass(r.exp)).length;
 const wc=WCHIPS().map(w=>{const n=cntW(w.k),dis=!n&&!(o.noCount);return `<button type="button" class="seg${GF.wk===w.k?' on':''}${dis?' dim':''}" data-wk="${w.k}" title="${w.lo?w.lo+' → '+w.hi:'every expiration up to ~60 days'}">${w.label}${o.noCount?'':` <span class="mute">${n}</span>`}</button>`}).join('');
 const sc=GSTRATS.map(([k,l])=>{const ok=k==='all'||avail.has(k);return `<button type="button" class="seg${st===k?' on':''}${ok?'':' dim'}" data-st="${k}" data-sec="${sec}" ${ok?'':'disabled title="not in this section"'}>${l}${ok&&k!=='all'&&!o.noCount?` <span class="mute">${cntS(k)}</span>`:''}</button>`}).join('');
 const sel=(k,opts,lab,dis,tip)=>`<label${tip?` title="${tip}"`:''}>${lab}<select data-gf="${k}"${dis?' disabled':''}>${opts.map(([v,l])=>`<option value="${v}"${String(GF[k])===String(v)?' selected':''}>${l}</option>`).join('')}</select></label>`;
 return `<div class="ufb" data-sec="${sec}">${mktNote(o.asof,o.stale,o.src)}<div class="ctl ufbw"><span class="mute">Expires</span>${wc}</div><div class="ctl ufbs"><span class="mute">Strategy</span>${sc}</div>
 <div class="sets pfil ufbf">${sel('prof',[['conservative','Conservative'],['balanced','Balanced'],['aggressive','Aggressive']],'Risk profile',!o.prof,o.prof?'':'no risk profiles in this section')}
 ${sel('px',GPX,'Stock price')}${sel('pop',[[0,'Any'],[50,'50%+'],[60,'60%+'],[70,'70%+'],[80,'80%+'],[90,'90%+']],'Min chance')}
 ${sel('ret',[[0,'Any'],[10,'10%+'],[20,'20%+'],[30,'30%+'],[50,'50%+'],[100,'100%+']],'Min return'+(o.minRet?` <span class="mute">(≥${o.minRet}% here)</span>`:''),false,'Profit if it works ÷ capital (spreads: credit ÷ max loss · puts: premium ÷ cash · calls: gain at the target)')}
 ${sel('cap',[[0,'Any'],[250,'$250'],[500,'$500'],[1000,'$1,000'],[2500,'$2,500'],[5000,'$5,000'],[10000,'$10,000']],'Max capital')}
 ${sel('sort',GSORT,'Rank by')}${sel('n',[[5,'Top 5'],[10,'Top 10'],[25,'Top 25'],[50,'Top 50']],'Show')}
 <label><input type="checkbox" data-gf="flag"${GF.flag?' checked':''}> hide earnings / catalyst-flagged</label>${o.extra||''}</div>${o.note?`<div class="mute" style="font-size:12px;margin:-4px 0 6px">${o.note}</div>`:''}</div>`}
function gfFilter(rows,o){o=o||{};const st=o.sec?gst(o.sec):'all',minRet=Math.max(+GF.ret||0,o.minRet||0);
 return rows.filter(r=>(st==='all'||r.strat===st)&&(o.noWk||wkPass(r.exp))&&pxPass(r.price)&&(o.noPop||r.pop==null||r.pop>=GF.pop)
  &&(r.ret==null?!minRet:r.ret>=minRet-1e-9)&&(!GF.cap||r.cap==null||r.cap<=GF.cap)&&!(GF.flag&&r.flag))}
function gfSort(rows){const k={best:r=>r.best??0,pop:r=>(r.pop??0)+(r.best??0)*1e-6,ret:r=>r.ret??0,cap:r=>-(r.cap??1e12),prem:r=>r.prem??0,soon:r=>-(r.dte??999),fast:r=>r.p50??-1,gex:r=>{const g=gexOf(rtk(r));return g==null?-1e18:g+(r.best??0)*1e-9}}[GF.sort]||(r=>r.best??0);
 return rows.slice().sort((a,b)=>k(b)-k(a))}
const STARS=n=>n>=2?'<span class="chip acc" title="★★ standout: return 30%+ with a 75%+ chance of profit — strongly positive expected value">★★</span>':n==1?'<span class="chip acc" title="★ return 25%+ with a 70%+ chance of profit — positive expected value">★</span>':'';
function starsOf(p,g,l){if(p==null||g==null)return 0;l=l??1;const ev=p*g-(1-p)*l;return ev>=0.12&&p>=0.75&&g>=0.30?2:ev>=0.05&&p>=0.70&&g>=0.25?1:0}
function gfRefresh(){for(const f of [()=>PK&&PK.d&&pkRender(),()=>CE&&CE.d&&ceRender(),()=>SC&&SC.res&&scRender(),()=>PT&&PT.loaded&&ptRender(),()=>CL&&CL.loaded&&clRender(),()=>W&&wrender(),()=>CC&&CC.r&&setupsDraw()])try{f()}catch(e){console.error(e)}}
document.addEventListener('click',e=>{const b=e.target.closest('.ufb [data-wk],.ufb [data-st]');if(!b||b.disabled)return;
 if(b.dataset.wk)GF.wk=b.dataset.wk;else GST[b.dataset.sec]=b.dataset.st;gfSave();gfRefresh()});
document.addEventListener('change',e=>{const el=e.target.closest('.ufb [data-gf]');if(!el)return;const k=el.dataset.gf;
 GF[k]=el.type==='checkbox'?el.checked:(['prof','sort'].includes(k)?el.value:+el.value);gfSave();gfRefresh()});
// GEX trend: past sessions sparkline + next 4 expiration weeks
function gexSpark(days,w,h){w=w||90;h=h||22;if(!days||days.length<2)return'<span class="mute" title="daily GEX history fills in from the first day this ticker is checked">trend: collecting</span>';
 const v=days.map(d=>d[1]),mx=Math.max(...v.map(Math.abs),1),bw=w/v.length;
 return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="vertical-align:middle"><line x1="0" x2="${w}" y1="${h/2}" y2="${h/2}" stroke="var(--line)"/>${v.map((x,i)=>{const bh=Math.max(1,Math.abs(x)/mx*(h/2-1));return `<rect x="${i*bw+0.5}" y="${x>=0?h/2-bh:h/2}" width="${Math.max(1,bw-1)}" height="${bh}" fill="var(--${x>=0?'call':'put'})"><title>${days[i][0]}: ${money(x)}${days[i][2]?' · price '+fmt(days[i][2]):''}</title></rect>`}).join('')}</svg>`}
function gex4Cells(w4,compact){if(!w4||!w4.length)return'<span class="mute">–</span>';
 return `<span class="g4">${w4.map(x=>x.net==null?`<span class="mute" title="no expiration that week">${x.fri.slice(5)} –</span>`:`<span class="g4c ${x.net>=0?'call':'put'}" title="Week of Fri ${x.fri}: net GEX ${money(x.net)}${x.pw!=null?' · put wall '+fmtK(x.pw):''}${x.cw!=null?' · call wall '+fmtK(x.cw):''}">${x.fri.slice(5).replace('-','/')} ${money(x.net)}${!compact&&x.pw!=null?` <span class="mute">PW ${fmtK(x.pw)}</span>`:''}</span>`).join(compact?' ':' · ')}</span>`}
function gexTrendBlock(days,w4){const d=days||[],chg=d.length>=2?d[d.length-1][1]-d[0][1]:null;
 return `<div class="gtrend"><span class="mute">GEX last ${d.length||0} sessions</span> ${gexSpark(d,120,24)}${chg!=null?` <span class="${chg>=0?'call':'put'}">${chg>=0?'▲':'▼'} ${money(Math.abs(chg))}</span> <span class="mute">since ${dshort(d[0][0])}</span>`:''}<br><span class="mute">Next 4 weeks</span> ${gex4Cells(w4)}</div>`}
// badges (✦ patterns, ★ analysts, ⚠ dilution, 24h) for any list of tickers
const BADGES={};let badgeT=0;
function badgeHtml(tk){const b=BADGES[tk];return b&&b.length?b.map(x=>`<span class="chip ${x.c||'mute'} bdg" title="${esc(x.tip||'')}">${esc(x.t)}</span>`).join(''):''}
async function loadBadges(tks,then){tks=[...new Set(tks.filter(Boolean))].filter(t=>!(t in BADGES));if(!tks.length)return;
 try{const j=await (await fetch('/api/badges?t='+encodeURIComponent(tks.join(',')))).json();Object.assign(BADGES,j.badges||{});
  if(j.pending){for(const t of tks)if(!(BADGES[t]||[]).length)delete BADGES[t];clearTimeout(badgeT);badgeT=setTimeout(()=>loadBadges(tks,then),20000)}
  if(then)then()}catch(e){}}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:H,body:JSON.stringify(b||{})});const j=await r.json();if(j.error)throw new Error(j.error);return j}
async function wload(){try{W=await (await fetch('/api/watch')).json();wrender();try{if(PT.loaded)ptRender()}catch(_){}}catch(e){}}
async function wsave(p){try{const j=await post('/api/watch',p);W.config=j.config;wrender()}catch(e){$('#wlnote').textContent=e.message}}
function addWatch(t,k){if(!W)return;const wl=W.config.watchlist.filter(x=>x.ticker!==t);wl.push({ticker:t,strike:k||null});wsave({watchlist:wl});$('#wl').open=true}
function rmWatch(t){wsave({watchlist:W.config.watchlist.filter(x=>x.ticker!==t)})}
let WL={win:'d5',kind:'pcs',prof:'balanced',sort:'gex',psort:'score'};try{Object.assign(WL,JSON.parse(localStorage.getItem('walls_wl')||'{}'))}catch(e){}
const WLW=[['0dte','0 DTE'],['d5','1–5 DTE'],['weekly','6–12 DTE'],['w2','13–20 DTE'],['w3','21–29 DTE'],['monthly','30–45 DTE'],['m60','46–60 DTE'],['m90','61–90 DTE']],WLP=[['conservative','Conservative'],['balanced','Balanced'],['aggressive','Aggressive']];
function wlSet(k,v){WL[k]=v;try{localStorage.setItem('walls_wl',JSON.stringify(WL))}catch(e){}wrender()}
function wlCands(S,kind){const out=[];for(const [k,w] of Object.entries(S||{})){if(!w)continue;
  const c=kind==='CSP'?w.put:kind==='BCS'?(w.bcs||{})[GF.prof]:(w.pcs||{})[GF.prof];if(!c)continue;
  out.push({c,k,exp:c.exp,strat:kind,pop:c.pop,ret:kind==='CSP'?c.roc:kind==='BCS'?(c.rr||0)*100:c.roc,cap:kind==='CSP'?c.cash:kind==='BCS'?c.cost:c.maxloss,
   prem:kind==='CSP'?c.prem:kind==='BCS'?c.maxprofit:(c.credit||0)*100,best:(c.pop||0)*(kind==='BCS'?(c.rr||0):(c.cw||(c.roc||0)/100))*(c.fallback?0.7:1),p50:c.p50,dte:c.dte,flag:!!c.earn_cross})}return out}
const wlKind=()=>{const k=gst('watch');return k==='all'?'PCS':k};
function wlCell(tk,st){const S=st.setups;if(!S)return '<span class="mute">press Check now</span>';const kind=wlKind();
 const x=gfSort(gfFilter(wlCands(S,kind),{sec:''}))[0],c=x&&x.c;
 if(!c&&kind==='BCS')return '<span class="mute">no bull call spread for these selections (0–6 DTE has none; earnings, thin or wide quotes)</span>';
 if(!c)return `<span class="mute">none that pays ${kind==='CSP'?'10':'15'}%+ for these selections${GF.wk==='0dte'?' (0 DTE only on an expiration day, market open)':''}</span>`;
 const p50=c.d50_date?` · 50% ~${dshort(c.d50_date)}`:'';
 if(kind==='BCS')return `${dshort(c.exp)} · buy <b>${c.long}/${c.short}C</b> for ${fmt(c.debit)} <span class="mute">($${fmt(c.cost,0)})</span><br><span class="mute">max +$${fmt(c.maxprofit,0)} (${fmt(c.rr,1)}:1) · chance ${fmt(c.pop,0)}%${c.d_up50_date?` · +50% ~${dshort(c.d_up50_date)}`:''}${c.earn_cross?' · <span class="amb">earnings</span>':''}</span> <a href="#" class="mute" onclick="calcOpen({tk:'${tk}',strat:'cds',exp:'${c.exp}',k1:${c.long},k2:${c.short}});return false">calc</a>`;
 if(kind==='CSP')return `${dshort(c.exp)} · sell <b>${c.short}P</b> ${fmt(c.credit)} <b class="call">$${fmt(c.credit*100,0)}</b><br><span class="mute">POP ${fmt(c.pop,0)}% · ${fmt(c.roc,1)}% on $${fmt(c.cash,0)} cash${p50}</span>`;
 if(c.fallback)return `<span class="amb" title="${esc((c.issues||[]).join(' · '))}">closest (not ideal)</span> ${dshort(c.exp)} · <b>${c.short}/${c.long}</b> cr ${fmt(c.credit)} <span class="mute">($${fmt(c.credit*100,0)})</span><br><span class="mute">POP ${fmt(c.pop,0)}% · ${esc((c.issues||[])[0]||'')}</span> <a href="#" class="mute" onclick="prefillTrade('${tk}','${c.exp}',${c.short},${c.long},${c.credit},'Watchlist',${c.pop});return false">+ track</a>`;
 return `${dshort(c.exp)} · <b>${c.short}/${c.long}</b> cr ${fmt(c.credit)} <span class="mute">($${fmt(c.credit*100,0)})</span><br><span class="mute">POP ${fmt(c.pop,0)}% · ${fmt(c.roc,0)}% on $${fmt(c.maxloss,0)} risk${p50}${c.earn_cross?' · <span class="amb">earnings</span>':''}</span> <a href="#" class="mute" onclick="prefillTrade('${tk}','${c.exp}',${c.short},${c.long},${c.credit},'Watchlist',${c.pop});return false">+ track</a>`}
function wlCtl(){const rows=[];for(const [t,st] of Object.entries((W&&W.status)||{}))if(st&&st.setups)for(const k of ['PCS','CSP','BCS'])rows.push(...wlCands(st.setups,k).map(x=>({...x,price:st.spot})));
 const seg=(k,v,l)=>`<button type="button" class="seg${WL[k]===v?' on':''}" onclick="wlSet('${k}','${v}')">${l}</button>`;
 return ufb('watch',rows,{strats:['PCS','CSP','BCS'],prof:true,extra:`<span class="mute">Order tickers:</span> ${seg('sort','gex','Highest GEX')}${seg('sort','rising','GEX rising most')}${seg('sort','added','As added')}`,note:'Best setup column = the best trade for each ticker in the chosen expiration week / strategy / risk profile'})}
const EXT={};let extT=0;
async function loadExt(tks){if(W&&W.market_open)return;tks=tks.filter(t=>!(t in EXT));if(!tks.length)return;try{Object.assign(EXT,await (await fetch('/api/ext?t='+encodeURIComponent(tks.join(',')))).json());if(W)wrender()}catch(e){}}
function extPick(x,L){if(!x)return null;const k=x.pick||(x.post&&x.post.last?'post':x.pre&&x.pre.last?'pre':null);return k&&x[k]&&x[k].last?[k==='post'?L[0]:L[1],x[k]]:null}
function extLine(tk){const p=extPick(EXT[tk],['AH','Pre']);
 return (p?`<br><span class="xh ${p[1].pct<0?'put':'call'}" title="${p[0]==='AH'?'after-hours':'pre-market'} last trade${p[1].ref?' · % vs '+esc(p[1].ref)+(p[1].close?' $'+fmt(p[1].close):''):''} · volume ${big(p[1].vol)} · ${esc(p[1].updated||'')}">${p[0]} ${fmt(p[1].last)}${p[1].pct==null?'':' '+(p[1].pct>=0?'+':'')+fmt(p[1].pct,1)+'%'}</span>`:'')}
function wrender(){const c=W.config,m=W.monitor||{};$('#wlctl').innerHTML=wlCtl();{const p=$('#mkt');if(p){p.textContent=W.market_open?'● Market open':'Market closed';p.classList.toggle('open',!!W.market_open)}}
 $('#wlnote').textContent=`· ${c.watchlist.length} ticker${c.watchlist.length==1?'':'s'} · ${W.market_open?'market open':'market closed'}${m.last_run?' · last check '+m.last_run:''}${m.note&&m.note!=='market closed'?' · '+m.note:''}`;
 if(!c.watchlist.length){$('#wlist').innerHTML='<p class="mute">Add any ticker. Add your short strike to also get alerts near it.</p>'}else{
 let h=`<table><tr><th>Ticker</th><th>Spot</th><th title="Net GEX across all expirations, and how it changed">Total GEX</th><th title="Daily net GEX over the last sessions (bars) and the GEX of each of the next 4 expiration weeks">GEX trend · next 4 weeks</th><th>Your strike</th><th>Best setup · ${esc((WCHIPS().find(x=>x.k===GF.wk)||{}).label||'')}</th><th>Expiration</th><th>Put wall</th><th>Net GEX</th><th></th></tr>`;
 const SS=W.status||{},gv=t=>{const v=(SS[t]||{}).net_total;return v==null?-Infinity:v},cv=t=>{const v=(SS[t]||{}).gex_chg_sess;return v==null?0:v};
 const order=[...c.watchlist];if(WL.sort==='gex')order.sort((a,b)=>gv(b.ticker)-gv(a.ticker)||cv(b.ticker)-cv(a.ticker));else if(WL.sort==='rising')order.sort((a,b)=>cv(b.ticker)-cv(a.ticker)||gv(b.ticker)-gv(a.ticker));
 for(const it of order){const st=(W.status||{})[it.ticker]||{};const ex=st.exps||[];
  const spot=st.spot==null?(st.error?'<span class="put">'+esc(st.error)+'</span>':'<span class="mute">not checked</span>'):LP(it.ticker,st.spot)+(st.chg_pct==null?'':` <span class="${st.chg_pct<0?'put':'call'}">${fmt(st.chg_pct,1)}%</span>`)+(st.rsi==null?'':`<br><span class="mute">RSI ${fmt(st.rsi,1)}</span>`);
  const k=it.strike?fmt(it.strike,2)+(st.spot?` <span class="mute">(${pct(it.strike,st.spot)}%)</span>`:''):'–';
  const exps=ex.map(e=>`${e.exp.slice(5)} <span class="mute">${e.dte}d</span>`).join('<br>')||'–';
  const pws=ex.map(e=>e.put_wall==null?'–':`${e.put_wall} <span class="${e.dist_pct<0?'put':'mute'}">(${e.dist_pct>=0?'+':''}${fmt(e.dist_pct,1)}%)</span>`).join('<br>')||'–';
  const nets=ex.map(e=>`<span class="${e.net_gex<0?'put':'call'}">${money(e.net_gex)}</span>`).join('<br>')||'–';
  const gt=(W.gex_trends||{})[it.ticker]||{};
  h+=`<tr><td><a href="?t=${it.ticker}" onclick="go('${it.ticker}');return false"><b>${it.ticker}</b></a><br><span data-bdg="${it.ticker}">${badgeHtml(it.ticker)}</span></td><td>${spot}${st.stale?`<br><span class="amb" style="font-size:11px" title="CBOE has not refreshed this option chain; no setups are shown from old quotes">⚠ CBOE quotes from ${esc(String(st.stale).slice(5,16))}</span>`:''}</td><td>${st.net_total==null?'<span class="mute">–</span>':`<b class="${st.net_total<0?'put':'call'}">${money(st.net_total)}</b>`+(st.gex_chg_sess!=null?`<br><span class="${st.gex_chg_sess<0?'put':'call'}" title="since ${esc(st.gex_since||'')}">${st.gex_chg_sess>=0?'▲':'▼'} ${money(Math.abs(st.gex_chg_sess))}</span>`:'')+(st.gex_chg15!=null?` <span class="mute" title="vs ~15 min earlier">15m ${st.gex_chg15>=0?'+':'-'}${money(Math.abs(st.gex_chg15)).replace('-','')}</span>`:'')}</td><td>${gexSpark(gt.days,90,22)}<br>${gex4Cells(gt.w4||st.gex4,true)}</td><td>${k}</td><td>${wlCell(it.ticker,st)}</td><td>${exps}</td><td>${pws}</td><td>${nets}</td><td><button class="x" title="Remove" onclick="rmWatch('${it.ticker}')">×</button></td></tr>`}
 $('#wlist').innerHTML=h+'</table>';const wt=c.watchlist.map(x=>x.ticker);loadBadges(wt,()=>wrender());loadExt(wt)}
 const f=[['interval_min','Check every (min)','number'],['near_pct','Near put wall within (%)','number'],['gex_drop_pct','GEX drop alert (%)','number'],['strike_near_pct','Near your strike within (%)','number'],
  ['include_weekly','Also watch weekly expiration','checkbox'],['market_hours_only','Market hours only','checkbox'],['toast','Windows pop-up','checkbox'],['ntfy','Phone push (ntfy.sh)','checkbox'],['ntfy_topic','ntfy topic','text']];
 if(!$('#sets').dataset.ok){$('#sets').innerHTML=f.map(([k,l,t])=>`<label>${t=='checkbox'?`<input type="checkbox" data-k="${k}"> ${l}`:`${l}<input type="${t}" data-k="${k}" step="any">`}</label>`).join('');
  $('#sets').querySelectorAll('input').forEach(el=>el.onchange=()=>wsave({[el.dataset.k]:el.type=='checkbox'?el.checked:el.value}));$('#sets').dataset.ok=1}
 $('#sets').querySelectorAll('input').forEach(el=>{const v=c[el.dataset.k];if(el.type=='checkbox')el.checked=!!v;else if(document.activeElement!==el)el.value=v})}
function alertHtml(a){return `<div class="al ${a.level=='danger'?'':'warn'}"><b>${esc(a.title)}</b>${esc(a.msg)} <span class="mute">· ${esc(a.ts)}</span></div>`}
async function aload(first){try{const a=await (await fetch('/api/alerts?since='+lastId)).json();if(!a.length)return;lastId=a[a.length-1].id;
 const old=$('#alog').querySelector('p')?'':$('#alog').innerHTML;$('#alog').innerHTML=a.slice().reverse().map(alertHtml).join('')+old;[...$('#alog').children].slice(25).forEach(n=>n.remove());
 if(!first){$('#banner').innerHTML=a.slice(-3).reverse().map(alertHtml).join('');wload();
  if('Notification' in window&&Notification.permission=='granted')a.forEach(x=>new Notification(x.title,{body:x.msg}))}}catch(e){}}
$('#wf').onsubmit=ev=>{ev.preventDefault();const t=$('#wt').value.trim().toUpperCase();if(!t)return;addWatch(t,parseFloat($('#wk').value)||null);$('#wt').value='';$('#wk').value=''};
$('#chk').onclick=async()=>{$('#wlnote').textContent='· checking…';try{const j=await post('/api/check');W.status=j.status;await aload();await wload()}catch(e){$('#wlnote').textContent=e.message}};
$('#tst').onclick=async()=>{try{await post('/api/test');$('#wlnote').textContent='· test alert sent'}catch(e){$('#wlnote').textContent=e.message}};
$('#bn').onclick=async()=>{if(!('Notification' in window))return;const p=await Notification.requestPermission();$('#bn').textContent=p=='granted'?'Browser notifications on':'Browser notifications blocked'};
wload();aload(true);setInterval(()=>{aload();wload()},60000);
tload();setInterval(()=>{if(!document.hidden&&(STREAM||Date.now()-(TR._t||0)>55000))tload()},10000);$('#trref').onclick=ev=>{ev.preventDefault();$('#trnote').textContent='· refreshing…';TR.sig='';tload()};
// every strategy the trade tracker can hold: strike boxes in order, legs [type, side], credit or debit
const TADD={PCS:{l:'Put credit spread (bullish)',k:['Short (sold) put','Long (bought) put — lower'],kind:'credit'},
 CCS:{l:'Call credit spread (bearish)',k:['Short (sold) call','Long (bought) call — higher'],legs:[['C',-1],['C',1]],kind:'credit'},
 IC:{l:'Iron condor',k:['Short put','Long put — lower','Short call','Long call — higher'],legs:[['P',-1],['P',1],['C',-1],['C',1]],kind:'credit'},
 CSP:{l:'Cash-secured / short put',k:['Short (sold) put'],legs:[['P',-1]],kind:'credit'},
 CC:{l:'Covered call / buy-write (100 shares + short call)',k:['Short (sold) call'],legs:[['STK',1],['C',-1]],kind:'debit',pr:'Net cost per share (stock price − call premium)'},
 BCS:{l:'Bull call spread (debit)',k:['Long (bought) call','Short (sold) call — higher'],legs:[['C',1],['C',-1]],kind:'debit'},
 PDS:{l:'Bear put spread (debit)',k:['Long (bought) put','Short (sold) put — lower'],legs:[['P',1],['P',-1]],kind:'debit'},
 LC:{l:'Long call / LEAPS',k:['Call strike'],legs:[['C',1]],kind:'debit'},
 LPUT:{l:'Long put',k:['Put strike'],legs:[['P',1]],kind:'debit'},
 SWING:{l:'Shares (stock / ETF)',k:[],legs:[['STK',1]],kind:'debit',noexp:1,pr:'Price paid per share',q:'Shares'},
 CUSTOM:{l:'Custom legs (anything else)',k:[],text:1,pr:'Net fill per share'}};
function tfShape(){const f=$('#tf'),c=TADD[f.st.value]||TADD.PCS,box=[f.sh,f.lg,f.k3,f.k4];
 box.forEach((b,i)=>{b.hidden=i>=c.k.length;b.required=i<c.k.length;b.placeholder=c.k[i]||''});
 f.lt.hidden=!c.text;f.lt.required=!!c.text;$('#tfkind').style.display=c.text?'':'none';$('#tfexp').style.display=c.noexp?'none':'';f.exp.required=!c.noexp&&!c.text;
 f.qty.placeholder=c.q||'Contracts';f.es.hidden=f.st.value!=='PCS';
 f.cr.placeholder=c.pr||((c.kind==='credit'?'Credit received':'Debit paid')+' per share (e.g. 2.83)');
 $('#tfhint').textContent=c.text?'Type each leg as sign, strike, P or C: − sold, + bought. Add "100 sh" for shares. Leave the expiration empty for shares only.':f.st.value==='CC'?'One contract = 100 shares plus 1 short call.':''}
$('#tf').st.innerHTML=Object.entries(TADD).map(([k,c])=>`<option value="${k}">${c.l}</option>`).join('');$('#tf').st.onchange=tfShape;tfShape();
$('#tf').onsubmit=async ev=>{ev.preventDefault();const f=ev.target;$('#trmsg').textContent='Adding…';
 const c=TADD[f.st.value]||TADD.PCS;
 if(f.st.value!=='PCS'){const ks=[f.sh,f.lg,f.k3,f.k4].map(b=>+b.value);let i=0;
  const a={ticker:f.tk.value.trim().toUpperCase(),exp:c.noexp?'':f.exp.value,strat:f.st.value==='CUSTOM'?'':f.st.value,price:+f.cr.value,kind:c.text?f.kd.value:c.kind,qty:+(f.qty.value||1),entry_date:f.ed.value||null,src:'Added by hand'};
  if(c.text)a.legs_text=f.lt.value;else a.legs=c.legs.map(([cp,side])=>({cp,side,n:1,K:cp==='STK'?null:ks[i++]}));
  const ok=await tsave({add_legs:a});if(ok){$('#trmsg').textContent='Added.';f.reset();f.qty.value=1;tfShape();$('#tradd').open=false}return}
 const ok=await tsave({add:{ticker:f.tk.value.trim().toUpperCase(),exp:f.exp.value,short:+f.sh.value,long:+f.lg.value,qty:+(f.qty.value||1),credit:+f.cr.value,entry_date:f.ed.value||null,entry_spot:f.es.value?+f.es.value:null,src:f.dataset.src||'',rec_p:f.dataset.pop?+f.dataset.pop/100:null}});f.dataset.src='';f.dataset.pop='';
 if(ok){$('#trmsg').textContent='Added.';f.reset();f.qty.value=1;tfShape();$('#tradd').open=false}};
const dshort=d=>{if(!d)return'–';const t=new Date(d+'T12:00:00Z');return ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][t.getUTCDay()]+' '+(t.getUTCMonth()+1)+'/'+t.getUTCDate()};
let SC={mode:'monthly',sort:'score',pop:65,risk:0,earn:false,res:null,st:{},kind:'pcs',ret:20,prem:0,exp:'',per:1,n:10,hid:{}};try{const v=JSON.parse(localStorage.getItem('walls_scan')||'{}');for(const k of ['sort','mode','kind','ret','prem','per','n'])if(v[k]!=null)SC[k]=v[k]}catch(e){}
const QC={Leading:'call',Improving:'acc',Weakening:'amb',Lagging:'put',Benchmark:'mute'};
const chip=q=>`<span class="chip ${QC[q]||'mute'}">${esc(q||'–')}</span>`;
function scPool(it){const s=it.spreads||[...(it.dte0||[]),...(it.dte5||[]),...(it.weekly||[]),...(it.mid||[]),...(it.monthly||[])];
 const p=Array.isArray(it.puts)?it.puts:[...(it.puts0||[]),...(it.puts5||[]),...(it.putsw||[]),...(it.putsmid||[]),...(it.putsm||[])];
 return [...s.map(c=>({...c,_k:'PCS'})),...p.map(c=>({...c,_k:'CSP'}))]}
const retOf=c=>c.long==null?c.roc:(c.width>c.credit?c.credit/(c.width-c.credit)*100:null);   // spreads: credit ÷ max loss · puts: premium ÷ cash
const riskOf=c=>c.long==null?c.cash:c.maxloss;
function scRows(r){const out=[];for(const it of Object.values(r.items||{}))for(const c of scPool(it)){const liq=1-Math.min(c.slip||0,60)/300;
  out.push({it,c,exp:c.exp,strat:c._k,price:it.price,pop:c.pop,ret:retOf(c),cap:riskOf(c),prem:c.credit*100,flag:!!(it.earnings&&it.earnings<=c.exp),
   best:(c.score||0)*(0.85+0.3*(it.mom||0)/100)*(c.below_wall?1.05:1)*liq,p50:c.p50,dte:c.dte})}return out}
function scMiss(x){const m=[];if(GF.pop&&x.pop<GF.pop)m.push(`POP ${fmt(x.pop,0)}% < your ${GF.pop}%`);if(GF.ret&&(x.ret==null||x.ret<GF.ret))m.push(`return ${x.ret==null?'–':fmt(x.ret,0)+'%'} < your ${GF.ret}%`);
 if(GF.cap&&x.cap>GF.cap)m.push(`capital $${fmt(x.cap,0)} > your max`);if(GF.flag&&x.flag)m.push('earnings before expiration');if(!pxPass(x.price))m.push('outside your price range');return m}
function scTop(relax){const r=SC.res;if(!r)return[];let rows=scRows(r);const st=gst('scan');
 if(!relax)rows=gfFilter(rows,{sec:'scan'});else rows=rows.filter(x=>(st==='all'||x.strat===st)&&wkPass(x.exp)).map(x=>{const miss=scMiss(x);return {...x,miss,best:x.best*0.8**miss.length}});
 rows=gfSort(rows);const per={},out=[],seen=new Set();
 for(const x of rows){const tk=x.it.ticker,k=tk+x.exp+x.c.short+x.strat;if(seen.has(k))continue;seen.add(k);per[tk]=per[tk]||0;if(per[tk]>=SC.per)continue;per[tk]++;
  out.push({...x.it,...x.c,rank:x.best,miss:x.miss||[],stars:starsOf(x.pop/100,(x.ret||0)/100,x.strat==='CSP'?0.15:1),notes:pcsIssues(x.c,x.it,x.dte<=5?'d5':'')});if(out.length>=GF.n)break}
 return out}
function scRender(){const st=SC.st||{},r=SC.res;
 $('#scnote').textContent=st.running?`· scanning… ${st.progress||''}`:st.error?`· error: ${st.error}`:r?`· last scan ${r.ts} · ${r.universe} tickers screened`:'· not run yet';
 const all=r?scRows(r):[];
 $('#scufb').innerHTML=ufb('scan',all,{asof:SC.res&&SC.res.ts?SC.res.ts:'',stale:SC.res&&SC.res.stale,src:srcOf(SC.res&&SC.res.items),strats:['PCS','CSP'],extra:`<label>Per ticker<select data-scper><option value="1"${SC.per==1?' selected':''}>Best 1</option><option value="3"${SC.per==3?' selected':''}>Top 3</option></select></label>`});
 if(!r){$('#scres').innerHTML='<p class="mute">Press Run scan (about a minute).</p>';rotRender();return}
 if(!all.length){$('#scres').innerHTML='<p class="mute">This scan has no spreads or puts. Press Run scan.</p>';rotRender();return}
 let top=scTop(),relaxed=false;if(!top.length){const t2=scTop(true).slice(0,8);if(t2.length){top=t2;relaxed=true}}
 const relNote=relaxed?`<div class="al warn" style="margin:0 0 8px"><b>Nothing passes all your selections — showing the closest ${top.length}</b>Each row lists which selection it misses (“Outside your selections”). Loosen one to see more.</div>`:'';
 const short=GF.wk==='0dte'||(WEEKS().find(w=>w.k===GF.wk)||{}).hi<=new Date(Date.now()+6*864e5).toISOString().slice(0,10);
 const smNote=short?'<div class="al warn" style="margin:0 0 8px"><b>Short-dated</b>Quotes are 15 minutes delayed and short-dated premiums move fast — re-check the live bid/ask before every order. 0 DTE contracts only exist on an expiration day while the market is open.</div>':'';
 if(!top.length){$('#scres').innerHTML=smNote+'<p class="mute">Nothing in this expiration week / strategy. Pick another week or “All”.</p>';rotRender();return}
 const sp=top.filter(x=>x.long!=null),pu=top.filter(x=>x.long==null);let h=relNote+smNote;
 if(sp.length){h+=`<h3 style="margin:6px 0 4px">Put credit spreads${dirTag('PCS')}</h3><table><tr><th>#</th><th>Ticker</th><th>Price · RSI</th><th>Momentum</th><th>Expiration</th><th>Sell / buy put</th><th title="Realistic fill: halfway between mid and natural">Credit (fill)</th><th class="srt" data-s="cap">Max loss</th><th>Cr/width</th><th class="srt" data-s="pop">POP</th><th class="srt" data-s="fast" title="Chance the spread is worth half the credit at some daily close before expiration">Hits 50%</th><th title="Most likely date the spread is worth half the credit">When 50%</th><th>Breakeven</th><th>Put wall</th><th>Earnings</th><th>Rank</th><th>${relaxed?'Outside your selections / watch':'Watch'}</th></tr>`;
  sp.forEach((x,i)=>{h+=`<tr><td>${i+1}</td><td><a href="?t=${x.ticker}" onclick="go('${x.ticker}');return false"><b>${x.ticker}</b></a> ${setupSig(x,x.trend)} ${STARS(x.stars)} <span data-bdg="${x.ticker}">${badgeHtml(x.ticker)}</span><br><span class="mute">${esc(x.kind)} · ${esc(x.sector)}</span><br><a href="#" class="mute" onclick="prefillTrade('${x.ticker}','${x.exp}',${x.short},${x.long},${x.credit},'Spread scanner',${x.pop});return false">+ track</a> · <a href="#" class="mute" onclick="calcOpen({tk:'${x.ticker}',strat:'pcs',exp:'${x.exp}',k1:${x.short},k2:${x.long}});return false">calc</a></td>
   <td>${LP(x.ticker,x.price,{sp:[x.short]})}<br><span class="mute">RSI ${x.rsi==null?'–':fmt(x.rsi,0)}</span></td>
   <td><span class="${x.rs1<0?'put':'call'}">${x.rs1>=0?'+':''}${fmt(x.rs1,1)}%</span> <span class="mute">1M vs SPY</span><br>${chip(x.quad)}${x.kind!=='ETF'&&x.sector_quad?` <span class="mute">sector</span> ${chip(x.sector_quad)}`:''}</td>
   <td>${dshort(x.exp)}<br><span class="mute">${x.dte}d</span></td><td><b>${x.short}</b> / ${x.long}<br><span class="mute">${fmt(x.otm,1)}% OTM · Δ ${fmt(x.sdelta)}</span></td>
   <td>${fmt(x.credit)}<br><span class="mute" title="mid / natural (fill at market); the credit shown is halfway between">${x.mid!=null?'mid '+fmt(x.mid)+' · ':''}nat ${fmt(x.nat)}</span>${x.q?`<br><span class="mute" style="font-size:11px">${fmt(x.q[0])}×${fmt(x.q[1])} / ${fmt(x.q[2])}×${fmt(x.q[3])}</span>`:''}</td><td>$${fmt(x.maxloss,0)}</td><td>${fmt(x.cw*100,0)}%</td><td><b>${fmt(x.pop,1)}%</b><br><span class="mute">${fmt(retOf(x),0)}% on risk</span></td><td>${x.p50==null?'–':'<b>'+fmt(x.p50,0)+'%</b>'}</td><td>${x.d50_date?'<b>'+dshort(x.d50_date)+'</b> <span class="mute">~'+fmt(x.d50,0)+'d</span>':'–'}<br><span class="mute">+$${fmt(x.credit*50,0)}</span></td><td>${fmt(x.be)}</td>
   <td>${x.put_wall==null?'–':x.put_wall}${x.below_wall?'<br><span class="call">short ≤ wall</span>':''}</td><td>${x.kind==='ETF'?'<span class="mute">ETF</span>':x.earnings?esc(x.earnings.slice(5)):(r.earnings_ok?'<span class="mute">none ≤50d</span>':'<span class="amb">unknown</span>')}</td><td>${fmt(x.rank,1)}</td><td style="text-align:left;font-size:12px">${(x.miss||[]).map(m=>`<span class="put">${esc(m)}</span>`).concat((x.notes||[]).map(n=>`<span class="amb">${esc(n)}</span>`)).join('<br>')||'<span class="call">✓ clean</span>'}</td></tr>`});h+='</table>'}
 if(pu.length){h+=`<h3 style="margin:10px 0 4px">Short puts (cash-secured)${dirTag('CSP')}</h3><table><tr><th>#</th><th>Ticker</th><th>Price · RSI</th><th>Momentum</th><th>Expiration</th><th>Sell put</th><th class="srt" data-s="prem">Premium (bid)</th><th class="srt" data-s="cap">Cash secured</th><th class="srt" data-s="ret">Return</th><th class="srt" data-s="pop">POP</th><th class="srt" data-s="fast">Hits 50%</th><th>When 50%</th><th>Breakeven</th><th>Put wall</th><th>Earnings</th><th>Rank</th><th>${relaxed?'Outside your selections / watch':'Watch'}</th></tr>`;
  pu.forEach((x,i)=>{h+=`<tr><td>${i+1}</td><td><a href="?t=${x.ticker}" onclick="go('${x.ticker}');return false"><b>${x.ticker}</b></a> ${STARS(x.stars)} <span data-bdg="${x.ticker}">${badgeHtml(x.ticker)}</span><br><span class="mute">${esc(x.kind)} · ${esc(x.sector)}</span><br><a href="#" class="mute" onclick="calcOpen({tk:'${x.ticker}',strat:'csp',exp:'${x.exp}',k1:${x.short}});return false">calc</a></td>
   <td>${LP(x.ticker,x.price,{sp:[x.short]})}<br><span class="mute">RSI ${x.rsi==null?'–':fmt(x.rsi,0)}</span></td>
   <td><span class="${x.rs1<0?'put':'call'}">${x.rs1>=0?'+':''}${fmt(x.rs1,1)}%</span> <span class="mute">1M vs SPY</span><br>${chip(x.quad)}</td>
   <td>${dshort(x.exp)}<br><span class="mute">${x.dte}d</span></td><td><b>${x.short}P</b><br><span class="mute">${fmt(x.otm,1)}% OTM · Δ ${fmt(x.sdelta)}</span></td>
   <td>${fmt(x.credit)} <span class="mute">(${fmt(x.nat)})</span><br><b>$${fmt(x.credit*100,0)}</b></td><td>$${fmt(x.cash,0)}</td>
   <td>${fmt(x.roc,2)}%<br><span class="mute">${fmt(x.ann,0)}%/yr</span></td><td><b>${fmt(x.pop,1)}%</b></td><td>${x.p50==null?'–':'<b>'+fmt(x.p50,0)+'%</b>'}</td>
   <td>${x.d50_date?'<b>'+dshort(x.d50_date)+'</b> <span class="mute">~'+fmt(x.d50,0)+'d</span>':'–'}</td><td>${fmt(x.be)}</td>
   <td>${x.put_wall==null?'–':x.put_wall}${x.below_wall?'<br><span class="call">strike ≤ wall</span>':''}</td><td>${x.kind==='ETF'?'<span class="mute">ETF</span>':x.earnings?esc(x.earnings.slice(5)):(r.earnings_ok?'<span class="mute">none ≤50d</span>':'<span class="amb">unknown</span>')}</td><td>${fmt(x.rank*100,1)}</td><td style="text-align:left;font-size:12px">${(x.miss||[]).map(m=>`<span class="put">${esc(m)}</span>`).concat((x.notes||[]).map(n=>`<span class="amb">${esc(n)}</span>`)).join('<br>')||'<span class="call">✓ clean</span>'}</td></tr>`});h+='</table><p class="mute">Short put = one leg, cash-secured (strike × 100 held as collateral). If assigned you buy 100 shares at the strike.</p>'}
 $('#scres').innerHTML=h;loadBadges(top.map(x=>x.ticker),()=>document.querySelectorAll('[data-bdg]').forEach(e=>e.innerHTML=badgeHtml(e.dataset.bdg)));
 rotRender()}
async function scLoad(){try{const j=await (await fetch('/api/scan')).json();SC.st=j.status;SC.sg=j.sector_gex||null;SC.gt=j.gex_trends||{};if(j.results)SC.res=j.results;scRender();if(SC.st.running)setTimeout(scLoad,2000)}catch(e){$('#scnote').textContent='· could not load scan results: '+e.message}}
$('#scrun').onclick=async()=>{try{const j=await post('/api/scan');SC.st=j.status;scRender();setTimeout(scLoad,1500)}catch(e){$('#scnote').textContent='· '+e.message}};
const scSave=()=>{try{localStorage.setItem('walls_scan',JSON.stringify({per:SC.per}))}catch(e){}};
$('#scres').addEventListener('click',e=>{const th=e.target.closest('th.srt');if(!th)return;GF.sort=th.dataset.s;gfSave();gfRefresh()});
document.addEventListener('change',e=>{const el=e.target.closest('[data-scper]');if(!el)return;SC.per=+el.value;scSave();scRender()});
scLoad();



// ---- tabs ----
// ---- sell puts tab ----
const PWIN={dte0:'0 DTE',dte5:'1–5 DTE',dte14:'6–14 DTE',dte45:'15–45 DTE',dte90:'46–90 DTE',all:'All'};
let PF={win:'dte5',pop:80,prem:15,roc:1,ann:0,cash:0,sort:'pc',earn:false,only:false};
try{Object.assign(PF,JSON.parse(localStorage.getItem('walls_pf')||'{}'))}catch(e){}
try{if(!localStorage.getItem('walls_pf_v2')){PF.sort='pc';localStorage.setItem('walls_pf_v2','1')}}catch(e){}
let PT={res:null,st:{},loaded:false};
const pfSave=()=>{try{localStorage.setItem('walls_pf',JSON.stringify(PF))}catch(e){}};
const pwList=()=>(W&&W.config&&W.config.puts_watchlist)||[];
function ptCands(it){return Array.isArray(it.puts)?it.puts:['dte0','dte5','dte14','dte45','dte90'].flatMap(k=>it[k]||[])}
const ptNorm=(it,c)=>({it,c,exp:c.exp,strat:'CSP',price:it.price,pop:c.pop,ret:c.roc,cap:c.cash,prem:c.prem,flag:!!(it.earnings&&it.earnings<=c.exp),best:c.roc*(c.pop/100)*(1-Math.min(c.slip||0,50)/200),p50:c.p50,dte:c.dte});
function ptBest(it){const L=gfSort(gfFilter(ptCands(it).map(c=>ptNorm(it,c)),{sec:'puts'}));if(!L.length)return null;const x=L[0];return {...x.c,rank:x.best,earn_cross:x.flag,stars:starsOf(x.pop/100,x.ret/100,0.15)}}
function ptRow(x,i){const c=x.c;return `<tr${x.watch?' class="wrow"':''}><td>${i==null?'':i+1}</td><td><a href="?t=${x.ticker}" onclick="tab('main');go('${x.ticker}');return false"><b>${x.ticker}</b></a> ${setupSig(c,x.trend)}${STARS(c.stars)}${x.watch?' <span class="chip acc" title="on your put watchlist">☆ watch</span>':''}<br><span class="mute">${LP(x.ticker,x.price,{sp:[c.short]})} · RSI ${x.rsi==null?'–':fmt(x.rsi,0)}</span></td>
 <td>${dshort(c.exp)}<br><span class="mute">${c.dte}d</span></td><td><b>${fmtK(c.short)}P</b><br><span class="mute">${fmt(c.otm,1)}% OTM · Δ ${fmt(c.sdelta)}</span></td>
 <td><b>$${fmt(c.cash,0)} → +$${fmt(c.prem,0)}</b><br><span class="mute">mid ${fmt(c.credit)} · bid ${fmt(c.bid)} (+$${fmt(c.bid*100,0)})</span></td>
 <td><b>${fmt(c.roc,2)}%</b><br><span class="mute">${fmt(c.roc_day,2)}%/day · ${fmt(c.ann,0)}%/yr</span></td><td><b>${fmt(c.pop,1)}%</b></td>
 <td>${fmt(c.be)}<br><span class="mute">${fmt((c.be/x.price-1)*100,1)}%</span></td><td>${c.p50==null?'<span class="mute">–</span>':'<b>'+fmt(c.p50,0)+'%</b> '+(c.d50_date?dshort(c.d50_date):'')}</td>
 <td>${c.put_wall==null?'–':fmtK(c.put_wall)}${c.below_wall?'<br><span class="call">≤ wall</span>':''}</td>
 <td>${x.earnings?(c.earn_cross?'<span class="amb">⚠ '+esc(x.earnings.slice(5))+'</span>':esc(x.earnings.slice(5))):'<span class="mute">none ≤50d</span>'}</td><td>${trkBtn({tk:x.ticker,exp:c.exp,strat:'CSP',legs:[{cp:'P',K:c.short,side:-1,n:1}],what:'sell '+c.short+'P',kind:'credit',price:c.credit,capital:c.cash,rec_p:c.pop/100,src:'Sell puts'})}</td></tr>`}
const PTH='<tr><th>#</th><th>Stock</th><th>Expiration</th><th>Sell put</th><th>Cash → premium</th><th>Return</th><th>POP</th><th>Breakeven</th><th>Hits 50%</th><th>Put wall</th><th>Earnings</th><th></th></tr>';
function ptRender(){const st=PT.st||{},r=PT.res;
 $('#ptnote').textContent=st.running?`· scanning… ${st.progress||''}`:st.error?`· error: ${st.error}`:r?`· last scan ${r.ts}${r.scope==='watchlist'?' (watchlist only)':''}`:'· not run yet';
 {const all=[];for(const it of Object.values((r&&r.items)||{}))for(const c of ptCands(it))all.push(ptNorm(it,c));
  $('#ptufb').innerHTML=ufb('puts',all,{asof:r&&r.ts?r.ts:'',stale:r&&r.stale,src:srcOf(r&&r.items),strats:['CSP'],extra:`<label><input type="checkbox" data-pfonly${PF.only?' checked':''}> watchlist only</label>`,note:'Return = premium ÷ cash secured for the trade'})}
 // watchlist chips + table
 const wl=pwList(),items=(r&&r.items)||{};
 $('#pwchips').innerHTML=wl.length?wl.map(t=>`<span class="pchip">${t}<button class="x" onclick="pwRemove('${t}')" title="Remove">×</button></span>`).join(''):'<span class="mute">No tickers yet — add the stocks you like to sell puts on.</span>';
 if(wl.length){let h='<table>'+PTH;wl.forEach(t=>{const it=items[t];
   if(!it){h+=`<tr><td></td><td><b>${t}</b></td><td colspan="9" class="mute">not scanned yet — press Scan watchlist</td></tr>`;return}
   const b=ptBest(it);if(b){h+=ptRow({...it,c:b,watch:false})}
   else{const all=ptCands(it).filter(c=>c.pop>=GF.pop&&wkPass(c.exp));const mx=all.reduce((a,c)=>c.prem>a.prem?c:a,{prem:0});
    h+=`<tr><td></td><td><b>${t}</b><br><span class="mute">${LP(t,it.price)} · RSI ${it.rsi==null?'–':fmt(it.rsi,0)}</span></td><td colspan="9" class="mute">nothing meets your selections${mx.prem?` — best at ${GF.pop}%+ POP: ${fmtK(mx.short)}P ${dshort(mx.exp)}, $${fmt(mx.cash,0)} → +$${fmt(mx.prem,0)} (${fmt(mx.roc,2)}%)`:''}</td></tr>`}});
  $('#pwres').innerHTML=h+'</table>'}else $('#pwres').innerHTML='';
 // screener
 if(!r){$('#ptres').innerHTML='<p class="mute">Press a Scan button (about a minute for the full scan).</p>';return}
 const rows=[];for(const it of Object.values(items)){if(PF.only&&!it.watch)continue;const b=ptBest(it);if(b)rows.push({...it,c:b})}
 rows.sort((a,b)=>b.c.rank-a.c.rank);rows.splice(GF.n);
 let wd=6;try{wd=new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'})).getDay()}catch(e){}
 if(!rows.length){$('#ptres').innerHTML='<p class="mute">Nothing passes your selections. Most stocks under $30 only list Friday expirations. Try “All” expirations, lower Min return / Min chance, or raise Max capital.</p>';return}
 $('#ptres').innerHTML=(GF.wk==='0dte'?'<div class="al warn" style="margin:0 0 8px"><b>0 DTE</b>Expiring today; POP uses the minutes left to 4:00 PM ET. Quotes are ~15 min delayed — check the live bid.</div>':'')+'<table>'+PTH+rows.map((x,i)=>ptRow(x,i)).join('')+'</table>'}
async function ptLoad(){PT.loaded=true;try{const j=await (await fetch('/api/csp')).json();PT.st=j.status;if(j.results)PT.res=j.results;ptRender();if(PT.st.running)setTimeout(ptLoad,2500)}catch(e){$('#ptnote').textContent='· could not load: '+e.message}}
async function ptScan(scope){try{const j=await post('/api/csp',{scope});PT.st=j.status;ptRender();setTimeout(ptLoad,2000)}catch(e){$('#ptnote').textContent='· '+e.message}}
async function pwSave(list){try{const j=await post('/api/watch',{puts_watchlist:list});W.config=j.config;ptRender()}catch(e){$('#pwmsg').textContent=e.message}}
function pwRemove(t){pwSave(pwList().filter(x=>x!==t))}
$('#pwf').onsubmit=ev=>{ev.preventDefault();const ts=$('#pwt').value.toUpperCase().split(/[\s,]+/).filter(Boolean);if(!ts.length)return;
 pwSave([...pwList(),...ts.filter(t=>!pwList().includes(t))]);$('#pwt').value='';$('#pwmsg').textContent='Added — press Scan watchlist to price them.'};
$('#ptscanw').onclick=()=>ptScan('watchlist');$('#ptscana').onclick=()=>ptScan('all');
document.addEventListener('change',e=>{const el=e.target.closest('[data-pfonly]');if(!el)return;PF.only=el.checked;pfSave();ptRender()});
$('#pmax').onchange=async e=>{try{const j=await post('/api/watch',{puts_max_price:+e.target.value});W.config=j.config}catch(err){$('#pwmsg').textContent=err.message}};
$('#petf').onchange=async e=>{try{const j=await post('/api/watch',{puts_etfs:e.target.checked});W.config=j.config}catch(err){$('#pwmsg').textContent=err.message}};
$('#pday').onchange=async e=>{try{const j=await post('/api/watch',{puts_daily:e.target.checked});W.config=j.config}catch(err){$('#pwmsg').textContent=err.message}};
$('#pscr').onchange=async e=>{try{const j=await post('/api/watch',{puts_screener:e.target.checked});W.config=j.config}catch(err){$('#pwmsg').textContent=err.message}};
setInterval(()=>{if(W&&W.config&&$('#pmax')&&document.activeElement!==$('#pmax')){$('#pmax').value=W.config.puts_max_price??30;$('#pscr').checked=W.config.puts_screener!==false;$('#petf').checked=W.config.puts_etfs!==false;$('#pday').checked=W.config.puts_daily!==false}},2000);


// ---- dark pool & insiders tab ----
let FL={tk:'',mkt:null};
const big=v=>v==null?'–':Math.abs(v)>=1e9?(v/1e9).toFixed(2)+'B':Math.abs(v)>=1e6?(v/1e6).toFixed(2)+'M':Math.abs(v)>=1e3?(v/1e3).toFixed(0)+'K':String(Math.round(v));
const usd2=v=>v==null?'–':(v<0?'−':'')+'$'+big(Math.abs(v));
function flChart(S){if(!S||S.length<3)return'';const Wd=900,H=170,pl=40,pr=40,pt=12,pb=22,iw=Wd-pl-pr,ih=H-pt-pb,n=S.length,step=iw/n;
 const mv=Math.max(...S.map(x=>x.off||0),1),sp=S.map(x=>x.short_pct).filter(v=>v!=null),lo=Math.max(0,Math.min(...sp)-5),hi=Math.min(100,Math.max(...sp)+5);
 const y=v=>pt+(hi-v)/(hi-lo)*ih,base=sp.slice(0,-1).slice(-20),avg=base.reduce((a,b)=>a+b,0)/Math.max(base.length,1);
 let g='';S.forEach((x,i)=>{const h=(x.off||0)/mv*ih*0.55;g+=`<rect x="${pl+i*step+step*0.15}" y="${pt+ih-h}" width="${step*0.7}" height="${h}" fill="var(--accent)" opacity=".28"><title>${x.date}: off-exchange ${big(x.off)} sh${x.off_pct!=null?' ('+fmt(x.off_pct,0)+'% of all volume)':''}</title></rect>`});
 g+=`<line x1="${pl}" x2="${Wd-pr}" y1="${y(avg)}" y2="${y(avg)}" stroke="var(--mute)" stroke-dasharray="4 4"/><text x="${Wd-pr+4}" y="${y(avg)+4}" class="ax">avg ${fmt(avg,0)}%</text>`;
 let path='';S.forEach((x,i)=>{if(x.short_pct==null)return;path+=(path?'L':'M')+(pl+i*step+step/2)+','+y(x.short_pct)});
 g+=`<path d="${path}" fill="none" stroke="var(--fib)" stroke-width="2"/>`;
 S.forEach((x,i)=>{if(x.short_pct==null)return;const hiC=x.short_pct>avg;g+=`<circle cx="${pl+i*step+step/2}" cy="${y(x.short_pct)}" r="3.2" fill="${hiC?'var(--call)':'var(--put)'}"><title>${x.date}: short ${fmt(x.short_pct,1)}% of off-exchange volume</title></circle>`});
 [lo,(lo+hi)/2,hi].forEach(t=>{g+=`<text x="${pl-6}" y="${y(t)+4}" class="ax" text-anchor="end">${fmt(t,0)}%</text>`});
 [0,Math.floor(n/2),n-1].forEach(i=>{g+=`<text x="${pl+i*step+step/2}" y="${H-5}" class="ax" text-anchor="middle">${S[i].date.slice(5)}</text>`});
 return `<svg viewBox="0 0 ${Wd} ${H}" role="img" aria-label="off-exchange short percentage">${g}</svg><div class="legend"><span><i class="bx" style="background:var(--fib)"></i>short % of off-exchange (dark pool) volume — green above / red below its average</span><span><i class="bx" style="background:var(--accent);opacity:.4"></i>off-exchange volume</span></div>`}
function insTable(rows,showTk){if(!rows||!rows.length)return'<p class="mute">No Form 4 trades listed.</p>';
 return '<div class="tw"><table><tr><th>Filed</th><th>Trade</th>'+(showTk?'<th>Ticker</th>':'')+'<th>Insider</th><th>Title</th><th>Type</th><th>Price</th><th>Shares</th><th>Value</th><th>Own Δ</th></tr>'+
  rows.map(x=>`<tr><td>${x.sec?`<a href="${esc(x.sec)}" target="_blank" rel="noopener" title="SEC Form 4">${esc(x.filed)}</a>`:esc(x.filed)}</td><td>${esc(x.date)}</td>${showTk?`<td><a href="#" onclick="flGo('${esc(x.ticker)}');return false"><b>${esc(x.ticker)}</b></a></td>`:''}<td>${esc(x.who)}${x.n_ins?` <span class="mute">(${esc(x.n_ins)} insiders)</span>`:''}</td><td class="mute">${esc(x.title)}</td>
  <td class="${/^P/.test(x.type)?'call':/^S/.test(x.type)?'put':'mute'}"><b>${esc(x.type)}</b></td><td>${x.price==null?'–':fmt(x.price)}</td><td>${x.qty==null?'–':big(x.qty)}</td><td class="${(x.value||0)<0?'put':'call'}">${usd2(x.value)}</td><td class="mute">${esc(x.own_chg||'')}</td></tr>`).join('')+'</table></div>'}
async function flGo(t){t=(t||'').trim().toUpperCase();if(!t)return;FL.tk=t;if($('#tabFlow').hidden)tab('flow');$('#flt').value=t;$('#flres').innerHTML=`<p class="mute">Loading ${esc(t)} from FINRA and SEC filings…</p>`;
 $('#flx').href='https://xynth.com/';$('#floi').href='http://openinsider.com/'+encodeURIComponent(t);$('#flsec').href='https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK='+encodeURIComponent(t)+'&type=4&owner=include&count=40';
 try{const r=await (await fetch('/api/flow?t='+encodeURIComponent(t))).json();if(r.error)throw new Error(r.error);flRender(r)}catch(e){$('#flres').innerHTML='<p class="err">'+esc(e.message)+'</p>'}}
function flRender(r){const st=r.stats,I=r.insiders||{},A=r.ats||{},SI=r.si||{},sr=(SI.rows||[]),sl=sr[sr.length-1];
 const z=st?st.z:null,zc=z==null?'mute':z>=1.5?'call':z<=-1.5?'put':'mute',zl=z==null?'n/a':z>=1.5?'unusually high':z<=-1.5?'unusually low':'normal';
 const net=(I.buy_value||0)+(I.sell_value||0),ic=I.error?['mute','n/a']:I.buyers&&I.buy_value>Math.abs(I.sell_value||0)?['call','net buying']:I.sellers?['put','net selling']:['mute','no open-market trades'];
 const wk=(A.weeks||[]),lw=wk[wk.length-1],pw=wk[wk.length-2];
 const tile=(lab,val,sub,c)=>`<div class="gt b-${c}"><div class="l">${lab}</div><div class="v">${val}</div>${sub?`<div class="l">${sub}</div>`:''}</div>`;
 let h=`<div class="head"><h1 style="font-size:22px">${esc(r.ticker)}</h1>${r.price!=null?`<span class="big">${fmt(r.price)}</span>`:''}<span>RSI(14) <b>${r.rsi==null?'n/a':fmt(r.rsi,1)}</b></span><button class="ghost" onclick="tab('main');go('${esc(r.ticker)}')">Walls &amp; spreads →</button></div><div class="gtiles">`;
 h+=tile('Dark pool short % ('+(r.asof||'').slice(5)+')',st?fmt(st.short_pct,1)+'%':'–',st?`20-day avg ${fmt(st.avg20,1)}% · <span class="${zc}">${zl}</span> (z ${fmt(st.z,1)})`:'needs 6+ sessions',zc);
 h+=tile('Off-exchange share of volume',st&&st.off_pct!=null?fmt(st.off_pct,0)+'%':'–','dark pools + wholesalers, latest session with full volume','mute');
 h+=tile('Off-exchange volume vs 20-day avg',st&&st.off_ratio!=null?fmt(st.off_ratio,2)+'×':'–',st&&st.off_ratio>=1.8?'<span class="amb">volume spike</span>':'','amb');
 h+=tile('ATS (dark pool) shares, week of '+(lw?lw.week.slice(5):'–'),lw?big(lw.shares):'–',lw?`avg trade ${fmt(lw.avg,0)} sh${pw?' · '+(lw.shares>=pw.shares?'+':'')+fmt((lw.shares/pw.shares-1)*100,0)+'% vs prior wk':''} · FINRA, ~2-wk lag`:(A.error?esc(A.error):'no ATS data'),'fib');
 h+=tile('Short interest (settled '+(sl?sl.date.slice(5):'–')+')',sl&&sl.short!=null?big(sl.short)+' sh':'–',sl?`${sl.prev?((sl.short>=sl.prev?'+':'')+fmt((sl.short/sl.prev-1)*100,0)+'% vs prior · '):''}${sl.dtc!=null?fmt(sl.dtc,1)+' days to cover':''} · FINRA, twice a month`:(SI.error?esc(SI.error):'no FINRA short-interest data'),sl&&sl.prev?(sl.short>sl.prev*1.1?'put':sl.short<sl.prev*0.9?'call':'mute'):'mute');
 h+=tile('Insiders, last 90 days',ic[1],I.error?esc(I.error):`buys ${usd2(I.buy_value)} (${I.buyers||0}) · sells ${usd2(I.sell_value)} (${I.sellers||0})`,ic[0]);
 h+=`</div><h3>Off-exchange (dark pool) short %, last ${r.finra_daily.length} sessions</h3>${flChart(r.finra_daily)}`;
 if(wk.length)h+=`<details><summary class="mute">ATS weekly volume (${wk.length} weeks) and top dark pools</summary><div class="tw"><table><tr><th>Week of</th><th>ATS shares</th><th>Trades</th><th>Avg trade</th></tr>${wk.slice().reverse().map(w=>`<tr><td>${w.week}</td><td>${big(w.shares)}</td><td>${big(w.trades)}</td><td>${fmt(w.avg,0)}</td></tr>`).join('')}</table></div>
  ${(A.venues||[]).length?`<div class="tw"><table><tr><th>Venue (latest week)</th><th>Shares</th><th>Trades</th></tr>${A.venues.map(v=>`<tr><td>${esc(v.name||'')}</td><td>${big(v.shares)}</td><td>${big(v.trades)}</td></tr>`).join('')}</table></div>`:''}</details>`;
 h+=`<h3>Insider trades (SEC Form 4)</h3>${I.error?'<p class="err">'+esc(I.error)+'</p>':insTable((I.rows||[]).slice(0,8),false)+((I.rows||[]).length>8?`<details><summary class="mute">Show all ${I.rows.length}</summary>${insTable(I.rows.slice(8),false)}</details>`:'')}`;
 h+=`<div id="flbm"><h3>Big money — institutions (13F) &amp; 5%+ owners (13D/13G)</h3><p class="mute">Loading SEC filings…</p></div>`;
 $('#flres').innerHTML=h;bmLoad(r.ticker)}
let BM={t:0};
async function bmLoad(t){clearTimeout(BM.t);try{const r=await (await fetch('/api/bigmoney?t='+encodeURIComponent(t))).json();if(FL.tk!==t||!$('#flbm'))return;bmRender(r);
 const st=(r.status||{}).state;if(st==='building'||(r.f13&&r.f13.status&&r.f13.status.state==='building'))BM.t=setTimeout(()=>bmLoad(t),15000)}catch(e){if($('#flbm'))$('#flbm').innerHTML='<h3>Big money</h3><p class="err">'+esc(e.message)+'</p>'}}
function bmRebuild(){post('/api/bigmoney/build',{}).then(()=>bmLoad(FL.tk)).catch(e=>alert(e.message))}
function bmRender(r){const F=r.f13||{},O=r.owners||{},so=r.shares_out,pct=v=>so?fmt(v/so*100,v/so*100<1?2:1)+'%':'–';
 const qd=d=>d?new Date(d+'T12:00:00').toLocaleDateString(undefined,{month:'short',day:'numeric',year:'numeric'}):'–';
 const tile=(lab,val,sub,c)=>`<div class="gt b-${c}"><div class="l">${lab}</div><div class="v">${val}</div>${sub?`<div class="l">${sub}</div>`:''}</div>`;
 const stc={added:'call',new:'call',cut:'put',exited:'put',same:'mute','no prior 13F':'mute'};
 const chg=x=>x.dsh==null?'<span class="mute">n/a</span>':`<span class="${x.dsh>0?'call':x.dsh<0?'put':'mute'}">${x.dsh>0?'+':''}${big(x.dsh)}${x.pct_chg!=null?` (${x.pct_chg>0?'+':''}${fmt(x.pct_chg,0)}%)`:''}</span>`;
 const tbl=(rows,showPrev)=>!rows||!rows.length?'<p class="mute">None.</p>':'<div class="tw"><table><tr><th>Manager</th><th>Shares</th><th>Value</th><th>% of shares out</th>'+(showPrev?'<th>Prior qtr</th>':'')+'<th>Change</th><th>Status</th></tr>'+
  rows.map(x=>`<tr><td>${esc(x.name)}</td><td>${big(x.sh)}</td><td>${usd2(x.val)}</td><td>${pct(x.sh||x.sh0)}</td>${showPrev?`<td>${big(x.sh0)}</td>`:''}<td>${chg(x)}</td><td class="${stc[x.status]||'mute'}"><b>${esc(x.status)}</b></td></tr>`).join('')+'</table></div>';
 let h=`<div class="shead"><h3 style="margin:0">Big money — institutions (13F) &amp; 5%+ owners (13D/13G)</h3>${r.price!=null?`<span class="mute">${esc(r.ticker)} ${fmt(r.price)} · RSI(14) ${r.rsi==null?'n/a':fmt(r.rsi,1)}</span>`:''}</div>`;
 if(r.error){$('#flbm').innerHTML=h+'<p class="err">'+esc(r.error)+'</p>';return}
 const S=r.status||{};
 if(F.status||!F.period){h+=F.error?`<p class="mute">13F: ${esc(F.error)}</p>`:`<p class="amb">13F index: ${esc(S.msg||'not built yet')}${S.state==='building'?' — first build downloads two quarters from SEC (~200 MB, a few minutes). This panel refreshes itself.':''}</p>${S.state==='error'?'<button class="ghost" onclick="bmRebuild()">Retry</button>':''}`}
 else{const net=F.net_dsh,P=F.puts||{},C=F.calls||{};
  h+=`<div class="gtiles">`+tile(`13F holders (qtr ended ${qd(F.period)})`,F.n_holders.toLocaleString(),`prior qtr ${F.n_prev.toLocaleString()} · ${F.n_new} new · ${F.n_exit} exited`,'fib')+
   tile('Shares held by 13F filers',big(F.sh),so?`${pct(F.sh)} of ${big(so)} shares out (${esc(r.shares_out_date||'')})`:usd2(F.val),'mute')+
   tile('Net change vs prior quarter',`<span class="${net>0?'call':net<0?'put':'mute'}">${net>0?'+':''}${big(net)} sh</span>`,`${F.n_add} added · ${F.n_cut} cut (managers that filed both quarters)`,net>0?'call':net<0?'put':'mute')+
   tile('Options held (13F, notional)',`calls ${usd2(C.val)} · puts ${usd2(P.val)}`,`prior: calls ${usd2(C.val0)} · puts ${usd2(P.val0)}`,(C.val||0)>=(P.val||0)?'call':'put')+`</div>`;
  h+=`<h4 style="margin:8px 0 4px">Top holders</h4>${tbl(F.top,true)}`;
  h+=`<details><summary><b>Biggest adds</b> <span class="mute">(${F.adds.length})</span></summary>${tbl(F.adds,true)}</details>`+
   `<details><summary><b>Biggest cuts</b> <span class="mute">(${F.cuts.length})</span></summary>${tbl(F.cuts,true)}</details>`+
   `<details><summary><b>New positions</b> <span class="mute">(${F.n_new})</span></summary>${tbl(F.new,false)}</details>`+
   `<details><summary><b>Exited</b> <span class="mute">(${F.n_exit})</span></summary>${tbl(F.exits,true)}</details>`;
  const ot=(L,k)=>!L.top||!L.top.length?'':`<details><summary><b>${k} holders</b> <span class="mute">(${L.n})</span></summary><div class="tw"><table><tr><th>Manager</th><th>Underlying shares</th><th>Notional</th><th>Prior qtr</th></tr>${L.top.map(x=>`<tr><td>${esc(x.name)}</td><td>${big(x.sh)}</td><td>${usd2(x.val)}</td><td>${x.val0==null?'n/a':usd2(x.val0)}</td></tr>`).join('')}</table></div></details>`;
  h+=ot(C,'Call')+ot(P,'Put');
  h+=`<p class="mute" style="font-size:12px">SEC Form 13F data set: ${esc(F.issuer)} ${esc(F.cls)} · CUSIP ${esc(F.cusip)} · ${F.filers.toLocaleString()} managers filed. 13F shows long positions of managers with $100M+ only, filed up to 45 days after quarter end; no shorts. Options are reported as the underlying's value, not premium, and can be hedges. Positions under $${big(F.min_value)} are not indexed, so “new” can mean it grew past that.</p>`}
 h+=`<h4 style="margin:10px 0 4px">5%+ owners — Schedule 13D (activist) / 13G (passive), last 2 years</h4>`;
 if(O.error)h+=`<p class="mute">${esc(O.error)}</p>`;
 else if(!(O.rows||[]).length)h+='<p class="mute">No 13D/13G filings in the last 2 years.</p>';
 else h+='<div class="tw"><table><tr><th>Filer</th><th>Latest form</th><th>Filed</th><th>% of class</th><th>Shares</th><th>Filings</th></tr>'+O.rows.map(x=>`<tr><td>${esc(x.filer)}${x.activist?' <span class="amb" title="13D = intends to influence the company">activist</span>':''}</td><td><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.form)}</a></td><td>${esc(x.filed)}</td><td>${x.pct==null?'–':`<b class="${x.pct>=5?'':'mute'}">${fmt(x.pct,x.pct<1?2:1)}%</b>${x.pct<5?' <span class="mute">(below 5%)</span>':''}`}</td><td>${x.shares==null?'–':big(x.shares)}</td><td>${x.n}</td></tr>`).join('')+'</table></div><p class="mute" style="font-size:12px">From SEC EDGAR full-text search; % and shares read from the filing itself (blank for older text filings that couldn’t be parsed). Not financial advice.</p>';
 $('#flbm').innerHTML=h}
function uTable(list,title,note,open){if(!list||!list.length)return`<h3>${title}</h3><p class="mute">None today.</p>`;
 return `<details${open?' open':''}><summary><b>${title}</b> <span class="mute">(${list.length})</span></summary><p class="mute" style="margin:0 0 4px">${note}</p><div class="tw"><table><tr><th>Ticker</th><th>Group</th><th>Short % (off-exch.)</th><th>20-day avg</th><th>z</th><th>Off-exch. volume</th><th>vs avg</th></tr>`+
  list.map(u=>`<tr><td><a href="#" onclick="flGo('${u.ticker}');return false"><b>${u.ticker}</b></a></td><td class="mute">${esc(u.sector)}</td><td><b>${fmt(u.short_pct,1)}%</b></td><td>${fmt(u.avg20,1)}%</td><td class="${u.z>0?'call':'put'}">${u.z>0?'+':''}${fmt(u.z,1)}</td><td>${big(u.off_vol)}</td><td>${u.vol_ratio==null?'–':fmt(u.vol_ratio,1)+'×'}</td></tr>`).join('')+'</table></div></details>'}
async function flMarket(){$('#flmkt').innerHTML='<p class="mute">Loading FINRA files and the latest Form 4 filings (first load of the day downloads ~25 FINRA files)…</p>';
 try{const m=await (await fetch('/api/flow/market')).json();if(m.error)throw new Error(m.error);FL.mkt=m;
  $('#flmkt').innerHTML=`<p class="mute">FINRA off-exchange data as of ${m.asof||'–'} (${m.sessions} sessions) · scanner universe + your watchlists</p>`+
   uTable(m.high_short,'Dark pool short % unusually HIGH','Commonly read as dealers selling short to fill buyers (a bullish lean in the SqueezeMetrics “DIX” idea). Not a signal on its own.',1)+
   uTable(m.low_short,'Dark pool short % unusually LOW','Fewer short-side fills than usual — often read as weaker buying demand.')+
   uTable(m.vol_spike,'Off-exchange volume spikes (≥1.8× 20-day avg)','Big dark-pool / wholesaler volume — check news and the ticker panel.')+
   `<details open><summary><b>Latest insider purchases ≥ $25K</b> <span class="mute">(${(m.insider_buys||[]).length})</span></summary>${insTable((m.insider_buys||[]).slice(0,15),true)}</details><details><summary><b>Cluster buys (several insiders)</b> <span class="mute">(${(m.cluster_buys||[]).length})</span></summary>${insTable(m.cluster_buys,true)}</details>`}
 catch(e){$('#flmkt').innerHTML='<p class="err">'+esc(e.message)+'</p>'}}
$('#flf').onsubmit=ev=>{ev.preventDefault();flGo($('#flt').value)};$('#flmr').onclick=()=>flMarket();


// ---- bull call spread setups (ticker lookup) ----
let BT={win:'swing',prof:'balanced'};try{Object.assign(BT,JSON.parse(localStorage.getItem('walls_bcs')||'{}'))}catch(e){}
const BWIN={short:'7–29 DTE',swing:'30–89 DTE',mid:'90–269 DTE'},BPROF={conservative:['Conservative','Buy an in-the-money call (delta 0.58+), highest chance of profit · skips trades that hold through earnings'],balanced:['Balanced','Best chance of profit × reward/risk · skips trades that hold through earnings'],aggressive:['Aggressive','Cheaper, more upside: reward/risk 1.5:1 or better, lower odds · earnings allowed']};
function bcsSet(k,v){BT[k]=v;try{localStorage.setItem('walls_bcs',JSON.stringify(BT))}catch(e){}bcsDraw()}
function bcsDraw(){const el=$('#bcs'),r=CC.r;if(!el||!r)return;const B=r.bull_call,d2=v=>fmt(v,2),spot=px(r);
 let h=`<div class="shead"><h2>Bull call spread setups</h2><span class="mute">bullish · buy a call, sell a higher call · ${esc(r.ticker)} ${fmt(spot)} · RSI(14) ${r.rsi==null?'n/a':fmt(r.rsi,1)}</span></div>`;
 if(!B||B.error){el.innerHTML=h+`<p class="mute">Not available${B&&B.error?': '+esc(B.error):''}. Look up the ticker again to compute it.</p>`;return}
 h+=`<div class="mute" style="margin:-2px 0 8px">${BPROF[GF.prof][1]}</div>`;
 const W=mergeWin(B),P=gfSort(gfFilter(((W.profiles||{})[GF.prof]||[]).map(c=>({c,exp:c.exp,strat:'BCS',pop:c.pop,ret:(c.rr||0)*100,cap:c.cost,prem:c.maxprofit,best:c.rank||c.score||0,p50:c.p_up50,dte:c.dte,flag:!!c.earn_cross})),{noWk:true})).map(x=>x.c).slice(0,4);
 const tr=r.trend?r.trend.trend:null,trTxt=tr==null?'':tr>=2?'<span class="call">Trend supports it: price above its 20/50-day averages</span>':'<span class="amb">⚠ Trend is weak (below its 20- or 50-day average) — a bull call needs the stock to rise</span>';
 if(!P.length){el.innerHTML=h+`<p class="mute">${W.n?`No ${wkLabel(GF.wk)} bull call spread meets the ${BPROF[GF.prof][0]} rules${r.strategy&&r.strategy.earnings?` (earnings ${esc(r.strategy.earnings)})`:''} — try another profile or window.`:`No liquid calls for ${wkLabel(GF.wk)} (open interest too low or quotes too wide).`}</p><p class="mute">${trTxt}</p>`;return}
 const c=P[0],flags=[];
 if(c.earn_cross)flags.push(`<span class="amb">⚠ Earnings ${esc(r.strategy&&r.strategy.earnings||'')} before expiration — IV usually drops after the report</span>`);
 if(c.slip>25)flags.push(`<span class="amb">⚠ Wide quotes: mid ${d2(c.debit)} vs natural ${d2(c.nat)} — use a limit order near the mid</span>`);
 if(c.oi<200)flags.push(`<span class="amb">⚠ Thin open interest (${c.oi.toLocaleString()})</span>`);
 if(c.call_wall!=null&&c.long>=c.call_wall)flags.push(`<span class="amb">⚠ The call wall (${fmtK(c.call_wall)}) is at or below your long strike — dealer hedging can cap rallies there</span>`);
 h+=`<div class="pick"><div class="ptitle">Buy <b>${fmtK(c.long)}C</b> / Sell <b>${fmtK(c.short)}C</b> <span class="mute">· ${c.exp} (${c.dte} DTE) · $${fmtK(c.width)} wide</span> <button class="ghost" style="font-size:12px;padding:3px 9px" onclick="calcOpen({tk:'${r.ticker}',strat:'cds',exp:'${c.exp}',k1:${c.long},k2:${c.short}})">Calculator</button> ${trkBtn({tk:r.ticker,exp:c.exp,strat:'BCS',legs:[{cp:'C',K:c.long,side:1,n:1},{cp:'C',K:c.short,side:-1,n:1}],what:'buy '+c.long+'C / sell '+c.short+'C',kind:'debit',price:c.debit,capital:c.debit*100,rec_p:c.pop!=null?c.pop/100:null,src:'Walls & spreads · bull call '+GF.prof})}</div>
 <div class="kpis">
 <div class="kpi"><div class="l">You pay (mid / natural)</div><div class="v">${d2(c.debit)} <span class="mute">/ ${d2(c.nat)}</span></div><div class="l">$${fmt(c.cost,0)} per spread = max loss</div></div>
 <div class="kpi"><div class="l">Max profit</div><div class="v call">$${fmt(c.maxprofit,0)}</div><div class="l">if ${esc(r.ticker)} ≥ ${fmtK(c.short)} at expiration (+${fmt(c.otm_short,1)}%)</div></div>
 <div class="kpi"><div class="l">Reward : risk</div><div class="v">${fmt(c.rr,1)} : 1</div><div class="l">make $${fmt(c.rr,2)} per $1 risked</div></div>
 <div class="kpi"><div class="l">Breakeven</div><div class="v">${d2(c.be)}</div><div class="l">${c.be_pct>=0?'+':''}${fmt(c.be_pct,1)}% from current price</div></div>
 <div class="kpi"><div class="l">Chance of profit</div><div class="v">${fmt(c.pop,0)}%</div><div class="l">full profit ${fmt(c.p_max,0)}% · model est.</div></div>
 <div class="kpi"><div class="l">Chance +50% before exp.</div><div class="v">${c.p_up50==null?'–':fmt(c.p_up50,0)+'%'}</div><div class="l">${c.d_up50_date?'most likely ~'+dshort(c.d_up50_date):''}${c.p_2x!=null?' · doubles '+fmt(c.p_2x,0)+'%':''}</div></div>
 <div class="kpi"><div class="l">Take profit · sell at</div><div class="v">${d2(c.tp50)} <span class="mute">/</span> ${d2(c.tp100)}</div><div class="l">+50% +$${fmt(c.debit*50,0)} · +100% +$${fmt(c.debit*100,0)}</div></div>
 <div class="kpi"><div class="l">Long / short call delta</div><div class="v">${d2(c.ldelta)} / ${d2(c.sdelta)}</div><div class="l">acts like ~${fmt((c.ldelta-c.sdelta)*100,0)} shares now</div></div>
 <div class="kpi"><div class="l">Call wall (this expiration)</div><div class="v">${c.call_wall==null?'–':fmtK(c.call_wall)}</div><div class="l">${c.call_wall==null?'':c.short<=c.call_wall?'<span class="call">short strike at/below the wall ✓</span>':'short strike above the wall'}</div></div>
 </div>${flags.length?'<div class="flags">'+flags.join('<br>')+'</div>':''}</div>`;
 if(P.length>1)h+=`<div class="tw"><table><tr><th>Alternatives</th><th>Expiration</th><th>Cost (nat)</th><th>Max profit</th><th>R:R</th><th>Chance of profit</th><th>Chance +50%</th><th>Breakeven</th><th></th></tr>`+
  P.slice(1).map(a=>`<tr><td>${fmtK(a.long)} / ${fmtK(a.short)}C${a.earn_cross?' <span class="amb" title="earnings before expiration">⚠E</span>':''}</td><td>${a.exp.slice(5)} <span class="mute">${a.dte}d</span></td><td>${d2(a.debit)} <span class="mute">(${d2(a.nat)})</span><br>$${fmt(a.cost,0)}</td><td class="call">$${fmt(a.maxprofit,0)}</td><td>${fmt(a.rr,1)}</td><td>${fmt(a.pop,0)}%</td><td>${a.p_up50==null?'–':fmt(a.p_up50,0)+'%'+(a.d_up50_date?' <span class="mute">'+dshort(a.d_up50_date)+'</span>':'')}</td><td>${d2(a.be)}</td><td><a href="#" onclick="calcOpen({tk:'${r.ticker}',strat:'cds',exp:'${a.exp}',k1:${a.long},k2:${a.short}});return false">calc</a></td></tr>`).join('')+'</table></div>';
 h+=`<p class="mute" style="margin-top:8px">${trTxt}${trTxt?' · ':''}How it works: you pay the debit up front — that is the most you can lose. You make the full width minus the debit if ${esc(r.ticker)} closes at or above the short strike on expiration day. Most traders take profits at +50% to +100% of the debit instead of waiting. Chances are model estimates from CBOE implied volatility (15-min delayed). Not financial advice.</p>`;
 el.innerHTML=h}


// ---- Picks tab (Strategy Scanner output on this PC) ----
let PK={tf:'daily',n:5,st:'',hideCat:false,d:null,loaded:false};try{Object.assign(PK,JSON.parse(localStorage.getItem('walls_pk')||'{}'),{d:null,loaded:false})}catch(e){}
const pkSave=()=>{try{const{d,loaded,...k}=PK;localStorage.setItem('walls_pk',JSON.stringify(k))}catch(e){}};
const PKTF=[['daily','Daily (0–4 days)'],['weekly','Weekly (5–20 days)'],['monthly','Monthly (21–60 days)'],['quarterly','2–8 months'],['leaps','LEAPS (8+ months)']];
async function pkLoad(){PK.loaded=true;try{PK.d=await (await fetch('/api/picks')).json();pkRender();if(PK.d.status&&PK.d.status.running)setTimeout(pkLoad,5000)}catch(e){$('#pknote').textContent='· '+e.message}}
function pkLegs(p){const L=String(p.legs||'').split(/[\/·]/).map(x=>x.trim()).map(x=>{const m=x.match(/([−+-])\s*([\d.]+)\s*([CP])/);return m?{s:m[1]==='+'?1:-1,k:+m[2],t:m[3]}:null}).filter(Boolean);return L}
function pkPlain(p){const L=pkLegs(p),tk=p.ticker,px=p.price,sh=L.find(l=>l.s<0),lg=L.find(l=>l.s>0),ty=sh?sh.t:(lg?lg.t:'');
 const what=L.map(l=>`${l.s<0?'Sell':'Buy'} the ${fmtK(l.k)} ${l.t==='P'?'put':'call'}`).join(', ');
 let win='';if(p.be_lo!=null&&p.be_hi!=null)win=`You profit if ${tk} stays between <b>$${fmt(p.be_lo)}</b> and <b>$${fmt(p.be_hi)}</b> at expiration (now $${fmt(px)}).`;
 else if(p.be_lo!=null)win=`You profit if ${tk} stays above <b>$${fmt(p.be_lo)}</b> at expiration — ${fmt(Math.abs(p.be_lo/px-1)*100,1)}% below today’s $${fmt(px)}.`;
 else if(p.be_hi!=null)win=`You profit if ${tk} stays below <b>$${fmt(p.be_hi)}</b> at expiration — ${fmt(Math.abs(p.be_hi/px-1)*100,1)}% above today’s $${fmt(px)}.`;
 return {what,win}}
function pkCard(p,i,d){const cat=((d.catalysts||{}).tickers||{})[p.ticker],tkx=(d.tickers||{})[p.ticker]||{},P=pkPlain(p),L=pkLegs(p);
 const pop=(p.pop_cons||0)*100,cr=p.credit!=null&&p.credit>0,cap=p.capital||0,ret=(p.roc||0)*100;
 const strat=p.strategy==='PCS'?'pcs':p.strategy==='CCS'?'ccs':null,sh=L.find(l=>l.s<0),lg=L.find(l=>l.s>0);
 const calc=strat&&sh&&lg?`<a href="#" onclick="calcOpen({tk:'${p.ticker}',strat:'${strat}',exp:'${p.expiry}',k1:${sh.k},k2:${lg.k}});return false">Calculator</a>`:'';
 const flags=[];if(cat&&cat.risk)flags.push(`⚠ ${esc(cat.note)}${cat.source?` <a href="${esc(cat.source)}" target="_blank" rel="noopener">source</a>`:''}`);
 if(p.earn_in_window)flags.push('⚠ Earnings before expiration');if(p.n8k_7d)flags.push(`${p.n8k_7d} SEC 8-K filing${p.n8k_7d>1?'s':''} in the last 7 days`);
 if(tkx.earnings_date)flags.push(`<span class="mute">next earnings ${esc(tkx.earnings_date)}</span>`);
 const tile=(l,v,c='',s='')=>`<div class="gt b-${c||'mute'}"><div class="l">${l}</div><div class="v ${c}">${v}</div>${s?`<div class="l">${s}</div>`:''}</div>`;
 return `<div class="pkc"><div class="pkh"><span class="pkn">#${i+1}</span>${(()=>{const id=LK.map.get(p.ticker+'|'+p.strategy+'|'+p.expiry+'|'+p.legs);return id?`<b class="chip call" title="This exact trade is tracked pick ${esc(id)}: locked this morning and counted in How the picks have done">🔒 Tracked ${esc(id)}</b>`:`<span class="chip mute" title="Live idea: re-priced every 30 minutes, not counted in the record">live · not tracked</span>`})()}<b class="pkt"><a href="?t=${p.ticker}" onclick="go('${p.ticker}');return false">${p.ticker}</a></b><span class="chip ${p.strategy==='PCS'?'call':p.strategy==='CCS'?'put':'acc'}">${esc(p.strategy_name||p.strategy)}</span>${dirTag(p.strategy)}${STARS(starsOf(p.pop_cal??p.pop_cons,p.roc))}<span class="mute" style="font-size:11px">${esc(TFN[p.tf]||p.tf||'')}</span><span data-bdg="${p.ticker}">${badgeHtml(p.ticker)}</span>
  <span class="mute">${LP(p.ticker,p.price,{...legK(p.legs),d:'$'})} · RSI ${p.rsi14==null?'–':fmt(p.rsi14,0)} · expires ${dshort(p.expiry)}${p.dte>250?' '+String(p.expiry).slice(0,4):''} (${p.dte===0?'today':p.dte+' day'+(p.dte==1?'':'s')})</span><span style="margin-left:auto">${calc} ${trkBtn({tk:p.ticker,exp:p.expiry,strat:p.strategy,legs_text:p.legs,what:(p.strategy_name||p.strategy)+' '+(p.legs||''),kind:cr&&!['CC','BW'].includes(p.strategy)?'credit':'debit',price:['CC','BW'].includes(p.strategy)||!cr?(cap/100||Math.abs(p.credit||0)):Math.abs(p.credit),capital:cap,rec_p:p.pop_cons,src:`Strategy Scanner · ${PK.tf} ${p.strategy} #${i+1}`})}</span></div>
  <div class="pkw">${P.what} <span class="mute">(${esc(p.legs)})</span></div>
  <div class="gtiles">${tile('Chance of profit',fmt(pop,pop>=99?1:0)+'%',pop>=80?'call':pop>=65?'amb':'put')}${tile(cr?'You collect':'You pay','$'+fmt(Math.abs(p.credit||0)*100,0),cr?'call':'put',cr?'per contract, up front':'')}${tile('Most you can lose','$'+fmt(cap,0),'put','capital at risk')}${tile('Return if it works',fmt(ret,0)+'%','',p.p50!=null?`chance to hit 50% profit early: ${fmt(p.p50*100,0)}%`:'')}${evTile(p)}</div>
  <div class="pkwin">${P.win}</div>${flags.length?`<div class="pkf">${flags.join('<br>')}</div>`:''}</div>`}
function myResults(R){const T=R&&R.total;
 let h=`<h3 style="margin:4px 0 4px">Your real trades <span class="mute" style="font-weight:400;font-size:12px">· trades you recorded with “+ I took it” / “+ Track”, at your own fills</span></h3>`;
 if(!T||!T.n)return h+`<p class="mute">None closed yet${R&&R.open?` (${R.open} open in My trades)`:''}. Press “+ I took it” on any recommended trade, then “Close trade” in My trades when you exit — expired trades are settled automatically at the official close.</p>`;
 const bar=(w,n)=>`<div class="wlbar" title="${w} won · ${n-w} lost"><i class="w" style="width:${n?w/n*100:0}%"></i><i class="l" style="width:${n?(n-w)/n*100:0}%"></i></div>`;
 const row=(k,g)=>`<tr><td style="text-align:left">${esc(k)}</td><td>${g.n}</td><td class="call"><b>${g.won}</b></td><td class="put"><b>${g.lost}</b></td><td>${bar(g.won,g.n)}</td><td class="${g.pnl<0?'put':'call'}"><b>${usd(g.pnl)}</b></td><td>${g.avg_win==null?'–':usd(g.avg_win)}</td><td>${g.avg_loss==null?'–':usd(g.avg_loss)}</td><td>${g.rec_p==null?'–':fmt(g.rec_p*100,0)+'%'}</td></tr>`;
 const head='<tr><th></th><th>Closed</th><th class="call">✓ Won</th><th class="put">✗ Lost</th><th>Won vs lost</th><th>Total P/L</th><th>Avg win</th><th>Avg loss</th><th title="Average chance of profit the app/scanner gave these when recommended">Expected</th></tr>';
 h+=`<p style="font-size:15px"><b class="call">✓ ${T.won} won</b> · <b class="put">✗ ${T.lost} lost</b> of ${T.n} closed — ${fmt(T.won/T.n*100,0)}% won · total P/L <b class="${T.pnl<0?'put':'call'}">${usd(T.pnl)}</b>${R.open?` · ${R.open} still open`:''}</p>`;
 h+=`<div class="tw"><table>${head}${Object.entries(R.by_strat).map(([k,g])=>row(STRATN[k]||k,g)).join('')}</table></div>`;
 h+=`<details><summary class="mute">By where the recommendation came from</summary><div class="tw"><table>${head}${Object.entries(R.by_source).map(([k,g])=>row(k,g)).join('')}</table></div></details>`;
 h+=`<details><summary class="mute">Show your ${T.n} closed trade${T.n==1?'':'s'}</summary>${myTradesTable(R.trades,false)}</details>`;
 if(T.n>=5&&T.won/T.n<0.5)h+=`<p class="amb" style="font-size:13px">Most of your closed trades lost. Compare with the scanner's own record below: if the scanner's picks did better than yours, the gap is in fills, timing or exits (e.g. holding past the 50% target, or closing on a dip); if both are poor for a strategy, that strategy's picks aren't working right now.</p>`;
 return h}
const PKMIN=()=>Math.max(20,+((W&&W.config&&W.config.picks_min_return_pct)||20));
const TFN={daily:'daily',weekly:'weekly',monthly:'monthly',quarterly:'2–8 months',leaps:'LEAPS'};
function pkRows(d){const out=[],seen=new Set();for(const [tf,L] of Object.entries(d.top||{}))for(const p of L||[]){const k=p.ticker+'|'+p.strategy+'|'+p.expiry+'|'+p.legs;if(seen.has(k))continue;seen.add(k);
 const cat=((d.catalysts||{}).tickers||{})[p.ticker]||{},pp=(p.pop_cal??p.pop_cons)||0,strat={LCALL:(p.dte||0)>=270?'LEAPS':'CALL',BW:'CC'}[p.strategy]||p.strategy;
 out.push({p:{...p,tf},exp:p.expiry,strat,price:p.price,pop:pp*100,ret:(p.roc||0)*100,cap:p.capital,prem:Math.abs(p.credit||0)*100,flag:!!(cat.risk||p.earn_in_window),
  best:Math.floor(pp*100)*1e3-(p.capital||0)*1e-3+(p.roc||0),p50:p.p50!=null?p.p50*100:null,dte:p.dte})}return out}
// ---- tracked (locked) picks: one fixed set per day, the only picks the record grades ----
const LK={tf:'',map:new Map()};
const lkLeg=l=>l.cp==='STK'?`${l.side==='sell'?'Sell':'Buy'} 100 shares`:`<span class="${l.side==='sell'?'put':'call'}">${l.side==='sell'?'Sell':'Buy'}</span> ${fmtK(l.K)} ${l.cp==='P'?'put':'call'}`;
const lkTrade=p=>(p.legs||[]).map(lkLeg).join(' + ')||esc(p.legs_text||'');
function lkEntry(p){const c=+p.credit||0;
 if(['CC','BW'].includes(p.strategy))return `buy shares at $${fmt(p.price)}, collect <b>$${fmt(c)}</b>`;
 return c>=0?`collect <b>$${fmt(c)}</b> <span class="mute">($${fmt(c*100,0)})</span>`:`pay <b>$${fmt(-c)}</b> <span class="mute">($${fmt(-c*100,0)})</span>`}
function lkNow(p){if(p.status==='closed'){const w=!!p.win;return `<b class="${w?'call':'put'}">${w?'✓ Won':'✗ Lost'}</b> <span class="${(p.pnl||0)>=0?'call':'put'}">${(p.pnl||0)>=0?'+':'−'}$${fmt(Math.abs(p.pnl||0),0)}</span><br><span class="mute" style="font-size:11px">closed at $${fmt(p.settle)} on ${esc(dshort(p.expiry))}</span>`}
 if(p.now_pnl==null)return '<span class="mute">open · no mark yet</span>';
 return `<span class="${p.now_pnl>=0?'call':'put'}">${p.now_pnl>=0?'+':'−'}$${fmt(Math.abs(p.now_pnl),0)}</span> <span class="mute">(${p.now_pct>=0?'+':''}${fmt(p.now_pct*100,0)}% of ${(+p.credit||0)>=0?'credit':'cost'})</span><br><span class="mute" style="font-size:11px">open · mid-price mark ${esc(dshort(p.now_at))}${p.hit50?' · reached 50% '+esc(dshort(p.hit50)):''}</span>`}
function lkTable(P,withDate){return `<div class="tw"><table class="lkt"><tr><th>ID</th>${withDate?'<th>Locked</th>':''}<th>Timeframe</th><th>Ticker</th><th style="text-align:left">Exact trade</th><th>Expires</th><th title="Per share, at the mid price when it was locked (per contract in brackets)">Entry</th><th title="Most you can lose per contract (capital at risk)">Risk</th><th title="The scanner's chance of profit when it was locked">Chance</th><th>Result / now</th><th></th></tr>`+
 P.map(p=>{const c=+p.credit||0,cr=c>0&&!['CC','BW'].includes(p.strategy),cap=p.capital||0;
  return `<tr><td style="white-space:nowrap"><b>🔒 ${esc(p.id)}</b></td>${withDate?`<td>${esc(dshort(p.date))} ${esc(p.locked_at||'')}</td>`:''}<td>${esc(TFN[p.tf]||p.tf)} <span class="mute">#${p.rank}</span></td>
  <td><a href="?t=${esc(p.ticker)}" onclick="go('${esc(p.ticker)}');return false"><b>${esc(p.ticker)}</b></a><br><span class="mute" style="font-size:11px">$${fmt(p.price)} at lock</span></td>
  <td style="text-align:left;white-space:nowrap">${esc(STRATN[p.strategy]||p.strategy_name||p.strategy)}<br>${lkTrade(p)}</td><td style="white-space:nowrap">${esc(p.expiry)}<br><span class="mute" style="font-size:11px">${p.dte!=null?p.dte+' DTE at lock':''}</span></td>
  <td style="white-space:nowrap">${lkEntry(p)}</td><td>$${fmt(cap,0)}</td><td>${p.pop==null?'–':fmt(p.pop*100,0)+'%'}</td><td style="white-space:nowrap">${lkNow(p)}</td>
  <td>${p.status==='open'?trkBtn({tk:p.ticker,exp:p.expiry,strat:p.strategy,legs_text:p.legs_text||'',what:(p.strategy_name||p.strategy)+' '+(p.legs_text||''),kind:cr?'credit':'debit',price:['CC','BW'].includes(p.strategy)||!cr?(cap/100||Math.abs(c)):Math.abs(c),capital:cap,rec_p:p.pop,src:`Tracked pick ${p.id}`}):''}</td></tr>`}).join('')+'</table></div>'}
function lockRender(d){const L=d.locked||{},el=$('#pklock');LK.map=new Map();if(!el)return;
 const T=(L.picks||[]).filter(p=>p.date===L.today);T.forEach(p=>LK.map.set(p.key,p.id));
 if(L.locked_today){const lt=L.locked_today,tfs=[...new Set(T.map(p=>p.tf))],S=T.filter(p=>!LK.tf||p.tf===LK.tf);
  el.innerHTML=`<div class="lkbox"><p style="margin:0 0 6px"><b>🔒 Today’s tracked picks</b> — locked <b>${esc(lt.locked_at)}</b> (quotes ${esc(lt.quotes_at)}). These ${T.length} trades do not change for the rest of the day, and they are the only picks counted in <a href="#" onclick="pkJump('pklearn');return false">How the picks have done</a>. Take one exactly as shown, or track it by its ID. The live list below keeps re-pricing; a card that matches a tracked pick shows its ID.</p>
   <div class="ctl"><button class="seg${!LK.tf?' on':''}" onclick="LK.tf='';lockRender(PK.d)">All (${T.length})</button>${tfs.map(t=>`<button class="seg${LK.tf===t?' on':''}" onclick="LK.tf='${t}';lockRender(PK.d)">${esc(TFN[t]||t)} (${T.filter(p=>p.tf===t).length})</button>`).join('')}</div>${lkTable(S,false)}</div>`;return}
 const prev=L.last_day?(L.picks||[]).filter(p=>p.date===L.last_day&&p.status==='open'):[];
 el.innerHTML=`<div class="lkbox"><p style="margin:0"><b>🔒 Today’s picks are not locked yet.</b> They lock at the first live re-price at or after <b>${esc(L.lock_at||'9:45 ET')}</b> on a market day (normally about 9:52 ET, 4:52 AM HST). Until then the list below can change and none of it is tracked.</p>
  ${prev.length?`<details><summary class="mute">Still open from ${esc(dshort(L.last_day))} (${prev.length})</summary>${lkTable(prev,true)}</details>`:''}</div>`}
function lkExposure(X){if(!X||!X.n_open)return '';
 const ws=(X.worst_expiry_share||0)*100, ow=(X.one_way_share||0)*100, risky=ws>40||ow>70;
 const ex=Object.entries(X.by_expiry||{}).sort((a,b)=>b[1]-a[1]).slice(0,4);
 return `<details${risky?' open':''} style="margin-top:8px"><summary><b>Where the tracked risk sits</b> <span class="mute" style="font-weight:400;font-size:12px">· $${fmt(X.total,0)} at risk across ${X.n_open} open picks${risky?' · ⚠ concentrated':''}</span></summary>
  <p style="margin:6px 0">Biggest expiration: <b class="${ws>40?'put':''}">${esc(X.worst_expiry||'–')}</b> with <b>$${fmt(X.worst_expiry_capital,0)}</b> (${fmt(ws,0)}% of the total). Pointing one way: <b class="${ow>70?'put':''}">${fmt(ow,0)}%</b>.
  ${X.ev_n?`Expected value of the open picks: mean <b class="${X.ev_mean>0?'call':'put'}">${X.ev_mean>=0?'+':'−'}$${fmt(Math.abs(X.ev_mean),0)}</b>, ${X.ev_negative} of ${X.ev_n} negative.`:''}</p>
  ${risky?`<p class="put" style="margin:0 0 6px"><b>⚠ This is the shape that lost $99,879 on 2026-11-20</b> — 1,217 positions and $367,900 of capital on one expiration date with 68% pointing the same way. Trades sharing an expiration are one bet, not many.</p>`:''}
  <table><tr><th>Expiration</th><th>At risk</th><th>Share</th></tr>${ex.map(([e,v])=>`<tr><td>${esc(e)}</td><td>$${fmt(v,0)}</td><td>${fmt(v/X.total*100,0)}%</td></tr>`).join('')}</table></details>`}
function lkLearn(L){L=L||{};const G=(L.by_group||[]).slice().sort((a,b)=>({daily:0,weekly:1,monthly:2,quarterly:3,leaps:4}[a.timeframe]??9)-({daily:0,weekly:1,monthly:2,quarterly:3,leaps:4}[b.timeframe]??9)||b.n-a.n);
 const TN=G.reduce((a,g)=>a+g.n,0),TW=G.reduce((a,g)=>a+Math.round(g.win_rate*g.n),0),TM=G.reduce((a,g)=>a+Math.round(g.managed_win_rate*g.n),0);
 const bar=(w,n)=>`<div class="wlbar" title="${w} won · ${n-w} lost"><i class="w" style="width:${n?w/n*100:0}%"></i><i class="l" style="width:${n?(n-w)/n*100:0}%"></i></div>`;
 const P=(L.picks||[]).slice().sort((a,b)=>(a.status===b.status?0:a.status==='open'?1:-1)||String(b.expiry).localeCompare(String(a.expiry))||String(a.id).localeCompare(String(b.id)));
 let h=`<h3 style="margin:14px 0 4px">🔒 Tracked picks <span class="mute" style="font-weight:400;font-size:12px">· the fixed set locked each morning (IDs like T1118-07), graded at the locked entry, settled at the official close on expiration day</span></h3>
  <p><b>${(L.n_open||0)+(L.n_closed||0)}</b> tracked · <b>${L.n_closed||0}</b> finished · ${L.n_open||0} still open.${L.days?` Locked on ${Object.keys(L.days).length} day${Object.keys(L.days).length==1?'':'s'} so far.`:''}</p>`+lkExposure(L.exposure);
 if((L.n_closed||0)&&(L.clusters||0)<20)h+=`<p class="mute" style="font-size:12px">⚠ ${L.n_closed} finished picks but only <b>${L.clusters||0}</b> independent ticker/expiration groups. Picks sharing a ticker and an expiration share one market move, so they are not separate evidence — about 20 groups are needed before a win rate here means anything.</p>`;
 if(TN)h+=`<p style="font-size:15px">Finished: <b class="call">✓ ${TW} won</b> · <b class="put">✗ ${TN-TW} lost</b> — ${fmt(TW/TN*100,0)}% won if held to expiration${TM!==TW?`, <b>${fmt(TM/TN*100,0)}%</b> if you took profit at 50%`:''}.</p>
  <div class="tw"><table><tr><th>Timeframe</th><th>Strategy</th><th>Finished</th><th class="call">✓ Won</th><th class="put">✗ Lost</th><th>Won vs lost</th><th>Won if you take 50% profit</th><th>Avg result per trade</th><th>Scanner expected</th></tr>${G.map(g=>{const w=Math.round(g.win_rate*g.n);
   return `<tr><td>${esc(TFN[g.timeframe]||g.timeframe)}</td><td style="text-align:left">${esc(STRATN[g.strategy]||g.strategy)}</td><td>${g.n}</td><td class="call"><b>${w}</b></td><td class="put"><b>${g.n-w}</b></td><td>${bar(w,g.n)}</td><td>${fmt(g.managed_win_rate*100,0)}%</td>
   <td class="${(g.avg_pnl_pct||0)>=0?'call':'put'}">${g.avg_pnl_pct==null?'–':((g.avg_pnl_pct>=0?'+':'')+fmt(g.avg_pnl_pct*100,1)+'%')}</td><td>${g.avg_pop==null?'–':fmt(g.avg_pop*100,0)+'%'}</td></tr>`}).join('')}</table></div>`;
 else h+=`<p class="mute">No tracked pick has expired yet${L.n_open?` — the first results come in as the ${L.n_open} open ones expire (daily picks within a few days)`:' — tracking starts with the first lock (next market day, ~9:52 ET)'}.</p>`;
 if(P.length)h+=`<details><summary class="mute">Show every tracked pick (${P.length})</summary>${lkTable(P,true)}</details>`;
 return h}
// Expected value per contract: what the trade is worth on average, not how often it wins.
// A trade can win 78% of the time and still lose money if the 22% loses four times as much --
// which is what 3,478 settled picks did (-10.1% of capital per trade at a 71% win rate).
function evTile(p){
 const ev=p.ev;
 if(ev==null)return `<div class="gt b-mute"><div class="l">Expected value</div><div class="v mute">–</div><div class="l">could not be valued</div></div>`;
 const good=ev>0, why=p.ev_fail||'';
 const note=good?'average result per contract':(why==='min_iv_rv'?'options not priced above the stock’s own movement':'loses money on average');
 return `<div class="gt b-${good?'call':'put'}" title="What this trade is worth on average per contract: the credit less the loss it expects to pay, priced at ${p.rv?fmt(p.rv*100,0)+'% realised volatility':'the stock’s realised volatility'} stressed 1.35x, after $0.65/contract commission each way. Positive expected value is the only reason to take a trade; a high chance of profit says nothing about how much it wins against how much it loses."><div class="l">Expected value</div><div class="v ${good?'call':'put'}">${ev>=0?'+':'−'}$${fmt(Math.abs(ev),0)}</div><div class="l">${note}</div></div>`}
// In a market with no volatility premium almost nothing has positive expected value, and saying so
// plainly is more use than an empty list.
function evBanner(d){
 const all=[];for(const L of Object.values(d.top||{}))for(const p of (L||[]))all.push(p);
 const have=all.filter(p=>p.ev!=null);if(!have.length)return '';
 const pass=have.filter(p=>p.ev>0), best=have.reduce((a,b)=>(b.ev>a.ev?b:a),have[0]);
 const med=(()=>{const x=have.map(p=>p.ev).sort((a,b)=>a-b);return x[Math.floor(x.length/2)]})();
 if(pass.length>=Math.max(3,have.length*0.2))
  return `<div class="lkbox" style="border-left-color:var(--call)"><b>${pass.length} of ${have.length} shown picks have positive expected value.</b> <span class="mute">Median ${med>=0?'+':'−'}$${fmt(Math.abs(med),0)} per contract. Only the positive ones are counted in the record.</span></div>`;
 return `<div class="lkbox" style="border-left-color:var(--put)"><b>⚠ ${pass.length} of ${have.length} shown picks have positive expected value today.</b>
  <p style="margin:6px 0 0">The median pick is worth <b class="${med>0?'call':'put'}">${med>=0?'+':'−'}$${fmt(Math.abs(med),0)}</b> per contract and the best is <b class="${best.ev>0?'call':'put'}">${best.ev>=0?'+':'−'}$${fmt(Math.abs(best.ev),0)}</b> (${esc(best.ticker)}).
  Selling options only pays when implied volatility is above the movement that actually turns up. When it is not, the honest answer is that there is nothing worth taking — these are still listed, best expected value first, so you can see how close they come.</p>
  <p class="mute" style="margin:6px 0 0;font-size:12px">Measured over 3,478 settled picks: a 71% win rate and −10.1% of capital per trade, because the average win was $47 and the average loss $211. <a href="#" onclick="pkJump('pklearn');return false">The record</a>.</p></div>`}
function pkRender(){const d=PK.d;if(!d)return;try{lockRender(d)}catch(e){console.error(e)}
 try{const b=$('#pkevb');if(b)b.innerHTML=evBanner(d)}catch(e){console.error(e)}const st=d.status||{},m=d.meta||{};
 const run=m.run_date?`${m.mode==='fast'?'live re-price':'full scan'} ${esc(m.run_date)} · file ${esc(d.file_time||'')} (PC time) · ${m.n_tickers||'?'} tickers`:'';
 $('#pknote').innerHTML=(st.running?`<b class="amb">refreshing… started ${esc(st.started||'')}</b> · `:'')+(st.error?`<span class="put">${esc(st.error)}</span> · `:'')+run+(m.market_closed?' · <span class="amb">prior-close quotes</span>':'');
 if(d.error){$('#pklist').innerHTML=`<p class="put">${esc(d.error)}</p>`;return}
 const all=pkRows(d),mn=PKMIN();
 $('#pkufb').innerHTML=ufb('picks',all,{asof:m.quotes_last_trade?String(m.quotes_last_trade).slice(5,16)+' ET quotes':'',strats:[...new Set(all.map(r=>r.strat))],minRet:mn,note:`Every pick pays at least ${mn}% of its capital if it works · <span class="acc">★</span> / <span class="acc">★★</span> = unusually high return with a high chance of profit (phone/pop-up alert for new ★★)`});
 const L=gfSort(gfFilter(all,{sec:'picks',minRet:mn})).slice(0,GF.n);
 $('#pklist').innerHTML=L.length?L.map((x,i)=>pkCard(x.p,i,d)).join(''):`<p class="mute">No picks pass these selections (${all.length} picks in total). Try “All” expirations, another strategy, or lower Min chance.</p>`;
 loadBadges(L.map(x=>x.p.ticker),()=>document.querySelectorAll('[data-bdg]').forEach(e=>e.innerHTML=badgeHtml(e.dataset.bdg)));

 const ev=d.events||[];$('#pkev').innerHTML=ev.length?ev.map(day=>`<div class="pkday"><b>${dshort(day.date)}</b>${day.trading===false?' <span class="mute">market closed</span>':''}<ul>${(day.events||[]).map(e=>`<li class="${e.impact==='high'?'':'mute'}">${e.impact==='high'?'<b class="amb">●</b> ':''}${e.time_et?`<b>${esc(e.time_et)} ET</b>${e.time_hst?` <span class="mute">(${esc(e.time_hst)} HST)</span>`:''} — `:''}${esc(e.title)}${e.source?` <a href="${esc(e.source)}" target="_blank" rel="noopener" class="mute">source</a>`:''}</li>`).join('')||'<li class="mute">nothing scheduled</li>'}</ul></div>`).join(''):'<p class="mute">No calendar file yet.</p>';
 const lr=d.learning||{},G=(lr.by_group||[]).filter(g=>g.n>=1).sort((a,b)=>({daily:0,weekly:1,monthly:2,quarterly:3,leaps:4}[a.timeframe]??9)-({daily:0,weekly:1,monthly:2,quarterly:3,leaps:4}[b.timeframe]??9)||b.n-a.n);
 const TW=G.reduce((a,g)=>a+Math.round(g.win_rate*g.n),0),TN=G.reduce((a,g)=>a+g.n,0),TM=G.reduce((a,g)=>a+Math.round((g.managed_win_rate??g.win_rate)*g.n),0);
 const verdict=g=>{const df=g.win_rate-g.avg_pop;if(g.n<10)return'<span class="chip mute" title="Too few finished trades to judge">too few yet</span>';
  return df>=0.03?'<span class="chip call">better than expected</span>':df>=-0.05?'<span class="chip acc">about as expected</span>':'<span class="chip amb">worse than expected</span>'};
 const bar=(w,n)=>`<div class="wlbar" title="${w} won · ${n-w} lost"><i class="w" style="width:${n?w/n*100:0}%"></i><i class="l" style="width:${n?(n-w)/n*100:0}%"></i></div>`;
 $('#pklearn').innerHTML=myResults(d.my_results||TR.res)+lkLearn(d.locked)+`<details style="margin-top:12px"><summary><b>Older count: every version the scanner logged</b> <span class="mute" style="font-weight:400;font-size:12px">· before locking, every refresh added the picks it showed (~500 a day, including pre-market prices), so these are not one fixed list</span></summary><p><b>${lr.n_total||0}</b> picks counted · <b>${lr.n_closed||0}</b> finished · ${lr.n_open||0} still open.${lr.n_excluded?` <span class="mute">${lr.n_excluded} older picks that paid under ${fmt((lr.min_counted_roc||0.1)*100,0)}% of their capital are not counted.</span>`:''}</p>`+(TN?`<p style="font-size:15px">Finished trades: <b class="call">✓ ${TW} won</b> · <b class="put">✗ ${TN-TW} lost</b> — ${fmt(TW/TN*100,0)}% won if held to expiration${TM!==TW?`, <b>${fmt(TM/TN*100,0)}%</b> if you took profit at 50%`:''}.</p>`:'')+
  (G.length?`<div class="tw"><table><tr><th>Timeframe</th><th>Strategy</th><th>Finished</th><th class="call">✓ Won</th><th class="put">✗ Lost</th><th>Won vs lost</th><th title="Counts a trade as a win if it reached 50% profit before expiration (when you take profits early)">Won if you take 50% profit</th><th title="Average profit or loss per trade as a % of the capital at risk">Avg result per trade</th><th title="The average chance of profit the scanner gave these picks when it chose them">Scanner expected</th><th>Verdict</th></tr>${G.map(g=>{const w=Math.round(g.win_rate*g.n),l=g.n-w;
   return `<tr><td>${esc(g.timeframe)}</td><td style="text-align:left">${esc(STRATN[g.strategy]||g.strategy)} <span class="mute">(${esc(g.strategy)})</span></td><td>${g.n}</td>
   <td class="call"><b>${w}</b> <span class="mute">${fmt(g.win_rate*100,0)}%</span></td><td class="put"><b>${l}</b> <span class="mute">${fmt((1-g.win_rate)*100,0)}%</span></td><td>${bar(w,g.n)}</td>
   <td>${g.managed_win_rate==null?'–':fmt(g.managed_win_rate*100,0)+'%'}</td><td class="${(g.avg_pnl_pct||0)>=0?'call':'put'}">${g.avg_pnl_pct==null?'–':((g.avg_pnl_pct>=0?'+':'')+fmt(g.avg_pnl_pct*100,1)+'%')}</td>
   <td>${g.avg_pop==null?'–':fmt(g.avg_pop*100,0)+'%'}</td><td>${verdict(g)}</td></tr>
   <tr class="pkx"><td colspan="10" style="text-align:left;padding-top:0"><details ontoggle="pkTrades(this,'${esc(g.timeframe)}','${esc(g.strategy)}')"><summary class="mute">Show the ${g.n} ${esc(g.timeframe)} ${esc(STRATN[g.strategy]||g.strategy)} trade${g.n==1?'':'s'}</summary><div class="pktr"><p class="mute">Loading…</p></div></details></td></tr>`}).join('')}</table></div>
  <p class="mute">How to read it: <b class="call">✓ Won</b> = the trade finished with a profit when held to expiration (settled at the official close). <b class="put">✗ Lost</b> = it finished with a loss. Green is always a win and red always a loss. “Won if you take 50% profit” also counts trades that reached half their max profit before expiring. “Scanner expected” is the chance of profit the scanner predicted; the verdict compares the real win rate with it (needs 10+ finished trades).</p>`:'<p class="mute">No finished picks yet.</p>')+'</details>'}
async function pkTrades(el,tf,st){if(!el.open||el.dataset.done)return;const box=el.querySelector('.pktr');
 try{const j=await (await fetch(`/api/picks/trades?tf=${encodeURIComponent(tf)}&st=${encodeURIComponent(st)}`)).json();if(j.error)throw new Error(j.error);el.dataset.done=1;
  const T=j.trades||[];if(!T.length){box.innerHTML='<p class="mute">No finished trades.</p>';return}
  const leg=l=>`<span class="${l.side==='sell'?'put':'call'}">${l.side==='sell'?'Sell':'Buy'}</span> `+(l.K==null||!['P','C'].includes(l.cp)?'100 shares':`${fmt(l.K,l.K%1?1:0)}${l.cp}`);
  const W=T.filter(t=>t.win).length;
  box.innerHTML=`<p class="mute" style="margin:4px 0">${T.length} finished · <b class="call">✓ ${W} won</b> · <b class="put">✗ ${T.length-W} lost</b> · newest expiration first. Settle = official close on expiration day.</p><div class="tw"><table class="pktt"><tr><th>Picked</th><th>Ticker</th><th>Expired</th><th>Trade (legs)</th><th>Stock then</th><th title="Credit received (+) or debit paid (−) per share">Credit / debit</th><th title="Money at risk per contract">Capital</th><th title="The scanner's chance of profit when it picked it">Scanner chance</th><th>Stock at expiry</th><th>Result</th><th title="Profit or loss per contract and as % of capital">P/L</th><th title="Did it reach 50% of max profit before expiring?">Hit 50%?</th></tr>`+
  T.map(t=>{const cr=t.credit,win=t.win==1;return `<tr><td>${esc(t.date)}</td><td><b>${esc(t.ticker)}</b></td><td>${esc(t.expiry)}${t.dte!=null?` <span class="mute">(${t.dte}d)</span>`:''}</td><td style="text-align:left;white-space:nowrap">${(t.legs||[]).map(leg).join(' / ')||'–'}</td><td>${fmt(t.price)}</td>
   <td>${cr==null?'–':cr>=0?'+$'+fmt(cr,2):'−$'+fmt(-cr,2)}</td><td>${t.capital==null?'–':'$'+fmt(t.capital,0)}</td><td>${t.pop_cons==null?'–':fmt(t.pop_cons*100,0)+'%'}</td><td>${fmt(t.settle_price)}</td>
   <td class="${win?'call':'put'}"><b>${win?'✓ Won':'✗ Lost'}</b></td><td class="${(t.pnl||0)>=0?'call':'put'}">${t.pnl==null?'–':((t.pnl>=0?'+$':'−$')+fmt(Math.abs(t.pnl),0))}${t.pnl_pct==null?'':` <span class="mute">(${t.pnl_pct>=0?'+':''}${fmt(t.pnl_pct*100,0)}%)</span>`}</td>
   <td>${t.hit50?`<span class="call">yes</span>${t.hit50_date?` <span class="mute">${esc(t.hit50_date.slice(5))}</span>`:''}`:'<span class="mute">no</span>'}</td></tr>`}).join('')+'</table></div>'}
 catch(e){box.innerHTML='<p class="err">'+esc(e.message)+'</p>'}}
$('#pkref').onclick=async()=>{try{const j=await post('/api/picks/refresh');if(PK.d)PK.d.status=j.status;pkRender();setTimeout(pkLoad,4000)}catch(e){$('#pknote').textContent='· '+e.message}};

// ---- abbreviations legend (all pages) ----
// [term, meaning, tabs it appears on]
const ABBR=[
 ['Strategies',null],
 ['PCS','Put credit spread — sell a put and buy a lower put; you collect a credit and profit if the stock stays above the short put.','picks main calc'],
 ['CCS','Call credit spread — sell a call and buy a higher call; profit if the stock stays below the short call.','picks calc'],
 ['BCS','Bull call spread (call debit spread) — buy a call and sell a higher call; you pay a debit and profit if the stock rises.','main calls calc'],
 ['CDS','Call debit spread — same as a bull call spread.','calls calc'],
 ['PDS','Put debit spread (bear put spread) — buy a put and sell a lower put; profit if the stock falls.','calc'],
 ['CSP','Cash-secured put — sell a put with cash set aside to buy 100 shares at the strike if assigned.','puts main calc picks'],
 ['CC','Covered call — own 100 shares and sell a call above the price; you keep the premium, shares are sold if the stock finishes above the strike.','calls calc picks'],
 ['IC','Iron condor — a put credit spread plus a call credit spread; profit if the stock stays between them.','picks'],
 ['BW','Buy-write — buy 100 shares and sell a covered call at the same time.','picks'],
 ['LEAPS','Long-term options, 9+ months to expiration.','calls calc picks'],
 ['Wheel','Sell cash-secured puts → if assigned, sell covered calls on the shares → repeat.','puts calls'],
 ['Option basics',null],
 ['C / P','Call / put. “245C” = the 245-strike call, “200P” = the 200-strike put.','all'],
 ['−200P / +195P','Minus = you sell that option, plus = you buy it.','picks main calc'],
 ['Strike (K)','The fixed price in the contract.','all'],
 ['DTE','Days to expiration. 0 DTE = expires today.','all'],
 ['Premium','The option price per share; one contract = 100 shares, so 1.50 = $150.','all'],
 ['Credit / debit','Credit = cash you receive when opening; debit = cash you pay.','all'],
 ['Mid','Halfway between bid and ask — where limit orders usually fill.','all'],
 ['Nat (natural)','The worst-case fill: sell at the bid, buy at the ask.','main puts calls'],
 ['Bid / ask','What buyers pay / what sellers want.','all'],
 ['ITM / ATM / OTM','In / at / out of the money. A put is ITM when the stock is below its strike; a call when above.','all'],
 ['Assignment','When the option you sold is exercised: you buy (put) or sell (call) 100 shares at the strike.','puts calls calc'],
 ['Width','Distance between the two strikes of a spread.','main calls calc'],
 ['Cr/W','Credit ÷ width — how much of the spread’s width you collect.','main'],
 ['Max loss','The most you can lose on the trade (capital at risk).','all'],
 ['B/E','Breakeven — stock price at expiration where the trade neither makes nor loses money.','all'],
 ['Probabilities & returns',null],
 ['POP','Probability of profit — model chance the stock finishes past breakeven at expiration (from implied volatility). Not a guarantee.','all'],
 ['Hits 50%','Chance the trade reaches 50% of max profit at some daily close before expiration (simulated).','main puts calls'],
 ['When 50%','Most likely date it reaches 50% profit; “flat” = the date if the stock doesn’t move.','main puts'],
 ['Chance of max','Chance the stock finishes beyond the far strike, giving full profit.','calls main'],
 ['ROC / Return','Return on capital — premium ÷ cash secured (puts) or credit ÷ max loss (spreads).','all'],
 ['Ann. / %/yr','Annualized return — the per-trade return scaled to a year.','puts main calls'],
 ['R:R','Reward : risk — max profit ÷ max loss.','calls main'],
 ['EM','Expected move — ±1 standard deviation move to expiration implied by option prices (≈68% of outcomes inside).','main'],
 ['Greeks & volatility',null],
 ['Δ (delta)','How much the option moves per $1 in the stock; also a rough chance of finishing in the money.','all'],
 ['θ (theta)','Time decay — value lost (buyer) or gained (seller) per day.','calc calls'],
 ['Vega','Change in value for a 1-point change in implied volatility.','calc'],
 ['IV','Implied volatility — how big a move option prices expect (annualized %).','all'],
 ['RV','Realized volatility — how much the stock has actually been moving.','picks'],
 ['IV/RV','Implied ÷ realized volatility; above 1 means options are priced rich vs recent moves.','picks'],
 ['IV crush','Drop in implied volatility after an event like earnings — option prices fall.','calls'],
 ['GEX & dealer positioning',null],
 ['GEX','Gamma exposure — how much market makers must buy/sell as the price moves, in $ per 1% move.','main picks'],
 ['Net GEX','Sum of call and put GEX. Positive = dealers dampen moves (range); negative = dealers amplify moves (trend).','main'],
 ['Put wall','Strike with the largest put GEX below the price — often support.','main puts'],
 ['Call wall','Strike with the largest call GEX above the price — often resistance.','main calls'],
 ['Gamma flip','Price where net GEX changes sign (the crossing nearest today’s price).','picks main'],
 ['Vanna','How dealers’ hedges change when implied volatility moves; shown as the stock they’d buy/sell if IV drops 1 point.','main'],
 ['Charm','How dealers’ hedges shift as time passes; shown as stock bought/sold per day.','main'],
 ['OI','Open interest — number of option contracts outstanding.','all'],
 ['P/C','Put/call open-interest ratio.','main'],
 ['Charts & indicators',null],
 ['RSI(14)','Relative Strength Index, 14 days: above 70 overbought, below 30 oversold.','all'],
 ['SMA / MA','Simple moving average (20 / 50 / 200 days).','main'],
 ['EMA','Exponential moving average (9 / 21) — reacts faster to recent prices.','main'],
 ['VWAP','Volume-weighted average price — the average price paid (anchored here from the month start or the swing low).','main'],
 ['BB','Bollinger Bands — 20-day average ± 2 standard deviations; %B = where price sits inside the bands.','main'],
 ['Fib','Fibonacci retracement levels; ★ = the key 38.2% / 50% / 61.8% levels.','main'],
 ['Golden / death cross','50-day average crossing above / below the 200-day.','main'],
 ['Scanner & rotation',null],
 ['RS / vs SPY','Relative strength — return compared with SPY over 1 month (1M) or 3 months (3M).','main'],
 ['Leading / Improving / Weakening / Lagging','Rotation quadrant vs SPY: strong and rising / weak but rising / strong but fading / weak and falling.','main'],
 ['Mom','Momentum score 0–100 (relative strength, trend and rotation).','main'],
 ['Dark pool & filings',null],
 ['ATS','Alternative trading system — a dark pool.','flow'],
 ['TRF','Trade Reporting Facility — where off-exchange (dark pool) trades are reported to FINRA.','flow'],
 ['NLS Plus','Nasdaq Last Sale Plus — Nasdaq’s free real-time trade feed (Nasdaq exchanges + FINRA/Nasdaq TRF).','flow'],
 ['FINRA','Financial Industry Regulatory Authority — publishes daily off-exchange short volume and weekly ATS volume.','flow'],
 ['Reg SHO','The SEC short-sale rule; FINRA’s daily short-volume files come from it.','flow'],
 ['z','z-score — how many standard deviations today is from the 20-day average.','flow'],
 ['Form 4','SEC filing for insider buys/sells.','flow picks'],
 ['8-K','SEC filing for material company events.','picks'],
 ['S-3 / 424B','SEC filings for share offerings.','picks'],
 ['13D / 13G','SEC filings for 5%+ ownership stakes.','picks'],
 ['Earnings',null],
 ['EPS','Earnings per share. “EPS est.” = analysts’ consensus forecast; “a year ago” = the same quarter last year.','picks main'],
 ['Before open / after close','When the company reports: before the 9:30 AM ET open or after the 4 PM ET close.','picks'],
 ['Units & times',null],
 ['K / M / Bn','Thousand / million / billion dollars.','all'],
 ['ET / HST','Eastern Time (US markets) / Hawaii Time.','all'],
 ['ETF / CEF','Exchange-traded fund / closed-end fund.','all'],
 ['▲ / ▼ / ●','Bullish / bearish / neutral signal (technical read); up / down change elsewhere.','main'],
 ['⚠','Something to check before trading (earnings, catalyst, wide quotes, thin open interest).','all']];
function qLine(q){if(!q||!q.legs)return'';const f=v=>v==null?'–':(+v).toFixed(2);
 return `<br><span class="mute" style="font-size:11.5px" title="Priced at a realistic fill: halfway between the mid and the natural price (what you get filling at market)">${q.legs.map(l=>`${esc(l[0])} ${f(l[1])}×${f(l[2])}`).join(' · ')} · mid ${f(q.mid)} · natural ${f(q.nat)} · <b>priced ${f(q.fill)}</b>${q.asof?' at '+esc(hhmm(q.asof))+' ET':''}${q.src?' ('+(q.src==='schwab'?'Schwab':'CBOE')+')':''}</span>`}
const SDIR={PCS:['bullish','call'],PUT:['bullish','call'],CSP:['bullish','call'],LCALL:['bullish','call'],CALL:['bullish','call'],LC:['bullish','call'],LEAPS:['bullish','call'],BCS:['bullish','call'],CDS:['bullish','call'],SWING:['bullish','call'],CCS:['bearish','put'],PDS:['bearish','put'],IC:['neutral','acc'],CC:['neutral to bullish','acc'],BW:['neutral to bullish','acc']};
const dirTag=c=>{const d=SDIR[String(c||'').toUpperCase()];return d?` <span class="chip ${d[1]}" style="font-size:11px" title="${d[0]==='bearish'?'Works if the stock stays below the short strike or falls':d[0]==='neutral'?'Works if the stock stays between the short strikes':d[0]==='bullish'?'Works if the stock stays above the short strike or rises':'Works if the stock stays flat or rises a little'}">${d[0]}</span>`:''};
const STRATN={SWING:'Swing (shares)',LCALL:'Long call',CALL:'Long call',PUT:'Short put',PCS:'Put credit spread',CCS:'Call credit spread',CSP:'Cash-secured put',CC:'Covered call',BCS:'Bull call spread',IC:'Iron condor',BW:'Buy-write',LC:'Long call',LEAPS:'LEAPS call',PDS:'Put debit spread',CDS:'Call debit spread',LPUT:'Long put'};
function abbrRows(f,q){let h='',sec=null;for(const [t,m,tabs] of ABBR){if(m===null){sec=t;continue}
  if(f&&!(tabs==='all'||tabs.split(' ').includes(f)))continue;if(q&&!(t+' '+m).toLowerCase().includes(q.toLowerCase()))continue;
  if(sec!==null){h+=`<div class="abbrsec">${sec}</div>`;sec=null}h+=`<div class="abbrrow"><b>${esc(t)}</b><span>${esc(m)}</span></div>`}return h||'<p class="mute">No match.</p>'}
function abbrLegend(f){return `<details class="card abbrleg"><summary><b>Abbreviations on this page</b> <span class="mute">· tap to open · full list under “ABC?” at the top</span></summary><div class="abbrgrid">${abbrRows(f)}</div></details>`}
for(const[f,id]of[['picks','#tabPicks'],['main','#tabMain'],['puts','#tabPuts'],['calls','#tabCalls'],['calc','#tabCalc'],['flow','#tabFlow']]){const el=$(id);if(el)el.insertAdjacentHTML('beforeend',abbrLegend(f))}
$('#abbrbtn').onclick=()=>{$('#abbrdlg').hidden=false;$('#abbrq').value='';$('#abbrall').innerHTML='<div class="abbrgrid">'+abbrRows(null)+'</div>';$('#abbrq').focus()};
$('#abbrq').oninput=e=>{$('#abbrall').innerHTML='<div class="abbrgrid">'+abbrRows(null,e.target.value)+'</div>'};
$('#abbrx').onclick=()=>{$('#abbrdlg').hidden=true};$('#abbrdlg').onclick=e=>{if(e.target.id==='abbrdlg')$('#abbrdlg').hidden=true};
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('#abbrdlg').hidden=true});


// ---- best use of capital (Picks tab) ----
let CE={hz:'le30',types:['credit','put','cc','call','bcs','leaps'],px:0,sort:'eff',pop:0,ret:15,cap:0,bud:1000,grp:'type',hideCat:false,d:null,busy:false};
try{const v=JSON.parse(localStorage.getItem('walls_ce2')||'{}');Object.assign(CE,v,{d:null,busy:false})}catch(e){}
const ceSave=()=>{try{const{d,busy,...k}=CE;localStorage.setItem('walls_ce2',JSON.stringify(k))}catch(e){}};
const CEHZ=[['0','Today (0 DTE)',0,0],['1-7','1–7 days',1,7],['8-14','8–14 days',8,14],['15-30','15–30 days',15,30],['le30','Any up to 30 days',0,30],['31-90','31–90 days',31,90],['91-269','3–9 months',91,269]];
const CETY=[['credit','Credit spreads (put = bullish, call = bearish)'],['put','Short puts'],['cc','Covered calls'],['call','Buy calls'],['bcs','Bull call spreads'],['leaps','LEAPS calls']];
const CEPX=[[0,'Any price'],[10,'Under $10'],[20,'Under $20'],[30,'Under $30'],[50,'Under $50'],[100,'Under $100'],[-100,'$100+']];
async function ceLoad(force){if(CE.busy)return;CE.busy=true;$('#cenote').textContent=' · loading…';
 try{CE.d=await (await fetch('/api/capital'+(force?'?force=1':''))).json();ceRender()}catch(e){$('#cenote').textContent=' · '+e.message}CE.busy=false}
function cePxOk(px){if(!CE.px||px==null)return true;return CE.px>0?px<CE.px:px>=-CE.px}
function ceRows(d){const T={put:'CSP',cc:'CC',call:'CALL',bcs:'BCS',leaps:'LEAPS'};
 return (d.rows||[]).map(r=>({r,exp:r.exp,strat:r.type==='credit'?(r.strat==='IC'?'IC':r.strat==='CCS'?'CCS':'PCS'):T[r.type],price:r.price,pop:r.p*100,ret:r.g*100,cap:r.capital,prem:r.collect,flag:!!r.flag,best:r.evd,dte:r.dte,stars:starsOf(r.p,r.g,r.l)}))}
function ceRender(){const d=CE.d,el=$('#celist');if(!el)return;const mn=PKMIN();
 const all=d&&!d.error?ceRows(d):[];
 const extra=`<label>Budget $<input data-ce="bud" type="number" min="100" step="100" value="${CE.bud}" style="width:90px"></label><label>Layout<select data-ce="grp"><option value="type"${CE.grp==='type'?' selected':''}>Best of each strategy</option><option value="one"${CE.grp==='one'?' selected':''}>One ranked list</option></select></label>`;
 $('#ceufb').innerHTML=ufb('cap',all,{stale:d&&d.stale,asof:d&&d.sources?Object.entries(d.sources).filter(([k,v])=>v&&!/error/.test(k)).map(([k,v])=>esc(k)+' '+esc(String(v).slice(5))).join(' · '):'',strats:['PCS','CCS','IC','CSP','CC','CALL','BCS','LEAPS','SWING'],minRet:mn,extra,note:`“Best overall” = capital efficiency (expected return per day your money is tied up) · at least ${mn}% return here`});
 if(!d){el.innerHTML='<p class="mute">Loading…</p>';return}if(d.error){el.innerHTML=`<p class="put">${esc(d.error)}</p>`;return}
 $('#cenote').textContent=` · ${Object.entries(d.sources||{}).filter(([k])=>k!=='swing_error').map(([k,v])=>`${k} ${v||'not run'}`).join(' · ')}`;
 const R=gfSort(gfFilter(all,{sec:'cap',minRet:mn}));
 const dedupe=L=>{const seen=new Set(),o=[];for(const x of L){const k=x.r.ticker+'|'+x.strat;if(seen.has(k))continue;seen.add(k);o.push(x)}return o};
 const row=(x,i)=>{const r=x.r,n=Math.floor(CE.bud/r.capital),pl=r.p*100,lc=r.type==='call'||r.type==='bcs'||r.type==='leaps',cs=r.calc||{};
  const calc=cs.strat?`<a href="#" onclick="calcOpen({tk:'${r.ticker}',strat:'${cs.strat}',exp:'${r.exp}'${cs.k1!=null?',k1:'+cs.k1:''}${cs.k2!=null?',k2:'+cs.k2:''}});return false">calc</a>`:'';
  return `<tr><td>${i+1}</td><td style="text-align:left"><a href="?t=${r.ticker}" onclick="go('${r.ticker}');return false"><b>${r.ticker}</b></a> ${STARS(x.stars)} <span class="mute">${r.price==null?'':LP(r.ticker,r.price,{...legK(r.legs),d:'$',up:['call','bcs','leaps'].includes(r.type)})}${r.rsi==null?'':' · RSI '+fmt(r.rsi,0)}</span>${r.flag?` <span class="amb" title="${esc(r.flag)}">⚠</span>`:''} <span data-bdg="${r.ticker}">${badgeHtml(r.ticker)}</span><br>${esc(r.name)}${dirTag(r.strat)} <span class="mute">${esc(r.legs||'')}</span>${qLine(r.quote)}<br><span class="mute" style="font-size:11.5px">${esc(r.src)}</span></td>
   <td>${dshort(r.exp)}${r.dte>250?' '+String(r.exp).slice(0,4):''}<br><span class="mute">${r.dte===0?'today':r.dte+'d'}</span></td><td><b>$${fmt(r.capital,0)}</b></td>
   <td class="call">${lc?`target +$${fmt(r.collect,0)}<br><span class="mute">${esc(r.target||'')}</span>`:`$${fmt(r.collect,0)}${r.target?`<br><span class="mute">${esc(r.target)}</span>`:''}`}</td>
   <td><b class="${pl>=85?'call':pl>=65?'amb':'put'}">${fmt(pl,pl>=99?1:0)}%</b><br><span class="mute">${lc?'of +50%':'of profit'}${r.p_model&&Math.abs(r.p_model-r.p)>0.005?` · model ${fmt(r.p_model*100,0)}%`:''}</span></td>
   <td>${fmt(r.g*100,r.g<0.05?1:0)}%</td><td class="${r.ev>=0?'call':'put'}">${r.ev>=0?'+':''}${fmt(r.ev*100,1)}%</td><td class="${r.evd>=0?'call':'put'}">${r.evd>=0?'+':''}${fmt(r.evd*100,Math.abs(r.evd*100)>=1?1:2)}%<br><span class="mute">${fmt(r.days,0)}d tied up</span></td>
   <td>${n?`<b>${n}</b> × → ${lc?'target':'collect'} <span class="call">$${fmt(n*r.collect,0)}</span><br><span class="mute">$${fmt(n*r.capital,0)} at risk</span>`:`<span class="mute">needs $${fmt(r.capital,0)}</span>`}</td><td>${calc} ${trkBtn({tk:r.ticker,exp:r.exp,strat:r.strat||r.type,legs_text:r.legs||'',what:(r.name||'')+' '+(r.legs||''),kind:(r.type==='credit'||r.type==='put')?'credit':'debit',price:(r.type==='credit'||r.type==='put')?r.collect/100:r.capital/100,capital:r.capital,rec_p:r.p,src:'Best use of capital · '+(r.src||r.type)})}</td></tr>`};
 const head='<tr><th>#</th><th>Trade</th><th>Expires</th><th>Capital</th><th>Collect / target</th><th>Chance</th><th>If it works</th><th title="chance × gain − (1 − chance) × loss">Expected</th><th>Per day</th><th>Your budget buys</th><th></th></tr>';
 const st=gst('cap');let h='';
 if(st==='SWING')h='<p class="mute">Swing trades (buy shares) are in their own card below.</p>';
 else if(st==='all'&&CE.grp==='type'){for(const [k,l] of GSTRATS){if(k==='all'||k==='SWING')continue;const L=dedupe(R.filter(x=>x.strat===k)).slice(0,Math.min(GF.n,5));
   h+=`<h3 style="margin:14px 0 4px">${l}</h3>`+(L.length?`<table>${head}${L.map(row).join('')}</table>`:((k==='CSP'||k==='CC')?`<p class="mute">None reach ${Math.max(mn,GF.ret)}% — short puts and covered calls rarely pay that much of the cash tied up in one trade.</p>`:'<p class="mute">None fit these selections — try “All” expirations, lower Min chance, or run that scan (Walls &amp; spreads scanner, Sell puts, Calls &amp; LEAPS).</p>'))}}
 else{const L=dedupe(R).slice(0,GF.n);h=L.length?`<table>${head}${L.map(row).join('')}</table>`:'<p class="mute">Nothing fits these selections.</p>'}
 el.innerHTML=h;ceSwing();loadBadges(R.slice(0,60).map(x=>x.r.ticker),()=>document.querySelectorAll('[data-bdg]').forEach(e=>e.innerHTML=badgeHtml(e.dataset.bdg)))}
document.addEventListener('change',e=>{const el=e.target.closest('[data-ce]');if(!el)return;const k=el.dataset.ce;CE[k]=k==='bud'?Math.max(100,+el.value||1000):el.value;ceSave();ceRender()});
function pkJump(id){const e=document.getElementById(id);if(e)(e.closest('.card')||e).scrollIntoView({behavior:'smooth',block:'start'})}
function ceSwing(){const el=$('#ceswing'),d=CE.d;if(!el)return;if(!d){el.innerHTML='<p class="mute">Loading…</p>';return}
 const S=d.swing||[],L=['all','SWING'].includes(gst('cap'))?S.filter(x=>pxPass(x.price)&&(!GF.cap||x.price<=GF.cap)).slice(0,Math.max(GF.n,15)):[];
 const rec=S.length?S.reduce((a,x)=>a+(x.bt_rec==null?0:x.bt_rec),0)/S.length:null;
 let h=`<p class="mute" style="margin:0 0 6px">Signals from the last 3 sessions that are still valid, from rules that beat arbitrary entries in the back-test (see <a href="#edge" onclick="tab('edge');setTimeout(()=>pkJump('aglist'),300);return false">Algos</a>). Reward and risk are measured against the stop: a 2:1 target pays 200% of the amount at risk. Uses the stock-price and max-price selections above · budget $${fmt(CE.bud,0)} · ${S.length} setups</p>`;
 if(EG.d)h+=egTape(EG.d);
 if(!L.length){el.innerHTML=h+`<p class="mute">${!['all','SWING'].includes(gst('cap'))?'Pick strategy “All” or “Swing (shares)” above to list swing trades.':S.length?'None in this price range.':'No valid setups right now — nothing has triggered in the last 3 sessions. The Early moves tab shows what is coiling.'}</p>`;return}
 h+='<table><tr><th>#</th><th>Stock</th><th title="The rule that fired, and how long ago">Setup</th><th>Buy near</th><th title="From the rule: 1.5 ATR under the entry (power factor move: under the factor day’s low)">Stop</th><th title="2× or 3× the risk, from the rule">Target</th><th>Up / down</th><th>Reward : risk</th><th title="This rule’s closed back-test trades on the app’s tickers: win rate · average result per trade in multiples of the risk (R) · last 120 sessions">Back-test</th><th title="Back-test win rate, pulled toward 36% until there are many trades, blended with this app’s live swing record once 10+ have closed — not a forecast">Chance</th><th>~Days</th><th>Your budget buys</th><th></th></tr>'+
  L.map((x,i)=>{const sh=Math.floor(CE.bud/x.price);return `<tr><td>${i+1}</td><td style="text-align:left" data-sv="${x.ticker}"><a href="?t=${x.ticker}" onclick="go('${x.ticker}');return false"><b>${x.ticker}</b></a> <span class="mute">RSI ${fmt(x.rsi,0)}</span><br><span class="mute">${esc(x.sector||'')}</span>${x.earn?` <span class="chip amb" title="Earnings ${esc(x.earn)}: a gap can jump the stop">earnings ${dshort(x.earn)}</span>`:''}</td>
   <td style="text-align:left;white-space:normal;min-width:130px"><b>${esc(x.kind)}</b> ${dirTag('LC')}<br><span class="mute">${x.age===0?'signal last session':x.age+' session'+(x.age>1?'s':'')+' ago'}${(x.also||[]).length?' · also: '+x.also.map(esc).join(', '):''}</span></td><td>${LP(x.ticker,x.price,{d:'$',up:1,stop:x.stop})}</td><td class="put">$${fmt(x.stop)}</td><td class="call">$${fmt(x.target)}</td>
   <td data-sv="${x.up}"><span class="call">+${fmt(x.up,1)}%</span> / <span class="put">−${fmt(x.down,1)}%</span></td><td data-sv="${x.rr}"><b>${fmt(x.rr,1)} : 1</b></td>
   <td data-sv="${x.bt_r}">${fmt(x.bt_win*100,0)}% won · <b class="${x.bt_r>0?'call':'put'}">${rfmt(x.bt_r)}</b><br><span class="mute">${x.bt_n} trades · recent ${x.bt_rec==null?'–':`<span class="${x.bt_rec>0?'call':'put'}">${rfmt(x.bt_rec)}</span>`}</span></td>
   <td data-sv="${x.p}">${fmt(x.p*100,0)}%${x.track?`<br><span class="mute">live record ${x.track} won</span>`:''}</td><td>~${x.days}</td>
   <td data-sv="${sh}">${sh?`<b>${sh}</b> sh ($${fmt(sh*x.price,0)})<br><span class="mute">risk $${fmt(sh*(x.price-x.stop),0)} to the stop · +$${fmt(sh*(x.target-x.price),0)} at target</span>`:`<span class="mute">1 share = $${fmt(x.price)}</span>`}</td><td>${trkBtn({tk:x.ticker,exp:'',strat:'SWING',legs_text:'shares',what:`buy shares near $${fmt(x.price)} · stop $${fmt(x.stop)} · target $${fmt(x.target)}`,kind:'debit',price:x.price,qty:Math.max(sh,1),shares:true,capital:x.price,rec_p:x.p,src:'Swing · '+x.kind})}</td></tr>`}).join('')+'</table>';
 el.innerHTML=h}
// ---- sortable columns: click any table header ----
const SORTS={};let sortBusy=false;
function sortKey(tb){const host=tb.closest('[id]');const all=host?[...host.querySelectorAll('table')]:[tb];return (host?host.id:'x')+'#'+all.indexOf(tb)}
function cellVal(td){if(!td)return null;const sv=td.dataset.sv!=null?td.dataset.sv:(td.querySelector('[data-sv]')||{dataset:{}}).dataset.sv;
 let t=(sv!=null?String(sv):td.textContent).trim().replace(/[−–—]/g,'-').replace(/^#\s*/,'');if(t===''||t==='-'||t==='n/a')return null;
 let m=t.match(/^(?:[A-Z][a-z]{2}\s+)?(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?(?!\d)/);if(m&&sv==null)return (+(m[3]||0))*10000+(+m[1])*100+(+m[2]);
 if(/^\d{4}-\d{2}-\d{2}/.test(t))return t.slice(0,16);
 m=t.match(/^([+\-]?)\s*\$?\s*([\d,]*\.?\d+)\s*(bn|b|m|k)?(?![a-z])/i);
 if(m){const n=parseFloat(m[2].replace(/,/g,''));if(!isNaN(n)){const u=(m[3]||'').toLowerCase();return (m[1]==='-'?-1:1)*n*(u==='bn'||u==='b'?1e9:u==='m'?1e6:u==='k'?1e3:1)}}
 return t.toLowerCase()}
function sortApply(tb){const st=SORTS[sortKey(tb)];if(!st)return;const rows=[...tb.rows];const hi=rows.findIndex(r=>r.cells.length&&[...r.cells].every(c=>c.tagName==='TH'));if(hi<0)return;
 const head=rows[hi];[...head.cells].forEach((c,i)=>{c.classList.toggle('sa',i===st.col&&st.dir>0);c.classList.toggle('sd',i===st.col&&st.dir<0)});
 const groups=[];for(const r of rows.slice(hi+1)){const f=r.cells[0];const follower=r.classList.contains('pkx')||(f&&f.colSpan>=3&&r.cells.length===1);
  if(follower&&groups.length)groups[groups.length-1].rows.push(r);else if(follower)return;else groups.push({rows:[r],v:cellVal(r.cells[st.col])})}
 if(groups.length<2)return;
 groups.forEach((g,i)=>g.i=i);groups.sort((a,b)=>{const x=a.v,y=b.v;if(x==null&&y==null)return a.i-b.i;if(x==null)return 1;if(y==null)return -1;
  if(typeof x==='number'&&typeof y==='number')return (x-y)*st.dir||a.i-b.i;if(typeof x==='number')return -1;if(typeof y==='number')return 1;return String(x).localeCompare(String(y))*st.dir||a.i-b.i});
 const par=head.parentNode;sortBusy=true;const frag=document.createDocumentFragment();groups.forEach(g=>g.rows.forEach(r=>frag.appendChild(r)));par.appendChild(frag);tb.dataset.srt=st.col+':'+st.dir;setTimeout(()=>{sortBusy=false},0)}
document.addEventListener('click',e=>{const th=e.target.closest('th');if(!th||e.target.closest('a,button,select,input'))return;const tb=th.closest('table');if(!tb||tb.classList.contains('nosort'))return;
 const tr=th.parentNode;if(![...tr.cells].every(c=>c.tagName==='TH'))return;const col=[...tr.cells].indexOf(th),k=sortKey(tb),cur=SORTS[k];
 let dir;if(cur&&cur.col===col)dir=-cur.dir;else{const sample=[...tb.rows].slice(tr.rowIndex+1).map(r=>cellVal(r.cells[col])).find(v=>v!=null);dir=typeof sample==='number'?-1:1}
 SORTS[k]={col,dir};tb.dataset.srt='';sortApply(tb);try{track('sort')}catch(_){}});
new MutationObserver(()=>{if(sortBusy||sortApply._t)return;sortApply._t=setTimeout(()=>{sortApply._t=0;document.querySelectorAll('table').forEach(tb=>{const st=SORTS[sortKey(tb)];if(st&&tb.dataset.srt!==st.col+':'+st.dir)sortApply(tb)})},60)}).observe(document.body,{childList:true,subtree:true});

// ---- Early moves, theme leaders, short squeeze, algos ----
let EG={d:null,stage:'all',theme:'',kind:'stocks',n:50,loaded:false,busy:false},SQ={d:null,tier:'main',loaded:false,busy:false};
try{Object.assign(EG,JSON.parse(localStorage.getItem('walls_eg')||'{}'),{d:null,loaded:false,busy:false})}catch(e){}
const egSave=()=>{try{localStorage.setItem('walls_eg',JSON.stringify({stage:EG.stage,theme:EG.theme,kind:EG.kind,n:EG.n}))}catch(e){}};
const STG={Ignition:['call','A factor move in the last 3 sessions that is still holding above its midpoint, from near the 50-day average — the earliest confirmed stage'],'Follow-through':['acc','A factor move 3–10 sessions ago, still above that day’s low'],'RS lead':['fib','Relative strength (vs SPY or vs its theme) at a 6-month high while price is still below its own high — strength showing before the breakout'],Coiling:['amb','Volatility squeezed (Bollinger width in its lowest fifth or inside Keltner). Context only: in testing, coils alone did not beat arbitrary entries'],Extended:['put','More than 6 ATR above the 50-day average — late, not early']};
const stg=s=>s?`<span class="chip ${STG[s]?STG[s][0]:'mute'}" title="${esc(STG[s]?STG[s][1]:'')}">${esc(s)}</span>`:'<span class="mute">–</span>';
const sgn=(v,d=1,suf='%')=>v==null?'<span class="mute">–</span>':`<span class="${v>=0?'call':'put'}">${v>=0?'+':'−'}${fmt(Math.abs(v),d)}${suf}</span>`;
const rfmt=v=>v==null?'–':(v>=0?'+':'−')+fmt(Math.abs(v),2)+'R';
function spk(a,color,w=120,h=30,b){if(!a||a.length<2)return'';const all=b?a.concat(b):a,mn=Math.min(...all),mx=Math.max(...all),sp=(mx-mn)||1;const pts=x=>x.map((v,i)=>`${(i/(x.length-1)*w).toFixed(1)},${(h-2-(v-mn)/sp*(h-4)).toFixed(1)}`).join(' ');
 return `<svg viewBox="0 0 ${w} ${h}" style="width:${w}px;height:${h}px;display:inline-block;vertical-align:middle" aria-hidden="true">${b?`<polyline points="${pts(b)}" fill="none" stroke="var(--mute)" stroke-width="1.2" stroke-dasharray="3 2"/>`:''}<polyline points="${pts(a)}" fill="none" stroke="${color||'var(--accent)'}" stroke-width="1.6"/></svg>`}
const tkl=(t,x)=>`<a href="?t=${t}" onclick="go('${t}');return false"><b>${t}</b></a>${x||''}`;
async function egLoad(run){if(EG.busy)return;EG.busy=true;EG.loaded=true;const n=$('#egnote');
 try{if(run){n.textContent=' · rebuilding…';await post('/api/edge/run')}
  for(let i=0;i<90;i++){const d=await (await fetch('/api/edge')).json();if(d.status&&d.status.running){n.textContent=' · '+(d.status.progress||'working')+'…';await new Promise(r=>setTimeout(r,2500));continue}
   if(d.empty&&i===0&&!run){await post('/api/edge/run');n.textContent=' · first build…';await new Promise(r=>setTimeout(r,2500));continue}
   EG.d=d;break}
  egRender();try{ceSwing()}catch(_){}}catch(e){n.textContent=' · '+e.message}EG.busy=false}
function egTape(d){const b=d.baseline||{},A=(d.algos||[]).filter(a=>a.bt&&a.bt.rec_n);if(!b.n)return'';
 const n=A.reduce((s,a)=>s+a.bt.rec_n,0),r=n?A.reduce((s,a)=>s+a.bt.rec_r*a.bt.rec_n,0)/n:null;const weak=r!=null&&r<=0;
 return `<div class="egtape ${weak?'weak':'ok'}"><b>${weak?'Breakouts have been failing lately':'Breakouts have been following through'}</b> — over the last 120 sessions these setups averaged <b>${rfmt(r)}</b> per trade (${n} trades) and arbitrary entries with the same exits <b>${rfmt(b.rec_r)}</b>. Over the whole test (${esc((d.test_from||'').slice(0,7))} → now): setups ${rfmt(A.length?A.reduce((s,a)=>s+a.bt.avg_r*a.bt.n,0)/A.reduce((s,a)=>s+a.bt.n,0):null)}, arbitrary ${rfmt(b.avg_r)}. ${weak?'<span class="put">Size down or wait: early signals work best when the tape rewards them.</span>':''} <span class="mute">R = multiples of the amount risked to the stop.</span></div>`}
const clv=n=>n>=8?`<span title="${n} of 12 conditions aligned" style="white-space:nowrap">${'🍀'.repeat(Math.min(n-7,4))}</span>`:'';
function cvRender(){const d=EG.d,el=$('#cvlist');if(!el||!d)return;const C=d.clover;if(!C){el.innerHTML='<p class="mute">Press Rebuild (Early moves) to build this.</p>';return}
 const N=C.names||{},R=(d.rows||[]).filter(r=>r.clv>=7&&pxPass(r.price)).sort((a,b)=>b.clv-a.clv||b.score-a.score),top=R.filter(r=>r.clv>=8);
 const st=(k,l)=>{const s=C[k]||{};return `<div class="gt"><div class="v">${s.n?fmt(s.up*100,0)+'% up':'–'}</div><div class="l">${l}<br>${s.n?`avg ${s.avg>=0?'+':'−'}${fmt(Math.abs(s.avg),1)}% · ${fmt(s.big*100,0)}% gained 10%+ · ${fmt(s.bad*100,0)}% lost 10%+ · ${fmt(s.n,0)} cases`:'no cases in this window'}</div></div>`};
 let h=`<div class="egtape ${top.length?'ok':''}"><b>${top.length?top.length+' aligned now':'Nothing is fully aligned right now'}</b>${R.length-top.length?` · ${R.length-top.length} one condition short`:''}. One clover = 8 of 12 conditions true together, two = 9, three = 10, four = 11 or 12. It happens on about ${C.share==null?'2':fmt(C.share*100,1)}% of stock-days.
 <b>What it has been worth</b> (20 sessions after, bought at the next open, ${esc((C.from||'').slice(0,7))} → now, this app’s tickers): see the tiles. In the longer test (2022 → 2026) 8+ aligned roughly doubled the average 20-session gain and was up about 56% of the time against 53% for any day — <b>a real tilt, not a sure thing</b>, and more clovers past the first did not reliably add more. Use it to pick where to look, with a stop.</div>
 <div class="g4r">${st('t8','🍀 or more (8+ aligned)')}${st('t2','🍀🍀 (9)')}${st('t3','🍀🍀🍀 (10)')}${st('t4','🍀🍀🍀🍀 (11–12)')}${st('all','any stock, any day')}${st('low','0–3 aligned')}</div>`;
 if(!R.length){el.innerHTML=h+'<p class="mute">No stock has even 7 of 12 right now.</p>';return}
 h+=`<table style="margin-top:8px"><tr><th>Stock</th><th title="8 aligned = one clover … 11–12 = four">Clovers</th><th title="Conditions true out of 12">Aligned</th><th title="Sessions in a row with 8+ aligned (1 = it just lined up)">Days aligned</th><th>What is aligned</th><th>What is missing</th><th title="Live extras, not part of the tested count: dealer gamma positive (dealers dampen drops), short-squeeze fuel (15%+ short and 3+ days to cover), earnings date (a gap risk)">Extras</th><th>Stage</th><th title="ATRs above the 50-day average; over 6 = extended, late">ATRs over 50d</th></tr>`+
 R.map(r=>{const on=new Set(r.clv_on||[]),chips=(ks,cls)=>ks.map(k=>`<span class="chip ${cls}" title="${esc((N[k]||[])[1]||'')}">${esc((N[k]||[k])[0])}</span>`).join(' ');
  return `<tr><td style="text-align:left" data-sv="${r.ticker}">${tkl(r.ticker)} ${LP(r.ticker,r.price,{d:'$'})} <span class="mute">RSI ${fmt(r.rsi,0)}</span><br><span class="mute">${esc(r.theme||r.kind)}</span></td>
  <td data-sv="${r.clv}">${r.clv>=8?clv(r.clv):'<span class="mute">one short</span>'}</td><td data-sv="${r.clv}"><b>${r.clv}</b> / 12</td><td data-sv="${r.clv>=8?r.clv_days:0}">${r.clv>=8?(r.clv_days>=60?'60+':r.clv_days):'<span class="mute">–</span>'}</td>
  <td style="text-align:left;white-space:normal;min-width:240px">${chips(Object.keys(N).filter(k=>on.has(k)),'call')}</td><td style="text-align:left;white-space:normal;min-width:150px">${chips(Object.keys(N).filter(k=>!on.has(k)),'mute')||'<span class="call">nothing</span>'}</td>
  <td style="text-align:left;white-space:normal">${[r.gamma_pos===true?'<span class="chip call" title="Net dealer gamma is positive: dealers tend to dampen moves">gamma +</span>':r.gamma_pos===false?'<span class="chip put" title="Net dealer gamma is negative: moves get amplified both ways">gamma −</span>':'',r.sq_fuel?'<span class="chip acc" title="15%+ of shares short and 3+ days to cover (see Short squeeze monitor)">squeeze fuel</span>':'',r.earn?`<span class="chip amb" title="Earnings ${esc(r.earn)}: a gap can jump the stop">earnings ${dshort(r.earn)}</span>`:''].filter(Boolean).join(' ')||'<span class="mute">–</span>'}</td>
  <td data-sv="${r.stage||'~'}">${stg(r.stage)}</td><td data-sv="${r.ext}"><span class="${r.ext>6?'put':''}">${fmt(r.ext,1)}</span></td></tr>`}).join('')+'</table>';
 el.innerHTML=h}
function egRender(){const d=EG.d,el=$('#eglist');if(!el||!d)return;try{lvRender()}catch(_){}try{cvRender()}catch(_){}if(d.empty){el.innerHTML='<p class="mute">Not built yet — press Rebuild.</p>';return}
 $('#egnote').textContent=` · daily bars through ${d.asof} · ${d.universe} tickers · built ${d.ts}`+(d.status&&d.status.error?' · last error: '+d.status.error:'');
 const themes=[...new Set((d.rows||[]).map(r=>r.theme).filter(Boolean))].sort();const cnt=s=>(d.rows||[]).filter(r=>r.stage===s).length;
 let L=(d.rows||[]).filter(r=>(EG.stage==='all'?true:r.stage===EG.stage)&&(!EG.theme||r.theme===EG.theme)&&(EG.kind==='all'||(EG.kind==='etf'?r.kind==='ETF':r.kind!=='ETF'))&&pxPass(r.price));
 const tot=L.length;L=L.slice(0,EG.n);
 let h=egTape(d)+`<div class="ufb"><div class="ctl"><span class="mute">Stage</span>${[['all','All'],['Ignition','Ignition'],['Follow-through','Follow-through'],['RS lead','RS lead'],['Coiling','Coiling'],['Extended','Extended']].map(([k,l])=>`<button type="button" class="seg${EG.stage===k?' on':''}" onclick="EG.stage='${k}';egSave();egRender()" title="${esc(STG[k]?STG[k][1]:'Every stage, best score first')}">${l}${k==='all'?'':' <span class="mute">'+cnt(k)+'</span>'}</button>`).join('')}</div>
 <div class="ctl"><span class="mute">Show</span>${[['stocks','Stocks'],['etf','ETFs'],['all','Both']].map(([k,l])=>`<button type="button" class="seg${EG.kind===k?' on':''}" onclick="EG.kind='${k}';egSave();egRender()">${l}</button>`).join('')}<span class="vsep"></span>${[25,50,100,400].map(k=>`<button type="button" class="seg${EG.n===k?' on':''}" onclick="EG.n=${k};egSave();egRender()">${k===400?'All':'Top '+k}</button>`).join('')}
 <select style="width:auto;font-size:12.5px" onchange="EG.theme=this.value;egSave();egRender()"><option value="">Every theme</option>${themes.map(t=>`<option${EG.theme===t?' selected':''}>${esc(t)}</option>`).join('')}</select></div></div>`;
 if(!L.length){el.innerHTML=h+'<p class="mute">Nothing in this stage right now.</p>';return}
 h+=`<table><tr><th>Stock</th><th title="Where the move is in its life: Ignition is earliest">Stage</th><th title="0–100: recent factor move, relative-strength highs vs theme and SPY, theme leading, accumulation, trend; minus points when extended">Early score</th><th title="Latest day up 1.5+ ATR on above-average volume, closing in the top half (last 10 sessions)">Factor move</th><th title="Last session: % change · volume vs its 50-day average">Last day</th><th title="Stock ÷ SPY: change over 3 months · ★ = the line is at a 6-month high · ‘before price’ = price still 3%+ under its own high">RS vs SPY</th><th title="Stock ÷ its theme basket (equal-weight index of the theme’s stocks): change over 3 months · ★ = 6-month high — a true theme leader">RS vs theme</th><th title="Is the theme itself beating SPY (theme ÷ SPY above its 50-day average and above a month ago)?">Theme</th><th title="Up-day volume ÷ down-day volume over 50 sessions (1.3+ = accumulation) · pocket pivots in the last 10 sessions">Accumulation</th><th title="How far above the 50-day average, in ATRs. Over 6 = extended">ATRs over 50d</th><th>Earnings</th></tr>`+
 L.map(r=>`<tr><td style="text-align:left" data-sv="${r.ticker}">${tkl(r.ticker)} ${clv(r.clv||0)} ${LP(r.ticker,r.price,{d:'$'})} <span class="mute">RSI ${fmt(r.rsi,0)}</span>${r.coil?' <span class="chip amb" title="Volatility squeezed (Bollinger width '+(r.bbw_pct==null?'low':'in the lowest '+fmt(r.bbw_pct,0)+'% of 6 months')+')">coil</span>':''}<br><span class="mute">${esc(r.theme||r.kind)}</span></td>
 <td data-sv="${r.stage||'~'}">${stg(r.stage)}</td><td data-sv="${r.score}"><b>${fmt(r.score,0)}</b></td>
 <td data-sv="${r.fm_days==null?'':(100-r.fm_days*10+r.fm_x)}">${r.fm_days==null?'<span class="mute">–</span>':`<b>${fmt(r.fm_x,1)}× ATR</b> · vol ${fmt(r.fm_rvol,1)}×<br><span class="mute">${r.fm_days===0?'last session':r.fm_days+'d ago'} · ${r.held?'holding':'<span class="put">gave back half</span>'} · low $${fmt(r.fm_low)}</span>`}</td>
 <td data-sv="${r.d1}">${sgn(r.d1)}<br><span class="mute">vol ${r.rvol==null?'–':fmt(r.rvol,1)+'×'}</span></td>
 <td data-sv="${r.rs63==null?'':r.rs63}">${sgn(r.rs63)}${r.rs_nh?' <span class="call" title="RS line at a 6-month high">★</span>':''}${r.rs_lead?'<br><span class="chip fib">before price</span>':''}</td>
 <td data-sv="${r.th63==null?'':r.th63}">${r.th63==null?'<span class="mute">–</span>':sgn(r.th63)}${r.th_nh?' <span class="call" title="Stock ÷ theme at a 6-month high">★</span>':''}${r.th_lead?'<br><span class="chip fib">before price</span>':''}</td>
 <td data-sv="${r.theme_up?1:0}">${r.kind==='ETF'||!r.theme?'<span class="mute">–</span>':r.theme_up?'<span class="chip call">leading</span>':'<span class="chip mute">lagging</span>'}</td>
 <td data-sv="${r.udvr==null?'':r.udvr}">${r.udvr==null?'–':`<span class="${r.udvr>=1.3?'call':r.udvr<0.8?'put':''}">${fmt(r.udvr,2)}</span>`}${r.pp10?`<br><span class="mute">${r.pp10} pocket pivot${r.pp10>1?'s':''}</span>`:''}</td>
 <td data-sv="${r.ext}"><span class="${r.ext>6?'put':r.ext<0?'mute':''}">${fmt(r.ext,1)}</span></td><td data-sv="${r.earn||''}">${r.earn?dshort(r.earn):'<span class="mute">–</span>'}</td></tr>`).join('')+'</table>'+(tot>L.length?`<p class="mute">${L.length} of ${tot} shown.</p>`:'');
 el.innerHTML=h;thRender();agRender();try{liveFetch()}catch(_){}}
function thRender(){const d=EG.d,el=$('#thlist');if(!el||!d)return;const T=d.themes||[];if(!T.length){el.innerHTML='<p class="mute">No themes yet.</p>';return}
 el.innerHTML=`<table><tr><th>Theme</th><th title="Stocks in the basket (3+ = equal-weight index of the stocks; fewer = the sector ETF)">Stocks</th><th title="Theme minus SPY, 1 month">vs SPY 1m</th><th title="Theme minus SPY, 3 months">vs SPY 3m</th><th>1 month</th><th>3 months</th><th title="Theme ÷ SPY above its 50-day average and higher than a month ago">Leading SPY?</th><th title="Share of the theme’s stocks above their 50-day average">Above 50d</th><th title="Stocks with a factor move in the last 3 sessions">Factor moves</th><th title="Stocks beating the theme itself: ★ = stock ÷ theme at a 6-month high">True leaders</th></tr>`+
 T.map(t=>`<tr><td style="text-align:left"><a href="#" onclick="EG.theme='${esc(t.theme)}';EG.stage='all';egSave();egRender();pkJump('eglist');return false"><b>${esc(t.theme)}</b></a>${t.rs_nh?' <span class="call" title="Theme ÷ SPY at a 6-month high">★</span>':''}</td><td>${t.n}${t.basket?'':' <span class="mute">(ETF)</span>'}</td><td data-sv="${t.rs1}">${sgn(t.rs1)}</td><td data-sv="${t.rs3}">${sgn(t.rs3)}</td><td data-sv="${t.r1}">${sgn(t.r1)}</td><td data-sv="${t.r3}">${sgn(t.r3)}</td>
 <td data-sv="${t.up?1:0}">${t.up?'<span class="chip call">leading</span>':'<span class="chip mute">no</span>'}</td><td data-sv="${t.breadth}">${fmt(t.breadth,0)}%</td><td>${t.fm3||'<span class="mute">0</span>'}</td>
 <td style="text-align:left">${(t.leaders||[]).map(l=>tkl(l.ticker,l.th_nh?'<span class="call">★</span>':'')+(l.th63==null?'':' <span class="mute">'+(l.th63>=0?'+':'−')+fmt(Math.abs(l.th63),0)+'%</span>')).join(' · ')}</td></tr>`).join('')+'</table>'}
function agRender(){const d=EG.d,el=$('#aglist');if(!el||!d)return;const A=d.algos||[],b=d.baseline||{};
 const row=(nm,rules,s,extra)=>`<tr><td style="text-align:left"><b>${esc(nm)}</b>${rules?`<details class="fibhelp"><summary class="mute">rules</summary><p class="mute" style="max-width:420px;white-space:normal">${esc(rules)}</p></details>`:''}</td><td>${s.n||0}</td><td data-sv="${s.win==null?'':s.win}">${s.n?fmt(s.win*100,0)+'%':'–'}</td><td data-sv="${s.avg_r==null?'':s.avg_r}"><b class="${(s.avg_r||0)>0?'call':'put'}">${s.n?rfmt(s.avg_r):'–'}</b></td><td>${s.pf==null?'–':fmt(s.pf,2)}</td><td>${s.n?fmt(s.target*100,0)+'%':'–'}</td><td data-sv="${s.dd_r==null?'':-s.dd_r}">${s.n?'−'+fmt(s.dd_r,0)+'R':'–'}</td><td data-sv="${s.rec_r==null?'':s.rec_r}">${s.rec_n?`<span class="${s.rec_r>0?'call':'put'}">${rfmt(s.rec_r)}</span> <span class="mute">(${s.rec_n})</span>`:'<span class="mute">–</span>'}</td><td>${s.eq?spk(s.eq,(s.avg_r||0)>0?'var(--call)':'var(--put)'):''}</td>${extra||'<td></td><td></td>'}</tr>`;
 let h=`<p class="mute" style="margin:0 0 6px">Fixed rules run on ${d.universe} tickers’ daily bars (${d.bars} sessions each). <b>Paper only — this app never places orders.</b> Every signal is logged the day it appears and then graded by the same rules, so a forward record builds next to the back-test.</p>
 <table><tr><th>Strategy</th><th title="Closed back-test trades">Trades</th><th>Win %</th><th title="Average result per trade in multiples of the amount risked, after 0.1% costs">Avg per trade</th><th title="Gross wins ÷ gross losses">Profit factor</th><th>Hit target</th><th title="Worst peak-to-trough of the running total">Max drawdown</th><th title="Last 120 sessions: average per trade (number of trades)">Recent</th><th>Running total</th><th title="Signals logged live by this app since 2026-11-12, graded forward">Forward record</th><th>Open now</th></tr>`+
 A.map(a=>row(a.name,a.rules,a.bt,`<td>${a.paper&&a.paper.n?`${rfmt(a.paper.avg_r)} <span class="mute">(${a.paper.n}, ${fmt(a.paper.win*100,0)}% won)</span>`:'<span class="mute">builds from today</span>'}${a.paused?' <span class="chip put" title="20+ paper trades closed below arbitrary entries: paused - not used for swing picks; still logged and graded so it can return">paused</span>':''}</td><td>${(a.live||[]).length||'<span class="mute">0</span>'}${a.beats?'':' <span class="chip mute" title="Has not beaten arbitrary entries in this window — shown for the record, not used for swing trades">not used</span>'}</td>`)).join('')+
 row('Arbitrary entries (same exits)','Every 10th session on every ticker: stop 1.5 ATR, target 2× the risk, 15 sessions. The bar a setup has to clear.',b)+'</table>';
 const LV=A.flatMap(a=>(a.live||[]).map(x=>({...x,algo:a.name})));
 if(LV.length)h+=`<h3 style="margin:12px 0 4px">Signals now</h3><table><tr><th>Stock</th><th>Strategy</th><th>Signal day</th><th>Status</th><th>Entry</th><th>Stop</th><th>Target</th><th>So far</th></tr>`+LV.map(x=>`<tr><td style="text-align:left">${tkl(x.ticker)} ${LP(x.ticker,x.price,{d:'$',stop:x.stop})}</td><td style="text-align:left">${esc(x.algo)}</td><td>${dshort(x.sig)}</td><td>${x.status==='pending'?'<span class="chip amb" title="Signal on the last session: the rule buys the next open">buy next open</span>':'<span class="chip acc">open</span>'}</td><td>${x.entry?'$'+fmt(x.entry):'<span class="mute">next open</span>'}</td><td class="put">${x.stop?'$'+fmt(x.stop):'–'}</td><td class="call">${x.target?'$'+fmt(x.target):'–'}</td><td data-sv="${x.r==null?'':x.r}">${x.status==='pending'?'–':rfmt(x.r)}</td></tr>`).join('')+'</table>';
 const R=d.rotation;if(R)h+=`<h3 style="margin:14px 0 4px">ETF rotation</h3><p class="mute" style="margin:0 0 4px">Every 5 sessions hold the 3 sector / theme ETFs with the best blend of 3-month and 1-month return that are rising and above their 50-day average (empty slots stay in cash).</p>
 <div class="g4r"><div class="gt"><div class="v ${R.ret>=0?'call':'put'}">${sgn(R.ret*100,1)}</div><div class="l">rotation since ${dshort(R.from)} · worst drop −${fmt(R.dd*100,0)}%</div></div><div class="gt"><div class="v">${sgn(R.spy*100,1)}</div><div class="l">SPY same period · worst drop −${fmt(R.spy_dd*100,0)}%</div></div>
 <div class="gt"><div class="v">${(R.holding||[]).map(x=>tkl(x.ticker)).join(' · ')||'cash'}</div><div class="l">holding now${R.next_in?` · next rebalance in ${R.next_in} session${R.next_in>1?'s':''}`:' · rebalances next session'}</div></div>
 <div class="gt"><div class="v">${(R.next||[]).map(x=>tkl(x.ticker)).join(' · ')||'cash'}</div><div class="l">would pick today</div></div></div><div style="margin-top:6px">${spk(R.eq,'var(--accent)',300,54,R.spy_eq)} <span class="mute">solid = rotation · dashed = SPY</span></div>`;
 const P=(d.paper_trades||[]).slice(0,40);if(P.length)h+=`<details class="fibhelp" style="margin-top:10px"><summary class="mute">Forward log (${(d.paper_trades||[]).length} signals logged live)</summary><table><tr><th>Stock</th><th>Strategy</th><th>Signal</th><th>Status</th><th>Entry</th><th>Exit</th><th>Result</th><th>Why</th></tr>`+P.map(p=>`<tr><td style="text-align:left">${tkl(p.ticker)}</td><td style="text-align:left">${esc(((A.find(a=>a.key===p.algo)||{}).name)||p.algo)}</td><td>${dshort(p.sig)}</td><td>${esc(p.status||'')}</td><td>${p.entry?'$'+fmt(p.entry):'–'}</td><td>${p.exit?'$'+fmt(p.exit)+' '+dshort(p.exit_date):'–'}</td><td data-sv="${p.r==null?'':p.r}">${p.r==null?'–':`<span class="${p.r>0?'call':'put'}">${rfmt(p.r)}</span>`}</td><td>${esc(p.why||'')}</td></tr>`).join('')+'</table></details>';
 el.innerHTML=h}
function lvRender(){const d=EG.d,el=$('#lvlist');if(!el||!d)return;const V=d.lev||{},R=V.rows||[],S=V.stats||{};
 if(V.error||!R.length){el.innerHTML=`<p class="mute">${V.error?esc(V.error):'Not built yet — press Rebuild (first run loads ~80 price histories).'}</p>`;return}
 const pc=v=>v==null?'–':(v>=0?'+':'−')+fmt(Math.abs(v),1)+'%';
 const sig=r=>{const out=[];for(const k of ['DIP','WASH','FM']){const n=(r.now||{})[k];if(!n)continue;const nm=S[k]?S[k].name:k;
   if(n.state==='buy')out.push(`<span class="chip call" title="${esc(S[k].rules)}">BUY ${r.bull} next open</span> <span class="mute">${esc(nm)}</span>`);
   else if(n.state==='in')out.push(`<span class="chip acc" title="${esc(S[k].rules)}">in ${r.bull}, day ${n.days}</span> <span class="mute">${esc(nm)} · ${pc(n.pl)} · sell after ${r.u} closes ${k==='FM'?'below $'+fmt(r.s10):'above $'+fmt(r.s5)} or in ${n.left} session${n.left===1?'':'s'}</span>`);
   else out.push(`<span class="chip amb">SELL ${r.bull} next open</span> <span class="mute">${esc(nm)} · ${pc(n.pl)}</span>`)}
  return out.join('<br>')||'<span class="mute">–</span>'};
 const rank=r=>{const n=r.now||{};return Object.values(n).some(x=>x.state==='buy')?0:Object.values(n).some(x=>x.state==='exit')?1:Object.keys(n).length?2:3};
 let h=`<table><tr><th>Rule</th><th title="Closed trades across all pairs in the cached history (~2 years)">Trades</th><th>Win %</th><th title="Average result per trade in the bull fund, after 0.1% costs">Avg per trade</th><th>Typical</th><th title="The worst single trade — size positions so this is survivable">Worst trade</th><th>Best</th><th>Avg sessions</th><th title="Last 120 sessions: average per trade (trades)">Recent</th></tr>`+
  ['DIP','WASH','FM'].filter(k=>S[k]).map(k=>{const s=S[k];return `<tr><td style="text-align:left"><b>${esc(s.name)}</b><details class="fibhelp"><summary class="mute">rules</summary><p class="mute" style="max-width:440px;white-space:normal">${esc(s.rules)}</p></details></td><td>${s.n}</td><td data-sv="${s.win==null?'':s.win}">${s.n?fmt(s.win*100,0)+'%':'–'}</td><td data-sv="${s.avg==null?'':s.avg}"><b class="${(s.avg||0)>0?'call':'put'}">${pc(s.avg)}</b></td><td>${pc(s.med)}</td><td class="put">${pc(s.worst)}</td><td class="call">${pc(s.best)}</td><td>${s.days==null?'–':fmt(s.days,1)}</td><td data-sv="${s.rec_avg==null?'':s.rec_avg}">${s.rec_n?`<span class="${s.rec_avg>0?'call':'put'}">${pc(s.rec_avg)}</span> <span class="mute">(${s.rec_n}, ${fmt(s.rec_win*100,0)}% won)</span>`:'<span class="mute">–</span>'}</td></tr>`}).join('')+'</table>';
 h+=`<h3 style="margin:12px 0 4px">Pairs</h3><table><tr><th>Underlying</th><th title="Signals are read from the underlying; trades use the bull fund">Signal now</th><th title="2-day RSI of the underlying: under 5 is the dip trigger">RSI(2)</th><th title="Underlying vs its 200-day and 50-day averages">Trend</th><th>Bull fund</th><th title="Shown for reference only — no inverse-fund rule passed testing">Bear fund</th><th title="Underlying’s 20-day realized volatility, annualized. Higher = more daily-reset drag">Volatility</th><th title="3 months: underlying · bull fund">3-month</th><th title="Bull fund’s 3-month return minus leverage × the underlying’s. Negative = decay from daily resetting">Drag</th><th title="This pair’s own closed trades: dip · washout · factor move (win %, avg)">This pair’s record</th></tr>`+
  R.slice().sort((a,b)=>rank(a)-rank(b)||a.rsi2-b.rsi2).map(r=>`<tr><td style="text-align:left" data-sv="${r.u}">${tkl(r.u)} ${LP(r.u,r.u_px,{d:'$'})} <span class="mute">RSI ${fmt(r.u_rsi,0)}</span></td><td style="text-align:left;white-space:normal;min-width:170px" data-sv="${rank(r)}">${sig(r)}</td>
  <td data-sv="${r.rsi2}"><b class="${r.rsi2<5?'call':r.rsi2>95?'put':''}">${fmt(r.rsi2,0)}</b></td><td data-sv="${(r.a200?2:0)+(r.a50?1:0)}">${r.a200==null?'–':r.a200?'<span class="call">above 200d</span>':'<span class="put">below 200d</span>'}<br><span class="mute">${r.a50?'above':'below'} 50d</span></td>
  <td style="text-align:left" data-sv="${r.bull}">${tkl(r.bull)} <span class="mute">${r.lev}×</span> ${LP(r.bull,r.bull_px,{d:'$'})} <span class="mute">RSI ${fmt(r.bull_rsi,0)}</span></td>
  <td style="text-align:left" data-sv="${r.bear||''}">${r.bear?`${tkl(r.bear)} <span class="mute">−${r.lev}×</span> $${fmt(r.bear_px)} <span class="mute">RSI ${fmt(r.bear_rsi,0)}</span>`:'<span class="mute">–</span>'}</td>
  <td data-sv="${r.rv20}">${fmt(r.rv20,0)}%</td><td data-sv="${r.bull63}">${sgn(r.u63)} · ${sgn(r.bull63)}</td><td data-sv="${r.drag==null?'':r.drag}">${r.drag==null?'–':sgn(r.drag)}</td>
  <td style="white-space:normal;min-width:150px">${['DIP','WASH','FM'].map(k=>{const b=(r.bt||{})[k]||{};return b.n?`${b.n}: ${fmt(b.win*100,0)}%, <span class="${b.avg>0?'call':'put'}">${pc(b.avg)}</span>`:'<span class="mute">0</span>'}).join(' · ')}</td></tr>`).join('')+'</table>';
 el.innerHTML=h}
$('#egref').onclick=()=>egLoad(true);
// short squeeze monitor
async function sqLoad(run){if(SQ.busy)return;SQ.busy=true;SQ.loaded=true;const n=$('#sqnote');
 try{if(run){n.textContent=' · starting…';await post('/api/squeeze/run')}
  for(let i=0;i<150;i++){const d=await (await fetch('/api/squeeze')).json();if(d.status&&d.status.running){n.textContent=' · '+(d.status.progress||'working')+'…';if(d.rows&&!SQ.d){SQ.d=d;sqRender(true)}await new Promise(r=>setTimeout(r,3000));continue}SQ.d=d;break}
  sqRender()}catch(e){n.textContent=' · '+e.message}SQ.busy=false}
const SQT={pass:['call','Passes every filter'],near:['amb','Near miss — fails one filter'],watch:['mute','Wider watch — fails two or more of your filters']};
function sqRender(keepNote){const d=SQ.d,el=$('#sqlist');if(!el||!d)return;if(d.empty){el.innerHTML='<p class="mute">Not run yet — press Run now (about 2 minutes).</p>'+(d.status&&d.status.error?`<p class="err">${esc(d.status.error)}</p>`:'');if(!keepNote)$('#sqnote').textContent='';return}
 if(!keepNote)$('#sqnote').textContent=` · run ${d.ts}`+(d.status&&d.status.error?' · last error: '+d.status.error:'');
 const R=d.rows||[],np=R.filter(r=>r.tier==='pass').length,nn=R.filter(r=>r.tier==='near').length;
 let L=R.filter(r=>SQ.tier==='all'?true:SQ.tier==='main'?r.tier!=='watch':r.tier==='watch').filter(r=>pxPass(r.px));
 let h=`<div class="egtape ${np?'ok':''}"><b>${np?np+' pass every filter':'No stock passes every filter today'}</b> · ${nn} near misses (one filter short) · ${d.candidates} stocks with 10%+ short interest out of ${fmt(d.screened,0)} screened.
 <span class="mute">Short interest: FINRA, settlement ${dshort(d.si_date)} (published twice a month, ~8 business days late). Borrow fee &amp; shares available: Interactive Brokers public file${d.borrow_asof?', '+esc(d.borrow_asof):' — not reachable this run'}${d.borrow_days<3?' · borrow trend needs 3 saved days ('+d.borrow_days+' so far) — until then the score uses the level, at 70% weight':''}. Float = SEC public float ÷ price that day (estimate). Not vetted for shells or pumps — check filings before trading.</span></div>
 <div class="ctl"><span class="mute">Show</span>${[['main',`Pass + near misses <span class="mute">${np+nn}</span>`],['watch','Wider watch'],['all','All']].map(([k,l])=>`<button type="button" class="seg${SQ.tier===k?' on':''}" onclick="SQ.tier='${k}';sqRender()">${l}</button>`).join('')}</div>`;
 if(!L.length){el.innerHTML=h+'<p class="mute">Nothing in this group.</p>';return}
 h+=`<table><tr><th>#</th><th>Stock</th><th title="Your filters: market cap $100M–$3B · float under 15M · average volume over 500K · short interest 20%+ · days to cover over 3 · not already up 60% in 5 days">Filters</th><th title="Your weights: borrow fee rising 25 · fewer shares to borrow 20 · relative volume 15 · catalyst within 30 days 15 · small float 10 · bullish gamma 10 · base breakout 5">Score</th><th title="Quiet → Building (volume picking up on an up day) → Igniting (factor move, or 2× volume and +5%) → Running (already up 60% in 5 days: late)">State</th><th title="Short interest as % of the float (or of shares outstanding when the float is not confirmed)">Short %</th><th title="Change in shares short vs the prior report">Δ short</th><th>Days to cover</th><th title="Estimated free float · shares outstanding">Float</th><th>Mkt cap</th><th title="Annual fee to borrow the stock (Interactive Brokers) · earlier saved value when there is one">Borrow fee</th><th title="Shares available to borrow · as % of shares short">To borrow</th><th title="Today’s volume ÷ its average daily volume">Vol ×</th><th>Today</th><th title="Off-exchange short volume as % of off-exchange volume, last 5 sessions (20-session figure in grey). High and rising = shorts pressing; it is flow, not open positions">Short vol 5d</th><th>Earnings</th><th>Why it ranks</th></tr>`+
 L.map((r,i)=>{const sp=r.si_float!=null&&r.si_float<=200?r.si_float:r.si_pct,mv=r.prev==null?'<span class="chip acc">new</span>':r.prev>i+1?`<span class="call">▲${r.prev-i-1}</span>`:r.prev<i+1?`<span class="put">▼${i+1-r.prev}</span>`:'';
  return `<tr><td>${i+1} ${SQ.tier==='main'?mv:''}</td><td style="text-align:left" data-sv="${r.ticker}">${tkl(r.ticker)} ${LP(r.ticker,r.px,{d:'$'})} <span class="mute">RSI ${r.rsi==null?'–':fmt(r.rsi,0)}</span><br><span class="mute">${esc(r.name||'')}</span></td>
  <td style="text-align:left;white-space:normal;min-width:130px" data-sv="${r.tier==='pass'?0:r.fails.length}"><span class="chip ${SQT[r.tier][0]}" title="${SQT[r.tier][1]}">${r.tier==='pass'?'pass':r.tier==='near'?'near miss':'watch'}</span>${r.fails.length?'<br><span class="mute">'+r.fails.map(esc).join('; ')+'</span>':''}</td>
  <td data-sv="${r.score}"><b>${fmt(r.score,0)}</b></td><td>${`<span class="chip ${r.state==='Igniting'?'call':r.state==='Building'?'acc':r.state==='Running'?'put':'mute'}">${r.state}</span>`}</td>
  <td data-sv="${sp}"><b>${fmt(sp,1)}%</b><br><span class="mute">${r.si_float!=null&&r.si_float<=200?'of float':'of shares out'}</span></td><td data-sv="${r.si_chg==null?'':r.si_chg}">${sgn(r.si_chg)}</td><td>${fmt(r.dtc,1)}</td>
  <td data-sv="${r.float||r.shares}">${r.float?fmt(r.float/1e6,1)+'M':'<span class="mute">?</span>'}<br><span class="mute">${fmt(r.shares/1e6,1)}M out</span></td><td data-sv="${r.cap}">${money(r.cap)}</td>
  <td data-sv="${r.fee==null?'':r.fee}">${r.fee==null?'<span class="mute">–</span>':`<b class="${r.fee>=20?'put':r.fee>=5?'amb':''}">${fmt(r.fee,1)}%</b>`}${r.fee_then!=null?`<br><span class="mute">was ${fmt(r.fee_then,1)}%</span>`:''}</td>
  <td data-sv="${r.avail==null?'':r.avail}">${r.avail==null?'<span class="mute">–</span>':money(r.avail).replace('$','')+`<br><span class="mute">${fmt(100*r.avail/r.short,1)}% of short</span>`}</td>
  <td data-sv="${r.rvol==null?'':r.rvol}">${r.rvol==null?'–':`<span class="${r.rvol>=2?'call':''}">${fmt(r.rvol,1)}×</span>`}</td><td data-sv="${r.chg==null?'':r.chg}">${sgn(r.chg)}</td>
  <td data-sv="${r.svr5==null?'':r.svr5}">${r.svr5==null?'–':fmt(r.svr5*100,0)+'%'}${r.svr20==null?'':` <span class="mute">${fmt(r.svr20*100,0)}%</span>`}</td><td data-sv="${r.earn||''}">${r.earn?dshort(r.earn):'<span class="mute">–</span>'}</td>
  <td style="text-align:left;white-space:normal;min-width:150px">${(r.why||[]).map(esc).join(' · ')||'<span class="mute">–</span>'}</td></tr>`}).join('')+'</table>';
 if((d.removed||[]).length)h+=`<p class="mute">Dropped since the last run: ${d.removed.map(t=>tkl(t)).join(', ')}</p>`;
 el.innerHTML=h;try{liveFetch()}catch(_){}}
$('#sqref').onclick=()=>sqLoad(true);
// tax-loss bounce (year-end selling -> January rebound)
const TL={tier:'main'};
async function tlLoad(run){if(TL.busy)return;TL.busy=true;TL.loaded=true;const n=$('#tlnote');
 try{if(run){n.textContent=' · starting…';await post('/api/taxloss/run')}
  for(let i=0;i<240;i++){const d=await (await fetch('/api/taxloss')).json();if(d.status&&d.status.running){n.textContent=' · '+(d.status.progress||'working')+'…';if(!TL.d){TL.d=d;tlRender(true)}await new Promise(r=>setTimeout(r,3000));continue}TL.d=d;break}
  tlRender()}catch(e){n.textContent=' · '+e.message}TL.busy=false}
const tlD=d=>dshort(d).slice(4);
function tlPut(o){const p=o&&o.put;if(!o)return'<span class="mute">-</span>';if(o.error)return'<span class="mute" title="'+esc(o.error)+'">no chain</span>';
 if(!p)return'<span class="mute" title="No put from 30 days out to the January monthly pays your minimum return on the cash at a 60%+ chance of profit">none passes</span>';
 return `<b>${tlD(p.exp)} ${fmt(p.short,p.short%1?1:0)}P</b> @ ${fmt(p.credit)} <span class="mute">${p.dte}d</span><br>POP <b class="${p.weak?'amb':''}">${fmt(p.pop,0)}%</b> · ${fmt(p.roc,1)}% on $${fmt(p.cash,0)}`+
  (p.weak?' <span class="chip amb" title="Best available, but under your 70% chance-of-profit bar">under 70%</span>':'')+
  (p.below_wall?' <span class="chip call" title="Strike is at or below the put wall ($'+fmt(p.put_wall,p.put_wall%1?1:0)+')">below wall</span>':'')+
  (p.short<=55?' <span class="chip acc" title="Strike fits your wheel cap ($55 a share)">wheel fit</span>':'')+
  (p.earn_cross?' <span class="chip amb" title="Earnings fall before this expiration">earnings</span>':'')}
function tlCall(o){const c=o&&o.call;if(!o)return'<span class="mute">-</span>';if(o.error)return'<span class="mute">no chain</span>';
 if(!c)return`<span class="mute" title="${(o.call_exps||[]).length?'No liquid call with delta 0.40-0.70 in '+o.call_exps.map(tlD).join(', '):'February-April expirations are not listed yet for this stock (most list after the December expiration)'}">${(o.call_exps||[]).length?'none passes':'not listed yet'}</span>`;
 return `<b>${tlD(c.exp)} ${fmt(c.long,c.long%1?1:0)}C</b> @ ${fmt(c.debit)} <span class="mute">$${fmt(c.cost,0)} · ${c.dte}d</span><br>POP ${fmt(c.pop,0)}% · delta ${fmt(c.delta,2)} · breakeven ${sgn(c.be_pct)}`+(c.earn_cross?' <span class="chip amb" title="Earnings fall before this expiration">earnings</span>':'')}
function tlSpread(o){const c=o&&o.spread;if(!o)return'<span class="mute">-</span>';if(o.error)return'<span class="mute">no chain</span>';
 if(!c)return`<span class="mute" title="No call spread pays at least 1:1 with a tradable bid/ask">${(o.call_exps||[]).length?'none passes':'not listed yet'}</span>`;
 return `<b>${tlD(c.exp)} ${fmt(c.long,c.long%1?1:0)}/${fmt(c.short,c.short%1?1:0)}C</b> @ ${fmt(c.debit)} <span class="mute">$${fmt(c.cost,0)}</span><br>POP ${fmt(c.pop,0)}% · max <span class="call">+$${fmt(c.maxprofit,0)}</span> (${fmt(c.rr*100,0)}%)`}
const TLS={Turning:['call','Above its 10-day average and up over 5 sessions, 3+ sessions after the low'],Basing:['acc','No new low in 3+ sessions, not yet turning up'],'Still falling':['put','Made its 52-week low in the last 3 sessions']};
function tlRender(keepNote){const d=TL.d,el=$('#tllist');if(!el||!d)return;const c=d.cal||{};let h='';
 if(c.steps)h+=`<div class="egtape ${c.phase==='buy'||c.phase==='exit'?'ok':''}"><b>Now: ${esc(c.name)}</b> · ${esc(c.do)}<div style="margin-top:6px;line-height:2">`+
  c.steps.map(s=>`<span class="chip ${s.key===c.phase?'call big':'mute'}" title="${esc(s.do)}">${esc(s.name)} ${tlD(s.from)}–${tlD(s.to)}</span>`).join(' ')+`</div></div>`;
 if(d.empty){el.innerHTML=h+'<p class="mute">Not run yet - press Run now (5-8 minutes the first time).</p>'+(d.status&&d.status.error?`<p class="err">${esc(d.status.error)}</p>`:'');if(!keepNote)$('#tlnote').textContent='';return}
 if(!keepNote)$('#tlnote').textContent=` · run ${d.ts}`+(d.status&&d.status.error?' · last error: '+d.status.error:'');
 const R=d.rows||[],K=d.rules||{};
 h+=`<div class="egtape ${d.n_pass?'ok':''}"><b>${d.n_pass?d.n_pass+' pass every filter':'No stock passes every filter today'}</b> · ${d.n_near} near misses · ${fmt(d.universe,0)} small and mid caps screened, top ${R.length} by score listed.
 <span class="mute">Filters: down ${-K.ytd}%+ this year · within ${K.low}% of the 52-week low · market cap ${money(K.cap_lo)}–${money(K.cap_hi)} · price $${K.px}+ · trades $5M+ a day. ${d.src==='schwab'?'52-week ranges: your Schwab feed; bars: CBOE.':'<b class="amb">Schwab is not connected, so only the 300 most-traded stocks were checked</b> - connect it for the full screen.'} Option prices: CBOE, ~15 min delayed - check the live chain before entering. Filings grade: the company’s SEC reports (top-ranked names only).${d.si_date?' Short interest: FINRA, settlement '+dshort(d.si_date)+'.':''}</span></div>`;
 const e=d.etf;if(e)h+=`<table style="margin-bottom:10px"><tr><th style="text-align:left">Whole-effect trade</th><th>This year</th><th title="Cheapest call with delta 0.40-0.70 and the best chance of profit, February-April">Buy a call</th><th title="Call debit spread paying at least 1:1">Call spread</th></tr>
 <tr><td style="text-align:left">${tkl(e.ticker)} ${LP(e.ticker,e.px,{d:'$'})} <span class="mute">RSI ${e.rsi==null?'-':fmt(e.rsi,0)}</span><br><span class="mute">${esc(e.name)}</span></td><td>${sgn(e.ytd)}</td><td style="text-align:left;white-space:normal">${tlCall(e.opt)}</td><td style="text-align:left;white-space:normal">${tlSpread(e.opt)}</td></tr></table>`;
 h+=`<div class="ctl"><span class="mute">Show</span>${[['main',`Pass <span class="mute">${d.n_pass}</span>`],['near',`Near misses <span class="mute">${d.n_near}</span>`],['all','All']].map(([k,l])=>`<button type="button" class="seg${TL.tier===k?' on':''}" onclick="TL.tier='${k}';tlRender()">${l}</button>`).join('')}</div>`;
 let L=R.filter(r=>TL.tier==='all'?true:TL.tier==='main'?r.tier==='pass':r.tier==='near').filter(r=>pxPass(r.px));
 if(TL.tier==='main'&&!L.length&&d.n_near){TL.tier='near';return tlRender(keepNote)}
 if(!L.length){el.innerHTML=h+'<p class="mute">Nothing in this group.</p>';return}
 h+=`<table><tr><th>#</th><th>Stock</th><th title="Pass = meets every filter. Near miss = one filter short.">Filters</th><th title="Depth of this year's loss 30 · closeness to the 52-week low 20 · RSI 15 · filings grade 15 · small size 10 · short interest 10 · minus points for weak filings or dilution">Score</th><th>This year</th><th title="How far the price is above its 52-week low">Above low</th><th title="How far the price is below its 52-week high">Off high</th><th title="Still falling: new low in the last 3 sessions · Basing: no new low · Turning: above its 10-day average and rising">State</th><th>Mkt cap</th><th title="Health grade from the company's own SEC filings: cash runway, debt, profit trend, dilution. F or dilution risk = no setup shown.">Filings</th><th title="Shares short as % of shares outstanding (FINRA)">Short %</th><th>Earnings</th><th title="Cash-secured put from 30 days out to the January monthly expiration: 70%+ chance of profit and at least your minimum return on the cash. Highest chance of profit first, then least cash, then best return.">Sell a put</th><th title="One call, February-April expiration, delta 0.40-0.70. POP = chance of finishing above breakeven at expiration.">Buy a call</th><th title="Call debit spread paying at least 1:1. Needs a spread-enabled account (not the Roth).">Call spread</th><th>Why it ranks</th></tr>`+
 L.map((r,i)=>`<tr><td>${i+1}</td><td style="text-align:left;min-width:150px" data-sv="${r.ticker}">${tkl(r.ticker)} ${LP(r.ticker,r.px,{d:'$'})} <span class="mute">RSI ${r.rsi==null?'-':fmt(r.rsi,0)}</span><br><span class="mute">${esc(r.name||'')}</span></td>
  <td style="text-align:left;white-space:normal;min-width:120px" data-sv="${r.fails.length}"><span class="chip ${r.knife?'put':r.tier==='pass'?'call':'amb'}">${r.knife?'weak filings':r.tier==='pass'?'pass':'near miss'}</span>${r.fails.length?'<br><span class="mute">'+r.fails.map(esc).join('; ')+'</span>':''}</td>
  <td data-sv="${r.score}"><b>${fmt(r.score,0)}</b></td><td data-sv="${r.ytd}">${sgn(r.ytd,0)}</td><td data-sv="${r.above_low}">${fmt(r.above_low,1)}%</td><td data-sv="${r.off_high}">${sgn(r.off_high,0)}</td>
  <td><span class="chip ${(TLS[r.state]||['mute'])[0]}" title="${(TLS[r.state]||['',''])[1]}">${esc(r.state)}</span></td><td data-sv="${r.cap}">${money(r.cap)}</td>
  <td data-sv="${r.grade||'Z'}" title="${esc((r.grade_why||[]).join(' · '))}">${r.grade?`<b class="${r.grade<'C'?'call':r.grade>'C'?'put':''}">${r.grade}</b>`:'<span class="mute">-</span>'}${r.dilution?'<br><span class="put">dilution risk</span>':''}</td>
  <td data-sv="${r.si_pct==null?'':r.si_pct}">${r.si_pct==null?'<span class="mute">-</span>':fmt(r.si_pct,1)+'%'}</td><td data-sv="${r.earn||''}">${r.earn?dshort(r.earn):'<span class="mute">-</span>'}</td>
  <td style="text-align:left;white-space:normal;min-width:170px">${tlPut(r.opt)}</td><td style="text-align:left;white-space:normal;min-width:170px">${tlCall(r.opt)}</td><td style="text-align:left;white-space:normal;min-width:170px">${tlSpread(r.opt)}</td>
  <td style="text-align:left;white-space:normal;min-width:150px">${(r.why||[]).map(esc).join(' · ')||'<span class="mute">-</span>'}</td></tr>`).join('')+'</table>';
 h+='<p class="mute">Roth IRA (single-leg only): the put and the call fit; the call spread needs a spread-enabled account. Take profits your usual way - puts at 50%, calls and spreads into strength inside the exit window.</p>';
 el.innerHTML=h;try{liveFetch()}catch(_){}}
$('#tlref').onclick=()=>tlLoad(true);

$('#ceref').onclick=()=>ceLoad(true);
// ---- mining earnings (Picks tab) ----
let MN={d:null,ind:'',days:21,busy:false};
const MNIND={'Precious Metals':'Gold & silver','Metal Mining':'Metals (copper, lithium, uranium…)','Other Metals and Minerals':'Other metals & minerals','Coal Mining':'Coal','Mining & Quarrying of Nonmetallic Minerals (No Fuels)':'Non-metallic minerals'};
async function mnLoad(force){if(MN.busy)return;MN.busy=true;$('#mnnote').textContent='· loading (first load takes ~30 s)…';
 try{MN.d=await (await fetch(`/api/mining_earnings?days=${MN.days}${force?'&force=1':''}`)).json();mnRender()}catch(e){$('#mnnote').textContent='· '+e.message}MN.busy=false}
function mnRender(){const d=MN.d;if(!d)return;if(d.error){$('#mnlist').innerHTML=`<p class="put">${esc(d.error)}</p>`;$('#mnnote').textContent='';return}
 const R=d.rows||[],inds=[...new Set(R.map(r=>r.industry))];
 $('#mnnote').textContent=`· ${R.length} reports from ${d.universe} miners · updated ${d.asof}${(d.failed_days||[]).length?` · ${d.failed_days.length} day(s) failed to load`:''}`;
 $('#mnind').innerHTML=[['','All'],...inds.map(i=>[i,MNIND[i]||i])].map(([k,l])=>`<button type="button" class="seg${MN.ind===k?' on':''}" data-k="${esc(k)}">${esc(l)} <span class="mute">${k?R.filter(r=>r.industry===k).length:R.length}</span></button>`).join('');
 $('#mnind').querySelectorAll('.seg').forEach(b=>b.onclick=()=>{MN.ind=b.dataset.k;mnRender()});
 const L=R.filter(r=>!MN.ind||r.industry===MN.ind);
 if(!L.length){$('#mnlist').innerHTML='<p class="mute">No mining earnings in this window.</p>';return}
 const wl=new Set(((W&&W.config&&W.config.watchlist)||[]).map(x=>x.ticker).concat((W&&W.config&&W.config.puts_watchlist)||[]));
 let h='<table><tr><th>Date</th><th>When</th><th>Ticker</th><th>Company</th><th>Price · RSI(14)</th><th>EPS est.</th><th>EPS a year ago</th><th>Quarter</th><th>Mkt cap</th><th></th></tr>',last='';
 for(const r of L){const nd=r.date!==last;last=r.date;
  h+=`<tr${wl.has(r.symbol)?' class="wrow"':''}><td>${nd?`<b>${dshort(r.date)}</b>`:''}</td><td><span class="chip ${r.time==='before open'?'acc':r.time==='after close'?'amb':'mute'}">${esc(r.time)}</span></td>
   <td><a href="?t=${r.symbol}" onclick="go('${r.symbol}');return false"><b>${r.symbol}</b></a>${wl.has(r.symbol)?' <span class="call" title="on your watchlist">★</span>':''}</td><td style="text-align:left">${esc(r.name)}<br><span class="mute">${esc(MNIND[r.industry]||r.industry)}</span></td>
   <td>${r.price==null?'–':'$'+fmt(r.price)}${r.chg_pct!=null?` <span class="${r.chg_pct<0?'put':'call'}">${r.chg_pct>=0?'+':''}${fmt(r.chg_pct,1)}%</span>`:''}<br><span class="mute">RSI ${r.rsi==null?'–':fmt(r.rsi,0)}</span></td>
   <td>${esc(r.eps_est||'–')}${r.n_est&&r.n_est!=='N/A'?` <span class="mute">(${esc(r.n_est)} est.)</span>`:''}</td><td>${esc(r.eps_last_year||'–')}</td><td>${esc(r.quarter||'')}</td><td>${r.cap?money(r.cap):'–'}</td>
   <td><a href="https://www.nasdaq.com/market-activity/stocks/${r.symbol.toLowerCase()}/earnings" target="_blank" rel="noopener" class="mute">Nasdaq ↗</a></td></tr>`}
 $('#mnlist').innerHTML=h+'</table>'}
$('#mndays').onchange=e=>{MN.days=+e.target.value;mnLoad(false)};$('#mnref').onclick=()=>mnLoad(true);
// ---- saved calculator trades ----
let CSV={items:[],live:{},armed:null};
function caSnapshot(){const st=caStats();return {net:st.net,mx:isFinite(st.mx)?st.mx:null,mn:isFinite(st.mn)?st.mn:null,mx_unl:!isFinite(st.mx),mn_unl:!isFinite(st.mn),pop:st.pop,bes:st.bes,cap:caCapital(st)}}
function caLegTxt(c){const L=CS[c.strat].legs,v=c.v;return L.map(l=>l.t==='S'?`100 sh @ ${fmt(v.sb)}`:`${l.s>0?'Buy':'Sell'} ${fmtK(v[l.k])}${l.t} @ ${fmt(v[l.p])}`).join('<br>')}
async function csLoad(live){try{$('#csnote').textContent=live?'· pricing…':'';const j=await (await fetch('/api/calcs'+(live?'?live=1':''))).json();CSV.items=j.items||[];if(live)CSV.live=j.live||{};
 $('#csnote').textContent=`· ${CSV.items.length} saved${live?' · prices '+j.asof:''}`;csRender()}catch(e){$('#csnote').textContent='· '+e.message}}
function csRender(){const el=$('#cslist');if(!el)return;if(!CSV.items.length){el.innerHTML='<p class="mute">Nothing saved yet. Set up a trade above and press <b>Save this trade</b>.</p>';return}
 let h='<table><tr><th>Saved</th><th>Trade</th><th>Legs</th><th>Pay / collect</th><th>Max profit</th><th>Max loss</th><th>Breakeven</th><th>Chance</th><th>Now</th><th>Note</th><th></th></tr>';
 for(const c of CSV.items){const s=c.snap||{},L=CSV.live[c.id];
  const net=s.net==null?'–':s.net>=0?`<span class="put">pay ${cusd(s.net)}</span>`:`<span class="call">collect ${cusd(-s.net)}</span>`;
  let now='<span class="mute">–</span>';if(L){if(L.error)now=`<span class="mute">${esc(L.error)}</span>`;else now=`<b class="${L.pl>=0?'call':'put'}">${L.pl>=0?'+':''}${cusd(L.pl)}</b>${L.pl_pct!=null?` <span class="mute">${fmt(L.pl_pct,0)}%</span>`:''}<br><span class="mute">${esc(c.tk)} ${fmt(L.spot)} · ${L.dte_now}d left</span>`}
  else if(c.tk&&c.exp)now='<span class="mute">press Refresh</span>';
  h+=`<tr><td>${esc(c.saved.slice(5,16))}</td><td><b>${esc(c.tk||'—')}</b> ${esc(CS[c.strat]?CS[c.strat].n:c.strat)}<br><span class="mute">${c.exp?dshort(c.exp)+' · ':''}${c.qty}× · stock then ${fmt(c.S)}</span></td><td>${caLegTxt(c)}</td><td>${net}</td>
   <td class="call">${s.mx_unl?'unlimited':cusd(s.mx)}</td><td class="put">${s.mn_unl?'unlimited':cusd(s.mn)}</td><td>${(s.bes||[]).map(b=>fmt(b)).join(' / ')||'–'}</td><td>${s.pop==null?'–':fmt(s.pop,0)+'%'}</td><td>${now}</td>
   <td><span class="csn" data-id="${c.id}" title="Click to edit" style="cursor:text">${c.note?esc(c.note):'<span class="mute">+ note</span>'}</span></td>
   <td style="white-space:nowrap"><a href="#" onclick="csOpen('${c.id}');return false">Open</a> · <a href="#" class="${CSV.armed===c.id?'put':'mute'}" onclick="csDel('${c.id}');return false">${CSV.armed===c.id?'Delete?':'Delete'}</a></td></tr>`}
 el.innerHTML=h+'</table>';
 el.querySelectorAll('.csn').forEach(n=>n.onclick=()=>{if(n.querySelector('input'))return;const c=CSV.items.find(x=>x.id===n.dataset.id);
  n.innerHTML=`<input value="${esc(c.note||'')}" maxlength="200" style="min-width:160px;font-size:13px;text-transform:none">`;const i=n.querySelector('input');i.focus();
  const done=async()=>{try{const j=await post('/api/calcs',{note:{id:c.id,text:i.value}});CSV.items=j.items}catch(e){}csRender()};i.onblur=done;i.onkeydown=e=>{if(e.key==='Enter')i.blur();if(e.key==='Escape'){i.onblur=null;csRender()}}})}
async function csDel(id){if(CSV.armed!==id){CSV.armed=id;csRender();setTimeout(()=>{if(CSV.armed===id){CSV.armed=null;csRender()}},4000);return}
 CSV.armed=null;try{const j=await post('/api/calcs',{remove:id});CSV.items=j.items;delete CSV.live[id];$('#csnote').textContent=`· ${CSV.items.length} saved`}catch(e){$('#csnote').textContent='· '+e.message}csRender()}
function csOpen(id){const c=CSV.items.find(x=>x.id===id);if(!c)return;CA.strat=c.strat;CA.tk=c.tk||'';CA.chain=null;CA.S=c.S;CA.iv=c.iv;CA.qty=c.qty;CA.v={...CA.v,...c.v};CA.ivk={...(c.ivk||{})};
 CA.touched={p1:true,p2:true,sb:true};CA.wiP=null;CA.wiD=0;
 if(c.exp){const d=Math.round((new Date(c.exp+'T16:00:00')-new Date())/864e5);CA.dte=Math.max(0,d)}else CA.dte=c.dte;
 $('#catk').value=c.tk||'';$('#caexp').innerHTML=c.exp?`<option value="${c.exp}">${dshort(c.exp)}</option>`:'<option value="">Expiration</option>';
 $('#canote').textContent=`Saved trade from ${c.saved}${c.exp?` · days left updated to ${CA.dte}`:''} — premiums are what you saved. Press Load for today's prices.`;
 $('#canote_in').value=c.note||'';caStrat();caInputs();caOut();$('#calc').scrollIntoView({behavior:'smooth'})}
$('#casavebtn').onclick=async()=>{if(!CA.init)caInit();const exp=CA.chain?CA.chain.exp:($('#caexp').value||null);
 try{const j=await post('/api/calcs',{add:{tk:CA.tk||'',strat:CA.strat,exp,S:CA.S,dte:CA.dte,iv:CA.iv,qty:CA.qty,v:CA.v,ivk:CA.ivk,snap:caSnapshot(),note:$('#canote_in').value}});
  CSV.items=j.items;$('#casavemsg').innerHTML='<span class="call">Saved ✓</span>';$('#canote_in').value='';$('#csnote').textContent=`· ${CSV.items.length} saved`;csRender();setTimeout(()=>$('#casavemsg').textContent='',3000)}
 catch(e){$('#casavemsg').innerHTML=`<span class="put">${esc(e.message)}</span>`}};
$('#csref').onclick=()=>csLoad(true);

function bestAvail(Wn,cur){const B=(Wn.best||[]).filter(b=>!cur||b.exp!==cur.exp||b.short!==cur.short);if(!B.length)return'';const d2=v=>fmt(v,2);
 return `<details class="fibhelp" ${cur?'':'open'}><summary class="mute"><b>Best available in this window</b> — top ${B.length} of ${Wn.n_all||B.length} spreads, any profile, with what to watch</summary><div class="tw"><table><tr><th>Spread</th><th>Expiration</th><th title="Realistic fill: halfway between mid and natural">Credit (fill)</th><th>Max loss</th><th>Return</th><th>POP</th><th>Hits 50%</th><th>Why it ranks</th><th>Watch out</th><th></th></tr>`+
 B.map(b=>`<tr><td><b>${fmtK(b.short)}</b> / ${fmtK(b.long)}P</td><td>${b.exp.slice(5)} <span class="mute">${b.dte}d</span></td><td>${d2(b.credit)} <span class="mute">(${d2(b.nat)})</span></td><td>$${fmt(b.maxloss,0)}</td><td>${b.roc==null?'–':fmt(b.roc,0)+'%'}</td><td>${fmt(b.pop,0)}%</td><td>${b.p50==null?'–':fmt(b.p50,0)+'%'}</td>
  <td style="text-align:left;font-size:12px">${esc((b.why||'').replace(/^Best available of \d+ spreads in this window: /,''))}</td><td class="amb" style="text-align:left;font-size:12px">${(b.issues||[]).map(esc).join('<br>')||'<span class="call">nothing major</span>'}</td>
  <td><a href="#" onclick="calcOpen({tk:'${CC.r.ticker}',strat:'pcs',exp:'${b.exp}',k1:${b.short},k2:${b.long}});return false">calc</a></td></tr>`).join('')+'</table></div></details>'}
// scanner: plain-English issues for a put credit spread row
function pcsIssues(c,it,mode){const o=[],sm=mode==='0dte'||mode==='d5',rt=retOf(c);
 if(c.long!=null){if(c.pop<(sm?80:60))o.push(`POP ${fmt(c.pop,0)}% (below ${sm?80:60}%)`);if(c.s_oi<50||c.l_oi<20)o.push(`thin OI ${c.s_oi}/${c.l_oi}`);if(c.slip>50)o.push('wide quotes');if(c.cw<0.10)o.push(`credit ${fmt(c.cw*100,0)}% of width`);if(rt!=null&&rt<20)o.push(`return ${fmt(rt,0)}%`)}
 else{if(c.pop<(sm?80:70))o.push(`POP ${fmt(c.pop,0)}%`);if(c.slip>35)o.push('wide quotes')}
 if(it.earnings&&it.earnings<=c.exp)o.push(`earnings ${it.earnings.slice(5)}`);if(c.put_wall!=null&&!c.below_wall)o.push('above put wall');return o}

// ---- usage tracking (local only: counts of tabs/sections used, for the daily layout review) ----
const UQ={};function track(k){UQ[k]=(UQ[k]||0)+1}
function uflush(){const k=Object.keys(UQ);if(!k.length)return;const ev={...UQ};k.forEach(x=>delete UQ[x]);
 try{fetch('/api/usage',{method:'POST',headers:H,body:JSON.stringify({events:ev}),keepalive:true})}catch(e){}}
setInterval(uflush,30000);document.addEventListener('visibilitychange',()=>{if(document.hidden)uflush()});
document.addEventListener('click',e=>{const s=e.target.closest&&e.target.closest('summary'),d=s&&s.parentElement;if(d&&d.tagName==='DETAILS'&&!d.open&&d.querySelector('summary')===s)track('open:'+(d.id||s.innerText.trim().split('\n')[0].slice(0,40)))},true);/* user opens only: details re-rendered with the open attribute fire toggle on every refresh and inflated usage (salvage panel 2,100+ in 14 d) */
document.addEventListener('click',e=>{const b=e.target.closest('.seg,button');if(!b)return;const p=b.closest('[id]');if(p&&p.id)track('click:'+p.id)},true);
// ---- compact layout ----
const DENS=(()=>{try{return localStorage.getItem('walls_dense')==='1'}catch(e){return false}})();
function setDense(on){document.body.classList.toggle('dense',on);$('#densbtn').textContent=on?'Comfortable':'Compact';try{localStorage.setItem('walls_dense',on?'1':'0')}catch(e){}}
setDense(DENS);$('#densbtn').onclick=()=>{setDense(!document.body.classList.contains('dense'));track('dense')};
// ---- fold long explanations into "How it works" (saves space; open when needed) ----
function foldHelp(root){(root||document).querySelectorAll('.card p.mute:not([data-f]), details p.mute:not([data-f])').forEach(p=>{p.dataset.f=1;
 if(p.closest('.abbrbox')||p.closest('summary'))return;if((p.textContent||'').length<260||p.closest('details.howto'))return;
 const d=document.createElement('details');d.className='howto';d.innerHTML='<summary class="mute">How it works / notes</summary>';p.replaceWith(d);d.appendChild(p)})}
foldHelp();new MutationObserver(ms=>{for(const m of ms)for(const n of m.addedNodes)if(n.nodeType===1&&n.querySelector)foldHelp(n)}).observe(document.querySelector('main'),{childList:true,subtree:true});
// ---- back to top ----
window.addEventListener('scroll',()=>{$('#totop').classList.toggle('show',scrollY>900)},{passive:true});$('#totop').onclick=()=>scrollTo({top:0,behavior:'smooth'});
// ---- health scorecard ----
let HL=null;
async function hlLoad(){try{HL=await (await fetch('/api/health')).json();const L=HL.latest;
 $('#hlthbtn').textContent=L&&L.overall!=null?`Health ${L.overall}`:'Health –';$('#hlthbtn').className='pill hbtn '+(L&&L.overall!=null?(L.overall>=85?'hgood':L.overall>=70?'hok':'hbad'):'')}catch(e){}}
function hlRender(){const L=HL&&HL.latest,H_=(HL&&HL.history)||[];if(!L){$('#hlthbody').innerHTML='<p class="mute">No audit yet. It runs daily (or run audit\\audit.py in the app folder).</p>';return}
 $('#hlthnote').textContent=` · ${L.date} ${L.time} · ${L.checks.length} checks in ${L.seconds}s`;
 const cats=Object.entries(L.scores||{}),pts=H_.filter(h=>h.overall!=null).slice(-30);
 const W=Math.min(640,($('#hlthbody').clientWidth||600)),Hh=70,sx=i=>8+i*(W-16)/Math.max(1,pts.length-1),sy=v=>Hh-6-(v/100)*(Hh-12);
 const spark=pts.length>1?`<svg viewBox="0 0 ${W} ${Hh}" style="max-width:${W}px"><polyline fill="none" stroke="var(--accent)" stroke-width="2" points="${pts.map((p,i)=>sx(i)+','+sy(p.overall)).join(' ')}"/>${pts.map((p,i)=>`<circle cx="${sx(i)}" cy="${sy(p.overall)}" r="3" fill="var(--accent)"><title>${p.date}: ${p.overall}</title></circle>`).join('')}</svg>`:'<p class="mute">The trend line appears after a few daily runs.</p>';
 const C=L.metrics&&L.metrics.calibration;
 $('#hlthbody').innerHTML=`<div class="gtiles">${[['Overall',L.overall]].concat(cats).map(([k,v])=>`<div class="gt b-${v>=85?'call':v>=70?'amb':'put'}"><div class="l">${esc(k)}</div><div class="v">${v}</div></div>`).join('')}</div>
  <h3>Trend</h3>${spark}
  <h3>Needs attention (${L.findings.length})</h3>${L.findings.length?'<ul class="cab">'+L.findings.map(f=>`<li>${esc(f)}</li>`).join('')+'</ul>':'<p class="call">Nothing failed.</p>'}
  ${C?`<h3>Self-graded picks</h3><p>${C.closed} finished · ${C.open} open.</p>${Object.entries(C.by_type||{}).map(([t,s])=>`<div>${esc(t)}: won ${s.won}/${s.n} (${fmt(s.won/s.n*100,0)}%) vs predicted ${fmt(s.pred*100,0)}% · ${s.clusters} independent ticker/expiry groups</div>`).join('')}`:''}
  <details class="howto"><summary class="mute">All ${L.checks.length} checks</summary><ul class="cab">${L.checks.map(c=>`<li>${c.ok?'<span class="call">✓</span>':'<span class="put">✗</span>'} <b>${esc(c.cat)}</b> ${esc(c.name)}: ${esc(String(c.value))}</li>`).join('')}</ul></details>
  <p class="mute">The audit runs daily: prices vs Nasdaq, RSI recomputed independently, wall and spread math, data freshness, response times, coverage, and the app’s own recommended trades logged and graded after they expire (needs 20+ independent ticker/expiry groups before a hit rate counts). Report: audit\\${esc(L.date)}.md in the app folder.</p>`}

const RF={busy:false,seen:{},done:0};
const RFAGAIN={scan:()=>scLoad(),csp:()=>ptLoad(),calls:()=>clLoad(),edge:()=>egLoad(false),sqz:()=>sqLoad(false),tlb:()=>tlLoad(false),picks:()=>pkLoad()};
const RFOPEN={scan:()=>true,csp:()=>PT.loaded,calls:()=>CL.loaded,edge:()=>EG.loaded,sqz:()=>SQ.loaded,tlb:()=>TL.loaded,picks:()=>PK.loaded};
function rfAuto(j){const a=j.auto||{},b=$('#rfall');if(!b)return;STREAM=!!(a.stream&&a.stream.on);
 for(const[k,v]of Object.entries(j.jobs||{})){const d=/^done /.test(v.progress||'')?v.progress:'';if(!d||RF.seen[k]===d)continue;
  if(RF.seen[k]===undefined){RF.seen[k]=d;continue}
  if(RF.busy||document.hidden)continue;RF.seen[k]=d;try{if(RFOPEN[k]&&RFOPEN[k]())RFAGAIN[k]()}catch(_){}}
 if(RF.busy||Date.now()-RF.done<180000)return;
 const live=a.on&&a.market_open,ev=fmt(a.every_min,0);
 b.innerHTML=live?`⟳<span class="rfx"> Auto${a.running?' · updating…':a.last_done?' · '+esc(String(a.last_done).replace(' ET','')):''}</span>`:'⟳<span class="rfx"> Refresh all</span>';
 b.title=(live?`Automatic: spreads, sell puts and calls are re-priced on live Schwab quotes every ${ev} min while the market is open`+(a.last_done?` (last round finished ${a.last_done}`+(a.secs?`, took ${a.secs} s)`:')'):'')+`. `
   :a.on?`Market closed: lists refresh on their own every ${ev} min once it opens (from 9:40 ET). `:'Automatic refresh is switched off ("auto_refresh": false). ')
  +(STREAM?`Prices and your trades stream live from Schwab (${a.stream.stocks} stocks, ${a.stream.options} option quotes). `:a.stream&&a.stream.error?`Live stream off: ${a.stream.error}. `:'')
  +'Click to refresh everything right now.'}
async function rfAll(){if(RF.busy)return;RF.busy=true;const b=$('#rfall');b.disabled=true;const t0=Date.now();
 const set=t=>{b.textContent=t},sleep=ms=>new Promise(r=>setTimeout(r,ms));
 const names={scan:'Walls & spreads',csp:'Sell puts',calls:'Calls & LEAPS',edge:'Early moves',sqz:'Squeeze',tlb:'Tax-loss bounce',picks:'Picks'};
 const again=RFAGAIN;
 try{set('⟳ Starting…');
  const st0=await post('/api/refresh_all'),skp=(await (await fetch('/api/refresh_all')).json()).skipped||[];
  const quick=[()=>liveFetch(true),async()=>{const r=await post('/api/check');W.status=r.status;await aload();await wload()},()=>tload(),()=>csLoad(true),()=>loadGexMap(),()=>bkLoad(),()=>mnLoad(true),()=>ceLoad(true),()=>flMarket()];
  for(const f of quick){try{Promise.resolve(f()).catch(()=>{})}catch(_){}}
  const done=new Set();let errs=[];
  for(let i=0;i<720;i++){const j=(await (await fetch('/api/refresh_all')).json()).jobs||{};let n=0,tot=0;errs=[];
   let cur='';for(const[k,v]of Object.entries(j)){tot++;if(v.running){if(!/^queued/.test(v.progress||'')&&!cur)cur=names[k]+(v.progress?' '+String(v.progress).replace(/^option chains /,'').slice(0,28):'');b.title=names[k]+(v.progress?': '+String(v.progress).slice(0,80):'')}else{n++;if(v.error)errs.push(names[k]+': '+v.error);if(!done.has(k)){done.add(k);try{again[k]()}catch(_){}}}}
   b.innerHTML=`⟳ ${n}/${tot} <span class="rfx">· ${esc(cur||'queued')} · ${Math.floor((Date.now()-t0)/60000)}m</span>`;
   if(n>=tot)break;await sleep(2500)}
  const hh=new Date().toTimeString().slice(0,8);
  set(errs.length?`⚠ ${errs.length} failed · ${hh}`:`✓ Updated ${hh}`);b.title=errs.length?errs.join('\n'):`Refreshed in ${Math.round((Date.now()-t0)/1000)} s`+(skp.length?` · already fresh, not re-run: ${skp.map(k=>names[k]).join(', ')} (daily data)`:'');
  await sleep(1500);try{liveFetch(true)}catch(_){}
 }catch(e){set('⚠ '+e.message)}
 b.disabled=false;RF.busy=false;RF.done=Date.now()}
$('#rfall').onclick=()=>rfAll();
async function hdrStatus(){try{const s=await (await fetch('/api/schwab')).json(),c=$('#swchip');
  if(s.connected){c.innerHTML=`<span class="call">●</span> Schwab<span class="rfx"> ${fmt(s.days_left,1)}d</span>`;c.title=`Schwab connected (real-time). Log in again within ${fmt(s.days_left,1)} days — click to open the connection panel.`;c.style.borderColor=s.days_left<1.5?'var(--amber)':''}
  else{c.innerHTML=`<span class="put">●</span><span class="rfx"> Connect</span> Schwab`;c.title=s.keys?'Schwab keys saved — log in to connect (click)':'Schwab not set up — click to connect';c.style.borderColor='var(--put)'}
 }catch(e){}
 try{const j=await (await fetch('/api/refresh_all')).json(),n=j.nasdaq||{},q=$('#nqchip');q.hidden=!n.blocked;try{rfAuto(j)}catch(_){}
  const nd=$('#nodata');if(nd){nd.hidden=!j.no_data;nd.textContent=j.no_data?'⚠ '+j.no_data+' — click here':''}
  const sc=$('#swchip');if(sc&&j.cboe)sc.dataset.cboe=j.cboe.down?'1':'';
  q.title=n.blocked?`Nasdaq.com is refusing this PC's requests (Access Denied) since ${n.since||'?'}. Nothing is stopped: prices come from Schwab and the scans' stock list is rebuilt from Nasdaq Trader's listings plus Schwab. Retrying in ~${n.retry_min} min.`:''}catch(e){}}
$('#swchip').onclick=()=>{tab('main');const d=$('#swb');if(d){d.open=true;swLoad();setTimeout(()=>d.scrollIntoView({behavior:'smooth',block:'start'}),60)}};
hdrStatus();setInterval(hdrStatus,20000);

$('#hlthbtn').onclick=async()=>{track('health');$('#hlthdlg').hidden=false;await hlLoad();hlRender()};
$('#hlthx').onclick=()=>{$('#hlthdlg').hidden=true};$('#hlthdlg').onclick=e=>{if(e.target.id==='hlthdlg')$('#hlthdlg').hidden=true};
hlLoad();
// ---- calls & LEAPS tab ----
const CLK={long:{n:'Buy calls',wins:[['short','7–29 DTE'],['swing','30–89 DTE'],['mid','90–269 DTE']],
  how:'<b>Buying a call</b> = paying a premium for the right to buy 100 shares at the strike price until expiration. You make money if the stock rises above your breakeven (strike + premium). The most you can lose is what you paid. Time decay works against you every day, so the stock has to move — and soon enough.',
  sorts:[['score','Best balance (chance × leverage)'],['p_up50','Chance of +50%'],['fast','Fastest to +50%'],['pop','Chance of profit at expiration'],['cheap','Cheapest']]},
 leaps:{n:'LEAPS',wins:[['leaps','9+ months']],
  how:'<b>LEAPS</b> = calls that expire 9 months to 2+ years out. A deep in-the-money LEAPS (delta 0.70–0.85) moves almost like the stock for a fraction of the cash, and time decay is slow. Watch the <b>time value per year</b>: it is what you pay above the intrinsic value — lower is better (like an interest rate on the shares you control).',
  sorts:[['score','Lowest time-value cost / year'],['delta','Most stock-like (highest delta)'],['p_up50','Chance of +50%'],['cheap','Cheapest']]},
 spread:{n:'Bull call spread',wins:[['short','7–29 DTE'],['swing','30–89 DTE'],['mid','90–269 DTE']],
  how:'<b>Bull call spread</b> (call debit spread) = buy a call and sell a higher-strike call in the same expiration. Cheaper than a call alone and less time decay, but profit is capped at the strike difference minus what you paid. Max loss = what you pay.',
  sorts:[['score','Chance × reward/risk'],['rr','Best reward/risk'],['pop','Chance of profit'],['p_up50','Chance of +50%'],['fast','Fastest to +50%'],['cheap','Cheapest']]},
 cc:{n:'Covered calls',wins:[['short','7–29 DTE'],['swing','30–89 DTE']],
  how:'<b>Covered call</b> = you own 100 shares and sell someone the right to buy them at a higher strike. You keep the premium no matter what. If the stock finishes above the strike your shares are sold (called away) at the strike — you keep the premium plus the gain up to the strike, but miss anything above it. This is the second half of the wheel.',
  sorts:[['score','Best annualized vs chance of being called'],['ann','Highest annualized'],['ret','Biggest premium %'],['safe','Least likely to be called away']]}};
let CL={kind:'long',win:'swing',cost:0,pop:0,sort:'score',per:1,n:25,trend:true,earn:false,res:null,st:{},loaded:false};
try{Object.assign(CL,JSON.parse(localStorage.getItem('walls_cl')||'{}'),{res:null,st:{},loaded:false})}catch(e){}
const clSave=()=>{try{const{res,st,loaded,...k}=CL;localStorage.setItem('walls_cl',JSON.stringify(k))}catch(e){}};
async function clLoad(){CL.loaded=true;try{const j=await (await fetch('/api/calls')).json();CL.st=j.status;if(j.results)CL.res=j.results;clRender();if(CL.st.running)setTimeout(clLoad,2500)}catch(e){$('#clnote').textContent='· '+e.message}}
const CLS={CALL:'long',LEAPS:'long',BCS:'spread',CC:'cc'};
function clRows(r){const out=[];for(const it of Object.values(r.items||{})){for(const [w,W_] of Object.entries(it.windows||{})){
  for(const c of W_.long||[]){const st=w==='leaps'||c.dte>=270?'LEAPS':'CALL';out.push({it,c,st,exp:c.exp,strat:st,price:it.price,pop:c.pop,ret:50,cap:c.cost,prem:c.cost*0.5,flag:!!c.earn_cross,best:c.score,p50:c.p_up50,dte:c.dte,stars:starsOf(c.p_up50!=null?c.p_up50/100:null,0.5)})}
  for(const c of W_.spread||[])out.push({it,c,st:'BCS',exp:c.exp,strat:'BCS',price:it.price,pop:c.pop,ret:c.rr*100,cap:c.cost,prem:c.maxprofit,flag:!!c.earn_cross,best:c.score,p50:c.p_up50,dte:c.dte,stars:starsOf(c.pop/100,c.rr)});
  for(const c of W_.cc||[])out.push({it,c,st:'CC',exp:c.exp,strat:'CC',price:it.price,pop:100-(c.p_called||0),ret:c.ret,cap:it.price*100,prem:c.prem,flag:!!c.earn_cross,best:c.score,p50:null,dte:c.dte,stars:0})}}
 return out}
function clTop(kind,rows){CL.hidT=0;const out=[],per={},seen=new Set();
 for(const x of gfSort(gfFilter(rows.filter(x=>x.strat===kind),{sec:'calls'}))){const it=x.it;if(CL.trend&&kind!=='CC'&&it.trend!=null&&it.trend<2){CL.hidT++;continue}
  const k=it.ticker+x.exp+(x.c.long||x.c.short);if(seen.has(k))continue;seen.add(k);per[it.ticker]=per[it.ticker]||0;if(per[it.ticker]>=CL.per)continue;per[it.ticker]++;
  out.push({...it,...x.c,rank:x.best,stars:x.stars});if(out.length>=(gst('calls')==='all'?Math.min(GF.n,5):GF.n))break}
 return out}
function clTk(x){return `<a href="?t=${x.ticker}" onclick="go('${x.ticker}');return false"><b>${x.ticker}</b></a>${STARS(x.stars)}<br><span class="mute">${esc(x.kind)}${x.sector?' · '+esc(x.sector):''}</span>`}
const clPx=x=>`${LP(x.ticker,x.price,x.strat==='cc'?{sc:[x.short]}:{up:1})}<br><span class="mute">RSI ${x.rsi==null?'–':fmt(x.rsi,0)}${x.trend!=null?' · '+['downtrend','weak','up','strong up'][x.trend]:''}</span>`;
const clEarn=x=>x.earnings?`<span class="${x.earn_cross?'amb':'mute'}">${dshort(x.earnings)}${x.earn_cross?' ⚠':''}</span>`:(CL.res&&CL.res.earnings_ok?'<span class="mute">none ≤50d</span>':'<span class="mute">–</span>');
const p50c=x=>x.p_up50==null?'–':`<b>${fmt(x.p_up50,0)}%</b>${x.d_up50_date?`<br><span class="mute">~${dshort(x.d_up50_date)}</span>`:''}`;
function clRender(){if(!$('#clres'))return;const st=CL.st||{},r=CL.res;
 const rows=r?clRows(r):[];$('#clufb').innerHTML=ufb('calls',rows,{asof:CL.res&&CL.res.ts?CL.res.ts:'',stale:CL.res&&CL.res.stale,src:srcOf(CL.res&&CL.res.items),strats:['CALL','BCS','CC','LEAPS'],extra:`<label>Per ticker<select data-clper><option value="1"${CL.per==1?' selected':''}>Best 1</option><option value="3"${CL.per==3?' selected':''}>Top 3</option></select></label><label><input type="checkbox" data-cltrend${CL.trend?' checked':''}> uptrend only</label>`,note:'Return: buy call / LEAPS = the +50% take-profit target · bull call = max profit ÷ cost · covered call = premium ÷ share cost'});
 const sk=gst('calls');$('#clhow').innerHTML=sk==='all'?'':CLK[{CALL:'long',LEAPS:'leaps',BCS:'spread',CC:'cc'}[sk]].how;
 $('#clnote').textContent=st.running?`· scanning… ${st.progress||''}`:st.error?`· error: ${st.error}`:r?`· last scan ${r.ts} · ${Object.keys(r.items||{}).length} tickers${r.custom?' (your list)':''}`:'· not run yet';
 if(!r){$('#clres').innerHTML='<p class="mute">Press Run scan (about 2–4 minutes for ~40 tickers). Leave the box empty to scan your watchlist plus the strongest uptrending stocks and ETFs from the last momentum scan.</p>';return}
 const kinds=sk==='all'?['CALL','BCS','CC','LEAPS']:[sk];let H='';
 for(const kd of kinds){const top=clTop(kd,rows);const KN={CALL:'Buy calls',LEAPS:'LEAPS calls',BCS:'Bull call spreads',CC:'Covered calls'}[kd];
 if(!top.length){H+=`<h3 style="margin:10px 0 4px">${KN}${dirTag(kd)}</h3><p class="mute">Nothing passes these selections${CL.hidT?` (${CL.hidT} hidden by “uptrend only”)`:''}${kd==='LEAPS'?' — pick the LEAPS or All expiration chip':''}.</p>`;continue}
 CL.kind={CALL:'long',LEAPS:'leaps',BCS:'spread',CC:'cc'}[kd];
 let h=`<h3 style="margin:10px 0 4px">${KN}${dirTag(kd)}</h3><table>`;
 if(CL.kind==='long'||CL.kind==='leaps'){
  h+=`<tr><th>#</th><th>Ticker</th><th>Price · RSI</th><th>Expiration</th><th>Buy call</th><th>Cost</th><th title="Price the stock must be above at expiration to make money">Breakeven</th><th title="Chance the stock is above breakeven at expiration (model)">Chance of profit</th><th title="Chance the call is worth 50% more than you paid at some close before expiration">Chance +50%</th><th title="Chance it doubles at some point">Chance 2×</th><th title="Value lost per day from time decay if nothing else changes">Decay / day</th><th title="Delta × 100 = how many shares it acts like">Acts like</th><th title="Premium above intrinsic value — what time costs you">Time value</th><th>Earnings</th><th></th></tr>`;
  top.forEach((x,i)=>{const itm=x.long<x.price;h+=`<tr><td>${i+1}</td><td>${clTk(x)}</td><td>${clPx(x)}</td><td>${dshort(x.exp)}<br><span class="mute">${x.dte}d</span></td>
   <td><b>${x.long}C</b><br><span class="mute">${itm?'in':'out of'} the money ${fmt(Math.abs(x.long/x.price-1)*100,1)}% · Δ ${fmt(x.delta)}</span></td>
   <td>${fmt(x.debit)} <span class="mute">(ask ${fmt(x.nat)})</span><br><b>$${fmt(x.cost,0)}</b></td>
   <td>${fmt(x.be)}<br><span class="mute">+${fmt(x.be_pct,1)}%${x.be_em!=null?' · '+fmt(x.be_em,1)+'× exp. move':''}</span></td>
   <td><b>${fmt(x.pop,0)}%</b></td><td>${p50c(x)}</td><td>${x.p_2x==null?'–':fmt(x.p_2x,0)+'%'}</td>
   <td class="put">-$${fmt(Math.abs(x.theta_day),0)}</td><td>${fmt(x.delta*100,0)} sh<br><span class="mute">${fmt(x.lev,1)}× leverage</span></td>
   <td>${fmt(x.ext_pct,0)}% of cost${CL.kind==='leaps'?`<br><b>${fmt(x.ext_yr,1)}%/yr</b>`:''}</td><td>${clEarn(x)}</td>
   <td><a href="#" onclick="calcOpen({tk:'${x.ticker}',strat:'${CL.kind==='leaps'?'leaps':'buycall'}',exp:'${x.exp}',k1:${x.long}});return false">Calculator</a></td></tr>`})}
 else if(CL.kind==='spread'){
  h+=`<tr><th>#</th><th>Ticker</th><th>Price · RSI</th><th>Expiration</th><th>Buy / sell call</th><th>Cost (max loss)</th><th>Max profit</th><th title="Max profit ÷ max loss">Reward : risk</th><th>Breakeven</th><th>Chance of profit</th><th title="Chance the stock finishes above the higher strike = full profit">Chance of max</th><th>Chance +50%</th><th>Earnings</th><th></th></tr>`;
  top.forEach((x,i)=>{h+=`<tr><td>${i+1}</td><td>${clTk(x)}</td><td>${clPx(x)}</td><td>${dshort(x.exp)}<br><span class="mute">${x.dte}d</span></td>
   <td><b>${x.long}</b> / ${x.short}<br><span class="mute">$${fmt(x.width,2)} wide</span></td><td>${fmt(x.debit)} <span class="mute">(nat ${fmt(x.nat)})</span><br><b>$${fmt(x.cost,0)}</b></td>
   <td class="call">$${fmt(x.maxprofit,0)}</td><td><b>${fmt(x.rr,1)} : 1</b></td><td>${fmt(x.be)}<br><span class="mute">+${fmt(x.be_pct,1)}%</span></td>
   <td><b>${fmt(x.pop,0)}%</b></td><td>${fmt(x.p_max,0)}%</td><td>${p50c(x)}</td><td>${clEarn(x)}</td>
   <td><a href="#" onclick="calcOpen({tk:'${x.ticker}',strat:'cds',exp:'${x.exp}',k1:${x.long},k2:${x.short}});return false">Calculator</a></td></tr>`})}
 else{
  h+=`<tr><th>#</th><th>Ticker</th><th>Price · RSI</th><th>Expiration</th><th>Sell call</th><th>Premium</th><th>Annualized</th><th title="Chance the stock finishes above the strike and your shares are sold">Chance called away</th><th title="Premium + gain up to the strike, if called">If called</th><th title="Price where the stock + premium breaks even">Breakeven</th><th>Call wall</th><th>Earnings</th><th></th></tr>`;
  top.forEach((x,i)=>{h+=`<tr><td>${i+1}</td><td>${clTk(x)}</td><td>${clPx(x)}</td><td>${dshort(x.exp)}<br><span class="mute">${x.dte}d</span></td>
   <td><b>${x.short}C</b><br><span class="mute">+${fmt(x.cap_pct,1)}% above · Δ ${fmt(x.delta)}</span></td><td>${fmt(x.credit)} <span class="mute">(bid ${fmt(x.nat)})</span><br><b>$${fmt(x.prem,0)}</b> · ${fmt(x.ret,2)}%</td>
   <td><b>${fmt(x.ann,0)}%</b></td><td>${fmt(x.p_called,0)}%</td><td class="call">+${fmt(x.if_called,1)}%</td><td>${fmt(x.be)}</td>
   <td>${x.call_wall==null?'–':x.call_wall}${x.above_wall?'<br><span class="call">strike ≥ wall</span>':''}</td><td>${clEarn(x)}</td>
   <td><a href="#" onclick="calcOpen({tk:'${x.ticker}',strat:'cc',exp:'${x.exp}',k1:${x.short}});return false">Calculator</a></td></tr>`})}
 H+=h+'</table>'}
 $('#clres').innerHTML=H;loadBadges(rows.slice(0,0).map(x=>x.it.ticker))}
$('#clf').onsubmit=async ev=>{ev.preventDefault();try{const j=await post('/api/calls',{tickers:$('#cltk').value});CL.st=j.status;clRender();setTimeout(clLoad,1500)}catch(e){$('#clnote').textContent='· '+e.message}};
document.addEventListener('change',e=>{const el=e.target.closest('[data-clper],[data-cltrend]');if(!el)return;if(el.dataset.clper!==undefined)CL.per=+el.value;else CL.trend=el.checked;clSave();clRender()});

// ---- beginner options calculator ----
// legs: t = C call / P put / S stock; s = +1 buy / -1 sell; k,p = which input holds the strike / price
const CS={
 buycall:{n:'Buy a call',legs:[{t:'C',s:1,k:'k1',p:'p1'}],f:{k1:'Call strike',p1:'Premium you pay (per share)'},pick:{k1:['C',0.55]},
  d:'You think the stock goes <b>up</b>. You pay a premium for the right to buy 100 shares at the strike. Most you can lose: what you pay.'},
 leaps:{n:'LEAPS call',legs:[{t:'C',s:1,k:'k1',p:'p1'}],f:{k1:'Call strike',p1:'Premium you pay (per share)'},pick:{k1:['C',0.75]},minDte:270,
  d:'A long-dated call (9+ months). An in-the-money LEAPS (delta ~0.75) moves much like the stock for less cash, with slow time decay. Most you can lose: what you pay.'},
 buyput:{n:'Buy a put',legs:[{t:'P',s:1,k:'k1',p:'p1'}],f:{k1:'Put strike',p1:'Premium you pay (per share)'},pick:{k1:['P',-0.45]},
  d:'You think the stock goes <b>down</b> (or you want insurance on shares you own). Most you can lose: what you pay.'},
 csp:{n:'Sell a put (cash-secured)',legs:[{t:'P',s:-1,k:'k1',p:'p1'}],f:{k1:'Put strike',p1:'Premium you collect (per share)'},pick:{k1:['P',-0.25]},
  d:'You are happy to buy the stock cheaper. You collect a premium now; if the stock is below the strike at expiration you buy 100 shares at the strike. Cash held: strike × 100.'},
 cc:{n:'Covered call',legs:[{t:'S',s:1,b:'sb'},{t:'C',s:-1,k:'k1',p:'p1'}],f:{sb:'Your share cost (per share)',k1:'Call strike',p1:'Premium you collect (per share)'},pick:{k1:['C',0.30]},
  d:'You own 100 shares and sell a call above today\'s price. You keep the premium; if the stock ends above the strike your shares are sold at the strike.'},
 pcs:{n:'Put credit spread',legs:[{t:'P',s:-1,k:'k1',p:'p1'},{t:'P',s:1,k:'k2',p:'p2'}],f:{k1:'Sell put strike (higher)',p1:'Premium collected',k2:'Buy put strike (lower)',p2:'Premium paid'},pick:{k1:['P',-0.25],k2:['W',-0.03]},
  d:'Neutral-to-bullish. Sell a put and buy a cheaper, lower put as protection. You keep the credit if the stock stays above the short strike. Max loss is capped.'},
 cds:{n:'Bull call spread',legs:[{t:'C',s:1,k:'k1',p:'p1'},{t:'C',s:-1,k:'k2',p:'p2'}],f:{k1:'Buy call strike (lower)',p1:'Premium paid',k2:'Sell call strike (higher)',p2:'Premium collected'},pick:{k1:['C',0.55],k2:['C',0.30]},
  d:'Bullish, cheaper than a call. Profit is capped at the gap between strikes minus what you pay. Max loss: what you pay.'},
 ccs:{n:'Call credit spread',legs:[{t:'C',s:-1,k:'k1',p:'p1'},{t:'C',s:1,k:'k2',p:'p2'}],f:{k1:'Sell call strike (lower)',p1:'Premium collected',k2:'Buy call strike (higher)',p2:'Premium paid'},pick:{k1:['C',0.25],k2:['W',0.03]},
  d:'Neutral-to-bearish. You keep the credit if the stock stays below the short call. Max loss is capped.'},
 pds:{n:'Put debit spread',legs:[{t:'P',s:1,k:'k1',p:'p1'},{t:'P',s:-1,k:'k2',p:'p2'}],f:{k1:'Buy put strike (higher)',p1:'Premium paid',k2:'Sell put strike (lower)',p2:'Premium collected'},pick:{k1:['P',-0.50],k2:['P',-0.25]},
  d:'Bearish, cheaper than a put. Profit capped at the gap between strikes minus what you pay.'}};
let CA={strat:'buycall',tk:'',S:100,dte:30,iv:40,qty:1,v:{k1:100,p1:4,k2:95,p2:2,sb:100},ivk:{},touched:{},chain:null,init:false,wiP:null,wiD:0};
const nd=x=>{const a=Math.abs(x),t=1/(1+0.2316419*a),y=1-Math.exp(-a*a/2)/Math.sqrt(2*Math.PI)*t*(0.31938153+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))));return x>=0?y:1-y};
function bs(t,S,K,T,iv,r=0.04){if(T<=1e-6||iv<=0)return t==='C'?Math.max(S-K,0):Math.max(K-S,0);const sd=iv*Math.sqrt(T),d1=(Math.log(S/K)+(r+iv*iv/2)*T)/sd,d2=d1-sd;
 return t==='C'?S*nd(d1)-K*Math.exp(-r*T)*nd(d2):K*Math.exp(-r*T)*nd(-d2)-S*nd(-d1)}
function caLegs(){return CS[CA.strat].legs.map(L=>L.t==='S'?{t:'S',s:L.s,k:0,p:+CA.v[L.b]||CA.S,iv:0}:{t:L.t,s:L.s,k:+CA.v[L.k],p:+CA.v[L.p]||0,iv:(CA.ivk[L.k]||CA.iv)/100})}
function caPL(x,days){const T=Math.max(CA.dte-days,0)/365;let v=0;for(const L of caLegs()){if(L.t==='S')v+=L.s*(x-L.p);else v+=L.s*((days>=CA.dte?bs(L.t,x,L.k,0,L.iv):bs(L.t,x,L.k,T,L.iv))-L.p)}return v*100*CA.qty}
function caModelPrem(key){const L=CS[CA.strat].legs.find(l=>l.p===key);if(!L)return null;return bs(L.t,CA.S,+CA.v[L.k],CA.dte/365,(CA.ivk[L.k]||CA.iv)/100)}
function caStats(){const S=CA.S,sig=CA.iv/100*Math.sqrt(Math.max(CA.dte,1)/365),legs=caLegs();
 const lo=S*Math.exp(-6*sig),hi=S*Math.exp(6*sig),N=1500,xs=[];for(let i=0;i<=N;i++)xs.push(lo+(hi-lo)*i/N);
 const pe=x=>caPL(x,CA.dte),ys=xs.map(pe);
 // probability of profit at expiration: lognormal density with drift r
 const mu=Math.log(S)+(0.04-(CA.iv/100)**2/2)*CA.dte/365;let pop=0,tot=0;
 for(let i=1;i<=N;i++){const x=(xs[i]+xs[i-1])/2,d=Math.exp(-((Math.log(x)-mu)**2)/(2*sig*sig))/(x*sig*Math.sqrt(2*Math.PI))*(xs[i]-xs[i-1]);tot+=d;if(pe(x)>0)pop+=d}
 pop=tot?pop/tot*100:null;
 const slopeHi=legs.reduce((a,L)=>a+(L.t==='C'||L.t==='S'?L.s:0),0),slopeLo=legs.reduce((a,L)=>a+(L.t==='P'?-L.s:0)+(L.t==='S'?L.s:0),0);
 const ext=[pe(0.0001),...ys,pe(S*20)],mx=Math.max(...ext),mn=Math.min(...ext);
 const bes=[];for(let i=1;i<=N;i++){const a=ys[i-1],b=ys[i];if((a<0&&b>=0)||(a>0&&b<=0)){bes.push(xs[i-1]+(xs[i]-xs[i-1])*(a/(a-b)))}}
 const net=legs.reduce((a,L)=>a+(L.t==='S'?0:L.s*L.p),0)*100*CA.qty;  // >0 you pay, <0 you collect
 let delta=0,theta=0,vega=0;const h=0.01*S;
 for(const L of legs){if(L.t==='S'){delta+=L.s*100*CA.qty;continue}const T=Math.max(CA.dte,0.5)/365;
  delta+=L.s*(bs(L.t,S+h,L.k,T,L.iv)-bs(L.t,S-h,L.k,T,L.iv))/(2*h)*100*CA.qty;
  theta+=L.s*(bs(L.t,S,L.k,Math.max(T-1/365,0),L.iv)-bs(L.t,S,L.k,T,L.iv))*100*CA.qty;
  vega+=L.s*(bs(L.t,S,L.k,T,L.iv+0.01)-bs(L.t,S,L.k,T,L.iv))*100*CA.qty}
 return {xs,ys,pop,mx:slopeHi>0?Infinity:mx,mn:slopeHi<0?-Infinity:mn,bes,net,delta,theta,vega,slopeHi,slopeLo,sig}}
function caCapital(st){const v=CA.v,q=CA.qty*100;switch(CA.strat){case'csp':return (+v.k1-(+v.p1))*q;case'cc':return ((+v.sb||CA.S)-(+v.p1))*q;
 case'pcs':case'ccs':return isFinite(st.mn)?-st.mn:null;default:return st.net>0?st.net:null}}
const cusd=v=>v==null?'–':!isFinite(v)?'unlimited':(v<0?'-':'')+'$'+Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:0});
function caWhere(st,target){const xs=st.xs,ys=st.ys,tol=Math.max(1,Math.abs(target)*0.002);let a=null,b=null;
 for(let i=0;i<xs.length;i++){if(Math.abs(ys[i]-target)<=tol){if(a==null)a=xs[i];b=xs[i]}}
 if(a==null)return'';const top=b>=xs[xs.length-2],bot=a<=xs[1];const tk=CA.tk||'the stock',step=(xs[1]-xs[0])*3;
 const snap=v=>{let best=v;for(const L of caLegs())if(L.t!=='S'&&Math.abs(L.k-v)<=step)best=L.k;return best};a=snap(a);b=snap(b);
 return top&&!bot?`if ${tk} is at or above $${fmt(a)}`:bot&&!top?`if ${tk} is at or below $${fmt(b)}`:top&&bot?'at any price':`if ${tk} is between $${fmt(a)} and $${fmt(b)}`}
function caInputs(){const C=CS[CA.strat],f=C.f;
 const num=(id,l,v,step,extra='')=>`<label>${l}<input id="${id}" type="number" step="${step}" value="${v}" inputmode="decimal"${extra}></label>`;
 let h=num('caS','Stock price $',CA.S,'0.01')+num('cadte','Days until expiration',CA.dte,'1',' min="0"')+num('caiv','Implied volatility %',fmt(CA.iv,1).replace(/,/g,''),'0.1',' min="1"')+num('caqty','Contracts',CA.qty,'1',' min="1"');
 const dl=t=>CA.chain?`<datalist id="cadl${t}">${(t==='C'?CA.chain.calls:CA.chain.puts).map(o=>`<option value="${o.k}">`).join('')}</datalist>`:'';
 for(const[k,l]of Object.entries(f)){const leg=C.legs.find(L=>L.k===k||L.p===k||L.b===k);
  if(k.startsWith('k'))h+=`<label>${l}<input data-v="${k}" type="number" step="0.5" value="${CA.v[k]}" inputmode="decimal"${CA.chain?` list="cadl${leg.t}"`:''}></label>`;
  else{const m=caModelPrem(k);h+=`<label>${l}<input data-v="${k}" type="number" step="0.01" value="${(+CA.v[k]).toFixed(2)}" inputmode="decimal"><span class="mute" style="font-size:11.5px">${CA.chain&&!CA.touched[k]?'from the live chain (mid)':m!=null?'model value $'+m.toFixed(2):''}</span></label>`}}
 $('#cain').innerHTML=h+dl('C')+dl('P');
 $('#cain').querySelectorAll('input').forEach(el=>el.oninput=()=>{const k=el.dataset.v,n=parseFloat(el.value);if(isNaN(n))return;
  if(el.id==='caS')CA.S=n;else if(el.id==='cadte')CA.dte=Math.max(0,Math.round(n));else if(el.id==='caiv')CA.iv=Math.max(1,n);else if(el.id==='caqty')CA.qty=Math.max(1,Math.round(n));
  else{CA.v[k]=n;if(k.startsWith('p')||k==='sb')CA.touched[k]=true;else caStrikeChanged(k)}
  if(['caS','cadte','caiv'].includes(el.id)||k&&k.startsWith('k'))caAutoPrem(el);caOut()})}
function caStrikeChanged(k){CA.ivk[k]=null;if(!CA.chain)return;const L=CS[CA.strat].legs.find(l=>l.k===k);const o=(L.t==='C'?CA.chain.calls:CA.chain.puts).find(x=>x.k===+CA.v[k]);
 if(o){CA.ivk[k]=o.iv*100;if(!CA.touched[L.p])CA.v[L.p]=+(((o.bid+o.ask)/2)||0).toFixed(2)}}
function caAutoPrem(skip){for(const L of CS[CA.strat].legs){if(!L.p||CA.touched[L.p])continue;const fromChain=CA.chain&&(L.t==='C'?CA.chain.calls:CA.chain.puts).some(x=>x.k===+CA.v[L.k])&&CA.S===+CA.chain.spot.toFixed(2)&&CA.dte===CA.chain.dte;
  if(!fromChain){const m=caModelPrem(L.p);if(m!=null)CA.v[L.p]=+m.toFixed(2)}
  const el=$(`#cain input[data-v="${L.p}"]`);if(el&&el!==skip)el.value=(+CA.v[L.p]).toFixed(2)}}
function caChart(st){const W=Math.min(860,Math.max(300,($('#caout').clientWidth||700))),H=250,pl=58,pr=12,pt=14,pb=28,iw=W-pl-pr,ih=H-pt-pb;
 const S=CA.S,x0=S*Math.exp(-2.6*st.sig)*0.97,x1=S*Math.exp(2.6*st.sig)*1.03,n=160,X=[],E=[],T=[];const day=CA.wiD||0;
 for(let i=0;i<=n;i++){const x=x0+(x1-x0)*i/n;X.push(x);E.push(caPL(x,CA.dte));T.push(caPL(x,Math.min(day,CA.dte)))}
 let y0=Math.min(...E,...T,0),y1=Math.max(...E,...T,0);const pad=(y1-y0)*0.08||1;y0-=pad;y1+=pad;
 const sx=x=>pl+(x-x0)/(x1-x0)*iw,sy=y=>pt+(y1-y)/(y1-y0)*ih,line=A=>A.map((y,i)=>`${i?'L':'M'}${sx(X[i]).toFixed(1)},${sy(y).toFixed(1)}`).join('');
 const area=`${line(E)}L${sx(X[n]).toFixed(1)},${sy(0).toFixed(1)}L${sx(X[0]).toFixed(1)},${sy(0).toFixed(1)}Z`,z=sy(0);
 let g='';for(let i=0;i<=4;i++){const y=y0+(y1-y0)*i/4;g+=`<line x1="${pl}" x2="${W-pr}" y1="${sy(y)}" y2="${sy(y)}" stroke="var(--line)"/><text x="${pl-6}" y="${sy(y)+4}" text-anchor="end" font-size="11" fill="var(--mute)">${cusd(y)}</text>`}
 for(let i=0;i<=5;i++){const x=x0+(x1-x0)*i/5;g+=`<text x="${sx(x)}" y="${H-8}" text-anchor="middle" font-size="11" fill="var(--mute)">$${fmtP(x)}</text>`}
 const be=st.bes.filter(b=>b>x0&&b<x1).map(b=>`<circle cx="${sx(b)}" cy="${z}" r="4" fill="var(--amber)"/><text x="${sx(b)}" y="${z-8}" text-anchor="middle" font-size="11" fill="var(--amber)">BE $${fmtP(b)}</text>`).join('');
 const wp=CA.wiP??S,wv=caPL(wp,Math.min(day,CA.dte));
 return `<svg viewBox="0 0 ${W} ${H}" style="max-width:${W}px"><defs><clipPath id="caup"><rect x="0" y="0" width="${W}" height="${z}"/></clipPath><clipPath id="cadn"><rect x="0" y="${z}" width="${W}" height="${H}"/></clipPath></defs>${g}
 <path d="${area}" fill="var(--call)" opacity=".16" clip-path="url(#caup)"/><path d="${area}" fill="var(--put)" opacity=".16" clip-path="url(#cadn)"/>
 <line x1="${pl}" x2="${W-pr}" y1="${z}" y2="${z}" stroke="var(--mute)"/><line x1="${sx(S)}" x2="${sx(S)}" y1="${pt}" y2="${pt+ih}" stroke="var(--mute)" stroke-dasharray="3 3"/><text x="${sx(S)+4}" y="${pt+10}" font-size="11" fill="var(--mute)">now $${fmtP(S)}</text>
 <path d="${line(E)}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>${day<CA.dte?`<path d="${line(T)}" fill="none" stroke="var(--fib)" stroke-width="2" stroke-dasharray="6 4"/>`:''}${be}
 ${wp>x0&&wp<x1?`<circle cx="${sx(wp)}" cy="${sy(wv)}" r="5" fill="var(--fib)" stroke="var(--card)" stroke-width="2"/>`:''}</svg>
 <div class="legend mute" style="font-size:12px"><span><b style="color:var(--accent)">━</b> profit/loss at expiration</span>${day<CA.dte?`<span><b style="color:var(--fib)">┅</b> ${day===0?'today':'in '+day+' day'+(day==1?'':'s')} (estimate)</span>`:''}<span><b style="color:var(--amber)">●</b> breakeven</span></div>`}
function caOut(){const st=caStats(),C=CS[CA.strat],tk=CA.tk||'the stock',S=CA.S,cap=caCapital(st);
 const tile=(l,v,c='',s='')=>`<div class="gt b-${c||'mute'}"><div class="l">${l}</div><div class="v ${c}">${v}</div>${s?`<div class="l">${s}</div>`:''}</div>`;
 const netTxt=st.net>0?`You pay <b>${cusd(st.net)}</b> up front`:st.net<0?`You collect <b>${cusd(-st.net)}</b> up front`:'No cash changes hands up front';
 let h=`<div class="gtiles">${tile(st.net>=0?'You pay':'You collect',cusd(Math.abs(st.net)),st.net>=0?'put':'call')}${tile('Max profit',cusd(st.mx),'call',isFinite(st.mx)?caWhere(st,st.mx):'no cap above breakeven')}${tile('Max loss',cusd(st.mn),'put',isFinite(st.mn)?caWhere(st,st.mn):'grows the further the stock moves against you')}
  ${tile('Breakeven at expiration',st.bes.length?st.bes.map(b=>'$'+fmt(b)).join(' / '):'–','',st.bes.map(b=>`${b>=S?'+':''}${fmt((b/S-1)*100,1)}% from now`).join(' / '))}${tile('Chance of profit',st.pop==null?'–':fmt(st.pop,0)+'%','','at expiration · model from IV')}${tile('Cash needed',cusd(cap),'',CA.strat==='csp'?'strike × 100 held, minus the premium':CA.strat==='cc'?'100 shares, minus the premium':'')}</div>`;
 const b=[];b.push(`${netTxt}${CA.qty>1?` for ${CA.qty} contracts`:''}.`);
 if(isFinite(st.mx))b.push(`<b>Best case:</b> +${cusd(st.mx)} ${caWhere(st,st.mx)} at expiration.`);else b.push(`<b>Best case:</b> no cap — the further ${tk} moves your way past breakeven, the more you make.`);
 if(isFinite(st.mn))b.push(`<b>Worst case:</b> ${cusd(st.mn)} ${caWhere(st,st.mn)} at expiration.`);else b.push(`<b>Worst case:</b> no cap — losses keep growing the further ${tk} moves against you.`);
 if(st.bes.length)b.push(`<b>Breakeven:</b> ${tk} needs to finish ${st.bes.length>1?'between':(caPL(st.bes[0]*1.05,CA.dte)>0?'above':'below')} ${st.bes.map(x=>'$'+fmt(x)).join(' and ')} on expiration day.`);
 b.push(`<b>Right now</b> this position acts like ${st.delta>=0?'owning':'shorting'} about <b>${fmt(Math.abs(st.delta),0)} shares</b>: a $1 move in ${tk} changes its value by about $${fmt(Math.abs(st.delta),0)}.`);
 b.push(`<b>Time decay:</b> each day that passes ${st.theta>=0?'adds':'costs'} about <b>$${fmt(Math.abs(st.theta),0)}</b> if nothing else changes${st.theta>=0?' — time is on your side.':' — time works against you.'}`);
 b.push(`<b>Volatility:</b> if implied volatility rises 1 point the position ${st.vega>=0?'gains':'loses'} about $${fmt(Math.abs(st.vega),0)}.`);
 const risk={csp:`If ${tk} is below $${fmt(+CA.v.k1)} at expiration you will likely be assigned 100 shares per contract at $${fmt(+CA.v.k1)} — effective cost $${fmt(+CA.v.k1-(+CA.v.p1))} per share.`,
  cc:`If ${tk} finishes above $${fmt(+CA.v.k1)} your shares are sold at $${fmt(+CA.v.k1)}. You still own the shares (and their downside) until then.`,
  buycall:'Calls can expire worthless. Most buyers close before expiration to keep some value.',leaps:'Check the time value: a deep in-the-money LEAPS wastes less to time decay.',
  pcs:'You can be assigned early if the short put goes deep in the money. Many traders take profit at 50% of the credit.',ccs:'Early assignment is possible if the short call goes deep in the money, especially before a dividend.'}[CA.strat];
 if(risk)b.push(`<b>Watch out:</b> ${risk}`);
 h+=`<ul class="cab">${b.map(x=>`<li>${x}</li>`).join('')}</ul>`;
 h+=`<div id="cachart">${caChart(st)}</div>`;
 const wp=CA.wiP??S,wd=Math.min(CA.wiD||0,CA.dte),wv=caPL(wp,wd);
 h+=`<div class="sets pfil"><label>What if ${esc(tk)} is at $<b id="cawpv">${fmt(wp)}</b> <span class="mute">(${wp>=S?'+':''}${fmt((wp/S-1)*100,1)}%)</span><input id="cawp" type="range" min="${(S*0.6).toFixed(2)}" max="${(S*1.4).toFixed(2)}" step="${(S/500).toFixed(3)}" value="${wp}"></label>
  <label>…in <b>${wd}</b> day${wd==1?'':'s'}${wd>=CA.dte?' (expiration)':''}<input id="cawd" type="range" min="0" max="${CA.dte}" step="1" value="${wd}"></label>
  <label>Estimated profit / loss<span style="font-size:22px;font-weight:700" class="${wv>=0?'call':'put'}">${wv>=0?'+':''}${cusd(wv)}</span><span class="mute">${cap?`${wv>=0?'+':''}${fmt(wv/cap*100,1)}% of the cash needed`:''}</span></label></div>`;
 const mv=[-20,-10,-5,0,5,10,20],half=Math.round(CA.dte/2);
 h+=`<div class="tw"><table><tr><th>If ${esc(tk)} moves</th>${mv.map(m=>`<th>${m>0?'+':''}${m}%<br><span class="mute">$${fmtP(S*(1+m/100))}</span></th>`).join('')}</tr>
  <tr><td>At expiration</td>${mv.map(m=>{const v=caPL(S*(1+m/100),CA.dte);return `<td class="${v>=0?'call':'put'}">${v>=0?'+':''}${cusd(v)}</td>`}).join('')}</tr>
  ${half>0&&half<CA.dte?`<tr><td>Halfway (${half}d)</td>${mv.map(m=>{const v=caPL(S*(1+m/100),half);return `<td class="${v>=0?'call':'put'}">${v>=0?'+':''}${cusd(v)}</td>`}).join('')}</tr>`:''}</table></div>
  <p class="mute">Estimates use the Black-Scholes model with your implied volatility, a 4% interest rate and no dividends; real prices also move with volatility, bid/ask spreads and early assignment. ${CA.chain?`Prices from CBOE (15-min delayed) as of ${esc(String(CA.chain.as_of))}.`:'Enter a ticker above to use real option prices.'} Not financial advice.</p>`;
 $('#caout').innerHTML=h;
 $('#cawp').oninput=e=>{CA.wiP=+e.target.value;caOut()};$('#cawd').oninput=e=>{CA.wiD=+e.target.value;caOut()}}
function caStrat(){$('#castrat').innerHTML=Object.entries(CS).map(([k,v])=>`<button type="button" class="seg${CA.strat===k?' on':''}" data-s="${k}">${v.n}</button>`).join('');
 $('#cadesc').innerHTML=CS[CA.strat].d;$('#castrat').querySelectorAll('.seg').forEach(b=>b.onclick=()=>caSetStrat(b.dataset.s))}
function caPick(){const C=CS[CA.strat],ch=CA.chain;CA.touched={};CA.ivk={};
 for(const[k,[t,d]]of Object.entries(C.pick||{})){if(!ch){continue}
  if(t==='W'){const base=+CA.v.k1,L=C.legs.find(l=>l.k===k),list=L.t==='C'?ch.calls:ch.puts,target=base*(1+d);
   const o=list.filter(x=>d<0?x.k<base:x.k>base).sort((a,b)=>Math.abs(a.k-target)-Math.abs(b.k-target))[0];if(o)CA.v[k]=o.k;continue}
  const list=(t==='C'?ch.calls:ch.puts).filter(x=>x.bid>0&&x.delta);const o=list.sort((a,b)=>Math.abs(a.delta-d)-Math.abs(b.delta-d))[0];if(o)CA.v[k]=o.k}
 if(!ch){const S=CA.S;Object.assign(CA.v,{k1:+(S).toFixed(0),k2:+(CA.strat==='pcs'||CA.strat==='pds'?S*0.95:S*1.05).toFixed(0)});
  if(CA.strat==='csp'||CA.strat==='pcs')CA.v.k1=+(S*0.95).toFixed(0),CA.v.k2=+(S*0.90).toFixed(0);if(CA.strat==='cc'||CA.strat==='ccs')CA.v.k1=+(S*1.05).toFixed(0),CA.v.k2=+(S*1.10).toFixed(0);
  if(CA.strat==='leaps')CA.v.k1=+(S*0.8).toFixed(0)}
 CA.v.sb=CA.S;for(const L of C.legs)if(L.k)caStrikeChanged(L.k);caAutoPrem()}
async function caSetStrat(s,keep){CA.strat=s;CA.wiP=null;const C=CS[s];
 if(C.minDte&&CA.dte<C.minDte){if(CA.chain&&CA.tk){const e=CA.chain.exps.find(x=>x.dte>=C.minDte);if(e){await caLoad(CA.tk,e.exp,true);return}}else CA.dte=540}
 if(!keep)caPick();caStrat();caInputs();caOut()}
async function caLoad(tk,exp,repick=true,after){tk=(tk||'').trim().toUpperCase();if(!tk){CA.chain=null;CA.tk='';caSetStrat(CA.strat);return}
 $('#canote').textContent=`Loading ${tk}…`;try{const j=await (await fetch('/api/chain?t='+encodeURIComponent(tk)+(exp?'&exp='+exp:''))).json();if(j.error)throw new Error(j.error);
  CA.chain=j;CA.tk=j.ticker;CA.S=+j.spot.toFixed(2);CA.dte=j.dte;$('#catk').value=j.ticker;
  const near=[...j.calls,...j.puts].filter(o=>o.iv>0).sort((a,b)=>Math.abs(a.k-j.spot)-Math.abs(b.k-j.spot)).slice(0,4);CA.iv=near.length?near.reduce((a,o)=>a+o.iv,0)/near.length*100:CA.iv;
  $('#caexp').innerHTML=j.exps.map(e=>`<option value="${e.exp}"${e.exp===j.exp?' selected':''}>${dshort(e.exp)} · ${e.dte}d${e.dte>=270?' (LEAPS)':''}</option>`).join('');
  $('#canote').textContent=`${j.ticker} $${fmt(j.spot)} · CBOE 15-min delayed`;
  if(after)after();else if(repick)caPick();caStrat();caInputs();caOut()}catch(e){$('#canote').textContent='Could not load: '+e.message}}
function caInit(){CA.init=true;caPick();caStrat();caInputs();caOut();
 $('#caload').onsubmit=ev=>{ev.preventDefault();caLoad($('#catk').value,null)};$('#caexp').onchange=e=>caLoad(CA.tk,e.target.value)}
function calcOpen(o){tab('calc');CA.strat=o.strat;caLoad(o.tk,o.exp,false,()=>{CA.touched={};CA.ivk={};if(o.k1!=null)CA.v.k1=o.k1;if(o.k2!=null)CA.v.k2=o.k2;CA.v.sb=CA.S;
  for(const L of CS[CA.strat].legs)if(L.k)caStrikeChanged(L.k);caAutoPrem()})}
function tab(n){track('tab:'+n);const map={picks:'#tabPicks',edge:'#tabEdge',main:'#tabMain',puts:'#tabPuts',calls:'#tabCalls',calc:'#tabCalc',flow:'#tabFlow'};
 for(const[k,id]of Object.entries(map))$(id).hidden=k!==n;document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===n));
 try{history.replaceState(null,'',location.pathname+location.search+(n==='main'?'':'#'+n))}catch(e){}
 if(n==='puts'&&!PT.loaded)ptLoad();if(n==='edge'){if(!EG.loaded)egLoad(false);if(!SQ.loaded)sqLoad(false);if(!TL.loaded)tlLoad(false)}if(n==='flow'){if(!FL.mkt)flMarket();if(!FL.tk&&CC.r)flGo(CC.r.ticker)}
 if(n==='picks'&&!PK.loaded){pkLoad();mnLoad(false);ceLoad(false);if(!EG.d&&!EG.busy)egLoad(false)}if(n==='calls'&&!CL.loaded)clLoad();if(n==='calc'){if(!CA.init)caInit();if(!CSV.loaded){CSV.loaded=true;csLoad(true)}}scrollTo(0,0)}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>tab(b.dataset.tab));
if(['#puts','#flow','#calls','#calc','#picks','#edge'].includes(location.hash))tab(location.hash.slice(1));

// ---- block-trade tape ----
let BK={list:[],cfg:{},tickers:[],note:'',last:''};
async function bkLoad(){try{const j=await (await fetch('/api/blocks')).json();BK.list=j.blocks||[];BK.cfg=j.cfg||{};BK.tickers=j.tickers||[];BK.note=j.note||'';BK.last=j.last_poll||'';bkRender()}catch(e){}}
function bkRender(){const L=BK.list,tape=$('#tape');
 if(tape){if(!L.length)tape.hidden=true;else{tape.hidden=false;const items=L.slice(0,30).map(b=>`<span class="tp"><b>${esc(b.ticker)}</b> ${Number(b.sz).toLocaleString()} @ ${fmt(b.px)} <span class="${b.usd>=(BK.cfg.block_alert_usd||5e6)?'amb':'call'}">${usd2(b.usd)}</span> <span class="mute">${esc(b.t.slice(0,5))}</span></span>`).join('');
  $('#tapein').innerHTML=items+items;$('#tapein').style.animationDuration=Math.max(20,L.slice(0,30).length*4)+'s'}}
 const el=$('#bklist');if(!el)return;const c=BK.cfg;
 $('#bknote').textContent=`· ${BK.note||'–'}${BK.last?' · last check '+BK.last:''} · watching ${BK.tickers.join(', ')||'–'}`;
 if(document.activeElement!==$('#bkmin'))$('#bkmin').value=c.block_min_usd??1e6;if(document.activeElement!==$('#bksh'))$('#bksh').value=c.block_min_shares??20000;
 if(document.activeElement!==$('#bkal'))$('#bkal').value=c.block_alert_usd??5e6;if(document.activeElement!==$('#bktk'))$('#bktk').value=(c.block_tickers||[]).join(', ');$('#bkon').checked=c.blocks_on!==false;
 if(!L.length){el.innerHTML='<p class="mute">No block prints yet. The tape fills in during market hours (9:30 AM–4:15 PM ET); prints at or above your size threshold appear here within about a minute.</p>';return}
 el.innerHTML='<div class="tw"><table><tr><th>Time (ET)</th><th>Ticker</th><th>Shares</th><th>Price</th><th>Value</th><th>Day</th></tr>'+L.slice(0,60).map(b=>`<tr><td>${esc(b.t)}</td><td><a href="#" onclick="flGo('${esc(b.ticker)}');return false"><b>${esc(b.ticker)}</b></a></td><td>${Number(b.sz).toLocaleString()}</td><td>${fmt(b.px)}</td><td class="${b.usd>=(c.block_alert_usd||5e6)?'amb':''}"><b>${usd2(b.usd)}</b></td><td class="mute">${esc(b.day.slice(5))}</td></tr>`).join('')+'</table></div>'}
async function bkSave(p){try{await post('/api/watch',p);await bkLoad()}catch(e){$('#bknote').textContent='· '+e.message}}
$('#bkmin').onchange=e=>bkSave({block_min_usd:+e.target.value});$('#bksh').onchange=e=>bkSave({block_min_shares:+e.target.value});
$('#bkal').onchange=e=>bkSave({block_alert_usd:+e.target.value});$('#bkon').onchange=e=>bkSave({blocks_on:e.target.checked});
$('#bktk').onchange=e=>bkSave({block_tickers:e.target.value.toUpperCase().split(/[\s,]+/).filter(Boolean)});
bkLoad();setInterval(()=>{if(!document.hidden)bkLoad()},20000);
// ---- rotation: ETFs / stocks ----
let RV={kind:'etf',grp:'',sort:'mom'};
try{RV.sort=localStorage.getItem('walls_rvsort')||'mom'}catch(e){}
// best put credit spread / short put for a ticker in the selected expiration week (chips), meeting the return floors
function rotBest(t){const it=((SC.res||{}).items||{})[t];if(!it)return null;let b=null;
 for(const c of scPool(it)){if(!wkPass(c.exp))continue;const ret=retOf(c);if(ret==null||ret<(c._k==='PCS'?15:10))continue;
  if(!b||c.pop>b.pop||(c.pop===b.pop&&ret>b.ret))b={...c,ret}}return b}
const RVSORTS=[['mom','Momentum (default)'],['pop','Highest chance of profit · selected week'],['up','Trending up (trend, then 1-month strength)'],['rs1','1-month strength'],['rs3','3-month strength'],['gex','Highest GEX (net, all expirations)']];
function rotRender(){const r=SC.res;if(!r)return;const el=$('#scrot');if(!el)return;
 const SG=((SC.sg||{}).etfs)||{},etf=RV.kind==='etf';
 const list=(etf?(r.rotation||[]):(r.stock_rotation||[])).filter(e=>e.ticker!=='SPY'&&e.quad!=='Benchmark');const groups=[...new Set(list.map(e=>e.sector))].sort();
 if(RV.grp&&!groups.includes(RV.grp))RV.grp='';
 let h=`<div class="ctl"><button class="seg${etf?' on':''}" onclick="RV.kind='etf';rotRender()">Sectors &amp; themes (${list.length&&etf?list.length:(r.rotation||[]).filter(e=>e.ticker!=='SPY').length})</button><button class="seg${!etf?' on':''}" onclick="RV.kind='stk';rotRender()">Stocks (${(r.stock_rotation||[]).length})</button>
  <select onchange="RV.grp=this.value;rotRender()" style="margin-left:6px"><option value="">All groups</option>${groups.map(g=>`<option${g===RV.grp?' selected':''}>${esc(g)}</option>`).join('')}</select>
  <select onchange="RV.sort=this.value;try{localStorage.setItem('walls_rvsort',this.value)}catch(e){};rotRender()" style="margin-left:6px" title="Sort the table">${RVSORTS.map(([k,l])=>`<option value="${k}"${RV.sort===k?' selected':''}>Sort: ${l}</option>`).join('')}</select>
  <span class="mute">${r.rotation_ts?'updated '+esc(r.rotation_ts):''}</span></div>`;
 if(!(r.stock_rotation||[]).length&&!etf){el.innerHTML=h+'<p class="mute">Press Run scan to build the stock rotation list.</p>';return}
 const rows=list.filter(e=>!RV.grp||e.sector===RV.grp).map(e=>({...e,_b:rotBest(e.ticker)}));
 const n0=v=>v==null||isNaN(v)?-1e9:+v;
 if(RV.sort==='pop')rows.sort((a,b)=>n0(b._b&&b._b.pop)-n0(a._b&&a._b.pop)||n0(b._b&&b._b.ret)-n0(a._b&&a._b.ret));
 else if(RV.sort==='up')rows.sort((a,b)=>n0(b.trend)-n0(a.trend)||n0(b.rs1)-n0(a.rs1)||n0(b.r1)-n0(a.r1));
 else if(RV.sort==='rs1')rows.sort((a,b)=>n0(b.rs1)-n0(a.rs1));
 else if(RV.sort==='rs3')rows.sort((a,b)=>n0(b.rs3)-n0(a.rs3));
 else if(RV.sort==='gex'){const gx=e=>{const g=SG[e.ticker];return g&&g.net!=null?g.net:gexOf(e.ticker)};rows.sort((a,b)=>n0(gx(b))-n0(gx(a)))}
 else rows.sort((a,b)=>n0(b.mom)-n0(a.mom));
 const bestCell=b=>!b?`<span class="mute" title="no put credit spread (15%+ return) or short put (10%+) in ${esc(wkLabel(GF.wk))}">–</span>`:`<b class="${b.pop>=80?'call':'acc'}">${fmt(b.pop,0)}%</b> <span class="mute">${b._k==='PCS'?fmtK(b.short)+'/'+fmtK(b.long)+'P':'sell '+fmtK(b.short)+'P'} ${esc(String(b.exp).slice(5))} · ${fmt(b.ret,0)}%</span>`;
 const gc=(c,g,day)=>{if(c==null)return'<span class="mute">–</span>';const up=c.chg>=0,a=String(c.at),same=g&&String(g.at).slice(0,10)===a.slice(0,10),
  when=day?a.slice(5,10).replace('-','/'):(same?'':a.slice(5,10).replace('-','/')+' ')+a.slice(11,16),pc=c.pct==null?'':` (${up?'+':'−'}${Math.abs(c.pct)>999?'999+':fmt(Math.abs(c.pct),0)}%)`;
  return `<span class="${up?'call':'put'}" style="white-space:nowrap" title="GEX ${up?'rose':'fell'} by ${money(Math.abs(c.chg))} since ${esc(a.replace('T',' '))}: now ${money(g&&g.net)}, then ${money(g&&g.net-c.chg)}"><b>${up?'▲ Up':'▼ Down'}</b> ${money(Math.abs(c.chg))}${pc}</span><br><span class="mute" style="font-size:11px">since ${esc(when)}</span>`};
 const anyHist=Object.values(SG).some(g=>g.d1||g.w1||g.m1);
 h+=`<table><tr><th>${etf?'Sector / theme':'Stock'}</th>${etf?'':'<th>Group</th>'}<th>Rotation</th><th title="Highest chance of profit among put credit spreads (15%+ return) and short puts (10%+) expiring in the week picked with the week buttons above; then that trade’s strikes, expiration and return">Best chance · ${esc(wkLabel(GF.wk))}</th>${!etf?'<th>Its sector</th>':''}<th title="1-month return minus the broad market’s (S&amp;P 500)">1-month strength</th><th title="3-month return minus the broad market’s">3-month strength</th><th>Trend</th><th>RSI</th><th>Momentum</th><th title="Daily net GEX (bars, last sessions) and the GEX of each of the next 4 expiration weeks">GEX trend · next 4 weeks</th>${etf?'<th title="Net gamma exposure, all expirations ($ per 1% move)">GEX now</th><th title="Change since the app’s previous GEX check, usually 1–2 hours earlier (checks at about 10:05, 12:05, 2:05, 3:50 and after the close ET)">Since last check<br><span class="mute">(intraday)</span></th><th title="Change since the previous trading day’s close">Since yesterday’s close</th><th title="Change since about 1 week ago">Since last week</th><th title="Change since about 1 month ago">Since last month</th>':''}</tr>`+rows.map(e=>{const g=SG[e.ticker];
  const name=etf?`<b>${esc(e.sector)}</b><br><a href="?t=${e.ticker}" onclick="go('${e.ticker}');return false" class="mute">${e.ticker}</a> <span class="mute">${LP(e.ticker,e.price)}</span>`:`<a href="?t=${e.ticker}" onclick="go('${e.ticker}');return false"><b>${e.ticker}</b></a><br><span class="mute">${LP(e.ticker,e.price)}</span>`;
  return `<tr><td style="text-align:left">${name}</td>${etf?'':`<td>${esc(e.sector)}</td>`}<td>${chip(e.quad)}</td><td style="white-space:nowrap">${bestCell(e._b)}</td>${!etf?`<td>${e.sector_quad?chip(e.sector_quad):'–'}</td>`:''}<td class="${e.rs1<0?'put':'call'}">${e.rs1>=0?'+':''}${fmt(e.rs1,1)}%</td><td class="${e.rs3<0?'put':'call'}">${e.rs3>=0?'+':''}${fmt(e.rs3,1)}%</td><td>${'●'.repeat(e.trend)}${'○'.repeat(3-e.trend)}</td><td>${e.rsi==null?'–':fmt(e.rsi,0)}</td><td><div class="mbar"><i style="width:${Math.max(2,Math.min(100,e.mom))}%"></i></div>${fmt(e.mom,0)}</td><td>${gexSpark(((SC.gt||{})[e.ticker]||{}).days,80,20)}<br>${gex4Cells(((SC.gt||{})[e.ticker]||{}).w4||((r.items||{})[e.ticker]||{}).gex4,true)}</td>
   ${etf?`<td>${g?`<b class="${g.net<0?'put':'call'}">${money(g.net)}</b>`:'<span class="mute">–</span>'}</td><td>${gc(g&&g.prev,g)}</td><td>${gc(g&&g.d1,g,1)}</td><td>${gc(g&&g.w1,g,1)}</td><td>${gc(g&&g.m1,g,1)}</td>`:''}</tr>`}).join('')+'</table>';
 if(etf)h+=`<p class="mute">Strength = return minus the broad market’s over 1 or 3 months. GEX: positive = dealers dampen moves, negative = they amplify them. <b class="call">▲ Up</b> = GEX rose (more dampening, or less amplifying if still negative); <b class="put">▼ Down</b> = GEX fell (less dampening, or more amplifying). “Since last check” compares with the app’s previous snapshot earlier in the session (1–2 h); “since yesterday’s close” compares with the previous trading day’s close. ${anyHist?'':'GEX history is just starting: “vs yesterday” fills in after the next trading day, “vs last week” after 7 days and “vs last month” after 4 weeks (CBOE does not publish past open interest, so it can’t be back-filled).'} ${SC.sg&&SC.sg.last_run?'Last GEX snapshot '+esc(SC.sg.last_run)+'.':''}</p>`;
 el.innerHTML=h}

</script></body></html>"""


class Server(ThreadingHTTPServer):
    """Refuse to share the port: on Windows SO_REUSEADDR would let two copies bind at once."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE.replace("__VER__", APP_VERSION).encode(), "text/html; charset=utf-8")
        if u.path == "/lwc.js":
            b = lwc_js()
            if b is None:
                return self._send(404, b"// chart library missing or modified", "application/javascript")
            return self._send(200, b, "application/javascript; charset=utf-8")
        if u.path == "/api/watch":
            cfg_ = load_config()
            return self._json({"config": cfg_, "status": apply_floors(_read_json(STATE_PATH, {}).get("_status", {})),
                               "gex_trends": gex_trend([w["ticker"] for w in cfg_.get("watchlist", [])]),
                               "weeks": [{"k": w["k"], "label": w["label"], "lo": str(w["lo"]), "hi": str(w["hi"])} for w in exp_windows()],
                               "monitor": _monitor, "market_open": market_open_now()})
        if u.path == "/api/scan":
            res_ = apply_floors(_read_json(SCAN_PATH, None))
            tks_ = list(dict.fromkeys([x["ticker"] for k_ in ("rotation", "stock_rotation") for x in ((res_ or {}).get(k_) or [])]
                                      + list(((res_ or {}).get("items") or {}).keys())))
            return self._json({"status": _scan, "results": res_, "sector_gex": sector_gex_changes(), "gex_trends": gex_trend(tks_)})
        if u.path == "/api/trades":
            return self._json({"trades": trades_report_fast(), "results": trade_results(), "asof": _tr_cache["asof"] or "valuing…",
                               "valuing": _tr_cache["busy"]})
        if u.path == "/api/csp":
            return self._json({"status": _csp, "results": apply_floors(_read_json(CSP_PATH, None))})
        if u.path == "/api/health":
            return self._json(health_data())
        if u.path == "/api/calcs":
            items = load_calcs()
            live = calcs_live(items) if parse_qs(u.query).get("live", ["0"])[0] == "1" else {}
            return self._json({"items": items, "live": live, "asof": et_now().strftime("%H:%M:%S ET")})
        if u.path == "/api/flow":
            try:
                return self._json(flow_ticker(parse_qs(u.query).get("t", [""])[0]))
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/calls":
            return self._json({"status": _calls, "results": _read_json(CALL_PATH, None)})
        if u.path == "/api/picks/trades":
            q = parse_qs(u.query)
            try:
                return self._json(picks_trades(q.get("tf", [""])[0], q.get("st", [""])[0]))
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/picks":
            return self._json(picks_data())
        if u.path == "/api/edge":
            return self._json(edge_data())
        if u.path == "/api/squeeze":
            return self._json(squeeze_data())
        if u.path == "/api/taxloss":
            return self._json(taxloss_data())
        if u.path == "/api/stacks":                      # where is the app spending its time? (this PC only)
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._json({"error": "local only"})
            return self._json(sample_stacks(min(float(parse_qs(u.query).get("secs", ["8"])[0]), 30)))
        if u.path == "/api/refresh_all":
            return self._json({"jobs": refresh_all_status(), "skipped": _rf["skipped"], "auto": auto_status(),
                               "cboe": {"down": cboe_down(), "since": _cboe_off["since"]}, "no_data": no_data_source(),
                               "nasdaq": {**nasdaq_blocked(), "list_src": _nq_scr.get("src")}})
        if u.path == "/api/capital":
            try:
                return self._json(capital_data(parse_qs(u.query).get("force", ["0"])[0] == "1"))
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/mining_earnings":
            q = parse_qs(u.query)
            try:
                dd = min(max(int(q.get("days", ["21"])[0]), 5), 45)
                return self._json(mining_earnings(dd, q.get("force", ["0"])[0] == "1"))
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/chain":
            q = parse_qs(u.query)
            try:
                return self._json(chain_view(q.get("t", [""])[0], q.get("exp", [None])[0]))
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/blocks":
            cfg = load_config()
            return self._json({"blocks": blocks_today(), "note": _blocks["note"], "last_poll": _blocks["last_poll"],
                               "tickers": block_tickers(cfg), "cfg": {k: cfg.get(k) for k in
                               ("blocks_on", "block_tickers", "block_min_usd", "block_min_shares", "block_alert_usd")}})
        if u.path == "/api/bigmoney":
            try:
                return self._json(big_money(parse_qs(u.query).get("t", [""])[0]))
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/insights":
            try:
                return self._json(insights(parse_qs(u.query).get("t", [""])[0]))
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/badges":
            tks_ = [clean_ticker(x) for x in re.split(r"[,\s]+", parse_qs(u.query).get("t", [""])[0]) if x][:120]
            return self._json(badges_for(tks_))
        if u.path == "/api/ext":
            tks_ = [clean_ticker(x) for x in re.split(r"[,\s]+", parse_qs(u.query).get("t", [""])[0]) if x][:60]
            with ThreadPoolExecutor(6) as ex:
                res_ = dict(zip(tks_, ex.map(lambda t: (lambda: ext_hours(t))() if t else {}, tks_)))
            return self._json(_clean(json.loads(json.dumps(res_, default=str))))
        if u.path == "/api/live":
            tks_ = list(dict.fromkeys(clean_ticker(x) for x in re.split(r"[,\s]+", parse_qs(u.query).get("t", [""])[0]) if x))[:200]
            sw_, need_ = {}, []
            for t_ in tks_:
                if t_ in INDEXES:
                    continue
                q_ = stream_quote(t_)
                hit_ = None if q_ else _nq_cache.get(t_)
                if q_:
                    sw_[t_] = q_
                elif hit_ and hit_[1] and time.time() - hit_[0] < 20:
                    sw_[t_] = hit_[1]
                else:
                    need_.append(t_)
            if need_:
                try:
                    sw_.update(schwab_live(need_))
                except Exception:
                    pass
            rest_ = [t for t in tks_ if t not in sw_]
            with ThreadPoolExecutor(12) as ex:
                qs_ = dict(zip(rest_, ex.map(_live_one, rest_)))
            qs_.update(sw_)
            return self._json(_clean(json.loads(json.dumps({"quotes": {k: v for k, v in qs_.items() if v}, "market_open": market_open_now(), "stream": bool(_stream.connected),
                                                            "asof": et_now().strftime("%H:%M:%S ET")}, default=str))))
        if u.path == "/api/schwab":
            return self._json(schwab_status())
        if u.path == "/api/chainraw":                     # for the Strategy Scanner: one chain, Schwab first, CBOE shape
            q_ = parse_qs(u.query)
            tk_ = clean_ticker(q_.get("t", [""])[0].lstrip("^"))
            try:
                if q_.get("src", [""])[0] == "schwab":
                    p_ = _raw_get(tk_, True)
                    if not p_ and _sw_ok(tk_):     # (Schwab stalls ~67 s on the chains it cannot serve)
                        p_ = schwab_chain(tk_)
                        if p_:
                            _raw_put(tk_, p_)
                else:
                    p_ = _raw_get(tk_) or _raw_put(tk_, _chain_json(tk_))
            except Exception as e:
                return self._json({"error": str(e)}, 502)
            if not p_:
                return self._json({"error": "Schwab not connected or no chain"}, 404)
            return self._json({k: v for k, v in p_.items() if k != "_lm"})
        if u.path == "/api/gexmap":
            h_ = _read_json(GEX_DAILY_PATH, {}) or {}
            out_ = {}
            for tk_, th_ in h_.items():
                if th_:
                    d_ = max(th_)
                    v_ = th_[d_]
                    out_[tk_] = {"net": v_[0], "w4": sum(x for x in (v_[2] or []) if isinstance(x, (int, float))) if len(v_) > 2 else None,
                                 "date": d_}
            return self._json({"gex": out_})
        if u.path == "/api/overnight":
            return self._json({k: v for k, v in (overnight_symbols() or {}).items() if k != "symbols"} |
                              {"n": len((overnight_symbols() or {}).get("symbols") or {})})
        if u.path == "/api/bigmoney/status":
            return self._json({"status": _h13f, "db": H13F_DB.exists()})
        if u.path == "/api/flow/market":
            try:
                return self._json(flow_market())
            except Exception as e:
                return self._json({"error": str(e)})
        if u.path == "/api/alerts":
            try:
                since = int(parse_qs(u.query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            return self._json(read_alerts(since))
        if u.path == "/api":
            q = parse_qs(u.query)
            try:
                band = min(max(float(q.get("band", ["0.15"])[0]), 0.05), 0.5)
                data = lookup(q.get("t", [""])[0], band)
                return self._json(data)
            except Exception as e:
                return self._json({"error": str(e)})
        self._send(404, b"not found", "text/plain")

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(_clean(obj), default=str, allow_nan=False).encode(), "application/json")

    def _allowed(self) -> bool:
        # custom header blocks cross-site requests from other web pages; only this PC / home network
        if self.headers.get("X-GexWalls") != "1":
            return False
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip in TAILNET     # TAILNET = your own Tailscale devices

    def do_POST(self):
        u = urlparse(self.path)
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self._json({"error": "request too large"}, 413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        try:
            if u.path == "/api/watch":
                return self._json({"config": save_config(body)})
            if u.path == "/api/trades":
                save_trades(body)
                return self._json({"trades": trades_report_fast(), "results": trade_results(), "asof": _tr_cache["asof"],
                                   "valuing": True})
            if u.path == "/api/calcs":
                return self._json({"items": save_calcs(body)})
            if u.path == "/api/usage":
                record_usage(body)
                return self._json({"ok": True})
            if u.path == "/api/csp":
                if not _csp["running"]:
                    _csp.update(running=True, progress="starting", error=None)
                    threading.Thread(target=_csp_bg, args=("watchlist" if body.get("scope") == "watchlist" else "all",),
                                     daemon=True).start()
                return self._json({"status": _csp})
            if u.path == "/api/bigmoney/build":
                threading.Thread(target=h13f_build, args=(True,), daemon=True).start()
                return self._json({"status": _h13f})
            if u.path == "/api/refresh_all":
                return self._json({"jobs": refresh_all_start()})
            if u.path == "/api/edge/run":
                if not _edge["running"]:
                    _edge.update(running=True, progress="starting", error=None)
                    threading.Thread(target=_edge_bg, daemon=True).start()
                return self._json({"status": _edge})
            if u.path == "/api/taxloss/run":
                if not _tlb["running"]:
                    _tlb.update(running=True, progress="starting", error=None, tried=time.time())
                    threading.Thread(target=_tlb_bg, daemon=True).start()
                return self._json({"status": {k: _tlb[k] for k in ("running", "progress", "error")}})
            if u.path == "/api/squeeze/run":
                if not _sqz["running"]:
                    _sqz.update(running=True, progress="starting", error=None)
                    threading.Thread(target=_sqz_bg, daemon=True).start()
                return self._json({"status": _sqz})
            if u.path == "/api/picks/refresh":
                if not _picks["running"]:
                    _picks.update(running=True, error=None)
                    threading.Thread(target=run_picks_refresh, daemon=True).start()
                return self._json({"status": _picks})
            if u.path == "/api/calls":
                if not _calls["running"]:
                    raw = str(body.get("tickers") or "")
                    custom = []
                    for t in re.split(r"[\s,;]+", raw.upper())[:CALL_UNIVERSE]:
                        if t and re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", t) and t not in custom:
                            custom.append(clean_ticker(t))
                    _calls.update(running=True, progress="starting", error=None)
                    threading.Thread(target=_calls_bg, args=(custom or None,), daemon=True).start()
                return self._json({"status": _calls})
            if u.path == "/api/scan":
                if not _scan["running"]:
                    _scan.update(running=True, progress="starting", error=None)
                    threading.Thread(target=_scan_bg, daemon=True).start()
                return self._json({"status": _scan})
            if u.path == "/api/check":
                res = check_watchlist()
                _monitor["last_run"] = et_now().strftime("%Y-%m-%d %H:%M ET")
                return self._json(res)
            if u.path.startswith("/api/schwab/"):
                if not ipaddress.ip_address(self.client_address[0]).is_loopback:
                    return self._json({"error": "Schwab setup only from this PC (not the phone)"}, 403)
                if u.path == "/api/schwab/keys":
                    return self._json(schwab_save_keys(body))
                if u.path == "/api/schwab/authurl":
                    return self._json({"url": schwab_auth_url()})
                if u.path == "/api/schwab/connect":
                    return self._json(schwab_connect(body.get("url", "")))
                if u.path == "/api/schwab/disconnect":
                    _write_json(SCHWAB_TOKEN, {})
                    return self._json(schwab_status())
            if u.path == "/api/quit" and ipaddress.ip_address(self.client_address[0]).is_loopback:
                self._json({"ok": True, "version": APP_VERSION})
                threading.Timer(0.3, lambda: os._exit(0)).start()  # lets a newer copy take the port
                return None
            if u.path == "/api/test":
                notify({"title": "GEX Walls test alert", "msg": "Alerts are working.", "level": "warn"}, load_config())
                return self._json({"ok": True})
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)

    def log_message(self, *a):  # quiet console
        pass


def sample_stacks(secs: float = 8.0) -> dict:
    """Sampling profiler: every 40 ms note what each thread is running; returns the busiest call sites."""
    import collections
    leaf, mine, n = collections.Counter(), collections.Counter(), 0
    me, end = threading.get_ident(), time.time() + secs
    while time.time() < end:
        for tid, fr in sys._current_frames().items():
            if tid == me:
                continue
            f, own = fr, None
            while f is not None:
                if own is None and f.f_code.co_filename.endswith("walls_app.py"):
                    own = f"{f.f_code.co_name}:{f.f_lineno}"
                f = f.f_back
            top = f"{os.path.basename(fr.f_code.co_filename)}:{fr.f_code.co_name}"
            if top.split(":")[1] in ("wait", "_wait_for_tstate_lock", "accept", "select", "sleep", "get", "recv_into", "readinto", "read", "acquire"):
                continue                                   # idle / waiting on the network
            leaf[top] += 1
            chain, f = [], fr
            while f is not None:
                if f.f_code.co_filename.endswith("walls_app.py"):
                    chain.append(f.f_code.co_name)
                f = f.f_back
            mine[" < ".join(chain[:4]) or top] += 1
        n += 1
        time.sleep(0.04)
    return {"samples": n, "threads": threading.active_count(), "busy_in_app": mine.most_common(25), "busy_leaf": leaf.most_common(15)}


# ---------------------------------------------------------------- one button: refresh everything
def refresh_all_status() -> dict:
    def prog(v):
        p = v.get("progress") or ""
        if v.get("running") and p.startswith("queued"):     # always name the job actually running now
            p = f"queued — waiting for the {_heavy_who[0]} to finish" if _heavy_who[0] else "queued"
        return p
    return {k: {"running": bool(v["running"]), "progress": prog(v), "error": v.get("error")}
            for k, v in (("scan", _scan), ("csp", _csp), ("calls", _calls), ("edge", _edge), ("sqz", _sqz),
                         ("tlb", _tlb), ("picks", _picks))}


_rf = {"skipped": [], "thread": None}
RF_FRESH = {"edge": (EDGE_PATH, 2 * 3600), "sqz": (SQZ_PATH, 6 * 3600), "tlb": (TLB_PATH, 6 * 3600)}   # built on daily data


def refresh_all_start() -> dict:
    """Refresh everything, fastest and most-used first, one job at a time: picks re-price, spreads scan, sell puts,
    calls, then the daily-data jobs (tax-loss, early moves, squeeze). Running them one by one is faster than all at
    once (they compete for one CPU core and for the same data limits). Daily-data jobs whose results are still fresh
    are skipped: their inputs (daily bars, FINRA short interest) have not changed."""
    if _rf["thread"] is not None and _rf["thread"].is_alive():
        return refresh_all_status()
    jobs = (("picks", _picks, run_picks_refresh, ()), ("scan", _scan, _scan_bg, ()), ("csp", _csp, _csp_bg, ("all",)),
            ("calls", _calls, _calls_bg, ()), ("tlb", _tlb, _tlb_bg, ()), ("edge", _edge, _edge_bg, ()), ("sqz", _sqz, _sqz_bg, ()))
    todo, _rf["skipped"] = [], []
    for name, st, fn, args in jobs:
        if st["running"]:
            continue
        if name in RF_FRESH and not st.get("error"):
            pth, age = RF_FRESH[name]
            try:
                if time.time() - pth.stat().st_mtime < age:
                    _rf["skipped"].append(name)
                    continue
            except OSError:
                pass
        st.update(running=True, error=None)
        if "progress" in st:
            st["progress"] = "queued"
        todo.append((st, fn, args))

    def run():
        for st, fn, args in todo:
            try:
                fn(*args)
            except Exception as e:
                st.update(error=str(e))
            finally:
                st["running"] = False
    _rf["thread"] = threading.Thread(target=run, daemon=True)
    _rf["thread"].start()
    return refresh_all_status()


def main() -> int:
    ap = argparse.ArgumentParser(description="On-demand GEX walls (weekly, ~30 DTE, ~60 DTE)")
    ap.add_argument("--cli", action="store_true", help="text mode instead of the browser page")
    ap.add_argument("--lan", action="store_true", help="listen on your home network so your phone can open it")
    ap.add_argument("--local", action="store_true", help="this PC only (overrides --lan and lan_default)")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-monitor", action="store_true", help="don't run downside alerts inside the page app")
    ap.add_argument("--watch", action="store_true", help="headless alert monitor (no page)")
    ap.add_argument("--check-once", action="store_true", help="run the alert check once and exit")
    ap.add_argument("--scan", action="store_true", help="run the put credit spread scanner and print the top 10")
    ap.add_argument("tickers", nargs="*", help="with --cli: print these and exit")
    a = ap.parse_args()

    if a.scan:
        res = run_scan()
        for mode, title in (("weekly", "WEEKLY"), ("monthly", "30-45 DTE")):
            print(f"\n{title} put credit spreads (min POP 65, earnings excluded) - scan {res['ts']}")
            for i, x in enumerate(top_spreads(res, mode, 65), 1):
                print(f"{i:>2}. {x['ticker']:<5} {x['exp']} sell {x['short']:g} / buy {x['long']:g}  credit {x['credit']:.2f} "
                      f"({x['nat']:.2f})  max loss ${x['maxloss']:,.0f}  POP {x['pop']:.1f}%  BE {x['be']:.2f}  "
                      f"{x['quad']}  1M vs SPY {x['rs1']:+.1f}%")
        return 0
    if a.check_once:
        res = check_watchlist()
        for tk, st in res["status"].items():
            if "error" in st:
                print(f"{tk}: {st['error']}")
                continue
            print(f"{tk} {st['spot']:,.2f}  " + "  ".join(
                f"{e['exp'][5:]} PW {e['put_wall']} ({e['dist_pct']:+.1f}%) GEX {money(e['net_gex'])}"
                for e in st["exps"] if e["put_wall"]))
        for x in res["alerts"]:
            print(f"! {x['title']}: {x['msg']}")
        return 0
    if a.watch:
        print("Downside alert monitor running. Edit the watchlist in the page app or alerts_config.json. Ctrl+C to stop.")
        try:
            monitor_loop(verbose=True)
        except KeyboardInterrupt:
            pass
        return 0

    if a.cli:
        if a.tickers:
            for t in a.tickers:
                try:
                    print_text(lookup(t))
                except Exception as e:
                    print(f"{t}: {e}")
            return 0
        cli()
        return 0

    # 2026-11-18a: double-clicking walls_app.py used to start it on 127.0.0.1 only, so the phone could not reach it
    # (found 11-16 and 11-17). Phone access is now the default; --local or "lan_default": false keeps it on this PC.
    try:
        lan_default = bool(load_config().get("lan_default", True))
    except Exception:
        lan_default = True
    a.lan = not a.local and (a.lan or lan_default)
    host = "0.0.0.0" if a.lan else "127.0.0.1"  # nosec B104 - home-network access for Joe's phone (accepted)
    try:
        srv = Server((host, a.port), Handler)
    except OSError:
        # an older copy is still running on this port: ask it to exit, then take over
        try:
            requests.post(f"http://127.0.0.1:{a.port}/api/quit", headers={"X-GexWalls": "1"}, json={}, timeout=5)
        except requests.RequestException:
            pass
        time.sleep(1.5)
        try:
            srv = Server((host, a.port), Handler)
            print("Closed an older copy that was still running.")
        except OSError:
            print(f"Port {a.port} is in use by an older GEX Walls window. Close that window and run again.")
            return 1
    url = f"http://127.0.0.1:{a.port}"
    print(f"GEX walls ({APP_VERSION}) running at {url}")
    if a.lan:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            print(f"On your phone (same Wi-Fi): http://{s.getsockname()[0]}:{a.port}")
            s.close()
        except OSError:
            pass
    threading.Thread(target=gex_recorder, daemon=True).start()     # 15-min GEX snapshots for change tracking
    threading.Thread(target=csp_daily_loop, daemon=True).start()   # daily put screener (~10:05 ET)
    threading.Thread(target=picks_loop, daemon=True).start()
    threading.Thread(target=strategy_loop, daemon=True).start()      # full strategy scan (weekdays) + universe (Sundays)
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    threading.Thread(target=edge_loop, daemon=True).start()         # early moves / swing / algos after each scan + close
    threading.Thread(target=squeeze_loop, daemon=True).start()      # short-squeeze monitor each evening
    threading.Thread(target=taxloss_loop, daemon=True).start()      # tax-loss bounce screen, in season only
    threading.Thread(target=_stream.run, daemon=True).start()        # Schwab real-time quote stream (push)
    threading.Thread(target=trades_live_loop, daemon=True).start()   # open trades re-valued as their quotes change
    threading.Thread(target=trades_loop, daemon=True).start()       # open-trade values in the background   # re-price spreads/puts + calls scans during market hours
    threading.Thread(target=sector_gex_loop, daemon=True).start()  # sector ETF GEX snapshots for rotation changes       # Strategy Scanner live re-price 9:50 / 12:45 / 15:15 ET
    threading.Thread(target=blocks_loop, daemon=True).start()
    threading.Thread(target=h13f_loop, daemon=True).start()
    threading.Thread(target=gex_daily_loop, daemon=True).start()   # daily GEX rows (trend sparklines) for every list        # SEC 13F index (checks daily for a new quarter)      # live block-trade tape + nightly dark-pool alerts
    if not a.no_monitor:
        threading.Thread(target=monitor_loop, daemon=True).start()
        print("Downside alerts: on (watchlist in the page).")
    print("Ctrl+C to stop.")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
