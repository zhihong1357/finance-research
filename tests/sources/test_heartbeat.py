import json

import pytest

from finresearch.core.time import utc
from finresearch.sources import ADAPTERS
from finresearch.sources.base import SourceError
from finresearch.sources.heartbeat import CANARY, DRILL, parse

NOW = utc(2026, 11, 14, 8, 17, 3)


def test_a_heartbeat_is_one_small_record_of_now() -> None:
    (r,) = ADAPTERS[CANARY].fetch({}, NOW)
    assert (r.name, r.url, r.fetched_at) == ("heartbeat", "heartbeat://heartbeat.canary", NOW)
    assert json.loads(r.content) == {"at": "2026-11-14T08:17:03.000000Z", "source": CANARY}
    table = parse(r.content, NOW)
    assert (table.name, table.num_rows) == (CANARY, 1)


def test_the_drill_fails_when_told_to() -> None:
    with pytest.raises(SourceError, match="simulated outage"):
        ADAPTERS[DRILL].fetch({"simulate_failure": True}, NOW)
    assert len(ADAPTERS[DRILL].fetch({"simulate_failure": False}, NOW)) == 1
