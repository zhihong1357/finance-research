"""The normalized layer: point-in-time tables built from the raw layer (docs/architecture.md 2.2).

The normalized layer is derived, never edited: it can always be rebuilt from
the raw layer, and a run that starts from a blank directory rebuilds it.
"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from finresearch.data.raw import RawStore
from finresearch.data.table import PitTable, from_parquet, to_parquet

Parser = Callable[[bytes, datetime], PitTable]


class CatalogError(RuntimeError):
    """The raw layer cannot be turned into tables as asked."""


def normalize(store: RawStore, source: str, parse: Parser, directory: Path) -> list[str]:
    """Parse every stored response of ``source`` and write one Parquet file per table.

    Returns the table names. Two responses for the same table (a repeated fetch)
    are refused for now: merging fetches is part of the Phase 2 data platform.
    """
    records = store.records(source)
    if not records:
        raise CatalogError(f"the raw layer has nothing for {source}")
    tables = [parse(store.read(r), r.fetched_at) for r in records]
    repeated = sorted(n for n, k in Counter(t.name for t in tables).items() if k > 1)
    if repeated:
        raise CatalogError(f"{source}: more than one raw response for {repeated}")
    write_tables(directory, tables)
    return sorted(t.name for t in tables)


def write_tables(directory: Path, tables: list[PitTable]) -> None:
    for t in tables:
        to_parquet(t, directory / f"{t.name}.parquet")


def load_tables(directory: Path) -> dict[str, PitTable]:
    tables = [from_parquet(p) for p in sorted(directory.glob("*.parquet"))]
    return {t.name: t for t in tables}
