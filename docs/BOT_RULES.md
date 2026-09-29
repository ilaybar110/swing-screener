# Bot Rules

1. Read `docs/PLAN.md`, `docs/CONTRACTS.md`, and `docs/OWNERSHIP.md` first, before
   writing any code.

2. Only edit files you own (see `docs/OWNERSHIP.md`). Never edit a shared/foundation
   file directly. If you need a change to one, append a request to
   `docs/CHANGE_REQUESTS.md` (who, what, why) and work around it locally with a stub
   in your own file in the meantime.

3. Implement the contract signatures in `src/contracts.py` exactly as documented
   there (same function name, same parameters, same return shape). Document any
   interpretation calls you had to make in your `docs/status/BOT_N.md`.

4. Test against `tests/fixtures/fixture.db` and `tests/fixtures/state/` (see
   `tests/conftest.py` for ready-made fixtures: `fixture_conn`, `writable_fixture_conn`,
   `data_access`). Never write to `data/research.db` or `state/` from a test. Any
   test that hits a live network endpoint must use 5 tickers or fewer and must be
   skippable (e.g. `@pytest.mark.skipif(not os.environ.get("RUN_NETWORK_TESTS"))`).

5. All EDGAR HTTP access goes through `src/data/edgar_client.py`. All other HTTP
   access goes through `src/utils/http.py` so throttling/retries stay consistent.

6. Point-in-time correctness is non-negotiable: nothing in the pipeline may use
   data published/filed after the `as_of` date it's being evaluated for. When in
   doubt, filter by `filed_date`/`acceptance_datetime`, not by the event date.

7. Code must run unmodified on both Windows (local dev) and Linux (cloud routine).
   Use `pathlib.Path` exclusively; no OS-specific shell calls or path separators.

8. Every module needs: type hints, a docstring, `logging` via
   `src.utils.logging.get_logger(__name__)` at meaningful steps, and `pytest` tests.

9. Commit only your own files: `git add <your paths>` (never `git add -A` / `.`).
   Prefix every commit message with `[Bot N]`. Do not push. If git reports
   `index.lock`, another bot is mid-commit — wait a few seconds and retry.

10. When your work is done, write `docs/status/BOT_N.md` covering: what you built,
    how to run it, what tests exist and how to run them, known issues, and any
    assumptions you made where the spec was ambiguous.
