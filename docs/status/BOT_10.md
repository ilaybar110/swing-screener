# Bot 10 - recommendation chart ("why was this flagged")

## What it does
The dashboard's recommendation detail shows an interactive Plotly chart that explains the signal with the exact
levels, dates and values the module used, plus a "Why this setup" checklist (rule, actual value, threshold, pass/fail),
the setup-score components, the guardrail result and the ranking components.

* Base layer: candlesticks (what the module saw vs. later price action in a lighter style and shaded background),
  volume, entry / stop / target lines (price, %, R), signal / valid-until / next-earnings lines, and outcome markers
  (entry fill, 2R partial, final exit with reason, expired) replayed with the real `tracker.simulate`.
* Module layers (one on/off toggle per module on a multi-module recommendation): A SMA20/50, 52w high, swing high,
  pullback low, shaded pullback with volume dry-up vs the 20-day average, trigger day, RS-vs-SPY subpanel with its
  percentile; B earnings day (BMO/AMC), gap box, gap-day volume vs 50-day average, gap-day low support, consolidation
  box, breakout trigger; C every insider purchase (name, title, shares, price, value, filing date), cluster total,
  SMA20, trigger, swing low used for the stop; D base box, base high / midpoint, ATR subpanel with contraction ratio,
  breakout volume vs 1.5x average, breakout.

## Where the data comes from
* New modules store the annotation fields in `details` (additive only; see CHANGE_REQUESTS 2026-10-13).
* Recommendations saved earlier lack them. `dashboard/rec_data.py::ensure_annotations` re-runs the module's own
  `scan_ticker` on prices truncated at `signal_date` (never later) with stored RS percentile, earnings and insider
  data from state/, and the chart says "annotations recomputed". When the chart's prices were split-adjusted after the
  signal, annotations are always recomputed on those prices.

## Files
`dashboard/rec_data.py` (data, recompute, outcome), `rec_chart.py` (figure), `rec_explain.py` (checklist, score
components), `rec_view.py` (Streamlit layout; entry point `render(row, stamp, is_live)`).

## Tests
`tests/test_module_details_regression.py` (signals unchanged vs golden), `tests/test_rec_chart.py` (recompute equals
original details, point-in-time, figures build per module, checklist passes and score components sum to the score).

## Notes
* Regenerating `tests/fixtures/golden_candidates.json` is only legitimate when signal logic is deliberately changed.
* The fixture's insider trade prices are synthetic (far from market); the chart draws such markers at the day's close
  and says so in the tooltip.
