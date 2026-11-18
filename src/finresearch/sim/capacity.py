"""Capacity: how much capital an edge can carry before its own trading eats it (step 4.4).

Over one period (a year, a round trip, any unit the inputs share), a strategy
with capital ``A`` makes ``gross_edge x A`` before costs and trades ``trades``
times, each trade a fraction ``trade_size`` of ``A``. With the cost model of
``sim.costs`` (a linear part ``linear_cost`` = fee + half spread per unit
traded, and square-root impact charged at its peak), its net result is

    net(A) = a A - b A^1.5
    a = gross_edge - trades x trade_size x linear_cost
    b = trades x y x daily_vol x trade_size^1.5 / sqrt(daily_volume)

So the net return per unit of capital, ``a - b sqrt(A)``, falls as capital
grows, and:

- **break-even** capital, where the net result is zero: ``(a / b)^2``;
- **best** capital, where the net result in money is largest:
  ``(2a / 3b)^2 = 4/9`` of break-even, earning ``a A / 3`` there.

If the edge does not cover the linear costs (``a <= 0``) there is no capacity
at all: every size loses.
"""

import math
from dataclasses import dataclass


class CapacityError(ValueError):
    """The inputs do not describe a strategy."""


@dataclass(frozen=True, slots=True)
class Strategy:
    gross_edge: float  # gross result per period, as a fraction of capital
    trades: float  # trades per period
    trade_size: float  # each trade's notional, as a fraction of capital
    linear_cost: float  # fee + half spread, per unit traded
    y: float  # square-root impact coefficient
    daily_vol: float
    daily_volume: float  # in quote currency

    def __post_init__(self) -> None:
        if self.trades <= 0.0 or self.trade_size <= 0.0:
            raise CapacityError("a strategy trades: trades and trade size must be positive")
        if self.linear_cost < 0.0 or self.y < 0.0 or self.daily_vol < 0.0:
            raise CapacityError("costs, impact coefficient and volatility must not be negative")
        if self.daily_volume <= 0.0:
            raise CapacityError(f"daily volume must be positive, got {self.daily_volume}")

    @property
    def a(self) -> float:
        """Net return per unit of capital before impact."""
        return self.gross_edge - self.trades * self.trade_size * self.linear_cost

    @property
    def b(self) -> float:
        """Impact cost per unit of capital^1.5."""
        return (
            self.trades
            * self.y
            * self.daily_vol
            * self.trade_size**1.5
            / math.sqrt(self.daily_volume)
        )


@dataclass(frozen=True, slots=True)
class Capacity:
    break_even: float  # capital at which the net result is zero
    best: float  # capital that earns the most money
    best_net: float  # what it earns there, per period


def net_result(strategy: Strategy, capital: float) -> float:
    """Net result per period, in money, at ``capital``."""
    if capital < 0.0:
        raise CapacityError(f"capital must not be negative, got {capital}")
    return strategy.a * capital - strategy.b * capital**1.5


def capacity(strategy: Strategy) -> Capacity:
    """Break-even and best capital for ``strategy``."""
    a, b = strategy.a, strategy.b
    if a <= 0.0:
        return Capacity(0.0, 0.0, 0.0)
    if b == 0.0:
        return Capacity(math.inf, math.inf, math.inf)  # no impact: nothing limits size
    best = (2.0 * a / (3.0 * b)) ** 2
    return Capacity((a / b) ** 2, best, net_result(strategy, best))
