"""Apprise dispatch: JSON webhook payloads and URL masking."""

from __future__ import annotations

import json

import httpx
import respx

from pitwall.notify.dispatch import mask_url, notify
from pitwall.notify.message import Message


def test_json_webhook_gets_pitwall_payload():
    msg = Message(
        type="reminder",
        title="🏁 Race in 1 hour",
        text="Race — Mexico City GP",
        data={"session": "RACE"},
        url="https://pitwall.example/events/3",
    )
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://hooks.example.com/pitwall").mock(
            return_value=httpx.Response(200)
        )
        ok, _ = notify("webhook", msg, ["jsons://hooks.example.com/pitwall"])
    assert ok
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "type": "reminder",
        "title": "🏁 Race in 1 hour",
        "text": "Race — Mexico City GP",
        "url": "https://pitwall.example/events/3",
        "data": {"session": "RACE"},
    }


def test_webhook_failure_reported():
    msg = Message(type="results", title="t", text="x")
    with respx.mock() as mock:
        mock.post("http://hook.local:8123/api").mock(return_value=httpx.Response(500))
        ok, _ = notify("webhook", msg, ["json://hook.local:8123/api"])
    assert not ok


def test_no_urls_configured():
    ok, detail = notify("email", Message(type="test", title="t", text="x"), [])
    assert not ok and "no Apprise URLs" in detail


def test_invalid_url_rejected():
    ok, detail = notify("webhook", Message(type="test", title="t", text="x"), ["nope://"])
    assert not ok and "invalid" in detail


def test_mask_url_hides_credentials():
    masked = mask_url("mailtos://me%40gmail.com:app-password@smtp.gmail.com?to=me@gmail.com")
    assert "app-password" not in masked and masked.startswith("mailtos://smtp.gmail.com")
    assert "abc123" not in mask_url("discord://1234/abc123")


def test_log_redaction_covers_message_and_extras(capsys):
    import json as _json
    import logging

    from pitwall.logging_setup import configure_logging

    configure_logging("INFO", ["hunter2-secret", "mailtos://u:pw@smtp.example.com"])
    logging.getLogger("t").warning(
        "password is %s", "hunter2-secret", extra={"url": "mailtos://u:pw@smtp.example.com"}
    )
    line = _json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["msg"] == "password is ***" and line["url"] == "***"
    logging.getLogger().handlers.clear()


def test_cli_test_command(engine, monkeypatch, capsys):
    from pitwall.__main__ import send_test

    with respx.mock() as mock:
        mock.post("https://hooks.example.com/pitwall").mock(return_value=httpx.Response(200))
        assert send_test("webhook") == 0
    assert "Test webhook: sent" in capsys.readouterr().out
    monkeypatch.setenv("APPRISE_URLS_EMAIL", "")
    from pitwall.config import get_env

    get_env.cache_clear()
    assert send_test("email") == 1
