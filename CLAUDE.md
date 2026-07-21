# Operating rules

## Execution boundary — absolute
- NEVER run `execute`. Not with flags, not in a script, not "just to test."
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
- Data venue is SEPARATE and read-only: Hyperliquid + Moon Dev. No orders ever
  go there. Never build against Polymarket **Global** (geo-blocked to US persons);
  no VPN/wallet workarounds.
- Kalshi defaults to the **demo** environment. Production requires an explicit
  config flag AND the interactive live-gating flow.

## Security
- All venue API keys are trading-only. No key with withdrawal authority may ever
  exist in this project. If a step seems to need one, STOP and ask.
- Private keys are referenced by file path, never inlined in `.env` or config.
- Never log, print, or persist any key or signed payload, even at debug level.
- Signal providers (Moon Dev, Hyperliquid) are strictly read-only — nothing is
  ever signed or sent to them.

## Toolchain notes
- Python 3.12 via `uv` (managed). This machine has a corporate/MITM root CA:
  uv needs `--native-tls`; runtime httpx clients must use the OS trust store
  (`truststore`) or live TLS calls will fail with `UnknownIssuer`.
