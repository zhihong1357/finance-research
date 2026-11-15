# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false
# pyarrow is only partly typed; its calls stay inside this module, behind typed functions.
"""Small tables kept as full builds (decision 0013): one complete copy per build.

A table of revised series gains versions for every period, old ones included,
so it cannot be cut into partitions that are written once. Each build writes
the whole table instead::

    canonical/<table>/v<format>/builds/<YYYY>/<built>/<part>.parquet
    canonical/<table>/v<format>/builds/<YYYY>/<built>/<part>.json   the build's card

``built`` is the build instant as ``20261115T045000Z``, so names sort in time
order and the current table is simply the last build listed: no pointer file
has to be rewritten, and the remote store stays append-only. A build whose
content equals the latest one is not written (``unchanged``). Every earlier
build stays, so what the dataset held on any day can be read back as it was.

The card records the keys, row count, content fingerprint, the file's
SHA-256 and the raw responses the build was made from (path -> fingerprint).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq

from finresearch.core.fingerprint import digest_file
from finresearch.core.time import ensure_utc
from finresearch.data.store import Limits, Remote, pull, with_retries
from finresearch.data.table import PitTable

ROOT = "canonical"
_STAMP = "%Y%m%dT%H%M%SZ"


class BuildError(RuntimeError):
    """A build that is missing or does not match its card."""


@dataclass(frozen=True)
class BuildCard:
    table: str
    keys: list[str]
    format: int
    built: str  # YYYYMMDDTHHMMSSZ
    part: str
    rows: int
    fingerprint: str  # PitTable.fingerprint
    sha256: str  # of the Parquet file
    inputs: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    notes: list[str] = field(default_factory=lambda: list[str]())

    @property
    def name(self) -> str:
        """How a run's provenance names this build: its place in the store."""
        return f"{self.table}/v{self.format}/builds/{self.built}/{self.part}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, indent=1) + "\n"

    @classmethod
    def from_json(cls, text: str) -> BuildCard:
        return cls(**json.loads(text))


def table_dir(table: str, fmt: int) -> str:
    return f"{ROOT}/{table}/v{fmt}/builds"


def build_dir(table: str, fmt: int, built: datetime) -> str:
    built = ensure_utc(built)
    return f"{table_dir(table, fmt)}/{built:%Y}/{built.strftime(_STAMP)}"


def latest_card_path(remote: Remote, table: str, fmt: int, part: str, limits: Limits) -> str | None:
    """The card of the newest build of ``part`` in the remote store, if there is one."""
    listing = with_retries(lambda: remote.list(table_dir(table, fmt)), limits)
    cards = sorted(p for p in listing if p.endswith(f"/{part}.json"))
    return cards[-1] if cards else None


def read_card(remote: Remote, path: str, limits: Limits) -> BuildCard:
    return BuildCard.from_json(with_retries(lambda: remote.read(path), limits).decode())


def write_build(
    t: PitTable,
    local: Path,
    fmt: int,
    part: str,
    built: datetime,
    inputs: Mapping[str, str],
    notes: Sequence[str] = (),
) -> BuildCard:
    """Write ``t`` as one build under ``local``; returns its card. The bytes depend on the rows."""
    folder = local / build_dir(t.name, fmt, built)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{part}.parquet"
    pq.write_table(t.canonical(), path, compression="zstd", write_statistics=True)
    card = BuildCard(
        table=t.name,
        keys=list(t.keys),
        format=fmt,
        built=ensure_utc(built).strftime(_STAMP),
        part=part,
        rows=t.num_rows,
        fingerprint=str(t.fingerprint),
        sha256=digest_file(path).hex,
        inputs=dict(sorted(inputs.items())),
        notes=list(notes),
    )
    (folder / f"{part}.json").write_text(card.to_json(), encoding="utf-8")
    return card


def load(remote: Remote, local: Path, card_path: str, limits: Limits) -> tuple[BuildCard, PitTable]:
    """A build from the remote store (pulled into ``local`` if missing), checked by its card."""
    folder = card_path.rsplit("/", 1)[0]
    pull(remote, local, folder, limits)
    card = BuildCard.from_json((local / card_path).read_text(encoding="utf-8"))
    data = local / folder / f"{card.part}.parquet"
    if digest_file(data).hex != card.sha256:
        raise BuildError(f"{data}: the file does not match its card")
    t = PitTable(card.table, tuple(card.keys), pq.read_table(data))
    if str(t.fingerprint) != card.fingerprint:
        raise BuildError(f"{card.name}: the content does not match its card")
    return card, t
