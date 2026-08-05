# Vault overlay — design

**Date:** 2026-08-04
**Status:** design, pending implementation plan
**Depends on:** `vault-post` (`C:\Users\hillz\Claude\vault-post`), built 2026-08-04

## The problem

The pipeline knows nothing about the world. It prices binary claims from
realized vol and a book, and that is all it will ever know on its own. Meanwhile
the Second Brain accumulates researched views — a stance on an underlying, a
catalyst on the calendar — that have no way to reach it.

`vault-post` already solved the hard half: mail is validated, its evidence is
scored into a risk ceiling, and a human approves it before any consumer sees it.
What remains is the consumer side: taking approved stances and catalysts and
letting them shape what this toolkit proposes, **without touching the
deterministic core that makes the toolkit trustworthy**.

## The rule that shapes everything here

Probability estimation stays untouched. A stance never nudges `p`.

The overlay only ever does three things, all of them outside the math:

| Dial | Effect | Mechanism |
|---|---|---|
| `bias` | which side may be bought | restrict eligible `Side` |
| `risk` | how large the position is | scale `SizingLimits.position_target` |
| catalyst | how much edge is demanded | raise `GateLimits.margin_pp`, or block |

Every one of these narrows. **The vault can make the system more selective, never
less.** A stance cannot grant permission the pipeline would otherwise refuse, and
an empty vault behaves exactly like today.

## Measurement versus action — the central distinction

`shadow` exists to score the *whole* eligible universe. Its own docstring is
explicit: recording only markets that passed the gates would "measure the model
on its own selection," and the interesting question is whether the model was
right about the ~2,094 contracts it declined.

Applying the overlay as a filter inside `shadow` would silently destroy exactly
that evidence. A "BTC bearish" stance would stop BTC-up forecasts being
recorded, and the calibration set would quietly become a record of what the
stances already believed.

So the split is:

- **`shadow` annotates.** Every market is still scored and recorded. Each record
  additionally carries what the overlay *would* have done. Nothing is suppressed.
- **`backtest` acts.** Blocked underlyings do not trade, bias restricts the side,
  risk scales the target, catalysts raise the required edge.
- **`propose` will act** when it exists. Out of scope here.

This is not damage control; it is the payoff. With the verdict on every record,
calibration can answer the question that actually matters: **do markets the
stances allowed calibrate better than the ones they blocked?** That is how the
vault's contribution gets measured instead of assumed — the same standard the
project already applies to the model itself.

## Architecture

```
vault-post (separate repo, path dependency)
      │  approved stances + catalysts, already scored and capped
      ▼
tradetk.overlay.policy          ← pure functions, no I/O
      │  UnderlyingPolicy per symbol
      ├──────────────► shadow evaluator   (records the verdict, filters nothing)
      └──────────────► backtest engine    (applies the verdict)
```

`tradetk` depends on `vaultpost` via a local path dependency, editable, so the
two develop together without a publish step.

### `UnderlyingPolicy`

The single object the rest of the system consults:

```python
@dataclass(frozen=True)
class UnderlyingPolicy:
    underlying: str
    bias: Bias | None                   # None when there is no stance
    sizing_limits: SizingLimits         # target scaled by effective risk
    gate_limits: GateLimits             # margin raised inside a catalyst window
    blocked: bool                       # risk 0, or a blocking catalyst
    reasons: tuple[str, ...]            # every mail id that moved a number
    source_mail: tuple[str, ...]

    def allowed_sides(self, claim: Claim) -> tuple[Side, ...]:
        """Sides permitted for one specific claim."""
```

`allowed_sides` is a **method taking a claim**, not a stored tuple. It cannot be
resolved at the underlying level, because which `Side` expresses a bullish view
depends on the claim's operator — see below.

`VaultOverlay.for_underlying(symbol, now)` returns one. With no mail for that
symbol it returns the global limits unchanged and `blocked=False` — the identity
case, which is what makes an empty vault a no-op.

### Mapping the dials

- `bias=bullish` → `(Side.yes,)`; `bearish` → `(Side.no,)`; `neutral` → both.
  Neutral is *no directional view*, never a brake.
- `risk` → `position_target * effective_risk / 100`, then `max_position_dollars`
  applied as an absolute **ceiling** on top (the smaller of the two wins, since
  every dial narrows). `risk=0` → `blocked`.
- catalyst `widen_edge` inside its window → `margin_pp + extra_margin_pp`.
  `block` inside its window → `blocked`.

Note that `effective_risk` already reflects evidence decay: vault-post computes
it at read time, so the same untouched note authorises less as its support ages.
`tradetk` does not re-derive this and must not.

### Side restriction, honestly stated

A YES contract on "BTC above 100k" and a NO contract on the same claim are
opposite directional bets, so `bias` maps cleanly onto `Side`. But a NO on
"BTC **below** 90k" is also a bullish bet. The mapping must therefore go through
the claim's operator, not the raw side.

`bullish_side(claim) -> Side` resolves this once: for `above`/`at_or_above`,
bullish is YES; for `below`, bullish is NO. `between` claims are **not
directional** — a range bet is neither bullish nor bearish — so a directional
stance leaves them untouched and says so in `reasons`.

Getting this wrong would invert a stance on half the universe, so it is a
first-class tested function rather than an inline conditional.

## Verifiers tradetk registers

`vault-post` defines the verifier contract and knows nothing about market data.
`tradetk` owns the data, so it registers the implementations. Without these the
`technical` evidence class scores zero everywhere and the reproducibility
mechanism is never exercised against reality.

Four, all computable today from the keyless Hyperliquid provider:

| Verifier | Checks | Params |
|---|---|---|
| `tradetk.spot` | spot price within tolerance | `symbol`, `tolerance_pct` |
| `tradetk.realized_vol` | N-day realized vol | `symbol`, `lookback_days`, `tolerance_pct` |
| `tradetk.price_change_pct` | % change over N hours | `symbol`, `hours`, `tolerance_pp` |
| `tradetk.funding` | current funding rate | `symbol`, `tolerance` |

Each takes the claimed `value` and answers whether the live data reproduces it
within tolerance. A verifier that cannot reach data returns `False` — evidence
that cannot be checked does not score. RSI, MACD and Bollinger wait for the
Strategy Lab.

## As-of integrity

`backtest` replays recorded history, so it must read stances **as of the replay
timestamp**, never live. Reading current state while replaying the past would
price it with views written afterwards — the exact failure vault-post's snapshot
store exists to prevent, and a cousin of the metadata-ordering bug this project
has already been bitten by once.

`record` already runs on a schedule and already snapshots market metadata, so it
also captures a vault-post snapshot each poll. One scheduled process, not two.

A test pins it: a backtest over a window preceding a stance's creation must not
see that stance.

## Configuration

A new optional block, disabled by default so nothing changes until asked:

```yaml
vault_overlay:
  enabled: false
  config_path: "../vault-post/config/config.yaml"
```

## Failure behaviour

Missing config, unreachable vault, unparseable mail, or an import failure →
**empty overlay, everything trades as it does today, and the degradation is
reported loudly** in both the JSON output (`"vault_overlay": {"ok": false,
"error": ...}`) and the logs.

This is fail-open with respect to the vault's *views*, which is correct: the
pipeline was built to be safe standalone, and the overlay only ever narrows. But
it must never be fail-*silent* — a bridge that quietly stopped working would
leave the operator believing their research was steering trades when it was not.

## Components

| File | Responsibility |
|---|---|
| `src/tradetk/overlay/policy.py` | `UnderlyingPolicy`, dial mapping, pure |
| `src/tradetk/overlay/direction.py` | `bullish_side(claim)`, operator-aware |
| `src/tradetk/overlay/loader.py` | build a `VaultOverlay` from config; fail-open |
| `src/tradetk/overlay/verifiers.py` | the four Hyperliquid-backed verifiers |
| `src/tradetk/shadow/records.py` | `ShadowRecord.overlay` annotation field |
| `src/tradetk/shadow/evaluator.py` | record the verdict; filter nothing |
| `src/tradetk/backtest/engine.py` | resolve limits per claim; apply the verdict |
| `src/tradetk/cli/record.py` | capture a vault snapshot each poll |
| `src/tradetk/config/schema.py` | the `vault_overlay` block |

## Testing

- `bullish_side` across every operator, including `between` being non-directional
- Identity: no mail → limits byte-identical to the global ones, `blocked=False`
- `bias` restricts the correct side *through the operator*, not the raw side
- `risk` scales the target; `risk=0` blocks; `max_position_dollars` caps
- Catalyst raises `required_edge_pp` only inside its window; `block` blocks
- Decay: the same stance read later yields a smaller target, untouched
- **Shadow records every market including blocked ones** (the anti-filter pin)
- Shadow records carry the overlay verdict
- Backtest applies the verdict
- **Backtest as-of: a stance created after the replay window is invisible**
- Fail-open: a broken bridge trades normally *and* reports `ok: false`
- Each verifier against a fake provider, including the unreachable case

## Out of scope

- `execute` — does not exist yet, and its boundary is a separate spec
- Any change to probability estimation
- Auto-generated stances (Strategy Lab)
- RSI/MACD/Bollinger verifiers
