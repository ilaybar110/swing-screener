# Swing Screener

Generates swing-trade recommendations for US stocks and tracks the hypothetical
outcome of every candidate automatically. See `docs/PLAN.md` for the full spec,
`docs/OWNERSHIP.md` for who owns which files, and `docs/BOT_RULES.md` for how the
build is being parallelized.

Full setup/run instructions (owned by Bot 7) land here once `run_daily.py` /
`run_weekly.py` / `run_backfill.py` exist.

## Quickstart (development)

```bash
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv/bin/activate on Linux
pip install -r requirements.txt
cp .env.example .env            # fill in SEC_EMAIL at minimum
python -m tests.fixtures.generate_fixtures   # only needed after changing fixtures
pytest
```
