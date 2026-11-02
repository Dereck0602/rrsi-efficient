# SkillOpt

**English** · [简体中文](./skillopt.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Quickstart configuration](../examples/quickstart.yaml)

SkillOpt improves skill files on two time scales. Fast updates turn successes and failures from a training batch into small edits. Slow updates compare versions across epochs and write longer-term guidance that fast edits cannot overwrite. Both kinds of resource update must strictly improve the dev score before the Target adopts them.

## Requirements

- The Target must expose a `skill` component. SkillOpt reads and edits its text files; evaluation runs the complete Snapshot through the native harness.
- Both train and dev must be non-empty. Training trajectories supply reflection evidence; dev scores decide whether to adopt a candidate. The algorithm does not read test tasks.
- The Worker can be a text LLM or an Agent. Both return JSON proposals through `ask()`; SkillOpt applies the edits. A file-editing Worker is not required.

The harness determines how skills are discovered and loaded. See [harness integrations](./integrations/harnesses.md) before changing the Target.

## Run a small example

Follow [Installation](./guide/installation.md), then run commands from the repository root. In [examples/quickstart.yaml](../examples/quickstart.yaml), configure `agent.llm` and `algorithm.worker.llm` independently: each needs its own model and credentials, plus `base_url` when using a custom endpoint.

```bash
flashrsi run --config examples/quickstart.yaml --check-only
flashrsi run --config examples/quickstart.yaml
```

`--check-only` checks initialization without model calls; it does not verify remote authentication. See [Configuration & CLI](./guide/configuration.md) for passing credentials through shell variables.

The bundled example uses MiniAgent, a text LLM Worker, and 12 local QA questions: six train, three dev, and three test. With `num_epochs: 1`, `max_steps: 1`, and `batch_size: 2`, it runs **one fast update**. It does not exercise slow updates or meta-skill generation.

To reach the slow phase at the end of a second complete epoch:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm.num_epochs 2 \
  --algorithm.max_steps null
```

This makes additional model calls. Reaching the slow phase does not guarantee an accepted update or a higher score. Use this small dataset to inspect the workflow; use the [SearchQA reproduction record](./searchqa-deepseek-reproduction.md) for a larger experimental protocol.

## Search and selection

1. Evaluate the initial Snapshot on dev and retain its score as the incumbent score.
2. Shuffle train using `seed + epoch`, then split it into training batches.
3. Execute a batch with the current Snapshot. Split its trajectories into reflection minibatches, then separate successes and failures within each minibatch. A success must have status `finished` and a primary score strictly greater than `success_threshold`.
4. Ask the Worker for edits from each non-empty group. Merge proposals within each source, then merge the two sources with a prompt that prioritizes failure fixes. If the result exceeds `edit_budget`, ask the Worker to rank and select edits.
5. Apply the patch and evaluate a changed candidate on the complete dev split. Adopt it only when `candidate_score > incumbent_score`. Ties and lower scores keep the current Snapshot. The last five fast patches rejected on dev are supplied to later reflection prompts.
6. Starting at the end of the second complete epoch, compare the previous epoch's ending Snapshot with the current one on the same sampled train tasks. When enabled, generate meta-skill memory and a slow guidance candidate. The guidance candidate passes through the same strict dev gate as fast edits.
7. Return the accepted Snapshot. The runner owns final test evaluation and experiment output; see [How a run works](./running-experiments.md).

`max_steps` counts training batches across epochs, not model calls. If the final allowed batch completes an epoch, that epoch can still run its slow phase. If the budget ends partway through an epoch, the incomplete epoch does not receive a slow update.

## What changes in a skill

Fast proposals support `append`, `insert_after`, `replace`, and `delete`. Every operation except `append` must identify text occurring exactly once in the file. Only `append` can create a file, and its path must satisfy the Target's skill component rules.

Slow updates write a protected region in existing text skill files, creating the region when absent:

```markdown
<!-- SLOW_UPDATE_START -->
Longer-term guidance
<!-- SLOW_UPDATE_END -->
```

Fast edits cannot create or change this region. This keeps a later local edit from overwriting guidance distilled across epochs.

**Meta-skill is Worker memory**, supplied to later fast reflection prompts during the current evolution run. It is not written into the Target's skill files and does not independently pass a dev gate. `use_meta_skill` and `use_slow_update` can be switched independently.

Malformed fast JSON or patch proposals caught by the patch protocol are recorded as `invalid_patch` and leave the incumbent in place. Empty patches and unchanged candidates do not trigger candidate dev evaluation. Worker execution failures propagate; slow-update and meta-skill protocol errors are outside the fast patch recovery branch.

## Parameters and cost

These are implementation defaults. A YAML configuration, including the quickstart, may override them. All fields below belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `num_epochs` | `4` | Train passes; integer, at least 1. |
| `batch_size` | `40` | Training tasks per fast update; at least 1. |
| `reflection_minibatch_size` | `8` | Trajectories per reflection group before success/failure separation; at least 1. |
| `edit_budget` | `4` | Maximum applied edits per fast update; at least 1. |
| `merge_batch_size` | `8` | Proposals combined per merge call; at least 2. |
| `seed` | `42` | Integer seed for shuffle and slow-phase sampling. |
| `max_steps` | `null` | Total training-batch limit; positive integer or `null`. |
| `success_threshold` | `0.0` | A finished trajectory must score strictly above this value to enter the success group. |
| `use_slow_update` | `true` | Generate and evaluate protected guidance updates. |
| `use_meta_skill` | `true` | Generate optimizer memory for subsequent fast reflections. |
| `slow_update_samples` | `20` | Train tasks for a slow comparison, capped by train size; at least 1. |
| `max_evidence_chars` | `30000` | Character limit for each serialized evidence block, not the entire prompt or a token limit. |

Target work includes baseline dev, each training batch, dev evaluation for changed candidates, and two sample evaluations per slow comparison. Worker work can include multiple reflection, merge, ranking, meta-skill, and guidance calls. A step is therefore not a fixed call budget; dev size and reflection grouping also affect cost.

## Inspect an experiment

Read the saved `decision` records alongside the candidate Snapshot and its task trajectories:

- `kind: fast` or `slow` identifies the update phase.
- Evaluated proposals record `score`, `baseline_score`, and `accepted`, with parent and candidate IDs.
- `invalid_patch`, `empty_patch`, and `no_change` explain updates that did not reach candidate dev evaluation.

For example, a candidate scoring the same as the baseline is evaluated but rejected. A malformed fast patch is rejected before candidate dev evaluation. These outcomes have different costs and should not be counted as accepted improvements.

## Source and adaptation

The repository's [SearchQA reproduction record](./searchqa-deepseek-reproduction.md) pins upstream `microsoft/SkillOpt` to commit `79124b37e9a6371e13b753f8bcd7adb1e493ade1` and documents the paper comparison. This page describes the local implementation; upstream comparisons follow that record.

Local choices include a fixed edit budget, component-scoped multi-file JSON patches, `seed + epoch` shuffling, and minibatch splitting before success/failure grouping. Both fast and slow resource candidates use a strict dev gate. Reproducing an experiment also requires matching model settings, initial skills, dataset members, grading, and recovery behavior.

Implementation: [search loop](../flashrsi/algorithms/skillopt/__init__.py), [prompts](../flashrsi/algorithms/skillopt/_prompts.py), and [patch protocol](../flashrsi/algorithms/skillopt/_patch.py).

## Validation scope

The [SearchQA reproduction record](./searchqa-deepseek-reproduction.md) documents larger historical runs with MiniAgent and Claude Code, including protocol differences and recovery attempts. Those results belong to their recorded configurations.

The quickstart configuration is deliberately limited to one fast update. Initialization checks do not establish real-model success, and a fast-only smoke does not validate slow guidance or meta-skill behavior. When reporting a run, name the algorithm, Target, Benchmark, budget, and phases actually exercised.
