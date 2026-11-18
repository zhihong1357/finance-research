import math
import random
from statistics import fmean, stdev

import pytest

from finresearch.stats.overfitting import (
    Metric,
    OverfittingError,
    logit,
    probability_of_overfitting,
    relative_rank,
)


def test_two_halves_by_hand() -> None:
    # S = 2: two combinations, first half trains / second tests, and the reverse. Means:
    #           first half  second half
    #   A [2, 2, -1, -1]    2       -1
    #   B [0, 0,  0,  0]    0        0
    #   C [1, 1,  1,  1]    1        1
    # Train on the first half: A wins (2); on the second A is worst, rank 1:
    #   w = 1 / 4, logit ln(1/3) = -1.098612.
    # Train on the second half: C wins (1); on the first C ranks 2 of 3 (B 0 < C 1 < A 2):
    #   w = 2 / 4, logit 0, which counts as overfit (the mass at 0 is included).
    trials = [[2.0, 2.0, -1.0, -1.0], [0.0] * 4, [1.0] * 4]
    r = probability_of_overfitting(trials, 2, Metric.MEAN)
    assert r.logits == (pytest.approx(math.log(1 / 3)), 0.0)
    assert r.pbo == 1.0
    assert r.selected == ((2.0, -1.0), (1.0, 1.0))
    assert r.probability_of_loss == 0.5
    assert r.combinations == 2


def test_four_pieces_by_hand() -> None:
    # S = 4, one observation per piece: C(4, 2) = 6 combinations, in itertools order.
    #   A [3, 0, 0, 0], B [0, 1, 1, 1]
    # train {0,1}: A 1.5, B 0.5 -> A; test {2,3}: A 0, B 1 -> A rank 1, w 1/3, logit ln(1/2)
    # train {0,2}: A 1.5, B 0.5 -> A; test {1,3}: A 0, B 1 -> ln(1/2)
    # train {0,3}: A 1.5, B 0.5 -> A; test {1,2}: A 0, B 1 -> ln(1/2)
    # train {1,2}: A 0,   B 1   -> B; test {0,3}: A 1.5, B 0.5 -> B rank 1 -> ln(1/2)
    # train {1,3}, {2,3}: likewise B wins and loses on the test set -> ln(1/2)
    # Every winner is the loser out of sample: PBO 1.
    r = probability_of_overfitting([[3.0, 0.0, 0.0, 0.0], [0.0, 1.0, 1.0, 1.0]], 4, Metric.MEAN)
    assert r.logits == tuple([pytest.approx(math.log(0.5))] * 6)
    assert r.pbo == 1.0
    # and a trial that is better everywhere is never overfit: logit ln(2) in all six
    r = probability_of_overfitting([[1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 0.0, 0.0]], 4, Metric.MEAN)
    assert r.logits == tuple([pytest.approx(math.log(2.0))] * 6)
    assert r.pbo == 0.0


def test_ranks_and_logits_by_hand() -> None:
    # winner 1 (value 5) among [3, 5, 5, 1]: two below it, the tie counts against it: rank 3,
    # w = 3 / 5
    assert relative_rank([3.0, 5.0, 5.0, 1.0], 1) == pytest.approx(0.6)
    assert relative_rank([3.0, 5.0, 5.0, 1.0], 3) == pytest.approx(0.2)
    assert logit(0.5) == 0.0
    assert logit(0.75) == pytest.approx(math.log(3.0))


def test_sixteen_pieces_give_12870_combinations() -> None:
    # C(16, 8) = 12,870 (the paper prints 12,780, a transposition)
    rng = random.Random(1)
    trials = [[rng.gauss(0.0, 1.0) for _ in range(32)] for _ in range(2)]
    assert probability_of_overfitting(trials, 16).combinations == math.comb(16, 8) == 12_870


def test_the_sharpe_ratio_pools_the_pieces_exactly() -> None:
    # the training winner's metric equals the Sharpe ratio computed directly on its rows
    rng = random.Random(2)
    trials = [[rng.gauss(0.01 * k, 1.0) for _ in range(60)] for k in range(5)]
    r = probability_of_overfitting(trials, 6)
    first_train = [v for p in (0, 1, 2) for v in trials[0][10 * p : 10 * (p + 1)]]
    candidates = []
    for series in trials:
        rows = [v for p in (0, 1, 2) for v in series[10 * p : 10 * (p + 1)]]
        candidates.append(fmean(rows) / stdev(rows))
    assert r.selected[0][0] == pytest.approx(max(candidates), rel=1e-12)
    assert len(first_train) == 30


# ---------------------------------------------------------------- step 3.7 acceptance
def _noise(n_trials: int, t: int, rng: random.Random) -> list[list[float]]:
    return [[rng.gauss(0.0, 1.0) for _ in range(t)] for _ in range(n_trials)]


@pytest.mark.parametrize("n_trials", [10, 11])
def test_pure_noise_gives_about_one_half(n_trials: int) -> None:
    # No skill: the winner's test rank is uniform on 1..N, so PBO is ceil(N / 2) / N on average:
    # 0.5 for 10 trials, 6/11 = 0.545455 for 11. Averaged over 60 independent noise families.
    rng = random.Random(20261118 + n_trials)
    pbos = [probability_of_overfitting(_noise(n_trials, 240, rng), 8).pbo for _ in range(60)]
    expected = math.ceil(n_trials / 2) / n_trials
    assert abs(fmean(pbos) - expected) < 3 * stdev(pbos) / math.sqrt(len(pbos))


def test_a_real_edge_drives_pbo_towards_zero() -> None:
    # One trial earns ``edge`` standard deviations a period among 19 pure-noise trials, T = 240.
    # On a half (120 periods) a Sharpe ratio has standard error about 1 / sqrt(120) = 0.091, and
    # the best of 19 noise trials is about 1.85 x 0.091 = 0.17. So an edge of 0.25 often wins in
    # training but not always, and 0.5 wins almost always and then stays in the top half on test:
    # PBO should fall with the edge and be near 0 at 0.5. Means over 20 families each.
    def mean_pbo(edge: float) -> float:
        rng = random.Random(7)
        pbos = []
        for _ in range(20):
            trials = _noise(19, 240, rng)
            trials.append([rng.gauss(edge, 1.0) for _ in range(240)])
            pbos.append(probability_of_overfitting(trials, 8).pbo)
        return fmean(pbos)

    none, some, strong = mean_pbo(0.0), mean_pbo(0.25), mean_pbo(0.5)
    assert strong < some < none
    assert strong < 0.01


def test_invalid_inputs_are_refused() -> None:
    bad_calls = [
        lambda: probability_of_overfitting([[1.0, 2.0]], 2),  # one trial
        lambda: probability_of_overfitting([[1.0, 2.0], [1.0]], 2),  # ragged
        lambda: probability_of_overfitting([[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]], 2),  # 3 into 2
        lambda: probability_of_overfitting([[1.0] * 6, [2.0] * 6], 3),  # odd
        lambda: probability_of_overfitting([[1.0] * 6, [2.0] * 6], 0),
        lambda: probability_of_overfitting([[1.0, math.nan], [2.0, 1.0]], 2),
        lambda: probability_of_overfitting([[1.0] * 4, [2.0, 1.0, 2.0, 1.0]], 2),  # constant: SR
    ]
    for call in bad_calls:
        with pytest.raises(OverfittingError):
            call()
