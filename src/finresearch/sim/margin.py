"""Maintenance margin and liquidation (roadmap step 4.3b; docs/prior-art.md L1).

Every amount is in the quote currency (USDT). The simulator keeps one cash
account for every instrument (cross margin): equity is cash plus every position
valued at a price.

**Maintenance margin** is tiered by the position's notional, as the exchanges
publish it: ``|notional| x rate_k - deduction_k`` in tier ``k``. The config
gives only each tier's upper bound and rate; the deductions follow from making
the margin continuous across tiers, ``deduction_k = deduction_(k-1) +
cap_(k-1) x (rate_k - rate_(k-1))``. Binance's published maintenance amounts
are exactly these (checked on BTCUSDT and ETHUSDT, ``config/simulator.toml``).

**Trigger.** The account is liquidated when its equity falls below the sum of
its maintenance margins. For one instrument with position ``q`` and the rest of
the account fixed (``rest`` = cash plus the other positions' value, ``mm_rest``
= their maintenance margin), equity minus margin is

    g(p) = rest + q p - mm_rest - mm(|q| p)

which is strictly increasing in the price ``p`` for a long and decreasing for a
short (every rate is below 1), so it crosses zero at most once: the
**trigger price** (``trigger_price``). In tier ``k`` it is
``(mm_rest - deduction_k - rest) / (q (1 - sign(q) rate_k))``.

The simulator's use of these (``sim.engine``) is described there.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from finresearch.core.config import load_toml

DEFAULT = "default"  # the table for an instrument the config does not list


class MarginError(ValueError):
    """The inputs do not describe a margin schedule."""


@dataclass(frozen=True, slots=True)
class Tier:
    cap: float  # upper bound of the notional, inclusive; math.inf for the last tier
    rate: float  # maintenance margin rate
    deduction: float  # makes the margin continuous at the tier's lower bound


def tiers_from(brackets: Sequence[tuple[float, float]]) -> tuple[Tier, ...]:
    """Tiers from ``(cap, rate)`` pairs, caps increasing; the last tier is open-ended."""
    if not brackets:
        raise MarginError("a margin schedule needs at least one tier")
    tiers: list[Tier] = []
    floor, previous_rate, deduction = 0.0, 0.0, 0.0
    for k, (cap, rate) in enumerate(brackets):
        if not 0.0 <= rate < 1.0:
            raise MarginError(f"maintenance rate must be in [0, 1), got {rate}")
        if tiers and rate < previous_rate:
            raise MarginError("maintenance rates must not fall as the notional grows")
        last = k == len(brackets) - 1
        if not last and not (math.isfinite(cap) and cap > floor):
            raise MarginError(f"tier caps must increase, got {cap} after {floor}")
        deduction += floor * (rate - previous_rate)
        tiers.append(Tier(math.inf if last else cap, rate, deduction))
        floor, previous_rate = cap, rate
    return tuple(tiers)


def _tier_index(notional: float, tiers: Sequence[Tier]) -> int:
    return next(k for k, tier in enumerate(tiers) if notional <= tier.cap)


def maintenance_margin(notional: float, tiers: Sequence[Tier]) -> float:
    """Maintenance margin of a position of ``notional`` (either sign)."""
    if not math.isfinite(notional):
        raise MarginError(f"notional must be finite, got {notional}")
    size = abs(notional)
    tier = tiers[_tier_index(size, tiers)]
    return size * tier.rate - tier.deduction


def trigger_price(position: float, rest: float, mm_rest: float, tiers: Sequence[Tier]) -> float:
    """The price at which ``g(p)`` (module docstring) is zero.

    A long is liquidated at prices below it, a short at prices above it. A long
    whose account stays above its margin even at a price of 0 never is: ``0.0``.
    A short already below its margin at a price of 0 is at every price: ``0.0``.
    """
    if position == 0.0 or not math.isfinite(position):
        raise MarginError(f"a trigger price needs a non-zero finite position, got {position}")
    at_zero = rest - mm_rest
    if position > 0.0 and at_zero >= 0.0:
        return 0.0
    if position < 0.0 and at_zero < 0.0:
        return 0.0
    sign = math.copysign(1.0, position)
    floor = 0.0
    for tier in tiers:
        price = (mm_rest - tier.deduction - rest) / (position * (1.0 - sign * tier.rate))
        size = abs(position) * price
        if floor <= size <= tier.cap:
            return price
        floor = tier.cap
    raise MarginError("no trigger price found: the margin schedule is not continuous")


@dataclass(frozen=True, slots=True)
class MarginModel:
    tables: Mapping[str, Mapping[str, tuple[Tier, ...]]]  # exchange -> instrument (or DEFAULT)
    liquidation_fee: Mapping[str, float]  # rate on the liquidated notional, by exchange

    def __post_init__(self) -> None:
        for exchange, rate in self.liquidation_fee.items():
            if not 0.0 <= rate < 1.0:
                raise MarginError(f"liquidation fee of {exchange} must be in [0, 1), got {rate}")
        for exchange, table in self.tables.items():
            if DEFAULT not in table:
                raise MarginError(f"the margin tables of {exchange} need a {DEFAULT!r} table")
            if exchange not in self.liquidation_fee:
                raise MarginError(f"no liquidation fee for {exchange}")

    def tiers(self, exchange: str, instrument: str) -> tuple[Tier, ...]:
        table = self.tables.get(exchange)
        if table is None:
            raise MarginError(
                f"no margin tables for exchange {exchange!r} (have {sorted(self.tables)})"
            )
        return table.get(instrument, table[DEFAULT])

    def fee(self, exchange: str, notional: float) -> float:
        """The liquidation fee rate times ``|notional|``, before any cap (``sim.engine``)."""
        return abs(notional) * self.liquidation_fee[exchange]


def margin_model(config: Mapping[str, Any] | None = None) -> MarginModel:
    """The margin model, from the ``margin`` section of ``config/simulator.toml`` unless given."""
    cfg = (load_toml("simulator") if config is None else config)["margin"]
    tables: dict[str, dict[str, tuple[Tier, ...]]] = {}
    fees: dict[str, float] = {}
    for exchange, spec in cfg.items():
        fees[exchange] = float(spec["liquidation_fee"])
        tables[exchange] = {name: _table(t) for name, t in spec["tiers"].items()}
    return MarginModel(tables, fees)


def _table(spec: Mapping[str, Any]) -> tuple[Tier, ...]:
    """``caps``: the upper bound of every tier but the last; ``rates``: every tier's rate."""
    caps = [float(c) for c in spec["caps"]]
    rates = [float(r) for r in spec["rates"]]
    if len(caps) != len(rates) - 1:
        raise MarginError(f"{len(rates)} rates need {len(rates) - 1} caps, got {len(caps)}")
    return tiers_from(list(zip([*caps, math.inf], rates, strict=True)))
