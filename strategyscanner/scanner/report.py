"""HTML report (phone-friendly, light/dark), JSON export, and optional Telegram summary."""
import datetime as dt
import html
import json
import urllib.parse
import urllib.request

from .strategies import STRATEGY_NAMES

TF_ORDER = ["daily", "weekly", "monthly", "quarterly", "leaps"]
TF_LABEL = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "quarterly": "2–8 months", "leaps": "LEAPS"}
TF_DTE = {"daily": "0–4 DTE", "weekly": "5–20 DTE", "monthly": "21–60 DTE", "quarterly": "61–239 DTE", "leaps": "240+ DTE"}
MINUS = "−"

e = html.escape


def _k(x):
    return f"{x:g}" if x is not None else "?"


def legs_text(c):
    parts = []
    for l in c["legs"]:
        sign = "+" if l["side"] == "buy" else MINUS
        if l["cp"] == "STK":
            parts.append(f"+100 sh")
        else:
            parts.append(f"{sign}{_k(l['K'])}{l['cp']}")
    if c["strategy"] == "IC":
        return f"{parts[0]}/{parts[1]} · {parts[2]}/{parts[3]}"
    return " / ".join(parts)


def pct(x, d=0):
    return "—" if x is None else f"{x * 100:.{d}f}%"


def money(x):
    return "—" if x is None else f"${x:,.0f}"


def num(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}"


def _rank_pop(c):
    return c.get("pop_cal") if c.get("pop_cal") is not None else c["pop_cons"]


def _return_cell(c):
    st = c["strategy"]
    if st == "LCALL":
        return f'{pct(c["roc"])}<span class="sub">if +1σ by expiry</span>'
    if st == "CC":
        return f'{pct(c.get("ret_called"), 1)}<span class="sub">if called · premium {pct(c["roc"], 1)}</span>'
    if st == "CSP":
        return f'{pct(c["roc"], 1)}<span class="sub">on cash secured</span>'
    return f'{pct(c["roc"])}<span class="sub">on max loss</span>'


def _flags(c, cat=None):
    out = []
    info = (cat or {}).get(c["ticker"])
    if info is not None:
        if info.get("risk"):
            out.append(f'<span class="chip warn" title="{e(info.get("note") or "")}">Catalyst risk</span>')
        else:
            out.append(f'<span class="chip good" title="{e(info.get("note") or "Reviewed: nothing material")}">News checked</span>')
    if c.get("earn_in_window"):
        out.append('<span class="chip warn" title="Earnings before expiry">Earnings</span>')
    if (c.get("n8k_7d") or 0) > 0:
        out.append('<span class="chip warn" title="8-K filed in the last 7 days">8-K</span>')
    ivrv = c.get("iv_rv")
    if ivrv and ivrv >= 1.2 and c["strategy"] != "LCALL":
        out.append(f'<span class="chip good" title="Implied vol above realized vol">IV/RV {ivrv:.2f}</span>')
    elif ivrv and ivrv < 0.9 and c["strategy"] != "LCALL":
        out.append(f'<span class="chip bad" title="Implied vol below realized vol: premium is thin">IV/RV {ivrv:.2f}</span>')
    svp = c.get("short_vs_putwall")
    if svp is not None and c["strategy"] in ("PCS", "CSP", "IC") and svp < 0:
        out.append('<span class="chip good" title="Short put sits below the put wall (dealer support above it)">Below put wall</span>')
    if c.get("spot_vs_flip") is not None and c["spot_vs_flip"] < 0:
        out.append('<span class="chip bad" title="Price is below the gamma flip: dealers amplify moves">Neg. gamma</span>')
    if c.get("p50") is not None:
        out.append(f'<span class="chip" title="Learned probability of reaching 50% profit">P(50%) {pct(c["p50"])}</span>')
    return "".join(out) or '<span class="muted">—</span>'


def _rsi_chip(r):
    if r is None:
        return "—"
    cls = "bad" if r >= 70 else ("good" if r <= 30 else "")
    return f'<span class="rsi {cls}">{r:.0f}</span>'


def _pop_cell(c):
    p = _rank_pop(c)
    w = max(0.0, min(1.0, p)) * 100
    lbl = "learned" if c.get("pop_cal") is not None else "conservative"
    return (f'<div class="pop"><span class="popnum">{pct(p, 1)}</span>'
            f'<span class="bar"><i style="width:{w:.1f}%"></i></span>'
            f'<span class="sub">{lbl} · IV {pct(c["pop_iv"])} · RV {pct(c["pop_rv"])}</span></div>')


def _row(i, c, cat=None):
    x = c["expiry"]
    exp = x.strftime("%b ") + str(x.day) + (" ’" + x.strftime("%y") if c["dte"] > 200 else "")
    cr = c["credit"]
    credit = f'{"Credit" if cr >= 0 else "Debit"} {abs(cr):.2f}'
    return f"""<tr>
<td class="n">{i}</td>
<td class="c-tk"><b class="tk">{e(c['ticker'])}</b><span class="sub">${num(c['S'])} · RSI {_rsi_chip(c.get('rsi14'))}</span></td>
<td class="c-st">{e(STRATEGY_NAMES[c['strategy']])}<span class="sub mono">{e(legs_text(c))}</span></td>
<td class="c-ex mono">{e(exp)}<span class="sub">{c['dte']} DTE</span></td>
<td class="c-pop">{_pop_cell(c)}</td>
<td class="num c-cap" data-label="Capital">{money(c['capital'])}<span class="sub">{credit}</span></td>
<td class="num c-ret" data-label="Return">{_return_cell(c)}</td>
<td class="flags c-fl">{_flags(c, cat)}</td>
</tr>"""


def _table(cands, cat=None):
    rows = "\n".join(_row(i + 1, c, cat) for i, c in enumerate(cands))
    return f"""<div class="scroll"><table class="picks">
<thead><tr><th>#</th><th>Ticker</th><th>Strategy · legs</th><th>Expiry</th><th>POP</th>
<th class="num">Capital</th><th class="num">Return</th><th>Signals</th></tr></thead>
<tbody>{rows}</tbody></table></div>"""


def _section(tf, rep, cat=None):
    head = f'<h2>{TF_LABEL[tf]} <span class="dte">{TF_DTE[tf]}</span></h2>'
    if not rep or not rep["top"]:
        return (f'<section id="{tf}" class="tf">{head}<p class="empty">No trade passed every filter for this '
                f'timeframe today. Filters are fixed on purpose; see Method for the thresholds.</p></section>')
    by = rep["by_strategy"]
    best = []
    for st in ["PCS", "CCS", "IC", "CSP", "CC", "LCALL"]:
        lst = by.get(st)
        if lst:
            c = lst[0]
            best.append(f'<li><span>{e(STRATEGY_NAMES[st])}</span><b class="mono">{e(c["ticker"])} '
                        f'{e(legs_text(c))}</b><em>{pct(_rank_pop(c), 1)} · {money(c["capital"])} · '
                        f'{pct(c.get("ret_called") if c["strategy"] == "CC" else c["roc"], 1)}</em></li>')
    return f"""<section id="{tf}" class="tf">{head}
{_table(rep['top'], cat)}
<details><summary>Best of each strategy</summary><ul class="beststrat">{''.join(best)}</ul></details>
</section>"""


CAT_LABEL = {"fed": "Fed", "macro": "Data", "opex": "Expiration", "earnings": "Earnings", "market": "Market",
             "white_house": "White House", "treasury": "Treasury", "bls": "Data", "other": "Event"}


def _week_section(events):
    if not events or not events.get("days"):
        return ""
    try:
        s = dt.date.fromisoformat(events["start"])
        en = dt.date.fromisoformat(events["end"])
        span = f'{s.strftime("%b")} {s.day} – {en.strftime("%b")} {en.day}'
    except (KeyError, ValueError):
        span = ""
    cards = []
    for d in events["days"]:
        day = dt.date.fromisoformat(d["date"])
        items = []
        for ev in d["events"]:
            t = ""
            if ev.get("time_et"):
                t = f'{e(ev["time_et"])} ET' + (f' · {e(ev["time_hst"])} HST' if ev.get("time_hst") else "")
            items.append(
                f'<li class="ev {"hi" if ev.get("impact") == "high" else ""}">'
                f'<span class="evmeta"><span class="chip {"warn" if ev.get("impact") == "high" else ""}">'
                f'{e(CAT_LABEL.get(ev.get("category"), "Event"))}</span>{f"<span class=evt>{t}</span>" if t else ""}</span>'
                f'<span class="evtitle">{e(ev["title"])} <a href="{e(ev["source"])}" target="_blank" rel="noopener">source</a></span></li>')
        body = "".join(items) if items else '<li class="ev none">Nothing scheduled</li>'
        closed = "" if d.get("trading", True) else ' <span class="muted">· market closed</span>'
        cards.append(f'<div class="day"><h3>{day.strftime("%a %b")} {day.day}{closed}</h3><ul>{body}</ul></div>')
    miss = [k for k, v in (events.get("status") or {}).items() if v != "ok"]
    note = ""
    if miss:
        names = {"fomc": "Fed calendar", "bea": "BEA schedule", "earnings": "earnings calendar",
                 "agent": "BLS / White House review"}
        note = ('<p class="model">Not available this run: ' + ", ".join(e(names.get(m, m)) for m in miss) + ".</p>")
    return (f'<section id="week"><h2>Week ahead <span class="dte">{e(span)}</span></h2>'
            f'<div class="week">{"".join(cards)}</div>{note}'
            '<p class="model">Fed, BEA, NYSE and earnings dates are read from the source each morning; option '
            'expirations follow Cboe/OCC rules; BLS releases and White House items come from the pre-market review, '
            'each linked to its official page.</p></section>')


def _catalyst_section(catalysts):
    if not catalysts or not catalysts.get("tickers"):
        return ('<section id="catalysts"><h2>Catalyst review</h2><p class="empty">Not reviewed yet for this run. '
                'Each trading morning the review checks SEC filings and company press releases for every ticker '
                'in the top picks and flags events that fall before expiry.</p></section>')
    rows = []
    items = sorted(catalysts["tickers"].items(), key=lambda kv: (-int(bool(kv[1].get("risk"))), kv[0]))
    for t, info in items:
        flag = '<span class="chip warn">Risk</span>' if info.get("risk") else '<span class="chip good">Clear</span>'
        src = str(info.get("source") or "")
        link = f'<a href="{e(src)}" target="_blank" rel="noopener">source</a>' if src.startswith("http") else "—"
        rows.append(f'<tr><td><b class="tk">{e(t)}</b></td><td>{flag}</td><td class="note">{e(info.get("note") or "")}</td>'
                    f'<td>{link}</td></tr>')
    when = e(str(catalysts.get("reviewed_at") or catalysts.get("date") or ""))
    return (f'<section id="catalysts"><h2>Catalyst review <span class="dte">{when}</span></h2>'
            f'<div class="scroll"><table><thead><tr><th>Ticker</th><th>Flag</th><th>What was found</th><th>Source</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')


def _learning(stats, meta):
    if stats["n_closed"] == 0:
        body = (f'<p>The pick log holds <b>{stats["n_total"]}</b> picks ({stats["n_open"]} open). None has expired yet, '
                f'so there is nothing to grade. Daily picks start resolving within a week, weekly picks in 1–2 weeks, '
                f'monthly picks in 3–8 weeks. Ranking uses the conservative POP until at least 50 picks have resolved.</p>')
    else:
        rows = "".join(
            f'<tr><td>{TF_LABEL.get(g["timeframe"], g["timeframe"])}</td><td>{e(STRATEGY_NAMES.get(g["strategy"], g["strategy"]))}</td>'
            f'<td class="num">{g["n"]}</td><td class="num">{pct(g["avg_pop"], 1)}</td><td class="num">{pct(g["win_rate"], 1)}</td>'
            f'<td class="num">{pct(g["hit50_rate"], 1)}</td><td class="num">{pct(g["managed_win_rate"], 1)}</td>'
            f'<td class="num">{pct(g["avg_pnl_pct"], 1)}</td></tr>' for g in stats["by_group"])
        body = (f'<p><b>{stats["n_closed"]}</b> resolved · <b>{stats["n_open"]}</b> open.</p>'
                f'<div class="scroll"><table><thead><tr><th>Timeframe</th><th>Strategy</th><th class="num">N</th>'
                f'<th class="num">Predicted POP</th><th class="num">Won at expiry</th><th class="num">Hit 50%</th>'
                f'<th class="num">Won if closed at 50%</th><th class="num">Avg return</th></tr></thead><tbody>{rows}</tbody></table></div>')
        if stats["calibration"]:
            cal = "".join(f'<tr><td>{e(b["bucket"])}</td><td class="num">{b["n"]}</td><td class="num">{pct(b["predicted"], 1)}</td>'
                          f'<td class="num">{pct(b["realized"], 1)}</td></tr>' for b in stats["calibration"])
            body += (f'<h3>Is POP honest?</h3><div class="scroll"><table><thead><tr><th>POP bucket</th><th class="num">N</th>'
                     f'<th class="num">Predicted</th><th class="num">Realized</th></tr></thead><tbody>{cal}</tbody></table></div>')
        if stats["signal_splits"]:
            sp = "".join(f'<tr><td>{e(s["signal"])}</td><td class="num">{pct(s["win_yes"], 1)} <span class="muted">n={s["n_yes"]}</span></td>'
                         f'<td class="num">{pct(s["win_no"], 1)} <span class="muted">n={s["n_no"]}</span></td></tr>'
                         for s in stats["signal_splits"])
            body += (f'<h3>Which signals mattered</h3><div class="scroll"><table><thead><tr><th>Signal</th><th class="num">Win rate when true</th>'
                     f'<th class="num">When false</th></tr></thead><tbody>{sp}</tbody></table></div>')
        for m in stats["models"]:
            coefs = sorted(((k, v) for k, v in m["coefs"].items() if k != "intercept"), key=lambda kv: -abs(kv[1]))[:6]
            cl = ", ".join(f'{e(k)} {v:+.2f}' for k, v in coefs)
            state = f'in use (weight {m["weight"]:.2f})' if m["weight"] > 0 else "not used: did not beat raw POP on recent picks"
            body += (f'<p class="model"><b>{e(m["family"])} · {e(m["target"])}</b> model, n={m["n"]}, {state}. '
                     f'Holdout Brier {m["brier_model"]:.3f} vs raw POP {m["brier_base"]:.3f}. Strongest weights: {cl}</p>')
    return f'<section id="learning"><h2>Learning loop</h2>{body}</section>'


def pick_tickers(lists):
    return sorted({c["ticker"] for rep in lists.values() for c in rep["top"]})


def _tickers(lists, feats):
    syms = pick_tickers(lists)
    rows = []
    for s in syms:
        f = feats.get(s) or {}
        ed = f.get("earnings_date")
        rows.append(
            f'<tr><td><b class="tk">{e(s)}</b></td><td class="num">${num(f.get("price"))}</td><td class="num">{_rsi_chip(f.get("rsi14"))}</td>'
            f'<td class="num">{pct(f.get("iv30"))}</td><td class="num">{pct(f.get("rv20"))}</td>'
            f'<td class="num">{num(f.get("put_wall"), 1)}</td><td class="num">{num(f.get("call_wall"), 1)}</td>'
            f'<td class="num">{num(f.get("gamma_flip"), 1)}</td><td class="num">{pct(f.get("short_vol_ratio"))}</td>'
            f'<td>{e(ed.isoformat()) if ed else "—"}</td></tr>')
    return (f'<section id="tickers"><h2>Tickers in today’s picks</h2><div class="scroll"><table><thead><tr><th>Ticker</th>'
            f'<th class="num">Price</th><th class="num">RSI(14)</th><th class="num">IV30</th><th class="num">RV20</th>'
            f'<th class="num">Put wall</th><th class="num">Call wall</th><th class="num">Gamma flip</th>'
            f'<th class="num">Short vol</th><th>Next earnings</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')


CSS = """
/* Layout: one column, sticky timeframe switcher, wide tables scroll inside their own frame */
:root{--bg:#f6f8f7;--panel:#ffffff;--ink:#14211f;--muted:#5d6b68;--line:#dbe3e1;--accent:#0b6b62;
--good:#26733a;--warn:#9a5b00;--bad:#b0302a;--chipbg:#eef3f2;
--display:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif;--body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
--mono:"IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0f1716;--panel:#16201f;--ink:#e3ebe9;--muted:#93a39f;
--line:#26332f;--accent:#4fc1b4;--good:#6cc884;--warn:#e0a94a;--bad:#f07a72;--chipbg:#1d2a28;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0f1716;--panel:#16201f;--ink:#e3ebe9;--muted:#93a39f;--line:#26332f;--accent:#4fc1b4;
--good:#6cc884;--warn:#e0a94a;--bad:#f07a72;--chipbg:#1d2a28;color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--body);font-size:15px;line-height:1.45;
padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:20px 48px}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:28px}
header{display:flex;flex-direction:column;gap:10px}
h1{font-family:var(--display);font-weight:600;font-size:clamp(26px,5vw,36px);letter-spacing:-.01em;margin:0;text-wrap:balance}
h2{font-family:var(--display);font-weight:600;font-size:22px;margin:0 0 10px;display:flex;align-items:baseline;gap:10px;text-wrap:balance}
h3{font-family:var(--display);font-weight:600;font-size:17px;margin:18px 0 8px}
.dte{font-family:var(--mono);font-size:12px;color:var(--muted);font-weight:400;letter-spacing:.04em}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--muted);font-size:13px}
.meta b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.order{font-size:13px;color:var(--muted)}
nav{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);padding-block:8px;display:flex;gap:6px;flex-wrap:wrap;border-bottom:1px solid var(--line)}
body::before{content:"";position:fixed;inset:0 0 auto;height:env(safe-area-inset-top,0px);z-index:10;background:var(--bg)}
nav a{font-family:var(--display);font-size:14px;color:var(--ink);text-decoration:none;padding:6px 12px;border:1px solid var(--line);border-radius:999px;background:var(--panel)}
a{color:var(--accent)}
.note{min-width:220px;white-space:normal}
nav a:hover,nav a:focus-visible{border-color:var(--accent);color:var(--accent);outline:none}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13.5px;font-variant-numeric:tabular-nums}
th{text-align:left;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
td.n{color:var(--muted);font-family:var(--mono);width:1%}
.num{text-align:right;white-space:nowrap}
.mono{font-family:var(--mono);font-size:12.5px}
.tk{font-family:var(--mono);font-size:14px;letter-spacing:.02em}
.sub{display:block;color:var(--muted);font-size:12px;margin-top:2px;white-space:nowrap}
.pop{display:flex;flex-direction:column;gap:3px;min-width:150px}
.popnum{font-weight:600;font-size:15px}
.bar{display:block;height:5px;background:var(--chipbg);border-radius:3px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}
.flags{min-width:150px}
.chip{display:inline-block;font-size:11px;padding:2px 7px;margin:0 4px 4px 0;border-radius:4px;background:var(--chipbg);color:var(--ink);white-space:nowrap}
.chip.good{color:var(--good)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}
.rsi.good{color:var(--good);font-weight:600}.rsi.bad{color:var(--bad);font-weight:600}
.muted{color:var(--muted)}
.empty{color:var(--muted);background:var(--panel);border:1px dashed var(--line);border-radius:8px;padding:14px}
details{margin-top:10px}
summary{cursor:pointer;color:var(--accent);font-size:14px}
.beststrat{list-style:none;padding:0;margin:8px 0 0;display:grid;gap:6px}
.beststrat li{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px}
.beststrat li span{color:var(--muted);min-width:170px}
.beststrat li em{font-style:normal;color:var(--muted);font-variant-numeric:tabular-nums}
.model{font-size:13px;color:var(--muted)}
.notes{font-size:13px;color:var(--muted);max-width:75ch}
.notes p{margin:0 0 8px}
.testbadge{display:inline-block;font-size:12px;color:var(--warn);border:1px solid var(--warn);border-radius:4px;padding:1px 6px;margin-left:8px;vertical-align:middle}
.c-ex{white-space:nowrap}
.week{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}
.day{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px;min-width:0}
.day h3{margin:0 0 8px;font-size:15px}
.day ul{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.ev{display:flex;flex-direction:column;gap:2px;font-size:13px;padding-left:8px;border-left:3px solid var(--line)}
.ev.hi{border-left-color:var(--warn)}
.ev.none{color:var(--muted)}
.evmeta{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.evmeta .chip{margin:0}
.evt{font-family:var(--mono);font-size:12px;color:var(--muted)}
.evtitle a{font-size:12px;white-space:nowrap}
.refresh{margin-top:10px;display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;font-size:13px;color:var(--muted)}
.refresh:empty{display:none}
.refresh button{font:inherit;font-weight:600;font-size:14px;min-height:40px;padding:8px 16px;border-radius:6px;border:1px solid var(--accent);background:var(--panel);color:var(--accent);cursor:pointer}
.refresh button:hover:not(:disabled),.refresh button:focus-visible{background:var(--accent);color:var(--panel);outline:none}
.refresh button:disabled{opacity:.55;cursor:default}
/* Phone: each pick becomes a compact card instead of a wide table row */
@media (max-width:720px){
.picks thead{display:none}
.picks,.picks tbody{display:block;width:100%}
.picks tr{display:grid;grid-template-columns:1fr 1fr 1fr;grid-template-areas:"tk st st" "pop pop pop" "ex cap ret" "fl fl fl";
gap:10px 12px;padding:14px 12px;border-bottom:1px solid var(--line)}
.picks tbody tr:last-child{border-bottom:0}
.picks td{border:0;padding:0;min-width:0}
.picks td.n{display:none}
.picks .c-tk{grid-area:tk}.picks .c-st{grid-area:st;text-align:right}.picks .c-pop{grid-area:pop}
.picks .c-ex{grid-area:ex}.picks .c-cap{grid-area:cap;text-align:left}.picks .c-ret{grid-area:ret;text-align:right}
.picks .c-fl{grid-area:fl;min-width:0}
.picks .pop{min-width:0}
.picks .sub{white-space:normal}
.picks .c-cap::before,.picks .c-ret::before{content:attr(data-label);display:block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:2px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""


REFRESH_JS = r"""<script>
(function () {
  var box = document.getElementById('refresh');
  if (!box) return;
  var trig = box.getAttribute('data-trigger'), run = Date.parse(box.getAttribute('data-run')) || 0;
  var btn = document.getElementById('refreshBtn'), msg = document.getElementById('refreshMsg');
  var KEY = 'scanRefreshAt', MINUTES = 5;
  function say(t) { msg.textContent = t; }
  function hhmm(ms) { return new Date(ms).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}); }
  function pending() {
    try { var v = +localStorage.getItem(KEY); return v && v > run && Date.now() - v < 10 * 60000 ? v : 0; }
    catch (e) { return 0; }
  }
  if (!window.claude || !window.claude.use) {
    say('Opened from the PC: double-click run_now.bat for a fresh scan.');
    return;
  }
  if (!trig) return;
  window.claude.use('mcp').then(function (mcp) {
    if (!mcp) return;
    btn.hidden = false;
    var p = pending();
    if (p) {
      say('Fresh scan requested at ' + hhmm(p) + '. Reload this page in a few minutes; tap Refresh again if nothing changed.');
    } else {
      say('Re-prices the best tickers live on your PC and updates this page (about ' + MINUTES + ' minutes).');
    }
    btn.addEventListener('click', function () {
      btn.disabled = true;
      say('Starting the scan…');
      mcp.callTool('Claude Code Remote', 'fire_trigger', {trigger_id: trig}, {cache: false}).then(function () {
        var now = Date.now();
        try { localStorage.setItem(KEY, String(now)); } catch (e) {}
        btn.disabled = false;
        say('Fresh scan started at ' + hhmm(now) + '. About ' + MINUTES + ' minutes; you get a notification, then reload this page.');
      }, function (err) {
        var c = err && err.code;
        btn.disabled = false;
        if (c === 'needs_reauth') say('Reconnect Claude Code Remote in claude.ai Settings → Connectors, then tap Refresh again.');
        else if (c === 'server_not_connected' || c === 'selection_required' || c === 'server_not_found') say('The Claude Code Remote connector is not available here. Double-click run_now.bat on the PC instead.');
        else if (c === 'approval_required' || c === 'not_in_manifest' || c === 'consent_required') say('Refresh needs your OK for this page. Tap Refresh again and choose Allow.');
        else if (c === 'blocked_by_policy') { btn.disabled = true; say('Your organization blocks this connector here. Double-click run_now.bat on the PC instead.'); }
        else if (c === 'not_granted' || c === 'capability_disabled' || c === 'capability_removed') { btn.hidden = true; say('Refresh is not available in this view. Double-click run_now.bat on the PC instead.'); }
        else if (c === 'tool_error') say('The scan task did not start: ' + (err.message || 'no reason given') + '.');
        else say('Could not confirm the scan started (' + (c || 'error') + '). If no notification arrives in a few minutes, tap Refresh again.');
      });
    });
  });
})();
</script>"""


def render_html(meta, lists, feats, stats, catalysts=None, events=None):
    spy = meta.get("spy") or {}
    gen = meta["generated_utc"]
    try:
        hst = dt.datetime.fromisoformat(gen).astimezone(dt.timezone(dt.timedelta(hours=-10)))
        gen_txt = hst.strftime("%a %b ") + str(hst.day) + hst.strftime(", %I:%M %p HST").replace(" 0", " ")
    except ValueError:
        gen_txt = gen
    last_trade = meta.get("quotes_last_trade") or meta.get("data_asof") or "—"
    cat = (catalysts or {}).get("tickers") or None
    sections = "\n".join(_section(tf, lists.get(tf), cat) for tf in TF_ORDER)
    nxt = ""
    for d in (events or {}).get("days", []):
        hi = [x for x in d["events"] if x.get("impact") == "high" and x.get("category") != "earnings"]
        if hi:
            day = dt.date.fromisoformat(d["date"])
            t = f' {hi[0]["time_hst"]} HST' if hi[0].get("time_hst") else ""
            nxt = (f'<div class="order">Next market event: <b>{e(day.strftime("%a %b"))} {day.day}{e(t)}</b> · '
                   f'{e(hi[0]["title"])} · <a href="#week">week ahead</a></div>')
            break
    if meta.get("test_run"):
        test = '<span class="testbadge">Test run, not logged</span>'
    elif meta.get("market_closed"):
        test = '<span class="testbadge">Prior-close quotes: not logged</span>'
    else:
        test = ""
    if meta.get("mode") == "fast":
        try:
            fg = dt.datetime.fromisoformat(meta["full_generated"]).astimezone(dt.timezone(dt.timedelta(hours=-10)))
            fg_txt = fg.strftime("%a %I:%M %p").replace(" 0", " ")
        except (KeyError, TypeError, ValueError):
            fg_txt = "the last"
        scope = (f'Live rescan of the <b>{meta["n_tickers"]:,}</b> best tickers from {e(fg_txt)} full scan of '
                 f'<b>{meta.get("full_n") or 0:,}</b> · <b>{meta["n_passed"]:,}</b> trades passed filters · '
                 f'took <b>{round(meta.get("seconds") or 0):,}s</b>')
    else:
        scope = (f'Full scan of <b>{meta["n_tickers"]:,}</b> tickers · <b>{meta["n_passed"]:,}</b> trades passed filters'
                 f' · took <b>{round((meta.get("seconds") or 0) / 60):,} min</b>')
    notes = f"""<section id="method" class="notes"><h2>Method</h2>
<p><b>Ranking.</b> Highest POP first, then lowest capital, then highest return. POP is grouped in 1-point steps so capital decides between near-equal POPs. A minimum return floor per strategy and timeframe keeps tiny-credit trades out.</p>
<p><b>POP.</b> Probability the trade is profitable at expiry (beyond breakeven), computed twice: once with the option’s own implied volatility and once with the higher of 20- and 60-day realized volatility. The lower of the two is used. After 50 or more picks resolve, a calibration model trained on this scanner’s own graded picks adjusts POP; it is switched off whenever it fails to beat raw POP on recent picks.</p>
<p><b>Capital.</b> Max loss for spreads and condors, cash secured for puts, net share cost for buy-writes, premium for LEAPS calls. One contract each.</p>
<p><b>Prices.</b> Fills assume halfway between mid and the natural price. Quotes are CBOE delayed (about 15 minutes); verify the live chain before trading.</p>
<p><b>Fixed filters.</b> Every listed trade makes at least {pct(meta.get("min_profit"), 0)} of its capital at max profit (credit ÷ max loss for spreads and condors, credit ÷ cash secured for puts, gain if called for buy-writes, the one-standard-deviation-up payoff for LEAPS calls). Price ≥ $3, 20-day average volume ≥ 300k, short-leg open interest ≥ 100, tight bid-ask, short-leg delta 0.03–0.45, no earnings before expiry for daily, weekly and monthly premium selling, capital caps ($2,500 spreads, $5,500 puts and buy-writes).</p>
<p><b>Sources.</b> CBOE delayed option chains; daily price history from Yahoo Finance’s chart feed; FINRA daily short-sale volume ({e(str(meta.get("short_volume_date") or "—"))}); SEC EDGAR filing counts; Nasdaq earnings calendar; 13-week T-bill yield for the risk-free rate.</p>
<p>Research output, not investment advice. Dividends and early assignment are not modeled.</p></section>"""
    return f"""<title>Strategy Scanner</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+Condensed:wght@600&family=IBM+Plex+Sans:wght@400;600&display=swap">
<style>{CSS}</style>
<div class="wrap">
<header>
<h1>Strategy Scanner · {e(meta["run_date"])}{test}</h1>
<div class="meta"><span>Run <b>{e(gen_txt)}</b></span><span>Quotes <b>{e(str(last_trade))}</b> ET</span>
<span>SPY <b>${num(spy.get("price"))}</b> RSI <b>{num(spy.get("rsi14"), 0)}</b></span>
<span>VIX <b>{num(meta.get("vix"), 1)}</b></span><span>T-bill <b>{pct(meta.get("risk_free"), 2)}</b></span>
<span>{scope}</span></div>
<div class="order">Ranked by highest POP, then lowest capital, then highest return. Minimum {pct(meta.get("min_profit"), 0)} profit on every trade.</div>
{nxt}
<div class="refresh" id="refresh" data-trigger="{e(meta.get("refresh_trigger") or "")}" data-run="{e(gen)}"><button type="button" id="refreshBtn" hidden>Refresh now</button><span id="refreshMsg" role="status" aria-live="polite"></span></div>
</header>
<nav>{"".join(f'<a href="#{tf}">{TF_LABEL[tf]}</a>' for tf in TF_ORDER)}<a href="#week">Week ahead</a><a href="#catalysts">Catalysts</a><a href="#learning">Learning</a><a href="#tickers">Tickers</a></nav>
{sections}
{_week_section(events)}
{_catalyst_section(catalysts)}
{_learning(stats, meta)}
{_tickers(lists, feats)}
{notes}
</div>
{REFRESH_JS}"""


def _cjson(c):
    keys = ["ticker", "strategy", "timeframe", "dte", "credit", "capital", "roc", "pop_cons", "pop_iv", "pop_rv",
            "pop_cal", "p50", "p_max", "be_lo", "be_hi", "rsi14", "iv_rv", "earn_in_window", "n8k_7d",
            "short_vs_putwall", "spot_vs_flip", "ret_called",
            "ev", "ev_iv", "ev_rv", "ev_pass", "ev_fail", "rv", "iv_short"]
    d = {k: c.get(k) for k in keys}
    d["price"] = c["S"]
    d["expiry"] = c["expiry"].isoformat()
    d["legs"] = legs_text(c)
    d["strategy_name"] = STRATEGY_NAMES[c["strategy"]]
    return d


def to_json(meta, lists, feats, stats):
    tickers = pick_tickers(lists)
    return {
        "meta": {k: v for k, v in meta.items() if k != "skipped"},
        "top": {tf: [_cjson(c) for c in rep["top"]] for tf, rep in lists.items()},
        "tickers": {t: {k: feats.get(t, {}).get(k) for k in ("price", "rsi14", "iv30", "rv20", "put_wall",
                                                            "call_wall", "gamma_flip", "earnings_date", "last_8k")}
                    for t in tickers},
        "learning": stats,
    }


def notify_telegram(tcfg, meta, lists):
    token, chat = (tcfg or {}).get("bot_token"), (tcfg or {}).get("chat_id")
    if not token or not chat:
        return False
    lines = [f"Strategy Scanner {meta['run_date']}"]
    for tf in TF_ORDER:
        rep = lists.get(tf)
        if not rep or not rep["top"]:
            continue
        lines.append(f"\n{TF_LABEL[tf]}:")
        for c in rep["top"][:3]:
            lines.append(f"{c['ticker']} {legs_text(c)} {c['expiry']:%m/%d} POP {pct(_rank_pop(c))} "
                         f"cap {money(c['capital'])} ret {pct(c['roc'], 1)}")
    data = urllib.parse.urlencode({"chat_id": chat, "text": "\n".join(lines)}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
        return True
    except Exception:
        return False
