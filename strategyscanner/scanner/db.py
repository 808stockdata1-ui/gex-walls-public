"""SQLite pick log: every logged pick keeps the exact features it was chosen with, so outcomes
can later be matched back to signals."""
import datetime as dt
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_date TEXT, data_asof TEXT,
  n_tickers INTEGER, n_candidates INTEGER, n_passed INTEGER, n_logged INTEGER,
  n_marked INTEGER, n_resolved INTEGER, notes TEXT);
CREATE TABLE IF NOT EXISTS ticker_daily (
  date TEXT, ticker TEXT, price REAL, rsi14 REAL, sma20 REAL, sma50 REAL, sma200 REAL,
  rv20 REAL, rv60 REAL, iv30 REAL, iv_rv REAL, iv_rank REAL, net_gex REAL, gamma_flip REAL,
  call_wall REAL, put_wall REAL, short_vol_ratio REAL, earnings_date TEXT, n8k_7d INTEGER,
  nform4_30d INTEGER, n13dg_90d INTEGER, ret5 REAL, ret20 REAL, avg_vol20 REAL,
  PRIMARY KEY (date, ticker));
CREATE TABLE IF NOT EXISTS picks (
  pick_id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, run_id INTEGER, date TEXT,
  timeframe TEXT, strategy TEXT, ticker TEXT, expiry TEXT, dte INTEGER, legs TEXT,
  price REAL, credit REAL, capital REAL, roc REAL, pop_iv REAL, pop_rv REAL, pop_cons REAL,
  pop_cal REAL, p_max REAL, be_lo REAL, be_hi REAL, features TEXT, rank INTEGER,
  times_seen INTEGER DEFAULT 1, last_seen TEXT,
  status TEXT DEFAULT 'open', hit50 INTEGER DEFAULT 0, hit50_date TEXT, days_to_50 INTEGER,
  last_mark REAL, last_mark_date TEXT, max_profit_pct REAL, min_profit_pct REAL,
  settle_price REAL, pnl REAL, pnl_pct REAL, win INTEGER, resolved_date TEXT);
CREATE INDEX IF NOT EXISTS ix_picks_status ON picks(status);
CREATE TABLE IF NOT EXISTS models (
  model_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, target TEXT, family TEXT, n INTEGER,
  n_pos INTEGER, feature_names TEXT, coefs TEXT, means TEXT, stds TEXT,
  brier_base REAL, brier_model REAL, weight REAL);
"""


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def pick_key(c):
    legs = ";".join(f'{l["side"][0]}{l["cp"]}{l["K"]}' for l in c["legs"])
    return f'{c["strategy"]}|{c["ticker"]}|{c["expiry"].isoformat()}|{legs}'


FEATURE_KEYS = ["rsi14", "above_sma50", "above_sma200", "iv_rv", "iv_rank", "iv30", "rv20",
                "spot_vs_flip", "net_gex_sign", "short_vs_putwall", "short_vs_callwall",
                "short_vol_ratio", "n8k_7d", "nform4_30d", "n13dg_90d", "ret5", "ret20",
                "dist_hi52", "vix", "spy_above_sma50", "earn_in_window", "short_delta",
                "sigma_dist", "dte", "roc"]


def insert_pick(con, run_id, run_date, c):
    key = pick_key(c)
    row = con.execute("SELECT pick_id FROM picks WHERE key=?", (key,)).fetchone()
    if row:
        con.execute("UPDATE picks SET times_seen=times_seen+1, last_seen=? WHERE pick_id=?",
                    (run_date, row["pick_id"]))
        return False
    legs = [{k: l[k] for k in ("cp", "K", "side", "bid", "ask", "iv", "delta", "oi", "code")} for l in c["legs"]]
    feats = {k: c.get(k) for k in FEATURE_KEYS}
    con.execute(
        """INSERT INTO picks (key, run_id, date, timeframe, strategy, ticker, expiry, dte, legs, price,
           credit, capital, roc, pop_iv, pop_rv, pop_cons, pop_cal, p_max, be_lo, be_hi, features, rank,
           last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, run_id, run_date, c["timeframe"], c["strategy"], c["ticker"], c["expiry"].isoformat(),
         c["dte"], json.dumps(legs), c["S"], c["credit"], c["capital"], c["roc"], c["pop_iv"],
         c["pop_rv"], c["pop_cons"], c.get("pop_cal"), c.get("p_max"), c.get("be_lo"), c.get("be_hi"),
         json.dumps(feats), c.get("rank"), run_date))
    return True


def upsert_ticker_daily(con, date, t, f):
    cols = ["price", "rsi14", "sma20", "sma50", "sma200", "rv20", "rv60", "iv30", "iv_rv", "iv_rank",
            "net_gex", "gamma_flip", "call_wall", "put_wall", "short_vol_ratio", "earnings_date",
            "n8k_7d", "nform4_30d", "n13dg_90d", "ret5", "ret20", "avg_vol20"]
    vals = []
    for k in cols:
        v = f.get(k)
        if isinstance(v, dt.date):
            v = v.isoformat()
        vals.append(v)
    con.execute(
        f"INSERT OR REPLACE INTO ticker_daily (date, ticker, {', '.join(cols)}) VALUES "
        f"({', '.join(['?'] * (len(cols) + 2))})", [date, t] + vals)


def iv_history(con, before_date):
    """{ticker: [iv30, ...]} from this scanner's own stored daily snapshots (last ~year)."""
    out = {}
    for r in con.execute("SELECT ticker, iv30 FROM ticker_daily WHERE iv30 IS NOT NULL AND date < ? "
                         "AND date >= date(?, '-365 days')", (before_date, before_date)):
        out.setdefault(r["ticker"], []).append(r["iv30"])
    return out


def iv_rank_from_history(hist, iv_now, min_days=40):
    """IV rank needs ~2 months of daily runs before it is reported; None until then."""
    if not iv_now or len(hist) + 1 < min_days:
        return None
    vals = hist + [iv_now]
    lo, hi = min(vals), max(vals)
    return None if hi <= lo else (iv_now - lo) / (hi - lo)
