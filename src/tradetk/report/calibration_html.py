"""Self-contained HTML calibration report.

Kept apart from the backtest report because it answers a different question. A
backtest asks "what would this have earned"; calibration asks "is the forecast
any good, and better than the price we would pay to disagree". On a $20 book the
second question is the real one, and giving it its own page keeps it from being
read as an appendix to a P&L figure.

No charting library here. A reliability diagram has no time axis, and forcing it
into a time-series chart would misread as one; a small inline SVG is both more
honest and lighter. Model and market are drawn on the *same* axes, because the
model's curve in isolation invites being graded on its own rather than compared.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tradetk.report.html import _rows
from tradetk.shadow.calibration import CalibrationReport, Comparison


def _reliability_svg(comparison: Comparison, *, size: int = 340) -> str:
    model, market = comparison.model, comparison.market
    if not model.buckets and not market.buckets:
        return '<p class="empty">Nothing has settled yet, so there is nothing to calibrate.</p>'

    pad = 38
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

    def series(buckets: list[Any], css: str, label: str) -> str:
        if not buckets:
            return ""
        biggest = max(b.n for b in buckets)
        path = " ".join(
            f"{x(b.mean_predicted):.1f},{y(b.observed_frequency):.1f}" for b in buckets
        )
        dots = "".join(
            f'<circle class="pt {css}" cx="{x(b.mean_predicted):.1f}" '
            f'cy="{y(b.observed_frequency):.1f}" r="{3.5 + 7 * (b.n / biggest):.1f}">'
            f"<title>{label}: predicted {b.mean_predicted:.3f}, observed "
            f"{b.observed_frequency:.3f}, n={b.n}</title></circle>"
            for b in buckets
        )
        return f'<polyline class="ln {css}" points="{path}"/>{dots}'

    return (
        f'<svg viewBox="0 0 {size} {size}" class="calib" role="img" '
        f'aria-label="Reliability diagram comparing model and market forecasts">'
        f'{ticks}'
        f'<line class="perfect" x1="{x(0)}" y1="{y(0)}" x2="{x(1)}" y2="{y(1)}"/>'
        f'{series(market.buckets, "market", "market")}'
        f'{series(model.buckets, "model", "model")}'
        f'<text class="axis" x="{size / 2}" y="{size - 4}">predicted probability</text>'
        f'<text class="axis" x="-{size / 2}" y="12" transform="rotate(-90)">'
        f"observed frequency</text></svg>"
        '<p class="legend"><span class="key model"></span>model'
        '<span class="key market"></span>market mid'
        '<span class="key perfectkey"></span>perfectly calibrated</p>'
    )


def _comparison_rows(comparison: Comparison) -> str:
    model, market = comparison.model, comparison.market

    def cell(value: float | None, digits: int = 4) -> str:
        return f"{value:.{digits}f}" if value is not None else "—"

    better = comparison.edge_exists
    css = "pos" if better else "neg" if better is False else ""
    return _rows(
        ["", "model", "market mid", ""],
        [
            ["n", str(model.n), str(market.n), "same contracts in both columns"],
            [
                "Brier",
                f'<span class="{css}">{cell(model.brier)}</span>',
                cell(market.brier),
                "lower is better; 0.25 = always saying 50%",
            ],
            [
                "reliability", cell(model.reliability), cell(market.reliability),
                "lower better — miscalibration is fixable by remapping",
            ],
            [
                "resolution", cell(model.resolution), cell(market.resolution),
                "higher better — zero means the forecast carries no signal",
            ],
            [
                "uncertainty", cell(model.uncertainty), cell(market.uncertainty),
                "base-rate variance; a property of the contracts, not of us",
            ],
            [
                "base rate", cell(model.base_rate, 3), cell(market.base_rate, 3),
                "share of contracts resolving YES",
            ],
        ],
        empty="No scored forecasts.",
    )


def _segment_table(segments: dict[str, Comparison]) -> str:
    rows: list[list[str]] = []
    for name, comparison in segments.items():
        if not comparison.n_common:
            continue
        beats = comparison.edge_exists
        winner = (
            '<span class="pill win">model</span>' if beats
            else '<span class="pill loss">market</span>' if beats is False
            else '<span class="pill unk">—</span>'
        )
        rows.append(
            [
                name,
                str(comparison.n_common),
                f"{comparison.model.brier:.4f}" if comparison.model.brier is not None else "—",
                f"{comparison.market.brier:.4f}" if comparison.market.brier is not None else "—",
                winner,
            ]
        )
    return _rows(
        ["segment", "n", "model Brier", "market Brier", "better forecaster"],
        rows,
        empty="No segments with settled forecasts.",
    )


STYLE = """
:root {
  color-scheme: light dark;
  --bg:#0e1116; --panel:#161b22; --line:#272e39; --tx:#e6edf3; --dim:#8b949e;
  --pos:#26a69a; --neg:#ef5350; --warn:#d29922; --warnbg:#211a09; --accent:#58a6ff;
  --posbg:#0d211d; --negbg:#241214;
}
@media (prefers-color-scheme: light) {
  :root { --bg:#f6f8fa; --panel:#fff; --line:#d8dee4; --tx:#1f2328; --dim:#636c76;
          --warnbg:#fff8e5; --posbg:#e8f5f2; --negbg:#fdeceb; }
}
* { box-sizing:border-box }
body { margin:0; padding:28px 20px 60px; background:var(--bg); color:var(--tx);
  font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif }
main { max-width:1040px; margin:0 auto }
h1 { font-size:1.5rem; margin:0 0 4px }
h2 { font-size:1.05rem; margin:0 0 14px; font-weight:600 }
.sub { color:var(--dim); margin:0 0 26px; font-size:.9rem }
section { background:var(--panel); border:1px solid var(--line); border-radius:12px;
  padding:20px; margin-bottom:20px }
.warn { border-color:var(--warn); background:var(--warnbg) }
.warn h2 { color:var(--warn) }
.warn ul { margin:0; padding-left:20px } .warn li { margin-bottom:8px }
.verdict { border-width:2px; font-size:1.05rem }
.verdict.good { border-color:var(--pos); background:var(--posbg) }
.verdict.bad { border-color:var(--neg); background:var(--negbg) }
.verdict.warn { border-color:var(--warn); background:var(--warnbg) }
.verdict p { margin:0 }
.split { display:grid; gap:22px; grid-template-columns:minmax(300px,380px) 1fr;
  align-items:start }
@media (max-width:800px) { .split { grid-template-columns:1fr } }
.scroll { overflow-x:auto }
table { border-collapse:collapse; width:100%; font-size:.87rem;
  font-variant-numeric:tabular-nums }
th { text-align:left; color:var(--dim); font-weight:600; font-size:.72rem;
  text-transform:uppercase; letter-spacing:.04em; padding:8px 12px 8px 0;
  border-bottom:1px solid var(--line) }
td { padding:9px 12px 9px 0; border-bottom:1px solid var(--line) }
td:last-child { color:var(--dim); font-size:.82rem }
tr:last-child td { border-bottom:none }
.pill { padding:2px 8px; border-radius:99px; font-size:.72rem; font-weight:600 }
.win { background:rgba(38,166,154,.16); color:var(--pos) }
.loss { background:rgba(239,83,80,.16); color:var(--neg) }
.unk { background:rgba(139,148,158,.16); color:var(--dim) }
.pos { color:var(--pos); font-weight:600 } .neg { color:var(--neg); font-weight:600 }
.empty { color:var(--dim); margin:0; font-size:.9rem }
.calib { width:100%; max-width:360px; height:auto }
.calib .grid { stroke:var(--line); stroke-width:1 }
.calib .perfect { stroke:var(--dim); stroke-width:1.5; stroke-dasharray:5 4 }
.calib .model { stroke:var(--accent); fill:var(--accent) }
.calib .market { stroke:var(--warn); fill:var(--warn) }
/* Element-qualified so it outranks the colour rules above, which also set
   fill for the dots. Without this the reliability lines render as filled
   blobs and the diagram becomes unreadable. */
.calib polyline.ln { fill:none; stroke-width:2 }
.calib circle.pt { fill-opacity:.7 }
.calib .tick { fill:var(--dim); font-size:10px; text-anchor:middle }
.calib .ty { text-anchor:end }
.calib .axis { fill:var(--dim); font-size:11px; text-anchor:middle }
.legend { color:var(--dim); font-size:.8rem; margin:6px 0 0 }
.key { display:inline-block; width:11px; height:11px; border-radius:3px;
  margin:0 5px 0 14px; vertical-align:-1px }
.legend .key:first-child { margin-left:0 }
.key.model { background:var(--accent) } .key.market { background:var(--warn) }
.key.perfectkey { background:var(--dim) }
code { background:rgba(127,127,127,.14); padding:1px 5px; border-radius:4px; font-size:.85em }
"""


def render_calibration_report(
    report: CalibrationReport, *, title: str = "tradetk calibration"
) -> str:
    """Render the whole report as one self-contained page, verdict first."""
    warnings = report.honesty_warnings()
    warn_html = "".join(f"<li>{w}</li>" for w in warnings)
    warn_block = (
        f'<section class="warn"><h2>Read this before the numbers</h2>'
        f"<ul>{warn_html}</ul></section>"
        if warnings else ""
    )

    headline = report.independent
    beats = headline.edge_exists
    verdict_class = "good" if beats else "bad" if beats is False else "warn"
    strategies = ", ".join(report.strategies) or "none"

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{STYLE}</style>
<main>
<h1>{title}</h1>
<p class="sub">{report.n_observations} observations over {report.n_contracts}
  independent contracts · strategies: <code>{strategies}</code></p>

{warn_block}

<section class="verdict {verdict_class}">
  <h2>Does the model beat the market's own price?</h2>
  <p>{headline.as_dict()["verdict"]}</p>
</section>

<section class="split">
  <div>
    <h2>Reliability</h2>
    {_reliability_svg(headline)}
  </div>
  <div>
    <h2>Headline — one forecast per contract</h2>
    {_comparison_rows(headline)}
    <p class="empty" style="margin-top:12px">Brier decomposes as
    <code>reliability − resolution + uncertainty</code>. Poor reliability with
    good resolution is fixable by recalibrating; good reliability with no
    resolution means there is no signal to recalibrate.</p>
  </div>
</section>

<section>
  <h2>By market type</h2>
  {_segment_table(report.by_measured_reference)}
  <p class="empty" style="margin-top:12px">Measured-reference markets set their
  strike at spot by construction, so they are ~50/50 by design and must never be
  pooled with fixed strikes.</p>
</section>

<section>
  <h2>By underlying</h2>
  {_segment_table(report.by_underlying)}
</section>

<section>
  <h2>By time to resolution</h2>
  {_segment_table(report.by_horizon)}
</section>
</main>
"""


def write_calibration_report(
    report: CalibrationReport, path: str | Path, *, title: str = "tradetk calibration"
) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_calibration_report(report, title=title), encoding="utf-8")
    return out
