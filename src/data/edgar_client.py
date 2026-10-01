"""Shared SEC EDGAR HTTP client: User-Agent, rate limiting, retries, raw cache.

Every EDGAR request in the codebase (Bots 2 and 3) must go through this client, not
requests directly, so the 4 req/s limit and User-Agent policy are enforced in one
place.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from src.data import raw_cache
from src.utils.http import get_session
from src.utils.logging import get_logger

log = get_logger(__name__)

BASE_SUBMISSIONS = "https://data.sec.gov/submissions"
BASE_COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts"
BASE_FRAMES = "https://data.sec.gov/api/xbrl/frames"
BASE_ARCHIVES = "https://www.sec.gov/Archives/edgar"
CIK_LOOKUP_URL = "https://www.sec.gov/files/company_tickers.json"


class EdgarClient:
    """Rate-limited, cached client for SEC EDGAR endpoints.

    max_requests_per_second comes from config.edgar.max_requests_per_second (4, per
    SEC's published limit). raw_cache_dir should be config.paths.raw_cache_dir.
    """

    def __init__(
        self,
        raw_cache_dir: Path,
        sec_email: Optional[str] = None,
        max_requests_per_second: float = 4.0,
    ):
        self.raw_cache_dir = Path(raw_cache_dir)
        sec_email = sec_email or os.environ.get("SEC_EMAIL")
        if not sec_email:
            raise ValueError(
                "SEC_EMAIL must be set (env var or passed explicitly) before any "
                "EDGAR request -- SEC requires a contact email in the User-Agent."
            )
        self._min_interval = 1.0 / max_requests_per_second
        self._last_request_ts = 0.0
        self._session = get_session(browser_like=False)
        self._session.headers.update(
            {
                "User-Agent": f"swing-screener/1.0 ({sec_email})",
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def _get(self, url: str, use_cache: bool = True) -> bytes:
        if use_cache:
            cached = raw_cache.get(self.raw_cache_dir, url)
            if cached is not None:
                return bytes.fromhex(cached["data"])

        self._throttle()
        resp = self._session.get(url, timeout=15)
        resp.raise_for_status()
        content = resp.content

        if use_cache:
            raw_cache.put(self.raw_cache_dir, url, content.hex())
        return content

    def get_json(self, url: str, use_cache: bool = True) -> Any:
        import json

        return json.loads(self._get(url, use_cache=use_cache))

    def get_text(self, url: str, use_cache: bool = True) -> str:
        return self._get(url, use_cache=use_cache).decode("utf-8", errors="replace")

    def get_cik_map(self) -> dict[str, str]:
        """Ticker -> zero-padded 10-digit CIK string, from SEC's company_tickers.json."""
        data = self.get_json(CIK_LOOKUP_URL)
        result = {}
        for entry in data.values():
            ticker = entry["ticker"].upper()
            cik = str(entry["cik_str"]).zfill(10)
            result[ticker] = cik
        return result

    def get_submissions(self, cik: str) -> dict:
        """Full submissions JSON for a zero-padded CIK."""
        return self.get_json(f"{BASE_SUBMISSIONS}/CIK{cik}.json")

    def get_companyfacts(self, cik: str) -> dict:
        """Full companyfacts JSON for a zero-padded CIK."""
        return self.get_json(f"{BASE_COMPANYFACTS}/CIK{cik}.json")

    def get_frames(self, taxonomy: str, tag: str, unit: str, period: str) -> dict:
        """XBRL frames API: e.g. taxonomy="us-gaap", tag="Assets", unit="USD",
        period="CY2023Q4I"."""
        return self.get_json(f"{BASE_FRAMES}/{taxonomy}/{tag}/{unit}/{period}.json")

    def daily_index_url(self, day: date) -> str:
        return f"{BASE_ARCHIVES}/daily-index/{day.year}/QTR{(day.month - 1) // 3 + 1}/form.{day:%Y%m%d}.idx"

    def latest_published_daily_index(self, day: date, lookback_days: int = 10) -> tuple[date, str]:
        """Return (actual_date, index_text) for the most recently published daily
        form index on or before `day`, walking backward up to lookback_days. Raises
        if none found."""
        for offset in range(lookback_days + 1):
            candidate = day - timedelta(days=offset)
            url = self.daily_index_url(candidate)
            try:
                text = self.get_text(url)
                return candidate, text
            except Exception as exc:  # noqa: BLE001 -- try earlier day on any failure
                log.debug("daily index not available for %s (%s): %s", candidate, url, exc)
                continue
        raise RuntimeError(
            f"No published daily index found in the {lookback_days} days before {day}"
        )
