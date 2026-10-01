# Bot 4 (Strategy modules, ranking, trade plan) — status

## What was built

- `src/indicators.py` — SMA, ATR(14), 52-week high, average volume, 6-month RS
  `(close[t-21]/close[t-126]) / (SPY same) - 1`, within-universe RS percentile, price-panel
  loading, universe-membership helpers, stop-distance rule.
- `src/modules/` — `a_momentum_pullback.py`, `b_earnings_gap_drift.py`,
  `c_insider_cluster.py`, `d_base_breakout.py`, shared scaffolding in `base.py`, and
  `get_enabled_modules(config)` in `__init__.py`. Each implements `scan(as_of, data)` and a
  vectorized `scan_history(start, end, data)`.
- `src/trade_plan.py` — `build(candidates, data, config, ...) -> Recommendation` (merge
  same-day candidates, target = entry + 2R, `valid_until` = 5 trading days, earnings
  flags, rationale, details, status `PENDING`) and `plan_levels(candidate, data, config)
  -> dict` (the dict-shaped signature in the `contracts.py` stub).
- `src/ranking.py` — `rank(candidates, as_of, data, conn, config=None, source="live")`.

## How scan == scan_history is guaranteed

All indicators are causal (rolling windows with `min_periods == window`; ATR is a simple
rolling mean of true range, not Wilder, so it doesn't depend on where the series starts).
`scan(as_of)` runs the same vectorized code as `scan_history` with prices loaded only up to
`as_of` (nothing later is ever read) and the evaluation range collapsed to that day.
Insider filings are cut off per candidate day by *filing* date. Tests prove: day-by-day
equality for every module, and that scrambling all prices/adding filings after `as_of`
doesn't change `scan(as_of)`.

## How to run / test

```bash
pytest tests/test_modules_scan.py tests/test_indicators.py tests/test_trade_plan.py tests/test_ranking.py -q
```
(~1 min; the day-by-day scan tests dominate.) Quick manual use:

```python
from src.config import load_config; from src.modules import get_enabled_modules
from src.data_access import DataAccess; from src.ranking import rank
cands = [c for m in get_enabled_modules(cfg) for c in m.scan(as_of, data)]
recs = rank(cands, as_of, data, conn)
```

## Fixture results (expected dates)

| Module | Fires on | Doesn't fire on |
|---|---|---|
| A | MOMA1 2024-12-31 | NEGA1 (in the engineered window) |
| B | MOMB1 2024-12-10 (**needs the patched gap open, see below**) | NEGB1 |
| C | MOMC1 2024-11-18 | NEGC1 |
| D | MOMD1 2024-12-24 | NEGD1 |

Random-walk controls do occasionally fire module A (it is a loose, spec'd screen); the
tests only assert the designated/negative tickers.

## Interpretation calls where the spec was ambiguous

General: entry = trigger day's high; stop must be below entry with distance 2–12 % of entry.
`rs_top_pct 0.20` → RS percentile ≥ 80; `pct_of_52w_high_max 0.25` → close ≥ 0.75 × 52w high
(52w high = highest *high* over 252 sessions); `base_*_weeks` 3–8 → 15–40 sessions. A ticker
needs a full 252 sessions of history to have a 52-week high (live download is ~300).

- **A**: "last 5 sessions" = the 5 ending on the trigger day (inclusive). Swing high = highest
  high of the 20 sessions *before* the trigger day; pullback = sessions after it up to the day
  before the trigger (≥ 3). Pullback volume is compared with the 20-day average ending the
  day before the trigger. "Within 2 % of SMA" counts lows at or below `1.02 × SMA`. Score =
  40 % RS pct + 30 % volume dry-up + 30 % shallowness (depth in ATR, 100 at ≤ 1 ATR → 0 at 6).
- **B**: gap = `open[g]/close[g-1] - 1` (true opening gap). Reaction session: `before_open`
  → event date; `after_close` → next session; unknown timing → whichever qualifies. Volume
  baseline and "pre-gap ATR" use the sessions ending the day before the gap. Consolidation =
  the 2–5 sessions after the gap day; trigger = the session after it; first trigger per gap
  only. The stop uses the ATR at the trigger day.
- **C**: qualifying trades = code P, officer or director, not flagged 10 % owner, value ≥ $50K
  each. Cluster = trades whose trade dates span ≤ 30 days, ≥ 2 distinct insiders, total ≥ $250K,
  using only trades *filed* by the evaluation day. Completion = the filing date on which that
  first became true (top-ups of the same cluster don't re-fire). Trigger must be within 20
  sessions of completion. Seniority: CEO/CFO/President/Chair = 1, other officer 0.6, director 0.4.
- **D**: base = the L sessions (15–40) ending the day before the trigger; contraction = ATR14 at
  base end < 0.7 × ATR14 on the session before the base; any qualifying L fires, longest is
  reported. Volume baseline = 50-day average ending the day before.
- **Ranking**: percentiles = `rank(pct=True) × 100` among the day's surviving candidates
  (a lone candidate gets 100). Overlap score is the configured 0/70/100 mapping used directly
  as the component. Track score for a module = `clip(50 + 25 × mean R, 0, 100)` over its
  closed live results (entered trades only, exit ≤ as_of, attributed by `primary_module`);
  unlocks at ≥ 30 and then takes 10 % with the other weights scaled by 0.9. The industry cap
  uses `config.ranking.max_per_industry`; a capped-out name doesn't consume a report slot;
  tickers without an industry aren't capped. `rank` returns every guardrail survivor.
  Setup/RS/overlap/track components are stored in `details["score_components"]`.
- **Earnings flag**: next event on/after the signal date whose acceptance time isn't after it
  (point-in-time). Consequently backtests only see earnings dates from calendar-type rows,
  not 8-K-derived ones that were filed later — conservative, avoids lookahead.
- `Recommendation.status` is `"PENDING"` as requested (`TrackResult`/tracker use lower-case
  `"pending"` — Bot 5 should normalise).
- Guardrail: `src.guardrail.evaluate` is imported lazily; until Bot 2's file exists (or if it
  raises) the result is "unknown". Only "fail" drops a recommendation.

## Known issues / change requests (in `docs/CHANGE_REQUESTS.md`)

1. **Fixture MOMB1 has no opening gap** (open ≈ prior close, +7.8 % close-to-close). Module B
   correctly doesn't fire on the shipped fixture; its tests run on a tmp copy with the gap-day
   open patched (+7 % / NEGB1 +3 %). Needs a `generate_fixtures.py` fix.
2. Module parameters not carried by `config.yaml` (pullback 5 sessions / 2 % / 0.97, SMA slope
   10, 0.1 ATR stop buffer, tight-range 1.5 ATR, cluster completion 20 sessions, stop lookback
   10, contraction 0.7) are module-level defaults, read through `getattr` so adding them to
   config takes effect without code changes.
3. Performance: each module loads its own price panel (one `get_prices` per ticker). Fine for
   fixtures and a daily run; for large backtests Bot 5 may want to batch by month.
