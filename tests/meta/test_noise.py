import math
import random
from statistics import fmean, stdev

import pytest

from finresearch.meta.noise import (
    Garch,
    NoiseError,
    NoiseModel,
    fit,
    fit_dof,
    garch_recursion,
    generate,
    nelder_mead,
    noise_model,
    quasi_likelihood,
    standard_t,
    t_log_likelihood,
)


def _autocorrelation(x: list[float], lag: int) -> float:
    m = fmean(x)
    d = [v - m for v in x]
    return math.fsum(a * b for a, b in zip(d, d[lag:], strict=False)) / math.fsum(v * v for v in d)


# ---------------------------------------------------------------- the pieces by hand
def test_garch_recursion_by_hand() -> None:
    # variance 1, a 0.1, b 0.8: w = 0.1. Shocks 1, 2, 0:
    # h1 = 1 -> x1 = 1; h2 = 0.1 + 0.1 x 1 + 0.8 x 1 = 1.0 -> x2 = 2;
    # h3 = 0.1 + 0.1 x 4 + 0.8 x 1 = 1.3 -> x3 = 0
    g = Garch(1.0, 0.1, 0.8, 5.0)
    assert g.omega == pytest.approx(0.1)
    assert garch_recursion(g, [1.0, 2.0, 0.0]) == pytest.approx([1.0, 2.0, 0.0])
    # the variance after x2 = 2 is 1.3: shock 1 there gives x3 = sqrt(1.3) = 1.140175
    assert garch_recursion(g, [1.0, 2.0, 1.0])[2] == pytest.approx(math.sqrt(1.3))


def test_loadings_by_hand() -> None:
    model = noise_model()
    assert model.loadings(5) == pytest.approx([0.7, 0.85, 1.0, 1.15, 1.3])
    assert model.loadings(1) == [1.0]


def test_the_config_holds_the_illustrative_model() -> None:
    m = noise_model()
    assert (m.factor.variance, m.factor.arch, m.factor.garch, m.factor.dof) == (
        0.0016,
        0.08,
        0.9,
        5.0,
    )
    assert m.idiosyncratic.arch + m.idiosyncratic.garch < 1.0
    assert m.burn_in == 500


def test_nelder_mead_finds_a_known_minimum() -> None:
    best = nelder_mead(lambda p: (p[0] - 1.0) ** 2 + 3.0 * (p[1] + 2.0) ** 2, [0.0, 0.0], 0.5)
    assert best == pytest.approx([1.0, -2.0], abs=1e-4)


def test_the_t_likelihood_by_hand() -> None:
    # dof 3, z = 1: log(Gamma(2) / (Gamma(1.5) sqrt(pi))) - 2 log(1 + 1) = log(1 / (0.886227 x
    # 1.772454)) - 1.386294 = log(0.636620) - 1.386294 = -0.451583 - 1.386294 = -1.837877
    assert t_log_likelihood([1.0], 3.0) == pytest.approx(-1.837877, abs=1e-6)
    # far out in dof it is the standard normal: log(phi(1)) = -0.5 log(2 pi) - 0.5 = -1.418939
    assert t_log_likelihood([1.0], 1e7) == pytest.approx(-1.418939, abs=1e-5)


def test_unit_variance_t_draws() -> None:
    # dof 5 has kurtosis 9, so the variance of 20,000 draws has standard error sqrt(8 / 20000)
    rng = random.Random(1)
    z = [standard_t(5.0, rng) for _ in range(20_000)]
    assert fmean([v * v for v in z]) == pytest.approx(1.0, abs=4 * math.sqrt(8 / 20_000))
    # and the dof is found again (iid draws, no GARCH): within 4 x 0.25 of 5
    assert fit_dof(z) == pytest.approx(5.0, abs=1.0)
    assert fit_dof([random.Random(2).gauss(0.0, 1.0) for _ in range(5000)]) > 30.0


def test_the_quasi_likelihood_prefers_the_true_dynamics() -> None:
    rng = random.Random(3)
    g = Garch(1.0, 0.1, 0.85, 8.0)
    x = garch_recursion(g, [standard_t(8.0, rng) for _ in range(3000)])
    assert quasi_likelihood([x], 0.1, 0.85) > quasi_likelihood([x], 0.02, 0.5)
    assert quasi_likelihood([x], 0.1, 0.85) > quasi_likelihood([x], 0.3, 0.65)


# ---------------------------------------------------------------- the factory's output
def test_noise_has_no_edge_but_looks_like_a_market() -> None:
    m = noise_model()
    returns = generate(m, 6, 4000, random.Random(4))
    for r in returns:
        sd = stdev(r)
        assert abs(fmean(r)) < 4 * sd / math.sqrt(len(r))  # no drift
        assert abs(_autocorrelation(r, 1)) < 4 / math.sqrt(len(r))  # no predictability in returns
        # clustering, seen in absolute returns: with these parameters the fourth moment is
        # infinite (a^2 kurt(z) + 2ab + b^2 = 0.0036 x 15 + 0.1104 + 0.8464 = 1.011 > 1 for the
        # instruments' own part), so squared returns would give an unreliable autocorrelation
        assert _autocorrelation([abs(v) for v in r], 1) > 4 / math.sqrt(len(r))
        m4 = fmean([(v - fmean(r)) ** 4 for v in r]) / sd**4
        assert m4 > 4.0  # fat tails (3 for a normal)
    # correlation between the extremes: loadings 0.7 and 1.3, factor 0.0016, own 0.0009:
    # 0.7 x 1.3 x 0.0016 / sqrt((0.49 x 0.0016 + 0.0009)(1.69 x 0.0016 + 0.0009))
    # = 0.001456 / sqrt(0.001684 x 0.003604) = 0.001456 / 0.0024636 = 0.5910
    a, b = returns[0], returns[-1]
    ma, mb = fmean(a), fmean(b)
    corr = math.fsum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True)) / math.sqrt(
        math.fsum((x - ma) ** 2 for x in a) * math.fsum((y - mb) ** 2 for y in b)
    )
    assert corr == pytest.approx(0.5910, abs=0.08)


# ---------------------------------------------------------------- step 3.10 acceptance
def test_the_parameters_are_found_again() -> None:
    # Generate with the config's parameters (10 instruments, 2000 days), fit with a 3-run bias
    # correction, and require every parameter within 4 standard deviations of the truth. The
    # standard deviations come from 10 to 20 offline repetitions of exactly this (2026-11-18):
    # factor arch 0.016, garch 0.025, dof 0.67; own variance 0.0001, arch 0.0036, garch 0.006,
    # dof 0.21; the factor variance swings widely (0.00105 to 0.0035), so only its order of
    # magnitude is checked. Without the correction the own dof came out 0.84 too high (5.2
    # standard deviations): mixing the instruments thins the tails; corrected it is 0.15 low.
    m = noise_model()
    rng = random.Random(5)
    f = fit(generate(m, 10, 2000, rng), rng, bias_runs=3, burn_in=m.burn_in)
    assert 0.5 * m.factor.variance < f.factor.variance < 2.5 * m.factor.variance
    assert f.factor.arch == pytest.approx(m.factor.arch, abs=4 * 0.016)
    assert f.factor.garch == pytest.approx(m.factor.garch, abs=4 * 0.025)
    assert f.factor.dof == pytest.approx(m.factor.dof, abs=4 * 0.67)
    own = m.idiosyncratic
    assert f.idiosyncratic.variance == pytest.approx(own.variance, abs=4 * 0.0001)
    assert f.idiosyncratic.arch == pytest.approx(own.arch, abs=4 * 0.0036)
    assert f.idiosyncratic.garch == pytest.approx(own.garch, abs=4 * 0.006)
    assert f.idiosyncratic.dof == pytest.approx(own.dof, abs=4 * 0.21)
    assert fmean(f.loadings) == pytest.approx(1.0)
    assert max(abs(a - b) for a, b in zip(f.loadings, m.loadings(10), strict=True)) < 0.25


def test_invalid_inputs_are_refused() -> None:
    m = noise_model()
    rng = random.Random(0)
    bad_calls = [
        lambda: Garch(0.0, 0.1, 0.8, 5.0),
        lambda: Garch(1.0, 0.3, 0.7, 5.0),  # not stationary
        lambda: Garch(1.0, -0.1, 0.8, 5.0),
        lambda: Garch(1.0, 0.1, 0.8, 2.0),  # no variance
        lambda: m.loadings(0),
        lambda: generate(m, 2, 0, rng),
        lambda: generate(m, 2, 10, rng, [1.0]),
        lambda: fit([[0.01] * 50]),  # one instrument
        lambda: fit([[0.01] * 5, [0.02] * 5]),  # too short
        lambda: fit([[0.01, math.nan] * 10, [0.02] * 20]),
        lambda: fit(generate(m, 3, 50, rng), None, bias_runs=2),  # correction without a generator
        lambda: (
            NoiseModel(m.factor, m.idiosyncratic, 1.0, 0.3, -1)
            and noise_model(
                {"simulation": {"burn_in": -1}, "factor": {}, "idiosyncratic": {}, "loadings": {}}
            )
        ),
    ]
    for call in bad_calls:
        with pytest.raises(NoiseError):
            call()
