"""Properties the simulator must keep on any market path and any orders (hypothesis-generated)."""

import math
from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from finresearch.sim.costs import Liquidity, Scenario, cost_model
from finresearch.sim.engine import (
    Bar,
    FillScenario,
    FundingEvent,
    Market,
    Order,
    OrderKind,
    Result,
    fill_rules,
    simulate,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
MARKETS = {
    "A": Market("binance", 0.0001, 50_000_000.0, 0.04),
    "B": Market("bybit", 0.0002, 5_000_000.0, 0.06),
}


@st.composite
def bars(draw: st.DrawFn) -> list[Bar]:
    out: list[Bar] = []
    for name in MARKETS:
        n = draw(st.integers(min_value=1, max_value=12))
        price = draw(st.floats(min_value=1.0, max_value=1000.0))
        for m in range(n):
            o = price
            c = o * (1.0 + draw(st.floats(min_value=-0.05, max_value=0.05)))
            h = max(o, c) * (1.0 + draw(st.floats(min_value=0.0, max_value=0.02)))
            lo = min(o, c) * (1.0 - draw(st.floats(min_value=0.0, max_value=0.02)))
            out.append(Bar(name, T0 + m * MINUTE, o, h, lo, c))
            price = c
    return sorted(out, key=lambda b: (b.start, b.instrument))


@st.composite
def orders(draw: st.DrawFn) -> list[Order]:
    out: list[Order] = []
    for _ in range(draw(st.integers(min_value=0, max_value=8))):
        decided = T0 + timedelta(seconds=draw(st.integers(min_value=0, max_value=14 * 60)))
        quantity = draw(st.floats(min_value=0.01, max_value=50.0)) * draw(st.sampled_from([1, -1]))
        name = draw(st.sampled_from(sorted(MARKETS)))
        if draw(st.booleans()):
            out.append(Order(decided, name, quantity))
        else:
            limit = draw(st.floats(min_value=0.5, max_value=1500.0))
            expires = decided + timedelta(minutes=draw(st.integers(min_value=0, max_value=15)))
            out.append(Order(decided, name, quantity, OrderKind.LIMIT, limit, expires))
    return out


@st.composite
def worlds(draw: st.DrawFn) -> tuple[list[Bar], list[Order], list[FundingEvent]]:
    """Bars, orders, and funding up to the end of the last bar (on the minute or not)."""
    path = draw(bars())
    end = max(b.start for b in path) + MINUTE
    seconds = int((end - T0).total_seconds())
    funding = [
        FundingEvent(
            draw(st.sampled_from(sorted(MARKETS))),
            T0 + timedelta(seconds=draw(st.sampled_from([0, 30])) + 60 * m),
            draw(st.floats(min_value=-0.003, max_value=0.003)),
            draw(st.floats(min_value=1.0, max_value=1000.0)),
        )
        for m in draw(st.lists(st.integers(min_value=0, max_value=seconds // 60), max_size=4))
    ]
    return path, draw(orders()), [f for f in funding if f.at <= end]


def run(world: tuple[list[Bar], list[Order], list[FundingEvent]], fill: FillScenario) -> Result:
    path, os, fs = world
    return simulate(path, os, fs, MARKETS, cost_model(Scenario.NORMAL), fill_rules(fill), MINUTE)


FILLS = st.sampled_from(list(FillScenario))
World = tuple[list[Bar], list[Order], list[FundingEvent]]


@settings(max_examples=200)
@given(worlds(), FILLS)
def test_no_fill_breaks_the_rules(world: World, fill: FillScenario) -> None:
    path, os, _ = world
    rules = fill_rules(fill)
    by_key = {(b.instrument, b.start): b for b in path}
    for f in run(world, fill).fills:
        order, bar = os[f.order], by_key[(f.instrument, f.at)]
        assert f.at >= order.decided_at + rules.latency  # never before the latency has passed
        if order.expires_at is not None:  # a limit order needs the whole bar before its expiry
            last = f.at if order.kind is OrderKind.MARKET else f.at + MINUTE
            assert last <= order.expires_at
        if order.kind is OrderKind.MARKET:
            assert (f.price, f.liquidity, f.quantity) == (bar.open, Liquidity.TAKER, order.quantity)
        else:
            assert order.limit_price is not None
            assert f.price == order.limit_price
            assert f.liquidity is Liquidity.MAKER
            assert (
                (bar.low < f.price) if order.quantity > 0 else (bar.high > f.price)
            )  # traded through
            assert math.isclose(f.quantity, order.quantity * rules.limit_fill_fraction)
            assert f.cost.spread == 0.0
            assert f.cost.impact == 0.0
        assert min(f.cost.fee, f.cost.spread, f.cost.impact) >= 0.0


@settings(max_examples=200)
@given(worlds(), FILLS)
def test_every_order_is_filled_once_or_left(world: World, fill: FillScenario) -> None:
    r = run(world, fill)
    filled = [f.order for f in r.fills]
    assert len(filled) == len(set(filled))
    assert sorted(filled + r.unfilled) == list(range(len(world[1])))


@settings(max_examples=200)
@given(worlds(), FILLS)
def test_the_books_balance(world: World, fill: FillScenario) -> None:
    path, _, _ = world
    r = run(world, fill)
    spent = math.fsum(f.quantity * f.price + f.cost.total for f in r.fills)
    received = math.fsum(p.cash for p in r.funding)
    assert math.isclose(r.cash, received - spent, rel_tol=1e-9, abs_tol=1e-9)
    for name in MARKETS:
        held = math.fsum(f.quantity for f in r.fills if f.instrument == name)
        assert math.isclose(r.positions.get(name, 0.0), held, rel_tol=1e-9, abs_tol=1e-12)
    # the last point is the final cash and positions at the last closes, funding included
    last_close = {b.instrument: b.close for b in path}
    value = math.fsum(q * last_close[name] for name, q in r.positions.items())
    assert math.isclose(r.equity[-1][1], r.cash + value, rel_tol=1e-9, abs_tol=1e-9)
    times = [t for t, _ in r.equity]
    assert times == sorted(set(times))  # one point per instant, in order
    assert times[-1] == max(b.start for b in path) + MINUTE


@settings(max_examples=200)
@given(worlds(), FILLS)
def test_each_point_sees_only_what_happened_by_then(world: World, fill: FillScenario) -> None:
    path, _, _ = world
    r = run(world, fill)
    for at, equity in r.equity:
        cash = math.fsum(p.cash for p in r.funding if p.at <= at) - math.fsum(
            f.quantity * f.price + f.cost.total for f in r.fills if f.at < at
        )
        closes = {b.instrument: b.close for b in path if b.start + MINUTE <= at}
        value = math.fsum(
            f.quantity * closes[f.instrument] for f in r.fills if f.at < at
        )  # every fill before ``at`` is on a bar that has ended by then
        assert math.isclose(equity, cash + value, rel_tol=1e-9, abs_tol=1e-9)


@settings(max_examples=100)
@given(worlds())
def test_funding_is_paid_on_the_position_held_before_the_fills_at_that_instant(
    world: World,
) -> None:
    _, _, fs = world
    r = run(world, FillScenario.CONSERVATIVE)
    for paid in r.funding:
        before = math.fsum(
            f.quantity for f in r.fills if f.instrument == paid.instrument and f.at < paid.at
        )
        assert math.isclose(paid.position, before, rel_tol=1e-9, abs_tol=1e-12)
        event = next(
            e
            for e in fs
            if e.instrument == paid.instrument and e.at == paid.at and e.rate == paid.rate
        )
        assert math.isclose(
            paid.cash, -paid.position * event.mark_price * event.rate, rel_tol=1e-9, abs_tol=1e-12
        )


@settings(max_examples=100)
@given(worlds())
def test_without_costs_doubling_every_order_doubles_the_result(world: World) -> None:
    path, os, fs = world
    free = cost_model(Scenario.ZERO)
    rules = fill_rules(FillScenario.CONSERVATIVE)
    once = simulate(path, os, fs, MARKETS, free, rules, MINUTE)
    doubled = [
        Order(o.decided_at, o.instrument, 2 * o.quantity, o.kind, o.limit_price, o.expires_at)
        for o in os
    ]
    twice = simulate(path, doubled, fs, MARKETS, free, rules, MINUTE)
    assert math.isclose(twice.equity[-1][1], 2 * once.equity[-1][1], rel_tol=1e-9, abs_tol=1e-9)
    assert once.costs == 0.0


@settings(max_examples=100)
@given(worlds(), st.integers(min_value=2, max_value=5), FILLS)
def test_splitting_orders_saves_no_cost(world: World, pieces: int, fill: FillScenario) -> None:
    # every order cut into equal pieces, decided at the same time: impact is charged on the
    # metaorder, so the costs and the result are what the whole orders give
    path, os, fs = world
    split = [
        Order(o.decided_at, o.instrument, o.quantity / pieces, o.kind, o.limit_price, o.expires_at)
        for o in os
        for _ in range(pieces)
    ]
    whole, cut = run(world, fill), run((path, split, fs), fill)
    assert math.isclose(cut.costs, whole.costs, rel_tol=1e-9, abs_tol=1e-9)
    assert math.isclose(cut.equity[-1][1], whole.equity[-1][1], rel_tol=1e-9, abs_tol=1e-9)


@settings(max_examples=100)
@given(worlds(), FILLS)
def test_the_same_inputs_give_the_same_result(world: World, fill: FillScenario) -> None:
    a, b = run(world, fill), run(world, fill)
    assert (a.fills, a.funding, a.unfilled, a.positions, a.cash, a.equity) == (
        b.fills,
        b.funding,
        b.unfilled,
        b.positions,
        b.cash,
        b.equity,
    )
