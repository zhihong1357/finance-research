"""Instrument identifiers.

Two levels of identity:

* ``SymbolKey`` - what a venue calls something today: ``binance:perp:BTCUSDT``.
  Venues reuse symbols: after a delisting the same code can later name a
  different contract. A symbol key is therefore *not* an identity over time.
* ``InstrumentId`` - a symbol key plus the date it was listed:
  ``binance:perp:ABCUSDT@2021-03-01``. Unique for all time, like the permanent
  numbers that research databases assign to stocks so that a reused ticker
  never merges two companies' histories.

``ListingTable`` resolves a symbol key at an instant to the instrument that
held it then, and finds the same underlying across venues. The listings
themselves (dates, base and quote assets, multipliers) are reference data,
loaded from each venue's own metadata in the data platform.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from itertools import pairwise

from finresearch.core.time import ensure_utc

_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]*")
_ASSET = re.compile(r"[A-Z0-9]+")


class Venue(StrEnum):
    BINANCE = "binance"
    BYBIT = "bybit"
    OKX = "okx"
    HYPERLIQUID = "hyperliquid"
    DERIBIT = "deribit"
    CBOE = "cboe"
    US = "us"  # US consolidated equity and ETF quotes


class Kind(StrEnum):
    SPOT = "spot"
    PERP = "perp"
    FUTURE = "future"
    OPTION = "option"
    EQUITY = "equity"
    ETF = "etf"
    INDEX = "index"


class IdentifierError(ValueError):
    """A malformed identifier, or a listing table that contradicts itself."""


@dataclass(frozen=True, slots=True, order=True)
class SymbolKey:
    venue: Venue
    kind: Kind
    symbol: str

    def __post_init__(self) -> None:
        if not _SYMBOL.fullmatch(self.symbol):
            raise IdentifierError(f"bad symbol {self.symbol!r}: upper-case letters, digits, . _ -")

    def __str__(self) -> str:
        return f"{self.venue}:{self.kind}:{self.symbol}"

    @classmethod
    def parse(cls, text: str) -> SymbolKey:
        parts = text.split(":")
        if len(parts) != len(("venue", "kind", "symbol")):
            raise IdentifierError(f"expected venue:kind:symbol, got {text!r}")
        venue, kind, symbol = parts
        try:
            return cls(Venue(venue), Kind(kind), symbol)
        except ValueError as exc:
            raise IdentifierError(f"{text!r}: {exc}") from exc


@dataclass(frozen=True, slots=True, order=True)
class InstrumentId:
    key: SymbolKey
    listed: date

    def __str__(self) -> str:
        return f"{self.key}@{self.listed.isoformat()}"

    @classmethod
    def parse(cls, text: str) -> InstrumentId:
        key, sep, listed = text.partition("@")
        if not sep:
            raise IdentifierError(f"expected venue:kind:symbol@YYYY-MM-DD, got {text!r}")
        try:
            return cls(SymbolKey.parse(key), date.fromisoformat(listed))
        except ValueError as exc:
            raise IdentifierError(f"{text!r}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Listing:
    """One instrument's life on its venue: ``[start, end)``; ``end`` is None while it still trades.

    ``base`` is the underlying asset per unit of ``multiplier`` (a contract on
    1000 units of PEPE has base ``PEPE`` and multiplier 1000), so the same
    exposure on different venues compares equal.
    """

    instrument: InstrumentId
    start: datetime
    end: datetime | None
    base: str
    quote: str
    multiplier: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start))
        if self.end is not None:
            object.__setattr__(self, "end", ensure_utc(self.end))
            if not self.start < self.end:
                raise IdentifierError(f"{self.instrument}: listing must start before it ends")
        if self.start.date() != self.instrument.listed:
            raise IdentifierError(f"{self.instrument}: listing starts {self.start.date()}")
        for asset in (self.base, self.quote):
            if not _ASSET.fullmatch(asset):
                raise IdentifierError(f"{self.instrument}: bad asset code {asset!r}")
        if self.multiplier < 1:
            raise IdentifierError(f"{self.instrument}: multiplier must be a positive integer")

    def active(self, t: datetime) -> bool:
        t = ensure_utc(t)
        return self.start <= t and (self.end is None or t < self.end)


class ListingTable:
    """Point-in-time resolution of symbols to instruments."""

    def __init__(self, listings: Iterable[Listing]) -> None:
        self._by_key: dict[SymbolKey, list[Listing]] = {}
        seen: set[InstrumentId] = set()
        for listing in listings:
            if listing.instrument in seen:
                raise IdentifierError(f"{listing.instrument} listed twice")
            seen.add(listing.instrument)
            self._by_key.setdefault(listing.instrument.key, []).append(listing)
        for key, group in self._by_key.items():
            group.sort(key=lambda x: x.start)
            for earlier, later in pairwise(group):
                if earlier.end is None or earlier.end > later.start:
                    raise IdentifierError(
                        f"{key}: {earlier.instrument} overlaps {later.instrument}"
                    )

    def resolve(self, key: SymbolKey, t: datetime) -> InstrumentId:
        """The instrument that ``key`` named at instant ``t``."""
        for listing in self._by_key.get(key, []):
            if listing.active(t):
                return listing.instrument
        raise IdentifierError(f"{key} named no instrument at {ensure_utc(t).isoformat()}")

    def same_underlying(self, base: str, quote: str, kind: Kind, t: datetime) -> list[InstrumentId]:
        """Every instrument of ``kind`` on ``base``/``quote`` trading at ``t``, across venues."""
        return sorted(
            listing.instrument
            for group in self._by_key.values()
            for listing in group
            if listing.instrument.key.kind is kind
            and (listing.base, listing.quote) == (base, quote)
            and listing.active(t)
        )
