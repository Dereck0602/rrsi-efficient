# DarwinX

**English** · [简体中文](./darwinx.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/darwinx.yaml)

DarwinX searches harness versions under a preserve-and-extend contract. Candidates must pass a cheap screen and fresh full-train confirmation before becoming mutation parents. Rejected but fully measured branches can still contribute complementary capabilities through recombination. Final selection uses the highest stored train score among admitted versions, including the seed; dev is unused.

## Requirements

Train must be non-empty and have unique `task_id` values. Every observed per-task primary metric must be in `[0, 1]`; this is checked when evaluations return. Dev is not required by this algorithm. Train supplies search evidence, admission decisions, and final selection, so its scores are not independent evidence of generalization. The runner evaluates the selected Snapshot on test after search.

The Worker must be a file-editing Agent (`algorithm.worker.type: agent`), with its own model and connection settings. `evolve_scope = {"*"}` permits editing all components the Target declares. Runtime-code changes require a Target that exposes that code; the algorithm does not open closed harness internals. See [harness integrations](./integrations/harnesses.md).

## Run a small example

Follow [Installation](./guide/installation.md), configure the Target and Worker connections in [examples/quickstart.yaml](../examples/quickstart.yaml), and run from the repository root:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm darwinx \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_steps 1 \
  --algorithm.screen_size 2 \
  --algorithm.screen_samples 1 \
  --algorithm.confirm_samples 2 \
  --algorithm.merge_every 0
```

This requests one mutation attempt with two confirmation repetitions and no recombination. Confirmation happens only if the candidate passes screening. Append `--check-only` for initialization without model calls or remote authentication validation. The dedicated [DarwinX configuration](../examples/darwinx.yaml) enables periodic recombination. See [Configuration & CLI](./guide/configuration.md) for overrides.

## Search and selection

1. Evaluate the seed on all train tasks `confirm_samples` times. Store its mean score and per-task means; tasks at or above `solved_threshold` become the initial preservation set. The seed is an eligible parent and final-selection fallback.
2. Select a mutation parent from admitted versions. With probability `exploration_probability`, sample uniformly; otherwise maximize `(cumulative_gain, score)`. Equal ranking keys retain the earlier archive member. Cumulative gain follows the primary-parent lineage and is not the same ranking as final score.
3. At every `merge_every`-th attempt, try recombination first. Both branches must have reached full confirmation and each must have solved a task the other did not. Admission is not required here. Rank unused pairs by solved-union size, then the lower of their scores; remaining ties follow archive enumeration order. The higher-scoring branch is primary, with the earlier branch primary on a tie.
4. Give the Worker the primary checkout, parent records, preservation set, primary traces, recent archive entries, lessons, declined edits, and component rules. For recombination, temporary ancestor and donor checkouts are available inside the working directory. The prompt asks the Worker to treat them as read-only; they are removed before committing the candidate.
5. Clean runtime artifacts and commit. Invalid resource changes, unchanged files, and duplicates of any archived harness are declined without evaluation or lesson extraction. They still consume an attempt. A selected merge pair is marked attempted even if its edit is declined; if no unused complementary pair exists, use mutation.
6. Screen the candidate on a random train subset, re-evaluating every parent on the same tasks with the same repetition count. A passing screen buys fresh confirmation of every parent and the candidate on the whole train pool. Apply the gates below.
7. Archive every evaluated candidate, including screen failures and confirmation failures. Ask the Worker for a `<LEARNINGS>` summary of that experiment. Only a candidate passing confirmation joins the mutation-parent pool and extends its inherited preservation set with newly solved tasks.
8. Promote the admitted version with the highest stored train score. Exact ties retain the earlier archived version. Selection does not re-evaluate or refresh old archive scores; it can keep the seed even when a candidate beat its parent's fresh confirmation measurements.

The Target's active version changes only at final promotion. `donor_id` records recombination input; Snapshot ancestry follows the primary parent only. Common-ancestor lookup also follows those primary-parent links.

## Screening and confirmation gates

Each repetition executes a complete requested task batch. A task's score is its mean over repetitions (**avg@k**), not best-of-k or pass@k. Let `c[t]` be the candidate's mean, `p[t]` the freshly measured primary parent's mean, and `r[t]` the maximum mean across the freshly measured parents:

```text
delta[t] = c[t] - p[t]
gain = sum(delta[t])
regression_mass = sum(max(r[t] - c[t], 0))

screen passes:
    any delta[t] > 1e-12
    and regression_mass <= screen_regression_tolerance + 1e-12

confirmation passes:
    gain > 1e-12
    and regression_mass <= regression_tolerance + 1e-12
    and every inherited invariant task has c[t] >= solved_threshold
```

For mutation, `r[t] = p[t]`. For recombination, regression is measured against the per-task maximum of both parents, while gain remains relative to primary. Recombination preserves the union of both inherited invariants and both parents' solved sets. Combining their existing capabilities can therefore extend primary without solving a task outside that union.

Regression mass is a **sum of per-task drops**, not an average or the net change. Default confirmation tolerance is zero, so gains elsewhere cannot compensate for a drop beyond numerical tolerance. The invariant gate remains mandatory even with nonzero regression tolerance. Screening does not enforce the full invariant; a local improvement can reach confirmation even if its screen-wide net gain is nonpositive.

With `solved_threshold: 1`, a task counts as solved only when every observed repetition scores 1. Accepted versions accumulate solved tasks along their lineage. These are finite-sample checks, not confidence intervals, significance tests, or guarantees on unseen tasks. Fresh parent measurements do not overwrite their stored archive scores or solved sets.

## Parameters and cost

Implementation defaults belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `max_steps` | `10` | Editing attempts; integer, at least 1. Mutations, recombinations, and declined edits all count. |
| `screen_size` | `3` | Screening task count; integer, at least 1, capped at train size. |
| `screen_samples` | `1` | Repetitions for each screened version; integer, at least 1. |
| `confirm_samples` | `3` | Repetitions for the seed and each confirmed version; integer, at least `screen_samples`. |
| `screen_regression_tolerance` | `1.0` | Nonnegative screening regression-mass allowance. |
| `regression_tolerance` | `0.0` | Nonnegative confirmation regression-mass allowance. |
| `solved_threshold` | `1.0` | Per-task mean needed for solved status; greater than 0 and at most 1. |
| `exploration_probability` | `0.5` | Uniform mutation-parent sampling probability, in `[0, 1]`. |
| `merge_every` | `3` | Try recombination every this many attempts; integer, at least 0. Zero disables it. |
| `seed` | `42` | Integer seed for parent and screen-task sampling, not model sampling. |
| `max_evidence_chars` | `24000` | Character limit per serialized evidence block; integer, at least 1. Archive, lessons, and declines are presented newest first. |

For N train tasks, s = `min(screen_size, N)`, q = `screen_samples`, and k = `confirm_samples`:

| Stage | Target task executions |
| --- | --- |
| Initial seed | `N * k` |
| Mutation screen | `2 * s * q` |
| Mutation confirmation, if reached | `2 * N * k` additional |
| Recombination screen | `3 * s * q` |
| Recombination confirmation, if reached | `3 * N * k` additional |

Each attempt calls `worker.edit` once. Each evaluated candidate also calls `worker.ask` once for lessons, even when rejected. Declined edits skip both candidate evaluation and lesson extraction. These counts exclude final test, native model/tool turns, and backend retries; they count task executions, not API requests. Worker failures and evaluation exceptions propagate; there is no algorithm-level repair loop.

## Inspect an experiment

Evaluation records retain raw repetitions. Admission decisions include `per_task`, `samples`, `fidelity`, `accepted`, `gain`, `regression_mass`, `lost_invariant`, `delta`, and `parent_per_task`. The last field contains the fresh measurements used for the comparison. `fidelity: confirmation` means full confirmation was performed; it does **not** imply acceptance.

`kind: admit` records an evaluated archive member, including rejected members. `kind: decline` records an unmeasured edit. `learnings` stores Worker summaries; final `promote` records the selected Snapshot, archive, lessons, and declined edits. A screen-only member's score covers its sampled tasks and must not be ranked as a full-pool score.

## Source and adaptation

The [implementation](../flashrsi/algorithms/darwinx/__init__.py) pins [SalesforceAIResearch/Beagle](https://github.com/SalesforceAIResearch/Beagle/tree/165343b46a85b5d28c6370e6197ac665c7eda871/beagle/algorithms/darwinx) revision `165343b46a85b5d28c6370e6197ac665c7eda871`, particularly `vendor/evolve/preserve_extend.py`, `parent_selection.py`, `merge_selection.py`, and `pipeline.py`. This is a mechanism port, not a reproduction of the complete upstream campaign.

- FlashRSI Snapshot/Worker/Benchmark interfaces replace Git worktrees, SQLite state, and campaign supervision; cross-process search resume is not implemented.
- A fixed train pool, numeric gates, and lineage invariants replace the upstream's broader configurable verdict and task-selection machinery.
- Local exploration samples eligible parents uniformly. Upstream's `mixed_high_score` broadening still ranks cumulative gain and score, then uses child counts and other tie breakers. Local pair selection uses solved-union size and scores instead of upstream utility/risk gates.
- Each mutation uses one edit call; there is no internal analyze/implement/review-and-repair sequence. Recombination supports two branches, not N-way merging or pruning/consolidation.
- Teacher models, independent verifier/anti-cheat checks, cross-benchmark or held-out admission gates, trace quality control, and cluster scheduling are not implemented. Prompt instructions alone are not an independent anti-cheat check.
- Prompt evidence is bounded by character limits; recombination source files are read from checkouts. The default algorithm uses train for final selection; a separately authorized experiment's dev-selection protocol does not change this default.
