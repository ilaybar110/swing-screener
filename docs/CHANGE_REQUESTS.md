# Change Requests

Append-only log of requested changes to shared/foundation files. A bot that needs a
change to a file it doesn't own adds an entry here (never edits the file directly)
and works around it locally in the meantime. Bot 0 (or whoever currently owns the
target file) reviews and applies.

Template:

```
## YYYY-MM-DD — Bot N
File: path/to/file
Request: what you need changed
Why: what breaks/is-blocked without it
Status: open | applied | declined (+ reason)
```

---

## 2026-10-01 — Bot 4
File: tests/fixtures/generate_fixtures.py
Request: In `build_module_b_trigger` (and `build_negative_b`), make the gap day's *open* gap, not only the close. `_ohlcv_from_close` derives `open[i]` from `close[i-1]`, so MOMB1's 2024-12-03 bar opens ~0.3% above the prior close even though it closes +7.8%. Set `open[gap_idx] = close[gap_idx-1] * 1.07` (NEGB1: `* 1.03`) after building the OHLCV frame, and keep `low <= open <= high`.
Why: PLAN.md 7-B (and the Bot 4 brief) require the earnings session to *open* >= 5% above the prior close. With the fixture as shipped, Module B correctly does not fire on MOMB1. Bot 4's tests patch a private copy of fixture.db (open of MOMB1 +7%, NEGB1 +3% on 2024-12-03) so MOMB1 triggers on 2024-12-10; once regenerated these patches become no-ops.
Status: open

## 2026-10-01 — Bot 4
File: config.yaml / src/config.py
Request: Add the module parameters that the strategy spec fixes but config does not yet carry (all currently defaulted as constants at the top of each src/modules/*.py file, read via `getattr(section, name, default)` so adding them to config takes effect with no code change):
- a_momentum_pullback: `pullback_touch_window: 5`, `pullback_touch_pct: 0.02`, `pullback_close_floor: 0.97`, `sma_slope_sessions: 10`, `stop_atr_buffer: 0.1`
- b_earnings_gap_drift: `tight_range_atr_multiple: 1.5`, `stop_atr_buffer: 0.1`
- c_insider_cluster: `completion_window_sessions: 20`, `stop_lookback_sessions: 10`
- d_base_breakout: `contraction_ratio: 0.7`
Why: "All parameters come from config.yaml" -- these are the remaining hard-coded numbers. (Existing keys already cover RS top 20% = pct >= 80, 75% of 52w high, and base 3-8 weeks = 15-40 sessions.)
Status: open
