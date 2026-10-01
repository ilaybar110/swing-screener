"""Telegram delivery (docs/PLAN.md section 12).

``send_report`` posts a short text summary and attaches the HTML report with
``sendDocument``; ``send_alert`` posts a plain text message. If ``TELEGRAM_BOT_TOKEN`` or
``TELEGRAM_CHAT_ID`` is missing the module logs that once and does nothing. Nothing in
here ever raises: failures are retried with backoff, then logged (with the bot token
scrubbed) and swallowed so a notification problem can never fail the pipeline.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

from src.utils.http import DEFAULT_TIMEOUT, TimeoutSession, get_session
from src.utils.logging import get_logger

log = get_logger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4000  # Telegram's hard limit is 4096
MAX_ATTEMPTS = 3
UPLOAD_TIMEOUT = 60
BACKOFF_SECONDS = 2.0

_warned_unconfigured = False


def _credentials() -> Optional[tuple[str, str]]:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if token and chat_id:
        return token, chat_id
    global _warned_unconfigured
    if not _warned_unconfigured:
        _warned_unconfigured = True
        log.info("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing); "
                 "skipping notifications")
    return None


def _scrub(text: object, token: str) -> str:
    return str(text).replace(token, "<token>")


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _post(
    session: Any,
    token: str,
    method: str,
    *,
    data: dict[str, Any],
    files: Optional[dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """POST one Bot API call with retries. Returns True on success, never raises.
    ``files`` values are ``(path, filename)`` so the file is re-opened on each attempt."""
    url = f"{API_BASE}/bot{token}/{method}"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        handles = []
        try:
            payload_files = None
            if files:
                payload_files = {}
                for field, (path, filename) in files.items():
                    fh = open(path, "rb")
                    handles.append(fh)
                    payload_files[field] = (filename, fh, "text/html")
            resp = session.post(url, data=data, files=payload_files, timeout=timeout)
            if resp.status_code == 200:
                return True
            retry_after = 0.0
            if resp.status_code == 429:
                try:
                    retry_after = float(resp.json().get("parameters", {}).get("retry_after", 0))
                except Exception:  # noqa: BLE001
                    retry_after = 0.0
            retryable = resp.status_code == 429 or resp.status_code >= 500
            log.warning("Telegram %s attempt %d/%d failed: HTTP %s %s", method, attempt,
                        MAX_ATTEMPTS, resp.status_code, _scrub(getattr(resp, "text", "")[:200], token))
            if not retryable:
                return False
            delay = max(retry_after, BACKOFF_SECONDS * attempt)
        except Exception as exc:  # noqa: BLE001 -- network errors, missing file, ...
            log.warning("Telegram %s attempt %d/%d failed: %s", method, attempt, MAX_ATTEMPTS,
                        _scrub(exc, token))
            delay = BACKOFF_SECONDS * attempt
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass
        if attempt < MAX_ATTEMPTS:
            sleep(min(delay, 30.0))
    return False


def _make_session() -> Any:
    # retries are handled in _post, so the shared session itself does not retry
    return TimeoutSession(get_session(total_retries=0), timeout=DEFAULT_TIMEOUT)


def send_alert(text: str, *, session: Optional[Any] = None,
               sleep: Callable[[float], None] = time.sleep) -> bool:
    """Send a plain text message. No-op (returns False) when Telegram is not configured
    or delivery fails; never raises."""
    try:
        creds = _credentials()
        if creds is None:
            return False
        token, chat_id = creds
        ok = _post(session or _make_session(), token, "sendMessage",
                   data={"chat_id": chat_id, "text": _truncate(text),
                         "disable_web_page_preview": "true"}, sleep=sleep)
        if not ok:
            log.error("Telegram alert not delivered")
        return ok
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram send_alert failed: %s", exc)
        return False


def send_report(html_path: Union[str, Path], summary_text: str, *, session: Optional[Any] = None,
                sleep: Callable[[float], None] = time.sleep) -> bool:
    """Send ``summary_text`` and attach the HTML report. No-op (returns False) when
    Telegram is not configured; never raises. The text is sent first so the summary
    still arrives if the upload fails."""
    try:
        creds = _credentials()
        if creds is None:
            return False
        token, chat_id = creds
        session = session or _make_session()
        text_ok = _post(session, token, "sendMessage",
                        data={"chat_id": chat_id, "text": _truncate(summary_text),
                              "disable_web_page_preview": "true"}, sleep=sleep)
        path = Path(html_path)
        if not path.is_file():
            log.error("Telegram: report file %s not found; summary only", path)
            return False
        doc_ok = _post(session, token, "sendDocument",
                       data={"chat_id": chat_id}, files={"document": (path, path.name)},
                       timeout=UPLOAD_TIMEOUT, sleep=sleep)
        if not (text_ok and doc_ok):
            log.error("Telegram report delivery incomplete (summary=%s, document=%s)", text_ok, doc_ok)
        return text_ok and doc_ok
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram send_report failed: %s", exc)
        return False
