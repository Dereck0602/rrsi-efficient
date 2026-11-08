"""Selection-only token estimates; provider counters remain untouched.

The sensitivity range is a heuristic stress scenario, not a confidence interval
or a guaranteed bound on unreported work. Donors never cross harnesses/tasks.
"""

from __future__ import annotations

from statistics import mean, median

POLICY = "tail_or_peer_v1"


def estimate_cost(measurement, config):
    """Attach reproducible estimates without changing cost, tokens, or usage."""
    from flashrsi.algorithms.rrsi import _counter, _token_accounting

    accounting = measurement["cost_accounting"]
    accounting.pop("estimate", None)
    if config.get("cost_policy", "complete") != "estimate" or accounting["complete"]:
        return
    trials = measurement["evidence"]
    estimates, values, lower, upper = [], [], [], []
    unresolved = []
    for row in trials:
        observed = row["observed_tokens"]
        if row["usage_complete"] and _counter(observed):
            values.append(observed)
            lower.append(observed)
            upper.append(observed)
            continue
        usage = row["trajectory"].get("usage") or {}
        calls = usage.get("model_calls") or []
        counted = [_token_accounting(call.get("usage")) for call in calls]
        # A readable serial trace can lose a final request. Recorded failures
        # add one allowance each; do not assume their server-side work was zero.
        readable = (
            calls
            and all(ok and value is not None for value, ok in counted)
            and not usage.get("unreadable_trace_entries", 0)
            and all(call.get("role", "primary") == "primary" for call in calls)
        )
        detail = {
            "task_id": row["task_id"],
            "trial": row["trial"],
            "observed_tokens": observed,
        }
        if _counter(observed) and readable:
            recent = [value for value, _ in counted[-5:]]
            messages = row["trajectory"].get("messages") or []
            failures = max(
                len(usage.get("model_request_errors") or []),
                sum(m.get("kind") == "model_request_error" for m in messages),
            )
            allowances = max(1, failures)
            point = observed + allowances * median(recent)
            high = observed + 2 * allowances * max(recent)
            detail.update(
                method="observed_plus_request_tail",
                request_allowances=allowances,
                reference_call_ids=[call.get("call_id") for call in calls[-5:]],
                reference_request_tokens=recent,
            )
        else:
            peers = [
                peer
                for peer in trials
                if peer["task_id"] == row["task_id"]
                and peer["trial"] != row["trial"]
                and peer["usage_complete"]
                and _counter(peer["observed_tokens"])
                and peer["observed_tokens"] > 0
            ]
            if not peers:
                unresolved.append(
                    {
                        **detail,
                        "reason": "no readable request tail or complete same-harness/task repeat",
                    }
                )
                continue
            donor_tokens = [peer["observed_tokens"] for peer in peers]
            point = max(observed or 0, mean(donor_tokens))
            high = max(observed or 0, 2 * max(donor_tokens))
            detail.update(
                method="same_harness_task_repeat",
                donor_trials=[peer["trial"] for peer in peers],
                donor_tokens=donor_tokens,
            )
        low = observed if observed is not None else 0
        estimates.append(
            {
                **detail,
                "estimated_tokens": point,
                "sensitivity_lower": low,
                "sensitivity_upper": high,
            }
        )
        values.append(point)
        lower.append(low)
        upper.append(high)
    fraction = (len(estimates) + len(unresolved)) / len(trials) if trials else 1
    eligible = (
        bool(trials)
        and not unresolved
        and fraction <= config.get("cost_estimate_max_frac", 0.25)
        and sum(lower) > 0
        and sum(values) > 0
    )
    resolved = bool(trials) and not unresolved
    accounting["estimate"] = {
        "policy": POLICY,
        "eligible": eligible,
        "estimated_trials": len(estimates),
        "estimated_fraction": fraction,
        "max_fraction": config.get("cost_estimate_max_frac", 0.25),
        "total_tokens": sum(values) if resolved else None,
        "mean_tokens": mean(values) if resolved else None,
        "sensitivity_lower_mean": mean(lower) if resolved else None,
        "sensitivity_upper_mean": mean(upper) if resolved else None,
        "trials": estimates,
        "unresolved": unresolved,
        "note": "Heuristic sensitivity range, not a confidence interval or guaranteed bound. Provider usage remains incomplete; estimates are for selection only.",
    }


def selection_cost(measurement, config):
    """Return (point, lower, upper, source) or None for unusable costs."""
    from flashrsi.algorithms.rrsi import _counter

    accounting = measurement.get("cost_accounting", {})
    cost = measurement.get("cost")
    if _counter(cost) and cost > 0 and accounting.get("complete", True):
        return cost, cost, cost, "full"
    estimate = accounting.get("estimate", {})
    if (
        config.get("cost_policy", "complete") == "estimate"
        and estimate.get("policy") == POLICY
        and estimate.get("eligible")
        and estimate.get("estimated_fraction", 1)
        <= config.get("cost_estimate_max_frac", 0.25)
    ):
        point, low, high = (
            estimate.get(key)
            for key in (
                "mean_tokens",
                "sensitivity_lower_mean",
                "sensitivity_upper_mean",
            )
        )
        if all(_counter(v) for v in (point, low, high)) and 0 < low <= point <= high:
            return point, low, high, "estimated"
    return None
