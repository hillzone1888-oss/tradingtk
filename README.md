# tradetk

A composable CLI trading toolkit you drive interactively. It derives signals from
**Hyperliquid** crypto market data (read-only) and trades **binary event contracts**
on a separate execution venue (**Kalshi** first, **Polymarket US** later).

Data venue and execution venue are completely separate. No order ever touches the
data layer. See `CLAUDE.md` for the operating rules — including the absolute
`execute` boundary.

## Status: build step 13 of 19 (two strategies to compare; risk layer next)

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
- [x] 9. Shadow evaluator (full eligible universe) + `shadow` command.
      Scores every eligible market **including the ones every gate rejected** —
      those are the population there is otherwise no evidence about. Costs no
      capital, so evidence accumulates at the rate the universe moves rather
      than the rate capital recycles: the same 17-minute tape yields 5 trades
      but **111 scored forecasts**. Writes are idempotent, so re-running cannot
      inflate the sample.
- [x] 10. `calibrate` command (reliability diagram + Brier + Murphy).
      Scores the model **and the venue's own mid on the same contracts**. That
      comparison, not P&L, is the project's success criterion: if the market
      forecasts better, every trade pays a spread to be more wrong.
      Brier is decomposed into reliability / resolution / uncertainty because
      the fixes are opposite — miscalibration can be remapped, missing signal
      cannot. Repeated looks at one contract are reported separately from the
      independent per-contract view, and measured-reference markets are never
      pooled with fixed strikes.
- [x] 11. Backtest engine (book-walking fills) + `backtest` command.
      Replays the project's **own recorded tape** — free by construction, since
      nobody sells prediction-market book depth. Fills walk the recorded ladder
      using the same cost code as live. Every as-of lookup is enforced in code
      (candles are invisible until they *close*; metadata recorded later is
      unreachable), because a backtest that merely intends not to peek
      eventually peeks, and the symptom is excellent results.
      Settlement is a **proxy** (Hyperliquid candles, not the venue's CF
      Benchmarks index); settlements landing near the strike are counted and
      reported, since those are the ones the proxy could get backwards.
      Honesty warnings are computed from the data and serialised first.
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
- [x] 13. `LiquidationSkewStrategy` + the `liquidations` signal types.
      The baseline's lognormal with **one term added**: a log-drift set by the
      imbalance of recent forced liquidations. Only one term changes, so step
      10 can attribute any difference in Brier score to it and to nothing else.
      The tilt is **capped, not fitted** — `max_drift_sigma` (default 0.25) is
      the shift at a perfectly one-sided window, measured in the claim's *own*
      horizon sigmas, so 0.25σ means the same thing on a 2-hour and a 20-hour
      contract. The **sign is a declared hypothesis**, not a finding:
      `regime: continuation | reversion` are both well-attested readings of
      forced flow, so the parameter is recorded in the method string and
      calibration adjudicates it — flipping it after seeing results on the same
      tape is fitting, and is called out as such in the code.
      It **abstains rather than falling back** to the baseline whenever the
      evidence is missing or thin (no profile, wrong asset, stale, few events,
      small notional, one whale holding up the imbalance, or a claim resolving
      beyond the signal's horizon); a fallback would make its calibration score
      the baseline's score under a different name.
      `signals/liquidations.py` types the event and reduces a stream to one
      window statistic, refusing — not filtering — the two inputs that produce
      plausible wrong numbers: another asset's events, and events after `as_of`.
      **The side convention is pinned by a test** (`long` = longs force-*sold* =
      downward pressure): feeds disagree, the two conventions are
      indistinguishable from the numbers, and the error would silently invert
      every trade.
      ⚠️ **Not runnable yet, by design.** It declares `Capability.LIQUIDATIONS`,
      which no provider advertises (Moon Dev's HL-derived endpoints are not
      implemented), so selecting it halts at startup instead of running on zeros.
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
  backtest/             # tape replay, as-of market data, settlement, engine
  report/               # rich terminal + self-contained HTML (vendored charts)
  notify/               # outbound Telegram push — text only, no venue access
  cli/                  # one module per command; structured JSON output
memory/                 # what a scheduled run reads on wake-up and writes back
routines/               # scheduled-run prompts + the contract they follow
scripts/                # calibrate.py, validate_provider.py, ...
reference/              # cloned docs (gitignored) — read, do not vendor
proposals/              # generated order proposals (gitignored)
data/shadow/            # accumulated evidence — COMMITTED (calibration needs it)
data/tape/, state/      # tape, cache, sqlite (gitignored)
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

## Running it unattended (routines)

Scheduled Claude Code runs drive the toolkit on a cron — no daemon, no service,
no Python process in a loop. A routine wakes up stateless, reads `memory/`, runs
the toolkit's own read-only commands, writes back what it learned, and commits.

| Routine | When (UTC) | Job |
|---|---|---|
| `sweep` | every 4h | record a slice → score the whole universe → commit the forecasts |
| `digest` | daily 13:00 | yesterday's evidence + the calibration headline, pushed to Telegram |
| `weekly-review` | Sun 22:00 | full calibration, model vs. mid, written recommendations |

The agent supplies scheduling, summarising and memory. It supplies **no opinions
about markets** — every decision that matters is the same deterministic, tested
Python it was before. And the execution boundary above binds a routine exactly as
it binds a person: unattended is the case it was written for. See
`routines/README.md` for the contract and `memory/GUARDRAILS.md` for the rules
every run reads first.

## Backtesting

```bash
uv run python -m tradetk.cli.backtest                       # table view
uv run python -m tradetk.cli.backtest --html report.html    # + charts
uv run python -m tradetk.cli.backtest --fixed-contracts 2   # fixed-size buys
uv run python -m tradetk.cli.backtest --json --pretty       # machine-readable
```

It costs nothing to run and needs no account, because it replays the orderbook
tape `record` wrote. **Its depth is entirely a function of how long the recorder
has been running** — that is the real price of a free backtest, and it is paid
in patience rather than money. Run `record --daemon` and leave it.

The HTML report is one self-contained file (TradingView
[Lightweight Charts](https://github.com/tradingview/lightweight-charts),
Apache-2.0, vendored into `report/vendor/` and inlined — no CDN, so an archived
report still renders years later). Both the terminal and HTML views print the
sample-size caveats **above** the P&L, because a return figure read without them
feels like evidence.

## Answering "is it working?"

Not from the balance, and not from backtest P&L — both are noise on a $20 book.

```bash
uv run python -m tradetk.cli.shadow                          # score everything
uv run python -m tradetk.cli.calibrate --html calib.html     # score the scores
```

`shadow` runs the strategy over the **whole eligible universe** and records the
forecast, gates included but not binding. Six slots can only ever produce six
data points; the universe produces thousands, and a forecast costs nothing to
score. This is the only realistic route to a readable sample size.

`calibrate` then asks the one question that decides everything: **is the model a
better forecaster than the price it would have to trade against?** Both are
scored on identical contracts. If Kalshi's mid wins, there is no edge and the
report says so in those words.

## Honesty about a $20 book

Live trading here validates *plumbing* (auth, fills, reconciliation, settlement),
not *edge*. A $3 swing is noise. "Is it working?" is answered from **calibration
and shadow results**, never the balance. Shadow and live numbers are never blended.
