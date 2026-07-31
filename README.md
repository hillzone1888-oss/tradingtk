# tradetk

A composable CLI trading toolkit you drive interactively. It derives signals from
**Hyperliquid** crypto market data (read-only) and trades **binary event contracts**
on a separate execution venue (**Kalshi** first, **Polymarket US** later).

Data venue and execution venue are completely separate. No order ever touches the
data layer. See `CLAUDE.md` for the operating rules — including the absolute
`execute` boundary.

## Status: build step 8 of 19 (translation layer)

- [x] 1. Scaffold, config schema, `.gitignore`, README, `CLAUDE.md`
- [x] 2. `DataProvider` protocol + `HyperliquidProvider` + cache + tests
- [x] 3. `MoonDevProvider` + typed models + `validate_provider.py`
      — Polymarket flow family only; HL-derived signals not yet implemented
      and deliberately not advertised in `capabilities()`
- [x] 4. `Venue` protocol + `KalshiVenue` — read-only; canonical
      YES-denominated book hides Kalshi's dual-bid representation.
      No order path exists in the adapter, by design.
- [x] 5. `recorder.py` — signal tape **and** orderbook tape, both recording.
      Pulled ahead of step 4 deliberately: neither the whale log (250-row cap
      ≈ 20 min of flow) nor prediction-market book depth can be bought later.
      Market data is read from **prod** (demo has no strike fields and no
      depth); execution still targets demo, and this path has no order endpoint.
- [x] 6. Market parser: contract -> typed `Claim` + `scan` command.
      Structured strikes only — `custom` markets are refused, never
      regexed out of the title. 2,124 of 2,428 real markets eligible.
- [x] 7. `costs/` fee model, verified vs live schedule.
      Kalshi's published formula with the constants injected, not hardcoded;
      reproduces all 21 rows of the published 100-contract table. Spread and
      book-walking slippage in probability points. `inspect` command.
      Rounding granularity (cent vs centicent) is genuinely ambiguous in the
      source docs — defaults to the conservative one and is settled by
      `reconcile_fill` against a real fill.
- [x] 8. Translation layer: probability -> edge -> sizing gate.
      Driftless lognormal probability (no scipy), edge in probability points
      after fees + spread + slippage, fixed-dollar sizing to integer contracts.
      Evaluates **both sides** — a YES ask of 0.50 against p=0.30 is a 20-point
      edge on NO, and a YES-only gate would discard half the universe.
      Deep-tail estimates are rejected by default: that is where the lognormal
      is known to be wrong and where fees are highest per dollar staked.
- [ ] 9. Shadow evaluator (full eligible universe)
- [ ] 10. `calibrate.py` (reliability diagram + Brier)
- [ ] 11. Backtest engine (book-walking fills)
- [x] 12. `BaseStrategy` contract + registry, and `BaselineVolStrategy`.
      Pulled ahead of step 11: the backtest engine needs something to replay,
      and a backtest harness written before any strategy exists tends to grow
      an interface the first strategy then has to fight.
      A strategy returns **only a probability** — never a trade decision — so
      it inherits the whole gate stack and cannot route around it. It is handed
      a frozen `MarketSnapshot`, not a provider, so it has no way to fetch
      future data during a replay. Abstention is a distinct answer from p=0.5.
      The baseline is implicitly **short volatility** (realized vol < implied,
      the variance risk premium); `vol_multiplier` is the honest lever for that
      and is deliberately not fitted to backtest results.
- [ ] 13. `LiquidationSkewStrategy`
- [ ] 14. Risk module
- [ ] 15. Paper executor
- [ ] 16. `propose` command + `CLAUDE.md` execute boundary
- [ ] 17. `execute` command (demo only, interactive-only)
- [ ] 18. `PolymarketUsVenue` (sandbox first)
- [ ] 19. Production flags

## Toolchain

Python 3.12 managed by [`uv`](https://docs.astral.sh/uv/).

> This machine has a corporate/MITM root CA. uv needs system certs
> (`export UV_SYSTEM_CERTS=1`), and runtime httpx clients use `truststore` for the
> same reason, or live TLS calls fail with `UnknownIssuer`.

```bash
export UV_SYSTEM_CERTS=1
uv sync                                              # create .venv, install deps
cp config/config.example.yaml config/config.yaml     # your real config (gitignored)
cp .env.example .env                                 # secrets (gitignored)

uv run python -m tradetk.config.loader config/config.yaml   # validate config
uv run pytest -q                                            # run tests
```

## Layout

```
config/                 # config.example.yaml (tracked) + config.yaml (gitignored)
src/tradetk/
  enums.py              # Mode, Env, VenueName, ProviderName, Capability
  config/               # pydantic schema + loader  <-- implemented
  signals/              # DataProvider protocol; hyperliquid + moondev
  translation/          # THE CORE: signal -> probability -> edge -> sizing
  costs/                # fee models, spread + slippage
  venues/               # Venue protocol; kalshi, polymarket_us, paper
  strategy/             # BaseStrategy + reference strategies
  shadow/               # scores estimates across the full eligible universe
  risk/                 # dollar-denominated sizing gate + limits
  state/                # positions, P&L, trade log (SQLite)
  cli/                  # one module per command; structured JSON output
scripts/                # calibrate.py, validate_provider.py, ...
reference/              # cloned docs (gitignored) — read, do not vendor
proposals/              # generated order proposals (gitignored)
data/, state/           # tape, cache, sqlite (gitignored)
```

> Note: the spec's tree lists `signals/`, `translation/`, etc. at repo root. They
> live under `src/tradetk/` here so the package is importable/installable and
> subpackage names (`config`, `state`, `costs`) don't shadow common top-level
> imports. Names and responsibilities are otherwise exactly as specified.

## The two-command execution boundary

- `propose` — scans, estimates, gates, and writes `proposals/<ts>.json` with the
  full trace. **Never contacts the venue order endpoint.** Safe to run constantly.
- `execute --proposal <file>` — the **only** path that submits an order.
  Re-validates against the live book + risk state, refuses if anything material
  moved, and requires interactive typed confirmation. Refuses to run
  non-interactively. **The human runs this, never the assistant.**

## Honesty about a $20 book

Live trading here validates *plumbing* (auth, fills, reconciliation, settlement),
not *edge*. A $3 swing is noise. "Is it working?" is answered from **calibration
and shadow results**, never the balance. Shadow and live numbers are never blended.
