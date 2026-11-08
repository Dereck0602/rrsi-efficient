"""Estimates affect selection, never the observed usage or reliability gate."""

import copy

import pytest

from flashrsi.algorithms.rrsi import RRSI, _judge
from flashrsi.algorithms.rrsi._cost import estimate_cost, selection_cost
from flashrsi.config import Config


def config(**values):
    return Config({**RRSI._defaults, "cost_policy": "estimate", **values})


def trial(task, repeat, observed, complete=True, **usage):
    return {
        "task_id": task,
        "trial": repeat,
        "observed_tokens": observed,
        "tokens": observed if complete else None,
        "usage_complete": complete,
        "trajectory": {"usage": usage},
    }


def measurement(rows, score=0.8):
    return {
        "score": score,
        "cost": None,
        "cost_accounting": {"complete": False},
        "evidence": rows,
        "guard_metrics": {},
    }


def test_missing_uses_only_complete_same_task_repeat_and_preserves_raw_usage():
    rows = [
        trial("a", 0, 100),
        trial("b", 0, 900),
        trial("a", 1, None, False),
        trial("b", 1, 700),
    ]
    m = measurement(rows)
    before = copy.deepcopy(rows)
    estimate_cost(m, config())
    estimate = m["cost_accounting"]["estimate"]
    assert estimate["total_tokens"] == 1800
    assert estimate["sensitivity_lower_mean"] == 1700 / 4
    assert estimate["sensitivity_upper_mean"] == 1900 / 4
    assert estimate["trials"][0]["donor_trials"] == [0]
    assert estimate["trials"][0]["donor_tokens"] == [100]
    assert rows == before and m["cost"] is None
    assert m["cost_accounting"]["complete"] is False
    assert selection_cost(m, config())[-1] == "estimated"


def test_partial_counts_failed_requests_and_estimates_tail_without_overwriting():
    calls = [
        {"role": "primary", "call_id": str(i), "usage": {"total_tokens": n}}
        for i, n in enumerate([20, 30, 50])
    ]
    row = trial("a", 0, 100, False, model_calls=calls, model_request_errors=[{}, {}])
    m = measurement([row, trial("a", 1, 500), trial("b", 0, 100), trial("b", 1, 100)])
    estimate_cost(m, config())
    estimate = m["cost_accounting"]["estimate"]["trials"][0]
    assert estimate["estimated_tokens"] == 160
    assert estimate["sensitivity_lower"] == 100
    assert estimate["sensitivity_upper"] == 300
    assert estimate["request_allowances"] == 2
    assert row["observed_tokens"] == 100 and row["tokens"] is None


@pytest.mark.parametrize(
    "kind", ["no_peer", "corrupt_trace", "too_many", "disabled", "zero"]
)
def test_unreliable_estimates_do_not_enable_cost_comparison(kind):
    rows = [
        trial("a", 0, 100),
        trial("b", 0, 100),
        trial("a", 1, None, False),
        trial("b", 1, 100),
    ]
    settings = config()
    if kind == "no_peer":
        rows[0]["task_id"] = "unrelated"
    elif kind == "corrupt_trace":
        rows[0]["task_id"] = "unrelated"
        rows[2] = trial(
            "a",
            1,
            50,
            False,
            model_calls=[{"usage": {"total_tokens": 50}}],
            unreadable_trace_entries=1,
        )
    elif kind == "too_many":
        rows[3] = trial("b", 1, None, False)
    elif kind == "disabled":
        settings.cost_policy = "complete"
    elif kind == "zero":
        rows[0]["observed_tokens"] = 0
    m = measurement(rows)
    estimate_cost(m, settings)
    assert selection_cost(m, settings) is None


def test_sensitivity_can_reject_favourable_point_estimate_and_guards_still_apply():
    base = {"score": 0.75, "cost": 100, "guard_metrics": {"safety": 1}}
    candidate = measurement(
        [
            trial("a", 0, 100),
            trial("b", 0, 100),
            trial("a", 1, None, False),
            trial("b", 1, 100),
        ],
        score=0.76,
    )
    estimate_cost(candidate, config())
    decision = _judge(
        candidate, base, 0.75, 0.1, [{"component": "skill"}], set(), config()
    )
    assert decision["utility_point"] > 0
    assert decision["utility"] < 0
    assert not decision["admissible"]
    assert decision["cost_comparison"] == "estimated_conservative"
    candidate.update(score=0.80, guard_metrics={"safety": 0})
    assert _judge(
        candidate, base, 0.75, 0.1, [{"component": "skill"}], set(), config()
    )["admissible"]
    guarded = _judge(
        candidate,
        base,
        0.75,
        0.1,
        [{"component": "skill"}],
        set(),
        config(guards={"safety": {"max_drop": 0}}),
    )
    assert not guarded["admissible"] and "guard violated" in guarded["reason"]


def test_historical_score_floor_and_gain_cost_cap_remain_active():
    base = {"score": 0.75, "cost": 100}
    candidate = measurement(
        [
            trial("a", 0, 1000),
            trial("b", 0, 1000),
            trial("a", 1, None, False),
            trial("b", 1, 1000),
        ],
        score=0.90,
    )
    estimate_cost(candidate, config())
    assert not _judge(candidate, base, 0.75, 0.1, [], set(), config())["admissible"]
    candidate["score"] = 0.79
    decision = _judge(candidate, base, 0.95, 0.1, [], set(), config())
    assert not decision["admissible"] and "historical best" in decision["reason"]
