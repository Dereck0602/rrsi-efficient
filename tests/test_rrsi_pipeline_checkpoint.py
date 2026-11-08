"""Run interruption/identity checks through shared queue and pipelined modes."""

import asyncio

import pytest
import test_rrsi_checkpoint as cases
from test_rrsi import Worker, change, saved, setup
from test_rrsi_checkpoint import (  # noqa: F401
    test_any_complete_round_restores_history_floor_and_timing,
    test_finished_error_trial_is_kept_without_selective_retry,
    test_incompatible_or_corrupt_resume_fails_before_inference,
    test_partial_aggregate_logs_are_regenerated_once,
    test_round_mode_discards_partial_round_trials,
    test_runner_resume_and_cli_entry,
    test_trial_resume_matches_uninterrupted_and_reuses_completed_calls,
    test_trial_resume_twice_and_final_test_trial_cache,
)

from flashrsi.algorithms.rrsi._checkpoint import CheckpointResume
from flashrsi.algorithms.rrsi._pipeline import GENERATION


@pytest.fixture(autouse=True, params=["queue", "pipeline"])
def execution_mode(monkeypatch, request):
    original = cases.build

    def build(*args, **kwargs):
        return original(
            *args,
            shared_candidate_queue=True,
            proposal_concurrency=2 if request.param == "pipeline" else 1,
            pipeline_evaluation=request.param == "pipeline",
            **kwargs,
        )

    monkeypatch.setattr(cases, "build", build)


def test_ready_candidate_trials_survive_interrupted_sibling_generation(
    monkeypatch, tmp_path
):
    def edit(prompt, directory):
        generation = GENERATION.get()
        (directory / "skills/main.md").write_text(
            "GOOD" if generation.variant == 0 else "BEST"
        )
        return [change()]

    options = dict(
        outcomes={"SEED": (0.5, 100), "GOOD": (0.8, 110), "BEST": (1, 120)},
        T=1,
        m=2,
        k=2,
        proposal_concurrency=2,
        shared_candidate_queue=True,
        pipeline_evaluation=True,
    )
    old, _ = setup(monkeypatch, tmp_path / "old", edit, **options)
    original_worker = Worker._run
    original_target = old.target.rollout
    completed = 0
    sibling_cancelled = False

    async def worker(agent, prompt, working_dir=None):
        nonlocal sibling_cancelled
        generation = GENERATION.get()
        if (
            generation is not None
            and generation.variant == 1
            and prompt.startswith("[RRSI: propose]")
        ):
            try:
                await asyncio.Event().wait()
            finally:
                sibling_cancelled = True
        return await original_worker(agent, prompt, working_dir)

    async def rollout(tasks, *, snapshot=None):
        nonlocal completed
        if snapshot.files["skills/main.md"].content != b"SEED":
            if completed == 2:
                raise RuntimeError("pipeline interruption")
            completed += 1
        return await original_target(tasks, snapshot=snapshot)

    with monkeypatch.context() as local:
        local.setattr(Worker, "_run", worker)
        local.setattr(old.target, "rollout", rollout)
        with pytest.raises(RuntimeError, match="pipeline interruption"):
            asyncio.run(asyncio.wait_for(old.evolve(), 5))
    assert sibling_cancelled
    assert not list((old._run_dir / "working").iterdir())
    current, calls = setup(monkeypatch, tmp_path / "new", edit, **options)
    CheckpointResume(old._run_dir).bind(current)
    final = asyncio.run(current.evolve())
    assert final.files["skills/main.md"].content == b"BEST"
    assert len([c for c in calls if c[1] == "GOOD"]) == 2
    assert len([c for c in calls if c[1] == "BEST"]) == 4
    proposals = [row for row in saved(current, "worker") if row.get("variant") == 0]
    assert len(proposals) == 2  # Original edit + review; no repeated proposal.
    assert [row["variant"] for row in saved(current, "edit-history")] == [0, 1]
