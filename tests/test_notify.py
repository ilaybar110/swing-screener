"""Telegram delivery with mocked HTTP: success, retries, no-op when unconfigured, never raises."""

from __future__ import annotations

import logging

import pytest

from notify import telegram


class FakeResponse:
    def __init__(self, status=200, text="ok", payload=None):
        self.status_code, self.text, self._payload = status, text, payload or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, data=None, files=None, timeout=None):
        body = None
        if files:
            name, fh, ctype = files["document"]
            body = (name, fh.read(), ctype)
        self.calls.append({"url": url, "data": data, "file": body, "timeout": timeout})
        r = self.responses.pop(0) if self.responses else FakeResponse()
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(telegram, "_warned_unconfigured", False)


def NOSLEEP(seconds):
    return None


def _methods(session):
    return [c["url"].rsplit("/", 1)[1] for c in session.calls]


def test_send_report_text_then_document(tmp_path):
    html = tmp_path / "2026-09-30.html"
    html.write_text("<html>report</html>", encoding="utf-8")
    s = FakeSession([FakeResponse(), FakeResponse()])
    assert telegram.send_report(html, "Regime: Favorable\nNew: 2", session=s, sleep=NOSLEEP) is True
    assert _methods(s) == ["sendMessage", "sendDocument"]
    assert s.calls[0]["url"].startswith("https://api.telegram.org/bot123:SECRET/")
    assert s.calls[0]["data"]["chat_id"] == "42" and "Regime: Favorable" in s.calls[0]["data"]["text"]
    assert s.calls[1]["file"][0] == "2026-09-30.html" and s.calls[1]["file"][1] == b"<html>report</html>"
    assert s.calls[1]["data"]["chat_id"] == "42"


def test_send_alert():
    s = FakeSession([FakeResponse()])
    assert telegram.send_alert("pipeline failed", session=s, sleep=NOSLEEP) is True
    assert _methods(s) == ["sendMessage"] and s.calls[0]["data"]["text"] == "pipeline failed"


def test_long_text_truncated():
    s = FakeSession([FakeResponse()])
    telegram.send_alert("x" * 10000, session=s, sleep=NOSLEEP)
    assert len(s.calls[0]["data"]["text"]) <= telegram.MAX_MESSAGE_CHARS


@pytest.mark.parametrize("missing", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
def test_unconfigured_is_silent_noop_logged_once(monkeypatch, tmp_path, caplog, missing):
    monkeypatch.delenv(missing)
    html = tmp_path / "r.html"
    html.write_text("x", encoding="utf-8")
    s = FakeSession([])
    with caplog.at_level(logging.INFO):
        assert telegram.send_report(html, "hi", session=s) is False
        assert telegram.send_alert("hi", session=s) is False
        assert telegram.send_report(html, "hi", session=s) is False
    assert s.calls == []
    assert sum("not configured" in r.getMessage() for r in caplog.records) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_retries_on_5xx_and_network_error_then_succeeds():
    s = FakeSession([FakeResponse(502), ConnectionError("boom"), FakeResponse(200)])
    sleeps = []
    assert telegram.send_alert("x", session=s, sleep=sleeps.append) is True
    assert len(s.calls) == 3 and len(sleeps) == 2


def test_429_honours_retry_after():
    s = FakeSession([FakeResponse(429, payload={"parameters": {"retry_after": 7}}), FakeResponse()])
    sleeps = []
    assert telegram.send_alert("x", session=s, sleep=sleeps.append) is True
    assert sleeps == [7.0]


def test_gives_up_after_max_attempts_without_raising():
    s = FakeSession([FakeResponse(500)] * 10)
    assert telegram.send_alert("x", session=s, sleep=NOSLEEP) is False
    assert len(s.calls) == telegram.MAX_ATTEMPTS


def test_client_error_not_retried():
    s = FakeSession([FakeResponse(400, text="Bad Request: chat not found")])
    assert telegram.send_alert("x", session=s, sleep=NOSLEEP) is False
    assert len(s.calls) == 1


def test_missing_html_still_sends_summary_and_does_not_raise(tmp_path):
    s = FakeSession([FakeResponse()])
    assert telegram.send_report(tmp_path / "nope.html", "summary", session=s, sleep=NOSLEEP) is False
    assert _methods(s) == ["sendMessage"]


def test_token_never_logged(caplog):
    err = ConnectionError("failed for https://api.telegram.org/bot123:SECRET/sendMessage")
    s = FakeSession([err, err, err])
    with caplog.at_level(logging.DEBUG):
        assert telegram.send_alert("x", session=s, sleep=NOSLEEP) is False
    assert caplog.records and not any("SECRET" in r.getMessage() for r in caplog.records)


def test_unexpected_exception_swallowed(monkeypatch, tmp_path):
    def bug(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(telegram, "_post", bug)
    html = tmp_path / "r.html"
    html.write_text("x", encoding="utf-8")
    assert telegram.send_report(html, "x", session=object()) is False
    assert telegram.send_alert("x", session=object()) is False
