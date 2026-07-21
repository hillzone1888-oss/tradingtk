"""Typed configuration schema, validated on load.

Design invariants encoded here (not enforced by convention elsewhere):

* Every capital and risk limit is a **dollar amount** or a **probability-point**
  count. No percentages appear in the capital/risk sections — at a $20 book
  percentages are meaningless.
* Sizing target is a *constraint*, not a quantity: the sizer converts
  `position_target` dollars into an integer contract count downstream.
* Two orthogonal safety axes, each guarded by two independent flags:
    - execution: `mode == live`  requires  `live_trading_confirmed is True`
    - environment: `venue.environment == prod`  requires  `venue.use_production`
  `execute` layers interactive TTY + a typed phrase on top of these, in code.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator

from tradetk.enums import Capability, Env, Mode, ProviderName, VenueName


class _Strict(BaseModel):
    """Base: reject unknown keys so typos in YAML fail loudly, not silently."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CapitalConfig(_Strict):
    total_capital: PositiveFloat = Field(description="$ — hard ceiling on the whole book.")
    position_target: PositiveFloat = Field(description="$ — sizing target per position.")
    per_position_ceiling: PositiveFloat = Field(
        description="$ — hard reject if even 1 contract's cost exceeds this."
    )
    max_positions: int = Field(ge=1, le=20, description="Concurrent open slots (5–8).")
    max_slots_per_underlying: int = Field(
        ge=1, description="Cap slots per underlying asset (prevents an all-BTC book)."
    )

    @model_validator(mode="after")
    def _ordering(self) -> "CapitalConfig":
        if not (self.position_target <= self.per_position_ceiling <= self.total_capital):
            raise ValueError(
                "require position_target <= per_position_ceiling <= total_capital "
                f"(got {self.position_target}, {self.per_position_ceiling}, {self.total_capital})"
            )
        if self.max_slots_per_underlying > self.max_positions:
            raise ValueError("max_slots_per_underlying cannot exceed max_positions")
        return self


class EdgeGateConfig(_Strict):
    min_net_edge_pp: float = Field(
        ge=0, description="Min edge in probability POINTS after fees + spread + margin."
    )
    margin_pp: float = Field(ge=0, description="Extra cushion required on top of modeled costs.")


class LiquidityConfig(_Strict):
    min_book_depth_multiple: PositiveFloat = Field(
        description="Visible depth must be >= this multiple of my order size to enter."
    )
    max_book_participation_pct: float = Field(
        gt=0, le=100, description="Cap order at this %% of visible book depth."
    )


class HorizonConfig(_Strict):
    max_hours_to_resolution: PositiveFloat = Field(
        description="Reject contracts resolving further out than this. Keep tight."
    )
    prefer_short_dated: bool = True


class RiskConfig(_Strict):
    max_daily_loss_dollars: PositiveFloat = Field(description="$ realized/day => halt new entries.")
    max_total_drawdown_dollars: PositiveFloat = Field(
        description="$ total => halt permanently until manual reset."
    )
    data_staleness_halt_seconds: PositiveFloat = Field(
        description="Halt if signal data is older than this."
    )


class OrdersConfig(_Strict):
    prefer_maker: bool = True
    allow_crossing: bool = Field(
        default=False, description="Crossing the spread (taker) requires this to be True."
    )
    limit_order_timeout_seconds: PositiveFloat = 120.0
    confirmation_phrase: str = Field(
        default="EXECUTE", min_length=1, description="Phrase typed interactively at execute time."
    )


class VenueConfig(_Strict):
    name: VenueName = VenueName.kalshi
    environment: Env = Env.demo
    use_production: bool = Field(
        default=False, description="Second, independent gate required for environment == prod."
    )

    @model_validator(mode="after")
    def _prod_gate(self) -> "VenueConfig":
        if self.environment is Env.prod and not self.use_production:
            raise ValueError(
                "environment == prod requires use_production: true (defense-in-depth)"
            )
        return self


class FeeScheduleConfig(_Strict):
    """Fallback multipliers ONLY. The live schedule is authoritative; these back
    an offline sanity check and trigger an alert on divergence."""

    taker_multiplier: float = Field(gt=0)
    maker_multiplier: float = Field(ge=0)
    roundup_to_cent: bool = True


class FeesConfig(_Strict):
    verify_against_schedule: bool = Field(
        default=True, description="Fetch and verify the live fee schedule at startup."
    )
    kalshi: FeeScheduleConfig


class ProviderConfig(_Strict):
    primary: ProviderName = ProviderName.hyperliquid
    moondev_enabled: bool = False
    moondev_tier: str = Field(default="standard", pattern="^(standard|qe)$")
    capabilities: dict[ProviderName, set[Capability]] = Field(
        description="Which capabilities each provider is allowed to supply."
    )


class RecorderConfig(_Strict):
    snapshot_interval_seconds: PositiveFloat = 60.0
    tape_dir: str = "data/tape"


class StrategyConfig(_Strict):
    name: str = Field(min_length=1)
    realized_vol_lookback_days: PositiveInt = 30


class PathsConfig(_Strict):
    proposals_dir: str = "proposals"
    state_db: str = "state/toolkit.sqlite"
    cache_dir: str = "data/cache"


class Config(_Strict):
    """Root config. Validated on load; see `loader.load_config`."""

    mode: Mode = Mode.shadow
    live_trading_confirmed: bool = False

    capital: CapitalConfig
    edge_gate: EdgeGateConfig
    liquidity: LiquidityConfig
    horizon: HorizonConfig
    risk: RiskConfig
    orders: OrdersConfig
    venue: VenueConfig
    provider: ProviderConfig
    fees: FeesConfig
    strategy: StrategyConfig
    recorder: RecorderConfig = RecorderConfig()
    paths: PathsConfig = PathsConfig()

    @model_validator(mode="after")
    def _live_gate(self) -> "Config":
        if self.mode is Mode.live and not self.live_trading_confirmed:
            raise ValueError(
                "mode: live requires live_trading_confirmed: true. "
                "(execute additionally demands an interactive TTY + typed phrase.)"
            )
        return self

    @model_validator(mode="after")
    def _crossing_needs_flag(self) -> "Config":
        # Belt-and-braces: a strategy cannot be configured to prefer taker fills
        # without the explicit crossing flag also being set.
        if not self.orders.prefer_maker and not self.orders.allow_crossing:
            raise ValueError(
                "prefer_maker: false implies crossing the spread; set orders.allow_crossing: true"
            )
        return self
