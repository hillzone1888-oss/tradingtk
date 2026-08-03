"""Telegram push — the only channel a routine has to reach a human.

Deliberately small and deliberately dumb: format a string, POST it, report
whether it landed. Everything interesting happens before this module is called.

**Credentials come from the environment, never from a file.** A scheduled cloud
run clones the repo; anything written into the repo to make notifications work
would be a token in version control. `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` are set on the cloud environment instead, and the names are
exported as constants here so the routine prompts can quote them character for
character — mismatched env var names are the single most common way this kind of
setup fails silently.

**A missing token is not an error.** :func:`notifier_from_env` returns ``None``
with a reason, so a routine that cannot notify still does its real work and
still commits its output to the repo. Losing the push is an inconvenience;
skipping the run because of it would be a self-inflicted outage.

**The token never appears in a log, an exception, or a repr.** httpx puts the
request URL in its error messages and the Telegram token lives *in the URL*, so
every failure path here is caught and re-raised with the URL redacted. That is
not paranoia about this project's secrets hygiene — it is that the one place a
token reliably leaks is a stack trace someone later pastes somewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

#: Environment variable names. Quoted verbatim in the routine prompts — if these
#: two strings and the cloud environment disagree by one character, the routine
#: runs fine and simply never tells anybody anything.
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"

API_BASE = "https://api.telegram.org"

#: Telegram's hard limit for a single message.
MAX_CHARS = 4096

#: Room for the truncation marker itself.
_TRUNC_NOTE = "\n\n… truncated; the full text is in the repo."


class NotifyError(Exception):
    """The message could not be delivered. Never carries the token."""


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


def truncate(text: str, limit: int = MAX_CHARS) -> str:
    """Cut to Telegram's limit, saying so rather than silently losing the tail.

    A digest that is quietly cut in half reads like a complete digest, which is
    worse than an obviously truncated one — the reader has no way to know that
    the interesting item was item eleven.
    """
    if len(text) <= limit:
        return text
    keep = limit - len(_TRUNC_NOTE)
    return text[:keep].rstrip() + _TRUNC_NOTE


@dataclass(frozen=True)
class TelegramNotifier:
    """A bound (token, chat) pair that can send text and nothing else."""

    token: str
    chat_id: str
    base_url: str = API_BASE
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.token:
            raise NotifyError("empty bot token")
        if not self.chat_id:
            raise NotifyError("empty chat id")

    def __repr__(self) -> str:  # never print the token, not even in a traceback
        return f"TelegramNotifier(chat_id={self.chat_id!r}, token=<redacted>)"

    def send(self, text: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        """POST one message. Raises :class:`NotifyError` on any failure."""
        if not text.strip():
            raise NotifyError("refusing to send an empty message")
        url = f"{self.base_url}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": truncate(text),
            "disable_web_page_preview": True,
        }
        owned = client is None
        client = client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise NotifyError(f"telegram request failed: {_redact(str(exc), self.token)}") from None
        finally:
            if owned:
                client.close()

        if response.status_code != 200:
            body = _redact(response.text[:300], self.token)
            raise NotifyError(f"telegram returned {response.status_code}: {body}")
        try:
            data = response.json()
        except ValueError:
            raise NotifyError("telegram returned a non-JSON body") from None
        if not data.get("ok"):
            raise NotifyError(f"telegram rejected the message: {data.get('description')!r}")
        return data


def notifier_from_env(env: dict[str, str] | None = None) -> tuple[TelegramNotifier | None, str]:
    """Build a notifier from the environment.

    Returns ``(notifier, reason)``. A ``None`` notifier is a normal outcome with
    an explanation attached, not a failure — the caller logs the reason, writes
    its output to the repo as usual, and carries on.
    """
    source = os.environ if env is None else env
    token = (source.get(TOKEN_ENV) or "").strip()
    chat = (source.get(CHAT_ENV) or "").strip()
    missing = [name for name, value in ((TOKEN_ENV, token), (CHAT_ENV, chat)) if not value]
    if missing:
        return None, (
            f"notifications disabled: {' and '.join(missing)} not set in the "
            "environment (the names must match character for character)"
        )
    return TelegramNotifier(token=token, chat_id=chat), "notifications enabled"
