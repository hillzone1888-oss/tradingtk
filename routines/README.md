# Routines

Scheduled Claude Code runs. **No daemon, no service, no Python process sitting
in a loop** — a routine wakes up, reads its files, runs the toolkit's own
commands, writes what it learned back to the repo, and stops.

The model of this directory is borrowed from Nate Herk's "24/7 trader" setup,
with the trading judgment removed. There, the agent decides what to buy. Here,
every decision that matters is already deterministic Python with tests around
it — probability, edge, costs, sizing — and the routine's job is to *run* that
machinery on a schedule and report what it found. The agent supplies scheduling,
summarising and memory. It does not supply opinions about markets.

## The contract every routine follows

1. **Read `memory/GUARDRAILS.md` and `memory/STATE.md` first.** A routine wakes
   up stateless. These files are the only continuity there is.
2. **Do the job** using the toolkit's CLI. Never reimplement trading logic in the
   prompt.
3. **Write back** what the next run needs: update `memory/STATE.md`, append to
   `memory/DECISIONS.md` if something changed or should change.
4. **Commit and push to `main`.** A remote routine works in a fresh clone that is
   destroyed afterwards. *Anything not committed never happened.*
5. **Notify** per the routine's own rule below.

## The routines

| File | Schedule (UTC) | Job | Notifies |
|---|---|---|---|
| `sweep.md` | `0 */4 * * *` | Record a fresh tape slice, score the whole eligible universe, commit the forecasts | Only on trouble or a gated-in trade |
| `digest.md` | `0 13 * * *` | Yesterday's evidence + calibration headline | Always |
| `weekly-review.md` | `0 22 * * 0` | Full calibration, strategy comparison, written recommendations | Always |

Crypto event contracts trade around the clock, so unlike an equities bot there is
no market open, no midday and no close — the cadence is set by how fast evidence
accumulates, not by a trading session.

`propose.md` is deliberately absent. The `propose` command is build step 16 and
does not exist yet; a routine that pretended to produce proposals would produce
fiction. Add it when step 16 lands.

## What a routine may never do

**Place an order.** Not in demo, not to test. See `memory/GUARDRAILS.md` — the
boundary exists precisely because a model running unattended on a cron is the
case it was written for. The strongest output a routine may produce is a proposal
file path in a Telegram message.

## Setup

**Cloud environment variables** — set on the routine's environment, never in this
repo. Names must match **character for character**; a one-letter mismatch is a
routine that runs happily and silently tells nobody anything:

```
TELEGRAM_BOT_TOKEN     # from @BotFather
TELEGRAM_CHAT_ID       # your chat id
KALSHI_API_KEY_ID      # demo credentials; trading-only, never withdrawal
KALSHI_PRIVATE_KEY     # PEM contents
MOONDEV_API_KEY        # optional; only if signals are enabled
ALPACA_API_KEY_ID      # paper account, held for a future equities agent —
ALPACA_API_SECRET      #   no code reads these and no routine uses them.
ALPACA_BASE_URL        #   See memory/DECISIONS.md for why they cannot paper-
                       #   trade Kalshi contracts.
```

**Per-routine permission:** enable *allow unrestricted branch pushes*, or step 4
of the contract silently fails and memory never persists.

**Test before trusting.** "Run now" each routine at least twice before leaving it
on a schedule — once to see it work, once to see it work against the state the
first run left behind.

## What persists, and what does not

- `data/shadow/` **is committed.** It is the accumulated evidence and the only
  thing calibration can be computed from. Losing it loses the project's answer to
  "is this working."
- `data/tape/` **is not committed.** Book depth is large, rewritten per poll, and
  git handles it badly. Each sweep records the slice it needs and consumes it in
  the same run. The consequence is real and worth stating: **remote runs build no
  long tape, so the backtest stays a local activity.** If deep history becomes
  the priority, run the recorder locally in daemon mode as well.
