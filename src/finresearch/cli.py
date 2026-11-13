"""Command line.

``finresearch registry check | check-append-only | check-commits | plan-fingerprint``
``finresearch run``        run a study for a hypothesis; ``--record`` enters it in the ledger
``finresearch reproduce``  rerun recorded trials from a blank state and compare fingerprints
``finresearch storage selftest``  write to the remote store, read back, compare, time it
"""

import argparse
import json
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from finresearch.core.config import load_toml
from finresearch.core.time import format_utc, parse_utc
from finresearch.data.hub import HubRemote
from finresearch.data.pit import AccessRefusedError
from finresearch.data.selftest import selftest
from finresearch.data.store import Limits, StoreError
from finresearch.registry.appendonly import check_append_only, check_commits
from finresearch.registry.ledger import read_chain
from finresearch.registry.models import Refusal, Segment, Universe, plan_fingerprint
from finresearch.registry.validate import REFUSALS, check, load
from finresearch.runner import RunError, execute, record, reproduce

EXIT_REFUSED = 2


def _report(problems: list[str], ok: str) -> int:
    for p in problems:
        print(f"problem: {p}")
    print(ok if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def _parser() -> argparse.ArgumentParser:
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
    k = reg.add_parser("check-commits", help="every commit in the ledger is in this history")
    k.add_argument("--root", type=Path, default=Path())
    f = reg.add_parser("plan-fingerprint", help="the fingerprint to record when registering a card")
    f.add_argument("card", type=Path)

    r = sub.add_parser("run", help="run a study for a hypothesis on one data segment")
    r.add_argument("--study", required=True)
    r.add_argument("--hypothesis", required=True)
    r.add_argument("--segment", required=True, choices=[s.value for s in Segment])
    r.add_argument("--record", action="store_true", help="enter the run in the trial ledger")
    r.add_argument("--json-out", type=Path, help="write the run summary as JSON")
    r.add_argument("--root", type=Path, default=Path(), help="repository root")
    r.add_argument("--work", type=Path, help="data directory (default: a fresh temporary one)")
    r.add_argument("--now", type=parse_utc, help="the present for clocks (default: now)")

    p = sub.add_parser("reproduce", help="rerun recorded trials and compare fingerprints")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--trial", help="one trial, e.g. T-000001")
    which.add_argument("--synthetic", action="store_true", help="every trial on synthetic data")
    p.add_argument("--root", type=Path, default=Path())

    st = sub.add_parser("storage", help="the remote store").add_subparsers(
        dest="cmd", required=True
    )
    t = st.add_parser("selftest", help="write, read back and compare; needs HF_TOKEN, HF_DATASET")
    t.add_argument("--run-id", help="folder name for this check (default: the current time)")
    return ap


def _selftest(args: argparse.Namespace) -> int:
    cfg = load_toml("storage")
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        remote = HubRemote.from_environment(cfg["hub"])
        with tempfile.TemporaryDirectory(prefix="finresearch-selftest-") as tmp:
            report = selftest(
                remote, Path(tmp), cfg["selftest"], Limits.from_config(cfg["hub"]), run_id
            )
    except StoreError as exc:
        print(f"error: {exc}")
        return 1
    print(report.to_json())
    return 0 if report.ok else 1


def _run(args: argparse.Namespace) -> int:
    recorded_at = datetime.now(UTC)
    now: datetime = args.now or recorded_at
    with tempfile.TemporaryDirectory(prefix="finresearch-run-") as tmp:
        work: Path = args.work or Path(tmp)
        try:
            out = execute(
                args.root,
                args.study,
                args.hypothesis,
                Segment(args.segment),
                work=work,
                now=now,
                recorded_at=recorded_at,
            )
        except AccessRefusedError as exc:
            last = Refusal.model_validate(read_chain(args.root / REFUSALS)[-1])
            print(f"refused: {exc}")
            print(f"recorded as {last.refusal_id} in {REFUSALS}")
            return EXIT_REFUSED
    if args.json_out:
        args.json_out.write_text(json.dumps(out.summary(), sort_keys=True, indent=2) + "\n")
    for name, value in out.metrics.items():
        print(f"{name}: {value:.6g}")
    print(f"result fingerprint: {out.manifest.fingerprint}")
    print(f"output fingerprint: {out.output_fingerprint}")
    if not out.manifest.reproducible:
        print("note: uncommitted changes, so this run cannot be recorded")
    if args.record:
        trial, path = record(args.root, out, recorded_at)
        print(f"recorded {trial.trial_id} at {format_utc(trial.recorded_at)}; report: {path}")
    return 0


def _reproduce(args: argparse.Namespace) -> int:
    reg, problems = load(args.root)
    if problems:
        return _report(problems, "")
    trials = [
        t
        for t in reg.trials
        if t.trial_id == args.trial
        or (
            args.synthetic
            and reg.hypotheses[t.hypothesis].card.scope.universe is Universe.SYNTHETIC
        )
    ]
    if args.trial and not trials:
        return _report([f"no trial {args.trial} in the ledger"], "")
    found: list[str] = []
    for t in trials:
        print(f"reproducing {t.trial_id} at {t.manifest['code']['commit'][:12]} ...")
        found.extend(reproduce(args.root, t))
    return _report(found, f"reproduced {len(trials)} trial(s): fingerprints match")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        if args.area == "run":
            return _run(args)
        if args.area == "reproduce":
            return _reproduce(args)
        if args.area == "storage":
            return _selftest(args)
    except RunError as exc:
        print(f"error: {exc}")
        return 1
    if args.cmd == "check":
        return _report(check(args.root), "registry: ok")
    if args.cmd == "check-append-only":
        return _report(check_append_only(args.root, args.base), "append-only history: ok")
    if args.cmd == "check-commits":
        return _report(check_commits(args.root), "recorded commits: ok")
    raw = tomllib.loads(Path(args.card).read_text(encoding="utf-8"))
    print(plan_fingerprint(raw["plan"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
