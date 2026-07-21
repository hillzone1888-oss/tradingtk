"""Shared enums used across config, signals, venues, and strategies.

Kept in one dependency-free module so both `config` and `signals` can import it
without creating an import cycle.
"""

from __future__ import annotations

from enum import Enum


class Mode(str, Enum):
    """What the toolkit is permitted to *do* on a run.

    Orthogonal to venue environment (demo/prod) — that is `Env`.
    """

    shadow = "shadow"  # score the whole universe; touch no venue. Default.
    paper = "paper"  # simulated fills against real recorded books.
    live = "live"  # the ONLY mode `execute` runs in, and only with gates set.


class VenueName(str, Enum):
    kalshi = "kalshi"
    polymarket_us = "polymarket_us"  # added once credentials land


class Env(str, Enum):
    demo = "demo"  # Kalshi demo-api.kalshi.co / venue sandbox. Default.
    prod = "prod"  # real money; requires an explicit second flag.


class ProviderName(str, Enum):
    hyperliquid = "hyperliquid"  # native; default + fallback
    moondev = "moondev"  # opt-in; paid tier exceeds a $20 book


class Capability(str, Enum):
    """Signals a strategy may declare it requires.

    Startup validates that the union of enabled providers' capabilities covers
    every capability a strategy declares. Missing capability => fail loudly.
    Never silently substitute zeros or stale values.
    """

    # ── base data, available natively from Hyperliquid ──
    SPOT_PRICE = "spot_price"
    PERP_PRICE = "perp_price"
    CANDLES = "candles"
    ORDERBOOK = "orderbook"
    FUNDING = "funding"
    REALIZED_VOL = "realized_vol"
    # ── Moon Dev-only signals (no native equivalent) ──
    LIQUIDATIONS = "liquidations"
    HLP_SENTIMENT = "hlp_sentiment"
    POSITION_SNAPSHOTS = "position_snapshots"
    SMART_MONEY = "smart_money"
    ORDER_FLOW = "order_flow"
    POLY_WHALES = "poly_whales"  # Polymarket GLOBAL flow — external signal only
