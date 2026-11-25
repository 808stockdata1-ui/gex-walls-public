"""Market-moving calendar for the coming week, from primary sources only.

Fetched here:  FOMC meetings (federalreserve.gov), BEA releases such as GDP and PCE (bea.gov),
               mega-cap earnings dates (Nasdaq's earnings calendar).
Fixed rules:   option expirations (monthly third Friday, quarterly "quad witching", VIX Wednesday, all
               moved to the prior business day on exchange holidays) and NYSE holidays / early closes
               as published on nyse.com.
Agent-checked: BLS releases (CPI, jobs, PPI, JOLTS, ECI), White House and Federal Register items, Fed
               speeches and Treasury refunding, written by the pre-market catalyst review to
               output/events_agent.json with a primary-source link on every line (bls.gov blocks scripts).
"""
import datetime as dt
import html
import json
import os
import re
from zoneinfo import ZoneInfo

from .util import ET, log

HST = ZoneInfo("Pacific/Honolulu")
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BEA_URL = "https://www.bea.gov/news/schedule"
NYSE_URL = "https://www.nyse.com/markets/hours-calendars"
CBOE_RULES_URL = "https://res-certification.cboe.com/resources/options/Cboe2026OPTIONSCalendar.pdf"
NASDAQ_EARN_API = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
NASDAQ_EARN_PAGE = "https://www.nasdaq.com/market-activity/earnings"

# NYSE holidays and 1:00 p.m. early closes, 2026-2027, as listed on nyse.com/markets/hours-calendars.
NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day", "2026-01-19": "Martin Luther King, Jr. Day", "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday", "2026-05-25": "Memorial Day", "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)", "2026-09-07": "Labor Day", "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day", "2027-01-18": "Martin Luther King, Jr. Day", "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday", "2027-05-31": "Memorial Day", "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)", "2027-09-06": "Labor Day", "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
HIGH_BEA = ("gdp", "personal income and outlays")


def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def prev_trading_day(d):
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def third_friday(y, m):
    d = dt.date(y, m, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def monthly_expiration(y, m):
    f = third_friday(y, m)
    return f if is_trading_day(f) else prev_trading_day(f)


def vix_expiration(y, m):
    """Wednesday 30 days before the next month's standard third Friday; a holiday on that Friday or
    Wednesday moves it to the business day before the Wednesday."""
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    f = third_friday(ny, nm)
    w = f - dt.timedelta(days=30)
    if not is_trading_day(f) or not is_trading_day(w):
        w = prev_trading_day(w)
    return w


def _ev(day, title, category, impact, source, time_et=""):
    return {"date": day.isoformat(), "time_et": time_et, "title": title, "category": category,
            "impact": impact, "source": source}


def expiration_events(start, end):
    out = []
    for y in range(start.year, end.year + 1):
        for m in range(1, 13):
            exp = monthly_expiration(y, m)
            if start <= exp <= end:
                if m in (3, 6, 9, 12):
                    out.append(_ev(exp, "Quarterly options expiration (quad witching): stock and index options and "
                                        "futures expire; S&P index rebalances take effect after the close",
                                   "opex", "high", CBOE_RULES_URL, "4:00 PM"))
                else:
                    out.append(_ev(exp, "Monthly options expiration (third Friday): pinning and gamma unwind "
                                        "around big open-interest strikes", "opex", "high", CBOE_RULES_URL, "4:00 PM"))
            v = vix_expiration(y, m)
            if start <= v <= end:
                out.append(_ev(v, "VIX options and futures expiration (settles at the open)", "opex", "medium",
                               CBOE_RULES_URL, "9:30 AM"))
    return out


def holiday_events(start, end):
    out = []
    d = start
    while d <= end:
        k = d.isoformat()
        if k in NYSE_HOLIDAYS:
            out.append(_ev(d, f"Market closed: {NYSE_HOLIDAYS[k]}", "market", "high", NYSE_URL))
        if k in NYSE_EARLY_CLOSE:
            out.append(_ev(d, "Early close 1:00 PM ET (options 1:15 PM)", "market", "high", NYSE_URL, "1:00 PM"))
        d += dt.timedelta(days=1)
    return out


def _text(s):
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def fomc_events(fx, start, end):
    page = fx._get(FED_URL, as_json=False, retries=2)
    if not page:
        return None
    out = []
    for block in re.split(r'<div class="panel panel-default">', page):
        ym = re.search(r"(\d{4}) FOMC Meetings", block)
        if not ym:
            continue
        year = int(ym.group(1))
        months = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>', block)
        dates = re.findall(r'fomc-meeting__date[^>]*>([^<]+)<', block)
        for mon, days in zip(months, dates):
            raw = _text(days)
            if re.search(r"unscheduled|notation|cancel", raw, re.I):
                continue
            mm = [MONTHS.get(x.strip().lower()) for x in mon.split("/")]
            nums = [int(x) for x in re.findall(r"\d+", raw)]
            if not nums or not all(mm):
                continue
            d1, d2 = nums[0], nums[-1]
            m1 = mm[0]
            m2 = mm[-1] if (len(mm) > 1 and d2 < d1) else mm[0]
            try:
                first, decision = dt.date(year, m1, d1), dt.date(year, m2, d2)
            except ValueError:
                continue
            sep = "*" in raw
            if start <= decision <= end:
                out.append(_ev(decision, "FOMC rate decision 2:00 PM ET, Chair's press conference 2:30 PM"
                               + (" (with economic projections / dot plot)" if sep else ""),
                               "fed", "high", FED_URL, "2:00 PM"))
            if start <= first <= end and first != decision:
                out.append(_ev(first, "FOMC meeting begins (decision tomorrow)", "fed", "medium", FED_URL))
        for rel in re.findall(r"\(Released ([A-Za-z]+ \d{1,2}, \d{4})\)", block):
            try:
                d = dt.datetime.strptime(rel, "%B %d, %Y").date()
            except ValueError:
                continue
            if start <= d <= end:
                out.append(_ev(d, "FOMC minutes released", "fed", "medium", FED_URL, "2:00 PM"))
    return out


def bea_events(fx, start, end):
    page = fx._get(BEA_URL, as_json=False, retries=2)
    if not page:
        return None
    ym = re.search(r"Year (\d{4})", page)
    year = int(ym.group(1)) if ym else start.year
    out, last_month = [], 0
    for row in re.findall(r"<tr[^>]*scheduled-releases-type[^>]*>(.*?)</tr>", page, re.S):
        md = re.search(r'release-date">([A-Za-z]+) (\d{1,2})<', row)
        tm = re.search(r'text-muted">([^<]+)<', row)
        title = re.search(r'release-title[^>]*>(.*?)</td>', row, re.S)
        if not md or not title:
            continue
        m = MONTHS.get(md.group(1).lower())
        if not m:
            continue
        if m < last_month:  # schedule rolled into the next year
            year += 1
        last_month = m
        d = dt.date(year, m, int(md.group(2)))
        if not (start <= d <= end):
            continue
        name = _text(title.group(1))
        high = any(k in name.lower() for k in HIGH_BEA)
        if not high and "trade in goods" not in name.lower():
            continue  # keep GDP, PCE/personal income and the trade balance
        out.append(_ev(d, f"BEA: {name}", "macro", "high" if high else "medium", BEA_URL,
                       _text(tm.group(1)) if tm else ""))
    return out


def _money(s):
    try:
        return float(str(s).replace("$", "").replace(",", ""))
    except ValueError:
        return 0.0


def earnings_events(fx, start, end, min_cap=250e9, per_day=5):
    hdr = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
           "Referer": "https://www.nasdaq.com/"}
    out, ok = [], False
    d = start
    while d <= end:
        if d.weekday() < 5:
            data = fx._get(NASDAQ_EARN_API.format(d=d.isoformat()), headers=hdr, retries=2)
            try:
                rows = data["data"]["rows"] or []
                ok = True
            except (TypeError, KeyError):
                rows = []
            big = sorted((r for r in rows if _money(r.get("marketCap")) >= min_cap),
                         key=lambda r: -_money(r.get("marketCap")))
            names = set()
            for r in big:
                name = (r.get("name") or "").strip()
                if name in names:
                    continue  # one line per company (GOOG / GOOGL)
                names.add(name)
                if len(names) > per_day:
                    break
                when = {"time-pre-market": "before the open", "time-after-hours": "after the close"}.get(r.get("time"), "")
                out.append(_ev(d, f"Earnings: {r.get('symbol')} ({name})" + (f", {when}" if when else ""), "earnings",
                               "high" if _money(r.get("marketCap")) >= 500e9 else "medium", NASDAQ_EARN_PAGE))
        d += dt.timedelta(days=1)
    return out if ok else None


def agent_events(path, start, end):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    out = []
    for e in raw.get("events") or []:
        try:
            d = dt.date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        if start <= d <= end and e.get("title") and e.get("source"):
            out.append(_ev(d, e["title"], e.get("category") or "other", e.get("impact") or "medium",
                           e["source"], e.get("time_et") or ""))
    return out


def _sort_time(t):
    try:
        return dt.datetime.strptime(t, "%I:%M %p").time()
    except ValueError:
        return dt.time(23, 59)


def hst_time(day, t):
    try:
        tt = dt.datetime.strptime(t, "%I:%M %p").time()
    except ValueError:
        return ""
    x = dt.datetime.combine(day, tt, tzinfo=ET).astimezone(HST)
    return x.strftime("%I:%M %p").lstrip("0")


def build_week(fx, output_dir, today=None, days=7):
    """Next `days` calendar days from today (ET). Writes output/events_week.json and returns it."""
    today = today or dt.datetime.now(ET).date()
    start, end = today, today + dt.timedelta(days=days)
    events, status = [], {}
    events += expiration_events(start, end)
    events += holiday_events(start, end)
    for name, fn in (("fomc", fomc_events), ("bea", bea_events), ("earnings", earnings_events)):
        try:
            got = fn(fx, start, end)
        except Exception as e:  # a layout change on one site must not break the scan
            log.warning("Events: %s failed: %r", name, e)
            got = None
        status[name] = "ok" if got is not None else "unavailable"
        events += got or []
    agent_path = os.path.join(output_dir, "events_agent.json")
    ag = agent_events(agent_path, start, end)
    status["agent"] = "ok" if os.path.exists(agent_path) else "missing"
    events += ag
    seen, uniq = set(), []
    for e in events:
        k = (e["date"], e["title"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    by_day = {}
    for e in uniq:
        e["time_hst"] = hst_time(dt.date.fromisoformat(e["date"]), e["time_et"]) if e["time_et"] else ""
        by_day.setdefault(e["date"], []).append(e)
    out_days = []
    d = start
    while d <= end:
        evs = sorted(by_day.get(d.isoformat(), []),
                     key=lambda e: ((2 if e["category"] == "earnings" else 0) + (0 if e["impact"] == "high" else 1),
                                    _sort_time(e["time_et"])))
        if evs or is_trading_day(d):
            out_days.append({"date": d.isoformat(), "dow": d.strftime("%a"), "trading": is_trading_day(d),
                             "events": evs})
        d += dt.timedelta(days=1)
    result = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "start": start.isoformat(),
              "end": end.isoformat(), "status": status, "days": out_days}
    with open(os.path.join(output_dir, "events_week.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1)
    log.info("Events: %d in the next %d days (%s)", len(uniq), days, status)
    return result


def load_week(output_dir):
    try:
        with open(os.path.join(output_dir, "events_week.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None
