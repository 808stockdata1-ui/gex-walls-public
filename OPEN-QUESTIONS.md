# Open questions

Ranked by how much the answer would change what this project does. Evidence over opinion: the
scoring rules in `ARENA.md` apply, and `data/picks.csv.gz` is the shared yardstick. If you can
settle one of these, that is worth more than any new feature.

Everything here follows from `AUDIT-2026-11-25.md`. Read it first.

## 1. Is there a volatility risk premium on this universe at all?

Over the measured period the median `iv30/rv20` was **1.08** while realised moves came in at
**1.00× implied** and **1.71× the rv20 forecast**. Only 3% of picks had enough implied premium to
cover the movement that actually turned up.

If short premium has no edge on this universe, then better filtering is the wrong fix and the project
needs a different strategy. The honest test needs more than five weeks and more than one regime.

**What would settle it:** forward/trailing volatility and implied/realised ratios across several
years and several regimes, by liquidity bucket and by sector. `analysis/volcheck.py` does the first
half of this on 110,318 observations from two years of cached daily bars — extend it to implied.

## 2. Concentration: what is the right cap?

Nothing in the scanner limits how much capital lands on one expiration date, one ticker, or one
direction. The record put $367,900 on 2026-11-20 with 68% pointing one way and lost $99,879 of it.

The dashboard now caps the *tracked* picks at 2 per ticker and 6 per expiration, and shows where the
risk sits — both numbers were chosen by judgement, not measured.

**What would settle it:** simulate the record under per-expiry, per-ticker and per-direction caps and
report the distribution of outcomes, not the mean. The question is the shape of the worst week.

## 3. Directional drift

Mean realised move over the period was **−0.86 sigma**. A symmetric lognormal cannot see that, so
every put-side probability was wrong in the same direction at the same time.

**What would settle it:** does conditioning on trend, or on the GEX positioning this project already
computes, improve out-of-sample Brier score against a symmetric model? Walk-forward only.

## 4. Is `pop_cons = min(pop_iv, pop_rv)` conservative enough?

It was still 7.3 points overconfident overall and **24 points** overconfident in the 70–75% bucket
specifically. `learn.choose_calibration` will switch on a group shift once 20 days are scored, which
is honest machinery but corrects the average, not that bucket.

## 5. Fill realism

`slippage: 0.5` (a quarter of the spread) is a guess, and until 2026-11-25 commissions were not
modelled at all. Fills are the difference between a marginal edge and no edge.

**What would settle it:** the distribution of actual fills against mid, by spread width and
liquidity. The owner's own fills are in the private repo; a contributor could instead measure
quoted spreads at the moment of the pick against the next trade print.

## 6. Does the expected-value gate survive contact with other data?

The gate is justified by arithmetic — a trade with negative expected value at the model's own
probability is a logical defect. It is **not** justified by the backtest: on the settled picks it
cuts the total loss from −$103,781 to −$36,286 but barely moves the per-trade average
(−$37.14 → −$34.82) with a clustered CI of ±$2,162, and at every stress level tested the surviving
picks still lost money.

**What would settle it:** the same gate on an independent options dataset, or a longer forward test.

## 7. Tail hedging

If the payoff really is "small gains most weeks, one expiration that takes it all back", does a cheap
long-wing overlay improve the geometric return? Most such overlays lose money on average — the
question is whether the drag is smaller than the tail it removes, which depends entirely on price.

## Things that are already known and do not need re-deriving

- `rv20` is median-unbiased over two years (0.998) — it is the right-skew (mean 1.12, RMS 1.36) that
  matters for expected value, not a bias in the median.
- Expected value at **implied** volatility is zero by no-arbitrage. Any gate built on it rejects
  everything; this was tried and reverted.
- Expected cost is **not monotone** in volatility. Stressing volatility in one direction made short
  call spreads look better the more the stock moved. `ev.py` now takes the worst of
  {rv/stress, rv, rv×stress}.
- Risk-neutral drift gives long LEAPS calls +8% of spot before they do anything. `ev.py` uses zero
  drift for this reason.
