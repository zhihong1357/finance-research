"""A synthetic data source for the Phase 1.6 thin slice.

It imitates a macro API that keeps every published version of a series (as
ALFRED does for FRED): one JSON response per series, each observation carrying
the date it became public. A second series holds monthly returns. The world is
known exactly, so the pipeline's answer can be checked:

* the true indicator ``x[m]`` for month ``m`` is standard normal;
* the first release, ``first_release_days`` after month ``m`` ends, is
  ``x[m]`` plus noise; the revision, ``revision_days`` after it ends, is
  ``x[m]`` itself;
* the return of month ``m + lag`` is ``beta * x[m]`` plus noise, and is
  public when month ``m + lag`` ends.

A bet on month ``m + lag`` is placed when that month starts. At that moment only
the first release is public; a study that uses the revision is looking ahead.

Values are written with a fixed number of decimals, so the bytes do not depend
on the last bit of the platform's maths library, and generation uses
``random.Random(seed)``: the same configuration gives the same bytes.
"""

import json
import random
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from finresearch.core.time import month_start, parse_utc, utc
from finresearch.data.table import PitTable, pit_table
from finresearch.sources.base import Adapter, Response

SOURCE = "synthetic.thin_slice"
INDICATOR = "synthetic.indicator"
RETURNS = "synthetic.returns"
DECIMALS = 6
_URL = "synthetic://thin-slice/{name}"


def first_month(config: Mapping[str, Any]) -> date:
    return date.fromisoformat(f"{config['first_month']}-01")


def _value(v: float) -> str:
    return f"{v:.{DECIMALS}f}"


def fetch(config: Mapping[str, Any], _now: datetime | None = None) -> list[Response]:
    """The raw responses a full backfill of the synthetic API returns (``now`` is not used)."""
    rng = random.Random(int(config["seed"]))  # noqa: S311 - a simulation, not security
    first = first_month(config)
    beta, lag = float(config["beta"]), int(config["lag_months"])
    release_noise, return_noise = float(config["release_noise"]), float(config["return_noise"])
    release_after = timedelta(days=int(config["first_release_days"]))
    revision_after = timedelta(days=int(config["revision_days"]))
    indicator: list[dict[str, str]] = []
    returns: list[dict[str, str]] = []
    for m in range(int(config["months"])):
        x = rng.gauss(0.0, 1.0)
        first_release = x + rng.gauss(0.0, release_noise)
        y = beta * x + rng.gauss(0.0, return_noise)
        label, month_end = month_start(first, m).isoformat(), month_start(first, m + 1)
        indicator.append(
            {
                "date": label,
                "realtime_start": (month_end + release_after).isoformat(),
                "value": _value(first_release),
            }
        )
        indicator.append(
            {
                "date": label,
                "realtime_start": (month_end + revision_after).isoformat(),
                "value": _value(x),
            }
        )
        returns.append(
            {
                "date": month_start(first, m + lag).isoformat(),
                "realtime_start": month_start(first, m + lag + 1).isoformat(),
                "value": _value(y),
            }
        )
    fetched_at = parse_utc(str(config["fetched_at"]))
    return [
        Response(
            name,
            _URL.format(name=name),
            fetched_at,
            json.dumps({"series": name, "observations": obs}, sort_keys=True).encode(),
        )
        for name, obs in ((INDICATOR, indicator), (RETURNS, returns))
    ]


def _midnight(d: str) -> datetime:
    day = date.fromisoformat(d)
    return utc(day.year, day.month, day.day)


def parse(content: bytes, fetched_at: datetime) -> PitTable:
    """One response -> a point-in-time table; ``available_at`` is the publication date."""
    payload = json.loads(content)
    name = str(payload["series"])
    obs: list[dict[str, str]] = payload["observations"]
    return pit_table(
        name,
        ("series",),
        {
            "series": [name] * len(obs),
            "event_time": [_midnight(o["date"]) for o in obs],
            "available_at": [_midnight(o["realtime_start"]) for o in obs],
            "ingested_at": [fetched_at] * len(obs),
            "value": [float(o["value"]) for o in obs],
        },
    )


ADAPTER = Adapter(SOURCE, fetch, parse)
