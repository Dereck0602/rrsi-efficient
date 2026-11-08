"""Worker truncation cannot discard the incumbent or leak partial edits."""

import asyncio
import copy
from io import StringIO

import pytest
from test_rrsi import Worker, change, saved, setup

from flashrsi._progress import progress_session
from flashrsi._run_summary import _collect_usage
from flashrsi.core import Trajectory

OUTPUT_ERROR = (
    "llm_error: Chat Completions generation did not complete: length "
    "(output_tokens=16384); output budget exhausted. "
    "worker-private-secret target-private-secret"
)


def truncated(error=OUTPUT_ERROR):
    return Trajectory(
        status="error",
        error_message=error,
        usage={
            "model_calls": [
                {"usage": {"prompt_tokens": 100, "completion_tokens": 16384}}
            ]
        },
    )


def good_edit(prompt, directory):
    (directory / "skills/main.md").write_text("GOOD")
    return [change()]


@pytest.mark.parametrize("stage", ["analyze", "digest", "propose", "critic"])
def test_one_truncation_recovers_with_same_limits_and_fresh_checkout(
    monkeypatch, tmp_path, stage
):
    original = Worker._run
    failures, directories, configs = [], [], []

    async def run(self, prompt, working_dir=None):
        configs.append(copy.deepcopy(dict(self.config.llm)))
        if prompt.startswith("[RRSI: propose]"):
            directories.append(working_dir)
            assert (working_dir / "skills/main.md").read_text() == "SEED"
        if prompt.startswith(f"[RRSI: {stage}]") and not failures:
            failures.append(working_dir)
            if stage == "propose":
                (working_dir / "skills/main.md").write_text("PARTIAL")
                (working_dir / "skills/partial.md").write_text("discard this edit")
            return truncated()
        if prompt.startswith("[RRSI: propose]"):
            assert not (working_dir / "skills/partial.md").exists()
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)
    algo, calls = setup(monkeypatch, tmp_path, good_edit)
    algo.worker.config.llm.update(
        max_tokens=16384,
        reasoning_effort="high",
        extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )
    expected_config = copy.deepcopy(dict(algo.worker.config.llm))
    output = StringIO()
    with progress_session("log", stream=output):
        result = asyncio.run(algo.evolve())

    assert result.files["skills/main.md"].content == b"GOOD"
    assert all(config == expected_config for config in configs)
    assert len(failures) == 1
    assert all(not directory.exists() for directory in directories)
    assert not list((algo._run_dir / "working").iterdir())
    assert {marker for _, marker, _ in calls} == {"SEED", "GOOD"}
    if stage in {"propose", "critic"}:
        assert len(directories) == 2 and directories[0] != directories[1]
    records = saved(algo, "worker-output-budget")
    assert len(records) == 1 and records[0]["action"] == "retry"
    assert "action=retry" in output.getvalue()
    usage = _collect_usage(algo._run_dir)
    assert usage["roles"]["worker"]["failed"]["records"] == 1
    failed = [
        row["trajectory"]
        for row in saved(algo, "worker")
        if row["trajectory"]["status"] == "error"
    ]
    assert failed[0]["usage"]["model_calls"][0]["usage"]["completion_tokens"] == 16384
    recorded = str(records) + str(failed) + output.getvalue()
    assert "worker-private-secret" not in recorded
    assert "target-private-secret" not in recorded


@pytest.mark.parametrize("retries", [0, 1])
def test_exhausted_candidate_is_skipped_but_siblings_and_rounds_continue(
    monkeypatch, tmp_path, retries
):
    original = Worker._run
    failures = []

    async def run(self, prompt, working_dir=None):
        if prompt.startswith("[RRSI: propose]") and "candidate 0." in prompt:
            failures.append(working_dir)
            (working_dir / "skills/main.md").write_text("PARTIAL")
            return truncated()
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)
    algo, calls = setup(
        monkeypatch,
        tmp_path,
        good_edit,
        T=2,
        m=2,
        repair_rounds=5,
        worker_output_retries=retries,
    )
    result = asyncio.run(algo.evolve())

    assert result.files["skills/main.md"].content == b"GOOD"
    assert len(failures) == 2 * (retries + 1)
    assert all(not directory.exists() for directory in failures)
    assert "PARTIAL" not in {marker for _, marker, _ in calls}
    history = [row for row in saved(algo, "edit-history") if row["variant"] == 0]
    assert len(history) == 2
    assert all(row["candidate_id"] is None for row in history)
    assert all(row["outcome"] == "REJECTED" for row in history)
    assert all("output budget exhausted" in row["reason"] for row in history)
    summaries = saved(algo, "round-summary")
    assert [row["round"] for row in summaries] == [1, 2]
    assert all(row["best_score"] == 1 for row in summaries)
    assert not list((algo._run_dir / "working").iterdir())


@pytest.mark.parametrize("stage", ["analyze", "digest"])
def test_exhausted_analysis_skips_only_that_round(monkeypatch, tmp_path, stage):
    original = Worker._run
    failures = 0

    async def run(self, prompt, working_dir=None):
        nonlocal failures
        if prompt.startswith(f"[RRSI: {stage}]") and failures < 2:
            failures += 1
            return truncated()
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, good_edit, T=2)
    seed_id = algo.target.snapshot.id
    result = asyncio.run(algo.evolve())

    assert result.files["skills/main.md"].content == b"GOOD"
    summaries = saved(algo, "round-summary")
    assert summaries[0]["status"] == "skipped"
    assert summaries[0]["current_snapshot_id"] == seed_id
    assert summaries[0]["current_score"] == 0.5
    assert not summaries[0]["accepted_candidate"]
    assert summaries[1]["accepted_candidate"]
    assert [row["action"] for row in saved(algo, "worker-output-budget")] == [
        "retry",
        "skip",
    ]
    assert all(row["t"] == 1 for row in saved(algo, "edit-history"))
    assert not list((algo._run_dir / "working").iterdir())


def test_all_candidates_exhausted_retains_incumbent(monkeypatch, tmp_path):
    original = Worker._run

    async def run(self, prompt, working_dir=None):
        if prompt.startswith("[RRSI: propose]"):
            return truncated()
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, good_edit, T=2, m=2)
    seed_id = algo.target.snapshot.id
    assert asyncio.run(algo.evolve()).id == seed_id
    assert len(saved(algo, "measurement")) == 1  # Only the baseline was measured.
    assert len(saved(algo, "round-summary")) == 2
    assert len(saved(algo, "worker-output-budget")) == 8


@pytest.mark.parametrize(
    "error",
    [
        "llm_error: 401 Unauthorized",
        "llm_error: connection refused",
        "llm_error: context_length_exceeded",
        "MiniAgent timed out after 3600s",
        "llm_error: Chat Completions generation did not complete: content_filter",
    ],
)
def test_other_worker_failures_still_propagate(monkeypatch, tmp_path, error):
    calls = []

    async def run(self, prompt, working_dir=None):
        calls.append(prompt)
        return truncated(error)

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, good_edit)
    with pytest.raises(RuntimeError, match="failed"):
        asyncio.run(algo.evolve())
    assert len(calls) == 1
    assert not saved(algo, "worker-output-budget")


def test_cancellation_is_never_retried(monkeypatch, tmp_path):
    calls = []

    async def run(self, prompt, working_dir=None):
        calls.append(prompt)
        raise asyncio.CancelledError()

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, good_edit)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(algo.evolve())
    assert len(calls) == 1
    assert not saved(algo, "worker-output-budget")


@pytest.mark.parametrize("retries", [-1, True, 1.5])
def test_invalid_worker_output_retries_rejected(monkeypatch, tmp_path, retries):
    with pytest.raises(ValueError, match="worker_output_retries"):
        setup(monkeypatch, tmp_path, good_edit, worker_output_retries=retries)
