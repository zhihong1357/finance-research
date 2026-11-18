"""The effective number of independent trials (roadmap step 3.6b).

Trials are rarely independent: twenty variants of one idea are closer to one
trial than to twenty. The deflated Sharpe ratio needs the effective number
``E[K]`` and the variance of the Sharpe ratios across those ``K`` effective
trials. López de Prado & Lewis (2019) estimate both by clustering the trials'
correlation matrix with ONC ("optimal number of clusters"); this module follows
the paper's section 8 and its appendix code (Snippets 1, 2 and 4):

- **Geometry.** ``D_ij = sqrt((1 - rho_ij) / 2)``; each trial is the point given
  by its row of ``D``, so k-means and silhouettes work on the Euclidean distance
  between rows, the "distance of distances" of section 8.1.
- **Base clustering** (Snippet 1). For each of ``n_init`` restarts and each
  ``k = 2 .. N - 1``: k-means (greedy k-means++ seeding as in scikit-learn, Lloyd
  iterations), then the
  silhouette ``s_i = (b_i - a_i) / max(a_i, b_i)`` of every trial (0 for a trial
  alone in its cluster) and the quality ``q = mean(s) / std(s)`` (population
  standard deviation, as numpy computes it). The highest ``q`` wins.
- **Top level** (Snippet 2). Each cluster's own quality is the mean over the
  standard deviation of its members' silhouettes. If more than two clusters are
  below the average quality, their members are clustered again, recursively;
  the result replaces them only if the new clustering's average cluster quality
  beats the redone clusters' old average. A cluster whose silhouettes have no
  spread (one member, or identical scores) has no quality: it is kept and left
  out of the average.
- **Cluster returns** (section 8.2, Snippet 4 as called by default): each
  cluster's trials combined with inverse-variance weights; the variance of the
  clusters' Sharpe ratios is then the ``variance`` that ``sharpe.deflated_sharpe``
  takes, with ``K`` clusters as its ``trials``. The paper does not say which
  variance; the sample variance (``K - 1``) is used. Its annualisation step
  cancels here, since all trials share one frequency.

ONC never returns fewer than two clusters (``k`` starts at 2, and a silhouette
compares clusters), so many variants of one idea come out as at least two
effective trials: the error is on the cautious side.

K-means runs on the matrix of pairwise squared distances, not on coordinates:
``|x_i - mean(C)|^2 = mean_j |x_i - x_j|^2 - mean_{j,l} |x_j - x_l|^2 / 2``
over ``j, l`` in ``C``. That is exact, and one iteration costs ``N^2`` whatever
``k`` is, which keeps this pure-Python reference usable (step 3.1b will add a
numpy version checked against it).
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

_N_INIT = 10  # restarts per k (Snippet 1's default)
_MAX_ITER = 300  # Lloyd iterations at most (scikit-learn's default, which the paper used)
_MIN_TRIALS = 3  # k runs over 2 .. N - 1
_REDO_MORE_THAN = 2  # the top level reclusters only when more than two clusters are weak
_MIN_SERIES = 2


class ClusterError(ValueError):
    """The trials cannot be clustered as asked."""


@dataclass(frozen=True)
class Clustering:
    clusters: tuple[tuple[int, ...], ...]  # trial indices, each cluster sorted, clusters by first
    silhouettes: tuple[float, ...]  # per trial
    quality: float  # mean / std of all silhouettes

    @property
    def k(self) -> int:
        return len(self.clusters)

    def labels(self, n: int) -> list[int]:
        out = [0] * n
        for c, members in enumerate(self.clusters):
            for i in members:
                out[i] = c
        return out


def _ratio(values: Sequence[float]) -> float:
    """``mean / std`` (population); NaN when the values have no spread."""
    m = math.fsum(values) / len(values)
    sd = math.sqrt(math.fsum((v - m) ** 2 for v in values) / len(values))
    return m / sd if sd > 0.0 else math.nan


def correlation_matrix(series: Sequence[Sequence[float]]) -> list[list[float]]:
    """Pearson correlations between the trials' return series."""
    if len(series) < _MIN_SERIES:
        raise ClusterError("need at least two series")
    t = len(series[0])
    if t < _MIN_SERIES or any(len(s) != t for s in series):
        raise ClusterError("every series needs the same number (at least 2) of observations")
    centred: list[list[float]] = []
    for s in series:
        m = math.fsum(s) / t
        d = [v - m for v in s]
        norm = math.sqrt(math.fsum(v * v for v in d))
        if norm == 0.0:
            raise ClusterError("a constant series has no correlation")
        centred.append([v / norm for v in d])
    n = len(series)
    out = [[1.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            r = max(
                -1.0,
                min(1.0, math.fsum(a * b for a, b in zip(centred[i], centred[j], strict=True))),
            )
            out[i][j] = out[j][i] = r
    return out


def _squared_distances(corr: Sequence[Sequence[float]], items: Sequence[int]) -> list[list[float]]:
    """Pairwise squared Euclidean distances between the rows of ``D`` (restricted to ``items``)."""
    rows = [[math.sqrt(max(0.0, (1.0 - corr[i][j]) / 2.0)) for j in items] for i in items]
    m = len(items)
    out = [[0.0] * m for _ in range(m)]
    for a in range(m):
        for b in range(a + 1, m):
            d2 = math.fsum((x - y) ** 2 for x, y in zip(rows[a], rows[b], strict=True))
            out[a][b] = out[b][a] = d2
    return out


def _seeds(p2: list[list[float]], k: int, rng: random.Random) -> list[int]:
    """Greedy k-means++ (scikit-learn's default, which the paper ran).

    The first seed is uniform. Each next seed: draw ``2 + int(ln k)`` candidates
    with chance proportional to the squared distance to the nearest seed so far,
    and keep the one that leaves the smallest total of those distances.
    """
    n = len(p2)
    trials = 2 + int(math.log(k))
    centres = [rng.randrange(n)]
    nearest = list(p2[centres[0]])
    while len(centres) < k:
        total = math.fsum(nearest)
        if total == 0.0:  # every point sits on a seed already: take any unused one
            centres.append(next(i for i in range(n) if i not in centres))
            nearest = [min(a, b) for a, b in zip(nearest, p2[centres[-1]], strict=True)]
            continue
        best: tuple[float, int, list[float]] | None = None
        for _ in range(trials):
            pick, acc, candidate = rng.random() * total, 0.0, n - 1
            for i, w in enumerate(nearest):
                acc += w
                if acc >= pick and w > 0.0:
                    candidate = i
                    break
            after = [min(a, b) for a, b in zip(nearest, p2[candidate], strict=True)]
            potential = math.fsum(after)
            if best is None or potential < best[0]:
                best = (potential, candidate, after)
        assert best is not None  # noqa: S101 - trials >= 2
        centres.append(best[1])
        nearest = best[2]
    return centres


def _kmeans(p2: list[list[float]], k: int, rng: random.Random) -> list[int]:
    """Lloyd's k-means from greedy k-means++ seeds, on pairwise squared distances ``p2``."""
    n = len(p2)
    centres = _seeds(p2, k, rng)
    labels = [min(range(k), key=lambda c, i=i: p2[i][centres[c]]) for i in range(n)]
    for _ in range(_MAX_ITER):
        sizes = [0] * k
        for c in labels:
            sizes[c] += 1
        while 0 in sizes:  # an empty cluster takes the point farthest from its own centre
            empty = sizes.index(0)
            costs = _own_costs(p2, labels, sizes, k)
            far = max((i for i in range(n) if sizes[labels[i]] > 1), key=lambda i: costs[i])
            sizes[labels[far]] -= 1
            labels[far] = empty
            sizes[empty] = 1
        sums = _sums(p2, labels, k)
        spread = [0.0] * k
        for i, c in enumerate(labels):
            spread[c] += sums[i][c]
        spread = [spread[c] / (2.0 * sizes[c] * sizes[c]) for c in range(k)]
        new: list[int] = []
        for acc in sums:
            best_c, best = 0, math.inf
            for c in range(k):
                cost = acc[c] / sizes[c] - spread[c]
                if cost < best:
                    best_c, best = c, cost
            new.append(best_c)
        if new == labels:
            break
        labels = new
    return labels


def _sums(p2: list[list[float]], labels: list[int], k: int) -> list[list[float]]:
    """For each point, the sum of its squared distances to the members of each cluster."""
    out: list[list[float]] = []
    for row in p2:
        acc = [0.0] * k
        for d, c in zip(row, labels, strict=True):
            acc[c] += d
        out.append(acc)
    return out


def _own_costs(p2: list[list[float]], labels: list[int], sizes: list[int], k: int) -> list[float]:
    """Each point's squared distance to its own cluster's centre."""
    sums = _sums(p2, labels, k)
    spread = [0.0] * k
    for i, c in enumerate(labels):
        spread[c] += sums[i][c]
    return [
        sums[i][c] / sizes[c] - spread[c] / (2.0 * sizes[c] * sizes[c]) if sizes[c] else 0.0
        for i, c in enumerate(labels)
    ]


def silhouettes(distances: Sequence[Sequence[float]], labels: Sequence[int]) -> list[float]:
    """Rousseeuw's silhouette of each point; 0 for a point alone in its cluster."""
    names = sorted(set(labels))
    if len(names) < 2:  # noqa: PLR2004 - a silhouette compares at least two clusters
        raise ClusterError("silhouettes need at least two clusters")
    index = {c: pos for pos, c in enumerate(names)}
    lab = [index[c] for c in labels]
    sizes = [0] * len(names)
    for c in lab:
        sizes[c] += 1
    out: list[float] = []
    for row, own in zip(distances, lab, strict=True):
        if sizes[own] == 1:
            out.append(0.0)
            continue
        acc = [0.0] * len(names)
        for d, c in zip(row, lab, strict=True):
            acc[c] += d
        a = acc[own] / (sizes[own] - 1)  # the point's distance to itself is 0
        b = min(acc[c] / sizes[c] for c in range(len(names)) if c != own)
        out.append((b - a) / max(a, b) if max(a, b) > 0.0 else 0.0)
    return out


def _clustering(items: Sequence[int], labels: Sequence[int], silh: Sequence[float]) -> Clustering:
    groups: dict[int, list[int]] = {}
    for pos, c in enumerate(labels):
        groups.setdefault(c, []).append(items[pos])
    clusters = tuple(sorted(tuple(sorted(g)) for g in groups.values()))
    by_item = dict(zip(items, silh, strict=True))
    return Clustering(clusters, tuple(by_item[i] for i in sorted(items)), _ratio(silh))


def _base(
    corr: Sequence[Sequence[float]], items: Sequence[int], n_init: int, rng: random.Random
) -> Clustering:
    p2 = _squared_distances(corr, items)
    dist = [[math.sqrt(v) for v in row] for row in p2]
    best: tuple[float, list[int], list[float]] | None = None
    for _ in range(n_init):
        for k in range(2, len(items)):
            labels = _kmeans(p2, k, rng)
            silh = silhouettes(dist, labels)
            q = _ratio(silh)
            if best is None or math.isnan(best[0]) or q > best[0]:
                best = (q, labels, silh)
    if best is None:
        raise ClusterError(f"need at least {_MIN_TRIALS} trials to cluster, got {len(items)}")
    return _clustering(items, best[1], best[2])


def _cluster_qualities(c: Clustering, items: Sequence[int]) -> list[float]:
    silh = dict(zip(sorted(items), c.silhouettes, strict=True))
    return [_ratio([silh[i] for i in members]) for members in c.clusters]


def _top(
    corr: Sequence[Sequence[float]], items: Sequence[int], n_init: int, rng: random.Random
) -> Clustering:
    base = _base(corr, items, n_init, rng)
    qualities = _cluster_qualities(base, items)
    defined = [q for q in qualities if not math.isnan(q)]
    if not defined:
        return base
    average = math.fsum(defined) / len(defined)
    redo = [c for c, q in enumerate(qualities) if not math.isnan(q) and q < average]
    if len(redo) <= _REDO_MORE_THAN:
        return base
    redo_items = sorted(i for c in redo for i in base.clusters[c])
    sub = _top(corr, redo_items, n_init, rng)
    kept = [members for c, members in enumerate(base.clusters) if c not in redo]
    new_clusters = kept + list(sub.clusters)
    position = {item: pos for pos, item in enumerate(items)}
    labels = [0] * len(items)
    for c, members in enumerate(new_clusters):
        for i in members:
            labels[position[i]] = c
    dist = [[math.sqrt(v) for v in row] for row in _squared_distances(corr, items)]
    candidate = _clustering(items, labels, silhouettes(dist, labels))
    new_qualities = [q for q in _cluster_qualities(candidate, items) if not math.isnan(q)]
    old_redo = math.fsum(qualities[c] for c in redo) / len(redo)
    if new_qualities and math.fsum(new_qualities) / len(new_qualities) > old_redo:
        return candidate
    return base


def onc(corr: Sequence[Sequence[float]], rng: random.Random, n_init: int = _N_INIT) -> Clustering:
    """ONC clustering of a correlation matrix (López de Prado & Lewis 2019, section 8.1)."""
    n = len(corr)
    if n < _MIN_TRIALS:
        raise ClusterError(f"need at least {_MIN_TRIALS} trials to cluster, got {n}")
    if any(len(row) != n for row in corr):
        raise ClusterError("the correlation matrix must be square")
    if not all(math.isfinite(v) and -1.0 <= v <= 1.0 for row in corr for v in row):
        raise ClusterError("correlations must be finite and within [-1, 1]")
    if any(abs(corr[i][j] - corr[j][i]) > 0.0 for i in range(n) for j in range(i)):
        raise ClusterError("the correlation matrix must be symmetric")
    if n_init < 1:
        raise ClusterError(f"need at least one restart, got {n_init}")
    return _top(corr, list(range(n)), n_init, rng)


def cluster_returns(series: Sequence[Sequence[float]], clustering: Clustering) -> list[list[float]]:
    """Each cluster's trials combined with inverse-variance weights (section 8.2, Snippet 4)."""
    t = len(series[0])
    out: list[list[float]] = []
    for members in clustering.clusters:
        inverse: list[float] = []
        for i in members:
            s = series[i]
            m = math.fsum(s) / t
            v = math.fsum((x - m) ** 2 for x in s) / (t - 1)
            if not v > 0.0:
                raise ClusterError(f"trial {i} is constant: no inverse-variance weight")
            inverse.append(1.0 / v)
        total = math.fsum(inverse)
        weights = [w / total for w in inverse]
        out.append(
            [
                math.fsum(w * series[i][r] for w, i in zip(weights, members, strict=True))
                for r in range(t)
            ]
        )
    return out


def clustered_sharpe_variance(series: Sequence[Sequence[float]], clustering: Clustering) -> float:
    """Sample variance across clusters of their per-period Sharpe ratios (``K - 1``)."""
    if clustering.k < _MIN_SERIES:
        raise ClusterError("a variance across clusters needs at least two clusters")
    ratios: list[float] = []
    for s in cluster_returns(series, clustering):
        m = math.fsum(s) / len(s)
        sd = math.sqrt(math.fsum((x - m) ** 2 for x in s) / (len(s) - 1))
        ratios.append(m / sd)
    mean = math.fsum(ratios) / len(ratios)
    return math.fsum((r - mean) ** 2 for r in ratios) / (len(ratios) - 1)
