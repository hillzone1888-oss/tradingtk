from datetime import date, datetime, timezone
from decimal import Decimal

from tradetk.state.ledger import (
    append_events,
    fill_event,
    project,
    read_ledger,
    reset_event,
    settle_event,
)

D = Decimal
TODAY = date(2026, 8, 6)


def _ts(day=6, hour=12):
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


def _fill(ticker, underlying, side, contracts, cost, ts):
    return fill_event(ticker=ticker, underlying=underlying, side=side, contracts=contracts,
                      assumed_price=D(str(cost)) / contracts, fee=D("0"), cost=D(str(cost)),
                      resolution_time=_ts(day=7), ts=ts)


def test_open_book_is_fills_without_a_later_settle():
    events = [_fill("A", "BTC", "yes", 5, "2.00", _ts())]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert len(book.open) == 1
    assert book.open[0].ticker == "A" and book.open[0].cost == D("2.00")
    assert book.capital_deployed == D("2.00")


def test_settle_removes_from_book_and_scores_realized_today():
    events = [
        _fill("A", "BTC", "yes", 5, "2.00", _ts(hour=10)),
        settle_event(ticker="A", result="no", side="yes", contracts=5,
                     proceeds=D("0"), realized_pnl=D("-2.00"),
                     resolution_time=_ts(day=7), ts=_ts(hour=11)),
    ]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert book.open == ()
    assert book.realized_today == D("-2.00")


def test_realized_today_excludes_other_days():
    events = [
        settle_event(ticker="A", result="yes", side="yes", contracts=1, proceeds=D("1"),
                     realized_pnl=D("-1.00"), resolution_time=_ts(day=5), ts=_ts(day=5)),
        settle_event(ticker="B", result="yes", side="yes", contracts=1, proceeds=D("1"),
                     realized_pnl=D("-3.00"), resolution_time=_ts(day=6), ts=_ts(day=6)),
    ]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert book.realized_today == D("-3.00")


def test_drawdown_is_peak_minus_current_realized_equity():
    events = [
        settle_event(ticker="A", result="yes", side="yes", contracts=6, proceeds=D("6"),
                     realized_pnl=D("4.00"), resolution_time=_ts(day=5), ts=_ts(day=5)),   # equity 24, peak 24
        settle_event(ticker="B", result="no", side="yes", contracts=7, proceeds=D("0"),
                     realized_pnl=D("-7.00"), resolution_time=_ts(day=6), ts=_ts(day=6)),  # equity 17
    ]
    book = project(events, starting_capital=D("20"), today=TODAY)
    assert book.drawdown == D("7.00")


def test_drawdown_latches_on_halt_event_and_clears_on_reset():
    from tradetk.state.ledger import halt_event
    breach = halt_event(reason="drawdown_halt", realized_today=D("0"), drawdown=D("9"),
                        data_age_seconds=D("0"), ts=_ts(hour=9))
    assert project([breach], starting_capital=D("20"), today=TODAY).drawdown_latched is True
    cleared = [breach, reset_event(note="manual", ts=_ts(hour=10))]
    assert project(cleared, starting_capital=D("20"), today=TODAY).drawdown_latched is False


def test_append_is_idempotent_by_key(tmp_path):
    path = tmp_path / "ledger.jsonl"
    e = _fill("A", "BTC", "yes", 5, "2.00", _ts())
    assert append_events(path, [e]) == 1
    assert append_events(path, [e]) == 0          # same key, skipped
    assert len(read_ledger(path)) == 1


def test_risk_state_projection_matches_open_book():
    events = [
        _fill("A", "BTC", "yes", 5, "2.00", _ts()),
        _fill("B", "BTC", "no", 4, "1.50", _ts()),
    ]
    rs = project(events, starting_capital=D("20"), today=TODAY).risk_state()
    assert rs.slots_used == 2 and rs.slots_for("BTC") == 2
    assert rs.capital_deployed == D("3.50")
