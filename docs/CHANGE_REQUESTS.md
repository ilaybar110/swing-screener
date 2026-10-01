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

## 2026-10-01 — Bot 3
File: .gitignore
Request: Change the `data/` line to `/data/` (anchored to the repo root).
Why: the unanchored pattern also ignores `src/data/`, so NOTHING under `src/data/` is tracked by git -- including Bot 0's `edgar_client.py`/`raw_cache.py` and every data module from Bots 1-3 (`git status` doesn't even list them). A fresh clone in the cloud routine would have no `src/data/` package at all. Bot 3 commits its own files there with `git add -f` as a workaround; Bots 0/1/2 files under `src/data/` are still untracked.
Status: open

## 2026-10-01 — Bot 3
File: src/data/edgar_client.py
Request: Add a streaming download method, e.g. `download_file(url, dest_path, resume=True)`, that goes through the client's session/User-Agent/throttle and writes to disk with HTTP Range resume (no in-memory/raw-cache buffering).
Why: `submissions.zip` is ~1.5 GB; `_get` buffers the whole body and hex-encodes it into the raw cache. `edgar_bulk.download_file` is a local workaround that uses the client's `_session` and `_throttle()` directly.
Status: open

## 2026-10-01 — Bot 6
File: src/stats.py
Request: `summary(conn, "live")` raises `KeyError: 'id'` (line ~195, `base[base["parent_rec_id"].isin(sub["id"])]`) when recommendations exist but none has closed yet (empty `closed` frame loses its columns after the `.map(...)` filter, so `sub["id"]` fails). Please make the per-module `contains` loop robust to an empty `closed` (e.g. skip modules when `closed.empty`, or build `sub` with `closed.loc[mask]`).
Why: This is the normal state for the first days/weeks of live running. Bot 6's report works around it with a counts-only fallback, so the report never fails, but the module/baseline table is blank until fixed.
Status: open
