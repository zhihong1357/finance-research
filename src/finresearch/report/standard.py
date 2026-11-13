"""The standard report: twelve sections, a header, and a fingerprinted body.

A report file has two parts, split by ``BODY_MARKER``:

* the **header**: trial id, time recorded, and the two fingerprints. It exists
  only once the trial is in the ledger;
* the **body**: the twelve sections of docs/protocol.md section 8. It depends
  only on the run's inputs, so rerunning the same commit on the same data
  reproduces it byte for byte. Its fingerprint is the trial's
  ``output_fingerprint``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from finresearch.core.fingerprint import Fingerprint, digest_bytes

SECTIONS = (
    "结论",
    "假说与机制",
    "数据",
    "方法",
    "结果",
    "边际贡献",
    "稳健性",
    "成本与容量",
    "适用范围",
    "更简单的解释",
    "局限",
    "运行清单",
)
BODY_MARKER = "<!-- finresearch:body -->"
_NOTE = "<!-- 本文件由程序生成，不要手工修改。标记以下的正文由输出指纹覆盖。 -->"
_FIELDS = {
    "trial_id": "试验编号",
    "recorded_at": "记录时间",
    "hypothesis": "假说",
    "segment": "数据段",
    "result_fingerprint": "结果指纹",
    "output_fingerprint": "输出指纹",
}
_ROW = re.compile(r"^\| (?P<key>[^|]+?) \| `?(?P<value>[^|`]+?)`? \|$")


class ReportError(ValueError):
    """A report is incomplete or malformed."""


def render_body(title: str, sections: Mapping[int, str]) -> str:
    expected = set(range(1, len(SECTIONS) + 1))
    if set(sections) != expected:
        missing = sorted(expected - set(sections))
        extra = sorted(set(sections) - expected)
        raise ReportError(f"report sections missing {missing}, unexpected {extra}")
    parts = [f"# {title}\n"]
    for i, heading in enumerate(SECTIONS, start=1):
        text = sections[i].strip()
        if not text:
            raise ReportError(f"section {i} ({heading}) is empty")
        parts.append(f"## {i}. {heading}\n\n{text}\n")
    return "\n".join(parts)


def body_fingerprint(body: str) -> Fingerprint:
    return digest_bytes(body.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class Header:
    trial_id: str
    recorded_at: str
    hypothesis: str
    segment: str
    result_fingerprint: str
    output_fingerprint: str


def render(header: Header, body: str) -> str:
    rows = [
        f"| {label} | `{getattr(header, key)}` |"
        if key.endswith("fingerprint")
        else f"| {label} | {getattr(header, key)} |"
        for key, label in _FIELDS.items()
    ]
    title = f"# 试验报告 {header.trial_id}"
    lines = [title, "", _NOTE, "", "| 项 | 值 |", "|---|---|", *rows, "", BODY_MARKER, "", body]
    return "\n".join(lines)


def parse(text: str) -> tuple[Header, str]:
    """Header and body of a report file; ``ReportError`` if the layout is wrong."""
    head, sep, rest = text.partition(f"\n{BODY_MARKER}\n\n")
    if not sep:
        raise ReportError("no body marker")
    found: dict[str, str] = {}
    labels = {label: key for key, label in _FIELDS.items()}
    for line in head.splitlines():
        m = _ROW.match(line)
        if m and m["key"] in labels:
            found[labels[m["key"]]] = m["value"]
    missing = [label for key, label in _FIELDS.items() if key not in found]
    if missing:
        raise ReportError(f"header is missing {missing}")
    return Header(**found), rest
