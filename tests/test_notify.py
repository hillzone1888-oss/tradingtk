"""The notifier. Mostly a test that the bot token cannot escape.

The token lives in the request URL, and httpx puts the URL in its error text, so
the realistic leak is not a `print` anybody wrote on purpose — it is a stack
trace pasted into a chat six weeks from now. Every failure path is checked for
redaction, including `repr`.
"""

from __future__ import annotations

import httpx
import pytest

from tradetk.notify import NotifyError, TelegramNotifier, notifier_from_env
from tradetk.notify.telegram import CHAT_ENV, MAX_CHARS, TOKEN_ENV, truncate

TOKEN = "123456:SUPER-SECRET-TOKEN"


def notifier(**kw) -> TelegramNotifier:
    return TelegramNotifier(token=TOKEN, chat_id="42", **kw)


def transport(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})


# ── the token must not escape ──────────────────────────────────────


def test_repr_does_not_contain_the_token() -> None:
    assert TOKEN not in repr(notifier())
    assert "redacted" in repr(notifier())


def test_transport_errors_are_re_raised_without_the_token() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed connecting to {request.url}")

    with pytest.raises(NotifyError) as exc:
        notifier().send("hi", client=transport(boom))
    assert TOKEN not in str(exc.value)
    assert "redacted" in str(exc.value)


def test_error_bodies_are_re_raised_without_the_token() -> None:
    def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad token {TOKEN}")

    with pytest.raises(NotifyError) as exc:
        notifier().send("hi", client=transport(unauthorized))
    assert TOKEN not in str(exc.value)
    assert "401" in str(exc.value)


# ── delivery ───────────────────────────────────────────────────────


def test_sends_the_body_to_the_configured_chat() -> None:
    seen: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.update(_json.loads(request.content))
        seen["url"] = str(request.url)
        return ok(request)

    notifier().send("shadow sweep: 114 scored", client=transport(capture))
    assert seen["chat_id"] == "42"
    assert seen["text"] == "shadow sweep: 114 scored"
    assert f"/bot{TOKEN}/sendMessage" in seen["url"]


def test_a_rejected_message_is_an_error_even_on_http_200() -> None:
    """Telegram answers 200 with ok:false. Treating that as success would make
    a permanently broken chat id look like a working notifier forever."""

    def rejected(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "chat not found"})

    with pytest.raises(NotifyError, match="chat not found"):
        notifier().send("hi", client=transport(rejected))


def test_empty_messages_are_refused() -> None:
    with pytest.raises(NotifyError, match="empty message"):
        notifier().send("   ", client=transport(ok))


@pytest.mark.parametrize("bad", [{"token": "", "chat_id": "42"}, {"token": TOKEN, "chat_id": ""}])
def test_incomplete_credentials_are_refused_at_construction(bad: dict) -> None:
    with pytest.raises(NotifyError):
        TelegramNotifier(**bad)


# ── truncation ─────────────────────────────────────────────────────


def test_long_messages_are_truncated_visibly() -> None:
    out = truncate("x" * (MAX_CHARS * 2))
    assert len(out) <= MAX_CHARS
    assert "truncated" in out  # a silently halved digest reads as a whole one


def test_short_messages_are_untouched() -> None:
    assert truncate("hello") == "hello"


# ── missing credentials are a normal outcome, not a failure ────────


def test_missing_credentials_disable_notifications_with_a_reason() -> None:
    notifier_, reason = notifier_from_env({})
    assert notifier_ is None
    assert TOKEN_ENV in reason and CHAT_ENV in reason


def test_a_half_configured_environment_names_the_missing_one() -> None:
    notifier_, reason = notifier_from_env({TOKEN_ENV: TOKEN})
    assert notifier_ is None
    assert CHAT_ENV in reason and TOKEN_ENV not in reason


def test_whitespace_only_values_count_as_missing() -> None:
    """A pasted-wrong env var is usually blank, not absent."""
    notifier_, _ = notifier_from_env({TOKEN_ENV: "  ", CHAT_ENV: "42"})
    assert notifier_ is None


def test_a_complete_environment_builds_a_notifier() -> None:
    notifier_, reason = notifier_from_env({TOKEN_ENV: TOKEN, CHAT_ENV: "42"})
    assert isinstance(notifier_, TelegramNotifier)
    assert "enabled" in reason
