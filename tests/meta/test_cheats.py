import math
import random

import pytest

from finresearch.meta.bench import BenchError, bench_config, family_world, run_family, run_many
from finresearch.meta.cheats import (
    CheatsConfig,
    blockers,
    cheats_config,
    logged_trials,
    rates,
    run_cheats,
    run_cheats_many,
    window_bounds,
)
from finresearch.meta.noise import noise_model
from finresearch.stats.sharpe import moments, probabilistic_sharpe

SMALL = {
    "instruments": 8,
    "days": 600,
    "lookbacks": (1, 5),
    "holdings": (1, 5),
    "embargo": 5,
    "partitions": 4,
    "restarts": 3,
    "hypotheses": 6,
    "draws": 2000,
}
CC = CheatsConfig(
    peeks=(3, 8),
    many_peeks=(3, 6),
    windows=((0.0, 1.0), (0.0, 0.5), (0.5, 1.0)),
    t_floor=2.5,
    logged_variants=3,
    levels=(2.0, 3.0),
    runs=1,
    workers=1,
)
AT_LEAST_HONEST = ("direction", "peek 3", "peek 8", "method", "sub-period", "combined")


# ---------------------------------------------------------------- the pieces by hand
def test_window_bounds_by_hand() -> None:
    # confirmation days 10..29 (20 days): the second half is 20..29, dropping the first
    # quarter leaves 15..29, dropping the last quarter leaves 10..24
    assert window_bounds(10, 30, (0.0, 1.0)) == (10, 30)
    assert window_bounds(10, 30, (0.5, 1.0)) == (20, 30)
    assert window_bounds(10, 30, (0.25, 1.0)) == (15, 30)
    assert window_bounds(10, 30, (0.0, 0.75)) == (10, 25)


def test_logged_trials_by_hand() -> None:
    # the kept trial first, then the first of the others, as many as were logged
    assert logged_trials(3, 6, 3) == [3, 0, 1]
    assert logged_trials(0, 4, 2) == [0, 1]
    assert logged_trials(2, 3, 10) == [2, 0, 1]


def test_ranked_starts_with_the_kept_variant() -> None:
    w = family_world(bench_config(**SMALL), noise_model(), random.Random(4))
    order = w.ranked()
    assert order[0] == w.best
    assert sorted(order) == list(range(len(w.variants)))


def _row(
    t: float, dsr: float, passed: bool, fresh: float, failed: tuple[str, ...]
) -> dict[str, object]:
    claim = {
        "t": t,
        "dsr": dsr,
        "passed": passed,
        "dsr_fresh": fresh,
        "passed_fresh": not failed,
        "failed_fresh": list(failed),
    }
    return {"claims": {"honest": claim}}


def test_rates_and_blockers_by_hand() -> None:
    rows = [
        _row(2.1, 0.2, False, 0.90, ("dsr", "t_stat")),
        _row(3.2, 0.97, True, 0.99, ()),
        _row(3.1, 0.3, False, 0.98, ("pbo",)),
        _row(-1.0, 0.0, False, 0.10, ("dsr", "segments", "t_stat")),
    ]
    r = rates(rows, (2.0, 3.0), 0.95)["honest"]
    assert (r["t >= 2"].hits, r["t >= 3"].hits, r["dsr >= 0.95"].hits) == (3, 2, 1)
    assert (r["pipeline"].hits, r["pipeline"].n) == (1, 4)
    # fixed gate: three fresh probabilities reach 0.95; two claims pass the whole verdict
    assert (r["fresh dsr >= 0.95"].hits, r["pipeline (fresh dsr)"].hits) == (2, 1)
    # among claims with t >= 3 (rows 2 and 3) only PBO stopped one
    assert blockers(rows, "honest", 3.0) == {"pbo": 1}
    # without PBO (a single fixed strategy) row 3 passes as well
    assert r["pipeline (fresh dsr, no pbo)"].hits == 2


# ---------------------------------------------------------------- the claims
def test_the_honest_claim_is_the_bench() -> None:
    # same seed, same world, same draws: the honest researcher is run_family's
    for seed in range(4):
        cfg = bench_config(**SMALL, cost=0.0006)
        claims = run_cheats(cfg, CC, noise_model(), random.Random(seed), random.Random(seed))
        trial = run_family(cfg, noise_model(), random.Random(seed))
        honest = claims["honest"]
        assert honest.t == trial.t_nonoverlapping
        assert honest.dsr == trial.dsr
        assert honest.passed == trial.verdict_nonoverlapping.passed


def test_the_fixed_gate_takes_the_confirmation_against_zero() -> None:
    # The confirmation is a fresh test, so the fixed gate is its probabilistic Sharpe ratio
    # against 0. That is never below the same ratio against the family's expected best (a
    # benchmark of at least 0), and the fixed verdict passes exactly when no check failed.
    cfg = bench_config(**SMALL, cost=0.0)
    for seed in range(4):
        claims = run_cheats(cfg, CC, noise_model(), random.Random(seed), random.Random(seed))
        world = family_world(cfg, noise_model(), random.Random(seed))
        mo = moments(world.confirmation(world.variants[world.best]).net)
        expected = probabilistic_sharpe(mo.sharpe, 0.0, mo.n, mo.skew, mo.kurt)
        assert claims["honest"].dsr_fresh == expected
        for c in claims.values():
            assert c.dsr_fresh >= c.dsr
            assert c.passed_fresh == (not c.failed_fresh)


def test_no_cheat_reports_less_than_the_honest_researcher() -> None:
    # every cheat chooses among options that include the honest one, so world by world
    # its t is at least the honest t; the threshold and unlogged cheats keep the honest t
    for seed in range(15):
        for cost in (0.0, 0.0006):
            claims = run_cheats(
                bench_config(**SMALL, cost=cost),
                CC,
                noise_model(),
                random.Random(seed),
                random.Random(seed),
            )
            honest = claims["honest"]
            for name in AT_LEAST_HONEST:
                assert claims[name].t >= honest.t, (seed, cost, name)
            assert claims["threshold"].t == honest.t
            assert claims["unlogged"].t == honest.t
            # a looser threshold can only turn a rejection into a pass
            assert claims["threshold"].passed or not honest.passed


def test_without_costs_choosing_the_direction_is_the_absolute_t() -> None:
    # with no costs the mirror image's cohort returns are exactly the negatives, so the
    # researcher who picks the sign afterwards reports |t|: twice the one-sided rate
    for seed in range(6):
        cfg = bench_config(**SMALL, cost=0.0)
        claims = run_cheats(cfg, CC, noise_model(), random.Random(seed), random.Random(seed))
        assert claims["direction"].t == abs(claims["honest"].t)
        assert math.isfinite(claims["direction"].t)


def test_the_many_hypotheses_world_honest_claim_is_the_bench() -> None:
    # the same check on the many-hypotheses world: run_cheats_many's honest claim is run_many's
    for seed in range(3):
        cfg = bench_config(**SMALL, cost=0.0006)
        claims = run_cheats_many(cfg, CC, noise_model(), random.Random(seed), random.Random(seed))
        trial = run_many(cfg, noise_model(), random.Random(seed))
        assert claims["honest"].t == trial.t_nonoverlapping
        assert claims["honest"].dsr == trial.dsr
        assert claims["honest"].passed == trial.verdict_nonoverlapping.passed


def test_no_cheat_reports_less_among_the_many_hypotheses() -> None:
    # world by world, as in the family; without costs reversing a feature negates its t
    for seed in range(8):
        for cost in (0.0, 0.0006):
            claims = run_cheats_many(
                bench_config(**SMALL, cost=cost),
                CC,
                noise_model(),
                random.Random(seed),
                random.Random(seed),
            )
            honest = claims["honest"]
            for name in ("direction", "peek 3", "peek 6", "method", "sub-period", "combined"):
                assert claims[name].t >= honest.t, (seed, cost, name)
            if cost == 0.0:
                assert claims["direction"].t == abs(honest.t)


def test_the_configured_cheats_load() -> None:
    cc = cheats_config()
    assert (0.0, 1.0) in cc.windows
    assert cc.t_floor == 2.5  # config/protocol.toml's floor


def test_invalid_settings_are_refused() -> None:
    good = {
        "peeks": (3,),
        "many_peeks": (3,),
        "windows": ((0.0, 1.0),),
        "t_floor": 2.5,
        "logged_variants": 3,
        "levels": (2.0,),
        "runs": 1,
        "workers": 1,
    }
    bad = [
        {"peeks": ()},
        {"peeks": (0,)},
        {"many_peeks": ()},
        {"windows": ((0.0, 0.5),)},  # the whole segment must be offered
        {"windows": ((0.0, 1.0), (0.5, 0.5))},
        {"logged_variants": 1},
    ]
    for override in bad:
        with pytest.raises(BenchError):
            CheatsConfig(**{**good, **override})  # type: ignore[arg-type]
    with pytest.raises(BenchError):
        rates([], (2.0,), 0.95)
