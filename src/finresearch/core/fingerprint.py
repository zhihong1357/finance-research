"""Content fingerprints and canonical serialisation.

A fingerprint names content by its SHA-256 digest, written ``sha256:<64 hex>``.
Equal content always gives the same fingerprint and any change gives a
different one, wherever and whenever it is computed.

Two rules make that true for structured values and for directories:

* ``canonical_json`` - one byte string per value: keys sorted, no whitespace,
  UTF-8, finite numbers only, aware datetimes in the canonical UTC text form.
  Types whose order is undefined (sets) are refused rather than sorted
  silently.
* ``digest_tree`` - a directory's fingerprint depends only on the relative
  paths and contents of its files: not on creation order, timestamps,
  permissions or where the directory lives. Empty directories are ignored;
  symbolic links are refused.

Changing either rule changes every fingerprint ever recorded, so both are
pinned by format-anchor tests.
"""

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path

from finresearch.core.time import format_utc

ALGORITHM = "sha256"
_HEX = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1 << 20


class FingerprintError(ValueError):
    """A value or file that cannot be fingerprinted unambiguously."""


@dataclass(frozen=True, slots=True, order=True)
class Fingerprint:
    hex: str

    def __post_init__(self) -> None:
        if not _HEX.fullmatch(self.hex):
            raise FingerprintError(f"not a lower-case SHA-256 hex digest: {self.hex!r}")

    def __str__(self) -> str:
        return f"{ALGORITHM}:{self.hex}"

    @classmethod
    def parse(cls, text: str) -> Fingerprint:
        algorithm, sep, hex_digest = text.partition(":")
        if not sep or algorithm != ALGORITHM:
            raise FingerprintError(f"expected {ALGORITHM}:<hex>, got {text!r}")
        return cls(hex_digest)


def digest_bytes(data: bytes) -> Fingerprint:
    return Fingerprint(hashlib.sha256(data).hexdigest())


def digest_file(path: Path, chunk_size: int = _CHUNK) -> Fingerprint:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk_size):
            h.update(block)
    return Fingerprint(h.hexdigest())


def tree_listing(root: Path) -> bytes:
    """The canonical description of a directory that ``digest_tree`` hashes.

    One line per regular file, sorted by POSIX relative path:
    ``<relative path> NUL <hex digest> LF``. NUL cannot occur in a path, so the
    listing is unambiguous.
    """
    if not root.is_dir():
        raise FingerprintError(f"not a directory: {root}")
    lines: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise FingerprintError(f"symbolic links are not fingerprinted: {path}")
        if path.is_file():
            lines.append((path.relative_to(root).as_posix(), digest_file(path).hex))
    lines.sort()
    return b"".join(f"{rel}\0{hex_digest}\n".encode() for rel, hex_digest in lines)


def digest_tree(root: Path) -> Fingerprint:
    return digest_bytes(tree_listing(root))


type Canonical = bool | int | float | str | list[Canonical] | dict[str, Canonical] | None


def _canonical(value: object, where: str) -> Canonical:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FingerprintError(f"{where}: {value} is not a finite number")
        return value
    if isinstance(value, Enum):
        return _canonical(value.value, where)
    if isinstance(value, datetime):
        return format_utc(value)  # rejects naive datetimes
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        out: dict[str, Canonical] = {}
        items: Mapping[object, object] = value  # pyright: ignore[reportUnknownVariableType]
        for k, v in items.items():
            if not isinstance(k, str):
                raise FingerprintError(f"{where}: keys must be strings, got {k!r}")
            out[k] = _canonical(v, f"{where}.{k}")
        return out
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        seq: Sequence[object] = value  # pyright: ignore[reportUnknownVariableType]
        return [_canonical(v, f"{where}[{i}]") for i, v in enumerate(seq)]
    raise FingerprintError(f"{where}: {type(value).__name__} has no canonical form")


def canonical_json(value: object) -> bytes:
    """The single byte string that represents ``value``."""
    return json.dumps(
        _canonical(value, "$"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest_value(value: object) -> Fingerprint:
    """Fingerprint of a structured value, such as a configuration."""
    return digest_bytes(canonical_json(value))
