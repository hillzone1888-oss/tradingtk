"""Self-contained HTML backtest reports with TradingView charts.

Uses TradingView's own **Lightweight Charts** (Apache-2.0), vendored into
``report/vendor/`` and inlined into the output. No CDN, no network at view time:
a report is one file you can open on a plane, mail to someone, or keep as a
record of what the strategy looked like on a given tape. A report that silently
depends on a CDN stops rendering the day the CDN version changes, which for an
archived result is the same as losing it.

**The layout puts the caveats above the numbers, on purpose.** The honesty
warnings render first, in the loudest element on the page, because a P&L figure
read without its sample size is worse than no figure at all — it feels like
evidence. The operating rules require sample size and calibration to travel with
P&L, and a report is exactly where that rule gets quietly broken.

What gets charted:

* **Underlying price** as candles, with a horizontal price line at every strike
  traded and a marker at every entry. This is the view that makes an entry
  legible at a glance — "we bought YES above 100k when spot was here, and it
  went there."
* **Realized P&L** over time, stepped, because P&L only changes at settlement
  and drawing a smooth line between settlements would imply intermediate values
  that never existed.
* **Calibration** as an inline SVG reliability diagram. Deliberately not a
  TradingView chart: it has no time axis, and forcing it into one would misread
  as a time series.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from tradetk.backtest.engine import BacktestResult, calibration_buckets
from tradetk.backtest.marketdata import MarketDataSet

VENDOR = Path(__file__).parent / "vendor" / "lightweight-charts.js"


@lru_cache(maxsize=1)
def _library() -> str:
    if not VENDOR.exists():
        raise FileNotFoundError(
            f"charting library missing at {VENDOR}. It is vendored deliberately "
            "so reports render offline; re-download it rather than switching to a CDN."
        )
    return VENDOR.read_text(encoding="utf-8")


def _epoch(when: datetime) -> int:
    return int(when.timestamp())


def _f(value: Any) -> float:
    return float(value) if value is not None else 0.0


def _candle_rows(data: MarketDataSet, symbol: str) -> list[dict[str, Any]]:
    """Candles as Lightweight Charts rows, deduplicated on time.

    The library requires strictly ascending unique timestamps and throws on a
    duplicate, so collisions are collapsed here rather than crashing the page.
    """
    series = data.series(symbol)
    if series is None:
        return []
    rows: dict[int, dict[str, Any]] = {}
    for candle in series._candles:  # noqa: SLF001 - report reads its own package's data
        rows[candle.close_ms // 1000] = {
            "time": candle.close_ms // 1000,
            "open": candle.o, "high": candle.h, "low": candle.l, "close": candle.c,
        }
    return [rows[k] for k in sorted(rows)]


def _equity_rows(result: BacktestResult) -> list[dict[str, Any]]:
    """Realized P&L over time, one point per distinct timestamp (last wins)."""
    rows: dict[int, float] = {}
    for point in result.equity_curve:
        rows[_epoch(point.when)] = _f(point.realized_pnl)
    return [{"time": t, "value": rows[t]} for t in sorted(rows)]


def _markers_and_strikes(
    result: BacktestResult, symbol: str
) -> tuple[list, list, dict[str, int] | None]:
    """Entry markers, strike price lines, and the window worth looking at.

    The price line is drawn at the contract's **strike** — the level the trade
    was actually a bet about. Drawing it at the settled value instead would put
    the line wherever price happened to end up, which is both tautological and
    invisible as a mistake.

    The focus window exists because the candle history is deliberately much
    longer than the tape (the vol lookback needs it), so fitting the whole range
    crushes every entry into the last pixel column.
    """
    markers: list[dict[str, Any]] = []
    strikes: dict[float, str] = {}
    entry_times: list[int] = []

    for trade in result.trades:
        if trade.underlying.upper() != symbol.upper():
            continue
        won = trade.resolved
        colour = "#7f8c99" if won is None else ("#26a69a" if won else "#ef5350")
        entered = _epoch(trade.entry_time)
        entry_times.append(entered)
        markers.append(
            {
                "time": entered,
                "position": "belowBar" if trade.side.value == "yes" else "aboveBar",
                "color": colour,
                "shape": "arrowUp" if trade.side.value == "yes" else "arrowDown",
                "text": f"{trade.side.value.upper()} {trade.contracts} @ {trade.average_price}",
            }
        )
        if trade.strike is not None:
            strikes.setdefault(
                round(float(trade.strike), 6), f"{trade.side.value.upper()} strike"
            )

    markers.sort(key=lambda m: m["time"])

    focus = None
    if entry_times:
        first, last = min(entry_times), max(entry_times)
        # Pad generously: a 17-minute tape needs surrounding context to read at
        # all, while a long tape is already its own context.
        pad = max(6 * 3600, (last - first))
        focus = {"from": first - pad, "to": last + pad}

    return markers, [{"price": p, "label": lbl} for p, lbl in sorted(strikes.items())], focus


def _calibration_svg(result: BacktestResult) -> str:
    """Reliability diagram: predicted on x, observed on y, diagonal is perfect."""
    buckets = calibration_buckets(result.trades)
    if not buckets:
        return (
            '<p class="empty">No settled contracts yet, so there is nothing to '
            "calibrate. This is the number that decides whether the model means "
            "anything — not the P&amp;L.</p>"
        )

    size, pad = 320, 34
    inner = size - 2 * pad

    def x(v: float) -> float:
        return pad + v * inner

    def y(v: float) -> float:
        return size - pad - v * inner

    ticks = "".join(
        f'<line class="grid" x1="{x(v):.1f}" y1="{pad}" x2="{x(v):.1f}" y2="{size - pad}"/>'
        f'<line class="grid" x1="{pad}" y1="{y(v):.1f}" x2="{size - pad}" y2="{y(v):.1f}"/>'
        f'<text class="tick" x="{x(v):.1f}" y="{size - pad + 15:.1f}">{v:.1f}</text>'
        f'<text class="tick ty" x="{pad - 8:.1f}" y="{y(v) + 4:.1f}">{v:.1f}</text>'
        for v in (0.0, 0.25, 0.5, 0.75, 1.0)
    )
    biggest = max(b.n for b in buckets)
    points = "".join(
        f'<circle class="pt" cx="{x(b.mean_predicted):.1f}" cy="{y(b.observed_frequency):.1f}" '
        f'r="{4 + 8 * (b.n / biggest):.1f}"><title>predicted {b.mean_predicted:.3f}, '
        f"observed {b.observed_frequency:.3f}, n={b.n}</title></circle>"
        for b in buckets
    )
    return f"""<svg viewBox="0 0 {size} {size}" class="calib" role="img"
     aria-label="Reliability diagram: predicted probability against observed frequency">
  {ticks}
  <line class="perfect" x1="{x(0)}" y1="{y(0)}" x2="{x(1)}" y2="{y(1)}"/>
  {points}
  <text class="axis" x="{size / 2}" y="{size - 4}">predicted probability</text>
  <text class="axis" x="-{size / 2}" y="12" transform="rotate(-90)">observed frequency</text>
</svg>"""


def _rows(headers: list[str], rows: list[list[str]], *, empty: str) -> str:
    if not rows:
        return f'<p class="empty">{empty}</p>'
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _trades_table(result: BacktestResult) -> str:
    def outcome(t) -> str:
        if t.resolved is None:
            return '<span class="pill unk">unsettled</span>'
        return (
            '<span class="pill win">YES</span>' if t.resolved
            else '<span class="pill loss">NO</span>'
        )

    return _rows(
        ["Ticker", "Side", "Entry", "N", "Avg px", "Cost", "Edge pp", "p", "Outcome", "P&L"],
        [
            [
                f'<span title="{t.claim_description}">{t.ticker}</span>',
                t.side.value.upper(),
                t.entry_time.strftime("%m-%d %H:%M"), str(t.contracts),
                str(t.average_price), f"${t.cost}", f"{t.net_edge_pp:.2f}",
                f"{t.p_estimate:.3f}", outcome(t),
                f'<span class="{"pos" if t.pnl > 0 else "neg" if t.pnl < 0 else ""}">${t.pnl}</span>',
            ]
            for t in result.trades
        ],
        empty="No positions were opened on this tape.",
    )


def _skips_table(result: BacktestResult) -> str:
    return _rows(
        ["Reason", "Count"],
        [[k.replace("_", " "), str(v)] for k, v in
         sorted(result.skipped.items(), key=lambda kv: -kv[1])],
        empty="Nothing was skipped.",
    )


def render_backtest_report(
    result: BacktestResult,
    data: MarketDataSet | None = None,
    *,
    title: str = "tradetk backtest",
) -> str:
    """Render a complete, self-contained HTML report."""
    summary = result.summary()
    warnings = result.honesty_warnings()

    charts: list[dict[str, Any]] = []
    if data is not None:
        for symbol in sorted({t.underlying for t in result.trades} or data.symbols):
            candles = _candle_rows(data, symbol)
            if not candles:
                continue
            markers, strikes, focus = _markers_and_strikes(result, symbol)
            charts.append(
                {
                    "symbol": symbol, "candles": candles, "markers": markers,
                    "strikes": strikes, "focus": focus,
                }
            )

    payload = json.dumps(
        {"charts": charts, "equity": _equity_rows(result)}, default=str
    )

    warn_html = "".join(f"<li>{w}</li>" for w in warnings)
    warn_block = (
        f'<section class="warn"><h2>Read this before the numbers</h2><ul>{warn_html}</ul></section>'
        if warnings else ""
    )

    def card(label: str, value: str, note: str = "") -> str:
        return (
            f'<div class="card"><span class="lbl">{label}</span>'
            f'<span class="val">{value}</span>'
            f'{f"<span class=note>{note}</span>" if note else ""}</div>'
        )

    brier = summary["brier_score"]
    cards = "".join(
        [
            card("Strategy", result.strategy),
            card("Opened", str(summary["trades_opened"])),
            card("Settled", str(summary["trades_settled"])),
            card(
                "Win rate",
                f'{summary["win_rate"]:.1%}' if summary["win_rate"] is not None else "—",
            ),
            card("Realized P&L", f'${summary["realized_pnl"]}'),
            card(
                "Brier",
                f"{brier:.4f}" if brier is not None else "—",
                "0.25 = always saying 50%",
            ),
            card("Tape span", f'{result.tape.get("tape_span_days", 0):.3f} d'),
            card("Observations", str(result.tape.get("observations", 0))),
        ]
    )

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{
  color-scheme: light dark;
  --bg:#0e1116; --panel:#161b22; --line:#272e39; --tx:#e6edf3; --dim:#8b949e;
  --pos:#26a69a; --neg:#ef5350; --warn:#d29922; --warnbg:#211a09; --accent:#58a6ff;
}}
@media (prefers-color-scheme: light) {{
  :root {{ --bg:#f6f8fa; --panel:#fff; --line:#d8dee4; --tx:#1f2328; --dim:#636c76;
           --warnbg:#fff8e5; }}
}}
* {{ box-sizing:border-box }}
body {{ margin:0; padding:28px 20px 60px; background:var(--bg); color:var(--tx);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1120px; margin:0 auto }}
h1 {{ font-size:1.5rem; margin:0 0 4px }}
h2 {{ font-size:1.05rem; margin:0 0 14px; font-weight:600 }}
.sub {{ color:var(--dim); margin:0 0 26px; font-size:.9rem }}
section {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:20px; margin-bottom:20px }}
.warn {{ border-color:var(--warn); background:var(--warnbg) }}
.warn h2 {{ color:var(--warn) }}
.warn ul {{ margin:0; padding-left:20px }}
.warn li {{ margin-bottom:8px }}
.cards {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  margin-bottom:20px }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; display:flex; flex-direction:column; gap:3px }}
.lbl {{ color:var(--dim); font-size:.72rem; text-transform:uppercase; letter-spacing:.05em }}
.val {{ font-size:1.4rem; font-weight:600; font-variant-numeric:tabular-nums }}
.note {{ color:var(--dim); font-size:.7rem }}
.chart {{ height:340px; width:100% }}
.chart-title {{ font-weight:600; margin:0 0 10px }}
.scroll {{ overflow-x:auto }}
table {{ border-collapse:collapse; width:100%; font-size:.87rem;
  font-variant-numeric:tabular-nums; white-space:nowrap }}
th {{ text-align:left; color:var(--dim); font-weight:600; font-size:.72rem;
  text-transform:uppercase; letter-spacing:.04em; padding:8px 12px 8px 0;
  border-bottom:1px solid var(--line) }}
td {{ padding:9px 12px 9px 0; border-bottom:1px solid var(--line) }}
tr:last-child td {{ border-bottom:none }}
.pill {{ padding:2px 8px; border-radius:99px; font-size:.72rem; font-weight:600 }}
.win {{ background:rgba(38,166,154,.16); color:var(--pos) }}
.loss {{ background:rgba(239,83,80,.16); color:var(--neg) }}
.unk {{ background:rgba(139,148,158,.16); color:var(--dim) }}
.pos {{ color:var(--pos) }} .neg {{ color:var(--neg) }}
.empty {{ color:var(--dim); margin:0; font-size:.9rem }}
.split {{ display:grid; gap:20px; grid-template-columns:minmax(280px,340px) 1fr;
  align-items:start }}
@media (max-width:760px) {{ .split {{ grid-template-columns:1fr }} }}
.calib {{ width:100%; max-width:340px; height:auto }}
.calib .grid {{ stroke:var(--line); stroke-width:1 }}
.calib .perfect {{ stroke:var(--dim); stroke-width:1.5; stroke-dasharray:5 4 }}
.calib .pt {{ fill:var(--accent); fill-opacity:.65; stroke:var(--accent) }}
.calib .tick {{ fill:var(--dim); font-size:10px; text-anchor:middle }}
.calib .ty {{ text-anchor:end }}
.calib .axis {{ fill:var(--dim); font-size:11px; text-anchor:middle }}
code {{ background:rgba(127,127,127,.14); padding:1px 5px; border-radius:4px; font-size:.85em }}
</style>
<main>
<h1>{title}</h1>
<p class="sub">Strategy <code>{result.strategy}</code> ·
  tape {result.tape.get("tape_start", "?")} → {result.tape.get("tape_end", "?")} ·
  settlement via <code>{result.parameters.get("settlement_source", "?")}</code></p>

{warn_block}

<div class="cards">{cards}</div>

<section>
  <h2>Underlying &amp; entries</h2>
  <div id="price-charts"></div>
</section>

<section>
  <h2>Realized P&amp;L</h2>
  <div id="equity" class="chart"></div>
  <p class="empty">Stepped on purpose: P&amp;L only changes at settlement, and a
  smooth line would imply values that never existed.</p>
</section>

<section class="split">
  <div>
    <h2>Calibration</h2>
    {_calibration_svg(result)}
    <p class="empty">Points on the dashed line are perfectly calibrated. Bubble
    size is sample count. This, not P&amp;L, is how "is it working?" gets answered.</p>
  </div>
  <div>
    <h2>Why markets were skipped</h2>
    {_skips_table(result)}
  </div>
</section>

<section>
  <h2>Trades</h2>
  {_trades_table(result)}
</section>

<script>{_library()}</script>
<script>
const DATA = {payload};
const dark = matchMedia('(prefers-color-scheme: dark)').matches;
const theme = {{
  layout: {{ background: {{ color: 'transparent' }}, textColor: dark ? '#8b949e' : '#636c76' }},
  grid: {{ vertLines: {{ color: dark ? '#21262d' : '#eaeef2' }},
           horzLines: {{ color: dark ? '#21262d' : '#eaeef2' }} }},
  rightPriceScale: {{ borderColor: dark ? '#272e39' : '#d8dee4' }},
  timeScale: {{ borderColor: dark ? '#272e39' : '#d8dee4', timeVisible: true }},
  crosshair: {{ mode: 0 }},
  autoSize: true,
}};

const host = document.getElementById('price-charts');
if (!DATA.charts.length) {{
  host.innerHTML = '<p class="empty">No underlying candles were loaded, so there is ' +
    'nothing to plot. Pass market data to the report to see price context.</p>';
}}
for (const spec of DATA.charts) {{
  const title = document.createElement('p');
  title.className = 'chart-title';
  title.textContent = spec.symbol;
  host.appendChild(title);

  const el = document.createElement('div');
  el.className = 'chart';
  host.appendChild(el);

  const chart = LightweightCharts.createChart(el, theme);
  const candles = chart.addCandlestickSeries({{
    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350',
  }});
  candles.setData(spec.candles);
  if (spec.markers.length) candles.setMarkers(spec.markers);
  for (const strike of spec.strikes) {{
    candles.createPriceLine({{
      price: strike.price, color: '#58a6ff', lineWidth: 1, lineStyle: 2,
      axisLabelVisible: true, title: strike.label,
    }});
  }}
  // Zoom to the entries. The candle history runs much longer than the tape
  // because the vol lookback needs it, so fitting everything would squash
  // every marker into the final pixel column. Scroll/zoom still works.
  if (spec.focus) {{
    chart.timeScale().setVisibleRange({{ from: spec.focus.from, to: spec.focus.to }});
  }} else {{
    chart.timeScale().fitContent();
  }}
}}

const eq = document.getElementById('equity');
if (DATA.equity.length) {{
  const chart = LightweightCharts.createChart(eq, theme);
  const line = chart.addLineSeries({{
    color: '#58a6ff', lineWidth: 2, lineType: 1,
    priceFormat: {{ type: 'price', precision: 2, minMove: 0.01 }},
  }});
  line.setData(DATA.equity);
  line.createPriceLine({{ price: 0, color: '#8b949e', lineWidth: 1, lineStyle: 2,
                          axisLabelVisible: false, title: 'flat' }});
  chart.timeScale().fitContent();
}} else {{
  eq.innerHTML = '<p class="empty">No settlements occurred, so realized P&amp;L ' +
    'never moved from zero.</p>';
}}
</script>
</main>
"""


def write_backtest_report(
    result: BacktestResult,
    path: str | Path,
    data: MarketDataSet | None = None,
    *,
    title: str = "tradetk backtest",
) -> Path:
    """Render and write the report, returning the path written."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_backtest_report(result, data, title=title), encoding="utf-8")
    return out
