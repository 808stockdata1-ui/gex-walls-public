# GEX Walls / StrategyScanner — public research copy

An options scanner and gamma-exposure dashboard that runs locally on one Windows PC. This is the
**public copy**: the code, the scanner's complete pick history, and an audit of how accurate it has
actually been. The owner's positions, account keys and notification settings are not here and never
will be — they live in a private repo.

**It is published so other models and people can find what is wrong with it.** Start with the audit.

## Start here

| file | what it is |
|---|---|
| **[AUDIT-2026-11-25.md](AUDIT-2026-11-25.md)** | The accuracy audit. Read this first. |
| **[OPEN-QUESTIONS.md](OPEN-QUESTIONS.md)** | What would most change the answer, ranked. |
| [AGENTS.md](AGENTS.md) | Rules for agents working on this repo. |
| [ARENA.md](ARENA.md) | How competing versions are scored against each other. |
| `data/picks.csv.gz` | Every pick the scanner logged: 9,456 rows, 3,478 settled, 2026-10-19 on. |
| `data/ticker_daily.csv.gz` | Daily per-ticker features the picks were made from. |
| `analysis/` | Scripts that reproduce every number in the audit. |

## The audit in three lines

Over 3,478 settled picks the scanner won **71%** of its trades and **lost 10.1% of capital per
trade**, because the average win was $47 and the average loss $211. It ranked candidates by
probability of profit and never compared what a trade wins against what it loses, so **56% of its
own picks had negative expected value at its own probabilities**. One expiration date,
2026-11-20, accounts for −$99,879 of the −$97,193 total: 1,217 positions and $367,900 of capital on
a single date with 68% of the risk pointing the same way.

```bash
python analysis/audit_picks.py data/picks.csv.gz   # reproduces it all
python analysis/verify_ev.py                       # parity + Monte Carlo checks on the EV engine
```

## What this is not

Not advice, not a product, and not a strategy anybody should copy. The track record above is a
record of **losses**. 26 expirations of data, one of which decides the result, cannot establish that
any version of this works — see the audit's section 6 for what the data does and does not support.

## Layout

```
gexwalls/          the dashboard: one Python file, no framework, serves a single page
strategyscanner/   the scanner: candidate generation, ranking, expected value, learning loop
  scanner/ev.py      expected value in closed form (the 2026-11-25 fix)
  scanner/ranker.py  filters and ranking
  scanner/learn.py   settlement, calibration, walk-forward model selection
analysis/          reproduce the audit
data/              the pick history
arena/             scoring for competing versions
```

Data sources are free ones only: Schwab's API with the owner's own login for real-time chains, CBOE's
public delayed files (which began refusing scripted requests in November 2026), SEC EDGAR, FINRA
short volume, Nasdaq Trader listings.

## Licence and provenance

Code here is the owner's own work plus contributions from several AI models working under
`AGENTS.md`. `gexwalls/lightweight-charts.standalone.production.js` is TradingView's, under its own
licence file alongside it. No licence is granted or implied for anything else; ask first.
