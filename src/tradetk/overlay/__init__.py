"""Researched views from the vault, narrowing what the pipeline proposes.

Nothing in this package touches probability estimation. The overlay only
restricts which side may be bought, shrinks the position target, or raises the
edge a trade must clear — every one of which narrows. A stance can never permit
a trade the pipeline would otherwise refuse.
"""
