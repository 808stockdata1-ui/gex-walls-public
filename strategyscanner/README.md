# Strategy Scanner

Every weekday it scans every optionable US stock and ETF with a liquid chain (refreshed weekly), builds every candidate trade across four
timeframes, ranks them **highest POP → lowest capital → highest return**, logs the best ones, and
grades its own past picks so the ranking improves with evidence.

| Timeframe | Expirations | Strategies |
|---|---|---|
| Daily | 0–4 DTE | put/call credit spreads, iron condors, cash-secured puts, buy-writes |
| Weekly | 5–14 DTE | same |
| Monthly | 21–60 DTE | same |
| LEAPS | 240+ DTE | same, plus deep-ITM LEAPS calls |

## Universe
- `universe_auto.txt`: rebuilt every Sunday 3:00 AM HST by `tools\build_universe.py` (task **StrategyScanner Universe**).
  Every symbol in the Cboe options symbol directory that is a US-listed stock or ETF, price >= $3, stocks with
  day volume >= 300k, ETFs with FINRA-reported volume >= 150k, and a chain with at least 8 liquid strikes
  within +/-25% of spot and 2,000+ open interest inside 60 days. First build (Oct 23 2026): 867 kept (725 stocks,
  142 ETFs) out of 5,327 optionable symbols. `output\universe_stats.csv` shows why each symbol was kept or dropped.
- `universe.txt`: your own watchlist, always scanned on top of the auto list.

## Schedule (two-stage, so the picks you trade are seconds old, not 40 minutes old)
| HST (weekdays) | What | Time it takes |
|---|---|---|
| 1:40 AM | **Full scan** of the whole universe on prior-close quotes (Windows task **StrategyScanner Daily**). Grades and refits the learning loop, marks open picks, fills the day's cache (price history, EDGAR counts, earnings calendar) and writes `output\shortlist.json`: the 60 best tickers, round-robin across every timeframe × strategy, including near-misses on the profit floor. | ~15 min |
| 2:32 AM | **Catalyst review** (Claude scheduled task) on the full scan's top picks; writes `output\catalysts_DATE.json`. | ~5 min |
| intraday | Picks re-priced at 9:50 AM, 12:45 PM, 3:15 PM ET by the GEX Walls app's **Picks** tab (the old 3:47 AM HST live-rescan task was retired). | — |
| any time | **Refresh now** button on the page: fires the auto-approved Claude scheduled task "Scanner Refresh button (live rescan)" (`run_daily.py --fast`): re-prices only the shortlist from live (15-min delayed) chains, logs the picks, applies the catalyst flags, republishes the page, sends a phone notification. | ~40–115 s scan, ~3 min end to end |

Cboe serves the delayed chains from two hosts (`cboe_hosts` in `config.json`), each throttled separately, so
requests alternate between them. Each host is paced on its own: a 429 reply pauses that host for Cboe's
Retry-After and slows it 12%; the settled pace is saved in `data\cache\pace.json` and the next run starts 3%
faster (`cboe_rate` is the per-host ceiling). Every request has a hard 45-second deadline so a stalled
transfer can never hang a run. Other programs pulling Cboe chains (e.g. the GEX Walls app) share the limits.
Weekend and holiday runs show prior-close quotes and log nothing.

## Week ahead (on the page, plus "Next market event" in the header)
Next 7 days, times in ET and HST, each linked to its source: FOMC meetings and decisions (federalreserve.gov),
GDP / PCE / trade releases (bea.gov), NYSE holidays and early closes (nyse.com), option expirations (monthly third
Friday, quarterly quad witching, VIX Wednesday; Cboe/OCC rules, holiday-adjusted), $250B+ company earnings
(Nasdaq calendar), and, from the 2:32 AM review, BLS releases (CPI, jobs, PPI, JOLTS, ECI), White House /
Federal Register items with a confirmed date, Fed speeches and Treasury refunding (`output\events_agent.json`).
`tools\build_events.py` rebuilds it; the full scan does so every morning.

## Profit floor (mandatory, never changed by learning)
Every listed trade must make **at least 20% of its capital at max profit** (and the credit/premium itself at least 20% of capital, min_premium_roc) (`min_profit`): credit ÷ max loss
for spreads and condors, credit ÷ cash secured for puts, gain if called away for buy-writes, and the
one-standard-deviation-up payoff for LEAPS calls. It must also clear **20% annualized** (`min_roc_annual`),
which only bites on positions held longer than a year.

## Outputs
- `reports\latest.html` and `reports\YYYY-MM-DD.html`: the ranked report (open in any browser)
- `output\latest.json`: machine-readable top picks, ticker data and learning stats
- `output\picks_YYYY-MM-DD.csv`: the same top picks as a spreadsheet
- `data\scanner.db`: the pick log (SQLite). Every logged pick keeps the exact signals it was chosen with.
- `logs\run_YYYY-MM-DD.log`

## How it learns
1. **Log**: the top 5 per timeframe × strategy (one structure per ticker) go into the pick log daily.
2. **Mark**: each run prices every open pick from the live chain and records whether it reached 50% profit.
3. **Settle**: after expiry, each pick is settled at the official close on expiration day.
4. **Fit**: once 50+ picks have resolved, a regularized logistic model learns how POP and the signals
   (IV/RV, RSI, trend, gamma flip, put wall, short volume, VIX, 8-Ks, catalyst flags, DTE) map to real
   wins. Its output is blended with raw POP by sample size and switched off if it doesn't beat raw POP on
   the most recent 20% of picks. The fixed filters never change.

## Catalyst review (daily, after the scan)
A Claude scheduled task runs at 2:32 AM HST on weekdays (after the full scan): `tools\catalyst_context.py` pulls recent EDGAR
filings for every ticker in the top picks, Claude checks company IR / official press releases for events
before each pick's expiry, writes `output\catalysts_DATE.json`, and `tools\add_catalysts.py` records the
flags in the pick log (so the learning loop can measure whether flagged picks really do worse) and
re-renders the report. Every Refresh-button live rescan carries the flags onto the published page.

## POP
Probability of finishing beyond breakeven at expiry, computed with the option's implied vol and with
realized vol (higher of 20- and 60-day). The lower of the two is used until the learned model takes over.

## Data (all free, no accounts)
CBOE delayed option chains · Yahoo Finance chart history · FINRA daily short-sale volume ·
SEC EDGAR submissions (8-K, Form 4, 13D/G; Item 2.02 8-Ks project earnings when no date is announced) ·
Nasdaq earnings calendar · 13-week T-bill (^IRX).

## Commands
- **Refresh now** button on the published report page (desktop shortcut "Strategy Scanner"): fires the Claude
  scheduled task "Scanner Refresh button (live rescan)" (id in `config.json` → `refresh_trigger_id`, runs with
  automatic approval), which runs the live rescan here, re-applies the morning catalyst review, republishes the
  page and sends a phone notification
- `run_now.bat` – full scan now (~15 min) and open the report
- `run_now.bat --fast` – live rescan of the shortlist (~40 s) and open the report
- `run_now.bat --tickers SPY,QQQ --no-log` – preview: grades and logs nothing, writes `reports\preview.html`
- `.venv\Scripts\python.exe tools\build_universe.py` – rebuild the universe now
- `.venv\Scripts\python.exe tests\test_core.py` – self-tests
- `.venv\Scripts\python.exe tools\add_catalysts.py output\catalysts_DATE.json` – attach a catalyst review

## Settings
- `universe.txt` – your watchlist, one ticker per line (added to the auto universe)
- `config.json` → `universe` – liquidity thresholds for the auto universe
- `config.json` – timeframes, return floors, capital caps, liquidity rules, wheel price cap ($55)
- Telegram push: put a bot token and chat id under `telegram` in `config.json`

Research tool, not investment advice. Quotes are ~15 minutes delayed; verify the live chain before trading.
Dividends and early assignment are not modeled; 50%-profit hits are checked once per run, not intraday.
