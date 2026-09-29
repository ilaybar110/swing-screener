# Follow-up connectivity test — 2026-09-29 13:12 UTC

| Test | Status | Key numbers | Duration |
|---|---|---|---|
| A Yahoo bulk | OK | 2725/2725 (100.0%), rate-limit errors: no | 352.0s |
| B Diagnosis | done | {'SYMBOL_FORMAT': 0, 'SHORT_HISTORY': 0, 'NO_DATA': 0}; after fmt fix 100.0%; ≥126d 100.0% | 0.0s |
| C Nasdaq hist | OK | 10/10 returned rows; price match 10/10 | 40.1s |

## Test A
Screener status: 200, tickers: 2725
First errors: []

## Test B
Failed in A: 0
Category counts: {'SYMBOL_FORMAT': 0, 'SHORT_HISTORY': 0, 'NO_DATA': 0}
Success rate after symbol-format fix: 2725/2725 = 100.0%
Success among tickers with ≥126 days history: 2657/2657 = 100.0% (NO_DATA tickers counted as failures; excluding them it is 100% by construction)
First errors: []

### SYMBOL_FORMAT (orig -> working, rows)

### SHORT_HISTORY (symbol, first date, rows, name, IPO year)

### NO_DATA (symbol — name — IPO year)

## Test C
Success 10/10; first errors: []
Captcha/HTML/403/429 notes: none

| Symbol | Group | Status | Rows | Range | Yahoo match |
|---|---|---|---|---|---|
| CMS | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| TTAN | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| AXTI | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| GPC | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| CDZIP | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| SAIC | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| PLSE | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| RCL | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| MTN | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |
| EXC | yahoo-ok | 200 | 251 | 2025-09-29..2026-09-28 | {'max_diff_pct': 0.0, 'ok': True} |

**Yahoo viable as primary price source from cloud: YES**
**Nasdaq historical viable as fallback: UNCERTAIN**
