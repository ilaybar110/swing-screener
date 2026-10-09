"""R2: the daily run processes at most daily.max_catchup_days missed sessions (Bot 9)."""

from __future__ import annotations

from datetime import date


def test_catch_up_is_limited_and_reported(env):
    import run_daily
    from tests.e2e.conftest import now_after, seed_last_day

    e = env
    e.cfg.daily.max_catchup_days = 2
    seed_last_day(e.state, date(2024, 12, 20))
    now = now_after(date(2024, 12, 26))  # missed: 12-23, 12-24, 12-26
    assert run_daily.prepare(e.cfg, now=now) == 0
    assert e.calls.edgar == [date(2024, 12, 23), date(2024, 12, 24)]  # oldest first, 2 only

    assert run_daily.finalize(e.cfg, now=now) == 0
    md = (e.reports / "2024-12-24.md").read_text(encoding="utf-8")
    html = (e.reports / "2024-12-24.html").read_text(encoding="utf-8")
    assert "Catch-up in progress, 1 day remaining" in md and "Catch-up in progress, 1 day remaining" in html

    e.calls.edgar.clear()  # the next run picks up the rest and the note disappears
    assert run_daily.prepare(e.cfg, now=now) == 0
    assert e.calls.edgar == [date(2024, 12, 26)]
    assert run_daily.finalize(e.cfg, now=now) == 0
    assert "Catch-up in progress" not in (e.reports / "2024-12-26.md").read_text(encoding="utf-8")
