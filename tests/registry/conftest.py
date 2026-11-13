"""Builders for small, valid registries in temporary directories."""

import tomllib
from datetime import datetime
from pathlib import Path

import pytest

from finresearch.core.fingerprint import digest_bytes, digest_value
from finresearch.core.provenance import CodeVersion, RunManifest
from finresearch.core.time import utc
from finresearch.registry.models import plan_fingerprint

CARD = """\
id = "H-0001"
title = "Negative dealer gamma raises next-day realised volatility in BTC"
program = "P4"
return_source = "structural_flow"
mechanism = "Dealers short gamma hedge in the direction of the move, amplifying it."
counterparty = "Short-horizon contrarian traders"
why_persistent = "Hedging is a risk-management requirement, not a choice"
why_us = "Daily frequency, small size"
falsification = "No significant difference in next-day realised volatility on the confirmation set"
prior = 0.25
data = ["deribit.book_summary"]
power = "d = 0.5, t >= 3, 80% power: about 240 days"

[scope]
universe = "U1"
instruments = "BTC perpetual"
horizon = "1d"
output = "volatility"

[plan]
metric = "difference in next-day realised volatility"
test = "block bootstrap, block 5 days, two-sided"
controls = ["DVOL level"]
robustness = ["threshold +-20%"]

[plan.exploration]
start = 2026-10-26
end = 2027-06-30

[plan.confirmation]
start = 2027-07-01
end = 2028-02-29

[plan.vault]
start = 2028-03-01
end = 2028-06-30

[[history]]
at = 2026-11-13T04:00:00Z
status = "draft"
"""

SOURCE = """\
id = "deribit.book_summary"
name = "Deribit book summary"
url = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
access = "snapshot"
redownloadable = false
event_time = "time of the request"
available_at = "time of the request"
"""

REGISTERED_AT = "2026-11-20T00:00:00Z"


def registered(card: str = CARD, at: str = REGISTERED_AT) -> str:
    fp = plan_fingerprint(tomllib.loads(card)["plan"])
    return card + f'\n[[history]]\nat = {at}\nstatus = "registered"\nplan_fingerprint = "{fp}"\n'


END_OF_EXPLORATION = utc(2027, 6, 30)


def manifest(clock_limit: datetime | None = END_OF_EXPLORATION, dirty: bool = False) -> RunManifest:
    return RunManifest(
        code=CodeVersion("a" * 40, dirty=dirty),
        environment=digest_bytes(b"environment"),
        config=digest_value({"lookback": 20}),
        data={"deribit.book_summary": digest_bytes(b"data")},
        seed=0,
        clock_limit=clock_limit,
    )


def write_registry(root: Path, card: str | None = CARD, source: str | None = SOURCE) -> Path:
    for sub in ("hypotheses", "relations", "sources"):
        (root / "registry" / sub).mkdir(parents=True, exist_ok=True)
    if card is not None:
        (root / "registry/hypotheses/H-0001.toml").write_text(card, encoding="utf-8")
    if source is not None:
        (root / "registry/sources/deribit.book_summary.toml").write_text(source, encoding="utf-8")
    return root


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return write_registry(tmp_path)
