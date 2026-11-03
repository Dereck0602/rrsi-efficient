# GEPA

**English** · [简体中文](./gepa.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/gepa.yaml)

GEPA reflects on a parent's training trajectories, screens a rewritten instruction on the same tasks, and retains candidates with per-task dev scores. Parent selection can revisit specialists that lead on individual dev tasks, even when they do not have the best mean score. Final selection uses the highest mean dev score, keeping the earlier candidate on a tie.

## Requirements

The Target must declare an `instruction_file` in its `prompt` component. The Worker can be a text LLM or an Agent returning a rewritten instruction through `ask()`.

Train and dev must be non-empty, and dev task IDs must be unique. `use_merge: true` is rejected at startup: system-aware merge is not implemented. The runner owns final test evaluation.

## Run a small example

Follow [Installation](./guide/installation.md) and independently configure both roles' model settings in [examples/quickstart.yaml](../examples/quickstart.yaml). From the repository root:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm gepa \
  --algorithm.num_epochs 1 \
  --algorithm.reflection_minibatch_size 2 \
  --algorithm.max_metric_calls 10
```

The bundled split has six train and three dev tasks. The budget allows baseline dev plus one worst-case iteration: `3 + 2 * 2 + 3 = 10` task evaluations. Early rejection can leave room for more iterations. Append `--check-only` for initialization only. GEPA uses `max_metric_calls` and `num_epochs`, not the quickstart's `max_steps` field.

For HotpotQA context-mode data, start with [examples/gepa.yaml](../examples/gepa.yaml) and its dataset prerequisites. See [Configuration & CLI](./guide/configuration.md) for connection settings.

## Search and selection

1. Evaluate the initial Snapshot on dev and store both its mean and per-task scores in the population.
2. Traverse shuffled train minibatches. Select the aggregate best parent, or sample from the per-task frontier according to `candidate_selection_strategy`.
3. Evaluate that parent on the minibatch. By default, exclude trajectories scoring exactly `1.0` from reflection evidence; if none remain, reject the iteration as `perfect_minibatch`.
4. Ask the Worker to rewrite the instruction using the parent's outputs, labeled feedback, and execution messages. Reject empty, unchanged, or already-seen instructions; duplicate comparison normalizes line endings and trailing newlines.
5. Evaluate the candidate on the same minibatch. By default it must strictly beat the parent. `improvement_or_equal` also lets ties pass this screen.
6. Evaluate a passing candidate on full dev and append it to the population, even if its mean dev score is lower. Admission is not final promotion.
7. When the task-evaluation budget or epoch limit ends search, promote the earliest population member with the highest mean dev score.

For Pareto selection, the implementation maps each dev task to its highest-scoring members, prunes redundant members while retaining coverage, and samples from the remaining memberships. A member leading on more tasks receives more sampling weight. This frontier determines parent eligibility; the full evaluated population remains recorded.

## Parameters and cost

All fields belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `max_metric_calls` | `null` | Target task-evaluation budget; positive integer or automatic. |
| `num_epochs` | `null` | Train traversal limit; positive integer or budget-only search. |
| `reflection_minibatch_size` | `3` | Minibatch size, capped by train size; at least 1. |
| `skip_perfect_score` | `true` | Exclude score-1.0 trajectories from reflection. |
| `acceptance_criterion` | `strict_improvement` | Minibatch screen: `strict_improvement` or `improvement_or_equal`. |
| `candidate_selection_strategy` | `pareto` | Parent policy: `pareto` or `current_best`. |
| `max_evidence_chars` | `24000` | Character limit for each serialized evidence block. |
| `seed` | `42` | Integer seed for training order and parent sampling. |
| `use_merge` | `false` | Must remain false; merge is unsupported. |

Let N be train size, D dev size, and B = `min(reflection_minibatch_size, N)`. Baseline costs D evaluations; a full iteration costs `2 * B + D`. Startup requires enough budget for both. Before each iteration, the algorithm reserves room for the full cost, then counts only evaluations actually performed.

If no explicit budget is supplied, it is `max(100, D + I * (2 * B + D))`. I is 10 for budget-only search, or `num_epochs * (N + B - N % B) / B` when epochs are specified. Shuffled epochs are padded with least-used task indices, including an extra full minibatch when N divides evenly by B. Thus automatic budgeting does not promise exactly ten executed iterations.

Worker calls, native model/tool turns, and final test are outside this task-evaluation budget. Allowing minibatch ties does not change final dev tie handling.

## Inspect an experiment

Iteration decisions distinguish `reject` from `admit`. Rejection reasons include `perfect_minibatch`, `empty_proposal`, `unchanged_instruction`, `duplicate_instruction`, and `failed_minibatch_screen`. An `admit` record's `accepted: true` means population admission, not adoption as the final Target.

The final `promote` record includes `metric_calls`, `iterations`, `acceptance_criterion`, `tie_break`, and the population with per-task scores and parent indices. Use it to identify the selected version and actual evaluation budget.

## Source and adaptation

The [search module](../flashrsi/algorithms/gepa/__init__.py) pins the sampling reference `gepa-ai/gepa-artifact` to `cbefbc1aa0f43dd39874ec4bf42211365dbda42e`. The [population module](../flashrsi/algorithms/gepa/_population.py) pins `gepa-ai/gepa` to `0632cdb5dcc052e690eab439e1b4a7e3e9cfe407`.

This port rewrites one harness instruction file, omits compound-system module rotation, and does not implement system-aware merge. Reflection uses FlashRSI training feedback and trajectories; the [prompts](../flashrsi/algorithms/gepa/_prompts.py) define the requested reply format.
