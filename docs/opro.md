# OPRO

**English** · [简体中文](./opro.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/opro.yaml)

OPRO improves an instruction by showing the Worker a history of instructions and their scores. Every evaluated instruction remains in the search history. The Target's active Snapshot stays unchanged during search; only the final selection activates the best version.

## Requirements

The Target must declare an `instruction_file` belonging to its `prompt` component. OPRO replaces that file's complete text while retaining the surrounding Snapshot resources. The Worker can be a text LLM or an Agent returning text through `ask()`.

Train must be non-empty. **This implementation scores and selects on train**, without a dev gate. The runner owns final test evaluation; test feedback does not enter proposal generation.

## Run a small example

Follow [Installation](./guide/installation.md). Configure the Target and Worker models and credentials independently in [examples/quickstart.yaml](../examples/quickstart.yaml), then run from the repository root using its bundled local QA data:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm opro \
  --algorithm.max_steps 1 \
  --algorithm.num_candidates 2
```

Add `--check-only` to check initialization without model calls. This does not verify remote authentication. For a method-specific SearchQA configuration, see [examples/opro.yaml](../examples/opro.yaml); its default search budget is much larger. Model settings and CLI overrides are described in [Configuration & CLI](./guide/configuration.md).

## Search and selection

1. Evaluate the initial Snapshot on all train tasks and insert its instruction as the first history entry.
2. At each step, choose the highest-scoring history entry as the parent. Ties keep the earlier entry.
3. Build a prompt from the highest-scoring entries meeting `score_threshold`, displayed in ascending score order, and sampled task/output/metric evidence from the current train leader.
4. Make `num_candidates` Worker calls with that prompt. Parse `<INS>...</INS>` replies; discard empty and duplicate instructions without resampling. All proposals for this step are collected before evaluation.
5. Construct each novel instruction from the step's parent Snapshot and evaluate it on the complete train split. Append it to history, including lower-scoring candidates. A strictly better result becomes the evidence source for the next step.
6. After `max_steps`, promote the earliest global best instruction, including the initial version if it remains best.

The history threshold only filters what the Worker sees. It does not remove entries from the stored history or prevent them from winning final selection. Displayed scores are rounded after multiplying by 100; selection uses the original scores.

The reply parser takes the last complete `INS` tag pair; if none exists, it uses the whole reply as the instruction text.

## Parameters and cost

Fields below belong under `algorithm`. These are implementation defaults, not the small example's overrides.

| Field | Default | Meaning |
| --- | --- | --- |
| `max_steps` | `200` | Proposal steps; integer, at least 1. |
| `num_candidates` | `8` | Worker proposal calls per step; at least 1. |
| `history_size` | `20` | Maximum qualifying history entries shown to the Worker; at least 1. |
| `score_threshold` | `0.1` | Minimum raw score for inclusion in the displayed history. |
| `few_shot_size` | `3` | Sampled leader trajectories per prompt, capped by train size; at least 1. |
| `max_evidence_chars` | `24000` | Character limit for the serialized evidence block. |
| `seed` | `42` | Integer seed; evidence sampling uses `seed + step`. |

For train size N, S steps, and C proposals per step, search performs at most `(1 + S * C) * N` Target task executions, including the baseline. Empty or duplicate proposals reduce evaluations. Worker proposal calls total `S * C` when execution completes normally. Final test, native tool/model turns, and any backend retries are additional costs.

## Inspect an experiment

The final `promote` decision includes `score`, `baseline_score`, and the full `history` of instruction text, score, and Snapshot ID. Pair these with `baseline` and `candidate` task trajectories. A history entry is an evaluated proposal, not necessarily an activated version; no intermediate promotion occurs.

## Source and adaptation

The [prompt module](../flashrsi/algorithms/opro/_prompts.py) pins `google-deepmind/opro` to commit `a76bdce2cbf6d4a0d1e570a6fcfe17be9c2abdd7`. The local port uses one native instruction file rather than upstream question-insertion positions, and supplies sampled task outputs and metrics from FlashRSI trajectories. See the [search implementation](../flashrsi/algorithms/opro/__init__.py) for the current behavior.

## Validation scope

The command above is a small-run recipe, not a reported experiment result. Initialization does not validate proposal quality or full evolution. A useful smoke records whether novel instructions were produced, train evaluation completed, and final promotion followed the scored history. A better train score alone does not establish held-out improvement; report final test separately.
