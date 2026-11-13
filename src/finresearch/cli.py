"""Command line.

``finresearch registry check | check-append-only | check-commits | plan-fingerprint``
``finresearch run``        run a study for a hypothesis; ``--record`` enters it in the ledger
``finresearch reproduce``  rerun recorded trials from a blank state and compare fingerprints
``finresearch storage selftest``  write to the remote store, read back, compare, time it
``finresearch collect plan | run``  scheduled collection, one job per source
``finresearch monitor``  data freshness, outage issues, keeping schedules alive
``finresearch archive plan | run``  archive manifests: list, keep what changed, spot-check
``finresearch u1 plan | build``  U1's 1-minute bars: which months to build, build one month
"""

import argparse
import json
import os
import sys
import tempfile
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

from finresearch import archive, collect, monitor, u1
from finresearch.core.config import load_toml
from finresearch.core.time import format_utc, parse_utc
from finresearch.data.hub import HubRemote
from finresearch.data.pit import AccessRefusedError
from finresearch.data.selftest import selftest
from finresearch.data.store import Limits, StoreError
from finresearch.github import GitHub, GitHubError
from finresearch.registry.appendonly import check_append_only, check_commits
from finresearch.registry.ledger import read_chain
from finresearch.registry.models import Refusal, Segment, Universe, plan_fingerprint
from finresearch.registry.validate import REFUSALS, check, load
from finresearch.runner import RunError, execute, record, reproduce
from finresearch.sources.base import SourceError

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

    co = sub.add_parser("collect", help="scheduled collection").add_subparsers(
        dest="cmd", required=True
    )
    cp = co.add_parser("plan", help="print the enabled sources of a group as JSON")
    cp.add_argument("--cron", default="", help="the cron string of a scheduled run")
    cp.add_argument("--group", default="", help="a group name (manual runs)")
    cr = co.add_parser("run", help="fetch one source, keep the raw bytes, push them")
    cr.add_argument("--source", required=True)
    sub.add_parser("monitor", help="check freshness, sync outage issues, keep schedules alive")

    ar = sub.add_parser("archive", help="archive manifests").add_subparsers(
        dest="cmd", required=True
    )
    ar.add_parser("plan", help="print the enabled datasets as JSON")
    arr = ar.add_parser("run", help="list a dataset, push the increment, spot-check files")
    arr.add_argument("--dataset", required=True)
    arr.add_argument("--seed", type=int, help="seed of the spot-check sample (default: today)")

    uu = sub.add_parser("u1", help="U1's 1-minute bars").add_subparsers(dest="cmd", required=True)
    up = uu.add_parser("plan", help="print the months to build as JSON")
    up.add_argument("--exchange", required=True, choices=["binance"])
    ub = uu.add_parser("build", help="build one month and push it")
    ub.add_argument("--exchange", required=True, choices=["binance"])
    ub.add_argument("--month", required=True, help="YYYY-MM")
    return ap


def _collect(args: argparse.Namespace) -> int:
    cfg = collect.collect_config()
    if args.cmd == "plan":
        group = collect.group_for_cron(cfg, args.cron) if args.cron else args.group
        print(json.dumps(collect.plan(cfg, group)))
        return 0
    hub = load_toml("storage")["hub"]
    try:
        with tempfile.TemporaryDirectory(prefix="finresearch-collect-") as tmp:
            result = collect.run_source(
                args.source,
                cfg,
                HubRemote.from_environment(hub),
                Limits.from_config(hub),
                Path(tmp),
                datetime.now(UTC),
            )
    except (SourceError, StoreError) as exc:
        print(f"failed: {exc}")
        return 1
    added = sum(len(r.added) for r in result.pushed)
    print(f"{result.source}: {result.responses} response(s), {added} file(s) pushed")
    return 0


def _warn(text: str) -> None:
    """A warning; inside GitHub Actions also an annotation on the run page."""
    on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    print(f"::warning::{text}" if on_actions else f"warning: {text}")


def _archive(args: argparse.Namespace) -> int:
    cfg = archive.archive_config()
    if args.cmd == "plan":
        print(json.dumps(archive.plan(cfg)))
        return 0
    now = datetime.now(UTC)
    seed: int = args.seed if args.seed is not None else int(f"{now:%Y%m%d}")
    hub = load_toml("storage")["hub"]
    try:
        with tempfile.TemporaryDirectory(prefix="finresearch-archive-") as tmp:
            result = archive.run(
                args.dataset,
                cfg,
                HubRemote.from_environment(hub),
                Limits.from_config(hub),
                Path(tmp),
                now,
                seed,
            )
    except (SourceError, StoreError) as exc:
        print(f"failed: {exc}")
        return 1
    gb = result.bytes_listed / 1e9
    print(f"{result.dataset}: {result.symbols} symbol folders, {result.files} files, {gb:.2f} GB")
    print(
        f"increment {result.increment}: {result.counts}; pushed {len(result.pushed.added)} file(s)"
    )
    if result.no_checksum:
        _warn(f"{result.dataset}: {result.no_checksum} file(s) without an official checksum file")
    if result.skipped:
        _warn(
            f"{result.dataset}: {len(result.skipped)} name(s) are not data files, e.g. "
            f"{result.skipped[0]}"
        )
        for name in result.skipped:
            print(f"  skipped: {name}")
    for change, urls in result.examples.items():
        _warn(f"{result.dataset}: {result.counts[change]} file(s) {change}, e.g. {urls[0]}")
        for url in urls:
            print(f"  {change}: {url}")
    print(f"spot check (seed {seed}): {sum(c.ok for c in result.checks)}/{len(result.checks)} ok")
    for c in result.checks:
        verdict = "ok" if c.ok else "PROBLEM: " + "; ".join(c.problems)
        print(f"  {c.url} {c.size} B sha256={c.sha256[:16]} etag={c.etag}: {verdict}")
    return 0 if result.ok else 1


def _u1(args: argparse.Namespace) -> int:
    cfg = u1.u1_config()
    storage = load_toml("storage")
    hub = storage["hub"]
    try:
        remote, limits = HubRemote.from_environment(hub), Limits.from_config(hub)
        with tempfile.TemporaryDirectory(prefix="finresearch-u1-") as tmp:
            if args.cmd == "plan":
                plan = u1.plan_binance(cfg, remote, limits, Path(tmp))
                for line in plan.stale:
                    _warn(f"u1.bars_1m binance {line}")
                print(json.dumps([f"{m:%Y-%m}" for m in plan.months]))
                return 0
            year, month = (int(x) for x in args.month.split("-"))
            result = u1.build_binance(
                date(year, month, 1),
                cfg,
                remote,
                limits,
                Path(tmp),
                int(storage["canonical"]["row_group_rows"]),
            )
    except (SourceError, StoreError) as exc:
        print(f"failed: {exc}")
        return 1
    card = result.card
    print(f"{card.name}: {card.rows} rows from {len(card.inputs)} files; {card.fingerprint}")
    if result.from_daily:
        print(f"built from daily files: {', '.join(result.from_daily)}")
    if card.notes:
        # The notes come from the data itself (decision 0008): only their number goes in this
        # public log; the notes are on the partition card in the private dataset.
        _warn(f"{card.name}: {len(card.notes)} note(s) on impossible bars, on the partition card")
    print(f"pushed {len(result.pushed.added)} file(s)")
    return 0


def _monitor() -> int:
    try:
        lines = monitor.run(
            collect.collect_config(),
            archive.archive_config(),
            HubRemote.from_environment(load_toml("storage")["hub"]),
            GitHub.from_environment(),
            datetime.now(UTC),
        )
    except (StoreError, GitHubError) as exc:
        print(f"error: {exc}")
        return 1
    for line in lines:
        print(line)
    return 0


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
        if args.area == "collect":
            return _collect(args)
        if args.area == "monitor":
            return _monitor()
        if args.area == "archive":
            return _archive(args)
        if args.area == "u1":
            return _u1(args)
    except (RunError, collect.CollectError, archive.ArchiveError, u1.U1Error) as exc:
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
