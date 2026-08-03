"""Outbound notifications — one direction only, and never to a venue.

A scheduled routine runs while nobody is watching. If it cannot tell anyone what
it found, the only way to learn that it has been failing for a week is to go
looking, which is exactly the thing nobody does. So the routines push.

**This package can only send text.** It holds no venue credentials, signs
nothing, and has no path to an order endpoint. A notifier that could also act
would be a second, unreviewed way for a scheduled job to reach the market.
"""

from tradetk.notify.telegram import (
    NotifyError,
    TelegramNotifier,
    notifier_from_env,
)

__all__ = ["NotifyError", "TelegramNotifier", "notifier_from_env"]
