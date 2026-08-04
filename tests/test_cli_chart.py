"""Unit tests for the `chart` CLI helpers (no network, no venue)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from tradetk.backtest.replay import BookObservation
from tradetk.cli import chart as chart_mod
from tradetk.cli.chart import candles_to_ohlc, implied_prob_series, render_chart, series_span
from tradetk.signals.base import Candle
from tradetk.venues.base import BinaryBook, BookLevel


def _obs(ticker: str, minute: int, bid: str | None, ask: str | None) -> BookObservation:
    when = datetime(2026, 8, 4, 12, minute, tzinfo=timezone.utc)
    book = BinaryBook(
        ticker=ticker,
        retrieved_at=when,
        yes_bids=[BookLevel(price=Decimal(bid), size=Decimal("10"))] if bid else [],
        yes_asks=[BookLevel(price=Decimal(ask), size=Decimal("10"))] if ask else [],
    )
    return BookObservation(ticker=ticker, observed_at=when, book=book)


def test_implied_prob_series_filters_ticker_and_computes_mid() -> None:
    obs = [
        _obs("KXBTCD-A", 0, "0.40", "0.42"),   # mid 0.41
        _obs("KXETHD-Z", 1, "0.10", "0.12"),   # other ticker, excluded
        _obs("KXBTCD-A", 2, "0.44", "0.46"),   # mid 0.45
    ]
    series = implied_prob_series(obs, "KXBTCD-A")
    assert [round(p, 2) for _, p in series] == [0.41, 0.45]
    assert [dt.minute for dt, _ in series] == [0, 2]


def test_implied_prob_series_skips_one_sided_books() -> None:
    obs = [
        _obs("KXBTCD-A", 0, "0.40", None),     # no ask -> mid None -> skipped
        _obs("KXBTCD-A", 1, "0.44", "0.46"),   # mid 0.45
    ]
    series = implied_prob_series(obs, "KXBTCD-A")
    assert [round(p, 2) for _, p in series] == [0.45]


def _candle(open_ms: int, o: float, h: float, l: float, c: float) -> Candle:  # noqa: E741
    return Candle(
        symbol="BTC", interval="1h", open_ms=open_ms, close_ms=open_ms + 3_600_000,
        o=o, h=h, l=l, c=c, v=1.0, trades=1,
    )


def test_candles_to_ohlc_orders_and_converts() -> None:
    times, o, h, l, c = candles_to_ohlc([  # noqa: E741
        _candle(1_000_000, 100, 110, 95, 105),
        _candle(4_600_000, 105, 120, 104, 118),
    ])
    assert [t.tzinfo is not None for t in times] == [True, True]
    assert times[0] < times[1]
    assert o == [100.0, 105.0]
    assert h == [110.0, 120.0]
    assert l == [95.0, 104.0]
    assert c == [105.0, 118.0]


def test_series_span_returns_first_and_last() -> None:
    s = [
        (datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc), 0.4),
        (datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc), 0.5),
    ]
    start, end = series_span(s)
    assert start.hour == 12 and end.hour == 13


def test_render_chart_writes_a_nonempty_png(tmp_path) -> None:
    t0 = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)
    prob = [(t0, 0.41), (t1, 0.45)]
    ohlc = ([t0, t1], [100.0, 105.0], [110.0, 120.0], [95.0, 104.0], [105.0, 118.0])
    out = tmp_path / "chart.png"
    result = render_chart(
        ticker="KXBTCD-A", symbol="BTC", prob_series=prob, ohlc=ohlc,
        out_path=str(out), strike=112.0,
    )
    assert result == str(out)
    assert out.exists()
    assert out.stat().st_size > 1000  # a real PNG, not an empty file


class _FakeProvider:
    def __enter__(self): return self
    def __exit__(self, *exc): return None
    def candles(self, symbol, interval, start_ms, end_ms):
        return [_candle(start_ms, 100, 110, 95, 105), _candle(start_ms + 3_600_000, 105, 120, 104, 118)]


def test_main_renders_and_reports(tmp_path, monkeypatch, capsys) -> None:
    ticker = "KXBTCD-A"
    obs = [_obs(ticker, 0, "0.40", "0.42"), _obs(ticker, 30, "0.44", "0.46")]

    class _Replay:
        @classmethod
        def from_tape(cls, tape_dir): return cls()
        def observations(self): return iter(obs)
        @property
        def tickers(self): return {ticker}
        @property
        def span(self):
            return obs[0].observed_at, obs[-1].observed_at
        def claim_as_of(self, tk, when, registry):
            class _C:
                underlying = "BTC"
                threshold = Decimal("112")
            return _C()

    monkeypatch.setattr(chart_mod, "TapeReplay", _Replay)
    monkeypatch.setattr(chart_mod, "UnderlyingRegistry",
                        type("R", (), {"from_yaml": staticmethod(lambda p: object())}))
    monkeypatch.setattr(chart_mod, "_provider_factory", lambda: _FakeProvider())

    out = tmp_path / "out.png"
    rc = chart_mod.main(["--ticker", ticker, "--out", str(out), "--tape-dir", "unused",
                         "--registry", "unused"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["symbol"] == "BTC"
    assert payload["prob_points"] == 2
    assert payload["candles"] == 2
    assert out.exists() and out.stat().st_size > 1000


def test_main_errors_when_ticker_absent_from_tape(tmp_path, monkeypatch, capsys) -> None:
    class _Empty:
        @classmethod
        def from_tape(cls, tape_dir): return cls()
        def observations(self): return iter([])
        @property
        def tickers(self): return set()
        @property
        def span(self):
            raise IndexError
        def claim_as_of(self, tk, when, registry): return None

    monkeypatch.setattr(chart_mod, "TapeReplay", _Empty)
    monkeypatch.setattr(chart_mod, "UnderlyingRegistry",
                        type("R", (), {"from_yaml": staticmethod(lambda p: object())}))
    rc = chart_mod.main(["--ticker", "KXBTCD-A", "--out", str(tmp_path / "x.png"),
                         "--tape-dir", "unused", "--registry", "unused", "--symbol", "BTC"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
