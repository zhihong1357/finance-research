import json
from datetime import datetime
from pathlib import Path

import pytest

from finresearch.core.time import parse_utc, utc
from finresearch.data.catalog import CatalogError, load_tables, normalize
from finresearch.data.raw import RawStore
from finresearch.data.table import PitTable, pit_table

AT = utc(2026, 11, 13)


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    payload = json.loads(content)
    rows = payload["rows"]
    return pit_table(
        payload["name"],
        ("series",),
        {
            "series": ["S"] * len(rows),
            "event_time": [parse_utc(r[0]) for r in rows],
            "available_at": [parse_utc(r[1]) for r in rows],
            "ingested_at": [fetched_at] * len(rows),
            "value": [float(r[2]) for r in rows],
        },
    )


def _response(name: str) -> bytes:
    rows = [["2026-10-01T00:00:00Z", "2026-10-15T00:00:00Z", 1.5]]
    return json.dumps({"name": name, "rows": rows}).encode()


def test_raw_to_tables_and_back(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    store.save("s.x", "a", "u", AT, _response("table.a"))
    store.save("s.x", "b", "u", AT, _response("table.b"))
    names = normalize(store, "s.x", parse, tmp_path / "tables")
    assert names == ["table.a", "table.b"]
    loaded = load_tables(tmp_path / "tables")
    assert sorted(loaded) == names
    direct = parse(_response("table.a"), AT)
    assert loaded["table.a"].fingerprint == direct.fingerprint  # Parquet round trip is lossless


def test_repeated_fetches_and_empty_sources_are_refused(tmp_path: Path) -> None:
    store = RawStore(tmp_path)
    with pytest.raises(CatalogError, match=r"nothing for s\.x"):
        normalize(store, "s.x", parse, tmp_path / "tables")
    store.save("s.x", "a", "u", AT, _response("table.a"))
    store.save("s.x", "a", "u", utc(2026, 11, 14), _response("table.a"))
    with pytest.raises(CatalogError, match="more than one raw response"):
        normalize(store, "s.x", parse, tmp_path / "tables")
