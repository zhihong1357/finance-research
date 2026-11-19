from finresearch.meta.dry_run import Task, run_task, summarize, tasks


def test_the_task_list_is_fixed() -> None:
    ts = tasks()
    # four noise settings of 200 runs, then 3 strengths x 4 lengths of 25 (config/meta.toml)
    assert len(ts) == 4 * 200 + 3 * 4 * 25
    assert len({t.seed for t in ts}) == len(ts)
    assert ts[0] == Task("family", False, 0.0, 0, 1_000_000)
    assert ts[-1] == Task("many", True, 0.1, 8, 22_000_024)
    assert tasks() == ts


def test_a_strong_planted_task_is_found() -> None:
    # strength 0.1 over one year: kept and supported in all 25 runs of the dry run
    row = run_task(Task("many", True, 0.1, 1, 19_000_000))
    assert row["selected_index"] == 0
    assert row["pass_no"]
    assert row["pass_nw"]


def test_summarize_counts_by_hand() -> None:
    base = {
        "mode": "many",
        "cost": True,
        "strength": 0.0,
        "years": 0,
        "selected_index": 3,
        "clusters": 2,
        "equivalent_trials": 3.0,
        "t_nw": 0.5,
        "t_no": 0.5,
        "dsr": 0.2,
        "pbo": 0.5,
        "pass_nw": False,
        "pass_no": False,
    }
    rows = [base, {**base, "t_no": 3.5, "t_nw": 1.5, "dsr": 0.97}]
    text = summarize(rows)
    assert "many, costs on: 2 runs" in text
    assert "t >= 3 (non-overlapping): 1/2 = 50.0%" in text
    assert "t >= 3 (Newey-West): 0/2 = 0.0%" in text
    assert "deflated Sharpe >= 0.95: 1/2" in text
    assert "ONC clusters: {2: 2}" in text
    assert "equivalent independent trials: median 3.0, range 3.0 to 3.0" in text
    planted = {**base, "strength": 0.05, "years": 2, "selected_index": 0, "pass_no": True}
    assert "detection: strength 0.05, 2 years: kept 1/1, supported 1/1" in summarize([planted])
