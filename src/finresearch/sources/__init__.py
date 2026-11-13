"""Data sources. Each turns raw responses into point-in-time tables."""

from finresearch.sources import synthetic
from finresearch.sources.base import Adapter

ADAPTERS: dict[str, Adapter] = {a.id: a for a in (synthetic.ADAPTER,)}
