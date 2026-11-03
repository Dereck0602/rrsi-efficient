# Meta-Harness

**English** · [简体中文](./meta-harness.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/meta_harness.yaml)

Meta-Harness lets a coding Worker retrieve historical source, scores, and full traces from files before constructing a candidate. Each editable checkout starts from the initial Snapshot, while the Worker can reuse or combine any measured version. The loop retains all measured candidates and promotes only the best dev version at the end; ties keep the earlier selected version.

## Requirements

Dev must be non-empty; train is unused. Dev feedback is available for diagnosis and selection, so it is search feedback rather than an independent final result. The runner owns test evaluation after selection; test data is not added to the algorithm's search history.

The Worker must be an Agent (`algorithm.worker.type: agent`) with file reading, search, and editing tools. Configure its model independently of the Target. All Target-declared components are editable, but the native component layout and execution interface remain constraints. Closed harness internals are not exposed by `evolve_scope = {"*"}`.

## Run a small example

Follow [Installation](./guide/installation.md) and configure both roles in [examples/quickstart.yaml](../examples/quickstart.yaml). Run from the repository root using its local QA data:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm meta_harness \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_steps 2
```

Two attempts allow the second Worker call to inspect a prior candidate or decline. They do not guarantee actual history retrieval or valid edits. Append `--check-only` for initialization without model calls or remote authentication checks. See the dedicated [Meta-Harness configuration](../examples/meta_harness.yaml) and [Configuration & CLI](./guide/configuration.md).

## Search and selection

1. Evaluate the initial Snapshot on full dev. Save it as seed and best, together with score and evaluation records.
2. For each attempt, check out seed as the editable directory. Inside it, temporarily export every measured Snapshot as native source and copy the existing framework trajectories and decisions.
3. Let the Worker selectively inspect those files, diagnose failures, and edit one candidate. The loop does not choose a historical parent by score or impose fixed mutation, crossover, or reflection stages.
4. Remove the temporary history and known runtime artifacts before committing. Invalid or unchanged edits are declined, consume one attempt, and skip candidate evaluation. Their records are visible in subsequent history.
5. Evaluate a valid candidate on full dev and retain it in history even if it regresses. Update best only on a strict improvement.
6. After `max_steps`, promote best, including seed if no candidate improved. The active Target is not switched during candidate search.

The candidate's `parent_id` is always its physical checkout/commit base, seed. It does not describe which historical mechanisms the Worker copied. Distinguish this from [ADAS](./adas.md), where each checkout starts from the current best and the prompt carries descriptions/scores rather than full historical files.

## History and resource boundaries

The temporary history contains:

| Entry | Content |
| --- | --- |
| `index.json` | Snapshot IDs, parent IDs, and paths to native source checkouts. |
| `sources/` | Every measured version, including regressions, with binary contents and executable permissions preserved. |
| `trajectories/` | Existing redacted evaluations, scores, decisions, and previous Worker traces. |

Source paths in the index are relative to the history directory. The history path given to the Worker is relative to its editing directory, so references travel with a relocated workspace. History is prompt-designated read-only reference material, not an OS-enforced read-only mount: accidental edits affect only temporary copies, removed before commit. The next attempt rebuilds history from original records and Snapshots.

History is not flattened into one prompt or truncated by an algorithm evidence-character limit. The Worker's own context and tool-output limits still apply. Each iteration re-exports the full measured history, increasing disk work as search grows; there is no history cache or index database.

Direct Python `evolve()` without a run directory uses temporary recording directories, cleaned on completion or failure. Runner-based execution keeps normal experiment records. Worker failures propagate; the temporary references and checkouts are cleaned, not adopted as resource files. This mechanism supports workspace relocation but does not by itself verify every container backend.

## Parameters and cost

The algorithm defines one parameter; model/execution budgets belong to their respective roles.

| Field | Default | Meaning |
| --- | --- | --- |
| `max_steps` | `20` | Editing attempts; integer, at least 1. Invalid attempts count. |

For D dev tasks and S attempts, Target search costs at most `(1 + S) * D` task executions. There are S Worker editing calls on normal completion; invalid edits skip candidate dev evaluation. Native model/tool turns and final test are additional. A repeated historical design can be measured again: there is no global candidate deduplication beyond checking whether the edit changed seed.

## Inspect an experiment

`kind: decline` records an unmeasured attempt. `kind: admit` records a measured candidate, its physical parent, score, and `improved_best`; admission means inclusion in history, not final selection. The final `promote` includes selected and baseline scores plus `candidate_ids`, which also includes seed.

To establish history use, inspect Worker file/tool traces. Merely providing history does not prove the Worker read it. Compare candidate resources with any claimed source versions; `parent_id` alone cannot establish conceptual ancestry.

## Source and adaptation

The [implementation](../flashrsi/algorithms/meta_harness/__init__.py) pins `stanford-iris-lab/meta-harness` to commit `8123ccabe2b19fa1123090b2e5f1bacc20963ce8`, referencing the text-classification and Terminal-Bench examples. [Prompts](../flashrsi/algorithms/meta_harness/_prompts.py) define file retrieval and edit boundaries. The prior integration note associates the method with paper identifier `2603.28052`.

This port uses one seed, one candidate per attempt, and one Benchmark primary metric to select one final Snapshot. It does not implement multiple initial harnesses, multi-objective accuracy/token frontiers, cross-process resume, upstream task-specific import/smoke checks, or cluster scheduling. The prompt's generalization instructions are not an independent anti-cheating verifier.
