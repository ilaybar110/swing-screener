"""Import/export between state/ CSVs (git-committed) and a SQLite DB (run.db or
research.db).

import_state(conn) loads state/ into an already-initialized (init_db'd) connection.
export_state(conn) writes the persisted subset of that connection back out to state/,
deterministically (sorted rows, stable columns, UTF-8, "\n" line endings) so git diffs
stay small and re-importing the export reproduces the same DB rows.

Retention windows and sharding are documented table-by-table below and in
docs/CONTRACTS.md. Tables not listed here (prices, fundamentals, filing_texts) are
ephemeral and never touch state/.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

# Fields kept in state/filings.csv -- enough for dedupe/processing, not the full row
# (primary_doc_url / exhibit_991_url are not persisted; they're re-derived on demand).
_FILINGS_FIELDS = [
    "accession", "cik", "ticker", "form", "filed_date", "acceptance_datetime",
    "items", "processed",
]

_RECOMMENDATIONS_FIELDS = [
    "id", "source", "signal_date", "ticker", "modules", "primary_module",
    "setup_score", "rs_pct", "overlap_score", "track_score", "total_score", "rank",
    "in_report", "entry", "stop", "target", "valid_until", "regime", "sector",
    "industry", "earnings_date", "earnings_in_window", "guardrail_status",
    "guardrail_reasons", "valuation_info", "rationale", "details", "llm_brief",
    "status", "current_r", "last_updated",
]

_RECOMMENDATION_RESULTS_FIELDS = [
    "rec_id", "final_status", "entry_date", "entry_fill", "exit_date",
    "avg_exit_price", "exit_reason", "r_multiple", "pct_return", "days_held",
    "mae_r", "mfe_r", "spy_return", "sector_etf_return", "excess_vs_spy",
    "excess_vs_sector",
]

_BASELINES_FIELDS = [
    "id", "parent_rec_id", "sample_no", "source", "signal_date", "ticker",
    "stop_pct", "status", "r_multiple", "pct_return", "exit_date", "exit_reason",
]

_TICKERS_FIELDS = [
    "ticker", "cik", "name", "exchange", "sector", "industry", "sic",
    "sector_etf", "is_active", "updated_at",
]

_UNIVERSE_SNAPSHOTS_FIELDS = ["snapshot_date", "ticker", "price", "market_cap", "adv20"]

_REGIME_LOG_FIELDS = ["date", "spy_close", "spy_sma200", "breadth_pct", "regime"]

_INSIDER_TRADES_FIELDS = [
    "accession", "row_num", "cik", "ticker", "insider_cik", "insider_name",
    "is_officer", "is_director", "is_ten_pct_owner", "officer_title",
    "trans_code", "trans_date", "shares", "price", "value", "acquired_disposed",
    "filed_date",
]

_EARNINGS_DATES_FIELDS = ["ticker", "event_date", "timing", "source", "acceptance_datetime"]

_FUNDAMENTALS_SUMMARY_FIELDS = [
    "as_of", "ticker", "revenue_ttm", "op_income_ttm", "fcf_ttm", "total_debt",
    "cash", "shares", "ev_fcf", "ev_fcf_sector_pct",
]

_JOB_LOG_FIELDS = ["run_id", "job", "trading_date", "started_at", "finished_at", "status", "message"]

# JSON-typed columns per table -- serialized with json.dumps(sort_keys=True) on
# export and json.loads on import so round-tripping is stable and diffable.
_JSON_COLUMNS = {
    "recommendations": {"modules", "guardrail_reasons", "valuation_info", "details", "llm_brief"},
}

INSIDER_TRADES_RETENTION_DAYS = 120
EARNINGS_DATES_RETENTION_DAYS = 180
FILINGS_RETENTION_DAYS = 120


def _csv_path(state_dir: Path, name: str) -> Path:
    return Path(state_dir) / f"{name}.csv"


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in fields})


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _serialize_json_columns(table: str, row: dict[str, Any]) -> dict[str, Any]:
    cols = _JSON_COLUMNS.get(table, set())
    out = dict(row)
    for col in cols:
        if col in out and out[col] is not None and out[col] != "":
            value = out[col]
            if isinstance(value, str):
                value = json.loads(value)
            out[col] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return out


def _deserialize_row_for_insert(table: str, row: dict[str, str]) -> dict[str, Any]:
    """CSV values are always strings; blank string means NULL for every column
    except JSON columns (where "" also means NULL, handled the same way)."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[k] = None if v == "" else v
    return out


def _rows_from_conn(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(query, params)
    return [dict(r) for r in cur.fetchall()]


def _month_shard(iso_date: str) -> str:
    return iso_date[:7]  # "YYYY-MM"


def _insert_rows(conn: sqlite3.Connection, table: str, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    placeholders = ", ".join(["?"] * len(fields))
    columns = ", ".join(fields)
    sql = f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r.get(f) for f in fields) for r in rows])
    conn.commit()


# ---------------------------------------------------------------------------
# import_state
# ---------------------------------------------------------------------------


def import_state(conn: sqlite3.Connection, state_dir: Path) -> None:
    """Load every state/ CSV into conn. conn must already have init_db() applied.
    Safe to call on an empty state/ directory (each table simply stays empty)."""
    state_dir = Path(state_dir)

    def load_simple(name: str, table: str, fields: list[str]) -> None:
        raw_rows = _read_csv(_csv_path(state_dir, name))
        rows = [_deserialize_row_for_insert(table, r) for r in raw_rows]
        _insert_rows(conn, table, fields, rows)

    load_simple("tickers", "tickers", _TICKERS_FIELDS)
    load_simple("universe_snapshots", "universe_snapshots", _UNIVERSE_SNAPSHOTS_FIELDS)
    load_simple("regime_log", "regime_log", _REGIME_LOG_FIELDS)
    load_simple("insider_trades", "insider_trades", _INSIDER_TRADES_FIELDS)
    load_simple("earnings_dates", "earnings_dates", _EARNINGS_DATES_FIELDS)
    load_simple("filings", "filings", _FILINGS_FIELDS)
    load_simple("fundamentals_summary", "fundamentals_summary", _FUNDAMENTALS_SUMMARY_FIELDS)
    load_simple("job_log", "job_log", _JOB_LOG_FIELDS)

    for shard_dir, table, fields in (
        ("recommendations", "recommendations", _RECOMMENDATIONS_FIELDS),
        ("recommendation_results", "recommendation_results", _RECOMMENDATION_RESULTS_FIELDS),
        ("baselines", "baselines", _BASELINES_FIELDS),
    ):
        dir_path = state_dir / shard_dir
        if not dir_path.exists():
            continue
        for csv_file in sorted(dir_path.glob("*.csv")):
            raw_rows = _read_csv(csv_file)
            # JSON columns (recommendations.modules etc.) arrive as already-
            # serialized JSON strings from export_state; sqlite stores them as TEXT.
            rows = [_deserialize_row_for_insert(table, r) for r in raw_rows]
            _insert_rows(conn, table, fields, rows)


# ---------------------------------------------------------------------------
# export_state
# ---------------------------------------------------------------------------


def export_state(conn: sqlite3.Connection, state_dir: Path, as_of: Optional[date] = None) -> None:
    """Write the persisted subset of conn back out to state/, deterministically.
    as_of controls retention-window cutoffs for insider_trades/earnings_dates/filings
    and defaults to date.today()."""
    state_dir = Path(state_dir)
    as_of = as_of or date.today()

    _export_simple(
        conn, state_dir, "tickers", "tickers", _TICKERS_FIELDS,
        order_by="ticker",
    )
    _export_simple(
        conn, state_dir, "universe_snapshots", "universe_snapshots",
        _UNIVERSE_SNAPSHOTS_FIELDS, order_by="snapshot_date, ticker",
    )
    _export_simple(
        conn, state_dir, "regime_log", "regime_log", _REGIME_LOG_FIELDS,
        order_by="date",
    )
    _export_simple(
        conn, state_dir, "job_log", "job_log", _JOB_LOG_FIELDS,
        order_by="run_id, job, trading_date",
    )

    insider_cutoff = (as_of - timedelta(days=INSIDER_TRADES_RETENTION_DAYS)).isoformat()
    _export_simple(
        conn, state_dir, "insider_trades", "insider_trades", _INSIDER_TRADES_FIELDS,
        where="filed_date >= ?", where_params=(insider_cutoff,),
        order_by="accession, row_num",
    )

    earnings_cutoff = (as_of - timedelta(days=EARNINGS_DATES_RETENTION_DAYS)).isoformat()
    _export_simple(
        conn, state_dir, "earnings_dates", "earnings_dates", _EARNINGS_DATES_FIELDS,
        where="event_date >= ?", where_params=(earnings_cutoff,),
        order_by="ticker, event_date, source",
    )

    filings_cutoff = (as_of - timedelta(days=FILINGS_RETENTION_DAYS)).isoformat()
    _export_simple(
        conn, state_dir, "filings", "filings", _FILINGS_FIELDS,
        where="filed_date >= ?", where_params=(filings_cutoff,),
        order_by="accession",
    )

    latest_row = conn.execute(
        "SELECT MAX(as_of) AS as_of FROM fundamentals_summary"
    ).fetchone()
    latest_as_of = latest_row["as_of"] if latest_row else None
    if latest_as_of:
        _export_simple(
            conn, state_dir, "fundamentals_summary", "fundamentals_summary",
            _FUNDAMENTALS_SUMMARY_FIELDS,
            where="as_of = ?", where_params=(latest_as_of,),
            order_by="as_of, ticker",
        )
    else:
        _write_csv(_csv_path(state_dir, "fundamentals_summary"), _FUNDAMENTALS_SUMMARY_FIELDS, [])

    _export_sharded(
        conn, state_dir, "recommendations", "recommendations",
        _RECOMMENDATIONS_FIELDS, date_field="signal_date", order_by="id",
    )
    _export_sharded_by_parent(
        conn, state_dir, "recommendation_results", "recommendation_results",
        _RECOMMENDATION_RESULTS_FIELDS, order_by="rec_id",
    )
    _export_sharded(
        conn, state_dir, "baselines", "baselines",
        _BASELINES_FIELDS, date_field="signal_date", order_by="id",
    )


def _export_simple(
    conn: sqlite3.Connection,
    state_dir: Path,
    csv_name: str,
    table: str,
    fields: list[str],
    order_by: str,
    where: Optional[str] = None,
    where_params: tuple = (),
) -> None:
    query = f"SELECT {', '.join(fields)} FROM {table}"
    if where:
        query += f" WHERE {where}"
    query += f" ORDER BY {order_by}"
    rows = _rows_from_conn(conn, query, where_params)
    rows = [_serialize_json_columns(table, r) for r in rows]
    _write_csv(_csv_path(state_dir, csv_name), fields, rows)


def _export_sharded(
    conn: sqlite3.Connection,
    state_dir: Path,
    shard_dir_name: str,
    table: str,
    fields: list[str],
    date_field: str,
    order_by: str,
) -> None:
    query = f"SELECT {', '.join(fields)} FROM {table} WHERE source = 'live' ORDER BY {order_by}"
    rows = _rows_from_conn(conn, query)
    rows = [_serialize_json_columns(table, r) for r in rows]

    shards: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        month = _month_shard(str(row[date_field]))
        shards.setdefault(month, []).append(row)

    shard_dir = state_dir / shard_dir_name
    shard_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in shard_dir.glob("*.csv")}
    written = set()
    for month, month_rows in sorted(shards.items()):
        fname = f"{month}.csv"
        _write_csv(shard_dir / fname, fields, month_rows)
        written.add(fname)
    # Remove shards that no longer have any live rows (e.g. a recommendation whose
    # source changed), so stale files don't linger.
    for stale in existing - written:
        (shard_dir / stale).unlink()


def _export_sharded_by_parent(
    conn: sqlite3.Connection,
    state_dir: Path,
    shard_dir_name: str,
    table: str,
    fields: list[str],
    order_by: str,
) -> None:
    """recommendation_results has no signal_date of its own; shard by the parent
    recommendation's signal_date month, live source only."""
    query = f"""
        SELECT {', '.join('r.' + f for f in fields)}
        FROM {table} r
        JOIN recommendations rec ON rec.id = r.rec_id
        WHERE rec.source = 'live'
        ORDER BY {order_by}
    """
    rows = _rows_from_conn(conn, query)
    month_query = "SELECT id, signal_date FROM recommendations WHERE source = 'live'"
    month_by_id = {r["id"]: _month_shard(str(r["signal_date"])) for r in _rows_from_conn(conn, month_query)}

    shards: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        month = month_by_id.get(row["rec_id"])
        if month is None:
            continue
        shards.setdefault(month, []).append(row)

    shard_dir = state_dir / shard_dir_name
    shard_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in shard_dir.glob("*.csv")}
    written = set()
    for month, month_rows in sorted(shards.items()):
        fname = f"{month}.csv"
        _write_csv(shard_dir / fname, fields, month_rows)
        written.add(fname)
    for stale in existing - written:
        (shard_dir / stale).unlink()
