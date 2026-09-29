"""Shared requests.Session factory with retries, backoff and sane headers.

Every HTTP call outside of edgar_client.py (which has its own rate limiting) should
go through get_session() so timeouts/retries stay consistent across data sources.
"""

from __future__ import annotations

from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 15  # seconds

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def get_session(
    total_retries: int = 3,
    backoff_factor: float = 0.5,
    browser_like: bool = False,
    extra_headers: Optional[dict] = None,
) -> requests.Session:
    """Build a requests.Session with retry/backoff on transient errors.

    browser_like=True adds browser-style headers, needed for endpoints (e.g.
    Nasdaq's API) that reject plain script user agents.
    """
    session = requests.Session()
    retry = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    if browser_like:
        session.headers.update(_BROWSER_HEADERS)
    if extra_headers:
        session.headers.update(extra_headers)

    return session


class TimeoutSession:
    """Thin wrapper that applies DEFAULT_TIMEOUT to every request unless the
    caller overrides it, so call sites don't have to repeat `timeout=...`."""

    def __init__(self, session: requests.Session, timeout: float = DEFAULT_TIMEOUT):
        self._session = session
        self._timeout = timeout

    def get(self, url: str, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs):
        kwargs.setdefault("timeout", self._timeout)
        return self._session.post(url, **kwargs)
