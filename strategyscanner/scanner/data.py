"""Primary-source data fetchers: CBOE delayed option chains, Yahoo daily history,
Nasdaq earnings calendar, FINRA daily short volume, SEC EDGAR filings."""
import datetime as dt
import itertools
import json
import os
import re
import shutil
import threading
import time

import requests

from .util import ET, log, safe_float, today_et

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

CBOE_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{sym}.json"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d&includeAdjustedClose=true"
NASDAQ_EARN_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


class _Limiter:
    """Shared pacing for one data source: a minimum gap between requests, plus a pause for every
    thread when the source answers 429 (it says how long to wait in Retry-After). Each 429 also
    widens the gap by 12% for the rest of the run (up to 3x the configured gap), so a run settles
    just under the source's real limit instead of paying the Retry-After penalty over and over."""

    def __init__(self, interval):
        self.base = interval
        self.interval = interval
        self.lock = threading.Lock()
        self.last = 0.0
        self.block_until = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(self.last + self.interval, self.block_until)
            if t > now:
                time.sleep(t - now)
            self.last = time.monotonic()

    def block(self, seconds):
        with self.lock:
            now = time.monotonic()
            if self.block_until <= now:  # one widening per penalty window, not one per waiting thread
                self.interval = min(self.interval * 1.12, self.base * 3)
            self.block_until = max(self.block_until, now + seconds)

    @property
    def rate(self):
        return 1.0 / self.interval if self.interval else float("inf")


class Fetcher:
    def __init__(self, cache_dir: str, sec_user_agent: str, timeout: int = 45, cboe_rate: float = 2.0,
                 yahoo_rate: float = 4.0, sec_rate: float = 6.0, cboe_hosts=None):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.sec_ua = sec_user_agent
        self.timeout = timeout
        self._local = threading.local()
        self.lim = {"yahoo": _Limiter(1.0 / yahoo_rate), "sec": _Limiter(1.0 / sec_rate)}
        # Cboe serves the same delayed chains from more than one host, each with its own throttle:
        # requests alternate between them, each paced on its own.
        self.cboe_hosts = list(cboe_hosts or ["cdn-api.cboe.com"])
        self.cboe_keys = []
        for i, _h in enumerate(self.cboe_hosts):
            k = "cboe" if i == 0 else f"cboe{i + 1}"
            self.lim[k] = _Limiter(1.0 / cboe_rate)
            self.cboe_keys.append(k)
        self._rr = itertools.count()
        self._rr_lock = threading.Lock()
        self.n429 = {k: 0 for k in self.lim}
        # Start where the last run settled (3% faster, to probe), never faster than the configured rate.
        self._pace_path = os.path.join(cache_dir, "pace.json")
        try:
            with open(self._pace_path, "r", encoding="utf-8") as f:
                learned = json.load(f)
            for k, lim in self.lim.items():
                if isinstance(learned.get(k), (int, float)) and learned[k] > 0:
                    lim.interval = min(max(lim.base, learned[k] * 0.97), lim.base * 3)
        except (OSError, ValueError):
            pass

        # Per-day cache for data that does not change during the session (completed daily bars, EDGAR
        # counts): the pre-market full scan fills it, so the live rescan only has to fetch option chains.
        self.today = today_et()
        self.daily_dir = os.path.join(cache_dir, "daily", self.today.isoformat())
        os.makedirs(self.daily_dir, exist_ok=True)
        try:
            for name in os.listdir(os.path.join(cache_dir, "daily")):
                try:
                    if (self.today - dt.date.fromisoformat(name)).days > 3:
                        shutil.rmtree(os.path.join(cache_dir, "daily", name), ignore_errors=True)
                except ValueError:
                    pass
        except OSError:
            pass

    def _dpath(self, kind, key):
        return os.path.join(self.daily_dir, f"{kind}_{re.sub(r'[^A-Za-z0-9_-]', '_', str(key))}.json")

    def _dload(self, kind, key):
        try:
            with open(self._dpath(kind, key), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _dsave(self, kind, key, obj):
        try:
            tmp = self._dpath(kind, key) + f".{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f)
            os.replace(tmp, self._dpath(kind, key))
        except OSError:
            pass

    @property
    def cboe_rate_total(self):
        return sum(self.lim[k].rate for k in self.cboe_keys)

    def save_pace(self):
        """Remember each source's settled pace for the next run."""
        try:
            with open(self._pace_path, "w", encoding="utf-8") as f:
                json.dump({k: round(lim.interval, 4) for k, lim in self.lim.items()}, f)
        except OSError:
            pass

    # ------------------------------------------------------------------ http
    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = BROWSER_UA
            self._local.s = s
        return s

    def _get(self, url, headers=None, retries=3, as_json=True, limiter=None):
        last = None
        lim = self.lim.get(limiter) if limiter else None
        for i in range(retries):
            if lim:
                lim.wait()
            try:
                # Connect/read timeouts alone never fire on a server that trickles bytes, so the body is
                # streamed under a hard deadline for the whole request.
                r = self._session().get(url, headers=headers, timeout=(10, 20), stream=True)
                if r.status_code == 200:
                    start, buf = time.monotonic(), []
                    for chunk in r.iter_content(65536):
                        buf.append(chunk)
                        if time.monotonic() - start > self.timeout:
                            r.close()
                            raise TimeoutError(f"transfer over {self.timeout}s")
                    r._content = b"".join(buf)
                    return r.json() if as_json else r.text
                r.close()
                if r.status_code in (400, 403, 404):
                    return None
                last = f"HTTP {r.status_code}"
                if r.status_code == 429:  # rate limited: everyone waits as long as the server asks
                    try:
                        ra = float(r.headers.get("Retry-After") or 20)
                    except ValueError:
                        ra = 20.0
                    if lim:
                        self.n429[limiter] += 1
                        lim.block(min(ra, 120) + 1)
                    else:
                        time.sleep(min(ra, 60))
                    continue
            except Exception as e:  # network hiccup, bad JSON
                last = repr(e)
            time.sleep(2.0 * (i + 1))
        log.debug("GET failed %s: %s", url, last)
        return None

    def _throttle(self, name: str, min_interval: float = 0.0):
        self.lim[name].wait()

    # ------------------------------------------------------------------ CBOE
    def option_chain(self, sym: str):
        """Return dict(price, iv30, asof, options=[...]) from CBOE delayed quotes, or None.
        Only standard-deliverable contracts (root == symbol) are kept."""
        cboe_sym = sym.replace(".", "")
        if sym.startswith("^"):
            cboe_sym = "_" + sym[1:]
        with self._rr_lock:
            i = next(self._rr) % len(self.cboe_hosts)
        d = _local_schwab_chain(sym)          # real-time Schwab chain via GEX Walls when connected
        if d is None:
            url = CBOE_URL.replace("cdn-api.cboe.com", self.cboe_hosts[i]).format(sym=cboe_sym)
            d = self._get(url, retries=4, limiter=self.cboe_keys[i])
        if not d or "data" not in d:
            return None
        data = d["data"]
        root = cboe_sym.lstrip("_")
        opts = []
        for o in data.get("options", []):
            code = o.get("option", "")
            if len(code) < 16:
                continue
            r, tail = code[:-15], code[-15:]
            if r != root:
                continue  # adjusted / non-standard deliverable
            try:
                exp = dt.date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6]))
                cp = tail[6]
                strike = int(tail[7:]) / 1000.0
            except ValueError:
                continue
            opts.append({
                "code": code, "exp": exp, "cp": cp, "K": strike,
                "bid": safe_float(o.get("bid"), 0.0), "ask": safe_float(o.get("ask"), 0.0),
                "iv": safe_float(o.get("iv"), 0.0), "oi": safe_float(o.get("open_interest"), 0.0),
                "vol": safe_float(o.get("volume"), 0.0), "delta": safe_float(o.get("delta"), 0.0),
                "gamma": safe_float(o.get("gamma"), 0.0), "theta": safe_float(o.get("theta"), 0.0),
            })
        price = safe_float(data.get("current_price"))
        if not price or price <= 0:
            return None
        if _chain_stale(data.get("last_trade_time")):
            log.info("skip %s: CBOE has not refreshed this option chain (last trade %s)", sym, data.get("last_trade_time"))
            return None
        return {
            "symbol": sym, "price": price, "iv30": safe_float(data.get("iv30"), 0.0) / 100.0,
            "asof": d.get("timestamp"), "last_trade_time": data.get("last_trade_time"),
            "options": opts,
        }

    # ----------------------------------------------------------------- Yahoo
    def history(self, sym: str, rng: str = "2y", cache: bool = True):
        """Completed daily bars (sessions before today, ET): list of dicts(date, open, high, low, close,
        volume). Callers add the live price themselves. Cached for the day unless cache=False, which
        also keeps today's partial bar."""
        if cache:
            raw = self._dload("h", f"{sym}_{rng}")
            if raw is not None:
                for b in raw:
                    b["date"] = dt.date.fromisoformat(b["date"])
                return raw
        bars = self._history_fetch(sym, rng)
        if bars is None or not cache:
            return bars
        bars = [b for b in bars if b["date"] < self.today]
        self._dsave("h", f"{sym}_{rng}", [dict(b, date=b["date"].isoformat()) for b in bars])
        return bars

    def _history_fetch(self, sym, rng):
        ysym = sym.replace(".", "-")
        d = None
        for host in ("query1", "query2"):
            d = self._get(YAHOO_URL.replace("query1", host).format(sym=requests.utils.quote(ysym), rng=rng),
                          limiter="yahoo")
            if d:
                break
        try:
            res = d["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
        except (TypeError, KeyError, IndexError):
            return None
        bars = []
        for i, t in enumerate(ts):
            c = safe_float(q["close"][i])
            if c is None:
                continue
            day = dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(ET).date()
            bars.append({
                "date": day, "open": safe_float(q["open"][i]), "high": safe_float(q["high"][i]),
                "low": safe_float(q["low"][i]), "close": c, "volume": safe_float(q["volume"][i], 0.0),
            })
        return bars

    # ---------------------------------------------------------------- Nasdaq
    def earnings_calendar(self, start: dt.date, days: int = 75):
        """Map symbol -> next earnings date within [start, start+days]. Cached per day."""
        cache = os.path.join(self.cache_dir, f"earnings_{start.isoformat()}_{days}.json")
        if os.path.exists(cache):
            with open(cache, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {k: dt.date.fromisoformat(v) for k, v in raw.items()}
        out = {}
        hdr = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
               "Referer": "https://www.nasdaq.com/"}
        days_list = [start + dt.timedelta(days=i) for i in range(days + 1)
                     if (start + dt.timedelta(days=i)).weekday() < 5]

        def one(day):
            d = self._get(NASDAQ_EARN_URL.format(d=day.isoformat()), headers=hdr, retries=2)
            try:
                return day, (d["data"]["rows"] or [])
            except (TypeError, KeyError):
                return day, None

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = sorted(ex.map(one, days_list), key=lambda x: x[0])
        ok_days = 0
        for day, rows in results:
            if rows is None:
                continue
            ok_days += 1
            for row in rows:
                s = (row.get("symbol") or "").strip().upper()
                if s and s not in out:
                    out[s] = day
        if ok_days:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({k: v.isoformat() for k, v in out.items()}, f)
        log.info("Earnings calendar: %d symbols over %d trading days", len(out), ok_days)
        return out

    # ----------------------------------------------------------------- FINRA
    def short_volume(self, ref: dt.date, lookback: int = 7):
        """Most recent FINRA consolidated daily short-sale volume file: symbol -> short ratio."""
        for i in range(lookback):
            day = ref - dt.timedelta(days=i)
            if day.weekday() >= 5:
                continue
            txt = self._get(FINRA_URL.format(d=day.strftime("%Y%m%d")), as_json=False, retries=2)
            if not txt or "|" not in txt:
                continue
            out = {}
            for line in txt.splitlines()[1:]:
                p = line.split("|")
                if len(p) < 5:
                    continue
                sv, tv = safe_float(p[2], 0.0), safe_float(p[4], 0.0)
                if tv and tv > 0:
                    out[p[1].upper()] = sv / tv
            if out:
                log.info("FINRA short volume %s: %d symbols", day, len(out))
                return day, out
        return None, {}

    # ------------------------------------------------------------------- SEC
    def sec_cik_map(self):
        cache = os.path.join(self.cache_dir, "sec_company_tickers.json")
        fresh = os.path.exists(cache) and (time.time() - os.path.getmtime(cache) < 7 * 86400)
        if fresh:
            with open(cache, "r", encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = self._get(SEC_TICKERS_URL, headers={"User-Agent": self.sec_ua}, limiter="sec")
            if raw:
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(raw, f)
            elif os.path.exists(cache):
                with open(cache, "r", encoding="utf-8") as f:
                    raw = json.load(f)
        out = {}
        for v in (raw or {}).values():
            t = str(v.get("ticker", "")).upper()
            if t and t not in out:
                out[t] = int(v["cik_str"])
        return out

    def sec_filing_counts(self, cik: int, ref: dt.date):
        """Counts of recent 8-K, Form 4 and 13D/13G filings straight from EDGAR (cached for the day)."""
        if ref == self.today:
            hit = self._dload("sec", cik)
            if hit is not None:
                return hit
            out = self._sec_counts_fetch(cik, ref)
            if out is not None:
                self._dsave("sec", cik, out)
            return out
        return self._sec_counts_fetch(cik, ref)

    def _sec_counts_fetch(self, cik, ref):
        d = self._get(SEC_SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": self.sec_ua}, retries=2,
                      limiter="sec")
        if not d:
            return None
        try:
            rec = d["filings"]["recent"]
            forms, dates = rec["form"], rec["filingDate"]
        except (KeyError, TypeError):
            return None
        items = rec.get("items") or [""] * len(forms)
        n8k = n4 = n13 = 0
        last8k = last_results = None
        for f, ds, it in zip(forms, dates, items):
            try:
                age = (ref - dt.date.fromisoformat(ds)).days
            except ValueError:
                continue
            if age > 200:
                break
            fu = f.upper()
            if fu.startswith("8-K"):
                if age <= 7:
                    n8k += 1
                    last8k = last8k or ds
                if last_results is None and "2.02" in (it or ""):
                    last_results = ds  # Item 2.02 = Results of Operations (earnings release)
            elif fu in ("4", "4/A") and age <= 30:
                n4 += 1
            elif ("13D" in fu or "13G" in fu) and age <= 90:
                n13 += 1
        return {"n8k_7d": n8k, "nform4_30d": n4, "n13dg_90d": n13, "last_8k": last8k,
                "last_results_8k": last_results}


def _chain_stale(last_trade) -> bool:
    """CBOE rebuilds some symbols' chain files only a few times a day. While the market is open, skip a chain whose
    last trade is over 45 minutes old or from before today's open: its option quotes are just as old."""
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
        t = dt.datetime.fromisoformat(str(last_trade)[:19])
    except Exception:
        return False
    if now.weekday() >= 5 or not (dt.time(9, 30) <= now.time() < dt.time(16, 0)):
        return False
    return (now - t).total_seconds() > 45 * 60 or t < now.replace(hour=9, minute=30, second=0, microsecond=0)


def _local_schwab_chain(sym: str):
    """Real-time option chain from Schwab through the GEX Walls app on this PC (it holds the Schwab login), in CBOE's
    file shape; None when the app is not running or not connected to Schwab (then the scanner uses CBOE as before)."""
    try:
        import requests as _rq
        r = _rq.get("http://127.0.0.1:8050/api/chainraw", params={"t": sym.lstrip("^"), "src": "schwab"}, timeout=(1.5, 60))
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, dict) and j.get("_src") == "schwab" and (j.get("data") or {}).get("options"):
                return j
    except Exception:
        pass
    return None
