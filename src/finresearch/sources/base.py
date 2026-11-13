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


class SourceError(RuntimeError):
    """A source could not be fetched: the network, the server, or what it sent back."""


@dataclass(frozen=True, slots=True)
class Adapter:
    """A data source: ``fetch`` returns raw responses, ``parse`` turns one into a table.

    ``fetch`` gets the source's configuration and the present instant; it raises
    ``SourceError`` when the source cannot be fetched. ``id`` is the source's
    entry in ``registry/sources/``.
    """

    id: str
    fetch: Callable[[Mapping[str, Any], datetime], list[Response]]
    parse: Callable[[bytes, datetime], PitTable]
