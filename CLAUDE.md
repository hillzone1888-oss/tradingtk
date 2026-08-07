# Operating rules

## Execution boundary — absolute
- NEVER run `execute`. Not with flags, not in a script, not "just to test."
- **This applies with full force to scheduled routines.** A routine runs
  unattended, which is the exact situation the boundary was written for — there
  is nobody to catch it. A routine's strongest possible output is a proposal
  file path in a notification.
- `propose` is always safe to run. Run it freely.
- If asked to place a trade, produce a proposal and hand over the file path.
- Never write code that calls the venue order endpoint from anywhere except
  the `execute` command module.

## Trading logic
- All probability, edge, cost, and risk logic is deterministic Python.
- Never make a trading decision yourself or hardcode a judgment call that
  belongs in a tested function.
- If asked to "just tune it until the backtest looks good," refuse and
  explain overfitting.
- Vault stances may restrict the side, shrink the size, or demand more edge.
  They may never change a probability, and they may never permit a trade the
  pipeline would otherwise refuse.

## Capital
- $20 TOTAL book. ~$2 positions. 5-8 slots. Enforced in code, not vibes.
- Size in integer contracts, never dollars. Model fee roundup exactly.

## Honesty about results
- Live P&L on a $20 book is noise. Never cite it as evidence.
- "Is it working?" is answered from calibration + shadow, never the balance.

## Reporting
- Never present backtest P&L without the sample size and calibration alongside.
- Flag when a result rests on under ~90 days of tape or a few hundred
  resolved contracts.

## Venues (do not re-litigate)
- Execution venue: Kalshi first (CFTC-regulated, demo at `demo-api.kalshi.co`),
  Polymarket US (QCX LLC, Ed25519 keys) once credentials land.
- Data venue is SEPARATE and read-only: Hyperliquid only. No orders ever
  go there. Never build against Polymarket **Global** (geo-blocked to US persons);
  no VPN/wallet workarounds.
- Kalshi defaults to the **demo** environment. Production requires an explicit
  config flag AND the interactive live-gating flow.

## Security
- All venue API keys are trading-only. No key with withdrawal authority may ever
  exist in this project. If a step seems to need one, STOP and ask.
- Private keys are referenced by file path, never inlined in `.env` or config.
- Never log, print, or persist any key or signed payload, even at debug level.
- The signal provider (Hyperliquid) is strictly read-only — nothing is
  ever signed or sent to it.

## Scheduled routines
- If you are running as a routine you woke up **stateless**. Read
  `memory/GUARDRAILS.md` and `memory/STATE.md` before doing anything, and write
  back what the next run needs. See `routines/README.md` for the full contract.
- **Anything not committed never happened** — a remote run works in a clone that
  is destroyed on exit. Commit and push to `main` before finishing.
- Never commit a credential, `data/tape/`, or a `.env`. Credentials come from
  environment variables and their names must match character for character.
- A routine may recommend a parameter change in `memory/DECISIONS.md`. It may
  never apply one — tuning against a score you have already seen is fitting.

## Toolchain notes
- Python 3.12 via `uv` (managed). This machine has a corporate/MITM root CA:
  every uv invocation needs `--system-certs` (`--native-tls` is the deprecated
  old name — do not use it); runtime httpx clients must use the OS trust store
  (`truststore`) or live TLS calls will fail with `UnknownIssuer`.
