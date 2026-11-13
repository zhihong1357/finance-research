"""H-0001, the Phase 1.6 thin slice: does the door keep revisions out of a backtest?

In the synthetic world (``finresearch.sources.synthetic``) an indicator for
month ``m`` is first released with noise and later revised to its true value;
the return of month ``m + lag`` depends on the true value. A bet on month
``m + lag`` is placed when that month starts, when only the first release is
public.

For every month whose return the run may see, the study opens a view of the
door stopped at the decision instant and takes the indicator as it was known
then. A naive backtest instead uses the indicator as known at the end of the
run, revisions included. With the world's parameters both correlations are
known in advance, so the report can say whether the pipeline got them right.
"""

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from finresearch.core.time import ensure_utc, month_start
from finresearch.data.pit import PointInTime, Snapshot, start_of_day
from finresearch.stats.correlation import fisher_interval, pearson, t_statistic
from finresearch.studies.base import Study, StudyResult

SOURCE = "synthetic.thin_slice"
INDICATOR = "synthetic.indicator"
RETURNS = "synthetic.returns"


def _by_month(snapshot: Snapshot) -> dict[datetime, float]:
    return {ensure_utc(r["event_time"]): float(r["value"]) for r in snapshot.rows()}


def theory(config: Mapping[str, Any]) -> tuple[float, float]:
    """Population correlations with the return: (first release, true value)."""
    beta = float(config["beta"])
    sd_y = math.sqrt(beta * beta + float(config["return_noise"]) ** 2)
    sd_first = math.sqrt(1.0 + float(config["release_noise"]) ** 2)
    return beta / (sd_y * sd_first), beta / sd_y


def compute(pit: PointInTime, config: Mapping[str, Any]) -> StudyResult:
    lag, confidence = int(config["lag_months"]), float(config["confidence"])
    returns = _by_month(pit.latest(RETURNS))
    revised = _by_month(pit.latest(INDICATOR))  # what a naive backtest would use
    x_pit: list[float] = []
    x_revised: list[float] = []
    y: list[float] = []
    for decision in sorted(returns):
        month = start_of_day(month_start(decision.date(), -lag))
        known_then = _by_month(pit.view(decision).latest(INDICATOR))
        if month in known_then and month in revised:
            x_pit.append(known_then[month])
            x_revised.append(revised[month])
            y.append(returns[decision])
    n = len(y)
    r_pit, r_revised = pearson(x_pit, y), pearson(x_revised, y)
    low, high = fisher_interval(r_pit, n, confidence)
    th_pit, th_revised = theory(config)
    differ = sum(a != b for a, b in zip(x_pit, x_revised, strict=True))
    metrics = {
        "pairs": float(n),
        "corr_pit": r_pit,
        "corr_pit_low": low,
        "corr_pit_high": high,
        "t_pit": t_statistic(r_pit, n),
        "corr_revised": r_revised,
        "t_revised": t_statistic(r_revised, n),
        "theory_pit": th_pit,
        "theory_revised": th_revised,
        "pairs_differing": float(differ),
    }
    first_decision, last_decision = min(returns), max(returns)
    sections = _sections(metrics, config, first_decision, last_decision, pit.clock.limit)
    return StudyResult(metrics, sections)


def _f(v: float) -> str:
    return f"{v:.3f}"


def _sections(
    m: Mapping[str, float],
    config: Mapping[str, Any],
    first: datetime,
    last: datetime,
    limit: datetime,
) -> dict[int, str]:
    n, pct = int(m["pairs"]), round(float(config["confidence"]) * 100)
    lag = int(config["lag_months"])
    inside = m["corr_pit_low"] <= m["theory_pit"] <= m["corr_pit_high"]
    gap, gap_theory = m["corr_revised"] - m["corr_pit"], m["theory_revised"] - m["theory_pit"]
    span = f"{first:%Y-%m} 至 {last:%Y-%m}"
    return {
        1: (
            f"在真相已知的合成世界里，按决策时刻能看到的首发值计算，指标与 {lag} 个月后收益的"
            f"相关系数为 {_f(m['corr_pit'])}（{pct}% 置信区间 {_f(m['corr_pit_low'])} 至 "
            f"{_f(m['corr_pit_high'])}，{n} 对），理论值 {_f(m['theory_pit'])} "
            f"{'落在区间内' if inside else '不在区间内，需要排查'}。"
            f"改用事后的修订值，相关系数为 {_f(m['corr_revised'])}，比时点值高 {_f(gap)}"
            f"（理论差值 {_f(gap_theory)}）。这个差就是修订带来的前视偏差："
            "时点查询接口在每个决策时刻只给出当时公布了的版本，把它挡在了回测之外。"
        ),
        3: (
            f"数据源 `{SOURCE}`：由代码按固定种子生成，模仿保存全部历史版本的宏观数据接口"
            "（像 FRED 的 ALFRED）。两张表：\n\n"
            f"- `{INDICATOR}`：每月一个观测、两个版本。首发值在月份结束后 "
            f"{config['first_release_days']} 天公布，含误差；修订值在 {config['revision_days']} "
            "天后公布，等于真实值。\n"
            f"- `{RETURNS}`：每月收益，在该月结束时公布。\n\n"
            f"本次用到的决策月份：{span}，共 {n} 个；运行的时钟上限 {limit:%Y-%m-%d %H:%M:%S} UTC。"
            "三段划分见第 2 节。已知缺陷：数据是合成的，不含真实市场的任何特征。"
        ),
        4: (
            f"每个月初做一次决策：打开一个时钟停在决策时刻的视图，取当时能看到的 {lag} "
            "个月前那个月的指标（只能是首发值），与该月收益配对。对照组用运行时钟上限时能看到的"
            "版本（修订值），这是常见回测的做法。相关系数为皮尔逊相关，t 值为 "
            "r·√((n−2)/(1−r²))，置信区间用费希尔变换。与注册方案的偏离：本假说处于探索阶段，"
            "还没有冻结的检验方案。"
        ),
        5: (
            "| 量 | 时点数据（首发值） | 修订后数据（对照） |\n"
            "|---|---|---|\n"
            f"| 相关系数 | {_f(m['corr_pit'])} | {_f(m['corr_revised'])} |\n"
            f"| 理论值 | {_f(m['theory_pit'])} | {_f(m['theory_revised'])} |\n"
            f"| t 值 | {m['t_pit']:.2f} | {m['t_revised']:.2f} |\n"
            f"| 配对数 | {n} | {n} |\n\n"
            f"时点相关系数的 {pct}% 置信区间：{_f(m['corr_pit_low'])} 至 {_f(m['corr_pit_high'])}。"
            "试验次数与折损：合成数据上的运行不计入试验次数（科研规程第 7 节），"
            "所以不做多重检验折损。"
        ),
        6: "不适用：这是元研究（P0），研究对象是流程本身，不产生进入整体模型的信号。",
        7: (
            "本次运行不做。换随机种子、换误差大小、换公布延迟后结论是否成立，"
            "属于 Phase 3 元研究的内容。"
        ),
        8: "不适用：没有交易，也不涉及成本。",
        9: (
            f"只适用于这个合成世界：一个月度指标、一次修订、{lag} 个月的领先期。"
            "结论关于流程本身（时点查询能否把修订挡在回测之外），不关于任何真实市场。"
        ),
        10: (
            f"1. 两个相关系数的差别只是抽样误差。观察到的差值 {_f(gap)} 与理论差值 "
            f"{_f(gap_theory)} 方向一致；单次抽样不能完全排除巧合，Phase 3 会用多个种子重复。\n"
            "2. 时点值和修订值其实是同一个数，差别来自别处。排除：两组指标逐对比较，"
            f"{int(m['pairs_differing'])} / {n} 对不同。\n"
            f"3. 生成数据本身有错。理论值由生成参数直接算出，时点估计的置信区间"
            f"{'包含' if inside else '不包含'}理论值。"
        ),
        11: (
            "合成世界只有一次修订，公布延迟固定；真实数据的修订次数、延迟和公布时刻都不固定，"
            f"还有停发和更改定义。{n} 对样本的置信区间仍然较宽。"
        ),
    }


STUDY = Study("thin_slice", (SOURCE,), compute)
