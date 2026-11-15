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

    Some responses do not say what was asked for (a page of candles does not
    name its currency). Such a source sets ``parse_url`` instead, which also
    gets the URL the response was fetched from, as kept in the raw layer.
    ``parse_record`` calls whichever the source has.
    """

    id: str
    fetch: Callable[[Mapping[str, Any], datetime], list[Response]]
    parse: Callable[[bytes, datetime], PitTable] | None = None
    parse_url: Callable[[bytes, datetime, str], PitTable] | None = None

    def parse_record(self, content: bytes, fetched_at: datetime, url: str) -> PitTable:
        if self.parse_url is not None:
            return self.parse_url(content, fetched_at, url)
        if self.parse is None:
            raise SourceError(f"{self.id}: the adapter has no parser")
        return self.parse(content, fetched_at)
