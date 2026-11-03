# ModularRSI

**English** · [简体中文](./modular-rsi.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [MiniAgent configuration](../examples/modular_rsi.yaml) · [Claude Code configuration](../examples/modular_rsi_claude_code.yaml)

ModularRSI evolves independent module branches from one seed, then combines and refines them. Repeated executions of the same train task supply successful and failed traces for diagnosis. File checks, diff review, and execution gates decide whether a branch advances; there is no train/dev score-improvement gate or best-score archive. A candidate can replace its parent despite equal or lower task scores if it passes those checks.

## Requirements

Train must be non-empty, its task IDs unique and disjoint from dev/test IDs. Dev is not used for diagnosis, candidate selection, or Worker prompts. The runner evaluates the final selected Snapshot on test. The algorithm uses the current `benchmark.train`; an independent external evolution dataset is optional.

The Worker must be a separate Agent instance with `algorithm.worker.type: agent`. It keeps its own harness resources, model, and connection settings while editing Target checkouts. Target and Worker may use different harnesses. The Target's active Snapshot changes only at final promotion, so a broken candidate does not replace the Worker's execution resources.

`algorithm.modules` must name either all five runtime modules or all three extension modules below. Every module needs a non-empty list of existing Target Snapshot files. `evolve_scope = {"*"}` permits Target components generally, while module file lists further restrict each edit.

## Module boundaries

For MiniAgent, the five functional modules can map to native files as follows:

```yaml
modules:
  agent_loop: [runtime/agent.py]
  tool_use: [runtime/agent.py, tools/shell.py]
  observation_management: [runtime/agent.py, runtime/tool_result.py]
  context_management: [runtime/agent.py]
  task_completion_detection: [runtime/agent.py]
```

Multiple modules may share a file. The framework enforces **file boundaries**; functional boundaries within those files are instructions checked by the Worker review, not AST isolation or function-level permissions. `file.py::function` is not accepted. Changes may add or remove functions, signatures, or decorators inside allowed files; new file paths must already exist in the initial resource mapping to be eligible.

Claude Code exposes native extension resources instead of its closed runtime source. Its three-module configuration is:

```yaml
modules:
  prompt: [CLAUDE.md]
  skill: [skills/workflow/SKILL.md]
  hook: [hooks/hooks.json]
```

The [Claude Code example](../examples/modular_rsi_claude_code.yaml) supplies these files and native plugin resources through `source_dir`, and uses a separate editing Worker. These are extension branches, not replacements for the five runtime mechanisms. File lists are exact paths, not directory globs. Changing harnesses requires a matching resource directory and module mapping; neither is inferred from the harness name.

## Run a small example

Follow [Installation](./guide/installation.md) and configure the Target and Worker separately in [examples/modular_rsi.yaml](../examples/modular_rsi.yaml). Its Target and module mapping use MiniAgent. To use the bundled local SearchQA tasks instead of the example's Terminal-Bench tasks, run from the repository root:

```bash
flashrsi run --config examples/modular_rsi.yaml \
  --benchmark searchqa \
  --benchmark.data_path ./examples/data/quickstart.jsonl \
  --algorithm.num_epochs 1 \
  --algorithm.integration_epochs 1 \
  --algorithm.batch_size 1 \
  --algorithm.num_rollouts 2 \
  --algorithm.validation_samples 1
```

The example's `split_counts: {train: 1, dev: 1, test: 1}` remains in effect. This still evolves five branches and performs integration, so one epoch is not one Worker edit. Append `--check-only` for initialization without model calls or remote authentication validation. For native Terminal-Bench or SWE tasks, configure the corresponding dataset and execution environment; see [Terminal configuration](../examples/modular_rsi.yaml) and [SWE configuration](../examples/modular_rsi_swe_bench.yaml).

## Branch evolution and admission

1. Start each module branch from the same initial Snapshot and evolve them sequentially in configuration order. Branches do not inherit other modules' intermediate edits or success memories. One seeded random generator is shared across shuffles and execution-gate sampling.
2. Shuffle train each epoch and divide it into batches. Execute the current branch on each batch `num_rollouts` times. A trajectory is successful only if its status is `finished` and its primary metric reaches `success_threshold`.
3. Group evidence by task: mixed success/failure is `contrastive`, all successes is `positive`, and no success is `negative`. Negative tasks may receive the most recently stored successful trajectory from an earlier visit in this branch. Prompts ask for same-task contrasts, failure diagnosis, or efficiency improvements as appropriate.
4. Ask the Worker for findings. Merge findings aimed at the same file, count distinct supporting task IDs, sort by that count, and keep up to `edit_budget` file targets. Equal counts preserve first appearance in the reply. An empty findings list skips editing for that batch.
5. Make one directory-editing call for the selected findings. Check the actual changed paths, compile changed surviving `.py` files, and reject empty edits. Review the complete diff, including executable-bit changes; if it exceeds the evidence budget, decline rather than review a truncated patch.
6. If the Worker accepts the diff, execute the candidate once on up to `validation_samples` random tasks from this batch. Every returned trajectory must have status `finished`. Scores still exist, but the gate does not require correct answers or a higher mean.
7. Advance to the candidate only if these checks pass; otherwise keep the previous branch version. Accepted module edits enter that branch's history. Continue through all batches and epochs, then retain the last accepted version of each branch.

There is no score tie breaker: an equally scoring candidate can advance, and a lower-scoring candidate can also advance. `success_threshold` controls evidence grouping, not admission. The primary metric is not required to lie in `[0, 1]`; choose the threshold in that metric's units.

## Integration and final selection

After independent branch evolution, the Worker edits a fresh seed checkout while reading temporary checkouts of all final module branches and their accepted-edit histories. It combines changes in shared files through ordinary file editing; the framework does not splice functions or mechanically overwrite one branch with another. Reference directories are removed before committing.

The initial integration edit passes the same file, compilation, and diff-review gates. If it is declined, use the seed as the combined version. Then execute the combined version on **all train tasks**. A passing combined version becomes the fallback; otherwise the fallback is the seed.

Integration refinement starts from the combined version even if that full-train check failed, so the independent Worker can attempt repairs. It repeats contrastive batch evaluation for `integration_epochs`; each batch directly requests an integration edit without a separate findings-analysis call. Edits may touch the union of declared module files, still have access to the original branch checkouts, and pass diff review plus the sampled execution gate.

The final refined version must pass a full-train execution gate. If it fails, return the fallback. If the initially failing combined version never changed, skip repeating the same final gate and return the fallback directly. This is a fixed fallback order, not a comparison of train/dev scores or a search for the best intermediate integration version. The seed is not separately guaranteed runnable by this fallback mechanism.

## Evidence, reply formats, and failures

Findings must be a JSON array. Each item has exactly `target`, `task_ids`, `rationale`, and `change`; the target must be a declared module file, cited IDs must belong to the current batch, and rationale/change must be non-empty strings. The review must be exactly a JSON object with boolean `accept` and non-empty string `reason`. An outer Markdown JSON fence is tolerated by the parser, but prompts request plain JSON. Invalid JSON or schema errors abort the run rather than silently discarding findings.

Full contrast trajectories remain in experiment records and branch memory. When prompt evidence is too long, the algorithm retains each task's group, counts, and one recent example of each available polarity, plus historical success, then shortens text fields. If even that structure cannot fit, it raises an error. Normal history and findings blocks use the shared character limit. Integration receives module histories; it does not append integration edits to those module histories.

File/syntax failures and oversized diffs decline an edit; a valid review with `accept: false` also declines it. Worker failures propagate. Contrast evaluation catches `ValueError`, `TypeError`, `KeyError`, and `AttributeError` as batch-level failure evidence without claiming which task failed. Execution gates treat the same exceptions as failure; other exceptions can propagate. There is no immediate retry loop for a declined batch edit, though later batches can diagnose and edit again.

## Parameters and cost

Implementation defaults belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `modules` | `{}` | Required complete five-runtime-module or three-extension-module mapping to existing files; the empty default cannot run. |
| `num_epochs` | `3` | Epochs per independent module; integer, at least 1. |
| `batch_size` | `10` | Train tasks per batch; integer, at least 1. |
| `num_rollouts` | `3` | Repeated executions per task for contrast evidence; integer, at least 2. |
| `integration_epochs` | `1` | Combined-harness refinement epochs; integer, at least 1. |
| `validation_samples` | `2` | Distinct tasks sampled from the current batch for each candidate execution gate; integer, at least 1, capped at batch size. |
| `edit_budget` | `1` | Maximum prioritized file targets after finding consolidation; integer, at least 1. Not the number of edit attempts or a hard changed-file cap within the module. |
| `success_threshold` | `1.0` | Primary-metric threshold for successful contrast evidence. |
| `seed` | `42` | Integer seed for train shuffling and gate-task sampling, not model sampling. |
| `max_evidence_chars` | `60000` | Character limit per evidence block; integer, at least 1. Complete diff review must fit. |

Let M be the module count (5 or 3), N the train size, B = `ceil(N / batch_size)`, E = `num_epochs`, I = `integration_epochs`, R = `num_rollouts`, and V = `min(validation_samples, batch_size)`.

| Work | Target task executions / Worker calls |
| --- | --- |
| Contrast evaluation across all branches and integration | `(M * E + I) * N * R` Target executions on a completed run |
| Sampled candidate execution gates | At most `(M * E + I) * B * V` additional Target executions |
| Initial combined and final full-train gates | At most `2 * N` additional Target executions |
| Independent module batches | One analysis call each; at most one edit and one review call each |
| Initial combination | One edit call and, if file checks pass, one review call |
| Integration refinement batches | At most one edit and one review call each; no separate analysis call |

The last batch may contain fewer than V tasks. Empty findings and rejected edits reduce the gate and review counts. Final test, native model/tool turns, backend retries, and environment setup are additional. `edit_budget` only truncates prioritized findings; increasing it does not create multiple editing attempts per batch.

## Evolution data

The default uses the configured dataset's train split and does not require `metadata.evolution_source`. Tasks with different IDs may share policy text or environment directories. Benchmark loaders retain their own data checks; the algorithm itself enforces train-ID uniqueness and separation from dev/test IDs.

TerminalBench and SWEBench optionally accept `benchmark.evolution_data_path` and `evolution_limit`. These replace train with an external task collection while preserving the configured downstream dev/test splits. TerminalBench expects native Harbor tasks; SWEBench expects SWE records with its required task image, baseline commit, and scoring fields, not a Harbor directory. Both reuse native loading, execution, and grading.

Explicit external mode adds overlap checks for IDs, task text, and checksums; SWEBench also rejects shared task images. `evolution_limit` limits the external pool independently of the downstream `limit`. The examples use absolute dataset paths; when using relative paths, follow [Configuration & CLI](./guide/configuration.md) and the selected benchmark's path rules. No external source is needed for the local example above.

## Inspect an experiment

`contrast` records keep individual repeated evaluations. `contrast_groups` records task group labels, and `contrast_error` records a batch-level protocol failure. `review` records the model's acceptance and reason; `gate` records execution completion or the caught error. Decisions identify the module and whether an edit was admitted or declined. A review acceptance alone does not mean the subsequent execution gate passed.

Final `promote` records the selected candidate and a `modules` mapping from each module name to its final branch Snapshot ID. The selected integrated Snapshot may differ from every independent branch. Temporary reference directories are excluded from candidate resources.

## Source and adaptation

The [implementation](../flashrsi/algorithms/modular_rsi/__init__.py) pins [IQuestLab/ModularRSI](https://github.com/IQuestLab/ModularRSI/tree/b5c72c36b0d08ff93f00ee202a8fbdebe849dfb9) commit `b5c72c36b0d08ff93f00ee202a8fbdebe849dfb9`, referencing `src/harbor/agents/terminus_2_modular/self_evo/`. The local code and prompts are independently written around FlashRSI interfaces.

- Native harness files replace upstream's discoverable function library and task-aware runtime Function Composer. There is no function-level permission system, discovery-contract enforcement, or self-attribute audit.
- The three-extension-module mode supports harnesses such as Claude Code; it does not expose the source of the five runtime mechanisms.
- Branches run sequentially. Integration uses Worker file editing and review, including shared-file conflict resolution and possible removal of redundant functions; there is no separate Function Merge stage.
- The default permits same-source train/dev/test partitions instead of requiring upstream's independent evolution pool. Optional external data can be configured explicitly.
- Python checks compile changed source without importing it into the host. Diff review is a model judgment, not an independent anti-cheat proof. Execution and final selection follow the local gates above rather than dev-score optimization.
