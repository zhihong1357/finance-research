"""The noise factory: returns with no edge that look like a market (roadmap step 3.10).

The meta-research asks how often the pipeline finds something in data where
there is nothing to find. That noise must not be easier than real markets, so
it keeps what makes real returns hard: fat tails, volatility clustering and
correlation across instruments. For instrument ``i`` at time ``t``:

    r_it = loading_i x f_t + e_it

- ``f_t`` (the market factor) and each ``e_it`` (the instrument's own part) are
  GARCH(1,1) processes (Bollerslev 1986): ``x_t = sqrt(h_t) z_t`` with
  ``h_t = w + a x_{t-1}^2 + b h_{t-1}``, ``w = variance x (1 - a - b)`` so that
  ``variance`` is the unconditional variance;
- the shocks ``z_t`` are Student t with ``dof`` degrees of freedom scaled to
  unit variance (Bollerslev 1987), drawn independently;
- loadings are spread evenly over ``mean +/- spread``.

Every mean is zero: no instrument, factor or combination has an edge. The
parameters live in ``config/noise.toml`` (illustrative until step 3.14
calibrates them on the U1 exploration segment).

``fit`` goes the other way, from returns to parameters, in two steps:

1. The cross-sectional mean stands in for the factor and each instrument's
   loading is its regression slope on it, so the loadings average exactly 1
   (the scale of the factor is fixed that way). With ``N`` instruments the mean
   also carries ``1/N`` of the idiosyncratic variance; the factor variance and
   the loadings are corrected for that.
2. GARCH(1,1) by Gaussian quasi-maximum likelihood with variance targeting (the
   unconditional variance is the sample variance), maximised by Nelder-Mead
   over ``a, b`` mapped into ``a, b > 0, a + b < 1``; the instruments' own parts
   share one ``a, b`` and are fitted together. Then the t degrees of freedom by
   maximum likelihood on the standardised residuals. The Gaussian likelihood is
   used because it stays consistent when the shocks are not normal; the factor's
   dynamics are estimated from the cross-sectional mean, so with few instruments
   they are slightly mixed with the idiosyncratic ones.
"""

import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from finresearch.core.config import load_toml

_MIN_DOF = 2.0  # a t distribution has a variance only above 2 degrees of freedom
_DOF_RANGE = (0.05, 200.0)  # fitted dof - 2 is searched in this range (log scale)
_GOLDEN = (math.sqrt(5.0) - 1.0) / 2.0
_SEARCH_STEPS = 80  # golden-section steps: the bracket shrinks by 0.618^80
_NM_TOLERANCE = 1e-10  # Nelder-Mead stops when the simplex's values differ by less
_NM_MAX_STEPS = 1000
_NM_START = (-2.5, 2.0)  # a = 0.07, b = 0.86 in the mapped space: a typical GARCH(1,1)
_NM_STEP = 0.5
_MIN_PERIODS = 10


class NoiseError(ValueError):
    """The parameters or the returns do not describe a usable noise model."""


@dataclass(frozen=True)
class Garch:
    variance: float  # unconditional
    arch: float  # a
    garch: float  # b
    dof: float  # of the unit-variance t shocks

    def __post_init__(self) -> None:
        if not self.variance > 0.0:
            raise NoiseError(f"variance must be positive, got {self.variance}")
        if not (self.arch >= 0.0 and self.garch >= 0.0 and self.arch + self.garch < 1.0):
            raise NoiseError(f"need arch, garch >= 0 and arch + garch < 1, got {self}")
        if not self.dof > _MIN_DOF:
            raise NoiseError(
                f"t shocks need more than {_MIN_DOF} degrees of freedom, got {self.dof}"
            )

    @property
    def omega(self) -> float:
        return self.variance * (1.0 - self.arch - self.garch)


@dataclass(frozen=True)
class NoiseModel:
    factor: Garch
    idiosyncratic: Garch
    loading_mean: float
    loading_spread: float
    burn_in: int

    def loadings(self, instruments: int) -> list[float]:
        """Evenly spaced over ``mean +/- spread`` (just the mean for one instrument)."""
        if instruments < 1:
            raise NoiseError(f"need at least one instrument, got {instruments}")
        if instruments == 1:
            return [self.loading_mean]
        step = 2.0 * self.loading_spread / (instruments - 1)
        return [self.loading_mean - self.loading_spread + i * step for i in range(instruments)]


def _garch(section: Mapping[str, Any]) -> Garch:
    return Garch(
        float(section["variance"]),
        float(section["arch"]),
        float(section["garch"]),
        float(section["dof"]),
    )


def noise_model(config: Mapping[str, Any] | None = None) -> NoiseModel:
    """The model in ``config/noise.toml`` unless ``config`` is given."""
    cfg = load_toml("noise") if config is None else config
    burn_in = int(cfg["simulation"]["burn_in"])
    if burn_in < 0:
        raise NoiseError(f"burn-in must not be negative, got {burn_in}")
    return NoiseModel(
        _garch(cfg["factor"]),
        _garch(cfg["idiosyncratic"]),
        float(cfg["loadings"]["mean"]),
        float(cfg["loadings"]["spread"]),
        burn_in,
    )


def standard_t(dof: float, rng: random.Random) -> float:
    """A Student t draw with variance 1: ``N / sqrt(chi2_dof / dof) x sqrt((dof - 2) / dof)``."""
    chi2 = rng.gammavariate(dof / 2.0, 2.0)
    return rng.gauss(0.0, 1.0) / math.sqrt(chi2 / dof) * math.sqrt((dof - 2.0) / dof)


def garch_recursion(g: Garch, shocks: Sequence[float]) -> list[float]:
    """``x_t = sqrt(h_t) z_t`` from the shocks ``z``, starting at the unconditional variance."""
    h = g.variance
    out: list[float] = []
    for z in shocks:
        x = math.sqrt(h) * z
        out.append(x)
        h = g.omega + g.arch * x * x + g.garch * h
    return out


def _path(g: Garch, periods: int, burn_in: int, rng: random.Random) -> list[float]:
    shocks = [standard_t(g.dof, rng) for _ in range(burn_in + periods)]
    return garch_recursion(g, shocks)[burn_in:]


def generate(
    model: NoiseModel,
    instruments: int,
    periods: int,
    rng: random.Random,
    loadings: Sequence[float] | None = None,
) -> list[list[float]]:
    """``returns[i][t]`` for ``instruments`` instruments over ``periods`` periods.

    The loadings are the model's evenly spaced ones unless given.
    """
    if periods < 1:
        raise NoiseError(f"need at least one period, got {periods}")
    if loadings is None:
        loadings = model.loadings(instruments)
    elif len(loadings) != instruments:
        raise NoiseError(f"need {instruments} loadings, got {len(loadings)}")
    factor = _path(model.factor, periods, model.burn_in, rng)
    out: list[list[float]] = []
    for loading in loadings:
        own = _path(model.idiosyncratic, periods, model.burn_in, rng)
        out.append([loading * f + e for f, e in zip(factor, own, strict=True)])
    return out


# ---------------------------------------------------------------- fitting
def nelder_mead(
    f: Callable[[Sequence[float]], float], start: Sequence[float], step: float
) -> list[float]:
    """Minimise ``f`` from ``start`` (standard reflection, expansion, contraction, shrink)."""
    n = len(start)
    simplex: list[list[float]] = [[float(s) for s in start]] + [
        [float(s) + (step if i == j else 0.0) for j, s in enumerate(start)] for i in range(n)
    ]
    values = [f(p) for p in simplex]
    for _ in range(_NM_MAX_STEPS):
        order = sorted(range(n + 1), key=lambda i: values[i])
        simplex, values = [simplex[i] for i in order], [values[i] for i in order]
        if values[-1] - values[0] < _NM_TOLERANCE:
            break
        centre = [math.fsum(p[j] for p in simplex[:-1]) / n for j in range(n)]
        worst = simplex[-1]
        reflected = [c + (c - w) for c, w in zip(centre, worst, strict=True)]
        fr = f(reflected)
        if fr < values[0]:
            expanded = [c + 2.0 * (c - w) for c, w in zip(centre, worst, strict=True)]
            fe = f(expanded)
            simplex[-1], values[-1] = (expanded, fe) if fe < fr else (reflected, fr)
        elif fr < values[-2]:
            simplex[-1], values[-1] = reflected, fr
        else:
            inside = fr >= values[-1]
            towards = worst if inside else reflected
            contracted = [c + 0.5 * (t - c) for c, t in zip(centre, towards, strict=True)]
            fc = f(contracted)
            if fc < min(fr, values[-1]):
                simplex[-1], values[-1] = contracted, fc
            else:  # shrink towards the best point
                best = simplex[0]
                simplex = [best] + [
                    [b + 0.5 * (x - b) for b, x in zip(best, p, strict=True)] for p in simplex[1:]
                ]
                values = [values[0]] + [f(p) for p in simplex[1:]]
    return simplex[min(range(n + 1), key=lambda i: values[i])]


def _arch_garch(u: Sequence[float]) -> tuple[float, float]:
    """``(a, b)`` from free coordinates: ``a = e^u0 / (1 + e^u0 + e^u1)``, likewise ``b``."""
    ea, eb = math.exp(u[0]), math.exp(u[1])
    total = 1.0 + ea + eb
    return ea / total, eb / total


def quasi_likelihood(series: Sequence[Sequence[float]], arch: float, garch: float) -> float:
    """Gaussian log quasi-likelihood (without constants) of GARCH(1,1), variance-targeted."""
    total = 0.0
    for s in series:
        variance = math.fsum(x * x for x in s) / len(s)
        omega, h = variance * (1.0 - arch - garch), variance
        for x in s:
            total -= 0.5 * (math.log(h) + x * x / h)
            h = omega + arch * x * x + garch * h
    return total


def _standardised(s: Sequence[float], arch: float, garch: float) -> list[float]:
    variance = math.fsum(x * x for x in s) / len(s)
    omega, h = variance * (1.0 - arch - garch), variance
    out: list[float] = []
    for x in s:
        out.append(x / math.sqrt(h))
        h = omega + arch * x * x + garch * h
    return out


def t_log_likelihood(z: Sequence[float], dof: float) -> float:
    """Log-likelihood of unit-variance Student t shocks with ``dof`` degrees of freedom."""
    c = (
        math.lgamma((dof + 1.0) / 2.0)
        - math.lgamma(dof / 2.0)
        - 0.5 * math.log(math.pi * (dof - 2.0))
    )
    return math.fsum(c - (dof + 1.0) / 2.0 * math.log1p(x * x / (dof - 2.0)) for x in z)


def fit_dof(z: Sequence[float]) -> float:
    """Maximum-likelihood degrees of freedom (golden-section search on ``log(dof - 2)``)."""
    lo, hi = math.log(_DOF_RANGE[0]), math.log(_DOF_RANGE[1])

    def neg(u: float) -> float:
        return -t_log_likelihood(z, _MIN_DOF + math.exp(u))

    a, b = hi - _GOLDEN * (hi - lo), lo + _GOLDEN * (hi - lo)
    fa, fb = neg(a), neg(b)
    for _ in range(_SEARCH_STEPS):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - _GOLDEN * (hi - lo)
            fa = neg(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + _GOLDEN * (hi - lo)
            fb = neg(b)
    return _MIN_DOF + math.exp((lo + hi) / 2.0)


def fit_garch(series: Sequence[Sequence[float]]) -> Garch:
    """One GARCH(1,1) shared by ``series`` (each with its own mean removed)."""
    if not series or any(len(s) < _MIN_PERIODS for s in series):
        raise NoiseError(f"need series of at least {_MIN_PERIODS} periods")
    centred: list[list[float]] = []
    for s in series:
        m = math.fsum(s) / len(s)
        centred.append([x - m for x in s])
    best = nelder_mead(lambda u: -quasi_likelihood(centred, *_arch_garch(u)), _NM_START, _NM_STEP)
    a, b = _arch_garch(best)
    z = [v for s in centred for v in _standardised(s, a, b)]
    variance = math.fsum(math.fsum(x * x for x in s) / len(s) for s in centred) / len(centred)
    return Garch(variance, a, b, fit_dof(z))


@dataclass(frozen=True)
class Fit:
    factor: Garch
    idiosyncratic: Garch
    loadings: tuple[float, ...]  # average exactly 1


def fit(
    returns: Sequence[Sequence[float]],
    rng: random.Random | None = None,
    bias_runs: int = 0,
    burn_in: int = 0,
) -> Fit:
    """Parameters of the noise model from ``returns[i][t]`` (see the module docstring).

    With ``bias_runs > 0`` the degrees of freedom are bias-corrected by a
    parametric bootstrap: ``bias_runs`` data sets as large as ``returns`` are
    generated from the first fit (``burn_in`` periods dropped) and fitted
    again, and each dof is moved by the mean gap between those refits and the
    first fit (``dof - (mean(dof*) - dof)``, floored just above 2).
    """
    first = _fit(returns)
    if bias_runs <= 0:
        return first
    if rng is None:
        raise NoiseError("a bias correction needs a random generator")
    model = NoiseModel(first.factor, first.idiosyncratic, 1.0, 0.0, burn_in)
    n, t = len(returns), len(returns[0])
    refits = [_fit(generate(model, n, t, rng, first.loadings)) for _ in range(bias_runs)]

    def corrected(dof: float, again: list[float]) -> float:
        return max(_MIN_DOF + _DOF_RANGE[0], 2.0 * dof - math.fsum(again) / len(again))

    f, e = first.factor, first.idiosyncratic
    return Fit(
        Garch(f.variance, f.arch, f.garch, corrected(f.dof, [r.factor.dof for r in refits])),
        Garch(e.variance, e.arch, e.garch, corrected(e.dof, [r.idiosyncratic.dof for r in refits])),
        first.loadings,
    )


def _fit(returns: Sequence[Sequence[float]]) -> Fit:
    n = len(returns)
    if n < 2:  # noqa: PLR2004 - a factor needs at least two instruments to stand apart
        raise NoiseError("need at least two instruments")
    t = len(returns[0])
    if t < _MIN_PERIODS or any(len(r) != t for r in returns):
        raise NoiseError(f"every instrument needs the same number (>= {_MIN_PERIODS}) of periods")
    if not all(math.isfinite(x) for r in returns for x in r):
        raise NoiseError("returns must be finite")
    proxy = [math.fsum(r[k] for r in returns) / n for k in range(t)]
    pm = math.fsum(proxy) / t
    pvar = math.fsum((p - pm) ** 2 for p in proxy) / t
    if not pvar > 0.0:
        raise NoiseError("the cross-sectional mean is constant: no factor")
    slopes: list[float] = []
    residuals: list[list[float]] = []
    for r in returns:
        rm = math.fsum(r) / t
        slope = math.fsum((x - rm) * (p - pm) for x, p in zip(r, proxy, strict=True)) / t / pvar
        slopes.append(slope)
        residuals.append([(x - rm) - slope * (p - pm) for x, p in zip(r, proxy, strict=True)])
    pooled = fit_garch(residuals)
    # The proxy carries 1/N of every instrument's own part, so a residual misses about 1/N of
    # its own variance (1 - 2/N + 1/N with loadings averaging 1), and the proxy's variance holds
    # 1/N of it on top of the factor's: correct both.
    own = Garch(pooled.variance * n / (n - 1), pooled.arch, pooled.garch, pooled.dof)
    leak = own.variance / n
    factor_variance = pvar - leak
    if not factor_variance > 0.0:
        raise NoiseError("the idiosyncratic part swamps the factor: no factor variance left")
    loadings = tuple((s * pvar - leak) / factor_variance for s in slopes)
    raw = fit_garch([proxy])
    factor = Garch(factor_variance, raw.arch, raw.garch, raw.dof)
    return Fit(factor, own, loadings)
