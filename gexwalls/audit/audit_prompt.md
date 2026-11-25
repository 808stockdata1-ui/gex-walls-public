# GEX Walls — daily review procedure (self-revising)

The daily scheduled task reads this file and follows it. You may improve steps 1–8 and the checklists (log every edit in
the Changelog). Never weaken the Guardrails section; the scheduled task restates them and they win over this file.

## Context
- App: `C:\Users\<user>\Documents\GEXWalls\walls_app.py` (one file: Python server + page). Runs on Joe's PC on port 8050,
  phone access over Wi-Fi via `GEX Walls (phone on Wi-Fi).bat --no-browser` (LAN mode). Python: `.venv\Scripts\python.exe`.
- Audit: `audit\audit.py` writes `audit\latest.json`, `audit\YYYY-MM-DD.md`, `audit\history.jsonl` (score trend),
  `audit\picks_log.jsonl` (the app's own recommendations, graded after expiry). Usage counts: `usage.json`.
- Picks scanner: since 2026-11-18b it is built into the app — `GEXWalls\strategy\output\latest.json` (picks + graded
  learning stats; the app runs the full scan, re-prices and universe rebuild). The old `C:\Users\<user>\StrategyScanner`
  folder is retired and read-only: its output is frozen at 11-17 and its two Windows tasks are Disabled ON PURPOSE — not
  a fault. Only its `scout\*.md` (tool scout reports, last 11-14) may still be read.
- Joe's goals (what "best" means): daily strategy picks ranked by highest POP, then lowest capital, then highest return;
  every Pick (Picks tab: scanner picks, Best use of capital, swing) pays ≥20% of its capital (scanner min_profit 0.20 and
  min_premium_roc 0.20; app picks_min_return_pct 20) with ★/★★ + alerts for high return AND high chance; credit spreads
  elsewhere ≥15%, nothing anywhere under 10%. Picks under 10% are not counted in any track record (learn.MIN_COUNTED_ROC, audit counted()); daily / weekly / monthly / LEAPS coverage; picks fresh enough to trade
  (a ~40-minute scan is too slow); GEX walls and changes; put credit spreads 0–90 DTE without gaps; cash-secured puts
  (incl. under $30) without tying up large cash for tiny premium; calls/LEAPS/bull call spreads; beginner calculator;
  dark pool / block trades / insiders (big-money and market-maker moves); sector rotation; mining earnings; easy to read
  on a phone. The SAME selections everywhere (one expiration-week chip bar: 0 DTE, every week to ~60 days, All, 2–9 mo,
  LEAPS; same strategy chips, profile, price, min chance/return, max capital, rank, show) — never add a section with its
  own different selectors. Every ticker list shows a GEX trend (past sessions + next 4 weeks), 24h/after-hours activity,
  candle/Fib signals, news, financial health, dilution and analyst view where available. Every ticker mentioned needs current price and RSI(14). Free tools only, no subscriptions or registration,
  primary sources only, never estimate options data without the CBOE chain.

## 1. Measure
0. Preflight: call `get_device_info`. If `connectedFolders` is empty, code changes cannot be tested in the cloud (staging
   is refused and walls_app.py is ~600 KB) — plan a measure/research/report-only run and put "re-attach the GEXWalls folder"
   first in the proposals. Don't spend time on backups or staging attempts.
1. Make sure the app is running (`Invoke-WebRequest http://127.0.0.1:8050/api/watch`) BEFORE starting audit.py (an audit
   against a down app is wasted). If down: note the last write time of hist_cache\* (≈ when it died), start it detached
   with the Win32_Process Create recipe in 5.4 (never from a Desktop Commander shell), wait 25 s, then audit.
   Why it died: `Get-WinEvent System` Kernel-Power 41 / 6008 = PC crashed or lost power and rebooted (11-14); the app
   has no autostart, so a reboot always means a dead app until this review.
   Also check HOW it runs: `Get-NetTCPConnection -LocalPort 8050 -State Listen` must show 0.0.0.0 (LAN). 127.0.0.1 means it
   was started without --lan (e.g. double-clicked walls_app.py) and the phone can't reach it - restart with the .bat (5.4).
   Never delete audit\run_out.txt before a rerun; -RedirectStandardOutput overwrites it.
   Helper scripts: give each a new dated name WITH time (e.g. audit\seen_add_YYYY-MM-DD-HHMM.py) and write it only via
   `if (Test-Path $f) {'EXISTS'; exit}` (two runs share one date: the evening HST run is the next ET date),
   and never delete helpers afterwards (only backups > 14 days may be deleted).
2. Run `.venv\Scripts\python.exe audit\audit.py` from the GEXWalls folder (≈2–4 min). Start it hidden:
   `Start-Process -WindowStyle Hidden -FilePath .venv\Scripts\python.exe -ArgumentList 'audit\audit.py'
   -RedirectStandardOutput audit\run_out.txt -RedirectStandardError audit\run_err.txt`, research meanwhile, then poll
   run_out.txt with `Start-Sleep 50` (Desktop Commander calls time out at ~60 s). The PC clock is Hawaii time.
   Edit this file with the venv's Python (utf-8), never PowerShell Get/Set-Content (PS5 reads it as ANSI and
   double-encodes every —, ≤, ★ - happened 2026-11-10, repaired).
3. Read `audit\latest.json` and the last 7 lines of `audit\history.jsonl`. Note the overall score, each category and
   the change vs the previous run.

4. Coverage check (no time gaps): for SPY, AAPL and one sub-$20 stock, `/api?t=` must show walls for every month out
   to ~100 days (largest gap between listed expirations ≤ ~35 days); spread, short-put, watchlist and call windows must
   be contiguous (0–90 days for puts/spreads, 7 days to LEAPS for calls); Strategy Scanner timeframes must be contiguous
   (daily 0–4, weekly 5–20, monthly 21–60, 2–8 months 61–239, LEAPS 240+).

## 2. Understand usage
Read `metrics.usage_14d` in latest.json. Which tabs/sections/buttons are used most and least? Unused or rarely used
sections are candidates to fold into "How it works" details or move down; heavily used ones should be reachable with
fewer taps on a phone.

## 3. Research (≤20 minutes, quality over quantity)
- Read the newest `StrategyScanner\scout\*.md` and `audit\research_seen.json` (create it as `[]` if missing); skip anything seen.
- Look for free, maintained tools/repos/papers/data that improve accuracy or coverage: dealer gamma (vanna, charm, gamma
  flip, sign conventions), 0DTE flows, OI changes, skew / term structure, earnings IV crush, dark-pool and block prints,
  13F / Form 4 / 13D (e.g. edgartools), short volume, borrow, institutional positioning; plus feature ideas from
  professional platforms (thinkorswim, tastytrade, OptionStrat, Market Chameleon, SpotGamma, Unusual Whales) — ideas
  only, never paid data.
- For each candidate: license, last commit, stars/issues, quick security review (no obfuscation, no binaries, no
  install scripts that fetch and run remote code), and whether it needs registration (then skip unless public data).
- Append what you reviewed to `audit\research_seen.json` with verdict adopt / watch / skip. Edit it with the venv's
  Python (json.load with utf-8-sig), not PowerShell 5 ConvertTo-Json (it nests arrays under "value").
- GitHub repo facts: `Invoke-RestMethod https://api.github.com/repos/<owner>/<repo>` on the PC (the cloud `gh` has no access).

## 4. Choose at most 3 improvements for today
Priority: (a) failed accuracy checks → (b) failed freshness / speed → (c) calibration gaps (predicted vs actual once
20+ independent ticker/expiry groups exist) → (d) a layout change supported by usage → (e) one research item that
serves Joe's goals. Each change must be small, testable and reversible. Big changes become proposals in the report.

## 5. Implement safely
1. Back up first: copy `walls_app.py` to `backups\walls_app.YYYY-MM-DD-HHMM.py` (keep 14 days of backups).
   Age backups by the date-time in the FILE NAME, never LastWriteTime (Copy-Item keeps the source file's time,
   so a fresh backup can look weeks old).
2. Edit in the cloud container: stage the file with `device_stage_files`, edit, then verify there:
   `python -m py_compile`, extract the page script and `node --check` it, `bandit -q -ll` (only the accepted B104
   LAN-bind finding may remain), and a Playwright smoke test that opens every tab on desktop and phone widths with no
   page errors.
3. Copy to `/mnt/user-data/outputs/`, wait ~20 s, `device_commit_files` (force), and verify the SHA-256 on the PC.
4. Restart detached from Desktop Commander (if it is started from a Desktop Commander shell it dies when that helper
   restarts — this took the app down on 2026-11-05): stop the python `walls_app` processes, then
   `Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c ""<GEXWalls>\.venv\Scripts\python.exe" walls_app.py --lan --no-browser 1>>logs\app_out.log 2>>logs\app_err.log"'; CurrentDirectory='<GEXWalls>'}`
   (hidden window via Win32_ProcessStartup ShowWindow=0). Wait 15 s, confirm the new version string (there is no
   /api/version: regex the APP_VERSION text out of `GET /`), rerun `audit.py`.
   Tip: `/api?t=SPY` is ~1 MB — print only the fields you need (e.g. `$j.expirations | % { "$($_.exp) $($_.dte)" }`).
   Cloud smoke test: run a copy with `--no-browser --port 8061` and click `nav.tabs button[data-tab=…]` for
   picks, main, puts, calls, calc, flow.
5. If the overall score drops or a new check fails, restore the backup, restart, and record why in the report.
6. If you add a new kind of check to the audit, add it to `audit\audit.py` the same way (backup → test → deploy).

## 6. Report
Write `audit\review_YYYY-MM-DD.md`: score and trend, failed checks, usage findings, research (adopt/watch/skip with
links), changes shipped (before/after score), and proposals that need Joe's decision.

## 7. Improve this procedure
If a step was slow, unclear or failed, edit this file to fix it and add a dated line to the Changelog. Keep it concise.

## 8. Message Joe (≤12 lines)
Overall score (Δ vs last run), top findings, what changed, up to 3 new tools with a one-line why and how to get them,
and decisions needed. Any ticker named gets its current price and RSI(14) from the app's data. End with: "CBOE quotes
~15 min delayed; not financial advice."

## Guardrails (never weaken)
- Never place trades, log into accounts, enter credentials, or register for services.
- Strategy Scanner folder and its scheduled tasks are read-only for this review.
- Free tools only; nothing installed without a security review; prefer local Windows execution.
- At most 3 changes per day; always back up first and roll back on any regression.
- Never fabricate numbers, sources or results; cite primary sources.
- Do not delete anything except backups older than 14 days.

## Changelog
- 2026-11-02: created. First audit run; added Black-Scholes gamma at displayed spot, usage tracking, Health view,
  compact layout, folded help text, back-to-top, fixed Dark-pool tab recursion.
- 2026-11-02: 02i/02j — dealer positioning card (vanna, charm, gamma flip, sign-convention check); "Big money" panel in
  Dark pool & insiders (SEC 13F data sets → local `h13f.sqlite`, rebuilt when SEC publishes a new quarter; 13D/13G
  5%+ owners via EDGAR full-text search). SEC User-Agent comes from `sec_user_agent` (falls back to the Strategy
  Scanner's). Strategy Scanner: track-record shrink lowers over-confident daily CC/CCS chances (learn.group_shrink).
  Review ideas: check `/api/bigmoney/status` is "ready" and the 13F period is ≤ ~5 months old.
- 2026-11-05: uniform selection bar on every tab (expiration-week chips, strategies, filters, ranking), Picks ≥20% with
  ★/★★ alerts, GEX trend (gex_daily.json + next 4 weeks), 24h badge from FINRA ATS (Blue Ocean/MOON/Bruce), after-hours
  via Nasdaq, insights card (SEC health/dilution, Nasdaq news/analysts, candle patterns), Strategy Scanner 2–8 month
  timeframe. Audit: added wall-expiration gap and next-4-weeks GEX coverage checks.
- 2026-11-05: 05b — after-hours % now measured from that day's regular close (Nasdaq's figure is vs the prior close);
  covered calls also in the 2–9 month window; ETFs/funds/commodity trusts get no health grade or dilution flag;
  424B counts as dilution risk only with real share growth; spread windows report listed vs quoted expirations
  (audit fails only when a week has no listed expiration); phone overflow from long SEC error text fixed.
- 2026-11-05: 05c — ★ now only for top-ranked analysts: config `top_analysts` (TipRanks top 10 U.S. analysts of 2025,
  as reported by CNBC 2026-01-25); a stock gets ★ when one of them rated it in the last 12 months (ratings parsed from
  MarketBeat's public forecast page, 1 request / 3 s, cached 24 h — aggregator, labelled as such). Rosenblatt firm star
  removed. Review idea: each January, update `top_analysts` from the new published TipRanks list. SEC User-Agent contact
  set to you@example.com (Joe's choice) in alerts_config.json and the Strategy Scanner config.
- 2026-11-05: 05d — ranked-analyst ★ now from two sources in TOP_ANALYSTS (walls_app.py): TipRanks top 10 of 2025 and
  Extel 2026 All-America #1-in-sector analysts (only the #1s that Jefferies and Evercore ISI published; Extel's full
  table is subscriber-only — add other firms' #1s as they publish them). StarMine has no stock-analyst awards after 2020
  (current StarMine awards = economists/FX), so it is not used. Top-analyst consensus: ★★ when 2+ ranked analysts who
  state a rating all agree (badge names the sources); lookup card shows Buy/Hold/Sell counts. Unnamed MarketBeat rows
  count as "likely" only inside the analyst's SIC coverage list. Review idea: each October add new Extel #1s, each
  January replace the TipRanks 10.
- 2026-11-05: 05e — fix for "page shows last close, picks wrong": (1) every list's stock price is now a live cell
  (`/api/live`, Nasdaq last sale incl. pre-market / after-hours, refreshed every 60 s) showing the move since the trade was
  priced and ⚠ when the move crosses / nears a short strike, a stop, or exceeds 3%; (2) automatic re-pricing while the
  market is open: picks every 30 min 9:50–15:50 ET, spread & put scan 9:47 / 11:47 / 13:47, calls 10:20 / 12:20 / 14:20,
  puts 10:05 / 12:05 / 14:05 ("auto_refresh": false turns the scan/calls part off); (3) each section says when its option
  prices were taken and that options can't re-price before 9:30 ET. Review idea: check that `/api/scan` ts is from today
  after 10:15 ET on trading days, and how long a full scan takes (it was ~25 min — a speed target).
- 2026-11-05: 05f — root cause of "prices not accurate": CBOE rebuilds some symbols' option-chain files only a few
  times a day (on 2026-11-05 at 15:00 ET, 32 of 134 tickers were > 30 min old, 17 still held 11-04 close quotes), so
  their underlying price AND option bid/ask were hours old. load_chain now records chain_time / chain_age_min / stale
  (older of the file's Last-Modified and last_trade_time; stale = > 45 min or before today's open while the market is
  open). Scans (spreads, puts, calls) and watchlist setups skip stale chains and list them ("stale" in each results
  file); lookup shows a red banner. Strategy Scanner data.option_chain skips the same (scanner/data.py _chain_stale).
  Review idea: report the count of stale chains per scan; if > 25%, note it in the report.
- 2026-11-05: 05g — CBOE returned HTTP 429 (rate limit) when the spreads scan, puts scan, calls scan and picks re-price ran
  together, stalling all of them. CBOE-heavy jobs now queue behind one lock (`_queued`: scans, picks re-price, daily
  GEX snapshot); a queued job shows "queued — waiting for …". Never start several scans at once by hand either.
- 2026-11-05: 05h — sector rotation: sort menu (momentum, highest chance of profit in the selected week, trending up,
  1-/3-month strength, highest GEX) and a "Best chance · <week>" column (best put credit spread 15%+ / short put 10%+).
  Global sort (every section's bar) gained "Highest GEX (net, all expirations)" from `/api/gexmap` (gex_daily.json).
- 2026-11-05: 05i — optional Schwab Trader API (Joe's account): "Schwab real-time data" card (keys entered by Joe on the
  PC only, kept in schwab_keys.json; OAuth tokens in schwab_token.json: access 30 min auto-refresh, refresh 7 days).
  load_chain uses Schwab first (CBOE shape; iv30 still from CBOE's quote file), CBOE on any problem; `/api/chainraw`
  serves the Strategy Scanner (scanner/data.py `_local_schwab_chain`). Never read or print the keys/tokens. Review idea:
  if `/api/schwab` shows connected with days_left < 2, remind Joe to log in again; check ok vs fail counts.
- 2026-11-06: 06a — speed: open-trade values computed in the background (`_tr_cache`, refreshed ~2 min while open),
  never inside GET /api/trades or /api/picks; Save responds at once ("Saving…" → toast "Saved ✓"); duplicate adds
  (same ticker/exp/legs/price/qty within 10 min) ignored. Background bulk jobs (recorder, watchlist, sector GEX) use
  CBOE (`_bg_cboe`); Schwab only for lookups, trade values, scans. Schwab full chains 502 for SPY/SPX → windows
  ALL→400d→120d, beyond-window contracts merged from CBOE. IV recomputed from mids (Black-76, put-call-parity
  forward) because Schwab's own IV is erratic on thin LEAPS. Source shown ("Schwab real-time"/"CBOE delayed") on
  lookup and scan sections. Spread credit = halfway mid↔natural (short put: mid↔bid); cards show leg bid/ask, mid,
  natural, priced time. Strategy direction chips (bullish/bearish/neutral). Schwab card shows "✓ Connected" +
  Reconnect. Roll: "Roll" button on open trades — old trade closed as "rolled" at the buy-back price, new trade
  entered or linked to one already in My trades (`rolled_to` / `rolled_from`), card shows realized + combined P/L.
- 2026-11-06: 06b — lookups took 50–60 s after close because CBOE was answering HTTP 429 (after-close bulk jobs) and
  `_cboe_get` backed off 2+4+8+16+32 s even for clicks. Now only `_bg_cboe` jobs are patient (0.6 s pacing, 5 tries);
  everything else waits ≤4 s and gets an immediate "busy" while CBOE throttles; IV30 keeps its last value; daily
  history falls back to Schwab `/marketdata/v1/pricehistory` (2 y daily) when CBOE fails.
- 2026-11-08: added step 1.0 preflight (empty folder grant → report-only run; 2nd day blocked), research_seen.json
  editing with Python (PS5 ConvertTo-Json nested the list once and was repaired), GitHub API via the PC.- 2026-11-09: 09a — et_today() (New York date) for option expiry/DTE; no empty Friday week chip; audit treats the
  furthest unlisted week as a note. Step 5.4: version check via GET /, keep /api output small, smoke-test recipe.- 2026-11-09: 09b — earnings IV-crush card (earn_iv in /api?t=). Strategy Scanner shrink never shows POP above raw.
  GitHub export: run `tools\export_repo.py --commit "<msg>"` after shipping changes so github\gex-walls stays current
  (Joe pushes). Other agents follow AGENTS.md / ARENA.md in that repo; compare with `arena\score.py`.- 2026-11-09: 09d — "all prices wrong": since Schwab chains (05i) adjusted contracts (after reverse splits/mergers,
  e.g. LCID1, deliverable ≠ 100 shares, ~0.00/0.01 quotes, big OI) were renamed with the plain ticker, so they sat
  next to the real contracts under the same name. The Strategy Scanner (which drops adjusted roots only by name) and
  the app's scans could price a spread's bought leg at ~0 → inflated credit (LCID −5C/+5.5C Jan-27: $0.30 shown vs
  $0.165 live mid, "156% return"); GEX/OI also inflated. Now skipped (nonStandard / multiplier ≠ 100 / root =
  ticker+digits); index roots kept (SPX vs SPXW); CBOE beyond-window merge keeps only the standard root.
  Check: backups\cmp_picks.py <scanner latest.json> and backups\cmp_app.py compare every pick with the live chain
  (after fix: 0/50 scanner picks and 0/120 app rows off, no duplicate contract names).
- 2026-11-10: step 1.2 — audit takes ~4 min now; start it hidden, research while it runs. Also review the app's own
  per-type graded picks (latest.json metrics.calibration.by_type) even below 20 clusters (swing was 0/19 on 11-10).
- 2026-11-11: app was found down (dead since 11-09 23:50 HST, whole 11-10 session lost); step 1.1 now checks the app
  before the audit and restarts detached. A stale audit\_add_seen.py was re-run and then deleted - helpers now get
  dated names and are never deleted. 11a FINRA short-interest tile (Dark pool tab); 11b swing chance blended with the
  app's graded swing record (1/29). Review idea: once 20+ new swing picks close, check swing calibration again.
- 2026-11-12: 12a — new "Early moves" tab + swing rebuilt (the old swing list graded 1 win / 32 closed: targets 10–20%
  away capped at 4 ATR; the Picks 20% floor left ~2 rows). Engine `run_edge` (edge_results.json, rebuilt after each
  rotation scan and after 16:25 ET): per ticker factor move (1.5+ ATR up, volume > 50-day avg, close in top half),
  RS line vs SPY and vs its theme basket (equal-weight index of the theme's stocks; EDGE_THEMES adds AI infra, drones,
  quantum, nuclear, miners, crypto, AI semis/power, space), coil, up/down volume, pocket pivots, stage (Ignition →
  Follow-through → Extended) and a 0–100 early score. Rules were picked on 2022–2024 bars and checked once on
  2025–2026 (423 tickers, CBOE daily bars): vs arbitrary entries with the same exits (+0.10R dev, +0.13R 2025, −0.10R
  2026) — THF factor move in a leading theme +0.29 / +0.23 / +0.04; PWR 2.5-ATR or 3×-volume factor move +0.21 / +0.22 /
  −0.03; THL theme RS high before price +0.36 / +0.48 / +0.05; THN theme RS new high +0.25 / +0.32 / +0.03. Dropped (no
  edge over arbitrary entries): coil breakouts, pullbacks to the 20-day, RSI(2) dips; RS-vs-SPY-before-price failed
  out of sample and is listed but "not used". Caveats: today's ticker list (survivorship), daily bars, next-open fills,
  0.1% cost. Swing rows = live signals (≤3 sessions) of rules whose cached-window back-test beats arbitrary entries;
  audit `counted()` accepts swing rows with a `setup` (measured in R against the stop). hist_cache now keeps 520 bars.
  Algos card: same rules with back-test, recent-120-session result ("tape" banner), forward log (algo_paper.json —
  signals logged the day they appear, graded by the rules; the app never places orders), ETF rotation vs SPY.
  Short squeeze monitor `run_squeeze` (squeeze_results.json, 18:40 ET daily): FINRA consolidated short interest for
  every symbol (api.finra.org, 5 pages), Nasdaq screener (price/cap/volume), FINRA daily short volume, SEC public float
  ÷ price (estimate), Interactive Brokers public stock-loan file (ftp2.interactivebrokers.com usa.txt, public
  "shortstock" read-only login: fee + shares available; bandit B321/B402 marked nosec for this one read-only download;
  borrow_hist.json keeps 30 days so fee/availability trends build locally). Joe's pass/fail filters and weights
  (areas: squeeze spec) → pass / near miss (which filter) / wider watch, rank change vs the prior day.
  Every table: click a column header to sort (generic, survives re-render; `data-sv` gives exact sort values).
  Review ideas: (1) metrics.edge in latest.json — each setup's avg R vs baseline, recent-120 and the forward log;
  if a setup's forward record (20+ closed) is below baseline, mark it not used; (2) after 3+ borrow snapshots check
  the fee-trend scoring; (3) never tune thresholds on the same window they are judged on.
- 2026-11-12: 12b — Leveraged ETFs card (Early moves tab): 30 underlying→bull/bear pairs (run_lev inside
  edge_results.json "lev"). Signals read on the UNDERLYING, traded as the bull fund from the next open; the fund's
  overnight+intraday returns compound daily so decay is in the results. Rules chosen 2022–2024, checked 2025/2026;
  kept only what was positive in all three: DIP (above 200d, RSI2<5, out over the 5-day, ≤7 sessions), WASH (below
  200d, RSI2<5, out over the 5-day, ≤5), FM (1.5+ ATR up day on volume above the 50d avg, out under the 10-day, ≤10).
  Dropped: plain holds, trend filters, 20-day breakouts and EVERY inverse-ETF rule (all negative — inverse funds are
  listed for hedging only, no signals). Card shows per-rule stats + recent-120, live BUY/in/SELL states, RSI(2),
  3-month fund-vs-leverage×underlying drag, and each pair's own record per rule. Review idea: check rec_avg per rule;
  if a rule's recent 50+ trades turn negative while the pooled stat stays positive, flag it in the report.
- 2026-11-12: 12c — "🍀 Everything aligned" card (top of Early moves; edge_results.json "clover", rows carry clv /
  clv_on / clv_days). 12 conditions (EDGE_CLV): trend stack, RS high vs SPY, RS high vs theme, theme leading, factor
  move held, accumulation, pocket pivots, above the 78.6% Fib of the 6-month range, MACD rising, RSI 55–75, volume
  surge, squeeze released. Clovers: 8 → 1, 9 → 2, 10 → 3, 11–12 → 4. Tested on 423 tickers: 8+ aligned is ~2–3% of
  stock-days; 20 sessions later vs any day — 2022–24 +0.69 vs ~+0.4 ATR, 2025 +1.31 vs +0.80, 2026 +0.49 vs −0.03;
  up 56% of the time vs 53%. The count is NOT monotonic past 8 (10–12 were no better out of sample; the conditions are
  correlated momentum measures), so the card says "a real tilt, not a sure thing" and shows the live tier stats.
  Extras (gamma sign, squeeze fuel, earnings date) are displayed but not counted. Review idea: compare the tile stats
  (clover.t8 vs clover.all) each week; if 8+ stops beating "any day" over 120 sessions, say so in the report.
- 2026-11-12: 12d — dealer card tile "Hedging vs trading volume" (dealer.size: |net GEX| per 1% move and per
  typical ATR day as % of 20-day $ volume; idea from simonro/Edge-GEX-Lab, MIT). Audit price check also accepts Nasdaq's
  extended-trading last outside 9:30–16:00 ET (a run right after the close compared Schwab's after-hours 684.00 with
  Nasdaq's regular close 690.38 and failed). Step 1.2 tip: write helpers with Set-Content -Encoding UTF8 then run them
  with the venv's Python; print with PYTHONIOENCODING=utf-8 (cp1252 crashes on ≤/★).
- 2026-11-12: 12e - Tax-loss bounce card in Early moves (strategy: TAX_LOSS_BOUNCE.md). Screen: small/mid caps down 30%+ this
  year within 10% of the 52-week low (Schwab batch quotes for the 52-week range, CBOE bars), SEC filings grade as a
  falling-knife check, season calendar (buy window = December expiration week to Dec 23, exit Jan 2-20), and put / call /
  call-spread setups from the real chain (`_tlb_setups`; `_call_candidates` gained `exps_force`). Runs 10:40 and 19:05 ET in
  season; results in taxloss_results.json; API /api/taxloss. Seasonal tendency, not back-tested here - say so if asked.
  Known: tests\test_repair_api.py fails (walls_app has no `repair_data`) - predates this change.
- 2026-11-13: 13a audit only — SPY unlisted weeks > 6 weeks out are notes (CBOE listed Dec 31 then Jan 15, no Jan 8);
  pre-12a swing rows graded as `swing_old` (shown, not scored). Step 1.2 tip: audit.py writes run_out.txt only at the end
  (~3 min) — an empty file while python runs is normal; poll with Start-Sleep 55. Quick listing check without the app:
  CBOE `cdn.cboe.com/api/global/delayed_quotes/options/<T>.json`, expiry = 6 digits before C/P (some roots are prefixed, e.g. B1SPY).
- 2026-11-14: 14a usage counts only user clicks on a closed <summary> (auto-opened salvage panels had inflated usage to 2,100+ opens); 14b header wraps on phones (was 143 px sideways scroll). Step 2 tip: distrust any usage key in the thousands - check whether the section is re-rendered open. Step 5.2 tip: in the phone smoke test also compare document.documentElement.scrollWidth with innerWidth on every tab.
- 2026-11-15: 15a outside market hours the daily bar / RSI / day % use Schwab's regular-session close (`regular.regularMarketLastPrice`), not the after-hours lastPrice (bug since 13a; SPY 11-13 bar was 749.51 vs 754.37 close). Audit price check falls back to CBOE's quote file when api.nasdaq.com returns `data: null` ('Error while calling vendor', all symbols, 11-14 night). App was down ~34 h after a reboot (Kernel-Power 41); step 1.1 now names the event log check. Review idea: on a weekday evening confirm lookup close = CBOE close.
- 2026-11-16: 16a p.put error text wraps (cause of Picks 420 px at 390 px in the cloud test: long 'Scanner output not found' path). App found on 127.0.0.1 only (15b started by double-click, no --lan) - step 1.1 now checks the listen address. Smoke-test tip: when scrollWidth > innerWidth, find the culprit by hiding children one at a time (display:none) until it fits.
- 2026-11-17: 17a run_edge adds Schwab's final regular-session bar when CBOE daily history lags a session (edge asof was 11-13 all evening of 11-16); audit gained 'early-moves bars (edge asof)' freshness check (NYSE_HOLIDAYS in audit.py - extend yearly). Step 5.4 tip: POSTs to the app need header X-GexWalls: 1 (else 403 forbidden), e.g. POST /api/edge/run to rebuild.
- 2026-11-18: 18a app listens on the home network by default (double-click no longer means 127.0.0.1-only; --local = PC only); audit gained ux check 'phone (Wi-Fi) can reach the app'. Step 5.4 tip: the Win32_Process Create recipe with ShowWindow=0 + 1>>/2>> redirection returned 0 but no app started (twice); `cmd.exe /c ""<GEXWalls>\GEX Walls (phone on Wi-Fi).bat" --no-browser"` via Create (no redirection) worked - the port is held by the child python.exe (the .venv python.exe is a launcher), and it takes ~30-45 s to listen.
- 2026-11-19: audit gained calibration check 'early-move <setup> forward log' (fails once 20+ paper trades closed below the arbitrary-entry baseline; silent before 20). Step 5.1: age backups by file name - 3 backups were deleted ~3 h early because LastWriteTime was the source file's time.
- 2026-11-19 (run 19:13 HST): Context — the Picks scanner lives in GEXWalls\strategy since 11-18b; the old
  StrategyScanner folder/tasks are retired (its 11-17 latest.json looked like a 2-day outage until APP_VERSION/SS_DIR
  were read). Tip: before calling any data 'stale', read APP_VERSION's newest entry. No code change this run.
- 2026-11-21: 21a app pauses an early-move setup for swing picks once its forward paper log has 20+ closed trades below the arbitrary-entry baseline (still logged/graded; un-pauses itself); audit forward-log check passes when paused. THN paused (21 closed, all stops in the 11-17..20 selloff). Step 1 tip: before acting on a forward-log failure, read edge_results.json paper_trades (why/exit) and confirm the stops against /api?t=<T> (cdn.cboe.com quotes/<T>.json now returns 403 from the PC). One-line python -c with \n fails in PowerShell - write a dated helper.
- 2026-11-22: 22a audit detail for 'Picks scanner <tf> <strategy>' also shows the shown-subset record (learning.by_group win_rate_shown / avg_pop_shown / n_shown); rule unchanged. Step 3 tip: on weekends nothing new is graded - expect the same score; the audit still takes ~3 min.
- 2026-11-23: step 1.2 timing - audit took 282 s on 11-22/23 (Sunday); first poll after ~4 min saves 3 empty polls. Sunday-evening runs repeat Saturday's data (no session) - report-only unless a check newly fails.
- 2026-11-24 (run 19:13 HST 11-23): 84 (-9) after the 11-20 monthly expiry + 11-17..20 selloff were graded: scanner PCS/IC far over-confident (weekly PCS shown 38% vs 78%). 'shown chance most accurate' fails because learn.choose_calibration needs 20 scored dates (15 now) before it may switch to group_shift - by design, not a bug; leave the check failing until Joe decides. 'capital table: cc' < 5 = Joe's 10%-of-capital floor (mid/spot) leaves 3 CCs in 40 tickers, not a bug. CBOE now 403s scripted requests (app 23b uses Schwab): Schwab refresh token (7 d) is now critical - remind Joe when /api/schwab days_left < 2. Audit took 157 s; first poll at ~3 min.
- 2026-11-24 (run 19:13 HST): 85 (+1), coverage back to 100, same 9 calibration failures; report-only. A helper name collided with the previous run's (seen_add_2026-11-24.py overwritten) - step 1.1 helper names now carry HHMM and are written behind a Test-Path gate. Usage 'salvage' keys in the thousands are 11-13/14 pre-14a data, gone from the window after 11-28.
