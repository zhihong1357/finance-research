"""The event-driven simulator core (docs/architecture.md section 5.4; roadmap step 4.3).

The same loop will run the paper portfolio (Phase 7): it takes a time-ordered
stream of bars, funding settlements and orders, and books every fill and every
cash flow. Orders come from outside (the portfolio builder, later); this core
only executes and accounts.

Fill rules (``FillRules``, from ``config/simulator.toml``):

- An order decided at ``t`` becomes active at ``t + latency`` and can only fill
  on a bar that starts at or after that instant (decision 0012).
- **Market order**: fills on the first such bar at its open, so that bar must
  start by the order's expiry (below). Fee (taker), half the spread
  and impact are booked as costs (``sim.costs``); impact is further multiplied
  by the scenario's ``impact_multiplier``.
- **Impact is charged per metaorder** (``sim.costs``): a market fill continues
  the instrument's current metaorder if it goes the same way and comes within
  the cost model's ``metaorder_window`` of that metaorder's first fill, and
  then pays only the impact it adds. Otherwise it starts a new one. Splitting
  an order, in one bar or over several, saves no impact.
- **Limit order that can fill the moment it is active** (prior-art L2): if the
  first bar it can trade on already *opens* through the limit (a buy opening
  below it, a sell above), it would not rest in the book but take liquidity at
  once. It is filled like a market order: all of it, at that open, as a taker
  (taker fee, half the spread, impact). Only that first bar counts; a gap
  through the limit on a later bar is an ordinary resting fill (next rule).
  The open plus costs may come out beyond the limit, where a real order would
  fill only in part; that edge is ignored (the open alone respects the limit).
- **Limit order**: fills only if the price trades *through* the limit, not just
  touches it: a buy when the bar's low is below the limit, a sell when its high
  is above. It fills at the limit even if the bar opened better, pays the maker
  fee and no spread or impact, and fills ``limit_fill_fraction`` of its size;
  the rest is cancelled. Only a bar that *ends* by the order's expiry can fill
  it: a bar cannot say whether its high or low came before or after an instant
  inside it, so a bar that straddles the expiry does not count.
- **Expiry**: ``expires_at`` if the order has one. If not, and the fill rules
  give a ``max_wait_bars`` (prior-art L4), the order expires ``max_wait_bars``
  bar lengths after it becomes active (``decided_at + latency``), so it cannot
  fill long after the decision on a price that no longer bears on it (Bybit has
  no bar for a minute without trades). Expired orders are left unfilled.
- **Funding** at instant ``T`` is settled on the position held before any fill
  at ``T``: settlements are processed before bars that start at ``T``. A limit
  fill is booked at its bar's start but may have come at any moment of the bar,
  so a settlement strictly inside that bar counts the fill only if that costs
  more (or earns less) than leaving it out: the worse of the two. A market fill
  happens at the open, before anything inside the bar. The cash
  moved is ``sim.costs.funding_cash(position x mark price, rate)``.

Accounting: cash starts at ``initial_cash``; a fill of ``q`` at ``p`` moves cash
by ``-q p`` and the costs. Equity is marked at each bar end ``E``: cash plus
every position valued at its instrument's latest close, after every funding
settlement at or before ``E`` and before any fill at ``E``. So the last point is
always the final cash plus the final positions at the last closes.

Inputs are checked before anything runs: UTC times; bars of one instrument in
order; every bar on one grid of ``bar_length`` (a bar off the grid would let a
mark at ``E`` see a close after ``E``); finite, positive, consistent prices;
finite funding rates and positive mark prices; no funding after the last bar
ends (no price would value it); positive finite limit prices; no expiry before
the decision.

``simulate_all`` runs every combination of the three cost scenarios and the two
fill scenarios (roadmap Phase 4 acceptance: one call gives all of them). The
stress fills are harsher assumptions, not a bound on the result: a losing limit
order loses less when only half of it fills, and a longer delay can land on a
better price. So stress can come out ahead of conservative; read both, and
never read stress as the worst case.
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
_FUNDING, _BAR = 0, 1  # at the same instant, funding is settled before bars are traded


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
    max_wait_bars: int | None = None  # how long an order without an expiry may wait, once active

    def __post_init__(self) -> None:
        if self.max_wait_bars is not None and self.max_wait_bars < 1:
            raise SimulationError(f"max wait must be at least one bar, got {self.max_wait_bars}")
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
    wait = cfg.get("max_wait_bars")
    return FillRules(
        timedelta(seconds=float(cfg["latency_seconds"])),
        float(cfg["limit_fill_fraction"]),
        float(cfg["impact_multiplier"]),
        None if wait is None else int(wait),
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


def _valid_bar(b: Bar) -> bool:
    prices = (b.open, b.high, b.low, b.close)
    return (
        all(math.isfinite(x) for x in prices)
        and 0.0 < b.low <= min(b.open, b.close)
        and max(b.open, b.close) <= b.high
    )


def _validate(
    bars: Sequence[Bar],
    orders: Sequence[Order],
    funding: Sequence[FundingEvent],
    markets: Mapping[str, Market],
    bar_length: timedelta,
    initial_cash: float,
) -> None:
    if bar_length <= timedelta(0):
        raise SimulationError(f"bar length must be positive, got {bar_length}")
    if not math.isfinite(initial_cash):
        raise SimulationError(f"initial cash must be finite, got {initial_cash}")
    last: dict[str, datetime] = {}
    for b in bars:
        _check_utc(b.start, "bar start")
        if b.instrument in last and b.start <= last[b.instrument]:
            raise SimulationError(f"bars of {b.instrument} must be in time order without repeats")
        last[b.instrument] = b.start
        if (b.start - bars[0].start) % bar_length != timedelta(0):
            raise SimulationError(
                f"bar of {b.instrument} at {b.start.isoformat()} is off the {bar_length} grid"
            )
        if not _valid_bar(b):
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
        if o.limit_price is not None and not (math.isfinite(o.limit_price) and o.limit_price > 0.0):
            raise SimulationError(f"order {i}: limit price must be positive, got {o.limit_price}")
        if o.expires_at is not None:
            _check_utc(o.expires_at, f"order {i} expiry")
            if o.expires_at < o.decided_at:
                raise SimulationError(f"order {i} expires before it is decided")
    end = max(b.start for b in bars) + bar_length if bars else None
    for f in funding:
        _check_utc(f.at, "funding time")
        if not math.isfinite(f.rate):
            raise SimulationError(f"funding rate must be finite, got {f.rate}")
        if not (math.isfinite(f.mark_price) and f.mark_price > 0.0):
            raise SimulationError(f"funding mark price must be positive, got {f.mark_price}")
        if end is not None and f.at > end:
            raise SimulationError(
                f"funding at {f.at.isoformat()} comes after the last bar ends at {end.isoformat()}"
            )


def _limit(order: Order) -> float:
    if order.limit_price is None:
        raise SimulationError("a limit order without a limit price")
    return order.limit_price


def _limit_fills(order: Order, bar: Bar) -> bool:
    """Traded through, not merely touched."""
    limit = _limit(order)
    return bar.low < limit if order.quantity > 0.0 else bar.high > limit


def _opens_through(order: Order, bar: Bar) -> bool:
    """The bar opens through the limit: the order can take liquidity at once."""
    limit = _limit(order)
    return bar.open < limit if order.quantity > 0.0 else bar.open > limit


def _expiry(order: Order, rules: FillRules, bar_length: timedelta) -> datetime | None:
    """The order's own expiry, else ``max_wait_bars`` bar lengths after it becomes active."""
    if order.expires_at is not None or rules.max_wait_bars is None:
        return order.expires_at
    return order.decided_at + rules.latency + rules.max_wait_bars * bar_length


def _too_late(expiry: datetime | None, last_instant: datetime) -> bool:
    return expiry is not None and last_instant > expiry


@dataclass(slots=True)
class _Metaorder:
    sign: float
    started: datetime
    traded: float  # unsigned notional so far


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
    _validate(bars, orders, funding, markets, bar_length, initial_cash)
    result = Result(cash=initial_cash)
    pending = sorted(range(len(orders)), key=lambda i: (orders[i].decided_at, i))
    closes: dict[str, float] = {}
    metaorders: dict[str, _Metaorder] = {}
    mark_due: datetime | None = None  # a bar end whose equity point is not yet taken
    in_bar: dict[str, list[tuple[datetime, float]]] = {}  # limit fills: (their bar's end, quantity)
    seen: set[int] = set()  # orders that have had their first bar

    def mark(at: datetime) -> None:
        value = math.fsum(q * closes[name] for name, q in result.positions.items())
        result.equity.append((at, result.cash + value))

    events: list[tuple[datetime, int, int]] = [(f.at, _FUNDING, k) for k, f in enumerate(funding)]
    events += [(b.start, _BAR, k) for k, b in enumerate(bars)]
    for at, kind, k in sorted(events):
        # the point at a bar end E: after funding at E, before fills at E
        if mark_due is not None and (at > mark_due or (at == mark_due and kind == _BAR)):
            mark(mark_due)
            mark_due = None
        if kind == _FUNDING:
            f = funding[k]
            # limit fills whose bar is still running may have come before or after this instant:
            # each counts only if that is the worse for the cash (funding is linear in position)
            unsure = [q for end, q in in_bar.get(f.instrument, []) if at < end]
            position = result.positions.get(f.instrument, 0.0) - math.fsum(unsure)
            for q in unsure:
                if funding_cash(q * f.mark_price, f.rate) < 0.0:
                    position += q
            if position:
                cash = funding_cash(position * f.mark_price, f.rate)
                result.cash += cash
                result.funding.append(FundingPaid(at, f.instrument, position, f.rate, cash))
            continue
        bar = bars[k]
        in_bar[bar.instrument] = []  # the instrument's earlier bars have ended (one grid)
        still: list[int] = []
        for i in pending:
            o = orders[i]
            if o.instrument != bar.instrument or o.decided_at + rules.latency > bar.start:
                still.append(i)
                continue
            first = i not in seen
            seen.add(i)
            expiry = _expiry(o, rules, bar_length)
            # a market order, or a limit order already through at its first open, takes at the open
            takes = o.kind is OrderKind.MARKET or (first and _opens_through(o, bar))
            if _too_late(expiry, bar.start if takes else bar.start + bar_length):
                result.unfilled.append(i)
                continue
            if takes:
                quantity, price, liquidity = o.quantity, bar.open, Liquidity.TAKER
            elif _limit_fills(o, bar):
                quantity, price = o.quantity * rules.limit_fill_fraction, _limit(o)
                liquidity = Liquidity.MAKER
            else:
                still.append(i)
                continue
            m = markets[o.instrument]
            notional = quantity * price
            prior = 0.0
            if liquidity is Liquidity.TAKER:
                sign = math.copysign(1.0, quantity)
                current = metaorders.get(o.instrument)
                if (
                    current is not None
                    and current.sign == sign
                    and bar.start - current.started < costs.metaorder_window
                ):
                    prior = current.traded
                else:
                    current = metaorders[o.instrument] = _Metaorder(sign, bar.start, 0.0)
                current.traded += abs(notional)
            base = costs.trade(
                m.exchange,
                notional,
                liquidity,
                half_spread=m.half_spread,
                daily_volume=m.daily_volume,
                daily_vol=m.daily_vol,
                prior=prior,
            )
            if liquidity is Liquidity.MAKER:  # a resting order pays the maker fee and nothing else
                cost = TradeCost(base.fee, 0.0, 0.0)
            else:
                cost = TradeCost(base.fee, base.spread, base.impact * rules.impact_multiplier)
            result.cash -= notional + cost.total
            result.positions[o.instrument] = result.positions.get(o.instrument, 0.0) + quantity
            result.fills.append(Fill(i, bar.start, o.instrument, quantity, price, liquidity, cost))
            if liquidity is Liquidity.MAKER:
                in_bar[o.instrument].append((bar.start + bar_length, quantity))
        pending = still
        closes[bar.instrument] = bar.close
        end = bar.start + bar_length
        mark_due = end if mark_due is None else max(mark_due, end)
    if mark_due is not None:
        mark(mark_due)
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
