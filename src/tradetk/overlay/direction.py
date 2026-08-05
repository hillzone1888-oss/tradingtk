"""Which side of a contract expresses a bullish view.

`bias` cannot map onto `Side` directly. YES on "BTC above 100k" and NO on
"BTC below 90k" are both bullish bets, because the claim's operator already
carries a direction. Resolving this in one tested function keeps the inversion
risk in a single place rather than scattered through call sites.
"""

from __future__ import annotations

from tradetk.translation.claims import Claim, ClaimOperator
from tradetk.venues.base import Side

_BULLISH_BY_OPERATOR = {
    ClaimOperator.above: Side.yes,
    ClaimOperator.at_or_above: Side.yes,
    ClaimOperator.below: Side.no,
}


def bullish_side(claim: Claim) -> Side | None:
    """The side that pays when the underlying goes up.

    Returns ``None`` for claims that are not directional at all: a ``between``
    claim wins on the price staying inside a range, which is neither a bullish
    nor a bearish view, so a directional stance has nothing to say about it.
    """
    return _BULLISH_BY_OPERATOR.get(claim.operator)
