"""Gzipped raw-download cache, keyed by an arbitrary string (typically the URL).

Locally this is persistent under config.paths.raw_cache_dir (data/raw/), so re-runs
don't re-download unchanged history. In the cloud, data/ is gitignored and the
directory is fresh every run, so the cache is effectively per-run only -- same code,
different lifetime.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _key_to_path(cache_dir: Path, key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return cache_dir / digest[:2] / f"{digest}.json.gz"


def get(cache_dir: Path, key: str) -> Optional[dict[str, Any]]:
    """Return {"fetched_at": iso str, "data": ...} if key is cached, else None."""
    path = _key_to_path(Path(cache_dir), key)
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def put(cache_dir: Path, key: str, data: Any) -> None:
    """Store data under key, stamped with the current UTC time."""
    path = _key_to_path(Path(cache_dir), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": data}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)


def get_or_fetch(cache_dir: Path, key: str, fetch_fn) -> Any:
    """Return cached data for key if present, else call fetch_fn(), cache the
    result, and return it."""
    cached = get(cache_dir, key)
    if cached is not None:
        return cached["data"]
    data = fetch_fn()
    put(cache_dir, key, data)
    return data
