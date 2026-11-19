"""Rougher noise: regimes, jumps and delisting (meta.noise), and the bench on it."""

import hashlib
import itertools
import math
import random
import struct
from statistics import fmean

import pytest

from finresearch.meta.bench import bench_config, rank_weights, run_family, run_many, tradable
from finresearch.meta.dry_run import Task, rough_tasks, run_task, summarize
from finresearch.meta.noise import (
    Delisting,
    Jumps,
    NoiseError,
    Regimes,
    generate,
    noise_model,
    rough,
)

ALL = ("regimes", "jumps", "delisting")
SMALL = {
    "instruments": 8,
    "days": 600,
    "lookbacks": (1, 5),
    "holdings": (1,),
    "embargo": 5,
    "partitions": 4,
    "restarts": 3,
    "hypotheses": 6,
    "draws": 500,
}


class _Fixed(random.Random):
    """A generator whose ``random()`` always returns ``value``."""

    def __init__(self, value: float) -> None:
        super().__init__(0)
        self.value = value

    def random(self) -> float:
        return self.value


def _robust_t(x: list[float], y: list[float]) -> float:
    """t of ``mean(x y) = 0`` with a heteroskedasticity-robust (White) standard error."""
    products = [a * b for a, b in zip(x, y, strict=True)]
    return math.fsum(products) / math.sqrt(math.fsum(p * p for p in products))


def _correlation(a: list[float], b: list[float]) -> float:
    ma, mb = fmean(a), fmean(b)
    return math.fsum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / math.sqrt(
        math.fsum((x - ma) ** 2 for x in a) * math.fsum((y - mb) ** 2 for y in b)
    )


# ---------------------------------------------------------------- off: nothing changes
def test_switched_off_the_noise_is_bit_for_bit_what_it_was() -> None:
    # Recorded on 2026-11-19 with the code before the optional parts existed (main at 3744355).
    m = noise_model()
    assert (m.regimes, m.jumps, m.delisting) == (None, None, None)
    r = generate(m, 5, 300, random.Random(20261119))
    digest = hashlib.sha256(b"".join(struct.pack("<d", x) for row in r for x in row)).hexdigest()
    assert digest == "d583a736a8c7470e95de75ba508bd424aee8887175da82c14d6f34c7480bfe16"
    assert r[0][0] == -0.021313066266312033
    assert r[4][299] == -0.013451105063558177
    assert rough(None, ()) == m


def test_switched_off_the_bench_is_bit_for_bit_what_it_was() -> None:
    # Recorded on 2026-11-19 with the code before delisting was handled (main at 3744355).
    m = noise_model()
    cfg = bench_config(**SMALL)
    a = run_family(cfg, m, random.Random(7))
    assert (a.t_newey_west, a.t_nonoverlapping, a.dsr, a.pbo) == (
        -0.9919528654583573,
        -0.9885264204513796,
        0.04561723107838565,
        0.0,
    )
    b = run_many(cfg, m, random.Random(8), 0.05)
    assert (b.t_newey_west, b.t_nonoverlapping, b.dsr, b.pbo) == (
        2.877039807839922,
        2.862618989302201,
        0.9754701967802063,
        0.0,
    )


def test_the_rough_world_starts_from_the_same_smooth_paths() -> None:
    # The optional parts are drawn after the GARCH paths: with jumps alone, a day without a
    # jump is exactly the smooth return
    smooth = generate(noise_model(), 4, 400, random.Random(3))
    jumpy = generate(rough(None, ["jumps"]), 4, 400, random.Random(3))
    same = sum(
        1 for a, b in zip(smooth, jumpy, strict=True) for x, y in zip(a, b, strict=True) if x == y
    )
    # P(no factor jump and no own jump) = 0.995 x 0.99 = 0.98505 of 1,600 cells: about 1,576
    assert 1500 < same < 1600


# ---------------------------------------------------------------- the pieces by hand
def test_regime_multipliers_by_hand() -> None:
    # p = 0.02, q = 0.1: turbulent share 0.02 / 0.12 = 1/6. Ratio 3: calm 1 / sqrt(5/6 + 9/6)
    # = 1 / sqrt(2.3333) = 0.654654, turbulent 1.963961; 5/6 x 0.428571 + 1/6 x 3.857143 = 1
    g = Regimes(0.02, 0.1, 3.0, 1.5)
    assert g.turbulent_share == pytest.approx(1 / 6)
    assert g.multipliers(3.0) == pytest.approx((0.654654, 1.963961), abs=1e-6)
    # ratio 1.5: 1 / sqrt(5/6 + 2.25/6) = 1 / sqrt(1.208333) = 0.909718, x 1.5 = 1.364576
    assert g.multipliers(1.5) == pytest.approx((0.909718, 1.364576), abs=1e-6)
    assert g.multipliers(1.0) == pytest.approx((1.0, 1.0))


def test_regime_paths_spend_the_stationary_share_turbulent() -> None:
    g = Regimes(0.02, 0.1, 3.0, 1.5)
    states = g.path(200_000, random.Random(1))
    assert fmean(states) == pytest.approx(1 / 6, abs=0.01)
    # a turbulent spell lasts 1 / q = 10 days on average
    spells = sum(1 for a, b in itertools.pairwise(states) if a and not b)
    assert sum(states) / spells == pytest.approx(10.0, rel=0.1)


def test_delisting_day_by_hand() -> None:
    # hazard 0.5: u = 1 - 0.3 = 0.7 gives log 0.7 / log 0.5 = 0.515 -> day 0;
    # u = 0.1 gives log 0.1 / log 0.5 = 3.32 -> day 3
    d = Delisting(0.5)
    assert d.day(_Fixed(0.3)) == 0
    assert d.day(_Fixed(0.9)) == 3
    # geometric: P(day >= k) = (1 - h)^k, mean (1 - h) / h = 99 for h = 0.01
    days = [Delisting(0.01).day(random.Random(k)) for k in range(4000)]
    assert fmean(days) == pytest.approx(99.0, rel=0.08)


def test_jump_paths_are_rare_symmetric_and_mean_zero() -> None:
    j = Jumps(0.0, 0.05, 0.0, 0.1, 3.0)
    x = j.path(0.05, 0.1, 100_000, random.Random(2))
    jumps = [v for v in x if v != 0.0]
    assert len(jumps) / len(x) == pytest.approx(0.05, abs=0.004)
    # each jump has standard deviation 0.1: the mean of 5,000 is within 4 x 0.1 / sqrt(5000)
    assert abs(fmean(jumps)) < 4 * 0.1 / math.sqrt(len(jumps))
    assert sum(1 for v in jumps if v > 0) / len(jumps) == pytest.approx(0.5, abs=0.03)
    assert j.path(0.0, 0.1, 100, random.Random(3)) == [0.0] * 100


def test_delisted_instruments_have_no_returns_afterwards() -> None:
    r = generate(rough(None, ["delisting"]), 40, 3000, random.Random(4))
    for row in r:
        dead = [t for t, x in enumerate(row) if not math.isfinite(x)]
        if dead:  # for good: every day from the first missing one
            assert dead == list(range(dead[0], 3000))
    # survival to the end: (1 - 0.0003)^3000 = 0.4065 of 40 instruments, about 16
    alive = sum(1 for row in r if math.isfinite(row[-1]))
    assert 6 <= alive <= 27


def test_turbulence_raises_volatility_and_correlation() -> None:
    # Two instruments with loading 1. Calm: factor 0.0016 x 0.428571 = 0.000686, own
    # 0.0009 x 0.827586 = 0.000745, correlation 0.4793. Turbulent: 0.006171 and 0.001676,
    # correlation 0.7864 (smooth: 0.0016 / 0.0025 = 0.64).
    model = rough(None, ["regimes"])
    assert model.regimes is not None
    r = generate(model, 2, 30_000, random.Random(5), [1.0, 1.0])
    replay = random.Random(5)  # the documented order: the smooth paths, then the regimes
    generate(noise_model(), 2, 30_000, replay, [1.0, 1.0])
    states = model.regimes.path(30_000, replay)
    for turbulent, expected in ((False, 0.4793), (True, 0.7864)):
        days = [t for t, s in enumerate(states) if s == turbulent]
        a, b = [r[0][t] for t in days], [r[1][t] for t in days]
        assert _correlation(a, b) == pytest.approx(expected, abs=0.06)
    turbulent_var = fmean([r[0][t] ** 2 for t, s in enumerate(states) if s])
    calm_var = fmean([r[0][t] ** 2 for t, s in enumerate(states) if not s])
    # 0.007847 / 0.001431 = 5.5
    assert 3.5 < turbulent_var / calm_var < 8.0


def test_bad_parameters_are_refused() -> None:
    bad = [
        lambda: Regimes(0.0, 0.1, 3.0, 1.5),
        lambda: Regimes(0.02, 1.5, 3.0, 1.5),
        lambda: Regimes(0.02, 0.1, 0.0, 1.5),
        lambda: Jumps(-0.1, 0.01, 0.06, 0.08, 3.0),
        lambda: Jumps(0.01, 0.01, -0.06, 0.08, 3.0),
        lambda: Jumps(0.01, 0.01, 0.06, 0.08, 2.0),
        lambda: Delisting(0.0),
        lambda: Delisting(1.0),
        lambda: rough(None, ["crash"]),
    ]
    for call in bad:
        with pytest.raises(NoiseError):
            call()


# ---------------------------------------------------------------- no edge, all parts on
def test_rough_noise_has_no_edge() -> None:
    # 20 instruments x 6,000 days with every part on. Each check is a t statistic with a
    # heteroskedasticity-robust standard error; |t| < 4 for each of them.
    r = generate(rough(None, ALL), 20, 6000, random.Random(6))
    live = [[x for x in row if math.isfinite(x)] for row in r]
    # 1. no drift
    pooled = [x for row in live for x in row]
    assert abs(math.fsum(pooled)) / math.sqrt(math.fsum(x * x for x in pooled)) < 4
    # 2. yesterday's return does not predict today's (pooled over instruments)
    prev = [x for row in live for x in row[:-1]]
    nxt = [x for row in live for x in row[1:]]
    assert abs(_robust_t(prev, nxt)) < 4
    assert abs(_correlation(prev, nxt)) < 0.03
    # 3. nor does yesterday's size (the volatility state) predict today's direction
    assert abs(_robust_t([abs(v) for v in prev], nxt)) < 4
    # 4. nor does yesterday's market (the cross-sectional mean) predict today's market
    days = min(len(row) for row in live)
    market = [fmean([row[t] for row in live]) for t in range(days)]
    assert abs(_robust_t(market[:-1], market[1:])) < 4
    # 5. cross-sectional momentum (yesterday's demeaned return times today's) earns nothing
    w = rank_weights(r, 1)
    pnl = [
        math.fsum(wi * (x if math.isfinite(x) else 0.0) for wi, x in zip(weights, col, strict=True))
        for weights, col in zip(w[:-1], list(zip(*r, strict=True))[1:], strict=True)
        if weights is not None
    ]
    assert abs(math.fsum(pnl)) / math.sqrt(math.fsum(p * p for p in pnl)) < 4


def test_returns_before_a_delisting_do_not_give_it_away() -> None:
    # The last 20 returns of instruments that leave look like any other: mean 0
    before: list[float] = []
    for seed in range(15):
        for row in generate(rough(None, ["delisting"]), 30, 2500, random.Random(100 + seed)):
            dead = next((t for t, x in enumerate(row) if not math.isfinite(x)), None)
            if dead is not None and dead >= 20:
                before += row[dead - 20 : dead]
    assert len(before) > 2000
    assert abs(math.fsum(before)) / math.sqrt(math.fsum(x * x for x in before)) < 4


# ---------------------------------------------------------------- the bench with delisting
def test_rank_weights_leave_out_the_delisted_by_hand() -> None:
    # day 0: instrument 1 is gone. Signals 0.3, 0.2, -0.1 rank 3, 2, 1 among three; centre 2;
    # demeaned 1, 0, -1; gross 2: weights 0.5, 0, 0, -0.5
    base = [[0.3], [math.nan], [0.2], [-0.1]]
    assert rank_weights(base, 1)[0] == pytest.approx([0.5, 0.0, 0.0, -0.5])
    # one left: no cross-section, nothing held
    assert rank_weights([[0.3], [math.nan]], 1)[0] == [0.0, 0.0]


def test_tradable_by_hand() -> None:
    whole = [[0.1, 0.2]]
    assert tradable(whole) is whole
    assert tradable([[0.1, math.nan], [0.3, 0.4]]) == [[0.1, 0.0], [0.3, 0.4]]


def test_the_bench_runs_on_rough_noise() -> None:
    cfg = bench_config(**SMALL)
    model = rough(None, ALL)
    for trial in (
        run_family(cfg, model, random.Random(9)),
        run_many(cfg, model, random.Random(9)),
        run_many(cfg, model, random.Random(9), 0.1),
    ):
        assert math.isfinite(trial.t_newey_west)
        assert math.isfinite(trial.t_nonoverlapping)
        assert 0.0 <= trial.dsr <= 1.0


# ---------------------------------------------------------------- the dry run's comparison
def test_the_rough_task_list_pairs_the_seeds() -> None:
    ts = rough_tasks()
    # two modes x (smooth + 4 rough settings) x 300 runs (config/meta.toml)
    assert len(ts) == 2 * 5 * 300
    assert ts[0] == Task("family", False, 0.0, 0, 1_000_000, ())
    assert ts[300] == Task("family", False, 0.0, 0, 1_000_000, ("regimes",))
    assert ts[-1] == Task("many", False, 0.0, 0, 3_000_299, ALL)
    smooth = run_task(Task("many", False, 0.0, 0, 3_000_000))
    assert smooth["noise"] == ""
    assert run_task(ts[1500]) == smooth  # the first smooth "many" task


def test_summarize_rough_rows_by_hand() -> None:
    base = {
        "mode": "family",
        "cost": False,
        "strength": 0.0,
        "years": 0,
        "noise": "jumps",
        "t_nw": 0.5,
        "t_no": 1.5,
        "dsr": 0.2,
        "pbo": 0.05,
        "pass_nw": False,
        "pass_no": False,
    }
    text = summarize([base, {**base, "t_no": 3.5, "t_nw": -0.5}])
    assert "family, costs off, noise jumps: 2 runs" in text
    assert "t >= 3 (non-overlapping): 1/2 = 50.0%" in text
    assert "PBO <= 0.1: 2/2 = 100.0%" in text
    # mean of 0.5 and -0.5 is 0; their sd is sqrt(0.5) = 0.707
    assert "t_nw: mean 0.000, sd 0.707" in text
    assert "costs on" not in text
