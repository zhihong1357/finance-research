import json
from datetime import datetime, timedelta

import pytest

from finresearch.core.config import load_toml
from finresearch.core.fingerprint import digest_bytes
from finresearch.core.time import utc
from finresearch.data.pit import Clock, PointInTime
from finresearch.data.table import TableError
from finresearch.sources.synthetic import INDICATOR, RETURNS, fetch, parse

CONFIG = load_toml("studies/thin_slice")


def test_same_configuration_same_bytes() -> None:
    a, b = fetch(CONFIG), fetch(CONFIG)
    assert [digest_bytes(r.content) for r in a] == [digest_bytes(r.content) for r in b]
    other = fetch({**CONFIG, "seed": CONFIG["seed"] + 1})
    assert [r.content for r in other] != [r.content for r in a]


def test_raw_responses_look_like_a_vintage_api() -> None:
    indicator, returns = fetch(CONFIG)
    assert (indicator.name, returns.name) == (INDICATOR, RETURNS)
    obs = json.loads(indicator.content)["observations"]
    assert len(obs) == 2 * CONFIG["months"]
    # January 2000 ends on 1 February: first release 15 days later, revision 45 days later
    assert [o["realtime_start"] for o in obs[:2]] == ["2000-02-16", "2000-03-17"]
    assert {o["date"] for o in obs[:2]} == {"2000-01-01"}
    assert all(len(o["value"].split(".")[1]) == 6 for o in obs)  # fixed decimals
    first = json.loads(returns.content)["observations"][0]
    # the return of month 0 + lag (March 2000) is public when March ends
    assert (first["date"], first["realtime_start"]) == ("2000-03-01", "2000-04-01")


def test_the_revision_comes_later_and_replaces_the_first_release() -> None:
    indicator, _ = fetch(CONFIG)
    table = parse(indicator.content, indicator.fetched_at)
    assert table.num_rows == 2 * CONFIG["months"]
    pit = PointInTime(Clock(limit=utc(2030, 1, 1)), [table])
    first_release, revision = utc(2000, 2, 16), utc(2000, 3, 17)
    tick = timedelta(microseconds=1)

    def january(at: datetime) -> list[float]:
        return [r["value"] for r in pit.as_of(INDICATOR, at).rows() if r["event_time"].month == 1]

    assert january(first_release - tick) == []
    assert len(january(first_release)) == 1
    assert january(revision - tick) == january(first_release)
    assert january(revision) != january(first_release)
    assert len(january(revision)) == 1  # one version at a time: the latest public one


def test_parse_keeps_the_three_times() -> None:
    _, returns = fetch(CONFIG)
    table = parse(returns.content, returns.fetched_at)
    assert table.keys == ("series",)
    with pytest.raises(TableError, match="ingested before it was available"):
        parse(returns.content, utc(2000, 1, 1))
