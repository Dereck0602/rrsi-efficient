# Arbor

**English** · [简体中文](./arbor.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/arbor.yaml)

Arbor organizes search as a hypothesis tree. A Coordinator expands research directions and chooses experiments; an Executor implements each hypothesis from the current selected trunk. Train screens candidates, while strict dev improvement controls adoption. Experiment lessons are synthesized upward through the tree to inform later planning. Tree ancestry describes ideas, not code inheritance.

## Requirements

Train and dev must both be non-empty. Task IDs must be unique within and disjoint across train/dev/test. Baseline train and dev trajectories must all finish; otherwise search stops. The algorithm uses the Benchmark's primary-metric mean without a special `[0, 1]` restriction, and interprets larger scores as better.

The Worker must be a separate file-editing Agent (`algorithm.worker.type: agent`) with independent model and connection settings. Coordinator, Executor, reflection, and synthesis are different prompts sent to this same configured Worker, not separately configured models. All Target-declared components are editable (`evolve_scope = {"*"}`); editing runtime code requires a harness that exposes it.

Dev is used repeatedly for selection and is not an independent final test. Test tasks, scores, and trajectories are not supplied to these search prompts. The runner evaluates the selected Snapshot on test after search.

## Run a small example

Follow [Installation](./guide/installation.md), configure both role connections in [examples/quickstart.yaml](../examples/quickstart.yaml), and run from the repository root:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm arbor \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_cycles 2 \
  --algorithm.max_depth 2 \
  --algorithm.merge_threshold 0.0
```

This uses local SearchQA data and permits at most two dispatched experiments. Coordinator planning can stop earlier, and the Worker may make multiple native model/tool calls per request. Append `--check-only` for initialization without model calls or remote authentication validation. The dedicated [Arbor example](../examples/arbor.yaml) instead uses a pinned HotpotQA context dataset with MiniAgent Target and Claude Code Worker.

## Search and selection

1. Evaluate the initial Snapshot once on full train and full dev. Create `ROOT` with the initial scores and version; initialize the selected trunk from that Snapshot.
2. Ask the Coordinator to expand directions, dispatch a pending experiment leaf, prune a subtree, or stop. It sees the tree, current trunk's file names and component rules, current train traces, and saved train/dev scores.
3. Dispatch a hypothesis to the Executor in a checkout of the **current trunk**. The conceptual parent might be a rejected experiment or belong to a different line of inquiry; its Snapshot is not automatically checked out. Record the actual editing base as `base_snapshot_id`.
4. Clean runtime artifacts and commit. Invalid resource changes or no changed files produce `needs_retry`, with no candidate evaluation. There is no separate framework compile/diff-review gate before train evaluation in this algorithm.
5. Evaluate a changed candidate on full train. Any non-finished trajectory prevents adoption. Otherwise, compare its mean with the saved train mean of the current trunk. If the gain reaches `merge_threshold`, evaluate full dev.
6. Adopt only if all dev trajectories finish and dev mean strictly exceeds the saved trunk dev score. Update the internal trunk and its saved train/dev evidence together. A train gate failure or a dev tie/regression retains the trunk.
7. Reflect on the experiment, including unsuccessful edits and regressions. Synthesize lessons into each ancestor, bottom-up, using its children that have insights. Save the tree and current trunk, then plan the next experiment.
8. At the cycle limit or Coordinator stop, promote the last selected trunk. There is no separate final archive ranking or remeasurement of all candidates. With no adoption, the seed remains selected.

The adoption rule is:

```text
all candidate train trajectories finished
and candidate_train - trunk_train >= merge_threshold
and all candidate dev trajectories finished
and candidate_dev > trunk_dev
```

`merge_threshold` is an absolute train-score difference in the primary metric's units, not a relative percentage or a dev threshold. At the default zero, a train tie can reach dev, but a dev tie never replaces the trunk. These comparisons have no additional numerical tolerance. Unchanged trunks are not re-evaluated each cycle, so the reference is their saved measurement.

## Tree actions and statuses

The Coordinator returns one JSON object in `<ARBOR>...</ARBOR>`. The available actions are:

| Action | Conditions and effect |
| --- | --- |
| `expand` | Add 1–3 nonempty hypotheses under `root`, `direction`, `done`, or `merged` nodes below `max_depth`. |
| `run` | Dispatch a `pending` leaf at depth at least 2. |
| `prune` | Mark a non-root node and all descendants `pruned`; retain their records and lessons. |
| `stop` | End search and keep the selected trunk. |

ROOT has depth 0, directions depth 1, and executable hypotheses start at depth 2. IDs follow creation order (`1`, `1.1`, etc.). A successful expansion does not itself spend an experiment cycle. Each dispatch/stop decision allows at most 12 Coordinator calls, including expansions, pruning, and malformed actions; exhausting them raises an error rather than silently dispatching an experiment.

| Status | Meaning |
| --- | --- |
| `root` / `direction` | Planning nodes rather than executable hypotheses. |
| `pending` | An experiment not yet dispatched. |
| `running` | The experiment is being implemented. |
| `done` | A scored experiment that was not adopted, including train-screened or dev-rejected candidates. |
| `merged` | The candidate was adopted at that time; later experiments may replace it. |
| `needs_retry` | Empty/invalid edit or non-finished candidate evaluation. No automatic retry is implemented. |
| `pruned` | Removed from further eligible planning/execution, while evidence remains. |

`needs_retry` nodes cannot currently be dispatched or expanded again: those actions require other statuses. The Coordinator can create an alternative hypothesis elsewhere. Pruning a node does not roll back a previously adopted trunk or delete its Snapshot; conceptual pruning and resource selection are separate operations.

## Evidence and insight propagation

The Executor sees the selected experiment, serialized tree and inherited insights, component rules, and actual trunk files. The prompt asks it to implement reusable mechanisms while keeping models, credentials and evaluation data fixed. It returns a prose account; the edited files determine the candidate.

Reflection sees the experiment record and candidate train trajectories, when available. It may see a measured dev score in the record, but raw dev trajectories are not passed into reflection. A null dev score means that stage was not evaluated; it is not a zero score. An invalid edit yields an empty trajectory-evidence list plus its rejection reason.

For an experiment at depth d, synthesis makes one Worker call for each of its d ancestors, through ROOT. Each call reads all immediate children with nonempty insights, including retained lessons from rejected or pruned experiments. This updates textual lessons, not numeric scores or selection probabilities. There is no UCB formula or numerical tree-backup rule in the implementation; the Coordinator makes planning choices through its prompt.

Each serialized evidence block is capped by `max_evidence_chars` and marked when truncated. Full tree/evaluation records are still saved, but the Worker is not guaranteed to see every historical row in every prompt. Reflection and synthesis prompts request fewer than 200 words; the algorithm does not mechanically enforce that word limit. ROOT's stored Snapshot/scores describe the initial version; the separate current-trunk record supplies the updated selection state.

## Parameters and cost

Defaults belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `max_cycles` | `10` | Maximum dispatched experiments; integer, at least 1. Invalid or empty edits also consume one. |
| `max_depth` | `3` | Maximum hypothesis depth; integer, at least 2, with ROOT at 0. |
| `merge_threshold` | `0.0` | Nonnegative minimum train gain required to reach dev evaluation. |
| `max_evidence_chars` | `30000` | Character limit per serialized prompt-evidence block; integer, at least 1. |

For N train tasks, M dev tasks, K test tasks and C = `max_cycles`, Target task executions are at most `(C + 1) * (N + M) + K`: one baseline train/dev evaluation, at most one candidate train/dev evaluation per experiment, then final test. Invalid edits skip both candidate evaluations; failed train execution or insufficient train gain skips dev. These are task executions, not API requests or native tool turns.

Each dispatched experiment calls the Worker once for editing, once for reflection, and d times for ancestor synthesis, plus up to 12 planning calls before dispatch. A stop decision also uses planning calls. The maximum depth therefore affects Worker cost even when the experiment budget is unchanged.

Worker failures and evaluation exceptions propagate. Returned non-finished candidate trajectories are recorded as `needs_retry`; they do not automatically stop later planning. There is no framework resume of an old checkout, automatic experiment retry, or hidden repair budget.

## Records and adaptation

`arbor-plan` saves expansions/pruning and the tree; `arbor-plan-rejected` saves invalid-action feedback; `arbor-stop` records early stopping. After each experiment and insight propagation, `arbor-tree` records the cycle, `trunk_id`, and nodes. Final `promote` records the selected Snapshot, dev score, and tree. Worker trajectories are recorded by the shared framework.

A node's `parent_id` is its hypothesis parent; `base_snapshot_id` is the actual resource version edited; `snapshot_id` is the produced candidate. These differ from Snapshot's resource lineage. Tree state is algorithm metadata and is not embedded in the evolving harness resources.

The [implementation](../flashrsi/algorithms/arbor/__init__.py) pins [RUC-NLPIR/Arbor](https://github.com/RUC-NLPIR/Arbor/tree/7cdaf1fa6d779b3d5e340052357bf3fe55dfed93) commit `7cdaf1fa6d779b3d5e340052357bf3fe55dfed93`, referencing Coordinator tree/prompts and executor/tree/Git tools.

- Upstream's Executor iteration and merge-evaluation signals (`B_dev` and `B_test`) map to train and dev here. FlashRSI test remains sealed until final selection; local adoption requires strict dev improvement.
- Snapshot checkouts and selection replace worktrees and Git merging. A local `merged` status means adoption, not a three-way Git merge or automatic conflict resolution.
- Experiments run sequentially from the current trunk. Executor continuation, parallel dispatch, cross-run persistent experience, arbitrary Coordinator tools, literature search, and interactive approvals are not implemented.
- Upstream's standalone CLI, MCP service, dashboard, and benchmark collection are outside this algorithm port. The local framework owns execution, scoring, resources, and records; no harness- or benchmark-specific branches are used in the algorithm.
