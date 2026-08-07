from decimal import Decimal

from tradetk.risk import BookHealth, HaltLimits, screen_halts

D = Decimal
LIMITS = HaltLimits(
    max_daily_loss_dollars=D("5"),
    max_total_drawdown_dollars=D("8"),
    data_staleness_halt_seconds=D("300"),
)


def _health(realized=D("0"), drawdown=D("0"), age=D("0"), latched=False):
    return BookHealth(realized_today=realized, drawdown=drawdown,
                      data_age_seconds=age, drawdown_latched=latched)


def test_healthy_book_is_admitted():
    assert screen_halts(_health(), LIMITS).admitted is True


def test_daily_loss_at_limit_halts():
    d = screen_halts(_health(realized=D("-5")), LIMITS)
    assert d.admitted is False and d.reason == "daily_loss_halt"


def test_daily_loss_just_under_limit_is_admitted():
    assert screen_halts(_health(realized=D("-4.99")), LIMITS).admitted is True


def test_daily_profit_never_halts():
    assert screen_halts(_health(realized=D("50")), LIMITS).admitted is True


def test_drawdown_at_limit_halts():
    d = screen_halts(_health(drawdown=D("8")), LIMITS)
    assert d.admitted is False and d.reason == "drawdown_halt"


def test_drawdown_latch_halts_even_when_current_drawdown_is_zero():
    d = screen_halts(_health(drawdown=D("0"), latched=True), LIMITS)
    assert d.admitted is False and d.reason == "drawdown_halt"


def test_staleness_strictly_greater_than_limit_halts():
    assert screen_halts(_health(age=D("301")), LIMITS).reason == "stale_data_halt"
    assert screen_halts(_health(age=D("300")), LIMITS).admitted is True


def test_drawdown_outranks_daily_loss_when_both_trip():
    d = screen_halts(_health(realized=D("-9"), drawdown=D("9")), LIMITS)
    assert d.reason == "drawdown_halt"


def test_from_config_reads_risk_block():
    class _R:
        max_daily_loss_dollars = 5.0
        max_total_drawdown_dollars = 8.0
        data_staleness_halt_seconds = 300.0

    class _C:
        risk = _R()

    limits = HaltLimits.from_config(_C())
    assert limits.max_daily_loss_dollars == D("5.0")
    assert limits.data_staleness_halt_seconds == D("300.0")
