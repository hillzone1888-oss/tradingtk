"""Send one message to Telegram. Used by the scheduled routines.

    uv run python -m tradetk.cli.notify --text "shadow sweep: 114 scored, 0 gated in"
    uv run python -m tradetk.cli.notify --file memory/DIGEST.md
    ... | uv run python -m tradetk.cli.notify --stdin

Exit code 0 means "handled": delivered, or skipped because no credentials are
configured. A routine must not fail because the push failed — its real output is
already committed to the repo. Exit code 1 is reserved for a *configured*
notifier that could not deliver, which is a real fault worth surfacing.
"""

from __future__ import annotations

import argparse
import json
import sys

import truststore

from tradetk.notify import NotifyError, notifier_from_env


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="tradetk.cli.notify", description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="Message body.")
    src.add_argument("--file", help="Read the body from this file.")
    src.add_argument("--stdin", action="store_true", help="Read the body from stdin.")
    ap.add_argument("--prefix", default="", help="Prepended to the body, e.g. a run label.")
    ap.add_argument("--json", action="store_true", help="Machine-readable result.")
    args = ap.parse_args(argv)

    truststore.inject_into_ssl()

    if args.text is not None:
        body = args.text
    elif args.file:
        with open(args.file, encoding="utf-8") as fh:
            body = fh.read()
    else:
        body = sys.stdin.read()
    if args.prefix:
        body = f"{args.prefix}\n{body}"

    notifier, reason = notifier_from_env()
    if notifier is None:
        result = {"sent": False, "reason": reason}
        print(json.dumps(result) if args.json else reason)
        return 0  # not a failure: the run's real output is in the repo

    try:
        notifier.send(body)
    except NotifyError as exc:
        result = {"sent": False, "reason": str(exc)}
        print(json.dumps(result) if args.json else f"notify failed: {exc}", file=sys.stderr)
        return 1

    result = {"sent": True, "chars": len(body), "chat_id": notifier.chat_id}
    print(json.dumps(result) if args.json else f"sent {len(body)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
