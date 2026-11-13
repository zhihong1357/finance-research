"""What every data source provides: a way to fetch raw responses and a parser for them."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from finresearch.data.table import PitTable


@dataclass(frozen=True, slots=True)
class Response:
    """One raw response, exactly as received."""

    name: str
    url: str
    fetched_at: datetime
    content: bytes


@dataclass(frozen=True, slots=True)
class Adapter:
    """A data source: ``fetch`` returns raw responses, ``parse`` turns one into a table.

    ``id`` is the source's entry in ``registry/sources/``.
    """

    id: str
    fetch: Callable[[Mapping[str, Any]], list[Response]]
    parse: Callable[[bytes, datetime], PitTable]
