"""The event-driven simulator core (docs/architecture.md section 5.4; roadmap step 4.3).

The same loop will run the paper portfolio (Phase 7): it takes a time-ordered
stream of bars, funding settlements and orders, and books every fill and every
cash flow. Orders come from outside (the portfolio builder, later); this core
only executes and accounts.

Fill rules (``FillRules``, from ``config/simulator.toml``):

- An order decided at ``t`` becomes active at ``t + latency`` and can only fill
  on a bar that starts at or after that instant (decision 0012).
- **Market order**: fills on the first such bar at its open. Fee (taker), half
  the spread and impact are booked as costs (``sim.costs``); impact is further
  multiplied by the scenario's ``impact_multiplier``.
- **Limit order**: fills only if the price trades *through* the limit, not just
  touches it: a buy when the bar's low is below the limit, a sell when its high
  is above. It fills at the limit even if the bar opened better, pays the maker
  fee and no spread or impact, and fills ``limit_fill_fraction`` of its size;
  the rest is cancelled. It expires after ``expires_at``.
- **Funding** at instant ``T`` is settled on the position held before any fill
  at ``T``: settlements are processed before bars that start at ``T``. The cash
  moved is ``sim.costs.funding_cash(position x mark price, rate)``.

Accounting: cash starts at ``initial_cash``; a fill of ``q`` at ``p`` moves cash
by ``-q p`` and the costs; equity at each bar's end is cash plus every position
valued at its instrument's latest close.

``simulate_all`` runs every combination of the three cost scenarios and the two
fill scenarios (roadmap Phase 4 acceptance: one call gives all of them).
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from finresearch.core.config import load_toml
from finresearch.sim.costs import (
    CostModel,
    Liquidity,
    Scenario,
    TradeCost,
    all_scenarios,
    funding_cash,
)

_UTC_OFFSET = timedelta(0)


class SimulationError(ValueError):
    """The inputs do not describe something the simulator can run."""


class OrderKind(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class FillScenario(StrEnum):
    CONSERVATIVE = "conservative"
    STRESS = "stress"


@dataclass(frozen=True, slots=True)
class FillRules:
    latency: timedelta
    limit_fill_fraction: float
    impact_multiplier: float

    def __post_init__(self) -> None:
        if self.latency < timedelta(0):
            raise SimulationError(f"latency must not be negative, got {self.latency}")
        if not 0.0 < self.limit_fill_fraction <= 1.0:
            raise SimulationError(
                f"limit fill fraction must be in (0, 1], got {self.limit_fill_fraction}"
            )
        if self.impact_multiplier < 0.0:
            raise SimulationError(
                f"impact multiplier must not be negative, got {self.impact_multiplier}"
            )


def fill_rules(scenario: FillScenario, config: Mapping[str, Any] | None = None) -> FillRules:
    """The fill rules of ``scenario``, from ``config/simulator.toml`` unless ``config`` is given."""
    cfg = (load_toml("simulator") if config is None else config)[scenario.value]
    return FillRules(
        timedelta(seconds=float(cfg["latency_seconds"])),
        float(cfg["limit_fill_fraction"]),
        float(cfg["impact_multiplier"]),
    )


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: str
    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True, slots=True)
class FundingEvent:
    instrument: str
    at: datetime
    rate: float
    mark_price: float


@dataclass(frozen=True, slots=True)
class Market:
    """What the cost model needs about an instrument's market."""

    exchange: str
    half_spread: float
    daily_volume: float  # in quote currency
    daily_vol: float


@dataclass(frozen=True, slots=True)
class Order:
    decided_at: datetime
    instrument: str
    quantity: float  # signed: positive buys
    kind: OrderKind = OrderKind.MARKET
    limit_price: float | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    order: int  # index into the orders given
    at: datetime
    instrument: str
    quantity: float
    price: float
    liquidity: Liquidity
    cost: TradeCost


@dataclass(frozen=True, slots=True)
class FundingPaid:
    at: datetime
    instrument: str
    position: float
    rate: float
    cash: float


@dataclass(slots=True)
class Result:
    fills: list[Fill] = field(default_factory=lambda: list[Fill]())
    funding: list[FundingPaid] = field(default_factory=lambda: list[FundingPaid]())
    unfilled: list[int] = field(default_factory=lambda: list[int]())  # never filled or expired
    positions: dict[str, float] = field(default_factory=lambda: dict[str, float]())
    cash: float = 0.0
    equity: list[tuple[datetime, float]] = field(
        default_factory=lambda: list[tuple[datetime, float]]()
    )

    @property
    def costs(self) -> float:
        return math.fsum(f.cost.total for f in self.fills)


def _check_utc(t: datetime, what: str) -> None:
    if t.tzinfo is None or t.utcoffset() != _UTC_OFFSET:
        raise SimulationError(f"{what} must be in UTC, got {t!r}")


def _validate(
    bars: Sequence[Bar],
    orders: Sequence[Order],
    funding: Sequence[FundingEvent],
    markets: Mapping[str, Market],
) -> None:
    last: dict[str, datetime] = {}
    for b in bars:
        _check_utc(b.start, "bar start")
        if b.instrument in last and b.start <= last[b.instrument]:
            raise SimulationError(f"bars of {b.instrument} must be in time order without repeats")
        last[b.instrument] = b.start
        if not (b.low <= min(b.open, b.close) and max(b.open, b.close) <= b.high and b.low > 0.0):
            raise SimulationError(f"impossible bar for {b.instrument} at {b.start.isoformat()}")
    for i, o in enumerate(orders):
        _check_utc(o.decided_at, f"order {i} decision time")
        if o.quantity == 0.0 or not math.isfinite(o.quantity):
            raise SimulationError(f"order {i} needs a non-zero finite quantity")
        if o.instrument not in markets:
            raise SimulationError(f"order {i}: no market parameters for {o.instrument}")
        if (o.kind is OrderKind.LIMIT) != (o.limit_price is not None):
            raise SimulationError(
                f"order {i}: a limit price goes with a limit order, and only then"
            )
        if o.expires_at is not None:
            _check_utc(o.expires_at, f"order {i} expiry")
    for f in funding:
        _check_utc(f.at, "funding time")


def _limit(order: Order) -> float:
    if order.limit_price is None:
        raise SimulationError("a limit order without a limit price")
    return order.limit_price


def _limit_fills(order: Order, bar: Bar) -> bool:
    """Traded through, not merely touched."""
    limit = _limit(order)
    return bar.low < limit if order.quantity > 0.0 else bar.high > limit


def simulate(
    bars: Sequence[Bar],
    orders: Sequence[Order],
    funding: Sequence[FundingEvent],
    markets: Mapping[str, Market],
    costs: CostModel,
    rules: FillRules,
    bar_length: timedelta,
    initial_cash: float = 0.0,
) -> Result:
    """Run the orders against the bars and settle funding (rules: the module docstring)."""
    _validate(bars, orders, funding, markets)
    if bar_length <= timedelta(0):
        raise SimulationError(f"bar length must be positive, got {bar_length}")
    result = Result(cash=initial_cash)
    pending = sorted(range(len(orders)), key=lambda i: (orders[i].decided_at, i))
    closes: dict[str, float] = {}
    # funding before bars at the same instant (kind 0 before kind 1)
    events: list[tuple[datetime, int, int]] = [(f.at, 0, k) for k, f in enumerate(funding)]
    events += [(b.start, 1, k) for k, b in enumerate(bars)]
    for at, kind, k in sorted(events):
        if kind == 0:
            f = funding[k]
            position = result.positions.get(f.instrument, 0.0)
            if position:
                cash = funding_cash(position * f.mark_price, f.rate)
                result.cash += cash
                result.funding.append(FundingPaid(at, f.instrument, position, f.rate, cash))
            continue
        bar = bars[k]
        still: list[int] = []
        for i in pending:
            o = orders[i]
            if o.instrument != bar.instrument or o.decided_at + rules.latency > bar.start:
                still.append(i)
                continue
            if o.expires_at is not None and bar.start > o.expires_at:
                result.unfilled.append(i)
                continue
            if o.kind is OrderKind.MARKET:
                quantity, price, liquidity = o.quantity, bar.open, Liquidity.TAKER
            elif _limit_fills(o, bar):
                quantity, price = o.quantity * rules.limit_fill_fraction, _limit(o)
                liquidity = Liquidity.MAKER
            else:
                still.append(i)
                continue
            m = markets[o.instrument]
            notional = quantity * price
            base = costs.trade(
                m.exchange,
                notional,
                liquidity,
                half_spread=m.half_spread,
                daily_volume=m.daily_volume,
                daily_vol=m.daily_vol,
            )
            if liquidity is Liquidity.MAKER:  # a resting order pays the maker fee and nothing else
                cost = TradeCost(base.fee, 0.0, 0.0)
            else:
                cost = TradeCost(base.fee, base.spread, base.impact * rules.impact_multiplier)
            result.cash -= notional + cost.total
            result.positions[o.instrument] = result.positions.get(o.instrument, 0.0) + quantity
            result.fills.append(Fill(i, bar.start, o.instrument, quantity, price, liquidity, cost))
        pending = still
        closes[bar.instrument] = bar.close
        value = math.fsum(q * closes.get(name, 0.0) for name, q in result.positions.items())
        point = (bar.start + bar_length, result.cash + value)
        if result.equity and result.equity[-1][0] == point[0]:
            result.equity[-1] = point  # one point per instant, after every bar ending there
        else:
            result.equity.append(point)
    result.unfilled.extend(pending)
    result.unfilled.sort()
    return result


def simulate_all(
    bars: Sequence[Bar],
    orders: Sequence[Order],
    funding: Sequence[FundingEvent],
    markets: Mapping[str, Market],
    bar_length: timedelta,
    *,
    initial_cash: float = 0.0,
    cost_config: Mapping[str, Any] | None = None,
    fill_config: Mapping[str, Any] | None = None,
) -> dict[tuple[Scenario, FillScenario], Result]:
    """Every cost scenario under every fill scenario."""
    cost_models = all_scenarios(cost_config)
    return {
        (cs, fs): simulate(
            bars,
            orders,
            funding,
            markets,
            cost_models[cs],
            fill_rules(fs, fill_config),
            bar_length,
            initial_cash,
        )
        for cs in Scenario
        for fs in FillScenario
    }
