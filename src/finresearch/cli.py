"""Command line: ``finresearch registry check | check-append-only | plan-fingerprint``."""

import argparse
import sys
import tomllib
from pathlib import Path

from finresearch.registry.appendonly import check_append_only
from finresearch.registry.models import plan_fingerprint
from finresearch.registry.validate import check


def _report(problems: list[str], ok: str) -> int:
    for p in problems:
        print(f"problem: {p}")
    print(ok if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="finresearch")
    sub = ap.add_subparsers(dest="area", required=True)
    reg = sub.add_parser("registry", help="registry checks").add_subparsers(
        dest="cmd", required=True
    )
    c = reg.add_parser(
        "check", help="validate every registry file and the cross-file protocol rules"
    )
    c.add_argument("--root", type=Path, default=Path())
    a = reg.add_parser(
        "check-append-only", help="compare append-only history with an earlier commit"
    )
    a.add_argument("--base", required=True, help="the earlier commit, e.g. the pull request base")
    a.add_argument("--root", type=Path, default=Path())
    f = reg.add_parser("plan-fingerprint", help="the fingerprint to record when registering a card")
    f.add_argument("card", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "check":
        return _report(check(args.root), "registry: ok")
    if args.cmd == "check-append-only":
        return _report(check_append_only(args.root, args.base), "append-only history: ok")
    raw = tomllib.loads(Path(args.card).read_text(encoding="utf-8"))
    print(plan_fingerprint(raw["plan"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
