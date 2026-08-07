from decimal import Decimal

from tradetk.state.settle import settle_position
from tradetk.venues.base import VenueMarket

D = Decimal


def _mkt(status="finalized", result="yes"):
    return VenueMarket(ticker="T", title="x", status=status, result=result)


def test_yes_position_wins_when_resolved_yes():
    out = settle_position(side="yes", contracts=5, cost=D("2.00"), market=_mkt(result="yes"))
    assert out.proceeds == D("5") and out.realized_pnl == D("3.00")


def test_yes_position_loses_when_resolved_no():
    out = settle_position(side="yes", contracts=5, cost=D("2.00"), market=_mkt(result="no"))
    assert out.proceeds == D("0") and out.realized_pnl == D("-2.00")


def test_no_position_wins_when_resolved_no():
    out = settle_position(side="no", contracts=4, cost=D("1.50"), market=_mkt(result="no"))
    assert out.proceeds == D("4") and out.realized_pnl == D("2.50")


def test_unresolved_market_is_pending():
    assert settle_position(side="yes", contracts=5, cost=D("2"), market=_mkt(status="open", result=None)) is None
    assert settle_position(side="yes", contracts=5, cost=D("2"), market=_mkt(status="finalized", result="")) is None
