"""The cost model (docs/architecture.md section 5.4; roadmap step 4.2).

Every amount is in the quote currency (USDT) and every rate a fraction
(0.0005 is 0.05%). Notionals are signed: positive buys or long positions,
negative sells or short positions.

A trade costs three things, each non-negative:

- **fee**: ``|notional| x rate``, the exchange's maker or taker rate
  (``config/costs.toml``);
- **spread**: ``|notional| x half_spread``, crossing half the bid-ask spread;
- **impact**: ``|notional| x y x daily_vol x sqrt(|notional| / daily_volume)``,
  the square-root law (Tóth et al. 2011). The peak impact is charged on the
  whole trade, which overstates the average and so errs on the costly side.

The square-root law prices a *metaorder*, all the child orders one trader
sends in one direction, not each child alone. Priced per child, splitting a
trade into k pieces would cut its impact by sqrt(k): 100 pieces of a $100,000
trade would pay a tenth of what the trade pays. So a trade that continues a
metaorder pays the increase it causes, ``cost(prior + |notional|) - cost(prior)``
with ``prior`` the notional the metaorder already traded, and the pieces add up
to exactly what one trade of the whole would pay. The simulator decides what
continues a metaorder (``sim.engine``): same instrument, same direction, within
``metaorder_window`` of its first fill (``config/costs.toml``; a day, the
period ``daily_volume`` is counted over).

A cost scenario (zero, normal, double) multiplies all three. Funding is not a
cost assumption but a cash flow the data records, possibly income, so it is
never scaled: holding ``position`` through a settlement with rate ``rate``
moves cash by ``-position x rate`` (a long pays a positive rate, a short
receives it), at the settlement instants the data shows (roadmap 4.0: 8, 4 or
1 hours).
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from finresearch.core.config import load_toml


class CostError(ValueError):
    """The inputs do not describe a trade the cost model can price."""


class Liquidity(StrEnum):
    MAKER = "maker"  # a resting order that was filled
    TAKER = "taker"  # an order that took resting liquidity


class Scenario(StrEnum):
    ZERO = "zero"
    NORMAL = "normal"
    DOUBLE = "double"


@dataclass(frozen=True, slots=True)
class Fees:
    maker: float
    taker: float

    def __post_init__(self) -> None:
        for name, rate in (("maker", self.maker), ("taker", self.taker)):
            if not 0.0 <= rate < 1.0:
                raise CostError(f"{name} fee must be in [0, 1), got {rate}")

    def rate(self, liquidity: Liquidity) -> float:
        return self.maker if liquidity is Liquidity.MAKER else self.taker


@dataclass(frozen=True, slots=True)
class TradeCost:
    fee: float
    spread: float
    impact: float

    @property
    def total(self) -> float:
        return self.fee + self.spread + self.impact


def fee(notional: float, fees: Fees, liquidity: Liquidity) -> float:
    return abs(notional) * fees.rate(liquidity)


def spread_cost(notional: float, half_spread: float) -> float:
    if half_spread < 0.0:
        raise CostError(f"half spread must not be negative, got {half_spread}")
    return abs(notional) * half_spread


def impact_fraction(notional: float, daily_volume: float, daily_vol: float, y: float) -> float:
    """Price impact as a fraction of price: ``y x daily_vol x sqrt(|notional| / daily_volume)``."""
    if daily_volume <= 0.0:
        raise CostError(f"daily volume must be positive, got {daily_volume}")
    if daily_vol < 0.0 or y < 0.0:
        raise CostError(f"volatility and y must not be negative, got {daily_vol}, {y}")
    return y * daily_vol * math.sqrt(abs(notional) / daily_volume)


def impact_cost(
    notional: float, daily_volume: float, daily_vol: float, y: float, prior: float = 0.0
) -> float:
    """Impact of ``notional`` traded after ``prior`` (unsigned) in the same metaorder."""
    if not prior >= 0.0:
        raise CostError(f"prior notional must not be negative, got {prior}")
    total = prior + abs(notional)
    return total * impact_fraction(total, daily_volume, daily_vol, y) - prior * impact_fraction(
        prior, daily_volume, daily_vol, y
    )


def funding_cash(position: float, rate: float) -> float:
    """Cash moved by one funding settlement: a long pays a positive rate, a short receives it."""
    if not math.isfinite(rate):
        raise CostError(f"funding rate must be finite, got {rate}")
    return -position * rate


@dataclass(frozen=True, slots=True)
class CostModel:
    fees: Mapping[str, Fees]  # by exchange
    y: float
    multiplier: float  # the cost scenario's
    metaorder_window: timedelta  # how long same-direction trades count as one metaorder

    def __post_init__(self) -> None:
        if self.metaorder_window <= timedelta(0):
            raise CostError(f"metaorder window must be positive, got {self.metaorder_window}")

    def trade(
        self,
        exchange: str,
        notional: float,
        liquidity: Liquidity,
        *,
        half_spread: float,
        daily_volume: float,
        daily_vol: float,
        prior: float = 0.0,
    ) -> TradeCost:
        """What a trade of ``notional`` costs under this scenario, ``prior`` into its metaorder."""
        fees = self.fees.get(exchange)
        if fees is None:
            raise CostError(f"no fees for exchange {exchange!r} (have {sorted(self.fees)})")
        m = self.multiplier
        return TradeCost(
            fee=m * fee(notional, fees, liquidity),
            spread=m * spread_cost(notional, half_spread),
            impact=m * impact_cost(notional, daily_volume, daily_vol, self.y, prior),
        )


def cost_model(scenario: Scenario, config: Mapping[str, Any] | None = None) -> CostModel:
    """The cost model of ``scenario``, from ``config/costs.toml`` unless ``config`` is given."""
    cfg = load_toml("costs") if config is None else config
    fees = {name: Fees(float(f["maker"]), float(f["taker"])) for name, f in cfg["fees"].items()}
    multiplier = float(cfg["scenarios"][scenario.value])
    if multiplier < 0.0:
        raise CostError(f"scenario {scenario} has a negative multiplier {multiplier}")
    window = timedelta(hours=float(cfg["impact"]["metaorder_hours"]))
    return CostModel(fees, float(cfg["impact"]["y"]), multiplier, window)


def all_scenarios(config: Mapping[str, Any] | None = None) -> dict[Scenario, CostModel]:
    """The three cost scenarios at once."""
    return {s: cost_model(s, config) for s in Scenario}
